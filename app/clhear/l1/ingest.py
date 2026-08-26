"""L1 ingestion CLI: python -m app.clhear.l1.ingest [adapter_key ...]

Runs each adapter through the pipeline, then the citator sync for adapters
with an official effects feed, then relays the outbox (to SQS when configured,
HLD: nothing publishes to SQS directly — this IS the relay).

Environment:
    CLHEAR_HTTP_MODE=live|record|replay   (default replay — offline)
    CLHEAR_ARTIFACT_STORE=s3|local        (default local)
    CLHEAR_EVENTS_QUEUE_URL=…             (optional; enables SQS relay)
"""
import json
import logging
import os
import sys

from app.clhear.db import get_engine, run_migrations
from app.clhear.l1 import families, pipeline
from app.clhear.l1.adapters import ADAPTER_KEYS, CITATOR_KEYS, get_adapter
from app.clhear.platform.events import InMemoryTransport, SqsTransport, relay_once
from app.clhear.platform.gateway import AnthropicProvider, Gateway
from app.clhear.settings import get_settings


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    keys = sys.argv[1:] or list(ADAPTER_KEYS)

    engine = get_engine()
    run_migrations(engine)

    if os.environ.get("CLHEAR_ARTIFACT_STORE") == "s3":
        store: pipeline.ArtifactStore = pipeline.S3Store(settings.clhear_datalake_bucket, settings.aws_region)
    else:
        store = pipeline.LocalStore(settings.clhear_artifacts_dir)

    gateway = Gateway(engine, AnthropicProvider()) if settings.anthropic_api_key else None

    from datetime import datetime, timezone

    job_id = f"job-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    summaries = []
    for key in keys:
        adapter = get_adapter(key)
        summaries.append(pipeline.ingest(engine, adapter, store, trigger="cli", gateway=gateway, job_id=job_id))
        if key in CITATOR_KEYS:
            summaries.append(families.sync_citator(engine, adapter, trigger="cli", job_id=job_id))

    relay_recorder = pipeline.RunRecorder(engine, "l0.relay", "cli", {"source": "events", "job_id": job_id})
    if settings.clhear_events_queue_url:
        transport = SqsTransport(settings.clhear_events_queue_url, settings.aws_region)
    else:
        transport = InMemoryTransport()
    relayed = relay_once(engine, transport, batch_size=1000)
    relay_recorder.stage("relay", events=relayed)
    relay_recorder.finish("succeeded", {"relayed": relayed})
    failed = [s.get("source") for s in summaries if s.get("status") == "not-fully-successful"]
    print(json.dumps({"summaries": summaries, "relayed_events": relayed, "failed": failed}, indent=2, default=str))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
