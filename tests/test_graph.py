"""HLD v2 I7 / §8 item 10 — the query graph and the vector index are projections
of the record: rebuilt from it, idempotent, disposable, and served by two
backends (Neo4j, in-process) that answer the same questions the same way."""
from __future__ import annotations

import struct

import sqlalchemy as sa

from app.clhear.l1.models import clauses
from app.clhear.models import events, graph_projections
from app.clhear.platform import embeddings, graph
from app.clhear.platform import task_classes as tc
from app.clhear.platform.events import Envelope
from app.clhear.workers import handle_envelope
from tests.test_l6_blueprints import BROKER, _corpus


def _built(engine, client, tmp_path) -> dict:
    _corpus(engine, tmp_path)
    out = client.post("/solon/build", json={"attributes": BROKER, "name": "graph test"}).json()
    assert out["ok"], out
    return out


# --------------------------------------------------------------------------- projection


def test_projection_reads_only_live_rows_and_is_deterministic(engine, client, tmp_path):
    out = _built(engine, client, tmp_path)
    snap = graph.project(engine)
    counts = snap.counts()
    assert counts["by_label"]["Obligation"] >= 5 and counts["by_label"]["Clause"] >= 5 and counts["by_label"]["Blueprint"] >= 1
    assert {"PART_OF", "ASSERTED_BY", "REQUIRES", "TRIGGERED_BY", "OPERATES", "MITIGATES", "INCLUDES", "SATISFIES", "FOR_PROFILE"} \
        <= set(counts["by_rel"])
    # every edge joins two projected nodes; the blueprint reaches its blocks and its profile
    for e in snap.edges:
        assert e["from"] in snap.nodes and e["to"] in snap.nodes
    bp = out["blueprint_id"]
    assert any(e["from"] == bp and e["rel"] == "INCLUDES" for e in snap.edges)
    assert any(e["from"] == bp and e["rel"] == "FOR_PROFILE" and e["to"] == out["profile_id"] for e in snap.edges)
    # same record -> same checksum (idempotent projection)
    assert snap.checksum() == graph.project(engine).checksum()
    # invalidating a row in the record drops it from the next projection (the record keeps it, I2)
    from app.clhear.derived_models import requires

    with engine.begin() as conn:
        rid = conn.execute(sa.select(requires.c.id).where(requires.c.valid_to.is_(None))).scalar()
        conn.execute(requires.update().where(requires.c.id == rid).values(valid_to=sa.func.current_date()))
    snap2 = graph.project(engine)
    assert snap2.counts()["by_rel"]["REQUIRES"] == counts["by_rel"]["REQUIRES"] - 1
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(requires)).scalar_one() >= counts["by_rel"]["REQUIRES"]


def test_rebuild_is_idempotent_logged_and_published(engine, client, tmp_path):
    _built(engine, client, tmp_path)
    g = graph.LocalGraph()
    first = graph.rebuild(engine, g, release="r1")
    second = graph.rebuild(engine, g, release="r1")
    assert first["status"] == second["status"] == "succeeded"
    assert first["checksum"] == second["checksum"] and first["nodes"] == second["nodes"]
    with engine.connect() as conn:
        runs = conn.execute(sa.select(graph_projections).where(graph_projections.c.backend == "local")
                            .order_by(graph_projections.c.id)).mappings().all()
        assert len(runs) >= 2 and all(r["status"] == "succeeded" for r in runs[-2:])
        assert runs[-1]["checksum"] == first["checksum"] and runs[-1]["release"] == "r1"
        kinds = [r[0] for r in conn.execute(sa.select(events.c.kind).where(events.c.kind == "clhear.l0.graph_rebuilt"))]
        assert len(kinds) >= 2
    st = graph.status(engine)
    assert st["last_rebuild"]["backend"] == "local" and st["budget_ms"] == 300 and st["backend"] == "local"


