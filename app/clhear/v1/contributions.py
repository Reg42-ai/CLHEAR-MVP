"""Community contribution API + page (HLD v2 §6, invariant I12).

    GET  /contribute                                   the Contribute page
    GET  /contributions/kinds                          kinds, required fields, statuses, roles, flow constants
    GET  /cla                                          the CLA text + hash; whether the caller signed
    POST /cla/sign                                     sign the current CLA (grants the contributor role)
    POST /contributions                                file a contribution (CLA required; automated checks)
    GET  /contributions?status=&kind=&contributor=&layer=
    GET  /contributions/summary                        counts by status, contributors, reviewers
    GET  /contributions/notifications?unread=          the caller's notifications
    POST /contributions/notifications/read             mark them read
    GET  /contributions/{id}                           one contribution: checks, fleet verdict, reviews, impact
    POST /contributions/{id}/rederive                  ask the fleet for its verdict now (reviewers)
    POST /contributions/{id}/reviews                   one reviewer's decision (two accepts apply; one reject closes)
    GET  /contributors                                 leaderboard (handles only)
    GET  /contributors/{email}                         a contributor's profile
    GET  /roles  · POST /roles · DELETE /roles         reviewer / maintainer grants (maintainers)
    GET  /governance · GET /governance/{doc}           charter, CLA, CoC, release / deprecation / CoI / voting / WG policies
    GET  /roadmap                                      the public roadmap
    POST /newsletter/subscribe                         beehiiv subscription (inert when unconfigured)

Identity: the session cookie / Cognito bearer (accounts.current_user) or, for the
maintainers' tooling, ``X-Reg42-User`` ∈ CLHEAR_MAINTAINERS. Contributions never
write a layer table directly — acceptance goes through the record path.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app.clhear.accounts import current_user
from app.clhear.community_models import CONTRIBUTION_KINDS, CONTRIBUTION_STATUSES, REVIEW_DECISIONS, ROLES
from app.clhear.db import get_engine
from app.clhear.platform import contributions as contrib
from app.clhear.platform import newsletter
from app.clhear.settings import get_settings

router = APIRouter(tags=["community"])
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
EXPORT_DIR = Path(__file__).resolve().parents[3] / "export" / "clhear"
GOVERNANCE_DOCS = {
    "charter": "governance/CHARTER.md", "cla": "governance/CLA.md", "code-of-conduct": "governance/CODE_OF_CONDUCT.md",
    "contributing": "governance/CONTRIBUTING.md", "release-policy": "governance/RELEASE_POLICY.md",
    "deprecation-policy": "governance/DEPRECATION_POLICY.md", "conflict-of-interest": "governance/CONFLICT_OF_INTEREST.md",
    "steering-voting": "governance/STEERING_VOTING.md", "working-groups": "governance/WORKING_GROUPS.md",
    "trademark-policy": "governance/TRADEMARK_POLICY.md", "licences": "LICENSES/README.md", "roadmap": "ROADMAP.md",
}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --------------------------------------------------------------------------- identity


def identity(request: Request, x_reg42_user: str | None = Header(default=None)) -> dict | None:
    """Session / bearer user, else a maintainer header identity, else None."""
    user = current_user(request)
    if user:
        return user
    if x_reg42_user and x_reg42_user in get_settings().maintainer_set:
        return {"id": None, "email": x_reg42_user.lower(), "display_name": x_reg42_user.split("@")[0], "via": "header"}
    return None


def require_identity(user: dict | None = Depends(identity)) -> dict:
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to contribute")
    return user


def _roles(email: str) -> set[str]:
    with get_engine().connect() as conn:
        return contrib.roles_for(conn, email)


def require_granting_role(user: dict = Depends(require_identity)) -> dict:
    if not (_roles(user["email"]) & contrib.GRANTING_ROLES):
        raise HTTPException(status_code=403, detail=f"{user['email']} lacks the maintainer role")
    return user


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, contrib.CLARequired):
        return HTTPException(status_code=403, detail={"code": "cla_required", "message": str(exc)})
    if isinstance(exc, contrib.NotAReviewer):
        return HTTPException(status_code=403, detail={"code": "not_a_reviewer", "message": str(exc)})
    if isinstance(exc, (contrib.SelfReview, contrib.WrongStatus, contrib.InvalidContribution, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=f"unknown {exc}")
    raise exc


# --------------------------------------------------------------------------- page + reference


@router.get("/contribute", response_class=HTMLResponse, include_in_schema=False)
def contribute_page() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "contribute.html").read_text(), headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/contributions/kinds")
def kinds() -> dict:
    return {
        "kinds": [{"kind": k, "layer": contrib.DEFAULT_LAYER[k], "required": list(contrib.REQUIRED_FIELDS[k])} for k in CONTRIBUTION_KINDS],
        "statuses": list(CONTRIBUTION_STATUSES), "decisions": list(REVIEW_DECISIONS), "roles": list(ROLES),
        "accepts_required": contrib.REQUIRED_ACCEPTS, "flow": contrib.FLOW_VERSION, "cla_version": contrib.CLA_VERSION,
        "channels": ["web", "api", "pr"], "discourse_url": get_settings().clhear_discourse_url or None,
        "newsletter": newsletter.configured(),
    }


# --------------------------------------------------------------------------- CLA


class SignCLA(BaseModel):
    acknowledge_patent_grant: bool = True
    display_name: str = ""


@router.get("/cla")
def get_cla(user: dict | None = Depends(identity)) -> dict:
    signed = None
    if user:
        with get_engine().connect() as conn:
            signed = contrib.cla_signed(conn, user["email"])
    return {"version": contrib.CLA_VERSION, "text_hash": contrib.cla_hash(), "text": contrib.cla_text(),
            "signed": signed, "user": user["email"] if user else None}


@router.post("/cla/sign")
def sign_cla(body: SignCLA, user: dict = Depends(require_identity)) -> dict:
    if not body.acknowledge_patent_grant:
        raise HTTPException(status_code=422, detail="the CLA's patent grant (§3) must be acknowledged")
    return contrib.sign_cla(get_engine(), user["email"], display_name=body.display_name or user.get("display_name", ""),
                            acknowledge_patent_grant=True)


# --------------------------------------------------------------------------- contributions


class NewContribution(BaseModel):
    kind: str
    proposed: dict = Field(default_factory=dict)
    target_ref: str = ""
    layer: str | None = None
    field: str = ""
    evidence: list[dict] = Field(default_factory=list)
    rationale: str = ""
    channel: str = "web"


@router.post("/contributions", status_code=201)
def create_contribution(body: NewContribution, user: dict = Depends(require_identity)) -> dict:
    try:
        return contrib.submit(get_engine(), contributor_email=user["email"], kind=body.kind, proposed=body.proposed,
                              target_ref=body.target_ref, layer=body.layer, field=body.field, evidence=body.evidence,
                              rationale=body.rationale, channel=body.channel if body.channel in ("web", "api", "pr") else "web",
                              display_name=user.get("display_name", ""))
    except Exception as exc:  # noqa: BLE001 — translated to HTTP below
        raise _translate(exc) from exc


@router.get("/contributions")
def list_contributions(status: str | None = Query(default=None), kind: str | None = Query(default=None),
                       contributor: str | None = Query(default=None), layer: str | None = Query(default=None),
                       limit: int = Query(default=100, ge=1, le=500)) -> dict:
    items = contrib.list_contributions(get_engine(), status=status, kind=kind, contributor=contributor, layer=layer, limit=limit)
    return {"count": len(items), "items": items}


@router.get("/contributions/summary")
def contributions_summary() -> dict:
    return contrib.summary(get_engine())


@router.get("/contributions/notifications")
def my_notifications(unread: bool = Query(default=False), user: dict = Depends(require_identity)) -> dict:
    items = contrib.notifications(get_engine(), user["email"], unread_only=unread)
    return {"count": len(items), "unread": sum(1 for n in items if not n.get("read_at")), "items": items}


@router.post("/contributions/notifications/read")
def read_notifications(user: dict = Depends(require_identity)) -> dict:
    return {"marked": contrib.mark_read(get_engine(), user["email"])}


@router.get("/contributions/{contribution_id}")
def get_contribution(contribution_id: str) -> dict:
    row = contrib.get(get_engine(), contribution_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown contribution {contribution_id}")
    return row


@router.post("/contributions/{contribution_id}/rederive")
def rederive_contribution(contribution_id: str, user: dict = Depends(require_identity)) -> dict:
    if not (_roles(user["email"]) & contrib.REVIEWER_ROLES):
        raise HTTPException(status_code=403, detail={"code": "not_a_reviewer", "message": f"{user['email']} is not a reviewer"})
    try:
        verdict = contrib.rederive(get_engine(), contribution_id)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return {"contribution_id": contribution_id, "rederivation": verdict}


class Review(BaseModel):
    decision: str
    note: str = ""


@router.post("/contributions/{contribution_id}/reviews")
def review_contribution(contribution_id: str, body: Review, user: dict = Depends(require_identity)) -> dict:
    try:
        return contrib.review(get_engine(), contribution_id, reviewer_email=user["email"], decision=body.decision, note=body.note)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


# --------------------------------------------------------------------------- contributors + roles


@router.get("/contributors")
def contributors(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    items = contrib.leaderboard(get_engine(), limit=limit)
    return {"count": len(items), "items": items, "score": "accepted×10 + released×5 + blueprints changed×2 + obligations changed"}


@router.get("/contributors/me")
def my_profile(user: dict = Depends(require_identity)) -> dict:
    return contrib.profile(get_engine(), user["email"])


@router.get("/contributors/{email}")
def contributor_profile(email: str) -> dict:
    return contrib.profile(get_engine(), email)


class RoleGrant(BaseModel):
    email: str
    role: str
    note: str = ""


@router.get("/roles")
def roles(role: str | None = Query(default=None)) -> dict:
    items = contrib.role_holders(get_engine(), role)
    return {"count": len(items), "items": items, "roles": list(ROLES)}


@router.post("/roles", status_code=201)
def grant(body: RoleGrant, user: dict = Depends(require_granting_role)) -> dict:
    try:
        return contrib.grant_role(get_engine(), email=body.email, role=body.role, granted_by=user["email"], note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/roles")
def revoke(email: str = Query(...), role: str = Query(...), user: dict = Depends(require_granting_role)) -> dict:
    return {"revoked": contrib.revoke_role(get_engine(), email=email, role=role, revoked_by=user["email"])}


# --------------------------------------------------------------------------- governance + roadmap + newsletter


@router.get("/governance")
def governance() -> dict:
    docs = []
    for slug, rel in GOVERNANCE_DOCS.items():
        path = EXPORT_DIR / rel
        if not path.exists():
            continue
        first = next((ln.lstrip("# ").strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("#")), slug)
        docs.append({"slug": slug, "title": first, "path": rel, "href": f"/governance/{slug}"})
    return {"count": len(docs), "docs": docs, "discourse_url": get_settings().clhear_discourse_url or None,
            "public_repo": get_settings().clhear_public_repo_url or None}


@router.get("/governance/{slug}", response_class=PlainTextResponse)
def governance_doc(slug: str) -> PlainTextResponse:
    rel = GOVERNANCE_DOCS.get(slug)
    if not rel or not (EXPORT_DIR / rel).exists():
        raise HTTPException(status_code=404, detail=f"unknown governance document {slug}")
    return PlainTextResponse((EXPORT_DIR / rel).read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")


@router.get("/roadmap", response_class=PlainTextResponse)
def roadmap() -> PlainTextResponse:
    return PlainTextResponse((EXPORT_DIR / "ROADMAP.md").read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")


class Subscribe(BaseModel):
    email: str


@router.post("/newsletter/subscribe")
def newsletter_subscribe(body: Subscribe) -> dict:
    if not _EMAIL.match(body.email.strip()):
        raise HTTPException(status_code=422, detail="a valid email is required")
    return newsletter.subscribe(body.email)


@router.get("/newsletter/digest")
def newsletter_digest(days: int = Query(default=7, ge=1, le=90)) -> dict:
    from datetime import datetime, timedelta, timezone

    digest = newsletter.compose_digest(get_engine(), datetime.now(timezone.utc).date() - timedelta(days=days))
    return {k: digest[k] for k in ("subject", "since", "count", "counts", "text")}
