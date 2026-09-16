"""Fidelity gate + repair loop tests (root-cause machinery).

Fleet-wide: every registry adapter (current and future) is parametrized
through the gate against recorded fixtures. Loop behavior: convergence via
salvage, LLM hint escalation with persistent hint memory, and exhaustion
("not fully successful") with nothing persisted and a rectification proposal.
"""
import json

import pytest
import sqlalchemy as sa

from app.clhear.l1 import fidelity, pipeline
from app.clhear.l1.adapters import ADAPTER_KEYS, get_adapter
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta
from app.clhear.l1.adapters.eur_lex import EurLexAdapter
from app.clhear.l1.models import clauses, doc_nodes, parse_hints, source_versions
from app.clhear.models import events, llm_calls, proposals
from app.clhear.platform.gateway import FakeProvider, Gateway

THRESHOLD = 0.995


# --------------------------------------------------------------- fleet-wide gate
@pytest.mark.parametrize("key", list(ADAPTER_KEYS) + ["eur_lex_oj"])
def test_every_adapter_passes_the_gate(key):
    """Any adapter in the registry (and the GDPR OJ original) must cover its
    own oracle at >= threshold with zero invariant violations — offline."""
    adapter = EurLexAdapter(celex_version="32016R0679") if key == "eur_lex_oj" else get_adapter(key)
    result = adapter.fetch()
    report = fidelity.check(result.tree, adapter.expected_text(result.artifacts))
    assert report.coverage >= THRESHOLD, report.summary()
    assert report.violations == [], report.violations[:5]


def test_gate_fails_on_silent_text_loss():
    """Negative control: a parser that drops artifact text MUST fail."""
    spans = ["alpha one two three", "bravo four five six", "charlie seven eight nine"]
    tree = [DocNode(node_type="provision", ref="r1", raw_text=spans[0])]  # drops 2/3
    report = fidelity.check(tree, spans)
    assert report.coverage < 0.5
    assert len(report.missing_spans) == 2
    assert not report.ok(THRESHOLD)


def test_lint_catches_label_duplication_and_dup_refs():
    tree = [
        DocNode(node_type="provision", ref="r1", label="1.", raw_text="1. duplicated marker"),
        DocNode(node_type="provision", ref="r1", raw_text="same ref twice"),
        DocNode(node_type="provision", ref="", raw_text="clause-grain without ref"),
    ]
    violations = fidelity.lint(tree)
    assert any("label duplicated in raw_text" in v for v in violations)
    assert any("duplicate ref" in v for v in violations)
    assert any("without ref" in v for v in violations)
    # word-boundary: 'IDENTIFY' does NOT duplicate label 'ID'
    ok = fidelity.lint([DocNode(node_type="part", ref="GV", label="ID", heading="IDENTIFY")])
    assert ok == []


# ------------------------------------------------------------------- loop stubs
class _GappyAdapter:
    """Parses only part of its artifact; oracle knows the full text. The gap
    size decides which tier can save it (salvage cap = 2%)."""

    key = "gappy"

    def __init__(self, missing: list[str], parsed: list[str], source_suffix: str = "x", version: str = "v1"):
        self.missing = missing
        self.parsed = parsed
        self.source_suffix = source_suffix
        self.version = version
        self.fetches = 0

    def meta(self) -> SourceMeta:
        return SourceMeta(
            family_key="gappy-family",
            family_name="Gappy family",
            source_key=f"gappy/{self.source_suffix}",
            name="Gappy source",
            kind="regulation",
            issuer="stub",
            jurisdiction="XX",
            license="open",
            canonical_url="https://example.invalid/gappy",
            adapter="gappy",
        )

    def fetch(self, since_version=None):
        self.fetches += 1
        from html import escape
        from app.clhear.l1.adapters.html_document import parse
        body = ("<h1>Fixture source</h1>" + "".join("<p>" + escape(text) + "</p>" for text in self.parsed + self.missing)).encode()
        tree = parse(body, self.meta().source_key)
        if self.missing:
            del tree[0].children[0].children[-len(self.missing):]
        return FetchResult(version_label=self.version, artifacts=[Artifact("doc.html", body, "text/html")], tree=tree)

    def expected_text(self, artifacts):
        from app.clhear.l1.originals import html_blocks
        return [row["text"] for row in html_blocks(artifacts[0].content)]


