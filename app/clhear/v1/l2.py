"""L2 public API — the obligation registry (HLD v2 §4.2 / §5).

    GET  /l2/obligations                       browse: jurisdiction / regulator / source / type / status / q
    GET  /l2/obligations/{id}                  one obligation: structured fields, sources + spans,
                                               equivalents, history, reviews, why-trail
    GET  /l2/obligations/{id}/history          version + change history (bi-temporal, I2)
    GET  /l2/obligations/{id}/why              why-trail chain (I3)
    POST /l2/obligations/{id}/modification-requests   "request a modification" -> proposals (l2_modification)
    POST /l2/obligations/{id}/reviews          expert verdict (correct | incorrect | unsure)
    GET  /l2/changes?since=YYYY-MM-DD          registry change feed (added / updated / revoked) with effective dates
    GET  /l2/equivalences                      cross-jurisdiction equivalence edges
    GET  /l2/compare?jurisdictions=uk,eu&q=    cross-jurisdiction comparison by concept / equivalence
    GET  /l2/scorecard                         published L2 scorecard (gates, counts, precision, duplicates)

`{id}` accepts the public stable id (OBL-000001) or the derivation key
(OBL:<source>#<ref>). Rights discipline (I8): the obligation's own
determination is derived text and always served; verbatim clause text is
served only when the basis source is republishable, otherwise spans + hashes.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.clhear.db import get_engine
from app.clhear.derived_models import (
    OBLIGATION_TYPES,
    asserts,
    concept_members,
    concepts,
    equivalences,
    l2_change_events,
    obligation_reviews,
    obligations,
    supersessions,
)
from app.clhear.l1 import rights as l1_rights
from app.clhear.l1.models import clauses, source_versions, sources
from app.clhear.l2 import registry
from app.clhear.platform import record

router = APIRouter(prefix="/l2", tags=["l2"])
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

LIVE = ("derived", "validated")


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _num(value):
    return float(value) if value is not None else None


def _resolve(conn, ref: str):
    oid = registry.resolve_obligation_id(conn, ref)
    if oid is None:
        raise HTTPException(status_code=404, detail=f"unknown obligation {ref}")
    return conn.execute(sa.select(obligations).where(obligations.c.id == oid)).mappings().one()


def _summary(ob) -> dict:
    return {
        "id": ob["stable_id"] or ob["id"],
        "derivation_key": ob["id"],
        "canonical_id": ob["canonical_id"],
        "status": ob["status"],
        "title": ob["title"],
        "determination": ob["determination"] or ob["statement"],
        "subject": ob["subject"],
        "action": ob["action"],
        "condition": ob["condition"],
        "object": ob["object"],
        "modality": ob["modality"],
        "obligation_type": ob["obligation_type"],
        "jurisdiction": ob["jurisdiction"],
        "jurisdictions": ob["jurisdictions"] or ([ob["jurisdiction"]] if ob["jurisdiction"] else []),
        "regulator": ob["regulator"],
        "source_key": ob["source_key"],
        "clause_ref": ob["clause_ref"],
        "effective_from": _iso(ob["effective_from"]),
        "effective_to": _iso(ob["effective_to"]),
        "confidence": _num(ob["confidence"]),
        "review_confidence": _num(ob["review_confidence"]),
        "version": ob["version"],
        "method": ob["method"],
        "derived_at": _iso(ob["derived_at"]),
    }


def _sources_for(conn, ob) -> list[dict]:
    """Basis clauses (asserts edges) with spans; verbatim text only when republishable."""
    rows = conn.execute(
        sa.select(asserts).where(asserts.c.obligation_id == ob["id"]).order_by(asserts.c.valid_to.is_(None).desc(), asserts.c.id)
    ).mappings().all()
    out: list[dict] = []
    for a in rows:
        clause = None
        if a["clause_id"] is not None:
            clause = conn.execute(
                sa.select(clauses, source_versions.c.version_label, source_versions.c.as_of_date, sources.c.rights_basis,
                          sources.c.instrument, sources.c.short_name, sources.c.canonical_url)
                .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
                .join(sources, sources.c.id == source_versions.c.source_id)
                .where(clauses.c.id == a["clause_id"])
            ).mappings().first()
        republish = bool(clause) and l1_rights.republishable(clause["rights_basis"]) and bool(clause["public_ok"])
        text = clause["text"] if republish else None
        span = None
        if a["span_start"] is not None and a["span_end"] is not None:
            span = {"start": a["span_start"], "end": a["span_end"]}
            if text:
                span["text"] = text[a["span_start"]:a["span_end"]]
        out.append({
            "assert_id": a["id"],
            "clause_id": a["clause_id"],
            "source_key": a["source_key"],
            "clause_ref": a["clause_ref"],
            "instrument": (clause["instrument"] or clause["short_name"]) if clause else None,
            "source_url": clause["canonical_url"] if clause else None,
            "version": clause["version_label"] if clause else None,
            "as_of_date": _iso(clause["as_of_date"]) if clause else None,
            "strength": a["strength"],
            "text_hash": a["text_hash"],
            "current_text_hash": clause["text_hash"] if clause else None,
            "basis_current": bool(clause) and clause["text_hash"] == a["text_hash"],
            "span": span,
            "text": text,
            "rights_basis": clause["rights_basis"] if clause else None,
            "text_withheld_reason": None if republish or not clause else f"rights basis {clause['rights_basis']} — derived facts only",
            "live": a["valid_to"] is None,
            "valid_from": _iso(a["valid_from"]),
            "valid_to": _iso(a["valid_to"]),
        })
    return out


def _equivalents(conn, ob) -> list[dict]:
    rows = conn.execute(
        sa.select(equivalences)
        .where(sa.or_(equivalences.c.obligation_a == ob["id"], equivalences.c.obligation_b == ob["id"]))
        .where(equivalences.c.valid_to.is_(None))
    ).mappings().all()
    out: list[dict] = []
    for e in rows:
        other_id = e["obligation_b"] if e["obligation_a"] == ob["id"] else e["obligation_a"]
        other = conn.execute(sa.select(obligations).where(obligations.c.id == other_id)).mappings().first()
        if other is None:
            continue
        out.append({
            "equivalence_id": e["id"],
            "basis": e["basis"],
            "concept_id": e["concept_id"],
            "similarity": _num(e["similarity"]),
            "method": e["method"],
            "obligation": _summary(other),
        })
    return out


def _history(conn, ob) -> dict:
    changes = [
        {
            "change_id": c["id"],
            "kind": c["kind"],
            "source_key": c["source_key"],
            "cause_clause_ids": c["cause_clause_ids"],
            "cause_l1_change_event_id": c["cause_l1_change_event_id"],
            "old_text_hash": c["old_text_hash"],
            "new_text_hash": c["new_text_hash"],
            "effective_date": _iso(c["effective_date"]),
            "effective_date_basis": c["effective_date_basis"],
            "detected_at": _iso(c["detected_at"]),
            "detail": c["detail"],
            "why_trail_id": c["why_trail_id"],
        }
        for c in conn.execute(
            sa.select(l2_change_events).where(l2_change_events.c.obligation_id == ob["id"]).order_by(l2_change_events.c.id)
        ).mappings()
    ]
    sup = conn.execute(
        sa.select(supersessions).where(
            sa.or_(supersessions.c.old_obligation_id == ob["id"], supersessions.c.new_obligation_id == ob["id"])
        )
    ).mappings().all()
    supers = []
    for s in sup:
        other_id = s["new_obligation_id"] if s["old_obligation_id"] == ob["id"] else s["old_obligation_id"]
        other = conn.execute(sa.select(obligations.c.stable_id, obligations.c.id).where(obligations.c.id == other_id)).first()
        supers.append({
            "supersession_id": s["id"],
            "direction": "superseded_by" if s["old_obligation_id"] == ob["id"] else "supersedes",
            "obligation_id": (other.stable_id or other.id) if other else other_id,
            "effective_date": _iso(s["effective_date"]),
            "cause_change_event_id": s["cause_change_event_id"],
            "note": s["note"],
        })
    reviews = [
        {"reviewer_kind": r["reviewer_kind"], "reviewer": r["reviewer"], "verdict": r["verdict"],
         "confidence": _num(r["confidence"]), "text_hash": r["text_hash"], "notes": r["notes"],
         "reviewed_at": _iso(r["reviewed_at"])}
        for r in conn.execute(
            sa.select(obligation_reviews).where(obligation_reviews.c.obligation_id == ob["id"]).order_by(obligation_reviews.c.id)
        ).mappings()
    ]
    return {
        "version": ob["version"],
        "valid_from": _iso(ob["valid_from"]),
        "valid_to": _iso(ob["valid_to"]),
        "effective_from": _iso(ob["effective_from"]),
        "effective_to": _iso(ob["effective_to"]),
        "changes": changes,
        "supersessions": supers,
        "reviews": reviews,
    }


def _why_chain(conn, ob) -> list[dict]:
    ids: list[str] = []
    if ob["why_trail_id"]:
        ids.append(ob["why_trail_id"])
    for c in conn.execute(
        sa.select(l2_change_events.c.why_trail_id).where(l2_change_events.c.obligation_id == ob["id"])
    ):
        if c[0] and c[0] not in ids:
            ids.append(c[0])
    if not ids:
        return []
    rows = conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id.in_(ids))).mappings().all()
    by_id = {r["id"]: r for r in rows}
    return [
        {
            "id": r["id"],
            "layer": r["layer"],
            "subject_ref": r["subject_ref"],
            "reasoning_summary": r["reasoning_summary"],
            "evidence_refs": r["evidence_refs"],
            "inputs_hash": r["inputs_hash"],
            "model_manifest": r["model_manifest"],
            "skill_version": r["skill_version"],
            "confidence": _num(r["confidence"]),
            "agent_id": r["agent_id"],
            "created_at": _iso(r["created_at"]),
        }
        for wid in ids
        if (r := by_id.get(wid)) is not None
    ]


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def l2_browser() -> HTMLResponse:
    """HLD v2 §4.2 L2 UI: registry browser, obligation page with highlighted
    spans, equivalents, history, why-trail, request-a-modification, compare."""
    return HTMLResponse((WEB_DIR / "l2.html").read_text(), headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/obligations")
def list_obligations(
    jurisdiction: str | None = Query(default=None),
    regulator: str | None = Query(default=None),
    source: str | None = Query(default=None, description="source key"),
    obligation_type: str | None = Query(default=None),
    status: str = Query(default="live", description="live | stale | all"),
    q: str | None = Query(default=None, description="substring match on determination / title"),
    canonical_only: bool = Query(default=False, description="hide merged duplicates"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    query = sa.select(obligations)
    if status == "live":
        query = query.where(obligations.c.status.in_(LIVE))
    elif status != "all":
        query = query.where(obligations.c.status == status)
    if jurisdiction:
        query = query.where(sa.func.lower(obligations.c.jurisdiction) == jurisdiction.lower())
    if regulator:
        query = query.where(sa.func.lower(obligations.c.regulator) == regulator.lower())
    if source:
        query = query.where(obligations.c.source_key == source)
    if obligation_type:
        query = query.where(obligations.c.obligation_type == obligation_type)
    if q:
        like = f"%{q.lower()}%"
        query = query.where(sa.or_(sa.func.lower(obligations.c.determination).like(like),
                                   sa.func.lower(obligations.c.title).like(like),
                                   sa.func.lower(obligations.c.statement).like(like)))
    if canonical_only:
        query = query.where(sa.or_(obligations.c.canonical_id.is_(None),
                                   obligations.c.canonical_id == obligations.c.stable_id))
    with get_engine().connect() as conn:
        total = conn.execute(sa.select(sa.func.count()).select_from(query.subquery())).scalar_one()
        rows = conn.execute(query.order_by(obligations.c.stable_id, obligations.c.id).limit(limit).offset(offset)).mappings().all()
        facets = {
            "jurisdictions": [r[0] for r in conn.execute(sa.select(obligations.c.jurisdiction).where(obligations.c.status.in_(LIVE)).distinct().order_by(obligations.c.jurisdiction)) if r[0]],
            "regulators": [r[0] for r in conn.execute(sa.select(obligations.c.regulator).where(obligations.c.status.in_(LIVE)).distinct().order_by(obligations.c.regulator)) if r[0]],
            "types": list(OBLIGATION_TYPES),
        }
    return {"total": total, "limit": limit, "offset": offset, "facets": facets, "items": [_summary(r) for r in rows]}


@router.get("/obligations/{ref:path}/history")
def obligation_history(ref: str) -> dict:
    with get_engine().connect() as conn:
        ob = _resolve(conn, ref)
        return {"id": ob["stable_id"] or ob["id"], **_history(conn, ob)}


@router.get("/obligations/{ref:path}/why")
def obligation_why(ref: str) -> dict:
    with get_engine().connect() as conn:
        ob = _resolve(conn, ref)
        return {"id": ob["stable_id"] or ob["id"], "why": _why_chain(conn, ob)}


@router.get("/obligations/{ref:path}")
def obligation_detail(ref: str) -> dict:
    with get_engine().connect() as conn:
        ob = _resolve(conn, ref)
        concept_rows = conn.execute(
            sa.select(concepts.c.id, concepts.c.name, concepts.c.canonical_statement)
            .join(concept_members, concept_members.c.concept_id == concepts.c.id)
            .where(concept_members.c.obligation_id == ob["id"])
        ).mappings().all()
        duplicates = []
        if ob["canonical_id"]:
            duplicates = [
                _summary(r) for r in conn.execute(
                    sa.select(obligations)
                    .where(obligations.c.canonical_id == ob["canonical_id"])
                    .where(obligations.c.id != ob["id"])
                    .where(obligations.c.status.in_(LIVE))
                ).mappings()
            ]
        return {
            **_summary(ob),
            "sources": _sources_for(conn, ob),
            "equivalents": _equivalents(conn, ob),
            "concepts": [dict(c) for c in concept_rows],
            "merged_with": duplicates,
            "history": _history(conn, ob),
            "why": _why_chain(conn, ob),
            "needs_human": record.needs_human("L2", _num(ob["review_confidence"]) if ob["review_confidence"] is not None else _num(ob["confidence"])),
        }


class ModificationRequest(BaseModel):
    field: str = Field(default="determination", description="determination | subject | action | condition | object | obligation_type | effective_from | effective_to | status")
    proposed_value: str
    rationale: str = Field(min_length=10)
    requester: str = Field(default="anonymous", max_length=200)


@router.post("/obligations/{ref:path}/modification-requests", status_code=201)
def request_modification(ref: str, body: ModificationRequest, request: Request) -> dict:
    """Community / member 'request a modification' (HLD v2 §7.1): a proposal the
    approval console decides on; the registry row is never edited directly."""
    from app.clhear.accounts import current_user
    from app.clhear.platform.proposals import create_proposal

    allowed = {"determination", "subject", "action", "condition", "object", "obligation_type",
               "effective_from", "effective_to", "status"}
    if body.field not in allowed:
        raise HTTPException(status_code=422, detail=f"field must be one of {sorted(allowed)}")
    if body.field == "obligation_type" and body.proposed_value not in OBLIGATION_TYPES:
        raise HTTPException(status_code=422, detail=f"obligation_type must be one of {OBLIGATION_TYPES}")
    user = current_user(request)
    requester = f"user:{user['id']}" if user else body.requester
    engine = get_engine()
    with engine.begin() as conn:
        ob = _resolve(conn, ref)
        current = ob[body.field]
        proposal_id = create_proposal(
            conn,
            layer="L2",
            kind="l2_modification",
            subject_ref=ob["stable_id"] or ob["id"],
            draft={
                "obligation_id": ob["stable_id"] or ob["id"],
                "derivation_key": ob["id"],
                "field": body.field,
                "current_value": _iso(current) if isinstance(current, (date, datetime)) else current,
                "proposed_value": body.proposed_value,
                "requester": requester,
                "text_hash": ob["text_hash"],
            },
            rationale=body.rationale,
            confidence=None,
        )
    return {"proposal_id": proposal_id, "obligation_id": ob["stable_id"] or ob["id"], "status": "proposed"}


class ExpertReview(BaseModel):
    verdict: str = Field(pattern="^(correct|incorrect|unsure)$")
    reviewer: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=2000)


@router.post("/obligations/{ref:path}/reviews", status_code=201)
def expert_review(ref: str, body: ExpertReview) -> dict:
    from app.clhear.l2 import review as l2_review

    try:
        return l2_review.record_expert_review(get_engine(), ref, reviewer=body.reviewer, verdict=body.verdict, notes=body.notes)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown obligation {ref}")


@router.get("/changes")
def change_feed(
    since: date | None = Query(default=None),
    until: date | None = Query(default=None),
    kind: str | None = Query(default=None, description="added | updated | revoked"),
    jurisdiction: str | None = Query(default=None),
    source: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    query = (
        sa.select(l2_change_events, obligations.c.stable_id, obligations.c.title, obligations.c.determination,
                  obligations.c.statement, obligations.c.jurisdiction, obligations.c.regulator, obligations.c.status)
        .join(obligations, obligations.c.id == l2_change_events.c.obligation_id)
    )
    if since:
        query = query.where(sa.or_(l2_change_events.c.effective_date >= since,
                                   sa.func.date(l2_change_events.c.detected_at) >= since.isoformat()))
    if until:
        query = query.where(sa.func.date(l2_change_events.c.detected_at) <= until.isoformat())
    if kind:
        query = query.where(l2_change_events.c.kind == kind)
    if jurisdiction:
        query = query.where(sa.func.lower(obligations.c.jurisdiction) == jurisdiction.lower())
    if source:
        query = query.where(l2_change_events.c.source_key == source)
    with get_engine().connect() as conn:
        rows = conn.execute(query.order_by(l2_change_events.c.detected_at.desc(), l2_change_events.c.id.desc()).limit(limit)).mappings().all()
    return {
        "since": _iso(since),
        "count": len(rows),
        "items": [
            {
                "change_id": r["id"],
                "kind": r["kind"],
                "obligation_id": r["stable_id"] or r["obligation_id"],
                "title": r["title"],
                "determination": r["determination"] or r["statement"],
                "jurisdiction": r["jurisdiction"],
                "regulator": r["regulator"],
                "obligation_status": r["status"],
                "source_key": r["source_key"],
                "cause_clause_ids": r["cause_clause_ids"],
                "cause_l1_change_event_id": r["cause_l1_change_event_id"],
                "effective_date": _iso(r["effective_date"]),
                "effective_date_basis": r["effective_date_basis"],
                "detected_at": _iso(r["detected_at"]),
                "detail": r["detail"],
            }
            for r in rows
        ],
    }


@router.get("/equivalences")
def list_equivalences(
    jurisdiction: str | None = Query(default=None, description="either side in this jurisdiction"),
    basis: str | None = Query(default=None, description="concept | lexical"),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict:
    a = obligations.alias("a")
    b = obligations.alias("b")
    query = (
        sa.select(equivalences, a.c.stable_id.label("a_sid"), a.c.jurisdiction.label("a_jur"), a.c.determination.label("a_det"),
                  a.c.statement.label("a_stmt"), a.c.title.label("a_title"),
                  b.c.stable_id.label("b_sid"), b.c.jurisdiction.label("b_jur"), b.c.determination.label("b_det"),
                  b.c.statement.label("b_stmt"), b.c.title.label("b_title"))
        .join(a, a.c.id == equivalences.c.obligation_a)
        .join(b, b.c.id == equivalences.c.obligation_b)
        .where(equivalences.c.valid_to.is_(None))
    )
    if jurisdiction:
        j = jurisdiction.lower()
        query = query.where(sa.or_(sa.func.lower(a.c.jurisdiction) == j, sa.func.lower(b.c.jurisdiction) == j))
    if basis:
        query = query.where(equivalences.c.basis == basis)
    with get_engine().connect() as conn:
        rows = conn.execute(query.order_by(equivalences.c.id).limit(limit)).mappings().all()
    return {
        "count": len(rows),
        "items": [
            {
                "equivalence_id": r["id"], "basis": r["basis"], "concept_id": r["concept_id"],
                "similarity": _num(r["similarity"]), "method": r["method"],
                "a": {"id": r["a_sid"] or r["obligation_a"], "jurisdiction": r["a_jur"], "title": r["a_title"],
                      "determination": r["a_det"] or r["a_stmt"]},
                "b": {"id": r["b_sid"] or r["obligation_b"], "jurisdiction": r["b_jur"], "title": r["b_title"],
                      "determination": r["b_det"] or r["b_stmt"]},
            }
            for r in rows
        ],
    }


@router.get("/compare")
def compare(
    jurisdictions: str = Query(description="comma-separated, e.g. uk,eu,il"),
    q: str | None = Query(default=None, description="filter concepts / determinations by substring"),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    """Cross-jurisdiction comparison: for each concept (or equivalence group)
    with members in >= 2 of the requested jurisdictions, the obligation per
    jurisdiction and the gaps (jurisdictions with no equivalent)."""
    wanted = [j.strip().lower() for j in jurisdictions.split(",") if j.strip()]
    if len(wanted) < 2:
        raise HTTPException(status_code=422, detail="give at least two jurisdictions")
    like = f"%{q.lower()}%" if q else None
    groups: dict[str, dict] = {}
    with get_engine().connect() as conn:
        live_by_id = {
            r["id"]: r for r in conn.execute(
                sa.select(obligations).where(obligations.c.status.in_(LIVE))
                .where(sa.func.lower(obligations.c.jurisdiction).in_(wanted))
            ).mappings()
        }
        for cm in conn.execute(
            sa.select(concept_members.c.concept_id, concept_members.c.obligation_id, concepts.c.name, concepts.c.canonical_statement)
            .join(concepts, concepts.c.id == concept_members.c.concept_id)
        ).mappings():
            ob = live_by_id.get(cm["obligation_id"])
            if ob is None:
                continue
            g = groups.setdefault(cm["concept_id"], {"group": cm["concept_id"], "basis": "concept", "name": cm["name"],
                                                     "canonical_statement": cm["canonical_statement"], "members": {}})
            g["members"].setdefault(ob["jurisdiction"].lower(), []).append(_summary(ob))
        for e in conn.execute(sa.select(equivalences).where(equivalences.c.valid_to.is_(None), equivalences.c.basis != "concept")).mappings():
            oa, obb = live_by_id.get(e["obligation_a"]), live_by_id.get(e["obligation_b"])
            if oa is None or obb is None:
                continue
            g = groups.setdefault(e["id"], {"group": e["id"], "basis": e["basis"], "name": oa["title"],
                                            "canonical_statement": oa["determination"] or oa["statement"], "members": {}})
            for ob in (oa, obb):
                bucket = g["members"].setdefault(ob["jurisdiction"].lower(), [])
                if all(m["derivation_key"] != ob["id"] for m in bucket):
                    bucket.append(_summary(ob))
    rows = []
    for g in groups.values():
        if len(g["members"]) < 2:
            continue
        if like and like.strip("%") not in (g["name"] + " " + (g["canonical_statement"] or "")).lower():
            continue
        rows.append({**g, "gaps": [j for j in wanted if j not in g["members"]]})
    rows.sort(key=lambda g: (-len(g["members"]), g["name"]))
    return {"jurisdictions": wanted, "count": len(rows), "groups": rows[:limit]}


@router.get("/scorecard")
def l2_scorecard() -> dict:
    """The published L2 scorecard: gate status + thresholds, registry counts,
    review precision, duplicate rate, change feed volume."""
    from app.clhear.l2 import dedupe as l2_dedupe
    from app.clhear.l2 import review as l2_review
    from app.clhear.platform.gates import GATE_THRESHOLDS, gate_status

    engine = get_engine()
    with engine.connect() as conn:
        by_status = {r[0]: r[1] for r in conn.execute(sa.select(obligations.c.status, sa.func.count()).group_by(obligations.c.status))}
        by_type = {r[0] or "unclassified": r[1] for r in conn.execute(
            sa.select(obligations.c.obligation_type, sa.func.count()).where(obligations.c.status.in_(LIVE)).group_by(obligations.c.obligation_type))}
        by_jur = {r[0] or "?": r[1] for r in conn.execute(
            sa.select(obligations.c.jurisdiction, sa.func.count()).where(obligations.c.status.in_(LIVE)).group_by(obligations.c.jurisdiction))}
        by_change = {r[0]: r[1] for r in conn.execute(sa.select(l2_change_events.c.kind, sa.func.count()).group_by(l2_change_events.c.kind))}
        n_asserts = conn.execute(sa.select(sa.func.count()).select_from(asserts).where(asserts.c.valid_to.is_(None))).scalar_one()
        n_equiv = conn.execute(sa.select(sa.func.count()).select_from(equivalences).where(equivalences.c.valid_to.is_(None))).scalar_one()
        n_super = conn.execute(sa.select(sa.func.count()).select_from(supersessions)).scalar_one()
    return {
        "gate": gate_status(engine, "L2"),
        "thresholds": GATE_THRESHOLDS["L2"],
        "obligations": {"by_status": by_status, "by_type": by_type, "by_jurisdiction": by_jur,
                        "live": sum(v for k, v in by_status.items() if k in LIVE)},
        "asserts": n_asserts,
        "equivalences": n_equiv,
        "supersessions": n_super,
        "changes": by_change,
        "precision": l2_review.precision(engine),
        "duplicates": l2_dedupe.duplicate_rate(engine),
    }
