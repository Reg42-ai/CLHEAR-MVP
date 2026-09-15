"""Preview uses real read handlers and production sign-in without write paths."""
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest
import sqlalchemy as sa

from app.clhear import accounts, db, preview
from app.clhear.l1 import permissions, viewer_snapshot
from app.clhear.settings import get_settings
from scripts import preview as launcher
from tests.test_l1_viewer_snapshot import corpus, KEY, MARKER

EMAIL = "preview@example.test"
SECRET = "test-preview-secret-with-more-than-thirty-two-characters"


@pytest.fixture
def preview_file(engine, tmp_path):
    corpus(engine)
    permissions.record_permission(engine, source_key=KEY,
        permissions={"store": True, "display_internal": True}, evidence_ref="test-only:preview",
        approved_by="test-reviewer", approved=True,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    path = tmp_path / "candidate.db"
    viewer_snapshot.compile_viewer_snapshot(engine, path, job_id="test-worker-job")
    target = db.make_engine(f"sqlite:///{path}")
    with target.begin() as conn:
        value = conn.execute(sa.select(viewer_snapshot.STATE.c.manifest)).scalar_one()
        # Unit fixture only: simulate the metadata a PostgreSQL worker emits.
        # Runtime preview rejects the local_sqlite_test marker without a bypass.
        value.update(database_backend="postgresql", source_environment="authoritative_postgresql")
        conn.execute(viewer_snapshot.STATE.update().values(manifest=value))
    target.dispose()
    return path


@pytest.fixture
def preview_config(preview_file, monkeypatch):
    config = {"CLHEAR_PREVIEW_MODE": "true", "CLHEAR_PREVIEW_SNAPSHOT_PATH": str(preview_file),
              "CLHEAR_RESTRICTED_ACCESS": "true", "CLHEAR_REVIEWER_EMAILS": EMAIL,
              "CLHEAR_SESSION_SECRET": SECRET, "CLHEAR_AUTH_DEBUG": "false",
              "CLHEAR_PUBLIC_BASE_URL": "http://localhost:8000",
              "CLHEAR_COGNITO_REGION": "us-east-1", "CLHEAR_COGNITO_USER_POOL_ID": "test-pool",
              "CLHEAR_COGNITO_CLIENT_ID": "test-client", "CLHEAR_COGNITO_DOMAIN": "https://auth.example.test"}
    for key, value in config.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield preview_file
    db.dispose_engine()
    preview._sync_states.clear()
    get_settings.cache_clear()


@pytest.fixture
def preview_client(preview_config):
    from app.main import create_app
    with TestClient(create_app(), base_url="http://localhost:8000") as client:
        yield client


def sign_in(client, monkeypatch, *, verified=True, email=EMAIL):
    import httpx
    from app.clhear import app_auth
    token_response = Mock()
    token_response.json.return_value = {"id_token": "verified-by-test-key"}
    monkeypatch.setattr(httpx, "post", Mock(return_value=token_response))
    monkeypatch.setattr(app_auth, "verify_cognito_token", Mock(return_value={
        "email": email, "email_verified": verified, "name": "Reviewer", "sub": "test-subject"}))
    state = accounts._sign({"exp": datetime.now().timestamp() + 60}, "oauth-state")
    return client.get("/auth/cognito/callback", params={"code": "test-code", "state": state}, follow_redirects=False)


def test_authenticated_preview_reads_snapshot_without_startup_or_auth_or_read_writes(preview_config, monkeypatch):
    from app.main import create_app
    from app.clhear import community_writes, curated
    from app.clhear.platform import audit
    before = hashlib.sha256(preview_config.read_bytes()).hexdigest()
    no_write = Mock(side_effect=AssertionError("preview attempted a write"))
    monkeypatch.setattr("app.main.run_migrations", no_write)
    monkeypatch.setattr(curated, "seed", no_write)
    monkeypatch.setattr(community_writes, "dispatch", no_write)
    monkeypatch.setattr(audit, "log", no_write)
    monkeypatch.setattr(audit, "log_http_write", no_write)
    with TestClient(create_app(), base_url="http://localhost:8000") as client:
        assert client.get("/api/clhear/preview").status_code == 401
        assert client.get("/", headers={"Accept": "text/html"}, follow_redirects=False).status_code == 303
        response = sign_in(client, monkeypatch)
        assert response.status_code == 307
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "Secure" not in response.headers["set-cookie"]
        evidence = client.get("/api/clhear/preview").json()
        assert evidence["mode"] == "offline_snapshot"
        assert evidence["worker_job_id"] == "test-worker-job"
        assert evidence["snapshot_age_seconds"] >= 0
        assert evidence["publisher_checks_live"] is False
        for path in ("/", "/l1", "/sources", "/api/clhear/layers", "/api/clhear/sources",
                     "/api/clhear/l1/inventory", "/api/clhear/l1/workflow", "/api/clhear/team?layer=L1"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.headers["x-clhear-preview"] == "read-only"
            if response.headers["content-type"].startswith("text/html"):
                assert "Read-only preview" in response.text
                assert "offline snapshot" in response.text
        document = client.get(f"/api/clhear/sources/{KEY}/document")
        assert document.status_code == 200
        assert document.json()["access"]["allowed"] is True
        assert MARKER in document.text
        assert client.post("/auth/logout").status_code == 200
    no_write.assert_not_called()
    assert hashlib.sha256(preview_config.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("method,path", [
    ("post", "/api/clhear/proposals/test/approve"), ("post", "/api/clhear/community/submissions"),
    ("post", "/api/clhear/eval/test/vote"), ("post", "/graphql"), ("post", "/auth/email"),
    ("get", "/api/clhear/eval"), ("get", "/auth/email/verify?token=not-enabled"),
    ("get", "/v1/releases/latest/l1/snapshot"),
])
def test_operational_and_get_side_effects_are_blocked(preview_client, monkeypatch, method, path):
    from app.clhear import eval_studio, community_writes
    forbidden = Mock(side_effect=AssertionError("preview tried a live side effect"))
    monkeypatch.setattr(eval_studio, "sample_tasks", forbidden)
    monkeypatch.setattr(community_writes, "dispatch", forbidden)
    sign_in(preview_client, monkeypatch)
    response = getattr(preview_client, method)(path)
    assert response.status_code == 405
    assert "Read-only preview" in response.json()["detail"]
    forbidden.assert_not_called()


@pytest.mark.parametrize("verified,email", [(False, EMAIL), (None, EMAIL), (True, "outside@example.test")])
def test_real_signin_requirements_cannot_be_bypassed(preview_client, monkeypatch, verified, email):
    response = sign_in(preview_client, monkeypatch, verified=verified, email=email)
    assert response.status_code == 403
    assert accounts.SESSION_COOKIE not in response.cookies
    assert preview_client.get("/api/clhear/sources", headers={"X-Reg42-User": EMAIL}).status_code == 401


def test_hosted_preview_keeps_secure_cookie_and_uses_synchronized_database(preview_config, monkeypatch):
    from app.main import create_app
    monkeypatch.setenv("CLHEAR_PUBLIC_BASE_URL", "https://preview.example.test")
    monkeypatch.setenv("CLHEAR_PREVIEW_SNAPSHOT_PATH", "")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{preview_config}")
    get_settings.cache_clear()
    with TestClient(create_app(), base_url="https://preview.example.test") as client:
        response = sign_in(client, monkeypatch)
        assert "Secure" in response.headers["set-cookie"]
        assert client.get("/api/clhear/preview").json()["mode"] == "hosted_snapshot"
        db.dispose_engine()  # Lambda disposes its engine after snapshot replacement.
        with db.get_engine().connect() as conn:
            assert conn.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
            with pytest.raises(sa.exc.OperationalError, match="readonly"):
                conn.exec_driver_sql("CREATE TABLE forbidden_after_refresh (id int)")


def test_preview_database_enforces_readonly_and_missing_file_never_created(preview_config):
    with db.get_engine().connect() as conn:
        with pytest.raises(sa.exc.OperationalError, match="readonly"):
            conn.exec_driver_sql("CREATE TABLE forbidden (id int)")
    preview_config.unlink()
    with pytest.raises((preview.PreviewUnavailable, sa.exc.OperationalError)):
        preview.preview_status()
    assert not preview_config.exists()


def test_missing_snapshot_returns_failure_and_never_creates_fallback(preview_config, monkeypatch):
    from app.main import create_app
    missing = preview_config.parent / "missing.db"
    monkeypatch.setenv("CLHEAR_PREVIEW_SNAPSHOT_PATH", str(missing))
    get_settings.cache_clear()
    with pytest.raises(preview.PreviewUnavailable, match="missing"):
        create_app()
    assert not missing.exists()


@pytest.mark.parametrize("change", ["unmarked", "local_test", "wrong_audience", "missing_tables"])
def test_unverified_or_incomplete_projection_fails_closed(preview_config, change):
    target = db.make_engine(f"sqlite:///{preview_config}")
    with target.begin() as conn:
        value = conn.execute(sa.select(viewer_snapshot.STATE.c.manifest)).scalar_one()
        if change == "unmarked":
            value["viewer_snapshot"] = False
        elif change == "local_test":
            value["source_environment"] = "local_sqlite_test"
        elif change == "wrong_audience":
            value["audience"] = "public"
        else:
            permissions.source_permissions.drop(conn)
        conn.execute(viewer_snapshot.STATE.update().values(manifest=value))
    target.dispose()
    with pytest.raises(preview.PreviewUnavailable, match="valid authoritative"):
        db.get_engine()


@pytest.mark.parametrize("key,value", [("CLHEAR_RESTRICTED_ACCESS", "false"), ("CLHEAR_AUTH_DEBUG", "true"),
    ("CLHEAR_SESSION_SECRET", "dev-only-not-a-secret"), ("CLHEAR_REVIEWER_EMAILS", ""),
    ("CLHEAR_PUBLIC_BASE_URL", "http://0.0.0.0:8000")])
def test_insecure_preview_config_is_rejected(preview_config, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    with pytest.raises(preview.PreviewUnavailable):
        preview.validate_configuration(get_settings())


def test_rebinding_host_and_production_session_are_rejected(preview_client, monkeypatch):
    response = preview_client.get("/signin", headers={"Host": "attacker.example"})
    assert response.status_code == 400
    monkeypatch.setenv("CLHEAR_PREVIEW_MODE", "false")
    get_settings.cache_clear()
    token = accounts.session_token({"id": "test", "email": EMAIL, "display_name": "Reviewer"})
    monkeypatch.setenv("CLHEAR_PREVIEW_MODE", "true")
    get_settings.cache_clear()
    preview_client.cookies.set(accounts.SESSION_COOKIE, token)
    assert preview_client.get("/api/clhear/sources").status_code == 401


def test_permission_expiry_is_enforced_even_with_text_in_offline_snapshot(preview_client, monkeypatch):
    sign_in(preview_client, monkeypatch)
    original = permissions.decision
    future = datetime.now(timezone.utc) + timedelta(days=2)
    def expired(conn, source_key, operation, now=None):
        return original(conn, source_key, operation, now=future)
    monkeypatch.setattr(permissions, "decision", expired)
    response = preview_client.get(f"/api/clhear/sources/{KEY}/document")
    assert response.status_code == 200
    assert response.json()["locked"] is True
    assert MARKER not in response.text


def test_launcher_does_not_inherit_production_authority_or_reuse_session_key(preview_file):
    source = {key: "test-config" for key in launcher.AUTH_ENV}
    source.update(AWS_PROFILE="production", AWS_ACCESS_KEY_ID="private", AWS_SECRET_ACCESS_KEY="private",
                  CLHEAR_SESSION_SECRET="production-secret", CLHEAR_EVENTS_QUEUE_URL="production-queue",
                  CLHEAR_DB_S3_URI="s3://private/production", INFER_TOKEN="private", DATABASE_URL="postgresql://secret")
    first, second = launcher.environment(str(preview_file), source), launcher.environment(str(preview_file), source)
    assert first["CLHEAR_SESSION_SECRET"] != second["CLHEAR_SESSION_SECRET"]
    assert len(first["CLHEAR_SESSION_SECRET"]) >= 32
    assert not ({"AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "CLHEAR_EVENTS_QUEUE_URL",
                 "CLHEAR_DB_S3_URI", "INFER_TOKEN"} & first.keys())
    assert first["DATABASE_URL"].startswith("sqlite:")
    assert first["CLHEAR_AUTH_DEBUG"] == "false"


def test_launcher_missing_snapshot_or_auth_never_starts(tmp_path, monkeypatch):
    child = Mock()
    monkeypatch.setattr(launcher.subprocess, "call", child)
    with pytest.raises(SystemExit):
        launcher.main(["--snapshot", str(tmp_path / "missing.db")])
    child.assert_not_called()


def test_launcher_uses_loopback_and_reload_only(preview_file, monkeypatch):
    for key in launcher.AUTH_ENV:
        monkeypatch.setenv(key, "test-config")
    child = Mock(return_value=0)
    monkeypatch.setattr(launcher.subprocess, "call", child)
    assert launcher.main(["--snapshot", str(preview_file)]) == 0
    args = child.call_args.args[0]
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert args[args.index("--port") + 1] == "8000"
    assert "--reload" in args


def test_preview_never_hydrates_operational_ssm_secrets():
    from app.clhear.secrets import hydrate_ssm_env
    getter = Mock(side_effect=AssertionError("preview attempted a secret read"))
    assert hydrate_ssm_env(environ={"CLHEAR_PREVIEW_MODE": "true"}, getter=getter) == {}
    getter.assert_not_called()


def test_reader_refresh_failure_does_not_extend_permission_window(preview_config, monkeypatch):
    import time
    monkeypatch.setenv("CLHEAR_PREVIEW_SNAPSHOT_S3_URI", "s3://test-only/candidate.db")
    get_settings.cache_clear()
    body = preview_config.read_bytes()
    client = Mock()
    client.head_object.return_value = {"ETag": '"test-snapshot-1"'}
    client.download_file.side_effect = lambda bucket, key, destination: __import__("pathlib").Path(destination).write_bytes(body)
    clock = time.time()
    first = preview.refresh_snapshot(s3_client=client, now=clock)
    assert first["checked"] == clock
    assert preview_config.stat().st_mode & 0o777 == 0o600
    client.head_object.side_effect = RuntimeError("test-only read failure")
    with pytest.raises(preview.PreviewUnavailable, match="could not be checked"):
        preview.refresh_snapshot(s3_client=client, now=clock + 301)
    state = next(iter(preview._sync_states.values()))
    assert state["checked"] == clock and state["failed"] is True
    with pytest.raises(preview.PreviewUnavailable):
        preview.refresh_snapshot(s3_client=client, now=clock + 302)
    assert client.head_object.call_count == 3
    assert preview_config.read_bytes() == body
    client.head_object.side_effect = None
    assert preview.refresh_snapshot(s3_client=client, now=clock + 303)["failed"] is False


def test_offline_snapshot_older_than_a_day_fails_closed(preview_config):
    target = db.make_engine(f"sqlite:///{preview_config}")
    with target.begin() as conn:
        value = conn.execute(sa.select(viewer_snapshot.STATE.c.manifest)).scalar_one()
        value["generated_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        conn.execute(viewer_snapshot.STATE.update().values(manifest=value))
    target.dispose()
    with pytest.raises(preview.PreviewUnavailable, match="older than 24 hours"):
        preview.preview_status()


def test_s3_launcher_requires_explicit_profile_and_retains_no_static_credentials(tmp_path):
    source = {key: "test-config" for key in launcher.AUTH_ENV}
    with pytest.raises(ValueError, match="AWS_PROFILE"):
        launcher.environment(str(tmp_path / "candidate.db"), source, snapshot_s3="s3://private/test.db")
    source.update(AWS_PROFILE="read-only-test", AWS_ACCESS_KEY_ID="not-inherited", AWS_SECRET_ACCESS_KEY="not-inherited")
    env = launcher.environment(str(tmp_path / "candidate.db"), source, snapshot_s3="s3://private/test.db")
    assert env["AWS_PROFILE"] == "read-only-test"
    assert env["CLHEAR_PREVIEW_SNAPSHOT_S3_URI"] == "s3://private/test.db"
    assert "AWS_SECRET_ACCESS_KEY" not in env and "AWS_ACCESS_KEY_ID" not in env


def test_generic_l0_refresh_without_parent_job_is_valid_and_correlation_stays_unknown(preview_config):
    target = db.make_engine(f"sqlite:///{preview_config}")
    with target.begin() as conn:
        value = conn.execute(sa.select(viewer_snapshot.STATE.c.manifest)).scalar_one()
        value["worker_job_id"] = None  # Valid for permission-review and generic L0 refreshes.
        conn.execute(viewer_snapshot.STATE.update().values(manifest=value))
    target.dispose()
    assert preview.preview_status()["worker_job_id"] is None


def test_search_never_calls_models_even_with_inherited_infer_configuration(preview_client, monkeypatch):
    from app.clhear.platform import embeddings
    monkeypatch.setenv("INFER_BASE_URL", "https://never-call.example.test")
    monkeypatch.setenv("INFER_TOKEN", "test-only-inherited-token")
    get_settings.cache_clear()
    forbidden = Mock(side_effect=AssertionError("preview attempted a model call"))
    monkeypatch.setattr(embeddings, "embedder", forbidden)
    monkeypatch.setattr(embeddings, "semantic_search", forbidden)
    sign_in(preview_client, monkeypatch)
    response = preview_client.get("/api/clhear/search?q=test")
    assert response.status_code == 200
    forbidden.assert_not_called()


@pytest.mark.parametrize("invalidate", [False, "missing", "changed"])
def test_reload_always_heads_but_reuses_only_validated_unchanged_snapshot(preview_config, monkeypatch, invalidate):
    import time
    from pathlib import Path
    monkeypatch.setenv("CLHEAR_PREVIEW_SNAPSHOT_S3_URI", "s3://test-only/candidate.db")
    get_settings.cache_clear()
    body = preview_config.read_bytes()
    client = Mock()
    client.head_object.return_value = {"ETag": '"test-snapshot-1"'}
    client.download_file.side_effect = lambda bucket, key, destination: Path(destination).write_bytes(body)
    clock = time.time()
    preview.refresh_snapshot(s3_client=client, now=clock)
    sidecar = Path(str(preview_config) + ".preview.json")
    assert not sidecar.exists()  # A transfer by itself is not validation.
    db.get_engine()  # Validates the worker manifest and evidence tables.
    assert sidecar.exists() and sidecar.stat().st_mode & 0o777 == 0o600
    assert '"checked"' not in sidecar.read_text()
    if invalidate == "missing":
        preview_config.unlink()
    elif invalidate == "changed":
        preview_config.write_bytes(b"invalid local replacement")
    preview._sync_states.clear()  # A new reload process has no in-memory TTL.
    db.dispose_engine()
    state = preview.refresh_snapshot(s3_client=client, now=clock + 1)
    assert state["checked"] == clock + 1
    assert client.head_object.call_count == 2  # Never trust a persisted freshness clock.
    assert client.download_file.call_count == (2 if invalidate else 1)
    assert preview_config.read_bytes() == body
