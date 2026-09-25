"""App-key auth for the consumer API (OS, Safeluance, later products).

Keys are configured as CLHEAR_APP_KEYS=app_id:secret,app_id:secret.
Each key is granted read:l1 today; extra scopes can be appended as
app_id:secret:read:l1+read:l2 when later layers ship.
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from app.clhear.settings import get_settings


def parse_app_keys(raw: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        bits = part.split(":")
        app_id, secret = bits[0], bits[1]
        scopes = {"read:l1"}
        if len(bits) > 2 and bits[2]:
            # Remainder is scope list (read:l1+read:l2). Do not split scopes on ':'.
            scope_raw = ":".join(bits[2:])
            scopes = {s.strip() for s in scope_raw.replace("+", ",").split(",") if s.strip()}
        out[app_id] = {"secret": secret, "scopes": scopes}
    return out


def require_app(
    authorization: str | None = Header(default=None),
    x_app_id: str | None = Header(default=None, alias="X-App-Id"),
) -> dict:
    settings = get_settings()
    keys = parse_app_keys(settings.clhear_app_keys)
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if x_app_id and x_app_id.startswith("key-"):
        # self-issued key (HLD v2 §5 "Build on it"): looked up in community.api_keys
        from app.clhear import api_keys, identity

        app = api_keys.verify(identity.engine(), x_app_id, token)
        if app is None:
            raise HTTPException(status_code=401, detail="Unknown, revoked or invalid API key")
        return app
    if not keys:
        raise HTTPException(status_code=503, detail="CLHEAR_APP_KEYS is not configured")
    if not x_app_id or x_app_id not in keys:
        raise HTTPException(status_code=401, detail="Unknown or missing X-App-Id")
    expected = keys[x_app_id]["secret"]
    if not token or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    return {"app_id": x_app_id, "scopes": sorted(keys[x_app_id]["scopes"])}


def require_scope(app: dict, scope: str) -> None:
    if scope not in app.get("scopes", []):
        raise HTTPException(status_code=403, detail=f"App is not granted {scope}")


# --------------------------------------------------------------- Cognito (HLD v2 §5)
#
# Public users sign in through a Cognito user pool (Google IdP for Reg42,
# infra/cognito.tf). The pool's RS256 id tokens are verified here against its
# JWKS; ``accounts`` then mints the ordinary session cookie so the rest of the
# app sees one identity model. Nothing here needs a secret.

_jwks_clients: dict[str, object] = {}


def cognito_enabled() -> bool:
    s = get_settings()
    return bool(s.clhear_cognito_region and s.clhear_cognito_user_pool_id and s.clhear_cognito_client_id)


def cognito_issuer() -> str:
    s = get_settings()
    return f"https://cognito-idp.{s.clhear_cognito_region}.amazonaws.com/{s.clhear_cognito_user_pool_id}"


def cognito_jwks_url() -> str:
    return f"{cognito_issuer()}/.well-known/jwks.json"


def _jwks_client():
    import jwt

    url = cognito_jwks_url()
    client = _jwks_clients.get(url)
    if client is None:
        client = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)
        _jwks_clients[url] = client
    return client


def verify_cognito_token(token: str, *, signing_key=None) -> dict | None:
    """Claims of a valid Cognito id token for our pool + client, else None.

    ``signing_key`` lets tests supply the public key instead of fetching JWKS.
    """
    if not token or token.count(".") != 2 or not cognito_enabled():
        return None
    import jwt

    try:
        key = signing_key if signing_key is not None else _jwks_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"], issuer=cognito_issuer(),
            audience=get_settings().clhear_cognito_client_id,
            options={"require": ["exp", "iat", "sub", "iss"]},
        )
    except Exception:
        return None
    if claims.get("token_use") not in (None, "id"):
        return None
    if not claims.get("email"):
        return None
    return claims
