"""Contributor accounts: email magic-link (SES), Google OAuth, Apple stub.

Sessions are stateless signed cookies (HMAC, CLHEAR_SESSION_SECRET). The
audience is compliance officers, lawyers, PMs and developers — so no GitHub
requirement: plain email works everywhere; Google/Apple activate when their
client credentials are configured (see docs/MANUAL.md).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.engine import Engine

from app.clhear.community_models import users
from app.clhear.db import get_engine
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.accounts")

router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_COOKIE = "clhear_session"
SESSION_TTL_S = 30 * 24 * 3600
MAGIC_TTL_S = 15 * 60


# ------------------------------------------------------------ token plumbing


def _sign(payload: dict, purpose: str) -> str:
    if get_settings().clhear_preview_mode:
        purpose = f"preview:{purpose}"
    secret = get_settings().clhear_session_secret.encode()
    body = base64.urlsafe_b64encode(json.dumps({**payload, "_p": purpose}).encode()).rstrip(b"=")
    mac = hmac.new(secret, body, hashlib.sha256).hexdigest()[:32]
    return f"{body.decode()}.{mac}"


def _verify(token: str, purpose: str) -> dict | None:
    if get_settings().clhear_preview_mode:
        purpose = f"preview:{purpose}"
    try:
        body, mac = token.rsplit(".", 1)
        secret = get_settings().clhear_session_secret.encode()
        expected = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(mac, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if payload.get("_p") != purpose or payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def session_token(user: dict) -> str:
    return _sign(
        {"uid": user["id"], "email": user["email"], "name": user["display_name"], "exp": time.time() + SESSION_TTL_S},
        "session",
    )


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    payload = _verify(token, "session")
    if payload:
        return {"id": payload["uid"], "email": payload["email"], "display_name": payload.get("name", "")}
    # HLD v2 §5: a Cognito id token in the Authorization header is the same
    # user — SDKs and SPAs can call without the cookie.
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and auth.count(".") == 2:
        from app.clhear.app_auth import verify_cognito_token

        claims = verify_cognito_token(auth[7:].strip())
        if claims:
            from app.clhear import community_writes

            email = claims["email"].strip().lower()
            return {"id": community_writes.user_id_for(email), "email": email,
                    "display_name": claims.get("name") or email.split("@")[0], "provider": "cognito",
                    "email_verified": claims.get("email_verified") is True}
    return None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to contribute")
    return user


# --------------------------------------------------------------- user store


def upsert_user(engine: Engine, email: str, display_name: str = "", provider: str = "email", provider_sub: str = "") -> dict:
    """Session identity is deterministic (uuid5 of email) so it is valid
    immediately even when the user row travels through the write queue."""
    from app.clhear import community_writes

    email = email.strip().lower()
    identity = {"id": community_writes.user_id_for(email), "email": email,
                "display_name": display_name or email.split("@")[0]}
    if get_settings().clhear_preview_mode:
        return identity  # Stateless verified sign-in must not enqueue an account write.
    try:
        community_writes.dispatch(
            engine,
            {"op": "upsert_user", "email": email, "display_name": display_name,
             "provider": provider, "provider_sub": provider_sub},
        )
    except Exception:
        log.exception("user upsert dispatch failed (login still proceeds)")
    return identity


def _set_session(response, user: dict):
    from app.clhear.preview import local_preview

    response.set_cookie(
        SESSION_COOKIE, session_token(user), max_age=SESSION_TTL_S,
        httponly=True, samesite="lax", secure=not (get_settings().clhear_auth_debug or local_preview()),
    )
    return response


# ------------------------------------------------------------- email links


@router.post("/email")
async def email_magic_link(request: Request) -> dict:
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    if "@" not in email or len(email) > 200:
        raise HTTPException(status_code=400, detail="a valid email is required")
    if body.get("website"):  # honeypot field: bots fill it, humans never see it
        return {"sent": True}
    settings = get_settings()
    if settings.clhear_restricted_access and email not in settings.reviewer_set:
        # Do not send mail to users this restricted deployment cannot admit.
        raise HTTPException(status_code=403, detail="This workspace is limited to approved reviewers")
    token = _sign({"email": email, "exp": time.time() + MAGIC_TTL_S}, "magic")
    link = f"{settings.clhear_public_base_url}/auth/email/verify?token={token}"
    if settings.clhear_auth_debug:
        return {"sent": False, "debug_link": link}  # dev: no SES in the loop
    sent = False
    try:
        import boto3

        boto3.client("sesv2", region_name=settings.aws_region).send_email(
            FromEmailAddress=settings.clhear_ses_sender,
            Destination={"ToAddresses": [email]},
            Content={
                "Simple": {
                    "Subject": {"Data": "Your CLHEAR sign-in link"},
                    "Body": {
                        "Text": {
                            "Data": "Sign in to CLHEAR — the open compliance stack:\n\n"
                            f"{link}\n\nThis link expires in 15 minutes. If you didn't request it, ignore this email."
                        }
                    },
                }
            },
        )
        sent = True
    except Exception:
        log.exception("SES send failed for %s", email)
    out: dict = {"sent": sent}
    if settings.clhear_auth_debug and not sent:
        out["debug_link"] = link  # dev only: no SES in the loop
    if not sent and not settings.clhear_auth_debug:
        raise HTTPException(status_code=502, detail="could not send the sign-in email; try again or use Google")
    return out


@router.get("/email/verify")
def email_verify(token: str):
    payload = _verify(token, "magic")
    if payload is None:
        raise HTTPException(status_code=400, detail="This sign-in link is invalid or expired")
    user = upsert_user(get_engine(), payload["email"], provider="email")
    return _set_session(RedirectResponse("/" if get_settings().clhear_restricted_access else "/stack#/contribute"), user)


# ------------------------------------------------------------ Google OAuth


@router.get("/google")
def google_start(request: Request):
    settings = get_settings()
    if not settings.google_oauth_client_id:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured yet — use email")
    from urllib.parse import urlencode

    params = urlencode(
        {
            "client_id": settings.google_oauth_client_id,
            "redirect_uri": f"{settings.clhear_public_base_url}/auth/google/callback",
            "response_type": "code",
            "scope": "openid email profile",
            "state": _sign({"exp": time.time() + 600}, "oauth-state"),
            "prompt": "select_account",
        }
    )
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@router.get("/google/callback")
def google_callback(code: str = "", state: str = ""):
    settings = get_settings()
    if not settings.google_oauth_client_id:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    if _verify(state, "oauth-state") is None:
        raise HTTPException(status_code=400, detail="invalid oauth state")
    import httpx

    token_resp = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "redirect_uri": f"{settings.clhear_public_base_url}/auth/google/callback",
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    token_resp.raise_for_status()
    access = token_resp.json()["access_token"]
    info = httpx.get(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {access}"},
        timeout=20,
    ).json()
    if settings.clhear_restricted_access and (info.get("email_verified") is not True or
                                             info.get("email", "").lower() not in settings.reviewer_set):
        raise HTTPException(status_code=403, detail="An approved, verified reviewer account is required")
    user = upsert_user(
        get_engine(), info["email"], display_name=info.get("name", ""), provider="google", provider_sub=info.get("sub", "")
    )
    return _set_session(RedirectResponse("/" if settings.clhear_restricted_access else "/stack#/contribute"), user)


# ------------------------------------------------------------ Cognito hosted UI (HLD v2 §5)


def _cognito_redirect_uri() -> str:
    return f"{get_settings().clhear_public_base_url}/auth/cognito/callback"


@router.get("/cognito")
def cognito_start(provider: str = ""):
    """Redirect to the pool's hosted UI (``provider=Google`` goes straight to Google)."""
    from urllib.parse import urlencode

    from app.clhear.app_auth import cognito_enabled

    settings = get_settings()
    if not cognito_enabled() or not settings.clhear_cognito_domain:
        raise HTTPException(status_code=503, detail="Cognito sign-in is not configured — use email or Google")
    params = {
        "client_id": settings.clhear_cognito_client_id,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": _cognito_redirect_uri(),
        "state": _sign({"exp": time.time() + 600}, "oauth-state"),
    }
    if provider:
        params["identity_provider"] = provider
    return RedirectResponse(f"{settings.clhear_cognito_domain.rstrip('/')}/oauth2/authorize?{urlencode(params)}")


