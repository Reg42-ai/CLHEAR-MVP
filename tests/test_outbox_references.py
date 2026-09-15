"""Recover oversized historical events without changing or dropping them."""
import json
import uuid
from unittest.mock import Mock

from botocore.exceptions import ClientError
import pytest
import sqlalchemy as sa

from app.clhear import workers
from app.clhear.l1 import workflow
from app.clhear.models import events as event_rows
from app.clhear.platform import events
from app.clhear.settings import get_settings


class LimitedQueue:
    def __init__(self):
        self.messages = []

    def send_message(self, **message):
        if len(message["MessageBody"].encode("utf-8")) >= 262144:
            raise ClientError({"Error": {"Code": "InvalidParameterValue",
                "Message": "Message must be shorter than 262144 bytes."}}, "SendMessage")
        self.messages.append(message)
        return {"MessageId": str(uuid.uuid4())}


def _row(engine, event_id):
    with engine.connect() as conn:
        return conn.execute(sa.select(event_rows).where(event_rows.c.event_id == event_id)).one()


def _large_event(engine, *, kind="CommunityWrite", layer="l0"):
    payload = {"operation": "test", "nested": {"z": "Ω" * 150000, "a": [3, 2, 1]}}
    with engine.begin() as conn:
        event_id = events.emit(conn, layer=layer, kind=kind, subject_ref="historical/event",
                               payload=payload, producer="regression")
    return event_id, payload


def _reference(engine, **kwargs):
    event_id, payload = _large_event(engine, **kwargs)
    envelope = events._row_to_envelope(_row(engine, event_id))
    return event_id, payload, events.transport_body(envelope)


def test_oversized_history_no_longer_blocks_snapshot_and_replays_original(engine, monkeypatch):
    import boto3
    queue = LimitedQueue()
    monkeypatch.setattr(boto3, "client", Mock(return_value=queue))
    transport = workers.RoutedOutboxTransport({"l0": "l0-queue", "l1": "l1-queue"}, "us-east-1")
    event_id, payload = _large_event(engine)
    original = events._row_to_envelope(_row(engine, event_id))
    # Reproduce the exact production queue failure with the old inline body.
    with pytest.raises(ClientError, match="shorter than 262144 bytes"):
        queue.send_message(QueueUrl="l0-queue", MessageBody=original.model_dump_json())
    with engine.begin() as conn:
        snapshot_id = events.emit(conn, layer="l0", kind="ViewerSnapshotRequested",
                                  subject_ref="viewer/current", producer="l1.worker")

    assert events.relay_once(engine, transport) == 2
    assert len(queue.messages) == 2
    reference, snapshot = [message["MessageBody"] for message in queue.messages]
    assert len(reference.encode("utf-8")) < 1024
    assert "transport_schema" in json.loads(reference)
    assert "transport_schema" not in json.loads(snapshot)
    assert _row(engine, event_id).payload == payload
    assert _row(engine, event_id).relayed_at is not None
    assert _row(engine, snapshot_id).relayed_at is not None
    assert events.resolve_envelope(engine, reference) == original
    assert events.relay_once(engine, transport) == 0

    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    monkeypatch.setenv("CLHEAR_L1_ONLY", "true")
    get_settings.cache_clear()
    community, refresh = Mock(return_value={"ok": True}), Mock(return_value={"published": True})
    monkeypatch.setitem(workers.HANDLERS, "CommunityWrite", community)
    monkeypatch.setitem(workers.HANDLERS, "ViewerSnapshotRequested", refresh)
    workers.handle_envelope(engine, None, reference)
    workers.handle_envelope(engine, None, snapshot)
    assert workers.handle_envelope(engine, None, reference) is None
    community.assert_called_once()
    refresh.assert_called_once()
    assert community.call_args.args[2] == original
    assert community.call_args.args[2].payload == payload
    assert refresh.call_args.args[2].event_id == snapshot_id


def test_reference_routes_layer_events_without_inlining_the_payload(engine, monkeypatch):
    import boto3
    client = Mock()
    client.put_events.return_value = {"FailedEntryCount": 0, "Entries": [{"EventId": "accepted"}]}
    monkeypatch.setattr(boto3, "client", Mock(return_value=client))
    transport = workers.RoutedOutboxTransport({"l0": "zero", "l1": "one"}, "us-east-1")
    event_id, payload = _large_event(engine, kind="clhear.l1.changed", layer="l1")
    assert events.relay_once(engine, transport) == 1
    entry = client.put_events.call_args.kwargs["Entries"][0]
    assert entry["DetailType"] == "clhear.l1.changed"
    assert len(entry["Detail"].encode("utf-8")) < 1024
    assert events.resolve_envelope(engine, entry["Detail"]).payload == payload
    client.send_message.assert_not_called()


