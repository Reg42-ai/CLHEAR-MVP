"""Lease fencing, resumable ingestion and measured worker evidence."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa

from app.clhear import workers
from app.clhear.l1 import workflow
from app.clhear.models import runs


def _task(engine, job="job-one", source="test/document"):
    workflow.ensure_job(engine, job, "test", "manual", job)
    return workflow.ensure_task(engine, job, source)


def test_atomic_claim_and_cross_job_source_lease(engine):
    task = _task(engine)
    other = _task(engine, job="job-two")
    barrier = threading.Barrier(2)
    def claim():
        barrier.wait()
        try:
            return workflow.claim_task(engine, task)
        except workflow.LeaseBusy:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))
    assert len([x for x in claims if x]) == 1
    assert workflow.task_info(engine, task)["attempt"] == 1
    with pytest.raises(workflow.LeaseBusy):
        workflow.claim_task(engine, other)
    assert workflow.task_info(engine, other)["attempt"] == 0
    workflow.finish_task(engine, task, next(x for x in claims if x))
    assert workflow.claim_task(engine, other)


def test_reclaim_fences_old_owner_and_bounds_attempts(engine):
    task = _task(engine)
    now = workflow.utcnow()
    first = workflow.claim_task(engine, task, now=now, lease_seconds=10)
    with workflow.bind_execution(engine, "job-one", task, first):
        stage = workflow.stage("acquisition_parse")
        stage.__enter__()  # Simulate process death, deliberately no exit.
    second = workflow.claim_task(engine, task, now=now + timedelta(seconds=11), lease_seconds=10)
    with pytest.raises(workflow.LeaseLost):
        workflow.heartbeat_task(engine, task, first, now=now + timedelta(seconds=12))
    with pytest.raises(workflow.LeaseLost):
        workflow.finish_task(engine, task, first, now=now + timedelta(seconds=12))
    with workflow.bind_execution(engine, "job-one", task, first), engine.begin() as conn:
        with pytest.raises(workflow.LeaseLost):
            workflow.assert_ownership(conn)
    evidence = workflow.workflow_summary(engine, job_id="job-one")
    assert evidence["steps"][0]["status"] == "interrupted"
    assert evidence["steps"][0]["duration_ms"] is None
    workflow.finish_task(engine, task, second, status="failed", now=now + timedelta(seconds=12))
    with pytest.raises(workflow.RetryDeferred):
        workflow.claim_task(engine, task, now=now + timedelta(seconds=13))
    third = workflow.claim_task(engine, task, now=now + timedelta(seconds=73))
    workflow.finish_task(engine, task, third, status="failed", now=now + timedelta(seconds=74))
    with pytest.raises(workflow.AttemptsExhausted):
        workflow.claim_task(engine, task, now=now + timedelta(seconds=500))
    assert workflow.task_info(engine, task)["status"] == "failed"


def test_heartbeat_extends_both_task_and_source_lease(engine):
    task = _task(engine)
    now = workflow.utcnow()
    token = workflow.claim_task(engine, task, now=now, lease_seconds=10)
    workflow.heartbeat_task(engine, task, token, now=now + timedelta(seconds=9), lease_seconds=30)
    with pytest.raises(workflow.LeaseBusy):
        workflow.claim_task(engine, task, now=now + timedelta(seconds=11))
    other = _task(engine, "job-other")
    with pytest.raises(workflow.LeaseBusy):
        workflow.claim_task(engine, other, now=now + timedelta(seconds=11))
    workflow.finish_task(engine, task, token, now=now + timedelta(seconds=12))


def test_consumer_idempotence_is_exact_and_separate_per_fleet(engine):
    first = workflow.claim_delivery(engine, "fleet.l2", "event-123")
    with pytest.raises(workflow.LeaseBusy):
        workflow.claim_delivery(engine, "fleet.l2", "event-123")
    assert workflow.claim_delivery(engine, "fleet.l7", "event-123")
    assert workflow.claim_delivery(engine, "fleet.l2", "event-12")
    workflow.finish_delivery(engine, "fleet.l2", "event-123", first)
    assert workflow.claim_delivery(engine, "fleet.l2", "event-123") is None


def test_stages_have_measured_duration_dependencies_and_no_secret_token(engine):
    task = _task(engine)
    token = workflow.claim_task(engine, task)
    with workflow.bind_execution(engine, "job-one", task, token):
        with workflow.stage("permission", {"allowed": True}):
            pass
        with workflow.stage("acquisition_parse"):
            pass
    evidence = workflow.workflow_summary(engine, source_key="test/document")
    assert evidence["status"] == "available"
    assert len(evidence["steps"]) == 2
    assert evidence["steps"][1]["depends_on"] == [evidence["steps"][0]["step_id"]]
    assert all(s["duration_ms"] >= 0 and s["finished_at"] for s in evidence["steps"])
    assert token not in json.dumps(evidence)
    assert workflow.execution_context() == {}


def _body(kind="AdapterRunRequested", event_id="retry-one", producer="test", ts="2026-09-15T00:00:00Z"):
    return json.dumps(dict(event_id=event_id, layer="l1", kind=kind, subject_ref="test",
                           payload={"adapter": "test"}, producer=producer, ts=ts))


def test_failure_has_no_handled_marker_and_retry_invokes_handler(engine, monkeypatch):
    handler = Mock(side_effect=[RuntimeError("transient"), {"ran": 1}])
    monkeypatch.setitem(workers.HANDLERS, "AdapterRunRequested", handler)
    with pytest.raises(RuntimeError, match="transient"):
        workers.handle_envelope(engine, None, _body())
    with engine.connect() as conn:
        assert not conn.execute(sa.select(runs.c.id).where(runs.c.fleet == "worker")).first()
    assert workers.handle_envelope(engine, None, _body())["ran"] == 1
    assert workers.handle_envelope(engine, None, _body()) is None
    assert handler.call_count == 2


def test_wrong_fleet_and_missing_schedule_identity_do_not_claim(engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_FLEET", "L0")
    with pytest.raises(workers.WrongFleet):
        workers.handle_envelope(engine, None, _body())
    monkeypatch.setenv("CLHEAR_FLEET", "L1")
    with pytest.raises(ValueError, match="occurrence timestamp"):
        workers.handle_envelope(engine, None, _body(producer="eventbridge", ts=""))
    with engine.connect() as conn:
        assert not conn.execute(sa.select(workflow.deliveries)).first()


def test_real_schedule_occurrences_are_stable_across_redelivery():
    from app.clhear.platform.events import Envelope
    one = Envelope.model_validate_json(_body(producer="eventbridge"))
    next_day = one.model_copy(update={"ts": "2026-09-16T00:00:00Z", "event_id": "next-event"})
    assert workers.delivery_event_key(one) == workers.delivery_event_key(one)
    assert workflow.job_id_for(workers.delivery_event_key(one), "test") != workflow.job_id_for(workers.delivery_event_key(next_day), "test")


def test_visibility_heartbeat_runs_during_blocking_work():
    renew = Mock()
    called = threading.Event()
    def callback():
        renew()
        if renew.call_count >= 2:
            called.set()
    with workflow.heartbeat(callback, interval_seconds=0.01):
        assert called.wait(timeout=1)
    assert renew.call_count >= 2


def test_l0_routes_layers_to_bus_commands_to_l1_and_checks_entry_failure(monkeypatch):
    import boto3
    client = Mock()
    client.put_events.return_value = {"FailedEntryCount": 0, "Entries": [{"EventId": "ok"}]}
    monkeypatch.setattr(boto3, "client", Mock(return_value=client))
    transport = workers.RoutedOutboxTransport({"l0": "zero", "l1": "one"}, "eu-west-1")
    transport.send(_body())
    assert client.send_message.call_args.kwargs["QueueUrl"] == "one"
    transport.send(_body(kind="clhear.l1.changed"))
    assert client.put_events.call_count == 1
    client.put_events.return_value = {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "ThrottlingException"}]}
    with pytest.raises(RuntimeError, match="acceptance"):
        transport.send(_body(kind="clhear.l1.changed"))


def test_manual_dispatch_uses_identical_handler(engine, tmp_path, monkeypatch):
    from app.clhear.platform import router
    envelope = tmp_path / "manual-envelope.json"
    envelope.write_text(_body())
    handler = Mock(return_value={"ran": 1})
    monkeypatch.setitem(workers.HANDLERS, "AdapterRunRequested", handler)
    monkeypatch.setattr(router, "build_providers", lambda settings: {})
    monkeypatch.setattr(router, "Router", Mock())
    assert workers.dispatch_once(str(envelope))["ran"] == 1
    assert workers.dispatch_once(str(envelope)) is None
    handler.assert_called_once()


def _fake_fleet(monkeypatch):
    from app.clhear.l1 import fleet, inventory, pipeline, registry_etoro
    from app.clhear.platform import evals
    adapters = [SimpleNamespace(key="test", meta=lambda key=key: SimpleNamespace(source_key=key))
                for key in ("test/one", "test/two")]
    monkeypatch.setattr(fleet, "fleet_plan", lambda key: [({"key": a.meta().source_key}, a) for a in adapters])
    monkeypatch.setattr(registry_etoro, "seed", Mock())
    audits = Mock(return_value={"audit_id": "audit-test", "inventory_hash": "binding-test"})
    monkeypatch.setattr(inventory, "run_inventory_audit", audits)
    monkeypatch.setattr(inventory, "planned_entries", lambda *a, **k: [])
    monkeypatch.setattr(inventory, "acceptance_status", lambda *a, **k: {"passed": True, "inventory_hash": "binding-test"})
    monkeypatch.setattr(evals, "run_suite", lambda *a, **k: {"passed": True, "scores": {}})
    source_evals = Mock(return_value=[{"passed": True, "suite": "fixture"}])
    monkeypatch.setattr(evals, "run_source_evals", source_evals)
    monkeypatch.setattr(workers, "_put_schedule_metric", Mock())
    return pipeline, audits, source_evals


def test_failed_source_resumes_without_reimporting_completed_source(engine, monkeypatch):
    pipeline, audits, evaluations = _fake_fleet(monkeypatch)
    counts = {"test/one": 0, "test/two": 0}
    def ingest(engine, adapter, store, **kwargs):
        source = adapter.meta().source_key
        counts[source] += 1
        assert kwargs["index_embeddings"] is False
        assert workflow.execution_context()["job_id"] == kwargs["job_id"]
        return {"source": source, "status": "failed" if source == "test/two" and counts[source] == 1 else "unchanged"}
    monkeypatch.setattr(pipeline, "ingest", ingest)
    with pytest.raises(workers.AdapterRunIncomplete):
        workers.handle_envelope(engine, None, _body())
    with engine.begin() as conn:
        conn.execute(workflow.tasks.update().where(workflow.tasks.c.status == "retrying").values(next_attempt_at=workflow.utcnow()))
    result = workers.handle_envelope(engine, None, _body())
    assert result["failures"] == []
    assert counts == {"test/one": 1, "test/two": 2}
    assert audits.call_count == 4
    assert [call.kwargs["discover"] for call in audits.call_args_list] == [False, False, False, False]
    assert len({call.kwargs["job_id"] for call in audits.call_args_list}) == 1
    assert evaluations.call_count == 4
    with engine.connect() as conn:
        assert len(conn.execute(sa.select(runs.c.id).where(runs.c.fleet == "worker")).all()) == 1


def test_eval_exception_redelivery_rechecks_without_reimport(engine, monkeypatch):
    pipeline, audits, evaluations = _fake_fleet(monkeypatch)
    ingest = Mock(return_value={"status": "unchanged"})
    monkeypatch.setattr(pipeline, "ingest", ingest)
    evaluations.side_effect = [RuntimeError("readback failed"), [{"passed": True}], [{"passed": True}]]
    with pytest.raises(RuntimeError, match="readback failed"):
        workers.handle_envelope(engine, None, _body())
    assert workflow.workflow_summary(engine)["jobs"][0]["status"] == "failed"
    assert workers.handle_envelope(engine, None, _body())["acceptance"] == "candidate_verified"
    assert ingest.call_count == 2
    assert audits.call_count == 4
    failed_steps = [s for s in workflow.workflow_summary(engine)["steps"] if s["stage"] == "readback_evals" and s["status"] == "failed"]
    assert len(failed_steps) == 1


def test_blocked_permission_does_not_retry_import_or_mark_handled(engine, monkeypatch):
    pipeline, audits, evaluations = _fake_fleet(monkeypatch)
    ingest = Mock(return_value={"status": "rights-blocked", "error": "No grant"})
    monkeypatch.setattr(pipeline, "ingest", ingest)
    for _ in range(2):
        with pytest.raises(workers.AdapterRunIncomplete):
            workers.handle_envelope(engine, None, _body())
    assert ingest.call_count == 2  # once per source, no hammering protected access
    assert audits.call_count == 4
    with engine.connect() as conn:
        assert not conn.execute(sa.select(runs.c.id).where(runs.c.fleet == "worker")).first()
    assert all(t["status"] == "blocked" for t in workflow.workflow_summary(engine)["tasks"])


def test_frozen_leftover_finra_notice_is_not_fetched(engine, monkeypatch):
    """A frozen registered job that still lists leaked notices must not fetch them."""
    pipeline, audits, evaluations = _fake_fleet(monkeypatch)
    from app.clhear.l1 import fleet
    rule = {"key": "finra/rule/2210",
            "canonical_url": "https://www.finra.org/rules-guidance/rulebooks/finra-rules/2210"}
    notice = {"key": "finra/document/leftovernotice",
              "canonical_url": "https://www.finra.org/rules-guidance/notices/16-37"}
    nyse = {"key": "finra/document/leftovernyse",
            "canonical_url": "https://www.finra.org/rules-guidance/rulebooks/incorporated-nyse-rules/rule-409"}
    adapters = [
        SimpleNamespace(key="finra", meta=lambda row=row: SimpleNamespace(
            source_key=row["key"], canonical_url=row["canonical_url"]))
        for row in (rule, notice, nyse)
    ]
    monkeypatch.setattr(fleet, "fleet_plan", lambda key: list(zip((rule, notice, nyse), adapters)))
    ingested = []
    def ingest(engine, adapter, store, **kwargs):
        ingested.append(adapter.meta().source_key)
        return {"status": "unchanged"}
    monkeypatch.setattr(pipeline, "ingest", ingest)
    result = workers.handle_envelope(engine, None, _body())
    assert ingested == [rule["key"], nyse["key"]]
    assert notice["key"] not in ingested
    assert result["failures"] == []
    tasks = {t["source_key"]: t for t in workflow.workflow_summary(engine)["tasks"]}
    assert tasks[notice["key"]]["status"] == "blocked"
    assert tasks[notice["key"]]["summary"]["status"] == "out-of-scope"
    assert tasks[rule["key"]]["status"] == "completed"
    assert tasks[nyse["key"]]["status"] == "completed"


def test_retry_freezes_source_keys_and_rejects_changed_inventory(engine, monkeypatch):
    from app.clhear.l1 import fleet, inventory
    pipeline, audits, evaluations = _fake_fleet(monkeypatch)
    ingest = Mock(return_value={"status": "unchanged"})
    monkeypatch.setattr(pipeline, "ingest", ingest)
    evaluations.side_effect = [RuntimeError("interrupted eval"), [{"passed": True}], [{"passed": True}]]
    with pytest.raises(RuntimeError, match="interrupted"):
        workers.handle_envelope(engine, None, _body())
    adapters = [SimpleNamespace(key="test", meta=lambda key=key: SimpleNamespace(source_key=key))
                for key in ("test/one", "test/two", "test/discovered-later")]
    monkeypatch.setattr(fleet, "fleet_plan", lambda key: [({"key": a.meta().source_key}, a) for a in adapters])
    audits.return_value = {"audit_id": "changed-audit", "inventory_hash": "new-inventory"}
    monkeypatch.setattr(inventory, "acceptance_status", lambda *a, **k: {"passed": True, "inventory_hash": "new-inventory"})
    with pytest.raises(workers.AdapterRunIncomplete):
        workers.handle_envelope(engine, None, _body())
    evidence = workflow.workflow_summary(engine)
    assert ingest.call_count == 2
    assert len(evidence["tasks"]) == 2
    assert evidence["jobs"][0]["summary"]["source_keys"] == ["test/one", "test/two"]
    assert evidence["jobs"][0]["summary"]["inventory_hash"] == "binding-test"
    assert any("Inventory changed" in reason for reason in evidence["jobs"][0]["summary"]["failures"])


def test_failed_mandatory_eval_never_marks_event_handled(engine, monkeypatch):
    from app.clhear.platform import evals
    pipeline, audits, evaluations = _fake_fleet(monkeypatch)
    monkeypatch.setattr(pipeline, "ingest", Mock(return_value={"status": "unchanged"}))
    monkeypatch.setattr(evals, "run_suite", lambda engine, suite, **kwargs: {"passed": suite != "l1_boundary_f1", "scores": {}})
    with pytest.raises(workers.AdapterRunIncomplete):
        workers.handle_envelope(engine, None, _body())
    with engine.connect() as conn:
        assert not conn.execute(sa.select(runs.c.id).where(runs.c.fleet == "worker")).first()
    evidence = workflow.workflow_summary(engine)
    assert evidence["jobs"][0]["status"] == "blocked"
    assert any(s["stage"] == "readback_evals" and s["status"] == "blocked" for s in evidence["steps"])


def test_diagnostic_not_applicable_does_not_override_independent_acceptance(engine, monkeypatch):
    pipeline, audits, evaluations = _fake_fleet(monkeypatch)
    monkeypatch.setattr(pipeline, "ingest", Mock(return_value={"status": "unchanged"}))
    evaluations.return_value = [{"suite": "e4_amendment_fidelity", "passed": False, "scores": {"n/a": True}}]
    result = workers.handle_envelope(engine, None, _body())
    assert result["acceptance"] == "candidate_verified"
    evidence = workflow.workflow_summary(engine)
    readback = next(s for s in evidence["steps"] if s["stage"] == "readback_evals")
    assert readback["details"]["sources"]["test/one"][0]["passed"] is False
    assert "diagnostic" in readback["details"]["source_suites_role"]
