"""Security and operations surface (HLD v2 §7.1; item 17).

    GET /.well-known/security.txt   RFC 9116 — how to report a vulnerability
    GET /security                   posture page: VDP, pen-test scope, SOC 2 pack, SBOM, signing
    GET /security.json              the same, machine-readable
    GET /status                     public status page (SLOs, freshness, gates)
    GET /status.json                what the page and the Upptime probe read
    GET /metrics                    Prometheus exposition for the AMP scrape
    GET /audit                      maintainers: the audit log (filters: actor, action, resource, since, request_id)
    GET /audit/summary              maintainers: counts by action, licensed reads by source
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from app.clhear.db import get_engine
from app.clhear.platform import audit, metrics
from app.clhear.platform import mode as l8_mode
from app.clhear.settings import get_settings

router = APIRouter(tags=["security"])
WEB_DIR = Path(__file__).resolve().parents[1] / "web"
DOCS_DIR = Path(__file__).resolve().parents[3] / "docs" / "security"

SECURITY_CONTACT = "security@reg42.ai"
POLICY_PATH = "/security"
ACK_PATH = "/security#hall-of-thanks"


def _require_maintainer(request: Request) -> dict:
    ident = l8_mode.request_identity(request) or {}
    if ident.get("maintainer") or (ident.get("email") and ident["email"] in get_settings().maintainer_set):
        return ident
    raise HTTPException(status_code=403 if ident else 401, detail="maintainer role required")


# --------------------------------------------------------------------------- disclosure


def security_txt(base_url: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    expires = (now + timedelta(days=365)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    base = base_url.rstrip("/")
    return "\n".join([
        f"Contact: mailto:{SECURITY_CONTACT}",
        f"Contact: {base}/security#report",
        f"Expires: {expires}",
        f"Policy: {base}{POLICY_PATH}",
        f"Acknowledgments: {base}{ACK_PATH}",
        "Preferred-Languages: en, he",
        f"Canonical: {base}/.well-known/security.txt",
        "",
    ])


@router.get("/.well-known/security.txt", include_in_schema=False)
def well_known_security_txt() -> PlainTextResponse:
    return PlainTextResponse(security_txt(get_settings().clhear_public_base_url), media_type="text/plain; charset=utf-8",
                             headers={"Cache-Control": "public, max-age=86400"})


def posture() -> dict:
    """Machine-readable summary of the security programme (what /security renders)."""
    return {
        "contact": SECURITY_CONTACT, "security_txt": "/.well-known/security.txt",
        "vdp": {"policy": "docs/security/VDP.md", "safe_harbour": True, "triage_sla_hours": 72, "fix_sla_days": {"critical": 7, "high": 30, "medium": 90, "low": 180}},
        "pen_test": {"scope": "docs/security/PENTEST_SCOPE.md", "cadence": "annual", "summary_published": True},
        "soc2": {"readiness_pack": "docs/security/SOC2_TYPE_I_READINESS.md", "target": "Type I by first design partner; Type II and ISO 27001 under the reseller engagement"},
        "supply_chain": {"sbom": "sbom.spdx.json per release (syft)", "signing": "Sigstore cosign keyless on the release manifest",
                         "pinning": "requirements.lock (hash-pinned) installed in CI and the release job", "verify": "scripts/verify_release.py"},
        "identity": {"public": "Cognito user pool (email magic link, Google)", "enterprise": "SAML 2.0 federation per enterprise (/auth/sso)",
                     "api": "per-organisation API keys with scopes and rate limits"},
        "audit": {"what": ["every write through record.write / invalidate", "every read of licensed clause text", "every mutating HTTP request"],
                  "retention": "append-only, never deleted (I2)", "access": "/audit (maintainers)"},
        "data_handling": {"agnostic_store": "no organisation data (I5); PII/org-identifier scan on every release", "benchmarks": "k ≥ 5, Laplace noise, re-identification test"},
        "availability": {"status": "/status", "slos": [s["name"] for s in metrics.SLOS], "dr": ".github/workflows/dr_drill.yml (nightly Postgres + Neo4j restore drill)"},
        "documents": sorted(p.name for p in DOCS_DIR.glob("*.md")) if DOCS_DIR.exists() else [],
    }


@router.get("/security.json")
def security_json() -> dict:
    return posture()


@router.get("/security", response_class=HTMLResponse, include_in_schema=False)
def security_page() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "security.html").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- status + metrics


@router.get("/status.json")
def status_json(release: str | None = Query(default=None)) -> JSONResponse:
    body = metrics.status(get_engine(), release=release)
    return JSONResponse(body, status_code=200 if body["status"] != "outage" else 503, headers={"Cache-Control": "no-store"})


@router.get("/status", response_class=HTMLResponse, include_in_schema=False)
def status_page() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "status.html").read_text(encoding="utf-8"))


@router.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> PlainTextResponse:
    return PlainTextResponse(metrics.prometheus(get_engine()), media_type="text/plain; version=0.0.4; charset=utf-8")


# --------------------------------------------------------------------------- audit (maintainers)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"bad timestamp {value!r}") from exc
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@router.get("/audit")
def audit_entries(request: Request, actor: str | None = None, action: str | None = None, resource: str | None = None,
                  request_id: str | None = None, since: str | None = None, until: str | None = None,
                  limit: int = Query(default=200, ge=1, le=1000), offset: int = Query(default=0, ge=0)) -> dict:
    _require_maintainer(request)
    with get_engine().connect() as conn:
        rows = audit.query(conn, actor=actor, action=action, resource=resource, request_id=request_id,
                           since=_parse_ts(since), until=_parse_ts(until), limit=limit, offset=offset)
    return {"entries": rows, "count": len(rows), "limit": limit, "offset": offset}


@router.get("/audit/summary")
def audit_summary(request: Request, since: str | None = None) -> dict:
    _require_maintainer(request)
    with get_engine().connect() as conn:
        return audit.summary(conn, since=_parse_ts(since))
