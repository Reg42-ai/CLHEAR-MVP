"""Long-lived web tier: the explorer and /v1 served by uvicorn on ECS.

The configured viewer snapshot is downloaded and checked before the process
reports ready. A background thread then checks the S3 ETag every
``CLHEAR_SNAPSHOT_POLL_S`` seconds; a changed object is downloaded beside the
live file, verified against its ``sha256`` metadata and renamed into place, so
a request never waits for a download. Restricted access keeps the Lambda
contract: when no successful check has happened within ``REFRESH_TTL_S``, the
app answers 503 instead of serving grants that may have been revoked.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from app.clhear.snapshot_sync import REFRESH_TTL_S, parse_uri

log = logging.getLogger("clhear.web_server")

READY_PATH = "/api/clhear/ready"


class SnapshotHolder:
    """One process-wide copy of the configured snapshot on local disk."""

    def __init__(self, uri: str, local_path: str, *, s3_client=None, poll_s: float = 60.0, clock=time.time,
                 on_swap=None):
        if not uri.startswith("s3://"):
            raise ValueError("CLHEAR_DB_S3_URI must be an s3:// URI")
        self.uri, self.local_path, self.poll_s, self.clock = uri, local_path, poll_s, clock
        self.on_swap = on_swap
        self._s3 = s3_client
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.state = {"etag": "", "checked": 0.0, "revision": None, "sha256": None, "loaded_at": None}

    def s3(self):
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client("s3")
        return self._s3

    def _download(self, head: dict) -> None:
        bucket, key = parse_uri(self.uri)
        staging = f"{self.local_path}.new"
        directory = os.path.dirname(self.local_path) or "."
        os.makedirs(directory, exist_ok=True)
        for name in os.listdir(directory):
            if name.startswith(os.path.basename(staging)):
                try:
                    os.remove(os.path.join(directory, name))
                except OSError:
                    pass
        self.s3().download_file(bucket, key, staging, ExtraArgs={"IfMatch": head["ETag"]})
        expected = (head.get("Metadata") or {}).get("sha256")
        if expected:
            digest = hashlib.sha256()
            with open(staging, "rb") as fh:
                for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                os.remove(staging)
                raise RuntimeError("Downloaded snapshot does not match its published sha256")
        os.replace(staging, self.local_path)

    def refresh(self, *, force: bool = False) -> bool:
        """Check S3 once; return True when a new snapshot was swapped in."""
        bucket, key = parse_uri(self.uri)
        with self._lock:
            head = self.s3().head_object(Bucket=bucket, Key=key)
            changed = force or head["ETag"] != self.state["etag"] or not os.path.exists(self.local_path)
            if changed:
                self._download(head)
                if self.on_swap is not None:
                    self.on_swap()
                else:
                    from app.clhear import db

                    db.dispose_engine()
                metadata = head.get("Metadata") or {}
                self.state.update(etag=head["ETag"], revision=metadata.get("revision"), sha256=metadata.get("sha256"),
                                  loaded_at=datetime.now(timezone.utc).isoformat())
                log.info("viewer snapshot swapped in (revision %s)", metadata.get("revision"))
            self.state["checked"] = self.clock()
            return changed

    def fresh(self) -> bool:
        return os.path.exists(self.local_path) and self.clock() - float(self.state["checked"] or 0) < REFRESH_TTL_S

    def ready(self) -> bool:
        return os.path.exists(self.local_path) and bool(self.state["etag"])

    def run_forever(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001 — keep serving; freshness decides whether to fail closed
                log.warning("snapshot refresh failed: %s: %s", type(exc).__name__, str(exc)[:300])

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, name="snapshot-refresh", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()


def _json(status: int, body: dict, headers: dict | None = None) -> tuple[int, list, bytes]:
    payload = json.dumps(body).encode()
    base = {"content-type": "application/json", "cache-control": "private, no-store", **(headers or {})}
    return status, [(k.encode(), v.encode()) for k, v in base.items()], payload


class SnapshotGate:
    """ASGI wrapper: readiness probe, fail-closed freshness and snapshot headers."""

    def __init__(self, app, holder: SnapshotHolder, *, restricted):
        self.app, self.holder, self.restricted = app, holder, restricted

    async def _send(self, send, status, headers, payload):
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get("path") == READY_PATH:
            ok = self.holder.ready()
            return await self._send(send, *_json(200 if ok else 503, {"ready": ok, "revision": self.holder.state["revision"]}))
        restricted = self.restricted()
        if not self.holder.ready() or (restricted and not self.holder.fresh()):
            return await self._send(send, *_json(503, {
                "error": "snapshot_unavailable",
                "detail": "CLHEAR cannot verify its current corpus and permission data. Please retry shortly.",
            }, {"retry-after": "30"}))
        if not restricted:
            return await self.app(scope, receive, send)
        checked = datetime.fromtimestamp(self.holder.state["checked"], timezone.utc).isoformat()

        async def stamped(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in {b"x-clhear-snapshot-max-age-seconds", b"x-clhear-snapshot-checked-at"}]
                headers += [(b"x-clhear-snapshot-max-age-seconds", str(REFRESH_TTL_S).encode()),
                            (b"x-clhear-snapshot-checked-at", checked.encode())]
                message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, stamped)


def _bind_database(local_path: str) -> None:
    from app.clhear import db
    from app.clhear.settings import get_settings

    os.environ["DATABASE_URL"] = f"sqlite:///{local_path}"
    get_settings.cache_clear()
    db.dispose_engine()


def build(holder: SnapshotHolder):
    from app.clhear.settings import get_settings
    from app.main import create_app

    return SnapshotGate(create_app(), holder, restricted=lambda: get_settings().clhear_restricted_access)


def main() -> None:
    import uvicorn

    from app.clhear.secrets import hydrate_ssm_env

    logging.basicConfig(level=logging.INFO)
    hydrate_ssm_env()
    uri = os.environ.get("CLHEAR_DB_S3_URI", "")
    local_path = os.environ.get("CLHEAR_DB_LOCAL_PATH", "/data/clhear.db")
    holder = SnapshotHolder(uri, local_path, poll_s=float(os.environ.get("CLHEAR_SNAPSHOT_POLL_S", "60")))
    _bind_database(local_path)
    started = time.monotonic()
    holder.refresh(force=True)
    log.info("viewer snapshot ready in %.1fs", time.monotonic() - started)
    holder.start()
    release_uri = os.environ.get("CLHEAR_RELEASE_DB_S3_URI", "")
    if release_uri:
        # /v1 reads the published release artifact, not the reviewers' viewer.
        from app.clhear import release_db

        release_path = os.environ.get(release_db.PATH_ENV, "/tmp/release.db")
        release = SnapshotHolder(release_uri, release_path, poll_s=holder.poll_s, on_swap=release_db.dispose)
        try:
            release.refresh(force=True)
            os.environ[release_db.PATH_ENV] = release_path
            release.start()
        except Exception as exc:  # noqa: BLE001 — the viewer still serves; /v1 falls back to it
            log.warning("release snapshot unavailable: %s: %s", type(exc).__name__, str(exc)[:300])
    uvicorn.run(build(holder), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                proxy_headers=True, forwarded_allow_ips="*", log_level="info", access_log=False)


if __name__ == "__main__":
    main()
