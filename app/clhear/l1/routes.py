"""/api/clhear/sources… routes + /sources Explorer (HLD §7.2).

Clause text is served exclusively through l1.public (the clauses_public
discipline): restricted sources expose refs and hashes, never text. BYOL
endpoints arrive in P3.
"""
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.clhear.db import get_engine
from app.clhear.l1.models import change_events, clauses, family_members, source_families, source_versions, sources
from app.clhear.l1.public import clause_refs_select, clauses_public_select

router = APIRouter()

WEB_DIR = Path(__file__).parent.parent / "web"


@router.get("/api/clhear/sources")
def list_sources() -> list[dict]:
    """Library view: families -> members -> latest-version summary."""
    engine = get_engine()
    with engine.connect() as conn:
        families = conn.execute(sa.select(source_families).order_by(source_families.c.name)).all()
        members = conn.execute(
            sa.select(
                family_members.c.family_id,
                family_members.c.relation,
                family_members.c.tier,
                family_members.c.status,
                family_members.c.added_via,
                sources.c.id.label("source_id"),
                sources.c.key,
                sources.c.name,
                sources.c.kind,
                sources.c.license,
                sources.c.canonical_url,
                sources.c.adapter,
            ).join(sources, sources.c.id == family_members.c.source_id)
        ).all()
        latest = {
            row.source_id: row
            for row in conn.execute(
                sa.select(
                    source_versions.c.source_id,
                    source_versions.c.version_label,
                    source_versions.c.retrieved_at,
                    source_versions.c.content_hash,
                    source_versions.c.id.label("version_id"),
                )
                .where(source_versions.c.status == "in_force")
                .order_by(source_versions.c.id)
            )
        }
        counts = {
            row.source_version_id: row.n
            for row in conn.execute(
                sa.select(clauses.c.source_version_id, sa.func.count().label("n")).group_by(
                    clauses.c.source_version_id
                )
            )
        }
    out = []
    for family in families:
        fam_members = []
        for m in sorted((m for m in members if m.family_id == family.id), key=lambda m: (m.relation != "root", m.key)):
            version = latest.get(m.source_id)
            fam_members.append(
                {
                    "key": m.key,
                    "name": m.name,
                    "kind": m.kind,
                    "license": m.license,
                    "relation": m.relation,
                    "tier": m.tier,
                    "status": m.status,
                    "added_via": m.added_via,
                    "canonical_url": m.canonical_url,
                    "latest_version": version.version_label if version else None,
                    "retrieved_at": str(version.retrieved_at) if version else None,
                    "content_hash": version.content_hash if version else None,
                    "clauses": counts.get(version.version_id, 0) if version else 0,
                }
            )
        out.append(
            {
                "key": family.key,
                "name": family.name,
                "scope_charter": family.scope_charter,
                "members": fam_members,
            }
        )
    return out


@router.get("/api/clhear/sources/{key:path}/clauses")
def source_clauses(
    key: str,
    version_label: str | None = None,
    limit: int = Query(default=1000, le=5000),
    offset: int = 0,
) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == key)).first()
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        version_q = sa.select(source_versions).where(source_versions.c.source_id == source.id)
        if version_label:
            version_q = version_q.where(source_versions.c.version_label == version_label)
        else:
            version_q = version_q.where(source_versions.c.status == "in_force")
        version = conn.execute(version_q.order_by(source_versions.c.id.desc()).limit(1)).first()
        if version is None:
            return {"source": key, "version": None, "clauses": [], "total": 0}

        # Restricted discipline: text flows only through clauses_public_select.
        base = clauses_public_select() if source.license == "open" else clause_refs_select()
        rows = conn.execute(
            base.where(clauses.c.source_version_id == version.id)
            .order_by(clauses.c.ordering)
            .limit(limit)
            .offset(offset)
        ).all()
        total = conn.execute(
            sa.select(sa.func.count()).select_from(clauses).where(clauses.c.source_version_id == version.id)
        ).scalar_one()
    return {
        "source": key,
        "version": version.version_label,
        "retrieved_at": str(version.retrieved_at),
        "s3_uri": version.s3_uri,
        "content_hash": version.content_hash,
        "locked": source.license != "open",
        "total": total,
        "clauses": [
            {
                "ref": row.ref,
                "path": row.path,
                "ordering": row.ordering,
                "text": getattr(row, "text", None),
                "text_hash": row.text_hash,
            }
            for row in rows
        ],
    }


