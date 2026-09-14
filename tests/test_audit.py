"""Audit log (HLD v2 §7.1; item 17): every write through the record path, every read
of licensed clause text, every mutating HTTP request — attributed, append-only, and
queryable by maintainers only."""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.clhear import curated
from app.clhear.derived_models import blocks as blocks_t
from app.clhear.l1 import pipeline
from app.clhear.l1.models import clauses, source_versions, sources
from app.clhear.platform import audit, record
from app.clhear.platform.audit import audit_log
from tests.test_l1_synthetic_amendment import V1, SyntheticAdapter

MAINTAINER = "avner@reg42.ai"


def _entries(engine, **filters):
    with engine.connect() as conn:
        return audit.query(conn, **filters)


# --------------------------------------------------------------------------- writes


def test_every_record_write_and_invalidate_lands_in_the_log_in_the_same_transaction(engine):
    why = record.WhyTrail(layer="L3", reasoning_summary="test block", agent_id="agent:test", skill_version="t1")
    with engine.begin() as conn:
        token = audit.bind_actor(audit.Actor(actor="ada@example.org", kind="user", request_id="req-1"))
        try:
            record.write(conn, blocks_t, {"id": "BLK-AUDIT-1", "name": "Audit block", "description": "", "capability": "", "evidence_artifacts": [],
                                          "satisfies": [], "implements_controls": [], "status": "curated", "kind": "Document", "purpose": "p"}, why=why)
        finally:
            audit.reset_actor(token)
    # migrations already seeded data through the same path, attributed to the migration
    seeded = _entries(engine, action="write", limit=1000)
    assert seeded and all(r["actor"].startswith("system:migration:") for r in seeded if r["resource_id"] != "BLK-AUDIT-1")
    rows = [r for r in seeded if r["resource_id"] == "BLK-AUDIT-1"]
    assert len(rows) == 1
    e = rows[0]
    assert e["actor"] == "ada@example.org" and e["actor_kind"] == "user" and e["request_id"] == "req-1"
    assert e["resource"].endswith(".blocks") and e["resource_id"] == "BLK-AUDIT-1"
    assert e["detail"]["why_trail_id"].startswith("WHY-") and e["detail"]["layer"] == "L3" and e["detail"]["version"] == 1

    # no actor bound: the row's derived_by becomes the system actor
    with engine.begin() as conn:
        record.write(conn, blocks_t, {"id": "BLK-AUDIT-2", "name": "B2", "description": "", "capability": "", "evidence_artifacts": [],
                                      "satisfies": [], "implements_controls": [], "status": "curated", "kind": "Document", "purpose": "p"},
                     why=record.WhyTrail(layer="L3", reasoning_summary="fleet", agent_id="fleet:l3"))
    sys_row = next(r for r in _entries(engine, action="write", limit=1000) if r["resource_id"] == "BLK-AUDIT-2")
    assert sys_row["actor"] == "fleet:l3" and sys_row["actor_kind"] == "system"

    # invalidation is a write too (I2: never delete, always leave a trace)
    with engine.begin() as conn:
        n = record.invalidate(conn, blocks_t, blocks_t.c.id == "BLK-AUDIT-1", why=record.WhyTrail(layer="L3", reasoning_summary="retire"), reason="test")
    assert n == 1
    inv = _entries(engine, action="invalidate")
    assert len(inv) == 1 and inv[0]["resource_id"] == "BLK-AUDIT-1" and inv[0]["detail"]["version"] == 2

    # a rolled-back write leaves no audit row: same transaction
    with pytest.raises(RuntimeError):
        with engine.begin() as conn:
            record.write(conn, blocks_t, {"id": "BLK-AUDIT-3", "name": "B3", "description": "", "capability": "", "evidence_artifacts": [],
                                          "satisfies": [], "implements_controls": [], "status": "curated", "kind": "Document", "purpose": "p"},
                         why=record.WhyTrail(layer="L3", reasoning_summary="x"))
            raise RuntimeError("abort")
    assert not [r for r in _entries(engine, action="write", limit=1000) if r["resource_id"] == "BLK-AUDIT-3"]


def test_curated_seed_is_fully_audited(engine):
    curated.seed(engine)
    with engine.connect() as conn:
        n_blocks = conn.execute(sa.select(sa.func.count()).select_from(blocks_t)).scalar_one()
        touched = conn.execute(sa.select(audit_log.c.action, audit_log.c.actor).where(audit_log.c.resource.like("%.blocks"))).all()
    assert n_blocks > 0 and len(touched) >= n_blocks
    assert {a for a, _ in touched} <= {"write", "update"} and all(actor.startswith("system:") for _, actor in touched)


# --------------------------------------------------------------------------- licensed reads


def _seed_l1(engine, tmp_path):
    from app.clhear.l1.permissions import record_permission
    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01"), store)  # rights basis: licensed
    pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01", adapter="govinfo_us", rights_basis="public_domain", source_key="synthetic/pd"), store)
    record_permission(engine, source_key="synthetic/finra", permissions={"acquire": True, "store": True, "parse": True},
                      evidence_ref="test:original-audit-fixture", approved_by="test", approved=True)
    pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01", adapter="finra", rights_basis="derived_only", source_key="synthetic/finra"), store)


def _clause(engine, source_key: str) -> int:
    with engine.connect() as conn:
        src = conn.execute(sa.select(sources.c.id).where(sources.c.key == source_key)).scalar_one()
        return conn.execute(sa.select(clauses.c.id).join(source_versions, source_versions.c.id == clauses.c.source_version_id)
                            .where(source_versions.c.source_id == src).order_by(clauses.c.id).limit(1)).scalar_one()


