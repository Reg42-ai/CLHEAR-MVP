"""Versioned app-read API: /v1/releases…

L1 resources are pinned to an accepted manifest. Downstream previews stay explicitly labeled.
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query

from app.clhear import releases as release_store
from app.clhear.app_auth import require_app, require_scope
from app.clhear.db import get_engine
from app.clhear.l1.models import change_events, clauses, source_families, source_versions, sources
from app.clhear.l1.public import clauses_public_select
from app.clhear.layers import LAYER_CATALOG, layer_public_meta, normalize_layer, not_published_body, status_banner

router = APIRouter(prefix="/v1", tags=["app-api"])

# Preview-layer collection names (resource -> layer) for the /v1 catch-all.
PREVIEW_RESOURCES = {
    "obligations": "L2",
    "building-blocks": "L3",
    "profiles": "L4",
    "activities": "L5",
    "programs": "L6",
    "risk-scores": "L7",
    "benchmarks": "L8",
}


def _engine():
    return get_engine()


@router.get("/layers")
def layers_catalog(app: dict = Depends(require_app)) -> dict:
    """The 8-layer contract: statuses + derivation contracts (feature detection)."""
    require_scope(app, "read:l1")
    from app.clhear.layers import LAYER_ORDER

    manifest = release_store.get_latest(_engine()) or {}
    promoted = bool(release_store.get_promotion(manifest.get("id", "")))
    return {"layers": [{**layer_public_meta(code), "published": promoted and code in manifest.get("layers", [])}
                       for code in LAYER_ORDER]}


@router.get("/releases/latest")
def latest_release(app: dict = Depends(require_app)) -> dict:
    require_scope(app, "read:l1")
    man = release_store.get_latest(_engine())
    if not man:
        raise HTTPException(404, "No CLHEAR release has been published yet")
    return man


@router.get("/releases")
def list_releases(app: dict = Depends(require_app)) -> dict:
    require_scope(app, "read:l1")
    items = release_store.list_releases(_engine())
    return {"releases": items, "count": len(items)}


@router.get("/releases/{release_id}")
def get_release(release_id: str, app: dict = Depends(require_app)) -> dict:
    require_scope(app, "read:l1")
    man = release_store.get_release(release_id, engine=_engine())
    if not man:
        raise HTTPException(404, f"Release {release_id} not found")
    return man


@router.post("/releases/{release_id}/pin")
def pin_release(release_id: str, app: dict = Depends(require_app)) -> dict:
    require_scope(app, "read:l1")
    try:
        return release_store.pin_release(release_id, engine=_engine())
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/releases/{release_id}/changelog")
def changelog(
    release_id: str,
    since: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    app: dict = Depends(require_app),
) -> dict:
    require_scope(app, "read:l1")
    manifest = _require_published_l1(release_id, "L1", app)
    with _engine().connect() as conn:
        ids = _release_bindings(conn, manifest)
        rows = conn.execute(_release_change_query(ids).order_by(change_events.c.id.desc()).limit(limit)).mappings().all()
    events = []
    for row in rows:
        item = dict(row)
        for k, v in list(item.items()):
            if hasattr(v, "isoformat"):
                item[k] = v.isoformat()
        events.append(item)
    if since:
        events = [e for e in events if str(e.get("detected_at") or e.get("id")) > since]
    return {"release_id": release_id, **_l1_label(manifest), "since": since, "events": events}


@router.get("/releases/{release_id}/{layer}/status")
def layer_status(release_id: str, layer: str, app: dict = Depends(require_app)) -> dict:
    require_scope(app, "read:l1")
    manifest = release_store.get_release(release_id, engine=_engine())
    if not manifest:
        raise HTTPException(404, f"Release {release_id} not found")
    code = normalize_layer(layer)
    if not code:
        raise HTTPException(404, f"Unknown layer {layer}")
    meta = LAYER_CATALOG[code]
    included = code in manifest.get("layers", []) and (code != "L1" or (manifest.get("acceptance") or {}).get("passed") is True)
    published = included and bool(release_store.get_promotion(manifest["id"]))
    status = "published" if published else "candidate" if included else meta["status"] if code != "L1" and meta["status"] in ("derived", "curated", "computed") else "not_published"
    body = {**layer_public_meta(code), "release_id": release_id, "layer": code,
            "layer_status": status, "status": status, "published": published}
    if status not in ("published", "not_published", "candidate"):
        body["banner"] = status_banner(code)
    return body


def _require_published_l1(release_id: str, layer: str, app: dict) -> dict:
    require_scope(app, "read:l1")
    manifest = release_store.get_release(release_id, engine=_engine())
    if not manifest:
        raise HTTPException(404, f"Release {release_id} not found")
    code = normalize_layer(layer)
    if code is None:
        raise HTTPException(404, f"Unknown layer {layer}")
    if code != "L1":
        raise HTTPException(status_code=501, detail=not_published_body(code))
    if ("L1" not in manifest.get("layers", []) or not (manifest.get("acceptance") or {}).get("passed")
            or manifest.get("status") in {"blocked", "live", "preview"}):
        raise HTTPException(409, {"layer_status": "not_published", "detail": "This manifest has no accepted L1 output. Browse the candidate in the source review workspace."})
    bindings = (manifest.get("projection") or {}).get("bindings")
    if not isinstance(bindings, list) or not bindings:
        raise HTTPException(409, "Release lacks exact source-version bindings; use its verified immutable artifact")
    return manifest


def _l1_label(manifest):
    promoted = bool(release_store.get_promotion(manifest["id"]))
    return {"layer_status": "published" if promoted else "candidate", "published": promoted,
            "data_binding": "manifest_source_versions"}


def _release_bindings(conn, manifest, *, require_current=False):
    """Never substitute live versions for a frozen release's source bindings."""
    from app.clhear.l1.release_snapshot import current_bindings
    expected = manifest["projection"]["bindings"]
    try:
        ids = [int(row["source_version_id"]) for row in expected]
        wanted = {row["source_key"]: (int(row["source_version_id"]), row["content_hash"]) for row in expected}
    except (KeyError, TypeError, ValueError):
        raise HTTPException(409, "Invalid release source-version bindings")
    if len(wanted) != len(expected) or len(set(ids)) != len(ids):
        raise HTTPException(409, "Ambiguous release source-version bindings")
    actual = {row.source_key: (row.id, row.content_hash) for row in conn.execute(sa.select(
        sources.c.key.label("source_key"), source_versions.c.id, source_versions.c.content_hash,
    ).join(source_versions, source_versions.c.source_id == sources.c.id).where(source_versions.c.id.in_(ids)))}
    if actual != wanted:
        raise HTTPException(409, "The exact release versions are unavailable or changed; use the verified immutable artifact")
    if require_current:
        current = {row["source_key"]: (row["source_version_id"], row["content_hash"]) for row in current_bindings(conn)}
        if current != wanted:
            raise HTTPException(409, "Live metadata no longer matches this frozen release; use the verified immutable artifact")
    return ids


