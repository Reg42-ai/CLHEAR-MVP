"""The long-lived web tier never makes a request wait for a snapshot download."""
import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.clhear import web_server
from app.clhear.snapshot_sync import REFRESH_TTL_S

URI = "s3://private-bucket/webui/l1/candidate.db"


class FakeS3:
    def __init__(self, body: bytes, etag: str = '"v1"', sha256: str | None = None):
        self.body, self.etag = body, etag
        self.sha256 = hashlib.sha256(body).hexdigest() if sha256 is None else sha256
        self.downloads = 0

    def head_object(self, Bucket, Key):
        assert (Bucket, Key) == ("private-bucket", "webui/l1/candidate.db")
        return {"ETag": self.etag, "VersionId": "ver-" + self.etag.strip('"'),
                "Metadata": {"sha256": self.sha256, "revision": self.etag.strip('"')}}

    def download_file(self, bucket, key, path, ExtraArgs=None):
        assert ExtraArgs == {"VersionId": "ver-" + self.etag.strip('"')}
        self.downloads += 1
        with open(path, "wb") as fh:
            fh.write(self.body)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture()
def holder(tmp_path):
    s3, clock = FakeS3(b"snapshot-one"), Clock()
    return web_server.SnapshotHolder(URI, str(tmp_path / "clhear.db"), s3_client=s3, clock=clock), s3, clock


def test_first_refresh_downloads_verifies_and_marks_ready(holder):
    h, s3, _ = holder
    assert not h.ready()
    assert h.refresh(force=True) is True
    assert h.ready() and h.fresh()
    assert open(h.local_path, "rb").read() == b"snapshot-one"
    assert h.state["revision"] == "v1" and s3.downloads == 1


def test_unchanged_etag_only_records_the_check(holder):
    h, s3, clock = holder
    h.refresh(force=True)
    clock.now += 120
    assert h.refresh() is False
    assert s3.downloads == 1 and h.state["checked"] == clock.now


def test_changed_object_is_swapped_in_and_the_engine_disposed(holder, monkeypatch):
    from app.clhear import db

    disposed = []
    monkeypatch.setattr(db, "dispose_engine", lambda: disposed.append(True))
    h, s3, _ = holder
    h.refresh(force=True)
    s3.body, s3.etag = b"snapshot-two", '"v2"'
    s3.sha256 = hashlib.sha256(s3.body).hexdigest()
    assert h.refresh() is True
    assert open(h.local_path, "rb").read() == b"snapshot-two"
    assert h.state["revision"] == "v2" and len(disposed) == 2


def test_digest_mismatch_keeps_the_live_file_and_does_not_extend_freshness(holder):
    h, s3, clock = holder
    h.refresh(force=True)
    checked = h.state["checked"]
    s3.body, s3.etag, s3.sha256 = b"tampered", '"v3"', "0" * 64
    clock.now += 60
    with pytest.raises(RuntimeError):
        h.refresh()
    assert open(h.local_path, "rb").read() == b"snapshot-one"
    assert h.state["checked"] == checked and h.state["etag"] == '"v1"'


def _client(h, restricted=True):
    app = FastAPI()

    @app.get("/api/clhear/sources")
    def sources():
        return {"ok": True}

    return TestClient(web_server.SnapshotGate(app, h, restricted=lambda: restricted))


def test_readiness_probe_waits_for_the_first_snapshot(holder):
    h, _, _ = holder
    client = _client(h)
    assert client.get(web_server.READY_PATH).status_code == 503
    assert client.get("/api/clhear/sources").status_code == 503
    h.refresh(force=True)
    assert client.get(web_server.READY_PATH).json() == {"ready": True, "revision": "v1"}


def test_restricted_requests_fail_closed_when_checks_go_stale(holder):
    h, _, clock = holder
    h.refresh(force=True)
    client = _client(h)
    fresh = client.get("/api/clhear/sources")
    assert fresh.status_code == 200
    assert fresh.headers["x-clhear-snapshot-max-age-seconds"] == str(REFRESH_TTL_S)
    clock.now += REFRESH_TTL_S + 1
    stale = client.get("/api/clhear/sources")
    assert stale.status_code == 503 and stale.headers["retry-after"] == "30"
    assert stale.json()["error"] == "snapshot_unavailable"


def test_unrestricted_public_explorer_keeps_serving_a_stale_snapshot(holder):
    h, _, clock = holder
    h.refresh(force=True)
    clock.now += REFRESH_TTL_S * 10
    response = _client(h, restricted=False).get("/api/clhear/sources")
    assert response.status_code == 200 and "x-clhear-snapshot-checked-at" not in response.headers


def test_answers_are_computed_on_the_staged_snapshot_and_served_by_the_new_engine(tmp_path, monkeypatch):
    from app.clhear import db, snapshot_cache

    monkeypatch.setenv("CLHEAR_DB_S3_URI", URI)
    snapshot_cache.clear()
    seen = []

    @snapshot_cache.cached("probe-swap")
    def answer(engine):
        seen.append(engine)
        return "precomputed"

    def precompute(path):
        assert open(path, "rb").read() == b"snapshot-one" and not (tmp_path / "clhear.db").exists()
        staged = object()
        answer(staged)
        answers = snapshot_cache.answers_for(staged)
        snapshot_cache.forget(staged)
        return answers

    live = object()
    monkeypatch.setattr(db, "dispose_engine", lambda: None)
    monkeypatch.setattr(db, "get_engine", lambda: live)
    h = web_server.SnapshotHolder(URI, str(tmp_path / "clhear.db"), s3_client=FakeS3(b"snapshot-one"),
                                  clock=Clock(), precompute=precompute)
    assert h.refresh(force=True) is True
    assert answer(live) == "precomputed" and len(seen) == 1
    snapshot_cache.clear()


def test_a_failed_precompute_still_swaps_the_snapshot_in(tmp_path, monkeypatch):
    from app.clhear import db

    monkeypatch.setattr(db, "dispose_engine", lambda: None)

    def broken(path):
        raise RuntimeError("cold answers are slow, not wrong")

    h = web_server.SnapshotHolder(URI, str(tmp_path / "clhear.db"), s3_client=FakeS3(b"snapshot-one"),
                                  clock=Clock(), precompute=broken)
    assert h.refresh(force=True) is True and open(h.local_path, "rb").read() == b"snapshot-one"
