"""HLD v2 §4.7 — L7 enforcement ingestion, linking and calibrated scoring.

Enforcement notices are L1 sources (kind ``enforcement``, informative tier — L2
never extracts obligations from them). The ingestor derives one event per
notice; the linker turns printed citations into event → obligation links; the
scorer publishes weights, bands and a held-out-year Brier score. Everything is
idempotent, re-derived on change and superseded rather than deleted.
"""
from __future__ import annotations

import json
from datetime import date

import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.clhear.derived_models import obligations
from app.clhear.l1 import pipeline
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta, flatten
from app.clhear.l1.adapters.enforcement import FcaFinalNoticesAdapter, SecEnforcementAdapter
from app.clhear.l1.models import CLAUSE_TYPES, FLEET_SCHEDULES, family_members, sources
from app.clhear.l1 import fidelity, rights
from app.clhear.l6 import composer
from app.clhear.l7 import enforcement as enf
from app.clhear.l7 import score as l7_score
from app.clhear.l7.models import BANDS, DIMENSIONS, WEIGHTS, band_for, enforcement_events, enforcement_links, risk_calibrations, risk_scores
from app.clhear.platform import evals as ev
from app.clhear.platform.gateway import FakeProvider, Gateway
from app.clhear.platform.router import TASKS
from tests.test_l1_synthetic_amendment import SyntheticAdapter
from tests.test_l6_blueprints import BROKER, SOURCE, _corpus

ENF_SOURCE = "synthetic/final-notices"

NOTICES = {
    "fn-2021-010": "Gamma Ltd\nOn 5 May 2021 the regulator fined Gamma Ltd £400,000 for breaching rule 1 of the Synthetic Principles.",
    "fn-2021-011": "Delta LLP\n20 November 2021: Delta LLP was fined £150,000 for breaches of SYN PRIN 2 and rule 1 of the Synthetic Principles.",
    "fn-2022-012": "Epsilon plc\nOn 9 September 2022 the regulator fined Epsilon plc £900,000 for breaching rule 1 of the Synthetic Principles.",
    "fn-2022-003": "Beta Bank plc\nOn 1 February 2022 the regulator publicly censured Beta Bank plc for failings under rule 2 of the Synthetic Principles.",
    "fn-2023-013": "Zeta Markets Ltd\n14 June 2023: fine of £1,200,000 on Zeta Markets Ltd for breaching SYN PRIN 1.",
    "fn-2023-002": "Mr John Smith\n3 July 2023: prohibition order against Mr John Smith, a former director, for breaches of Synthetic Principles 3.",
    "fn-2024-001": "Acme Capital Ltd\nFinal notice dated 12 March 2024. The FCA fined Acme Capital Ltd £2,500,000 for breaching rule 1 of the Synthetic Principles and SYN PRIN 2.",
    "fn-2024-004": "Theta Payments Ltd\n2 October 2024: Theta Payments Ltd agreed a voluntary requirement (VREQ); no provision is named in the summary.",
}


class EnforcementAdapter(SyntheticAdapter):
    """A synthetic enforcement listing: one provision per notice, title as heading."""

    key = "synthetic_enforcement"

    def meta(self) -> SourceMeta:
        base = super().meta()
        return SourceMeta(**{**base.__dict__, "source_key": self.source_key, "name": "Synthetic final notices", "kind": "enforcement",
                             "short_name": "SYN FN", "instrument": "Synthetic final notices", "adapter": "synthetic_enforcement"})

    def fetch(self, since_version=None):
        body = "\n".join(f"{ref} {text}" for ref, text in self.provisions.items())
        tree = [DocNode(node_type="section", ref="s1", label="Section 1", raw_text="", children=[
            DocNode(node_type="provision", ref=ref, label=text.split("\n")[0], heading=text.split("\n")[0], raw_text=text.split("\n", 1)[1])
            for ref, text in self.provisions.items()])]
        return FetchResult(version_label=f"consolidated:{self.version}",
                           artifacts=[Artifact(name="doc.txt", content=body.encode(), content_type="text/plain")], tree=tree)

    def expected_text(self, artifacts):
        return ["Section 1"] + [p for t in self.provisions.values() for p in t.split("\n")]


def _ingest_notices(engine, tmp_path, notices=NOTICES, version="2026-01-01"):
    store = pipeline.LocalStore(tmp_path / "lake")
    return pipeline.ingest(engine, EnforcementAdapter(notices, version, source_key=ENF_SOURCE), store, gateway=Gateway(engine, FakeProvider()))


