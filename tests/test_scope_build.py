"""A scoped corpus is built from L1 up, one layer at a time, with no curated shortcuts."""
import pytest
import sqlalchemy as sa

from app.clhear import layer_builds
from app.clhear.l1 import scopes, source_registry
from app.clhear.l1.models import clauses, source_versions, sources
from app.clhear.l2.extract import obligation_id

SCOPE = "compliance-program-demo"
MARKETING = ("An investment adviser must not, directly or indirectly, distribute any advertisement that includes "
             "any untrue statement of a material fact, and must disclose compensation paid to a promoter.")
TEXTS = {
    "cfr/17/ia-marketing": {"275.206(4)-1": MARKETING},
    "cfr/16/255": {
        "255.1": "Endorsements must reflect the honest opinions, findings, beliefs, or experience of the endorser, "
                 "and an advertiser must not convey a misleading claim through an endorser.",
        "255.5": "An advertiser must disclose a material connection clearly and conspicuously when an endorser "
                 "who is paid promotes the product on social media.",
    },
    "usc/15/ftc-act-45": {
        "sec45(a)": "(a) Declaration of unlawfulness (1) Unfair or deceptive acts or practices in or affecting "
                    "commerce are hereby declared unlawful, and no person shall use them in advertising.",
        "sec45(b)": "(b) Proceeding by Commission Whenever the Commission shall have reason to believe that any person "
                    "has been using such a method, it shall issue and serve a complaint.",
    },
    "cfr/17/ia-compliance": {
        "275.206(4)-7": "It shall be unlawful for an investment adviser to provide investment advice unless the adviser "
                        "adopts and implements written policies and procedures and designates a chief compliance officer.",
    },
    "sec/exams/risk-alert-041724/b1": None,
}
RISK_ALERT = {"sec/exams/risk-alert-041724": {
    "sec/exams/risk-alert-041724/b1": "Staff observed advisers that did not have a reasonable basis for believing "
                                      "that advertised performance and testimonials could be substantiated.",
}}
FAQ = {"sec/staff/marketing-faq": {
    "sec/staff/marketing-faq/b1": "Amendments to rule 206(4)-1 under the Advisers Act. The marketing rule applies "
                                  "to advertisements. Under rule 206(4)-1 an adviser must comply with the marketing "
                                  "rule. The staff answers questions on the marketing rule and rule 206(4)-1.",
}}


