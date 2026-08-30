"""Consolidated CLHEAR obligations: seeding, resolution weights, consolidation
proposals, human gate, integrity evals, multi-entity blueprints, lineage."""
import json

import sqlalchemy as sa

from app.clhear import curated
from app.clhear.derived_models import obligations
from app.clhear.l1.models import clauses, family_members, source_families, source_versions, sources
from app.clhear.l2.concepts import get_concept, list_concepts, resolve_concept, upsert_concept
from app.clhear.l2.consolidate import draft_and_propose, find_candidates
from app.clhear.l2.extract import run_extraction
from app.clhear.models import proposals as proposals_t
from app.clhear.platform.evals import run_suite
from app.clhear.platform.gateway import FakeProvider, Gateway

UK_DUTY_27 = (
    "A relevant person must apply customer due diligence measures if the person establishes a "
    "business relationship or carries out an occasional transaction exceeding the threshold."
)
UK_DUTY_40 = (
    "A relevant person must keep the records specified, including copies of documents obtained to "
    "satisfy customer due diligence requirements and sufficient supporting transaction records."
)
EU_DUTY_30 = (
    "Each controller shall maintain a record of processing activities under its responsibility, "
    "containing the purposes of processing, categories of recipients and retention time limits."
)
EU_DUTY_20 = (
    "Obliged entities shall apply customer due diligence measures and verify the customer identity "
    "when establishing a business relationship or carrying out an occasional transaction."
)
RESTRICTED_TEXT = "the organization shall determine the boundaries and applicability of the information security management system to establish its scope"


def _seed(engine):
    with engine.begin() as conn:
        fam = conn.execute(source_families.insert().values(key="test", name="Test", scope_charter={})).inserted_primary_key[0]

        def add_source(key, name, jurisdiction, license="open"):
            sid = conn.execute(
                sources.insert().values(
                    family_id=fam, key=key, name=name, kind="regulation", license=license,
                    short_name=name[:12], jurisdiction=jurisdiction, topics=["financial-crime"],
                )
            ).inserted_primary_key[0]
            conn.execute(
                family_members.insert().values(
                    family_id=fam, source_id=sid, relation="root", tier="binding", status="active", added_via="manual"
                )
            )
            vid = conn.execute(
                source_versions.insert().values(
                    source_id=sid, version_label=f"consolidated:{key[-4:]}", version_kind="consolidated",
                    content_hash=f"sha:{key}", s3_uri=f"s3://x/{key}", status="in_force",
                )
            ).inserted_primary_key[0]
            return vid

        uk = add_source("uksi/2017/692", "UK MLRs 2017", "UK")
        eu_gdpr = add_source("celex/32016R0679", "EU GDPR", "EU")
        eu_amlr = add_source("celex/32024R1624", "EU AMLR", "EU")
        iso = add_source("iso/27001-2022", "ISO 27001", "International", license="restricted")
        for vid, ref, order, text, h, public in (
            (uk, "regulation-27", 27, UK_DUTY_27, "h27", True),
            (uk, "regulation-40", 40, UK_DUTY_40, "h40", True),
            (eu_gdpr, "art_30", 30, EU_DUTY_30, "h30", True),
            (eu_amlr, "art_20", 20, EU_DUTY_20, "h20", True),
            (iso, "clause-4.3", 43, RESTRICTED_TEXT, "hiso", False),
        ):
            conn.execute(
                clauses.insert().values(
                    source_version_id=vid, ref=ref, path=ref, ordering=order,
                    text=text, text_hash=h, public_ok=public,
                )
            )
    run_extraction(engine)
    curated.seed(engine)


# ------------------------------------------------------------------ seeding


