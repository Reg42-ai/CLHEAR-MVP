"""HLD v2 §4.4 done-tests for the NYC L4 profile space.

Register-backed ontology (jurisdictions -> authorisations -> permitted
products -> client types -> channels) built idempotently from the reviewed
snapshot with why-trails; validity rules that make impossible permutations
detectable; the validator (schema, closed-world values, authorisation
jurisdiction, permits, rules); the guided builder that only offers valid
options; the permutation explorer; profile storage (fingerprinted, invalid
never stored as valid) and re-validation after an ontology change; deterministic
applicability predicates with L2 change propagation through the worker; the
grounded LLM applicability read; the L4 gate suites; L6 composer triggering via
applies_to; the migration on a legacy store; the /l4 API and browser."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.clhear.derived_models import applies_to, licences, obligations, permits, profiles, validity_rules
from app.clhear.l1 import pipeline
from app.clhear.l2.extract import run_extraction
from app.clhear.l4 import builder as l4_builder
from app.clhear.l4 import ontology as l4_ontology
from app.clhear.l4 import predicates as l4_predicates
from app.clhear.l4 import validate as l4_validate
from app.clhear.l4.ontology import matches
from app.clhear.models import events as events_t
from app.clhear.platform import record
from app.clhear.platform.evals import run_suite
from app.clhear.platform.gateway import FakeProvider, Gateway
from app.clhear.platform.router import Router
from tests.test_l1_synthetic_amendment import SyntheticAdapter

SOURCE = "synthetic/uk-cass"
UK = {
    "1": "A firm that holds client money must reconcile its client money records at least once every business day.",
    "2": "An electronic money institution must safeguard funds received in exchange for electronic money that has been issued.",
    "3": "A firm must retain records required by this chapter for at least five years.",
    "4": "Where the framework contract is concluded at a distance, a payment service provider must provide the information in a durable medium.",
    "5": "This chapter applies to every firm.",
}
EMI = {
    "jurisdictions": ["UK"],
    "authorisations": ["E-money institution (Electronic Money Regulations 2011)"],
    "products": ["e-money issuance", "payment accounts"],
    "customer_base": ["retail"],
    "channels": ["online and mobile app"],
    "data_footprint": "large-scale personal data",
}
BROKER = {
    "jurisdictions": ["UK"],
    "authorisations": ["UK MiFID investment firm (Part 4A permission)", "Dealing in investments as agent", "Client money permission (CASS)"],
    "products": ["equities brokerage", "client money holding"],
    "customer_base": ["retail", "professional"],
}


class UKAdapter(SyntheticAdapter):
    def meta(self):
        return dataclasses.replace(super().meta(), jurisdiction="UK", family_key="synthetic-uk", family_name="Synthetic UK")


def _ingest(engine, tmp_path, provisions=UK, version="2026-01-01", source_key=SOURCE):
    store = pipeline.LocalStore(tmp_path / "lake")
    return pipeline.ingest(engine, UKAdapter(provisions, version, source_key=source_key), store, gateway=Gateway(engine, FakeProvider()))


def _live(conn, source_key=SOURCE):
    return {r["clause_ref"]: dict(r) for r in conn.execute(
        sa.select(obligations).where(obligations.c.source_key == source_key, obligations.c.status.in_(("derived", "validated")))).mappings()}


def _edges(conn, obligation_id, live=True):
    q = sa.select(applies_to).where(applies_to.c.obligation_id == obligation_id)
    if live:
        q = q.where(applies_to.c.valid_to.is_(None))
    return [dict(r) for r in conn.execute(q.order_by(applies_to.c.id)).mappings()]


def _router(engine, answers: list[dict]) -> Router:
    queue = [json.dumps(a) for a in answers]
    provider = FakeProvider(script=lambda **_: queue.pop(0) if queue else "{}")
    return Router(engine, providers={provider.name: provider})


# --------------------------------------------------------------------------- predicate language


def test_predicate_language_shared_with_l5_l6():
    attrs = {"jurisdictions": ["UK"], "authorisations": ["CASP (MiCA)"], "crypto_services": True, "data_footprint": "large"}
    assert matches({"jurisdictions": "uk"}, attrs)  # case-insensitive containment
    assert matches({"authorisations": ["CASP (MiCA)", "Payment institution (PSD2)"]}, attrs)  # any-of
    assert not matches({"authorisations": "Payment institution (PSD2)"}, attrs)
    assert matches({"crypto_services": True}, attrs) and not matches({"crypto_services": False}, attrs)
    assert matches({"data_footprint": "*"}, attrs) and not matches({"channels": "*"}, attrs)
    assert matches({}, attrs)
    from app.clhear.l6.composer import when_matches

    assert when_matches({"jurisdictions": "UK", "crypto_services": True}, attrs)


# --------------------------------------------------------------------------- ontology build


def test_ontology_built_from_register_snapshot_with_provenance_and_why(engine):
    snap = l4_ontology.snapshot()
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(sa.select(licences).where(licences.c.valid_to.is_(None))).mappings()]
        n_permits = conn.execute(sa.select(sa.func.count()).select_from(permits).where(permits.c.valid_to.is_(None))).scalar_one()
        n_rules = conn.execute(sa.select(sa.func.count()).select_from(validity_rules).where(validity_rules.c.valid_to.is_(None))).scalar_one()
    assert len(rows) == len(snap["licences"]) and n_permits == len(snap["permits"]) and n_rules == len(snap["validity_rules"])
    assert {r["jurisdiction"] for r in rows} == {"UK", "EU", "US"}
    for r in rows:  # every authorisation cites the register it was read from and carries a why-trail (I3)
        assert r["register"] and r["register_url"].startswith("https://") and r["why_trail_id"] and r["valid_from"]
    with engine.connect() as conn:
        trail = conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id == rows[0]["why_trail_id"])).mappings().one()
    assert trail["layer"] == "L4" and trail["agent_id"] == "l4.ontology" and "register snapshot" in trail["reasoning_summary"]

    # idempotent: a second build changes nothing and publishes no event
    out = l4_ontology.build_ontology(engine, check_registers=False)
    assert all(c["added"] == 0 and c["updated"] == 0 and c["invalidated"] == 0 for c in out["counts"].values())
    assert out["version"] == l4_ontology.snapshot_version(snap)

    # replay posture: live register checks fall back to the reviewed snapshot, honestly labelled
    out = l4_ontology.build_ontology(engine, check_registers=True)
    assert out["registers"] and all(v["freshness"] == "snapshot" for v in out["registers"].values())

    onto = l4_ontology.ontology(engine)
    assert len(onto["licences"]) == len(snap["licences"]) and onto["attribute_schema"] and onto["jurisdictions"]
    lookup = l4_ontology.Lookup(onto["licences"])
    assert lookup.resolve("casp (mica)")["id"] == "LIC:EU:casp" and lookup.resolve("crypto-asset service provider")["id"] == "LIC:EU:casp"


def test_snapshot_change_reversions_and_invalidates_never_deletes(engine, monkeypatch):
    snap = json.loads(json.dumps(l4_ontology.snapshot()))
    snap["licences"] = [l for l in snap["licences"] if l["id"] != "LIC:US:cftc-fcm"]
    snap["permits"] = [p for p in snap["permits"] if p["licence_id"] != "LIC:US:cftc-fcm"]
    casp = next(l for l in snap["licences"] if l["id"] == "LIC:EU:casp")
    casp["aliases"] = casp["aliases"] + ["MiCA authorised CASP"]
    monkeypatch.setattr(l4_ontology, "snapshot", lambda: snap)
    out = l4_ontology.build_ontology(engine, check_registers=False)
    assert out["counts"]["licences"]["invalidated"] == 1 and out["counts"]["licences"]["updated"] == 1
    with engine.connect() as conn:
        gone = conn.execute(sa.select(licences).where(licences.c.id == "LIC:US:cftc-fcm")).mappings().one()
        upd = conn.execute(sa.select(licences).where(licences.c.id == "LIC:EU:casp")).mappings().one()
        event = conn.execute(sa.select(events_t.c.kind).order_by(events_t.c.id.desc()).limit(1)).scalar()
    assert gone["valid_to"] is not None and gone["review"][-1]["event"] == "invalidated"  # I2
    assert upd["version"] == 2 and upd["review"][-1]["event"] == "re-versioned" and "MiCA authorised CASP" in upd["aliases"]
    assert event == "clhear.l4.changed"


# --------------------------------------------------------------------------- validator


def test_validator_rejects_every_impossible_permutation_with_a_reason(engine):
    ok = l4_validate.validate(engine, EMI)
    assert ok["valid"] and ok["errors"] == [] and ok["normalized"]["authorisations"] == EMI["authorisations"]
    assert ok["resolved"]["authorisations"] == ["LIC:UK:e-money-institution"] and ok["ontology_version"]

    def codes(attrs):
        return [e["code"] for e in l4_validate.validate(engine, attrs)["errors"]]

    assert codes({**EMI, "headcount": 5}) == ["unknown_attribute"]
    assert codes({**EMI, "jurisdictions": "UK"}) == ["type"]
    assert "unknown_value" in codes({**EMI, "jurisdictions": ["Atlantis"]})
    assert "unknown_value" in codes({**EMI, "authorisations": ["Universal licence"]})  # closed world, no invented permission
    assert "authorisation_jurisdiction" in codes({**EMI, "jurisdictions": ["EU"]})
    bad = l4_validate.validate(engine, {**EMI, "products": ["e-money issuance", "custody of client assets"]})
    err = next(e for e in bad["errors"] if e["code"] == "product_not_permitted")
    assert err["value"] == "custody of client assets" and "Safeguarding and administering investments (custody)" in err["allowed"]
    rule = l4_validate.validate(engine, {**BROKER, "authorisations": BROKER["authorisations"][1:]})
    ids = {e.get("rule_id") for e in rule["errors"]}
    assert "VR:uk-rao-permission-requires-part-4a" in ids
    # aliases and ids resolve too; values are normalised to ontology names
    alias = l4_validate.validate(engine, {"jurisdictions": ["eu"], "authorisations": ["MiCA CASP"], "products": ["crypto custody"],
                                          "crypto_services": True, "financial_entity_dora": True})
    assert alias["valid"] and alias["normalized"]["jurisdictions"] == ["EU"] and alias["normalized"]["authorisations"] == ["CASP (MiCA)"]
    # regime flags: a CASP that says it is not a crypto service / DORA entity is an impossible permutation
    flags = l4_validate.validate(engine, {"jurisdictions": ["EU"], "authorisations": ["CASP (MiCA)"], "crypto_services": False})
    assert {e["rule_id"] for e in flags["errors"]} >= {"VR:crypto-flag-requires-authorisation", "VR:eu-casp-is-dora-entity"} or \
        any(e["code"] == "validity_rule" for e in flags["errors"])
    # warnings surface without invalidating
    warn = l4_validate.validate(engine, {"jurisdictions": ["EU"], "authorisations": ["MiFID II investment firm authorisation", "MiFID II A(3) Dealing on own account"],
                                         "products": ["CFDs and leveraged derivatives"], "customer_base": ["retail"], "channels": ["online and mobile app"],
                                         "financial_entity_dora": True})
    assert warn["valid"] and any(w["rule_id"] == "VR:eu-retail-cfds-intervention" for w in warn["warnings"])


# --------------------------------------------------------------------------- builder + permutations


def test_builder_offers_only_valid_options_in_dependency_order(engine):
    step = l4_builder.next_step(engine, {})
    assert step["attribute"] == "jurisdictions" and [o["value"] for o in step["options"]] == ["EU", "UK", "US"]
    step = l4_builder.next_step(engine, {"jurisdictions": ["UK"]})
    assert step["attribute"] == "authorisations" and all(o["id"].startswith("LIC:UK:") for o in step["options"]) and not step["unavailable"]
    agent = next(o for o in step["options"] if o["value"] == "Dealing in investments as agent")
    assert agent["then_requires"] and "Part 4A" in agent["then_requires"][0] and agent["register"]["url"].startswith("https://")
    step = l4_builder.next_step(engine, {"jurisdictions": ["UK"], "authorisations": ["UK MiFID investment firm (Part 4A permission)", "Dealing in investments as agent"]})
    assert step["attribute"] == "products"
    assert {o["value"] for o in step["options"]} == {"equities brokerage", "listed options and futures"}
    blocked = {o["value"]: o["blocked_by"] for o in step["unavailable"]}
    assert "client money holding" in blocked and "Client money permission (CASS)" in blocked["client money holding"][0]
    assert all(not o["valid"] for o in step["unavailable"])  # impossible permutations are never offered as valid
    partial = {"jurisdictions": ["EU"], "authorisations": ["CASP (MiCA)"], "products": ["crypto custody"], "customer_base": ["retail"],
               "channels": ["online and mobile app"], "data_footprint": "large-scale personal data"}
    step = l4_builder.next_step(engine, partial)
    assert step["attribute"] == "crypto_services" and next(o for o in step["options"] if o["value"] is True)["suggested"]
    assert l4_builder.next_step(engine, {**partial, "crypto_services": True, "financial_entity_dora": True})["complete"]


def test_permutation_explorer_and_similar_profiles(engine):
    perms = l4_builder.permutations(engine, ["UK"])
    j = perms["jurisdictions"][0]
    assert j["jurisdiction"] == "UK" and j["licences"] == 11 and j["licence_pairs"]["invalid"] == 0
    agent = next(s for s in j["single_licence_permutations"] if s["licence"] == "Dealing in investments as agent")
    assert agent["with"] == ["UK MiFID investment firm (Part 4A permission)"] and agent["valid"] and "equities brokerage" in agent["products"]
    casp = next(s for s in l4_builder.permutations(engine, ["EU"])["jurisdictions"][0]["single_licence_permutations"] if s["licence_id"] == "LIC:EU:casp")
    assert casp["valid"] and casp["attributes"]["crypto_services"] is True and casp["attributes"]["financial_entity_dora"] is True
    sim = l4_builder.similar_profiles(engine, {"products": ["crypto custody", "crypto exchange"]})
    assert [s["name"] for s in sim["similar"]] == ["EU crypto-asset service provider (MiCA)"]  # the stored sample
    assert {r["jurisdiction"] for r in sim["same_products_reachable_in"]} == {"EU", "UK", "US"}


# --------------------------------------------------------------------------- profile store


def test_profiles_stored_validated_fingerprinted_and_revalidated(engine, monkeypatch):
    with engine.connect() as conn:
        samples = [dict(r) for r in conn.execute(sa.select(profiles).where(profiles.c.source == "sample")).mappings()]
    assert len(samples) == 2 and all(s["status"] == "valid" and s["why_trail_id"] for s in samples)  # migration stored the curated samples

    with pytest.raises(ValueError) as exc:
        l4_validate.create_profile(engine, {**EMI, "products": ["custody of client assets"]})
    assert exc.value.args[0]["errors"][0]["code"] == "product_not_permitted"
    row = l4_validate.create_profile(engine, EMI, name="Test EMI")
    again = l4_validate.create_profile(engine, {**EMI, "authorisations": ["emi (uk)"] if False else EMI["authorisations"]})
    assert row["id"].startswith("PRF-") and again["id"] == row["id"]  # same attribute set -> same profile (fingerprint)
    assert row["status"] == "valid" and row["fingerprint"] == l4_validate.fingerprint(row["attributes"]) and row["jurisdictions"] == ["UK"]
    kept = l4_validate.create_profile(engine, {**EMI, "products": ["custody of client assets"]}, allow_invalid=True)
    assert kept["status"] == "invalid" and kept["validity"]["errors"]

    # ontology change -> propagator re-judges every stored profile; the row stays (I2), status flips are on the record
    snap = json.loads(json.dumps(l4_ontology.snapshot()))
    snap["permits"] = [p for p in snap["permits"] if not (p["licence_id"] == "LIC:UK:e-money-institution" and p["product_id"] == "PRD:payment-accounts")]
    monkeypatch.setattr(l4_ontology, "snapshot", lambda: snap)
    l4_ontology.build_ontology(engine, check_registers=False)
    out = l4_validate.revalidate_profiles(engine)
    assert out["checked"] >= 4 and out["changed"] >= 1
    with engine.connect() as conn:
        flipped = l4_validate.get_profile(conn, row["id"])
    assert flipped["status"] == "invalid" and flipped["version"] == 2
    assert flipped["review"][-1]["event"] == "revalidated" and flipped["review"][-1]["from"] == "valid" and flipped["review"][-1]["why_trail_id"]


# --------------------------------------------------------------------------- applicability predicates


def test_deterministic_predicates_from_jurisdiction_subject_condition(engine, tmp_path):
    _ingest(engine, tmp_path)
    run_extraction(engine, source_key=SOURCE)
    out = l4_predicates.extract_predicates(engine)
    assert out["obligations"] == 4 and out["added"] >= 8
    with engine.connect() as conn:
        live = _live(conn)
        by_ref = {ref: {e["basis"]: e for e in _edges(conn, ob["id"])} for ref, ob in live.items()}
    e1 = by_ref["1"]
    assert e1["jurisdiction"]["predicate"] == {"jurisdictions": "UK"}
    assert e1["subject"]["predicate"] == {"authorisations": "*"} and "firm" in e1["subject"]["rationale"]
    assert e1["condition"]["predicate"] == {"products": "client money holding"} and "client money" in e1["condition"]["rationale"]
    assert by_ref["2"]["subject"]["predicate"] == {"authorisations": "E-money institution (Electronic Money Regulations 2011)"}
    assert by_ref["4"]["subject"]["predicate"] == {"authorisations": ["Authorised payment institution (Payment Services Regulations 2017)",
                                                                       "E-money institution (Electronic Money Regulations 2011)"]}
    assert by_ref["4"]["condition"]["predicate"] == {"channels": "online and mobile app"}
    for edges in by_ref.values():  # every edge: why-trail, text hash, deterministic method (I3)
        for e in edges.values():
            assert e["why_trail_id"] and e["obligation_text_hash"] and e["method"] == "deterministic-v1"
    # idempotent
    again = l4_predicates.extract_predicates(engine)
    assert again["added"] == 0 and again["invalidated"] == 0 and again["unchanged"] == out["added"]

    # obligations for a profile: all edges must match
    emi = l4_predicates.obligations_for_profile(engine, EMI)
    refs = {o["clause_ref"] for o in emi["obligations"]}
    assert refs == {"2", "3", "4"}  # not "1": the EMI holds no client money
    broker = l4_predicates.obligations_for_profile(engine, BROKER)
    assert {o["clause_ref"] for o in broker["obligations"]} == {"1", "3"}
    eu = l4_predicates.obligations_for_profile(engine, {"jurisdictions": ["EU"], "authorisations": ["CASP (MiCA)"]})
    assert eu["count"] == 0  # UK duties do not reach an EU-only profile
    cov = l4_predicates.coverage(engine)
    assert cov["obligations"] == 4 and cov["with_predicates"] == 4 and cov["rate"] == 1.0 and cov["narrowed_beyond_jurisdiction"] == 4


def test_l2_change_restamps_and_withdraws_edges_via_worker(engine, tmp_path):
    from app.clhear import workers
    from app.clhear.platform.events import Envelope

    _ingest(engine, tmp_path)
    run_extraction(engine, source_key=SOURCE)
    l4_predicates.extract_predicates(engine)
    gateway = Gateway(engine, FakeProvider())

    def send(payload, n):
        env = Envelope(event_id=f"evt-l4-{n}", layer="L2", kind="clhear.l2.changed", subject_ref=payload["obligation_id"],
                       payload=payload, producer="l2.change", ts=datetime.now(timezone.utc).isoformat())
        return workers.handle_envelope(engine, gateway, env.model_dump_json())

    with engine.connect() as conn:
        live = _live(conn)
        before = _edges(conn, live["1"]["id"])
    assert len(before) == 3
    out = send({"change_event_id": "CHG-000201", "obligation_id": live["1"]["stable_id"], "derivation_key": live["1"]["id"], "change": "revoked"}, 1)
    assert out["l4"]["invalidated"] == 3
    with engine.connect() as conn:
        assert _edges(conn, live["1"]["id"]) == []
        closed = _edges(conn, live["1"]["id"], live=False)
        assert all(c["review"][-1]["reason"] == "obligation revoked" and c["review"][-1]["why_trail_id"] for c in closed)

    with engine.begin() as conn:
        conn.execute(obligations.update().where(obligations.c.id == live["2"]["id"]).values(text_hash="sha256:changed"))
    out = send({"change_event_id": "CHG-000202", "obligation_id": live["2"]["stable_id"], "derivation_key": live["2"]["id"], "change": "updated"}, 2)
    assert out["l4"]["restamped"] == 3 and out["l4"]["invalidated"] == 0
    with engine.connect() as conn:
        edges = _edges(conn, live["2"]["id"], live=False)
        assert len(edges) == 6 and all(e["obligation_text_hash"] == "sha256:changed" for e in edges if e["valid_to"] is None)
        assert {json.dumps(e["predicate"], sort_keys=True) for e in edges if e["valid_to"] is None} == \
            {json.dumps(e["predicate"], sort_keys=True) for e in edges if e["valid_to"] is not None}

    # ontology change event -> profiles re-validated through the worker too
    env = Envelope(event_id="evt-l4-onto", layer="L4", kind="clhear.l4.changed", subject_ref="l4.ontology@x", payload={},
                   producer="l4.ontology", ts=datetime.now(timezone.utc).isoformat())
    out = workers.handle_envelope(engine, gateway, env.model_dump_json())
    assert out["checked"] == 2 and out["changed"] == 0


def test_grounded_llm_applicability_read_discards_ungrounded(engine, tmp_path):
    _ingest(engine, tmp_path)
    run_extraction(engine, source_key=SOURCE)
    # Only obligation 3 ("A firm must retain records") is subject-only; the scripted answer quotes a real span
    # for one predicate and invents another (no quote in the text) plus an unknown value -> discarded.
    router = _router(engine, [{"predicates": [
        {"attribute": "customer_base", "values": ["retail"], "quote": "records required by this chapter"},
        {"attribute": "products", "values": ["margin lending"], "quote": "margin lending"},
        {"attribute": "authorisations", "values": ["Universal licence"], "quote": "A firm"},
    ]}])
    with engine.connect() as conn:
        live = _live(conn)
    # give obligation 3 a pure-jurisdiction shape by removing the generic 'firm' reading
    with engine.begin() as conn:
        conn.execute(obligations.update().where(obligations.c.id == live["3"]["id"]).values(
            subject="", addressee="", determination="Records required by this chapter must be retained for at least five years.",
            statement="Records required by this chapter must be retained for at least five years."))
    out = l4_predicates.extract_predicates(engine, router)
    assert out["jurisdiction_only"] == 1 and out["llm_edges"] == 1 and out["llm_discarded"] == 2
    with engine.connect() as conn:
        edges = _edges(conn, live["3"]["id"])
    llm = [e for e in edges if e["basis"] == "llm"]
    assert len(llm) == 1 and llm[0]["predicate"] == {"customer_base": "retail"} and llm[0]["rationale"].startswith("llm: '")
    assert llm[0]["method"] not in ("", "deterministic-v1") and llm[0]["why_trail_id"]
    with engine.connect() as conn:
        trail = conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id == llm[0]["why_trail_id"])).mappings().one()
    assert trail["model_manifest"]["method"] == "grounded-llm" and record.needs_human("L4", float(trail["confidence"])) in (True, False)
    # a later deterministic pass keeps the LLM edge (text unchanged) and does not re-ask
    again = l4_predicates.extract_predicates(engine, router)
    assert again["llm_edges"] == 0 and again["invalidated"] == 0


# --------------------------------------------------------------------------- gates


def test_l4_gates_and_scorecard(engine, tmp_path):
    from app.clhear.platform.gates import LAYER_GATES, gate_status

    assert LAYER_GATES["L4"] == ("l4_validity", "l4_applicability", "l4_grounding")
    v = run_suite(engine, "l4_validity")
    assert v["passed"] and v["scores"]["accuracy"] == 1.0 and v["scores"]["checks"] >= 30 and v["scores"]["provenance_missing"] == []
    _ingest(engine, tmp_path)
    run_extraction(engine, source_key=SOURCE)
    l4_predicates.extract_predicates(engine)
    a = run_suite(engine, "l4_applicability")
    assert a["passed"] and a["scores"]["precision"] >= 0.95 and a["scores"]["recall"] >= 0.95 and a["scores"]["edges"] >= 8
    assert a["scores"]["dangling_edges"] == [] and a["scores"]["non_schema_edges"] == []
    run_suite(engine, "l4_grounding")
    assert gate_status(engine, "L4")["passed"]
    # a dangling edge (obligation rejected) fails the referential check honestly
    with engine.connect() as conn:
        live = _live(conn)
    with engine.begin() as conn:
        conn.execute(obligations.update().where(obligations.c.id == live["3"]["id"]).values(status="rejected"))
    assert not run_suite(engine, "l4_applicability")["passed"]


# --------------------------------------------------------------------------- L6 composer


def test_composer_triggers_obligations_through_applies_to(engine, tmp_path):
    from app.clhear.l6.composer import compose

    _ingest(engine, tmp_path)
    run_extraction(engine, source_key=SOURCE)
    l4_predicates.extract_predicates(engine)
    bp = compose(engine, {"attributes": BROKER}, log_request=False)
    via_l4 = {c["clause_ref"]: c for c in bp["coverage"] if "L4:applies_to" in c["triggered_by"] and c["source_key"] == SOURCE}
    assert set(via_l4) == {"1", "3"} and via_l4["1"]["state"] in ("covered", "gap")
    assert not any(c["source_key"] == SOURCE and c["clause_ref"] == "2" for c in bp["coverage"])  # the broker is not an EMI
    assert not any(u["obligation_id"] == via_l4["1"]["obligation_id"] for u in bp["unmapped_obligations"]["sample"])


# --------------------------------------------------------------------------- API + UI


def test_l4_api_and_browser(client, engine, tmp_path):
    assert client.get("/l4").status_code == 200 and "profile space" in client.get("/l4").text
    onto = client.get("/l4/ontology").json()
    assert onto["counts"]["licences"] == 29 and onto["version"] and onto["registers"] and onto["attribute_schema"]
    lics = client.get("/l4/ontology/licences", params={"jurisdiction": "US"}).json()
    assert lics["count"] == 7 and all(i["jurisdiction"] == "US" for i in lics["items"])
    assert client.get("/l4/ontology/nope").status_code == 404
    lic = client.get("/l4/licences/LIC:UK:client-money-permission").json()
    assert lic["permits"][0]["name"] == "client money holding" and "VR:uk-client-money-requires-cass" in {r["id"] for r in lic["validity_rules"]}
    assert lic["why"] and lic["register_url"].startswith("https://")
    assert client.get("/l4/licences/Client money permission (CASS)").json()["id"] == "LIC:UK:client-money-permission"

    bad = client.post("/l4/profiles/validate", json={"attributes": {**EMI, "products": ["custody of client assets"]}}).json()
    assert not bad["valid"] and bad["errors"][0]["code"] == "product_not_permitted"
    r = client.post("/l4/profiles", json={"attributes": {**EMI, "products": ["custody of client assets"]}})
    assert r.status_code == 422 and r.json()["detail"]["errors"][0]["code"] == "product_not_permitted"
    created = client.post("/l4/profiles", json={"attributes": EMI, "name": "API EMI"})
    assert created.status_code == 201 and created.json()["status"] == "valid"
    pid = created.json()["id"]
    listed = client.get("/l4/profiles", params={"source": "builder"}).json()
    assert listed["count"] == 1 and listed["items"][0]["id"] == pid and listed["facets"]["sources"]["sample"] == 2
    page = client.get(f"/l4/profiles/{pid}").json()
    assert page["name"] == "API EMI" and page["validity"]["valid"] and page["why"] and page["history"]["version"] == 1
    assert client.get("/l4/profiles/PRF-999999").status_code == 404
    sim = client.get(f"/l4/profiles/{pid}/similar").json()
    assert sim["same_products_reachable_in"] and all(s["id"] != pid for s in sim["similar"])

    step = client.post("/l4/builder/next", json={"attributes": {"jurisdictions": ["US"]}}).json()
    assert step["attribute"] == "authorisations" and len(step["options"]) == 7
    perms = client.get("/l4/permutations", params={"jurisdiction": "UK"}).json()
    assert perms["jurisdictions"][0]["licences"] == 11

    _ingest(engine, tmp_path)
    run_extraction(engine, source_key=SOURCE)
    l4_predicates.extract_predicates(engine)
    with engine.connect() as conn:
        live = _live(conn)
    obs = client.get(f"/l4/profiles/{pid}/obligations").json()
    assert obs["count"] == 3 and {o["clause_ref"] for o in obs["items"]} == {"2", "3", "4"} and obs["by_jurisdiction"] == {"UK": 3}
    ap = client.get(f"/l4/obligations/{live['1']['stable_id']}/applies-to").json()
    assert ap["count"] == 3 and ap["applies_when"] == {"jurisdictions": "UK", "authorisations": "*", "products": "client money holding"} and ap["why"]
    assert client.get("/l4/obligations/OBL-999999/applies-to").status_code == 404
    card = client.get("/l4/scorecard").json()
    assert card["ontology"]["counts"]["licences"] == 29 and card["applicability"]["with_predicates"] == 4 and card["profiles"]["valid"] == 3
    assert set(card["thresholds"]) == {"profile_validity", "applicability_pr"}
    layers = {l["layer"]: l for l in client.get("/api/clhear/layers").json()["layers"]}
    assert layers["L4"]["banner"]["data_status"] == "derived"
