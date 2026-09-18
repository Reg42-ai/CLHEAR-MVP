"""Public /l1 API (HLD v2 §4.1 / §5): browse by jurisdiction / regulator /
instrument, versions, change feed with effective dates, clause spans, rights
discipline (derived-only text withheld), family scorecards, watchlists."""
import sqlalchemy as sa

from app.clhear.l1 import pipeline
from app.clhear.l1.models import clauses, source_versions, sources
from tests.test_l1_synthetic_amendment import V1, V2, SyntheticAdapter


def _seed(engine, tmp_path):
    from app.clhear.l1.permissions import record_permission
    store = pipeline.LocalStore(tmp_path / "lake")
    record_permission(engine, source_key="synthetic/finra-3110", permissions={"acquire": True, "store": True, "parse": True},
                      evidence_ref="test:original-api-fixture", approved_by="test", approved=True)
    pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01"), store)
    pipeline.ingest(engine, SyntheticAdapter(V2, "2026-06-01"), store)
    pipeline.ingest(
        engine,
        SyntheticAdapter(V1, "2026-01-01", adapter="finra", rights_basis="derived_only", source_key="synthetic/finra-3110"),
        store,
    )


def test_browse_sources_with_facets_and_filters(engine, client, tmp_path):
    _seed(engine, tmp_path)
    body = client.get("/l1/sources").json()
    assert body["total"] == 2
    assert body["facets"]["jurisdictions"] == ["XX"]
    assert set(body["facets"]["rights_bases"]) == {"licensed", "derived_only"}
    one = next(s for s in body["sources"] if s["key"] == "synthetic/prin")
    assert one["regulator"] == "Synthetic Regulator"
    assert one["instrument"] == "Synthetic Principles"
    assert one["rights"]["basis"] == "licensed" and one["rights"]["republish_text"] is True
    assert one["rights"]["licence"] == "open" and one["rights"]["licence_ref"]
    assert one["versions"] == 2 and one["latest_version"]["label"] == "consolidated:2026-06-01"

    assert client.get("/l1/sources", params={"rights_basis": "derived_only"}).json()["total"] == 1
    assert client.get("/l1/sources", params={"regulator": "synthetic"}).json()["total"] == 2
    assert client.get("/l1/sources", params={"instrument": "principles"}).json()["total"] == 2
    assert client.get("/l1/sources", params={"jurisdiction": "uk"}).json()["total"] == 0
    assert client.get("/l1/sources", params={"family": "synthetic-family"}).json()["total"] == 2
    assert client.get("/l1/sources", params={"q": "finra"}).json()["total"] == 1


def test_source_detail_versions_and_changes(engine, client, tmp_path):
    _seed(engine, tmp_path)
    detail = client.get("/l1/sources/synthetic/prin").json()
    assert detail["family"] == "synthetic-family" and detail["family_root"] is True
    assert detail["rights"]["history"][0]["rights_basis"] == "licensed"
    assert detail["memberships"][0]["relation"] == "root"
    assert detail["recent_changes"][0]["kind"] == "amended"
    assert detail["recent_changes"][0]["effective_date"] == "2027-01-01"
    assert "scorecard" in detail

    versions = client.get("/l1/sources/synthetic/prin/versions").json()["versions"]
    assert [v["label"] for v in versions] == ["consolidated:2026-01-01", "consolidated:2026-06-01"]
    assert all(v["clauses"] > 0 and v["content_hash"] for v in versions)

    changes = client.get("/l1/sources/synthetic/prin/changes").json()["changes"]
    assert [c["kind"] for c in changes] == ["amended", "added"]
    assert changes[0]["effective_date_basis"] == "text" and changes[0]["clause_ids"]

    assert client.get("/l1/sources/nope/none").status_code == 404


def test_change_feed_since_and_by_source(engine, client, tmp_path):
    _seed(engine, tmp_path)
    feed = client.get("/l1/changes").json()
    assert feed["count"] == 3
    only_future = client.get("/l1/changes", params={"since": "2027-01-01"}).json()
    assert [c["kind"] for c in only_future["changes"]] == ["amended"]
    assert client.get("/l1/changes", params={"source": "synthetic/finra-3110"}).json()["count"] == 1
    assert client.get("/l1/changes", params={"jurisdiction": "XX"}).json()["count"] == 3
    assert client.get("/l1/changes", params={"watched": "true"}).status_code == 401


