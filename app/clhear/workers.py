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
from app.clhear.platform.gateway import Gateway, Provider, parse_json_object
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
    """Rehearsal consumer: one routed call (triage-shaped) -> one proposal."""
    from app.clhear.platform.router import complete

    result = complete(
        gateway,
        "dummy.triage",
        prompt=f"Classify this candidate for {envelope.subject_ref}: {json.dumps(envelope.payload)}",
        system="Respond with JSON: {\"classification\": ..., \"confidence\": ...}",
        required_keys=["classification", "confidence"],
    )
    triage = parse_json_object(result.text)
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


def run_adapter_fleet(
    engine: Engine,
    adapter_key: str,
    gateway: Gateway | None = None,
    *,
    force_nightly: bool = False,
    nightly_only: bool = False,
) -> dict:
    """One scheduled fleet run: every source owned by `adapter_key` goes through
    the full ingest pipeline (fetch -> gate/repair -> persist -> annotate ->
    index -> diff). Unchanged sources record an 'unchanged' run; failures are
    isolated per source (IngestFidelityFailed + rectification proposal)."""
    from datetime import datetime, timezone

    from app.clhear.l1 import families, pipeline, registry_etoro
    from app.clhear.l1.adapters import CITATOR_KEYS

    settings = get_settings()
    if os.environ.get("CLHEAR_ARTIFACT_STORE") == "s3" or settings.clhear_snapshot_s3_uri:
        store: pipeline.ArtifactStore = pipeline.S3Store(settings.clhear_datalake_bucket, settings.aws_region)
    else:
        store = pipeline.LocalStore(settings.clhear_artifacts_dir)

    registry_etoro.seed(engine)
    job_id = f"job-sched-{adapter_key}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    from app.clhear.l1.fleet import fleet_plan

    plan = [] if nightly_only else fleet_plan(adapter_key)
    statuses: dict[str, int] = {}
    failures: list[str] = []
    for entry, adapter in plan:
        source_key = adapter.meta().source_key
        try:
            summary = pipeline.ingest(engine, adapter, store, trigger="schedule", gateway=gateway, job_id=job_id)
            status = summary.get("status", "?")
        except Exception as exc:
            log.exception("scheduled ingest crashed for %s", source_key)
            status = "failed"
            try:
                pipeline.RunRecorder(engine, f"l1.{adapter.key}", "schedule", {"source": source_key, "job_id": job_id}).finish(
                    "failed", {"source": source_key, "error": str(exc)[:500]}
                )
            except Exception:
                log.exception("could not record failed run for %s", source_key)
        statuses[status] = statuses.get(status, 0) + 1
        if status in ("not-fully-successful", "crashed", "failed"):
            failures.append(source_key)
        if entry is None and adapter.key in CITATOR_KEYS:
            try:
                families.sync_citator(engine, adapter, trigger="schedule", job_id=job_id)
            except Exception:
                log.exception("citator sync failed for %s", source_key)
    try:
        from app.clhear.platform import evals as l1_evals

        l1_evals.run_suite(engine, "l1_completeness", release=job_id)
        schedule_kept = l1_evals.run_suite(engine, "l1_schedule_kept", release=job_id)
        _put_schedule_metric(schedule_kept["scores"].get("missed_count", 0))
        for _entry, adapter in plan:
            key = adapter.meta().source_key
            try:
                l1_evals.run_source_evals(engine, key, release=job_id)
            except Exception:
                log.exception("source evals failed for %s", key)
    except Exception:
        log.exception("fleet evals failed for %s", adapter_key)

    # Stack refresh: L1 changes flow into the derived layers + AI fleets.
    # GPU + fleets run at most once per UTC day (idempotent; later adapters skip).
    stack_recorder = pipeline.RunRecorder(
        engine, "l2.extract", "schedule", {"source": f"stack-refresh-{adapter_key}", "job_id": job_id}
    )
    try:
        from app.clhear.fleets import run_nightly_if_due
        from app.clhear.platform.router import Router, is_router

        llm = gateway
        if not is_router(gateway):
            from app.clhear.platform.router import build_providers

            llm = Router(engine, providers={"ollama": gateway._provider, "anthropic": gateway._provider,
                                            "fake": gateway._provider})
        nightly = run_nightly_if_due(engine, llm, force=force_nightly)
        if nightly is None:
            from app.clhear import curated
            from app.clhear.l2.extract import run_extraction

            seeded = curated.seed(engine)
            extraction = run_extraction(engine)
            stack_recorder.finish("succeeded", {"extraction": extraction, "curated": seeded, "fleets": "already-ran"})
        else:
            stack_recorder.finish("succeeded", nightly)
    except Exception as exc:
        log.exception("stack refresh failed for %s", adapter_key)
        stack_recorder.finish("failed", {"error": str(exc)[:400]})

    return {"adapter": adapter_key, "job_id": job_id, "ran": len(plan), "statuses": statuses, "failures": failures}