def _release_change_query(ids):
    return sa.select(change_events).join(source_versions, sa.and_(
        source_versions.c.source_id == change_events.c.source_id,
        source_versions.c.version_label == change_events.c.new_version,
    )).where(source_versions.c.id.in_(ids))


@router.get("/releases/{release_id}/{layer}/families")
def l1_families(release_id: str, layer: str, app: dict = Depends(require_app)) -> dict:
    manifest = _require_published_l1(release_id, layer, app)
    engine = _engine()
    with engine.connect() as conn:
        ids = _release_bindings(conn, manifest, require_current=True)
        family_ids = sa.select(sources.c.family_id).join(source_versions, source_versions.c.source_id == sources.c.id).where(source_versions.c.id.in_(ids))
        rows = conn.execute(sa.select(source_families).where(source_families.c.id.in_(family_ids)).order_by(source_families.c.key)).mappings().all()
    items = []
    for row in rows:
        charter = row["scope_charter"]
        if isinstance(charter, str):
            charter = json.loads(charter)
        items.append({"id": row["id"], "key": row["key"], "name": row["name"], "scope_charter": charter})
    return {"release_id": release_id, "layer": "L1", **_l1_label(manifest), "families": items}


@router.get("/releases/{release_id}/{layer}/sources")
def l1_sources(release_id: str, layer: str, app: dict = Depends(require_app)) -> dict:
    manifest = _require_published_l1(release_id, layer, app)
    from app.clhear.l1.routes import list_sources
    with _engine().connect() as conn:
        _release_bindings(conn, manifest, require_current=True)
    allowed = {row["source_key"] for row in manifest["projection"]["bindings"]}
    families = []
    for family in list_sources():
        members = [member for member in family.get("members", []) if member["key"] in allowed]
        if members:
            families.append({**family, "members": members})
    # Validate after the live metadata query too; concurrent ingestion cannot
    # silently change which versions this release-labeled response describes.
    with _engine().connect() as conn:
        _release_bindings(conn, manifest, require_current=True)
    return {"release_id": release_id, "layer": "L1", **_l1_label(manifest), "sources": families,
            "metadata_state": "current_metadata_for_matching_release_bindings"}


