"""Independent scope gaps and exact stored artifact/projection reconciliation.

All prose below is original unit-test material, not a regulatory demo corpus.
"""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.clhear.l1 import inventory as inv
from app.clhear.l1.adapters.base import SourceMeta
from app.clhear.l1.adapters.finra import parse
from app.clhear.l1.models import clauses, doc_nodes, source_versions
from app.clhear.l1.permissions import record_permission
from app.clhear.l1.pipeline import LocalStore, ensure_source, persist_tree
from app.clhear.l1.spans import canonical_text
from app.clhear.models import runs


KEY = "finra/rule/2210"
URL = "https://www.finra.org/rules-guidance/rulebooks/finra-rules/2210"
BODY = b"<html><body><h1>2210. Original test rule</h1><p>(a) Test firms must keep test records.</p><p>(b) Test firms must review test notices.</p></body></html>"
ENTRY = inv._discovered_entry(URL, "rules")


def grant(engine, key=KEY, **changes):
    args = {"source_key": key, "permissions": {"acquire": True, "store": True, "parse": True},
            "evidence_ref": "test-only:reviewed-artifact", "approved_by": "unit-test-reviewer", "approved": True}
    args.update(changes)
    return record_permission(engine, **args)


@pytest.fixture
def small_scope(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(inv, "_declared_entries", lambda scope: {KEY: dict(ENTRY)})
    return engine, LocalStore(tmp_path / "originals")


def imported(engine, store, *, body=BODY, version_label="test-edition-1", latest=True):
    meta = SourceMeta(family_key="test-finra", family_name="Unit-test FINRA", source_key=KEY,
                      name="Original test rule", kind="regulation", issuer="FINRA", jurisdiction="US",
                      license="restricted", canonical_url=URL, adapter="finra")
    tree = parse(body, KEY, URL)
    digest = hashlib.sha256(body).hexdigest()
    key = f"restricted/{KEY}/{version_label}/original.html"
    uri = store.put(key, body, "text/html")
    with engine.begin() as conn:
        _, source_id = ensure_source(conn, meta)
        version_id = conn.execute(source_versions.insert().values(source_id=source_id, version_label=version_label,
                     content_hash=digest, s3_uri=uri, status="in_force" if latest else "superseded").returning(source_versions.c.id)).scalar_one()
        rows = persist_tree(conn, version_id, tree, public_ok=False)
        conn.execute(clauses.insert(), rows)
        conn.execute(runs.insert().values(fleet="l1.finra", trigger="test", inputs={"source": KEY}, outputs={
            "status": "added", "source_version_id": version_id, "content_hash": digest,
            "publisher_checked_at": datetime.now(timezone.utc).isoformat(), "parser_identity": {"sha256": "unit-test-parser"},
            "fetch_evidence": [{"url": URL, "origin": "live", "sha256": digest, "checked_at": datetime.now(timezone.utc).isoformat()}],
            "canonical_text_hash": hashlib.sha256(canonical_text(tree).encode()).hexdigest(),
            "artifact_manifest": [{"name": "original.html", "key": key, "uri": uri, "sha256": digest,
                                    "byte_count": len(body), "content_type": "text/html"}],
        }))
    return version_id, key


def complete_discovery(monkeypatch):
    monkeypatch.setattr(inv, "_discover", lambda engine, store: ({}, {
        "complete": True, "checked_at": datetime.now(timezone.utc).isoformat(), "pages": [],
        "categories": [{"key": "rules", "status": "checked"}], "findings": [],
    }))


def review_and_audit(engine, store, monkeypatch):
    complete_discovery(monkeypatch)
    first = inv.run_inventory_audit(engine, store, job_id="initial-discovery", scope="finra", discover=True)
    inv.record_scope_review(engine, first["inventory_hash"], "test-only:independent-complete-index", "unit-test-reviewer", True)
    return inv.run_inventory_audit(engine, store, job_id="after-scope-review", scope="finra")


def codes(source):
    return {finding["code"] for finding in source["findings"]}


def test_default_scope_separates_actual_editions_and_keeps_registry_denominator(engine):
    from app.clhear.l1.registry_etoro import S
    entries = inv._declared_entries("registered")
    assert set(entries) >= {e["key"] for e in S} - {"finra/rulebook"}
    assert "iso/27001-2022" in entries and "iso/27001-2022-amd1-2024" in entries
    assert entries["iso/27001-2022-amd1-2024"]["relation"] == "amends"
    assert entries["aicpa/soc2-tsc"]["canonical_url"].endswith("2017-trust-services-criteria-with-revised-points-of-focus-2022")
    state = inv.inventory_summary(engine)
    assert state["known_expected"] >= len(S) - 1
    assert state["status"] == "not_run" and state["known_expected_is_lower_bound"]


def test_missing_permission_and_missing_document_remain_expected(small_scope):
    engine, store = small_scope
    result = inv.run_inventory_audit(engine, store, job_id="audit", scope="finra")
    assert result["known_expected"] == 1 and result["verified"] == 0 and result["unresolved"] == 1
    assert codes(result["sources"][0]) >= {"permission_unverified", "awaiting_artifact"}
    assert not result["full_scope_verified"] and not result["discovery_complete"]
    assert "scope_review_required" in codes(result)
    assert inv.acceptance_status(engine, "finra")["passed"] is False


def test_audit_and_scope_snapshots_are_append_only(small_scope):
    engine, store = small_scope
    first = inv.run_inventory_audit(engine, store, job_id="a", scope="finra")
    second = inv.run_inventory_audit(engine, store, job_id="b", scope="finra")
    assert first["audit_id"] != second["audit_id"]
    assert first["inventory_hash"] == second["inventory_hash"]
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(inv.inventory_audits)).scalar_one() == 2
        assert conn.execute(sa.select(sa.func.count()).select_from(inv.inventory_snapshots)).scalar_one() == 1
        stored = conn.execute(sa.select(inv.inventory_audits.c.summary).where(inv.inventory_audits.c.id == first["audit_id"])).scalar_one()
    assert stored == first


