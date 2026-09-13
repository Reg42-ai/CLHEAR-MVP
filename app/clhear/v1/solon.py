"""Solon front door API (HLD v2 §5): the single field, the questions L4
needs, and the narrated build. No key, no session, no paywall — the agnostic
blueprint is open (I9: open by mode)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.clhear import solon
from app.clhear.db import get_engine
from app.clhear.l4 import validate as l4_validate
from app.clhear.l6 import composer

router = APIRouter(tags=["solon"])
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

EXAMPLES = [
    {"id": "global-retail-broker", "label": "Global retail broker (UK / EU / US)",
     "text": "We are a global retail equities broker holding client money, FCA authorised in the UK, with EU and US licences."},
    {"id": "uk-emi", "label": "UK e-money institution",
     "text": "A UK e-money institution issuing prepaid cards and payment accounts to consumers and small businesses."},
    {"id": "eu-casp", "label": "EU crypto-asset service provider",
     "text": "An EU CASP under MiCA offering crypto exchange and crypto custody to retail and professional clients."},
]


class IntakeBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class AnswerBody(BaseModel):
    attributes: dict = Field(default_factory=dict)
    attribute: str
    value: object = None
    asked: list[str] = Field(default_factory=list)
    merge: bool = False


class BuildBody(BaseModel):
    attributes: dict | None = None
    profile_id: str | None = None
    name: str = ""
    requested_by: str = "solon"


def _llm():
    """Router over Infer when configured; deterministic reading otherwise."""
    from app.clhear.platform.router import live_llm

    try:
        return live_llm(get_engine())
    except Exception:
        return None


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def front_door() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "front_door.html").read_text(), headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/solon/examples")
def examples() -> dict:
    return {"examples": EXAMPLES, "max_questions": solon.MAX_QUESTIONS, "budget_ms": solon.BUDGET_MS}


@router.post("/solon/intake")
def intake(body: IntakeBody) -> dict:
    return solon.intake(get_engine(), body.text, llm=_llm())


@router.post("/solon/answer")
def answer(body: AnswerBody) -> dict:
    try:
        return solon.answer(get_engine(), body.attributes, body.attribute, body.value, asked=body.asked, merge=body.merge)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _attributes_for(body: BuildBody) -> tuple[dict, str | None]:
    engine = get_engine()
    if body.profile_id:
        with engine.connect() as conn:
            row = l4_validate.get_profile(conn, body.profile_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown profile {body.profile_id}")
        return composer._json(row["attributes"], {}), body.profile_id
    if not body.attributes:
        raise HTTPException(status_code=422, detail="attributes or profile_id is required")
    return body.attributes, None


@router.post("/solon/build")
def build(body: BuildBody) -> dict:
    attrs, pid = _attributes_for(body)
    out = solon.build(get_engine(), attrs, requested_by=body.requested_by, profile_id=pid, name=body.name)
    if not out["ok"]:
        raise HTTPException(status_code=422, detail={"steps": out["steps"]})
    return out


@router.get("/solon/build/stream")
def build_stream(profile_id: str = Query(...), requested_by: str = "solon") -> StreamingResponse:
    """Server-sent events: one ``step`` event per layer as it completes."""
    engine = get_engine()
    with engine.connect() as conn:
        row = l4_validate.get_profile(conn, profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown profile {profile_id}")
    attrs = composer._json(row["attributes"], {})
    return StreamingResponse(solon.sse(solon.narrate(engine, attrs, requested_by=requested_by, profile_id=profile_id)),
                             media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/solon/profile")
def store_profile(body: BuildBody) -> dict:
    """Validated attributes -> stored PRF- (so the stream can be started by id)."""
    if not body.attributes:
        raise HTTPException(status_code=422, detail="attributes is required")
    try:
        row = l4_validate.create_profile(get_engine(), body.attributes, name=body.name or "Solon intake", source="solon")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=exc.args[0] if exc.args else "invalid profile") from exc
    return {"profile_id": row["id"], "validity": row.get("validity"), "status": row.get("status")}
