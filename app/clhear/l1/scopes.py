"""Named source scopes: which registered documents a database holds.

``CLHEAR_SOURCE_SCOPE`` names one entry of ``scopes.json``. When it is set the
registry, the fleet plan and everything that reads them see only that scope's
sources, so L1 acceptance, derivation and releases cover exactly the chosen
corpus. Unset means the full registry. A scope is configuration (which
documents, which role each plays), never derived content.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

SCOPE_ENV = "CLHEAR_SOURCE_SCOPE"
_PATH = Path(__file__).with_name("scopes.json")


@lru_cache(maxsize=1)
def _all() -> dict:
    return json.loads(_PATH.read_text())


def names() -> list[str]:
    return sorted(_all())


def get(name: str) -> dict:
    scopes = _all()
    if name not in scopes:
        raise KeyError(f"Unknown source scope {name!r}; known: {', '.join(sorted(scopes))}")
    return scopes[name]


def active_name() -> str:
    return os.environ.get(SCOPE_ENV, "").strip()


def active() -> dict | None:
    name = active_name()
    return get(name) if name else None


def source_keys(name: str | None = None) -> tuple[str, ...]:
    scope = get(name) if name else active()
    return tuple(scope["sources"]) if scope else ()


def role(role_name: str, name: str | None = None) -> tuple[str, ...]:
    scope = get(name) if name else active()
    return tuple((scope or {}).get("roles", {}).get(role_name, ()))


def in_scope(source_key: str) -> bool:
    scope = active()
    return scope is None or source_key in scope["sources"]
