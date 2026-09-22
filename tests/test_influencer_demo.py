"""Influencer demo corpus: pinned public sources, one fixed import, a score that moves."""
import sqlalchemy as sa

from app.clhear.demo_corpus import (
    CLAUSE_GUIDES,
    CLAUSE_MARKETING,
    DEMO_SOURCE_KEYS,
    GALAXY_ATTRIBUTES,
    POST_A,
    POST_B,
    derive_demo,
)
from app.clhear.l1.adapters.govinfo_us import GovInfoEcfrAdapter
from app.clhear.l1.adapters.official_html import OfficialHtmlAdapter
from app.clhear.l1.fleet import adapter_for, fleet_plan
from app.clhear.l1.inventory import _declared_entries
from app.clhear.l1.models import clauses, source_versions, sources
from app.clhear.l1.source_registry import S
from app.clhear.l2.extract import obligation_id
from app.clhear.l6.explain import cited_ids
from app.clhear.models import events

AUTH = {"Authorization": "Bearer dev-os-key", "X-App-Id": "os-dev"}

DUTY_STATUTE = (
    "The Commission shall prevent persons from using unfair or deceptive acts in commerce, "
    "including a paid endorsement that retail customers cannot identify as advertising."
)
DUTY_GUIDES = (
    "An advertiser must disclose a material connection clearly and conspicuously when retail "
    "customers see an endorsement on social media from finfluencers."
)
DUTY_MARKETING = (
    "An investment adviser must disclose compensation paid to a promoter, must keep a written "
    "agreement, and must oversee promotions aimed at retail customers on social media."
)


def _entry(key):
    return next(row for row in S if row["key"] == key)


def test_demo_keys_resolve_to_pinned_text_and_stay_out_of_finra_discovery():
    finra = _declared_entries("finra")
    for key in DEMO_SOURCE_KEYS:
        assert key not in finra
        assert not key.startswith("finra/")

    statute = adapter_for(_entry("usc/15/ftc-act-45"))
    assert isinstance(statute, OfficialHtmlAdapter)
    assert statute._url == (
        "https://www.govinfo.gov/content/pkg/USCODE-2023-title15/html/"
        "USCODE-2023-title15-chap2-subchapI-sec45.htm"
    )
    assert _entry("usc/15/ftc-act-45")["kind"] == "law"

    guides = adapter_for(_entry("cfr/16/255"))
    assert isinstance(guides, GovInfoEcfrAdapter)
    assert guides.sections == ("255.0", "255.1", "255.2", "255.3", "255.4", "255.5", "255.6")
    assert guides.subchapter == "B" and guides.title == "16" and guides.part == "255"
    guide_url = guides.section_url("255.5")
    assert "subchapter=B" in guide_url and "section=255.5" in guide_url
    assert _entry("cfr/16/255")["kind"] == "guidance"
    from app.clhear.l1 import publishers
    assert publishers.publisher_ids(_entry("cfr/16/255")) == ["ftc"]

    marketing = adapter_for(_entry("cfr/17/ia-marketing"))
    assert marketing.sections == ("275.206(4)-1",)
    assert marketing.subchapter == "" and marketing.chapter == "II" and marketing.part == "275"
    marketing_url = marketing.section_url("275.206(4)-1")
    assert "subchapter" not in marketing_url
    assert "section=275.206%284%29-1" in marketing_url
    assert _entry("cfr/17/ia-marketing")["kind"] == "regulation"

    # Reg BI does not declare a subchapter, so the FATCA default stays A.
    reg_bi = adapter_for(_entry("cfr/17/reg-bi-sp"))
    assert reg_bi.subchapter == "A"
    assert "subchapter=A" in reg_bi.section_url("240.15l-1")

    planned = {adapter.meta().source_key for _, adapter in fleet_plan("govinfo_us")}
    assert "nist/csf-2.0" in planned
    assert set(DEMO_SOURCE_KEYS) <= planned
    marketing_id = obligation_id("cfr/17/ia-marketing", CLAUSE_MARKETING)
    assert cited_ids(f"See {marketing_id}.") == {marketing_id}


def test_demo_import_request_is_one_fixed_run(engine, monkeypatch):
    from app.clhear import workers

    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    assert workers.cli(["--request-demo-import", "--verification-id", "demo-1"]) == 0
    assert workers.cli(["--request-demo-import", "--verification-id", "demo-1"]) == 0
    with engine.connect() as conn:
        rows = list(conn.execute(sa.select(events.c.kind, events.c.payload)).mappings())
    assert [row["kind"] for row in rows] == ["AdapterRunRequested"]
    payload = rows[0]["payload"]
    assert payload["discover"] is False
    assert payload["adapter"] == "govinfo_us"
    assert payload["source_keys"] == list(DEMO_SOURCE_KEYS)


