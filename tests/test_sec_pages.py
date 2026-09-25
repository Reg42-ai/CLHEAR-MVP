"""SEC.gov publications through L1, and the demo's L7 and L8 on top of them."""
import pytest
import sqlalchemy as sa

from app.clhear.l1.adapters.base import Artifact
from app.clhear.l1.adapters.sec_pages import SecPageAdapter, is_sec_page, original_text, parse, verify

PAGE = """<html><head><title>SEC.gov | Sweep</title><script>var x = 1;</script></head><body>
<nav class="usa-sidenav"><ul><li>Newsroom</li><li>Press Releases</li></ul></nav>
<div class="node-details-layout__main-region">
<nav class="usa-breadcrumb"><ol><li>Home</li><li>Newsroom</li></ol></nav>
<div class="block block-core"><div class="page-title"><span class="eyebrow">Press Release</span>
<h1 class="page-title__heading">SEC Charges Nine Investment Advisers in Ongoing Sweep into Marketing Rule Violations</h1></div></div>
<div class="node-details-layout__main-region__content"><div class="clearfix text-formatted usa-prose">
<p>For Immediate Release</p><p>Washington D.C., Sept. 9, 2024 &mdash;</p>
<p>The Securities and Exchange Commission today announced settled charges against nine registered investment advisers
for violating the Marketing Rule. All nine firms have agreed to pay $1,240,000 in combined civil penalties.</p>
<ul><li>Howard Bailey Securities LLC agreed to pay a civil penalty of $90,000;</li></ul>
<p>It also advertised endorsements that did not disclose that the endorser was a paid, non-client in videos, on
social<em>media</em>, and on physical objects.</p>
<p>The firms consented to be censured, to cease and desist, and to comply with certain undertakings.</p>
</div></div></div>
<div class="node-details-layout__rightrail-region"><ul><li><a href="/files/ia-6681.pdf">Order - Howard Bailey</a></li></ul></div>
</body></html>""".encode()

KEY = "sec/enforcement/2024-121"


def test_publication_scope_excludes_site_chrome_and_both_readers_agree():
    from app.clhear.l1.adapters.publication_pages import publication_title

    assert is_sec_page(PAGE) and not is_sec_page(b"<html><body><ul><li>listing</li></ul></body></html>")
    tree = parse(PAGE, KEY, "SEC 2024-121")
    blocks = [n for n in tree[0].walk() if n.node_type == "provision"]
    assert publication_title(tree[0]).startswith("Press Release SEC Charges Nine")
    assert [n.ref for n in blocks] == [f"{KEY}/b{i}" for i in range(1, len(blocks) + 1)]
    items = [n for n in blocks if n.source_locator["block"] == "item"]
    assert [n.raw_text for n in items] == ["Howard Bailey Securities LLC agreed to pay a civil penalty of $90,000;"]
    text = " ".join(n.raw_text for n in blocks)
    for chrome in ("Newsroom", "Home", "Order - Howard Bailey", "var x"):
        assert chrome not in text
    assert "socialmedia" in text  # inline markup joins, as the page renders it
    assert original_text(PAGE) == f"{publication_title(tree[0])} {text}"
    artifacts = [Artifact(name="publication.html", content=PAGE, content_type="text/html")]
    assert verify(artifacts, KEY, tree)
    items[0].raw_text = items[0].raw_text.replace("$90,000", "$9,000")
    assert not verify(artifacts, KEY, tree)


def test_publication_verifies_through_original_projection():
    from app.clhear.l1.originals import attach_source_locations, verify_original_projection

    tree = parse(PAGE, KEY, "SEC 2024-121")
    artifacts = [Artifact(name="publication.html", content=PAGE, content_type="text/html")]
    assert attach_source_locations(KEY, "sec_enforcement", artifacts, tree)
    report = verify_original_projection(KEY, "sec_enforcement", artifacts, tree)
    assert report["verified"], report["findings"]


def test_landing_page_cannot_stand_in_for_the_publication(monkeypatch):
    from app.clhear.l1 import http

    monkeypatch.setattr(http, "get", lambda url, headers=None: b"<html><body><p>Moved</p></body></html>")
    adapter = SecPageAdapter(KEY, "SEC 2024-121", "https://www.sec.gov/newsroom/press-releases/2024-121",
                             meta=None, adapter="sec_enforcement")
    with pytest.raises(ValueError, match="SEC publication page"):
        adapter.fetch()


def test_sec_evidence_rides_existing_lanes_and_never_derives_obligations():
    from app.clhear.demo_corpus import ENFORCEMENT_SOURCE_KEYS, REFERENCE_SOURCE_KEYS
    from app.clhear.l1 import publishers
    from app.clhear.l1.fleet import adapter_for
    from app.clhear.l1.source_registry import S

    rows = {row["key"]: row for row in S}
    for key in ENFORCEMENT_SOURCE_KEYS:
        adapter = adapter_for(rows[key])
        assert isinstance(adapter, SecPageAdapter) and adapter.key == "sec_enforcement"
        assert rows[key]["kind"] == "enforcement" and rows[key]["tier"] == "guidance"
        assert rows[key]["canonical_url"].startswith("https://www.sec.gov/newsroom/press-releases/")
        assert publishers.publisher_ids(rows[key]) == ["sec"]
    alert = rows[REFERENCE_SOURCE_KEYS[0]]
    assert isinstance(adapter_for(alert), SecPageAdapter) and adapter_for(alert).key == "sec_edgar"
    assert alert["kind"] == "guidance" and alert["tier"] == "guidance"