@pytest.fixture()
def scoped(tmp_path, monkeypatch):
    """A fresh database created with the scope already active, as the demo database is."""
    from app.clhear import db as clhear_db
    from app.clhear.db import make_engine, run_migrations
    from app.clhear.settings import get_settings

    saved_s, saved_f = list(source_registry.S), list(source_registry.FAMILIES)
    monkeypatch.setenv(scopes.SCOPE_ENV, SCOPE)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/scoped.db")
    monkeypatch.setenv("CLHEAR_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("CLHEAR_SESSION_SECRET", "test-only-session-signing-secret")
    monkeypatch.setenv("CLHEAR_APP_KEYS", "os-dev:dev-os-key")
    get_settings.cache_clear()
    source_registry.apply_scope()
    engine = make_engine(get_settings().database_url)
    run_migrations(engine)
    clhear_db.set_engine(engine)
    source_registry.seed(engine)
    yield engine
    clhear_db.set_engine(None)
    get_settings.cache_clear()
    source_registry.S[:] = saved_s
    source_registry.FAMILIES[:] = saved_f


def _in_force(engine, key, texts):
    with engine.begin() as conn:
        source_id = conn.execute(sa.select(sources.c.id).where(sources.c.key == key)).scalar_one()
        version_id = conn.execute(source_versions.insert().values(
            source_id=source_id, version_label="consolidated:2025-12-31", version_kind="consolidated",
            content_hash="sha256:" + key, s3_uri="s3://test/" + key, status="in_force")).inserted_primary_key[0]
        for ordering, (ref, text) in enumerate(texts.items(), 1):
            conn.execute(clauses.insert().values(source_version_id=version_id, ref=ref, path=ref, ordering=ordering,
                                                 text=text, text_hash=f"h-{key}-{ref}", public_ok=True))


def _world(engine):
    for key, texts in {**{k: v for k, v in TEXTS.items() if v}, **RISK_ALERT, **FAQ}.items():
        _in_force(engine, key, texts)


def _llm(engine):
    from app.clhear.platform.gateway import FakeProvider
    from app.clhear.platform.router import Router

    return Router(engine, providers={"fake": FakeProvider()})


def test_build_derives_each_layer_from_the_one_below_with_no_curated_rows(scoped):
    from app.clhear import derived_models as d
    from app.clhear.scope_build import build

    engine = scoped
    _world(engine)
    report = build(engine, _llm(engine), skip_import=True,
                   profiles=[{"name": "Galaxy", "attributes": {"jurisdictions": ["US"]}}])
    assert list(report["layers"]) == list(layer_builds.ORDER)
    for layer, inputs in layer_builds.INPUTS.items():
        assert set(report["layers"][layer]["inputs"]) == set(inputs)
    with engine.connect() as conn:
        obligations = {r.id for r in conn.execute(sa.select(d.obligations.c.id))}
        curated_blocks = conn.execute(sa.select(sa.func.count()).select_from(d.blocks).where(d.blocks.c.status == "curated")).scalar_one()
        curated_activities = conn.execute(sa.select(sa.func.count()).select_from(d.activities).where(d.activities.c.status == "curated")).scalar_one()
        samples = conn.execute(sa.select(sa.func.count()).select_from(d.sample_profiles)).scalar_one()
        builds = [r.layer for r in conn.execute(sa.select(layer_builds.layer_builds.c.layer).order_by(layer_builds.layer_builds.c.id))]
    assert obligation_id("cfr/17/ia-marketing", "275.206(4)-1") in obligations
    assert obligation_id("usc/15/ftc-act-45", "sec45(a)") in obligations
    assert obligation_id("usc/15/ftc-act-45", "sec45(b)") not in obligations  # Commission procedure, not a duty
    assert (curated_blocks, curated_activities, samples) == (0, 0, 0)
    assert builds == list(layer_builds.ORDER)


def test_a_layer_does_not_build_from_an_input_that_changed_after_its_build(scoped):
    from app.clhear import derived_models as d
    from app.clhear.scope_build import build

    engine = scoped
    _world(engine)
    build(engine, _llm(engine), skip_import=True)
    with engine.begin() as conn:
        conn.execute(d.obligations.update().where(d.obligations.c.clause_ref == "255.1").values(statement="edited"))
    with pytest.raises(layer_builds.LayerOrderViolation, match="L2"):
        build(engine, _llm(engine), layers=("L3",))


def test_proof_traces_every_layer_and_reports_each_component(scoped):
    from app.clhear.scope_build import build
    from app.clhear.scope_proof import run

    engine = scoped
    _world(engine)
    build(engine, _llm(engine), skip_import=True, profiles=[{"name": "Galaxy", "attributes": {"jurisdictions": ["US"]}}])
    proof = run(engine, SCOPE)
    for layer in ("L2", "L3", "L4", "L5", "L6", "L8"):
        assert proof["layers"][layer]["passed"], (layer, proof["layers"][layer])
    assert proof["layers"]["L1"]["passed"] is False  # this fixture imports only some scoped sources
    assert proof["components"]["endorsements_and_influencers"]["obligations"] >= 3
    assert proof["golden"]["status"] in {"awaiting_reviewer", "scored"}


def test_the_marketing_rule_is_named_by_the_staff_faq_not_by_a_table(scoped):
    from app.clhear.l7.enforcement import _alias_index, extract_citations, mine_instrument_names

    engine = scoped
    _world(engine)
    with engine.connect() as conn:
        names = mine_instrument_names(conn)
        aliases = _alias_index(conn)
    assert names["Marketing Rule"]["source_keys"] == ("cfr/17/ia-marketing",)
    assert names["Marketing Rule"]["evidence"] == "sec/staff/marketing-faq"
    [cite] = [c for c in extract_citations("SEC charges advisers for Marketing Rule violations", aliases) if c.get("whole_instrument")]
    assert cite["source_keys"] == ["cfr/17/ia-marketing"] and cite["named_by"] == "sec/staff/marketing-faq"


def test_l8_reference_rows_are_the_alert_blocks_mapped_by_shared_words(scoped):
    from app.clhear.l8.reference import reference_rows
    from app.clhear.scope_build import build

    engine = scoped
    _world(engine)
    build(engine, _llm(engine), skip_import=True)
    [row] = reference_rows(engine)
    assert row["kind"] == "finding" and row["peer_data"] is False
    assert row["quote"].startswith("Staff observed advisers") and row["source"]["clause_ref"].endswith("/b1")


def test_scope_release_lists_only_gated_layers_and_serves_v1_from_its_artifact(scoped, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.clhear import release_db
    from app.main import create_app
    from app.clhear.scope_build import build
    from app.clhear.scope_release import publish

    engine = scoped
    _world(engine)
    build(engine, _llm(engine), skip_import=True, profiles=[{"name": "Galaxy", "attributes": {"jurisdictions": ["US"]}}])
    released = publish(engine, SCOPE)
    assert "L1" not in released["layers"]  # the fixture's scope is not fully in force
    assert {"L2", "L3", "L5", "L6"} <= set(released["layers"])
    path = released["snapshot_uri"].removeprefix("file://")
    monkeypatch.setenv(release_db.PATH_ENV, path)
    release_db.dispose()
    auth = {"Authorization": "Bearer dev-os-key", "X-App-Id": "os-dev"}
    with TestClient(create_app()) as client:
        body = client.get(f"/v1/releases/{released['id']}/L2/obligations", headers=auth)
    assert body.status_code == 200, body.text
    assert "275.206(4)-1" in body.text
    release_db.dispose()



def test_vendor_observations_move_the_derived_blueprint_score(scoped):
    """Synthetic Galaxy posts arrive the way the OS sends them: /v1/observations."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.clhear.scope_build import build

    engine = scoped
    _world(engine)
    build(engine, _llm(engine), skip_import=True, layers=("L1", "L2", "L3", "L4", "L5"))
    marketing = obligation_id("cfr/17/ia-marketing", "275.206(4)-1")
    disclosure = obligation_id("cfr/16/255", "255.5")
    honest = obligation_id("cfr/16/255", "255.1")
    auth = {"Authorization": "Bearer dev-os-key", "X-App-Id": "os-dev"}
    performer = {"kind": "app", "id": "safeluence", "protocol": "mcp"}
    with TestClient(create_app()) as client:
        before = client.post("/v1/blueprint", json={"attributes": {"jurisdictions": ["US"]}}, headers=auth)
        assert before.status_code == 200, before.text
        first = before.json()["compliance_score"]
        assert first["required"] >= 3 and first["passed"] == 0
        failed = client.post("/v1/observations", headers=auth, json={
            "subject": [marketing, disclosure, "galaxy-post-a"], "result": "fail", "labels": ["cleared"],
            "evidence": {"pointer": "galaxy:posts/a", "text": "Paid promoter post; disclosure only in the bio."},
            "reasoning": f"{disclosure} requires a clear and conspicuous disclosure; {marketing} requires a promoter agreement.",
            "performer": performer, "observed_at": "2026-09-21T12:00:00+00:00"})
        assert failed.status_code == 200, failed.text
        assert failed.json()["mapped"] is True and "cleared" in failed.json()["unmapped_labels"]
        passed = client.post("/v1/observations", headers=auth, json={
            "subject": [marketing, disclosure, honest, "galaxy-post-b"], "result": "pass",
            "evidence": {"pointer": "galaxy:posts/b", "text": "Paid relationship stated in the video; agreement on file."},
            "reasoning": f"{disclosure}, {honest} and {marketing} are satisfied.",
            "performer": performer, "observed_at": "2026-09-21T13:00:00+00:00"})
        assert passed.status_code == 200, passed.text
        after = client.post("/v1/blueprint", json={"attributes": {"jurisdictions": ["US"]}}, headers=auth).json()
        second = after["compliance_score"]
        assert second["passed"] > first["passed"]
        assert any(point["result"] == "pass" for point in second["points"])
