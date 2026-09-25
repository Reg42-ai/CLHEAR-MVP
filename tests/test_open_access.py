"""Account-required mode: free with an account, every request counted, abuse limited."""
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.clhear import identity, ratelimit, usage
from app.clhear.accounts import SESSION_COOKIE, session_token
from app.clhear.community_writes import user_id_for
from app.clhear.identity_models import account_profiles, api_usage
from app.clhear.settings import get_settings

SECRET = "local-test-session-secret-with-more-than-32-characters"
USER = {"id": user_id_for("analyst@example.test"), "email": "analyst@example.test", "display_name": "Analyst"}
APP_KEY = "os-test-key-with-more-than-thirty-two-characters"


@pytest.fixture()
def accounts(engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_ACCESS_MODE", "accounts")
    monkeypatch.setenv("CLHEAR_RESTRICTED_ACCESS", "true")
    monkeypatch.setenv("CLHEAR_REVIEWER_EMAILS", "reviewer@example.test")
    monkeypatch.setenv("CLHEAR_SESSION_SECRET", SECRET)
    monkeypatch.setenv("CLHEAR_AUTH_DEBUG", "false")
    monkeypatch.setenv("CLHEAR_APP_KEYS", f"galaxy:{APP_KEY}")
    get_settings.cache_clear()
    ratelimit.reset_local()
    from app.main import create_app

    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client
    get_settings.cache_clear()


def _sign_in(client, user=USER):
    client.cookies.set(SESSION_COOKIE, session_token(user))


def test_public_pages_need_no_account_and_the_rest_needs_one(accounts):
    for path in ("/signin", "/signup", "/terms", "/api/clhear/health"):
        assert accounts.get(path).status_code == 200, path
    assert accounts.get("/api/clhear/sources").status_code == 401
    assert accounts.get("/l1", headers={"Accept": "text/html"}, follow_redirects=False).status_code == 303
    _sign_in(accounts)
    assert accounts.get("/api/clhear/sources").status_code == 200


def test_any_account_reads_but_only_reviewers_write(accounts):
    _sign_in(accounts)
    response = accounts.post("/api/clhear/community/votes", json={})
    assert response.status_code == 403 and "Reviewer" in response.json()["detail"]
    assert accounts.get("/admin/usage").status_code == 403


def test_suspended_accounts_are_refused(accounts, engine):
    with engine.begin() as conn:
        conn.execute(account_profiles.insert().values(user_id=USER["id"], email=USER["email"], status="suspended"))
    _sign_in(accounts)
    assert accounts.get("/api/clhear/sources").status_code == 403


def test_v1_needs_a_key_and_a_key_is_rate_limited(accounts, monkeypatch):
    assert accounts.get("/v1/layers").status_code == 401
    monkeypatch.setenv("CLHEAR_RATE_V1_PER_MINUTE", "2")
    get_settings.cache_clear()
    headers = {"Authorization": f"Bearer {APP_KEY}", "X-App-Id": "galaxy"}
    assert [accounts.get("/v1/layers", headers=headers).status_code for _ in range(2)] == [200, 200]
    limited = accounts.get("/v1/layers", headers=headers)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1


def test_signup_link_carries_the_profile_and_terms(accounts, engine, monkeypatch):
    import boto3

    sent = Mock()
    monkeypatch.setattr(boto3, "client", lambda *a, **k: Mock(send_email=sent))
    body = {"email": "new@example.test", "signup": {"name": "New Person", "organization": "Galaxy",
                                                     "intended_use": "Build a compliance program", "accept_terms": True}}
    response = accounts.post("/auth/email", json=body)
    assert response.status_code == 200 and response.json()["sent"] is True
    link = sent.call_args.kwargs["Content"]["Simple"]["Body"]["Text"]["Data"].split("\n\n")[1]
    verified = accounts.get(link.split("testserver", 1)[-1].replace("http://localhost:8000", ""), follow_redirects=False)
    assert verified.status_code in {302, 303, 307}
    with engine.connect() as conn:
        row = conn.execute(sa.select(account_profiles).where(account_profiles.c.email == "new@example.test")).mappings().one()
    assert row["organization"] == "Galaxy" and row["terms_accepted_at"] is not None
    assert row["terms_version"] == get_settings().clhear_terms_version


def test_signup_without_accepting_terms_is_refused(accounts):
    response = accounts.post("/auth/email", json={"email": "x@example.test", "signup": {"name": "X", "accept_terms": False}})
    assert response.status_code == 400


def test_magic_links_are_rate_limited_per_address(accounts, monkeypatch):
    import boto3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: Mock())
    codes = [accounts.post("/auth/email", json={"email": "burst@example.test"}).status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429


def test_every_request_is_counted_with_its_route_template_and_layer(accounts, engine):
    _sign_in(accounts)
    accounts.get("/api/clhear/sources?x=secret-query")
    accounts.get("/v1/releases/2026.09.13/l2/obligations", headers={"Authorization": f"Bearer {APP_KEY}", "X-App-Id": "galaxy"})
    usage.flush()
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(sa.select(api_usage)).mappings()]
    routes = {r["route"] for r in rows}
    assert "/api/clhear/sources" in routes and not any("secret-query" in r for r in routes)
    v1 = next(r for r in rows if r["app_id"] == "galaxy")
    assert v1["route"] == "/v1/releases/{release_id}/{layer}/{resource}" and v1["layer"] == "L2"
    assert all(len(r["ip_hash"]) == 32 for r in rows)
    assert next(r for r in rows if r["route"] == "/api/clhear/sources")["user_id"] == USER["id"]