def _stack(engine, tmp_path):
    _corpus(engine, tmp_path)
    _ingest_notices(engine, tmp_path)
    enf.ingest_events(engine)
    enf.link_events(engine)


def _live_events(conn):
    return [dict(r) for r in conn.execute(sa.select(enforcement_events).where(enforcement_events.c.valid_to.is_(None))).mappings()]


def _live_links(conn):
    return [dict(r) for r in conn.execute(sa.select(enforcement_links).where(enforcement_links.c.valid_to.is_(None))).mappings()]


# ------------------------------------------------------------------ L1 enforcement sources


def test_enforcement_adapters_parse_listings_and_feeds_verbatim():
    html = ("<html><head><title>Final notices</title></head><body><nav><a href='/'>Home</a></nav><h1>Final notices</h1><ul>"
            "<li class='search-item'><a href='/publication/final-notices/acme-2024.pdf'>Acme Capital Ltd</a><p>12 March 2024</p>"
            "<p>Fined £2,500,000 for breaching SYSC 6.1.1R.</p></li>"
            "<li class='search-item'><a href='/publication/final-notices/smith-2023.pdf'>Mr John Smith</a><p>3 July 2023</p><p>Prohibition order.</p></li>"
            "</ul><footer>© FCA</footer></body></html>").encode()
    fca = FcaFinalNoticesAdapter(source_key="fca/final-notices", title="FCA final notices", url="https://www.fca.org.uk/x")
    tree = fca.parse(html)
    report = fidelity.check(tree, fca.expected_text([Artifact(name="p.html", content=html, content_type="text/html")]))
    assert report.violations == [] and report.coverage >= 0.995
    provisions = {n.ref: n for n in flatten(tree) if n.node_type == "provision"}
    assert set(provisions) == {"acme-2024", "smith-2023"}
    assert provisions["acme-2024"].heading == "Acme Capital Ltd" and "SYSC 6.1.1R" in provisions["acme-2024"].raw_text
    assert provisions["acme-2024"].subtree_text().startswith("Acme Capital Ltd\n")  # title lands in the clause projection
    assert fca.meta().kind == "enforcement" and fca.meta().rights_basis == "licensed"

    rss = ("<?xml version='1.0'?><rss><channel><title>SEC Litigation Releases</title>"
           "<item><title>SEC v. Acme Advisors LLC</title><link>https://www.sec.gov/litigation/litreleases/lr-26012</link>"
           "<description>Lit. Rel. No. 26012. Violations of Exchange Act Rule 15l-1; $1,200,000 civil penalty.</description></item>"
           "</channel></rss>").encode()
    sec = SecEnforcementAdapter(source_key="sec/litigation-releases", title="SEC", url="https://www.sec.gov/x")
    tree = sec.parse(rss)
    report = fidelity.check(tree, sec.expected_text([Artifact(name="feed.xml", content=rss, content_type="application/rss+xml")]))
    assert report.violations == [] and report.coverage >= 0.995
    refs = {n.ref for n in flatten(tree) if n.node_type in CLAUSE_TYPES}
    assert "lr-26012" in refs
    assert sec.meta().rights_basis == "public_domain"


def test_enforcement_sources_are_scheduled_with_rights_and_sit_in_the_informative_tier(engine, tmp_path):
    for key in ("fca_enforcement", "sec_enforcement", "finra_enforcement"):
        assert key in FLEET_SCHEDULES
        assert rights.rights_for(key).basis in {"licensed", "public_domain", "derived_only"}
    assert rights.rights_for("finra_enforcement").basis == "derived_only"
    _corpus(engine, tmp_path)
    _ingest_notices(engine, tmp_path)
    with engine.connect() as conn:
        row = conn.execute(sa.select(sources.c.kind, family_members.c.tier).join(family_members, family_members.c.source_id == sources.c.id)
                           .where(sources.c.key == ENF_SOURCE)).first()
        assert row is not None and row.kind == "enforcement" and row.tier == "informative"
        # L2 never derives obligations from notices (I1: enforcement is L7 evidence, not law)
        assert conn.execute(sa.select(sa.func.count()).select_from(obligations).where(obligations.c.source_key == ENF_SOURCE)).scalar_one() == 0


# ------------------------------------------------------------------ ingestor


