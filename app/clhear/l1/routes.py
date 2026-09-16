"""Source Explorer and read-only worker evidence APIs (HLD §7.2).

Protected text requires independent authentication and publisher permissions.
Inventory and workflow endpoints return recorded operational metadata only.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request
from urllib.parse import urlencode
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
from app.clhear.l1 import rights as l1_rights
from app.clhear.l1.public import clause_refs_select, clauses_public_select, nodes_public_select, nodes_refs_select, nodes_internal_select
from app.clhear.models import eval_runs, events, runs
from app.clhear.platform import audit

router = APIRouter()

WEB_DIR = Path(__file__).parent.parent / "web"


@router.get("/api/clhear/viewer-snapshot")
def viewer_snapshot_state() -> dict:
    from app.clhear.l1.viewer_snapshot import read_viewer_state

    return read_viewer_state(get_engine())


@router.get("/api/clhear/l1/inventory")
def l1_inventory(scope: str = Query("all_publishers", pattern="^(registered|finra|all_publishers)$")) -> dict:
    from app.clhear.l1.inventory import inventory_summary

    return inventory_summary(get_engine(), scope=scope)


@router.get("/api/clhear/l1/publishers")
def l1_publishers() -> dict:
    from app.clhear.l1.publishers import publisher_profiles
    profiles = publisher_profiles()
    return {"publishers": profiles, "total": len(profiles), "denominator_known": False,
            "notice": "Configured publisher boundaries. Completion requires the recorded worker discovery and document audits."}


@router.get("/api/clhear/l1/cycles")
def l1_cycles(cycle_id: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)) -> dict:
    from app.clhear.l1.cycles import cycle_summary
    return cycle_summary(get_engine(), cycle_id=cycle_id, offset=offset, limit=limit)


@router.get("/api/clhear/l1/workflow")
def l1_workflow(job_id: str | None = None, source_key: str | None = None,
                task_offset: int = Query(0, ge=0), task_limit: int = Query(100, ge=1, le=1000),
                step_offset: int = Query(0, ge=0), step_limit: int = Query(200, ge=1, le=1000),
                job_offset: int = Query(0, ge=0), job_limit: int = Query(10, ge=1, le=100)) -> dict:
    from app.clhear.l1.workflow import workflow_summary

    return workflow_summary(get_engine(), job_id=job_id, source_key=source_key,
                            task_offset=task_offset, task_limit=task_limit, step_offset=step_offset,
                            step_limit=step_limit, job_offset=job_offset, job_limit=job_limit)


def _source_presentation(source):
    """Current registry display metadata, including old read-only projections.

    L0/L1 reconcile the persisted fields; readers never migrate or edit text.
    Only source metadata is projected here, never originals or historical rows.
    """
    from app.clhear.l1.source_registry import S, _about, source_role
    key = source.key
    entry = next((entry for entry in S if entry["key"] == key), None)
    return {"source_role": source_role(key), **({"name": entry["name"], "short_name": entry["short_name"],
            "about": _about(entry), "canonical_url": entry["canonical_url"]} if entry else {})}


def _acquisition_status(conn, source):
    from app.clhear.l1 import permissions
    if not permissions.required_for(source):
        return "public_basis" if source.license == "open" and l1_rights.republishable(source.rights_basis or "") else "unverified"
    decisions = [permissions.decision(conn, source.key, op) for op in ("acquire", "store", "parse")]
    return "permitted" if all(choice["allowed"] for choice in decisions) else "blocked"


def _text_access(conn, source, request: Request) -> dict:
    """Authentication and publisher permissions are independent requirements."""
    from app.clhear.l1 import permissions
    from app.clhear.review_access import reviewer

    if not permissions.required_for(source):
        allowed = source.license == "open" and l1_rights.republishable(source.rights_basis or "")
        return {"allowed": allowed, "internal": False,
                "reason": "Public text" if allowed else "Text access requires recorded permission."}
    public = permissions.decision(conn, source.key, "display_public")
    if public["allowed"]:
        return {**public, "internal": False}
    user = reviewer(request)
    internal = permissions.decision(conn, source.key, "display_internal")
    request.state.private_text = bool(user and internal["allowed"])
    return {**internal, "allowed": bool(user and internal["allowed"]), "internal": True,
            "reason": internal["reason"] if user else "An approved reviewer and internal display permission are required."}


def _audit_text_read(conn, request, source, version, access, route, ids):
    from app.clhear.settings import get_settings
    if get_settings().clhear_preview_mode:
        return  # Preview is a read-only projection; permission checks above still apply.
    if not access["allowed"] or not ids:
        return
    from app.clhear.l1 import permissions
    from app.clhear.accounts import current_user
    if not permissions.required_for(source):
        entry = audit.log_licensed_read(conn, source_key=source.key, rights_basis=source.rights_basis or "",
                                        clause_ids=ids, route=route)
        if entry:
            conn.commit()
        return
    user = current_user(request)
    audit.log(conn, "read.licensed_text", resource=source.key, resource_id=str(version.id),
              actor=audit.Actor(actor=user["email"] if user else "", kind="user" if user else "anonymous"),
              detail={"route": route, "source_version_id": version.id, "record_ids": ids[:50],
                      "records": len(ids), "permission_id": access.get("permission_id")})
    conn.commit()


@router.get("/api/clhear/sources")
def list_sources(publisher: str | None = None) -> list[dict]:
    """Library view: families -> members -> latest-version summary."""
    engine = get_engine()
    from app.clhear.l1.origin import corpus_sources_predicate, production_worker
    from app.clhear.l1.source_registry import FAMILIES
    from app.clhear.l1.publishers import publisher_ids
    charters = {key: charter for key, _, charter in FAMILIES}
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
                sources.c.rights_basis,
                sources.c.publisher,
                sources.c.issuer,
                sources.c.canonical_url,
                sources.c.adapter,
                sources.c.short_name,
                sources.c.about,
                sources.c.topics,
            ).join(sources, sources.c.id == family_members.c.source_id).where(corpus_sources_predicate() if production_worker() else sa.true())
        ).all()
        acquisition = {member.key: _acquisition_status(conn, member) for member in members}
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
        for row in conn.execute(sa.select(runs).where(runs.c.fleet.like("l1.%")).order_by(runs.c.id.desc())):
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
                "rights-blocked",
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
            presentation = _source_presentation(m)
            if presentation["source_role"] != "document":
                library_status = presentation["source_role"]
            elif last_status.get(m.key) == "rights-blocked":
                library_status = "rights-blocked"
            elif m.license == "restricted":
                library_status = "locked-restricted"
            elif version:
                library_status = "ingested"
            elif m.key in failed_today:
                library_status = "failed-today"
            elif m.adapter and _schedule_label(m.adapter) != "unscheduled" and m.key not in last_status:
                # No recorded execution does not establish a missed scheduler occurrence.
                library_status = "no-execution-evidence"
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
                    "publisher": m.publisher or m.issuer,
                    "publisher_ids": publisher_ids({"key": m.key}),
                    "acquisition_status": acquisition[m.key],
                    "import_status": ("stored" if version else "not_imported") if presentation["source_role"] == "document" else "not_a_document",
                    "verification_status": "inspect_version_evidence" if version and presentation["source_role"] == "document" else "unverified",
                    **presentation,
                }
            )
        out.append(
            {
                "key": family.key,
                "name": family.name,
                "scope_charter": {"registry": charters[family.key], "scope": "company_independent"} if family.key in charters else {k: v for k, v in (family.scope_charter or {}).items() if k != "partner"},
                "members": fam_members,
            }
        )
    if publisher:
        for family in out:
            family["members"] = [m for m in family["members"] if publisher in m["publisher_ids"] or publisher == m["publisher"]]
        out = [family for family in out if family["members"]]
    return out


def _resolve_version(conn, source, version_label: str | None):
    version_q = sa.select(source_versions).where(source_versions.c.source_id == source.id)
    if version_label:
        version_q = version_q.where(source_versions.c.version_label == version_label)
    else:
        version_q = version_q.where(source_versions.c.status == "in_force")
    return conn.execute(version_q.order_by(source_versions.c.id.desc()).limit(1)).first()


def _declared_source_only(engine, key: str) -> dict:
    """A discovered expected document can be inspected before any import."""
    from app.clhear.l1.inventory import source_inventory_evidence

    evidence = source_inventory_evidence(engine, key)
    if not evidence.get("name"):
        raise HTTPException(status_code=404, detail="source not found")
    return {"key": key, "name": evidence["name"], "short_name": "", "kind": "not recorded",
            "issuer": "not recorded", "jurisdiction": "not recorded", "license": "not recorded",
            "license_ref": "", "adapter": "not recorded", "canonical_url": evidence.get("canonical_url"),
            "about": "Declared by the worker inventory; no source version has been imported.",
            "topics": [], "versions": [], "changes": [], "s3_uri": "", "content_hash": "",
            "inventory": evidence, "provenance": {"text_states": [], "related_instruments": []}}


@router.get("/api/clhear/sources/{key:path}/document")
def source_document(key: str, request: Request, version_label: str | None = None) -> dict:
    """Ordered node list for reconstructing the original document view."""
    engine = get_engine()
    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == key)).first()
        if source is None:
            _declared_source_only(engine, key)
            if version_label:
                raise HTTPException(status_code=404, detail="source version not found")
            return {"source": key, "version": None, "nodes": [], "amended_refs": [], "total": 0}
        version = _resolve_version(conn, source, version_label)
        if version is None:
            if version_label:
                raise HTTPException(status_code=404, detail="source version not found")
            return {"source": key, "version": None, "nodes": [], "amended_refs": [], "total": 0}

        access = _text_access(conn, source, request)
        locked = not access["allowed"]
        # Explicit internal permission permits raw rows; public reads retain the public view.
        base = nodes_refs_select() if locked else (nodes_internal_select(conn) if access["internal"] else nodes_public_select(conn))
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
            .where(sa.literal(not locked))
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
            .where(change_events.c.new_version == version.version_label)
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
        _audit_text_read(conn, request, source, version, access, request.url.path, [r.id for r in rows])

    from app.clhear import legal
    from app.clhear.l1.inventory import source_inventory_evidence

    evidence = source_inventory_evidence(engine, key)
    publisher_verified = (evidence.get("verified") is True and evidence.get("source_version_id") == version.id
                          and evidence.get("content_hash") == version.content_hash)
    return {
        "source": key,
        "version": version.version_label,
        "source_version_id": version.id,
        "canonical_url": source.canonical_url,
        "dataset_kind": "stored_candidate",
        "real_publisher_verified": publisher_verified,
        "notice": "Stored candidate. Coverage and publisher fidelity require version-specific evidence.",
        "access": access,
        "permission_reason": access["reason"],
        "version_kind": version.version_kind,
        "as_of_date": str(version.as_of_date) if version.as_of_date else None,
        "as_published_sibling": as_published_sibling if version.version_kind != "as_published" else None,
        "retrieved_at": str(version.retrieved_at),
        "s3_uri": version.s3_uri,
        "content_hash": version.content_hash,
        "locked": locked,
        "attribution": legal.attribution_for(source.key, source.license),
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
                "source_locator": getattr(row, "source_locator", None) if not locked else None,
            }
            for row in rows
        ],
    }


@router.get("/api/clhear/sources/{key:path}/english")
def source_english(key: str, request: Request, version_label: str | None = None,
                   offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)) -> dict:
    """Read an explicit English representation; never translate during a read."""
    from app.clhear.l1 import translation
    engine = get_engine()
    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == key)).first()
        if source is None:
            _declared_source_only(engine, key)
            return {"source": key, "status": "not_imported", "english_ready": False, "segments": [], "total": 0}
        version = _resolve_version(conn, source, version_label)
        if version is None:
            if version_label:
                raise HTTPException(status_code=404, detail="source version not found")
            return {"source": key, "status": "not_imported", "english_ready": False, "segments": [], "total": 0}
        access = _text_access(conn, source, request)
    summary = translation.english_summary(engine, version.id)
    result = {**summary, "source": key, "version": version.version_label, "source_version_id": version.id,
              "source_content_hash": version.content_hash, "locked": not access["allowed"],
              "segments": [], "total": 0, "offset": offset, "limit": limit, "has_more": False}
    if not access["allowed"] or not summary.get("english_ready"):
        return result
    try:
        representation = translation.english_segments(engine, summary["view_id"], internal=access["internal"])
    except (ValueError, PermissionError):
        return {**result, "status": "unavailable", "english_ready": False,
                "finding_codes": ["english_view_binding_or_permission_changed"]}
    reused_id = representation.get("reuse_source_version_id")
    if reused_id is not None:
        with engine.connect() as conn:
            english_version = conn.execute(sa.select(source_versions).where(source_versions.c.id == reused_id)).first()
            english_source = conn.execute(sa.select(sources).where(sources.c.id == english_version.source_id)).first()
            if not _text_access(conn, english_source, request)["allowed"]:
                return {**result, "locked": True, "english_ready": False}
        result["publisher_document"] = {"source_key": english_source.key, "version_label": english_version.version_label,
                                        "source_version_id": reused_id, "content_hash": english_version.content_hash}
    rows = representation.get("segments", [])
    selected = rows[offset:offset + limit]
    with engine.connect() as conn:
        _audit_text_read(conn, request, source, version, access, request.url.path, [row["doc_node_id"] for row in selected])
    return {**result, "segments": selected, "total": len(rows), "has_more": offset + len(selected) < len(rows)}


@router.get("/api/clhear/nodes/{node_id}")
def node_inspector(node_id: int, request: Request, source_key: str | None = None, version_label: str | None = None) -> dict:
    """Intelligence payload for the hover/click inspector."""
    engine = get_engine()
    with engine.connect() as conn:
        node = conn.execute(nodes_internal_select(conn).where(doc_nodes.c.id == node_id)).first()
        if node is None:
            raise HTTPException(status_code=404, detail="node not found")
        version = conn.execute(
            sa.select(source_versions).where(source_versions.c.id == node.source_version_id)
        ).one()
        source = conn.execute(sa.select(sources).where(sources.c.id == version.source_id)).one()
        if (source_key and source_key != source.key) or (version_label and version_label != version.version_label):
            raise HTTPException(status_code=404, detail="node is not in the requested source version")
        access = _text_access(conn, source, request)
        public = bool(node.public_ok)
        readable = access["allowed"] and (access["internal"] or public)
        encoded = conn.execute(sa.select(clauses).where(clauses.c.doc_node_id == node.id)
                               .where(clauses.c.source_version_id == version.id).order_by(clauses.c.ordering)).all()
        ancestors = []
        parent_id = node.parent_id
        while parent_id is not None:
            parent = conn.execute(nodes_internal_select(conn).where(doc_nodes.c.id == parent_id)).first()
            if parent is None:
                break
            ancestors.append(
                {"id": parent.id, "node_type": parent.node_type, "ref": parent.ref,
                 "label": parent.label if readable else None, "heading": parent.heading if readable else None}
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
        while readable and walk_id and walk_id not in seen:
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
        if readable:
            _audit_text_read(conn, request, source, version, access, request.url.path, [node.id])
    return {
        "id": node.id,
        "node_type": node.node_type,
        "ref": node.ref,
        "label": node.label if readable else None,
        "heading": node.heading if readable else None,
        "raw_text": node.raw_text if readable else None,
        "source_fragment": node.source_fragment if readable else None,
        "source_locator": node.source_locator if readable else None,
        "canonical_offset_unit": "unicode_code_points",
        "locked": not readable,
        "permission_reason": access["reason"],
        "source_version_id": version.id,
        "clauses": [{"id": c.id, "ref": c.ref, "path": c.path if readable else None, "ordering": c.ordering,
                     "span_start": c.span_start, "span_end": c.span_end, "text_hash": c.text_hash,
                     "source_version_id": c.source_version_id} for c in encoded],
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
        "permalink": "/l1?" + urlencode({"source": source.key, "version": version.version_label, "node": node.id}),
        "ancestors": ancestors,
        "indexed_as": indexed_as,
        "changes": changes,
    }


@router.get("/api/clhear/sources/{key:path}/clauses")
def source_clauses(
    key: str,
    request: Request,
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
            if version_label:
                raise HTTPException(status_code=404, detail="source version not found")
            return {"source": key, "version": None, "clauses": [], "total": 0}

        # Restricted discipline (I8): text flows only through clauses_public_select, and only
        # when the file is open *and* the rights basis allows republication.
        access = _text_access(conn, source, request)
        open_text = access["allowed"]
        base = (sa.select(clauses) if access["internal"] else clauses_public_select()) if open_text else clause_refs_select()
        rows = conn.execute(
            base.where(clauses.c.source_version_id == version.id)
            .order_by(clauses.c.ordering)
            .limit(limit)
            .offset(offset)
        ).all()
        total = conn.execute(
            sa.select(sa.func.count()).select_from(clauses).where(clauses.c.source_version_id == version.id)
        ).scalar_one()
        _audit_text_read(conn, request, source, version, access, request.url.path, [r.id for r in rows])
    return {
        "source": key,
        "version": version.version_label,
        "retrieved_at": str(version.retrieved_at),
        "s3_uri": version.s3_uri,
        "content_hash": version.content_hash,
        "locked": not open_text,
        "source_version_id": version.id,
        "permission_reason": access["reason"],
        "rights_basis": getattr(source, "rights_basis", None),
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
        "source_version_id": v.id,
        "version_label": v.version_label,
        "version_kind": v.version_kind,
        "as_of_date": str(v.as_of_date) if v.as_of_date else None,
        "retrieved_at": str(v.retrieved_at),
        "status": v.status,
        "content_hash": v.content_hash,
        "s3_uri": v.s3_uri,
    }


@router.get("/api/clhear/sources/{key:path}/evals")
def source_evals(key: str, version_label: str | None = None) -> dict:
    """E1–E7 scorecard + last fetch / artifact for the Evidence tab."""
    from app.clhear.platform import evals as l1_evals

    engine = get_engine()
    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == key)).first()
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        version = _resolve_version(conn, source, version_label)
        if version_label and version is None:
            raise HTTPException(status_code=404, detail="source version not found")
        last_run = None
        for row in conn.execute(sa.select(runs).where(runs.c.fleet.like("l1.%"))
                                .where(runs.c.inputs["source"].as_string() == key)
                                .order_by(runs.c.id.desc())):
            inputs = row.inputs if isinstance(row.inputs, dict) else json.loads(row.inputs or "{}")
            if inputs.get("source") == key:
                outputs = _display_outputs(row)
                if version and (outputs.get("version") != version.version_label or outputs.get("content_hash") != version.content_hash):
                    continue
                last_run = {
                    "run_id": row.id,
                    "status": outputs.get("status"),
                    "ts": str(row.created_at),
                    "coverage": outputs.get("coverage"),
                    "freshness": outputs.get("freshness"),
                    "error": outputs.get("error"),
                    "note": outputs.get("note"),
                    "duration_ms": row.duration_ms,
                    "fleet": row.fleet,
                    "stages": outputs.get("stages", []),
                }
                break
    card = l1_evals.latest_source_scorecard(engine, key, source_version_id=version.id if version else None)
    from app.clhear.l1.inventory import source_inventory_evidence
    from app.clhear.l1.workflow import workflow_summary

    inventory = source_inventory_evidence(engine, key)
    inventory_matches = bool(version and inventory.get("source_version_id") == version.id
                             and inventory.get("content_hash") == version.content_hash)
    return {
        "source": key,
        "locked": source.license != "open",
        "version": version.version_label if version else None,
        "source_version_id": version.id if version else None,
        "s3_uri": version.s3_uri if version else "",
        "content_hash": version.content_hash if version else "",
        "retrieved_at": str(version.retrieved_at) if version else None,
        "last_run": last_run,
        "scorecard": card,
        "inventory": inventory,
        "inventory_matches_selected_version": inventory_matches,
        "publisher_checked_at": inventory.get("publisher_checked_at") if inventory_matches else None,
        "artifact_checked_at": inventory.get("artifact_checked_at") if inventory_matches else None,
        "freshness_basis": inventory.get("freshness_basis") if inventory_matches else None,
        "workflow": workflow_summary(engine, source_key=key),
        "l2_ready": False,
        "readiness_reason": "L1 requires a complete scope manifest and publisher comparison before downstream acceptance.",
    }


@router.get("/api/clhear/sources/{key:path}/inventory")
def source_inventory(key: str) -> dict:
    """Worker audit for a declared source, including not-yet-imported sources."""
    from app.clhear.l1.inventory import source_inventory_evidence

    return source_inventory_evidence(get_engine(), key)


@router.get("/api/clhear/sources/{key:path}")
def source_detail(key: str) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == key)).first()
        if source is None:
            return _declared_source_only(engine, key)
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
    from app.clhear import legal
    from app.clhear.l1.inventory import source_inventory_evidence

    return {
        "key": source.key,
        "name": source.name,
        "short_name": source.short_name,
        "kind": source.kind,
        "issuer": source.issuer,
        "jurisdiction": source.jurisdiction,
        "license": source.license,
        "license_ref": source.license_ref,
        "attribution": legal.attribution_for(source.key, source.license),
        "adapter": source.adapter,
        "canonical_url": source.canonical_url,
        "about": source.about,
        **_source_presentation(source),
        "topics": source.topics if isinstance(source.topics, list) else json.loads(source.topics or "[]"),
        "versions": [_version_dict(v) for v in versions],
        "changes": [_change_dict(c) for c in changes],
        "s3_uri": versions[0].s3_uri if versions else "",
        "content_hash": versions[0].content_hash if versions else "",
        "inventory": source_inventory_evidence(engine, key),
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
    "rights-blocked": "warning",
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
    """A missing completion record is unknown, not proof of a crash."""
    outputs = dict(_outputs_of(row))
    if outputs.get("status") != "running":
        return outputs
    created = row.created_at
    if created is not None and getattr(created, "tzinfo", None) is None:
        created = created.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - created) if created is not None else _STALE_RUNNING
    if age >= _STALE_RUNNING:
        outputs["status"] = "unknown"
        outputs.setdefault("note", "No recent completion or heartbeat evidence; inspect the worker before retrying.")
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
    try:
        from app.clhear import ai_ops

        items.extend(ai_ops.activity_items(get_engine(), limit=limit))
    except Exception:
        pass
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
        "duration_basis": "sum_of_recorded_task_durations_not_wall_clock",
        "dependency_basis": "legacy_run_order_not_recorded_dependencies",
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


@router.get("/l1", response_class=HTMLResponse, include_in_schema=False)
def l1_browser() -> HTMLResponse:
    """HLD v2 §4.1 L1 UI: browse by jurisdiction / regulator / instrument,
    family tree, change timeline, rights badge, watch this instrument."""
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
