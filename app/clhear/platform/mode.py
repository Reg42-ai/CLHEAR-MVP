"""Open by mode, not by layer (HLD v2 I9).

Three modes decide what a request may read:

* ``agnostic`` — the public product. L1 pointers/hashes (verbatim where rights
  allow) through L6 and base priorities are open; the re-derivation engine,
  L8 fills and member benchmarks, instance overlays, Reg42 OS and Solon are closed.
* ``member`` — a signed-in member (``l8_benchmarks.members``) or an application key
  issued to a member: adds L8 fills and benchmark aggregates.
* ``instance`` — the deployment runs inside a client account (``CLHEAR_MODE=instance``):
  adds the Actual overlay, gap diffs and instance priorities (item 18).

Public metadata about closed content (a fill *exists* and is *endorsed*) is open
in every mode; the content is not.
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import HTTPException, Request
from sqlalchemy.engine import Connection, Engine

from app.clhear.settings import get_settings

MODES = ("agnostic", "member", "instance")
CLOSED_IN_AGNOSTIC = ("L8 fills", "member benchmarks", "re-derivation engine", "instance overlay", "Reg42 OS", "Solon")


def deployment_mode() -> str:
    mode = (getattr(get_settings(), "clhear_mode", "") or "agnostic").lower()
    return mode if mode in MODES else "agnostic"


def is_instance() -> bool:
    return deployment_mode() == "instance"


# --------------------------------------------------------------------------- membership


def is_member(conn: Connection, *, email: str | None = None, user_id: str | None = None) -> bool:
    from app.clhear.l8.models import members

    if not email and not user_id:
        return False
    q = sa.select(members.c.id).where(members.c.valid_to.is_(None))
    if email and user_id:
        q = q.where(sa.or_(members.c.email == email.lower(), members.c.user_id == str(user_id)))
    elif email:
        q = q.where(members.c.email == email.lower())
    else:
        q = q.where(members.c.user_id == str(user_id))
    return conn.execute(q.limit(1)).first() is not None


def grant_membership(engine: Engine, *, email: str, granted_by: str, org_label: str = "", plan: str = "member") -> dict:
    from app.clhear.community_writes import user_id_for
    from app.clhear.l8.models import members

    email = email.strip().lower()
    with engine.begin() as conn:
        live = conn.execute(sa.select(members).where(members.c.email == email, members.c.valid_to.is_(None))).mappings().first()
        if live:
            return _plain(live)
        conn.execute(members.insert().values(email=email, user_id=user_id_for(email), org_label=org_label[:120], plan=plan,
                                             granted_by=granted_by))
        live = conn.execute(sa.select(members).where(members.c.email == email, members.c.valid_to.is_(None))).mappings().first()
    return _plain(live)


def revoke_membership(engine: Engine, *, email: str) -> int:
    from app.clhear.l8.models import members

    with engine.begin() as conn:
        return conn.execute(members.update().where(members.c.email == email.strip().lower(), members.c.valid_to.is_(None))
                            .values(valid_to=datetime.now(timezone.utc))).rowcount


def list_members(engine: Engine) -> list[dict]:
    from app.clhear.l8.models import members

    with engine.connect() as conn:
        return [_plain(r) for r in conn.execute(sa.select(members).where(members.c.valid_to.is_(None)).order_by(members.c.granted_at)).mappings()]


def _plain(row) -> dict:
    out = {}
    for k, v in dict(row).items():
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


# --------------------------------------------------------------------------- per-request mode


def request_identity(request: Request) -> dict | None:
    """Session / Cognito user, else the application key's owner, else a maintainer header."""
    from app.clhear.accounts import current_user

    user = current_user(request)
    if user:
        return {"email": user["email"], "user_id": user.get("id"), "via": "session"}
    app_id = request.headers.get("x-app-id", "")
    auth = request.headers.get("authorization", "")
    if app_id and auth.lower().startswith("bearer "):
        from app.clhear.api_keys import verify
        from app.clhear.db import get_engine

        app = verify(get_engine(), app_id, auth[7:].strip())
        if app:
            return {"email": None, "user_id": app["user_id"], "via": "app_key", "app_id": app_id}
    header = request.headers.get("x-reg42-user", "")
    if header and header in get_settings().maintainer_set:
        return {"email": header.lower(), "user_id": None, "via": "header", "maintainer": True}
    return None


def request_mode(request: Request) -> dict:
    """{mode, identity, closed} for this request."""
    if is_instance():
        return {"mode": "instance", "identity": request_identity(request), "closed": []}
    identity = request_identity(request)
    if identity:
        from app.clhear.db import get_engine

        with get_engine().connect() as conn:
            if identity.get("maintainer") or is_member(conn, email=identity.get("email"), user_id=identity.get("user_id")):
                return {"mode": "member", "identity": identity, "closed": ["instance overlay", "Reg42 OS", "Solon"]}
    return {"mode": "agnostic", "identity": identity, "closed": list(CLOSED_IN_AGNOSTIC)}


def require_member(request: Request) -> dict:
    """FastAPI dependency: member or instance mode, else 403 with the public metadata hint."""
    ctx = request_mode(request)
    if ctx["mode"] == "agnostic":
        raise HTTPException(status_code=403, detail={
            "code": "members_only",
            "message": "L8 fills and benchmarks are member content (HLD v2 I9). Fill existence and maturity are public at /l8/availability.",
            "mode": "agnostic", "signed_in": ctx["identity"] is not None,
        })
    return ctx
