"""Outbox writer + SQS relay + envelope schema (HLD §7.1).

Writers insert into l0_platform.events in the SAME transaction as their data
change; the relay ships unrelayed rows to SQS and stamps relayed_at. Nothing
publishes to SQS directly. Consumers must be idempotent on event_id.
"""
import json
import logging
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Protocol

import sqlalchemy as sa
from pydantic import BaseModel, Field
from sqlalchemy.engine import Connection, Engine

from app.clhear.models import events

log = logging.getLogger("clhear.events")

ENVELOPE_SCHEMA_VERSION = 1


class Envelope(BaseModel):
    """Frozen event envelope — the most expensive thing to change later (HLD §6.1)."""

    model_config = {"frozen": True}

    event_id: str
    layer: str
    kind: str
    subject_ref: str
    payload: dict = Field(default_factory=dict)
    schema_version: int = ENVELOPE_SCHEMA_VERSION
    producer: str
    ts: str


def emit(
    conn: Connection,
    *,
    layer: str,
    kind: str,
    subject_ref: str,
    payload: dict | None = None,
    producer: str,
    schema_version: int = ENVELOPE_SCHEMA_VERSION,
) -> str:
    """Write an outbox row inside the caller's transaction. Returns event_id."""
    event_id = str(uuid.uuid4())
    conn.execute(
        events.insert().values(
            event_id=event_id,
            layer=layer,
            kind=kind,
            subject_ref=subject_ref,
            payload=payload or {},
            schema_version=schema_version,
            producer=producer,
        )
    )
    return event_id


class Transport(Protocol):
    def send(self, body: str) -> None: ...


class SqsTransport:
    def __init__(self, queue_url: str, region: str):
        import boto3

        self._client = boto3.client("sqs", region_name=region)
        self._queue_url = queue_url

    def send(self, body: str) -> None:
        self._client.send_message(QueueUrl=self._queue_url, MessageBody=body)


class InMemoryTransport:
    """Offline stand-in for SQS in tests and local dev."""

    def __init__(self):
        self.queue: deque[str] = deque()

    def send(self, body: str) -> None:
        self.queue.append(body)

    def receive(self) -> str | None:
        return self.queue.popleft() if self.queue else None


def _row_to_envelope(row) -> Envelope:
    payload = row.payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    created = row.created_at or datetime.now(timezone.utc)
    if isinstance(created, str):
        ts = created
    else:
        ts = created.isoformat()
    return Envelope(
        event_id=str(row.event_id),
        layer=row.layer,
        kind=row.kind,
        subject_ref=row.subject_ref,
        payload=payload,
        schema_version=row.schema_version,
        producer=row.producer,
        ts=ts,
    )


def relay_once(engine: Engine, transport: Transport, batch_size: int = 100) -> int:
    """Ship unrelayed outbox rows to the transport; stamp relayed_at. Returns count."""
    shipped = 0
    with engine.begin() as conn:
        rows = conn.execute(
            sa.select(events).where(events.c.relayed_at.is_(None)).order_by(events.c.id).limit(batch_size)
        ).all()
        for row in rows:
            envelope = _row_to_envelope(row)
            transport.send(envelope.model_dump_json())
            conn.execute(
                events.update()
                .where(events.c.id == row.id)
                .values(relayed_at=datetime.now(timezone.utc))
            )
            shipped += 1
    if shipped:
        log.info("relayed %d event(s)", shipped)
    return shipped


def relay_forever(engine: Engine, transport: Transport, interval_s: float = 2.0) -> None:
    while True:
        try:
            if relay_once(engine, transport) == 0:
                time.sleep(interval_s)
        except Exception:
            log.exception("relay iteration failed; backing off")
            time.sleep(interval_s * 5)
