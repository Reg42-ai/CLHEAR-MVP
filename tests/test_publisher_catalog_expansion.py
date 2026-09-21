"""Offline metadata contracts; authored markup is never a regulatory corpus.

Real route/protocol references are in publisher_catalogs and legislation_catalogs.
Fixtures below exercise those observed route shapes and documented response
schemas, without claiming synthetic titles/text were ever publisher material.
"""
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import pytest

from app.clhear.l1 import inventory, publishers
from app.clhear.l1 import publisher_catalogs as c, legislation_catalogs as l
from app.clhear.l1.pipeline import LocalStore
from app.clhear.l1.permissions import record_permission


def profile(key):
    return next(p for p in publishers.publisher_profiles() if p['publisher_id'] == key)


def grant(engine, key):
    record_permission(engine, source_key=key, permissions={'acquire': True, 'store': True, 'parse': True},
                      approved=True, evidence_ref='test:authored-catalog', approved_by='unit-test')


@pytest.mark.parametrize('publisher_id', sorted(c.LIBRARIES))
def test_every_reviewed_library_seed_classifies_as_collection(publisher_id):
    spec = c.LIBRARIES[publisher_id]
    target, _ = c.library_decoder(profile(publisher_id))
    for category, url in spec['roots'].items():
        found = target(url, {'url': url, 'category': category})
        assert found and found['role'] == 'collection'
        assert found['source_key'] == f'{publisher_id}/catalog/{category}'
        assert 'entry' not in found


def test_all_profiles_have_executable_contract_without_claiming_complete():
    all_profiles = publishers.publisher_profiles()
    assert len(all_profiles) == 46
    assert all(p['discovery_adapter'] for p in all_profiles)
    assert all(not p['denominator_known'] and p['expected_documents'] is None for p in all_profiles)


def test_pagination_inside_nav_and_exposed_get_years_are_preserved():
    target, decode = c.library_decoder(profile('fincen'))
    url = c.LIBRARIES['fincen']['roots']['guidance']
    page = {'url': url, 'category': 'guidance', 'role': 'collection'}
    body = b'''<main><nav aria-label="Pagination"><a rel="next" href="?page=1">Next</a></nav>
    <form method="get"><select name="year"><option value="2025">2025</option><option value="2024">2024</option></select></form>
    <a href="/resources/statutes-regulations/guidance/test-only-fixture">Fixture title</a></main>'''
    result = decode(body, page)
    assert {x['url'] for x in result['links']} >= {url+'?page=1', url+'?year=2025', url+'?year=2024'}
    assert not result['findings']
    assert result['entries'][0]['fetch']['document_type'] == 'publisher_publication'
    assert target(url+'?page=1', page)['source_key'] == 'fincen/catalog/guidance'


def test_unreviewed_form_parameters_and_post_never_silently_exhaust():
    _, decode = c.library_decoder(profile('fincen'))
    page = {'url': c.LIBRARIES['fincen']['roots']['guidance'], 'category': 'guidance', 'role': 'collection'}
    for form in ('<form method="post"><select name="year"><option value="2025">2025</option></select></form>',
                 '<form><select name="unknown"><option value="2025">2025</option></select></form>'):
        result = decode(('<main>'+form+'</main>').encode(), page)
        assert not result['links'] and result['findings']


def test_safe_url_rejects_lookalike_hosts_arbitrary_queries_and_duplicate_filters():
    spec = c.LIBRARIES['fca']
    for url in ('https://www.fca.org.uk.evil.example/publications', 'https://www.fca.org.uk/%2e%2e/private',
                'https://user:pass@www.fca.org.uk/publications', 'https://www.fca.org.uk/publications?next=https://evil.example',
                'https://www.fca.org.uk/publications?page=1&page=2', 'https://www.fca.org.uk:444/publications'):
        assert c.safe_url(url, spec) is None
    assert c.safe_url('https://www.fca.org.uk/publications/search-results?category=policy+and+guidance&p_search_term=&sort_by=dmetaZ&page=1', spec)


def test_discovery_uses_catalog_grant_but_does_not_fetch_document_without_own_grant(engine, tmp_path):
    from app.clhear.l1.catalogs import discover_catalog
    index = c.LIBRARIES['fincen']['roots']['guidance']
    document = index + '/test-only-fixture'
    grant(engine, 'fincen/catalog/guidance')
    calls = []
    def fetch(url):
        calls.append(url)
        assert url == index
        return f'<main><a href="{document}">Fixture guidance</a></main>'.encode(), 'live'
    entries, result = discover_catalog(engine, LocalStore(tmp_path), profile('fincen'), job_id='fixture', fetcher=fetch)
    assert calls == [index] and len(entries) == 1
    assert not result['complete']
    assert {f['code'] for f in result['findings']} >= {'discovery_permission_blocked', 'fincen_catalog_reconciliation_required'}


def test_licensed_product_page_remains_artifact_gap_and_is_not_fetched(engine, tmp_path):
    from app.clhear.l1.catalogs import discover_catalog
    for category in c.LIBRARIES['iso']['roots']:
        grant(engine, 'iso/catalog/' + category)
    calls = []
    def fetch(url):
        calls.append(url)
        return b'<main><a href="https://www.iso.org/standard/test-only-catalog-id">Authored standard fixture</a></main>', 'live'
    entries, report = discover_catalog(engine, LocalStore(tmp_path), profile('iso'), job_id='iso-fixture', fetcher=fetch)
    assert len(calls) == 2 and all('/committee/' in u for u in calls)
    assert len(entries) == 1 and not report['complete']
    entry = next(iter(entries.values()))
    assert entry['fetch']['blocked'] == 'authorized_standard_artifact_required'
    from app.clhear.l1.fleet import adapter_for
    assert adapter_for(entry).declaration_gap


def test_language_metadata_requires_actual_body_and_native_language_not_url():
    body = b'<html lang="fr"><main><h1>Authored fixture</h1><p>A complete authored paragraph used only for a unit test.</p></main></html>'
    assert c.language_metadata(body, publisher_id='fca', document_key='fixture', url='https://www.fca.org.uk/en') is None
    result = c.language_metadata(body, publisher_id='fr-amf', document_key='fixture', url='https://www.amf-france.org/fr/fixture')
    assert result['language'] == 'fr' and result['artifact_sha256'] == hashlib.sha256(body).hexdigest()
    assert c.language_metadata(body.replace(b'lang="fr"', b'lang="en"'), publisher_id='fr-amf', document_key='fixture', url='https://www.amf-france.org/en/fixture') is None
    assert c.language_metadata(body.replace(b'A complete', b'Unofficial translation of a complete'), publisher_id='fr-amf', document_key='fixture', url='https://www.amf-france.org/fr/fixture') is None


def test_generic_original_adapter_rejects_catalog_and_preserves_real_bytes(monkeypatch):
    from app.clhear.l1.fleet import adapter_for
    from app.clhear.l1 import http
    entry = c.document_entry(profile('fincen'), c.LIBRARIES['fincen'],
                             'https://www.fincen.gov/resources/statutes-regulations/guidance/test-only', 'Authored fixture', 'guidance')
    adapter = adapter_for(entry)
    body = b'<html lang="en"><body><main><h1>Authored fixture</h1><p>This paragraph was authored to test original preservation without regulatory text.</p></main></body></html>'
    monkeypatch.setattr(http, 'get', lambda url, **kwargs: body)
    result = adapter.fetch()
    assert result.artifacts[0].content == body and result.as_of_date is None
    from app.clhear.l1.originals import attach_source_locations, verify_original_projection
    assert attach_source_locations(entry['key'], entry['adapter'], result.artifacts, result.tree, entry['canonical_url'])
    assert verify_original_projection(entry['key'], entry['adapter'], result.artifacts, result.tree, canonical_url=entry['canonical_url'])['verified']
    monkeypatch.setattr(http, 'get', lambda url, **kwargs: b'<html><main><h1>Catalog</h1><a href="/file.pdf">An entire page containing only a download link</a></main></html>')
    with pytest.raises(ValueError, match='catalog'):
        adapter.fetch()


def test_uk_atom_protocol_preserves_identifiers_and_publisher_next_link():
    seeds, decode = l.uk_atom(profile('uk-law'))
    body = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry xml:lang="en"><id>http://www.legislation.gov.uk/id/uksi/2017/692</id><title>Authored catalog fixture</title><updated>2026-09-16T00:00:00Z</updated></entry><link rel="next" href="http://www.legislation.gov.uk/all/data.feed?page=2"/></feed>'''
    result = decode(body, {'url': seeds[0]['url']})
    assert result['entries'][0]['key'] == 'uksi/2017/692'
    assert result['entries'][0]['fetch'] == {'doc': 'uksi/2017/692'}
    assert result['links'][0]['url'] == 'https://www.legislation.gov.uk/all/data.feed?page=2'
    assert result['entries'][0]['language_candidates'][0]['authority'] == 'unknown'
    with pytest.raises(ValueError, match='advance'):
        decode(body, {'url': result['links'][0]['url']})
    with pytest.raises(ValueError, match='Untrusted'):
        decode(body.replace(b'www.legislation.gov.uk/all/data.feed', b'evil.example/all/data.feed'), {'url': seeds[0]['url']})


