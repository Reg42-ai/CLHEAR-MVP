"""Interoperability API (standard §8; item 16) — all agnostic-mode, no key needed.

    GET  /interop                               what is here and how to use it
    GET  /interop/crosswalks                    frameworks, rights basis, row counts
    GET  /interop/crosswalks/{framework}        every block ↔ identifier row for a framework
    GET  /interop/crosswalks/{framework}/{ref}  blocks for one identifier (+ the obligations that require them)
    GET  /interop/blocks/{BLK-id}/crosswalk
    GET  /interop/context.jsonld                the JSON-LD @context
    GET  /interop/jsonld/{id}                   any public id as JSON-LD (OBL-, BLK-, ACT-, PRF-, BLU-, WHY-)
    GET  /interop/schema.graphql                the GraphQL SDL
    POST /graphql · GET /graphql?query=         GraphQL over the open layers (depth ≤ 8, lists ≤ 200)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.clhear.db import get_engine
from app.clhear.interop import crosswalks, graphql_api, jsonld

router = APIRouter(tags=["interop"])
JSONLD = "application/ld+json"


@router.get("/interop")
def index() -> dict:
    return {
        "oscal": {"components": "/l6/export/oscal/components", "blueprint": "/l6/blueprints/{BLU-id}/export?format=oscal", "version": "1.1.2"},
        "jsonld": {"context": "/interop/context.jsonld", "node": "/interop/jsonld/{id}", "blueprint": "/l6/blueprints/{BLU-id}/export?format=jsonld"},
        "crosswalks": {"index": "/interop/crosswalks", "frameworks": list(crosswalks.frameworks()), "by_ref": "/interop/crosswalks/{framework}/{ref}",
                       "by_block": "/interop/blocks/{BLK-id}/crosswalk"},
        "graphql": {"endpoint": "/graphql", "sdl": "/interop/schema.graphql", "max_depth": graphql_api.MAX_DEPTH, "max_list": graphql_api.MAX_LIMIT},
        "bulk": {"releases": "/v1/releases", "snapshot": "/v1/releases/{release}/{layer}/snapshot"},
        "note": "Open outputs (ODC-By 1.0 data, CC BY 4.0 schemas). Licensed frameworks appear in crosswalks as identifiers only.",
    }


# --------------------------------------------------------------------------- crosswalks


@router.get("/interop/crosswalks")
def crosswalk_index() -> dict:
    with get_engine().connect() as conn:
        return crosswalks.summary(conn)


# Framework keys are always `<org>/<version>` (nist/csf-2.0, iso/27001-2022), so two fixed segments
# route unambiguously; a greedy `:path` param would swallow the ref.
@router.get("/interop/crosswalks/{org}/{version}/{ref}")
def crosswalk_ref(org: str, version: str, ref: str) -> dict:
    try:
        with get_engine().connect() as conn:
            return crosswalks.for_ref(conn, f"{org}/{version}", ref)
    except crosswalks.UnknownFramework as exc:
        raise HTTPException(status_code=404, detail=f"unknown framework {exc}; see /interop/crosswalks") from exc


@router.get("/interop/crosswalks/{org}/{version}")
def crosswalk_framework(org: str, version: str) -> dict:
    framework = f"{org}/{version}"
    try:
        with get_engine().connect() as conn:
            rows = crosswalks.for_framework(conn, framework)
        fw = crosswalks.frameworks()[framework]
    except crosswalks.UnknownFramework as exc:
        raise HTTPException(status_code=404, detail=f"unknown framework {exc}; see /interop/crosswalks") from exc
    return {"framework": framework, "name": fw["name"], "rights": fw["rights"], "identifiers_only": fw["identifiers_only"], "rows": rows, "count": len(rows)}


@router.get("/interop/blocks/{block_id}/crosswalk")
def block_crosswalk(block_id: str) -> dict:
    with get_engine().connect() as conn:
        rows = crosswalks.for_block(conn, block_id)
    return {"block_id": block_id, "rows": rows, "count": len(rows)}


# --------------------------------------------------------------------------- JSON-LD


@router.get("/interop/context.jsonld")
def context() -> JSONResponse:
    return JSONResponse(jsonld.context_document(), media_type=JSONLD, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/interop/jsonld/{node_id:path}")
def node(node_id: str) -> JSONResponse:
    with get_engine().connect() as conn:
        doc = jsonld.node(conn, node_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"no public node {node_id}")
    return JSONResponse(doc, media_type=JSONLD)


# --------------------------------------------------------------------------- GraphQL


@router.get("/interop/schema.graphql", response_class=PlainTextResponse)
def sdl() -> str:
    return graphql_api.sdl()


@router.post("/graphql")
async def graphql_post(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="body must be JSON: {query, variables?, operationName?}") from exc
    query = (body or {}).get("query")
    if not query or not isinstance(query, str):
        raise HTTPException(status_code=400, detail="query is required")
    return graphql_api.execute(get_engine(), query, variables=body.get("variables"), operation_name=body.get("operationName"))


@router.get("/graphql")
def graphql_get(query: str = Query(...), operationName: str | None = None) -> dict:
    return graphql_api.execute(get_engine(), query, operation_name=operationName)
