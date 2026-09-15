"""Owning-fleet deployment orchestration, without remote imports or AWS calls."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa

from app.clhear import deployment_verification as verification, workers
from app.clhear.l1 import workflow
from app.clhear.l1.models import source_families, sources, source_versions
from app.clhear.models import runs
from app.clhear.settings import get_settings
from tests.test_l1_workflow import _fake_fleet


def _fleet(monkeypatch, name):
    monkeypatch.setenv("CLHEAR_FLEET", name)
    monkeypatch.setenv("CLHEAR_L1_ONLY", "true")
    get_settings.cache_clear()


def _finra_worker(engine, monkeypatch, outcomes=("added", "unchanged")):
    from app.clhear.l1 import fleet, inventory
    pipeline, audits, _ = _fake_fleet(monkeypatch)
    adapter = SimpleNamespace(key="test", meta=lambda: SimpleNamespace(source_key="finra/rule/2210"))
    monkeypatch.setattr(fleet, "fleet_plan", lambda key: [({"key": "finra/rule/2210"}, adapter)])
    audits.return_value = {"audit_id": "test-audit", "inventory_hash": "binding-test", "status": "gaps",
                           "verified": 0, "unresolved": 1}
    pending = iter(outcomes)
    calls = []

    def ingest(engine, adapter, store, **kwargs):
        calls.append(kwargs["job_id"])
        outcome = next(pending)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome in {"added", "amended"}:
            with engine.begin() as conn:
                sid = conn.execute(sa.select(sources.c.id).where(sources.c.key == "finra/rule/2210")).scalar_one_or_none()
                if sid is None:
                    fid = conn.execute(source_families.insert().values(key="verification-test", name="Test metadata").returning(source_families.c.id)).scalar_one()
                    sid = conn.execute(sources.insert().values(family_id=fid, key="finra/rule/2210", name="Test metadata", kind="regulation").returning(sources.c.id)).scalar_one()
                conn.execute(source_versions.insert().values(source_id=sid, version_label=f"test-{len(calls)}", content_hash=str(len(calls)) * 64))
        return {"status": outcome}

    monkeypatch.setattr(pipeline, "ingest", ingest)
    return calls, inventory


def test_verify_uses_real_handlers_two_distinct_jobs_and_preserves_versions(engine, monkeypatch):
    _fleet(monkeypatch, "L1")
    calls, _ = _finra_worker(engine, monkeypatch)
    result = verification.run_phase(engine, None, "verify", "deployment-one")
    assert result["status"] == "verified" and result["exit_code"] == 0
    assert result["accepted_release"] is False and result["downstream"] == "held"
    assert result["nightly_schedule_validation"] == "pending"
    assert result["evidence_mode"] == "manual_deployment_verification"
    assert len(calls) == len(set(calls)) == 2
    check = result["steps"]["unchanged_check"]
    assert check["passed"] and check["version_count_before"] == check["version_count_after"] == 1
    evidence = workflow.workflow_summary(engine, job_id=result["job_id"])
    assert all(step["finished_at"] and step["duration_ms"] >= 0 for step in evidence["steps"])
    assert verification.run_phase(engine, None, "verify", "deployment-one") == result
    assert len(calls) == 2  # immutable phase redelivery never reruns imports
    with engine.connect() as conn:
        markers = list(conn.execute(sa.select(runs.c.trigger).where(runs.c.fleet == "worker")).scalars())
    assert markers.count("AdapterRunRequested") == 2 and markers.count("L1InventoryAuditRequested") == 1


def test_permissions_blocked_is_review_ready_not_unchanged_or_accepted(engine, monkeypatch):
    _fleet(monkeypatch, "L1")
    calls, inventory = _finra_worker(engine, monkeypatch, ("rights-blocked", "rights-blocked"))
    monkeypatch.setattr(inventory, "acceptance_status", lambda *a, **k: {"passed": False, "inventory_hash": "binding-test", "reasons": ["permission_unverified"]})
    result = verification.run_phase(engine, None, "verify", "blocked-one")
    assert result["status"] == "review_ready" and result["exit_code"] == 2
    assert result["acceptance"] == "awaiting_verification" and result["accepted_release"] is False
    assert not result["steps"]["unchanged_check"]["passed"]
    assert result["steps"]["unchanged_check"]["successful_source_count"] == 0 and len(calls) == 2
    with engine.connect() as conn:
        assert not conn.execute(sa.select(runs).where(runs.c.fleet == "worker", runs.c.trigger == "AdapterRunRequested")).first()


def test_initial_source_failure_stops_repeat_and_reports_no_plaintext(engine, monkeypatch):
    _fleet(monkeypatch, "L1")
    calls, _ = _finra_worker(engine, monkeypatch, (RuntimeError("private source body or secret URL"),))
    result = verification.run_phase(engine, None, "verify", "failed-one")
    assert result["status"] == "failed" and result["exit_code"] == 1 and len(calls) == 1
    assert "private source" not in json.dumps(result) and "finra_repeat" not in result["steps"]


def test_changed_repeat_is_review_ready_and_does_not_claim_idempotence(engine, monkeypatch):
    _fleet(monkeypatch, "L1")
    _finra_worker(engine, monkeypatch, ("added", "amended"))
    result = verification.run_phase(engine, None, "verify", "changed-one")
    assert result["status"] == "review_ready" and result["exit_code"] == 2
    assert not result["steps"]["unchanged_check"]["passed"]
    assert result["steps"]["unchanged_check"]["version_count_after"] == 2


def test_repeat_retry_reuses_first_job_and_frozen_comparison(engine, monkeypatch):
    _fleet(monkeypatch, "L1")
    calls, _ = _finra_worker(engine, monkeypatch, ("added", RuntimeError("transient"), "unchanged"))
    first = verification.run_phase(engine, None, "verify", "retry-one")
    assert first["exit_code"] == 1
    with engine.begin() as conn:
        conn.execute(workflow.tasks.update().where(workflow.tasks.c.status == "retrying").values(next_attempt_at=workflow.utcnow()))
    resumed = verification.run_phase(engine, None, "verify", "retry-one")
    assert resumed["exit_code"] == 0 and resumed["steps"]["unchanged_check"]["passed"]
    assert calls[0] != calls[1] == calls[2] and len(calls) == 3


@pytest.mark.parametrize("phase", ["bootstrap", "publish"])
def test_l0_snapshot_phases_dispatch_existing_handler_and_return_verified_artifact_metadata(engine, monkeypatch, phase):
    from app.clhear.l1 import registry_etoro
    _fleet(monkeypatch, "L0")
    seed = Mock()
    monkeypatch.setattr(registry_etoro, "seed", seed)
    snapshot = Mock(return_value={"revision": "revision-one", "sha256": "a" * 64, "byte_count": 4096,
                                "snapshot_uri": "s3://test/webui/private.db", "accepted_release": False,
                                "source_environment": "authoritative_postgresql", "raw_text": "must not be printed"})
    monkeypatch.setitem(workers.HANDLERS, "ViewerSnapshotRequested", snapshot)
    result = verification.run_phase(engine, None, phase, "snapshot-one", bootstrap={"applied_migrations": [24, 25, 26]})
    assert result["status"] == "succeeded" and result["exit_code"] == 0
    assert result["steps"]["viewer_snapshot"]["revision"] == "revision-one"
    assert "must not be printed" not in json.dumps(result)
    assert seed.call_count == (1 if phase == "bootstrap" else 0)
    assert snapshot.call_args.args[2].kind == "ViewerSnapshotRequested"


def test_snapshot_failure_is_nonzero_and_preserves_failure_evidence(engine, monkeypatch):
    _fleet(monkeypatch, "L0")
    monkeypatch.setitem(workers.HANDLERS, "ViewerSnapshotRequested", Mock(side_effect=RuntimeError("storage unavailable")))
    result = verification.run_phase(engine, None, "publish", "snapshot-failure")
    assert result["exit_code"] == 1 and result["status"] == "failed"
    evidence = workflow.workflow_summary(engine, job_id=result["job_id"])
    assert evidence["steps"][-1]["status"] == "failed"


def test_wrong_fleet_and_disabled_hold_fail_before_claim(engine, monkeypatch):
    _fleet(monkeypatch, "L0")
    with pytest.raises(workers.WrongFleet):
        verification.run_phase(engine, None, "verify", "wrong-fleet")
    monkeypatch.setenv("CLHEAR_L1_ONLY", "false")
    get_settings.cache_clear()
    with pytest.raises(workers.L1AcceptanceHold):
        verification.run_phase(engine, None, "bootstrap", "hold-disabled")
    with engine.connect() as conn:
        assert not conn.execute(sa.select(workflow.jobs)).first()


def test_production_entrypoint_rejects_sqlite_before_migrations(engine, monkeypatch):
    from app.clhear import db
    _fleet(monkeypatch, "L1")
    migrate = Mock()
    monkeypatch.setattr(db, "run_migrations", migrate)
    result = verification.execute("verify", "no-fallback")
    assert result["exit_code"] == 1 and "authoritative_postgresql" in result["failed_requirements"]
    migrate.assert_not_called()


def test_worker_cli_preserves_review_ready_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(verification, "execute", lambda phase, verification_id: {"phase": phase, "verification_id": verification_id, "status": "review_ready", "exit_code": 2})
    assert workers.cli(["--verify-deployment", "verify", "--verification-id", "cli-one"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "review_ready"
    with pytest.raises(SystemExit) as error:
        workers.cli(["--verify-deployment", "verify", "--once", "--verification-id", "cli-one"])
    assert error.value.code == 1  # Invalid arguments must not masquerade as review_ready/2.


def test_operational_task_check_is_not_limited_by_viewer_pagination(engine, monkeypatch):
    event_key = "deployment:large:finra.first"
    job_id = workflow.job_id_for(event_key, "finra")
    workflow.ensure_job(engine, job_id, "finra", "manual", event_key)
    workflow.update_job(engine, job_id, "blocked", {"acceptance": "awaiting_verification"})
    with engine.begin() as conn:
        conn.execute(workflow.tasks.insert(), [
            {"task_id": f"large-{i}", "job_id": job_id, "source_key": f"finra/rule/{i}",
             "worker": "l1.test", "status": "completed" if i < 250 else "retrying",
             "created_at": workflow.utcnow(), "summary": {"status": "unchanged" if i < 250 else "failed"}}
            for i in range(251)
        ])
    monkeypatch.setattr(verification, "_dispatch", Mock(return_value={}))
    result = verification._adapter_step(engine, None, "large", "finra.first")
    assert result["source_tasks"] == 251 and len(result["successful_sources"]) == 250
    assert result["failed_sources"] == ["finra/rule/250"] and result["execution_failed"]


def test_snapshot_completion_records_followup_refresh_in_same_ledger_transaction(engine, monkeypatch):
    from app.clhear.models import events
    _fleet(monkeypatch, "L0")
    monkeypatch.setenv("CLHEAR_VIEWER_SNAPSHOT_S3_URI", "s3://test/webui/private.db")
    monkeypatch.setitem(workers.HANDLERS, "ViewerSnapshotRequested", Mock(return_value={
        "revision": "complete-one", "sha256": "a" * 64, "byte_count": 4096,
    }))
    result = verification.run_phase(engine, None, "publish", "finished-snapshot")
    with engine.connect() as conn:
        requests = list(conn.execute(sa.select(events.c.payload).where(events.c.kind == "ViewerSnapshotRequested")).scalars())
        completed = conn.execute(sa.select(runs.c.outputs).where(runs.c.fleet == "l0.deployment_verification")).scalar_one()
    assert requests == [{"reason": "deployment_phase_finished", "job_id": result["job_id"]}]
    assert completed["status"] == "succeeded"


def test_final_phase_uses_actual_snapshot_handler_and_checks_actual_bytes(engine, monkeypatch):
    import boto3
    from tests.test_l1_viewer_snapshot import ConditionalStore
    _fleet(monkeypatch, "L0")
    monkeypatch.setenv("CLHEAR_VIEWER_SNAPSHOT_S3_URI", "s3://test/webui/private.db")
    store = ConditionalStore()
    monkeypatch.setattr(boto3, "client", Mock(return_value=store))
    result = verification.run_phase(engine, None, "publish", "actual-snapshot")
    snapshot = result["steps"]["viewer_snapshot"]
    assert result["exit_code"] == 0 and store.value.startswith(b"SQLite format 3")
    assert snapshot["byte_count"] == len(store.value)
    assert snapshot["sha256"] == hashlib.sha256(store.value).hexdigest()
    assert snapshot["accepted_release"] is False


def test_cli_bootstrap_migrates_before_handler_and_records_measured_migration_time(engine, monkeypatch):
    from app.clhear import db
    from app.clhear.platform import router
    _fleet(monkeypatch, "L0")
    for name, value in {
        "DATABASE_URL": "postgresql://worker:private-test-password@db.invalid/clhear",
        "CLHEAR_SNAPSHOT_S3_URI": "", "CLHEAR_HTTP_MODE": "live", "CLHEAR_ARTIFACT_STORE": "s3",
        "CLHEAR_VIEWER_SNAPSHOT_S3_URI": "s3://test/webui/private.db", "CLHEAR_LLM_PROVIDER": "infer",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    calls = []
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(db, "run_migrations", lambda e: calls.append("migrations") or [24, 25, 26])
    monkeypatch.setattr(router, "build_providers", lambda settings: {})
    monkeypatch.setattr(router, "Router", Mock())

    def phase(engine, gateway, phase, verification_id, *, bootstrap):
        calls.append("phase")
        assert bootstrap["applied_migrations"] == [24, 25, 26]
        assert bootstrap["duration_ms"] >= 0 and bootstrap["started_at"] <= bootstrap["finished_at"]
        return {"status": "succeeded", "exit_code": 0}

    monkeypatch.setattr(verification, "run_phase", phase)
    result = verification.execute("bootstrap", "ordered-bootstrap")
    assert calls == ["migrations", "phase"] and result["exit_code"] == 0
    assert "private-test-password" not in json.dumps(result)
