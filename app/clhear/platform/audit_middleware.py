"""Binds the caller to the audit context for every request and logs mutating ones
(HLD v2 §7.1; item 17). Pure ASGI middleware so it costs one header read on GETs.

Identity resolution, in order: session cookie / Cognito bearer (``accounts.current_user``),
``X-Reg42-User`` (maintainer stand-in), ``X-App-Id`` (API key). Anything else is
anonymous. The request id is honoured from ``X-Request-Id`` when a proxy set one and
minted otherwise; it is echoed back so a client can quote it in a report.
"""
from __future__ import annotations

import time
import uuid

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.clhear.platform import audit


def resolve_actor(request: Request) -> audit.Actor:
    headers = request.headers
    request_id = headers.get("x-request-id") or uuid.uuid4().hex
    ip = (headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "")) or ""
    common = {"request_id": request_id, "ip_hash": audit.hash_ip(ip), "user_agent": headers.get("user-agent", "")}
    try:
        from app.clhear.accounts import current_user

        user = current_user(request)
    except Exception:  # noqa: BLE001 — identity failures must not break the request path
        user = None
    if user:
        from app.clhear.settings import get_settings

        kind = "maintainer" if user["email"] in get_settings().maintainer_set else "user"
        return audit.Actor(actor=user["email"], kind=kind, **common)
    staff = headers.get("x-reg42-user")
    if staff:
        return audit.Actor(actor=staff, kind="maintainer", **common)
    app_id = headers.get("x-app-id")
    if app_id:
        return audit.Actor(actor=f"app:{app_id}", kind="app", **common)
    return audit.Actor(**common)


class AuditMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        actor = resolve_actor(request)
        token = audit.bind_actor(actor)
        status = {"code": 0}
        started = time.monotonic()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = list(message.get("headers") or [])
                if not Headers(raw=headers).get("x-request-id"):
                    headers.append((b"x-request-id", actor.request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            audit.reset_actor(token)
            method, path = scope.get("method", ""), scope.get("path", "")
            if audit.should_audit_http(method, path):
                from app.clhear.db import get_engine

                audit.log_http_write(get_engine(), method=method, path=path, status=status["code"] or 500, actor=actor,
                                     duration_ms=int((time.monotonic() - started) * 1000))
