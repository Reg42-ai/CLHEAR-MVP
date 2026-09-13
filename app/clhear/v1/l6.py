"""L6 public API — the blueprint composer (HLD v2 §4.6 / §5).

    GET  /l6/vocabulary                         engine version, item bases, rubric
    GET  /l6/blueprints                         stored blueprints (profile_id / status filters)
    POST /l6/blueprints                         compose + store for {profile_id} or {attributes[, activities]}
    GET  /l6/blueprints/{id}                    one blueprint: composition (items, program tree, minimality proof),
                                                history, why-trail, what changed since
    GET  /l6/blueprints/{id}/items/{item}       one item: why (obligations -> registry entries), characteristics,
                                                activities, removal impact
    GET  /l6/blueprints/{id}/minimality         the minimality proof, independently re-verified
    GET  /l6/blueprints/{id}/diff?against=      diff against another blueprint id, or "now" (recompose)
    GET  /l6/blueprints/{id}/export?format=     oscal (system-security-plan) | jsonld | json
    POST /l6/compare                            compose two profiles (unstored) and diff them
    GET  /l6/profiles/{id}/blueprint            the current blueprint of a stored L4 profile (composed on demand)
    GET  /l6/export/oscal/components            L3 blocks as an OSCAL component-definition
    GET  /l6/scorecard                          published L6 scorecard (gates, blueprints, completeness, minimality)

Nothing here serves clause text; obligations are referenced by their ids.
"""
from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.clhear.db import get_engine
from app.clhear.derived_models import blueprint_items, blueprints, obligations
from app.clhear.interop import oscal
from app.clhear.l4 import validate as l4_validate
from app.clhear.l6 import composer, diff as l6_diff
from app.clhear.l6.check import check_blueprint, verify_minimality
from app.clhear.l6.explain import rubric
from app.clhear.l6.models import vocabulary
from app.clhear.platform import record

router = APIRouter(prefix="/l6", tags=["l6"])
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class ComposeBody(BaseModel):
    profile_id: str | None = None
    attributes: dict | None = None
    activities: list[str] | None = None
    release: str = ""
    requested_by: str = "api"


class CompareBody(BaseModel):
    a: ComposeBody
    b: ComposeBody


def _why_rows(conn, ids: list[str]) -> list[dict]:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return []
    rows = {r["id"]: r for r in conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id.in_(ids))).mappings()}
    return [
        {"id": r["id"], "layer": r["layer"], "subject_ref": r["subject_ref"], "reasoning_summary": r["reasoning_summary"],
         "evidence_refs": r["evidence_refs"], "model_manifest": r["model_manifest"], "skill_version": r["skill_version"],
         "confidence": float(r["confidence"]) if r["confidence"] is not None else None, "agent_id": r["agent_id"],
         "created_at": str(r["created_at"])}
        for wid in ids if (r := rows.get(wid)) is not None
    ]


