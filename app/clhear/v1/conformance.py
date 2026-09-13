"""Conformance program API (standard §7, Annex E; item 15).

Public:
    GET  /conformance                          the program page (levels, self-assessment form, register)
    GET  /conformance/levels                   CL1–CL4 and the criteria (from conformance/criteria.json)
    GET  /conformance/criteria?level=CL2       criteria required at a level
    GET  /conformance/register?organization=&level=   every mark ever granted — the authoritative register
    GET  /conformance/register/{CFM-id}        one mark with its statement
    GET  /conformance/assessors                the accredited assessor register (name, firm, jurisdiction, validity)
    GET  /conformance/summary

Signed in:
    POST /conformance/self-assessments         submit an Annex E form → automated checks, level supported
    GET  /conformance/self-assessments         my submissions
    GET  /conformance/self-assessments/{id}    one submission (submitter or verifier)

Program verifiers (maintainer / steering):
    GET  /conformance/queue                    submissions awaiting a decision
    POST /conformance/self-assessments/{id}/decision   grant | decline | request_changes
    POST /conformance/marks                    record a CL3/CL4 mark from an accredited assessor's report
    POST /conformance/marks/{id}/withdraw
    POST /conformance/assessors · POST /conformance/assessors/{id}/withdraw
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.clhear import conformance as cf
from app.clhear.db import get_engine
from app.clhear.v1.contributions import identity, require_granting_role, require_identity

router = APIRouter(prefix="/conformance", tags=["conformance"])
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, cf.NotPermitted):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, cf.InvalidSubmission):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=f"unknown {exc}")
    raise exc


# --------------------------------------------------------------------------- public


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def page() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "conformance.html").read_text(), headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/levels")
def levels() -> dict:
    c = cf.criteria()
    return {"version": c["version"], "levels": c["levels"], "criteria": c["criteria"], "validity_months": cf.VALIDITY_MONTHS,
            "documents": {"annex_e": "conformance/ANNEX_E_SELF_ASSESSMENT.md", "assessor_guide": "conformance/ASSESSOR_GUIDE.md",
                          "marks_policy": "conformance/MARKS_POLICY.md", "levels": "conformance/LEVELS.md"}}


@router.get("/criteria")
def criteria(level: str = Query(default="CL2")) -> dict:
    try:
        rows = cf.criteria_for(level)
    except cf.InvalidSubmission as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"level": level, "criteria": rows, "count": len(rows)}


@router.get("/register")
def register(organization: str | None = None, level: str | None = None, include_withdrawn: bool = True) -> dict:
    rows = cf.register(get_engine(), organization=organization, level=level, include_withdrawn=include_withdrawn)
    return {"marks": rows, "count": len(rows), "note": "The only authoritative list of CLHEAR conformance marks (MARKS_POLICY.md). "
                                                       "Withdrawn and expired marks stay listed."}


@router.get("/register/{mark_id}")
def mark(mark_id: str) -> dict:
    row = cf.get_mark(get_engine(), mark_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown mark {mark_id}")
    return row


@router.get("/assessors")
def assessors(active_only: bool = False) -> dict:
    rows = cf.list_assessors(get_engine(), active_only=active_only)
    return {"assessors": rows, "count": len(rows)}


@router.get("/summary")
def summary() -> dict:
    return cf.summary(get_engine())


# --------------------------------------------------------------------------- self-assessments


@router.post("/self-assessments", status_code=201)
def submit(form: dict, user: dict = Depends(require_identity)) -> dict:
    try:
        return cf.submit(get_engine(), form, submitted_by=user["email"])
    except (cf.InvalidSubmission, cf.NotPermitted, KeyError) as exc:
        raise _translate(exc) from exc


@router.get("/self-assessments")
def mine(user: dict = Depends(require_identity), status: str | None = None) -> dict:
    rows = cf.list_assessments(get_engine(), submitted_by=user["email"], status=status)
    return {"assessments": rows, "count": len(rows)}


@router.get("/self-assessments/{assessment_id}")
def one(assessment_id: str, user: dict = Depends(require_identity)) -> dict:
    row = cf.get_assessment(get_engine(), assessment_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown assessment {assessment_id}")
    if row["submitted_by"] != user["email"].lower():
        from app.clhear.platform.contributions import GRANTING_ROLES, roles_for

        with get_engine().connect() as conn:
            if not (roles_for(conn, user["email"]) & GRANTING_ROLES):
                raise HTTPException(status_code=403, detail="only the submitter or a program verifier may read a submission")
    return row


@router.get("/queue")
def queue(_: dict = Depends(require_granting_role)) -> dict:
    rows = cf.list_assessments(get_engine(), status="submitted")
    return {"assessments": rows, "count": len(rows)}


class DecisionBody(BaseModel):
    decision: str
    note: str = ""
    level: str | None = None


@router.post("/self-assessments/{assessment_id}/decision")
def decision(assessment_id: str, body: DecisionBody, verifier: dict = Depends(require_granting_role)) -> dict:
    try:
        return cf.decide(get_engine(), assessment_id, decided_by=verifier["email"], decision=body.decision, note=body.note, level=body.level)
    except (cf.InvalidSubmission, cf.NotPermitted, KeyError) as exc:
        raise _translate(exc) from exc


# --------------------------------------------------------------------------- marks + assessors (verifiers)


class AssessedMarkBody(BaseModel):
    level: str
    organization: str
    program: str
    scope: str
    release: str
    blueprint_id: str
    assessor_id: int
    report_ref: str
    period_start: date
    period_end: date


@router.post("/marks", status_code=201)
def record_mark(body: AssessedMarkBody, verifier: dict = Depends(require_granting_role)) -> dict:
    try:
        return cf.record_assessed_mark(get_engine(), recorded_by=verifier["email"], **body.model_dump())
    except (cf.InvalidSubmission, cf.NotPermitted, KeyError) as exc:
        raise _translate(exc) from exc


class WithdrawBody(BaseModel):
    reason: str


@router.post("/marks/{mark_id}/withdraw")
def withdraw(mark_id: str, body: WithdrawBody, verifier: dict = Depends(require_granting_role)) -> dict:
    try:
        return cf.withdraw_mark(get_engine(), mark_id, withdrawn_by=verifier["email"], reason=body.reason)
    except (cf.InvalidSubmission, cf.NotPermitted, KeyError) as exc:
        raise _translate(exc) from exc


class AssessorBody(BaseModel):
    name: str
    email: str
    firm: str = ""
    jurisdiction: str = ""
    licence_ref: str
    briefing_completed: date
    conflicts_declared: bool = False


@router.post("/assessors", status_code=201)
def accredit(body: AssessorBody, verifier: dict = Depends(require_granting_role)) -> dict:
    try:
        return cf.accredit_assessor(get_engine(), accredited_by=verifier["email"], **body.model_dump())
    except (cf.InvalidSubmission, cf.NotPermitted) as exc:
        raise _translate(exc) from exc


@router.post("/assessors/{assessor_id}/withdraw")
def withdraw_assessor(assessor_id: int, body: WithdrawBody, verifier: dict = Depends(require_granting_role)) -> dict:
    try:
        return cf.withdraw_assessor(get_engine(), assessor_id, withdrawn_by=verifier["email"], reason=body.reason)
    except (cf.InvalidSubmission, cf.NotPermitted, KeyError) as exc:
        raise _translate(exc) from exc


@router.get("/whoami", include_in_schema=False)
def whoami(user: dict | None = Depends(identity)) -> dict:
    if not user:
        return {"signed_in": False, "verifier": False}
    from app.clhear.platform.contributions import GRANTING_ROLES, roles_for

    with get_engine().connect() as conn:
        roles = roles_for(conn, user["email"])
    return {"signed_in": True, "email": user["email"], "verifier": bool(roles & GRANTING_ROLES)}
