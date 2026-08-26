"""Snapshot refresh must download when the S3 ETag is new or unknown."""
from app.clhear.snapshot_sync import sync_snapshot


class FakeS3:
    def __init__(self, etag: str, payload: bytes):
        self.etag = etag
        self.payload = payload
        self.downloads = 0

    def head_object(self, Bucket, Key):
        return {"ETag": self.etag}

    def download_file(self, bucket, key, dest):
        self.downloads += 1
        with open(dest, "wb") as fh:
            fh.write(self.payload)


def test_sync_downloads_when_etag_unknown(tmp_path):
    local = tmp_path / "clhear.db"
    local.write_bytes(b"stale")
    s3 = FakeS3('"abc"', b"fresh")
    state = {"etag": "", "checked": 0.0}
    assert sync_snapshot("s3://bucket/webui/clhear-latest.db", state, local_path=str(local), s3_client=s3)
    assert local.read_bytes() == b"fresh"
    assert state["etag"] == '"abc"'
    assert s3.downloads == 1


def test_sync_skips_when_etag_matches(tmp_path):
    local = tmp_path / "clhear.db"
    local.write_bytes(b"current")
    s3 = FakeS3('"abc"', b"ignored")
    state = {"etag": '"abc"', "checked": 0.0}
    assert not sync_snapshot("s3://bucket/webui/clhear-latest.db", state, local_path=str(local), s3_client=s3)
    assert local.read_bytes() == b"current"
    assert s3.downloads == 0


def test_sync_downloads_when_etag_changes(tmp_path):
    local = tmp_path / "clhear.db"
    local.write_bytes(b"old")
    s3 = FakeS3('"new"', b"newer")
    state = {"etag": '"old"', "checked": 0.0}
    assert sync_snapshot("s3://bucket/webui/clhear-latest.db", state, local_path=str(local), s3_client=s3)
    assert local.read_bytes() == b"newer"
    assert state["etag"] == '"new"'


def test_sync_respects_ttl(tmp_path):
    local = tmp_path / "clhear.db"
    local.write_bytes(b"old")
    s3 = FakeS3('"new"', b"newer")
    state = {"etag": '"old"', "checked": 1000.0}
    assert not sync_snapshot(
        "s3://bucket/webui/clhear-latest.db",
        state,
        local_path=str(local),
        s3_client=s3,
        now=1001.0,
        ttl_s=300,
    )
    assert s3.downloads == 0
