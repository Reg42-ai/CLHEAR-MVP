"""Build the deployable L1 corpus DB (P1).

Ingests the historical MLR snapshot first, then every registered adapter's
current text (real replay: the MLR amendment history lands as a change_event),
runs the citator syncs, and — when configured — stores originals in the real
S3 datalake and relays SourceChanged through the real SQS queue, draining it
worker-style afterwards so the queue is left clean.

Usage:
    DATABASE_URL=sqlite:///deploy/clhear.db \
    CLHEAR_HTTP_MODE=replay CLHEAR_HTTP_FIXTURES=tests/fixtures/http \
    CLHEAR_ARTIFACT_STORE=s3 CLHEAR_EVENTS_QUEUE_URL=... \
        python scripts/build_corpus.py
"""
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clhear.db import get_engine, run_migrations  # noqa: E402
from app.clhear.l1 import families, pipeline  # noqa: E402
from app.clhear.l1.adapters import ADAPTER_KEYS, CITATOR_KEYS, get_adapter  # noqa: E402
from app.clhear.l1.adapters.eur_lex import EurLexAdapter  # noqa: E402
from app.clhear.l1.adapters.uk_legislation import UkLegislationAdapter  # noqa: E402
from app.clhear.platform.events import SqsTransport, relay_once  # noqa: E402
from app.clhear.platform.gateway import AnthropicProvider, FakeProvider, Gateway  # noqa: E402
from app.clhear.settings import get_settings  # noqa: E402
from app.clhear.workers import handle_envelope  # noqa: E402

MLR_HISTORICAL_SNAPSHOT = "2020-01-09"  # day before SI 2019/1511 came into force
GDPR_OJ_ORIGINAL = "32016R0679"  # OJ act as published (preamble + recitals)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()

    engine = get_engine()
    run_migrations(engine)

    if os.environ.get("CLHEAR_ARTIFACT_STORE") == "s3":
        store: pipeline.ArtifactStore = pipeline.S3Store(settings.clhear_datalake_bucket, settings.aws_region)
    else:
        store = pipeline.LocalStore(settings.clhear_artifacts_dir)

    # LLM repair tier is available only when a key is configured; daily runs
    # stay LLM-free whenever the deterministic tiers pass.
    gateway = Gateway(engine, AnthropicProvider()) if settings.anthropic_api_key else None

    summaries = []
    # Historical versions first so current texts replay as amendments:
    # MLR point-in-time snapshot, then the GDPR OJ original act.
    summaries.append(
        pipeline.ingest(engine, UkLegislationAdapter(snapshot=MLR_HISTORICAL_SNAPSHOT), store, gateway=gateway)
    )
    summaries.append(
        pipeline.ingest(engine, EurLexAdapter(celex_version=GDPR_OJ_ORIGINAL), store, gateway=gateway)
    )
    for key in ADAPTER_KEYS:
        adapter = get_adapter(key)
        summaries.append(pipeline.ingest(engine, adapter, store, trigger="build_corpus", gateway=gateway))
        if key in CITATOR_KEYS:
            summaries.append(families.sync_citator(engine, adapter, trigger="build_corpus"))

    relayed = drained = 0
    if settings.clhear_events_queue_url:
        import boto3

        transport = SqsTransport(settings.clhear_events_queue_url, settings.aws_region)
        relayed = relay_once(engine, transport, batch_size=1000)
        # Drain worker-style (no P1 consumer reacts to SourceChanged yet).
        sqs = boto3.client("sqs", region_name=settings.aws_region)
        gateway = Gateway(engine, FakeProvider())
        while True:
            resp = sqs.receive_message(
                QueueUrl=settings.clhear_events_queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=2
            )
            messages = resp.get("Messages", [])
            if not messages:
                break
            for message in messages:
                handle_envelope(engine, gateway, message["Body"])
                sqs.delete_message(
                    QueueUrl=settings.clhear_events_queue_url, ReceiptHandle=message["ReceiptHandle"]
                )
                drained += 1

    failed = [s.get("source") for s in summaries if s.get("status") == "not-fully-successful"]
    print(json.dumps({"summaries": summaries, "relayed": relayed, "drained": drained, "failed": failed}, indent=2, default=str))
    if failed:
        logging.error("corpus build NOT fully successful for: %s", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
