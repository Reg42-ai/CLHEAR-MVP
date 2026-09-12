"""App-key auth for the consumer API (OS, Safeluance, later products).

Keys are configured as CLHEAR_APP_KEYS=app_id:secret,app_id:secret.
Each key is granted read:l1 today; extra scopes can be appended as
app_id:secret:read:l1+read:l2 when later layers ship.
"""
from __future__ import annotations

from fastapi import Header, HTTPException

from app.clhear.settings import get_settings


def parse_app_keys(raw: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        bits = part.split(":")
        app_id, secret = bits[0], bits[1]
        scopes = {"read:l1"}
        if len(bits) > 2 and bits[2]:
            # Remainder is scope list (read:l1+read:l2). Do not split scopes on ':'.
            scope_raw = ":".join(bits[2:])
            scopes = {s.strip() for s in scope_raw.replace("+", ",").split(",") if s.strip()}
        out[app_id] = {"secret": secret, "scopes": scopes}
    return out


def require_app(
    authorization: str | None = Header(default=None),
    x_app_id: str | None = Header(default=None, alias="X-App-Id"),
) -> dict:
    settings = get_settings()
    keys = parse_app_keys(settings.clhear_app_keys)
    if not keys:
        raise HTTPException(status_code=503, detail="CLHEAR_APP_KEYS is not configured")
    if not x_app_id or x_app_id not in keys:
        raise HTTPException(status_code=401, detail="Unknown or missing X-App-Id")
    expected = keys[x_app_id]["secret"]
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    return {"app_id": x_app_id, "scopes": sorted(keys[x_app_id]["scopes"])}


def require_scope(app: dict, scope: str) -> None:
    if scope not in app.get("scopes", []):
        raise HTTPException(status_code=403, detail=f"App is not granted {scope}")
