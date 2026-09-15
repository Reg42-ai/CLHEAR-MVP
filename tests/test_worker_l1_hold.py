"""An L1 hold must stop downstream consumption before SQS counts a receive."""
from contextlib import nullcontext
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
import sqlalchemy as sa

from app.clhear import db, workers
from app.clhear.l1 import workflow
from app.clhear.models import runs
from app.clhear.platform import errors, events, router
from app.clhear.settings import get_settings


class StopWorker(BaseException):
    """Stop the actual infinite worker loop without its retry handler swallowing it."""


@pytest.fixture
def runtime(monkeypatch):
    settings = SimpleNamespace(
        clhear_l1_only=True,
        clhear_snapshot_s3_uri="",
        database_url="postgresql://unused",
        aws_region="us-east-1",
        clhear_events_queue_url="https://sqs.example.test/fleet",
    )
    monkeypatch.setattr(workers, "get_settings", Mock(return_value=settings))
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    spies = {}
    for module, name in (
        (db, "get_engine"), (db, "run_migrations"), (db, "dispose_engine"),
        (errors, "init"), (router, "build_providers"),
        (router, "record_missing_providers"), (router, "Router"),
        (events, "SqsTransport"), (events, "relay_once"),
        (workers, "RoutedOutboxTransport"), (workers, "_snapshot_pull"),
        (workers, "_snapshot_push"), (workers, "handle_envelope"),
        (boto3, "client"),
    ):
        spies[name] = Mock(name=name)
        monkeypatch.setattr(module, name, spies[name])
    spies["build_providers"].return_value = {"test": object()}
    spies["handle_envelope"].return_value = {"processed": True}
    message = {"Body": "test envelope", "ReceiptHandle": "receipt"}
    queue = Mock()
    queue.receive_message.side_effect = [{"Messages": [message]}, StopWorker()]
    spies["client"].return_value = queue
    monkeypatch.setattr(workflow, "heartbeat", lambda callback: nullcontext())
    # Three actual hold iterations demonstrate that waiting does not consume a
    # message later. In a polling regression, StopWorker instead ends receive #2.
    sleep = Mock(side_effect=[None, None, StopWorker()])
    monkeypatch.setattr(workers.time, "sleep", sleep)
    return settings, spies, queue, sleep


@pytest.mark.parametrize("fleet", [f"L{layer}" for layer in range(2, 9)])
def test_l1_hold_pauses_downstream_before_startup_or_receive(runtime, monkeypatch, caplog, fleet):
    settings, spies, queue, sleep = runtime
    monkeypatch.setenv("CLHEAR_FLEET", fleet)
    # Even legacy snapshot configuration must remain untouched while held.
    settings.database_url = "sqlite:///unused.db"
    settings.clhear_snapshot_s3_uri = "s3://unused/snapshot.sqlite"
    with pytest.raises(StopWorker):
        workers.main()

    assert sleep.call_count == 3
    assert all(call.args == (60,) for call in sleep.call_args_list)
    for spy in spies.values():
        spy.assert_not_called()
    queue.receive_message.assert_not_called()
    queue.change_message_visibility.assert_not_called()
    queue.delete_message.assert_not_called()
    assert "L1 acceptance hold" in caplog.text
    assert fleet.lower() in caplog.text


@pytest.mark.parametrize("fleet,held", [("l0", True), ("l1", True)] + [
    (f"l{layer}", False) for layer in range(2, 9)
])
def test_allowed_fleets_keep_normal_poll_handle_and_ack(runtime, monkeypatch, fleet, held):
    settings, spies, queue, sleep = runtime
    settings.clhear_l1_only = held
    monkeypatch.setenv("CLHEAR_FLEET", fleet)
    with pytest.raises(StopWorker):
        workers.main()

    spies["get_engine"].assert_called_once_with()
    spies["run_migrations"].assert_called_once_with(spies["get_engine"].return_value)
    assert queue.receive_message.call_count == 2
    spies["handle_envelope"].assert_called_once_with(
        spies["get_engine"].return_value, spies["Router"].return_value, "test envelope",
    )
    queue.delete_message.assert_called_once_with(
        QueueUrl=settings.clhear_events_queue_url, ReceiptHandle="receipt",
    )
    assert spies["relay_once"].call_count == (2 if fleet == "l0" else 0)
    sleep.assert_not_called()