def _seed_clauses(engine):
    from app.clhear.l1.source_registry import seed

    seed(engine)
    texts = {
        "usc/15/ftc-act-45": ("45", DUTY_STATUTE),
        "cfr/16/255": (CLAUSE_GUIDES, DUTY_GUIDES),
        "cfr/17/ia-marketing": (CLAUSE_MARKETING, DUTY_MARKETING),
    }
    with engine.begin() as conn:
        for key, (ref, text) in texts.items():
            source_id = conn.execute(sa.select(sources.c.id).where(sources.c.key == key)).scalar_one()
            version_id = conn.execute(source_versions.insert().values(
                source_id=source_id, version_label="consolidated:2025-12-31", version_kind="consolidated",
                content_hash="sha256:" + key, s3_uri="s3://test/" + key, status="in_force",
            )).inserted_primary_key[0]
            conn.execute(clauses.insert().values(
                source_version_id=version_id, ref=ref, path=ref, ordering=1,
                text=text, text_hash="h-" + ref, public_ok=True,
            ))


def test_posts_move_the_compliance_score_and_open_the_clause(client, engine):
    _seed_clauses(engine)
    derived = derive_demo(engine)
    assert derived["extracted"]["cfr/16/255"]["candidates"] >= 1
    assert derived["extracted"]["nist/csf-2.0"]["skipped"] == "nist/csf-2.0"

    failed = client.post("/v1/observations", json=POST_A, headers=AUTH)
    assert failed.status_code == 200, failed.text
    assert failed.json()["mapped"] is True
    assert "cleared" in failed.json()["unmapped_labels"]

    before = client.post("/v1/blueprint", json={"attributes": GALAXY_ATTRIBUTES}, headers=AUTH)
    assert before.status_code == 200, before.text
    first = before.json()["compliance_score"]
    assert first["required"] >= 1 and first["value"] == 0
    guide = next(point for point in first["points"] if any(clause["clause_ref"] == CLAUSE_GUIDES for clause in point["clauses"]))
    assert guide["result"] == "fail"
    assert obligation_id("cfr/16/255", CLAUSE_GUIDES) in guide["reasoning"]
    assert any(clause["source_key"] == "cfr/16/255" and clause["clause_ref"] == CLAUSE_GUIDES for clause in guide["clauses"])
    item = next(row for row in before.json()["items"] if row["block_id"] == guide["block_id"])
    assert item["performance"]["required"]
    assert "cleared" in item["performance"]["unmapped_labels"]

    passed = client.post("/v1/observations", json=POST_B, headers=AUTH)
    assert passed.status_code == 200, passed.text
    after = client.post("/v1/blueprint", json={"attributes": GALAXY_ATTRIBUTES}, headers=AUTH)
    second = after.json()["compliance_score"]
    assert second["value"] == 1
    assert second["value"] > first["value"]
    opened = next(point for point in second["points"] if any(clause["clause_ref"] == CLAUSE_MARKETING for clause in point["clauses"]))
    assert opened["result"] == "pass"
    assert obligation_id("cfr/17/ia-marketing", CLAUSE_MARKETING) in opened["reasoning"]

    vendor = client.post("/v1/observations", json={
        **POST_B,
        "result": "approved",
        "labels": ["ok"],
        "observed_at": "2026-09-21T14:00:00+00:00",
    }, headers=AUTH)
    assert vendor.status_code == 200, vendor.text
    assert vendor.json()["mapped"] is False
    assert "approved" in vendor.json()["unmapped_labels"]
    held = client.post("/v1/blueprint", json={"attributes": GALAXY_ATTRIBUTES}, headers=AUTH).json()["compliance_score"]
    assert held["value"] == 1

    layers = {}
    for layer, resource in (
        ("L2", "obligations"), ("L3", "building-blocks"), ("L4", "profiles"),
        ("L5", "activities"), ("L6", "programs"), ("L7", "risk-scores"),
    ):
        response = client.get(f"/v1/releases/clhear-vLIVE/{layer}/{resource}", headers=AUTH)
        assert response.status_code == 200, response.text
        layers[layer] = response.json()
        assert layers[layer]["layer_status"] in {"derived", "curated", "computed"}
        assert "banner" in layers[layer]
    assert CLAUSE_GUIDES in response_text(layers["L2"])
    assert layers["L7"]["view"] == "enforcement exposure"
    assert layers["L7"]["moves_with_observations"] is False
    locked = client.get("/v1/releases/clhear-vLIVE/L8/benchmarks", headers=AUTH)
    assert locked.status_code == 501


def response_text(body) -> str:
    import json
    return json.dumps(body)