def test_seed_concepts_skips_missing_members_and_never_clobbers(engine, client):
    _seed(engine)
    summary = curated.seed_concepts(engine)
    assert summary["concepts_written"] >= 2  # CDD + records-retention have live members here
    cdd = get_concept(engine, "CON:customer-due-diligence")
    assert cdd is not None
    member_ids = {m["obligation_id"] for m in cdd["members"]}
    assert "OBL:uksi/2017/692#regulation-27" in member_ids
    assert "OBL:celex/32024R1624#art_20" in member_ids
    # regulation-28 isn't in this corpus: skipped, reported, not invented.
    assert "OBL:uksi/2017/692#regulation-28" not in member_ids
    assert "OBL:uksi/2017/692#regulation-28" in summary["missing_members"]

    # Re-seed is create-only: a maintainer edit survives.
    upsert_concept(
        engine, concept_id="CON:customer-due-diligence", name="EDITED BY MAINTAINER",
        canonical_statement="edited", themes=[], members=[{"obligation_id": "OBL:uksi/2017/692#regulation-27"}],
        approved_by="avner@reg42.ai",
    )
    again = curated.seed_concepts(engine)
    assert again["concepts_written"] == 0 or get_concept(engine, "CON:customer-due-diligence")["name"] == "EDITED BY MAINTAINER"


# --------------------------------------------------------------- resolution


def test_resolution_weight_matches_jurisdictions(engine, client):
    _seed(engine)
    curated.seed_concepts(engine)
    concept = get_concept(engine, "CON:customer-due-diligence")

    both = resolve_concept(engine, concept, ["UK", "EU"])
    uk_only = resolve_concept(engine, concept, ["UK"])
    us_included = resolve_concept(engine, concept, ["UK", "EU", "US"])

    assert both["resolvable"] and uk_only["resolvable"]
    # UK-only is strictly lighter: fewer facets, fewer obligations.
    assert uk_only["weight"]["obligations"] < both["weight"]["obligations"]
    assert set(uk_only["facets"]) == {"UK"}
    assert set(both["facets"]) == {"UK", "EU"}
    # Excluded facets are named, not silently merged.
    assert uk_only["excluded_jurisdictions"] == ["EU"]
    # Requesting a jurisdiction with no basis is stated honestly.
    assert us_included["uncovered_jurisdictions"] == ["US"]
    assert "NOT covered here: US" in us_included["claim_scope"]
    assert "not a claim of global compliance" in us_included["claim_scope"].lower()
    # Determinism.
    assert resolve_concept(engine, concept, ["UK", "EU"]) == both


# ------------------------------------------------- consolidation proposals


def test_candidates_are_cross_jurisdiction_only(engine, client):
    _seed(engine)
    groups = find_candidates(engine)
    assert groups, "UK reg-27 and EU AMLR art_20 share CDD vocabulary across jurisdictions"
    top = groups[0]
    assert len({g["jurisdiction"] for g in top}) >= 2


def test_draft_propose_approve_flow(engine, client):
    _seed(engine)
    canned = json.dumps({
        "name": "Apply customer due diligence before doing business",
        "canonical_statement": "Identify and verify customers before establishing relationships or large occasional transactions.",
        "member_notes": {"OBL:uksi/2017/692#regulation-27": "UK trigger set"},
    })
    gateway = Gateway(engine, FakeProvider(canned_text=canned))
    summary = draft_and_propose(engine, gateway)
    assert summary["proposed"] >= 1 and summary["llm_drafted"] >= 1

    # Re-running does not duplicate pending proposals.
    assert draft_and_propose(engine, gateway)["proposed"] == 0

    with engine.connect() as conn:
        prop = conn.execute(sa.select(proposals_t).where(proposals_t.c.kind == "l2_concept")).first()
    assert prop is not None and prop.status == "proposed"

    # Not live before approval.
    draft = prop.draft if isinstance(prop.draft, dict) else json.loads(prop.draft)
    assert get_concept(engine, draft["id"]) is None

    resp = client.post(f"/api/clhear/proposals/{prop.id}/approve", headers={"X-Reg42-User": "avner@reg42.ai"})
    assert resp.status_code == 200, resp.text
    concept = get_concept(engine, draft["id"])
    assert concept is not None
    assert concept["status"] == "curated"
    assert concept["approved_by"] == "avner@reg42.ai"
    assert concept["drafted_by"] == "gateway"
    assert len(concept["members"]) >= 2


# --------------------------------------------------------------- integrity


