"""Evals harness (HLD §7.1): suites registered per layer; runs recorded in
l0_platform.eval_runs plus a JSON artifact; release gate = all suites passed.

Evals are gates, not reports (HLD principle 5).
"""
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import eval_runs
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.evals")

# suite name -> callable(engine, source_key) -> (scores: dict, passed: bool)
SUITES: dict[str, Callable] = {}


def register_suite(name: str):
    def wrap(fn: Callable):
        SUITES[name] = fn
        return fn

    return wrap


@register_suite("l0_smoke")
def l0_smoke(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """P0 skeleton suite: the l0_platform tables exist and are queryable."""
    from app.clhear.models import events, llm_calls, proposals, runs

    counts = {}
    with engine.connect() as conn:
        for table in (events, proposals, llm_calls, runs):
            counts[table.name] = conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
    return {"tables_queryable": len(counts), "row_counts": counts}, True


@register_suite("l1_fidelity")
def l1_fidelity(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """E2/E3 skeleton (pulled forward from P4): every registry adapter's parse
    must cover its own oracle text at >= the gate threshold with zero contract
    violations. Runs offline against recorded fixtures in CI; blocks releases
    via the existing release gate."""
    from app.clhear.l1 import fidelity
    from app.clhear.l1.adapters import ADAPTER_KEYS, get_adapter

    settings = get_settings()
    threshold = settings.clhear_fidelity_threshold
    scores: dict = {}
    passed = True
    for key in ADAPTER_KEYS:
        if source_key and key != source_key:
            continue
        adapter = get_adapter(key)
        try:
            result = adapter.fetch()
            report = fidelity.check(result.tree, adapter.expected_text(result.artifacts))
            ok = report.ok(threshold)
            scores[key] = {
                "coverage": round(report.coverage, 5),
                "violations": len(report.violations),
                "passed": ok,
            }
            passed = passed and ok
        except Exception as exc:
            scores[key] = {"error": str(exc)[:200], "passed": False}
            passed = False
    return {"threshold": threshold, "adapters": scores}, passed


def run_suite(engine: Engine, suite: str, source_key: str | None = None, release: str | None = None) -> dict:
    """Run one suite -> eval_runs row + JSON artifact. Returns the record."""
    fn = SUITES[suite]
    scores, passed = fn(engine, source_key)
    record = {
        "suite": suite,
        "source_key": source_key,
        "release": release,
        "scores": scores,
        "passed": passed,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    with engine.begin() as conn:
        conn.execute(
            eval_runs.insert().values(
                suite=suite, source_key=source_key, release=release, scores=scores, passed=passed
            )
        )
    artifacts_dir = Path(get_settings().clhear_artifacts_dir) / "evals"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact = artifacts_dir / f"{suite}{'-' + source_key if source_key else ''}-{stamp}.json"
    artifact.write_text(json.dumps(record, indent=2, default=str))
    log.info("suite %s passed=%s -> %s", suite, passed, artifact)
    return record


def run_all(engine: Engine, release: str | None = None) -> list[dict]:
    return [run_suite(engine, suite, release=release) for suite in sorted(SUITES)]


def release_gate(engine: Engine, release: str) -> bool:
    """True iff every recorded eval run for this release passed and none is missing."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(eval_runs.c.suite, eval_runs.c.passed).where(eval_runs.c.release == release)
        ).all()
    ran = {row.suite for row in rows}
    return bool(rows) and all(row.passed for row in rows) and set(SUITES) <= ran


def main() -> int:
    """CLI: python -m app.clhear.platform.evals [suite|all] [release]."""
    from app.clhear.db import get_engine, run_migrations

    engine = get_engine()
    run_migrations(engine)
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    release = sys.argv[2] if len(sys.argv) > 2 else None
    records = run_all(engine, release) if target == "all" else [run_suite(engine, target, release=release)]
    print(json.dumps(records, indent=2, default=str))
    return 0 if all(r["passed"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
