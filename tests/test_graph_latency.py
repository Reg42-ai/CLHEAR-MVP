"""HLD v2 §8 item 10 acceptance: obligation -> evidence and clause -> derived,
one hop each way, each under 300 ms on a corpus-sized graph.

The graph is scaled past the synthetic corpus: two real sources (UK MLR 2017
and the GDPR, replayed) supply the article-grain clauses, and a few thousand
obligations are asserted on them (a dozen per clause) with REQUIRES edges into
the L3 blocks — the fan-out a nightly projection has, not a toy. Both backends are held to the same
budget: the in-process projection always; Neo4j when a server is reachable
(``CLHEAR_TEST_NEO4J_URI``), otherwise that half is skipped, not faked.
"""
from __future__ import annotations

import os
import random
import statistics
import time

import pytest
import sqlalchemy as sa

from app.clhear.derived_models import asserts, blocks, obligations, requires
from app.clhear.l1 import pipeline
from app.clhear.l1.adapters.eur_lex import EurLexAdapter
from app.clhear.l1.adapters.uk_legislation import UkLegislationAdapter
from app.clhear.l1.models import clauses, source_versions, sources
from app.clhear.platform import graph
from tests.test_l6_blueprints import _corpus

BUDGET_MS = 300
N_OBLIGATIONS = 3000
SAMPLE = 200


def _scaled_record(engine, tmp_path) -> dict:
    """Synthetic L2–L6 corpus + two real sources, then N obligations asserted on real clauses."""
    _corpus(engine, tmp_path)
    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, UkLegislationAdapter(), store)
    pipeline.ingest(engine, EurLexAdapter(), store)
    rng = random.Random(42)
    with engine.begin() as conn:
        clause_rows = conn.execute(
            sa.select(clauses.c.id, sources.c.key.label("source_key"), clauses.c.ref, clauses.c.text_hash)
            .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
            .join(sources, sources.c.id == source_versions.c.source_id)
            .where(source_versions.c.status == "in_force", sources.c.key.in_(("uksi/2017/692", "celex/32016R0679")))
        ).all()
        block_ids = [r[0] for r in conn.execute(sa.select(blocks.c.id).where(blocks.c.valid_to.is_(None)))]
        assert len(clause_rows) > 200 and block_ids, (len(clause_rows), len(block_ids))
        ob_rows, as_rows, rq_rows = [], [], []
        for i in range(N_OBLIGATIONS):
            c = rng.choice(clause_rows)
            oid = f"OBL-L{i:06d}"
            ob_rows.append({
                "id": oid, "stable_id": f"OBL:latency/{c.source_key}#{i}", "source_key": c.source_key,
                "clause_ref": c.ref, "title": f"Latency obligation {i} on {c.ref}", "statement": "",
                "jurisdiction": "UK" if c.source_key.startswith("uk") else "EU", "themes": [],
                "confidence": 0.9, "status": "derived", "method": "latency-fixture", "text_hash": c.text_hash,
            })
            for j, cl in enumerate({c.id, rng.choice(clause_rows).id}):
                as_rows.append({"id": f"AST-L{i:06d}{j}", "obligation_id": oid, "clause_id": cl, "source_key": c.source_key,
                                "clause_ref": c.ref, "strength": "explicit" if j == 0 else "implied", "text_hash": c.text_hash})
            for j, bid in enumerate(rng.sample(block_ids, k=min(2, len(block_ids)))):
                rq_rows.append({"id": f"REQ-L{i:06d}{j}", "obligation_id": oid, "block_id": bid, "method": "latency-fixture"})
        conn.execute(obligations.insert(), ob_rows)
        conn.execute(asserts.insert(), as_rows)
        conn.execute(requires.insert(), rq_rows)
    return {"clauses": len(clause_rows), "obligations": N_OBLIGATIONS, "asserts": len(as_rows), "requires": len(rq_rows)}


def _sample(snap: graph.GraphSnapshot, rng: random.Random) -> tuple[list[str], list[str]]:
    obs = [nid for nid, n in snap.nodes.items() if n["kind"] == "obligation"]
    cls = list({e["to"] for e in snap.edges if e["rel"] == "ASSERTED_BY"})
    return rng.sample(obs, SAMPLE), rng.sample(cls, min(SAMPLE, len(cls)))


