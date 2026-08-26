"""E1–E7 + l1_completeness per source (L2 gate)."""
from app.clhear.l1 import pipeline
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta
from app.clhear.l1.adapters.restricted_file import RestrictedFileAdapter
from app.clhear.l1.registry_etoro import seed
from app.clhear.platform import evals


class _Tiny:
    key = "stub"

    def meta(self) -> SourceMeta:
        return SourceMeta(
            family_key="eu-mifid",
            family_name="EU MiFID II framework",
            source_key="celex/32014L0065",
            name="MiFID II",
            kind="law",
            issuer="EU",
            jurisdiction="EU",
            license="open",
            canonical_url="https://example.invalid",
            adapter="eur_lex",
            short_name="MiFID II",
        )

    def fetch(self, since_version=None):
        return FetchResult(
            version_label="v1",
            artifacts=[Artifact("a.txt", b"investment firm must", "text/plain")],
            tree=[DocNode(node_type="article", ref="art_4", heading="investment firm", raw_text="investment firm must")],
        )

    def expected_text(self, artifacts):
        return ["investment firm must"]


def test_source_evals_pass_on_ingested_tree(engine, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, _Tiny(), store)
    records = evals.run_source_evals(engine, "celex/32014L0065")
    by = {r["suite"]: r for r in records}
    assert by["e1_fidelity"]["passed"]
    assert by["e2_completeness"]["passed"]
    assert by["e3_roundtrip"]["passed"]
    assert by["e4_change_replay"]["passed"]  # n/a first version
    assert by["e5_provenance"]["passed"]
    assert by["e7_closure"]["passed"]
    card = evals.latest_source_scorecard(engine, "celex/32014L0065")
    assert "e1_fidelity" in card["suites"]


def test_completeness_fails_when_registry_row_has_no_version(engine):
    seed(engine)
    scores, passed = evals.l1_completeness(engine, None)
    assert passed is False
    assert scores["missing_count"] > 0


def test_restricted_placeholder_is_not_an_e1_fail(engine, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, RestrictedFileAdapter("iso/27001-2022", "ISO 27001"), store)
    scores, passed = evals.e1_fidelity(engine, "iso/27001-2022")
    assert passed is True
    assert scores.get("n/a")
