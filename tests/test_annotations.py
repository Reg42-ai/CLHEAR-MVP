"""Clause understanding layer + stage dictionary + short names tests."""
import json

import pytest
import sqlalchemy as sa

from app.clhear.l1 import annotate, pipeline
from app.clhear.l1.adapters import ADAPTER_KEYS, get_adapter
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta
from app.clhear.l1.adapters.uk_legislation import UkLegislationAdapter
from app.clhear.l1.models import STAGE_INFO, clause_annotations
from app.clhear.models import llm_calls
from app.clhear.platform.gateway import FakeProvider, Gateway

PIPELINE_STAGES = {"fetch", "parse", "gate", "hints", "llm_repair", "salvage", "persist", "annotate", "index", "diff", "relay", "drain"}


def test_stage_dictionary_is_complete_and_served(client, engine):
    assert PIPELINE_STAGES <= set(STAGE_INFO)
    payload = client.get("/api/clhear/meta").json()
    assert set(payload["stages"]) == set(STAGE_INFO)
    assert all(len(v) > 40 for v in payload["stages"].values())
    assert payload["annotation_categories"] == ["definition", "requirement", "enforcement", "other"]


@pytest.mark.parametrize("key", list(ADAPTER_KEYS))
def test_every_source_has_a_short_name(key):
    assert get_adapter(key).meta().short_name.strip()


def test_heuristic_annotator_on_mlr(engine, client, tmp_path):
    pipeline.ingest(engine, UkLegislationAdapter(), pipeline.LocalStore(tmp_path / "lake"))
    with engine.connect() as conn:
        rows = conn.execute(sa.select(clause_annotations)).all()
    assert rows and all(r.origin == "heuristic" for r in rows)

    doc = client.get("/api/clhear/sources/uksi/2017/692/document").json()
    assert doc["short_name"] == "UK AML Regulations (MLRs 2017)"
    by_ref = {n["ref"]: n for n in doc["nodes"] if n.get("annotation")}
    assert by_ref["regulation-3"]["annotation"]["category"] == "definition"  # "General interpretation"
    assert by_ref["regulation-28"]["annotation"]["category"] == "requirement"  # "must apply CDD measures"
    assert "aml" in by_ref["regulation-28"]["annotation"]["topics"]

    # Search category filter: definition hits exclude reg 28.
    hits = client.get("/api/clhear/search?q=customer due diligence&category=definition").json()
    refs = {h["ref"] for h in hits}
    assert "regulation-3" in refs and "regulation-28" not in refs
    assert all(h["short_name"] == "UK AML Regulations (MLRs 2017)" for h in hits)


class _TinyAdapter:
    key = "tiny"

    def meta(self) -> SourceMeta:
        return SourceMeta(
            family_key="tiny-family",
            family_name="Tiny family",
            source_key="tiny/one",
            name="Tiny source",
            kind="regulation",
            issuer="stub",
            jurisdiction="XX",
            license="open",
            canonical_url="https://example.invalid",
            adapter="tiny",
            short_name="Tiny",
            about="stub",
            topics=["stub-topic"],
            version_policy="edition",
        )

    def fetch(self, since_version=None):
        texts = {
            "r1": "A relevant person must keep records of transactions for five years.",
            "r2": "In this regulation, definitions: 'transaction' has the meaning of any transfer.",
            "r3": "A person must not disclose the report to the customer.",
        }
        return FetchResult(
            version_label="edition:1",
            artifacts=[Artifact(name="doc.txt", content="\n".join(texts.values()).encode())],
            tree=[DocNode(node_type="provision", ref=ref, raw_text=text) for ref, text in texts.items()],
            version_kind="edition",
        )

    def expected_text(self, artifacts):
        return artifacts[0].content.decode().split("\n")


def test_llm_explainer_job_offline(engine, client, tmp_path):
    pipeline.ingest(engine, _TinyAdapter(), pipeline.LocalStore(tmp_path / "lake"))

    canned = {
        "annotations": [
            {"ref": "r1", "summary": "Firms have to keep transaction records for five years.", "category": "requirement", "topics": ["record-keeping"]},
            {"ref": "r2", "summary": "Defines what counts as a transaction.", "category": "definition", "topics": ["definitions"]},
            {"ref": "r3", "summary": "Tipping off the customer is forbidden.", "category": "requirement", "topics": ["confidentiality"]},
        ]
    }
    provider = FakeProvider(canned_text=json.dumps(canned))
    gateway = Gateway(engine, provider)

    summary = annotate.annotate_llm(engine, gateway)
    assert summary["annotated"] == 3
    assert provider.calls == 1
    with engine.connect() as conn:
        llm_rows = conn.execute(
            sa.select(clause_annotations).where(clause_annotations.c.origin == "llm")
        ).all()
        assert len(llm_rows) == 3
        assert all(r.model == "claude-3-5-haiku-latest" and r.prompt_hash for r in llm_rows)
        call = conn.execute(sa.select(llm_calls)).one()
        assert call.fleet == "l1.annotate"

    # Idempotent: nothing left to annotate, no extra gateway call.
    again = annotate.annotate_llm(engine, gateway)
    assert again["annotated"] == 0 and provider.calls == 1

    # Document endpoint prefers the llm explainer over the heuristic row.
    doc = client.get("/api/clhear/sources/tiny/one/document").json()
    r1 = next(n for n in doc["nodes"] if n["ref"] == "r1")
    assert r1["annotation"]["origin"] == "llm"
    assert "five years" in r1["annotation"]["summary"]
    # ...and the verbatim text is untouched.
    assert r1["raw_text"].startswith("A relevant person must keep records")


def test_heuristic_classifier_units():
    assert annotate.classify("General interpretation", "anything must anything", "Part 1") == "definition"
    assert annotate.classify("", "The person must not disclose", "") == "requirement"
    assert annotate.classify("", "A person who fails commits an offence and is liable to a fine", "") == "enforcement"
    assert annotate.classify("", "The firm must assess risks.", "") == "requirement"
    assert annotate.classify("Signature block", "Signed by authority of the Treasury", "") == "other"
