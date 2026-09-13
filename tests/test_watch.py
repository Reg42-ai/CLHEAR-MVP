"""HLD v2 §5 — Watch: the public change feed (L1 / L2 / L6, effective dates
separate from detection), Atom rendering, instrument watchlists, profile
watches and the digest for "profiles like mine"."""
from __future__ import annotations

from datetime import date

from app.clhear import watch
from app.clhear.l1 import pipeline
from app.clhear.l2.extract import run_extraction
from app.clhear.l3 import decompose as l3_decompose
from app.clhear.l4 import predicates as l4_predicates
from app.clhear.l4 import validate as l4_validate
from app.clhear.l5 import map as l5
from app.clhear.l6 import composer, diff as l6_diff
from app.clhear.platform.gateway import FakeProvider, Gateway
from tests.test_l6_blueprints import BROKER, SOURCE, UK, UKAdapter, _corpus

UK_V2 = {**UK, "5": "A firm must establish, maintain and publish a written conflicts of interest policy.",
         "6": "A firm must keep a record of each complaint received and the measures taken for its resolution."}


def _amend(engine, tmp_path):
    """A second version of the corpus: one clause amended, one added → L1 and L2 change events, L6 recomposes."""
    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, UKAdapter(UK_V2, "2026-07-01", source_key=SOURCE), store, gateway=Gateway(engine, FakeProvider()))
    run_extraction(engine, source_key=SOURCE)
    l3_decompose.decompose(engine, source_key=SOURCE)
    l4_predicates.extract_predicates(engine, source_key=SOURCE)
    l5.map_activities(engine)
    return l6_diff.recompose(engine, cause="L2 changed", publish=True)


def test_public_feed_combines_layers_with_effective_dates(engine, client, tmp_path):
    empty = client.get("/watch/feed").json()
    assert empty["count"] == 0 and empty["counts"] == {"L1": 0, "L2": 0, "L6": 0}
    _corpus(engine, tmp_path)
    prof = l4_validate.create_profile(engine, BROKER, name="Broker", source="golden")
    composer.compose_for_profile(engine, prof["id"], requested_by="test")
    out = _amend(engine, tmp_path)
    assert out["changed"] >= 1
    feed = client.get("/watch/feed").json()
    assert feed["counts"]["L1"] >= 1 and feed["counts"]["L2"] >= 1 and feed["counts"]["L6"] >= 1
    assert feed["count"] == len(feed["entries"]) == sum(feed["counts"].values())
    for e in feed["entries"]:
        assert e["layer"] in ("L1", "L2", "L6") and e["title"] and e["href"].startswith("/l")
        assert "effective_date" in e and e["effective_date_basis"] and e["detected_at"]  # change ≠ detection (I7)
    l1 = next(e for e in feed["entries"] if e["layer"] == "L1")
    assert l1["kind"] == "amended" and l1["subject"] == SOURCE and l1["clause_ids"]
    l2 = next(e for e in feed["entries"] if e["layer"] == "L2")
    assert l2["kind"] in ("added", "updated") and l2["subject"].startswith("OBL") and l2["id"].startswith("CHG-")
    l6 = next(e for e in feed["entries"] if e["layer"] == "L6")
    assert l6["kind"] == "recomposed" and l6["subject"].startswith("BLU-") and "supersedes" in l6["title"]
    # layer filter + since filter
    only = client.get("/watch/feed", params={"layers": "L2"}).json()
    assert only["layers"] == ["L2"] and all(e["layer"] == "L2" for e in only["entries"]) and only["count"] >= 1
    future = client.get("/watch/feed", params={"since": (date.today().replace(year=date.today().year + 1)).isoformat()}).json()
    assert future["count"] == 0


