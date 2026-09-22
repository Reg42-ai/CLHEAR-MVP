"""Synthetic catalog fixtures exercise worker code without operational imports."""
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.clhear.l1 import inventory, discovery, publishers, source_registry
from app.clhear.l1.pipeline import LocalStore
from app.clhear.l1.permissions import record_permission

INDEX = "https://www.finra.org/rules-guidance/rulebooks/finra-rules"
NEXT = INDEX + "?page=1"
RULE = INDEX + "/2210"
PDF = "https://www.finra.org/sites/default/files/test-only.pdf"


def grant(engine, key, approved=True):
    return record_permission(engine, source_key=key, permissions={"acquire": True, "store": True, "parse": True},
                             approved=approved, evidence_ref="test:catalog-contract", approved_by="test-reviewer")


def setup_catalog(monkeypatch):
    monkeypatch.setattr(inventory, "FINRA_CATEGORIES", (("rules", "Rules", INDEX),))


def test_profiles_cover_each_declared_source_and_separate_company_scope():
    assert all(publishers.publisher_ids(e) for e in source_registry.S)
    profiles = publishers.publisher_profiles()
    assert len({p["publisher_id"] for p in profiles}) == len(profiles)
    assert all(p["expected_documents"] is None and not p["denominator_known"] for p in profiles)
    iso = next(p for p in profiles if p["publisher_id"] == "iso")
    assert "unrelated industry standards" in iso["boundaries"]["exclude"]
    assert iso["boundaries"]["organization_filter"] is None
    assert iso["artifact_acquisition"] == "reviewed_authorized_artifact"


def test_collections_and_false_national_bundles_cannot_be_imported():
    from app.clhear.l1.fleet import adapter_for
    expected = inventory._declared_entries("registered")
    for entry in source_registry.S:
        if source_registry.source_role(entry["key"]) == "document":
            continue
        assert entry["key"] not in expected
        adapter = adapter_for(entry)
        assert adapter.declaration_gap["code"]
        with pytest.raises(ValueError):
            adapter.fetch()
    assert all(not e["canonical_url"] and "url" not in e["fetch"] for e in source_registry.S if e["key"].startswith("ovl/"))


def test_same_catalog_pagination_preserves_catalog_permission_not_document_grant(engine, tmp_path, monkeypatch):
    setup_catalog(monkeypatch)
    grant(engine, "finra/catalog/rules")
    fetched = []
    bodies = {INDEX: f'<main><a href="{NEXT}" rel="next">Next</a><a href="{RULE}">Rule</a></main>'.encode(),
              NEXT: f'<main><a href="{PDF}">PDF</a></main>'.encode()}
    def fetch(url):
        fetched.append(url)
        assert url in bodies  # neither document has a grant
        return bodies[url], "live"
    monkeypatch.setattr(inventory, "_fetch_discovery", fetch)
    entries, report = inventory._discover(engine, LocalStore(tmp_path))
    assert set(fetched) == {INDEX, NEXT}
    assert "finra/rule/2210" in entries and inventory._source_key(PDF) not in entries
    assert not report["complete"]
    assert all(f.get("source_key") != inventory._source_key(NEXT) for f in report["findings"])


def test_interrupted_batch_resumes_and_does_not_refetch_checked_catalog(engine, tmp_path, monkeypatch):
    setup_catalog(monkeypatch)
    grant(engine, "finra/catalog/rules")
    monkeypatch.setenv("CLHEAR_L1_DISCOVERY_MAX_PAGES", "1")
    calls = []
    def fetch(url):
        calls.append(url)
        return (f'<main><a href="{NEXT}" rel="next">Next</a></main>'.encode() if url == INDEX else f'<main><a href="{RULE}">Rule</a></main>'.encode()), "live"
    monkeypatch.setattr(inventory, "_fetch_discovery", fetch)
    first_entries, first = inventory._discover(engine, LocalStore(tmp_path))
    assert first["pending_pages"] == 1
    second_entries, second = inventory._discover(engine, LocalStore(tmp_path))
    assert first["cycle_id"] == second["cycle_id"]
    assert calls == [INDEX, NEXT]
    assert "finra/rule/2210" in second_entries
    assert not second["complete"]


