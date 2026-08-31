#!/usr/bin/env python3
"""Enqueue AdapterRunRequested with force_nightly=true (one live GPU night)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument(
        "--queue-url",
        default=os.environ.get(
            "CLHEAR_EVENTS_QUEUE_URL",
            "https://sqs.us-east-1.amazonaws.com/730649732189/clhear-events",
        ),
    )
    parser.add_argument("--adapter", default="uk_legislation")
    args = parser.parse_args()
    import boto3

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    envelope = {
        "event_id": str(uuid4()),
        "layer": "l1",
        "kind": "AdapterRunRequested",
        "subject_ref": args.adapter,
        "payload": {"adapter": args.adapter, "force_nightly": True, "force": True},
        "schema_version": 1,
        "producer": "force_live_nightly",
        "ts": now,
    }
    boto3.client("sqs", region_name=args.region).send_message(
        QueueUrl=args.queue_url, MessageBody=json.dumps(envelope)
    )
    print(json.dumps({"queued": True, "event_id": envelope["event_id"], "adapter": args.adapter}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
