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

from app.clhear.secrets import hydrate_ssm_env
from app.clhear.snapshot_sync import DB_LOCAL_PATH, sync_snapshot

hydrate_ssm_env()

_state = {"etag": "", "checked": 0.0}


def _prepare_db() -> None:
    uri = os.environ.get("CLHEAR_DB_S3_URI", "")
    if uri.startswith("s3://") and not os.path.exists(DB_LOCAL_PATH):
        try:
            sync_snapshot(uri, _state, local_path=DB_LOCAL_PATH, force=True)
        except Exception:
            pass
    if os.path.exists(DB_LOCAL_PATH):
        os.environ["DATABASE_URL"] = f"sqlite:///{DB_LOCAL_PATH}"


def _refresh_db() -> None:
    """Re-fetch the snapshot when the fleet published a newer one (TTL-gated)."""
    uri = os.environ.get("CLHEAR_DB_S3_URI", "")
    if not uri.startswith("s3://"):
        return
    try:
        if sync_snapshot(uri, _state, local_path=DB_LOCAL_PATH):
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
