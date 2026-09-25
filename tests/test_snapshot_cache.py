"""Viewer-independent answers are computed once per published snapshot."""
from app.clhear import snapshot_cache


def test_answers_are_cached_per_snapshot_engine_and_never_on_a_writable_db(monkeypatch):
    calls = []

    @snapshot_cache.cached("probe")
    def answer(engine, n):
        calls.append(n)
        return n * 2

    snapshot_cache.clear()
    monkeypatch.delenv("CLHEAR_DB_S3_URI", raising=False)
    engine_a, engine_b = object(), object()
    assert answer(engine_a, 2) == 4 and answer(engine_a, 2) == 4 and calls == [2, 2]
    monkeypatch.setenv("CLHEAR_DB_S3_URI", "s3://private/webui/l1/candidate.db")
    calls.clear()
    assert answer(engine_a, 3) == 6 and answer(engine_a, 3) == 6 and calls == [3]
    assert answer(engine_b, 3) == 6 and calls == [3, 3]  # a swapped snapshot is a new engine
    snapshot_cache.clear()


def test_unhashable_arguments_are_answered_without_caching(monkeypatch):
    calls = []

    @snapshot_cache.cached("probe-list")
    def answer(engine, *, themes):
        calls.append(themes)
        return len(themes)

    snapshot_cache.clear()
    monkeypatch.setenv("CLHEAR_DB_S3_URI", "s3://private/webui/l1/candidate.db")
    engine = object()
    assert answer(engine, themes=["aml"]) == 1 and answer(engine, themes=["aml"]) == 1
    assert calls == [["aml"], ["aml"]]


def test_disposing_an_engine_forgets_its_answers(monkeypatch):
    from app.clhear import db, release_db

    calls = []

    @snapshot_cache.cached("probe-dispose")
    def answer(engine):
        calls.append(1)
        return "value"

    snapshot_cache.clear()
    monkeypatch.setenv("CLHEAR_DB_S3_URI", "s3://private/webui/l1/candidate.db")
    engine = object()
    answer(engine)
    db.dispose_engine()
    answer(engine)
    release_db.dispose()
    answer(engine)
    assert calls == [1, 1, 1]
    snapshot_cache.clear()
