"""Ingest the original starter adapters into an existing corpus DB.

Keeps GDPR, UK MLRs (as-made + historical + current), FATCA, and NIST next
to the eToro Wave-1 sources so the library still shows the live originals.
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clhear.db import get_engine, run_migrations  # noqa: E402
from app.clhear.l1 import families, pipeline  # noqa: E402
from app.clhear.l1.adapters import ADAPTER_KEYS, CITATOR_KEYS, get_adapter  # noqa: E402
from app.clhear.l1.adapters.eur_lex import EurLexAdapter  # noqa: E402
from app.clhear.l1.adapters.uk_legislation import UkLegislationAdapter  # noqa: E402
from app.clhear.platform.gateway import AnthropicProvider, Gateway  # noqa: E402
from app.clhear.settings import get_settings  # noqa: E402

MLR_HISTORICAL_SNAPSHOT = "2020-01-09"
GDPR_OJ_ORIGINAL = "32016R0679"


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    engine = get_engine()
    run_migrations(engine)
    if os.environ.get("CLHEAR_ARTIFACT_STORE") == "s3":
        store: pipeline.ArtifactStore = pipeline.S3Store(settings.clhear_datalake_bucket, settings.aws_region)
    else:
        store = pipeline.LocalStore(settings.clhear_artifacts_dir)
    gateway = Gateway(engine, AnthropicProvider()) if settings.anthropic_api_key else None
    job_id = f"job-starter-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    adapters = [
        UkLegislationAdapter(as_made=True),
        UkLegislationAdapter(snapshot=MLR_HISTORICAL_SNAPSHOT),
        EurLexAdapter(celex_version=GDPR_OJ_ORIGINAL),
        *[get_adapter(k) for k in ADAPTER_KEYS],
    ]
    results = []
    for adapter in adapters:
        key = adapter.meta().source_key
        try:
            summary = pipeline.ingest(engine, adapter, store, trigger="starter", gateway=gateway, job_id=job_id)
            results.append({"key": key, "status": summary.get("status"), "version": summary.get("version")})
            if adapter.key in CITATOR_KEYS and not getattr(adapter, "as_made", False) and not getattr(adapter, "snapshot", None):
                families.sync_citator(engine, adapter, trigger="starter", job_id=job_id)
        except Exception as exc:
            logging.exception("starter ingest crashed for %s", key)
            results.append({"key": key, "status": "crashed", "error": f"{type(exc).__name__}: {exc}"[:200]})
    print(json.dumps({"job_id": job_id, "results": results}, indent=1, default=str))
    return 0 if all(r["status"] not in ("crashed", "not-fully-successful") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