def test_licensed_text_reads_are_logged_and_public_domain_or_withheld_reads_are_not(engine, client, tmp_path):
    _seed_l1(engine, tmp_path)
    licensed, pd, derived = _clause(engine, "synthetic/prin"), _clause(engine, "synthetic/pd"), _clause(engine, "synthetic/finra")

    body = client.get(f"/l1/clauses/{licensed}", headers={"User-Agent": "auditor/1.0", "X-Forwarded-For": "203.0.113.9"}).json()
    assert body["text"] and body["rights_basis"] == "licensed"
    reads = _entries(engine, action="read.licensed_text")
    assert len(reads) == 1
    r = reads[0]
    assert r["resource"] == "synthetic/prin" and r["resource_id"] == str(licensed) and r["detail"]["rights_basis"] == "licensed"
    assert r["actor_kind"] == "anonymous" and r["user_agent"] == "auditor/1.0"
    assert r["ip_hash"] and r["ip_hash"] != "203.0.113.9" and len(r["ip_hash"]) == 32  # salted hash, never the address
    assert r["request_id"]

    assert client.get(f"/l1/clauses/{pd}").json()["text"]  # public domain: served, not audited
    assert client.get(f"/l1/clauses/{derived}").json()["text"] is None  # withheld: nothing to audit
    assert len(_entries(engine, action="read.licensed_text")) == 1

    # the signed-in reader is named; the request id round-trips in the response header
    resp = client.get(f"/l1/clauses/{licensed}", headers={"X-Reg42-User": MAINTAINER, "X-Request-Id": "trace-42"})
    assert resp.headers["x-request-id"] == "trace-42"
    named = _entries(engine, request_id="trace-42")
    assert named and named[0]["actor"] == MAINTAINER and named[0]["actor_kind"] == "maintainer"

    # the bulk clause listing logs once per response with the clause count
    listing = client.get("/api/clhear/sources/synthetic/prin/clauses").json()
    assert listing["clauses"] and listing["clauses"][0]["text"]
    bulk = [e for e in _entries(engine, action="read.licensed_text") if e["detail"]["route"].startswith("/api/clhear/sources")]
    assert len(bulk) == 1 and bulk[0]["detail"]["clauses"] == len(listing["clauses"])


# --------------------------------------------------------------------------- HTTP writes


def test_mutating_requests_are_logged_with_actor_and_status_but_reads_are_not(engine, client):
    before = len(_entries(engine, action="http.write"))
    client.get("/l1/sources")
    assert len(_entries(engine, action="http.write")) == before
    r = client.post("/l6/blueprints", json={"attributes": {"jurisdictions": ["UK"]}}, headers={"X-App-Id": "os-dev", "Authorization": "Bearer dev-os-key"})
    rows = _entries(engine, action="http.write")
    assert len(rows) == before + 1
    e = rows[0]
    assert e["resource"] == "/l6/blueprints" and e["detail"]["method"] == "POST" and e["detail"]["status"] == r.status_code
    assert e["actor"] == "app:os-dev" and e["actor_kind"] == "app" and e["detail"]["duration_ms"] >= 0
    # GraphQL is a read even though it POSTs
    client.post("/graphql", json={"query": "{ release { id } }"})
    assert len(_entries(engine, action="http.write")) == before + 1
    # a rejected write is still a record of an attempt
    client.post("/l8/members", json={"email": "x@example.org"})
    denied = _entries(engine, action="http.write", resource="/l8/members")
    assert denied and denied[0]["detail"]["status"] in (401, 403) and denied[0]["actor_kind"] == "anonymous"


# --------------------------------------------------------------------------- reading it back


def test_audit_api_is_maintainer_only_and_filters(engine, client, tmp_path):
    _seed_l1(engine, tmp_path)
    client.get(f"/l1/clauses/{_clause(engine, 'synthetic/prin')}")
    assert client.get("/audit").status_code == 401
    assert client.get("/audit", headers={"X-Reg42-User": "nobody@example.org"}).status_code == 401  # unknown header = no identity
    h = {"X-Reg42-User": MAINTAINER}
    page = client.get("/audit", headers=h, params={"action": "read.*"}).json()
    assert page["count"] >= 1 and all(e["action"].startswith("read.") for e in page["entries"])
    since = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert client.get("/audit", headers=h, params={"since": since}).json()["count"] == 0
    assert client.get("/audit", headers=h, params={"since": "not-a-date"}).status_code == 422
    s = client.get("/audit/summary", headers=h).json()
    assert s["by_action"]["read.licensed_text"] >= 1 and s["licensed_reads_by_source"]["synthetic/prin"] >= 1 and s["total"] >= s["by_action"]["read.licensed_text"]
    assert client.get("/audit/summary").status_code == 401


def test_audit_log_is_append_only_and_hashes_never_hold_raw_ips():
    src = inspect.getsource(audit)
    audit.assert_append_only(src[: src.index("def assert_append_only")])  # everything but the checker's own needle list
    with pytest.raises(AssertionError):
        audit.assert_append_only("conn.execute(audit_log.delete())")
    assert audit.hash_ip("") == "" and audit.hash_ip("10.0.0.1") == audit.hash_ip("10.0.0.1") != audit.hash_ip("10.0.0.2")
    with pytest.raises(ValueError):
        audit.Actor(kind="root")
    assert audit.should_audit_http("POST", "/l6/blueprints") and not audit.should_audit_http("GET", "/l6/blueprints") and not audit.should_audit_http("POST", "/graphql")