def test_ingestor_reads_one_event_per_notice_with_only_what_the_notice_prints(engine, tmp_path):
    _corpus(engine, tmp_path)
    _ingest_notices(engine, tmp_path)
    stats = enf.ingest_events(engine)
    assert stats["inserted"] == len(NOTICES) and stats["clauses"] == len(NOTICES)
    with engine.connect() as conn:
        by_ref = {e["clause_ref"]: e for e in _live_events(conn)}
    acme = by_ref["fn-2024-001"]
    assert acme["id"].startswith("ENF-") and acme["version"] == 1
    assert acme["respondent"] == "Acme Capital Ltd" and acme["respondent_type"] == "firm"
    assert acme["decided_on"] == date(2024, 3, 12) and float(acme["amount"]) == 2_500_000 and acme["currency"] == "GBP"
    assert acme["kind"] == "fine" and acme["regulator"] == "Synthetic Regulator" and acme["jurisdiction"] == "XX"
    cited = acme["cited_refs"] if isinstance(acme["cited_refs"], list) else json.loads(acme["cited_refs"])
    assert [(c["instrument"], c["ref"]) for c in cited] == [("Synthetic Principles", "1"), ("SYN PRIN", "2")]
    assert all(c["source_keys"] == [SOURCE] for c in cited)
    smith = by_ref["fn-2023-002"]
    assert smith["kind"] == "prohibition" and smith["respondent_type"] == "individual" and smith["amount"] is None
    assert by_ref["fn-2022-003"]["kind"] == "censure"
    assert by_ref["fn-2024-004"]["kind"] == "undertaking" and by_ref["fn-2024-004"]["cited_refs"] in ([], "[]")
    assert acme["why_trail_id"] and acme["text_hash"]
    # idempotent
    again = enf.ingest_events(engine)
    assert again["inserted"] == 0 and again["re_derived"] == 0 and again["unchanged"] == len(NOTICES)


def test_changed_notice_is_re_versioned_and_vanished_notice_invalidated_never_deleted(engine, tmp_path):
    _stack(engine, tmp_path)
    with engine.connect() as conn:
        before = {e["clause_ref"]: e for e in _live_events(conn)}
    amended = dict(NOTICES)
    amended["fn-2024-001"] = "Acme Capital Ltd\nFinal notice dated 12 March 2024. The FCA fined Acme Capital Ltd £3,000,000 for breaching rule 1 of the Synthetic Principles."
    del amended["fn-2022-003"]
    _ingest_notices(engine, tmp_path, amended, version="2026-02-01")
    stats = enf.ingest_events(engine)
    assert stats["re_derived"] == 1 and stats["invalidated"] == 1 and stats["inserted"] == 0
    acme_id = before["fn-2024-001"]["id"]
    with engine.connect() as conn:
        versions = conn.execute(sa.select(enforcement_events.c.version, enforcement_events.c.amount, enforcement_events.c.valid_to)
                                .where(enforcement_events.c.id == acme_id).order_by(enforcement_events.c.version)).all()
        assert [(v.version, float(v.amount), v.valid_to is None) for v in versions] == [(2, 2_500_000.0, False), (3, 3_000_000.0, True)]
        beta = conn.execute(sa.select(enforcement_events).where(enforcement_events.c.id == before["fn-2022-003"]["id"])).mappings().all()
        assert len(beta) == 1 and beta[0]["valid_to"] is not None
        review = beta[0]["review"] if isinstance(beta[0]["review"], list) else json.loads(beta[0]["review"])
        assert review[-1]["reason"] == "notice vanished from the source"
    link_stats = enf.link_events(engine)
    with engine.connect() as conn:
        live = {(l["event_id"], l["obligation_id"]) for l in _live_links(conn)}
    # the amended notice no longer cites SYN PRIN 2 → that link closed; the vanished notice's link closed with it
    assert (acme_id, f"OBL:{SOURCE}#2") not in live and (acme_id, f"OBL:{SOURCE}#1") in live
    assert not any(eid == before["fn-2022-003"]["id"] for eid, _ in live)
    assert link_stats["closed_links"] >= 1


# ------------------------------------------------------------------ linker


