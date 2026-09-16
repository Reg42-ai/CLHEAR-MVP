"""L1 public API (HLD v2 §4.1 / §5).

    GET  /l1/sources                      browse by jurisdiction / regulator / instrument / family
    GET  /l1/sources/{key}                one source: rights badge + history, family, scorecard
    GET  /l1/sources/{key}/versions       version history (as-of / effective / retrieved)
    GET  /l1/sources/{key}/changes        change timeline for one source
    GET  /l1/clauses/{id}                 clause with span offsets, normative flag, citations
    GET  /l1/changes?since=YYYY-MM-DD     change feed with effective dates + clause ids
    GET  /l1/families                     family scorecard
    GET  /l1/families/{key}               family tree (root, members, ingested state)
    GET  /l1/scorecard                    published L1 scorecard (gates, families, starter corpus)
    GET  /l1/watchlists                   the caller's watched instruments
    POST /l1/watchlists {source_key}      "watch this instrument"
    POST /l1/watchlists/{source_key}/unwatch

Rights discipline: clause text is returned only when the source's rights basis
allows republication (l1.rights.republishable); otherwise hashes, spans and
citations are served and ``text`` is null with ``rights_basis`` explaining why.
Watch identity: the community session cookie, else an ``X-Watcher-Id`` header
(app integrations), else 401.
"""
from datetime import date, datetime, timezone

import sqlalchemy as sa
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from app.clhear.db import get_engine
from app.clhear.l1 import families as l1_families
from app.clhear.l1 import rights as l1_rights
from app.clhear.l1.public import clauses_public_select
from app.clhear.l1.models import (
    change_events,
    citations,
    clause_annotations,
    clauses,
    family_members,
    source_families,
    source_versions,
    sources,
    watchlists,
)
from app.clhear.platform import audit, record

router = APIRouter(prefix="/l1", tags=["l1"])


def _watcher(request: Request, x_watcher_id: str | None) -> str:
    from app.clhear.accounts import current_user

    user = current_user(request)
    if user:
        return f"user:{user['id']}"
    if x_watcher_id:
        return f"app:{x_watcher_id.strip()}"
    raise HTTPException(status_code=401, detail="Sign in or send X-Watcher-Id to manage watchlists")


def _source_row(conn, key: str):
    row = conn.execute(sa.select(sources).where(sources.c.key == key)).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown source {key}")
    return row


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _source_summary(conn, row, *, family_key: str | None = None) -> dict:
    latest = conn.execute(
        sa.select(source_versions)
        .where(source_versions.c.source_id == row["id"])
        .order_by(source_versions.c.id.desc())
        .limit(1)
    ).mappings().first()
    n_versions = conn.execute(
        sa.select(sa.func.count()).select_from(source_versions).where(source_versions.c.source_id == row["id"])
    ).scalar_one()
    watchers = conn.execute(
        sa.select(sa.func.count()).select_from(watchlists)
        .where(watchlists.c.source_key == row["key"])
        .where(watchlists.c.valid_to.is_(None))
    ).scalar_one()
    return {
        "key": row["key"],
        "name": row["name"],
        "short_name": row["short_name"],
        "kind": row["kind"],
        "jurisdiction": row["jurisdiction"],
        "regulator": row["issuer"],
        "publisher": row["publisher"] or row["issuer"],
        "instrument": row["instrument"] or row["short_name"] or row["name"],
        "family": family_key,
        "family_root": bool(row["family_root"]),
        "adapter": row["adapter"],
        "canonical_url": row["canonical_url"],
        "topics": row["topics"] or [],
        "rights": {
            "basis": row["rights_basis"],
            "republish_text": l1_rights.republishable(row["rights_basis"]),
            "licence": row["license"],
            "licence_ref": row["license_ref"],
        },
        "latest_version": None if latest is None else {
            "label": latest["version_label"],
            "kind": latest["version_kind"],
            "as_of_date": _iso(latest["as_of_date"]),
            "effective_date": _iso(latest["effective_date"]),
            "retrieved_at": _iso(latest["retrieved_at"]),
            "status": latest["status"],
        },
        "versions": n_versions,
        "watchers": watchers,
    }