def test_permission_revocation_during_fetch_prevents_artifact_persistence(engine, tmp_path, monkeypatch):
    setup_catalog(monkeypatch)
    grant(engine, "finra/catalog/rules")
    def fetch(url):
        grant(engine, "finra/catalog/rules", False)
        return b"<main>Test</main>", "live"
    class DeniedStore:
        def put(self, *args):
            pytest.fail("Revoked content persisted")
    monkeypatch.setattr(inventory, "_fetch_discovery", fetch)
    _, report = inventory._discover(engine, DeniedStore())
    assert report["pages"] == []
    assert report["findings"][0]["code"] == "discovery_permission_changed"


def test_claim_recovery_is_token_fenced(engine):
    with engine.begin() as conn:
        conn.execute(discovery.cycles.insert().values(id="cycle", publisher_id="finra", profile_hash="hash", cycle_date="2026-09-16"))
        conn.execute(discovery.pages.insert().values(**discovery._page("cycle", INDEX, "finra/catalog/rules", "rules", "collection")))
    first = discovery._claim(engine, "cycle", "one")
    assert discovery._claim(engine, "cycle", "two") is None
    with engine.begin() as conn:
        conn.execute(discovery.pages.update().values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
    second = discovery._claim(engine, "cycle", "two")
    assert first["lease_token"] != second["lease_token"]
    with engine.begin() as conn:
        stale = conn.execute(discovery.pages.update().where(discovery.pages.c.id == first["id"], discovery.pages.c.lease_token == first["lease_token"]).values(status="checked"))
        assert not stale.rowcount


def test_full_publisher_scope_does_not_certify_seed_inventory(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "_fetch_discovery", lambda url: pytest.fail("No catalog acquisition without grants"))
    result = inventory.run_inventory_audit(engine, LocalStore(tmp_path), job_id="all-publishers", scope="all_publishers", discover=True)
    assert result["scope"] == "registered"
    assert result["expected_total"] is None and not result["denominator_known"]
    assert not result["full_scope_verified"]
    assert {f.get("publisher_id") for f in result["findings"] if f["code"].endswith("_catalog_reconciliation_required")} >= {"iso", "sec", "fca"}
    assert inventory.inventory_summary(engine, "registered")["inventory_hash"] == result["inventory_hash"]


def test_metadata_reconciliation_retains_versions_and_removes_partner_charter(engine):
    from app.clhear.l1.models import sources, source_families, source_versions
    source_registry.seed(engine)
    with engine.begin() as conn:
        row = conn.execute(sa.select(sources).where(sources.c.key == "finra/rulebook")).mappings().one()
        version_id = conn.execute(source_versions.insert().values(source_id=row["id"], version_label="historical-index",
                   content_hash="0" * 64, status="in_force", s3_uri="test-only:retained").returning(source_versions.c.id)).scalar_one()
        conn.execute(sources.update().where(sources.c.id == row["id"]).values(name="Old bundle", about="eToro blueprint"))
        conn.execute(source_families.update().values(scope_charter={"partner": "etoro"}))
    source_registry.seed(engine)
    with engine.connect() as conn:
        same = conn.execute(sa.select(source_versions).where(source_versions.c.id == version_id)).mappings().one()
        corrected = conn.execute(sa.select(sources).where(sources.c.id == row["id"])).mappings().one()
        charters = conn.execute(sa.select(source_families.c.scope_charter)).scalars().all()
    assert same["content_hash"] == "0" * 64 and same["status"] == "in_force"
    assert "collection" in corrected["name"] and "eToro" not in corrected["about"]
    # Other starter families may have independent charters; the registry's
    # reconciled families must never carry the old partner restriction.
    assert all(c.get("partner") != "etoro" for c in charters)


def test_known_official_pdf_library_enumerates_real_entries_but_siblings_remain_gaps(engine, tmp_path):
    from app.clhear.l1.catalogs import discover_catalog
    profile = next(p for p in publishers.publisher_profiles() if p["publisher_id"] == "nydfs")
    index = next(e for e in source_registry.S if e["key"] == "nydfs/part200-500")["canonical_url"]
    pdf = "https://www.dfs.ny.gov/test-only-fixtures/regulation.pdf"
    grant(engine, "nydfs/catalog/regulations")
    calls = []
    def fetch(url):
        calls.append(url)
        assert url == index
        return f'<main><a href="{pdf}">Regulation</a><a href="https://unreviewed.example/other.pdf">Other</a></main>'.encode(), "live"
    entries, result = discover_catalog(engine, LocalStore(tmp_path), profile, job_id="library", fetcher=fetch)
    assert len(entries) == 1 and calls == [index]
    entry = next(iter(entries.values()))
    assert entry["canonical_url"] == pdf and entry["source_role"] == "document"
    assert "blocked" not in entry["fetch"]
    assert not result["complete"] and not result["denominator_known"]
    assert {f["code"] for f in result["findings"]} >= {"nydfs_catalog_reconciliation_required"}


def test_checkpoint_read_does_not_refresh_publisher_time(engine, tmp_path, monkeypatch):
    setup_catalog(monkeypatch)
    grant(engine, "finra/catalog/rules")
    monkeypatch.setattr(inventory, "_fetch_discovery", lambda url: (f'<main><a href="{RULE}">Rule</a></main>'.encode(), "live"))
    _, first = inventory._discover(engine, LocalStore(tmp_path))
    _, second = discovery.read_cycle(engine, first["cycle_id"])
    assert first["checked_at"] == second["checked_at"]
    assert first["last_attempt_at"] == second["last_attempt_at"]


def test_expired_owner_cannot_publish_discovery_checkpoint(engine, tmp_path, monkeypatch):
    setup_catalog(monkeypatch)
    grant(engine, "finra/catalog/rules")
    def fetch(url):
        with engine.begin() as conn:
            conn.execute(discovery.pages.update().where(discovery.pages.c.status == "leased").values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        return b"<main>Fixture catalog</main>", "live"
    monkeypatch.setattr(inventory, "_fetch_discovery", fetch)
    with pytest.raises(RuntimeError, match="lease lost"):
        inventory._discover(engine, LocalStore(tmp_path))
    with engine.connect() as conn:
        assert conn.execute(sa.select(discovery.pages.c.status)).scalar_one() == "leased"


def test_frozen_plan_uses_exact_audit_instead_of_newer_other_cycle(engine, tmp_path, monkeypatch):
    one = inventory._discovered_entry(RULE, "rules")
    two = inventory._discovered_entry(RULE.replace("2210", "3110"), "rules")
    monkeypatch.setattr(inventory, "_declared_entries", lambda scope: {})
    monkeypatch.setattr(inventory, "_discover", lambda engine, store: ({one["key"]: one}, {"complete": False, "checked_at": None, "categories": [], "pages": [], "findings": []}))
    first = inventory.run_inventory_audit(engine, LocalStore(tmp_path), scope="finra", job_id="first", discover=True)
    monkeypatch.setattr(inventory, "_discover", lambda engine, store: ({two["key"]: two}, {"complete": False, "checked_at": None, "categories": [], "pages": [], "findings": []}))
    inventory.run_inventory_audit(engine, LocalStore(tmp_path), scope="finra", job_id="second", discover=True)
    assert [e["key"] for e in inventory.planned_entries(engine, "finra", audit_id=first["audit_id"])] == [one["key"]]
    assert len(inventory.planned_entries(engine, "finra")) == 2
    with pytest.raises(ValueError, match="another scope"):
        inventory.planned_entries(engine, "registered", audit_id=first["audit_id"])


def test_cycle_date_binding_preserves_frontier_across_midnight(engine, tmp_path, monkeypatch):
    setup_catalog(monkeypatch)
    grant(engine, "finra/catalog/rules")
    monkeypatch.setenv("CLHEAR_L1_DISCOVERY_MAX_PAGES", "1")
    monkeypatch.setattr(inventory, "_fetch_discovery", lambda url: (f'<main><a href="{NEXT}" rel="next">Next</a></main>'.encode(), "live"))
    with discovery.bind_cycle_date("2026-09-15"):
        _, first = inventory._discover(engine, LocalStore(tmp_path))
    with discovery.bind_cycle_date("2026-09-15"):
        _, second = inventory._discover(engine, LocalStore(tmp_path))
    assert first["cycle_id"] == second["cycle_id"]
    assert second["cycle_date"] == "2026-09-15"
    with discovery.bind_cycle_date("2026-09-16"):
        _, other = inventory._discover(engine, LocalStore(tmp_path))
    assert other["cycle_id"] != first["cycle_id"]


def test_official_finra_attachment_host_and_nonrule_parser_dispatch(monkeypatch):
    from app.clhear.l1 import http
    from app.clhear.l1.fleet import adapter_for
    attachment = 'https://files.finra.org/test-only-fixtures/notice.pdf'
    assert inventory._url(attachment) == attachment
    assert inventory._in_scope_url(attachment, attachment=True)
    assert inventory._url('https://files.finra.org.evil.example/notice.pdf') is None
    assert inventory._url('https://files.finra.org/%2e%2e/notice.pdf') is None
    entry = inventory._discovered_entry('https://www.finra.org/rules-guidance/notices/test-only', 'notices')
    adapter = adapter_for(entry)
    assert type(adapter).__name__ == 'FinraDocumentAdapter'
    body = b'<html><h1>Authored fixture notice</h1><article><div class="field--name-body"><p>This authored paragraph exercises publication parsing without any regulatory source text.</p></div></article></html>'
    monkeypatch.setattr(http, 'get', lambda url: body)
    result = adapter.fetch()
    assert result.artifacts[0].content == body
    assert 'authored paragraph' in result.tree[0].subtree_text()
    monkeypatch.setattr(http, 'get', lambda url: b'<html><h1>Catalog</h1><main><a href="/rule">Only a link</a></main></html>')
    with pytest.raises(ValueError, match='official article body'):
        adapter.fetch()


def test_source_descriptions_identify_publisher_jurisdiction_and_declared_coverage():
    finra = next(e for e in source_registry.S if e['key'] == 'finra/rulebook')
    fca = next(e for e in source_registry.S if e['adapter'] == 'fca_handbook' and (e.get('fetch') or {}).get('chapters'))
    first = source_registry.source_meta(finra).about
    second = source_registry.source_meta(fca).about
    assert 'FINRA' in first and '(US)' in first and 'publication collection' in first
    assert fca['publisher'] in second and fca['jurisdiction'] in second
    assert 'Declared coverage:' in second and ', '.join(fca['fetch']['chapters']) in second
    assert first != second and 'CLHEAR regulatory corpus;' not in first + second


def test_registry_metadata_cannot_rename_explicit_fixture_into_publisher(engine):
    from app.clhear.l1.models import sources
    source_registry.seed(engine)
    with engine.begin() as conn:
        conn.execute(sources.update().where(sources.c.key == 'finra/rule/2210').values(issuer='Test fixture', name='Authored fixture'))
    source_registry.seed(engine)
    with engine.connect() as conn:
        row = conn.execute(sa.select(sources).where(sources.c.key == 'finra/rule/2210')).mappings().one()
    assert row['issuer'] == 'Test fixture' and row['name'] == 'Authored fixture'


def test_discovery_exact_original_alias_keeps_declared_document_identity(engine, tmp_path, monkeypatch):
    entry = next(dict(e) for e in source_registry.S if e['key'].startswith('esma/') and source_registry.source_role(e['key']) == 'document')
    new = {**entry, 'key': 'esma/document/new-discovery-key', 'discovered_category': 'esma-library'}
    monkeypatch.setattr(inventory, '_discover_publishers', lambda *args: ({new['key']: new}, {
        'complete': False, 'findings': [], 'pages': [], 'categories': [], 'checked_at': None}))
    result = inventory.run_inventory_audit(engine, LocalStore(tmp_path), job_id='exact-alias', discover=True)
    keys = {s['source_key'] for s in result['sources']}
    assert entry['key'] in keys and new['key'] not in keys
    assert result['source_aliases'] == [{'discovered_source_key': new['key'], 'source_key': entry['key'], 'canonical_url': entry['canonical_url']}]
    repeated = inventory.run_inventory_audit(engine, LocalStore(tmp_path), job_id='exact-alias-readback', discover=False)
    assert repeated['source_aliases'] == result['source_aliases']
    assert repeated['inventory_hash'] == result['inventory_hash']