def test_linker_resolves_printed_citations_to_obligations_with_the_quote(engine, tmp_path):
    _corpus(engine, tmp_path)
    _ingest_notices(engine, tmp_path)
    enf.ingest_events(engine)
    stats = enf.link_events(engine)
    assert stats["events"] == len(NOTICES) and stats["linked_events"] == len(NOTICES) - 1  # the VREQ names no provision
    assert stats["unresolved_citations"] == 0 and stats["llm_links"] == 0
    with engine.connect() as conn:
        links = _live_links(conn)
        events = {e["id"]: e for e in _live_events(conn)}
        acme = next(e for e in events.values() if e["clause_ref"] == "fn-2024-001")
        mine = sorted((l["obligation_id"], l["method"], l["citation"]) for l in links if l["event_id"] == acme["id"])
        assert mine == [(f"OBL:{SOURCE}#1", "citation", "rule 1 of the Synthetic Principles"), (f"OBL:{SOURCE}#2", "citation", "SYN PRIN 2")]
        assert all(float(l["confidence"]) >= 0.85 and l["why_trail_id"] for l in links)
        who = enf.events_for_obligation(conn, f"OBL:{SOURCE}#2")
        assert {e["respondent"] for e in who} == {"Acme Capital Ltd", "Delta LLP", "Beta Bank plc"}
        assert all(e["link"]["obligation_id"] == f"OBL:{SOURCE}#2" for e in who)
    again = enf.link_events(engine)
    assert again["new_links"] == 0 and again["links"] == stats["links"]


def test_linker_precision_gate_meets_ninety_percent_on_golden_notices(engine):
    run = ev.run_suite(engine, "l7_linker")
    assert run["passed"] is True
    assert run["scores"]["precision"] >= 0.90 and run["scores"]["cases"] >= 10
    assert run["scores"]["dangling"] == 0 and run["scores"]["uncited"] == 0


def test_link_text_never_links_without_a_printed_citation():
    index = enf.index_from_rows([{"source_key": "fca/handbook/SYSC", "clause_ref": "SYSC 6.1.1"}, {"source_key": "uksi/2017/692", "clause_ref": "regulation-28"}])
    assert enf.link_text("Fined £2.5 million on 12 March 2024 for AML failings.", index) == []
    hits = enf.link_text("breached SYSC 6.1.1R and regulation 28(2) of the MLRs 2017", index)
    assert [(h["clause_ref"], h["method"]) for h in hits] == [("SYSC 6.1.1", "citation"), ("regulation-28", "citation")]
    assert hits[0]["confidence"] > hits[1]["confidence"]  # exact clause beats sub-paragraph-of-provision


def test_llm_fallback_is_closed_world_and_quote_bound(engine, tmp_path, monkeypatch):
    from app.clhear.platform import router as router_mod

    _corpus(engine, tmp_path)
    _ingest_notices(engine, tmp_path, {"fn-x": "Kappa Ltd\n9 January 2024: Kappa Ltd fined £50,000 for failing to reconcile client money records daily."})
    enf.ingest_events(engine)
    with engine.connect() as conn:
        target = conn.execute(sa.select(obligations.c.id).where(obligations.c.source_key == SOURCE, obligations.c.clause_ref == "1")).scalar_one()
        other = conn.execute(sa.select(obligations.c.id).where(obligations.c.source_key == SOURCE, obligations.c.clause_ref == "3")).scalar_one()

    class _Result:
        def __init__(self, text):
            self.text = text

    answers = iter([json.dumps({"links": [
        {"obligation_id": target, "quote": "failing to reconcile client money records daily"},
        {"obligation_id": other, "quote": "this sentence is not in the notice"},
        {"obligation_id": "OBL:not/in#menu", "quote": "Kappa Ltd"},
    ]})])
    monkeypatch.setattr(router_mod, "complete", lambda llm, task_id, **kw: _Result(next(answers)))
    assert TASKS["l7.link"].task_class == "l7_score" and TASKS["l7.link"].layer == "L7"
    stats = enf.link_events(engine, llm=object())
    assert stats["llm_links"] == 1
    with engine.connect() as conn:
        links = _live_links(conn)
    assert [(l["obligation_id"], l["method"]) for l in links] == [(target, "llm")]
    assert links[0]["citation"] == "failing to reconcile client money records daily"


# ------------------------------------------------------------------ scorer


def test_weights_and_bands_are_published_and_sum_to_one(client):
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9 and set(DIMENSIONS) == set(WEIGHTS)
    assert band_for(0.9) == "critical" and band_for(0.6) == "high" and band_for(0.4) == "medium" and band_for(0.1) == "low"
    method = client.get("/l7/method").json()
    assert method["weights"] == WEIGHTS and [b["band"] for b in method["bands"]] == [b for b, _ in BANDS]
    assert {d["key"] for d in method["dimensions"]} == set(DIMENSIONS) and all(d["note"] for d in method["dimensions"])
    assert "sigmoid" in method["likelihood"] and method["openness"]["open"]


