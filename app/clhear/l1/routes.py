"""/api/clhear/sources… routes + /sources Explorer (HLD §7.2).

Clause text is served exclusively through l1.public (the clauses_public
discipline): restricted sources expose refs and hashes, never text. BYOL
endpoints arrive in P3.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

import json

from app.clhear.db import get_engine
from app.clhear.l1.models import (
    FLEET_SCHEDULES,
    change_events,
    clause_annotations,
    clauses,
    doc_nodes,
    family_members,
    search_units,
    source_families,
    source_versions,
    sources,
)
from app.clhear.l1.public import clause_refs_select, clauses_public_select, nodes_public_select, nodes_refs_select
from app.clhear.models import eval_runs, events, runs

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
                sources.c.short_name,
                sources.c.about,
                sources.c.topics,
            ).join(sources, sources.c.id == family_members.c.source_id)
        ).all()
        latest = {
            row.source_id: row
            for row in conn.execute(
                sa.select(
                    source_versions.c.source_id,
                    source_versions.c.version_label,
                    source_versions.c.version_kind,
                    source_versions.c.as_of_date,
                    source_versions.c.retrieved_at,
                    source_versions.c.content_hash,
                    source_versions.c.s3_uri,
                    source_versions.c.id.label("version_id"),
                )
                .where(source_versions.c.status == "in_force")
                .order_by(source_versions.c.id)
            )
        }
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).date().isoformat()
        failed_today: set[str] = set()
        last_status: dict[str, str] = {}
        for row in conn.execute(sa.select(runs).where(runs.c.fleet.like("l1.%")).order_by(runs.c.id.desc()).limit(800)):
            inputs = row.inputs if isinstance(row.inputs, dict) else json.loads(row.inputs or "{}")
            outputs = row.outputs if isinstance(row.outputs, dict) else json.loads(row.outputs or "{}")
            key = inputs.get("source")
            if not key:
                continue
            last_status.setdefault(key, outputs.get("status") or "")
            if str(row.created_at)[:10] == today and outputs.get("status") in {
                "failed",
                "stale",
                "not-fully-successful",
            }:
                failed_today.add(key)
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
            if m.license == "restricted":
                library_status = "locked-restricted"
            elif version:
                library_status = "ingested"
            elif m.key in failed_today:
                library_status = "failed-today"
            elif m.adapter and _schedule_label(m.adapter) != "unscheduled" and m.key not in last_status:
                # Scheduled but no run ever recorded: the schedule was not kept.
                library_status = "schedule-missed"
            else:
                library_status = "never-fetched"
            fam_members.append(
                {
                    "key": m.key,
                    "name": m.name,
                    "short_name": m.short_name,
                    "kind": m.kind,
                    "license": m.license,
                    "relation": m.relation,
                    "tier": m.tier,
                    "status": m.status,
                    "added_via": m.added_via,
                    "canonical_url": m.canonical_url,
                    "about": m.about,
                    "topics": m.topics if isinstance(m.topics, list) else json.loads(m.topics or "[]"),
                    "latest_version": version.version_label if version else None,
                    "version_kind": version.version_kind if version else None,
                    "as_of_date": str(version.as_of_date) if version and version.as_of_date else None,
                    "retrieved_at": str(version.retrieved_at) if version else None,
                    "content_hash": version.content_hash if version else None,
                    "s3_uri": version.s3_uri if version else None,
                    "clauses": counts.get(version.version_id, 0) if version else 0,
                    "library_status": library_status,
                    "last_run_status": last_status.get(m.key),
                    "failed_today": m.key in failed_today,
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


def _resolve_version(conn, source, version_label: str | None):
    version_q = sa.select(source_versions).where(source_versions.c.source_id == source.id)
    if version_label:
        version_q = version_q.where(source_versions.c.version_label == version_label)
    else:
        version_q = version_q.where(source_versions.c.status == "in_force")
    return conn.execute(version_q.order_by(source_versions.c.id.desc()).limit(1)).first()


@router.get("/api/clhear/sources/{key:path}/document")
def source_document(key: str, version_label: str | None = None) -> dict:
    """Ordered node list for reconstructing the original document view."""
    engine = get_engine()
    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == key)).first()
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        version = _resolve_version(conn, source, version_label)
        if version is None:
            return {"source": key, "version": None, "nodes": [], "amended_refs": [], "total": 0}

        locked = source.license != "open"
        base = nodes_refs_select() if locked else nodes_public_select()
        rows = conn.execute(
            base.where(doc_nodes.c.source_version_id == version.id).order_by(doc_nodes.c.seq)
        ).all()
        # Clause understanding layer: annotations keyed by doc_node_id
        # (llm explainer preferred, heuristic classification as fallback).
        annotations: dict[int, dict] = {}
        for row in conn.execute(
            sa.select(
                clauses.c.doc_node_id,
                clause_annotations.c.origin,
                clause_annotations.c.summary,
                clause_annotations.c.category,
                clause_annotations.c.topics,
            )
            .join(clause_annotations, clause_annotations.c.clause_id == clauses.c.id)
            .where(clauses.c.source_version_id == version.id)
            .order_by(clause_annotations.c.origin)  # 'heuristic' < 'llm': llm overwrites
        ):
            if row.doc_node_id is None:
                continue
            existing = annotations.get(row.doc_node_id, {})
            annotations[row.doc_node_id] = {
                "origin": row.origin,
                "summary": row.summary or existing.get("summary", ""),
                "category": row.category or existing.get("category", ""),
                "topics": row.topics if isinstance(row.topics, list) else json.loads(row.topics or "[]"),
            }
        latest_change = conn.execute(
            sa.select(change_events)
            .where(change_events.c.source_id == source.id)
            .order_by(change_events.c.id.desc())
            .limit(1)
        ).first()
        amended = []
        if latest_change is not None and latest_change.kind == "amended":
            amended = latest_change.clause_refs if isinstance(latest_change.clause_refs, list) else []
        # For the preamble notice: does an as-published sibling exist?
        as_published_sibling = conn.execute(
            sa.select(source_versions.c.version_label)
            .where(source_versions.c.source_id == source.id)
            .where(source_versions.c.version_kind == "as_published")
            .limit(1)
        ).scalar()

    return {
        "source": key,
        "version": version.version_label,
        "version_kind": version.version_kind,
        "as_of_date": str(version.as_of_date) if version.as_of_date else None,
        "as_published_sibling": as_published_sibling if version.version_kind != "as_published" else None,
        "retrieved_at": str(version.retrieved_at),
        "s3_uri": version.s3_uri,
        "content_hash": version.content_hash,
        "locked": locked,
        "amended_refs": amended,
        "total": len(rows),
        "short_name": source.short_name,
        "nodes": [
            {
                "id": row.id,
                "parent_id": row.parent_id,
                "seq": row.seq,
                "depth": row.depth,
                "node_type": row.node_type,
                "ref": row.ref,
                "label": row.label,
                "heading": row.heading,
                "raw_text": getattr(row, "raw_text", None),
                "text_hash": row.text_hash,
                "annotation": annotations.get(row.id),
            }
            for row in rows
        ],
    }


@router.get("/api/clhear/nodes/{node_id}")
def node_inspector(node_id: int) -> dict:
    """Intelligence payload for the hover/click inspector."""
    engine = get_engine()
    with engine.connect() as conn:
        node = conn.execute(sa.select(doc_nodes).where(doc_nodes.c.id == node_id)).first()
        if node is None:
            raise HTTPException(status_code=404, detail="node not found")
        version = conn.execute(
            sa.select(source_versions).where(source_versions.c.id == node.source_version_id)
        ).one()
        source = conn.execute(sa.select(sources).where(sources.c.id == version.source_id)).one()
        public = bool(node.public_ok)
        ancestors = []
        parent_id = node.parent_id
        while parent_id is not None:
            parent = conn.execute(sa.select(doc_nodes).where(doc_nodes.c.id == parent_id)).first()
            if parent is None:
                break
            ancestors.append(
                {"id": parent.id, "node_type": parent.node_type, "ref": parent.ref, "label": parent.label, "heading": parent.heading}
            )
            parent_id = parent.parent_id
        ancestors.reverse()
        changes = []
        if node.ref:
            for change in conn.execute(
                sa.select(change_events)
                .where(change_events.c.source_id == source.id)
                .order_by(change_events.c.id.desc())
            ):
                refs = change.clause_refs if isinstance(change.clause_refs, list) else []
                if node.ref in refs:
                    changes.append(_change_dict(change))
        indexed_as = None
        walk_id = node.id
        seen: set[int] = set()
        while walk_id and walk_id not in seen:
            seen.add(walk_id)
            # Prefer paragraph-grain (heading prefix used at index time) over
            # the distilled clause line. Walk ancestors so a short point still
            # shows the article unit that actually made the search hit.
            unit_text = conn.execute(
                sa.select(search_units.c.text)
                .where(search_units.c.doc_node_id == walk_id)
                .order_by(search_units.c.grain.desc(), search_units.c.id)
                .limit(1)
            ).scalar()
            if unit_text:
                indexed_as = unit_text.split("\n", 1)[0].strip() or None
                if indexed_as:
                    break
            parent = conn.execute(
                sa.select(doc_nodes.c.parent_id).where(doc_nodes.c.id == walk_id)
            ).first()
            walk_id = parent.parent_id if parent is not None else None
    return {
        "id": node.id,
        "node_type": node.node_type,
        "ref": node.ref,
        "label": node.label,
        "heading": node.heading,
        "raw_text": node.raw_text if public else None,
        "source_fragment": node.source_fragment if public else None,
        "text_hash": node.text_hash,
        "public_ok": public,
        "seq": node.seq,
        "depth": node.depth,
        "source_key": source.key,
        "source_name": source.name,
        "license": source.license,
        "version_label": version.version_label,
        "retrieved_at": str(version.retrieved_at),
        "s3_uri": version.s3_uri,
        "content_hash": version.content_hash,
        "permalink": f"/sources?source={source.key}&node={node.id}",
        "ancestors": ancestors,
        "indexed_as": indexed_as,
        "changes": changes,
    }


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
        version = _resolve_version(conn, source, version_label)
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
                "id": row.id,
                "doc_node_id": row.doc_node_id,
                "ref": row.ref,
                "path": row.path,
                "ordering": row.ordering,
                "text": getattr(row, "text", None),
                "text_hash": row.text_hash,
            }
            for row in rows
        ],
    }


def _version_dict(v) -> dict:
    return {
        "version_label": v.version_label,
        "version_kind": v.version_kind,
        "as_of_date": str(v.as_of_date) if v.as_of_date else None,
        "retrieved_at": str(v.retrieved_at),
        "status": v.status,
        "content_hash": v.content_hash,
        "s3_uri": v.s3_uri,
    }


@router.get("/api/clhear/sources/{key:path}/evals")
def source_evals(key: str) -> dict:
    """E1–E7 scorecard + last fetch / artifact for the Evidence tab."""
    from app.clhear.platform import evals as l1_evals

    engine = get_engine()
    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == key)).first()
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        version = conn.execute(
            sa.select(source_versions)
            .where(source_versions.c.source_id == source.id)
            .where(source_versions.c.status == "in_force")
            .order_by(source_versions.c.id.desc())
            .limit(1)
        ).first()
        last_run = None
        for row in conn.execute(sa.select(runs).where(runs.c.fleet.like("l1.%")).order_by(runs.c.id.desc()).limit(400)):
            inputs = row.inputs if isinstance(row.inputs, dict) else json.loads(row.inputs or "{}")
            if inputs.get("source") == key:
                outputs = _display_outputs(row)
                last_run = {
                    "run_id": row.id,
                    "status": outputs.get("status"),
                    "ts": str(row.created_at),
                    "coverage": outputs.get("coverage"),
                    "freshness": outputs.get("freshness"),
                    "error": outputs.get("error"),
                    "note": outputs.get("note"),
                }
                break
    card = l1_evals.latest_source_scorecard(engine, key)
    return {
        "source": key,
        "locked": source.license != "open",
        "version": version.version_label if version else None,
        "s3_uri": version.s3_uri if version else "",
        "content_hash": version.content_hash if version else "",
        "retrieved_at": str(version.retrieved_at) if version else None,
        "last_run": last_run,
        "scorecard": card,
        "l2_ready": card.get("green") and source.license == "open",
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
        # Provenance axis 2: the family instruments that caused the changes
        # (amending SIs, corrigenda; informative-tier drafts join here in P2+).
        instruments = conn.execute(
            sa.select(
                sources.c.key,
                sources.c.name,
                sources.c.canonical_url,
                family_members.c.relation,
                family_members.c.tier,
                family_members.c.status,
                family_members.c.added_via,
            )
            .join(family_members, family_members.c.source_id == sources.c.id)
            .where(family_members.c.family_id == source.family_id)
            .where(sources.c.id != source.id)
            .order_by(sources.c.key)
        ).all()
    return {
        "key": source.key,
        "name": source.name,
        "short_name": source.short_name,
        "kind": source.kind,
        "issuer": source.issuer,
        "jurisdiction": source.jurisdiction,
        "license": source.license,
        "license_ref": source.license_ref,
        "adapter": source.adapter,
        "canonical_url": source.canonical_url,
        "about": source.about,
        "topics": source.topics if isinstance(source.topics, list) else json.loads(source.topics or "[]"),
        "versions": [_version_dict(v) for v in versions],
        "changes": [_change_dict(c) for c in changes],
        "s3_uri": versions[0].s3_uri if versions else "",
        "content_hash": versions[0].content_hash if versions else "",
        "provenance": {
            "text_states": [_version_dict(v) for v in reversed(versions)],  # oldest first
            "related_instruments": [
                {
                    "key": i.key,
                    "name": i.name,
                    "relation": i.relation,
                    "tier": i.tier,
                    "status": i.status,
                    "added_via": i.added_via,
                    "canonical_url": i.canonical_url,
                }
                for i in instruments
            ],
        },
    }


@router.get("/api/clhear/meta")
def meta() -> dict:
    """UI-facing constants: version-kind + pipeline-stage dictionaries + gates."""
    from app.clhear.l1.models import ANNOTATION_CATEGORIES, FLEET_SCHEDULES, STAGE_INFO, VERSION_KINDS
    from app.clhear.settings import get_settings

    settings = get_settings()
    return {
        "version_kinds": VERSION_KINDS,
        "stages": STAGE_INFO,
        "annotation_categories": list(ANNOTATION_CATEGORIES),
        "fidelity_threshold": settings.clhear_fidelity_threshold,
        "salvage_cap": settings.clhear_salvage_cap,
        "schedules": FLEET_SCHEDULES,
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
def search_clauses(
    q: str = Query(min_length=2),
    category: str | None = None,
    topic: str | None = None,
    scope: str | None = None,
    limit: int = Query(default=30, le=100),
) -> list[dict]:
    """Hybrid search over the unified search-unit store: ref-lookup + FTS5/BM25
    + LIKE fused with Reciprocal Rank Fusion, deduped per clause, capped per
    source, with context restored. Units are built from PUBLIC clauses only,
    so restricted text is excluded by construction. `scope` narrows to a
    family key or topic (the "projects" pattern)."""
    from app.clhear.l1 import retrieval

    return retrieval.search(
        get_engine(), q, scope=scope, category=category, topic=topic, limit=limit
    )


# ---------------------------------------------------------------- audit trail

_RUN_STATUS = {
    "succeeded": "success",
    "warning": "warning",
    "failed": "failure",
    "running": "running",
    "up-to-date": "info",
    "unchanged": "info",
    "stale": "warning",
    "not-fully-successful": "failure",
}


def _outputs_of(row) -> dict:
    return row.outputs if isinstance(row.outputs, dict) else json.loads(row.outputs or "{}")


_STALE_RUNNING = timedelta(minutes=15)


def _display_outputs(row) -> dict:
    """Crash before finish() left status=running — show failed, not a spinner."""
    outputs = dict(_outputs_of(row))
    if outputs.get("status") != "running":
        return outputs
    created = row.created_at
    if created is not None and getattr(created, "tzinfo", None) is None:
        created = created.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - created) if created is not None else _STALE_RUNNING
    if age >= _STALE_RUNNING:
        outputs["status"] = "failed"
        outputs.setdefault("error", "crashed before finish")
        outputs.setdefault("note", "crash ≠ running")
    return outputs


def _run_item(row) -> dict:
    outputs = _display_outputs(row)
    inputs = row.inputs if isinstance(row.inputs, dict) else json.loads(row.inputs or "{}")
    status = _RUN_STATUS.get(outputs.get("status", ""), "info")
    source = inputs.get("source") or inputs.get("family") or ""
    bits = []
    if outputs.get("change"):
        bits.append(outputs["change"])
    if outputs.get("version"):
        kind = outputs.get("version_kind")
        bits.append(f"{outputs['version']}" + (f" ({kind})" if kind else ""))
    if outputs.get("nodes"):
        bits.append(f"{outputs['nodes']} nodes / {outputs.get('clauses', 0)} clauses")
    if outputs.get("coverage") is not None:
        bits.append(f"coverage {outputs['coverage']:.1%}" if isinstance(outputs["coverage"], float) else f"coverage {outputs['coverage']}")
    if outputs.get("hints_used"):
        bits.append(f"{len(outputs['hints_used'])} learned hint(s)")
    if outputs.get("recovered_spans"):
        bits.append(f"{outputs['recovered_spans']} salvaged span(s)")
    if outputs.get("llm_assisted"):
        bits.append("LLM-assisted repair")
    if outputs.get("new_members") is not None:
        bits.append(f"{len(outputs['new_members'])} new family member(s)")
    if outputs.get("status") == "failed":
        bits.append(outputs.get("error") or "pending manual rectification")
    if outputs.get("status") in {"up-to-date", "unchanged"}:
        bits.append(outputs.get("note") or "probed, unchanged")
    if outputs.get("freshness") == "stale":
        bits.append("stale last-good")
    if outputs.get("error") and outputs.get("status") == "stale":
        bits.append(outputs["error"][:160])
    summary = " · ".join(bits) if bits else outputs.get("status", "run")
    return {
        "ts": str(row.created_at),
        "type": "run",
        "run_id": row.id,
        "actor": row.fleet,
        "status": status,
        "source_key": source,
        "summary": summary,
        "duration_ms": row.duration_ms,
        "links": {"review": "/review"} if status == "failure" else {},
        "details": outputs,
    }


@router.get("/api/clhear/activity")
def activity(
    status: str | None = None,
    fleet: str | None = None,
    source: str | None = None,
    limit: int = Query(default=100, le=400),
) -> list[dict]:
    """The system audit trail: a read-only chronological projection of the
    append-only runs ledger, change events, outbox events, and eval runs.
    Metadata only — refs, hashes, counts — never clause text."""
    engine = get_engine()
    items: list[dict] = []
    with engine.connect() as conn:
        short_names = dict(conn.execute(sa.select(sources.c.key, sources.c.short_name)).all())
        for row in conn.execute(sa.select(runs).order_by(runs.c.id.desc()).limit(limit)):
            item = _run_item(row)
            item["short_name"] = short_names.get(item.get("source_key"), "")
            items.append(item)
        for row in conn.execute(
            sa.select(change_events, sources.c.key.label("source_key"), sources.c.name.label("source_name"))
            .join(sources, sources.c.id == change_events.c.source_id)
            .order_by(change_events.c.id.desc())
            .limit(limit)
        ):
            refs = row.clause_refs if isinstance(row.clause_refs, list) else []
            transition = (
                f"new version {row.new_version} supersedes {row.old_version} · {len(refs)} clause(s) changed"
                if row.old_version
                else f"first version {row.new_version} ingested"
            )
            items.append(
                {
                    "ts": str(row.detected_at),
                    "type": "version_update",
                    "actor": "l1.pipeline",
                    "status": "success",
                    "source_key": row.source_key,
                    "summary": f"{row.source_name} — {transition}",
                    "refs": refs[:30],
                    "links": {"document": f"/sources?source={row.source_key}", "diff": row.diff_s3_uri},
                    "details": {"kind": row.kind, "old_version": row.old_version, "new_version": row.new_version},
                }
            )
        for row in conn.execute(
            sa.select(events)
            .where(events.c.kind.in_(("ProposalApproved", "ProposalRejected", "FamilyMembersAdded", "IngestFidelityFailed")))
            .order_by(events.c.id.desc())
            .limit(limit)
        ):
            payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload or "{}")
            failure = row.kind == "IngestFidelityFailed"
            items.append(
                {
                    "ts": str(row.created_at),
                    "type": "event",
                    "actor": payload.get("approver") or row.producer,
                    "status": "failure" if failure else "success",
                    "source_key": row.subject_ref,
                    "summary": (
                        f"{row.subject_ref} ingest NOT fully successful — pending manual rectification"
                        if failure
                        else f"{row.kind} — {row.subject_ref}"
                    ),
                    "links": {"review": "/review"} if failure or row.kind.startswith("Proposal") else {},
                    "details": {k: v for k, v in payload.items() if k != "clause_refs"},
                }
            )
        for row in conn.execute(sa.select(eval_runs).order_by(eval_runs.c.id.desc()).limit(limit)):
            items.append(
                {
                    "ts": str(row.ran_at),
                    "type": "eval",
                    "actor": f"evals.{row.suite}",
                    "status": "success" if row.passed else "failure",
                    "source_key": row.source_key or "",
                    "summary": f"eval suite {row.suite} {'passed' if row.passed else 'FAILED'}"
                    + (f" (release {row.release})" if row.release else ""),
                    "links": {},
                    "details": row.scores if isinstance(row.scores, dict) else json.loads(row.scores or "{}"),
                }
            )
    if status:
        items = [i for i in items if i["status"] == status]
    if fleet:
        items = [i for i in items if fleet in str(i.get("actor", ""))]
    if source:
        items = [i for i in items if i.get("source_key") == source]
    items.sort(key=lambda i: i["ts"], reverse=True)
    return items[:limit]


def _schedule_label(adapter: str) -> str:
    """Human schedule for the Fleet table — same dictionary the EventBridge rules use."""
    key = adapter
    if adapter.startswith("govinfo") or adapter.startswith("nist"):
        key = "govinfo_us"
    sched = FLEET_SCHEDULES.get(key) or FLEET_SCHEDULES.get(adapter)
    if not sched:
        return "unscheduled"
    return f"{sched['cadence']} · {sched['utc_time']} UTC"


@router.get("/api/clhear/fleet")
def fleet_board() -> list[dict]:
    """Per-source pipeline health: last run + stages, coverage, versions,
    freshness — the OpenSanctions-style board the Fleet view renders."""
    engine = get_engine()
    with engine.connect() as conn:
        source_rows = conn.execute(sa.select(sources).where(sources.c.adapter != "")).all()
        versions = conn.execute(sa.select(source_versions).order_by(source_versions.c.id)).all()
        run_rows = conn.execute(
            sa.select(runs).where(runs.c.fleet.like("l1.%")).order_by(runs.c.id.desc()).limit(400)
        ).all()
    by_source_versions: dict[int, list] = {}
    for v in versions:
        by_source_versions.setdefault(v.source_id, []).append(v)
    latest_run: dict[str, dict] = {}
    for row in run_rows:
        inputs = row.inputs if isinstance(row.inputs, dict) else json.loads(row.inputs or "{}")
        key = inputs.get("source") or inputs.get("family") or ""
        if key and key not in latest_run:
            outputs = _display_outputs(row)
            latest_run[key] = {
                "run_id": row.id,
                "fleet": row.fleet,
                "ts": str(row.created_at),
                "status": _RUN_STATUS.get(outputs.get("status", ""), "info"),
                "raw_status": outputs.get("status"),
                "coverage": outputs.get("coverage"),
                "duration_ms": row.duration_ms,
                "stages": outputs.get("stages", []),
                "freshness": outputs.get("freshness"),
                "note": outputs.get("note"),
                "error": outputs.get("error"),
            }
    now = datetime.now(timezone.utc)
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    board = []
    for source in source_rows:
        source_version_list = by_source_versions.get(source.id, [])
        current = next((v for v in reversed(source_version_list) if v.status == "in_force"), None)
        previous = next(
            (v for v in reversed(source_version_list) if current is None or v.id != current.id), None
        )
        run = latest_run.get(source.key)
        last_attempted = run["ts"] if run else None
        attempted_24h = False
        if last_attempted:
            try:
                attempted_at = datetime.fromisoformat(str(last_attempted))
                if attempted_at.tzinfo is None:
                    attempted_at = attempted_at.replace(tzinfo=timezone.utc)
                attempted_24h = (now - attempted_at) <= timedelta(hours=24)
            except ValueError:
                pass
        scheduled = _schedule_label(source.adapter) != "unscheduled"
        if source.license == "restricted":
            library_status = "locked-restricted"
        elif current:
            library_status = "ingested"
        elif run and run.get("status") == "failure":
            library_status = "failed-today"
        elif scheduled and not attempted_24h:
            # The schedule promised a run that did not happen — say so.
            library_status = "schedule-missed"
        else:
            library_status = "never-fetched"
        board.append(
            {
                "source_key": source.key,
                "source_name": source.name,
                "short_name": source.short_name,
                "adapter": source.adapter,
                "license": source.license,
                "current_version": current.version_label if current else None,
                "current_version_kind": current.version_kind if current else None,
                "current_as_of": str(current.as_of_date) if current and current.as_of_date else None,
                "current_retrieved_at": str(current.retrieved_at) if current else None,
                "previous_version": previous.version_label if previous else None,
                "versions": len(source_version_list),
                "schedule": _schedule_label(source.adapter),
                "last_run": run,
                "last_attempted": last_attempted,
                "attempted_24h": attempted_24h,
                "next_run_utc": next_midnight.isoformat() if scheduled else None,
                "library_status": library_status,
            }
        )
    return board


# ------------------------------------------------------------ fleet job graph

def _job_tasks(conn, job_id: str) -> list[dict]:
    rows = conn.execute(sa.select(runs).order_by(runs.c.id)).all()
    short_names = dict(conn.execute(sa.select(sources.c.key, sources.c.short_name)).all())
    tasks = []
    for row in rows:
        inputs = row.inputs if isinstance(row.inputs, dict) else json.loads(row.inputs or "{}")
        if inputs.get("job_id") != job_id:
            continue
        outputs = _display_outputs(row)
        status = _RUN_STATUS.get(outputs.get("status", ""), "info")
        source = inputs.get("source") or inputs.get("family") or ""
        if row.fleet == "l1.citator":
            step = f"citator sync ({len(outputs.get('new_members', []))} new members)"
        elif row.fleet == "l0.relay":
            step = f"relay events ({outputs.get('relayed', 0)} relayed)"
        elif outputs.get("version"):
            step = f"ingest {outputs['version']}"
        else:
            step = outputs.get("status", "run")
        tasks.append(
            {
                "run_id": row.id,
                "fleet": row.fleet,
                "source": source,
                "short_name": short_names.get(source, ""),
                "step": step,
                "status": status,
                "ts": str(row.created_at),
                "duration_ms": row.duration_ms,
                "coverage": outputs.get("coverage"),
                "version": outputs.get("version"),
                "version_kind": outputs.get("version_kind"),
                "nodes": outputs.get("nodes"),
                "llm_assisted": bool(outputs.get("llm_assisted")),
                "recovered_spans": outputs.get("recovered_spans", 0),
            }
        )
    return tasks


def _job_graph(conn, job_id: str) -> dict:
    tasks = _job_tasks(conn, job_id)
    if not tasks:
        raise HTTPException(status_code=404, detail="job not found")
    # Lanes: tasks chained per source in run order; relay is the convergence.
    lanes: dict[str, list[dict]] = {}
    relay_task = None
    for task in tasks:
        if task["fleet"] == "l0.relay":
            relay_task = task
            continue
        lanes.setdefault(task["source"], []).append(task)
    edges: list[list[int]] = []  # [from_run_id, to_run_id]; 0 = job start
    for lane in lanes.values():
        edges.append([0, lane[0]["run_id"]])
        for a, b in zip(lane, lane[1:]):
            edges.append([a["run_id"], b["run_id"]])
        if relay_task is not None:
            edges.append([lane[-1]["run_id"], relay_task["run_id"]])
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    first = min(tasks, key=lambda t: t["run_id"])
    return {
        "job_id": job_id,
        "trigger": "build_corpus" if any(t["fleet"] == "l0.relay" for t in tasks) else "cli",
        "started_at": first["ts"],
        "total_duration_ms": sum(t["duration_ms"] or 0 for t in tasks),
        "status_counts": counts,
        "running": any(t["status"] == "running" for t in tasks),
        "lanes": [{"source": source, "tasks": lane} for source, lane in lanes.items()],
        "relay": relay_task,
        "edges": edges,
    }


@router.get("/api/clhear/jobs/latest")
def latest_job() -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sa.select(runs.c.inputs).order_by(runs.c.id.desc()).limit(300)).all()
        job_id = None
        for row in rows:
            inputs = row.inputs if isinstance(row.inputs, dict) else json.loads(row.inputs or "{}")
            if inputs.get("job_id"):
                job_id = inputs["job_id"]
                break
        if job_id is None:
            raise HTTPException(status_code=404, detail="no jobs recorded")
        return _job_graph(conn, job_id)


@router.get("/api/clhear/jobs/{job_id}")
def job_detail(job_id: str) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        return _job_graph(conn, job_id)


@router.get("/api/clhear/runs/{run_id}")
def run_detail(run_id: int) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sa.select(runs).where(runs.c.id == run_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    outputs = _display_outputs(row)
    return {
        "id": row.id,
        "fleet": row.fleet,
        "trigger": row.trigger,
        "inputs": row.inputs if isinstance(row.inputs, dict) else json.loads(row.inputs or "{}"),
        "status": outputs.get("status"),
        "stages": outputs.get("stages", []),
        "outputs": {k: v for k, v in outputs.items() if k != "stages"},
        "duration_ms": row.duration_ms,
        "created_at": str(row.created_at),
    }


@router.get("/sources", response_class=HTMLResponse)
def sources_explorer() -> HTMLResponse:
    # no-cache: the app shell must always match the deployed API/corpus.
    return HTMLResponse(
        (WEB_DIR / "sources.html").read_text(),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


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
