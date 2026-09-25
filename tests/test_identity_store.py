"""Accounts and keys written by the read-only web tier land in the identity store."""
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.clhear import identity
from app.clhear.accounts import SESSION_COOKIE, session_token, upsert_user
from app.clhear.community_models import api_keys as api_keys_t, users
from app.clhear.community_writes import user_id_for
from app.clhear.db import make_engine, run_migrations
from app.clhear.identity_models import TABLES

USER = {"id": user_id_for("dev@example.org"), "email": "dev@example.org", "display_name": "Dev"}


def _identity_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/identity.db"
    store = make_engine(url)
    run_migrations(store)
    monkeypatch.setenv(identity.IDENTITY_URL_ENV, url)
    identity.reset()
    return store


def test_migration_creates_the_identity_tables(engine):
    names = set(sa.inspect(engine).get_table_names())
    assert {t.name for t in TABLES} <= names


def test_keys_issued_on_a_snapshot_tier_are_stored_and_verified_in_the_identity_store(engine, tmp_path, monkeypatch):
    store = _identity_db(tmp_path, monkeypatch)
    monkeypatch.setenv("CLHEAR_DB_S3_URI", "s3://private/webui/l1/candidate.db")
    from app.main import create_app

    with TestClient(create_app()) as client:
        client.cookies.set(SESSION_COOKIE, session_token(USER))
        key = client.post("/keys", json={"label": "os"}).json()
        client.cookies.clear()
        headers = {"X-App-Id": key["app_id"], "Authorization": f"Bearer {key['secret']}"}
        assert client.get("/v1/layers", headers=headers).status_code == 200
    with store.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(api_keys_t)).scalar_one() == 1
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(api_keys_t)).scalar_one() == 0
    identity.reset()


def test_key_issuance_refuses_when_writes_would_be_lost(engine, monkeypatch):
    monkeypatch.delenv(identity.IDENTITY_URL_ENV, raising=False)
    identity.reset()
    monkeypatch.setenv("CLHEAR_DB_S3_URI", "s3://private/webui/l1/candidate.db")
    from app.main import create_app

    with TestClient(create_app()) as client:
        client.cookies.set(SESSION_COOKIE, session_token(USER))
        response = client.post("/keys", json={"label": "lost"})
    assert response.status_code == 503


def test_sign_in_writes_the_account_directly_when_the_store_is_configured(engine, tmp_path, monkeypatch):
    store = _identity_db(tmp_path, monkeypatch)
    monkeypatch.setenv("CLHEAR_DB_S3_URI", "s3://private/webui/l1/candidate.db")
    result = upsert_user(engine, "New.Person@Example.org", display_name="New Person", provider="google")
    assert result["email"] == "new.person@example.org"
    with store.connect() as conn:
        row = conn.execute(sa.select(users).where(users.c.email == "new.person@example.org")).mappings().one()
    assert row["provider"] == "google"
    identity.reset()