def au_row(**changes):
    return dict(titleId='C2004A03712', start='2025-01-01T00:00:00Z', retrospectiveStart='2025-01-01T00:00:00Z',
                rectificationVersionNumber=0, type='Primary', uniqueTypeNumber=0, volumeNumber=1, format='Pdf',
                registerId='C2025C00001', name='Authored fixture document', isAuthorised=True, **changes)


def test_au_odata_exact_composite_document_identity_and_offset_count():
    seeds, decode = l.au_odata(profile('au-law'))
    result = decode(json.dumps({'value':[au_row()], '@odata.count': 2}).encode(), {'url': seeds[0]['url']})
    entry = result['entries'][0]
    assert "rectificationversionnumber=0" in entry['canonical_url'] and "volumeNumber=1" in entry['canonical_url']
    assert entry['publisher_edition'] == '2025-01-01T00:00:00Z'
    assert parse_qs(urlparse(result['links'][0]['url']).query)['$skip'] == ['1']
    assert '$select' in parse_qs(urlparse(seeds[0]['url']).query)
    with pytest.raises(ValueError, match='ended before'):
        decode(b'{"value":[],"@odata.count":2}', {'url': seeds[0]['url']})
    with pytest.raises(ValueError, match='Duplicate'):
        decode(json.dumps({'value':[au_row(),au_row()], '@odata.count':2}).encode(), {'url': seeds[0]['url']})
    row = au_row(); row['bytes'] = 'unexpected'
    with pytest.raises(ValueError, match='bytes'):
        decode(json.dumps({'value':[row], '@odata.count':1}).encode(), {'url': seeds[0]['url']})


def test_finra_includes_nonrule_decisions_and_actual_year_form_values(engine, tmp_path, monkeypatch):
    categories = {key for key, _, _ in inventory.FINRA_CATEGORIES}
    assert {'nac','oho','sanctions','faqs'} <= categories
    index = 'https://www.finra.org/rules-guidance/notices'
    monkeypatch.setattr(inventory, 'FINRA_CATEGORIES', (('notices','Notices',index),))
    monkeypatch.setenv('CLHEAR_L1_FINRA_FULL_DISCOVERY', 'true')  # notices are outside the default rulebook seeds
    monkeypatch.setenv('CLHEAR_L1_DISCOVERY_MAX_PAGES', '1')
    grant(engine, 'finra/catalog/notices')
    monkeypatch.setattr(inventory, '_fetch_discovery', lambda url: (b'<main><form method="get"><select name="year"><option value="1995">1995</option><option value="2026">2026</option></select></form><nav><a rel="next" href="?page=1">Next</a></nav></main>', 'live'))
    _, report = inventory._discover(engine, LocalStore(tmp_path))
    assert report['pending_pages'] == 3 and not report['complete']
    assert inventory._in_scope_url('https://www.finra.org/rules-guidance/adjudication-decisions/national-adjudicatory-council-nac/test-only')
    assert inventory._url(index+'?page=1&page=2') is None


def test_cellar_preserves_language_catalog_but_authoritative_english_satisfies_both_views():
    from app.clhear.l1 import structured_catalogs as s
    _, decode = s._cellar(profile('eu-law'))
    result = decode(json.dumps({'results':{'bindings':[{'celex':{'value':'32016R0679'}, 'languages':{'value':
        'http://publications.europa.eu/resource/authority/language/FRA|http://publications.europa.eu/resource/authority/language/ENG|http://publications.europa.eu/resource/authority/language/DEU'}}]}}).encode(), {})
    assert len(result['entries']) == 1
    entry = result['entries'][0]
    assert entry['key'] == 'celex/32016R0679' and entry['fetch']['language'] == 'ENG'
    assert entry['publisher_language_expressions'] == ['de','en','fr']
    assert not result['findings']


def test_cellar_missing_english_preserves_original_language_and_separate_identity():
    from app.clhear.l1 import structured_catalogs as s
    _, decode = s._cellar(profile('eu-law'))
    result = decode(json.dumps({'results':{'bindings':[{'celex':{'value':'32016R0679'}, 'languages':{'value':
        'http://publications.europa.eu/resource/authority/language/FRA'}}]}}).encode(), {})
    assert result['entries'][0]['key'] == 'celex/32016R0679/fr'
    assert result['entries'][0]['fetch']['language'] == 'FRA'
    assert result['entries'][0]['document_group_key'] == 'celex/32016R0679'