def _body(kind, event_id):
    return json.dumps({"event_id": event_id, "layer": "L1", "kind": kind,
                       "subject_ref": "test/source", "producer": "test", "ts": "2026-09-15T00:00:00Z"})


@pytest.mark.parametrize("kind", ["GraphRebuildRequested", "DrDrillRequested"])
def test_global_projection_work_is_held_before_index_or_projection(engine, monkeypatch, kind):
    monkeypatch.setenv("CLHEAR_L1_ONLY", "true")
    get_settings.cache_clear()
    handler = Mock()
    monkeypatch.setitem(workers.HANDLERS, kind, handler)
    with pytest.raises(workers.L1AcceptanceHold):
        workers.handle_envelope(engine, None, _body(kind, "held-rebuild"))
    handler.assert_not_called()
    with engine.connect() as conn:
        assert not conn.execute(sa.select(runs.c.id).where(runs.c.fleet == "worker")).first()


def test_completed_message_is_snapshotted_when_later_message_is_held(engine, monkeypatch):
    from app.clhear import releases

    snapshot = "s3://test-only-bucket/candidate.db"
    queue = "https://sqs.example.test/test-only-queue"
    monkeypatch.setenv("CLHEAR_L1_ONLY", "true")
    monkeypatch.setenv("CLHEAR_SNAPSHOT_S3_URI", snapshot)
    monkeypatch.setenv("CLHEAR_EVENTS_QUEUE_URL", queue)
    # main updates this variable after its mocked snapshot pull; restore it at teardown.
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    get_settings.cache_clear()
    region = get_settings().aws_region

    completed = {"Body": _body("AdapterRunRequested", "completed-source"), "ReceiptHandle": "ack-completed"}
    held = {"Body": _body("clhear.l1.changed", "held-change"), "ReceiptHandle": "keep-held"}
    sqs = Mock()
    sqs.receive_message.side_effect = [{"Messages": [completed, held]}, StopWorker()]
    client_factory = Mock(return_value=sqs)
    monkeypatch.setattr(boto3, "client", client_factory)
    monkeypatch.setattr(errors, "init", Mock())
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(db, "dispose_engine", Mock())
    monkeypatch.setattr(db, "run_migrations", Mock())
    monkeypatch.setattr(router, "build_providers", Mock(return_value={}))
    monkeypatch.setattr(router, "record_missing_providers", Mock())
    monkeypatch.setattr(router, "Router", Mock())
    monkeypatch.setattr(events, "SqsTransport", Mock())
    relay = Mock()
    monkeypatch.setattr(events, "relay_once", relay)
    pull, push, publish = Mock(), Mock(), Mock()
    monkeypatch.setattr(workers, "_snapshot_pull", pull)
    monkeypatch.setattr(workers, "_snapshot_push", push)
    monkeypatch.setattr(releases, "publish_release", publish)
    ingest = Mock(return_value={"ran": 1, "downstream": "held"})
    monkeypatch.setitem(workers.HANDLERS, "AdapterRunRequested", ingest)

    with pytest.raises(StopWorker):
        workers.main()

    ingest.assert_called_once()
    client_factory.assert_called_once_with("sqs", region_name=region)
    sqs.delete_message.assert_called_once_with(QueueUrl=queue, ReceiptHandle="ack-completed")
    pull.assert_called_once_with(snapshot, region)
    push.assert_called_once_with(snapshot, region)
    publish.assert_not_called()  # candidate snapshot is not an accepted release
    assert relay.call_count == 3  # before each receive plus completed-batch relay
    with engine.connect() as conn:
        handled = conn.execute(sa.select(runs.c.inputs).where(runs.c.fleet == "worker")).scalars().all()
    assert [row["event_id"] for row in handled] == ["completed-source"]
