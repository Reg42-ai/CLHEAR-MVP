"""The published release artifact the consumer API reads.

``/v1`` serves a release, not the reviewers' L1 viewer: its L1 bindings and its
derived L2–L8 rows come from the release's own snapshot. The web service keeps
that snapshot on local disk (``CLHEAR_RELEASE_DB_PATH``); without it, ``/v1``
reads the ordinary engine as before.
"""
from __future__ import annotations

import os
import threading

from sqlalchemy.engine import Engine

PATH_ENV = "CLHEAR_RELEASE_DB_PATH"
_engine: Engine | None = None
_lock = threading.Lock()


def configured() -> bool:
    path = os.environ.get(PATH_ENV, "")
    return bool(path) and os.path.exists(path)


def engine() -> Engine | None:
    global _engine
    if not configured():
        return None
    with _lock:
        if _engine is None:
            from app.clhear.db import make_engine

            _engine = make_engine(f"sqlite:///{os.environ[PATH_ENV]}")
        return _engine


def dispose() -> None:
    from app.clhear import snapshot_cache

    global _engine
    with _lock:
        if _engine is not None:
            snapshot_cache.forget(_engine)
            _engine.dispose()
        _engine = None
