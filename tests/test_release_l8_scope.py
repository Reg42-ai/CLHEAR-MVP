"""A scope release's L8 rows come from the scope's reference sources, whatever process serves them."""
from app.clhear import app_api
from app.clhear.l1 import scopes
from app.clhear.l8 import reference


def test_rows_are_read_from_exactly_the_given_reference_sources(engine, monkeypatch):
    seen = []
    monkeypatch.setattr(reference, "_blocks", lambda conn, keys: seen.append(list(keys)) or [])
    reference.reference_rows(engine, source_keys=["sec/exams/risk-alert-041724", "ftc/guidance/disclosures-101"])
    reference.reference_rows(engine)
    assert seen[0] == ["sec/exams/risk-alert-041724", "ftc/guidance/disclosures-101"]
    assert seen[1] == reference.reference_source_keys(engine.connect())


def test_v1_takes_the_reference_sources_from_the_release_scope(monkeypatch):
    releases = {"demo": {"id": "demo", "scope": {"name": "compliance-program-demo"}}, "plain": {"id": "plain"}}
    monkeypatch.setattr(app_api.release_store, "get_release", lambda rid, engine=None: releases.get(rid))
    monkeypatch.setattr(app_api, "_engine", lambda: None)
    assert app_api._scope_reference_keys("demo") == list(scopes.get("compliance-program-demo")["roles"]["reference"])
    assert app_api._scope_reference_keys("plain") is None
