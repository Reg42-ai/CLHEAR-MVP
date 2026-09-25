"""A licence belongs to the jurisdiction of the law it quotes, and duties reach it by its addressee."""
import json
from types import SimpleNamespace

import sqlalchemy as sa

from app.clhear.derived_models import licences, license_types
from app.clhear.l4 import licenses as l4_licenses
from app.clhear.l4.predicates import _SUBJECT_CUES, _Onto, _scan
from app.clhear.platform.gateway import FakeProvider
from app.clhear.platform.router import Router
from tests.test_influencer_demo import _add_clauses

REGISTRATION = ("(a) Necessity of registration\nExcept as provided in subsection (b), it shall be unlawful for any "
                "investment adviser, unless registered under this section, to make use of the mails in connection "
                "with his or its business as an investment adviser; registration is required.")
ANCHOR = {"source_key": "usc/15/80b-3", "ref": "sec80b-3(a)", "text_hash": "h-usc/15/80b-3sec80b-3(a)"}


def _seeded(engine):
    from app.clhear.l1.source_registry import seed

    seed(engine)
    _add_clauses(engine, "usc/15/80b-3", {"sec80b-3(a)": REGISTRATION})


def _live_licences(engine):
    with engine.connect() as conn:
        return {r.id: r.jurisdiction for r in conn.execute(
            sa.select(licences.c.id, licences.c.jurisdiction).where(licences.c.valid_to.is_(None)))}


def test_a_licence_takes_the_jurisdiction_of_the_source_its_anchor_quotes(engine):
    _seeded(engine)
    canned = json.dumps({"license_types": [{"name": "Investment Adviser Registration", "issuing_regime": "Advisers Act",
                                            "source_key": "usc/15/80b-3", "ref": "sec80b-3(a)"}]})
    out = l4_licenses.extract_licenses(engine, Router(engine, providers={"fake": FakeProvider(canned_text=canned)}))
    assert out["written"] >= 1 and set(out["ids"]) == {"LIC:US:investment-adviser-registration"}
    with engine.connect() as conn:
        rows = conn.execute(sa.select(license_types.c.id, license_types.c.jurisdiction)
                            .where(license_types.c.name == "Investment Adviser Registration")).all()
    assert [(r.id, r.jurisdiction) for r in rows] == [("LIC:US:investment-adviser-registration", "US")]


def test_a_type_labelled_with_another_jurisdiction_than_its_anchor_is_retired(engine):
    from app.clhear.l4.ontology import build_ontology

    _seeded(engine)
    with engine.begin() as conn:
        conn.execute(license_types.insert().values(
            id="LIC:UK:investment-adviser-registration", jurisdiction="UK", name="Investment Adviser Registration",
            issuing_regime="", clause_anchors=[ANCHOR], status="ai_generated", generated_by="test"))
    build_ontology(engine, check_registers=False)
    assert _live_licences(engine).get("LIC:UK:investment-adviser-registration") == "UK"
    assert l4_licenses.retire_unsound(engine) == ["LIC:UK:investment-adviser-registration"]
    assert "LIC:UK:investment-adviser-registration" not in _live_licences(engine)
    assert all(t["id"] != "LIC:UK:investment-adviser-registration" for t in l4_licenses.list_license_types(engine))


def test_the_same_name_in_two_jurisdictions_is_two_licences(engine):
    from app.clhear.l4.ontology import build_ontology

    _seeded(engine)
    _add_clauses(engine, "uksi/2017/692", {"regulation-8": "A registered investment adviser must hold a registration."})
    with engine.begin() as conn:
        for jur, anchor in (("US", ANCHOR), ("UK", {"source_key": "uksi/2017/692", "ref": "regulation-8", "text_hash": "x"})):
            conn.execute(license_types.insert().values(
                id=f"LIC:{jur}:adviser-registration-test", jurisdiction=jur, name="Adviser Registration Test",
                issuing_regime="", clause_anchors=[anchor], status="ai_generated", generated_by="test"))
    build_ontology(engine, check_registers=False)
    live = _live_licences(engine)
    assert live.get("LIC:US:adviser-registration-test") == "US" and live.get("LIC:UK:adviser-registration-test") == "UK"


