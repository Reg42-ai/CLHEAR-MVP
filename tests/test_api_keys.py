"""HLD v2 §5 "Build on it" + CLHEAR-0.30 identity: one-click API keys for
signed-in users (hashed, revocable, never deleted), accepted by the consumer
API next to the statically configured app keys; Cognito id-token verification;
SDK stubs and the public evals summary."""
from __future__ import annotations

import time
import uuid

import sqlalchemy as sa

from app.clhear import api_keys
from app.clhear.accounts import SESSION_COOKIE, session_token
from app.clhear.community_models import api_keys as api_keys_t

from app.clhear.community_writes import user_id_for

USER = {"id": user_id_for("dev@example.org"), "email": "dev@example.org", "display_name": "Dev"}


def _sign_in(client, user=USER):
    client.cookies.set(SESSION_COOKIE, session_token(user))


def test_issue_list_use_and_revoke_a_key(engine, client):
    assert client.get("/keys").status_code == 401
    assert client.post("/keys", json={"label": "ci"}).status_code == 401
    _sign_in(client)
    r = client.post("/keys", json={"label": "ci"})
    assert r.status_code == 201, r.text
    key = r.json()
    assert key["id"] == "KEY-000001" and key["app_id"].startswith("key-") and key["secret"].startswith("clh_")
    assert key["prefix"] == key["secret"][:8] and key["active"] is True
    assert key["scopes"] == list(api_keys.DEFAULT_SCOPES)
    # the secret is shown once: it is never stored or listed again
    listed = client.get("/keys").json()["keys"]
    assert len(listed) == 1 and "secret" not in listed[0] and listed[0]["label"] == "ci"
    with engine.connect() as conn:
        row = conn.execute(sa.select(api_keys_t)).mappings().one()
    assert row["secret_hash"] != key["secret"] and len(row["secret_hash"]) == 64

    # the key works on the consumer API exactly like a configured app key
    client.cookies.clear()
    headers = {"X-App-Id": key["app_id"], "Authorization": f"Bearer {key['secret']}"}
    assert client.get("/v1/layers", headers=headers).status_code == 200
    assert client.get("/v1/layers", headers={**headers, "Authorization": "Bearer clh_wrong"}).status_code == 401
    assert client.get("/v1/layers", headers={"X-App-Id": "key-unknown", "Authorization": "Bearer x"}).status_code == 401
    assert client.get("/v1/layers", headers={"X-App-Id": "os-dev", "Authorization": "Bearer dev-os-key"}).status_code == 200  # static keys still work
    assert client.get("/keys", headers=headers).status_code == 401  # a key is not a session
    assert api_keys.list_for(engine, USER["id"])[0]["last_used_at"]

    # revoke: the row stays (I2), the key stops working, the id is never reused
    _sign_in(client)
    assert client.post("/keys/KEY-999999/revoke").status_code == 404
    revoked = client.post(f"/keys/{key['id']}/revoke").json()
    assert revoked["active"] is False and revoked["revoked_at"]
    client.cookies.clear()
    assert client.get("/v1/layers", headers=headers).status_code == 401
    _sign_in(client)
    second = client.post("/keys", json={}).json()
    assert second["id"] == "KEY-000002" and second["label"] == "default"
    assert [k["active"] for k in client.get("/keys").json()["keys"]] == [False, True]
    # another user cannot revoke it
    _sign_in(client, {"id": str(uuid.uuid4()), "email": "other@example.org", "display_name": "O"})
    assert client.post(f"/keys/{second['id']}/revoke").status_code == 404


def test_active_key_cap_and_scope_filtering(engine, client):
    _sign_in(client)
    for _ in range(api_keys.MAX_ACTIVE_KEYS):
        assert client.post("/keys", json={"scopes": ["read:l1", "write:everything"]}).status_code == 201
    assert client.post("/keys", json={}).status_code == 409
    keys = client.get("/keys").json()["keys"]
    assert all(k["scopes"] == ["read:l1"] for k in keys)  # unknown scopes are dropped, never granted
    app = api_keys.verify(engine, keys[0]["app_id"], "clh_nope")
    assert app is None


def test_build_overview_sdks_and_public_evals(client):
    ov = client.get("/build/overview").json()
    assert ov["signed_in"] is False and ov["max_active_keys"] == api_keys.MAX_ACTIVE_KEYS
    assert {r["path"] for r in ov["open_without_key"]} >= {"/solon/build", "/explore/node/{id}", "/watch/feed", "/evals/summary"}
    assert {s["language"] for s in ov["sdks"]} == {"python", "typescript"}
    py = next(s for s in ov["sdks"] if s["language"] == "python")
    assert "clhear/__init__.py" in py["files"] and "pyproject.toml" in py["files"]
    ts = next(s for s in ov["sdks"] if s["language"] == "typescript")
    assert "src/index.ts" in ts["files"] and "package.json" in ts["files"]
    assert client.get("/build").status_code == 200 and client.get("/build/sdks").status_code == 200
    ev = client.get("/evals/summary").json()
    assert set(ev["layers"]) >= {"L1", "L2", "L6"} and ev["layers"]["L1"]["thresholds"]["byte_fidelity"] == "100%"
    for layer in ev["layers"].values():
        assert {"passed", "thresholds", "suites", "missing"} <= set(layer)
    page = client.get("/evals")
    assert page.status_code == 200 and "Evals gate publication." in page.text


