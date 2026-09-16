"""No publisher-host boundary may be bypassed by redirects or old caches."""
import gzip
import json

import pytest

from app.clhear.l1 import http
from tests.test_l1_http_freshness import isolated_cache, response  # noqa: F401

URL='https://publisher.example/document'
HOSTS={'publisher.example'}


@pytest.mark.parametrize('destination', ['https://third-party.example/copy','http://publisher.example/plain',
    'https://publisher.example:8443/copy','https://user:password@publisher.example/copy'])
def test_redirect_rejected_before_destination_request_and_cannot_fallback(monkeypatch,destination):
    calls=[]
    http._write_fixture(http._cache_path(URL),URL,200,b'old',final_url=URL,redirect_chain=[])
    def fetch(url,**kw):
        calls.append(url)
        assert kw['follow_redirects'] is False
        return response(url,302,b'',{'location':destination})
    monkeypatch.setattr(http.httpx,'get',fetch)
    monkeypatch.setattr(http,'_datalake_get',lambda *a:pytest.fail('Boundary errors cannot use datalake fallback'))
    with pytest.raises(http.PublisherBoundaryError):http.get(URL,allowed_redirect_hosts=HOSTS)
    assert calls==[URL] and http.publisher_checked_at() is None
    assert http.fetch_evidence()[-1]['origin']=='failed'


def test_same_host_redirect_is_recorded_and_revalidated(monkeypatch):
    final=URL+'/current';calls=[]
    def fetch(url,**kw):
        calls.append((url,kw['headers']))
        if len(calls)==1:return response(url,302,b'',{'location':'/document/current'})
        if len(calls)==2:return response(url,headers={'etag':'"one"'})
        return response(url,304,b'')
    monkeypatch.setattr(http.httpx,'get',fetch)
    assert http.get(URL,allowed_redirect_hosts=HOSTS)==b'official version one'
    cache=json.loads(gzip.decompress(http._cache_path(URL).read_bytes()))
    assert cache['final_url']==final and cache['redirect_chain']==[URL]
    http.begin_fetch()
    assert http.get(URL,allowed_redirect_hosts=HOSTS)==b'official version one'
    assert calls[-1][1]['If-None-Match']=='"one"'
    assert http.fetch_evidence()[-1]['origin']=='revalidated'


@pytest.mark.parametrize('metadata',[{}, {'final_url':'https://third-party.example/copy','redirect_chain':[]},
    {'final_url':URL,'redirect_chain':['https://third-party.example/hop']}])
def test_unproven_cache_never_sends_validator_or_satisfies_304(monkeypatch,metadata):
    http._write_fixture(http._cache_path(URL),URL,200,b'unproven',etag='"old"',**metadata)
    def fetch(url,**kw):
        assert 'If-None-Match' not in kw['headers']
        return response(url,304,b'')
    monkeypatch.setattr(http.httpx,'get',fetch)
    monkeypatch.setattr(http,'_datalake_get',lambda *a:pytest.fail('Raw S3 cache cannot certify publisher provenance'))
    with pytest.raises(RuntimeError,match='304 without verified'):http.get(URL,allowed_redirect_hosts=HOSTS)
    assert http.publisher_checked_at() is None


def test_outage_can_only_return_verified_local_cache_as_stale(monkeypatch):
    http._write_fixture(http._cache_path(URL),URL,200,b'prior',final_url=URL,redirect_chain=[])
    monkeypatch.setattr(http,'_fetch_live',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('offline test outage')))
    monkeypatch.setattr(http,'_datalake_get',lambda *a:pytest.fail('Guarded fallback must stay bound to verified local bytes'))
    assert http.get(URL,allowed_redirect_hosts=HOSTS)==b'prior'
    assert http.publisher_checked_at() is None
    assert http.fetch_evidence()[-1]['origin']=='stale_cache'
    assert http.fetch_evidence()[-1]['final_url']==URL


def test_replay_does_not_follow_or_certify_legacy_fixture(monkeypatch):
    monkeypatch.setenv('CLHEAR_HTTP_MODE','replay')
    http._write_fixture(http._fixture_path(URL),URL,200,b'authored fixture')
    monkeypatch.setattr(http.httpx,'get',lambda *a,**kw:pytest.fail('Offline fixture must not fetch'))
    assert http.get(URL,allowed_redirect_hosts=HOSTS)==b'authored fixture'
    assert http.publisher_checked_at() is None
    http._write_fixture(http._fixture_path(URL),URL,200,b'unapproved copy',final_url='https://third-party.example/copy',redirect_chain=[])
    with pytest.raises(http.PublisherBoundaryError):http.get(URL,allowed_redirect_hosts=HOSTS)


def test_initial_url_and_finra_policy_cannot_be_widened(monkeypatch):
    monkeypatch.setattr(http.httpx,'get',lambda *a,**kw:pytest.fail('Unapproved initial URL must not fetch'))
    with pytest.raises(http.PublisherBoundaryError):http.get('https://third-party.example/copy',allowed_redirect_hosts=HOSTS)
    calls=[]
    monkeypatch.setattr(http.httpx,'get',lambda url,**kw:(calls.append(url) or response(url,302,b'',{'location':URL})))
    with pytest.raises(http.PublisherBoundaryError):
        http.get('https://www.finra.org/original',allowed_redirect_hosts={'www.finra.org','publisher.example'})
    assert calls==['https://www.finra.org/original']
