"""AI-native surfaces: router, ops, eval studio, team, corrections."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.clhear.accounts import current_user, require_user
from app.clhear.db import get_engine

router = APIRouter()


@router.get("/api/clhear/router")
def router_state() -> dict:
    from app.clhear.platform.router import last_decisions, quality_table, registry_public

    engine = get_engine()
    table = quality_table(engine)
    return {
        "tasks": registry_public(),
        "quality": {f"{k[0]}|{k[1]}": v for k, v in table.items()},
        "recent": last_decisions(engine, limit=30),
    }


@router.get("/api/clhear/ops")
def ops_feed(
    layer: str | None = None,
    kind: str | None = None,
    fleet: str | None = None,
    limit: int = Query(default=80, le=200),
) -> dict:
    from app.clhear import ai_ops

    engine = get_engine()
    return {
        "dashboard": ai_ops.dashboard(engine),
        "items": ai_ops.list_ops(engine, layer=layer, kind=kind, fleet=fleet, limit=limit),
    }


@router.get("/api/clhear/team")
def team(layer: str | None = None) -> dict:
    from app.clhear.team import team_view

    return team_view(get_engine(), layer=layer)


@router.get("/api/clhear/eval")
def eval_home(user: dict | None = Depends(current_user)) -> dict:
    from app.clhear import eval_studio
    from app.clhear.governance import audit_coverage

    engine = get_engine()
    eval_studio.sample_tasks(engine)
    uid = str(user["id"]) if user else None
    return {
        "open": eval_studio.list_open(engine, user_id=uid) if uid else eval_studio.list_open(engine),
        "scores": eval_studio.agreement_scores(engine),
        "audit_coverage": {code: audit_coverage(engine, code) for code in ("L2", "L3", "L4", "L5", "L7")},
        "signed_in": bool(user),
    }


@router.post("/api/clhear/eval/{task_id}/vote")
async def eval_vote(task_id: str, body: dict, user: dict = Depends(require_user)) -> dict:
    from app.clhear import eval_studio

    agrees = body.get("agrees")
    if agrees is None:
        raise HTTPException(400, "agrees (bool) is required")
    try:
        return eval_studio.record_vote(
            get_engine(), task_id=task_id, user_id=str(user["id"]),
            agrees=bool(agrees), comment=str(body.get("comment") or ""),
        )
    except KeyError:
        raise HTTPException(404, "eval task not found")


@router.get("/api/clhear/corrections")
def corrections(status: str | None = None) -> list:
    from app.clhear.governance import list_corrections

    return list_corrections(get_engine(), status=status)


@router.post("/api/clhear/corrections/{correction_id}/revalidate")
def revalidate(correction_id: str) -> dict:
    from app.clhear.governance import revalidate as _rev
    from app.clhear.platform.router import Router, build_providers

    engine = get_engine()
    llm = Router(engine, build_providers())
    try:
        return _rev(engine, llm, correction_id)
    except KeyError:
        raise HTTPException(404, "correction not found")


@router.get("/api/clhear/how-live")
def how_live() -> dict:
    """Payload for the graphical /how: funnel, pipeline counts, benchmark, loop."""
    from app.clhear import ai_ops, eval_studio
    from app.clhear.fleets import FLEET_RUN
    from app.clhear.governance import audit_coverage, list_corrections
    from app.clhear.models import runs
    from app.clhear.platform.router import last_decisions, registry_public
    import sqlalchemy as sa

    engine = get_engine()
    with engine.connect() as conn:
        nightly = conn.execute(
            sa.select(runs).where(runs.c.fleet == FLEET_RUN).order_by(runs.c.id.desc()).limit(1)
        ).first()
    pipeline = nightly.outputs if nightly and isinstance(nightly.outputs, dict) else {}
    return {
        "tasks": registry_public(),
        "routing": last_decisions(engine, limit=20),
        "dashboard": ai_ops.dashboard(engine),
        "pipeline": pipeline,
        "pipeline_reasoning": nightly.reasoning if nightly else None,
        "benchmark": eval_studio.agreement_scores(engine),
        "audit_coverage": audit_coverage(engine),
        "corrections": {
            "pending": len(list_corrections(engine, status="correction_pending")),
            "rejected": len(list_corrections(engine, status="ai_rejected")),
            "admin_queue": len(list_corrections(engine, status="admin_queue")),
        },
    }
