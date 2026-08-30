"""/api/clhear/layers… + the Stack UI shell.

The public 8-layer surface for the web app: registry with derivation
contracts, per-layer items (honesty-labeled: live/derived/curated/computed/
locked), and the lineage inspector that walks any item down to real L1
clauses.
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.clhear import layer_service
from app.clhear.db import get_engine
from app.clhear.layers import LAYER_CATALOG, layer_public_meta, normalize_layer, status_banner

router = APIRouter()

WEB_DIR = Path(__file__).parent / "web"


@router.get("/api/clhear/layers")
def layers_index() -> dict:
    return {"layers": layer_service.layer_index(get_engine())}


@router.get("/api/clhear/layers/{layer}")
def layer_detail(
    layer: str,
    q: str | None = Query(default=None),
    source_key: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    code = normalize_layer(layer)
    if not code:
        raise HTTPException(status_code=404, detail=f"Unknown layer {layer}")
    engine = get_engine()
    meta = layer_public_meta(code)
    body: dict = {**meta, "counts": layer_service.layer_counts(engine).get(code, {})}
    layer_status = LAYER_CATALOG[code]["status"]
    if layer_status != "live":
        body["banner"] = status_banner(code)
    if code == "L2":
        body["registry"] = layer_service.obligation_items(
            engine, q=q, source_key=source_key, status=status, limit=limit, offset=offset
        )
    elif code not in ("L0", "L1"):
        body["items"] = layer_service.layer_items(engine, code)
    if code == "L8":
        body["locked"] = True
    return body


@router.get("/api/clhear/layers/{layer}/items/{item_id:path}/lineage")
def layer_lineage(layer: str, item_id: str) -> dict:
    code = normalize_layer(layer)
    if not code:
        raise HTTPException(status_code=404, detail=f"Unknown layer {layer}")
    try:
        chain = layer_service.lineage(get_engine(), code, item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"{code} item {item_id} not found")
    body = {"layer": code, "item_id": item_id, "lineage": chain}
    if LAYER_CATALOG[code]["status"] != "live":
        body["banner"] = status_banner(code)
    return body


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def stack_home() -> HTMLResponse:
    # no-cache: the app shell must always match the deployed API/corpus.
    return HTMLResponse(
        (WEB_DIR / "stack.html").read_text(),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@router.get("/static/theme.css", include_in_schema=False)
def theme_css():
    from fastapi.responses import Response

    return Response(
        (WEB_DIR / "theme.css").read_text(),
        media_type="text/css",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )
