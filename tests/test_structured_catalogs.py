"""Protocol tests use authored metadata fixtures, never live publisher records."""
import json
from urllib.parse import urlparse
import pytest
from app.clhear.l1 import publishers, structured_catalogs as c
from app.clhear.l1.pipeline import LocalStore
from app.clhear.l1.permissions import record_permission


def profile(key):
    return next(p for p in publishers.publisher_profiles() if p["publisher_id"] == key)


def grant(engine, key):
    record_permission(engine, source_key=key, permissions={"acquire": True, "store": True, "parse": True},
                      approved=True, approved_by="fixture-reviewer", evidence_ref="test:metadata-catalog")


def test_cellar_keyset_continuation_and_exact_identifiers():
    seeds, decode = c._cellar(profile("eu-law"))
    ids = [f"32020R{i:04d}" for i in range(1, 101)]
    decoded = decode(json.dumps({"results": {"bindings": [{"celex": {"value": i}} for i in ids]}}).encode(), {"url": seeds[0]["url"]})
    assert len(decoded["entries"]) == 100
    assert decoded["entries"][0]["key"] == "celex/32020R0001"
    assert decoded["links"][0]["url"] == c.cellar_url(ids[-1])
    assert decoded["links"][0]["source_key"] == seeds[0]["source_key"]
    assert decoded["entries"][0]["fetch"]["celex_version"] == ids[0]
    with pytest.raises(ValueError):
        c.cellar_url('" } SERVICE <https://untrusted.example> {')
    with pytest.raises(ValueError):
        decode(b'{"results":{"bindings":[{"celex":{"value":"bad"}}]}}', {})


def test_cellar_catalog_uses_worker_frontier_without_importing_originals(engine, tmp_path):
    grant(engine, "eu-law/catalog/celex")
    calls = []
    def fetch(url):
        calls.append(url)
        assert url.startswith(c.CELLAR)
        return b'{"results":{"bindings":[{"celex":{"value":"32020R0001"}}]}}', "live"
    entries, result = c.discover_structured(engine, LocalStore(tmp_path), profile("eu-law"), job_id="catalog-fixture", fetcher=fetch)
    assert list(entries) == ["celex/32020R0001"] and len(calls) == 1
    assert not result["complete"]
    assert result["findings"][-1]["code"] == "publisher_categories_unconfigured"


def test_esma_excludes_speeches_preserves_unknown_type_gap_and_follows_catalog(monkeypatch):
    seeds, decode = c._esma(profile("esma"))
    monkeypatch.setattr(c, "ESMA_PAGE_SIZE", 3)
    body = b'''<main>5 documents<table><tr><th>Date</th><th>Reference</th><th>Title</th><th>Sections</th><th>Type</th><th>Main document</th></tr>
    <tr><td>date</td><td>TEST-1</td><td>Fixture guidelines</td><td>Guidelines</td><td>Guidelines &amp; Recommendations</td><td><a href="/sites/default/files/test1.pdf">PDF</a></td></tr>
    <tr><td>date</td><td>TEST-2</td><td>Fixture speech</td><td>Speeches</td><td>Speech</td><td><a href="/sites/default/files/test2.pdf">PDF</a></td></tr>
    <tr><td>date</td><td>TEST-3</td><td>Fixture ambiguous</td><td>Other</td><td>Reference</td><td><a href="/sites/default/files/test3.pdf">PDF</a></td></tr></table></main>'''
    result = decode(body, {"url": seeds[0]["url"]})
    assert len(result["entries"]) == 1
    assert result["entries"][0]["publisher_reference"] == "TEST-1"
    assert result["findings"][0]["publisher_reference"] == "TEST-3"
    assert result["links"][0]["url"] == c.ESMA + "?page=1"
    assert result["catalog_metadata"]["observed_rows"] == 3


