"""Influencer demo sources in the production registry: pinned readers, one fixed import request."""
import sqlalchemy as sa

from app.clhear.demo_corpus import DEMO_SOURCE_KEYS, DERIVE_KIND
from app.clhear.l1.adapters.govinfo_us import GovInfoEcfrAdapter, GovInfoUscAdapter
from app.clhear.l1.fleet import adapter_for, fleet_plan
from app.clhear.l1.inventory import _declared_entries
from app.clhear.l1.models import clauses, source_versions, sources
from app.clhear.l1.source_registry import S
from app.clhear.l2.extract import obligation_id
from app.clhear.l6.explain import cited_ids
from app.clhear.models import events

AUTH = {"Authorization": "Bearer dev-os-key", "X-App-Id": "os-dev"}
CLAUSE_STATUTE, CLAUSE_TYPICAL, CLAUSE_MARKETING = "sec45(a)", "255.2", "275.206(4)-1"

DUTY_STATUTE = (
    "(a) Declaration of unlawfulness (1) Unfair or deceptive acts or practices in or affecting commerce are hereby "
    "declared unlawful, including a paid endorsement that retail customers cannot identify as advertising."
)
DUTY_GUIDES = (
    "An advertiser must disclose a material connection clearly and conspicuously when retail "
    "customers see an endorsement on social media from finfluencers."
)
DUTY_MARKETING = (
    "An investment adviser must disclose compensation paid to a promoter, must keep a written "
    "agreement, and must oversee promotions aimed at retail customers on social media."
)
DUTY_HONEST = (
    "Endorsements must reflect the honest opinions, findings, beliefs, or experience of the "
    "endorser, and an advertiser must not convey a misleading claim through a retail endorser."
)
DUTY_TYPICAL = (
    "An advertiser must possess and rely upon adequate substantiation when a consumer endorsement "
    "conveys that the endorser's results are what retail customers can generally expect."
)
DUTY_EXPERT = (
    "An expert endorser must in fact have the expertise the advertisement represents the endorser "
    "as possessing, and the endorsement must be supported by an actual exercise of it."
)
DUTY_ORGANISATION = (
    "An organization's endorsement must reflect the collective judgment of the organization and "
    "must be reached by a process sufficient to ensure that it fairly reflects that judgment."
)


def _entry(key):
    return next(row for row in S if row["key"] == key)


def test_demo_keys_resolve_to_pinned_text_and_stay_out_of_finra_discovery():
    finra = _declared_entries("finra")
    for key in DEMO_SOURCE_KEYS:
        assert key not in finra
        assert not key.startswith("finra/")

    # The GPO section reader: original verification for govinfo_us HTML compares
    # against it, and the generic HTML adapter failed that comparison live.
    statute = adapter_for(_entry("usc/15/ftc-act-45"))
    assert isinstance(statute, GovInfoUscAdapter)
    assert statute.sections == ("45",) and statute.title == "15" and statute.edition == "2023"
    assert statute._url_template.format(ed=statute.edition, title=statute.title, sec="45") == (
        "https://www.govinfo.gov/content/pkg/USCODE-2023-title15/html/"
        "USCODE-2023-title15-chap2-subchapI-sec45.htm"
    )
    assert statute.meta().source_key == "usc/15/ftc-act-45"
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


def test_cited_ids_keep_a_closing_parenthesis_the_id_opened():
    statute = obligation_id("usc/15/ftc-act-45", CLAUSE_STATUTE)
    typical = obligation_id("cfr/16/255", CLAUSE_TYPICAL)
    marketing = obligation_id("cfr/17/ia-marketing", CLAUSE_MARKETING)
    assert statute.endswith("#sec45(a)")
    assert cited_ids(f"See {statute}.") == {statute}
    assert cited_ids(f"({statute}, {typical})") == {statute, typical}
    assert cited_ids(f"{marketing}) applies; BLK-MKT-CLAIMS-REVIEW;") == {marketing, "BLK-MKT-CLAIMS-REVIEW"}
    assert cited_ids("OBL:usc/15/exchange-act#sec78j(b)): end") == {"OBL:usc/15/exchange-act#sec78j(b)"}


GPO_SECTION = b"""<html><body>
<!-- field-start:head --><h3 class="section-head">&sect;45. Unfair methods of competition unlawful</h3><!-- field-end:head -->
<!-- field-start:statute -->
<h4 class="subsection-head">(a) Declaration of unlawfulness</h4>
<p class="statutory-body">Unfair or deceptive acts or practices in or affecting commerce are hereby declared unlawful, and a person shall not use them.</p>
<h4 class="subsection-head">(b) Proceeding by Commission</h4>
<p class="statutory-body">Whenever the Commission shall have reason to believe that any person has been using such a method, it shall issue a complaint.</p>
<!-- field-end:statute -->
<!-- field-start:notes -->
<h4 class="note-head">Effective Date of 1994 Amendment</h4>
<p class="note-body">Amendment by Pub. L. 103-312 shall apply to proceedings pending on the date of enactment.</p>
<h4 class="futureamend-note-head">Amendment of Section</h4>
<p class="note-body">A future amendment note.</p>
<!-- field-end:notes -->
</body></html>"""


