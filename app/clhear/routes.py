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


@router.post("/api/clhear/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: str, approver: str = Depends(require_maintainer)) -> dict:
    try:
        return l0_proposals.approve(get_engine(), proposal_id, approver)
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    except l0_proposals.ProposalNotPending as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/clhear/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: str, approver: str = Depends(require_maintainer)) -> dict:
    try:
        return l0_proposals.reject(get_engine(), proposal_id, approver)
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    except l0_proposals.ProposalNotPending as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/review", response_class=HTMLResponse)
def review_console() -> str:
    return (WEB_DIR / "review.html").read_text()
