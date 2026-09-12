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
        from app.clhear.l2.concepts import list_concepts

        body["concepts"] = list_concepts(engine)
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


@router.get("/api/clhear/legal")
def legal_meta() -> dict:
    from app.clhear import legal

    return {
        "disclaimer": legal.DISCLAIMER_SHORT,
        "contribution_license": legal.CONTRIBUTION_LICENSE,
        "terms_url": "/terms",
        "status": "draft-pending-counsel-review",
    }


_LEGAL_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>CLHEAR · {title}</title><link rel="stylesheet" href="/static/theme.css"/>
<style>main{{position:relative;z-index:1;max-width:840px;margin:0 auto;padding:40px 22px 90px;}}
main h1{{font-size:26px;}} main h2{{font-size:17px;margin-top:28px;}}
main p, main li{{color:var(--muted);font-size:14.5px;line-height:1.7;}}
.draft{{border:1px solid rgba(251,191,36,0.4);background:rgba(251,191,36,0.07);border-radius:10px;
padding:10px 16px;color:var(--warn);font-size:13px;margin-bottom:20px;}}</style></head>
<body><header class="appbar"><div class="wordmark" onclick="location.href='/'"><b>CLHEAR</b>
<span>the compliance stack</span></div><div class="spacer"></div>
<a class="navlink" href="/">Stack</a><a class="navlink" href="/sources">Sources Explorer</a></header>
<main><div class="draft">DRAFT — pending counsel review before commercial promotion.</div>{body}</main></body></html>"""


@router.get("/disclaimer", response_class=HTMLResponse, include_in_schema=False)
def disclaimer_page() -> HTMLResponse:
    from app.clhear import legal

    body = f"""
    <h1>Disclaimer</h1>
    <p>{legal.DISCLAIMER_SHORT}</p>
    <h2>Accuracy tiers, honestly labeled</h2>
    <ul>
      <li><b>LIVE (L0–L1)</b> — verbatim texts fetched from the official publisher, stored immutably,
          hash-verified; every clause links to its authoritative source. Errors are still possible
          (parsing, timing); the official publication always prevails.</li>
      <li><b>DERIVED (L2)</b> — obligations machine-extracted by a deterministic, versioned extractor.
          Items marked <i>derived</i> have not been human-validated and may be wrong or incomplete.
          Per-source extraction scorecards are published nightly.</li>
      <li><b>CURATED (L3–L5)</b> — human-authored mappings, reviewed before publication and open to
          challenge by the community.</li>
      <li><b>COMPUTED (L6–L7)</b> — deterministic engine output over the tiers above; a blueprint or a
          risk score is only as good as its inputs, and each publishes its inputs.</li>
    </ul>
    <h2>Not legal advice</h2>
    <p>CLHEAR is an information tool. It does not create a lawyer–client relationship and is not a
       substitute for legal advice on your specific circumstances. Regulators, courts and official
       publications are the only authoritative statements of the law.</p>"""
    return HTMLResponse(_LEGAL_PAGE.format(title="Disclaimer", body=body))


@router.get("/terms", response_class=HTMLResponse, include_in_schema=False)
def terms_page() -> HTMLResponse:
    from app.clhear import legal

    body = f"""
    <h1>Terms of Use</h1>
    <h2>1. The service</h2>
    <p>CLHEAR republishes regulatory texts from official sources and derives structured compliance
       data. Use it at your own risk and subject to the <a href="/disclaimer">Disclaimer</a>, which
       is part of these terms.</p>
    <h2>2. Source texts and attribution</h2>
    <p>Verbatim texts remain subject to their publishers' terms (Crown copyright / OGL v3 for
       legislation.gov.uk; © European Union reuse policy for EUR-Lex; US public domain for federal
       materials and NIST; publisher terms for FCA, FINRA, Nasdaq, FATF and others). Attribution is
       displayed per source across the site and in API responses. Restricted standards (ISO, AICPA
       TSC, PCI DSS, IFRS) are never republished: refs and hashes only.</p>
    <h2>3. Accounts</h2>
    <p>Sign-in requires a working email (or Google/Apple). We store your email, display name and
       your contributions; we use them to operate the service and credit your work. No marketing
       without consent. Deletion requests: privacy@reg42.ai.</p>
    <h2>4. Contributions</h2>
    <p>{legal.CONTRIBUTION_LICENSE}</p>
    <h2>5. API</h2>
    <p>API access is keyed and rate-limited; keys may be revoked for abuse. Responses embed the
       same accuracy tiers and disclaimers as the site.</p>
    <h2>6. Liability</h2>
    <p>The service is provided "as is" without warranties. To the maximum extent permitted by law,
       Reg42 is not liable for losses arising from reliance on the service. Nothing limits liability
       that cannot lawfully be limited.</p>"""
    return HTMLResponse(_LEGAL_PAGE.format(title="Terms", body=body))


@router.get("/static/theme.css", include_in_schema=False)
def theme_css():
    from fastapi.responses import Response

    return Response(
        (WEB_DIR / "theme.css").read_text(),
        media_type="text/css",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )
