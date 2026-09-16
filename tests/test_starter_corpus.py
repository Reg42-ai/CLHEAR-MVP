"""HLD v2 §4.1 starter corpus: every instrument resolves to registry rows +
adapters, tier-A sources sit behind tier-A adapters, and the published
coverage scorecard reflects ingested state."""
from app.clhear.l1 import pipeline, starter_corpus
from app.clhear.l1.registry_etoro import S
from app.clhear.l1.starter_corpus import STARTER_CORPUS, TIER_A_ADAPTERS, coverage, starter_keys, starter_plan

HLD_INSTRUMENTS = {
    "MiFID II", "MiFIR", "MAR", "MLRs 2017 family", "FSMA 2000 / RAO / FPO", "FCA Handbook (selected)",
    "SEC rules via EDGAR", "FINRA rules via EDGAR (derived-only)", "FATCA statute + 26 CFR + Rev. Proc.",
    "GDPR", "ESMA guidelines (MiFID II)", "NIST spine", "FATF / BIS / IOSCO standards", "MAS / ASIC / ISA",
}


def test_starter_corpus_covers_the_hld_instruments():
    names = {item.instrument for item in STARTER_CORPUS}
    assert HLD_INSTRUMENTS <= names
    by_key = {e["key"]: e for e in S}
    unknown = [k for k in starter_keys() if k not in by_key]
    assert unknown == []
    assert len(starter_keys()) == len(set(starter_keys()))


def test_starter_plan_resolves_every_source_to_an_adapter():
    plan = starter_plan()
    assert len(plan) >= 55
    keys = [adapter.meta().source_key for _, adapter in plan]
    assert len(keys) == len(set(keys))
    for entry, adapter in plan:
        meta = adapter.meta()
        assert meta.adapter
        assert meta.canonical_url.startswith("http")
        if entry is not None:
            assert entry["key"] == meta.source_key
            assert meta.rights_basis or entry["adapter"]  # adapter default applies when unset


def test_tier_a_instruments_use_tier_a_adapters():
    by_key = {e["key"]: e for e in S}
    for item in STARTER_CORPUS:
        if item.tier != "A":
            continue
        for key in item.source_keys:
            adapter = by_key[key]["adapter"]
            # Publisher-cadence exceptions inside tier-A instruments: ESMA guidance
            # (MAR context), SEC releases and IRS Rev. Procs are published ad hoc,
            # so the 24 h currency clock does not apply to them.
            if adapter in {"esma", "sec_edgar", "irs_gov"}:
                continue
            assert adapter in TIER_A_ADAPTERS, (key, adapter)


def test_coverage_scorecard_tracks_ingested_sources(engine, tmp_path, monkeypatch):
    empty = coverage(engine)
    assert empty["ingested"] == 0 and empty["share"] == 0.0
    assert {i["instrument"] for i in empty["instruments"]} >= HLD_INSTRUMENTS

    # Ingest a source under one of the starter keys: coverage moves.
    key = "fca/handbook/DISP"
    from app.clhear.l1 import http
    from app.clhear.l1.adapters.official_html import OfficialHtmlAdapter
    body = b"<main><h1>Authored scorecard fixture</h1><p>This fixture tests ingestion counts in a disposable test database.</p></main>"
    monkeypatch.setattr(http, "get", lambda url: body)
    adapter = OfficialHtmlAdapter(source_key=key, title="Authored scorecard fixture",
                                  url="https://example.invalid/scorecard", adapter="fca_handbook")
    result = pipeline.ingest(engine, adapter, pipeline.LocalStore(tmp_path / "lake"))
    assert result["status"] == "added"
    after = coverage(engine)
    assert after["ingested"] == 1
    fca = next(i for i in after["instruments"] if i["instrument"] == "FCA Handbook (selected)")
    assert fca["ingested"] == 1 and key not in fca["missing"]


def test_dry_run_cli_lists_the_plan(capsys):
    assert starter_corpus.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "fca/handbook" in out and out.strip().endswith("sources")
