"""Approval console routes (HLD v2 I4, §8 item 11).

Reading the queues is open (I9 — the fact that a determination is awaiting a
human is public information); every decision needs a maintainer identity and is
recorded under it.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.clhear.db import get_engine
from app.clhear.platform import console
from app.clhear.platform import proposals as l0_proposals
from app.clhear.routes import require_maintainer

router = APIRouter(tags=["console"])
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class ValidateBody(BaseModel):
    note: str = Field(default="", max_length=1000)


class DecisionBody(BaseModel):
    override: dict | None = Field(default=None, description="maintainer-edited draft fields (e.g. a corrected proposed_value)")


@router.get("/console", response_class=HTMLResponse)
def console_page() -> str:
    return (WEB_DIR / "console.html").read_text(encoding="utf-8")


@router.get("/console/summary")
def console_summary() -> dict:
    return console.summary(get_engine())


@router.get("/console/queue")
def low_confidence_queue(layer: str | None = Query(default=None, pattern="^[lL][2-5]$"),
                         limit: int = Query(default=200, ge=0, le=1000)) -> dict:
    return console.low_confidence_queue(get_engine(), layer=layer, limit=limit)


@router.get("/console/requests")
def modification_requests(status: str = Query(default="proposed", pattern="^(proposed|approved|rejected)$"),
                          limit: int = Query(default=200, ge=1, le=1000)) -> list[dict]:
    return console.list_requests(get_engine(), status=status, limit=limit)


@router.get("/console/contributions")
def contribution_reviews(status: str = Query(default="proposed", pattern="^(proposed|approved|rejected)$"),
                         limit: int = Query(default=200, ge=1, le=1000)) -> list[dict]:
    return console.list_requests(get_engine(), status=status, contributions=True, limit=limit)


@router.get("/console/edits")
def human_edits(status: str | None = Query(default=None, pattern="^(accepted|reproduced|escalated|superseded)$"),
                layer: str | None = None, limit: int = Query(default=200, ge=1, le=1000)) -> list[dict]:
    return console.list_edits(get_engine(), status=status, layer=layer, limit=limit)


@router.post("/console/queue/{layer}/{subject_ref:path}/validate")
def validate_determination(layer: str, subject_ref: str, body: ValidateBody | None = None,
                           approver: str = Depends(require_maintainer)) -> dict:
    try:
        return console.validate(get_engine(), layer=layer, subject_ref=subject_ref, approver=approver,
                                note=(body.note if body else ""))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown {layer} determination {subject_ref}")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _decide(proposal_id: str, decision: str, approver: str, body: DecisionBody | None) -> dict:
    try:
        return console.decide(get_engine(), proposal_id, decision, approver, override=(body.override if body else None))
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    except l0_proposals.ProposalNotPending as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/console/proposals/{proposal_id}/approve")
def approve(proposal_id: str, body: DecisionBody | None = None, approver: str = Depends(require_maintainer)) -> dict:
    return _decide(proposal_id, "approved", approver, body)


@router.post("/console/proposals/{proposal_id}/reject")
def reject(proposal_id: str, body: DecisionBody | None = None, approver: str = Depends(require_maintainer)) -> dict:
    return _decide(proposal_id, "rejected", approver, body)


@router.post("/console/edits/reproduce")
def reproduce(layer: str | None = None, approver: str = Depends(require_maintainer)) -> dict:
    """Run the reproduce-or-escalate pass now (the nightly stack runs it after each layer)."""
    return {"by": approver, **console.reproduce_human_edits(get_engine(), layer=layer)}
