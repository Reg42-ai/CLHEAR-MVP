"""Private reviewer access guards every corpus surface, including direct APIs."""
import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.clhear.accounts import SESSION_COOKIE, session_token
from app.clhear.settings import get_settings


REVIEWER = "reviewer@example.test"
TEST_SECRET = "local-test-session-secret-with-more-than-32-characters"


@pytest.fixture()
def restricted_settings(engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_RESTRICTED_ACCESS", "true")
    monkeypatch.setenv("CLHEAR_REVIEWER_EMAILS", f" {REVIEWER.upper()} ")
    monkeypatch.setenv("CLHEAR_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("CLHEAR_AUTH_DEBUG", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def restricted_client(restricted_settings):
    from app.main import create_app

    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client


def _session(client, email=REVIEWER):
    client.cookies.set(SESSION_COOKIE, session_token({
        "id": "test-reviewer", "email": email, "display_name": "Reviewer",
    }))


@pytest.mark.parametrize("path", ["/", "/stack", "/solon", "/l1", "/sources", "/l2", "/console"])
def test_anonymous_html_redirects_to_signin(restricted_client, path):
    response = restricted_client.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/signin"
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("path", [
    "/api/clhear/layers", "/api/clhear/sources", "/api/clhear/sources/finra/2210/document",
    "/api/clhear/nodes/1", "/api/clhear/fleet", "/api/clhear/jobs/latest",
    "/l1/sources", "/v1/releases/latest/l1/snapshot",
    "/l6/blueprints/BLU-unknown/export?format=oscal", "/graphql?query=%7B__typename%7D",
])
def test_anonymous_direct_reads_exports_and_graphql_are_blocked(restricted_client, path):
    response = restricted_client.get(path)
    assert response.status_code == 401
    assert response.json()["detail"] == "An approved reviewer must sign in."
    assert "no-store" in response.headers["cache-control"]


def test_graphql_post_cannot_bypass_reviewer_check(restricted_client):
    response = restricted_client.post("/graphql", json={"query": "{ __typename }"})
    assert response.status_code == 401
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("headers", [
    {"X-Reg42-User": REVIEWER},
    {"X-Reg42-User": "avner@reg42.ai"},
    {"X-App-Id": "os-dev", "X-API-Key": "dev-os-key"},
])
def test_identity_headers_do_not_grant_reviewer_access(restricted_client, headers):
    response = restricted_client.get("/api/clhear/sources", headers=headers)
    assert response.status_code == 401


APP_KEY = "os-test-key-with-more-than-thirty-two-characters"


@pytest.fixture()
def app_keys(restricted_settings, monkeypatch):
    monkeypatch.setenv("CLHEAR_APP_KEYS", f"os-dev:{APP_KEY}")
    get_settings.cache_clear()


def test_app_key_reads_v1_release_under_restricted_access(restricted_client, app_keys, monkeypatch):
    from app.clhear import releases

    monkeypatch.setattr(releases, "get_latest", lambda engine=None: {"id": "2026.09.13", "layers": ["L0", "L1"]})
    response = restricted_client.get("/v1/releases/latest",
                                     headers={"Authorization": f"Bearer {APP_KEY}", "X-App-Id": "os-dev"})
    assert response.status_code == 200, response.text
    assert response.json()["id"] == "2026.09.13"
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("headers,detail", [
    ({"Authorization": "Bearer wrong-key", "X-App-Id": "os-dev"}, "Invalid bearer token"),
    ({"Authorization": f"Bearer {APP_KEY}", "X-App-Id": "unknown-app"}, "Unknown or missing X-App-Id"),
])
def test_app_key_bypass_still_requires_a_valid_key(restricted_client, app_keys, headers, detail):
    response = restricted_client.get("/v1/releases/latest", headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"] == detail


def test_app_key_headers_do_not_open_non_v1_routes(restricted_client, app_keys):
    headers = {"Authorization": f"Bearer {APP_KEY}", "X-App-Id": "os-dev"}
    for path in ("/api/clhear/sources", "/l1/sources", "/v1x/releases", "/graphql?query=%7B__typename%7D"):
        assert restricted_client.get(path, headers=headers).status_code == 401, path


def test_every_v1_route_requires_an_app_key(restricted_settings):
    from fastapi.routing import APIRoute

    from app.clhear.app_api import router as v1_router
    from app.clhear.app_auth import require_app
    from app.main import create_app

    def depends_on(dependant, target):
        return any(d.call is target or depends_on(d, target) for d in dependant.dependencies)

    guarded = {r.path for r in v1_router.routes if isinstance(r, APIRoute) and depends_on(r.dependant, require_app)}
    declared = {r.path for r in v1_router.routes if isinstance(r, APIRoute)}
    assert declared and guarded == declared
    served = {p for p in create_app().openapi()["paths"] if p == "/v1" or p.startswith("/v1/")}
    # Every /v1 path the app serves comes from the guarded router, never from another one.
    assert served and served <= guarded


def test_allowlisted_signed_session_reads_real_handlers_without_shared_caching(restricted_client):
    _session(restricted_client)
    for path in ("/", "/l1", "/api/clhear/sources", "/api/clhear/layers", "/graphql?query=%7B__typename%7D"):
        response = restricted_client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert response.headers["cache-control"] == "private, no-store"
        assert {"cookie", "authorization"} <= {
            item.strip().lower() for item in response.headers["vary"].split(",")
        }
        assert response.headers["x-robots-tag"] == "noindex, nofollow"


def test_valid_session_for_unapproved_user_is_not_enough(restricted_client):
    _session(restricted_client, "outside@example.test")
    response = restricted_client.get("/api/clhear/sources", headers={"X-Reg42-User": REVIEWER})
    assert response.status_code == 401


def test_tampered_session_is_rejected(restricted_client):
    token = session_token({"id": "test", "email": REVIEWER, "display_name": "Reviewer"})
    replacement = "0" if token[-1] != "0" else "1"
    restricted_client.cookies.set(SESSION_COOKIE, token[:-1] + replacement)
    assert restricted_client.get("/api/clhear/sources").status_code == 401


@pytest.mark.parametrize("verified,status", [(True, 200), (False, 401), (None, 401)])
def test_cognito_bearer_requires_verified_allowlisted_email(restricted_client, monkeypatch, verified, status):
    from app.clhear import app_auth

    verifier = Mock(return_value={"email": REVIEWER, "name": "Reviewer", "email_verified": verified})
    monkeypatch.setattr(app_auth, "verify_cognito_token", verifier)
    response = restricted_client.get("/api/clhear/sources", headers={"Authorization": "Bearer test.signed.token"})
    assert response.status_code == status
    verifier.assert_called_with("test.signed.token")


@pytest.mark.parametrize("secret,debug", [
    ("dev-only-not-a-secret", "false"), ("", "false"), ("   ", "false"),
    ("x" * 31, "false"), (TEST_SECRET, "true"),
])
def test_insecure_restricted_configuration_fails_closed(restricted_client, monkeypatch, secret, debug):
    # A previously issued session must not bypass a now-invalid configuration.
    _session(restricted_client)
    monkeypatch.setenv("CLHEAR_SESSION_SECRET", secret)
    monkeypatch.setenv("CLHEAR_AUTH_DEBUG", debug)
    get_settings.cache_clear()
    response = restricted_client.get("/api/clhear/sources")
    assert response.status_code == 503
    assert "no-store" in response.headers["cache-control"]


def test_signin_and_auth_discovery_remain_reachable(restricted_client):
    for path in ("/signin", "/auth/me", "/static/theme.css", "/api/clhear/health"):
        response = restricted_client.get(path)
        assert response.status_code == 200, path
        assert "no-store" in response.headers["cache-control"]
    assert "Reviewer sign in" in restricted_client.get("/signin").text
    assert restricted_client.get("/auth/me").json()["user"] is None


def test_magic_link_request_does_not_reveal_or_mail_an_unapproved_recipient(restricted_client, monkeypatch):
    import boto3

    monkeypatch.setattr(boto3, "client", Mock(side_effect=AssertionError("no mail for an unapproved address")))
    outside = restricted_client.post("/auth/email", json={"email": "outside@example.test"})
    assert outside.status_code == 200 and outside.json()["sent"] is True
    assert "debug_link" not in outside.json()


@pytest.mark.parametrize("provider", ["google", "cognito"])
@pytest.mark.parametrize("email,verified,status", [
    (REVIEWER.upper(), True, 307),
    (REVIEWER, False, 403),
    (REVIEWER, None, 403),
    ("outside@example.test", True, 403),
])
def test_oauth_callbacks_issue_session_only_for_verified_reviewer(
    restricted_client, monkeypatch, provider, email, verified, status,
):
    import httpx
    from app.clhear import accounts, app_auth

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-google-client")
    monkeypatch.setenv("CLHEAR_COGNITO_DOMAIN", "https://auth.example.test")
    get_settings.cache_clear()
    identity = {"email": email, "email_verified": verified, "name": "Reviewer", "sub": "test-subject"}
    # All provider I/O stays local. The test exercises the callback's checks
    # after provider verification, not the provider's token verifier itself.
    token_response = httpx.Response(200, json={"access_token": "test-access", "id_token": "test-id"},
                                   request=httpx.Request("POST", "https://auth.example.test/token"))
    monkeypatch.setattr(httpx, "post", Mock(return_value=token_response))
    monkeypatch.setattr(httpx, "get", Mock(return_value=httpx.Response(200, json=identity)))
    monkeypatch.setattr(app_auth, "cognito_enabled", lambda: True)
    monkeypatch.setattr(app_auth, "verify_cognito_token", Mock(return_value=identity))
    upsert = Mock(return_value={"id": "test-reviewer", "email": REVIEWER, "display_name": "Reviewer"})
    monkeypatch.setattr(accounts, "upsert_user", upsert)
    state = accounts._sign({"exp": time.time() + 60}, "oauth-state")
    response = restricted_client.get(f"/auth/{provider}/callback", params={"code": "test-code", "state": state},
                                     follow_redirects=False)
    assert response.status_code == status
    assert "no-store" in response.headers["cache-control"]
    if status == 307:
        upsert.assert_called_once()
        assert response.headers["location"] == "/"
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie
        assert restricted_client.get("/api/clhear/sources").status_code == 200
    else:
        upsert.assert_not_called()
        assert "set-cookie" not in response.headers


def test_restricted_startup_never_migrates_or_seeds(restricted_settings, monkeypatch):
    from app import main
    from app.clhear import curated

    migrate = Mock(side_effect=AssertionError("Restricted startup must not migrate"))
    seed = Mock(side_effect=AssertionError("Restricted startup must not seed"))
    monkeypatch.setattr(main, "run_migrations", migrate)
    monkeypatch.setattr(curated, "seed", seed)
    with TestClient(main.create_app()) as client:
        assert client.get("/signin").status_code == 200
    migrate.assert_not_called()
    seed.assert_not_called()


def test_an_app_rotating_its_key_is_accepted_on_both_during_the_overlap(restricted_settings, monkeypatch):
    from app.clhear.app_auth import _authenticate

    monkeypatch.setenv("CLHEAR_APP_KEYS", "galaxy:new-secret-with-more-than-32-characters-xx,galaxy:old-short")
    get_settings.cache_clear()
    for secret in ("new-secret-with-more-than-32-characters-xx", "old-short"):
        assert _authenticate(f"Bearer {secret}", "galaxy")["app_id"] == "galaxy"
    with pytest.raises(Exception):
        _authenticate("Bearer other", "galaxy")