def test_calibration_publishes_brier_on_held_out_year_and_beats_the_base_rate(engine, tmp_path):
    _stack(engine, tmp_path)
    cal = l7_score.calibrate(engine)
    assert cal["status"] == "calibrated" and cal["held_out_year"] == 2024 and cal["training_years"] == [2022, 2023]
    assert cal["fitted"] is True and cal["n"] == 5 and cal["positives"] == 2
    assert cal["brier"] is not None and cal["baseline_brier"] is not None and cal["brier"] <= cal["baseline_brier"]
    with engine.connect() as conn:
        stored = l7_score.latest_calibration(conn)
    assert stored["id"] == cal["id"] and stored["published"] is True and stored["reliability"]
    run = ev.run_suite(engine, "l7_brier")
    assert run["passed"] is True and run["scores"]["brier"] == cal["brier"] and run["scores"]["beats_baseline"] is True
    # a second run is appended, never overwrites
    l7_score.calibrate(engine, held_out_year=2023)
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(risk_calibrations)).scalar_one() == 2


def test_brier_gate_fails_until_a_calibration_is_published(engine):
    run = ev.run_suite(engine, "l7_brier")
    assert run["passed"] is False and run["scores"]["published"] is False


def test_scores_carry_dimensions_weights_calibration_and_evidence_and_supersede_on_change(engine, tmp_path):
    _stack(engine, tmp_path)
    cal = l7_score.calibrate(engine)
    stats = l7_score.score_obligations(engine)
    assert stats["written"] == 5 and stats["with_events"] == 3 and stats["calibration"] == cal["id"]
    with engine.connect() as conn:
        scores = {s["subject_ref"]: s for s in l7_score.list_scores(conn, kind="obligation")}
    one, two, four = scores[f"OBL:{SOURCE}#1"], scores[f"OBL:{SOURCE}#2"], scores[f"OBL:{SOURCE}#4"]
    assert one["id"].startswith("RSK-") and one["weights"] == WEIGHTS and one["method_version"] == "risk-v2"
    assert set(one["dimensions"]) == set(DIMENSIONS) and all(0.0 <= v <= 1.0 for v in one["dimensions"].values())
    assert one["composite"] == round(sum(WEIGHTS[d] * one["dimensions"][d] for d in DIMENSIONS), 4)
    assert one["band"] == band_for(one["composite"]) and one["calibration_set_ref"] == cal["id"]
    assert one["composite"] > two["composite"] > four["composite"]  # five fines > two outcomes > nothing
    assert one["evidence"]["event_count"] == 5 and one["evidence"]["total_amount"] == 5_150_000.0 and len(one["evidence"]["events"]) == 5
    assert one["dimensions"]["enforcement_history"] == 1.0 and one["dimensions"]["likelihood"] > four["dimensions"]["likelihood"]
    assert scores[f"OBL:{SOURCE}#3"]["dimensions"]["reputational_impact"] == 1.0  # the prohibition of an individual
    assert four["evidence"]["event_count"] == 0 and four["band"] == "low"
    # unchanged inputs → nothing rewritten; changed inputs → supersede, never overwrite (I2)
    again = l7_score.score_obligations(engine)
    assert again["written"] == 0 and again["unchanged"] == 5
    _ingest_notices(engine, tmp_path, {**NOTICES, "fn-2025-020": "Iota Ltd\n5 May 2025: Iota Ltd fined £9,000,000 for breaching Synthetic Principles 4."}, version="2026-03-01")
    enf.ingest_events(engine)
    enf.link_events(engine)
    rescored = l7_score.score_obligations(engine)
    assert rescored["superseded"] >= 1 and rescored["written"] == rescored["superseded"]
    with engine.connect() as conn:
        history = l7_score.list_scores(conn, kind="obligation", subject_ref=f"OBL:{SOURCE}#4", include_history=True)
        current = [h for h in history if h["status"] == "current"]
    assert len(history) == 2 and len(current) == 1 and current[0]["evidence"]["event_count"] == 1 and current[0]["composite"] > four["composite"]
    assert next(h for h in history if h["status"] == "superseded")["valid_to"] is not None


