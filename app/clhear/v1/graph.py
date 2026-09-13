"""Graph + vector-index routes (HLD v2 I7, §8 item 10). Read-only and open (I9);
the rebuild endpoint needs an app key because it does work on behalf of the
caller, not because the graph is secret."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.clhear.app_auth import require_app
from app.clhear.db import get_engine
from app.clhear.platform import embeddings, graph

router = APIRouter(tags=["graph"])


def _timed(response: Response, started: float, name: str) -> None:
    ms = (time.perf_counter() - started) * 1000
    response.headers["Server-Timing"] = f"{name};dur={ms:.1f}"
    response.headers["X-Query-Ms"] = f"{ms:.1f}"


@router.get("/graph/status")
def graph_status() -> dict:
    engine = get_engine()
    return {**graph.status(engine), "index": embeddings.index_status(engine)}


@router.get("/graph/obligations/{obligation_id:path}/evidence")
def evidence_for(obligation_id: str, response: Response) -> dict:
    """One hop toward the evidence: obligation → asserting clauses → their sources."""
    started = time.perf_counter()
    out = graph.get_graph(get_engine()).evidence_for(obligation_id)
    if out is None:
        raise HTTPException(status_code=404, detail=f"unknown obligation {obligation_id}")
    _timed(response, started, "evidence_for")
    return out


@router.get("/graph/clauses/{clause_id}/derived")
def derived_from(clause_id: str, response: Response) -> dict:
    """One hop toward the derived: clause → obligations asserted on it → what they require / trigger / satisfy."""
    started = time.perf_counter()
    out = graph.get_graph(get_engine()).derived_from(clause_id)
    if out is None:
        raise HTTPException(status_code=404, detail=f"unknown clause {clause_id}")
    _timed(response, started, "derived_from")
    return out


@router.get("/graph/nodes/{node_id:path}")
def neighbourhood(node_id: str, response: Response) -> dict:
    started = time.perf_counter()
    g = graph.get_graph(get_engine())
    if not hasattr(g, "neighbourhood"):
        raise HTTPException(status_code=501, detail="neighbourhood is served by the explore API on this backend")
    out = g.neighbourhood(node_id)
    if out is None:
        raise HTTPException(status_code=404, detail=f"unknown node {node_id}")
    _timed(response, started, "neighbourhood")
    return out


@router.get("/graph/subgraph")
def subgraph(response: Response, focus: str | None = Query(default=None), depth: int = Query(default=2, ge=1, le=4),
             layers: str = Query(default=""), grouping: str = Query(default="items"), expand: str = Query(default=""),
             max_nodes: int = Query(default=1500, ge=10, le=4000)) -> dict:
    """What the map draws (HLD v2 §5): ``focus`` plus ``depth`` hops — or the whole
    projection without a focus — folded to the granularity ``grouping`` names
    (``programs`` | ``areas`` | ``items`` | ``everything``), with the groups in
    ``expand`` opened. For a blueprint focus the coverage gaps are drawn too, joined
    to the blueprint by ``gap`` edges, because a program map that hides what is not
    covered would be a lie."""
    started = time.perf_counter()
    engine = get_engine()
    g = graph.get_graph(engine)
    if not hasattr(g, "subgraph"):
        raise HTTPException(status_code=501, detail="the map is served from the local projection on this backend")
    if grouping not in graph.GROUPINGS:
        raise HTTPException(status_code=422, detail=f"grouping must be one of {sorted(graph.GROUPINGS)}")
    wanted = {l.strip().upper() for l in layers.split(",") if l.strip()} or None
    opened = {e.strip() for e in expand.split(",") if e.strip()}
    extra = _blueprint_gaps(engine, focus) if focus and focus.startswith("BLU-") else None
    out = g.subgraph(focus=focus, depth=depth, layers=wanted, grouping=grouping, expand=opened, max_nodes=max_nodes, extra=extra)
    if out is None:
        raise HTTPException(status_code=404, detail=f"unknown node {focus}")
    _timed(response, started, "subgraph")
    return out


def _blueprint_gaps(engine, blueprint_id: str) -> dict[str, dict]:
    """Obligations the blueprint's composition lists as gaps: not in the projection
    (nothing satisfies them), so the map adds them from the stored composition."""
    import json

    import sqlalchemy as sa

    from app.clhear.derived_models import blueprints

    with engine.connect() as conn:
        row = conn.execute(sa.select(blueprints.c.composition).where(blueprints.c.stable_id == blueprint_id)).first()
    if row is None:
        return {}
    comp = row[0]
    if isinstance(comp, str):
        try:
            comp = json.loads(comp)
        except ValueError:
            return {}
    return {c["obligation_id"]: {"from": blueprint_id, "rel": "gap", "kind": "obligation", "layer": "L2", "gap": True,
                                 "label": c.get("title") or c["obligation_id"], "stable_id": c.get("stable_id")}
            for c in (comp or {}).get("coverage") or [] if c.get("state") == "gap" and c.get("obligation_id")}


@router.get("/graph/search")
def vector_search(q: str = Query(min_length=2), limit: int = Query(default=10, ge=1, le=50), response: Response = None) -> dict:
    """Nearest clauses by embedding — the vector leg on its own, for inspection."""
    started = time.perf_counter()
    engine = get_engine()
    emb = embeddings.embedder()
    hits = embeddings.nearest(engine, emb.embed([q])[0], limit=limit, model=emb.name)
    if response is not None:
        _timed(response, started, "vector_search")
    return {"q": q, "model": emb.name, "hits": [{"clause_id": cid, "score": round(score, 4)} for cid, score in hits]}


@router.post("/graph/rebuild", dependencies=[Depends(require_app)])
def rebuild(index: bool = Query(default=True), release: str = "") -> dict:
    engine = get_engine()
    out = {"graph": graph.rebuild(engine, release=release, trigger="api")}
    if index:
        out["index"] = embeddings.rebuild_index(engine, release=release, trigger="api")
    return out
