"""New publisher adapters (HLD v2 §4.1 starter corpus): FCA Handbook, SEC/FINRA
via EDGAR, ESMA, FATF, BIS, IOSCO, MAS, ASIC, ISA, IRS. Each golden fixture
must parse verbatim (fidelity gate ≥ 99.5 %, lint clean), produce
clause-grain nodes with refs and carry the declared rights basis."""
import json
from pathlib import Path

import pytest

from app.clhear.l1 import fidelity, rights, spans
from app.clhear.l1.adapters import PUBLISHER_ADAPTER_CLASSES, publisher_adapter_class
from app.clhear.l1.adapters.base import Artifact, flatten
from app.clhear.l1.fleet import PUBLISHER_ADAPTERS
from app.clhear.l1.models import CLAUSE_TYPES, FLEET_SCHEDULES, RIGHTS_BASES
from app.clhear.platform.evals import _golden_adapter

GOLDEN = sorted(Path("clhear-evals/l1/boundary").glob("*.json"))


def _case(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.mark.parametrize("path", GOLDEN, ids=[p.stem for p in GOLDEN])
def test_golden_fixture_passes_fidelity_gate(path):
    case = _case(path)
    adapter = _golden_adapter(case)
    if "pages" in case:
        tree = adapter.parse_pages(case["pages"])
        expected = [line.strip() for page in case["pages"] for line in page.splitlines() if line.strip()]
    else:
        content = case["html"].encode()
        tree = adapter.parse(content)
        expected = adapter.expected_text([Artifact(name="page.html", content=content, content_type="text/html")])
    report = fidelity.check(tree, expected)
    assert report.violations == [], report.violations
    assert report.coverage >= 0.995, report.summary()

    clause_nodes = [n for n in flatten(tree) if n.node_type in CLAUSE_TYPES]
    assert clause_nodes
    assert all(n.ref for n in clause_nodes)
    assert len({n.ref for n in clause_nodes}) == len(clause_nodes)
    golden_refs = {c["ref"] for c in case["clauses"]}
    assert golden_refs <= {n.ref for n in clause_nodes}

    # Spans are byte-exact into the canonical text.
    canonical = spans.canonical_text(tree)
    layout = spans.span_layout(tree)
    for node in clause_nodes:
        start, end = layout[id(node)]
        assert canonical[start:end] == node.subtree_text()


def test_fca_status_letters_drive_normative_flag():
    case = _case(Path("clhear-evals/l1/boundary/fca_prin_2_1.json"))
    tree = _golden_adapter(case).parse(case["html"].encode())
    by_ref = {n.ref: n for n in flatten(tree) if n.node_type == "provision"}
    rules = [n for n in by_ref.values() if n.status == "R"]
    guidance = [n for n in by_ref.values() if n.status == "G"]
    assert rules and guidance
    assert all(spans.is_normative(n.subtree_text(), status_hint=n.status) for n in rules)
    assert not any(spans.is_normative(n.subtree_text(), status_hint=n.status) for n in guidance)


def test_finra_refs_nest_rule_paragraph_and_subparagraph():
    case = _case(Path("clhear-evals/l1/boundary/finra_3110.json"))
    tree = _golden_adapter(case).parse(case["html"].encode())
    refs = {n.ref for n in flatten(tree) if n.node_type in CLAUSE_TYPES}
    assert {"3110", "3110(a)", "3110(c)", "3110(c)(1)"} <= refs


def test_every_publisher_adapter_declares_rights_and_schedule():
    for key in PUBLISHER_ADAPTER_CLASSES:
        basis = rights.rights_for(key)
        assert basis.basis in RIGHTS_BASES, key
        assert basis.ref, key
    assert set(PUBLISHER_ADAPTERS) == set(PUBLISHER_ADAPTER_CLASSES)
    # finra rides the EDGAR channel; every other adapter has its own schedule row.
    assert set(PUBLISHER_ADAPTERS) <= set(FLEET_SCHEDULES)
    # FINRA/ISA never republish text; SEC is public domain.
    assert rights.rights_for("finra").basis == "derived_only" and not rights.republishable("derived_only")
    assert rights.rights_for("isa").basis == "derived_only"
    assert rights.rights_for("sec_edgar").basis == "public_domain" and rights.republishable("public_domain")
    assert rights.rights_for("anything", license="restricted").basis == "byol_only"


def test_publisher_adapter_meta_carries_rights_publisher_instrument():
    cls = publisher_adapter_class("fca_handbook")
    adapter = cls("PRIN", chapters=["2"], source_key="fca/handbook", title="FCA PRIN", url="https://www.handbook.fca.org.uk/handbook/PRIN")
    meta = adapter.meta()
    assert meta.adapter == "fca_handbook"
    assert meta.rights_basis == "licensed"
    assert meta.publisher and meta.instrument
    urls = adapter.chapter_urls()
    assert urls and all(u.startswith("https://www.handbook.fca.org.uk/handbook/PRIN/2") for u in urls)

    sec = publisher_adapter_class("sec_edgar")(channel="finra", source_key="finra/rule/3110", title="FINRA 3110", url="https://www.finra.org/x")
    assert sec.meta().rights_basis == "derived_only"
    sec2 = publisher_adapter_class("sec_edgar")(channel="sec", source_key="sec/release/1", title="SEC", url="https://www.sec.gov/x")
    assert sec2.meta().rights_basis == "public_domain"


def test_repeated_handbook_citation_keeps_the_first_provision():
    html = b"""<html><body>
    <p>SYSC 4.1.1 R A firm must have robust governance arrangements.</p>
    <p>SYSC 4.1.1 See the rule above.</p>
    </body></html>"""
    adapter = publisher_adapter_class("fca_handbook")(
        "SYSC", chapters=["4"], source_key="fca/handbook/SYSC", title="SYSC",
        url="https://www.handbook.fca.org.uk/handbook/SYSC")
    tree = adapter.parse(html)
    provisions = [n for n in flatten(tree) if n.node_type == "provision"]
    assert [n.ref for n in provisions] == ["SYSC 4.1.1"]
    assert any(n.node_type == "paragraph" and "See the rule above" in n.raw_text for n in flatten(tree))
