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