def _profile_for(body: ComposeBody) -> dict:
    engine = get_engine()
    if body.profile_id:
        with engine.connect() as conn:
            row = l4_validate.get_profile(conn, body.profile_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown profile {body.profile_id}")
        return {"attributes": composer._json(row["attributes"], {}), "activities": body.activities, "profile_id": body.profile_id}
    if not body.attributes:
        raise HTTPException(status_code=422, detail="profile_id or attributes is required")
    try:
        validity = l4_validate.validate(engine, body.attributes)
    except sa.exc.OperationalError:
        validity = {"valid": True, "errors": []}
    if not validity["valid"]:
        raise HTTPException(status_code=422, detail={"message": "profile is not a valid permutation", "errors": validity["errors"]})
    return {"attributes": body.attributes, "activities": body.activities, "profile_id": None}


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def l6_page() -> HTMLResponse:
    """HLD v2 §4.6 L6 UI: blueprint view (program tree by kind, why per item, what changed, compare, export)."""
    return HTMLResponse((WEB_DIR / "l6.html").read_text(), headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/vocabulary")
def get_vocabulary() -> dict:
    return vocabulary()


# ----------------------------------------------------------------- blueprints


@router.get("/blueprints")
def list_blueprints(profile_id: str | None = None, status: str | None = None, limit: int = Query(default=100, le=500)) -> dict:
    with get_engine().connect() as conn:
        items = composer.list_blueprints(conn, profile_id=profile_id, status=status, limit=limit)
    return {"count": len(items), "items": items}


@router.post("/blueprints", status_code=201)
def create_blueprint(body: ComposeBody) -> dict:
    profile = _profile_for(body)
    result = composer.compose(get_engine(), profile, requested_by=body.requested_by, release=body.release)
    result["check"] = check_blueprint(result)
    return result


@router.get("/blueprints/{blueprint_id}")
def get_blueprint(blueprint_id: str, since: bool = False) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        bp = composer.get_blueprint(conn, blueprint_id)
        if bp is None:
            raise HTTPException(status_code=404, detail=f"unknown blueprint {blueprint_id}")
        bp["history"] = composer.history(conn, blueprint_id)
        bp["why"] = _why_rows(conn, [bp["why_trail_id"]])
    bp["check"] = check_blueprint(bp["composition"])
    if since:
        bp["what_changed_since"] = l6_diff.what_changed_since(engine, blueprint_id)
    return bp


@router.get("/blueprints/{blueprint_id}/minimality")
def blueprint_minimality(blueprint_id: str) -> dict:
    with get_engine().connect() as conn:
        bp = composer.get_blueprint(conn, blueprint_id)
    if bp is None:
        raise HTTPException(status_code=404, detail=f"unknown blueprint {blueprint_id}")
    comp = bp["composition"]
    return {"blueprint_id": blueprint_id, "claimed": comp.get("minimality"), "verified": verify_minimality(comp),
            "proof_rows": bp["proof_rows"]}


@router.get("/blueprints/{blueprint_id}/items/{item_id}")
def blueprint_item(blueprint_id: str, item_id: str) -> dict:
    with get_engine().connect() as conn:
        bp = composer.get_blueprint(conn, blueprint_id)
        if bp is None:
            raise HTTPException(status_code=404, detail=f"unknown blueprint {blueprint_id}")
        comp = bp["composition"]
        item = next((i for i in comp.get("items", []) if i.get("id") == item_id or i["block_id"] == item_id), None)
        if item is None:
            raise HTTPException(status_code=404, detail=f"unknown item {item_id} in {blueprint_id}")
        by_oid = {c["obligation_id"]: c for c in comp.get("coverage", [])}
        rows = {r["id"]: dict(r) for r in conn.execute(
            sa.select(obligations).where(obligations.c.id.in_(item["obligations_satisfied"] or [""]))).mappings()}
        stored = conn.execute(sa.select(blueprint_items).where(blueprint_items.c.blueprint_id == blueprint_id,
                                                               blueprint_items.c.block_id == item["block_id"])).mappings().first()
        why = _why_rows(conn, [stored["why_trail_id"]]) if stored else []
    obligations_out = []
    for oid in item["obligations_satisfied"]:
        c = by_oid.get(oid, {})
        r = rows.get(oid, {})
        obligations_out.append({
            "obligation_id": oid, "stable_id": r.get("stable_id") or c.get("stable_id"), "title": r.get("title") or c.get("title"),
            "source_key": c.get("source_key"), "clause_ref": c.get("clause_ref"), "jurisdiction": r.get("jurisdiction"),
            "determination": r.get("determination"), "triggered_by": c.get("triggered_by"), "conditions": c.get("conditions"),
            "satisfied_by": c.get("satisfied_by"), "only_here": c.get("satisfied_by") == [item["block_id"]],
        })
    proof = next((p for p in comp.get("minimality", {}).get("proof", []) if p["block_id"] == item["block_id"]), None)
    return {"blueprint_id": blueprint_id, "item": item, "obligations": obligations_out, "proof": proof,
            "rubric": rubric(item.get("explanation", ""), item, comp), "why": why}


@router.get("/blueprints/{blueprint_id}/diff")
def blueprint_diff(blueprint_id: str, against: str = Query(default="now")) -> dict:
    engine = get_engine()
    try:
        if against == "now":
            return l6_diff.what_changed_since(engine, blueprint_id)
        with engine.connect() as conn:
            return l6_diff.diff_blueprints(conn, blueprint_id, against)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown blueprint {exc.args[0]}") from exc


@router.get("/blueprints/{blueprint_id}/export")
def blueprint_export(blueprint_id: str, format: str = Query(default="oscal")) -> dict:
    with get_engine().connect() as conn:
        bp = composer.get_blueprint(conn, blueprint_id)
    if bp is None:
        raise HTTPException(status_code=404, detail=f"unknown blueprint {blueprint_id}")
    if format == "json":
        return bp["composition"]
    if format == "jsonld":
        from app.clhear.interop import jsonld

        return jsonld.blueprint(bp["composition"], blueprint_id=blueprint_id, row=bp)
    if format != "oscal":
        raise HTTPException(status_code=422, detail="format must be oscal, jsonld or json")
    return oscal.blueprint_ssp(bp["composition"], blueprint_id=blueprint_id)


@router.post("/compare")
def compare(body: CompareBody) -> dict:
    engine = get_engine()
    a = composer.compose(engine, _profile_for(body.a), release=body.a.release, log_request=False)
    b = composer.compose(engine, _profile_for(body.b), release=body.b.release, log_request=False)
    out = l6_diff.diff_compositions(a, b)
    out["a"] = {"profile_id": a["profile_id"], "attributes": a["profile_attributes"], "items": [i["block_id"] for i in a["items"]],
                "coverage_summary": a["coverage_summary"]}
    out["b"] = {"profile_id": b["profile_id"], "attributes": b["profile_attributes"], "items": [i["block_id"] for i in b["items"]],
                "coverage_summary": b["coverage_summary"]}
    return out


@router.get("/profiles/{profile_id}/blueprint")
def profile_blueprint(profile_id: str) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        current = composer.list_blueprints(conn, profile_id=profile_id, status="current", limit=1)
    if current:
        with engine.connect() as conn:
            bp = composer.get_blueprint(conn, current[0]["blueprint_id"])
        comp = bp["composition"]
        comp["blueprint_id"] = bp["blueprint_id"]
        comp["status"] = bp["status"]
        return comp
    try:
        return composer.compose_for_profile(engine, profile_id, requested_by="api")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown profile {profile_id}") from exc


@router.get("/export/oscal/components")
def oscal_components(release: str = "") -> dict:
    with get_engine().connect() as conn:
        return oscal.component_definition(conn, release=release)


@router.get("/scorecard")
def l6_scorecard() -> dict:
    from app.clhear.platform.gates import GATE_THRESHOLDS, gate_status

    engine = get_engine()
    with engine.connect() as conn:
        current = composer.list_blueprints(conn, status="current", limit=500)
        total = conn.execute(sa.select(sa.func.count()).select_from(blueprints).where(blueprints.c.stable_id.isnot(None))).scalar_one()
        items = conn.execute(sa.select(sa.func.count()).select_from(blueprint_items)).scalar_one()
        comps = []
        for row in current:
            bp = composer.get_blueprint(conn, row["blueprint_id"])
            if bp:
                comps.append(bp["composition"])
    applicable = sum((c.get("coverage_summary") or {}).get("total", 0) for c in comps)
    covered = sum((c.get("coverage_summary") or {}).get("covered", 0) for c in comps)
    minimal = sum(1 for c in comps if (c.get("minimality") or {}).get("minimal"))
    return {
        "gate": gate_status(engine, "L6"),
        "thresholds": GATE_THRESHOLDS.get("L6", {}),
        "blueprints": {"current": len(current), "total": total, "items": items},
        "completeness": {"applicable": applicable, "covered": covered, "ratio": (covered / applicable) if applicable else None},
        "minimality": {"minimal": minimal, "checked": len(comps)},
        "by_profile": [{"blueprint_id": r["blueprint_id"], "profile_id": r["profile_id"], "coverage_summary": r["coverage_summary"],
                        "minimal": r["minimal"], "blocks": len(r["blocks"])} for r in current[:50]],
    }