def test_govinfo_sitemap_to_exact_linked_original_rejects_external_host():
    seeds, decode = c._govinfo(profile("us-law"))
    page = {"url": c.GOVINFO["USCODE"], "source_key": "us-law/catalog/USCODE", "category": "USCODE"}
    result = decode(b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://www.govinfo.gov/sitemap/USCODE_2025_sitemap.xml</loc></sitemap></sitemapindex>', page)
    assert result["links"][0]["role"] == "collection"
    page["url"] = result["links"][0]["url"]
    result = decode(b'<urlset><url><loc>https://www.govinfo.gov/app/details/USCODE-2025-title15</loc></url><url><loc>https://evil.example/USCODE.pdf</loc></url></urlset>', page)
    assert len(result["links"]) == 1 and result["findings"]
    page["url"] = result["links"][0]["url"]
    result = decode(b'<h1>Test title</h1><a href="/content/pkg/USCODE-2025-title15/pdf/USCODE-2025-title15.pdf">Full PDF</a>', page)
    assert result["entries"][0]["key"] == "govinfo/package/USCODE-2025-title15"
    assert result["entries"][0]["fetch"]["kind"] == "pdf"
    assert not result["findings"]
    with pytest.raises(ValueError, match="entities"):
        decode(b'<!DOCTYPE test [<!ENTITY x SYSTEM "file:///etc/passwd">]><urlset/>', page)


def test_discovered_entries_create_real_adapters_without_inventing_family():
    from app.clhear.l1.fleet import adapter_for
    _, decode = c._cellar(profile("eu-law"))
    entry = decode(b'{"results":{"bindings":[{"celex":{"value":"32020R0001"}}]}}', {})["entries"][0]
    adapter = adapter_for(entry)
    assert adapter.meta().source_key == entry["key"]
    assert adapter.meta().family_key == "publisher-eu-law"
    assert not hasattr(adapter, "declaration_gap")


def test_cellar_repeated_cursor_fails_instead_of_certifying_truncated_catalog():
    _, decode = c._cellar(profile('eu-law'))
    body = b'{"results":{"bindings":[{"celex":{"value":"32020R0001"}}]}}'
    with pytest.raises(ValueError, match='did not advance'):
        decode(body, {'url': c.cellar_url('32020R0001')})


def test_govinfo_cross_collection_sitemap_is_not_followed():
    _, decode = c._govinfo(profile('us-law'))
    page = {'url': c.GOVINFO['CFR'], 'source_key': 'us-law/catalog/CFR', 'category': 'CFR'}
    result = decode(b'<sitemapindex><sitemap><loc>https://www.govinfo.gov/sitemap/CREC_2025_sitemap.xml</loc></sitemap></sitemapindex>', page)
    assert not result['links']
    assert any(f['code'] == 'catalog_link_unverified' for f in result['findings'])


def test_exact_open_publisher_contracts_preserve_existing_rights_policy():
    from app.clhear.l1.permissions import required_for
    _, decode = c._cellar(profile('eu-law'))
    eu = decode(b'{"results":{"bindings":[{"celex":{"value":"32020R0001R(01)"}}]}}', {})['entries'][0]
    assert eu['rights_basis'] == 'open_licence' and not required_for(eu)
    us = c._entry(profile('us-law'), 'govinfo/package/CFR-2025-title17', 'Fixture',
                  'https://www.govinfo.gov/content/pkg/CFR-2025-title17/pdf/CFR-2025-title17.pdf',
                  'govinfo_us', 'us-broker-dealer')
    assert us['rights_basis'] == 'public_domain' and not required_for(us)
    assert us['rights_evidence_url'] == 'https://www.govinfo.gov/about/policies'


def test_failed_catalog_parse_retains_exact_acquisition_evidence(engine, tmp_path):
    import hashlib
    grant(engine, 'eu-law/catalog/celex')
    body = b'{"results":{"bindings":[{"celex":{"value":"invalid-id"}}]}}'
    entries, result = c.discover_structured(engine, LocalStore(tmp_path), profile('eu-law'),
        job_id='failed-decoder', fetcher=lambda url: (body, 'live'))
    assert not entries and not result['complete']
    assert result['pages'][0]['sha256'] == hashlib.sha256(body).hexdigest()
    assert result['pages'][0]['publisher_checked_at']
    assert any(f['code'] == 'discovery_failed' for f in result['findings'])
    assert result['pending_pages'] == 0
    assert result['categories'][0]['pending_pages'] == 0
    assert result['categories'][0]['unresolved_pages'] == 1
