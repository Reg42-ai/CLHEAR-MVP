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


SCOPE = tuple(f"synthetic/rule/{n}" for n in (2111, 2210, 3110, 3310, 4511))


class _Scope:
    """Five real synthetic documents standing in for the fixed FINRA scope. Each
    call of the plan can change one document (amend), break one (raise) or drop one."""

    def __init__(self, monkeypatch, tmp_path, keys=SCOPE):
        from app.clhear.l1 import fleet, inventory, registry_etoro
        from app.clhear.platform import evals
        from tests.test_l1_synthetic_amendment import V1, SyntheticAdapter
        self.keys, self.calls, self.next = list(keys), [], {}
        self.V1 = V1
        self.make = SyntheticAdapter
        monkeypatch.setenv("CLHEAR_ARTIFACTS_DIR", str(tmp_path / "lake"))
        monkeypatch.delenv("CLHEAR_ARTIFACT_STORE", raising=False)
        get_settings.cache_clear()
        monkeypatch.setattr(verification, "DEPLOYMENT_SCOPE", tuple(self.keys))
        monkeypatch.setattr(fleet, "fleet_plan", self.plan)
        monkeypatch.setattr(registry_etoro, "seed", Mock())
        self.audits = Mock(return_value={"audit_id": "test-audit", "inventory_hash": "binding-test", "status": "gaps",
                                         "verified": 0, "unresolved": 1})
        monkeypatch.setattr(inventory, "run_inventory_audit", self.audits)
        monkeypatch.setattr(inventory, "planned_entries", Mock(return_value=[]))
        self.acceptance = {"passed": True, "inventory_hash": "binding-test"}
        monkeypatch.setattr(inventory, "acceptance_status", lambda *a, **k: dict(self.acceptance))
        monkeypatch.setattr(evals, "run_suite", lambda *a, **k: {"passed": True, "scores": {}})
        monkeypatch.setattr(evals, "run_source_evals", lambda *a, **k: [{"passed": True, "suite": "fixture"}])
        monkeypatch.setattr(workers, "_put_schedule_metric", Mock())

    def plan(self, key=None):
        self.calls.append(key)
        plan = []
        for source_key in self.keys:
            behaviour = self.next.get(source_key, "same")
            if behaviour == "drop":
                continue
            adapter = self.make(self.V1 if behaviour != "amend" else {**self.V1, "9": "An added provision."},
                                "2026-06-01" if behaviour == "amend" else "2026-01-01", source_key=source_key)
            if behaviour == "raise":
                def fetch(since_version=None, _key=source_key):
                    raise RuntimeError(f"private source body or secret URL for {_key}")
                adapter.fetch = fetch
            plan.append((None, adapter))
        return plan


def _versions(engine, key):
    with engine.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(source_versions).join(sources, sources.c.id == source_versions.c.source_id)
                            .where(sources.c.key == key)).scalar_one()


