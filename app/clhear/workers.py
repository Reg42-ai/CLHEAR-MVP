"""SQS consumer entrypoint (ECS clhear-workers): python -m app.clhear.workers

The worker loop does two jobs:
  1. relay the l0 outbox to SQS (events.relay_forever semantics, interleaved)
  2. consume envelopes from SQS and dispatch to per-kind handlers

Consumers are idempotent on event_id: each handled envelope is recorded in the
l0_platform.runs ledger and skipped if already present (HLD §6.1, §7.1).
"""
import json
import logging
import os
import time

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import runs
from app.clhear.platform import events as l0_events
from app.clhear.platform import proposals as l0_proposals
from app.clhear.platform.events import Envelope
from app.clhear.platform.gateway import Gateway, Provider
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.workers")

DUMMY_FLEET = "dummy"


def run_dummy_fleet(engine: Engine, subject_ref: str = "dummy/rehearsal-1") -> str:
    """P0 rehearsal fleet: one data change + outbox event in the same transaction."""
    started = time.monotonic()
    with engine.begin() as conn:
        event_id = l0_events.emit(
            conn,
            layer="l0",
            kind="DummyChanged",
            subject_ref=subject_ref,
            payload={"note": "dummy-fleet rehearsal"},
            producer="fleet.dummy",
        )
        conn.execute(
            runs.insert().values(
                fleet=DUMMY_FLEET,
                trigger="manual",
                inputs={"subject_ref": subject_ref},
                outputs={"event_id": event_id},
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )
    return event_id


def handle_dummy_changed(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """Rehearsal consumer: one gateway call (triage-shaped) -> one proposal."""
    result = gateway.call(
        fleet=DUMMY_FLEET,
        model="claude-3-5-haiku-latest",
        prompt=f"Classify this candidate for {envelope.subject_ref}: {json.dumps(envelope.payload)}",
        system="Respond with JSON: {\"classification\": ..., \"confidence\": ...}",
        required_keys=["classification", "confidence"],
    )
    triage = json.loads(result.text)
    with engine.begin() as conn:
        proposal_id = l0_proposals.create_proposal(
            conn,
            layer="l0",
            kind="dummy_candidate",
            subject_ref=envelope.subject_ref,
            draft={"triage": triage, "event_id": envelope.event_id},
            rationale="dummy-fleet rehearsal proposal",
            confidence=float(triage.get("confidence", 0)),
        )
    return {"proposal_id": proposal_id, "cost_usd": result.cost_usd}


def run_adapter_fleet(engine: Engine, adapter_key: str, gateway: Gateway | None = None) -> dict:
    """One scheduled fleet run: every source owned by `adapter_key` goes through
    the full ingest pipeline (fetch -> gate/repair -> persist -> annotate ->
    index -> diff). Unchanged sources record an 'unchanged' run; failures are
    isolated per source (IngestFidelityFailed + rectification proposal)."""
    from datetime import datetime, timezone

    from app.clhear.l1 import families, pipeline, registry_etoro
    from app.clhear.l1.adapters import ADAPTER_KEYS, CITATOR_KEYS, get_adapter

    settings = get_settings()
    if os.environ.get("CLHEAR_ARTIFACT_STORE") == "s3" or settings.clhear_snapshot_s3_uri:
        store: pipeline.ArtifactStore = pipeline.S3Store(settings.clhear_datalake_bucket, settings.aws_region)
    else:
        store = pipeline.LocalStore(settings.clhear_artifacts_dir)

    registry_etoro.seed(engine)
    job_id = f"job-sched-{adapter_key}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    # Rule key -> adapter keys (the govinfo weekly rule also covers NIST).
    rule_map = {
        "uk_legislation": ["uk_legislation"],
        "eur_lex": ["eur_lex"],
        "govinfo_us": ["govinfo_us_usc", "govinfo_us_ecfr", "nist_sp800_53", "nist_csf"],
    }
    adapter_keys = rule_map.get(adapter_key, [])
    plan: list = []
    for key in adapter_keys:
        if key in ADAPTER_KEYS:
            plan.append((None, get_adapter(key)))
    plan.extend(registry_etoro.wave1_adapters(adapter_key))
    if not plan:
        return {"adapter": adapter_key, "note": "no adapter registered yet (P3)", "ran": 0}

    statuses: dict[str, int] = {}
    failures: list[str] = []
    for entry, adapter in plan:
        source_key = adapter.meta().source_key
        try:
            summary = pipeline.ingest(engine, adapter, store, trigger="schedule", gateway=gateway, job_id=job_id)
            status = summary.get("status", "?")
        except Exception:
            log.exception("scheduled ingest crashed for %s", source_key)
            status = "crashed"
        statuses[status] = statuses.get(status, 0) + 1
        if status in ("not-fully-successful", "crashed"):
            failures.append(source_key)
        if entry is None and adapter.key in CITATOR_KEYS:
            try:
                families.sync_citator(engine, adapter, trigger="schedule", job_id=job_id)
            except Exception:
                log.exception("citator sync failed for %s", source_key)
    return {"adapter": adapter_key, "job_id": job_id, "ran": len(plan), "statuses": statuses, "failures": failures}


def handle_adapter_run(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    return run_adapter_fleet(engine, envelope.payload.get("adapter", envelope.subject_ref), gateway)


HANDLERS = {
    "DummyChanged": handle_dummy_changed,
    "AdapterRunRequested": handle_adapter_run,
    # P1+: SourceChanged -> embeddings + citation-mining jobs, etc.
}

# Scheduled kinds re-fire with the same envelope id by design (EventBridge
# static input); their work is naturally idempotent (unchanged -> no-op run).
_ALWAYS_RUN = {"AdapterRunRequested"}


def _already_handled(engine: Engine, event_id: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(runs.c.id)
            .where(runs.c.fleet == "worker")
            .where(runs.c.inputs.cast(sa.Text).like(f'%{event_id}%'))
            .limit(1)
        ).first()
    return row is not None


def handle_envelope(engine: Engine, gateway: Gateway, body: str) -> dict | None:
    envelope = Envelope.model_validate_json(body)
    if envelope.kind not in _ALWAYS_RUN and _already_handled(engine, envelope.event_id):
        log.info("event %s already handled; skipping (idempotent)", envelope.event_id)
        return None
    handler = HANDLERS.get(envelope.kind)
    if handler is None:
        log.info("no handler for kind %s; ignoring", envelope.kind)
        outputs: dict = {"ignored": True}
    else:
        started = time.monotonic()
        outputs = handler(engine, gateway, envelope)
        outputs["duration_ms"] = int((time.monotonic() - started) * 1000)
    with engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                fleet="worker",
                trigger=envelope.kind,
                inputs={"event_id": envelope.event_id, "subject_ref": envelope.subject_ref},
                outputs=outputs,
                duration_ms=outputs.get("duration_ms"),
            )
        )
    return outputs


SNAPSHOT_LOCAL = "/tmp/clhear.db"


def _snapshot_pull(uri: str, region: str) -> None:
    import boto3

    bucket, key = uri[len("s3://") :].split("/", 1)
    boto3.client("s3", region_name=region).download_file(bucket, key, SNAPSHOT_LOCAL)
    log.info("snapshot pulled from %s", uri)


def _snapshot_push(uri: str, region: str) -> None:
    import boto3

    bucket, key = uri[len("s3://") :].split("/", 1)
    boto3.client("s3", region_name=region).upload_file(SNAPSHOT_LOCAL, bucket, key)
    log.info("snapshot published to %s", uri)


def main() -> None:
    """Long-running ECS worker: relay outbox + consume SQS.

    Snapshot mode (CLHEAR_SNAPSHOT_S3_URI set): the corpus SQLite is pulled
    from S3 at startup, every scheduled fleet run mutates it, and it is
    published back after each handled batch — the public explorer picks the
    new snapshot up on its next TTL check.
    """
    import boto3

    from app.clhear import db
    from app.clhear.db import get_engine, run_migrations
    from app.clhear.platform.events import SqsTransport, relay_once
    from app.clhear.platform.gateway import AnthropicProvider, FakeProvider

    logging.basicConfig(level=logging.INFO)
    settings = get_settings()

    snapshot_uri = settings.clhear_snapshot_s3_uri
    if snapshot_uri:
        try:
            _snapshot_pull(snapshot_uri, settings.aws_region)
        except Exception:
            log.exception("no existing snapshot at %s; starting fresh", snapshot_uri)
        os.environ["DATABASE_URL"] = f"sqlite:///{SNAPSHOT_LOCAL}"
        get_settings.cache_clear()
        settings = get_settings()
        db.dispose_engine()

    engine = get_engine()
    run_migrations(engine)

    provider: Provider
    provider = AnthropicProvider() if settings.anthropic_api_key else FakeProvider()
    gateway = Gateway(engine, provider)
    transport = SqsTransport(settings.clhear_events_queue_url, settings.aws_region)
    sqs = boto3.client("sqs", region_name=settings.aws_region)

    log.info("clhear worker started (provider=%s, snapshot=%s)", provider.name, snapshot_uri or "off")
    while True:
        try:
            relay_once(engine, transport)
            resp = sqs.receive_message(
                QueueUrl=settings.clhear_events_queue_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=10,
            )
            messages = resp.get("Messages", [])
            handled_work = False
            for message in messages:
                outputs = handle_envelope(engine, gateway, message["Body"])
                if outputs and not outputs.get("ignored"):
                    handled_work = True
                sqs.delete_message(
                    QueueUrl=settings.clhear_events_queue_url,
                    ReceiptHandle=message["ReceiptHandle"],
                )
            if handled_work and snapshot_uri:
                relay_once(engine, transport)  # ship this batch's change events too
                _snapshot_push(snapshot_uri, settings.aws_region)
        except Exception:
            log.exception("worker iteration failed; backing off")
            time.sleep(10)


if __name__ == "__main__":
    main()