def test_eu_language_urls_cannot_cross_use_cache_or_accept_wrong_language(monkeypatch):
    from app.clhear.l1.adapters.eur_lex_languages import EurLexLanguageAdapter
    from app.clhear.l1 import http
    calls = []
    def fetch(url, **kwargs):
        assert kwargs["allowed_redirect_hosts"] == {"eur-lex.europa.eu"}
        calls.append(url)
        lang = 'fr' if '/FR/' in url else 'en'
        return ('<html lang="'+lang+'"><body><div id="art_1"><p class="oj-normal">Authored document fixture.</p></div></body></html>').encode()
    monkeypatch.setattr(http, 'get', fetch)
    for language in ('FRA', 'ENG'):
        result = EurLexLanguageAdapter(language=language, celex='32016R0679', celex_version='32016R0679').fetch()
        assert result.artifacts[0].content
    assert calls[0] != calls[1] and '/FR/TXT/HTML/' in calls[0] and '/EN/TXT/HTML/' in calls[1]
    monkeypatch.setattr(http, 'get', lambda url, **kwargs: b'<html lang="en"><body><div id="art_1"><p>Authored fixture</p></div></body></html>')
    with pytest.raises(ValueError, match='language'):
        EurLexLanguageAdapter(language='FRA').fetch()
    monkeypatch.setattr(http, 'get', lambda url, **kwargs: b'<html lang="fr"><body>Enable JavaScript to verify you are not a robot.</body></html>')
    with pytest.raises(ValueError, match='legal document'):
        EurLexLanguageAdapter(language='FRA').fetch()


def test_nist_abstract_landing_page_is_catalog_only_actual_pdf_is_original():
    target, decode = c.library_decoder(profile('nist'))
    parent = {'url': c.LIBRARIES['nist']['roots']['publications'], 'category': 'publications', 'role': 'collection'}
    url = 'https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final'
    # Older and current documented routes are both metadata landings.
    landing = target(url, parent)
    assert landing['role'] == 'collection' and 'entry' not in landing
    body = b'<main><h1>Fixture NIST standard abstract</h1><p>This is an authored abstract, not the publication text.</p><a href="https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-53r5.pdf">Publication PDF</a></main>'
    result = decode(body, landing)
    assert len(result['entries']) == 1
    assert result['entries'][0]['canonical_url'].startswith('https://nvlpubs.nist.gov/')
    assert result['entries'][0]['fetch']['kind'] == 'pdf'


def test_exact_registered_original_keeps_source_identity_not_collection_identity():
    from app.clhear.l1.source_registry import S
    known = next(e for e in S if e['key'] == 'iso/27001-2022')
    entry = c.document_entry(profile('iso'), c.LIBRARIES['iso'], known['canonical_url'], 'ISO/IEC 27001', 'security')
    assert entry['key'] == known['key']
    assert 'blocked' not in entry['fetch'] and 'document_type' not in entry['fetch']
    from app.clhear.l1.fleet import adapter_for
    assert type(adapter_for(entry)).__name__ == 'RestrictedFileAdapter'
    another = c.document_entry(profile('iso'), c.LIBRARIES['iso'], 'https://www.iso.org/standard/test-only-different', 'ISO/IEC 27001', 'security')
    assert another['key'] != known['key']


def test_au_request_contract_matches_captured_official_openapi_metadata():
    from pathlib import Path
    captured = json.loads((Path(__file__).parent / 'fixtures/publisher_catalogs/au_openapi_metadata.json').read_text())
    assert captured['source'] == 'https://api.prod.legislation.gov.au/swagger/v1/swagger.json'
    props = captured['document_properties']
    assert set(l.AU_FIELDS.split(',')) <= props.keys()
    assert set(l.AU_ORDER.split(',')) <= props.keys()
    assert l.AU_TYPES == set(props['type']['enum'])
    assert props['isAuthorised']['type'] == 'boolean'
    assert 'bytes' not in l.AU_FIELDS and 'Pdf' in props['format']['enum']
    assert '/v1/Documents' in captured['paths']
    assert any("rectificationversionnumber={keyrectificationVersionNumber}" in p for p in captured['paths'])


def test_library_preserves_other_language_metadata_without_duplicate_imports():
    target, decode = c.library_decoder(profile('fatf'))
    root = c.LIBRARIES['fatf']['roots']['publications']
    url = 'https://www.fatf-gafi.org/en/publications/Mutualevaluations/test-only.html'
    page = target(url, {'url': root, 'category': 'publications'})
    body = b'<html lang="en"><head><link rel="alternate" hreflang="fr" href="https://www.fatf-gafi.org/fr/publications/Mutualevaluations/test-only.html"></head><main><h1>Authored fixture</h1><p>This authored English original is long enough to establish an actual body.</p></main></html>'
    result = decode(body, page)
    assert len(result['entries']) == 1 and not result['links']
    assert result['catalog_metadata']['language_alternates'][0]['language'] == 'fr'
    assert result['entries'][0]['language_evidence']['language'] == 'en'