@pytest.mark.parametrize("mutation", ["missing", "hash", "payload", "kind", "layer", "schema", "extra", "uuid"])
def test_invalid_reference_never_claims_delivery(engine, monkeypatch, mutation):
    event_id, _, body = _reference(engine)
    value = json.loads(body)
    if mutation == "missing":
        value["event_id"] = str(uuid.uuid4())
    elif mutation == "hash":
        value["envelope_sha256"] = "0" * 64
    elif mutation == "payload":
        with engine.begin() as conn:
            conn.execute(event_rows.update().where(event_rows.c.event_id == event_id).values(payload={"changed": True}))
    elif mutation in {"kind", "layer"}:
        value[mutation] = "different"
    elif mutation == "schema":
        value["transport_schema"] = "clhear.outbox-reference.v2"
    elif mutation == "extra":
        value["payload"] = {"injected": True}
    else:
        value["event_id"] = "not-a-uuid"
    claim = Mock()
    monkeypatch.setattr(workflow, "claim_delivery", claim)
    with pytest.raises(ValueError):
        workers.handle_envelope(engine, None, json.dumps(value))
    claim.assert_not_called()


@pytest.mark.parametrize("hold,expected", [(True, workers.L1AcceptanceHold), (False, workers.WrongFleet)])
def test_hydration_preserves_hold_and_ownership_checks(engine, monkeypatch, hold, expected):
    _, _, body = _reference(engine, kind="clhear.l1.changed", layer="l1")
    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    monkeypatch.setenv("CLHEAR_L1_ONLY", str(hold).lower())
    get_settings.cache_clear()
    claim = Mock()
    monkeypatch.setattr(workflow, "claim_delivery", claim)
    with pytest.raises(expected):
        workers.handle_envelope(engine, None, body)
    claim.assert_not_called()


def test_unicode_byte_limit_and_inline_compatibility():
    small = events.Envelope(event_id=str(uuid.uuid4()), layer="l0", kind="CommunityWrite",
        subject_ref="example", producer="test", ts="2026-09-15T16:00:00+00:00", payload={"unchanged": "Ω"})
    assert events.transport_body(small) == small.model_dump_json()
    large = small.model_copy(update={"payload": {"text": "Ω" * 130000}})
    assert len(large.model_dump_json()) < events.MAX_INLINE_ENVELOPE_BYTES
    assert len(large.model_dump_json().encode("utf-8")) > events.MAX_INLINE_ENVELOPE_BYTES
    assert isinstance(events.parse_transport(events.transport_body(large)), events.OutboxReference)


def test_reference_hash_is_stable_across_jsonb_key_order_and_timezone():
    first = events.Envelope(event_id=str(uuid.uuid4()), layer="l0", kind="CommunityWrite",
        subject_ref="example", producer="test", ts="2026-09-15T16:00:00+00:00",
        payload={"z": "Ω" * 150000, "a": {"y": [2, 1], "x": " exact \n wording "}})
    same = first.model_copy(update={"ts": "2026-09-15T19:00:00+03:00",
        "payload": {"a": {"x": " exact \n wording ", "y": [2, 1]}, "z": "Ω" * 150000}})
    assert events.transport_body(first) == events.transport_body(same)
    different = first.model_copy(update={"payload": {**first.payload, "a": {"y": [1, 2], "x": " exact \n wording "}}})
    assert events.transport_body(first) != events.transport_body(different)


def test_transport_rejection_retains_original_for_retry(engine, monkeypatch):
    event_id, payload = _large_event(engine)
    transport = Mock()
    transport.send.side_effect = RuntimeError("transport unavailable")
    with pytest.raises(RuntimeError, match="unavailable"):
        events.relay_once(engine, transport)
    row = _row(engine, event_id)
    assert row.relayed_at is None
    assert row.payload == payload
    transport.send.side_effect = None
    assert events.relay_once(engine, transport) == 1
    assert transport.send.call_args_list[0] == transport.send.call_args_list[1]
