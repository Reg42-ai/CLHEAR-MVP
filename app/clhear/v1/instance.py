"""Instance-mode API (HLD v2 I5/I9; build item 18) — the contract Reg42 OS codes against.

    GET  /instance/contract               the contract summary (open: method and shapes)
    GET  /instance/contract/openapi.json  standalone OpenAPI for these endpoints (open)
    GET  /instance/guard                  is this deployment allowed to run instance mode? (open, no secrets)
    POST /instance/overlay/diff           Actual overlay → gap diff            (instance deployments only)
    POST /instance/overlay/priorities     Actual overlay → instance priorities (instance deployments only)

On the public (agnostic) deployment the two POST endpoints answer 403 ``instance_only``
and point at the contract; nothing in the request body is read, logged or audited.
In an instance deployment they compute inside :func:`instance_contract.session`, which
guarantees the agnostic store is unchanged afterwards (I5).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.clhear import instance_contract as ic
from app.clhear.db import get_engine

router = APIRouter(prefix="/instance", tags=["instance"])


def _require_instance_deployment(request: Request) -> dict:
    guard = ic.deployment_guard()
    if not guard["allowed"]:
        raise HTTPException(status_code=403, detail={
            "code": "instance_only",
            "message": "Instance mode (Actual overlay, gap diff, instance priorities) runs only in the client's account through Reg42 OS. "
                       "This deployment is agnostic: it holds no organization data (HLD v2 I5).",
            "reason": guard["reason"], "contract": "/instance/contract", "document": ic.CONTRACT_DOC})
    return guard


@router.get("/contract")
def contract() -> dict:
    return ic.contract()


@router.get("/contract/openapi.json", include_in_schema=False)
def contract_openapi() -> JSONResponse:
    return JSONResponse(ic.openapi(), headers={"Cache-Control": "public, max-age=3600"})


@router.get("/guard")
def guard() -> dict:
    g = ic.deployment_guard()
    return {k: g[k] for k in ("mode", "instance_account_pinned", "allowed", "reason")}


def _run(overlay: ic.ActualOverlay) -> dict:
    try:
        return ic.run_session(get_engine(), overlay, compose_missing=True)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ic.StoreLeak as exc:  # a leak is a defect, never a 200
        raise HTTPException(status_code=500, detail={"code": "store_leak", "message": str(exc)}) from exc


async def _overlay(request: Request) -> ic.ActualOverlay:
    """Parsed only after the deployment guard passed: on the public node the body is never read."""
    try:
        return ic.ActualOverlay.model_validate(await request.json())
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid JSON body: {exc}") from exc


_OVERLAY_BODY = {"requestBody": {"required": True, "description": "ActualOverlay (see /instance/contract)",
                                 "content": {"application/json": {"schema": ic.ActualOverlay.model_json_schema()}}}}


@router.post("/overlay/diff", response_model=ic.GapDiff, openapi_extra=_OVERLAY_BODY)
async def overlay_diff(request: Request) -> dict:
    """Blueprint/Actual overlay → gap diff (ghost = required, solid = present, delta = gap)."""
    _require_instance_deployment(request)
    return _run(await _overlay(request))["gap_diff"]


@router.post("/overlay/priorities", response_model=ic.InstancePriorities, openapi_extra=_OVERLAY_BODY)
async def overlay_priorities(request: Request) -> dict:
    """Instance priorities: what this organization should fix first, from its gaps and the open base priorities."""
    _require_instance_deployment(request)
    return _run(await _overlay(request))["priorities"]