def test_daily_rollups_recompute_in_place(accounts, engine):
    from datetime import datetime, timezone

    from app.clhear.identity_models import api_usage_daily

    _sign_in(accounts)
    accounts.get("/api/clhear/sources")
    usage.flush()
    today = datetime.now(timezone.utc).date()

    def sources_requests():
        with engine.connect() as conn:
            return [r.requests for r in conn.execute(sa.select(api_usage_daily).where(
                api_usage_daily.c.day == today, api_usage_daily.c.route == "/api/clhear/sources"))]

    usage.rollup(today)
    usage.rollup(today)
    assert sources_requests() == [1]
    accounts.get("/api/clhear/sources")
    usage.flush()
    usage.rollup(today)
    assert sources_requests() == [2]


def test_security_headers_and_cross_site_session_writes(accounts):
    response = accounts.get("/signin")
    for header in ("strict-transport-security", "content-security-policy", "x-content-type-options",
                   "referrer-policy", "permissions-policy"):
        assert header in response.headers
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    _sign_in(accounts)
    blocked = accounts.post("/keys", json={"label": "x"}, headers={"Origin": "https://evil.example"})
    assert blocked.status_code == 403


def test_the_edge_header_is_required_when_configured(accounts, monkeypatch):
    monkeypatch.setenv("CLHEAR_ORIGIN_VERIFY_SECRET", "edge-secret")
    get_settings.cache_clear()
    assert accounts.get("/signin").status_code == 403
    assert accounts.get("/signin", headers={"X-CLHEAR-Origin": "edge-secret"}).status_code == 200


def test_page_and_body_sizes_are_capped(accounts):
    _sign_in(accounts)
    unbounded = []
    for path, operations in accounts.app.openapi()["paths"].items():
        for operation in operations.values():
            for param in operation.get("parameters", []):
                if param["in"] == "query" and param["name"] == "limit":
                    schema = param.get("schema", {})
                    maximum = schema.get("maximum", next((s.get("maximum") for s in schema.get("anyOf", []) if "maximum" in s), None))
                    if maximum is None or maximum > 5000:
                        unbounded.append(path)
    assert unbounded == []
    assert accounts.get("/api/clhear/community/submissions?limit=100000").status_code == 422
    big = accounts.post("/auth/email", content=b"x" * 2_000_001, headers={"Content-Type": "application/json"})
    assert big.status_code == 413


def test_licensed_text_stays_reviewer_only_in_account_mode(accounts, engine, monkeypatch):
    from app.clhear.l1 import routes

    source = type("Source", (), {"key": "iso/27001-2022", "license": "restricted", "rights_basis": "licensed",
                                 "canonical_url": "https://www.iso.org/standard/27001"})()
    monkeypatch.setattr("app.clhear.l1.permissions.required_for", lambda s: True)
    monkeypatch.setattr("app.clhear.l1.permissions.decision",
                        lambda conn, key, op: {"allowed": True, "reason": "private POC grant"})
    request = type("Req", (), {"cookies": {}, "headers": {}, "state": type("S", (), {})()})()
    monkeypatch.setattr("app.clhear.review_access.reviewer", lambda req: None)
    with engine.connect() as conn:
        access = routes._text_access(conn, source, request)
    assert access["allowed"] is False and "reviewers" in access["reason"]
