"""/api/clhear/sources… routes + /sources Explorer (HLD §7.2).

Clause text is served exclusively through l1.public (the clauses_public
discipline): restricted sources expose refs and hashes, never text. BYOL
endpoints arrive in P3.
"""
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

import json

from app.clhear.db import get_engine
from app.clhear.l1.models import (
    change_events,
    clauses,
    doc_nodes,
    family_members,
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
        latest_change = conn.execute(
            sa.select(change_events)
            .where(change_events.c.source_id == source.id)
            .order_by(change_events.c.id.desc())
            .limit(1)
        ).first()
        amended = []
        if latest_change is not None and latest_change.kind == "amended":
            amended = latest_change.clause_refs if isinstance(latest_change.clause_refs, list) else []

    return {
        "source": key,
        "version": version.version_label,
        "retrieved_at": str(version.retrieved_at),
        "s3_uri": version.s3_uri,
        "content_hash": version.content_hash,
        "locked": locked,
        "amended_refs": amended,
        "total": len(rows),
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
                public.c.doc_node_id,
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
                "doc_node_id": row.doc_node_id,
                "source_key": row.source_key,
                "source_name": row.source_name,
                "version": row.version_label,
                "snippet": snippet,
            }
        )
    return out


# ---------------------------------------------------------------- audit trail

_RUN_STATUS = {
    "succeeded": "success",
    "warning": "warning",
    "failed": "failure",
    "running": "running",
    "up-to-date": "info",
    "unchanged": "info",
}


def _outputs_of(row) -> dict:
    return row.outputs if isinstance(row.outputs, dict) else json.loads(row.outputs or "{}")


def _run_item(row) -> dict:
    outputs = _outputs_of(row)
    inputs = row.inputs if isinstance(row.inputs, dict) else json.loads(row.inputs or "{}")
    status = _RUN_STATUS.get(outputs.get("status", ""), "info")
    source = inputs.get("source") or inputs.get("family") or ""
    bits = []
    if outputs.get("change"):
        bits.append(outputs["change"])
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
        bits.append("pending manual rectification")
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
        for row in conn.execute(sa.select(runs).order_by(runs.c.id.desc()).limit(limit)):
            items.append(_run_item(row))
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
            outputs = _outputs_of(row)
            latest_run[key] = {
                "run_id": row.id,
                "fleet": row.fleet,
                "ts": str(row.created_at),
                "status": _RUN_STATUS.get(outputs.get("status", ""), "info"),
                "coverage": outputs.get("coverage"),
                "duration_ms": row.duration_ms,
                "stages": outputs.get("stages", []),
            }
    board = []
    for source in source_rows:
        source_version_list = by_source_versions.get(source.id, [])
        if not source_version_list and source.key not in latest_run:
            continue  # reference-level family member, not a fleet job
        current = next((v for v in reversed(source_version_list) if v.status == "in_force"), None)
        previous = next(
            (v for v in reversed(source_version_list) if current is None or v.id != current.id), None
        )
        board.append(
            {
                "source_key": source.key,
                "source_name": source.name,
                "adapter": source.adapter,
                "license": source.license,
                "current_version": current.version_label if current else None,
                "current_retrieved_at": str(current.retrieved_at) if current else None,
                "previous_version": previous.version_label if previous else None,
                "versions": len(source_version_list),
                "schedule": "manual (EventBridge schedules ship disabled in P0)",
                "last_run": latest_run.get(source.key),
            }
        )
    return board


@router.get("/api/clhear/runs/{run_id}")
def run_detail(run_id: int) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sa.select(runs).where(runs.c.id == run_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    outputs = _outputs_of(row)
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