def saml_provider_for(email: str) -> str | None:
    """Cognito SAML provider name for an email's domain, or None when the domain has no
    enterprise IdP (the user then signs in with email / Google like everyone else)."""
    domain = email.rsplit("@", 1)[-1].strip().lower() if "@" in email else email.strip().lower()
    if not domain:
        return None
    mapping = get_settings().saml_domain_map
    # exact domain first, then parent domains (sub.acme.example -> acme.example)
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        hit = mapping.get(".".join(parts[i:]))
        if hit:
            return hit
    return None


@router.get("/sso")
def sso_start(email: str = ""):
    """Enterprise SSO (HLD v2 §7.1): route to the SAML IdP federated for the email's
    domain. 404 when the domain has none, so the sign-in form can fall back cleanly."""
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="email is required")
    provider = saml_provider_for(email)
    if provider is None:
        raise HTTPException(status_code=404, detail="no enterprise SSO for this domain — sign in with email or Google")
    return cognito_start(provider=provider)


@router.get("/sso/domains")
def sso_domains() -> dict:
    """Which domains have enterprise SSO (public: the sign-in form uses it to offer the
    SSO button before the user types a password they do not have)."""
    return {"providers": sorted({v for v in get_settings().saml_domain_map.values()}),
            "domains": sorted(get_settings().saml_domain_map)}


