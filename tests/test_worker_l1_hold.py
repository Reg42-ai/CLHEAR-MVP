"""Acceptance holds retain events without discarding a batch's completed work."""
import json
import os
from unittest.mock import Mock

import pytest
import sqlalchemy as sa

from app.clhear import workers
from app.clhear.models import runs
from app.clhear.settings import get_settings


def _body(kind, event_id):
    return json.dumps({"event_id": event_id, "layer": "L1", "kind": kind,
                       "subject_ref": "test/source", "producer": "test", "ts": "2026-09-15T00:00:00Z"})


def test_graph_rebuild_is_held_before_index_or_projection(engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_L1_ONLY", "true")
    get_settings.cache_clear()
    handler = Mock()
    monkeypatch.setitem(workers.HANDLERS, "GraphRebuildRequested", handler)
    with pytest.raises(workers.L1AcceptanceHold):
        workers.handle_envelope(engine, None, _body("GraphRebuildRequested", "held-rebuild"))
    handler.assert_not_called()
    with engine.connect() as conn:
        assert not conn.execute(sa.select(runs.c.id).where(runs.c.fleet == "worker")).first()


def test_completed_message_is_snapshotted_when_later_message_is_held(engine, monkeypatch):
    import boto3
    from app.clhear import db, releases
    from app.clhear.platform import errors, events, router

    class StopWorker(BaseException):
        """Exit the infinite loop after the one controlled batch."""

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
