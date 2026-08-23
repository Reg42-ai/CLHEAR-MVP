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
        body = "\n".join(self.parsed + self.missing)
        return FetchResult(
            version_label=self.version,
            artifacts=[Artifact(name="doc.txt", content=body.encode(), content_type="text/plain")],
            tree=[
                DocNode(node_type="provision", ref=f"p{i}", raw_text=text)
                for i, text in enumerate(self.parsed)
            ],
        )

    def expected_text(self, artifacts):
        return artifacts[0].content.decode().split("\n")


def test_loop_converges_via_salvage(engine, tmp_path):
    """A small residual gap (< salvage cap) is recovered as flagged notes;
    the run persists as a WARNING with recovered_spans in the summary."""
    parsed = [f"provision text number {i} with plenty of tokens to weigh the corpus" for i in range(50)]
    adapter = _GappyAdapter(missing=["one tiny missed line of several tokens"], parsed=parsed, source_suffix="salvage")
    summary = pipeline.ingest(engine, adapter, pipeline.LocalStore(tmp_path / "lake"))
    assert summary["status"] == "added"
    assert summary["recovered_spans"] == 1
    assert summary["coverage"] >= THRESHOLD
    with engine.connect() as conn:
        recovered = conn.execute(
            sa.select(doc_nodes).where(doc_nodes.c.ref == fidelity.SALVAGE_REF)
        ).one()
        assert recovered.node_type == "note"
        run_status = conn.execute(sa.text("select outputs from runs order by id desc limit 1")).scalar_one()
    assert json.loads(run_status)["status"] == "warning"


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


def test_llm_tier_repairs_and_hint_memory_prevents_repeat_calls(engine, tmp_path):
    """Tier 4: LLM hints repair a big gap (gate re-validates, text stays
    byte-from-artifact); hints persist; a SECOND ingest converges via stored
    hints with ZERO additional LLM calls."""
    parsed = ["intro paragraph with some tokens"]
    missing = [f"unparsed article {i} text " + "word " * 20 for i in range(10)]
    adapter = _GappyAdapter(missing=missing, parsed=parsed, source_suffix="llm")
    gateway, provider = _repair_gateway(engine, missing)

    summary = pipeline.ingest(engine, adapter, pipeline.LocalStore(tmp_path / "lake"), gateway=gateway)
    assert summary["status"] == "added"
    assert summary["llm_assisted"] is True
    assert provider.calls == 1
    with engine.connect() as conn:
        call = conn.execute(sa.select(llm_calls)).one()
        assert call.fleet == "l1.repair"
        stored = conn.execute(sa.select(parse_hints)).all()
        assert len(stored) == len(missing)
        assert all(h.status == "candidate" and h.origin == "llm" for h in stored)
        ratification = conn.execute(sa.select(proposals).where(proposals.c.kind == "parse_hint")).one()
        assert ratification.status == "proposed"
        # LLM never authored text: every recovered node's text is artifact text
        recovered = conn.execute(
            sa.select(doc_nodes.c.raw_text).where(doc_nodes.c.node_type == "paragraph")
        ).scalars().all()
        artifact_text = "\n".join(parsed + missing)
        assert all(fidelity.ws(t) in fidelity.ws(artifact_text) for t in recovered if t)

    # Re-ingest (new version content) — stored hints apply at tier 1b, no LLM.
    adapter2 = _GappyAdapter(missing=missing, parsed=parsed + ["a new provision"], source_suffix="llm", version="v2")
    summary2 = pipeline.ingest(engine, adapter2, pipeline.LocalStore(tmp_path / "lake"), gateway=gateway)
    assert summary2["status"] == "amended"
    assert summary2.get("hints_used")
    assert "llm_assisted" not in summary2
    assert provider.calls == 1  # unchanged: fleet learned, does not repeat the mistake
    with engine.connect() as conn:
        hint = conn.execute(sa.select(parse_hints).order_by(parse_hints.c.id).limit(1)).one()
        assert hint.times_used >= 2
        assert hint.last_used_at is not None


def test_rejected_hint_is_retired_and_not_applied(engine, tmp_path):
    parsed = ["intro paragraph"]
    missing = [f"gap {i} " + "tok " * 25 for i in range(8)]
    adapter = _GappyAdapter(missing=missing, parsed=parsed, source_suffix="retire")
    gateway, provider = _repair_gateway(engine, missing)
    assert pipeline.ingest(engine, adapter, pipeline.LocalStore(tmp_path / "lake"), gateway=gateway)["llm_assisted"]

    from app.clhear.platform import proposals as l0_proposals

    with engine.connect() as conn:
        proposal_id = conn.execute(
            sa.select(proposals.c.id).where(proposals.c.kind == "parse_hint")
        ).scalar_one()
    l0_proposals.reject(engine, proposal_id, "avner@reg42.ai")
    with engine.connect() as conn:
        statuses = set(conn.execute(sa.select(parse_hints.c.status)).scalars())
        assert statuses == {"retired"}

    # Next ingest: retired hints NOT applied -> loop needs the LLM again.
    adapter2 = _GappyAdapter(missing=missing, parsed=parsed + ["more"], source_suffix="retire", version="v2")
    summary = pipeline.ingest(engine, adapter2, pipeline.LocalStore(tmp_path / "lake"), gateway=gateway)
    assert provider.calls == 2
    assert summary.get("llm_assisted") is True


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
