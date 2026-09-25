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


class RestrictedAccessMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
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
