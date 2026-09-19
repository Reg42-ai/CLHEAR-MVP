"""Lambda entrypoint for the Sources Explorer and restricted review workspace.

The configured SQLite snapshot is checked against S3 at cold start and at most
300 seconds after the preceding successful check. Restricted access fails
closed when a due check fails: cached grants are never extended by an outage.
This bounded cache window is exposed in response headers; it is not an
instantaneous revocation feed. Public legacy explorers retain their old cache
on refresh outages, but a missing configured snapshot is never an empty corpus.
"""
import json
import logging
import os
from datetime import datetime, timezone

from app.clhear.secrets import hydrate_ssm_env
from app.clhear.snapshot_sync import DB_LOCAL_PATH, REFRESH_TTL_S, sync_snapshot

hydrate_ssm_env()
log = logging.getLogger(__name__)
_state = {"etag": "", "checked": 0.0}
_snapshot_error = None
_mangum = None


class SnapshotUnavailable(RuntimeError):
    """The configured corpus or its current permission state is unavailable."""


def _restricted() -> bool:
    from app.clhear.settings import get_settings
    return get_settings().clhear_restricted_access


def _bind_database() -> None:
    from app.clhear.settings import get_settings
    value = f"sqlite:///{DB_LOCAL_PATH}"
    if os.environ.get("DATABASE_URL") != value:
        os.environ["DATABASE_URL"] = value
        get_settings.cache_clear()
        from app.clhear import db
        db.dispose_engine()


def _prepare_db() -> None:
    global _snapshot_error
    uri = os.environ.get("CLHEAR_DB_S3_URI", "")
    if not uri:
        return  # An explicitly configured direct database remains authoritative.
    if not uri.startswith("s3://"):
        _snapshot_error = SnapshotUnavailable("The configured snapshot URI is unsupported")
        raise _snapshot_error
    if not os.path.exists(DB_LOCAL_PATH):
        pending = dict(_state)
        try:
            sync_snapshot(uri, pending, local_path=DB_LOCAL_PATH, force=True)
            if not os.path.exists(DB_LOCAL_PATH):
                raise FileNotFoundError("Snapshot synchronization did not create a database")
        except Exception as exc:
            log.warning("snapshot load failed: %s: %s", type(exc).__name__, str(exc)[:300])
            _snapshot_error = SnapshotUnavailable("The configured corpus snapshot could not be loaded")
            raise _snapshot_error from exc
        _state.update(pending)
        _snapshot_error = None
    _bind_database()


def _refresh_db() -> None:
    """A failed restricted refresh must neither extend TTL nor serve old grants."""
    global _snapshot_error
    uri = os.environ.get("CLHEAR_DB_S3_URI", "")
    if not uri:
        return
    if not uri.startswith("s3://"):
        _snapshot_error = SnapshotUnavailable("The configured snapshot URI is unsupported")
        raise _snapshot_error
    restricted = _restricted()
    # sync_snapshot stamps checked before I/O. Stage restricted state so a
    # failed check cannot grant a fresh five-minute window to obsolete rights.
    pending = dict(_state) if restricted else _state
    try:
        replaced = sync_snapshot(uri, pending, local_path=DB_LOCAL_PATH,
                                 force=_snapshot_error is not None or not os.path.exists(DB_LOCAL_PATH))
        if not os.path.exists(DB_LOCAL_PATH):
            raise FileNotFoundError("Configured corpus snapshot is missing")
        if restricted:
            _state.update(pending)
        _bind_database()
        if replaced:
            from app.clhear import db
            db.dispose_engine()
        _snapshot_error = None
    except Exception as exc:
        log.warning("snapshot refresh failed: %s: %s", type(exc).__name__, str(exc)[:300])
        if restricted or not os.path.exists(DB_LOCAL_PATH):
            _snapshot_error = SnapshotUnavailable("The current corpus and permission snapshot could not be verified")
            raise _snapshot_error from exc
        log.warning("Public explorer is retaining its existing snapshot after a refresh failure")


# A cold-start outage must still produce an explicit 503 through handler,
# without starting FastAPI/Mangum against an accidental empty fallback DB.
try:
    _prepare_db()
except SnapshotUnavailable:
    log.warning("Configured snapshot is unavailable at initialization; requests will retry and fail closed")


def _application():
    global _mangum
    if _mangum is None:
        from mangum import Mangum
        from app.main import app
        _mangum = Mangum(app)
    return _mangum


def handler(event, context):
    try:
        _refresh_db()
    except SnapshotUnavailable:
        return {
            "statusCode": 503,
            "headers": {"content-type": "application/json", "cache-control": "private, no-store", "retry-after": "30"},
            "body": json.dumps({"error": "snapshot_unavailable",
                                "detail": "CLHEAR cannot verify its current corpus and permission data. Please retry shortly."}),
        }
    response = _application()(event, context)
    if _restricted() and os.environ.get("CLHEAR_DB_S3_URI", ""):
        checked = _state.get("checked") or 0
        response["headers"] = {
            **(response.get("headers") or {}),
            "cache-control": "private, no-store",
            "x-clhear-snapshot-max-age-seconds": str(REFRESH_TTL_S),
            "x-clhear-snapshot-checked-at": datetime.fromtimestamp(checked, timezone.utc).isoformat() if checked else "not_checked",
        }
    return response