def test_evidence_and_derived_are_one_hop_each_way(engine, client, tmp_path):
    out = _built(engine, client, tmp_path)
    g = graph.get_graph(engine)
    snap = g.snapshot
    ob = next(n for n in snap.nodes.values() if n["kind"] == "obligation")
    ev = g.evidence_for(ob["id"])
    assert ev["obligation"]["id"] == ob["id"] and ev["count"] >= 1
    clause = ev["clauses"][0]
    assert clause["clause"]["kind"] == "clause" and clause["source"]["kind"] == "source"
    assert clause["strength"] in ("explicit", "implied")
    # stable id resolves too (public ids are the API surface)
    assert g.evidence_for(ob["stable_id"])["obligation"]["id"] == ob["id"]
    # and back: the clause knows what was derived from it, one hop down each layer
    d = g.derived_from(clause["clause"]["id"])
    assert d["clause"]["id"] == clause["clause"]["id"] and d["source"]["id"] == clause["source"]["id"]
    assert any(r["obligation"]["id"] == ob["id"] for r in d["obligations"])
    row = next(r for r in d["obligations"] if r["obligation"]["id"] == ob["id"])
    assert row["requires"] and all(b["kind"] == "block" for b in row["requires"])
    assert out["blueprint_id"] in row["satisfied_in"] or row["satisfied_in"] == []  # satisfied when the blueprint covers it
    assert g.evidence_for("OBL-nope") is None and g.derived_from("CLS-999999") is None
    # numeric clause ids are accepted (the L1 API speaks in them)
    assert g.derived_from(clause["clause"]["id"].removeprefix("CLS-"))["count"] == d["count"]


# --------------------------------------------------------------------------- neo4j plan


class _FakeResult:
    def __init__(self, record=None):
        self._record = record

    def single(self):
        return self._record


class _FakeSession:
    def __init__(self, log):
        self.log = log

    def run(self, statement, **params):
        self.log.append((statement, params))
        return _FakeResult()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeDriver:
    def __init__(self):
        self.log: list[tuple[str, dict]] = []

    def session(self, database=None):
        return _FakeSession(self.log)


def test_neo4j_rebuild_merges_by_id_and_sweeps_stale_projection(engine, client, tmp_path):
    _built(engine, client, tmp_path)
    snap = graph.project(engine)
    drv = _FakeDriver()
    ng = graph.Neo4jGraph("bolt://fake:7687", "neo4j", "x", driver=drv)
    out = ng.rebuild(snap)
    assert out["backend"] == "neo4j" and out["nodes"] == len(snap.nodes) and out["edges"] == len(snap.edges)
    statements = [s for s, _ in drv.log]
    # uniqueness constraints per label, MERGE (never CREATE) for nodes and edges, stamped with the projection id
    assert sum("CREATE CONSTRAINT" in s for s in statements) == len(graph.LABELS)
    merges = [(s, p) for s, p in drv.log if s.startswith("UNWIND")]
    assert merges and all("MERGE" in s and "CREATE (" not in s for s, _ in merges)
    assert all(p["stamp"] == snap.projected_at for _, p in merges)
    merged_nodes = sum(len(p["rows"]) for s, p in merges if "MERGE (n:" in s)
    merged_edges = sum(len(p["rows"]) for s, p in merges if "MERGE (a)-[r:" in s)
    assert merged_nodes == len(snap.nodes) and merged_edges == len(snap.edges)
    # properties are Neo4j-safe (no nested maps) and the sweep removes what this projection did not produce
    for s, p in merges:
        for row in p["rows"]:
            for v in row["props"].values():
                assert isinstance(v, (str, int, float, bool, list)) or v is None
                if isinstance(v, list):  # Neo4j rejects null members and mixed types
                    assert v and None not in v and len({type(x) for x in v}) == 1
    assert statements[-2:] == [graph.CYPHER["sweep_edges"], graph.CYPHER["sweep_nodes"]]
    # the two acceptance queries are parameterised, never string-built
    assert "$id" in graph.CYPHER["evidence_for"] and "ASSERTED_BY" in graph.CYPHER["evidence_for"] and "PART_OF" in graph.CYPHER["evidence_for"]
    assert "$id" in graph.CYPHER["derived_from"] and "REQUIRES" in graph.CYPHER["derived_from"]
    ng.evidence_for("OBL-000001")
    assert drv.log[-1] == (graph.CYPHER["evidence_for"], {"id": "OBL-000001"})
    ng.derived_from("7")
    assert drv.log[-1] == (graph.CYPHER["derived_from"], {"id": "CLS-7"})


