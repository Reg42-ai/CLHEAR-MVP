"""Verified reviewer access; distinct from the right to use a source's text."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.clhear.accounts import current_user
from app.clhear.settings import get_settings

router = APIRouter()


def reviewer(request: Request) -> dict | None:
    settings = get_settings()
    if len(settings.clhear_session_secret.strip()) < 32 or settings.clhear_auth_debug:
        return None
    user = current_user(request)
    if not user or user.get("email", "").lower() not in get_settings().reviewer_set:
        return None
    if user.get("provider") == "cognito" and user.get("email_verified") is not True:
        return None
    return user


def app_key_request(request: Request) -> bool:
    """A /v1 call that presents app credentials; the route's require_app decides."""
    path = request.url.path
    if path != "/v1" and not path.startswith("/v1/"):
        return False
    authorization = request.headers.get("authorization", "")
    return authorization.lower().startswith("bearer ") and bool(request.headers.get("x-app-id", "").strip())


def _signin_required(request: Request, detail: str):
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/signin", status_code=303, headers={"Cache-Control": "no-store"})
    return JSONResponse({"detail": detail}, status_code=401, headers={"Cache-Control": "no-store"})


def _accounts_gate(request: Request):
    """Account-required mode; None admits the request."""
    from app.clhear import access

    kind = access.classify(request)
    if kind == "public":
        return None
    if kind == "v1":
        return None if app_key_request(request) else JSONResponse(
            {"detail": "An API key is required: send Authorization: Bearer <key> and X-App-Id."},
            status_code=401, headers={"Cache-Control": "no-store"})
    user = access.signed_in_account(request)
    if user is None:
        return _signin_required(request, "Sign in or create a free account to use CLHEAR.")
    status = access.account_status(user)
    if status == "suspended":
        return JSONResponse({"detail": "This account is suspended."}, status_code=403, headers={"Cache-Control": "no-store"})
    if status == "unknown":
        return JSONResponse({"detail": "Account status cannot be verified right now. Please retry shortly."},
                            status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "30"})
    if kind == "maintainer" and not access.is_maintainer(user):
        return JSONResponse({"detail": "Maintainer access is required."}, status_code=403, headers={"Cache-Control": "no-store"})
    if kind == "reviewer" and reviewer(request) is None:
        return JSONResponse({"detail": "Reviewer access is required for this action."}, status_code=403,
                            headers={"Cache-Control": "no-store"})
    from app.clhear import ratelimit

    try:
        ratelimit.hit(f"account:{user.get('id')}", limit=get_settings().clhear_rate_account_per_minute, window_s=60)
    except ratelimit.RateLimited as exc:
        return JSONResponse({"detail": "Too many requests"}, status_code=429,
                            headers={"Cache-Control": "no-store", "Retry-After": str(exc.retry_after)})
    return None


class RestrictedAccessMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        from app.clhear.access import accounts_mode

        if accounts_mode():
            if len(settings.clhear_session_secret.strip()) < 32 or settings.clhear_auth_debug:
                return JSONResponse({"detail": "Account access requires a private session secret of at least 32 characters and production authentication."},
                                    status_code=503, headers={"Cache-Control": "no-store"})
            denied = _accounts_gate(request)
            if denied is not None:
                return denied
            response = await call_next(request)
            if getattr(request.state, "private_text", False) or not request.url.path.startswith("/static/"):
                response.headers["Cache-Control"] = response.headers.get("Cache-Control") if request.url.path.startswith("/static/") else "private, no-store"
                response.headers["Vary"] = "Cookie, Authorization"
            return response
        if not settings.clhear_restricted_access:
            response = await call_next(request)
            if getattr(request.state, "private_text", False):
                response.headers["Cache-Control"] = "private, no-store"
                response.headers["Vary"] = "Cookie, Authorization"
            return response
        path = request.url.path
        public = path in {"/signin", "/api/clhear/health", "/terms", "/disclaimer"}
        public = public or path.startswith("/auth/") or path.startswith("/static/")
        if not public and not app_key_request(request):
            if len(settings.clhear_session_secret.strip()) < 32 or settings.clhear_auth_debug:
                return JSONResponse({"detail": "Restricted access requires a private session secret of at least 32 characters and production authentication."},
                                    status_code=503, headers={"Cache-Control": "no-store"})
            if reviewer(request) is None:
                if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                    return RedirectResponse("/signin", status_code=303, headers={"Cache-Control": "no-store"})
                return JSONResponse({"detail": "An approved reviewer must sign in."}, status_code=401,
                                    headers={"Cache-Control": "no-store"})
        response = await call_next(request)
        # Never let shared caches retain private corpus responses or identity.
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "Cookie, Authorization"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response


@router.get("/signin", response_class=HTMLResponse, include_in_schema=False)
def sign_in() -> HTMLResponse:
    return HTMLResponse('''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CLHEAR · Sign in</title>
<link rel="stylesheet" href="/static/theme.css"><style>main{max-width:480px;margin:12vh auto;padding:28px}label,input,button{display:block;margin:12px 0}input{width:100%;padding:12px}button{padding:12px 18px}a{display:inline-block;margin:12px 12px 12px 0}</style></head>
<body><a href="#main" class="skip">Skip to content</a><main id="main"><h1>CLHEAR</h1><h2>Reviewer sign in</h2><p>This workspace is available to approved reviewers.</p>
<form id="login"><label for="email">Email address</label><input id="email" name="email" type="email" autocomplete="email" required>
<button type="submit">Send sign-in link</button></form><p id="message" role="status"></p><div id="providers"></div></main>
<script>fetch('/auth/me').then(r=>r.json()).then(d=>{const p=d.providers||{};if(!p.email)document.getElementById('login').hidden=true;for(const [k,label] of [['cognito','Organization sign in'],['google','Google']]){if(p[k]){const a=document.createElement('a');a.href='/auth/'+k;a.textContent=label;document.getElementById('providers').append(a);}}});
document.getElementById('login').addEventListener('submit',async e=>{e.preventDefault();const m=document.getElementById('message');m.textContent='Sending…';try{const r=await fetch('/auth/email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('email').value})});m.textContent=r.ok?'Check your email for the sign-in link.':'Unable to send the link. Check your access or try your organization sign in.';}catch{m.textContent='Connection unavailable. Please try again.'}});</script></body></html>''',
                        headers={"Cache-Control": "no-store"})
