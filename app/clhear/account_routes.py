"""Self-service accounts: sign-up, profile and terms, own usage; maintainer controls."""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.clhear import identity, usage
from app.clhear.access import is_maintainer
from app.clhear.accounts import current_user, record_account, require_user
from app.clhear.identity_models import account_profiles
from app.clhear.settings import get_settings

router = APIRouter(tags=["accounts"])

SIGNUP_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CLHEAR · Create an account</title>
<link rel="stylesheet" href="/static/theme.css"><style>main{max-width:520px;margin:8vh auto;padding:28px}
label,input,textarea,button{display:block;margin:10px 0;width:100%}input,textarea{padding:10px}
button{width:auto;padding:12px 18px}.check{display:flex;gap:8px;align-items:flex-start}.check input{width:auto}</style></head>
<body><a href="#main" class="skip">Skip to content</a><main id="main"><h1>CLHEAR</h1><h2>Create a free account</h2>
<p>CLHEAR data is free to use in the web app and through the API. We ask who you are so we can see how it is used.</p>
<form id="signup"><label for="email">Work email</label><input id="email" type="email" autocomplete="email" required>
<label for="name">Name</label><input id="name" autocomplete="name" required>
<label for="organization">Organization</label><input id="organization" autocomplete="organization" required>
<label for="intended_use">How will you use CLHEAR?</label><textarea id="intended_use" rows="3" required></textarea>
<div class="check"><input id="accept" type="checkbox" required><label for="accept">I accept the
<a href="/terms">terms</a> (version __TERMS__) and the <a href="/disclaimer">disclaimer</a>.</label></div>
<input id="website" name="website" tabindex="-1" autocomplete="off" hidden>
<button type="submit">Send my sign-in link</button></form><p id="message" role="status"></p>
<p>Already have an account? <a href="/signin">Sign in</a></p></main>
<script>document.getElementById('signup').addEventListener('submit',async e=>{e.preventDefault();const v=id=>document.getElementById(id).value;
const m=document.getElementById('message');m.textContent='Sending…';try{const r=await fetch('/auth/email',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({email:v('email'),website:v('website'),signup:{name:v('name'),organization:v('organization'),intended_use:v('intended_use'),accept_terms:document.getElementById('accept').checked}})});
const d=await r.json().catch(()=>({}));m.textContent=r.ok?(d.detail||'Check your email for the sign-in link.'):(d.detail||'Unable to send the link. Please try again.');}
catch{m.textContent='Connection unavailable. Please try again.'}});</script></body></html>"""


@router.get("/signup", response_class=HTMLResponse, include_in_schema=False)
def signup_page() -> HTMLResponse:
    return HTMLResponse(SIGNUP_PAGE.replace("__TERMS__", get_settings().clhear_terms_version),
                        headers={"Cache-Control": "no-store"})


def profile_of(user_id: str) -> dict | None:
    with identity.engine().connect() as conn:
        row = conn.execute(sa.select(account_profiles).where(account_profiles.c.user_id == user_id)).mappings().first()
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(row).items()} if row else None


class ProfileBody(BaseModel):
    name: str = Field(max_length=120)
    organization: str = Field(max_length=160)
    intended_use: str = Field(max_length=600)
    accept_terms: bool


@router.get("/account/profile")
def my_profile(user: dict = Depends(require_user)) -> dict:
    return {"profile": profile_of(user["id"]), "terms_version": get_settings().clhear_terms_version}


@router.post("/account/profile")
def complete_profile(body: ProfileBody, user: dict = Depends(require_user)) -> dict:
    """Profile and terms for accounts that signed in through Google or an organization."""
    if not body.accept_terms:
        raise HTTPException(status_code=400, detail="Accept the terms to use CLHEAR")
    record_account(user, provider=(profile_of(user["id"]) or {}).get("provider", "email"), email_verified=True, signup={
        "name": body.name.strip(), "organization": body.organization.strip(), "intended_use": body.intended_use.strip(),
        "terms_version": get_settings().clhear_terms_version})
    return {"profile": profile_of(user["id"])}


@router.get("/account/usage")
def my_usage(days: int = 30, user: dict = Depends(require_user)) -> dict:
    return usage.usage_for(user["id"], days=max(1, min(days, 90)))


def require_maintainer(request: Request) -> dict:
    user = current_user(request)
    if not is_maintainer(user):
        raise HTTPException(status_code=403, detail="Maintainer access is required")
    return user


@router.get("/admin/usage")
def all_usage(days: int = 30, user: dict = Depends(require_maintainer)) -> dict:
    return usage.overview(days=max(1, min(days, 90)))


class SuspendBody(BaseModel):
    reason: str = Field(default="", max_length=300)


@router.post("/admin/accounts/{user_id}/suspend")
def suspend(user_id: str, body: SuspendBody, user: dict = Depends(require_maintainer)) -> dict:
    return _set_status(user_id, "suspended", body.reason, actor=user["email"])


@router.post("/admin/accounts/{user_id}/unsuspend")
def unsuspend(user_id: str, user: dict = Depends(require_maintainer)) -> dict:
    return _set_status(user_id, "active", "", actor=user["email"])


def _set_status(user_id: str, status: str, reason: str, *, actor: str) -> dict:
    with identity.engine().begin() as conn:
        updated = conn.execute(account_profiles.update().where(account_profiles.c.user_id == user_id).values(
            status=status, suspended_reason=reason, suspended_at=datetime.now(timezone.utc) if status == "suspended" else None))
        if not updated.rowcount:
            raise HTTPException(status_code=404, detail="Unknown account")
    usage.note_action(actor, "suspend" if status == "suspended" else "unsuspend", user_id, {"reason": reason})
    return {"user_id": user_id, "status": status}


@router.post("/admin/keys/{key_id}/revoke")
def revoke_any_key(key_id: str, user: dict = Depends(require_maintainer)) -> dict:
    from app.clhear.community_models import api_keys

    with identity.engine().begin() as conn:
        row = conn.execute(sa.select(api_keys.c.user_id, api_keys.c.revoked_at).where(api_keys.c.id == key_id)).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Unknown key")
        if row.revoked_at is None:
            conn.execute(api_keys.update().where(api_keys.c.id == key_id).values(revoked_at=datetime.now(timezone.utc)))
    usage.note_action(user["email"], "revoke_key", key_id)
    return {"key_id": key_id, "revoked": True}