def test_verify_uses_real_handlers_two_distinct_jobs_and_preserves_versions(engine, monkeypatch, tmp_path):
    _fleet(monkeypatch, "L1")
    scope = _Scope(monkeypatch, tmp_path)
    result = verification.run_phase(engine, None, "verify", "deployment-one")
    assert result["status"] == "verified" and result["exit_code"] == 0, result
    assert result["accepted_release"] is False and result["downstream"] == "held"
    assert result["nightly_schedule_validation"] == "pending"
    assert result["evidence_mode"] == "deployment_verification" and "not FINRA acceptance" in result["scope_label"]
    assert result["finra_acceptance"] == result["l1_acceptance"] == "not_claimed"
    assert result["scope"] == list(SCOPE) and result["deployment_checks"] == {"imports": True, "readback": True, "unchanged_repeat": True, "passed": True}
    first, repeat = result["steps"]["finra_import"], result["steps"]["finra_repeat"]
    assert first["job_id"] != repeat["job_id"] and first["scope_complete"] and repeat["scope_complete"]
    assert first["statuses"] == {"added": 5} and repeat["statuses"] == {"unchanged": 5}
    assert first["scope"] == list(SCOPE) and first["missing_sources"] == [] and "tasks" not in first
    readback = result["steps"]["readback"]
    assert readback["passed"] and readback["verified_sources"] == sorted(SCOPE)
    assert all(row["checks"] == {"version_identity": True, "artifact_hashes": True, "encoded_projection": True}
               for row in readback["sources"].values())
    check = result["steps"]["unchanged_check"]
    assert check["passed"] and check["version_count_before"] == check["version_count_after"] == 5
    assert all(row["passed"] for row in check["per_source"].values())
    assert all(_versions(engine, key) == 1 for key in SCOPE)
    # discovery stayed off: the handler ran a fixed-scope reconciliation, never a frontier expansion
    assert all(call.kwargs.get("discover") is False for call in scope.audits.call_args_list)
    evidence = workflow.workflow_summary(engine, job_id=result["job_id"])
    assert {step["stage"] for step in evidence["steps"]} >= {"inventory_audit", "finra_import", "readback", "finra_repeat"}
    assert verification.run_phase(engine, None, "verify", "deployment-one") == result
    assert all(_versions(engine, key) == 1 for key in SCOPE)  # immutable phase redelivery never reruns imports
    with engine.connect() as conn:
        markers = list(conn.execute(sa.select(runs.c.trigger).where(runs.c.fleet == "worker")).scalars())
    assert markers.count("AdapterRunRequested") == 2 and markers.count("L1InventoryAuditRequested") == 1
    with engine.connect() as conn:
        job = conn.execute(sa.select(workflow.jobs.c.summary).where(workflow.jobs.c.job_id == first["job_id"])).scalar_one()
    assert job["fixed_scope"] == sorted(SCOPE) and job["discover"] is False


def test_acceptance_hold_is_review_ready_not_verified_and_never_a_failed_deployment(engine, monkeypatch, tmp_path):
    _fleet(monkeypatch, "L1")
    scope = _Scope(monkeypatch, tmp_path)
    scope.acceptance = {"passed": False, "inventory_hash": "binding-test", "reasons": ["permission_unverified"]}
    result = verification.run_phase(engine, None, "verify", "blocked-one")
    assert result["status"] == "review_ready" and result["exit_code"] == 2
    assert result["deployment_checks"]["passed"] and result["steps"]["unchanged_check"]["passed"]
    assert result["acceptance"] == "awaiting_verification" and result["corpus_acceptance"] == "awaiting_verification"
    assert result["accepted_release"] is False and result["finra_acceptance"] == "not_claimed"


def test_initial_source_failure_stops_repeat_reports_no_plaintext_and_keeps_safe_failure_details(engine, monkeypatch, tmp_path):
    _fleet(monkeypatch, "L1")
    scope = _Scope(monkeypatch, tmp_path)
    scope.next[SCOPE[2]] = "raise"
    result = verification.run_phase(engine, None, "verify", "failed-one")
    assert result["status"] == "failed" and result["exit_code"] == 1
    assert "finra_repeat" not in result["steps"] and "readback" not in result["steps"]
    text = json.dumps(result)
    assert "private source" not in text and "secret URL" not in text
    first = result["steps"]["finra_import"]
    assert first["failed_sources"] == [SCOPE[2]] and not first["scope_complete"] and first["execution_failed"]
    assert len(first["successful_sources"]) == 4  # the other four documents were persisted; nothing is rolled back
    [failure] = result["failure_summary"]
    assert failure["source"] == SCOPE[2] and failure["worker"] == "l1" and failure["attempt"] == 1
    assert failure["error_type"] and failure["stage"] and "sqlstate" in failure


def test_missing_scope_document_keeps_deployment_held(engine, monkeypatch, tmp_path):
    _fleet(monkeypatch, "L1")
    scope = _Scope(monkeypatch, tmp_path)
    scope.next[SCOPE[0]] = "drop"
    result = verification.run_phase(engine, None, "verify", "missing-one")
    assert result["status"] == "failed" and result["exit_code"] == 1
    first = result["steps"]["finra_import"]
    assert first["missing_sources"] == [SCOPE[0]] and not first["scope_complete"]