def test_sweep_release_yields_one_fine_per_charged_firm():
    from app.clhear.l1.adapters.publication_pages import publication_title
    from app.clhear.l7.enforcement import _publication_outcomes

    tree = parse(PAGE, KEY, "SEC 2024-121")
    rows = [{"id": i, "ref": n.ref, "text": n.raw_text, "text_hash": f"h{i}", "source_key": KEY, "source_name": "SEC 2024-121",
             "issuer": "U.S. Securities and Exchange Commission", "jurisdiction": "US", "canonical_url": "",
             "source_version_id": 1, "block": n.source_locator["block"], "publication_title": publication_title(tree[0])}
            for i, n in enumerate((n for n in tree[0].walk() if n.node_type == "provision"), 1)]
    [outcome] = _publication_outcomes(rows, [])
    notice = outcome["parsed"]
    assert notice["respondent"] == "Howard Bailey Securities LLC" and notice["respondent_type"] == "firm"
    assert notice["kind"] == "fine" and notice["amount"] == 90000 and str(notice["decided_on"]) == "2024-09-09"
    assert outcome["row"]["block"] == "item"


def test_undertaking_without_a_civil_penalty_stays_an_undertaking():
    from app.clhear.l7.enforcement import parse_notice

    notice = parse_notice("Voluntary requirement", "The firm gave an undertaking to restrict new business.", regulator="FCA")
    assert notice["kind"] == "undertaking"


def _in_force(engine, key, text):
    from app.clhear.l1.models import clauses, source_versions, sources

    with engine.begin() as conn:
        source_id = conn.execute(sa.select(sources.c.id).where(sources.c.key == key)).scalar_one()
        version_id = conn.execute(source_versions.insert().values(
            source_id=source_id, version_label="as_published:test", version_kind="as_published",
            content_hash="sha256:" + key, s3_uri="s3://test/" + key, status="in_force")).inserted_primary_key[0]
        conn.execute(clauses.insert().values(
            source_version_id=version_id, ref=f"{key}/publication", path="1", ordering=1,
            text=text, text_hash="h-" + key, public_ok=True))


RISK_ALERT = ("Initial Observations Regarding Advisers Act Marketing Rule Compliance\nThe Risk Alert details observations "
              "of investment adviser compliance with (1) the Compliance Rule, (2) the Books and Records Rule, and (3) the "
              "General Prohibitions. Deficiencies related to (1) untrue statements of material fact and unsubstantiated "
              "statements of material fact; (2) omission of material facts or misleading inference.")


def test_l8_reference_rows_quote_the_in_force_clause_and_are_not_peer_data(client, engine):
    from app.clhear.l1.source_registry import seed
    from app.clhear.l8.cohorts import K
    from app.clhear.l8.reference import reference_rows

    seed(engine)
    assert reference_rows(engine) == []
    _in_force(engine, "sec/exams/risk-alert-041724", RISK_ALERT)
    rows = {row["id"]: row for row in reference_rows(engine)}
    assert set(rows) == {"REF-SEC-EXAMS-2024-01", "REF-SEC-EXAMS-2024-02", "REF-SEC-EXAMS-2024-06", "REF-SEC-EXAMS-2024-07"}
    for row in rows.values():
        assert row["quote"] in RISK_ALERT and row["peer_data"] is False
        assert row["source"]["clause_ref"] == "sec/exams/risk-alert-041724/publication"
    assert rows["REF-SEC-EXAMS-2024-01"]["block_id"] == "BLK-MKT-CLAIMS-REVIEW"
    assert rows["REF-SEC-EXAMS-2024-07"]["block_id"] is None

    auth = {"Authorization": "Bearer dev-os-key", "X-App-Id": "os-dev"}
    body = client.get("/v1/releases/clhear-vLIVE/L8/benchmarks", headers=auth)
    assert body.status_code == 200, body.text
    body = body.json()
    assert body["layer_status"] == "reference" and body["banner"]["data_status"] == "reference"
    assert "not peer data" in body["view"] and len(body["items"]) == 4
    assert body["peer_aggregates"] == {"status": "locked", "k_threshold": K}


def test_scores_can_be_limited_to_the_demo_obligations(engine):
    from app.clhear.l7.score import score_obligations
    from tests.test_influencer_demo import _seed_clauses
    from app.clhear.demo_corpus import LEGAL_SOURCE_KEYS, derive_demo

    _seed_clauses(engine)
    derive_demo(engine)
    assert score_obligations(engine, source_keys=("cfr/16/255",))["obligations"] == 5
    assert score_obligations(engine, source_keys=LEGAL_SOURCE_KEYS)["obligations"] == 7