def test_item_scores_rank_blueprint_items_by_their_riskiest_obligation(engine, tmp_path):
    _stack(engine, tmp_path)
    l7_score.calibrate(engine)
    l7_score.score_obligations(engine)
    bp = composer.compose(engine, {"attributes": BROKER}, requested_by="test")
    stats = l7_score.score_items(engine)
    assert stats["items"] >= 1 and stats["written"] == stats["items"]
    with engine.connect() as conn:
        items = l7_score.list_scores(conn, kind="item", blueprint_id=bp["blueprint_id"])
        ob_scores = {s["subject_ref"]: s for s in l7_score.list_scores(conn, kind="obligation")}
    assert items and items == sorted(items, key=lambda s: -s["composite"])
    for it in items:
        assert it["subject_ref"].startswith("ITM-") and it["blueprint_id"] == bp["blueprint_id"]
        for d in DIMENSIONS:
            assert it["dimensions"][d] == max(ob_scores[o]["dimensions"][d] for o in it["evidence"]["obligations"])
        assert it["composite"] == round(sum(WEIGHTS[d] * it["dimensions"][d] for d in DIMENSIONS), 4)
    assert l7_score.score_items(engine)["written"] == 0


# ------------------------------------------------------------------ API + UI


def test_l7_api_serves_scores_enforcement_priorities_method_and_page(engine, tmp_path, client: TestClient):
    _stack(engine, tmp_path)
    l7_score.calibrate(engine)
    l7_score.score_obligations(engine)
    bp = composer.compose(engine, {"attributes": BROKER}, requested_by="test")
    l7_score.score_items(engine)

    scores = client.get("/l7/scores", params={"kind": "obligation"}).json()
    assert scores["count"] == 5 and scores["items"][0]["obligation"]["clause_ref"] == "1"
    one = client.get("/l7/scores", params={"obligation": "OBL-000001"}).json()
    assert one["count"] == 1 and one["items"][0]["subject_ref"] == f"OBL:{SOURCE}#1"
    assert client.get("/l7/scores", params={"obligation": "OBL-999999"}).status_code == 404
    assert client.get("/l7/scores", params={"band": "extreme"}).status_code == 422
    detail = client.get(f"/l7/scores/{one['items'][0]['id']}").json()
    assert detail["why"] and detail["calibration"]["held_out_year"] == 2024 and len(detail["enforcement"]) == 5 and detail["history"]
    assert client.get("/l7/scores/RSK-999999").status_code == 404

    events = client.get("/l7/enforcement", params={"kind": "fine"}).json()
    assert events["count"] == 5 and events["total_amount"] == 5_150_000.0
    assert all(e["links"] for e in events["items"]) and events["items"][0]["links"][0]["obligation"]["source_key"] == SOURCE
    assert client.get("/l7/enforcement", params={"regulator": "synthetic", "since": "2024-01-01"}).json()["count"] == 2
    assert client.get("/l7/enforcement", params={"q": "john smith"}).json()["items"][0]["kind"] == "prohibition"
    assert client.get("/l7/enforcement", params={"kind": "bogus"}).status_code == 422
    ev_id = events["items"][0]["id"]
    ev_detail = client.get(f"/l7/enforcement/{ev_id}").json()
    assert ev_detail["versions"] and ev_detail["why"] and ev_detail["links"][0]["citation"]
    assert client.get("/l7/enforcement/ENF-999999").status_code == 404

    who = client.get(f"/l7/obligations/OBL:{SOURCE}%232/enforcement").json()
    assert who["count"] == 3 and who["total_amount"] == 2_650_000.0 and who["regulators"] == ["Synthetic Regulator"]
    assert client.get("/l7/obligations/OBL-000002/score").json()["score"]["subject_ref"] == f"OBL:{SOURCE}#2"

    pri = client.get(f"/l7/blueprints/{bp['blueprint_id']}/priorities").json()
    assert pri["count"] >= 1 and pri["items"] == sorted(pri["items"], key=lambda s: -s["composite"]) and pri["unscored_items"] == []
    assert pri["items"][0]["item"]["name"] and pri["items"][0]["obligations"][0]["card"]
    assert client.get("/l7/blueprints/BLU-999999/priorities").status_code == 404

    cals = client.get("/l7/calibrations").json()
    assert cals["count"] == 1 and cals["items"][0]["brier"] is not None
    ev.run_suite(engine, "l7_linker"); ev.run_suite(engine, "l7_brier"); ev.run_suite(engine, "l7_number_echo")
    card = client.get("/l7/scorecard").json()
    assert card["gate"]["passed"] is True and card["events"] == len(NOTICES) and card["bands"]["obligation"] and card["weights"] == WEIGHTS

    page = client.get("/l7")
    assert page.status_code == 200 and 'id="main"' in page.text and "Enforcement explorer" in page.text and 'href="/l6"' in page.text
    assert 'href="/l7"' in client.get("/l6").text
