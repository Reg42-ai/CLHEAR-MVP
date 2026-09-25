"""Edge-facing guards for every request: origin, size, CSRF, headers, usage.

* Requests must carry the CloudFront origin header when one is configured, so
  the regional API endpoint cannot be used to bypass the WAF.
* Request bodies and page sizes are capped.
* A write authenticated only by the session cookie must come from our own origin.
* Every response gets HSTS, CSP (with ``frame-ancestors``), nosniff,
  Referrer-Policy and Permissions-Policy.
* Every request, reads included, is recorded for usage (``app.clhear.usage``).
"""
from __future__ import annotations

import hmac
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.clhear.settings import get_settings

READY_PATH = "/api/clhear/ready"
SAFE = frozenset({"GET", "HEAD", "OPTIONS"})
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; object-src 'none'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self' https:")
HEADERS = {
    "strict-transport-security": "max-age=63072000; includeSubDomains",
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "x-frame-options": "DENY",
}


def _deny(status: int, detail: str, **headers) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status, headers={"Cache-Control": "no-store", **headers})


def _cross_site(request: Request) -> bool:
    """A browser always sends Origin (or Sec-Fetch-Site) on a cross-site POST; a
    request that names another origin is refused. With SameSite=Lax cookies this
    closes the classic form-post CSRF path."""
    if request.headers.get("sec-fetch-site", "") == "cross-site":
        return True
    expected = urlparse(get_settings().clhear_public_base_url).netloc
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value:
            return urlparse(value).netloc not in {expected, request.url.netloc}
    return False


class WebGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        path = request.url.path
        secret = settings.clhear_origin_verify_secret
        if secret and path != READY_PATH and not hmac.compare_digest(
                request.headers.get("x-clhear-origin", "").encode(), secret.encode()):
            return _deny(403, "Requests must reach CLHEAR through its public address")
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.clhear_max_body_bytes:
            return _deny(413, "Request body is too large")
        limit = request.query_params.get("limit")
        if limit and limit.isdigit() and int(limit) > settings.clhear_max_page_size:
            return _deny(400, f"limit may not exceed {settings.clhear_max_page_size}")
        cookie_write = (request.method.upper() not in SAFE and "clhear_session" in request.cookies
                        and not request.headers.get("authorization", "").lower().startswith("bearer "))
        if cookie_write and _cross_site(request):
            return _deny(403, "Cross-site requests cannot use your session")
        started = time.monotonic()
        response = await call_next(request)
        for key, value in HEADERS.items():
            response.headers.setdefault(key, value)
        if path != READY_PATH:
            _record(request, response, started)
        return response


def _record(request: Request, response, started: float) -> None:
    from app.clhear import usage

    try:
        route = request.scope.get("route")
        template = getattr(route, "path", "") or ("<unmatched>" if response.status_code == 404 else request.url.path)
        app = getattr(request.state, "app", None) or {}
        user_id = app.get("user_id") or ""
        if not user_id:
            from app.clhear.accounts import current_user

            user = current_user(request)
            user_id = (user or {}).get("id", "")
        usage.record({
            "at": datetime.now(timezone.utc), "user_id": str(user_id or ""), "key_id": app.get("key_id") or "",
            "app_id": app.get("app_id") or "", "method": request.method.upper(), "route": template[:300],
            "layer": usage.layer_of(template) or usage.layer_of(request.url.path), "status": response.status_code,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "response_bytes": int(response.headers.get("content-length") or 0),
            "ip_hash": usage.ip_hash(request.client.host if request.client else ""),
            "user_agent": (request.headers.get("user-agent") or "")[:200],
        })
    except Exception:  # noqa: BLE001 — recording usage never fails a request
        pass
