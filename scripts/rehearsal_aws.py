"""P0 dummy-fleet rehearsal against REAL AWS (HLD §9 P0, post-`terraform apply`).

Mirrors tests/test_dummy_fleet_rehearsal.py steps 1–5, but the event travels
through the real `clhear-events` SQS queue and the exported snapshot is
uploaded to the real datalake bucket under public-ok/. DB is local SQLite
(Aurora DSN not wired yet — /clhear/DATABASE_URL is still a placeholder).

Usage (AWS credentials from the environment):
    CLHEAR_EVENTS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/<acct>/clhear-events \
        python scripts/rehearsal_aws.py

Exits 0 and prints a JSON evidence report iff every step passed.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="clhear-rehearsal-"))
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{workdir}/clhear-rehearsal.db")
    os.environ.setdefault("CLHEAR_ARTIFACTS_DIR", str(workdir / "artifacts"))

    import boto3
    import sqlalchemy as sa

    from app.clhear import db as clhear_db
    from app.clhear.db import make_engine, run_migrations
    from app.clhear.models import events, llm_calls, runs
    from app.clhear.platform.events import SqsTransport, relay_once
    from app.clhear.platform.evals import run_all
    from app.clhear.platform.exporter import export_release
    from app.clhear.platform.gateway import FakeProvider, Gateway
    from app.clhear.settings import get_settings
    from app.clhear.workers import handle_envelope, run_dummy_fleet

    settings = get_settings()
    queue_url = settings.clhear_events_queue_url
    if not queue_url:
        print("CLHEAR_EVENTS_QUEUE_URL is not set", file=sys.stderr)
        return 2
    region = settings.aws_region
    bucket = settings.clhear_datalake_bucket
    release = "clhear-v0.0.0-rehearsal-aws"

    engine = make_engine(settings.database_url)
    run_migrations(engine)
    clhear_db.set_engine(engine)

    sqs = boto3.client("sqs", region_name=region)
    s3 = boto3.client("s3", region_name=region)
    evidence: dict = {"queue_url": queue_url, "bucket": bucket, "release": release}

    # 1. Dummy fleet: data change + outbox event in one transaction.
    event_id = run_dummy_fleet(engine)
    with engine.connect() as conn:
        outbox_row = conn.execute(sa.select(events).where(events.c.event_id == event_id)).one()
    assert outbox_row.relayed_at is None and outbox_row.kind == "DummyChanged"
    evidence["event_id"] = event_id

    # 2. Relay ships it to the REAL queue and stamps relayed_at.
    transport = SqsTransport(queue_url, region)
    assert relay_once(engine, transport) == 1
    with engine.connect() as conn:
        relayed_at = conn.execute(
            sa.select(events.c.relayed_at).where(events.c.event_id == event_id)
        ).scalar_one()
    assert relayed_at is not None
    evidence["relayed_at"] = str(relayed_at)

    def receive_matching(expected_event_id: str, timeout_s: float = 60.0) -> tuple[str, str]:
        """Long-poll the real queue until the expected envelope arrives."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=10)
            for message in resp.get("Messages", []):
                if json.loads(message["Body"]).get("event_id") == expected_event_id:
                    return message["Body"], message["ReceiptHandle"]
                # Not ours (e.g. a stray schedule message): put it back immediately.
                sqs.change_message_visibility(
                    QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"], VisibilityTimeout=0
                )
        raise TimeoutError(f"event {expected_event_id} not received within {timeout_s}s")

    # 3. Worker consumes from the REAL queue; one gateway call logged with cost.
    body, receipt = receive_matching(event_id)
    gateway = Gateway(engine, FakeProvider())
    outputs = handle_envelope(engine, gateway, body)
    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
    assert outputs and "proposal_id" in outputs
    with engine.connect() as conn:
        call_row = conn.execute(sa.select(llm_calls)).one()
    assert float(call_row.cost_usd) > 0 and call_row.fleet == "dummy" and len(call_row.prompt_hash) == 64
    evidence["llm_call"] = {"cost_usd": float(call_row.cost_usd), "prompt_hash": call_row.prompt_hash}

    # Idempotency: redelivery of the same envelope is a no-op.
    transport.send(body)
    body2, receipt2 = receive_matching(event_id)
    assert handle_envelope(engine, gateway, body2) is None
    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt2)
    evidence["idempotent_redelivery"] = True

    # 4. Proposal approved via the API with a recorded maintainer identity.
    from fastapi.testclient import TestClient

    from app.main import create_app

    proposal_id = outputs["proposal_id"]
    with TestClient(create_app()) as client:
        resp = client.post(
            f"/api/clhear/proposals/{proposal_id}/approve",
            headers={"X-Reg42-User": "avner@reg42.ai"},
        )
    assert resp.status_code == 200, resp.text
    decided = resp.json()
    assert decided["status"] == "approved" and decided["approver"] == "avner@reg42.ai"
    evidence["proposal"] = decided

    # ...and the downstream ProposalApproved event goes through the REAL queue too.
    with engine.connect() as conn:
        downstream = conn.execute(sa.select(events).where(events.c.kind == "ProposalApproved")).one()
    assert relay_once(engine, transport) == 1
    ds_body, ds_receipt = receive_matching(str(downstream.event_id))
    handle_envelope(engine, gateway, ds_body)  # no P0 handler -> recorded as ignored
    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=ds_receipt)
    evidence["downstream_event_id"] = str(downstream.event_id)

    # 5. Green evals + export; snapshot uploaded to the REAL datalake (public-ok/).
    records = run_all(engine, release=release)
    assert records and all(r["passed"] for r in records)
    out_dir = workdir / "public-repo"
    export_release(engine, release, repo_dir=out_dir)
    snapshot_key = f"public-ok/rehearsals/{release}/snapshot.json"
    s3.upload_file(str(out_dir / "snapshots" / release / "snapshot.json"), bucket, snapshot_key)
    fetched = json.loads(s3.get_object(Bucket=bucket, Key=snapshot_key)["Body"].read())
    assert fetched["release"] == release and fetched["all_evals_passed"] is True
    head = s3.head_object(Bucket=bucket, Key=snapshot_key)
    evidence["snapshot_s3"] = {
        "key": snapshot_key,
        "version_id": head.get("VersionId"),
        "object_lock_mode": head.get("ObjectLockMode"),
        "object_lock_retain_until": str(head.get("ObjectLockRetainUntilDate")),
    }

    # Restricted-zone discipline: this principal (not the worker task role) must
    # be DENIED reads under restricted/ by the bucket policy.
    restricted_key = f"restricted/rehearsals/{release}/marker.json"
    s3.put_object(Bucket=bucket, Key=restricted_key, Body=b'{"rehearsal": true}')
    try:
        s3.get_object(Bucket=bucket, Key=restricted_key)
        evidence["restricted_read_denied"] = False
    except s3.exceptions.ClientError as exc:
        evidence["restricted_read_denied"] = exc.response["Error"]["Code"] == "AccessDenied"
    assert evidence["restricted_read_denied"] is True

    # Everything was recorded in the run ledger (replayability).
    with engine.connect() as conn:
        fleets = {row.fleet for row in conn.execute(sa.select(runs.c.fleet))}
    assert {"dummy", "worker"} <= fleets
    evidence["run_ledger_fleets"] = sorted(fleets)

    print(json.dumps(evidence, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(main())
