"""Fleet kernel: every registry row has an adapter; crash → failed; HTML/lists/BYOL."""
import sqlalchemy as sa

from app.clhear.l1 import fidelity, pipeline
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta
from app.clhear.l1.adapters.lists import ListsAdapter
from app.clhear.l1.adapters.official_html import OfficialHtmlAdapter
from app.clhear.l1.adapters.restricted_file import RestrictedFileAdapter
from app.clhear.l1.fleet import adapter_for, fleet_plan
from app.clhear.l1.models import FLEET_SCHEDULES
from app.clhear.l1.registry_etoro import S
from app.clhear.models import runs


HTML = b"""<!doctype html><html><head><title>PRIN 1</title></head>
<body>
<nav>Skip</nav>
<h1>PRIN 1 Integrity</h1>
<p>A firm must conduct its business with integrity.</p>
<footer>copyright</footer>
</body></html>"""

OFAC = b"""<?xml version="1.0"?>
<sdnList>
  <sdnEntry>
    <uid>1</uid>
    <firstName>ALICE</firstName>
    <lastName>EXAMPLE</lastName>
    <sdnType>Individual</sdnType>
    <remarks>test listing</remarks>
  </sdnEntry>
</sdnList>"""


class _BoomAdapter:
    key = "stub"

    def meta(self) -> SourceMeta:
        return SourceMeta(
            family_key="boom",
            family_name="Boom",
            source_key="boom/one",
            name="Boom",
            kind="regulation",
            issuer="x",
            jurisdiction="XX",
            license="open",
            canonical_url="https://example.invalid",
            adapter="stub",
        )

    def fetch(self, since_version=None):
        raise RuntimeError("HTTP 202 empty")

    def expected_text(self, artifacts):
        return []


class _OkThenBoom:
    key = "stub"

    def __init__(self, boom=False):
        self.boom = boom

    def meta(self) -> SourceMeta:
        return SourceMeta(
            family_key="boom",
            family_name="Boom",
            source_key="boom/kept",
            name="Kept",
            kind="regulation",
            issuer="x",
            jurisdiction="XX",
            license="open",
            canonical_url="https://example.invalid",
            adapter="stub",
        )

    def fetch(self, since_version=None):
        if self.boom:
            raise RuntimeError("TNA 202")
        return FetchResult(
            version_label="v1",
            artifacts=[Artifact(name="a.txt", content=b"hello world from publisher", content_type="text/plain")],
            tree=[DocNode(node_type="provision", ref="r1", raw_text="hello world from publisher")],
        )

    def expected_text(self, artifacts):
        return ["hello world from publisher"]


def test_fleet_plan_has_an_adapter_for_every_s_row():
    plan = {adapter.meta().source_key: adapter for _, adapter in fleet_plan()}
    for entry in S:
        assert entry["key"] in plan
        assert plan[entry["key"]].meta().adapter == entry["adapter"]


def test_every_adapter_key_has_a_daily_schedule():
    keys = {e["adapter"] for e in S}
    assert keys <= set(FLEET_SCHEDULES)
    assert "catalog_watchers" not in FLEET_SCHEDULES


def test_official_html_passes_the_gate():
    adapter = OfficialHtmlAdapter(
        source_key="fca/handbook",
        title="FCA Handbook",
        url="https://example.invalid/prin",
        adapter="fca_handbook",
    )
    soup_tree = adapter._parse(HTML)
    report = fidelity.check(soup_tree, adapter.expected_text([Artifact("p.html", HTML, "text/html")]))
    assert report.coverage >= 0.995, report.summary()
    assert report.violations == []


def test_lists_ofac_grain():
    adapter = ListsAdapter("lists/ofac-sdn", "OFAC SDN")
    tree = adapter.expected_text  # noqa: just bind
    from app.clhear.l1.adapters.lists import _parse_ofac
    import xml.etree.ElementTree as ET

    tree = _parse_ofac(ET.fromstring(OFAC), "lists/ofac-sdn")
    report = fidelity.check(tree, adapter.expected_text([Artifact("sdn.xml", OFAC, "application/xml")]))
    assert report.coverage >= 0.995, report.summary()
    assert any(n.ref == "lists/ofac-sdn/1" for n in tree[0].children)


def test_restricted_placeholder_ingests(engine, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")
    adapter = RestrictedFileAdapter("iso/27001-2022", "ISO 27001")
    summary = pipeline.ingest(engine, adapter, store)
    assert summary["status"] == "added"
    assert summary["source"] == "iso/27001-2022"


def test_fetch_crash_records_failed_not_running(engine, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")
    summary = pipeline.ingest(engine, _BoomAdapter(), store)
    assert summary["status"] == "failed"
    with engine.connect() as conn:
        row = conn.execute(sa.select(runs).order_by(runs.c.id.desc()).limit(1)).one()
        outputs = row.outputs if isinstance(row.outputs, dict) else {}
        assert outputs["status"] == "failed"
        assert "202" in outputs.get("error", "")


def test_fetch_crash_keeps_previous_as_stale(engine, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")
    first = pipeline.ingest(engine, _OkThenBoom(False), store)
    assert first["status"] == "added"
    second = pipeline.ingest(engine, _OkThenBoom(True), store)
    assert second["status"] == "stale"
    assert second["freshness"] == "stale"
    with engine.connect() as conn:
        from app.clhear.l1.models import source_versions

        assert conn.execute(sa.select(sa.func.count()).select_from(source_versions)).scalar_one() == 1


def test_adapter_for_class_b_and_corrigenda():
    fca = next(e for e in S if e["key"] == "fca/handbook")
    assert adapter_for(fca).key == "fca_handbook"
    corr = next(e for e in S if e["key"] == "celex/32016R0679R(01)")
    assert adapter_for(corr).key == "eur_lex"
    sdrt = next(e for e in S if e["key"] == "uksi/1986/1711")
    assert adapter_for(sdrt).key == "uk_legislation"
