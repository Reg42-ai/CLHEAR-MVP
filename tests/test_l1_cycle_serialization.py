"""Whole-cycle fencing and FIFO recovery against SQLite and real PostgreSQL.

The optional PostgreSQL URL must name a local disposable test server. Every
test creates its own database; neither publishers nor cloud APIs are called.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import multiprocessing
import time
import uuid

import pytest
import sqlalchemy as sa

from app.clhear import db
from app.clhear.l1 import cycles, workflow
from app.clhear.models import events
from tests.test_l1_cycles import envelope, queued, fake_cycle, dispatch, start_and_plan, finish
from tests.test_migration_concurrency import real_postgresql


@pytest.fixture(params=["sqlite", "postgresql"])
def serial_engine(request, engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_CODE_REVISION", "a" * 40)
    monkeypatch.setenv("CLHEAR_WORKER_IMAGE_DIGEST", "sha256:" + "1" * 64)
    if request.param == "sqlite":
        return engine
    pg = request.getfixturevalue("real_postgresql")
    db.run_migrations(pg)
    return pg


def request(engine, name):
    return cycles.start(engine, envelope("L1CycleRequested", {"cycle_id": "cycle-manual-" + name},
                       event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, name))))["cycle_id"]


def state(engine, cycle_id):
    return cycles.cycle_summary(engine, cycle_id)["cycles"][0]


def failed(engine, cycle_id):
    cycles.failed_phase(engine, cycle_id, "L1CycleDiscoveryRequested", RuntimeError("fixture"), workflow.MAX_ATTEMPTS)


def expire(engine, cycle_id):
    with engine.begin() as conn:
        conn.execute(cycles.queue.update().where(cycles.queue.c.cycle_id == cycle_id).values(
            lease_until=workflow.utcnow() - timedelta(seconds=1)))


def test_concurrent_requests_and_duplicates_admit_only_fifo_head(serial_engine):
    engine = serial_engine
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda n: request(engine, f"fixture-{n % 6}"), range(12)))
    summary = cycles.cycle_summary(engine)
    assert len(summary["queue"]) == len(summary["cycles"]) == 6
    head = summary["queue"][0]["cycle_id"]
    assert [r["cycle_id"] for r in summary["cycles"] if r["status"] == "discovering"] == [head]
    assert len(queued(engine, "L1CycleDiscoveryRequested")) == 1
    # Duplicate requests cannot move the FIFO head or produce more commands.
    request(engine, head.removeprefix("cycle-manual-"))
    assert len(queued(engine, "L1CycleDiscoveryRequested")) == 1
    for expected in summary["queue"]:
        assert state(engine, expected["cycle_id"])["status"] == "discovering"
        failed(engine, expected["cycle_id"])
        cycles.reconcile(engine)
    assert cycles.reconcile(engine)["status"] == "idle"


def test_incomplete_scheduler_receipts_do_not_starve_ready_manual_cycle(serial_engine, monkeypatch):
    engine = serial_engine
    fake_cycle(monkeypatch)
    at = "2026-09-16T00:00:00Z"
    original = []
    first = envelope("AdapterRunRequested", {"adapter": "alpha"}, producer="eventbridge", ts=at)
    scheduled, _ = cycles.scheduled_child(engine, first)
    cycles.advance(engine, scheduled)
    manual = request(engine, "manual")
    assert state(engine, manual)["status"] == "discovering"
    second = envelope("AdapterRunRequested", {"adapter": "beta"}, producer="eventbridge", ts=at)
    cycles.scheduled_child(engine, second)
    cycles.advance(engine, scheduled)
    assert state(engine, scheduled)["status"] == "queued"
    original = [(c["event_id"], c["event_time"]) for c in cycles.cycle_summary(engine, scheduled)["children"]]
    failed(engine, manual)
    cycles.reconcile(engine)
    assert state(engine, scheduled)["status"] == "discovering"
    assert original == [(first.event_id, at), (second.event_id, at)]
    assert original == [(c["event_id"], c["event_time"]) for c in cycles.cycle_summary(engine, scheduled)["children"]]


def test_expired_lease_retries_same_event_then_failure_wakes_next(serial_engine):
    engine = serial_engine
    first, second = request(engine, "first"), request(engine, "second")
    event = queued(engine, "L1CycleDiscoveryRequested")[0]
    for _ in range(workflow.MAX_ATTEMPTS):
        with engine.begin() as conn:
            conn.execute(events.update().where(events.c.event_id == event.event_id).values(relayed_at=workflow.utcnow()))
        expire(engine, first)
        assert cycles.reconcile(engine)["active_cycle_id"] == first
        with engine.connect() as conn:
            assert conn.execute(sa.select(events.c.relayed_at).where(events.c.event_id == event.event_id)).scalar() is None
        assert len(queued(engine, "L1CycleDiscoveryRequested")) == 1
    expire(engine, first)
    assert cycles.reconcile(engine)["active_cycle_id"] == second
    assert state(engine, first)["result"]["reason"] == "cycle_recovery_attempts_exhausted"
    assert not state(engine, first)["result"]["accepted_release"]


def test_heartbeat_extends_lease_but_never_steals_executing_handler(serial_engine):
    engine = serial_engine
    first, second = request(engine, "first"), request(engine, "second")
    command = queued(engine, "L1CycleDiscoveryRequested")[0]
    expire(engine, first)
    with cycles.operation_guard(engine, command):
        assert cycles.cycle_summary(engine, first)["queue"][0]["heartbeat_at"]
        expire(engine, first)  # simulate a failed heartbeat while network is slow
        failed(engine, first)
        assert cycles.reconcile(engine)["status"] == "executing"
        assert state(engine, second)["status"] == "queued"
    assert cycles.reconcile(engine)["active_cycle_id"] == second


@pytest.mark.parametrize("kind", ["AdapterRunRequested", "L1InventoryAuditRequested", "TranslationRequested"])
def test_direct_operations_and_cycles_cannot_overlap(serial_engine, kind):
    engine = serial_engine
    command = envelope(kind, {"adapter": "fixture"})
    with cycles.operation_guard(engine, command):
        cycle_id = request(engine, "queued-behind-direct")
        assert state(engine, cycle_id)["status"] == "queued"
        assert not queued(engine, "L1CycleDiscoveryRequested")
    cycles.reconcile(engine)
    with pytest.raises(workflow.RetryDeferred, match="Direct L1 work"):
        with cycles.operation_guard(engine, command):
            pytest.fail("Direct operation bypassed whole-cycle slot")
    if kind != "AdapterRunRequested":
        borrowed = command.model_copy(update={"payload": {"cycle_id": cycle_id}})
        with pytest.raises(workflow.RetryDeferred, match="Direct L1 work"):
            with cycles.operation_guard(engine, borrowed):
                pytest.fail("Standalone operation borrowed a cycle ID")


def test_runtime_change_waits_for_running_handler_then_requires_new_id(serial_engine, monkeypatch):
    engine = serial_engine
    first = request(engine, "old")
    old_queued = request(engine, "old-queued")
    command = queued(engine, "L1CycleDiscoveryRequested")[0]
    with cycles.operation_guard(engine, command):
        monkeypatch.setenv("CLHEAR_CODE_REVISION", "b" * 40)
        new = request(engine, "new")
        assert cycles.reconcile(engine)["status"] == "executing"
        assert state(engine, first)["status"] == "discovering"
    assert cycles.reconcile(engine)["active_cycle_id"] == new
    assert state(engine, first)["result"]["reason"] == "worker_revision_changed_requires_new_cycle"
    assert state(engine, old_queued)["status"] == "failed"
    assert request(engine, "old") == first
    assert state(engine, first)["status"] == "failed"


def test_bootstrap_retires_old_runtime_without_admitting_waiting_cycles(serial_engine, monkeypatch):
    engine = serial_engine
    old = request(engine, "old")
    monkeypatch.setenv("CLHEAR_CODE_REVISION", "b" * 40)
    # Enqueue under a running fence so normal request handling cannot admit it.
    with cycles._execution_fence(engine):
        new = request(engine, "new")
    assert cycles.reconcile(engine, admit=False)["status"] == "idle"
    assert state(engine, old)["status"] == "failed"
    assert state(engine, new)["status"] == "queued"
    with cycles.operation_guard(engine, envelope("AdapterRunRequested")):
        pass  # deployment FINRA verification remains possible while fleets held
    assert cycles.reconcile(engine)["active_cycle_id"] == new
    assert cycles.reconcile(engine, admit=False)["status"] == "active"


def test_bounded_guard_contention_never_marks_cycle_failed(serial_engine):
    engine = serial_engine
    cycle_id = request(engine, "active")
    for error in [workflow.LeaseBusy("busy"), workflow.RetryDeferred("queued"), cycles.CycleRevisionChanged("old")]:
        cycles.failed_phase(engine, cycle_id, "discovery", error, 1000)
    assert state(engine, cycle_id)["status"] == "discovering"


def test_normal_completion_commits_result_before_admitting_next(serial_engine, monkeypatch):
    engine = serial_engine
    fake_cycle(monkeypatch, ("alpha",))
    first, _ = start_and_plan(engine, monkeypatch)
    second = request(engine, "second")
    for command in queued(engine, "AdapterRunRequested"):
        dispatch(engine, monkeypatch, command, "l1")
    result = finish(engine, monkeypatch)
    assert result["status"] == "completed_for_review" and not result["accepted_release"]
    assert state(engine, second)["status"] == "queued"
    assert cycles.reconcile(engine)["active_cycle_id"] == second
    assert cycles.cycle_summary(engine, first)["queue"][0]["finished_at"] is not None
    assert not queued(engine, "PublishReleaseRequested")


def test_running_recovery_reuses_unfinished_child_commands(serial_engine, monkeypatch):
    engine = serial_engine
    fake_cycle(monkeypatch)
    cycle_id, _ = start_and_plan(engine, monkeypatch)
    commands = queued(engine, "AdapterRunRequested")
    dispatch(engine, monkeypatch, commands[0], "l1")
    with engine.begin() as conn:
        conn.execute(events.update().where(events.c.event_id.in_([c.event_id for c in commands])).values(relayed_at=workflow.utcnow()))
    expire(engine, cycle_id)
    cycles.reconcile(engine)
    with engine.connect() as conn:
        rows = {str(r.event_id): r.relayed_at for r in conn.execute(sa.select(events).where(
            events.c.event_id.in_([c.event_id for c in commands])))}
    assert rows[commands[0].event_id] is not None  # completed child stays completed
    assert rows[commands[1].event_id] is None
    assert {c.event_id for c in queued(engine, "AdapterRunRequested")} == {c.event_id for c in commands}


def test_late_discovery_cannot_run_during_final_evaluation(serial_engine):
    engine = serial_engine
    cycle_id = request(engine, "fixture")
    discovery = queued(engine, "L1CycleDiscoveryRequested")[0]
    with engine.begin() as conn:
        conn.execute(cycles.cycles.update().where(cycles.cycles.c.cycle_id == cycle_id).values(status="evaluating"))
    with pytest.raises(workflow.RetryDeferred, match="cycle phase"):
        with cycles.operation_guard(engine, discovery):
            pytest.fail("Old discovery command entered final evaluation")


def test_phase_requires_exact_durable_l0_command(serial_engine):
    engine = serial_engine
    request(engine, "fixture")
    discovery = queued(engine, "L1CycleDiscoveryRequested")[0]
    for forged in [discovery.model_copy(update={"event_id": str(uuid.uuid4())}),
                   discovery.model_copy(update={"payload": {**discovery.payload, "changed": True}})]:
        with pytest.raises(ValueError, match="durable L0 request"):
            with cycles.operation_guard(engine, forged):
                pytest.fail("Unbound phase command was executed")


def hold_from_process(url, ready):
    engine = db.make_engine(url)
    try:
        with cycles._execution_fence(engine):
            ready.set()
            time.sleep(20)
    finally:
        engine.dispose()


def test_process_death_releases_execution_fence_for_durable_recovery(serial_engine):
    engine = serial_engine
    first, second = request(engine, "first"), request(engine, "second")
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    child = ctx.Process(target=hold_from_process, args=(engine.url.render_as_string(hide_password=False), ready))
    child.start()
    try:
        assert ready.wait(10)
        failed(engine, first)
        assert cycles.reconcile(engine)["status"] == "executing"
        child.terminate()
        child.join(10)
        assert not child.is_alive()
        assert cycles.reconcile(engine)["active_cycle_id"] == second
    finally:
        if child.is_alive():
            child.terminate()
        child.join(10)
