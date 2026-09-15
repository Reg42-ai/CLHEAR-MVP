"""One-off deployment checks executed by the owning CLHEAR fleets.

L0 bootstraps and publishes the private viewer; L1 audits, imports and repeats
FINRA through its ordinary envelope handlers. This module never releases held
downstream events, grants permissions, or promotes an accepted release.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

import sqlalchemy as sa

from app.clhear.l1 import workflow
from app.clhear.models import runs
from app.clhear.platform.events import Envelope

PHASE_FLEETS = {"bootstrap": "l0", "verify": "l1", "publish": "l0"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _now():
    return datetime.now(timezone.utc).isoformat()


def phase_job_id(verification_id, phase):
    return workflow.job_id_for(f"deployment:{verification_id}", f"deployment.{phase}")


def _job(engine, job_id):
    with engine.connect() as conn:
        return conn.execute(sa.select(workflow.jobs).where(workflow.jobs.c.job_id == job_id)).mappings().first()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _finra_state(engine):
    """Metadata only: a repeat must preserve historical as well as current IDs."""
    from app.clhear.l1.models import source_versions, sources
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(sa.select(
            sources.c.key.label("source_key"), source_versions.c.id.label("source_version_id"),
            source_versions.c.content_hash, source_versions.c.status,
        ).join(source_versions, source_versions.c.source_id == sources.c.id)
            .where(sources.c.key.startswith("finra/")).order_by(sources.c.key, source_versions.c.id)).mappings()]
    return {"version_count": len(rows), "bindings_hash": _digest(rows), "bindings": rows}


def _dispatch(engine, gateway, verification_id, step, kind, payload, layer, subject):
    from app.clhear import workers
    envelope = Envelope(event_id=f"deployment:{verification_id}:{step}", layer=layer, kind=kind,
                        subject_ref=subject, payload=payload, producer="deployment.worker", ts=_now())
    result = workers.handle_envelope(engine, gateway, envelope.model_dump_json())
    if result is not None:
        return result
    # A recovered one-off task reuses successfully completed child events.
    # It must not turn deduplication into a fictitious new run or empty success.
    consumer = f"fleet.{os.environ['CLHEAR_FLEET'].lower()}:{kind}"
    with engine.connect() as conn:
        result = conn.execute(sa.select(runs.c.outputs).where(
            runs.c.fleet == "worker", runs.c.inputs["consumer"].as_string() == consumer,
            runs.c.inputs["event_key"].as_string() == workers.delivery_event_key(envelope),
        ).order_by(runs.c.id.desc()).limit(1)).scalar_one_or_none()
    if result is None:
        raise RuntimeError("Completed child event is missing its persisted result")
    return result


def _adapter_step(engine, gateway, verification_id, step):
    from app.clhear import workers
    child_id = workflow.job_id_for(f"deployment:{verification_id}:{step}", "finra")
    incomplete = False
    try:
        _dispatch(engine, gateway, verification_id, step, "AdapterRunRequested",
                  {"adapter": "finra", "job_id": child_id}, "l1", "finra")
    except workers.AdapterRunIncomplete:
        # Permission/coverage holds are expected review evidence, not acceptance.
        incomplete = True
    job = _job(engine, child_id)
    if job is None:
        raise RuntimeError("Adapter execution is missing its durable job evidence")
    # The viewer summary paginates tasks. Operational reconciliation must read
    # the entire frozen job, including failures beyond its first 200 sources.
    with engine.connect() as conn:
        tasks = list(conn.execute(sa.select(workflow.tasks).where(workflow.tasks.c.job_id == child_id)).mappings())
    successful = sorted(task["source_key"] for task in tasks if task["status"] == "completed"
                        and (task.get("summary") or {}).get("status") in {"added", "amended", "unchanged", "up-to-date"})
    failed = [task["source_key"] for task in tasks if task["status"] not in {"completed", "blocked"}]
    summary = job.get("summary") or {}
    return {"job_id": child_id, "status": job["status"], "incomplete": incomplete,
            "acceptance": summary.get("acceptance", "awaiting_verification"),
            "inventory_audit_id": summary.get("inventory_audit_id"),
            "source_tasks": len(tasks), "successful_sources": successful, "failed_sources": failed,
            "statuses": summary.get("statuses", {}),
            "execution_failed": bool(failed) or job["status"] == "failed"}


def run_phase(engine, gateway, phase, verification_id, *, bootstrap=None):
    """Run one phase on its actual fleet; tests may supply a migrated SQLite DB."""
    from app.clhear import workers
    from app.clhear.settings import get_settings
    if phase not in PHASE_FLEETS or not _IDENTIFIER.fullmatch(verification_id or ""):
        raise ValueError("Invalid deployment verification identity or phase")
    fleet = os.environ.get("CLHEAR_FLEET", "").lower()
    if fleet != PHASE_FLEETS[phase]:
        raise workers.WrongFleet(f"Deployment {phase} requires {PHASE_FLEETS[phase]}")
    if not get_settings().clhear_l1_only:
        raise workers.L1AcceptanceHold("Deployment verification requires the downstream hold")
    job_id = phase_job_id(verification_id, phase)
    event_key = f"deployment:{verification_id}:{phase}"
    existing = workflow.ensure_job(engine, job_id, f"deployment.{phase}", "deployment", event_key)
    # Completed checks are immutable. A new deployment/check uses a new ID.
    previous = existing.get("summary") or {}
    if existing["status"] in {"verified", "review_ready", "succeeded"} and previous.get("result"):
        return previous["result"]
    token = workflow.claim_delivery(engine, f"deployment.{phase}", event_key)
    if token is None:
        raise RuntimeError("Completed deployment phase is missing its result")
    started = time.monotonic()
    result = {"verification_id": verification_id, "phase": phase, "job_id": job_id,
              "worker": fleet, "status": "running", "accepted_release": False,
              "downstream": "held", "evidence_mode": "manual_deployment_verification",
              "nightly_schedule_validation": "pending", "started_at": _now(), "steps": {}}
    if bootstrap:
        result["steps"]["worker_migrations"] = bootstrap
    workflow.update_job(engine, job_id, "running", {
        "bootstrap": previous.get("bootstrap") or bootstrap or {},
        "bootstrap_attempts": [*(previous.get("bootstrap_attempts") or []), *([bootstrap] if bootstrap else [])],
        "result": result,
    })
    try:
        with workflow.heartbeat(lambda: workflow.heartbeat_delivery(engine, f"deployment.{phase}", event_key, token)):
            with workflow.bind_execution(engine, job_id):
                if phase in {"bootstrap", "publish"}:
                    if phase == "bootstrap":
                        from app.clhear.l1 import registry_etoro
                        with workflow.stage("registry_bootstrap", {"worker": "l0", "operation": "registered_source_metadata"}):
                            registry_etoro.seed(engine)
                    with workflow.stage("viewer_snapshot", {"worker": "l0", "accepted": False}) as stage:
                        snapshot = _dispatch(engine, gateway, verification_id, f"{phase}.snapshot", "ViewerSnapshotRequested",
                                             {"job_id": job_id}, "l0", "viewer/current")
                        summary = {key: snapshot.get(key) for key in (
                            "revision", "sha256", "byte_count", "snapshot_uri", "source_environment", "accepted_release")}
                        if not summary["revision"] or not summary["sha256"] or not summary["byte_count"]:
                            raise RuntimeError("Viewer publication did not return verified snapshot evidence")
                        stage.details.update(summary)
                        result["steps"]["viewer_snapshot"] = summary
                    result.update(status="succeeded", exit_code=0)
                else:
                    with workflow.stage("inventory_audit", {"worker": "l1"}) as stage:
                        audit = _dispatch(engine, gateway, verification_id, "audit", "L1InventoryAuditRequested",
                                          {"scope": "finra", "discover": False}, "l1", "finra")
                        audit_summary = {key: audit.get(key) for key in ("audit_id", "status", "inventory_hash", "verified", "unresolved")}
                        stage.details.update(audit_summary)
                        result["steps"]["inventory_audit"] = audit_summary
                    with workflow.stage("finra_import", {"worker": "l1"}) as stage:
                        first = _adapter_step(engine, gateway, verification_id, "finra.first")
                        stage.details.update(first)
                        if first["incomplete"]:
                            stage.status = "blocked"
                        result["steps"]["finra_import"] = first
                    if first["execution_failed"]:
                        raise RuntimeError("Initial FINRA source execution failed; repeat requires recovery first")
                    # Preserve the first result across a crash after the repeat.
                    frozen = (_job(engine, job_id)["summary"] or {}).get("before_repeat")
                    if frozen is None:
                        frozen = _finra_state(engine)
                        workflow.update_job(engine, job_id, "running", {"before_repeat": frozen})
                    with workflow.stage("finra_repeat", {"worker": "l1"}) as stage:
                        repeat = _adapter_step(engine, gateway, verification_id, "finra.repeat")
                        stage.details.update(repeat)
                        if repeat["incomplete"]:
                            stage.status = "blocked"
                        result["steps"]["finra_repeat"] = repeat
                    after = _finra_state(engine)
                    successful = bool(frozen["version_count"]) and bool(first["successful_sources"]) and first["successful_sources"] == repeat["successful_sources"]
                    same = frozen["bindings_hash"] == after["bindings_hash"]
                    unchanged = successful and same and all(status in {"unchanged", "up-to-date", "rights-blocked"}
                                                            for status in repeat["statuses"])
                    result["steps"]["unchanged_check"] = {
                        "passed": unchanged, "bindings_unchanged": same,
                        "successful_source_count": len(repeat["successful_sources"]),
                        "version_count_before": frozen["version_count"], "version_count_after": after["version_count"],
                        "before_bindings_hash": frozen["bindings_hash"], "after_bindings_hash": after["bindings_hash"],
                        "reason": "verified_successful_source_repeat" if unchanged else "repeat_not_verified",
                    }
                    accepted_candidate = all(row["acceptance"] == "candidate_verified" for row in (first, repeat))
                    failed = first["execution_failed"] or repeat["execution_failed"]
                    result.update(status="failed" if failed else "verified" if accepted_candidate and unchanged else "review_ready",
                                  exit_code=1 if failed else 0 if accepted_candidate and unchanged else 2,
                                  acceptance="finra_candidate_verified" if accepted_candidate and unchanged else "awaiting_verification")
    except Exception as exc:
        # Exception messages may contain SQL, URLs or source excerpts. Persist
        # only the type in this deployment result; owning tasks retain details.
        result.update(status="failed", exit_code=1, error_type=type(exc).__name__)
    result.update(finished_at=_now(), duration_ms=int((time.monotonic() - started) * 1000))
    # Do not overwrite a reclaimed phase's evidence after losing ownership.
    workflow.heartbeat_delivery(engine, f"deployment.{phase}", event_key, token)
    workflow.update_job(engine, job_id, result["status"], {"result": result})
    workflow.finish_delivery(engine, f"deployment.{phase}", event_key, token,
                             error="deployment_phase_failed" if result["exit_code"] == 1 else None)
    with engine.begin() as conn:
        conn.execute(runs.insert().values(fleet=f"{fleet}.deployment_verification", trigger=phase,
                     inputs={"verification_id": verification_id, "job_id": job_id}, outputs=result,
                     duration_ms=result["duration_ms"]))
        # Snapshot compilation precedes its own completion record. Request one
        # normal L0 refresh after persisting this result, so the viewer can show
        # the finished phase. The snapshot handler itself never requests another.
        from app.clhear.l1.viewer_snapshot import request_refresh
        request_refresh(conn, reason="deployment_phase_finished", job_id=job_id)
    return result


def execute(phase, verification_id):
    """Production CLI boundary: never bootstrap a fallback corpus database."""
    from app.clhear import db
    from app.clhear.platform.router import Router, build_providers
    from app.clhear.settings import get_settings
    settings = get_settings()
    requirements = {
        "valid_identity": bool(_IDENTIFIER.fullmatch(verification_id or "")),
        "owning_fleet": os.environ.get("CLHEAR_FLEET", "").lower() == PHASE_FLEETS.get(phase),
        "downstream_hold": settings.clhear_l1_only,
        "authoritative_postgresql": bool(os.environ.get("DATABASE_URL")) and settings.database_url.startswith("postgresql"),
        "no_snapshot_database_override": not settings.clhear_snapshot_s3_uri,
        "live_acquisition": os.environ.get("CLHEAR_HTTP_MODE") == "live",
        "s3_artifacts": os.environ.get("CLHEAR_ARTIFACT_STORE") == "s3",
        "private_viewer_destination": bool(os.environ.get("CLHEAR_VIEWER_SNAPSHOT_S3_URI")),
        "real_provider_mode": settings.clhear_llm_provider.strip().lower() != "fake",
    }
    missing = [name for name, passed in requirements.items() if not passed]
    if missing:
        return {"phase": phase, "status": "failed", "exit_code": 1, "error_type": "ConfigurationError",
                "failed_requirements": missing, "accepted_release": False,
                "evidence_mode": "manual_deployment_verification", "nightly_schedule_validation": "pending",
                "downstream": "held" if settings.clhear_l1_only else "not_held"}
    started, tick = _now(), time.monotonic()
    try:
        engine = db.get_engine()
        applied = db.run_migrations(engine)
        bootstrap = {"started_at": started, "finished_at": _now(), "duration_ms": int((time.monotonic() - tick) * 1000),
                     "applied_migrations": applied, "status": "succeeded", "worker": os.environ["CLHEAR_FLEET"].lower()}
        gateway = Router(engine, build_providers(settings))
        return run_phase(engine, gateway, phase, verification_id, bootstrap=bootstrap)
    except Exception as exc:
        return {"verification_id": verification_id, "phase": phase, "status": "failed", "exit_code": 1,
                "error_type": type(exc).__name__, "accepted_release": False, "downstream": "held",
                "evidence_mode": "manual_deployment_verification", "nightly_schedule_validation": "pending"}