def test_clause_detail_serves_spans_and_respects_rights(engine, client, tmp_path):
    _seed(engine, tmp_path)
    with engine.connect() as conn:
        def _clause_id(source_key: str, ref: str) -> int:
            src_id = conn.execute(sa.select(sources.c.id).where(sources.c.key == source_key)).scalar_one()
            return conn.execute(
                sa.select(clauses.c.id)
                .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
                .where(source_versions.c.source_id == src_id)
                .where(clauses.c.ref == ref)
                .order_by(clauses.c.id.desc())
                .limit(1)
            ).scalar_one()

        open_id = _clause_id("synthetic/prin", "2")
        derived_id = _clause_id("synthetic/finra-3110", "1")
    body = client.get(f"/l1/clauses/{open_id}").json()
    assert body["source"] == "synthetic/prin" and body["ref"] == "2"
    assert body["text"].startswith("A firm must pay due regard")
    assert body["span"]["start"] is not None and body["span"]["end"] > body["span"]["start"]
    assert body["normative"] is True
    assert body["why"]["derived_by"] and body["why"]["valid_from"] is None  # fixture has no publisher effective-date metadata

    derived = client.get(f"/l1/clauses/{derived_id}").json()
    assert derived["source"] == "synthetic/finra-3110"
    assert derived["text"] is None
    assert derived["rights_basis"] == "derived_only"
    assert "derived_only" in derived["text_withheld_reason"]
    assert derived["text_hash"] and derived["span"]["end"] is not None

    assert client.get("/l1/clauses/999999").status_code == 404


def test_families_and_scorecard(engine, client, tmp_path):
    _seed(engine, tmp_path)
    fams = client.get("/l1/families").json()
    assert fams["passed"] is True
    assert fams["families"][0]["family"] == "synthetic-family" and fams["families"][0]["members"] == 2

    tree = client.get("/l1/families/synthetic-family").json()
    assert tree["scorecard"]["completeness"] == 1.0
    assert client.get("/l1/families/none").status_code == 404

    card = client.get("/l1/scorecard").json()
    assert card["gate"]["layer"] == "L1"
    assert card["rights_mix"] == {"licensed": 1, "derived_only": 1}
    assert card["clauses"]["total"] > 0 and card["clauses"]["normative"] > 0
    assert card["starter_corpus"]["sources"] >= 55
    assert "l1_currency" in card["thresholds"] or card["thresholds"]


def test_watchlists_roundtrip(engine, client, tmp_path):
    _seed(engine, tmp_path)
    assert client.get("/l1/watchlists").status_code == 401
    headers = {"X-Watcher-Id": "acme-app"}

    assert client.post("/l1/watchlists", json={"source_key": "nope"}, headers=headers).status_code == 404
    created = client.post("/l1/watchlists", json={"source_key": "synthetic/prin"}, headers=headers)
    assert created.status_code == 201 and created.json()["watching"] is True

    mine = client.get("/l1/watchlists", headers=headers).json()
    assert mine["watcher"] == "app:acme-app"
    assert [w["source_key"] for w in mine["watching"]] == ["synthetic/prin"]
    assert client.get("/l1/sources/synthetic/prin").json()["watchers"] == 1

    watched = client.get("/l1/changes", params={"watched": "true"}, headers=headers).json()
    assert watched["count"] == 2 and all(c["source"] == "synthetic/prin" for c in watched["changes"])

    gone = client.post("/l1/watchlists/synthetic/prin/unwatch", headers=headers).json()
    assert gone["watching"] is False and gone["closed"] == 1
    assert client.get("/l1/watchlists", headers=headers).json()["watching"] == []
    assert client.get("/l1/changes", params={"watched": "true"}, headers=headers).json()["count"] == 0

    # Re-watch reopens the same row (unique per watcher/source), never deletes.
    assert client.post("/l1/watchlists", json={"source_key": "synthetic/prin"}, headers=headers).status_code == 201
    assert client.get("/l1/sources/synthetic/prin").json()["watchers"] == 1


def test_l1_browser_page_is_served(client):
    resp = client.get("/l1")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    for needle in ("Sources", "Changes", "source_version_id", "recordMatches", 'href="/l8"'):
        assert needle in resp.text
    assert "L1 Sources" not in resp.text
    redirected = client.get("/sources", follow_redirects=False)
    assert redirected.status_code == 307
    assert redirected.headers["location"] == "/l1"
    assert client.get("/sources", follow_redirects=True).text == resp.text
    # The JSON API under the same prefix still answers.
    assert client.get("/l1/sources").json()["total"] == 0
