#!/usr/bin/env python3
"""Run the private viewer against an already authorized worker snapshot."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

REPOSITORY = Path(__file__).resolve().parents[1]
AUTH_ENV = ("CLHEAR_REVIEWER_EMAILS", "CLHEAR_COGNITO_REGION", "CLHEAR_COGNITO_USER_POOL_ID",
            "CLHEAR_COGNITO_CLIENT_ID", "CLHEAR_COGNITO_DOMAIN")
RUNTIME_ENV = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR")


def environment(snapshot: str, source: dict[str, str], *, snapshot_s3: str = "") -> dict[str, str]:
    """Do not inherit AWS credentials, production signing keys, queues or Infer."""
    path = Path(snapshot).expanduser().resolve()
    if not snapshot_s3 and not path.is_file():
        raise ValueError("Snapshot does not exist. Supply an authorized worker-produced SQLite projection.")
    missing = [key for key in AUTH_ENV if not source.get(key, "").strip()]
    if missing:
        raise ValueError("Configure reviewer emails and Cognito sign-in first: " + ", ".join(missing))
    result = {key: source[key] for key in (*AUTH_ENV, *RUNTIME_ENV) if key in source}
    result.update({"CLHEAR_PREVIEW_MODE": "true", "CLHEAR_PREVIEW_SNAPSHOT_PATH": str(path),
                   "DATABASE_URL": "sqlite:///" + str(path), "CLHEAR_PUBLIC_BASE_URL": "http://localhost:8000",
                   "CLHEAR_RESTRICTED_ACCESS": "true", "CLHEAR_AUTH_DEBUG": "false",
                   "CLHEAR_SESSION_SECRET": secrets.token_urlsafe(48),
                   "AWS_EC2_METADATA_DISABLED": "true", "PYTHONUNBUFFERED": "1"})
    if snapshot_s3:
        # This is the same S3 HEAD/GET viewer path as Lambda, never a worker.
        if not source.get("AWS_PROFILE"):
            raise ValueError("S3 preview requires an explicitly selected read-only AWS_PROFILE")
        result["AWS_PROFILE"] = source["AWS_PROFILE"]
        result["AWS_DEFAULT_REGION"] = source.get("AWS_DEFAULT_REGION", "us-east-1")
        result["CLHEAR_PREVIEW_SNAPSHOT_S3_URI"] = snapshot_s3
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    snapshots = parser.add_mutually_exclusive_group(required=True)
    snapshots.add_argument("--snapshot", help="Existing authorized L0 viewer snapshot (no download is performed)")
    snapshots.add_argument("--snapshot-s3", help="Private L0 viewer snapshot; use the existing read-only synchronizer every 300 seconds")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="clhear-private-preview-") as private_cache:
        try:
            env = environment(args.snapshot or str(Path(private_cache) / "candidate.db"), os.environ,
                              snapshot_s3=args.snapshot_s3 or "")
        except ValueError as exc:
            parser.error(str(exc))
        print("CLHEAR read-only preview: http://localhost:8000 — sign in with your approved Cognito account.", flush=True)
        print("Using the existing worker snapshot. Changes reload locally; no jobs, imports or deployments run.", flush=True)
        return subprocess.call([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                                "--port", "8000", "--reload", "--reload-dir", "app"], cwd=REPOSITORY, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
