"""/api/clhear/layers… + the Stack UI shell.

The public 8-layer surface for the web app: registry with derivation
contracts, per-layer items (demo-labeled for L2–L8), and the lineage
inspector that walks any item down to real L1 clauses.
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from app.clhear import layer_service
from app.clhear.db import get_engine
from app.clhear.layers import LAYER_CATALOG, demo_banner, layer_public_meta, normalize_layer

router = APIRouter()

WEB_DIR = Path(__file__).parent / "web"


@router.get("/api/clhear/layers")
def layers_index() -> dict:
    return {"layers": layer_service.layer_index(get_engine())}


@router.get("/api/clhear/layers/{layer}")
def layer_detail(layer: str) -> dict:
    code = normalize_layer(layer)
    if not code:
        raise HTTPException(status_code=404, detail=f"Unknown layer {layer}")
    engine = get_engine()
    meta = layer_public_meta(code)
    body: dict = {**meta, "counts": layer_service.layer_counts(engine).get(code, {})}
    if LAYER_CATALOG[code]["status"] == "demo":
        body["banner"] = demo_banner(code)
        body["items"] = layer_service.layer_items(engine, code)
    elif code == "L8":
        body["items"] = layer_service.layer_items(engine, code)
        body["locked"] = True
    return body


@router.get("/api/clhear/layers/{layer}/items/{item_id}/lineage")
def layer_lineage(layer: str, item_id: str) -> dict:
    code = normalize_layer(layer)
    if not code:
        raise HTTPException(status_code=404, detail=f"Unknown layer {layer}")
    try:
        chain = layer_service.lineage(get_engine(), code, item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"{code} item {item_id} not found")
    body = {"layer": code, "item_id": item_id, "lineage": chain}
    if LAYER_CATALOG[code]["status"] == "demo":
        body["banner"] = demo_banner(code)
    return body


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def stack_home() -> HTMLResponse:
    # no-cache: the app shell must always match the deployed API/corpus.
    return HTMLResponse(
        (WEB_DIR / "stack.html").read_text(),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )
