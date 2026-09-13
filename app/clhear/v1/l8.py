"""L8 API — fills and member benchmarks (HLD v2 §4.8, open by mode I9).

Public in every mode (metadata only — never content):
    GET  /l8                                   the L8 page
    GET  /l8/mode                              this request's mode: agnostic | member | instance
    GET  /l8/availability?block=BLK-…          per block: fills exist? how many, at which maturity
    GET  /l8/summary                           fill counts by maturity/provenance/kind; benchmark metric catalogue
    GET  /l8/metrics                           the benchmark metric catalogue (units, bounds)

Members only (session member, member's application key, instance mode, or maintainer):
    GET  /l8/fills?block=&maturity=&kind=       fill content for a block
    GET  /l8/fills/{id}                        one fill: content, reviews, why-trail
    GET  /l8/fills/{id}/history                every version (I2)
    POST /l8/fills/{id}/reviews                rubric review (reviewer / maintainer role): endorse | revise | reject
    GET  /l8/benchmarks?cohort=&metric=&block=  k ≥ 5, DP-noised aggregates
    GET  /l8/benchmarks/{id}                   one aggregate with its why-trail
    POST /l8/benchmarks/inputs                 opt-in observation (stored under an HMAC — never served)

Maintainers:
    GET  /l8/members · POST /l8/members · POST /l8/members/revoke
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.clhear.db import get_engine
from app.clhear.l8 import aggregate as l8_aggregate
from app.clhear.l8 import fills as l8_fills
from app.clhear.l8.models import FILL_KINDS, K_MIN, MATURITIES, METHOD_VERSION, PROVENANCES, REVIEW_DECISIONS, RUBRIC_CRITERIA, RUBRIC_MIN
from app.clhear.platform import contributions as contrib
from app.clhear.platform import mode as l8_mode

router = APIRouter(prefix="/l8", tags=["l8"])
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

REVIEWING_ROLES = {"reviewer", "maintainer", "steering"}


def _require_maintainer(request: Request) -> dict:
    ctx = l8_mode.request_mode(request)
    ident = ctx.get("identity") or {}
    email = ident.get("email")
    if ident.get("maintainer"):
        return ident
    if email:
        with get_engine().connect() as conn:
            if contrib.roles_for(conn, email) & contrib.GRANTING_ROLES:
                return ident
    raise HTTPException(status_code=403 if ident else 401, detail="maintainer role required")


def _require_reviewer(request: Request) -> dict:
    ctx = l8_mode.require_member(request)
    ident = ctx.get("identity") or {}
    if ident.get("maintainer"):
        return ident
    email = ident.get("email")
    if email:
        with get_engine().connect() as conn:
            if contrib.roles_for(conn, email) & REVIEWING_ROLES:
                return ident
    raise HTTPException(status_code=403, detail={"code": "not_a_reviewer", "message": "fill reviews need the reviewer role"})


# --------------------------------------------------------------------------- public


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def l8_page() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "l8.html").read_text(), headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/mode")
def mode(request: Request) -> dict:
    ctx = l8_mode.request_mode(request)
    ident = ctx.get("identity") or {}
    return {"mode": ctx["mode"], "closed": ctx["closed"], "signed_in": bool(ident),
            "via": ident.get("via"), "deployment": l8_mode.deployment_mode(),
            "member_content": ["L8 fills", "member benchmarks"], "public_metadata": ["/l8/availability", "/l8/summary", "/l8/metrics"]}


@router.get("/availability")
def availability(block: str | None = Query(default=None, description="comma-separated block ids")) -> dict:
    ids = [b.strip() for b in block.split(",") if b.strip()] if block else None
    return l8_fills.availability(get_engine(), block_ids=ids)


@router.get("/summary")
def summary() -> dict:
    engine = get_engine()
    return {"fills": l8_fills.summary(engine), "benchmarks": l8_aggregate.summary(engine),
            "method": {"version": METHOD_VERSION, "rubric_criteria": list(RUBRIC_CRITERIA), "rubric_min": RUBRIC_MIN, "k_min": K_MIN,
                       "kinds": list(FILL_KINDS), "maturities": list(MATURITIES), "provenances": list(PROVENANCES)}}


@router.get("/metrics")
def metrics() -> dict:
    return {"metrics": [{"metric": m, **spec} for m, spec in l8_aggregate.METRICS.items()], "k_min": K_MIN,
            "cohort_key": "jurisdiction|sector|size, e.g. UK|payments|retail", "dp": "Laplace(1/ε) histogram, ε=1.0"}


# --------------------------------------------------------------------------- members: fills


@router.get("/fills")
def list_fills(ctx: dict = Depends(l8_mode.require_member), block: str | None = None, maturity: str | None = None,
               kind: str | None = None, provenance: str | None = None, limit: int = Query(default=200, le=500)) -> dict:
    rows = l8_fills.list_fills(get_engine(), block_id=block, maturity=maturity, kind=kind, provenance=provenance, limit=limit)
    return {"fills": rows, "count": len(rows), "mode": ctx["mode"]}


@router.get("/fills/{fill_id}")
def get_fill(fill_id: str, ctx: dict = Depends(l8_mode.require_member)) -> dict:
    row = l8_fills.get(get_engine(), fill_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown fill {fill_id}")
    return row


@router.get("/fills/{fill_id}/history")
def fill_history(fill_id: str, ctx: dict = Depends(l8_mode.require_member)) -> dict:
    rows = l8_fills.history(get_engine(), fill_id)
    if not rows:
        raise HTTPException(status_code=404, detail=f"unknown fill {fill_id}")
    return {"id": fill_id, "versions": rows}


class ReviewBody(BaseModel):
    rubric: dict[str, float] = Field(description=f"0..1 for each of {RUBRIC_CRITERIA}")
    decision: str = Field(description=f"one of {REVIEW_DECISIONS}")
    note: str = ""


@router.post("/fills/{fill_id}/reviews", status_code=201)
def review_fill(fill_id: str, body: ReviewBody, reviewer: dict = Depends(_require_reviewer)) -> dict:
    try:
        return l8_fills.review_fill(get_engine(), fill_id, reviewer=reviewer.get("email") or str(reviewer.get("user_id")),
                                    rubric=body.rubric, decision=body.decision, note=body.note)
    except l8_fills.NotEndorsable as exc:
        raise HTTPException(status_code=422, detail={"code": "below_rubric", "message": str(exc)}) from exc
    except l8_fills.InvalidFill as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown fill {exc}") from exc


# --------------------------------------------------------------------------- members: benchmarks


@router.get("/benchmarks")
def list_benchmarks(ctx: dict = Depends(l8_mode.require_member), cohort: str | None = None, metric: str | None = None,
                    block: str | None = None, limit: int = Query(default=200, le=500)) -> dict:
    rows = l8_aggregate.list_aggregates(get_engine(), cohort_key=cohort, metric=metric, block_id=block, limit=limit)
    return {"aggregates": rows, "count": len(rows), "k_min": K_MIN, "mode": ctx["mode"],
            "note": "Every row is a k ≥ 5 cohort with Laplace noise; individual observations are never served."}


class InputBody(BaseModel):
    cohort_key: str
    metric: str
    value: float
    block_id: str | None = None


@router.post("/benchmarks/inputs", status_code=201)
def submit_input(body: InputBody, ctx: dict = Depends(l8_mode.require_member)) -> dict:
    ident = ctx.get("identity") or {}
    member_id = ident.get("email") or (str(ident.get("user_id")) if ident.get("user_id") else None)
    if not member_id:
        raise HTTPException(status_code=401, detail="a member identity is required to contribute an observation")
    try:
        return l8_aggregate.submit_input(get_engine(), member_id=member_id, cohort_key=body.cohort_key, metric=body.metric,
                                         value=body.value, block_id=body.block_id)
    except l8_aggregate.InvalidInput as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/benchmarks/{agg_id}")
def get_benchmark(agg_id: str, ctx: dict = Depends(l8_mode.require_member)) -> dict:
    row = l8_aggregate.get_aggregate(get_engine(), agg_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown aggregate {agg_id}")
    return row


# --------------------------------------------------------------------------- maintainers: membership


class MemberBody(BaseModel):
    email: str
    org_label: str = ""
    plan: str = "member"


@router.get("/members")
def list_members(_: dict = Depends(_require_maintainer)) -> dict:
    rows = l8_mode.list_members(get_engine())
    return {"members": rows, "count": len(rows)}


@router.post("/members", status_code=201)
def grant_member(body: MemberBody, granter: dict = Depends(_require_maintainer)) -> dict:
    if "@" not in body.email:
        raise HTTPException(status_code=422, detail="email required")
    return l8_mode.grant_membership(get_engine(), email=body.email, granted_by=granter.get("email") or "maintainer",
                                    org_label=body.org_label, plan=body.plan)


@router.post("/members/revoke")
def revoke_member(body: MemberBody, _: dict = Depends(_require_maintainer)) -> dict:
    return {"revoked": l8_mode.revoke_membership(get_engine(), email=body.email)}
