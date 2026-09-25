"""Answers computed from a read-only snapshot, cached for that snapshot's life.

A published snapshot never changes; a new one replaces the engine (the web
service disposes it on swap). So an expensive, viewer-independent answer —
the layer index, the sources list, status facts, risk scores — can be
computed once per engine and reused. The web service computes them against a
new snapshot before it goes live and hands them to the new engine
(``answers_for`` / ``adopt``). On a writable database nothing is cached.
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


def answers_for(engine) -> dict:
    """The answers cached for ``engine``, keyed without it."""
    with _lock:
        return {(k[0], *k[2:]): v for k, v in _cache.items() if k[1] == id(engine)}


def adopt(engine, answers: dict) -> None:
    """Serve ``answers`` (from ``answers_for`` on the same snapshot content) for ``engine``."""
    with _lock:
        for (name, args, kwargs), value in answers.items():
            _cache[(name, id(engine), args, kwargs)] = value


def forget(engine) -> None:
    with _lock:
        for key in [k for k in _cache if k[1] == id(engine)]:
            del _cache[key]


def clear() -> None:
    with _lock:
        _cache.clear()
