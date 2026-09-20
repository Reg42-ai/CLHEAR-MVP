"""Independent scope gaps and exact stored artifact/projection reconciliation.

All prose below is original unit-test material, not a regulatory demo corpus.
"""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.clhear.l1 import inventory as inv
from app.clhear.l1.adapters.base import Artifact, SourceMeta
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


def imported(engine, store, *, body=BODY, version_label="test-edition-1", latest=True, content_hash_method=None):
    meta = SourceMeta(family_key="test-finra", family_name="Unit-test FINRA", source_key=KEY,
                      name="Original test rule", kind="regulation", issuer="FINRA", jurisdiction="US",
                      license="restricted", canonical_url=URL, adapter="finra")
    tree = parse(body, KEY, URL)
    from app.clhear.l1.originals import attach_source_locations
    assert attach_source_locations(KEY, "finra", [Artifact("original.html", body, "text/html")], tree, URL)
    digest = hashlib.sha256(body).hexdigest()
    from app.clhear.l1.pipeline import artifact_set_hash
    version_hash = artifact_set_hash([Artifact("original.html", body, "text/html")]) if content_hash_method == "artifact-set-v2" else digest
    key = f"restricted/{KEY}/{version_label}/original.html"
    uri = store.put(key, body, "text/html")
    with engine.begin() as conn:
        _, source_id = ensure_source(conn, meta)
        version_id = conn.execute(source_versions.insert().values(source_id=source_id, version_label=version_label,
                     content_hash=version_hash, s3_uri=uri, status="in_force" if latest else "superseded").returning(source_versions.c.id)).scalar_one()
        rows = persist_tree(conn, version_id, tree, public_ok=False)
        conn.execute(clauses.insert(), rows)
        conn.execute(runs.insert().values(fleet="l1.finra", trigger="test", inputs={"source": KEY}, outputs={
            "status": "added", "source_version_id": version_id, "content_hash": version_hash,
            **({"content_hash_method": content_hash_method} if content_hash_method else {}),
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
    # Acceptance now includes an authorized English reader, not import alone.
    from app.clhear.l1 import permissions
    with engine.connect() as conn:
        existing = {op: permissions.decision(conn, KEY, op)["allowed"] for op in permissions.OPERATIONS}
    grant(engine, permissions={**existing, "display_internal": True})
    complete_discovery(monkeypatch)
    first = inv.run_inventory_audit(engine, store, job_id="initial-discovery", scope="finra", discover=True)
    inv.record_scope_review(engine, first["inventory_hash"], "test-only:independent-complete-index", "unit-test-reviewer", True)
    result = inv.run_inventory_audit(engine, store, job_id="after-scope-review", scope="finra")
    from app.clhear.l1 import translation
    for source in result["sources"]:
        if source.get("source_version_id") and source["verified"]:
            with engine.begin() as conn:
                translation.record_language_binding(conn, source_version_id=source["source_version_id"],
                    language="en", document_key=source["source_key"], authority="authoritative",
                    evidence_ref="test-only:authored-English-fixture", approved_by="unit-test-reviewer")
            translation.build_english_view(engine, None, source["source_version_id"])
    return result


def codes(source):
    return {finding["code"] for finding in source["findings"]}


def test_default_scope_separates_actual_editions_and_keeps_registry_denominator(engine):
    from app.clhear.l1.source_registry import S, source_role
    entries = inv._declared_entries("registered")
    declared_documents = {e["key"] for e in S if source_role(e["key"]) == "document"}
    assert set(entries) >= declared_documents
    assert "iso/27001-2022" in entries and "iso/27001-2022-amd1-2024" in entries
    assert entries["iso/27001-2022-amd1-2024"]["relation"] == "amends"
    assert entries["aicpa/soc2-tsc"]["canonical_url"].endswith("2017-trust-services-criteria-with-revised-points-of-focus-2022")
    state = inv.inventory_summary(engine)
    assert state["known_expected"] >= len(declared_documents)
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
    # Simulate out-of-band storage damage; the worker store itself is immutable.
    (store.base_dir / key).write_bytes(b"original artifact was truncated")
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


def test_live_discovery_is_paced_and_waits_out_publisher_throttling(monkeypatch):
    """finra.org answered 429 to a burst of ~80 ms discovery requests on 19 Sep;
    discovery must pace like document fetches and obey Retry-After."""
    import httpx
    from app.clhear.l1 import http as l1_http
    monkeypatch.setenv("CLHEAR_HTTP_MODE", "live")
    monkeypatch.setattr(l1_http, "HOST_PACING_S", {"www.finra.org": 0.05})
    naps, calls = [], []
    monkeypatch.setattr(inv.time, "sleep", lambda s: naps.append(s))  # inventory and http share the time module

    class Stream:
        def __init__(self, status, body=b""):
            self.status_code, self._body, self.is_redirect = status, body, False
            self.headers = {"retry-after": "7"} if status == 429 else {}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(str(self.status_code), request=httpx.Request("GET", "https://www.finra.org/x"), response=httpx.Response(self.status_code))
        def iter_bytes(self): yield self._body

    responses = [Stream(429), Stream(200, b"<main><a href='/rules-guidance/rulebooks/finra-rules/3110'>3110</a></main>")]
    def stream(method, url, **kwargs):
        calls.append(url)
        return responses.pop(0)
    monkeypatch.setattr(httpx, "stream", stream)
    body, origin = inv._fetch_discovery("https://www.finra.org/rules-guidance/rulebooks/finra-rules")
    assert origin == "live" and b"3110" in body and len(calls) == 2
    assert 30.0 in naps  # a 429 waits at least 30 s (Retry-After 7 s is shorter)
    assert any(0 < n < 1 for n in naps)  # and the retry itself is host-paced
    # The waits are bounded so one page's discovery lease outlives them: the
    # 19 Sep full cycle died with "checkpoint lease lost" after 30+60+90 s.
    from app.clhear.l1 import discovery
    assert sum(inv.THROTTLE_WAITS_S) + 60 < discovery.PAGE_LEASE.total_seconds()
    naps.clear(); calls.clear()
    responses[:] = [Stream(429), Stream(429), Stream(429), Stream(429)]
    for r in responses:
        r.headers = {"retry-after": "900"}  # a publisher asking for 15 min is still capped
    with pytest.raises(httpx.HTTPStatusError):  # the last 429 surfaces; run_batch keeps the page pending
        inv._fetch_discovery("https://www.finra.org/rules-guidance/rulebooks/finra-rules")
    assert len(calls) == 4 and max(n for n in naps if n >= 1) <= max(inv.THROTTLE_WAITS_S)


def test_discovery_requires_each_exact_permission_before_fetch(engine, tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "discovery")
    monkeypatch.setattr(inv, "_fetch_discovery", lambda url: pytest.fail("unauthorized discovery network fetch"))
    entries, result = inv._discover(engine, store)
    assert entries == {} and not result["complete"]
    assert len([f for f in result["findings"] if f["code"] == "discovery_permission_blocked"]) == len(inv.finra_seed_categories())
    assert {key for key, _, _ in inv.finra_seed_categories()} == set(inv.FINRA_RULEBOOK_CATEGORIES)
    monkeypatch.setenv("CLHEAR_L1_FINRA_FULL_DISCOVERY", "true")
    assert inv.finra_seed_categories() == inv.FINRA_CATEGORIES
    assert any(f["code"] == "finra_enforcement_search_contract_required" for f in result["findings"])


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
    assert KEY in entries and inv._source_key(attachment) not in entries
    assert URL not in fetched  # a rule leaf is enumerated, not fetched
    assert attachment not in fetched  # off-book PDFs stay off the rulebook frontier
    assert not result["complete"] and "discovery_limit" in codes(result)
    assert "Test firms" not in str(result)


def test_throttled_seed_page_stays_pending_instead_of_planning_from_a_gap(engine, tmp_path, monkeypatch):
    """19 Sep: every rulebook seed 429'd, was marked failed, and the cycle planned
    with no FINRA Rules. A throttled catalog page must remain pending in the cycle."""
    import httpx
    from app.clhear.l1 import discovery
    index = "https://www.finra.org/rules-guidance/rulebooks/finra-rules"
    monkeypatch.setattr(inv, "FINRA_CATEGORIES", (("rules", "Rules", index),))
    monkeypatch.setenv("CLHEAR_L1_DISCOVERY_MAX_PAGES", "1")
    grant(engine, "finra/catalog/rules")
    def throttled(url):
        response = httpx.Response(429, request=httpx.Request("GET", url))
        raise httpx.HTTPStatusError("429", request=response.request, response=response)
    monkeypatch.setattr(inv, "_fetch_discovery", throttled)
    _, result = inv._discover(engine, LocalStore(tmp_path / "discovery"))
    assert result["pending_pages"] == 1 and "discovery_throttled" in codes(result) and not result["complete"]
    with engine.connect() as conn:
        row = conn.execute(sa.select(discovery.pages.c.status, discovery.pages.c.attempts)).one()
    assert (row.status, row.attempts) == ("pending", 1)
    monkeypatch.setattr(inv, "_fetch_discovery", lambda url: (_ for _ in ()).throw(ValueError("not a catalog")))
    _, result = inv._discover(engine, LocalStore(tmp_path / "discovery"))
    assert result["pending_pages"] == 0 and "discovery_failed" in codes(result)  # a real failure still fails


def test_rulebook_discovery_ignores_notice_links_and_settles_leftover_frontier(engine, tmp_path, monkeypatch):
    """19 Sep: the same-day frontier already held pending rule leaves and notices.
    Discovery fetched both, 429'd, and the queued rulebook cycle never started."""
    from app.clhear.l1 import discovery
    index = "https://www.finra.org/rules-guidance/rulebooks/finra-rules"
    notice = "https://www.finra.org/rules-guidance/notices/00-10"
    leftover = index + "/7630"
    monkeypatch.setattr(inv, "FINRA_CATEGORIES", (("rules", "Rules", index),))
    grant(engine, "finra/catalog/rules")
    grant(engine, KEY)
    grant(engine, "finra/rule/7630")
    fetched = []
    def fetch(url):
        fetched.append(url)
        return (f'<main><a href="{URL}">2210</a><a href="{notice}">Notice</a></main>'.encode(), "live")
    monkeypatch.setattr(inv, "_fetch_discovery", fetch)
    entries, first = inv._discover(engine, LocalStore(tmp_path / "discovery"))
    assert fetched == [index] and KEY in entries
    assert inv._source_key(notice) not in entries
    with engine.begin() as conn:
        cycle_id = conn.execute(sa.select(discovery.cycles.c.id)).scalar_one()
        conn.execute(discovery.pages.insert(), [
            discovery._page(cycle_id, notice, inv._source_key(notice), "notices", "document"),
            discovery._page(cycle_id, leftover, "finra/rule/7630", "rules", "document"),
        ])
    entries, second = inv._discover(engine, LocalStore(tmp_path / "discovery"))
    assert fetched == [index]
    assert second["pending_pages"] == 0 and KEY in entries
    with engine.connect() as conn:
        rows = {row["url"]: row for row in conn.execute(sa.select(discovery.pages)).mappings()}
    assert rows[notice]["status"] == "checked" and rows[notice]["result"]["out_of_scope"] is True
    assert rows[leftover]["status"] == "checked" and rows[leftover]["result"]["terminal"] is True
    assert rows[leftover]["attempts"] == 0


def test_source_key_keeps_lettered_and_drupal_alias_finra_rules():
    """20 Sep live index: 664 finra-rules child paths, but only 606 matched
    \\d{4,5}[A-Z]?. Lettered TRF/ADF series and 12407-0 aliases were hashed."""
    assert inv._source_key(URL) == KEY
    assert inv._source_key("https://www.finra.org/rules-guidance/rulebooks/finra-rules/6300a") == "finra/rule/6300A"
    assert inv._source_key("https://www.finra.org/rules-guidance/rulebooks/finra-rules/6340B") == "finra/rule/6340B"
    assert inv._source_key("https://www.finra.org/rules-guidance/rulebooks/finra-rules/12407-0") == "finra/rule/12407"
    assert inv._source_key("https://www.finra.org/rules-guidance/rulebooks/finra-rules/part-iv").startswith("finra/document/")
    nyse = inv._discovered_entry(
        "https://www.finra.org/rules-guidance/rulebooks/incorporated-nyse-rules/rule-409", "nyse_archive")
    assert nyse["key"] == "finra/nyse/409" and inv.rulebook_import(nyse)
    assert inv._source_key(
        "https://www.finra.org/rules-guidance/rulebooks/incorporated-nyse-rules/rule-299c") == "finra/nyse/299C"
    assert inv.official_nyse_leaf(
        "https://www.finra.org/rules-guidance/rulebooks/incorporated-nyse-rules/rule-1")


def test_rulebook_index_enumerates_lettered_rules_without_fetching_them(engine, tmp_path, monkeypatch):
    index = "https://www.finra.org/rules-guidance/rulebooks/finra-rules"
    lettered = index + "/6300a"
    alias = index + "/12407-0"
    monkeypatch.setattr(inv, "FINRA_CATEGORIES", (("rules", "Rules", index),))
    grant(engine, "finra/catalog/rules")
    grant(engine, "finra/rule/6300A")
    grant(engine, "finra/rule/12407")
    fetched = []
    def fetch(url):
        fetched.append(url)
        return (f'<main><a href="{lettered}">6300A</a><a href="{alias}">12407</a></main>'.encode(), "live")
    monkeypatch.setattr(inv, "_fetch_discovery", fetch)
    entries, result = inv._discover(engine, LocalStore(tmp_path / "discovery"))
    assert fetched == [index]
    assert "finra/rule/6300A" in entries and "finra/rule/12407" in entries
    assert lettered not in fetched and alias not in fetched
    assert result["pending_pages"] == 0


def test_official_nyse_titles_name_published_rule_paths():
    from app.clhear.l1.finra_catalog import official_nyse_leaf_urls, official_nyse_rule_numbers
    from bs4 import BeautifulSoup
    numbers = official_nyse_rule_numbers("Dealings and Settlements (Rules 45–299C)")
    assert numbers[0] == "45" and "299" in numbers and "299C" in numbers
    assert len(numbers) == 256  # 45..299 plus 299C
    assert official_nyse_rule_numbers("Rules 1–10000") == []  # unbounded range is not enumerated
    soup = BeautifulSoup(
        "<main><h1>Incorporated NYSE Rules</h1>"
        "<a href='/rules-guidance/rulebooks/incorporated-nyse-rules-1'>Definitions (Rules 1–2)</a>"
        "<a href='/rules-guidance/rulebooks/incorporated-nyse-rules/rule-409'>Rule 409. Statements</a>"
        "</main>", "html.parser")
    urls = official_nyse_leaf_urls(soup)
    assert "https://www.finra.org/rules-guidance/rulebooks/incorporated-nyse-rules/rule-1" in urls
    assert "https://www.finra.org/rules-guidance/rulebooks/incorporated-nyse-rules/rule-2" in urls
    assert "https://www.finra.org/rules-guidance/rulebooks/incorporated-nyse-rules/rule-409" in urls


def test_nyse_index_enumerates_official_rule_paths_without_fetching_them(engine, tmp_path, monkeypatch):
    index = "https://www.finra.org/rules-guidance/rulebooks/incorporated-nyse-rules"
    monkeypatch.setattr(inv, "FINRA_CATEGORIES", (("nyse_archive", "NYSE", index),))
    grant(engine, "finra/catalog/nyse_archive")
    grant(engine, "finra/nyse/1")
    grant(engine, "finra/nyse/2")
    grant(engine, "finra/nyse/409")
    fetched = []
    html = (
        "<main><h1>Incorporated NYSE Rules</h1>"
        "<a href='/rules-guidance/rulebooks/incorporated-nyse-rules-1'>Definitions (Rules 1–2)</a>"
        "<a href='/rules-guidance/rulebooks/incorporated-nyse-rules/rule-409'>Rule 409. Statements</a>"
        "</main>"
    ).encode()
    def fetch(url):
        fetched.append(url)
        return (html, "live")
    monkeypatch.setattr(inv, "_fetch_discovery", fetch)
    entries, result = inv._discover(engine, LocalStore(tmp_path / "discovery"))
    assert {"finra/nyse/1", "finra/nyse/2", "finra/nyse/409"} <= set(entries)
    assert all("/rule-" not in url for url in fetched)
    assert result["pending_pages"] == 0


def test_finra_series_heading_ingest_is_catalog_page_residue(engine, tmp_path, monkeypatch):
    from app.clhear.l1.adapters.sec_edgar import SecEdgarAdapter
    from app.clhear.l1.pipeline import ingest
    url = "https://www.finra.org/rules-guidance/rulebooks/finra-rules/11300"
    grant(engine, "finra/rule/11300")
    html = (
        b'<html><body><div id="the-rule"><h1>11300. DELIVERY OF SECURITIES</h1>'
        b'<div class="book-navigation"><ul>'
        b'<li><a href="/rules-guidance/rulebooks/finra-rules/11310">11310</a></li>'
        b'<li><a href="/rules-guidance/rulebooks/finra-rules/11320">11320</a></li>'
        b'</ul></div></div></body></html>'
    )
    adapter = SecEdgarAdapter(channel="finra", source_key="finra/rule/11300",
                              title="FINRA 11300", url=url)
    adapter.key = "finra"
    monkeypatch.setattr(adapter, "fetch_bytes", lambda: [("page.html", html)])
    summary = ingest(engine, adapter, LocalStore(tmp_path / "originals"), index_embeddings=False)
    assert summary["status"] == "catalog-page"


def test_rulebook_collection_landings_are_not_imported():
    expanded = {"key": "finra/document/expandedhash",
                "canonical_url": "https://www.finra.org/rules-guidance/rulebooks/finra-rules-expanded"}
    pending = {"key": "finra/document/pendinghash",
               "canonical_url": "https://www.finra.org/rules-guidance/rulebooks/immediately-effective-rule-changes-pending-sec-notification"}
    agreements = {"key": "finra/document/trfhash",
                  "canonical_url": "https://www.finra.org/rules-guidance/rulebooks/corporate-organization/trf-llc-agreements"}
    article = {"key": "finra/document/bylawhash",
               "canonical_url": "https://www.finra.org/rules-guidance/rulebooks/corporate-organization/article-iv-board-directors"}
    assert not inv.rulebook_import(expanded)
    assert not inv.rulebook_import(pending)
    assert not inv.rulebook_import(agreements)
    assert inv.rulebook_import(article)
    assert inv.rulebook_collection_url(expanded["canonical_url"])
    assert not inv.rulebook_collection_url(article["canonical_url"])


def test_unpublished_official_nyse_leaf_is_listed_residue(engine, tmp_path, monkeypatch):
    import httpx
    from app.clhear.l1.adapters.finra_document import FinraDocumentAdapter
    url = "https://www.finra.org/rules-guidance/rulebooks/incorporated-nyse-rules/rule-46"
    grant(engine, "finra/nyse/46")
    adapter = FinraDocumentAdapter("finra/nyse/46", "Incorporated NYSE Rule 46", url)
    def boom(_since=None):
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
    monkeypatch.setattr(adapter, "fetch", boom)
    from app.clhear.l1.pipeline import ingest
    summary = ingest(engine, adapter, LocalStore(tmp_path / "originals"), index_embeddings=False)
    assert summary["status"] == "not-published"


def test_finra_audit_drops_leaked_notice_documents_from_the_rulebook_plan(engine, tmp_path, monkeypatch):
    engine, store = engine, LocalStore(tmp_path / "originals")
    notice = inv._discovered_entry("https://www.finra.org/rules-guidance/notices/00-10", "notices")
    extra = inv._discovered_entry(URL.replace("2210", "9999"), "rules")
    monkeypatch.setattr(inv, "_discover", lambda engine, store: ({notice["key"]: notice, extra["key"]: extra}, {
        "complete": False, "checked_at": None, "categories": [], "pages": [], "findings": []}))
    first = inv.run_inventory_audit(engine, store, job_id="leaked-notices", scope="finra", discover=True)
    assert extra["key"] in {e["source_key"] for e in first["sources"]}
    assert notice["key"] not in {e["source_key"] for e in first["sources"]}
    assert extra["key"] in {e["key"] for e in inv.planned_entries(engine, scope="finra")}
    assert notice["key"] not in {e["key"] for e in inv.planned_entries(engine, scope="finra")}


def test_registered_audit_drops_leaked_finra_notices_from_the_import_plan(engine, tmp_path, monkeypatch):
    """20 Sep nightly: all_publishers reused the 19 Sep snapshot and imported
    leftover finra/document notices, 429'd 4511, and never reached GovInfo/NIST."""
    notice = inv._discovered_entry("https://www.finra.org/rules-guidance/notices/07-57", "notices")
    filing = inv._discovered_entry("https://www.finra.org/rules-guidance/rule-filings/sr-finra-2016-043", "filings")
    extra = inv._discovered_entry(URL.replace("2210", "9999"), "rules")
    bylaw = inv._discovered_entry(
        "https://www.finra.org/rules-guidance/rulebooks/corporate-organization/article-iv-board-directors",
        "governing")
    cab = inv._discovered_entry(
        "https://www.finra.org/rules-guidance/rulebooks/capital-acquisition-broker-rules/121",
        "cab_rules")
    funding = inv._discovered_entry(
        "https://www.finra.org/rules-guidance/rulebooks/funding-portal-rules/100",
        "funding_portal_rules")
    nyse = inv._discovered_entry(
        "https://www.finra.org/rules-guidance/rulebooks/incorporated-nyse-rules/rule-409",
        "nyse_archive")
    report = {"complete": False, "checked_at": None, "categories": [], "pages": [], "findings": []}
    monkeypatch.setattr(inv, "_discover_publishers", lambda engine, store, job_id: (
        {row["key"]: row for row in (notice, filing, extra, bylaw, cab, funding, nyse)}, report))
    audit = inv.run_inventory_audit(engine, LocalStore(tmp_path / "originals"),
                                    job_id="registered-leaked-notices", scope="registered", discover=True)
    keys = {e["source_key"] for e in audit["sources"]}
    assert extra["key"] in keys
    assert notice["key"] not in keys and filing["key"] not in keys
    assert {bylaw["key"], cab["key"], funding["key"], nyse["key"]} <= keys
    planned = {e["key"] for e in inv.planned_entries(engine, scope="registered")}
    assert extra["key"] in planned
    assert notice["key"] not in planned and filing["key"] not in planned
    assert {bylaw["key"], cab["key"], funding["key"], nyse["key"]} <= planned
    assert notice["key"] not in {e["key"] for e in inv.planned_entries(engine, scope="all_publishers")}
    assert all(inv.rulebook_import(row) for row in (extra, bylaw, cab, funding, nyse))
    assert not inv.rulebook_import(notice) and not inv.rulebook_import(filing)
    # Cycle stubs use finra/<lane>/doc, not leftover document hashes.
    assert inv.rulebook_import({"key": "finra/doc"})


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


def test_legacy_snapshot_missing_location_column_remains_readable_but_unverified(small_scope):
    engine, store = small_scope
    grant(engine)
    imported(engine, store)
    with engine.begin() as conn:
        conn.exec_driver_sql('ALTER TABLE doc_nodes DROP COLUMN source_locator')
    result = inv.run_inventory_audit(engine, store, job_id='legacy-location-evidence', scope='finra')
    assert result['sources'][0]['verified'] is False
    assert 'source_locator_unverified' in codes(result['sources'][0])


def test_registered_key_with_test_issuer_cannot_supply_publisher_evidence(small_scope):
    from app.clhear.l1.models import sources
    engine, store = small_scope
    grant(engine)
    imported(engine, store)
    with engine.begin() as conn:
        conn.execute(sources.update().where(sources.c.key == KEY).values(issuer='Test fixture'))
    class NoReadStore:
        def get(self, key):
            pytest.fail('A fixture original cannot be read as publisher evidence')
    result = inv.run_inventory_audit(engine, NoReadStore(), job_id='fixture-origin', scope='finra')
    assert result['known_expected'] == 1 and not result['sources'][0]['verified']
    assert 'test_origin_not_publisher_evidence' in codes(result['sources'][0])


@pytest.mark.parametrize('method,observed', [(None, 'legacy-unframed-single-v1'), ('artifact-set-v2', 'artifact-set-v2')])
def test_artifact_hash_methods_are_explicit_and_exact(small_scope, method, observed):
    engine, store = small_scope
    grant(engine)
    imported(engine, store, content_hash_method=method)
    result = inv.run_inventory_audit(engine, store, job_id='hash-method-audit', scope='finra')
    source = result['sources'][0]
    assert source['verified'], source['findings']
    assert source['artifact_set_hash_evidence']['method'] == observed
    assert source['artifact_set_hash_evidence']['declared_method'] == method
    assert source['artifact_set_hash_evidence']['verified'] is True


def test_declared_framed_method_never_falls_back_to_legacy(small_scope):
    engine, store = small_scope
    grant(engine)
    imported(engine, store)  # historical raw single-file digest
    with engine.begin() as conn:
        row = conn.execute(sa.select(runs.c.id, runs.c.outputs)).mappings().one()
        conn.execute(runs.update().where(runs.c.id == row['id']).values(outputs={**row['outputs'], 'content_hash_method': 'artifact-set-v2'}))
    result = inv.run_inventory_audit(engine, store, job_id='misdeclared-hash', scope='finra')
    assert not result['sources'][0]['verified']
    assert 'artifact_composite_mismatch' in codes(result['sources'][0])


def test_legacy_multipart_manifest_is_readable_but_requires_framed_reingestion(small_scope):
    engine, store = small_scope
    grant(engine)
    version_id, _ = imported(engine, store)
    second = BODY.replace(b'test records', b'additional test records')
    artifact_key = f'restricted/{KEY}/test-edition-1/second.html'
    uri = store.put(artifact_key, second, 'text/html')
    combined = hashlib.sha256(BODY + second).hexdigest()
    second_hash = hashlib.sha256(second).hexdigest()
    with engine.begin() as conn:
        row = conn.execute(sa.select(runs.c.id, runs.c.outputs)).mappings().one()
        output = dict(row['outputs'])
        output['content_hash'] = combined
        output['artifact_manifest'] = [*output['artifact_manifest'], {
            'name': 'second.html', 'key': artifact_key, 'uri': uri, 'sha256': second_hash,
            'byte_count': len(second), 'content_type': 'text/html'}]
        output['fetch_evidence'] = [*output['fetch_evidence'], {'url': URL, 'origin': 'live', 'sha256': second_hash}]
        conn.execute(runs.update().where(runs.c.id == row['id']).values(outputs=output))
        conn.execute(source_versions.update().where(source_versions.c.id == version_id).values(content_hash=combined))
    result = inv.run_inventory_audit(engine, store, job_id='legacy-multipart', scope='finra')
    source = result['sources'][0]
    assert all(part['verified'] for part in source['artifacts'])
    assert source['artifact_set_hash_evidence']['method'] == 'legacy-unframed-multipart-v1'
    assert source['artifact_set_hash_evidence']['verified'] is False
    assert 'legacy_artifact_set_unframed' in codes(source)
    assert source['source_version_id'] == version_id


def test_read_time_test_origin_change_invalidates_previous_passing_audit(small_scope, monkeypatch):
    from app.clhear.l1.models import sources
    engine, store = small_scope
    grant(engine)
    imported(engine, store)
    accepted = review_and_audit(engine, store, monkeypatch)
    assert accepted['status'] == 'verified'
    with engine.begin() as conn:
        conn.execute(sources.update().where(sources.c.key == KEY).values(issuer='Test fixture'))
    summary = inv.inventory_summary(engine, 'finra')
    assert summary['status'] == 'stale' and not summary['current_binding_valid']
    assert not inv.source_inventory_evidence(engine, KEY)['verified']
    assert not inv.acceptance_status(engine, 'finra')['passed']


@pytest.mark.parametrize('change', ['scope_review_revoked', 'version_changed'])
def test_invalidated_scope_never_retains_a_certified_total(small_scope, monkeypatch, change):
    engine, store = small_scope
    grant(engine)
    imported(engine, store)
    accepted = review_and_audit(engine, store, monkeypatch)
    assert accepted['denominator_known'] and accepted['expected_total'] == 1
    if change == 'scope_review_revoked':
        inv.record_scope_review(engine, accepted['inventory_hash'], 'test-only:review-withdrawn', 'unit-test-reviewer', False)
    else:
        imported(engine, store, version_label='test-changed-version')
    result = inv.inventory_summary(engine, 'finra')
    assert not result['full_scope_verified'] and result['known_expected_is_lower_bound']
    assert result['expected_total'] is None and result['denominator_known'] is False