@router.get("/api/clhear/sources/{key:path}")
def source_detail(key: str) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == key)).first()
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        versions = conn.execute(
            sa.select(source_versions)
            .where(source_versions.c.source_id == source.id)
            .order_by(source_versions.c.id.desc())
        ).all()
        changes = conn.execute(
            sa.select(change_events)
            .where(change_events.c.source_id == source.id)
            .order_by(change_events.c.id.desc())
        ).all()
    return {
        "key": source.key,
        "name": source.name,
        "kind": source.kind,
        "issuer": source.issuer,
        "jurisdiction": source.jurisdiction,
        "license": source.license,
        "license_ref": source.license_ref,
        "adapter": source.adapter,
        "canonical_url": source.canonical_url,
        "versions": [
            {
                "version_label": v.version_label,
                "retrieved_at": str(v.retrieved_at),
                "status": v.status,
                "content_hash": v.content_hash,
                "s3_uri": v.s3_uri,
            }
            for v in versions
        ],
        "changes": [_change_dict(c) for c in changes],
    }


@router.get("/api/clhear/changes")
def recent_changes(limit: int = Query(default=50, le=200)) -> list[dict]:
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(change_events, sources.c.key.label("source_key"), sources.c.name.label("source_name"))
            .join(sources, sources.c.id == change_events.c.source_id)
            .order_by(change_events.c.id.desc())
            .limit(limit)
        ).all()
    return [{**_change_dict(row), "source_key": row.source_key, "source_name": row.source_name} for row in rows]


@router.get("/api/clhear/search")
def search_clauses(q: str = Query(min_length=2), limit: int = Query(default=50, le=100)) -> list[dict]:
    """Full-text search over PUBLIC clause text (pg_trgm on Aurora; LIKE on
    SQLite — # ARCH). Restricted text is excluded by construction."""
    engine = get_engine()
    pattern = f"%{q.lower()}%"
    public = clauses_public_select().subquery()
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                public.c.ref,
                public.c.path,
                public.c.text,
                sources.c.key.label("source_key"),
                sources.c.name.label("source_name"),
                source_versions.c.version_label,
            )
            .join(source_versions, source_versions.c.id == public.c.source_version_id)
            .join(sources, sources.c.id == source_versions.c.source_id)
            .where(source_versions.c.status == "in_force")
            .where(sa.func.lower(public.c.text).like(pattern))
            .order_by(sources.c.key, public.c.ordering)
            .limit(limit)
        ).all()
    out = []
    for row in rows:
        text = row.text
        pos = text.lower().find(q.lower())
        start = max(0, pos - 120)
        snippet = ("…" if start > 0 else "") + text[start : pos + len(q) + 200] + "…"
        out.append(
            {
                "ref": row.ref,
                "path": row.path,
                "source_key": row.source_key,
                "source_name": row.source_name,
                "version": row.version_label,
                "snippet": snippet,
            }
        )
    return out


@router.get("/sources", response_class=HTMLResponse)
def sources_explorer() -> str:
    return (WEB_DIR / "sources.html").read_text()


def _change_dict(row) -> dict:
    refs = row.clause_refs if isinstance(row.clause_refs, list) else []
    return {
        "id": row.id,
        "kind": row.kind,
        "old_version": row.old_version,
        "new_version": row.new_version,
        "clause_refs": refs,
        "detected_at": str(row.detected_at),
        "diff_s3_uri": row.diff_s3_uri,
    }