def _put_schedule_metric(missed_count: int) -> None:
    """CLHEAR/ScheduleMissedSources: alarmed in CloudWatch when > 0."""
    try:
        import boto3

        boto3.client("cloudwatch", region_name=get_settings().aws_region).put_metric_data(
            Namespace="CLHEAR",
            MetricData=[{"MetricName": "ScheduleMissedSources", "Value": float(missed_count), "Unit": "Count"}],
        )
    except Exception:
        log.exception("could not publish ScheduleMissedSources metric")


def handle_adapter_run(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    payload = envelope.payload or {}
    force = bool(payload.get("force_nightly") or payload.get("force"))
    nightly_only = bool(payload.get("nightly_only"))
    return run_adapter_fleet(
        engine,
        payload.get("adapter", envelope.subject_ref),
        gateway,
        force_nightly=force,
        nightly_only=nightly_only,
    )


SNAPSHOT_LOCAL = "/tmp/clhear.db"


def handle_publish_release(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """Named immutable L1 release. Unknown future layer kinds stay ignored."""
    from app.clhear.releases import publish_release
    from app.clhear.settings import get_settings

    settings = get_settings()
    snapshot_path = SNAPSHOT_LOCAL if os.path.exists(SNAPSHOT_LOCAL) else None
    return publish_release(
        engine,
        snapshot_path=snapshot_path,
        snapshot_uri=settings.clhear_snapshot_s3_uri or None,
        release_id=envelope.payload.get("release_id"),
    )


def handle_community_write(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """Apply a community op from the read-only web app (single-writer rule)."""
    from app.clhear import community_writes

    return community_writes.apply_op(engine, envelope.payload)


HANDLERS = {
    "DummyChanged": handle_dummy_changed,
    "AdapterRunRequested": handle_adapter_run,
    "PublishReleaseRequested": handle_publish_release,
    "CommunityWrite": handle_community_write,
    # Later layers: add kinds here. handle_envelope already ignores unknown kinds.
}

# Scheduled kinds re-fire with the same envelope id by design (EventBridge
# static input); their work is naturally idempotent (unchanged -> no-op run).
_ALWAYS_RUN = {"AdapterRunRequested", "PublishReleaseRequested"}


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
    from app.clhear.platform.router import Router, build_providers, record_missing_providers

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

    if settings.ollama_base_url:
        from app.clhear.platform.ollama_sidecar import wait_http_tags

        if wait_http_tags(settings.ollama_base_url, timeout_s=1800):
            log.info("local Ollama ready at %s", settings.ollama_base_url)
        else:
            log.error("OLLAMA_BASE_URL %s never became ready", settings.ollama_base_url)

    providers = build_providers(settings)
    if not providers:
        record_missing_providers(engine)
    gateway = Router(engine, providers)
    transport = SqsTransport(settings.clhear_events_queue_url, settings.aws_region)
    sqs = boto3.client("sqs", region_name=settings.aws_region)

    log.info("clhear worker started (providers=%s, snapshot=%s)", ",".join(providers) or "none", snapshot_uri or "off")
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
                try:
                    from app.clhear.releases import publish_release

                    publish_release(
                        engine,
                        snapshot_path=SNAPSHOT_LOCAL,
                        snapshot_uri=snapshot_uri,
                    )
                except Exception:
                    log.exception("named release publish after snapshot failed")
        except Exception:
            log.exception("worker iteration failed; backing off")
            time.sleep(10)


if __name__ == "__main__":
    main()
