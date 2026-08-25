"""Lambda entrypoint for the public Sources Explorer.

# ARCH: HLD §5 serves the UI from the existing reg42-os web service behind the
# existing ALB (host rule clhear.reg42.ai). That infra is not accessible from
# this repo, so the MVP ships the same FastAPI app behind API Gateway
# — near-zero idle cost, swaps out cleanly when reg42-infra is wired.

The read-only corpus (SQLite maintained by the ingestion fleet) is fetched
from the deploy bucket at cold start and re-checked on a short TTL, so the
daily fleet's snapshot updates reach the public UI without a redeploy.
"""
import os
import time

DB_LOCAL_PATH = "/tmp/clhear.db"
_REFRESH_TTL_S = 300
_state = {"etag": "", "checked": 0.0}


def _parse_uri(uri: str) -> tuple[str, str]:
    bucket, key = uri[len("s3://") :].split("/", 1)
    return bucket, key


def _prepare_db() -> None:
    uri = os.environ.get("CLHEAR_DB_S3_URI", "")
    if uri.startswith("s3://") and not os.path.exists(DB_LOCAL_PATH):
        import boto3

        bucket, key = _parse_uri(uri)
        boto3.client("s3").download_file(bucket, key, DB_LOCAL_PATH)
        _state["checked"] = time.time()
    if os.path.exists(DB_LOCAL_PATH):
        os.environ["DATABASE_URL"] = f"sqlite:///{DB_LOCAL_PATH}"


def _refresh_db() -> None:
    """Re-fetch the snapshot when the fleet published a newer one (TTL-gated)."""
    uri = os.environ.get("CLHEAR_DB_S3_URI", "")
    if not uri.startswith("s3://") or time.time() - _state["checked"] < _REFRESH_TTL_S:
        return
    _state["checked"] = time.time()
    try:
        import boto3

        bucket, key = _parse_uri(uri)
        s3 = boto3.client("s3")
        etag = s3.head_object(Bucket=bucket, Key=key)["ETag"]
        if _state["etag"] and etag == _state["etag"]:
            return
        if not _state["etag"]:  # first invocation records the cold-start etag
            _state["etag"] = etag
            return
        tmp = f"{DB_LOCAL_PATH}.new"
        s3.download_file(bucket, key, tmp)
        os.replace(tmp, DB_LOCAL_PATH)
        _state["etag"] = etag
        from app.clhear import db

        db.dispose_engine()
    except Exception:  # stale-but-working beats a crashed explorer
        pass


_prepare_db()

from mangum import Mangum  # noqa: E402

from app.main import app  # noqa: E402

_mangum = Mangum(app)


def handler(event, context):
    _refresh_db()
    return _mangum(event, context)
