"""Record fleet-queue backlog evidence, then optionally purge the nine live queues.

The dead-letter queue is never purged. Sampling uses ReceiveMessage without
DeleteMessage. Write the JSON report locally and to
s3://clhear-deploy-730649732189/deployments/l1/queue-evidence/.

Usage:
    python scripts/queue_backlog_evidence.py --output /tmp/queue-backlog.json
    python scripts/queue_backlog_evidence.py --output /tmp/queue-backlog.json --purge
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clhear.platform import routing  # noqa: E402

ACCOUNT = "730649732189"
REGION = "us-east-1"
BUCKET = f"clhear-deploy-{ACCOUNT}"
PREFIX = "deployments/l1/queue-evidence"
FLEETS = tuple(f"l{i}" for i in range(9))
QUEUES = {
    fleet: f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/" + (
        "clhear-events" if fleet == "l1" else f"clhear-fleet-{fleet}"
    )
    for fleet in FLEETS
}
DLQ_NAME = "clhear-events-dlq"
SAMPLE_SIZE = 200


def _name(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _classify_body(body: str) -> dict:
    try:
        payload = json.loads(body)
        kind = payload.get("kind") or (payload.get("detail-type") if isinstance(payload, dict) else None)
        if not kind and isinstance(payload.get("detail"), dict):
            kind = payload["detail"].get("kind")
        category, owner = routing.classify(str(kind or ""))
        return {"kind": kind, "category": category, "owner": owner}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"kind": None, "category": "malformed", "owner": None}


def _attributes(sqs, url: str) -> dict:
    names = ["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
             "ApproximateNumberOfMessagesDelayed"]
    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=names)["Attributes"]
    return {name: int(attrs.get(name, 0)) for name in names}


def _sample(sqs, url: str, limit: int) -> list[dict]:
    seen, rows = set(), []
    while len(rows) < limit:
        batch = sqs.receive_message(
            QueueUrl=url, MaxNumberOfMessages=min(10, limit - len(rows)),
            VisibilityTimeout=30, WaitTimeSeconds=0,
            AttributeNames=["SentTimestamp", "ApproximateReceiveCount"],
        ).get("Messages") or []
        if not batch:
            break
        fresh = False
        for message in batch:
            mid = message["MessageId"]
            if mid in seen:
                continue
            seen.add(mid)
            fresh = True
            classified = _classify_body(message.get("Body", ""))
            rows.append({
                "message_id": mid,
                "receive_count": message.get("Attributes", {}).get("ApproximateReceiveCount"),
                **classified,
            })
            if len(rows) >= limit:
                break
        if not fresh:
            break
    return rows


def collect(sqs, *, sample_size: int = SAMPLE_SIZE) -> dict:
    dlq_url = sqs.get_queue_url(QueueName=DLQ_NAME)["QueueUrl"]
    queues = {**QUEUES, "dlq": dlq_url}
    report = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "account": ACCOUNT, "region": REGION,
        "queues": {}, "purged": [], "dlq_purged": False,
    }
    for name, url in queues.items():
        counts = _attributes(sqs, url)
        sample = _sample(sqs, url, sample_size)
        kinds = Counter((row["kind"], row["category"]) for row in sample)
        report["queues"][name] = {
            "url": url, "name": _name(url), **counts,
            "sample_size": len(sample),
            "sample_kinds": {f"{kind or 'none'}:{category}": n for (kind, category), n in kinds.items()},
            "sample": sample,
        }
    return report


def purge_fleet_queues(sqs, report: dict) -> None:
    for fleet, url in QUEUES.items():
        sqs.purge_queue(QueueUrl=url)
        report["purged"].append(_name(url))
    report["dlq_purged"] = False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sample-size", type=int, default=SAMPLE_SIZE)
    parser.add_argument("--purge", action="store_true",
                        help="Purge the nine fleet queues after writing evidence. Never purges the DLQ.")
    parser.add_argument("--no-s3", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.sample_size <= 1000:
        raise SystemExit("sample-size must be within 1..1000")
    sqs = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", REGION))
    report = collect(sqs, sample_size=args.sample_size)
    if args.purge:
        purge_fleet_queues(sqs, report)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(report, indent=2, sort_keys=True).encode()
    path.write_bytes(body)
    os.chmod(path, 0o600)
    if not args.no_s3:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        key = f"{PREFIX}/queue-backlog-{stamp}.json"
        boto3.client("s3", region_name=REGION).put_object(
            Bucket=BUCKET, Key=key, Body=body, ServerSideEncryption="AES256",
            ContentType="application/json",
        )
        report["s3"] = f"s3://{BUCKET}/{key}"
        path.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps({
        "output": str(path), "s3": report.get("s3"),
        "purged": report["purged"], "dlq_purged": False,
        "visible": {name: q["ApproximateNumberOfMessages"] for name, q in report["queues"].items()},
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