def test_exact_original_and_projection_verified_but_seeds_do_not_accept_scope(small_scope):
    engine, store = small_scope
    grant(engine)
    imported(engine, store)
    result = inv.run_inventory_audit(engine, store, job_id="audit", scope="finra")
    assert result["sources"][0]["verified"], result["sources"][0]["findings"]
    assert result["verified"] == 1 and not result["full_scope_verified"]
    encoded = str(result)
    assert "Test firms must keep test records" not in encoded
    assert "raw_text" not in encoded


def test_reviewed_exact_scope_can_accept_and_revocation_blocks(small_scope, monkeypatch):
    engine, store = small_scope
    grant(engine)
    imported(engine, store)
    result = review_and_audit(engine, store, monkeypatch)
    assert result["status"] == "verified", result
    assert inv.acceptance_status(engine, "finra")["passed"]
    inv.record_scope_review(engine, result["inventory_hash"], "test-only:withdrawn-index-review", "unit-test-reviewer", False)
    assert not inv.acceptance_status(engine, "finra")["passed"]


def test_artifact_corruption_detected_without_rewriting_history(small_scope):
    engine, store = small_scope
    grant(engine)
    version_id, key = imported(engine, store)
    store.put(key, b"original artifact was truncated", "text/html")
    result = inv.run_inventory_audit(engine, store, job_id="corruption-audit", scope="finra")
    assert "artifact_hash_mismatch" in codes(result["sources"][0])
    with engine.connect() as conn:
        assert conn.execute(sa.select(source_versions.c.status).where(source_versions.c.id == version_id)).scalar_one() == "in_force"


def test_clause_corruption_invalidates_acceptance_even_when_version_hash_unchanged(small_scope, monkeypatch):
    engine, store = small_scope
    grant(engine)
    imported(engine, store)
    review_and_audit(engine, store, monkeypatch)
    assert inv.acceptance_status(engine, "finra")["passed"]
    with engine.begin() as conn:
        conn.execute(clauses.update().values(text="modified test text"))
    assert "projection_changed:" + KEY in inv.acceptance_status(engine, "finra")["reasons"]
    result = inv.run_inventory_audit(engine, store, job_id="projection-audit", scope="finra")
    assert "clause_roundtrip_mismatch" in codes(result["sources"][0])


def test_wrong_current_version_and_duplicate_current_versions_are_explicit(small_scope):
    engine, store = small_scope
    grant(engine)
    imported(engine, store)
    inv.run_inventory_audit(engine, store, job_id="old", scope="finra")
    imported(engine, store, version_label="test-edition-2")
    summary = inv.inventory_summary(engine, "finra")
    assert not summary["current_binding_valid"] and summary["status"] == "stale"
    result = inv.run_inventory_audit(engine, store, job_id="new", scope="finra")
    assert "duplicate_current_versions" in codes(result["sources"][0])


def test_permission_revocation_blocks_readback_and_stales_old_report(small_scope):
    engine, store = small_scope
    grant(engine)
    imported(engine, store)
    inv.run_inventory_audit(engine, store, job_id="before", scope="finra")
    grant(engine, approved=False)
    assert not inv.inventory_summary(engine, "finra")["current_binding_valid"]
    class NoReadStore:
        def get(self, key):
            pytest.fail("Denied audit read original bytes")
    result = inv.run_inventory_audit(engine, NoReadStore(), job_id="after", scope="finra")
    assert result["sources"][0]["status"] == "permission_blocked"
    assert result["sources"][0]["artifacts"] == []