def test_unlocated_salvage_cannot_certify_source_hierarchy(engine, tmp_path):
    parsed = [f"provision text number {i} with plenty of tokens to weigh the corpus" for i in range(50)]
    adapter = _GappyAdapter(missing=["one tiny missed line of several tokens"], parsed=parsed, source_suffix="salvage")
    summary = pipeline.ingest(engine, adapter, pipeline.LocalStore(tmp_path / "lake"))
    assert summary["status"] == "not-fully-successful"
    assert not summary["original_verification"]["verified"]
    assert any(f["code"] == "publisher_hierarchy_mismatch" for f in summary["original_verification"]["findings"])
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(source_versions)).scalar_one() == 0
        assert conn.execute(sa.select(sa.func.count()).select_from(doc_nodes)).scalar_one() == 0


def test_loop_exhaustion_persists_nothing_and_files_rectification(engine, tmp_path, caplog):
    """A big unstructured gap (> salvage cap, no hints, no LLM) exhausts the
    loop: nothing persisted, 'NOT fully successful' logged, event + proposal."""
    parsed = ["short parsed bit"]
    missing = [f"large missed span {i} " + "tok " * 30 for i in range(20)]
    adapter = _GappyAdapter(missing=missing, parsed=parsed, source_suffix="fail")
    with caplog.at_level("ERROR"):
        summary = pipeline.ingest(engine, adapter, pipeline.LocalStore(tmp_path / "lake"))
    assert summary["status"] == "not-fully-successful"
    assert any("NOT fully successful" in r.message for r in caplog.records)
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(source_versions)).scalar_one() == 0
        assert conn.execute(sa.select(sa.func.count()).select_from(doc_nodes)).scalar_one() == 0
        assert conn.execute(sa.select(sa.func.count()).select_from(clauses)).scalar_one() == 0
        event = conn.execute(sa.select(events).where(events.c.kind == "IngestFidelityFailed")).one()
        assert event.subject_ref == "gappy/fail"
        proposal = conn.execute(sa.select(proposals).where(proposals.c.kind == "ingest_rectification")).one()
        assert proposal.status == "proposed"


def _repair_gateway(engine, missing_spans):
    """FakeProvider returning gate-valid parse hints for the missing spans."""
    hints = [
        {"match": fidelity.ws(span)[:40], "node_type": "paragraph", "label": "", "ref": ""}
        for span in missing_spans
    ]
    provider = FakeProvider(canned_text=json.dumps({"hints": hints}))
    return Gateway(engine, provider), provider


def test_llm_hint_text_is_not_enough_without_original_structure(engine, tmp_path):
    parsed = ["intro paragraph with some tokens"]
    missing = [f"unparsed article {i} text " + "word " * 20 for i in range(10)]
    adapter = _GappyAdapter(missing=missing, parsed=parsed, source_suffix="llm")
    gateway, provider = _repair_gateway(engine, missing)
    summary = pipeline.ingest(engine, adapter, pipeline.LocalStore(tmp_path / "lake"), gateway=gateway)
    assert summary["status"] == "not-fully-successful"
    assert provider.calls == 1
    assert not summary["original_verification"]["verified"]
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(source_versions)).scalar_one() == 0
        assert conn.execute(sa.select(sa.func.count()).select_from(parse_hints)).scalar_one() == 0
        assert conn.execute(sa.select(llm_calls.c.fleet)).scalar_one() == "l1.repair"


