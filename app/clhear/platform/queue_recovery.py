"""Bounded, resumable recovery of the fleet queues and the dead-letter queue.

Millions of messages accumulated while held layer events and audit records
circulated through queues nobody could consume. Recovery reads them in
bounded batches, gives every message a durable disposition in the
deferred-delivery ledger, and only then deletes it from the queue:

* a valid command goes back to its owning queue (the owner's delivery ledger
  makes a second delivery a no-op) and is recorded as ``rerouted``;
* a held layer event is recorded ``deferred`` and stays unexecuted while L2–L8
  are held;
* an audit-only record is recorded ``audit_only``;
* an unknown kind, a malformed body, an unresolvable outbox reference or a
  scheduled occurrence without its timestamp is ``quarantined`` — nothing
  invents scheduling evidence.

Nothing is purged, nothing is replayed blindly, and stopping at the bound
loses nothing: the queues themselves are the cursor, so the next run
continues where this one stopped.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

from app.clhear.platform import deferred, routing

DEFAULT_MAX_MESSAGES = 5000
DEFAULT_MAX_SECONDS = 900
HARD_MAX_MESSAGES = 200_000
RECEIVE_BATCH = 10
QUARANTINE_REASONS = frozenset({"unknown_kind", "malformed", "unidentifiable_schedule", "unresolvable_reference"})


def _now():
    return datetime.now(timezone.utc).isoformat()


def queue_urls(sqs, configured: dict[str, str]) -> dict[str, str]:
    """Fleet queues from the configured map, plus their dead-letter queue."""
    urls = dict(configured)
    for url in list(configured.values()):
        attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["RedrivePolicy"]).get("Attributes", {})
        policy = attrs.get("RedrivePolicy")
        if not policy:
            continue
        try:
            arn = json.loads(policy).get("deadLetterTargetArn", "")
        except ValueError:
            continue
        name = arn.rsplit(":", 1)[-1]
        if name and "dlq" not in urls:
            urls["dlq"] = sqs.get_queue_url(QueueName=name)["QueueUrl"]
    return urls


def classify_message(body: str) -> tuple[str, str, str | None]:
    """``(disposition, reason, owner)`` for one raw queue body."""
    identity = deferred._identity(body)
    if identity.get("malformed"):
        return "quarantined", "malformed", None
    kind = identity.get("kind")
    try:
        data = json.loads(body)
    except ValueError:
        data = {}
    if isinstance(data, dict) and "envelope_sha256" in data and "payload" not in data:
        # An outbox reference: only the L0 worker that owns the outbox can resolve
        # it, and only if the row still exists. Recovery does not replay it.
        return "quarantined", "unresolvable_reference", None
    if not kind:
        return "quarantined", "malformed", None
    if identity.get("producer") == "eventbridge" and not identity.get("ts"):
        return "quarantined", "unidentifiable_schedule", None
    category, owner = routing.classify(kind)
    if category == "command":
        if kind in routing.DOWNSTREAM_HELD_KINDS:
            return "deferred", "downstream_held", owner
        return "rerouted", "wrong_owner", owner
    if category == "layer_event":
        return "deferred", "downstream_held", None
    if category == "audit":
        return "deferred", "audit_only", None
    return "quarantined", "unknown_kind", None


def _owned_here(body: str, queue_name: str) -> bool:
    """True for a valid, unheld command sitting on the queue of the fleet that owns it."""
    disposition, _reason, owner = classify_message(body)
    return disposition == "rerouted" and owner == queue_name


def recover(engine: Engine, *, sqs, queues: dict[str, str], fleet: str = "l0", max_messages: int = DEFAULT_MAX_MESSAGES,
            max_seconds: int = DEFAULT_MAX_SECONDS, hold_downstream: bool = True, only: list[str] | None = None,
            clock=time.monotonic) -> dict:
    """One bounded pass. Returns counts per queue and disposition; ``exhausted``
    is True when every selected queue answered empty before the bound."""
    max_messages = max(1, min(int(max_messages), HARD_MAX_MESSAGES))
    started = clock()
    selected = {name: url for name, url in queues.items() if not only or name in only}
    report = {"started_at": _now(), "fleet": fleet, "max_messages": max_messages, "max_seconds": int(max_seconds),
              "queues": {}, "processed": 0, "by_disposition": {}, "by_reason": {}, "rerouted_to": {},
              "exhausted": False, "bound_reached": None}
    fleet_queue_by_name = {name: url for name, url in queues.items() if name != "dlq"}
    processed = 0
    empty = set()
    for name, url in selected.items():
        row = report["queues"].setdefault(name, {"received": 0, "deleted": 0, "duplicates": 0, "left_for_owner": 0, "empty": False})
        seen_owned: set[str] = set()
        while processed < max_messages and clock() - started < max_seconds:
            resp = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=RECEIVE_BATCH, VisibilityTimeout=120,
                                       WaitTimeSeconds=0, AttributeNames=["ApproximateReceiveCount", "SentTimestamp",
                                                                         "ApproximateFirstReceiveTimestamp"])
            messages = resp.get("Messages", [])
            if not messages:
                row["empty"] = True
                empty.add(name)
                break
            # A valid command already on its owner's queue is the owner's to consume:
            # leave it untouched. When a batch holds nothing else, this queue is done.
            owned = [m for m in messages if _owned_here(m["Body"], name)]
            for message in owned:
                if message["MessageId"] not in seen_owned:
                    row["left_for_owner"] += 1
                seen_owned.add(message["MessageId"])
            messages = [m for m in messages if not _owned_here(m["Body"], name)]
            if not messages:
                row["empty"] = True
                empty.add(name)
                break
            row["received"] += len(messages)
            to_delete = []
            with engine.begin() as conn:
                for message in messages:
                    body = message["Body"]
                    disposition, reason, owner = classify_message(body)
                    target = None
                    if disposition == "rerouted":
                        # A command goes home to its owner's queue (from another fleet's
                        # queue or the DLQ, where the owner could never consume it).
                        target = fleet_queue_by_name.get(owner)
                        if target is None:
                            disposition, reason = "quarantined", "unknown_kind"
                    if disposition == "deferred" and reason == "downstream_held" and not hold_downstream:
                        # Hold released: the message may travel to its consumers again.
                        target = fleet_queue_by_name.get(owner) if owner else None
                        disposition = "rerouted" if target else "deferred"
                    attributes = message.get("Attributes") or {}
                    metadata = {k: attributes[k] for k in ("ApproximateReceiveCount", "SentTimestamp", "ApproximateFirstReceiveTimestamp") if k in attributes}
                    metadata["md5_of_body"] = message.get("MD5OfBody")
                    metadata["recovered_from"] = name
                    entry = deferred.record(conn, channel="dlq" if name == "dlq" else "sqs", queue=url.rsplit("/", 1)[-1],
                                            message_id=message["MessageId"], fleet=fleet, body=body, reason=reason,
                                            detail=f"queue recovery: {disposition}", queue_metadata=metadata,
                                            status="deferred" if disposition == "rerouted" else disposition)
                    if entry["duplicate"]:
                        row["duplicates"] += 1
                    if disposition == "rerouted" and entry["duplicate"] and entry["status"] == "rerouted":
                        # Crash between the earlier ledger commit and the delete: already sent home.
                        pass
                    elif disposition == "rerouted":
                        sqs.send_message(QueueUrl=target, MessageBody=body)
                        deferred.resolve(conn, entry["id"], status="rerouted",
                                         resolution={"owner": owner, "queue": target.rsplit("/", 1)[-1], "at": _now()})
                        report["rerouted_to"][owner] = report["rerouted_to"].get(owner, 0) + 1
                    report["by_disposition"][disposition] = report["by_disposition"].get(disposition, 0) + 1
                    report["by_reason"][reason] = report["by_reason"].get(reason, 0) + 1
                    to_delete.append({"Id": message["MessageId"][:80], "ReceiptHandle": message["ReceiptHandle"]})
                    processed += 1
            # The ledger transaction is committed; only now may the queue forget the messages.
            for offset in range(0, len(to_delete), 10):
                result = sqs.delete_message_batch(QueueUrl=url, Entries=to_delete[offset:offset + 10])
                row["deleted"] += len(result.get("Successful", []))
                if result.get("Failed"):
                    row.setdefault("delete_failures", 0)
                    row["delete_failures"] += len(result["Failed"])
        if processed >= max_messages:
            report["bound_reached"] = "max_messages"
            break
        if clock() - started >= max_seconds:
            report["bound_reached"] = "max_seconds"
            break
    report["processed"] = processed
    report["exhausted"] = set(selected) <= empty and report["bound_reached"] is None
    report["finished_at"] = _now()
    report["duration_ms"] = int((clock() - started) * 1000)
    return report


def handle_queue_recovery(engine: Engine, gateway, envelope) -> dict:
    """L0 handler for ``QueueRecoveryRequested``. Payload: ``max_messages``,
    ``max_seconds``, optional ``queues`` (names: l0…l8, dlq)."""
    import boto3

    from app.clhear.settings import get_settings
    payload = envelope.payload or {}
    settings = get_settings()
    sqs = boto3.client("sqs", region_name=settings.aws_region)
    configured = json.loads(os.environ.get("CLHEAR_FLEET_QUEUE_URLS", "{}"))
    if not configured:
        raise RuntimeError("Queue recovery needs the L0 fleet queue map (CLHEAR_FLEET_QUEUE_URLS)")
    urls = queue_urls(sqs, configured)
    only = payload.get("queues") or None
    if only and not set(only) <= set(urls):
        raise ValueError(f"Unknown queues requested: {sorted(set(only) - set(urls))}")
    report = recover(engine, sqs=sqs, queues=urls, fleet=os.environ.get("CLHEAR_FLEET", "l0").lower(),
                     max_messages=payload.get("max_messages") or DEFAULT_MAX_MESSAGES,
                     max_seconds=payload.get("max_seconds") or DEFAULT_MAX_SECONDS,
                     hold_downstream=settings.clhear_l1_only, only=only)
    report["ledger"] = deferred.counts(engine)
    report["downstream"] = "held" if settings.clhear_l1_only else "released"
    return report