@router.get("/cognito/callback")
def cognito_callback(code: str = "", state: str = ""):
    from app.clhear.app_auth import cognito_enabled, verify_cognito_token

    settings = get_settings()
    if not cognito_enabled() or not settings.clhear_cognito_domain:
        raise HTTPException(status_code=503, detail="Cognito sign-in is not configured")
    if _verify(state, "oauth-state") is None:
        raise HTTPException(status_code=400, detail="invalid oauth state")
    import httpx

    token_resp = httpx.post(
        f"{settings.clhear_cognito_domain.rstrip('/')}/oauth2/token",
        data={"grant_type": "authorization_code", "client_id": settings.clhear_cognito_client_id,
              "code": code, "redirect_uri": _cognito_redirect_uri()},
        timeout=20,
    )
    token_resp.raise_for_status()
    claims = verify_cognito_token(token_resp.json().get("id_token", ""))
    if claims is None:
        raise HTTPException(status_code=401, detail="Cognito token could not be verified")
    if settings.clhear_restricted_access and (claims.get("email_verified") is not True or
                                             claims.get("email", "").lower() not in settings.reviewer_set):
        raise HTTPException(status_code=403, detail="An approved, verified reviewer account is required")
    # federated users carry the IdP in `identities`; keep it so the audit log can tell SAML from Google
    provider = "cognito"
    for ident in claims.get("identities") or []:
        if isinstance(ident, dict) and ident.get("providerType") == "SAML":
            provider = f"saml:{ident.get('providerName', '')}"
        elif isinstance(ident, dict) and ident.get("providerName"):
            provider = f"cognito:{ident['providerName']}"
    user = upsert_user(get_engine(), claims["email"], display_name=claims.get("name", ""), provider=provider,
                       provider_sub=claims.get("sub", ""))
    return _set_session(RedirectResponse("/" if settings.clhear_restricted_access else "/stack#/contribute"), user)


@router.get("/apple")
def apple_start():
    settings = get_settings()
    if not settings.apple_oauth_client_id:
        raise HTTPException(
            status_code=503,
            detail="Apple sign-in is not configured yet (requires an Apple Developer Service ID) — use email or Google",
        )
    # ARCH: Sign in with Apple lands once the Service ID + key exist; the
    # provider contract mirrors google_start/google_callback.
    raise HTTPException(status_code=503, detail="Apple sign-in is not enabled yet")


# ------------------------------------------------------------------ session


@router.get("/me")
def me(request: Request) -> dict:
    user = current_user(request)
    settings = get_settings()
    return {
        "user": user,
        "providers": {
            "email": not settings.clhear_preview_mode,
            "google": bool(settings.google_oauth_client_id),
            "apple": bool(settings.apple_oauth_client_id),
            "cognito": bool(settings.clhear_cognito_user_pool_id and settings.clhear_cognito_domain),
        },
    }


@router.post("/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response