def test_changed_repeat_is_not_verified(engine, monkeypatch, tmp_path):
    _fleet(monkeypatch, "L1")
    scope = _Scope(monkeypatch, tmp_path)
    original = scope.plan
    def plan(key=None):
        if len(scope.calls) >= 1:
            scope.next[SCOPE[1]] = "amend"
        return original(key)
    monkeypatch.setattr(__import__("app.clhear.l1.fleet", fromlist=["fleet_plan"]), "fleet_plan", plan)
    result = verification.run_phase(engine, None, "verify", "changed-one")
    assert result["status"] == "failed" and result["exit_code"] == 1
    check = result["steps"]["unchanged_check"]
    assert not check["passed"] and check["version_count_after"] == 6
    assert not check["per_source"][SCOPE[1]]["passed"] and check["per_source"][SCOPE[0]]["passed"]
    assert result["deployment_checks"] == {"imports": True, "readback": True, "unchanged_repeat": False, "passed": False}


def test_readback_failure_keeps_deployment_held(engine, monkeypatch, tmp_path):
    from app.clhear.l1 import readback
    _fleet(monkeypatch, "L1")
    _Scope(monkeypatch, tmp_path)
    real = readback.verify_source_version
    def tampered(engine, store, key, summary):
        if key == SCOPE[4]:
            summary = {**summary, "content_hash": "0" * 64}  # the worker claims a version the record does not hold
        return real(engine, store, key, summary)
    monkeypatch.setattr(readback, "verify_source_version", tampered)
    result = verification.run_phase(engine, None, "verify", "readback-one")
    assert result["status"] == "failed" and result["exit_code"] == 1 and "finra_repeat" not in result["steps"]
    rb = result["steps"]["readback"]
    assert rb["failed_sources"] == [SCOPE[4]] and rb["sources"][SCOPE[4]]["checks"]["version_identity"] is False
    assert "version_identity_mismatch" in rb["sources"][SCOPE[4]]["findings"]


def test_repeat_retry_reuses_first_job_and_frozen_comparison(engine, monkeypatch, tmp_path):
    _fleet(monkeypatch, "L1")
    scope = _Scope(monkeypatch, tmp_path)
    original = scope.plan
    def plan(key=None):
        # the second plan (the repeat) breaks one document once; the resumed repeat heals it
        scope.next[SCOPE[3]] = "raise" if len(scope.calls) == 1 else "same"
        return original(key)
    monkeypatch.setattr(__import__("app.clhear.l1.fleet", fromlist=["fleet_plan"]), "fleet_plan", plan)
    first = verification.run_phase(engine, None, "verify", "retry-one")
    assert first["exit_code"] == 1 and first["steps"]["finra_repeat"]["failed_sources"] == [SCOPE[3]]
    with engine.begin() as conn:
        conn.execute(workflow.tasks.update().where(workflow.tasks.c.status == "retrying").values(next_attempt_at=workflow.utcnow()))
    resumed = verification.run_phase(engine, None, "verify", "retry-one")
    assert resumed["exit_code"] == 0 and resumed["steps"]["unchanged_check"]["passed"], resumed
    assert resumed["steps"]["finra_import"]["job_id"] == first["steps"]["finra_import"]["job_id"]
    assert all(_versions(engine, key) == 1 for key in SCOPE)


def test_production_scope_is_the_five_finra_rules_and_labelled_as_deployment_verification():
    assert verification.DEPLOYMENT_SCOPE == ("finra/rule/2111", "finra/rule/2210", "finra/rule/3110", "finra/rule/3310", "finra/rule/4511")
    assert verification.EVIDENCE_MODE == "deployment_verification"
    assert "not FINRA acceptance" in verification.SCOPE_LABEL and "not L1 acceptance" in verification.SCOPE_LABEL


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
