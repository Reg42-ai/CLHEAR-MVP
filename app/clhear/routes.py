"""/api/clhear/* routes + /review console (HLD §7.1).

P0 scope: health, proposals list/approve/reject. /api/clhear/sources… and BYOL
endpoints arrive with L1 (P1+).
"""
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse

from app.clhear.db import get_engine
from app.clhear.platform import proposals as l0_proposals
from app.clhear.settings import get_settings

router = APIRouter()

WEB_DIR = Path(__file__).parent / "web"


def require_maintainer(x_reg42_user: str | None = Header(default=None)) -> str:
    """# ARCH: stand-in auth. reg42-os has session auth with roles; on merge this
    becomes a dependency on the existing authenticated-user-with-role('maintainer')
    check. The identity returned here is what gets recorded as approver.
    """
    if not x_reg42_user:
        raise HTTPException(status_code=401, detail="X-Reg42-User header required")
    if x_reg42_user not in get_settings().maintainer_set:
        raise HTTPException(status_code=403, detail=f"{x_reg42_user} lacks the maintainer role")
    return x_reg42_user


@router.get("/api/clhear/health")
def health() -> dict:
    return {"status": "ok", "service": "clhear"}


@router.get("/api/clhear/proposals")
def list_proposals(status: str | None = None) -> list[dict]:
    return l0_proposals.list_proposals(get_engine(), status=status)


def _decide(proposal_id: str, approver: str, action) -> dict:
    engine = get_engine()
    try:
        decided = action(engine, proposal_id, approver)
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    except l0_proposals.ProposalNotPending as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if str(decided.get("kind", "")).startswith("community_"):
        from app.clhear import community

        community.sync_submission_from_proposal(engine, decided)
    return decided


@router.post("/api/clhear/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: str, approver: str = Depends(require_maintainer)) -> dict:
    return _decide(proposal_id, approver, l0_proposals.approve)


@router.post("/api/clhear/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: str, approver: str = Depends(require_maintainer)) -> dict:
    return _decide(proposal_id, approver, l0_proposals.reject)


@router.post("/api/clhear/obligations/{obligation_id:path}/validate")
def validate_obligation(obligation_id: str, approver: str = Depends(require_maintainer)) -> dict:
    """Named-human gate: promotion to `validated` is always a recorded
    maintainer action — community votes only SUGGEST it."""
    from datetime import datetime, timezone

    import sqlalchemy as sa

    from app.clhear.derived_models import obligations
    from app.clhear.platform import events as l0_events

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(sa.select(obligations).where(obligations.c.id == obligation_id)).first()
        if row is None:
            raise HTTPException(status_code=404, detail="obligation not found")
        if row.status not in ("derived",):
            raise HTTPException(status_code=409, detail=f"obligation is {row.status}, only derived can be validated")
        conn.execute(
            obligations.update()
            .where(obligations.c.id == obligation_id)
            .values(status="validated", validated_by=approver, validated_at=datetime.now(timezone.utc))
        )
        l0_events.emit(
            conn, layer="l2", kind="ObligationValidated", subject_ref=obligation_id,
            payload={"approver": approver}, producer="l2.review",
        )
    return {"id": obligation_id, "status": "validated", "validated_by": approver}


@router.get("/review", response_class=HTMLResponse)
def review_console() -> str:
    return (WEB_DIR / "review.html").read_text()
