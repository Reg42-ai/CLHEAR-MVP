"""L4 public API — the profile space (HLD v2 §4.4 / §5).

    GET  /l4/ontology                         the whole live ontology (+ attribute schema, jurisdictions, registers)
    GET  /l4/ontology/{collection}            one collection: licences | products_services | client_types | channels | permits | validity_rules
    GET  /l4/licences/{id}                    one authorisation: register provenance, permits, rules, profiles holding it, why
    POST /l4/profiles/validate                judge an attribute set (errors with codes, warnings, normalised values)
    POST /l4/profiles                         validate + store (422 with the errors when the permutation is impossible)
    GET  /l4/profiles                         stored profiles (status / source / q)
    GET  /l4/profiles/{id}                    one profile: validity, history (re-validations), why-trail
    GET  /l4/profiles/{id}/obligations        obligations whose applies_to edges all match the profile
    GET  /l4/profiles/{id}/similar            similar stored profiles + jurisdictions where the same products are reachable
    POST /l4/builder/next                     guided builder: next question, valid options, blocked options with reasons
    GET  /l4/permutations                     permutation explorer per jurisdiction
    GET  /l4/obligations/{id}/applies-to      the applicability edges of one obligation
    GET  /l4/scorecard                        published L4 scorecard (gates, ontology counts, predicate coverage)

Profiles are declared facts, never inferred; nothing here serves clause text.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.clhear.db import get_engine
from app.clhear.derived_models import applies_to, licences, obligations, permits, products_services, profiles, validity_rules
from app.clhear.l2 import registry as l2_registry
from app.clhear.l4 import builder as l4_builder
from app.clhear.l4 import ontology as l4_ontology
from app.clhear.l4 import predicates as l4_predicates
from app.clhear.l4 import validate as l4_validate
from app.clhear.platform import record

router = APIRouter(prefix="/l4", tags=["l4"])
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _clean(row: dict) -> dict:
    out = dict(row)
    for k in ("valid_from", "valid_to", "derived_at"):
        if k in out:
            out[k] = _iso(out[k])
    if "confidence" in out and out["confidence"] is not None:
        out["confidence"] = float(out["confidence"])
    return out


def _why_rows(conn, ids: list[str]) -> list[dict]:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return []
    rows = {r["id"]: r for r in conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id.in_(ids))).mappings()}
    return [
        {"id": r["id"], "layer": r["layer"], "subject_ref": r["subject_ref"], "reasoning_summary": r["reasoning_summary"],
         "evidence_refs": r["evidence_refs"], "inputs_hash": r["inputs_hash"], "model_manifest": r["model_manifest"],
         "skill_version": r["skill_version"], "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
         "agent_id": r["agent_id"], "created_at": _iso(r["created_at"])}
        for wid in ids if (r := rows.get(wid)) is not None
    ]


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def l4_browser() -> HTMLResponse:
    """HLD v2 §4.4 L4 UI: guided validating builder, ontology explorer,
    permutation explorer, profile page with its obligations, scorecard."""
    return HTMLResponse((WEB_DIR / "l4.html").read_text(), headers={"Cache-Control": "no-cache, must-revalidate"})


# ----------------------------------------------------------------- ontology


@router.get("/ontology")
def get_ontology() -> dict:
    onto = l4_ontology.ontology(get_engine())
    for c in l4_ontology.COLLECTIONS:
        onto[c] = [_clean(r) for r in onto[c]]
    onto["counts"] = {c: len(onto[c]) for c in l4_ontology.COLLECTIONS}
    return onto


@router.get("/ontology/{collection}")
def get_collection(collection: str, q: str | None = None, jurisdiction: str | None = None,
                   include_invalidated: bool = False) -> dict:
    table = l4_ontology._TABLES.get(collection)
    if table is None:
        raise HTTPException(status_code=404, detail=f"unknown collection {collection}; one of {list(l4_ontology.COLLECTIONS)}")
    stmt = sa.select(table)
    if not include_invalidated:
        stmt = stmt.where(table.c.valid_to.is_(None))
    if jurisdiction and "jurisdiction" in table.c:
        stmt = stmt.where(sa.or_(table.c.jurisdiction == jurisdiction.upper(), table.c.jurisdiction == "*"))
    with get_engine().connect() as conn:
        rows = [_clean(r) for r in conn.execute(stmt).mappings()]
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in " ".join(str(v) for v in r.values()).lower()]
    return {"collection": collection, "count": len(rows), "items": rows}


@router.get("/licences/{licence_id}")
def get_licence(licence_id: str) -> dict:
    with get_engine().connect() as conn:
        row = conn.execute(sa.select(licences).where(licences.c.id == licence_id)).mappings().first()
        if row is None:
            lk = l4_ontology.Lookup([dict(r) for r in conn.execute(sa.select(licences).where(licences.c.valid_to.is_(None))).mappings()])
            resolved = lk.resolve(licence_id)
            if resolved is None:
                raise HTTPException(status_code=404, detail=f"unknown licence {licence_id}")
            row = resolved
        row = dict(row)
        perm = [dict(r) for r in conn.execute(
            sa.select(permits.c.product_id, permits.c.basis, products_services.c.name)
            .join(products_services, products_services.c.id == permits.c.product_id)
            .where(permits.c.licence_id == row["id"]).where(permits.c.valid_to.is_(None))).mappings()]
        rules = [_clean(r) for r in conn.execute(sa.select(validity_rules).where(validity_rules.c.valid_to.is_(None))).mappings()
                 if row["name"].lower() in json.dumps(r["rule"]).lower()]
        holders = [{"id": p["id"], "name": p["name"], "status": p["status"]}
                   for p in conn.execute(sa.select(profiles).where(profiles.c.valid_to.is_(None))).mappings()
                   if row["name"].lower() in [str(a).lower() for a in (p["attributes"] or {}).get("authorisations", [])]]
        why = _why_rows(conn, [row.get("why_trail_id")])
    return {**_clean(row), "permits": perm, "validity_rules": rules, "profiles": holders, "why": why,
            "needs_human": record.needs_human("L4", float(row["confidence"]) if row.get("confidence") is not None else None)}


# ----------------------------------------------------------------- profiles


class ProfileIn(BaseModel):
    attributes: dict = Field(default_factory=dict)
    name: str = Field(default="", max_length=200)


@router.post("/profiles/validate")
def validate_profile(body: ProfileIn) -> dict:
    return l4_validate.validate(get_engine(), body.attributes)


@router.post("/profiles", status_code=201)
def create_profile(body: ProfileIn, request: Request) -> dict:
    from app.clhear.accounts import current_user

    user = current_user(request)
    try:
        row = l4_validate.create_profile(get_engine(), body.attributes, name=body.name, source="api" if user else "builder")
    except ValueError as exc:
        detail = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {"errors": [{"message": str(exc)}]}
        raise HTTPException(status_code=422, detail={"message": "impossible permutation: profile not stored", **detail})
    return _clean(row)


@router.get("/profiles")
def list_profiles(status: str | None = None, source: str | None = None, q: str | None = None,
                  limit: int = Query(default=100, le=500)) -> dict:
    stmt = sa.select(profiles).where(profiles.c.valid_to.is_(None)).order_by(profiles.c.id)
    if status:
        stmt = stmt.where(profiles.c.status == status)
    if source:
        stmt = stmt.where(profiles.c.source == source)
    with get_engine().connect() as conn:
        rows = [_clean(r) for r in conn.execute(stmt.limit(limit)).mappings()]
        facets = {
            "statuses": {r[0]: r[1] for r in conn.execute(sa.select(profiles.c.status, sa.func.count()).where(profiles.c.valid_to.is_(None)).group_by(profiles.c.status))},
            "sources": {r[0]: r[1] for r in conn.execute(sa.select(profiles.c.source, sa.func.count()).where(profiles.c.valid_to.is_(None)).group_by(profiles.c.source))},
        }
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in (r["name"] + " " + str(r["attributes"])).lower()]
    return {"count": len(rows), "items": rows, "facets": facets}


def _profile(conn, profile_id: str) -> dict:
    row = l4_validate.get_profile(conn, profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown profile {profile_id}")
    return row


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: str) -> dict:
    with get_engine().connect() as conn:
        row = _profile(conn, profile_id)
        why_ids = [row.get("why_trail_id")] + [e.get("why_trail_id") for e in (row.get("review") or [])]
        why = _why_rows(conn, why_ids)
        items = l4_predicates.obligations_for_attributes(conn, row["attributes"])
    return {
        **_clean(row),
        "history": {"valid_from": _iso(row["valid_from"]), "valid_to": _iso(row["valid_to"]), "version": row["version"],
                    "events": row.get("review") or []},
        "obligations": {"count": len(items), "by_jurisdiction": _by(items, "jurisdiction"), "by_type": _by(items, "obligation_type")},
        "why": why,
        "needs_human": row["status"] != "valid",
    }


def _by(items: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for i in items:
        out[i.get(key) or "unspecified"] = out.get(i.get(key) or "unspecified", 0) + 1
    return out


@router.get("/profiles/{profile_id}/obligations")
def profile_obligations(profile_id: str, jurisdiction: str | None = None, q: str | None = None,
                        limit: int = Query(default=200, le=1000)) -> dict:
    with get_engine().connect() as conn:
        row = _profile(conn, profile_id)
        items = l4_predicates.obligations_for_attributes(conn, row["attributes"])
    if jurisdiction:
        items = [i for i in items if i["jurisdiction"].upper() == jurisdiction.upper()]
    if q:
        needle = q.lower()
        items = [i for i in items if needle in (i["title"] + " " + i["source_key"]).lower()]
    return {"profile_id": row["id"], "attributes": row["attributes"], "count": len(items), "items": items[:limit],
            "by_jurisdiction": _by(items, "jurisdiction"), "by_type": _by(items, "obligation_type")}


@router.get("/profiles/{profile_id}/similar")
def profile_similar(profile_id: str) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        row = _profile(conn, profile_id)
    return {"profile_id": row["id"], **l4_builder.similar_profiles(engine, row["attributes"], exclude_id=row["id"])}


class AttributesIn(BaseModel):
    attributes: dict = Field(default_factory=dict)


@router.post("/builder/next")
def builder_next(body: AttributesIn) -> dict:
    return l4_builder.next_step(get_engine(), body.attributes)


@router.post("/builder/similar")
def builder_similar(body: AttributesIn) -> dict:
    return l4_builder.similar_profiles(get_engine(), body.attributes)


@router.get("/permutations")
def permutations(jurisdiction: str | None = None) -> dict:
    return l4_builder.permutations(get_engine(), [jurisdiction] if jurisdiction else None)


# ----------------------------------------------------------------- obligations


@router.get("/obligations/{ref:path}/applies-to")
def obligation_applies_to(ref: str, include_invalidated: bool = False) -> dict:
    with get_engine().connect() as conn:
        oid = l2_registry.resolve_obligation_id(conn, ref)
        if oid is None:
            raise HTTPException(status_code=404, detail=f"unknown obligation {ref}")
        ob = conn.execute(sa.select(obligations).where(obligations.c.id == oid)).mappings().one()
        stmt = sa.select(applies_to).where(applies_to.c.obligation_id == oid).order_by(applies_to.c.id)
        if not include_invalidated:
            stmt = stmt.where(applies_to.c.valid_to.is_(None))
        edges = [_clean(r) for r in conn.execute(stmt).mappings()]
        why = _why_rows(conn, [e["why_trail_id"] for e in edges])
    return {"obligation_id": ob["stable_id"] or ob["id"], "derivation_key": ob["id"], "title": ob["title"],
            "jurisdiction": ob["jurisdiction"], "count": len(edges), "edges": edges,
            "applies_when": {k: v for e in edges if e["valid_to"] is None for k, v in (e["predicate"] or {}).items()}, "why": why}


# ----------------------------------------------------------------- scorecard


@router.get("/scorecard")
def l4_scorecard() -> dict:
    from app.clhear.platform.gates import GATE_THRESHOLDS, gate_status

    engine = get_engine()
    with engine.connect() as conn:
        lic_by_jur = {r[0]: r[1] for r in conn.execute(
            sa.select(licences.c.jurisdiction, sa.func.count()).where(licences.c.valid_to.is_(None)).group_by(licences.c.jurisdiction))}
        lic_by_status = {r[0]: r[1] for r in conn.execute(
            sa.select(licences.c.status, sa.func.count()).where(licences.c.valid_to.is_(None)).group_by(licences.c.status))}
        counts = {c: conn.execute(sa.select(sa.func.count()).select_from(t).where(t.c.valid_to.is_(None))).scalar_one()
                  for c, t in l4_ontology._TABLES.items()}
        prof = {r[0]: r[1] for r in conn.execute(
            sa.select(profiles.c.status, sa.func.count()).where(profiles.c.valid_to.is_(None)).group_by(profiles.c.status))}
    snap = l4_ontology.snapshot()
    return {
        "gate": gate_status(engine, "L4"),
        "thresholds": GATE_THRESHOLDS.get("L4", {}),
        "ontology": {"version": l4_ontology.snapshot_version(snap), "counts": counts, "licences_by_jurisdiction": lic_by_jur,
                     "licences_by_status": lic_by_status, "registers": snap["registers"]},
        "profiles": prof,
        "applicability": l4_predicates.coverage(engine),
    }
