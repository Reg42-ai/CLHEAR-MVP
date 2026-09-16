"""Explicit routing, per-event relay commits, the deferred-delivery ledger and
bounded queue recovery — the machinery that stops held or misrouted messages
circulating through the fleet queues into the dead-letter queue."""
import json
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.clhear import workers
from app.clhear.models import events
from app.clhear.platform import deferred, queue_recovery, routing
from app.clhear.platform.events import Envelope, emit, relay_once
from app.clhear.settings import get_settings


def _envelope(kind, layer="l1", producer="test", ts=None, **payload):
    return Envelope(event_id=f"e-{kind}-{len(payload)}", layer=layer, kind=kind, subject_ref="s", payload=payload,
                    producer=producer, ts=datetime.now(timezone.utc).isoformat() if ts is None else ts)


class Transport:
    def __init__(self, fail_on=()):
        self.sent, self.fail_on = [], set(fail_on)

    def send(self, body):
        kind = json.loads(body)["kind"]
        if kind in self.fail_on:
            raise RuntimeError(f"transport refused {kind}")
        self.sent.append(kind)


class FakeSqs:
    """Enough of SQS for recovery: per-queue FIFO lists, receive/delete/send."""
    def __init__(self, queues):
        self.queues = {url: list(msgs) for url, msgs in queues.items()}
        self.sent, self.deleted = [], []
        self.redrive = {}

    def get_queue_attributes(self, QueueUrl, AttributeNames):
        return {"Attributes": {"RedrivePolicy": self.redrive[QueueUrl]}} if QueueUrl in self.redrive else {"Attributes": {}}

    def get_queue_url(self, QueueName):
        return {"QueueUrl": next(u for u in self.queues if u.endswith("/" + QueueName))}

    def receive_message(self, QueueUrl, MaxNumberOfMessages, **_):
        batch = self.queues[QueueUrl][:MaxNumberOfMessages]
        return {"Messages": [{"MessageId": m["id"], "ReceiptHandle": "rh-" + m["id"], "Body": m["body"], "MD5OfBody": "x",
                              "Attributes": {"ApproximateReceiveCount": "7", "SentTimestamp": "1"}} for m in batch]}

    def delete_message_batch(self, QueueUrl, Entries):
        ids = {e["ReceiptHandle"][3:] for e in Entries}
        self.queues[QueueUrl] = [m for m in self.queues[QueueUrl] if m["id"] not in ids]
        self.deleted.extend(sorted(ids))
        return {"Successful": [{"Id": e["Id"]} for e in Entries]}

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append((QueueUrl.rsplit("/", 1)[-1], json.loads(MessageBody)["kind"]))
        self.queues.setdefault(QueueUrl, []).append({"id": f"resent-{len(self.sent)}", "body": MessageBody})


# --------------------------------------------------------------------------- routing table

def test_every_handled_command_has_exactly_one_owner_and_audit_events_have_none():
    commands = {k for k in workers.HANDLERS if not k.startswith("clhear.")}
    assert commands <= set(routing.COMMAND_OWNERS), commands - set(routing.COMMAND_OWNERS)
    assert set(routing.COMMAND_OWNERS.values()) <= {"l0", "l1"}
    assert routing.classify("SourceChanged") == ("audit", None)
    assert routing.classify("clhear.l2.changed") == ("layer_event", None) and routing.consumers_for("clhear.l2.changed") == {"l3", "l4", "l5"}
    assert routing.classify("AdapterRunRequested") == ("command", "l1") and routing.consumers_for("AdapterRunRequested") == {"l1"}
    assert routing.classify("NoSuchKind") == ("unknown", None) and routing.consumers_for("NoSuchKind") == frozenset()
    assert "SourceChanged" not in routing.COMMAND_OWNERS  # never a default-to-L0 command again


def test_consumer_refuses_audit_unknown_and_foreign_kinds_with_distinct_reasons(monkeypatch):
    monkeypatch.setenv("CLHEAR_FLEET", "L0")
    with pytest.raises(workers.AuditOnlyKind):
        workers._owned_handler("SourceChanged", "l0")
    with pytest.raises(workers.UnknownKind):
        workers._owned_handler("NoSuchKind", "l0")
    with pytest.raises(workers.WrongFleet) as err:
        workers._owned_handler("AdapterRunRequested", "l0")
    assert not isinstance(err.value, (workers.UnknownKind, workers.AuditOnlyKind))
    assert workers._owned_handler("ViewerSnapshotRequested", "l0") is workers.HANDLERS["ViewerSnapshotRequested"]
    assert workers.deferral_reason(workers.AuditOnlyKind("x"))[0] == "audit_only"
    assert workers.deferral_reason(workers.UnknownKind("x"))[0] == "unknown_kind"
    assert workers.deferral_reason(workers.WrongFleet("x"))[0] == "wrong_owner"
    assert workers.deferral_reason(workers.L1AcceptanceHold("x"))[0] == "downstream_held"
    assert workers.deferral_reason(workers.MalformedDelivery("Scheduled event is missing occurrence timestamp"))[0] == "unidentifiable_schedule"
    assert workers.deferral_reason(workers.MalformedDelivery("Referenced outbox event or its original timestamp is unavailable"))[0] == "unresolvable_reference"
    assert workers.deferral_reason(workers.MalformedDelivery("not json"))[0] == "malformed"


