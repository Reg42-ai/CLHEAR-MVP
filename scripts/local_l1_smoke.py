"""Local live L1 smoke: one fetchable document per lane plus full bulk lanes.

Runs the real ``run_adapter_fleet`` path against disposable PostgreSQL with
``CLHEAR_HTTP_MODE=live`` and discovery off. Acceptance evals are stubbed so
the report measures fetch/parse/permission crashers, not release gates.

Usage:
    DATABASE_URL=postgresql+psycopg://… CLHEAR_HTTP_MODE=live \\
        python scripts/local_l1_smoke.py --report /tmp/clhear-smoke-report.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FULL_LANES = ("eur_lex", "uk_legislation", "fca_handbook")
PREFERRED = {
    "finra": "finra/rule/2111",
    "govinfo_us": "nist/csf-2.0",
    "lists": "lists/un-consolidated",
    "restricted_file": "iso/27001-2022",
    "asic": "au/asic-rg227",
    "esma": "esma/guidelines/suitability",
    "au_legislation": "au/privacy-act-1988",
    "bis_basel": "bis/basel/CRE20",
    "malta": "mt/cap376",
    "mas": "sg/mas-aml-sfa04-n02",
    "sec_edgar": "sec/release/34-86031",
    "sg_legislation": "sg/pdpa-2012",
}
POC_PERMS = {
    "acquire": True, "store": True, "parse": True, "display_internal": True,
    "display_public": False,
}

log = logging.getLogger("clhear.smoke")


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_jsonable(payload), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _pick(lane: str) -> tuple[list[str] | None, str | None]:
    from app.clhear.l1.fleet import fleet_plan

    plan = [(entry, adapter) for entry, adapter in fleet_plan(lane)
            if adapter.meta().source_key != "finra/rulebook"]
    if not plan:
        return None, "empty_lane"
    if lane in FULL_LANES:
        return None, None
    preferred = PREFERRED.get(lane)
    keyed = {adapter.meta().source_key: (entry, adapter) for entry, adapter in plan}
    if preferred in keyed:
        return [preferred], None
    fetchable = [adapter.meta().source_key for entry, adapter in plan
                 if not getattr(adapter, "declaration_gap", None)]
    if fetchable:
        return [sorted(fetchable)[0]], None
    return [sorted(keyed)[0]], "declaration_gap_only"


def _grant(engine) -> list[str]:
    from app.clhear.l1.permissions import record_permission, required_for
    from app.clhear.l1.source_registry import S

    granted = []
    for entry in S:
        if not required_for(entry):
            continue
        record_permission(
            engine, source_key=entry["key"], permissions=POC_PERMS,
            evidence_ref="local-smoke:private-poc", approved_by="owner: private POC test environment",
            approved=True,
        )
        granted.append(entry["key"])
    return granted


def _stub_gates() -> None:
    """Skip release evals; the smoke is about import crashers."""
    from app.clhear import workers
    from app.clhear.l1 import inventory
    from app.clhear.platform import evals

    inventory.acceptance_status = lambda *a, **k: {
        "passed": True, "inventory_hash": k.get("inventory_hash"), "reasons": [],
    }
    evals.run_suite = lambda *a, **k: {"passed": True, "scores": {}, "suite": k.get("suite", a[1] if len(a) > 1 else "smoke")}
    evals.run_source_evals = lambda *a, **k: [{"passed": True, "suite": "smoke"}]
    workers._put_schedule_metric = lambda *a, **k: None


def _tasks(engine, job_id: str) -> list[dict]:
    import sqlalchemy as sa
    from app.clhear.l1 import workflow

    with engine.connect() as conn:
        rows = list(conn.execute(sa.select(workflow.tasks).where(workflow.tasks.c.job_id == job_id)
                                 .order_by(workflow.tasks.c.source_key)).mappings())
    out = []
    for row in rows:
        summary = row["summary"] or {}
        started, finished = row["started_at"], row["finished_at"]
        duration_s = None
        if started and finished:
            duration_s = round((finished - started).total_seconds(), 2)
        failure = summary.get("failure") if isinstance(summary, dict) else None
        out.append({
            "source_key": row["source_key"],
            "task_status": row["status"],
            "import_status": summary.get("status") if isinstance(summary, dict) else None,
            "error": row["error"],
            "error_type": (failure or {}).get("error_type") if isinstance(failure, dict) else None,
            "error_code": (failure or {}).get("error_code") if isinstance(failure, dict) else None,
            "sqlstate": (failure or {}).get("sqlstate") if isinstance(failure, dict) else None,
            "declaration_gap": summary.get("declaration_gap") if isinstance(summary, dict) else None,
            "duration_s": duration_s,
            "summary": {k: summary.get(k) for k in (
                "status", "source", "version", "clauses", "error", "error_type", "error_code", "sqlstate"
            )} if isinstance(summary, dict) else summary,
        })
    return out


def _run_lane(engine, lane: str, source_keys: list[str] | None) -> dict:
    from app.clhear import workers
    from app.clhear.l1 import workflow

    event_key = str(uuid.uuid4())
    job_id = workflow.job_id_for(event_key, lane)
    started = time.monotonic()
    result, error, crash = {}, None, None
    try:
        result = workers.run_adapter_fleet(
            engine, lane, gateway=None, event_key=event_key, trigger="manual",
            source_keys=source_keys, discover=False,
        )
    except workers.AdapterRunIncomplete as exc:
        error = str(exc)
        try:
            import sqlalchemy as sa
            with engine.connect() as conn:
                result = conn.execute(sa.select(workflow.jobs.c.summary)
                                      .where(workflow.jobs.c.job_id == job_id)).scalar_one() or {}
        except Exception:
            result = {"incomplete": True}
        result = {**result, "incomplete": True}
    except Exception as exc:
        crash = f"{type(exc).__name__}: {exc}"
        log.exception("lane %s crashed", lane)
        error = crash
        result = {"crashed": True, "traceback": traceback.format_exc()[-4000:]}
    tasks = []
    try:
        tasks = _tasks(engine, job_id)
    except Exception:
        log.exception("could not read tasks for %s", job_id)
    return {
        "lane": lane,
        "job_id": job_id,
        "source_keys": source_keys,
        "duration_s": round(time.monotonic() - started, 2),
        "error": error,
        "crash": crash,
        "result": {k: result.get(k) for k in (
            "adapter", "ran", "statuses", "failures", "acceptance", "incomplete", "crashed"
        )} if isinstance(result, dict) else {"raw": result},
        "tasks": tasks,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="/tmp/clhear-smoke-report.json")
    parser.add_argument("--lanes", default="", help="comma-separated lane filter")
    parser.add_argument("--skip-full", action="store_true")
    parser.add_argument("--only-full", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.environ.setdefault("CLHEAR_HTTP_MODE", "live")
    os.environ.setdefault("CLHEAR_LLM_PROVIDER", "fake")
    os.environ.setdefault("CLHEAR_FLEET", "all")
    os.environ.setdefault("CLHEAR_ARTIFACTS_DIR", "/tmp/clhear-smoke-artifacts")
    os.environ.pop("CLHEAR_ARTIFACT_STORE", None)
    os.environ.pop("CLHEAR_SNAPSHOT_S3_URI", None)

    from app.clhear.db import dispose_engine, get_engine, run_migrations
    from app.clhear.l1 import cycles, registry_etoro
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    dispose_engine()
    settings = get_settings()
    if not str(settings.database_url).startswith("postgresql"):
        raise SystemExit("DATABASE_URL must be PostgreSQL for this smoke")

    engine = get_engine()
    applied = run_migrations(engine)
    registry_etoro.seed(engine)
    granted = _grant(engine)
    _stub_gates()

    wanted = [k.strip() for k in args.lanes.split(",") if k.strip()] or list(cycles.adapter_keys())
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "database_url_host": "127.0.0.1",
        "http_mode": os.environ.get("CLHEAR_HTTP_MODE"),
        "artifacts_dir": os.environ.get("CLHEAR_ARTIFACTS_DIR"),
        "migrations": applied,
        "permissions_granted": granted,
        "lanes": {},
        "finished_at": None,
    }
    path = Path(args.report)
    _write(path, report)

    for lane in wanted:
        if args.skip_full and lane in FULL_LANES:
            report["lanes"][lane] = {"skipped": True, "reason": "skip_full"}
            _write(path, report)
            continue
        if args.only_full and lane not in FULL_LANES:
            continue
        keys, reason = _pick(lane)
        if reason == "empty_lane":
            report["lanes"][lane] = {"skipped": True, "reason": "empty_fleet_plan"}
            log.info("lane %s: empty (expected for enforcement)", lane)
            _write(path, report)
            continue
        log.info("lane %s keys=%s", lane, keys if keys else "ALL")
        row = _run_lane(engine, lane, keys)
        if reason:
            row["pick_reason"] = reason
        report["lanes"][lane] = row
        statuses = {t["import_status"]: 0 for t in row.get("tasks", [])}
        for task in row.get("tasks", []):
            statuses[task["import_status"]] = statuses.get(task["import_status"], 0) + 1
        log.info("lane %s done in %ss statuses=%s crash=%s",
                 lane, row["duration_s"], statuses or row.get("result"), row.get("crash"))
        _write(path, report)

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    counts = {"lanes": 0, "imported": 0, "blocked": 0, "failed": 0, "crashed": 0, "skipped": 0}
    for row in report["lanes"].values():
        if row.get("skipped"):
            counts["skipped"] += 1
            continue
        counts["lanes"] += 1
        if row.get("crash"):
            counts["crashed"] += 1
        for task in row.get("tasks", []):
            status = task.get("import_status") or task.get("task_status")
            if status in {"added", "amended", "unchanged", "up-to-date"}:
                counts["imported"] += 1
            elif status in {"rights-blocked", "source-blocked", "awaiting-artifact", "blocked"}:
                counts["blocked"] += 1
            elif status in {"failed", "crashed"}:
                counts["failed"] += 1
    report["counts"] = counts
    _write(path, report)
    print(json.dumps({"report": str(path), **counts}, indent=2))
    return 1 if counts["crashed"] or counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