def test_legacy_unbound_manifest_and_unknown_publisher_checks_fail(small_scope):
    engine, store = small_scope
    grant(engine)
    imported(engine, store)
    with engine.begin() as conn:
        conn.execute(runs.update().values(outputs={"status": "added", "source_version_id": 999, "content_hash": "wrong"}))
    result = inv.run_inventory_audit(engine, store, job_id="legacy", scope="finra")
    assert codes(result["sources"][0]) >= {"artifact_manifest_unverified", "parser_provenance_unverified", "publisher_check_unverified"}


def test_discovery_requires_each_exact_permission_before_fetch(engine, tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "discovery")
    monkeypatch.setattr(inv, "_fetch_discovery", lambda url: pytest.fail("unauthorized discovery network fetch"))
    entries, result = inv._discover(engine, store)
    assert entries == {} and not result["complete"]
    assert all(f["code"] == "discovery_permission_blocked" for f in result["findings"])


def test_discovery_pagination_limits_and_attachment_gaps_are_preserved(engine, tmp_path, monkeypatch):
    index = "https://www.finra.org/rules-guidance/rulebooks/finra-rules"
    next_page = index + "?page=1"
    attachment = "https://www.finra.org/sites/default/files/test-publication.pdf"
    monkeypatch.setattr(inv, "FINRA_CATEGORIES", (("rules", "Rules", index),))
    monkeypatch.setenv("CLHEAR_L1_DISCOVERY_MAX_PAGES", "2")
    grant(engine, "finra/catalog/rules")
    grant(engine, inv._source_key(next_page))
    bodies = {index: f'<main><a href="{URL}">Rule</a><a href="{next_page}" rel="next">Next</a><a href="{attachment}">Attachment</a></main>'.encode(),
              next_page: b"<main>Next index page</main>"}
    fetched = []
    def fetch(url):
        fetched.append(url)
        return bodies[url], "live"
    monkeypatch.setattr(inv, "_fetch_discovery", fetch)
    entries, result = inv._discover(engine, LocalStore(tmp_path / "discovery"))
    assert KEY in entries and inv._source_key(attachment) in entries
    assert URL not in fetched  # new document has no permission yet
    assert not result["complete"] and "discovery_limit" in codes(result)
    assert "Test firms" not in str(result)


def test_failed_discovery_keeps_previously_expected_documents(small_scope, monkeypatch):
    engine, store = small_scope
    other = inv._discovered_entry(URL.replace("2210", "3110"), "rules")
    monkeypatch.setattr(inv, "_discover", lambda engine, store: ({other["key"]: other}, {
        "complete": False, "checked_at": None, "categories": [], "pages": [], "findings": []}))
    first = inv.run_inventory_audit(engine, store, job_id="discover-first", scope="finra", discover=True)
    monkeypatch.setattr(inv, "_discover", lambda engine, store: ({}, {
        "complete": False, "checked_at": None, "categories": [], "pages": [], "findings": [{"code": "discovery_failed", "detail": "test"}]}))
    second = inv.run_inventory_audit(engine, store, job_id="discover-failed", scope="finra", discover=True)
    assert first["known_expected"] == second["known_expected"] == 2
    assert other["key"] in {entry["key"] for entry in inv.planned_entries(engine)}


def test_scope_review_requires_existing_inventory_and_explicit_evidence(engine):
    with pytest.raises(ValueError, match="existing frozen"):
        inv.record_scope_review(engine, "0" * 64, "evidence", "reviewer", True)
    with pytest.raises(ValueError, match="reviewer"):
        inv.record_scope_review(engine, "0" * 64, "", "reviewer", True)


def test_private_standards_missing_artifacts_not_claimed_as_complete(engine, tmp_path, monkeypatch):
    from app.clhear.l1.registry_etoro import S
    standards = {e["key"]: dict(e) for e in S if e["key"].startswith(("iso/", "aicpa/", "pci/", "ifrs/"))}
    monkeypatch.setattr(inv, "_declared_entries", lambda scope: standards)
    result = inv.run_inventory_audit(engine, LocalStore(tmp_path), job_id="standards")
    assert result["known_expected"] == len(standards) and result["unresolved"] == len(standards)
    assert all(e["status"] == "permissions_unverified" and "awaiting_artifact" in codes(e) for e in result["sources"])
    assert {e["expected_edition"] for e in result["sources"] if e["source_key"].startswith("iso/")} >= {
        "ISO/IEC 27001:2022", "ISO/IEC 27001:2022/Amd 1:2024"}


