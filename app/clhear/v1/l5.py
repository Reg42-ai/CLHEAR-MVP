"""L5 public API — the activities junction (HLD v2 §4.5 / §5).

    GET  /l5/vocabulary                        sides, action types, edge kinds
    GET  /l5/activities                        activities (side / action_type / status / q, facets)
    GET  /l5/activities/{id}                   one activity: triggers resolved to obligations, implies / operates /
                                               mitigates edges, history, why-trail
    GET  /l5/activities/{id}/obligations       the obligations the activity triggers (anchored, live)
    GET  /l5/profiles/{id}/activities          the activity map of a stored L4 profile
    POST /l5/activity-map                      the activity map for an ad-hoc attribute set
    GET  /l5/obligations/{ref}/activities      activities anchored to one obligation + the edges it lights
    GET  /l5/edges/{kind}                      implies | operates | mitigates (live, or include_invalidated)
    GET  /l5/scorecard                         published L5 scorecard (gates, junction check, coverage)

Nothing here serves clause text; obligations are referenced by their stable id.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.clhear.db import get_engine
from app.clhear.derived_models import activities as activities_t
from app.clhear.derived_models import blocks, implies, mitigates, obligations, operates, products_services
from app.clhear.l2 import registry as l2_registry
from app.clhear.l4 import predicates as l4_predicates
from app.clhear.l4 import validate as l4_validate
from app.clhear.l5 import map as l5
from app.clhear.l5.models import ACTION_TYPES, SIDES, vocabulary
from app.clhear.platform import record

router = APIRouter(prefix="/l5", tags=["l5"])
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_EDGES = {"implies": implies, "operates": operates, "mitigates": mitigates}


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _clean(row: dict) -> dict:
    out = dict(row)
    for k in ("valid_from", "valid_to", "derived_at", "updated_at"):
        if k in out:
            out[k] = _iso(out[k])
    if out.get("confidence") is not None:
        out["confidence"] = float(out["confidence"])
    return out


def _why_rows(conn, ids: list[str]) -> list[dict]:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return []
    rows = {r["id"]: r for r in conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id.in_(ids))).mappings()}
    return [
        {"id": r["id"], "layer": r["layer"], "subject_ref": r["subject_ref"], "reasoning_summary": r["reasoning_summary"],
         "evidence_refs": r["evidence_refs"], "model_manifest": r["model_manifest"], "skill_version": r["skill_version"],
         "confidence": float(r["confidence"]) if r["confidence"] is not None else None, "agent_id": r["agent_id"],
         "created_at": _iso(r["created_at"])}
        for wid in ids if (r := rows.get(wid)) is not None
    ]


def _facet(items: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for i in items:
        v = i.get(key) or "unspecified"
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def _ob_public(ob: dict) -> dict:
    return {"obligation_id": ob.get("stable_id") or ob["id"], "derivation_key": ob["id"], "title": ob.get("title"),
            "jurisdiction": ob.get("jurisdiction"), "source_key": ob.get("source_key"), "clause_ref": ob.get("clause_ref"),
            "status": ob.get("status"), "obligation_type": ob.get("obligation_type")}


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def l5_browser() -> HTMLResponse:
    """HLD v2 §4.5 L5 UI: activity map per profile, activity catalogue, activity page, scorecard."""
    return HTMLResponse((WEB_DIR / "l5.html").read_text(), headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/vocabulary")
def get_vocabulary() -> dict:
    return vocabulary()


# ----------------------------------------------------------------- activities


@router.get("/activities")
def list_activities(side: str | None = None, action_type: str | None = None, status: str | None = None,
                    q: str | None = None, include_invalidated: bool = False,
                    limit: int = Query(default=200, le=1000)) -> dict:
    if side and side not in SIDES:
        raise HTTPException(status_code=422, detail=f"side must be one of {list(SIDES)}")
    with get_engine().connect() as conn:
        stmt = sa.select(activities_t)
        if not include_invalidated:
            stmt = stmt.where(activities_t.c.valid_to.is_(None))
        rows = [dict(r) for r in conn.execute(stmt.order_by(activities_t.c.side, activities_t.c.name)).mappings()]
        imp = l5._live_edges(conn, implies)
        opr = l5._live_edges(conn, operates)
        mit = l5._live_edges(conn, mitigates)
    for r in rows:
        r["triggers"] = l5._json(r.get("triggers"), [])
    all_rows = rows
    if side:
        rows = [r for r in rows if r["side"] == side]
    if action_type:
        rows = [r for r in rows if r["action_type"] == action_type]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in (r["name"] + " " + r["description"] + " " + r["business_owner"]).lower()]
    imp_by = {}
    for e in imp:
        imp_by.setdefault(e["activity_id"], set()).add(e["product_id"])
    opr_by = {}
    for e in opr:
        opr_by.setdefault(e["activity_id"], set()).add(e["block_id"])
    governs, governed_by = {}, {}
    for e in mit:
        governs.setdefault(e["compliance_activity_id"], set()).add(e["business_activity_id"])
        governed_by.setdefault(e["business_activity_id"], set()).add(e["compliance_activity_id"])
    items = []
    for r in rows[:limit]:
        items.append({**_clean({k: v for k, v in r.items() if k != "triggers"}), "triggers": len(r["triggers"]),
                      "implied_by": len(imp_by.get(r["id"], ())), "operates": len(opr_by.get(r["id"], ())),
                      "governs": len(governs.get(r["id"], ())), "governed_by": len(governed_by.get(r["id"], ()))})
    return {"count": len(rows), "items": items,
            "facets": {"side": _facet(all_rows, "side"), "action_type": _facet(all_rows, "action_type"),
                       "status": _facet(all_rows, "status")}}


def _activity(conn, activity_id: str) -> dict:
    row = conn.execute(sa.select(activities_t).where(activities_t.c.id == activity_id)).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown activity {activity_id}")
    row = dict(row)
    row["triggers"] = l5._json(row.get("triggers"), [])
    return row


@router.get("/activities/{activity_id}")
def get_activity(activity_id: str) -> dict:
    with get_engine().connect() as conn:
        row = _activity(conn, activity_id)
        index = l5._anchor_index(l5._live_obligations(conn))
        products = l5._live_products(conn)
        blocks_by_id = l5._live_blocks(conn)
        acts = {a["id"]: a for a in l5.live_activities(conn)}
        triggers = []
        for t in row["triggers"]:
            resolved = l5.resolve_anchor(t.get("anchor") or {}, index)
            triggers.append({**t, "obligations": [_ob_public(o) for o in resolved], "resolved": bool(resolved)})
        imp = [{**_clean(e), "product": products.get(e["product_id"], {}).get("name", e["product_id"])}
               for e in l5._live_edges(conn, implies) if e["activity_id"] == row["id"]]
        opr = [{**_clean(e), "block": blocks_by_id.get(e["block_id"], {}).get("name", e["block_id"]),
                "block_kind": blocks_by_id.get(e["block_id"], {}).get("kind")}
               for e in l5._live_edges(conn, operates) if e["activity_id"] == row["id"]]
        mit = l5._live_edges(conn, mitigates)
        governs = [{**_clean(e), "activity": acts.get(e["business_activity_id"], {}).get("name")}
                   for e in mit if e["compliance_activity_id"] == row["id"]]
        governed_by = [{**_clean(e), "activity": acts.get(e["compliance_activity_id"], {}).get("name")}
                       for e in mit if e["business_activity_id"] == row["id"]]
        why = _why_rows(conn, [row.get("why_trail_id")] + [e.get("why_trail_id") for e in imp + opr + governs + governed_by])
    return {
        **_clean({k: v for k, v in row.items() if k != "triggers"}),
        "triggers": triggers,
        "obligations": sorted({o["obligation_id"] for t in triggers for o in t["obligations"]}),
        "implied_by": imp, "operates": opr, "governs": governs, "governed_by": governed_by,
        "history": {"version": row.get("version"), "valid_from": _iso(row.get("valid_from")), "valid_to": _iso(row.get("valid_to")),
                    "events": row.get("review") or []},
        "why": why,
        "needs_human": record.needs_human("L5", row.get("confidence")) if row.get("status") not in ("curated",) else False,
    }


@router.get("/activities/{activity_id}/obligations")
def activity_obligations(activity_id: str, limit: int = Query(default=200, le=1000)) -> dict:
    with get_engine().connect() as conn:
        row = _activity(conn, activity_id)
        index = l5._anchor_index(l5._live_obligations(conn))
    seen: dict[str, dict] = {}
    for t in row["triggers"]:
        for o in l5.resolve_anchor(t.get("anchor") or {}, index):
            item = seen.setdefault(o["id"], {**_ob_public(o), "when": [], "methods": []})
            item["when"].append(t.get("when") or {})
            item["methods"].append(t.get("method") or "curated")
    items = sorted(seen.values(), key=lambda i: (i["jurisdiction"] or "", i["source_key"], i["clause_ref"]))
    return {"activity_id": row["id"], "side": row["side"], "count": len(items), "items": items[:limit],
            "by_jurisdiction": _facet(items, "jurisdiction")}


# ----------------------------------------------------------------- activity map


class AttributesIn(BaseModel):
    attributes: dict = Field(default_factory=dict)


def _map_for(conn, attributes: dict) -> dict:
    try:
        applicable = {i["obligation_id"] for i in l4_predicates.obligations_for_attributes(conn, attributes)}
    except sa.exc.OperationalError:
        applicable = set()
    amap = l5.activity_map(conn, attributes, matched_obligations=applicable or None)
    amap["l4_applicable_obligations"] = len(applicable)
    return amap


@router.get("/profiles/{profile_id}/activities")
def profile_activities(profile_id: str) -> dict:
    with get_engine().connect() as conn:
        row = l4_validate.get_profile(conn, profile_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown profile {profile_id}")
        amap = _map_for(conn, row["attributes"])
    return {"profile_id": row["id"], "name": row.get("name"), "attributes": row["attributes"], **amap}


@router.post("/activity-map")
def activity_map_for(body: AttributesIn) -> dict:
    with get_engine().connect() as conn:
        return {"attributes": body.attributes, **_map_for(conn, body.attributes)}


# ----------------------------------------------------------------- obligations / edges


@router.get("/obligations/{ref:path}/activities")
def obligation_activities(ref: str) -> dict:
    with get_engine().connect() as conn:
        oid = l2_registry.resolve_obligation_id(conn, ref)
        if oid is None:
            raise HTTPException(status_code=404, detail=f"unknown obligation {ref}")
        ob = dict(conn.execute(sa.select(obligations).where(obligations.c.id == oid)).mappings().one())
        acts = l5.live_activities(conn)
        stable = ob.get("stable_id") or ob["id"]
        anchored = []
        for a in acts:
            for t in a["triggers"]:
                anc = t.get("anchor") or {}
                if anc.get("source_key") == ob["source_key"] and (not anc.get("refs") or ob["clause_ref"] in anc["refs"]):
                    anchored.append({"activity_id": a["id"], "name": a["name"], "side": a["side"], "action_type": a["action_type"],
                                     "when": t.get("when") or {}, "method": t.get("method") or "curated", "cue": t.get("cue")})
        lit = [_clean(e) for e in l5._live_edges(conn, mitigates) if stable in e["obligation_refs"] or oid in e["obligation_refs"]]
        opr = [_clean(e) for e in l5._live_edges(conn, operates) if stable in e["obligation_refs"] or oid in e["obligation_refs"]]
    return {**_ob_public(ob), "activities": anchored, "mitigates": lit, "operates": opr,
            "counts": {"activities": len(anchored), "mitigates": len(lit), "operates": len(opr)}}


@router.get("/edges/{kind}")
def list_edges(kind: str, activity_id: str | None = None, include_invalidated: bool = False,
               limit: int = Query(default=500, le=5000)) -> dict:
    table = _EDGES.get(kind)
    if table is None:
        raise HTTPException(status_code=404, detail=f"unknown edge kind {kind}; one of {list(_EDGES)}")
    stmt = sa.select(table).order_by(table.c.id)
    if not include_invalidated:
        stmt = stmt.where(table.c.valid_to.is_(None))
    with get_engine().connect() as conn:
        rows = [_clean(r) for r in conn.execute(stmt).mappings()]
        names = {a["id"]: a["name"] for a in l5.live_activities(conn)}
        if kind == "implies":
            products = l5._live_products(conn)
        blocks_by_id = l5._live_blocks(conn) if kind == "operates" else {}
    if activity_id:
        cols = [c for c in ("activity_id", "compliance_activity_id", "business_activity_id") if c in rows[0]] if rows else []
        rows = [r for r in rows if any(r.get(c) == activity_id for c in cols)]
    for r in rows:
        r["obligation_refs"] = l5._json(r.get("obligation_refs"), []) if "obligation_refs" in r else None
        if kind == "implies":
            r["product"] = products.get(r["product_id"], {}).get("name", r["product_id"])
            r["activity"] = names.get(r["activity_id"])
        elif kind == "operates":
            r["block"] = blocks_by_id.get(r["block_id"], {}).get("name", r["block_id"])
            r["activity"] = names.get(r["activity_id"])
        else:
            r["compliance_activity"] = names.get(r["compliance_activity_id"])
            r["business_activity"] = names.get(r["business_activity_id"])
    return {"kind": kind, "count": len(rows), "items": rows[:limit]}


# ----------------------------------------------------------------- scorecard


@router.get("/scorecard")
def l5_scorecard() -> dict:
    from app.clhear.l5.check import check_junction
    from app.clhear.platform.gates import GATE_THRESHOLDS, gate_status

    engine = get_engine()
    junction = check_junction(engine)
    cov = l5.coverage(engine)
    with engine.connect() as conn:
        acts = l5.live_activities(conn)
        by_type: dict[str, dict[str, int]] = {s: {} for s in SIDES}
        for a in acts:
            by_type.setdefault(a["side"], {})[a["action_type"]] = by_type.setdefault(a["side"], {}).get(a["action_type"], 0) + 1
        products = conn.execute(sa.select(sa.func.count()).select_from(products_services).where(products_services.c.valid_to.is_(None))).scalar_one() \
            if sa.inspect(conn).has_table("products_services") else 0
        block_count = conn.execute(sa.select(sa.func.count()).select_from(blocks)).scalar_one()
    return {
        "gate": gate_status(engine, "L5"),
        "thresholds": GATE_THRESHOLDS.get("L5", {}),
        "junction": {k: (v if not isinstance(v, list) else v[:20]) for k, v in junction.items()},
        "coverage": cov,
        "activities_by_action_type": by_type,
        "vocabulary": {s: sorted(ACTION_TYPES[s]) for s in SIDES},
        "inputs": {"products_services": products, "blocks": block_count},
    }
