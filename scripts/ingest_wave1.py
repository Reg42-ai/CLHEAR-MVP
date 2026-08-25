"""Run the eToro Wave-1 ingestion plan through the fleet.

Every class-A registry source (existing adapters, parameter reuse) is fetched
live, parsed to a DocNode tree, and pushed through the fidelity gate + repair
loop. Failures are isolated per source: a crash or gate exhaustion records a
failed run + IngestFidelityFailed event and the batch continues. Ends with an
honest per-source report.

Usage:
    DATABASE_URL=sqlite:///deploy/clhear.db CLHEAR_HTTP_MODE=live \
    CLHEAR_ARTIFACT_STORE=s3 python scripts/ingest_wave1.py [--adapter eur_lex] [--only KEY]
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clhear.db import get_engine, run_migrations  # noqa: E402
from app.clhear.l1 import pipeline, registry_etoro  # noqa: E402
from app.clhear.platform.gateway import AnthropicProvider, Gateway  # noqa: E402
from app.clhear.settings import get_settings  # noqa: E402

log = logging.getLogger("clhear.wave1")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", default=None, help="only this adapter key (eur_lex | uk_legislation)")
    parser.add_argument("--only", default=None, help="only this source key")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    engine = get_engine()
    run_migrations(engine)

    # Blueprint rows first so every ingest lands on a seeded source.
    registry_etoro.seed(engine)

    if os.environ.get("CLHEAR_ARTIFACT_STORE") == "s3":
        store: pipeline.ArtifactStore = pipeline.S3Store(settings.clhear_datalake_bucket, settings.aws_region)
    else:
        store = pipeline.LocalStore(settings.clhear_artifacts_dir)
    gateway = Gateway(engine, AnthropicProvider()) if settings.anthropic_api_key else None

    job_id = f"job-wave1-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    plan = registry_etoro.wave1_adapters(args.adapter)
    if args.only:
        plan = [(e, a) for e, a in plan if e["key"] == args.only]

    results = []
    for entry, adapter in plan:
        key = entry["key"]
        try:
            summary = pipeline.ingest(
                engine, adapter, store, trigger="wave1", gateway=gateway, job_id=job_id
            )
            results.append({"key": key, **{k: summary.get(k) for k in ("status", "version", "coverage", "clauses")}})
        except Exception as exc:  # isolate: one bad source never kills the batch
            log.exception("wave1 ingest crashed for %s", key)
            recorder = pipeline.RunRecorder(
                engine, f"l1.{entry['adapter']}", "wave1", {"source": key, "job_id": job_id}
            )
            recorder.finish("failed", {"source": key, "error": f"{type(exc).__name__}: {exc}"[:300]})
            results.append({"key": key, "status": "crashed", "error": f"{type(exc).__name__}: {exc}"[:200]})

    ok = [r for r in results if r["status"] in ("added", "amended", "unchanged", "up-to-date")]
    bad = [r for r in results if r not in ok]
    print(json.dumps({"job_id": job_id, "total": len(results), "ok": len(ok), "failed": len(bad), "results": results}, indent=1, default=str))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
