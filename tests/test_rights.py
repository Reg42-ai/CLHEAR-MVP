"""Rights and sourcing (HLD v2 §7.3; never-list "verbatim text without a recorded rights
basis" and "quoting licensed text beyond the rights basis"; trace rows 11.4 and 12.7).

* every source carries a recorded rights basis with evidence, and the ledger keeps history;
* no route serves clause text for a `derived_only` / `byol_only` basis — detail, listing,
  node inspector, search, GraphQL, JSON-LD, change feed;
* the similarity guard bounds how much of a restricted clause a derived output may repeat,
  and CI runs it (`python -m app.clhear.l1.guard`).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.clhear.derived_models import asserts, obligations
from app.clhear.l1 import guard, pipeline
from app.clhear.l1 import rights as l1_rights
from app.clhear.l1.models import RIGHTS_BASES, clauses, rights_records, source_versions, sources
from app.clhear.l2.extract import run_extraction
from app.clhear.platform import record
from tests.test_l1_synthetic_amendment import V1, SyntheticAdapter

ROOT = Path(__file__).resolve().parents[1]
RESTRICTED = {"derived_only", "byol_only"}


LONG_CLAUSE = ("A member must establish and maintain a system to supervise the activities of each associated person that is "
               "reasonably designed to achieve compliance with applicable securities laws and regulations, and with applicable "
               "FINRA rules, including written procedures, the designation of registered principals, and an annual review.")
RESTRICTED_PROVISIONS = {**V1, "4": LONG_CLAUSE}


def _seed(engine, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01"), store)  # licensed (open licence file)
    pipeline.ingest(engine, SyntheticAdapter(RESTRICTED_PROVISIONS, "2026-01-01", adapter="finra", rights_basis="derived_only", source_key="synthetic/finra"), store)
    pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01", adapter="govinfo_us", rights_basis="public_domain", source_key="synthetic/pd"), store)


def _clause_row(engine, source_key: str, ref: str | None = None):
    with engine.connect() as conn:
        src = conn.execute(sa.select(sources).where(sources.c.key == source_key)).mappings().one()
        q = (sa.select(clauses).join(source_versions, source_versions.c.id == clauses.c.source_version_id)
             .where(source_versions.c.source_id == src["id"]).order_by(clauses.c.id))
        if ref:
            q = q.where(clauses.c.ref == ref)
        return src, conn.execute(q.limit(1)).mappings().one()


# --------------------------------------------------------------------------- basis recorded


def test_every_adapter_has_a_basis_with_evidence_and_the_ledger_keeps_history(engine, tmp_path):
    for key, basis in l1_rights.RIGHTS.items():
        assert basis.basis in RIGHTS_BASES and basis.ref, key
        if basis.basis in ("public_domain", "open_licence", "licensed"):
            assert basis.evidence_url or key in ("lists", "restricted_file"), f"{key}: republishable basis needs an evidence URL"
    assert l1_rights.rights_for("finra").basis == "derived_only" and l1_rights.rights_for("anything", license="restricted").basis == "byol_only"
    assert l1_rights.rights_for("never-heard-of-it").basis == "licensed"  # unknown publisher: cautious default, never public domain
    with pytest.raises(ValueError):
        l1_rights.RightsBasis("free_for_all", "x")

    _seed(engine, tmp_path)
    src, _ = _clause_row(engine, "synthetic/finra")
    with engine.begin() as conn:
        current = l1_rights.history(conn, src["id"])[-1]
        assert l1_rights.record(conn, src["id"], l1_rights.RightsBasis(current["rights_basis"], current["basis_ref"])) is False  # unchanged: no new row
        assert l1_rights.record(conn, src["id"], l1_rights.RightsBasis("licensed", "publisher granted permission", "https://example.org/permission")) is True
        hist = l1_rights.history(conn, src["id"])
        assert [h["rights_basis"] for h in hist] == ["derived_only", "licensed"]
        assert conn.execute(sa.select(sources.c.rights_basis).where(sources.c.id == src["id"])).scalar_one() == "licensed"
        n = conn.execute(sa.select(sa.func.count()).select_from(rights_records).where(rights_records.c.source_id == src["id"])).scalar_one()
    assert n == 2  # append-only ledger


# --------------------------------------------------------------------------- no text without a basis


def test_no_route_serves_restricted_text(engine, client, tmp_path):
    _seed(engine, tmp_path)
    src, row = _clause_row(engine, "synthetic/finra")
    text = row["text"]
    assert text and len(text) > 20  # the store holds it (for derivation); the API must not
    probe = text[:40]

    detail = client.get(f"/l1/clauses/{row['id']}").json()
    assert detail["text"] is None and detail["text_hash"] and "derived_only" in detail["text_withheld_reason"]
    listing = client.get("/api/clhear/sources/synthetic/finra/clauses").json()
    assert listing["locked"] is True and listing["rights_basis"] == "derived_only"
    assert listing["clauses"] and all(c["text"] is None and c["ref"] and c["text_hash"] for c in listing["clauses"])  # refs and hashes, never text
    src_detail = client.get("/l1/sources/synthetic/finra").json()
    assert src_detail["rights"]["basis"] == "derived_only" and src_detail["rights"]["republish_text"] is False
    for url in ("/l1/sources/synthetic/finra", "/l1/changes?source=synthetic/finra", f"/l1/clauses/{row['id']}", "/l1/sources"):
        assert probe not in client.get(url).text, url
    q = json.dumps({"query": '{ obligations(limit: 200) { id statement source } }'})
    assert probe not in client.post("/graphql", content=q, headers={"Content-Type": "application/json"}).text

    # the licensed and public-domain sources do serve text, with the basis stated
    for key in ("synthetic/prin", "synthetic/pd"):
        _, r = _clause_row(engine, key)
        d = client.get(f"/l1/clauses/{r['id']}").json()
        assert d["text"] and d["rights_basis"] in ("licensed", "public_domain")


def test_extraction_never_derives_statements_from_restricted_text(engine, tmp_path):
    _seed(engine, tmp_path)
    run_extraction(engine)
    with engine.connect() as conn:
        restricted = conn.execute(sa.select(obligations.c.statement).where(obligations.c.source_key == "synthetic/finra")).scalars().all()
        open_ = conn.execute(sa.select(obligations.c.statement).where(obligations.c.source_key == "synthetic/prin")).scalars().all()
    assert not restricted  # no machine derivation on text we may not inspect
    assert open_ and all(s for s in open_)


# --------------------------------------------------------------------------- similarity guard


def test_similarity_guard_bounds_quotation_by_basis():
    clause = ("A relevant person must apply customer due diligence measures when the person establishes a business "
              "relationship, carries out an occasional transaction, suspects money laundering or doubts the veracity of documents.")
    fact = "Firms must apply customer due diligence when starting a relationship, on occasional transactions, or on suspicion."
    assert guard.check(fact, clause, "derived_only").ok
    copy = guard.check(clause, clause, "derived_only")
    assert not copy.ok and copy.longest_run > 12 and copy.containment == 1.0 and any("verbatim run" in r for r in copy.reasons)
    assert guard.check(clause, clause, "public_domain").ok and guard.check(clause, clause, "open_licence").ok
    quote = f"Under the regulation, '{' '.join(clause.split()[:14])}' — the firm's programme therefore starts at onboarding."
    assert guard.check(quote, clause, "licensed").ok and not guard.check(quote, clause, "byol_only").ok
    # punctuation and case changes do not launder a copy
    laundered = clause.upper().replace(",", " ;").replace(".", " !")
    assert not guard.check(laundered, clause, "derived_only").ok
    long_copy = " ".join([clause] * 3)
    v = guard.check(long_copy, clause, "licensed")
    assert not v.ok and any("copied text" in r for r in v.reasons)  # republication, not quotation
    with pytest.raises(ValueError):
        guard.check("x", "y", "creative_commons")
    assert guard.longest_common_run([], ["a"]) == 0 and guard.containment(["a", "b"], ["a", "b"]) == 1.0
    assert guard.self_test() == []


def test_similarity_guard_scans_the_store_and_finds_a_planted_copy(engine, tmp_path):
    _seed(engine, tmp_path)
    run_extraction(engine)
    clean = guard.scan(engine)
    assert clean["ok"] and clean["checked"] >= 1  # the licensed source's derived statements are within its basis
    src, row = _clause_row(engine, "synthetic/finra", ref="4")
    assert len(row["text"].split()) > 12  # long enough that a copy is a copy, not a de-minimis phrase
    with engine.begin() as conn:
        why = record.WhyTrail(layer="L2", reasoning_summary="planted for the guard test", agent_id="test")
        record.write(conn, obligations, {"id": "OBL:synthetic/finra#copy", "stable_id": "OBL-999001", "source_key": "synthetic/finra", "clause_ref": row["ref"],
                                         "title": "Copied clause", "statement": row["text"], "status": "derived", "text_hash": row["text_hash"]}, why=why)
        record.write(conn, asserts, {"id": "AST-999001", "obligation_id": "OBL:synthetic/finra#copy", "clause_id": row["id"], "source_key": "synthetic/finra",
                                     "clause_ref": row["ref"], "strength": "explicit", "text_hash": row["text_hash"]}, why=why)
    dirty = guard.scan(engine)
    assert not dirty["ok"] and dirty["checked"] >= 1
    hit = next(v for v in dirty["violations"] if v["id"] == "OBL:synthetic/finra#copy" and v["field"] == "statement")
    assert hit["layer"] == "L2" and hit["basis"] == "derived_only" and hit["containment"] == 1.0
    assert guard.main(["--self-test"]) == 0


def test_ci_runs_the_guard_on_every_push():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "python -m app.clhear.l1.guard" in ci
