"""Answers computed from a read-only snapshot, cached for that snapshot's life.

A published snapshot never changes; a new one replaces the engine (the web
service disposes it on swap). So an expensive, viewer-independent answer —
the layer index, the sources list, sample programs, risk items — can be
computed once per engine and reused. On a writable database nothing is cached.
"""
from __future__ import annotations

import functools
import threading

_cache: dict[tuple, object] = {}
_lock = threading.Lock()
MAX_ENTRIES = 256


def enabled() -> bool:
    from app.clhear.community_writes import snapshot_readonly

    return snapshot_readonly()


def cached(name: str):
    """Memoize ``fn(engine, *args, **kwargs)`` per engine instance and arguments."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(engine, *args, **kwargs):
            if not enabled():
                return fn(engine, *args, **kwargs)
            key = (name, id(engine), args, tuple(sorted(kwargs.items())))
            try:
                hash(key)
            except TypeError:
                return fn(engine, *args, **kwargs)
            with _lock:
                if key in _cache:
                    return _cache[key]
            value = fn(engine, *args, **kwargs)
            with _lock:
                if len(_cache) >= MAX_ENTRIES:
                    _cache.clear()
                _cache[key] = value
            return value
        inner.uncached = fn
        return inner
    return wrap


def clear() -> None:
    with _lock:
        _cache.clear()