def test_concept_integrity_eval_gates(engine, client):
    _seed(engine)
    curated.seed_concepts(engine)
    assert run_suite(engine, "l2_concept_integrity")["passed"] is True

    # A stale member flags the concept; flagged concepts refuse to resolve.
    from app.clhear.l2.concepts import flag_stale_concepts

    with engine.begin() as conn:
        conn.execute(
            obligations.update().where(obligations.c.id == "OBL:uksi/2017/692#regulation-27").values(status="stale")
        )
    flagged = flag_stale_concepts(engine)
    assert "CON:customer-due-diligence" in flagged
    concept = get_concept(engine, "CON:customer-due-diligence")
    resolution = resolve_concept(engine, concept, ["UK", "EU"])
    assert resolution["resolvable"] is False and "flagged" in resolution["reason"]
    assert run_suite(engine, "l2_concept_integrity")["passed"] is False


def test_restricted_ngram_guard(engine, client):
    _seed(engine)
    upsert_concept(
        engine, concept_id="CON:leaky", name="Leaky",
        canonical_statement="Per the standard, " + RESTRICTED_TEXT,
        themes=[], members=[{"obligation_id": "OBL:uksi/2017/692#regulation-27"}],
        approved_by="x",
    )
    record = run_suite(engine, "l2_concept_integrity")
    assert record["passed"] is False
    assert "CON:leaky" in record["scores"]["restricted_leaks"]


# --------------------------------------------------------------- blueprint


AUTH = {"Authorization": "Bearer dev-os-key", "X-App-Id": "os-dev"}


def test_multi_entity_blueprint_group_vs_entity_views(engine, client):
    _seed(engine)
    curated.seed_concepts(engine)
    from app.clhear.releases import publish_release

    publish_release(engine, release_id="clhear-v-test")
    body = {
        "entities": [
            {"name": "UK EMI", "attributes": {"jurisdictions": ["UK"], "data_footprint": ""}},
            {"name": "EU CASP", "attributes": {"jurisdictions": ["EU"], "crypto_services": True, "data_footprint": ""}},
        ]
    }
    resp = client.post("/v1/blueprint", json=body, headers=AUTH)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["mode"] == "group"
    assert out["group"]["jurisdictions"] == ["EU", "UK"]
    group_cdd = next(r for r in out["group"]["consolidated"] if r["concept_id"] == "CON:customer-due-diligence")
    assert set(group_cdd["facets"]) == {"EU", "UK"}
    uk_entity = next(e for e in out["entities"] if e["name"] == "UK EMI")
    uk_cdd = next(r for r in uk_entity["consolidated"] if r["concept_id"] == "CON:customer-due-diligence")
    assert set(uk_cdd["facets"]) == {"UK"}
    assert uk_cdd["weight"]["obligations"] < group_cdd["weight"]["obligations"]
    assert "not a claim of global compliance" in group_cdd["claim_scope"].lower()
    assert out["legal"]["not_legal_advice"] is True


def test_single_profile_blueprint_gains_consolidated(engine, client):
    _seed(engine)
    curated.seed_concepts(engine)
    resp = client.post(
        "/v1/blueprint",
        json={"attributes": {"jurisdictions": ["UK"], "data_footprint": ""}},
        headers=AUTH,
    )
    assert resp.status_code == 200
    consolidated = resp.json()["consolidated"]
    assert consolidated and all(set(r["facets"]) <= {"UK"} for r in consolidated)


# ------------------------------------------------------------------ lineage


def test_concept_lineage_walks_facets_to_clauses(engine, client):
    _seed(engine)
    curated.seed_concepts(engine)
    body = client.get("/api/clhear/layers/l2/items/CON:customer-due-diligence/lineage").json()
    chain = body["lineage"]
    assert chain["kind"] == "concept"
    facets = {n["title"]: n for n in chain["children"]}
    assert "UK facet" in facets and "EU facet" in facets
    uk_ob = facets["UK facet"]["children"][0]
    assert uk_ob["kind"] == "obligation"
    clause_leaf = uk_ob["children"][0]
    assert clause_leaf["layer"] == "L1" and clause_leaf["meta"]["resolved"] is True
    assert "customer due diligence" in clause_leaf["meta"]["text"]
