"""L0/L1 integration: real handlers and fixture records, no cloud/publisher IO."""
import json
import os
from unittest.mock import Mock

import boto3
import pytest
import sqlalchemy as sa

from app.clhear import workers, deployment_verification
from app.clhear.l1 import cycles, workflow, translation
from app.clhear.l1.models import doc_nodes, source_versions
from app.clhear.l1.translation_models import english_views
from app.clhear.models import events, runs
from app.clhear.settings import get_settings
from tests.test_l1_cycles import envelope, dispatch, queued, fake_cycle, start_and_plan, finish
from tests.test_l1_translation import imported


def test_terminal_delivery_ack_requires_committed_retirement(engine, monkeypatch):
    calls, _, _ = fake_cycle(monkeypatch, ("alpha",))
    monkeypatch.setenv("CLHEAR_CODE_REVISION", "a" * 40)
    cycle_id, _ = start_and_plan(engine, monkeypatch)
    command = queued(engine, "AdapterRunRequested")[0]
    monkeypatch.setenv("CLHEAR_CODE_REVISION", "b" * 40)
    with pytest.raises(cycles.CycleRevisionChanged):
        dispatch(engine, monkeypatch, command, "l1")
    with engine.connect() as conn:
        assert not conn.execute(sa.select(workflow.deliveries).where(
            workflow.deliveries.c.event_key == command.event_id)).first()
        assert not conn.execute(sa.select(runs).where(runs.c.trigger == "AdapterRunRequested")).first()
    assert cycles.cycle_summary(engine, cycle_id)["cycles"][0]["status"] == "running"
    cycles.reconcile(engine)
    result = dispatch(engine, monkeypatch, command, "l1")
    assert result["status"] == "terminal_delivery_ignored"
    assert result["cycle_status"] == "failed" and not result["source_work_performed"]
    assert not result["accepted_release"] and not calls
    with engine.connect() as conn:
        assert conn.execute(sa.select(workflow.deliveries.c.status).where(
            workflow.deliveries.c.event_key == command.event_id)).scalar_one() == "completed"
    assert dispatch(engine, monkeypatch, command, "l1") is None


def test_guard_contention_does_not_consume_operational_retry_budget(engine, monkeypatch):
    _, audits, _ = fake_cycle(monkeypatch, ("alpha",))
    cycle_id = dispatch(engine, monkeypatch, envelope("L1CycleRequested"), "l0")["cycle_id"]
    command = queued(engine, "L1CycleDiscoveryRequested")[0]
    for _ in range(2):
        with cycles._execution_fence(engine, exclusive=True), pytest.raises(workflow.LeaseBusy):
            dispatch(engine, monkeypatch, command, "l1")
    audits.side_effect = RuntimeError("first actual acquisition attempt failed")
    with pytest.raises(RuntimeError, match="first actual"):
        dispatch(engine, monkeypatch, command, "l1")
    assert audits.call_count == 1
    assert cycles.cycle_summary(engine, cycle_id)["cycles"][0]["status"] == "discovering"
    with engine.connect() as conn:
        assert conn.execute(sa.select(workflow.deliveries.c.attempt).where(
            workflow.deliveries.c.event_key == command.event_id)).scalar_one() == 1


def test_handler_internal_lease_failure_still_counts_an_execution_attempt(engine, monkeypatch):
    _, audits, _ = fake_cycle(monkeypatch, ("alpha",))
    dispatch(engine, monkeypatch, envelope("L1CycleRequested"), "l0")
    command = queued(engine, "L1CycleDiscoveryRequested")[0]
    audits.side_effect = workflow.LeaseBusy("publisher page lease belongs to another task")
    with pytest.raises(workflow.LeaseBusy, match="publisher page"):
        dispatch(engine, monkeypatch, command, "l1")
    with engine.connect() as conn:
        delivery = conn.execute(sa.select(workflow.deliveries).where(
            workflow.deliveries.c.event_key == command.event_id)).mappings().one()
    assert delivery["attempt"] == 1 and delivery["status"] == "failed"


