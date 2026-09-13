"""One-off: load the v1 SQLite snapshot into the (migrated) Aurora record.

Runs where the database is reachable, i.e. as an ECS one-off in the fleet image:

    python -m app.clhear.tools.load_record --source s3://bucket/webui/clhear-latest.db \
        --target-ssm /clhear/AURORA_DSN [--verify]

or locally with a file path and --target <dsn>. The target is migrated first; rows
are upserted, nothing is deleted (I2). With --verify the DR record check
(`platform.dr.verify_record`) runs afterwards and its report is printed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger("clhear.load_record")


def _fetch_source(source: str, region: str) -> Path:
    if not source.startswith("s3://"):
        return Path(source)
    import boto3

    bucket, key = source[len("s3://"):].split("/", 1)
    dest = Path(tempfile.mkdtemp(prefix="clhear-load-")) / "source.db"
    boto3.client("s3", region_name=region).download_file(bucket, key, str(dest))
    log.info("pulled %s (%d MB)", source, dest.stat().st_size // (1024 * 1024))
    return dest


def _target_dsn(args) -> str:
    if args.target:
        return args.target
    import boto3

    resp = boto3.client("ssm", region_name=args.region).get_parameter(Name=args.target_ssm, WithDecryption=True)
    return str(resp["Parameter"]["Value"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="SQLite path or s3:// URI of the snapshot")
    ap.add_argument("--target", help="target DSN (postgresql+psycopg://...)")
    ap.add_argument("--target-ssm", default="/clhear/AURORA_DSN", help="SSM parameter holding the target DSN")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--verify", action="store_true", help="run platform.dr.verify_record(source, target) afterwards")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from app.clhear.db import make_engine, run_migrations
    from app.clhear.platform.record_copy import copy_record

    src_path = _fetch_source(args.source, args.region)
    source = make_engine(f"sqlite:///{src_path}")
    target = make_engine(_target_dsn(args))
    t0 = time.time()
    log.info("migrating source snapshot to the current schema")
    run_migrations(source)
    log.info("migrating target")
    applied = run_migrations(target)
    log.info("target migrations applied: %s", applied or "none pending")
    report = copy_record(source, target)
    out = {"copy": report.summary(), "seconds": round(time.time() - t0, 1)}
    if args.verify:
        from app.clhear.platform.dr import verify_record

        out["verify"] = verify_record(source, target)
    print(json.dumps(out, indent=2, default=str))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
