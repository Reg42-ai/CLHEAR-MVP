"""HLD v2 §3 event plane: clhear.<layer>.<event> on the `clhear` bus, outbox → relay."""
import json

import pytest

from app.clhear.platform import events


class _FakeEventsClient:
    def __init__(self):
        self.entries = []

    def put_events(self, Entries):
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0}


def _eb(monkeypatch):
    client = _FakeEventsClient()
    transport = events.EventBridgeTransport.__new__(events.EventBridgeTransport)
    transport._client = client
    transport._bus = events.BUS_NAME
    transport._source = "clhear"
    return transport, client


def test_layer_event_kind_contract():
    assert events.layer_event_kind("L2", "derived") == "clhear.l2.derived"
    assert events.layer_event_kind("l6", "below_gate") == "clhear.l6.below_gate"
    with pytest.raises(ValueError):
        events.layer_event_kind("L2", "deleted")  # I2: nothing is ever deleted


def test_publish_layer_event_relays_to_bus_and_queue(engine, monkeypatch):
    eb, client = _eb(monkeypatch)
    queue = events.InMemoryTransport()
    with engine.begin() as conn:
        eid = events.publish_layer_event(
            conn, layer="L2", event="changed", subject_ref="OBL-000001",
            payload={"fields": ["canonical_statement"]}, producer="l2.change",
        )
        # a fleet job message (not a layer event) also goes through the outbox
        events.emit(conn, layer="l1", kind="AdapterRunRequested", subject_ref="fca", producer="test")
    sent = events.relay_once(engine, events.FanoutTransport(queue, eb))
    assert sent == 2
    assert len(queue.queue) == 2
    # EventBridge receives only the clhear.* layer events, with detail-type == kind
    assert len(client.entries) == 1
    entry = client.entries[0]
    assert entry["EventBusName"] == "clhear"
    assert entry["DetailType"] == "clhear.l2.changed"
    detail = json.loads(entry["Detail"])
    assert detail["event_id"] == eid
    assert detail["subject_ref"] == "OBL-000001"
    assert detail["schema_version"] == events.ENVELOPE_SCHEMA_VERSION


def test_relay_is_idempotent(engine):
    queue = events.InMemoryTransport()
    with engine.begin() as conn:
        events.publish_layer_event(conn, layer="L3", event="derived", subject_ref="BLK-000001", producer="l3")
    assert events.relay_once(engine, queue) == 1
    assert events.relay_once(engine, queue) == 0