# --------------------------------------------------------------------------- vector index


def test_hash_embedder_is_deterministic_unit_length_and_1024_dims():
    emb = embeddings.HashEmbedder()
    a, b = emb.embed(["A firm must reconcile client money daily", "A firm must reconcile client money daily"])
    assert a == b and len(a) == embeddings.DIM == 1024
    assert abs(sum(x * x for x in a) - 1.0) < 1e-6
    c = emb.embed(["The regulator may publish guidance on marketing"])[0]
    near = sum(x * y for x, y in zip(a, emb.embed(["reconcile client money every day"])[0]))
    far = sum(x * y for x, y in zip(a, c))
    assert near > far
    assert embeddings.unpack(embeddings.pack(a)) == [struct.unpack("<f", struct.pack("<f", x))[0] for x in a]


def test_index_is_rebuilt_from_the_record_and_only_reembeds_changes(engine, client, tmp_path):
    _built(engine, client, tmp_path)
    emb = embeddings.HashEmbedder()
    first = embeddings.rebuild_index(engine, emb, force=True)
    assert first["backend"] == "sqlite-vec" and first["embedded"] == first["indexable"] >= 5 and first["model"] == "hash-v1"
    again = embeddings.rebuild_index(engine, emb)
    assert again["embedded"] == 0 and again["skipped"] == first["indexable"]
    # text changed in the record -> that clause (only) is re-embedded
    with engine.begin() as conn:
        cid = conn.execute(sa.select(clauses.c.id).where(clauses.c.embedding.is_not(None))).scalar()
        conn.execute(clauses.update().where(clauses.c.id == cid).values(text_hash="changed"))
    third = embeddings.rebuild_index(engine, emb)
    assert third["embedded"] == 1
    # another model -> everything is re-embedded under its name; the old vectors are replaced, not mixed
    other = embeddings.HashEmbedder()
    other.name = "hash-v2"
    fourth = embeddings.rebuild_index(engine, other)
    assert fourth["embedded"] == first["indexable"]
    st = embeddings.index_status(engine)
    assert st["dim"] == 1024 and [m["model"] for m in st["embedded"]] == ["hash-v2"]
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(graph_projections)
                            .where(graph_projections.c.backend == "sqlite-vec")).scalar_one() >= 4


def test_nearest_ranks_by_cosine_and_search_fuses_the_vector_leg(engine, client, tmp_path):
    _built(engine, client, tmp_path)
    emb = embeddings.HashEmbedder()
    embeddings.rebuild_index(engine, emb, force=True)
    hits = embeddings.semantic_search(engine, "reconcile client money records every business day", emb=emb)
    assert hits and hits[0][1] >= hits[-1][1]
    with engine.connect() as conn:
        top = conn.execute(sa.select(clauses.c.text).where(clauses.c.id == hits[0][0])).scalar_one()
    assert "client money" in top.lower()
    # the route exposes the leg on its own and the fused search still answers
    r = client.get("/graph/search", params={"q": "reconcile client money"})
    assert r.status_code == 200 and r.json()["hits"][0]["clause_id"] == hits[0][0] and r.headers["X-Query-Ms"]
    assert client.get("/graph/search", params={"q": "x"}).status_code == 422
    fused = client.get("/api/clhear/search", params={"q": "reconcile client money"}).json()
    assert fused and any("client money" in h["snippet"].lower() for h in fused[:3])


def test_vector_leg_fuses_only_for_a_learned_model(engine, client, tmp_path, monkeypatch):
    from app.clhear.l1 import retrieval

    _built(engine, client, tmp_path)
    embeddings.rebuild_index(engine, embeddings.HashEmbedder(), force=True)
    with engine.connect() as conn:
        # offline hash embedder: leg is named and empty (weight 0 -> not queried)
        assert retrieval._vec_list(engine, conn, "client money", 30) == ("vec:hash", [])
        # a learned model (same vectors, different name) fuses at full weight
        learned = embeddings.HashEmbedder()
        learned.name = "amazon.titan-embed-text-v2:0"
        embeddings.rebuild_index(engine, learned, force=True)
        monkeypatch.setattr(embeddings, "embedder", lambda **kw: learned)
        name, ids = retrieval._vec_list(engine, conn, "client money", 30)
        assert name == "vec" and ids
    fused = client.get("/api/clhear/search", params={"q": "reconcile client money"}).json()
    assert fused and "client money" in fused[0]["snippet"].lower()