@router.get("/releases/{release_id}/{layer}/clauses")
def l1_clauses(
    release_id: str,
    layer: str,
    q: str | None = Query(default=None),
    source_key: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    app: dict = Depends(require_app),
) -> dict:
    manifest = _require_published_l1(release_id, layer, app)
    engine = _engine()
    with engine.connect() as conn:
        ids = _release_bindings(conn, manifest)
        stmt = clauses_public_select(conn).where(clauses.c.source_version_id.in_(ids))
        if q:
            like = f"%{q}%"
            stmt = stmt.where(sa.or_(clauses.c.ref.ilike(like), clauses.c.text.ilike(like), clauses.c.path.ilike(like)))
        if source_key:
            stmt = stmt.where(clauses.c.source_version_id.in_([
                row["source_version_id"] for row in manifest["projection"]["bindings"] if row["source_key"] == source_key]))
        count_stmt = sa.select(sa.func.count()).select_from(stmt.subquery())
        total = int(conn.execute(count_stmt).scalar() or 0)
        rows = conn.execute(stmt.order_by(clauses.c.id).limit(limit).offset(offset)).mappings().all()
    items = []
    for row in rows:
        items.append(
            {
                "id": row["id"],
                "source_version_id": row["source_version_id"],
                "doc_node_id": row["doc_node_id"],
                "ref": row["ref"],
                "path": row["path"],
                "ordering": row["ordering"],
                "text": row["text"],
                "text_hash": row["text_hash"],
            }
        )
    return {
        "release_id": release_id,
        "layer": "L1",
        **_l1_label(manifest),
        "total": total,
        "limit": limit,
        "offset": offset,
        "clauses": items,
    }


@router.get("/releases/{release_id}/{layer}/change-events")
def l1_change_events(
    release_id: str,
    layer: str,
    limit: int = Query(default=50, ge=1, le=200),
    app: dict = Depends(require_app),
) -> dict:
    manifest = _require_published_l1(release_id, layer, app)
    engine = _engine()
    with engine.connect() as conn:
        ids = _release_bindings(conn, manifest)
        rows = conn.execute(_release_change_query(ids).order_by(change_events.c.id.desc()).limit(limit)).mappings().all()
    events = []
    for row in rows:
        item = dict(row)
        for k, v in list(item.items()):
            if hasattr(v, "isoformat"):
                item[k] = v.isoformat()
        events.append(item)
    return {"release_id": release_id, "layer": "L1", **_l1_label(manifest), "events": events}


@router.get("/releases/{release_id}/{layer}/snapshot")
def l1_snapshot(release_id: str, layer: str, app: dict = Depends(require_app)) -> dict:
    """Bulk download: signed URL to the L1 snapshot (or local file URI in tests)."""
    man = _require_published_l1(release_id, layer, app)
    from app.clhear.l1 import permissions
    if man.get("audience") != "public":
        raise HTTPException(403, "Restricted review snapshots are not available through an app-key public download")
    with _engine().connect() as conn:
        ids = _release_bindings(conn, man)
        source_rows = conn.execute(sa.select(sources).join(source_versions, source_versions.c.source_id == sources.c.id)
                                   .where(source_versions.c.id.in_(ids))).mappings().all()
        for source in source_rows:
            if permissions.required_for(source) and any(not permissions.decision(conn, source["key"], op)["allowed"]
                                                       for op in ("display_public", "redistribute")):
                raise HTTPException(403, "Current permissions do not allow public snapshot redistribution")
    uri = ((man.get("l1") or {}).get("snapshot_uri")) or ""
    if uri.startswith("s3://"):
        import boto3
        from app.clhear.settings import get_settings

        bucket, key = uri[len("s3://") :].split("/", 1)
        url = boto3.client("s3", region_name=get_settings().aws_region).generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=3600,
        )
        return {"release_id": release_id, "layer": "L1", **_l1_label(man), "url": url, "expires_in": 3600, "snapshot_uri": uri}
    raise HTTPException(409, "A verified downloadable public artifact is not available")