def test_rejected_hint_is_retired_and_not_applied(engine, tmp_path):
    from app.clhear.platform import proposals as l0_proposals
    from app.clhear.l1.models import sources
    missing = [f"gap {i} " + "tok " * 25 for i in range(8)]
    adapter = _GappyAdapter(missing=missing, parsed=["intro paragraph"], source_suffix="retire")
    with engine.begin() as conn:
        _, source_id = pipeline.ensure_source(conn, adapter.meta())
        proposal_id = l0_proposals.create_proposal(conn, layer="l1", kind="parse_hint", subject_ref=adapter.meta().source_key,
            draft={"hints": []}, rationale="Test-only candidate requiring explicit review")
        conn.execute(parse_hints.insert().values(source_id=source_id, hint={"match": "gap", "node_type": "paragraph"},
                                               origin="llm", status="candidate", proposal_id=proposal_id))
    l0_proposals.reject(engine, proposal_id, "avner@reg42.ai")
    with engine.connect() as conn:
        assert set(conn.execute(sa.select(parse_hints.c.status)).scalars()) == {"retired"}
    gateway, provider = _repair_gateway(engine, missing)
    summary = pipeline.ingest(engine, adapter, pipeline.LocalStore(tmp_path / "lake"), gateway=gateway)
    assert provider.calls == 1
    assert not summary.get("hints_used")
    assert summary["status"] == "not-fully-successful"


def test_llm_tier_skipped_without_gateway(engine, tmp_path):
    """No API key configured -> tier 4 skipped cleanly, exhaustion still works."""
    parsed = ["short"]
    missing = [f"big gap {i} " + "tok " * 30 for i in range(15)]
    adapter = _GappyAdapter(missing=missing, parsed=parsed, source_suffix="nokey")
    summary = pipeline.ingest(engine, adapter, pipeline.LocalStore(tmp_path / "lake"), gateway=None)
    assert summary["status"] == "not-fully-successful"
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(llm_calls)).scalar_one() == 0


# ------------------------------------------------------------ audit trail feeds
def test_activity_feed_and_fleet_board(engine, client, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")
    ok_adapter = _GappyAdapter(missing=[], parsed=["fine text " * 10], source_suffix="ok")
    pipeline.ingest(engine, ok_adapter, store)
    fail_adapter = _GappyAdapter(
        missing=[f"gone {i} " + "tok " * 30 for i in range(15)], parsed=["short"], source_suffix="bad"
    )
    pipeline.ingest(engine, fail_adapter, store)

    feed = client.get("/api/clhear/activity").json()
    statuses = {(i["source_key"], i["status"]) for i in feed}
    assert ("gappy/ok", "success") in statuses
    assert ("gappy/bad", "failure") in statuses
    failure = next(i for i in feed if i["source_key"] == "gappy/bad" and i["type"] == "run")
    assert "pending manual rectification" in failure["summary"]
    assert failure["links"].get("review") == "/review"
    version_updates = [i for i in feed if i["type"] == "version_update"]
    assert any("first version v1" in i["summary"] for i in version_updates)

    board = client.get("/api/clhear/fleet").json()
    ok_row = next(r for r in board if r["source_key"] == "gappy/ok")
    assert ok_row["current_version"] == "v1"
    assert ok_row["last_run"]["status"] == "success"
    stage_names = [s["stage"] for s in ok_row["last_run"]["stages"]]
    assert stage_names[:3] == ["fetch", "parse", "gate"]

    run = client.get(f"/api/clhear/runs/{ok_row['last_run']['run_id']}").json()
    assert run["status"] == "succeeded"
    assert any(s["stage"] == "persist" for s in run["stages"]) or run["outputs"].get("nodes")

    # Audit trail carries metadata only — no clause text.
    assert "fine text" not in json.dumps(feed)


def test_stale_running_run_is_unknown_without_completion_evidence(engine, client, tmp_path):
    """Midnight TNA 202 crashes left status=running; Fleet must not pulse forever."""
    from datetime import datetime, timedelta, timezone

    from app.clhear.models import runs

    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, _GappyAdapter(missing=[], parsed=["fine text " * 10], source_suffix="ok"), store)
    with engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                fleet="l1.uk_legislation",
                trigger="schedule",
                inputs={"source": "gappy/ok"},
                outputs={"status": "running", "stages": []},
                created_at=datetime.now(timezone.utc) - timedelta(hours=8),
            )
        )
    board = client.get("/api/clhear/fleet").json()
    row = next(r for r in board if r["source_key"] == "gappy/ok")
    assert row["last_run"]["status"] == "info"
    assert row["last_run"]["raw_status"] == "unknown"
    assert not row["last_run"].get("error")