def test_gpo_editorial_notes_are_notes_not_clauses():
    from app.clhear.l1.adapters.base import Artifact
    from app.clhear.l1.adapters.dom_document import parse
    from app.clhear.l1.models import CLAUSE_TYPES
    from app.clhear.l1.originals import attach_source_locations, verify_original_projection

    tree = parse(GPO_SECTION, "usc/15/ftc-act-45")
    nodes = [node for root in tree for node in root.walk()]
    clause_refs = [node.ref for node in nodes if node.node_type in CLAUSE_TYPES]
    assert clause_refs == ["sec45", "sec45(a)", "sec45(b)"]
    headings = {node.heading: node.node_type for node in nodes if node.heading}
    assert headings["Effective Date of 1994 Amendment"] == "note"
    assert headings["Amendment of Section"] == "note"
    notes_text = " ".join(node.raw_text or "" for node in nodes)
    assert "Pub. L. 103-312" in notes_text

    artifacts = [Artifact(name="sec45.htm", content=GPO_SECTION, content_type="text/html")]
    assert attach_source_locations("usc/15/ftc-act-45", "govinfo_us", artifacts, tree)
    report = verify_original_projection("usc/15/ftc-act-45", "govinfo_us", artifacts, tree)
    assert report["verified"], report["findings"]


def test_ftc_act_procedure_addressed_to_the_commission_is_not_a_duty(engine):
    from app.clhear.derived_models import obligations
    from app.clhear.l1.source_registry import seed
    from app.clhear.l2.extract import run_extraction

    seed(engine)
    _add_clauses(engine, "usc/15/ftc-act-45", {
        CLAUSE_STATUTE: DUTY_STATUTE,
        "sec45(b)": "(b) Proceeding by Commission Whenever the Commission shall have reason to believe that any person "
                    "has used such a method, it shall issue a complaint.",
        "sec45(k)": "(k) Scope This subsection shall not apply to any bank, savings and loan institution, or air carrier.",
    })
    result = run_extraction(engine, source_key="usc/15/ftc-act-45")
    assert result["candidates"] == 1
    with engine.connect() as conn:
        refs = [r for (r,) in conn.execute(sa.select(obligations.c.clause_ref).where(
            obligations.c.source_key == "usc/15/ftc-act-45"))]
    assert refs == [CLAUSE_STATUTE]


def test_deploy_runner_names_demo_keys_without_importing_the_app():
    """`python scripts/deploy_l1.py` does not put the repo root on sys.path.

    Importing the application there raised ModuleNotFoundError outside the
    handler and rolled a verified deployment back to failed_maintenance.
    """
    import inspect

    from scripts.deploy_l1 import Deployer, _DEMO_SOURCE_KEYS

    assert _DEMO_SOURCE_KEYS == DEMO_SOURCE_KEYS
    source = inspect.getsource(Deployer._request_demo_import)
    assert "demo_corpus" not in source
    assert "import app" not in source


def test_demo_import_request_is_one_fixed_run(engine, monkeypatch):
    from app.clhear import workers

    from app.clhear.demo_corpus import ENFORCEMENT_SOURCE_KEYS, REFERENCE_SOURCE_KEYS

    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    assert workers.cli(["--request-demo-import", "--verification-id", "demo-1"]) == 0
    assert workers.cli(["--request-demo-import", "--verification-id", "demo-1"]) == 0
    with engine.connect() as conn:
        rows = list(conn.execute(sa.select(events.c.kind, events.c.payload, events.c.producer)).mappings())
    assert [row["kind"] for row in rows] == ["AdapterRunRequested"] * 3
    runs = {row["payload"]["adapter"]: row["payload"] for row in rows}
    assert {row["producer"] for row in rows} == {"l0.demo"}
    assert all(payload["discover"] is False for payload in runs.values())
    assert runs["govinfo_us"]["source_keys"] == list(DEMO_SOURCE_KEYS)
    assert runs["sec_enforcement"]["source_keys"] == list(ENFORCEMENT_SOURCE_KEYS)
    assert runs["sec_edgar"]["source_keys"] == list(REFERENCE_SOURCE_KEYS)
    assert len({payload["job_id"] for payload in runs.values()}) == 3


def _add_clauses(engine, key, texts):
    with engine.begin() as conn:
        source_id = conn.execute(sa.select(sources.c.id).where(sources.c.key == key)).scalar_one()
        version_id = conn.execute(source_versions.insert().values(
            source_id=source_id, version_label="consolidated:2025-12-31", version_kind="consolidated",
            content_hash="sha256:" + key, s3_uri="s3://test/" + key, status="in_force",
        )).inserted_primary_key[0]
        for ordering, (ref, text) in enumerate(texts.items(), 1):
            conn.execute(clauses.insert().values(
                source_version_id=version_id, ref=ref, path=ref, ordering=ordering,
                text=text, text_hash="h-" + key + ref, public_ok=True,
            ))


def test_the_retired_derivation_is_acknowledged_not_run(engine):
    from app.clhear import workers
    from app.clhear.platform import routing
    from app.clhear.platform.events import Envelope

    assert routing.classify(DERIVE_KIND) == ("command", "l0")
    result = workers.handle_demo_derive(engine, None, Envelope(
        event_id="derive-1", layer="l0", kind=DERIVE_KIND, subject_ref="demo", payload={"job_id": "job-l1-demo"},
        producer="l1.demo", ts="2026-09-24T12:05:00+00:00"))
    assert result["status"] == "retired" and "scope_build" in result["reason"]
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(events)).scalar_one() == 0