def test_completed_discovery_redelivery_is_acknowledged_after_phase_advanced(engine, monkeypatch):
    _, audits, _ = fake_cycle(monkeypatch, ("alpha",))
    cycle_id = dispatch(engine, monkeypatch, envelope("L1CycleRequested"), "l0")["cycle_id"]
    command = queued(engine, "L1CycleDiscoveryRequested")[0]
    dispatch(engine, monkeypatch, command, "l1")
    assert cycles.cycle_summary(engine, cycle_id)["cycles"][0]["status"] == "planned"
    assert dispatch(engine, monkeypatch, command, "l1") is None
    assert audits.call_count == 1
    with engine.connect() as conn:
        assert conn.execute(sa.select(workflow.deliveries.c.attempt).where(
            workflow.deliveries.c.event_key == command.event_id)).scalar_one() == 1


def test_english_runs_on_l1_after_final_original_audit_under_cycle_fence(engine, monkeypatch, tmp_path):
    from app.clhear.l1 import pipeline
    secret = "Must retain 7 confidential fixture records."
    version = imported(engine, tmp_path, key="alpha/doc", language="en", body=f"<h1>Rule 7</h1><p>{secret}</p>")
    real_english = translation.build_english_view
    with engine.connect() as conn:
        original = [dict(r) for r in conn.execute(sa.select(doc_nodes)).mappings()]
        content_hash = conn.execute(sa.select(source_versions.c.content_hash).where(source_versions.c.id == version)).scalar_one()
    calls, audits, audit = fake_cycle(monkeypatch, ("alpha",))
    audit["sources"][0].update(source_version_id=version, content_hash=content_hash)
    sequence = []
    def import_source(*args, **kwargs):
        sequence.append("import")
        return {"status": "unchanged", "source_version_id": version, "content_hash": content_hash}
    monkeypatch.setattr(pipeline, "ingest", import_source)
    def audit_source(*args, **kwargs):
        sequence.append("discovery" if kwargs["discover"] else "original_readback")
        return audit
    audits.side_effect = audit_source
    def english(*args, **kwargs):
        assert os.environ["CLHEAR_FLEET"] == "l1"
        assert sequence[-1] == "original_readback"
        assert cycles.reconcile(engine)["status"] == "executing"
        sequence.append("english")
        return real_english(*args, **kwargs)
    monkeypatch.setattr(translation, "build_english_view", english)
    cycle_id, _ = start_and_plan(engine, monkeypatch)
    for command in queued(engine, "AdapterRunRequested"):
        dispatch(engine, monkeypatch, command, "l1")
    assert sequence == ["discovery", "import"]
    result = finish(engine, monkeypatch)
    assert sequence == ["discovery", "import", "original_readback", "english"]
    assert result["english"]["passed"]
    assert not result["accepted_release"]
    with engine.connect() as conn:
        assert original == [dict(r) for r in conn.execute(sa.select(doc_nodes)).mappings()]
        assert conn.execute(sa.select(english_views.c.status)).scalar_one() == "ready"
        task = conn.execute(sa.select(workflow.tasks).where(workflow.tasks.c.worker == "l1.english_view")).mappings().one()
        assert task["status"] == "completed" and task["job_id"] == result["english"]["job_id"]
        evidence = {"runs": [dict(r) for r in conn.execute(sa.select(runs)).mappings()],
                    "steps": [dict(r) for r in conn.execute(sa.select(workflow.steps)).mappings()],
                    "outbox": [dict(r) for r in conn.execute(sa.select(events)).mappings()]}
    assert secret not in json.dumps(evidence, default=str)
    assert not queued(engine, "L1TranslationRequested")  # cycle completion cannot outrun English tasks


