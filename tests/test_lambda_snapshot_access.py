"""Restricted Lambda readers never renew stale permission grants during outages."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.clhear import db, secrets, snapshot_sync
from app.clhear.settings import get_settings


class FakeS3:
    def __init__(self):
        self.etag = '"version-1"'
        self.body = b"TEST ONLY snapshot"
        self.failed = False
        self.download_failed = False
        self.head_calls = 0
        self.download_calls = 0

    def head_object(self, **kwargs):
        self.head_calls += 1
        if self.failed:
            raise RuntimeError("test-only S3 outage")
        return {"ETag": self.etag}

    def download_file(self, bucket, key, destination):
        self.download_calls += 1
        if self.download_failed:
            Path(destination).write_bytes(b"partial TEST ONLY transfer")
            raise RuntimeError("test-only interrupted download")
        Path(destination).write_bytes(self.body)


@pytest.fixture
def load_lambda(tmp_path, monkeypatch):
    clock = [1000.0]
    s3 = FakeS3()
    local = tmp_path / "reader.db"
    fallback = tmp_path / "must-not-create.db"
    original_sync = snapshot_sync.sync_snapshot
    monkeypatch.setattr(secrets, "hydrate_ssm_env", Mock())
    monkeypatch.setattr(db, "dispose_engine", Mock())
    monkeypatch.setattr(snapshot_sync, "DB_LOCAL_PATH", str(local))
    monkeypatch.setattr(snapshot_sync, "sync_snapshot", lambda uri, state, **kwargs: original_sync(
        uri, state, now=clock[0], s3_client=s3, **kwargs))
    def load(*, restricted=True, existing=True, fail=False, uri="s3://test-only/reader.db"):
        monkeypatch.setenv("CLHEAR_RESTRICTED_ACCESS", "true" if restricted else "false")
        monkeypatch.setenv("CLHEAR_DB_S3_URI", uri)
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{fallback}")
        get_settings.cache_clear()
        if existing:
            local.write_bytes(b"previous TEST ONLY snapshot")
        s3.failed = fail
        spec = importlib.util.spec_from_file_location("clhear_lambda_test", Path(__file__).resolve().parents[1] / "app/clhear/lambda_web.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    yield load, s3, clock, local, fallback
    get_settings.cache_clear()


def test_due_restricted_refresh_failure_blocks_reads_and_does_not_renew_ttl(load_lambda):
    load, s3, clock, local, _ = load_lambda
    module = load()
    application = Mock(return_value={"statusCode": 200, "headers": {}, "body": "test-only response"})
    module._mangum = application
    first = module.handler({}, None)
    assert first["statusCode"] == 200
    assert first["headers"]["x-clhear-snapshot-max-age-seconds"] == "300"
    assert module._state["checked"] == 1000
    application.reset_mock()
    prior = local.read_bytes()
    s3.failed = True
    clock[0] = 1301
    failure = module.handler({}, None)
    assert failure["statusCode"] == 503
    assert failure["headers"]["cache-control"] == "private, no-store"
    assert "snapshot_unavailable" in failure["body"]
    application.assert_not_called()
    assert local.read_bytes() == prior
    assert module._state["checked"] == 1000  # failed sync stamped only its staged copy
    clock[0] = 1302
    assert module.handler({}, None)["statusCode"] == 503
    assert s3.head_calls == 3  # failure cannot create a new cache window


def test_restricted_reader_recovers_only_after_successful_refresh(load_lambda):
    load, s3, clock, local, _ = load_lambda
    module = load(fail=True)
    application = Mock(return_value={"statusCode": 200, "body": "ok"})
    module._mangum = application
    assert module.handler({}, None)["statusCode"] == 503
    s3.failed = False
    s3.body = b"TEST ONLY updated permission ledger"
    clock[0] += 1
    response = module.handler({}, None)
    assert response["statusCode"] == 200
    assert local.read_bytes() == s3.body
    assert module._snapshot_error is None
    assert module._state["checked"] == clock[0]
    application.assert_called_once()


@pytest.mark.parametrize("restricted", [True, False])
def test_cold_start_missing_snapshot_never_creates_fallback_database(load_lambda, restricted):
    load, s3, clock, local, fallback = load_lambda
    module = load(restricted=restricted, existing=False, fail=True)
    assert module._mangum is None
    assert module._snapshot_error is not None
    assert not local.exists() and not fallback.exists()
    assert module.handler({}, None)["statusCode"] == 503
    assert module._mangum is None
    assert not fallback.exists()
    # The next successful pull recovers the same warm container.
    s3.failed = False
    module._mangum = Mock(return_value={"statusCode": 200, "body": "ok"})
    assert module.handler({}, None)["statusCode"] == 200
    assert local.read_bytes() == s3.body
    assert not fallback.exists()


def test_public_legacy_reader_keeps_existing_stale_cache_behavior(load_lambda):
    load, s3, clock, local, _ = load_lambda
    module = load(restricted=False, fail=True)
    previous = local.read_bytes()
    module._mangum = Mock(return_value={"statusCode": 200, "body": "legacy"})
    assert module.handler({}, None)["statusCode"] == 200
    assert local.read_bytes() == previous
    assert s3.head_calls == 1
    clock[0] += 1
    assert module.handler({}, None)["statusCode"] == 200
    assert s3.head_calls == 1  # legacy stale retry throttling is unchanged


def test_successful_restricted_check_uses_only_the_declared_five_minute_window(load_lambda):
    load, s3, clock, local, _ = load_lambda
    module = load()
    module._mangum = Mock(return_value={"statusCode": 200, "body": "ok"})
    assert module.handler({}, None)["statusCode"] == 200
    first_checked = module.handler({}, None)["headers"]["x-clhear-snapshot-checked-at"]
    clock[0] = 1299
    within = module.handler({}, None)
    assert within["statusCode"] == 200
    assert within["headers"]["x-clhear-snapshot-checked-at"] == first_checked
    assert s3.head_calls == 1
    clock[0] = 1300
    due = module.handler({}, None)
    assert due["headers"]["x-clhear-snapshot-checked-at"] != first_checked
    assert s3.head_calls == 2
    assert s3.download_calls == 1  # unchanged ETag still confirms freshness


def test_interrupted_download_preserves_old_file_and_blocks_restricted_reads(load_lambda):
    load, s3, clock, local, _ = load_lambda
    module = load()
    module._mangum = Mock(return_value={"statusCode": 200, "body": "ok"})
    module.handler({}, None)
    previous = local.read_bytes()
    s3.etag = '"new-version"'
    s3.download_failed = True
    clock[0] = 1301
    assert module.handler({}, None)["statusCode"] == 503
    assert local.read_bytes() == previous
    assert module._state["etag"] == '"version-1"'
    assert module._state["checked"] == 1000


def test_missing_local_file_bypasses_ttl_and_fails_closed(load_lambda):
    load, s3, clock, local, _ = load_lambda
    module = load()
    module._mangum = Mock(return_value={"statusCode": 200, "body": "ok"})
    module.handler({}, None)
    local.unlink()
    s3.failed = True
    clock[0] += 1
    assert module.handler({}, None)["statusCode"] == 503
    assert s3.head_calls == 2


def test_no_snapshot_configuration_does_not_override_direct_database(load_lambda, monkeypatch):
    import os
    load, s3, clock, local, fallback = load_lambda
    module = load(uri="")
    module._mangum = Mock(return_value={"statusCode": 200, "body": "direct"})
    assert module.handler({}, None)["statusCode"] == 200
    assert os.environ["DATABASE_URL"] == f"sqlite:///{fallback}"
    assert s3.head_calls == 0