def test_python_sdk_stub_speaks_the_api(engine, client, monkeypatch):
    import importlib.util
    import json
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("clhear_sdk", Path(api_keys.SDK_DIR) / "python" / "clhear" / "__init__.py")
    sdk = importlib.util.module_from_spec(spec)
    sys.modules["clhear_sdk"] = sdk
    spec.loader.exec_module(sdk)

    # route the SDK's urllib calls through the test client
    def fake_request(self, method, path, *, params=None, body=None):
        r = client.request(method, path, params={k: v for k, v in (params or {}).items() if v is not None}, json=body)
        if r.status_code >= 400:
            raise sdk.ClhearError(r.status_code, r.json().get("detail"))
        return r.json()

    monkeypatch.setattr(sdk.Client, "_request", fake_request)
    c = sdk.Client("http://testserver")
    out = c.build("A UK retail equities broker holding client money for retail clients.")
    assert out["ok"] and out["blueprint_id"].startswith("BLU-")
    assert c.node(out["blueprint_id"])["kind"] == "blueprint"
    assert "system-security-plan" in c.oscal_ssp(out["blueprint_id"])
    assert c.evals()["layers"]["L6"]["thresholds"]["completeness"] == "100%"
    assert c.feed()["count"] >= 0
    try:
        c.build("Nonsense with no jurisdiction", accept_suggestions=False)
    except sdk.ClhearError as exc:
        assert exc.status == 422 and exc.detail["attribute"] == "jurisdictions"
    else:
        raise AssertionError("questions must surface when suggestions are not accepted")


# --------------------------------------------------------------------------- Cognito (CLHEAR-0.30)


def _rsa_pair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return pem, pub


def test_cognito_id_tokens_are_verified_and_become_the_same_user(engine, client, monkeypatch):
    import jwt

    from app.clhear import app_auth
    from app.clhear.settings import get_settings

    monkeypatch.setenv("CLHEAR_COGNITO_REGION", "eu-west-2")
    monkeypatch.setenv("CLHEAR_COGNITO_USER_POOL_ID", "eu-west-2_TESTPOOL")
    monkeypatch.setenv("CLHEAR_COGNITO_CLIENT_ID", "client-abc")
    get_settings.cache_clear()
    assert app_auth.cognito_enabled()
    assert app_auth.cognito_issuer() == "https://cognito-idp.eu-west-2.amazonaws.com/eu-west-2_TESTPOOL"
    assert app_auth.cognito_jwks_url().endswith("/.well-known/jwks.json")

    priv, pub = _rsa_pair()
    _, other_pub = _rsa_pair()
    now = int(time.time())
    claims = {"sub": "sub-1", "email": "dev@example.org", "name": "Dev", "token_use": "id", "aud": "client-abc",
              "iss": app_auth.cognito_issuer(), "iat": now, "exp": now + 600}
    token = jwt.encode(claims, priv, algorithm="RS256", headers={"kid": "k1"})
    assert app_auth.verify_cognito_token(token, signing_key=pub)["email"] == "dev@example.org"
    # wrong key, wrong audience, wrong issuer, expired, access token, no email → None
    assert app_auth.verify_cognito_token(token, signing_key=other_pub) is None
    assert app_auth.verify_cognito_token(jwt.encode({**claims, "aud": "someone-else"}, priv, algorithm="RS256"), signing_key=pub) is None
    assert app_auth.verify_cognito_token(jwt.encode({**claims, "iss": "https://evil.example"}, priv, algorithm="RS256"), signing_key=pub) is None
    assert app_auth.verify_cognito_token(jwt.encode({**claims, "exp": now - 1}, priv, algorithm="RS256"), signing_key=pub) is None
    assert app_auth.verify_cognito_token(jwt.encode({**claims, "token_use": "access"}, priv, algorithm="RS256"), signing_key=pub) is None
    assert app_auth.verify_cognito_token(jwt.encode({k: v for k, v in claims.items() if k != "email"}, priv, algorithm="RS256"), signing_key=pub) is None
    assert app_auth.verify_cognito_token("not-a-jwt", signing_key=pub) is None

    # through the app: the bearer id token resolves to the same deterministic user id as a magic-link login
    class FakeJwks:
        def get_signing_key_from_jwt(self, _token):
            class K:
                key = pub
            return K()

    monkeypatch.setattr(app_auth, "_jwks_client", lambda: FakeJwks())
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["user"]["email"] == "dev@example.org" and me["user"]["id"] == USER["id"] and me["user"]["provider"] == "cognito"
    assert me["providers"]["cognito"] is False  # hosted UI needs the domain too
    r = client.post("/keys", json={"label": "from cognito"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201 and api_keys.list_for(engine, USER["id"])[0]["label"] == "from cognito"
    assert client.get("/auth/me", headers={"Authorization": "Bearer a.b.c"}).json()["user"] is None
    assert client.get("/auth/cognito").status_code == 503  # no hosted-UI domain configured → honest 503
    get_settings.cache_clear()
