"""Publisher freshness requires a real check of every artifact in a run."""
import httpx
import pytest

from app.clhear.l1 import http


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("CLHEAR_HTTP_MODE", "live")
    monkeypatch.setenv("CLHEAR_HTTP_FIXTURES", str(tmp_path / "fixtures"))
    monkeypatch.setenv("CLHEAR_HTTP_CACHE_DIR", str(tmp_path / "private-cache"))
    monkeypatch.setattr(http, "_datalake_put", lambda *a: None)
    monkeypatch.setattr(http, "_datalake_get", lambda *a: None)
    http.begin_fetch()


def response(url, status=200, content=b"official version one", headers=None):
    return httpx.Response(status, content=content, headers=headers, request=httpx.Request("GET", url))


def test_live_ignores_committed_fixture_and_revalidates_each_run(monkeypatch):
    url = "https://publisher.example/rule"
    http._write_fixture(http._fixture_path(url), url, 200, b"old recorded fixture")
    calls = []
    def fetch(url, **kwargs):
        calls.append(kwargs["headers"])
        return response(url, headers={"etag": '"one"'}) if len(calls) == 1 else response(url, 304, b"")
    monkeypatch.setattr(http.httpx, "get", fetch)
    assert http.get(url) == b"official version one"
    first_check = http.publisher_checked_at()
    assert first_check
    http.begin_fetch()
    assert http.get(url) == b"official version one"
    assert calls[1]["If-None-Match"] == '"one"'
    assert http.fetch_evidence()[0]["origin"] == "revalidated"
    assert http.publisher_checked_at() >= first_check
    assert http._read_fixture(http._fixture_path(url)) == b"old recorded fixture"
    assert http._cache_path(url).stat().st_mode & 0o077 == 0


def test_one_cached_fallback_makes_entire_multi_artifact_check_stale(monkeypatch):
    def fetch(url, *args):
        if url.endswith("failed"):
            raise RuntimeError("publisher outage")
        return b"current official bytes"
    monkeypatch.setattr(http, "_fetch_live", fetch)
    monkeypatch.setattr(http, "_datalake_get", lambda url: b"last good")
    assert http.get("https://publisher.example/failed") == b"last good"
    assert http.get("https://publisher.example/working") == b"current official bytes"
    assert http.last_good_used()
    assert http.publisher_checked_at() is None
    http.begin_fetch()
    assert not http.last_good_used()
    assert http.publisher_checked_at() is None


def test_fixture_replay_is_never_a_publisher_check(monkeypatch):
    monkeypatch.setenv("CLHEAR_HTTP_MODE", "replay")
    url = "https://publisher.example/rule"
    http._write_fixture(http._fixture_path(url), url, 200, b"recorded bytes")
    monkeypatch.setattr(http, "_fetch_live", lambda *a: pytest.fail("replay contacted publisher"))
    assert http.get(url) == b"recorded bytes"
    assert http.publisher_checked_at() is None
    assert http.fetch_evidence()[0]["origin"] == "fixture"


def test_cache_paths_are_private():
    assert http._datalake_cache_key("https://www.finra.org/rule").startswith("restricted/")


def test_corrupted_cache_cannot_satisfy_304(monkeypatch):
    url = "https://publisher.example/rule"
    http._write_fixture(http._cache_path(url), url, 200, b"wrong bytes", sha256="tampered")
    monkeypatch.setattr(http.httpx, "get", lambda url, **kw: response(url, 304, b""))
    with pytest.raises(RuntimeError, match="304 without verified"):
        http.get(url)
    assert http.publisher_checked_at() is None


def test_new_response_without_validators_discards_old_etag(monkeypatch):
    url = "https://publisher.example/rule"
    calls = []
    def fetch(url, **kw):
        calls.append(kw["headers"])
        return response(url, content=f"version {len(calls)}".encode(),
                        headers={"etag": "old"} if len(calls) == 1 else {})
    monkeypatch.setattr(http.httpx, "get", fetch)
    for _ in range(3):
        http.begin_fetch()
        http.get(url)
    assert calls[1]["If-None-Match"] == "old"
    assert "If-None-Match" not in calls[2]


def test_finra_off_publisher_redirect_is_not_verified(monkeypatch):
    monkeypatch.setattr(http.httpx, "get", lambda url, **kw: response("https://unapproved.example/copy"))
    with pytest.raises(ValueError, match="outside the authorized publisher"):
        http.get("https://www.finra.org/rules-guidance/rulebooks/finra-rules/2210")
    assert http.publisher_checked_at() is None


def test_finra_redirect_checked_before_destination_request(monkeypatch):
    calls = []
    def fetch(url, **kw):
        calls.append(url)
        assert kw["follow_redirects"] is False
        return response(url, 302, b"", {"location": "https://unapproved.example/copy"})
    monkeypatch.setattr(http.httpx, "get", fetch)
    original = "https://www.finra.org/rules-guidance/rulebooks/finra-rules/2210"
    with pytest.raises(ValueError, match="outside the authorized publisher"):
        http.get(original)
    assert calls == [original]


def test_finra_official_redirect_retains_provenance(monkeypatch):
    original, final = "https://finra.org/old", "https://www.finra.org/current"
    calls = []
    def fetch(url, **kw):
        calls.append(url)
        return response(url, 301, b"", {"location": final}) if url == original else response(url)
    monkeypatch.setattr(http.httpx, "get", fetch)
    assert http.get(original) == b"official version one"
    assert calls == [original, final]
    evidence = http.fetch_evidence()[0]
    assert evidence["final_url"] == final and evidence["redirect_chain"] == [original]
    assert http.publisher_checked_at()