# --------------------------------------------------------------------------- relay

def _outbox(engine, kinds):
    with engine.begin() as conn:
        for kind in kinds:
            emit(conn, layer="l1", kind=kind, subject_ref="s", payload={"n": kinds.index(kind)}, producer="test")


def _dispositions(engine):
    with engine.connect() as conn:
        return [(r.kind, r.relay_disposition, r.relayed_at is not None) for r in
                conn.execute(sa.select(events).order_by(events.c.id))]


def test_relay_dispatches_commands_records_audit_rows_and_quarantines_unknown_kinds(engine):
    _outbox(engine, ["SourceChanged", "ViewerSnapshotRequested", "NoSuchKind", "AdapterRunRequested", "clhear.l2.changed"])
    transport = Transport()
    assert relay_once(engine, transport) == 3
    assert transport.sent == ["ViewerSnapshotRequested", "AdapterRunRequested", "clhear.l2.changed"]
    assert _dispositions(engine) == [("SourceChanged", "audit_only", True), ("ViewerSnapshotRequested", "sent", True),
                                     ("NoSuchKind", "quarantined", True), ("AdapterRunRequested", "sent", True),
                                     ("clhear.l2.changed", "sent", True)]
    ledger = deferred.counts(engine)
    assert ledger["total"] == 1 and ledger["by_reason"] == {"unknown_kind": 1} and ledger["by_status"] == {"quarantined": 1}
    with engine.connect() as conn:
        row = conn.execute(sa.select(deferred.deferred_deliveries)).mappings().one()
        assert row["channel"] == "outbox" and row["event_kind"] == "NoSuchKind" and row["fleet"] == "l0"
        assert json.loads(deferred.body_for(conn, row["payload_hash"]))["kind"] == "NoSuchKind"
    # the audit record is preserved as evidence and never dispatched, even on a second pass
    assert relay_once(engine, transport) == 0 and transport.sent.count("SourceChanged") == 0


def test_relay_commits_per_event_so_one_refused_row_never_resends_the_rows_before_it(engine):
    _outbox(engine, ["ViewerSnapshotRequested", "AdapterRunRequested", "clhear.l2.changed", "L1InventoryAuditRequested"])
    transport = Transport(fail_on={"clhear.l2.changed"})
    with pytest.raises(RuntimeError):
        relay_once(engine, transport)
    assert transport.sent == ["ViewerSnapshotRequested", "AdapterRunRequested"]
    assert _dispositions(engine)[:2] == [("ViewerSnapshotRequested", "sent", True), ("AdapterRunRequested", "sent", True)]
    assert _dispositions(engine)[2] == ("clhear.l2.changed", None, False)
    with pytest.raises(RuntimeError):
        relay_once(engine, transport)
    assert transport.sent == ["ViewerSnapshotRequested", "AdapterRunRequested"]  # not sent twice
    transport.fail_on.clear()
    assert relay_once(engine, transport) == 2
    assert transport.sent[-2:] == ["clhear.l2.changed", "L1InventoryAuditRequested"]
    assert all(done for _, _, done in _dispositions(engine))


def test_routed_transport_has_no_default_queue(monkeypatch):
    transport = workers.RoutedOutboxTransport.__new__(workers.RoutedOutboxTransport)
    transport.queues = {"l0": "https://sqs/l0", "l1": "https://sqs/l1"}
    sent = []
    transport.sqs = type("S", (), {"send_message": lambda self, QueueUrl, MessageBody: sent.append(QueueUrl)})()
    transport.send(_envelope("AdapterRunRequested").model_dump_json())
    transport.send(_envelope("ViewerSnapshotRequested", layer="l0").model_dump_json())
    assert sent == ["https://sqs/l1", "https://sqs/l0"]
    with pytest.raises(workers.WrongFleet):
        transport.send(_envelope("SourceChanged").model_dump_json())
    with pytest.raises(workers.WrongFleet):
        transport.send(_envelope("NoSuchKind").model_dump_json())
    assert sent == ["https://sqs/l1", "https://sqs/l0"]