def test_a_duty_addressed_to_investment_advisers_reaches_the_derived_adviser_licence():
    onto = _Onto.__new__(_Onto)
    onto.schema = {"authorisations": {}}
    rows = {"LIC:US:investment-adviser-registration": {"id": "LIC:US:investment-adviser-registration", "jurisdiction": "US",
                                                       "name": "Investment Adviser Registration"},
            "LIC:UK:investment-adviser-registration": {"id": "LIC:UK:investment-adviser-registration", "jurisdiction": "UK",
                                                       "name": "Investment Adviser Registration"}}
    onto.view = SimpleNamespace(licences=SimpleNamespace(rows=rows))
    edges = _scan("any investment adviser registered under section 203", _SUBJECT_CUES, onto, "US", "subject")
    assert {"predicate": {"authorisations": "Investment Adviser Registration"}, "basis": "subject",
            "rationale": "subject: 'investment adviser'"} in edges
    assert _scan("any investment adviser", _SUBJECT_CUES, onto, "EU", "subject") == []


def test_only_names_of_licences_become_licence_types(engine):
    assert l4_licenses.names_a_licence("Investment Adviser Registration")
    assert l4_licenses.names_a_licence("Payment institution authorisation")
    for name in ("Registration", "Censure", "Suspension", "Revocation", "Withdrawal", "Exemption",
                 "Authority to enter order requiring accounting and disgorgement"):
        assert not l4_licenses.names_a_licence(name), name
    assert (l4_licenses.licence_key("Registration of investment advisers")
            == l4_licenses.licence_key("Investment Adviser Registration"))
    _seeded(engine)
    canned = json.dumps({"license_types": [
        {"name": "Investment Adviser Registration", "source_key": "usc/15/80b-3", "ref": "sec80b-3(a)"},
        {"name": "Registration of investment advisers", "source_key": "usc/15/80b-3", "ref": "sec80b-3(a)"},
        {"name": "Censure", "source_key": "usc/15/80b-3", "ref": "sec80b-3(a)"},
        {"name": "Registration", "source_key": "usc/15/80b-3", "ref": "sec80b-3(a)"},
    ]})
    l4_licenses.extract_licenses(engine, Router(engine, providers={"fake": FakeProvider(canned_text=canned)}))
    assert [t["name"] for t in l4_licenses.list_license_types(engine)] == ["Investment Adviser Registration"]


def test_stored_types_that_do_not_name_a_licence_are_retired(engine):
    _seeded(engine)
    with engine.begin() as conn:
        conn.execute(license_types.insert().values(id="LIC:US:censure", jurisdiction="US", name="Censure",
                                                   issuing_regime="", clause_anchors=[ANCHOR], status="ai_generated"))
    assert l4_licenses.retire_unsound(engine) == ["LIC:US:censure"]


def test_the_duty_text_names_the_bearer_when_the_addressee_does_not():
    from app.clhear.l4.predicates import deterministic_predicates

    onto = _Onto.__new__(_Onto)
    onto.schema = {"authorisations": {}}
    onto.view = SimpleNamespace(jurisdictions={"US"}, licences=SimpleNamespace(rows={
        "LIC:US:investment-adviser-registration": {"id": "LIC:US:investment-adviser-registration", "jurisdiction": "US",
                                                   "name": "Investment Adviser Registration"}}))
    ob = {"jurisdiction": "US", "addressee": "advertisement",
          "statement": "(a) It shall constitute a fraudulent act for any investment adviser registered or required to be "
                       "registered under section 203 of the Act to disseminate any advertisement that violates this rule."}
    predicates = [e["predicate"] for e in deterministic_predicates(ob, onto)]
    assert {"authorisations": "Investment Adviser Registration"} in predicates
    persons = {"jurisdiction": "US", "addressee": "", "statement": "Unfair methods of competition in or affecting "
               "commerce, and unfair or deceptive acts or practices in or affecting commerce, are hereby declared unlawful."}
    assert [e["predicate"] for e in deterministic_predicates(persons, onto)] == [{"jurisdictions": "US"}]


def test_the_statement_names_the_bearer_when_the_structured_duty_sentence_does_not():
    from app.clhear.l4.predicates import deterministic_predicates

    onto = _Onto.__new__(_Onto)
    onto.schema = {"authorisations": {}}
    onto.view = SimpleNamespace(jurisdictions={"US"}, licences=SimpleNamespace(rows={
        "LIC:US:investment-adviser-registration": {"id": "LIC:US:investment-adviser-registration", "jurisdiction": "US",
                                                   "name": "Investment Adviser Registration"}}))
    ob = {"jurisdiction": "US", "addressee": "",
          "determination": "provide investment advice to clients unless you adopt and implement written policies",
          "statement": "If you are an investment adviser registered or required to be registered under section 203 of "
                       "the Act, it shall be unlawful for you to provide investment advice to clients unless you adopt "
                       "and implement written policies and procedures."}
    assert {"authorisations": "Investment Adviser Registration"} in [e["predicate"] for e in deterministic_predicates(ob, onto)]