@router.get("/sources")
def list_sources(
    jurisdiction: str | None = Query(default=None),
    regulator: str | None = Query(default=None, description="issuer / regulator substring"),
    instrument: str | None = Query(default=None, description="instrument substring"),
    family: str | None = Query(default=None),
    rights_basis: str | None = Query(default=None),
    ingested: bool | None = Query(default=None),
    q: str | None = Query(default=None, description="free-text over name / short name / key"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        stmt = (
            sa.select(sources, source_families.c.key.label("family_key"))
            .join(source_families, source_families.c.id == sources.c.family_id)
            .order_by(sources.c.jurisdiction, sources.c.key)
        )
        if jurisdiction:
            stmt = stmt.where(sa.func.lower(sources.c.jurisdiction) == jurisdiction.lower())
        if regulator:
            stmt = stmt.where(sa.func.lower(sources.c.issuer).like(f"%{regulator.lower()}%"))
        if instrument:
            stmt = stmt.where(
                sa.or_(
                    sa.func.lower(sources.c.instrument).like(f"%{instrument.lower()}%"),
                    sa.func.lower(sources.c.short_name).like(f"%{instrument.lower()}%"),
                )
            )
        if family:
            stmt = stmt.where(source_families.c.key == family)
        if rights_basis:
            stmt = stmt.where(sources.c.rights_basis == rights_basis)
        if q:
            needle = f"%{q.lower()}%"
            stmt = stmt.where(
                sa.or_(
                    sa.func.lower(sources.c.name).like(needle),
                    sa.func.lower(sources.c.short_name).like(needle),
                    sa.func.lower(sources.c.key).like(needle),
                )
            )
        rows = conn.execute(stmt).mappings().all()
        items = [_source_summary(conn, r, family_key=r["family_key"]) for r in rows]
        if ingested is not None:
            items = [i for i in items if bool(i["latest_version"]) == ingested]
        facets = {
            "jurisdictions": sorted({i["jurisdiction"] for i in items if i["jurisdiction"]}),
            "regulators": sorted({i["regulator"] for i in items if i["regulator"]}),
            "families": sorted({i["family"] for i in items if i["family"]}),
            "rights_bases": sorted({i["rights"]["basis"] for i in items}),
        }
        total = len(items)
        return {"sources": items[offset: offset + limit], "total": total, "facets": facets}


@router.get("/sources/{key:path}/versions")
def source_versions_list(key: str) -> dict:
    with get_engine().connect() as conn:
        row = _source_row(conn, key)
        rows = conn.execute(
            sa.select(source_versions).where(source_versions.c.source_id == row["id"]).order_by(source_versions.c.id)
        ).mappings().all()
        return {
            "source": key,
            "versions": [
                {
                    "id": v["id"],
                    "label": v["version_label"],
                    "kind": v["version_kind"],
                    "as_of_date": _iso(v["as_of_date"]),
                    "effective_date": _iso(v["effective_date"]),
                    "retrieved_at": _iso(v["retrieved_at"]),
                    "status": v["status"],
                    "content_hash": v["content_hash"],
                    "artifact": v["s3_uri"],
                    "clauses": conn.execute(
                        sa.select(sa.func.count()).select_from(clauses).where(clauses.c.source_version_id == v["id"])
                    ).scalar_one(),
                }
                for v in rows
            ],
        }


def _change_rows(conn, where=None, *, since: date | None = None, limit: int = 200) -> list[dict]:
    stmt = (
        sa.select(change_events, sources.c.key.label("source_key"), sources.c.short_name, sources.c.instrument,
                  sources.c.jurisdiction, sources.c.rights_basis)
        .join(sources, sources.c.id == change_events.c.source_id)
        .order_by(change_events.c.id.desc())
        .limit(limit)
    )
    if where is not None:
        stmt = stmt.where(where)
    if since is not None:
        stmt = stmt.where(
            sa.or_(
                change_events.c.effective_date >= since,
                sa.and_(change_events.c.effective_date.is_(None), change_events.c.detected_at >= datetime(since.year, since.month, since.day, tzinfo=timezone.utc)),
            )
        )
    out = []
    for r in conn.execute(stmt).mappings():
        out.append(
            {
                "id": r["id"],
                "source": r["source_key"],
                "instrument": r["instrument"] or r["short_name"],
                "jurisdiction": r["jurisdiction"],
                "kind": r["kind"],
                "old_version": r["old_version"],
                "new_version": r["new_version"],
                "clause_refs": r["clause_refs"] or [],
                "clause_ids": r["clause_ids"] or [],
                "effective_date": _iso(r["effective_date"]),
                "effective_date_basis": r["effective_date_basis"],
                "detected_at": _iso(r["detected_at"]),
                "diff": r["diff_s3_uri"],
            }
        )
    return out


@router.get("/sources/{key:path}/changes")
def source_changes(key: str, since: date | None = Query(default=None), limit: int = Query(default=100, ge=1, le=500)) -> dict:
    with get_engine().connect() as conn:
        row = _source_row(conn, key)
        return {"source": key, "changes": _change_rows(conn, change_events.c.source_id == row["id"], since=since, limit=limit)}


@router.get("/sources/{key:path}/scorecard")
def source_scorecard(key: str) -> dict:
    from app.clhear.platform.evals import latest_source_scorecard

    with get_engine().connect() as conn:
        _source_row(conn, key)
    return latest_source_scorecard(get_engine(), key)


@router.get("/sources/{key:path}")
def source_detail(key: str) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        row = _source_row(conn, key)
        fam = conn.execute(sa.select(source_families.c.key, source_families.c.name).where(source_families.c.id == row["family_id"])).first()
        body = _source_summary(conn, row, family_key=fam.key if fam else None)
        body["family_name"] = fam.name if fam else None
        body["rights"]["history"] = l1_rights.history(conn, row["id"])
        body["memberships"] = [
            dict(m)
            for m in conn.execute(
                sa.select(source_families.c.key.label("family"), family_members.c.relation, family_members.c.tier,
                          family_members.c.status, family_members.c.added_via)
                .join(source_families, source_families.c.id == family_members.c.family_id)
                .where(family_members.c.source_id == row["id"])
            ).mappings()
        ]
        body["recent_changes"] = _change_rows(conn, change_events.c.source_id == row["id"], limit=10)
    from app.clhear.platform.evals import latest_source_scorecard

    body["scorecard"] = latest_source_scorecard(engine, key)
    return body


@router.get("/clauses/{clause_id}")
def clause_detail(clause_id: int) -> dict:
    with get_engine().connect() as conn:
        row = conn.execute(
            sa.select(*(column for column in clauses.c if column.name not in {"text", "path"}),
                      source_versions.c.version_label, source_versions.c.source_id, source_versions.c.as_of_date)
            .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
            .where(clauses.c.id == clause_id)
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown clause")
        src = conn.execute(sa.select(sources).where(sources.c.id == row["source_id"])).mappings().one()
        public = conn.execute(clauses_public_select(conn).where(clauses.c.id == clause_id)).mappings().first()
        republish = public is not None
        if republish:
            audit.log_licensed_read(conn, source_key=src["key"], rights_basis=src["rights_basis"], clause_ids=[clause_id], route="/l1/clauses/{id}")
            conn.commit()
        cites = [
            {"raw": c["raw_text"] if republish else None, "disposition": c["disposition"],
             "reason": c["reason"] if republish else None, "resolved_source_id": c["resolved_source_id"]}
            for c in conn.execute(sa.select(
                citations.c.disposition, citations.c.resolved_source_id,
                (citations.c.raw_text if republish else sa.literal(None)).label("raw_text"),
                (citations.c.reason if republish else sa.literal(None)).label("reason"),
            ).where(citations.c.from_clause_id == clause_id)).mappings()
        ]
        notes = [
            {"origin": a["origin"], "category": a["category"], "summary": a["summary"], "topics": a["topics"], "model": a["model"]}
            for a in conn.execute(sa.select(clause_annotations).where(
                clause_annotations.c.clause_id == clause_id, sa.literal(republish))).mappings()
        ]
        return {
            "id": row["id"],
            "source": src["key"],
            "instrument": src["instrument"] or src["short_name"],
            "version": row["version_label"],
            "as_of_date": _iso(row["as_of_date"]),
            "ref": row["ref"],
            "path": public["path"] if republish else None,
            "span": {"start": row["span_start"], "end": row["span_end"]},
            "normative": bool(row["normative"]),
            "text": public["text"] if republish else None,
            "text_hash": row["text_hash"],
            "rights_basis": src["rights_basis"],
            "text_withheld_reason": None if republish else f"Rights basis {src['rights_basis']}; current public-text permission is required; references and hashes only",
            "citations": cites,
            "annotations": notes,
            "why": {
                "derived_by": row["derived_by"],
                "derived_at": _iso(row["derived_at"]),
                "version": row["version"],
                "valid_from": _iso(row["valid_from"]),
                "valid_to": _iso(row["valid_to"]),
            },
        }


@router.get("/changes")
def change_feed(
    request: Request,
    since: date | None = Query(default=None),
    source: str | None = Query(default=None),
    jurisdiction: str | None = Query(default=None),
    watched: bool = Query(default=False, description="only instruments on the caller's watchlist"),
    limit: int = Query(default=200, ge=1, le=1000),
    x_watcher_id: str | None = Header(default=None, alias="X-Watcher-Id"),
) -> dict:
    with get_engine().connect() as conn:
        where = None
        if source:
            where = sources.c.key == source
        if jurisdiction:
            cond = sa.func.lower(sources.c.jurisdiction) == jurisdiction.lower()
            where = cond if where is None else sa.and_(where, cond)
        if watched:
            watcher = _watcher(request, x_watcher_id)
            keys = [
                r[0]
                for r in conn.execute(
                    sa.select(watchlists.c.source_key).where(watchlists.c.watcher_id == watcher).where(watchlists.c.valid_to.is_(None))
                )
            ]
            cond = sources.c.key.in_(keys) if keys else sa.false()
            where = cond if where is None else sa.and_(where, cond)
        rows = _change_rows(conn, where, since=since, limit=limit)
    return {"since": _iso(since), "changes": rows, "count": len(rows)}


@router.get("/families")
def families_index() -> dict:
    return l1_families.family_scorecard(get_engine())


@router.get("/families/{key}")
def family_detail(key: str) -> dict:
    with get_engine().connect() as conn:
        tree = l1_families.family_tree(conn, key)
    if tree is None:
        raise HTTPException(status_code=404, detail=f"unknown family {key}")
    tree["scorecard"] = next((f for f in l1_families.family_scorecard(get_engine(), key)["families"]), None)
    return tree


@router.get("/scorecard")
def l1_scorecard() -> dict:
    """The published L1 scorecard: gate status, family completeness, starter
    corpus coverage and rights mix. Read by the UI and by release notes."""
    from app.clhear.l1.starter_corpus import coverage
    from app.clhear.platform.gates import GATE_THRESHOLDS, gate_status

    engine = get_engine()
    with engine.connect() as conn:
        rights_mix = {
            r[0]: r[1]
            for r in conn.execute(sa.select(sources.c.rights_basis, sa.func.count()).group_by(sources.c.rights_basis))
        }
        normative = conn.execute(sa.select(sa.func.count()).select_from(clauses).where(clauses.c.normative.is_(True))).scalar_one()
        total_clauses = conn.execute(sa.select(sa.func.count()).select_from(clauses)).scalar_one()
    return {
        "gate": gate_status(engine, "L1"),
        "thresholds": GATE_THRESHOLDS["L1"],
        "families": l1_families.family_scorecard(engine),
        "starter_corpus": coverage(engine),
        "rights_mix": rights_mix,
        "clauses": {"total": total_clauses, "normative": normative},
    }


class WatchBody(BaseModel):
    source_key: str


@router.get("/watchlists")
def my_watchlist(request: Request, x_watcher_id: str | None = Header(default=None, alias="X-Watcher-Id")) -> dict:
    watcher = _watcher(request, x_watcher_id)
    with get_engine().connect() as conn:
        rows = conn.execute(
            sa.select(watchlists.c.source_key, watchlists.c.created_at)
            .where(watchlists.c.watcher_id == watcher)
            .where(watchlists.c.valid_to.is_(None))
            .order_by(watchlists.c.created_at)
        ).all()
    return {"watcher": watcher, "watching": [{"source_key": r.source_key, "since": _iso(r.created_at)} for r in rows]}


@router.post("/watchlists", status_code=201)
def watch(body: WatchBody, request: Request, x_watcher_id: str | None = Header(default=None, alias="X-Watcher-Id")) -> dict:
    watcher = _watcher(request, x_watcher_id)
    engine = get_engine()
    with engine.begin() as conn:
        _source_row(conn, body.source_key)
        existing = conn.execute(
            sa.select(watchlists.c.id)
            .where(watchlists.c.watcher_id == watcher)
            .where(watchlists.c.source_key == body.source_key)
        ).mappings().first()
        if existing:
            # Re-watch after unwatch: reopen the same row (unique per watcher/source).
            conn.execute(watchlists.update().where(watchlists.c.id == existing["id"]).values(valid_to=None))
            return {"watcher": watcher, "source_key": body.source_key, "watching": True}
        record.write(
            conn,
            watchlists,
            {"id": record.new_uuid(), "watcher_id": watcher, "source_key": body.source_key},
            why=record.WhyTrail(layer="L1", subject_ref=body.source_key, reasoning_summary="user watch request", agent_id=watcher),
        )
    return {"watcher": watcher, "source_key": body.source_key, "watching": True}


@router.post("/watchlists/{source_key:path}/unwatch")
def unwatch(source_key: str, request: Request, x_watcher_id: str | None = Header(default=None, alias="X-Watcher-Id")) -> dict:
    watcher = _watcher(request, x_watcher_id)
    with get_engine().begin() as conn:
        n = record.invalidate(
            conn,
            watchlists,
            sa.and_(watchlists.c.watcher_id == watcher, watchlists.c.source_key == source_key, watchlists.c.valid_to.is_(None)),
            why=record.WhyTrail(layer="L1", subject_ref=source_key, reasoning_summary="user unwatch request", agent_id=watcher),
            reason="unwatch",
        )
    return {"watcher": watcher, "source_key": source_key, "watching": False, "closed": n}