# --------------------------------------------------------------------------- deferred ledger

def test_deferred_ledger_keeps_exact_message_identity_hash_and_metadata_once(engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_FLEET", "L0")
    monkeypatch.setenv("CLHEAR_L1_ONLY", "true")
    get_settings.cache_clear()
    body = _envelope("clhear.l2.changed", layer="l2", producer="l2.change", change="added").model_dump_json()
    with pytest.raises(workers.L1AcceptanceHold) as held:
        workers.handle_envelope(engine, None, body)
    reason, detail = workers.deferral_reason(held.value)
    message = {"MessageId": "m-1", "Body": body, "MD5OfBody": "abc", "Attributes": {"ApproximateReceiveCount": "4", "SentTimestamp": "1700000000000"}}
    first = workers.defer_message(engine, fleet="l0", queue_url="https://sqs.us-east-1.amazonaws.com/1/clhear-fleet-l0", message=message, reason=reason, detail=detail)
    again = workers.defer_message(engine, fleet="l0", queue_url="https://sqs.us-east-1.amazonaws.com/1/clhear-fleet-l0", message=message, reason=reason, detail=detail)
    assert first["duplicate"] is False and again["duplicate"] is True and again["id"] == first["id"]
    with engine.connect() as conn:
        row = conn.execute(sa.select(deferred.deferred_deliveries)).mappings().one()
        stored = deferred.body_for(conn, row["payload_hash"])
    assert stored == body and row["payload_hash"] == deferred.payload_hash(body)
    assert row["event_kind"] == "clhear.l2.changed" and row["event_id"] == "e-clhear.l2.changed-1" and row["event_ts"]
    assert row["queue"] == "clhear-fleet-l0" and row["queue_metadata"]["ApproximateReceiveCount"] == "4"
    assert row["reason"] == "downstream_held" and row["status"] == "deferred" and row["resolved_at"] is None
    # nothing downstream ran: no delivery marker, no run for the held kind
    from app.clhear.l1 import workflow
    with engine.connect() as conn:
        assert not conn.execute(sa.select(workflow.deliveries)).first()
    # the same body arriving on another queue is a second delivery of one stored body
    other = workers.defer_message(engine, fleet="l3", queue_url="https://sqs/clhear-fleet-l3", message={**message, "MessageId": "m-2"}, reason=reason)
    assert other["duplicate"] is False and other["payload_hash"] == first["payload_hash"]
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(deferred.deferred_bodies)).scalar_one() == 1
        assert conn.execute(sa.select(sa.func.count()).select_from(deferred.deferred_deliveries)).scalar_one() == 2
    assert deferred.counts(engine)["by_kind"] == {"clhear.l2.changed": 2}