def test_manual_translation_routes_to_l1_and_never_exposes_text_in_receipt(engine, monkeypatch, tmp_path):
    text = "Must retain 7 private fixture records."
    version = imported(engine, tmp_path, language="en", body=f"<h1>Rule 7</h1><p>{text}</p>")
    command = envelope("L1TranslationRequested", {"source_version_id": version})
    with pytest.raises(workers.WrongFleet):
        dispatch(engine, monkeypatch, command, "l0")
    with engine.connect() as conn:
        assert not conn.execute(sa.select(workflow.deliveries)).first()
    result = dispatch(engine, monkeypatch, command, "l1")
    assert result["english_ready"] and not result["accepted_release"]
    assert text not in json.dumps(result)
    client = Mock()
    monkeypatch.setattr(boto3, "client", Mock(return_value=client))
    transport = workers.RoutedOutboxTransport({"l0": "queue-l0", "l1": "queue-l1"}, "us-east-1")
    transport.send(command.model_dump_json())
    client.send_message.assert_called_once_with(QueueUrl="queue-l1", MessageBody=command.model_dump_json())


@pytest.mark.parametrize("kind", ["L1InventoryAuditRequested", "L1TranslationRequested", "AdapterRunRequested"])
def test_active_cycle_blocks_standalone_handler_before_source_work(engine, monkeypatch, kind):
    cycle_id = cycles.start(engine, envelope("L1CycleRequested"))["cycle_id"]
    payload = {"source_version_id": 1, "adapter": "fixture"}
    if kind != "AdapterRunRequested":
        payload["cycle_id"] = cycle_id  # cannot borrow the active cycle
    handler = Mock()
    monkeypatch.setitem(workers.HANDLERS, kind, handler)
    command = envelope(kind, payload)
    with pytest.raises(workflow.RetryDeferred):
        dispatch(engine, monkeypatch, command, "l1")
    handler.assert_not_called()
    with engine.connect() as conn:
        assert not conn.execute(sa.select(workflow.deliveries).where(
            workflow.deliveries.c.event_key == command.event_id)).first()
        assert not conn.execute(sa.select(runs).where(runs.c.trigger == kind)).first()


@pytest.mark.parametrize("changed_runtime", [False, True])
def test_bootstrap_only_retires_prior_runtime_and_cannot_start_queued_cycle(engine, monkeypatch, changed_runtime):
    from app.clhear.l1 import registry_etoro
    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    monkeypatch.setenv("CLHEAR_L1_ONLY", "true")
    monkeypatch.setenv("CLHEAR_CODE_REVISION", "a" * 40)
    get_settings.cache_clear()
    first = cycles.start(engine, envelope("L1CycleRequested"))["cycle_id"]
    if changed_runtime:
        monkeypatch.setenv("CLHEAR_CODE_REVISION", "b" * 40)
    with cycles._execution_fence(engine):
        second = cycles.start(engine, envelope("L1CycleRequested"))["cycle_id"]
    seed = Mock()
    snapshot = Mock(return_value={"revision": "fixture", "sha256": "a" * 64, "byte_count": 42})
    monkeypatch.setattr(registry_etoro, "seed", seed)
    monkeypatch.setitem(workers.HANDLERS, "ViewerSnapshotRequested", snapshot)
    monkeypatch.setattr(boto3, "client", Mock(side_effect=AssertionError("Bootstrap must not poll queues")))
    result = deployment_verification.run_phase(engine, None, "bootstrap", "fixture-bootstrap")
    assert cycles.cycle_summary(engine, second)["cycles"][0]["status"] == "queued"
    assert len(queued(engine, "L1CycleDiscoveryRequested")) == 1
    if changed_runtime:
        assert result["exit_code"] == 0 and result["steps"]["cycle_recovery"]["status"] == "idle"
        assert cycles.cycle_summary(engine, first)["cycles"][0]["status"] == "failed"
        seed.assert_called_once(); snapshot.assert_called_once()
    else:
        assert result["exit_code"] == 1 and result["status"] == "failed"
        assert cycles.cycle_summary(engine, first)["cycles"][0]["status"] == "discovering"
        seed.assert_not_called(); snapshot.assert_not_called()