def _measure(fn, ids: list[str]) -> dict:
    times = []
    hits = 0
    for i in ids:
        t = time.perf_counter()
        out = fn(i)
        times.append((time.perf_counter() - t) * 1000)
        hits += bool(out and out["count"])
    times.sort()
    return {"n": len(ids), "hits": hits, "max_ms": times[-1], "p95_ms": times[int(len(times) * 0.95) - 1],
            "median_ms": statistics.median(times)}


def test_local_projection_answers_both_directions_under_budget(engine, client, tmp_path):
    scale = _scaled_record(engine, tmp_path)
    g = graph.LocalGraph()
    run = graph.rebuild(engine, g, release="latency", publish=False)
    assert run["status"] == "succeeded" and run["nodes"] > scale["obligations"] + scale["clauses"]
    rng = random.Random(7)
    ob_ids, cl_ids = _sample(g.snapshot, rng)

    evidence = _measure(g.evidence_for, ob_ids)
    derived = _measure(g.derived_from, cl_ids)
    assert evidence["hits"] == SAMPLE and derived["hits"] == len(cl_ids)
    assert evidence["max_ms"] < BUDGET_MS, evidence
    assert derived["max_ms"] < BUDGET_MS, derived

    # through the API, cold (first request builds nothing extra: the projection is cached) and warm
    graph.invalidate(engine)
    graph.rebuild(engine, release="latency", publish=False)
    for oid in ob_ids[:20]:
        r = client.get(f"/graph/obligations/{oid}/evidence")
        assert r.status_code == 200 and r.json()["count"] >= 1
        assert float(r.headers["X-Query-Ms"]) < BUDGET_MS, r.headers
    for cid in cl_ids[:20]:
        r = client.get(f"/graph/clauses/{cid.removeprefix('CLS-')}/derived")
        assert r.status_code == 200 and r.json()["count"] >= 1
        assert float(r.headers["X-Query-Ms"]) < BUDGET_MS, r.headers
    st = client.get("/graph/status").json()
    assert st["budget_ms"] == BUDGET_MS and st["last_rebuild"]["nodes"] == run["nodes"]


@pytest.mark.skipif(not os.environ.get("CLHEAR_TEST_NEO4J_URI"), reason="needs a Neo4j server (CLHEAR_TEST_NEO4J_URI)")
def test_neo4j_projection_answers_both_directions_under_budget(engine, client, tmp_path):
    from neo4j import GraphDatabase

    uri = os.environ["CLHEAR_TEST_NEO4J_URI"]
    user, password = os.environ.get("CLHEAR_TEST_NEO4J_USER", "neo4j"), os.environ.get("CLHEAR_TEST_NEO4J_PASSWORD", "")
    driver = GraphDatabase.driver(uri, auth=(user, password) if password else None, notifications_min_severity="WARNING")
    try:
        _scaled_record(engine, tmp_path)
        ng = graph.Neo4jGraph(uri, driver=driver)
        snap = graph.project(engine)
        t = time.perf_counter()
        out = ng.rebuild(snap)
        rebuild_ms = (time.perf_counter() - t) * 1000
        assert out["nodes"] == len(snap.nodes) and out["edges"] == len(snap.edges)
        rng = random.Random(7)
        ob_ids, cl_ids = _sample(snap, rng)
        evidence = _measure(ng.evidence_for, ob_ids)
        derived = _measure(ng.derived_from, cl_ids)
        assert evidence["hits"] == SAMPLE and derived["hits"] == len(cl_ids)
        assert evidence["max_ms"] < BUDGET_MS, {**evidence, "rebuild_ms": rebuild_ms}
        assert derived["max_ms"] < BUDGET_MS, {**derived, "rebuild_ms": rebuild_ms}
        # both backends answer the same question the same way
        lg = graph.LocalGraph()
        lg.rebuild(snap)
        for oid in ob_ids[:10]:
            a, b = ng.evidence_for(oid), lg.evidence_for(oid)
            assert {c["clause"]["id"] for c in a["clauses"]} == {c["clause"]["id"] for c in b["clauses"]}
        for cid in cl_ids[:10]:
            a, b = ng.derived_from(cid), lg.derived_from(cid)
            assert {r["obligation"]["id"] for r in a["obligations"]} == {r["obligation"]["id"] for r in b["obligations"]}
        # a second rebuild of the same record leaves the graph exactly as large (idempotent, stale swept)
        ng.rebuild(snap)
        with driver.session() as s:
            assert s.run("MATCH (n) RETURN count(n) AS c").single()["c"] == out["nodes"]
            assert s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"] == out["edges"]
    finally:
        driver.close()