def test_malformed_and_unidentifiable_scheduled_deliveries_are_quarantined_not_repaired(engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_FLEET", "L1")
    monkeypatch.setenv("CLHEAR_L1_ONLY", "true")
    get_settings.cache_clear()
    with pytest.raises(workers.MalformedDelivery):
        workers.handle_envelope(engine, None, "this is not an envelope")
    scheduled = json.loads(_envelope("AdapterRunRequested", producer="eventbridge").model_dump_json())
    scheduled["ts"] = ""
    with pytest.raises(workers.MalformedDelivery) as err:
        workers.handle_envelope(engine, None, json.dumps(scheduled))
    assert workers.deferral_reason(err.value)[0] == "unidentifiable_schedule"
    entry = workers.defer_message(engine, fleet="l1", queue_url="https://sqs/clhear-events",
                                  message={"MessageId": "m-bad", "Body": "this is not an envelope"}, reason="malformed")
    assert entry["status"] == "quarantined" and entry["duplicate"] is False
    with engine.connect() as conn:
        row = conn.execute(sa.select(deferred.deferred_deliveries)).mappings().one()
    assert row["event_kind"] is None and row["event_ts"] is None and row["status"] == "quarantined"


# --------------------------------------------------------------------------- queue recovery

def _queues():
    base = "https://sqs.us-east-1.amazonaws.com/1/"
    return {name: base + f"clhear-fleet-{name}" for name in ("l0", "l2", "l3")} | {"l1": base + "clhear-events"}


def test_queue_recovery_gives_every_message_a_disposition_before_deleting_and_is_bounded(engine, monkeypatch):
    urls = _queues()
    dlq = "https://sqs.us-east-1.amazonaws.com/1/clhear-events-dlq"
    held = _envelope("clhear.l2.changed", layer="l2", producer="l2.change").model_dump_json()
    audit = _envelope("SourceChanged").model_dump_json()
    command = _envelope("AdapterRunRequested").model_dump_json()
    unknown = _envelope("NoSuchKind").model_dump_json()
    scheduled = json.loads(_envelope("AdapterRunRequested", producer="eventbridge").model_dump_json()); scheduled["ts"] = ""
    reference = json.dumps({"event_id": "00000000-0000-0000-0000-000000000000", "layer": "l1", "kind": "AdapterRunRequested", "envelope_sha256": "0" * 64})
    sqs = FakeSqs({
        urls["l0"]: [{"id": "l0-1", "body": held}, {"id": "l0-2", "body": audit}, {"id": "l0-3", "body": command}],
        urls["l1"]: [], urls["l2"]: [{"id": "l2-1", "body": held}], urls["l3"]: [],
        dlq: [{"id": "d-1", "body": held}, {"id": "d-2", "body": unknown}, {"id": "d-3", "body": "garbage"},
              {"id": "d-4", "body": json.dumps(scheduled)}, {"id": "d-5", "body": reference}, {"id": "d-6", "body": command}],
    })
    sqs.redrive[urls["l0"]] = json.dumps({"deadLetterTargetArn": "arn:aws:sqs:us-east-1:1:clhear-events-dlq", "maxReceiveCount": 5})
    queues = queue_recovery.queue_urls(sqs, urls)
    assert queues["dlq"] == dlq

    first = queue_recovery.recover(engine, sqs=sqs, queues=queues, max_messages=4, hold_downstream=True)
    assert first["processed"] == 4 and first["bound_reached"] == "max_messages" and not first["exhausted"]
    assert len(sqs.deleted) == 4  # deletes follow the committed ledger rows, and only those
    remaining = sum(len(v) for v in sqs.queues.values())
    second = queue_recovery.recover(engine, sqs=sqs, queues=queues, max_messages=500, hold_downstream=True)
    assert second["exhausted"] and second["bound_reached"] is None
    assert first["processed"] + second["processed"] == 10 and remaining == 7
    assert len(sqs.queues[urls["l1"]]) == 2  # the two rerouted commands now wait on the L1 queue
    assert second["queues"]["l1"]["left_for_owner"] == 1 and second["queues"]["l1"]["received"] == 0  # the L1 queue is visited before the DLQ; its command stays for L1
    by_reason = {}
    for report in (first, second):
        for k, v in report["by_reason"].items():
            by_reason[k] = by_reason.get(k, 0) + v
    assert by_reason == {"downstream_held": 3, "audit_only": 1, "wrong_owner": 2, "unknown_kind": 1, "malformed": 1,
                         "unidentifiable_schedule": 1, "unresolvable_reference": 1}
    assert sqs.sent == [("clhear-events", "AdapterRunRequested"), ("clhear-events", "AdapterRunRequested")]
    ledger = deferred.counts(engine)
    assert ledger["total"] == 10 and ledger["by_status"] == {"deferred": 4, "rerouted": 2, "quarantined": 4}
    with engine.connect() as conn:
        rows = conn.execute(sa.select(deferred.deferred_deliveries).order_by(deferred.deferred_deliveries.c.id)).mappings().all()
        bodies = conn.execute(sa.select(sa.func.count()).select_from(deferred.deferred_bodies)).scalar_one()
    assert bodies == 7  # seven distinct bodies; the held event on three queues is stored once
    assert {r["channel"] for r in rows} == {"sqs", "dlq"}
    rerouted = [r for r in rows if r["status"] == "rerouted"]
    assert all(r["resolution"]["owner"] == "l1" and r["resolution"]["queue"] == "clhear-events" for r in rerouted)
    assert all(r["queue_metadata"]["ApproximateReceiveCount"] == "7" for r in rows)
    # nothing was executed for the held events, and the ledger id can replay them later
    from app.clhear.l1 import workflow
    with engine.connect() as conn:
        assert not conn.execute(sa.select(workflow.deliveries)).first()
    # a third pass finds nothing to recover and leaves the rerouted commands for their owner
    third = queue_recovery.recover(engine, sqs=sqs, queues=queues, max_messages=500)
    assert third["processed"] == 0 and third["exhausted"] and len(sqs.queues[urls["l1"]]) == 2


def test_queue_recovery_survives_a_crash_between_ledger_commit_and_delete(engine):
    urls = _queues()
    command = _envelope("ViewerSnapshotRequested", layer="l0").model_dump_json()
    sqs = FakeSqs({urls["l1"]: [{"id": "x-1", "body": command}], urls["l0"]: []})
    real_delete = sqs.delete_message_batch
    calls = []

    def crash(QueueUrl, Entries):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("network")
        return real_delete(QueueUrl, Entries)
    sqs.delete_message_batch = crash
    with pytest.raises(RuntimeError):
        queue_recovery.recover(engine, sqs=sqs, queues={"l0": urls["l0"], "l1": urls["l1"]}, only=["l1"])
    assert sqs.sent == [("clhear-fleet-l0", "ViewerSnapshotRequested")]
    report = queue_recovery.recover(engine, sqs=sqs, queues={"l0": urls["l0"], "l1": urls["l1"]}, only=["l1"])
    assert report["queues"]["l1"]["duplicates"] == 1 and report["queues"]["l1"]["deleted"] == 1
    assert sqs.sent == [("clhear-fleet-l0", "ViewerSnapshotRequested")]  # not sent home twice
    assert deferred.counts(engine)["total"] == 1


def test_recovery_cli_is_l0_only_durable_and_idempotent(engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_FLEET", "L1")
    with pytest.raises(SystemExit) as err:
        workers.cli(["--recover-queues", "--verification-id", "rec-1"])
    assert err.value.code == 1
    monkeypatch.setenv("CLHEAR_FLEET", "L0")
    monkeypatch.setenv("CLHEAR_L1_ONLY", "true")
    monkeypatch.setenv("CLHEAR_FLEET_QUEUE_URLS", json.dumps(_queues()))
    get_settings.cache_clear()
    from app.clhear import db
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(db, "run_migrations", lambda e: [])
    fake = FakeSqs({url: [] for url in _queues().values()})
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: fake)
    first = workers.recover_queues_once("rec-1", max_messages=10)
    assert first["status"] == "recovered" and first["exhausted"] and first["downstream"] == "held" and first["ledger"]["available"]
    again = workers.recover_queues_once("rec-1", max_messages=10)
    assert again["status"] == "already_recovered"
    assert workers.recover_queues_once("bad id!")["status"] == "failed"


# --------------------------------------------------------------------------- progress surfaces

def test_progress_record_keeps_the_four_states_apart_and_is_served_by_the_viewer(engine, client, monkeypatch):
    from app.clhear.l1 import progress
    monkeypatch.delenv("CLHEAR_VIEWER_SNAPSHOT_S3_URI", raising=False)
    monkeypatch.delenv("CLHEAR_DB_S3_URI", raising=False)
    body = client.get("/api/clhear/l1/progress").json()
    assert body["schema"] == "clhear.l1-progress.v1" and body["served_from"] == "live_database"
    assert set(body["states"]) == {"deployment", "corpus_verification", "publisher_permissions", "nightly_validation"}
    assert body["states"]["deployment"]["evidence_mode"] == "deployment_verification"
    assert body["ready_for_private_review"]["ready"] is False and body["ready_for_private_review"]["corpus_acceptance"] == "not_claimed"
    assert body["deferred_messages"]["available"] and body["binding_waits"] == 0
    cycles = client.get("/api/clhear/l1/cycles").json()
    assert {"deferred_messages", "binding_waits", "verification_progress"} <= set(cycles)
    # publication: rate-limited, only when something changed, beside the candidate viewer
    monkeypatch.setenv("CLHEAR_VIEWER_SNAPSHOT_S3_URI", "s3://bucket/webui/l1/candidate.db")
    puts = []
    s3 = type("S3", (), {"put_object": lambda self, **kw: puts.append(kw)})()
    progress._last_publish.update(at=0.0, hash=None)
    first = progress.publish(engine, s3_client=s3)
    assert first["uri"] == "s3://bucket/webui/l1/progress.json" and puts[0]["Key"] == "webui/l1/progress.json"
    assert puts[0]["ServerSideEncryption"] == "AES256" and json.loads(puts[0]["Body"])["schema"] == "clhear.l1-progress.v1"
    assert progress.publish(engine, s3_client=s3) is None and len(puts) == 1  # inside the interval
    progress._last_publish["at"] = 0.0
    assert progress.publish(engine, s3_client=s3) is None and len(puts) == 1  # unchanged content
    assert progress.publish(engine, s3_client=s3, force=True) is not None and len(puts) == 2
    # the viewer prefers the published record when one exists
    published = json.loads(puts[-1]["Body"])
    monkeypatch.setattr(progress, "read_published", lambda **kw: published)
    served = client.get("/api/clhear/l1/progress").json()
    assert served["served_from"] == "l0_published_record" and served["generated_at"] == published["generated_at"]
    assert client.get("/api/clhear/l1/progress?live=true").json()["served_from"] == "live_database"