def test_atom_rendering(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    _amend(engine, tmp_path)
    r = client.get("/watch/feed.atom")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/atom+xml")
    body = r.text
    assert body.startswith('<?xml version="1.0"') and '<feed xmlns="http://www.w3.org/2005/Atom">' in body
    assert body.count("<entry>") == client.get("/watch/feed").json()["count"] >= 2
    assert '<category term="L1"/>' in body and "<updated>" in body and "</feed>" in body
    doc = watch.feed(engine)
    xml = watch.atom(doc, base_url="https://clhear.org")
    assert 'href="https://clhear.org/watch/feed.atom" rel="self"' in xml


def test_profile_watches_and_digest_for_profiles_like_mine(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    prof = l4_validate.create_profile(engine, BROKER, name="Broker", source="golden")
    composer.compose_for_profile(engine, prof["id"], requested_by="test")
    headers = {"X-Watcher-Id": "w-1"}
    assert client.get("/watch/digest").status_code == 401  # identity required
    d0 = client.get("/watch/digest", headers=headers).json()
    assert d0["count"] == 0 and "watch an instrument or a profile" in d0["empty_reason"]
    assert client.post("/watch/profiles", json={"profile_id": "PRF-999999"}, headers=headers).status_code == 404
    r = client.post("/watch/profiles", json={"profile_id": prof["id"]}, headers=headers)
    assert r.status_code == 201 and r.json()["watching"] is True
    mine = client.get("/watch/profiles", headers=headers).json()["profiles"]
    assert [p["profile_id"] for p in mine] == [prof["id"]] and mine[0]["name"] == "Broker"
    # so far only the corpus arriving: every entry is an "added" change on the duties this profile carries
    d1 = client.get("/watch/digest", headers=headers).json()
    assert d1["count"] >= 1 and {e["kind"] for e in d1["entries"]} == {"added"} and d1["counts"]["L6"] == 0
    assert d1["watching"]["profiles_like_mine"][0]["profile_id"] == prof["id"] and d1["watching"]["profiles_like_mine"][0]["obligations"] >= 1
    assert all(e["subject"] == SOURCE for e in d1["entries"] if e["layer"] == "L1")
    # the corpus is amended: the digest adds the L1 / L2 changes that touch this profile's duties and its blueprint's recomposition
    _amend(engine, tmp_path)
    d2 = client.get("/watch/digest", headers=headers).json()
    assert d2["count"] > d1["count"] and d2["counts"]["L6"] >= 1 and d2["counts"]["L1"] >= 2
    assert any(e["kind"] == "amended" for e in d2["entries"] if e["layer"] == "L1")
    assert all(e.get("profile_id") in (None, prof["id"]) for e in d2["entries"] if e["layer"] == "L6")
    # another watcher who follows nothing sees nothing — the digest is per watcher, the feed is public
    other = client.get("/watch/digest", headers={"X-Watcher-Id": "w-2"}).json()
    assert other["count"] == 0 and client.get("/watch/feed").json()["count"] >= d2["count"]
    # unwatch (never deleted: valid_to set) and re-watch reopen the same row
    assert client.post(f"/watch/profiles/{prof['id']}/unwatch", headers=headers).json()["watching"] is False
    assert client.get("/watch/profiles", headers=headers).json()["profiles"] == []
    assert client.post("/watch/profiles", json={"profile_id": prof["id"]}, headers=headers).status_code == 201
    assert len(client.get("/watch/profiles", headers=headers).json()["profiles"]) == 1


def test_instrument_watchlist_feeds_the_digest(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    headers = {"X-Watcher-Id": "w-3"}
    assert client.post("/l1/watchlists", json={"source_key": SOURCE}, headers=headers).status_code == 201
    _amend(engine, tmp_path)
    d = client.get("/watch/digest", headers=headers).json()
    assert d["watching"]["instruments"] == [SOURCE]
    assert d["counts"]["L1"] >= 1 and d["counts"]["L2"] >= 1 and d["counts"]["L6"] == 0  # no profile watched → no L6 rows
    assert all(e["layer"] != "L1" or e["subject"] == SOURCE for e in d["entries"])


def test_watch_page_served(client):
    r = client.get("/watch")
    assert r.status_code == 200 and "What changed, and from when." in r.text and "/watch/feed.atom" in r.text