def test_embed_task_class_is_non_derivation_and_mirrored_to_infer():
    embed = tc.TASK_CLASSES["embed"]
    assert embed.derivation is False and embed.layer == "L0" and embed.ladder[0] == tc.TITAN_EMBED_V2
    assert "embed" not in tc.DERIVATION_CLASSES
    assert "  embed:" in tc.to_infer_yaml()


def test_infer_embedder_calls_the_embeddings_endpoint_with_the_task_class():
    calls = []

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"model": "amazon.titan-embed-text-v2:0", "data": [{"index": i, "embedding": [0.0] * 1024} for i in range(2)]}

    class _Client:
        def post(self, url, headers, json):
            calls.append((url, headers, json))
            return _Resp()

    emb = embeddings.InferEmbedder("https://infer.test/v1", "tok", client=_Client())
    out = emb.embed(["a", "b"])
    assert len(out) == 2 and len(out[0]) == 1024 and emb.name == "amazon.titan-embed-text-v2:0"
    url, headers, body = calls[0]
    assert url == "https://infer.test/v1/embeddings" and headers["X-Task-Class"] == "embed"
    assert body["dimensions"] == 1024 and body["input"] == ["a", "b"] and headers["authorization"] == "Bearer tok"


# --------------------------------------------------------------------------- api + worker


def test_graph_routes_and_rebuild_event(engine, client, tmp_path, monkeypatch):
    out = _built(engine, client, tmp_path)
    st = client.get("/graph/status").json()
    assert st["backend"] == "local" and st["index"]["dim"] == 1024
    node = client.get(f"/explore/node/{out['blueprint_id']}").json()
    oid = next(e["to"] for e in node["edges"] if e["rel"] == "SATISFIES") if any(e["rel"] == "SATISFIES" for e in node["edges"]) else None
    snap = graph.get_graph(engine).snapshot
    oid = oid or next(n["id"] for n in snap.nodes.values() if n["kind"] == "obligation")
    from urllib.parse import quote

    r = client.get(f"/graph/obligations/{quote(oid, safe=':/')}/evidence")
    assert r.status_code == 200 and r.json()["count"] >= 1 and float(r.headers["X-Query-Ms"]) < 300
    cid = r.json()["clauses"][0]["clause"]["id"]
    r2 = client.get(f"/graph/clauses/{cid}/derived")
    assert r2.status_code == 200 and r2.json()["count"] >= 1 and "Server-Timing" in r2.headers
    assert client.get("/graph/obligations/OBL-nope/evidence").status_code == 404
    assert client.get("/graph/clauses/CLS-0/derived").status_code == 404
    assert client.get(f"/graph/nodes/{out['blueprint_id']}").json()["focus"] == out["blueprint_id"]
    # rebuild needs an app key; the worker handles the scheduled kind idempotently
    assert client.post("/graph/rebuild").status_code == 401
    monkeypatch.setenv("CLHEAR_APP_KEYS", "demo:secret")
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    r = client.post("/graph/rebuild", headers={"X-App-Id": "demo", "Authorization": "Bearer secret"})
    assert r.status_code == 200 and r.json()["graph"]["status"] == "succeeded" and "index" in r.json()
    env = Envelope(event_id="graph-1", layer="l0", kind="GraphRebuildRequested", subject_ref="all", payload={"release": "r9"},
                   producer="test", ts="")
    done = handle_envelope(engine, None, env.model_dump_json())
    assert done["graph"]["status"] == "succeeded" and done["graph"]["nodes"] == r.json()["graph"]["nodes"]
    with engine.connect() as conn:
        assert conn.execute(sa.select(graph_projections.c.release).where(graph_projections.c.trigger == "event")).scalar() == "r9"
