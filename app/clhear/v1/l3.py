"""L3 public API — the building-block catalogue (HLD v2 §4.3 / §5).

    GET  /l3/kinds                              the eight kinds and their fixed characteristic schemas
    GET  /l3/blocks                             catalogue: kind / status / q / canonical_only
    GET  /l3/blocks/{id}                        one block: characteristics (backed / not specified / unbacked),
                                                backing obligations with rationale spans, needed-by profiles,
                                                merged duplicates, history, why-trail
    GET  /l3/blocks/{id}/history                requires-edge and characteristic history (bi-temporal, I2)
    GET  /l3/blocks/{id}/why                    why-trail chain (I3)
    POST /l3/blocks/{id}/modification-requests  "request a modification" -> proposals (l3_modification)
    GET  /l3/obligations/{id}/blocks            what an obligation requires (live edges, canonical blocks)
    GET  /l3/scorecard                          published L3 scorecard (gates, completeness, characteristics, reuse)

`{id}` is the block id (BLK-000001, or a curated id such as BLK-AML-CDD).
Block text is derived (names, purposes, characteristic values are CLHEAR
determinations); the backing obligation's determination is served, verbatim
clause text is not (that lives behind /l2 with its rights check, I8).
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.clhear.db import get_engine
from app.clhear.derived_models import blocks, blueprints, characteristics, l3_kinds, obligations, requires
from app.clhear.l2 import registry as l2_registry
from app.clhear.l3.kinds import KINDS, kinds_catalog, required_fields
from app.clhear.platform import record

router = APIRouter(prefix="/l3", tags=["l3"])
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

LIVE_OBLIGATION = ("derived", "validated")


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _num(value):
    return float(value) if value is not None else None


def _block(conn, block_id: str):
    row = conn.execute(sa.select(blocks).where(blocks.c.id == block_id)).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown block {block_id}")
    return row


def _summary(b, edge_counts: dict[str, int] | None = None) -> dict:
    return {
        "id": b["id"],
        "kind": b["kind"],
        "name": b["name"],
        "purpose": b["purpose"] or b["description"],
        "description": b["description"],
        "capability": b["capability"],
        "status": b["status"],
        "canonical_id": b["canonical_id"],
        "evidence_artifacts": b["evidence_artifacts"] or [],
        "implements_controls": b["implements_controls"] or [],
        "satisfies": b["satisfies"] or [],
        "obligations": (edge_counts or {}).get(b["id"], 0),
        "confidence": _num(b["confidence"]),
        "valid_from": _iso(b["valid_from"]),
        "valid_to": _iso(b["valid_to"]),
    }


def _edge_counts(conn) -> dict[str, int]:
    return {
        r[0]: r[1]
        for r in conn.execute(
            sa.select(requires.c.block_id, sa.func.count())
            .where(requires.c.valid_to.is_(None))
            .group_by(requires.c.block_id)
        )
    }


def _ob_ref(ob) -> str:
    return ob["stable_id"] or ob["id"]


def _backing_obligations(conn, block_id: str, live_only: bool = True) -> list[dict]:
    q = sa.select(requires, obligations).join(obligations, obligations.c.id == requires.c.obligation_id).where(requires.c.block_id == block_id)
    if live_only:
        q = q.where(requires.c.valid_to.is_(None))
    out: list[dict] = []
    for r in conn.execute(q.order_by(requires.c.valid_to.is_(None).desc(), requires.c.id)).mappings():
        text = r["determination"] or r["statement"] or ""
        span = None
        if r["rationale_start"] is not None and r["rationale_end"] is not None:
            span = {"start": r["rationale_start"], "end": r["rationale_end"], "text": text[r["rationale_start"]:r["rationale_end"]]}
        out.append({
            "requires_id": r[requires.c.id],
            "obligation_id": r["stable_id"] or r[requires.c.obligation_id],
            "derivation_key": r[requires.c.obligation_id],
            "title": r["title"],
            "determination": text,
            "jurisdiction": r["jurisdiction"],
            "regulator": r["regulator"],
            "source_key": r["source_key"],
            "clause_ref": r["clause_ref"],
            "obligation_status": r[obligations.c.status],
            "rationale": r["rationale"],
            "span": span,
            "method": r[requires.c.method],
            "basis_current": r["obligation_text_hash"] == r["text_hash"],
            "live": r[requires.c.valid_to] is None,
            "valid_from": _iso(r[requires.c.valid_from]),
            "valid_to": _iso(r[requires.c.valid_to]),
            "why_trail_id": r[requires.c.why_trail_id],
        })
    return out


def _characteristics(conn, block) -> list[dict]:
    rows = {
        r["key"]: r
        for r in conn.execute(
            sa.select(characteristics).where(characteristics.c.block_id == block["id"]).where(characteristics.c.valid_to.is_(None))
        ).mappings()
    }
    out: list[dict] = []
    for key in required_fields(block["kind"]):
        r = rows.get(key)
        if r is None:
            out.append({"key": key, "value": "", "status": "missing", "backing_obligation_id": None, "backing_span": "", "method": ""})
            continue
        backing = None
        if r["backing_obligation_id"]:
            ob = conn.execute(sa.select(obligations.c.stable_id).where(obligations.c.id == r["backing_obligation_id"])).first()
            backing = (ob.stable_id if ob and ob.stable_id else r["backing_obligation_id"])
        out.append({
            "key": key,
            "value": r["value"],
            "status": r["status"],
            "backing_obligation_id": backing,
            "backing_derivation_key": r["backing_obligation_id"],
            "backing_span": r["backing_span"],
            "method": r["method"],
            "confidence": _num(r["confidence"]),
            "why_trail_id": r["why_trail_id"],
        })
    return out


def _needed_by(conn, block_id: str, limit: int = 20) -> list[dict]:
    """Blueprints (L6) whose result selected this block: the profiles that need it."""
    out: list[dict] = []
    for bp in conn.execute(sa.select(blueprints).order_by(blueprints.c.id.desc()).limit(500)).mappings():
        selected = {b.get("id") for b in (bp["result"] or {}).get("blocks", [])}
        if block_id in selected:
            attrs = (bp["profile"] or {}).get("attributes", {})
            out.append({"blueprint_id": bp["id"], "requested_by": bp["requested_by"], "release": bp["release"],
                        "profile_attributes": attrs, "created_at": _iso(bp["created_at"])})
            if len(out) >= limit:
                break
    return out


def _history(conn, block) -> dict:
    edges = _backing_obligations(conn, block["id"], live_only=False)
    chars = [
        {"key": c["key"], "value": c["value"], "status": c["status"], "backing_obligation_id": c["backing_obligation_id"],
         "method": c["method"], "valid_from": _iso(c["valid_from"]), "valid_to": _iso(c["valid_to"]),
         "review": c["review"] or [], "why_trail_id": c["why_trail_id"]}
        for c in conn.execute(sa.select(characteristics).where(characteristics.c.block_id == block["id"]).order_by(characteristics.c.id)).mappings()
    ]
    merged_into = None
    if block["canonical_id"]:
        merged_into = block["canonical_id"]
    merged_from = [r[0] for r in conn.execute(sa.select(blocks.c.id).where(blocks.c.canonical_id == block["id"]))]
    return {
        "valid_from": _iso(block["valid_from"]),
        "valid_to": _iso(block["valid_to"]),
        "merged_into": merged_into,
        "merged_from": merged_from,
        "requires": edges,
        "characteristics": chars,
    }


def _why_chain(conn, block) -> list[dict]:
    ids: list[str] = []
    if block["why_trail_id"]:
        ids.append(block["why_trail_id"])
    for table in (requires, characteristics):
        for r in conn.execute(sa.select(table.c.why_trail_id).where(table.c.block_id == block["id"])):
            if r[0] and r[0] not in ids:
                ids.append(r[0])
    if not ids:
        return []
    rows = conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id.in_(ids))).mappings().all()
    by_id = {r["id"]: r for r in rows}
    return [
        {
            "id": r["id"], "layer": r["layer"], "subject_ref": r["subject_ref"], "reasoning_summary": r["reasoning_summary"],
            "evidence_refs": r["evidence_refs"], "inputs_hash": r["inputs_hash"], "model_manifest": r["model_manifest"],
            "skill_version": r["skill_version"], "confidence": _num(r["confidence"]), "agent_id": r["agent_id"],
            "created_at": _iso(r["created_at"]),
        }
        for wid in ids
        if (r := by_id.get(wid)) is not None
    ]


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def l3_browser() -> HTMLResponse:
    """HLD v2 §4.3 L3 UI: catalogue by kind, block page with characteristics,
    backing obligations (rationale spans), needed-by, history, why-trail."""
    return HTMLResponse((WEB_DIR / "l3.html").read_text(), headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/kinds")
def list_kinds() -> dict:
    with get_engine().connect() as conn:
        rows = {r["kind"]: r for r in conn.execute(sa.select(l3_kinds)).mappings()}
        counts = {r[0]: r[1] for r in conn.execute(
            sa.select(blocks.c.kind, sa.func.count()).where(blocks.c.canonical_id.is_(None)).group_by(blocks.c.kind))}
    items = []
    for k in kinds_catalog():
        row = rows.get(k["kind"])
        items.append({**k, "description": (row["description"] if row else k.get("description", "")), "blocks": counts.get(k["kind"], 0)})
    return {"count": len(items), "items": items}


@router.get("/blocks")
def list_blocks(
    kind: str | None = Query(default=None),
    status: str | None = Query(default=None, description="curated | derived | validated | ..."),
    q: str | None = Query(default=None, description="substring on name / purpose"),
    canonical_only: bool = Query(default=True, description="hide blocks merged into a canonical one"),
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    if kind and kind not in KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {KINDS}")
    query = sa.select(blocks)
    if kind:
        query = query.where(blocks.c.kind == kind)
    if status:
        query = query.where(blocks.c.status == status)
    if q:
        like = f"%{q.lower()}%"
        query = query.where(sa.or_(sa.func.lower(blocks.c.name).like(like), sa.func.lower(blocks.c.purpose).like(like),
                                   sa.func.lower(blocks.c.description).like(like)))
    if canonical_only:
        query = query.where(blocks.c.canonical_id.is_(None))
    with get_engine().connect() as conn:
        total = conn.execute(sa.select(sa.func.count()).select_from(query.subquery())).scalar_one()
        rows = conn.execute(query.order_by(blocks.c.kind, blocks.c.name).limit(limit).offset(offset)).mappings().all()
        counts = _edge_counts(conn)
        facets = {
            "kinds": list(KINDS),
            "statuses": [r[0] for r in conn.execute(sa.select(blocks.c.status).distinct().order_by(blocks.c.status))],
        }
    return {"total": total, "limit": limit, "offset": offset, "facets": facets, "items": [_summary(r, counts) for r in rows]}


@router.get("/blocks/{block_id}/history")
def block_history(block_id: str) -> dict:
    with get_engine().connect() as conn:
        b = _block(conn, block_id)
        return {"id": b["id"], **_history(conn, b)}


@router.get("/blocks/{block_id}/why")
def block_why(block_id: str) -> dict:
    with get_engine().connect() as conn:
        b = _block(conn, block_id)
        return {"id": b["id"], "why": _why_chain(conn, b)}


@router.get("/blocks/{block_id}")
def block_detail(block_id: str) -> dict:
    with get_engine().connect() as conn:
        b = _block(conn, block_id)
        chars = _characteristics(conn, b)
        backing = _backing_obligations(conn, b["id"])
        duplicates = [_summary(r) for r in conn.execute(sa.select(blocks).where(blocks.c.canonical_id == b["id"])).mappings()]
        canonical = _summary(_block(conn, b["canonical_id"])) if b["canonical_id"] else None
        filled = sum(1 for c in chars if c["status"] in ("backed", "not_specified"))
        conf = _num(b["confidence"])
        return {
            **_summary(b, {b["id"]: len(backing)}),
            "characteristics": chars,
            "characteristics_summary": {"required": len(chars), "filled": filled,
                                        "backed": sum(1 for c in chars if c["status"] == "backed"),
                                        "not_specified": sum(1 for c in chars if c["status"] == "not_specified"),
                                        "unbacked": sum(1 for c in chars if c["status"] == "unbacked"),
                                        "missing": sum(1 for c in chars if c["status"] == "missing")},
            "backing_obligations": backing,
            "needed_by": _needed_by(conn, b["id"]),
            "canonical": canonical,
            "merged_duplicates": duplicates,
            "history": _history(conn, b),
            "why": _why_chain(conn, b),
            "needs_human": record.needs_human("L3", conf) if conf is not None else b["status"] == "derived",
        }


@router.get("/obligations/{ref:path}/blocks")
def obligation_blocks(ref: str) -> dict:
    """What an obligation requires: its live requires edges resolved to canonical blocks."""
    with get_engine().connect() as conn:
        oid = l2_registry.resolve_obligation_id(conn, ref)
        if oid is None:
            raise HTTPException(status_code=404, detail=f"unknown obligation {ref}")
        ob = conn.execute(sa.select(obligations).where(obligations.c.id == oid)).mappings().one()
        edges = conn.execute(sa.select(requires).where(requires.c.obligation_id == oid).where(requires.c.valid_to.is_(None))).mappings().all()
        items = []
        for e in edges:
            b = conn.execute(sa.select(blocks).where(blocks.c.id == e["block_id"])).mappings().first()
            if b is None:
                continue
            canonical = b
            while canonical["canonical_id"]:
                nxt = conn.execute(sa.select(blocks).where(blocks.c.id == canonical["canonical_id"])).mappings().first()
                if nxt is None:
                    break
                canonical = nxt
            items.append({
                "requires_id": e["id"], "method": e["method"], "rationale": e["rationale"],
                "span": ({"start": e["rationale_start"], "end": e["rationale_end"]}
                         if e["rationale_start"] is not None and e["rationale_end"] is not None else None),
                "block": _summary(canonical),
                "linked_block_id": b["id"],
                "why_trail_id": e["why_trail_id"],
            })
    return {"obligation_id": _ob_ref(ob), "derivation_key": ob["id"], "count": len(items), "items": items}


class ModificationRequest(BaseModel):
    field: str = Field(default="name", description="name | purpose | kind | status | characteristic:<key> | requires:<obligation id>")
    proposed_value: str
    rationale: str = Field(min_length=10)
    requester: str = Field(default="anonymous", max_length=200)


@router.post("/blocks/{block_id}/modification-requests", status_code=201)
def request_modification(block_id: str, body: ModificationRequest, request: Request) -> dict:
    """Community / member 'request a modification' (HLD v2 §7.1): a proposal the
    approval console decides on; the catalogue row is never edited directly."""
    from app.clhear.accounts import current_user
    from app.clhear.platform.proposals import create_proposal

    base = {"name", "purpose", "kind", "status"}
    field = body.field
    if field not in base and not field.startswith(("characteristic:", "requires:")):
        raise HTTPException(status_code=422, detail=f"field must be one of {sorted(base)} or characteristic:<key> / requires:<obligation id>")
    if field == "kind" and body.proposed_value not in KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {KINDS}")
    user = current_user(request)
    requester = f"user:{user['id']}" if user else body.requester
    engine = get_engine()
    with engine.begin() as conn:
        b = _block(conn, block_id)
        if field.startswith("characteristic:"):
            key = field.split(":", 1)[1]
            if key not in required_fields(b["kind"]):
                raise HTTPException(status_code=422, detail=f"{key} is not a characteristic of kind {b['kind']}")
            row = conn.execute(sa.select(characteristics.c.value).where(characteristics.c.block_id == b["id"])
                               .where(characteristics.c.key == key).where(characteristics.c.valid_to.is_(None))).first()
            current = row.value if row else None
        elif field.startswith("requires:"):
            current = None
        else:
            current = b[field]
        proposal_id = create_proposal(
            conn,
            layer="L3",
            kind="l3_modification",
            subject_ref=b["id"],
            draft={"block_id": b["id"], "kind": b["kind"], "field": field, "current_value": current,
                   "proposed_value": body.proposed_value, "requester": requester},
            rationale=body.rationale,
            confidence=None,
        )
    return {"proposal_id": proposal_id, "block_id": b["id"], "status": "proposed"}


@router.get("/scorecard")
def l3_scorecard() -> dict:
    """The published L3 scorecard: gate status + thresholds, catalogue counts by
    kind / status, decomposition completeness, characteristic fill, reuse."""
    from app.clhear.l3 import characterize as l3_characterize
    from app.clhear.l3 import decompose as l3_decompose
    from app.clhear.l3 import harmonize as l3_harmonize
    from app.clhear.platform.gates import GATE_THRESHOLDS, gate_status

    engine = get_engine()
    with engine.connect() as conn:
        by_kind = {r[0]: r[1] for r in conn.execute(
            sa.select(blocks.c.kind, sa.func.count()).where(blocks.c.canonical_id.is_(None)).group_by(blocks.c.kind))}
        by_status = {r[0]: r[1] for r in conn.execute(sa.select(blocks.c.status, sa.func.count()).group_by(blocks.c.status))}
        merged = conn.execute(sa.select(sa.func.count()).select_from(blocks).where(blocks.c.canonical_id.isnot(None))).scalar_one()
        by_method = {r[0]: r[1] for r in conn.execute(
            sa.select(requires.c.method, sa.func.count()).where(requires.c.valid_to.is_(None)).group_by(requires.c.method))}
    return {
        "gate": gate_status(engine, "L3"),
        "thresholds": GATE_THRESHOLDS.get("L3", {}),
        "blocks": {"by_kind": by_kind, "by_status": by_status, "canonical": sum(by_kind.values()), "merged": merged},
        "requires_by_method": by_method,
        "completeness": l3_decompose.completeness(engine),
        "characteristics": l3_characterize.completeness(engine),
        "reuse": l3_harmonize.reuse_ratio(engine),
    }
