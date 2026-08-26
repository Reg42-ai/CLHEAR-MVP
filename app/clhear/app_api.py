"""Versioned app-read API: /v1/releases…

L1 resources are live. L2–L8 return 501 + layer_status=not_published.
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
from app.clhear.layers import LAYER_CATALOG, normalize_layer, not_published_body

router = APIRouter(prefix="/v1", tags=["app-api"])


def _engine():
    return get_engine()


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
    if not release_store.get_release(release_id, engine=_engine()):
        raise HTTPException(404, f"Release {release_id} not found")
    engine = _engine()
    q = sa.select(change_events).order_by(change_events.c.id.desc()).limit(limit)
    with engine.connect() as conn:
        rows = conn.execute(q).mappings().all()
    events = []
    for row in rows:
        item = dict(row)
        for k, v in list(item.items()):
            if hasattr(v, "isoformat"):
                item[k] = v.isoformat()
        events.append(item)
    if since:
        events = [e for e in events if str(e.get("detected_at") or e.get("id")) > since]
    return {"release_id": release_id, "since": since, "events": events}


@router.get("/releases/{release_id}/{layer}/status")
def layer_status(release_id: str, layer: str, app: dict = Depends(require_app)) -> dict:
    require_scope(app, "read:l1")
    if not release_store.get_release(release_id, engine=_engine()):
        raise HTTPException(404, f"Release {release_id} not found")
    code = normalize_layer(layer)
    if not code:
        raise HTTPException(404, f"Unknown layer {layer}")
    meta = LAYER_CATALOG[code]
    if not meta["published"]:
        raise HTTPException(status_code=501, detail=not_published_body(code))
    return {"release_id": release_id, "layer": code, "layer_status": "published", **meta}


def _require_published_l1(release_id: str, layer: str, app: dict) -> None:
    require_scope(app, "read:l1")
    if not release_store.get_release(release_id, engine=_engine()):
        raise HTTPException(404, f"Release {release_id} not found")
    code = normalize_layer(layer)
    if code is None:
        raise HTTPException(404, f"Unknown layer {layer}")
    if code != "L1":
        raise HTTPException(status_code=501, detail=not_published_body(code))


@router.get("/releases/{release_id}/{layer}/families")
def l1_families(release_id: str, layer: str, app: dict = Depends(require_app)) -> dict:
    _require_published_l1(release_id, layer, app)
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(sa.select(source_families).order_by(source_families.c.key)).mappings().all()
    items = []
    for row in rows:
        charter = row["scope_charter"]
        if isinstance(charter, str):
            charter = json.loads(charter)
        items.append({"id": row["id"], "key": row["key"], "name": row["name"], "scope_charter": charter})
    return {"release_id": release_id, "layer": "L1", "families": items}


@router.get("/releases/{release_id}/{layer}/sources")
def l1_sources(release_id: str, layer: str, app: dict = Depends(require_app)) -> dict:
    _require_published_l1(release_id, layer, app)
    from app.clhear.l1.routes import list_sources

    return {"release_id": release_id, "layer": "L1", "sources": list_sources()}


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
    _require_published_l1(release_id, layer, app)
    engine = _engine()
    stmt = clauses_public_select()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(sa.or_(clauses.c.ref.ilike(like), clauses.c.text.ilike(like), clauses.c.path.ilike(like)))
    if source_key:
        stmt = (
            stmt.join(source_versions, source_versions.c.id == clauses.c.source_version_id)
            .join(sources, sources.c.id == source_versions.c.source_id)
            .where(sources.c.key == source_key)
        )
    count_stmt = sa.select(sa.func.count()).select_from(stmt.subquery())
    with engine.connect() as conn:
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
    _require_published_l1(release_id, layer, app)
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(sa.select(change_events).order_by(change_events.c.id.desc()).limit(limit)).mappings().all()
    events = []
    for row in rows:
        item = dict(row)
        for k, v in list(item.items()):
            if hasattr(v, "isoformat"):
                item[k] = v.isoformat()
        events.append(item)
    return {"release_id": release_id, "layer": "L1", "events": events}


@router.get("/releases/{release_id}/{layer}/snapshot")
def l1_snapshot(release_id: str, layer: str, app: dict = Depends(require_app)) -> dict:
    """Bulk download: signed URL to the L1 snapshot (or local file URI in tests)."""
    _require_published_l1(release_id, layer, app)
    man = release_store.get_release(release_id, engine=_engine()) or {}
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
        return {"release_id": release_id, "layer": "L1", "url": url, "expires_in": 3600, "snapshot_uri": uri}
    return {"release_id": release_id, "layer": "L1", "url": uri, "expires_in": 0, "snapshot_uri": uri}


@router.get("/releases/{release_id}/{layer}/{resource}")
def reserved_layer_resource(
    release_id: str,
    layer: str,
    resource: str,
    app: dict = Depends(require_app),
) -> dict:
    """Catch-all so /l2/obligations etc. feature-detect as not_published."""
    require_scope(app, "read:l1")
    if not release_store.get_release(release_id, engine=_engine()):
        raise HTTPException(404, f"Release {release_id} not found")
    code = normalize_layer(layer)
    if not code:
        raise HTTPException(404, f"Unknown layer {layer}")
    meta = LAYER_CATALOG[code]
    if not meta["published"]:
        raise HTTPException(status_code=501, detail=not_published_body(code))
    raise HTTPException(404, f"Unknown {code} resource {resource}")