def test_artifact_review_is_exact_hash_bound_and_grants_no_permission(small_scope):
    from app.clhear.l1 import permissions
    engine, store = small_scope
    digest = hashlib.sha256(BODY).hexdigest()
    row = inv.record_artifact_review(engine, KEY, digest, "test publisher edition", URL,
             evidence_ref="test-only:reviewed-edition", approved_by="test-reviewer", approved=True)
    assert row["coverage"] == "full" and row["content_hash"] == digest
    with engine.connect() as conn:
        assert not permissions.decision(conn, KEY, "parse")["allowed"]
        assert inv._artifact_review(conn, KEY, "0" * 64) is None
    grant(engine)
    imported(engine, store)
    inv.run_inventory_audit(engine, store, job_id="reviewed", scope="finra")
    inv.record_artifact_review(engine, KEY, digest, "test publisher edition", URL, coverage="preview",
             evidence_ref="test-only:preview-discovered", approved_by="test-reviewer", approved=True)
    assert not inv.inventory_summary(engine, "finra")["current_binding_valid"]
    result = inv.run_inventory_audit(engine, store, job_id="partial", scope="finra")
    assert "partial_artifact" in codes(result["sources"][0])


def test_artifact_review_rejects_implicit_or_invalid_approval(engine):
    with pytest.raises(ValueError, match="Explicit"):
        inv.record_artifact_review(engine, KEY, "0" * 64, "edition", URL)
    with pytest.raises(ValueError, match="HTTPS"):
        inv.record_artifact_review(engine, KEY, "0" * 64, "edition", "file:///tmp/input",
                                  evidence_ref="test:review", approved_by="test-reviewer", approved=True)


def test_registered_audit_inherits_finra_discovery_without_refetch(small_scope, monkeypatch):
    engine, store = small_scope
    extra = inv._discovered_entry(URL.replace("2210", "3110"), "rules")
    seen_at = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(inv, "_discover", lambda engine, store: ({extra["key"]: extra}, {
        "complete": False, "checked_at": seen_at, "categories": [], "pages": [], "findings": []}))
    inv.run_inventory_audit(engine, store, job_id="finra-discovery", scope="finra", discover=True)
    monkeypatch.setattr(inv, "_discover", lambda engine, store: pytest.fail("registered audit performed discovery"))
    result = inv.run_inventory_audit(engine, store, job_id="registered-audit", scope="registered")
    assert result["known_expected"] == 2 and result["discovery"]["checked_at"] == seen_at


def test_failed_or_old_publisher_check_cannot_be_refreshed_by_audit(small_scope, monkeypatch):
    engine, store = small_scope
    grant(engine)
    imported(engine, store)
    complete_discovery(monkeypatch)
    with engine.begin() as conn:
        row = conn.execute(sa.select(runs)).mappings().one()
        conn.execute(runs.update().where(runs.c.id == row["id"]).values(outputs={
            **row["outputs"], "publisher_checked_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}))
    result = review_and_audit(engine, store, monkeypatch)
    assert "publisher_check_overdue" in codes(result["sources"][0])
    assert not inv.acceptance_status(engine, "finra")["passed"]


def test_manifest_uri_cannot_read_outside_configured_store(small_scope):
    engine, store = small_scope
    grant(engine)
    imported(engine, store)
    with engine.begin() as conn:
        row = conn.execute(sa.select(runs)).mappings().one()
        output = row["outputs"]
        output["artifact_manifest"][0]["uri"] = "file:///etc/passwd"
        conn.execute(runs.update().where(runs.c.id == row["id"]).values(outputs=output))
    result = inv.run_inventory_audit(engine, store, job_id="wrong-store", scope="finra")
    assert "artifact_store_error" in codes(result["sources"][0])


def test_changed_declared_inventory_requires_new_scope_review(small_scope, monkeypatch):
    engine, store = small_scope
    grant(engine)
    imported(engine, store)
    previous = review_and_audit(engine, store, monkeypatch)
    extra = inv._discovered_entry(URL.replace("2210", "3110"), "rules")
    monkeypatch.setattr(inv, "_declared_entries", lambda scope: {KEY: dict(ENTRY), extra["key"]: extra})
    assert inv.inventory_summary(engine, "finra")["status"] == "stale"
    current = inv.run_inventory_audit(engine, store, job_id="scope-expanded", scope="finra")
    assert current["inventory_hash"] != previous["inventory_hash"]
    assert not current["full_scope_verified"] and "scope_review_required" in codes(current)