@router.post("/blueprint")
async def blueprint(request_body: dict, app: dict = Depends(require_app)) -> dict:
    """Tailored compliance-program blueprint.

    Body (single entity): {"attributes": {...L4 schema...}, "activities": [ids]|null}
    Body (group):         {"entities": [{"name", "attributes", "activities"?}, ...]}

    Consolidated CLHEAR obligations resolve per jurisdiction set: a group with
    US+EU+UK entities gets the full union (common core + per-jurisdiction
    deltas); an EU+UK-only group gets a strictly lighter resolution; each
    entity additionally gets its own single-jurisdiction view. The claim scope
    is always explicit — never "global compliance".
    """
    require_scope(app, "read:l1")
    from app.clhear import legal
    from app.clhear.l2.concepts import resolve_all
    from app.clhear.l6.composer import compose

    engine = _engine()
    latest = release_store.get_latest(engine) or {}
    release = latest.get("id", "")

    entities = request_body.get("entities")
    if entities is not None:
        if not isinstance(entities, list) or not entities or not all(
            isinstance(e, dict) and isinstance(e.get("attributes"), dict) for e in entities
        ):
            raise HTTPException(422, "body.entities must be a non-empty list of {name, attributes[, activities]}")
        entity_results = []
        union_jurisdictions: set[str] = set()
        all_sources: set[str] = set()
        for entity in entities:
            try:
                from app.clhear.l4.licenses import validate_authorisations

                validate_authorisations(engine, entity["attributes"])
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            bp = compose(
                engine,
                {"attributes": entity["attributes"], "activities": entity.get("activities")},
                requested_by=f"{app['app_id']}:{entity.get('name', 'entity')}",
                release=release,
            )
            jurs = list(entity["attributes"].get("jurisdictions", []))
            union_jurisdictions.update(jurs)
            all_sources.update(c["source_key"] for c in bp["coverage"])
            from app.clhear.observations import attach_performance

            attach_performance(engine, bp)
            entity_results.append(
                {
                    "name": entity.get("name", "entity"),
                    "jurisdictions": jurs,
                    "blueprint": bp,
                    # Per-entity view: only this entity's facets — never heavier
                    # than its own jurisdictions require.
                    "consolidated": [r for r in resolve_all(engine, jurs) if r.get("resolvable")],
                }
            )
        group_resolutions = [r for r in resolve_all(engine, sorted(union_jurisdictions)) if r.get("resolvable")]
        return {
            "mode": "group",
            "release": release,
            "engine_version": entity_results[0]["blueprint"]["engine_version"] if entity_results else "",
            "group": {
                "jurisdictions": sorted(union_jurisdictions),
                "consolidated": group_resolutions,
                "note": "Group view is the UNION of entity jurisdictions: common core + per-jurisdiction "
                "deltas. Each entity's own view below is intentionally lighter — build to the entity "
                "view per entity, to the group view for shared platforms.",
            },
            "entities": entity_results,
            "layer_status": {"L2": "derived", "L3": "curated", "L5": "curated", "L6": "computed"},
            "legal": legal.api_legal_block(sorted(all_sources)),
        }

    attributes = request_body.get("attributes")
    if not isinstance(attributes, dict) or not attributes:
        raise HTTPException(422, "body.attributes (profile facts) is required — see GET /v1/layers l4 schema")
    try:
        from app.clhear.l4.licenses import validate_authorisations

        validate_authorisations(engine, attributes)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    activities = request_body.get("activities")
    if activities is not None and not isinstance(activities, list):
        raise HTTPException(422, "body.activities must be a list of activity ids or null for all")
    result = compose(
        engine,
        {"attributes": attributes, "activities": activities},
        requested_by=app["app_id"],
        release=release,
    )
    result["consolidated"] = [
        r for r in resolve_all(engine, list(attributes.get("jurisdictions", []))) if r.get("resolvable")
    ]
    result["layer_status"] = {"L2": "derived", "L3": "curated", "L5": "curated", "L6": "computed"}
    result["legal"] = legal.api_legal_block(sorted({c["source_key"] for c in result["coverage"]}))
    from app.clhear.observations import attach_performance

    attach_performance(engine, result)
    return result


@router.post("/observations")
def post_observation(body: dict, app: dict = Depends(require_app)) -> dict:
    """One observation. MCP structuredContent and A2A DataPart both arrive as this JSON.

    Unmapped vendor words are stored. They do not change the compliance score.
    """
    require_scope(app, "read:l1")
    from app.clhear.observations import ObservationRejected, accept_observation

    try:
        return accept_observation(_engine(), body)
    except ObservationRejected as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/releases/{release_id}/{layer}/{resource}")
def reserved_layer_resource(
    release_id: str,
    layer: str,
    resource: str,
    app: dict = Depends(require_app),
) -> dict:
    """Preview layers answer clearly-labeled real data (derived / curated /
    computed); locked layers stay not_published (501). Clients MUST branch on
    layer_status."""
    require_scope(app, "read:l1")
    if not release_store.get_release(release_id, engine=_engine()):
        raise HTTPException(404, f"Release {release_id} not found")
    code = normalize_layer(layer)
    if not code:
        raise HTTPException(404, f"Unknown layer {layer}")
    meta = LAYER_CATALOG[code]
    if meta["published"]:
        raise HTTPException(404, f"Unknown {code} resource {resource}")
    if meta["status"] in ("derived", "curated", "computed") and PREVIEW_RESOURCES.get(resource) == code:
        from app.clhear import layer_service

        body = {
            "release_id": release_id,
            "layer": code,
            "layer_status": meta["status"],
            "banner": status_banner(code),
        }
        if code == "L2":
            body["registry"] = layer_service.obligation_items(_engine())
        else:
            body["items"] = layer_service.layer_items(_engine(), code)
        if code == "L7":
            body["view"] = "enforcement exposure"
            body["moves_with_observations"] = False
        return body
    raise HTTPException(status_code=501, detail=not_published_body(code))
