"""Original-byte negative controls, never a live publisher acquisition."""
import copy
import hashlib
import json

import pytest
import sqlalchemy as sa

from app.clhear.l1 import originals, pipeline, spans
from app.clhear.l1.adapters.base import Artifact, CLAUSE_TYPES, DocNode
from app.clhear.l1.adapters.html_document import parse as parse_html
from app.clhear.l1.adapters.xml_document import parse as parse_xml
from app.clhear.l1.adapters.json_document import parse as parse_json
from app.clhear.l1.models import clauses, doc_nodes


def proof(key, adapter, body, tree, name="document.html"):
    artifacts = [Artifact(name, body)]
    originals.attach_source_locations(key, adapter, artifacts, tree)
    return originals.verify_original_projection(key, adapter, artifacts, tree)


def html_fixture(count=2):
    body = ("<main><h1>Fixture source</h1>" + "".join(f"<h2>Section {i}</h2><p>Record {i} must preserve café 🪐.</p>" for i in range(count)) + "</main>").encode()
    tree = parse_html(body, "fixture/source")
    return body, tree


def test_exact_html_order_multiplicity_additions_and_hierarchy():
    body, tree = html_fixture()
    assert proof("fixture/source", "official_html", body, tree)["verified"]
    for mutation in ("order", "duplicate", "addition", "parent"):
        changed = copy.deepcopy(tree)
        heading = changed[0].children[0]
        if mutation == "order":
            heading.children.reverse()
        elif mutation == "duplicate":
            heading.children[0].children.append(copy.deepcopy(heading.children[0].children[0]))
        elif mutation == "addition":
            heading.children[0].children[0].raw_text += " Added wording."
        else:
            # Move a terminal paragraph to its parent's parent without changing
            # document order. Text-only coverage still equals one.
            child = heading.children[-1].children.pop()
            heading.children.append(child)
        report = originals.verify_original_projection("fixture/source", "official_html", [Artifact("document.html", body)], changed)
        assert not report["verified"], mutation


def test_complete_clause_bijection_including_late_missing_and_extra(engine):
    body, tree = html_fixture(205)
    assert proof("fixture/source", "official_html", body, tree)["verified"]
    # Persist using the production encoder in the disposable test database.
    with engine.begin() as conn:
        meta = __import__("app.clhear.l1.adapters.official_html", fromlist=["OfficialHtmlAdapter"]).OfficialHtmlAdapter(
            "fixture/source", "Fixture", "https://example.invalid/source").meta()
        _, source_id = pipeline.ensure_source(conn, meta)
        from app.clhear.l1.models import source_versions
        version_id = conn.execute(source_versions.insert().values(source_id=source_id, version_label="fixture:1", content_hash="fixture", s3_uri="fixture", status="in_force").returning(source_versions.c.id)).scalar_one()
        projected = pipeline.persist_tree(conn, version_id, tree, True)
        conn.execute(clauses.insert(), projected)
        rows = conn.execute(sa.select(doc_nodes).where(doc_nodes.c.source_version_id == version_id)).mappings().all()
        clause_rows = list(conn.execute(sa.select(clauses).where(clauses.c.source_version_id == version_id)).mappings())
    result = originals.verify_original_projection("fixture/source", "official_html", [Artifact("document.html", body)], rows, clause_rows)
    assert result["verified"], result["findings"]
    assert result["expected_clause_count"] == result["observed_clause_count"] > 200
    assert not originals.verify_original_projection("fixture/source", "official_html", [Artifact("document.html", body)], rows, clause_rows[:-1])["verified"]
    assert not originals.verify_original_projection("fixture/source", "official_html", [Artifact("document.html", body)], rows, clause_rows + [clause_rows[-1]])["verified"]
    changed = [dict(r) for r in clause_rows]
    changed[-1]["ref"] = "wrong-legal-identity"
    assert not originals.verify_original_projection("fixture/source", "official_html", [Artifact("document.html", body)], rows, changed)["verified"]


def test_unicode_span_and_original_location_are_separate_exact_representations():
    body, tree = html_fixture()
    assert proof("fixture/source", "official_html", body, tree)["verified"]
    canonical = spans.canonical_text(tree)
    layout = spans.span_layout(tree)
    for root in tree:
        for node in root.walk():
            start, end = layout[id(node)]
            assert canonical[start:end] == node.subtree_text()
    text_node = next(n for r in tree for n in r.walk() if "🪐" in n.raw_text)
    assert text_node.source_locator["offset_unit"] == "unicode_code_points"
    text_node.source_locator["fields"]["raw_text"]["end"] += 1
    report = originals.verify_original_projection("fixture/source", "official_html", [Artifact("document.html", body)], tree)
    assert any(f["code"] == "source_locator_mismatch" for f in report["findings"])


def test_clml_complete_scope_literal_markers_and_parent_ids():
    body = b'<Legislation><Secondary><Body><P1group><Title>Definitions</Title><P1 id="regulation-1"><Pnumber>1</Pnumber><P1para><Text>The term <Emphasis>record</Emphasis> includes all records.</Text><P2 id="regulation-1-1"><Pnumber>1</Pnumber><P2para><Text>They must be retained.</Text></P2para></P2></P1para></P1></P1group></Body><Schedules><Schedule id="schedule-1"><Title>Required fields</Title><Text>Additional source content.</Text></Schedule></Schedules></Secondary></Legislation>'
    tree = parse_xml(body, "fixture/xml", "uk_legislation")
    assert proof("fixture/xml", "uk_legislation", body, tree, "document.xml")["verified"]
    refs = {n.ref for r in tree for n in r.walk() if n.node_type in CLAUSE_TYPES}
    assert refs == {"regulation-1", "regulation-1-1", "schedule-1"}
    clause = next(n for r in tree for n in r.walk() if n.ref == "regulation-1")
    assert "They must be retained." in clause.subtree_text()
    nested = next(n for r in tree for n in r.walk() if n.ref == "regulation-1-1")
    nested.source_locator["attributes"]["id"] = "regulation-99"
    assert not originals.verify_original_projection("fixture/xml", "uk_legislation", [Artifact("document.xml", body)], tree)["verified"]


def test_oscal_keeps_all_guidance_parameters_and_only_controls_as_clauses():
    value = {"catalog": {"metadata": {"title": "Test catalog", "version": "1"}, "groups": [{"id": "ac", "title": "Access", "controls": [{"id": "ac-1", "title": "Policy", "params": [{"id": "ac-1_prm", "label": "retention period"}], "parts": [{"id": "ac-1_smt", "name": "statement", "prose": "Records must be retained."}, {"id": "ac-1_gdn", "name": "guidance", "prose": "Guidance remains actual source content."}], "controls": [{"id": "ac-1.1", "title": "Enhancement", "parts": []}]}]}]}}
    body = json.dumps(value).encode()
    tree = parse_json(body, "nist/sp800-53r5")
    assert proof("nist/sp800-53r5", "nist", body, tree, "catalog.json")["verified"]
    assert {n.ref for r in tree for n in r.walk() if n.node_type in CLAUSE_TYPES} == {"ac-1", "ac-1.1"}
    control = next(n for r in tree for n in r.walk() if n.ref == "ac-1")
    assert "retention period" in control.subtree_text() and "Guidance remains actual source content." in control.subtree_text()
    control.children.reverse()
    assert not originals.verify_original_projection("nist/sp800-53r5", "nist", [Artifact("catalog.json", body)], tree)["verified"]


def minimal_pdf(lines):
    """A generated test-only PDF; no publisher text or network required."""
    stream = ("BT /F1 12 Tf 40 760 Td " + " ".join(f"({line}) Tj 0 -20 Td" for line in lines) + " ET").encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    body, offsets = b"%PDF-1.4\n", [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(body)); body += str(i).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(body)
    body += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(f"{n:010d} 00000 n \n".encode() for n in offsets[1:])
    return body + b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"


@pytest.mark.parametrize("adapter_key", ["restricted_file", "govinfo_us", "finra"])
def test_real_pdf_bytes_two_decoders_sections_and_wrong_parent(adapter_key):
    from app.clhear.l1.adapters.pdf_docling import extract_pdf_pages, pages_to_tree
    body = minimal_pdf(["1 Scope", "Records must be retained.", "1.1 Evidence", "Evidence must be preserved.", "2 Review", "Review is required."])
    tree = pages_to_tree(extract_pdf_pages(body), "fixture/pdf", "Test PDF")
    report = proof("fixture/pdf", adapter_key, body, tree, "document.pdf")
    assert report["verified"], report["findings"]
    assert report["artifact_evidence"][0]["pages"][0]["text_blocks"] > 0
    section = tree[0].children[-1]
    child = section.children.pop()
    tree[0].children.append(child)
    assert not originals.verify_original_projection("fixture/pdf", adapter_key, [Artifact("document.pdf", body)], tree)["verified"]


def test_pdf_contents_listing_then_body_is_resolved_once_and_still_verified():
    """SR-filing PDFs open with a numbered contents list, then the body restarts
    at the same first marker. The listing is kept verbatim as unnumbered text;
    a mid-body repeat stays unresolvable."""
    from app.clhear.l1.adapters.pdf_docling import extract_pdf_pages, pages_to_tree
    body = minimal_pdf(["1 Purpose", "2 Statutory Basis", "1 Purpose", "FINRA proposes a change.", "2 Statutory Basis", "Section 15A applies."])
    tree = pages_to_tree(extract_pdf_pages(body), "fixture/filing", "Test filing")
    kinds = [n.node_type for n in tree[0].children]
    assert kinds == ["paragraph", "paragraph", "section", "section"]
    assert [n.ref for n in tree[0].children[2:]] == ["fixture/filing/section/1", "fixture/filing/section/2"]
    assert tree[0].children[0].raw_text == "1 Purpose" and "FINRA proposes a change." in tree[0].children[2].subtree_text()
    report = proof("fixture/filing", "finra", body, tree, "document.pdf")
    assert report["verified"], report["findings"]
    with pytest.raises(ValueError, match="repeats a section"):
        pages_to_tree(extract_pdf_pages(minimal_pdf(["1 Scope", "text", "2 Review", "text", "2 Review again"])), "fixture/x", "T")


def test_finra_navigation_page_is_catalog_structure_not_a_failure():
    from app.clhear.l1.adapters.finra_document import NavigationPage, document_markup
    container = (b"<html><body><main><h1>ARTICLE IV BOARD OF DIRECTORS</h1><div class='node__content'>"
                 b"<div class='book-navigation'><ul><li><a href='/rules-guidance/rulebooks/corporate-organization/general-powers'>General Powers</a></li>"
                 b"<li><a href='/rules-guidance/rulebooks/corporate-organization/number-directors'>Number of Directors</a></li></ul></div></div></main></body></html>")
    with pytest.raises(NavigationPage):
        document_markup(container)
    with pytest.raises(ValueError, match="lacks an identified title"):
        document_markup(b"<html><body><main><h1>Untitled</h1><div class='node__content'></div></main></body></html>")


def test_numbered_publisher_pdf_uses_independent_line_and_parent_evidence():
    from app.clhear.l1.adapters.standards_bodies import FatfAdapter
    body = minimal_pdf(["A. POLICIES", "1. First requirement", "Records must be retained.", "2. Second requirement"])
    adapter = FatfAdapter(source_key="fixture/fatf", title="Test", url="https://example.invalid/test.pdf")
    tree = adapter.parse(body)
    report = proof("fixture/fatf", "fatf", body, tree, "document.pdf")
    assert report["verified"], report["findings"]
    assert {n.ref for r in tree for n in r.walk() if n.node_type == "provision"} == {"R.1", "R.2"}


def test_empty_original_and_unrecognized_hierarchy_are_never_verified():
    node = DocNode("provision", ref="1", raw_text="content")
    empty = originals.verify_original_projection("fixture/unknown", "unknown", [Artifact("empty.txt", b"")], [node])
    assert not empty["verified"]
    unknown = originals.verify_original_projection("fixture/unknown", "unknown", [Artifact("document.txt", b"content")], [node])
    assert unknown["status"] == "unsupported" and not unknown["verified"]


def test_parser_identity_covers_dependency_config_and_ignores_runtime_counter():
    from app.clhear.l1.adapters.official_html import OfficialHtmlAdapter
    adapter = OfficialHtmlAdapter("fixture/source", "Fixture", "https://example.invalid/source")
    first = pipeline.parser_identity(adapter)
    adapter.fetches = 10
    assert pipeline.parser_identity(adapter) == first
    adapter._url += "?edition=2"
    assert pipeline.parser_identity(adapter)["configuration_sha256"] != first["configuration_sha256"]
    assert "pdfminer.six" in first["dependencies"]


@pytest.mark.parametrize("key", ["uk_legislation", "eur_lex", "govinfo_us_usc", "govinfo_us_ecfr", "nist_sp800_53", "nist_csf"])
def test_archived_publisher_fixture_has_complete_independent_records(key):
    from app.clhear.l1.adapters import get_adapter
    adapter = get_adapter(key)
    result, meta = adapter.fetch(), adapter.meta()
    originals.attach_source_locations(meta.source_key, meta.adapter, result.artifacts, result.tree, meta.canonical_url)
    report = originals.verify_original_projection(meta.source_key, meta.adapter, result.artifacts, result.tree, canonical_url=meta.canonical_url)
    assert report["verified"], report["findings"]
    assert any(n.node_type in CLAUSE_TYPES and n.ref for r in result.tree for n in r.walk())
    if key == "govinfo_us_usc":
        by_ref = {n.ref: n for r in result.tree for n in r.walk() if n.ref}
        assert "sec1471" in by_ref and "sec1471(a)" in by_ref
        assert "withholdable payment" in by_ref["sec1471(a)"].subtree_text().lower()
        assert len(by_ref["sec1471"].subtree_text()) > len(by_ref["sec1471(a)"].subtree_text())
    if key == "govinfo_us_ecfr":
        assert any(n.ref == "1.1471-1" for r in result.tree for n in r.walk())


@pytest.mark.parametrize("schema,body", [
    ("ofac", b'<sdnList><sdnEntry><uid>11</uid><lastName>Test record</lastName><akaList><aka><uid>12</uid><lastName>First alias</lastName></aka><aka><uid>13</uid><lastName>Second alias</lastName></aka></akaList><remarks>Complete original remarks</remarks></sdnEntry></sdnList>'),
    ("ofsi", b'Unique ID,Name 1,Name 2,Other Information\n11,Test record,Alias,Complete original remarks\n')])
def test_structured_list_records_keep_all_fields_and_parent_identity(schema, body):
    from app.clhear.l1.adapters.list_records import parse_records
    source = "lists/uk-ofsi" if schema == "ofsi" else "lists/ofac-sdn"
    name = "list.csv" if schema == "ofsi" else "list.xml"
    tree = parse_records(body, name, source, "Test-only list")
    report = proof(source, "lists", body, tree, name)
    assert report["verified"], report["findings"]
    field = next(n for r in tree for n in r.walk() if n.raw_text == "Complete original remarks")
    field.raw_text = "Truncated"
    assert not originals.verify_original_projection(source, "lists", [Artifact(name, body)], tree)["verified"]


def test_list_identity_comes_from_root_record_even_when_alias_appears_first():
    from app.clhear.l1.adapters.list_records import parse_records
    body = b'<sdnList><sdnEntry><akaList><aka><uid>999</uid><lastName>Alias first</lastName></aka></akaList><uid>11</uid><lastName>Root record</lastName></sdnEntry></sdnList>'
    tree = parse_records(body, "list.xml", "lists/ofac-sdn", "Test list")
    assert tree[0].children[0].ref == "lists/ofac-sdn/11"
    assert proof("lists/ofac-sdn", "lists", body, tree, "list.xml")["verified"]
    with pytest.raises(ValueError, match="root publisher identity"):
        parse_records(body.replace(b"<uid>11</uid>", b""), "list.xml", "lists/ofac-sdn", "Test list")


def test_repair_refetch_failure_finishes_the_run_ledger(engine, tmp_path, monkeypatch):
    from app.clhear.l1.adapters.official_html import OfficialHtmlAdapter
    from app.clhear.l1.adapters.base import FetchResult
    from app.clhear.models import runs
    from app.clhear.settings import get_settings
    monkeypatch.setenv("CLHEAR_SALVAGE_CAP", "0")
    get_settings.cache_clear()

    class Retrying(OfficialHtmlAdapter):
        calls = 0
        def fetch(self, since_version=None):
            self.calls += 1
            if self.calls > 1:
                raise TimeoutError("test-only transport failure")
            body = b"<h1>Fixture</h1><p>Actual source text</p>"
            return FetchResult("fixture:1", [Artifact("document.html", body)], parse_html(body, self.meta().source_key))
        def expected_text(self, artifacts):
            # A faulty diagnostic oracle triggers re-acquisition even though
            # independent source comparison succeeds. Failure must close ledger.
            return ["Actual source text", "An expected block that was not received"]

    adapter = Retrying("fixture/retry", "Fixture", "https://example.invalid/source")
    result = pipeline.ingest(engine, adapter, pipeline.LocalStore(tmp_path / "artifacts"), index_embeddings=False)
    assert adapter.calls == 2
    assert result["status"] == "failed" and result["error_type"] == "TimeoutError"
    with engine.connect() as conn:
        state = conn.execute(sa.select(runs.c.outputs).where(runs.c.id == result["run_id"])).scalar_one()
    assert state["status"] == "failed" and state["previous_version_preserved"] is False
    get_settings.cache_clear()


@pytest.mark.parametrize("kind", ["xml", "html"])
def test_source_fragment_must_match_independent_original_element(kind):
    if kind == "xml":
        body = b'<Legislation><Secondary><Body><P1 id="regulation-1"><Pnumber>1</Pnumber><P1para><Text>Original fixture wording.</Text></P1para></P1></Body></Secondary></Legislation>'
        key, adapter, name = "fixture/xml", "uk_legislation", "document.xml"
        tree = parse_xml(body, key, adapter)
    else:
        body, tree = html_fixture()
        key, adapter, name = "fixture/source", "official_html", "document.html"
    assert proof(key, adapter, body, tree, name)["verified"]
    node = next(n for r in tree for n in r.walk() if n.source_fragment)
    node.source_fragment = "<p>Different source wording.</p>"
    result = originals.verify_original_projection(key, adapter, [Artifact(name, body)], tree)
    assert not result["verified"]
    assert any(f["code"] in {"source_fragment_mismatch", "publisher_hierarchy_mismatch"} for f in result["findings"])


def test_source_xml_fragment_accepts_equivalent_namespace_serialization():
    body = b'<Legislation xmlns="urn:fixture"><Secondary><Body><P1 id="regulation-1"><Pnumber>1</Pnumber><Text>Source wording.</Text></P1></Body></Secondary></Legislation>'
    key = "fixture/xml"
    tree = parse_xml(body, key, "uk_legislation")
    assert proof(key, "uk_legislation", body, tree, "document.xml")["verified"]
    node = next(n for r in tree for n in r.walk() if n.source_fragment)
    node.source_fragment = node.source_fragment.replace("ns0", "publisher")
    assert originals.verify_original_projection(key, "uk_legislation", [Artifact("document.xml", body)], tree)["verified"]


def test_independent_decoder_exception_returns_safe_diagnostic(monkeypatch):
    from pdfminer.pdfparser import PDFSyntaxError
    body, tree = html_fixture()
    monkeypatch.setattr(originals, "original_view", lambda *args, **kwargs: (_ for _ in ()).throw(PDFSyntaxError("private fixture diagnostic")))
    result = originals.verify_original_projection("fixture/source", "official_html", [Artifact("document.html", body)], tree)
    assert not result["verified"]
    assert result["findings"][0]["error_type"] == "PDFSyntaxError"
    assert "private fixture diagnostic" not in json.dumps(result)


def test_diagnostic_oracle_exception_finalizes_run(engine, tmp_path):
    from app.clhear.l1.adapters.official_html import OfficialHtmlAdapter
    from app.clhear.l1.adapters.base import FetchResult
    from app.clhear.models import runs
    from pdfminer.pdfparser import PDFSyntaxError

    class OracleFailure(OfficialHtmlAdapter):
        def fetch(self, since_version=None):
            body, tree = html_fixture()
            return FetchResult("fixture:1", [Artifact("document.html", body)], tree)
        def expected_text(self, artifacts):
            raise PDFSyntaxError("private fixture diagnostic")

    result = pipeline.ingest(engine, OracleFailure("fixture/source", "Fixture", "https://example.invalid"), pipeline.LocalStore(tmp_path / "artifacts"), index_embeddings=False)
    assert result["status"] == "failed" and result["error_type"] == "PDFSyntaxError"
    assert "private fixture diagnostic" not in json.dumps(result)
    with engine.connect() as conn:
        row = conn.execute(sa.select(runs).where(runs.c.id == result["run_id"])).mappings().one()
        assert row["outputs"]["status"] == "failed" and row["duration_ms"] is not None
        assert conn.execute(sa.select(sa.func.count()).select_from(clauses)).scalar() == 0


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_unchanged_import_repairs_archive_without_overwriting_valid_bytes(engine, tmp_path, damage):
    from pathlib import Path
    from urllib.parse import urlparse, unquote
    from app.clhear.l1.adapters.official_html import OfficialHtmlAdapter
    from app.clhear.l1.adapters.base import FetchResult

    class Archived(OfficialHtmlAdapter):
        def fetch(self, since_version=None):
            body, tree = html_fixture()
            return FetchResult("fixture:1", [Artifact("document.html", body)], tree)
        def expected_text(self, artifacts):
            return [originals.html_text(a.content) for a in artifacts]

    adapter = Archived("fixture/source", "Fixture", "https://example.invalid")
    store = pipeline.LocalStore(tmp_path / "originals")
    first = pipeline.ingest(engine, adapter, store, index_embeddings=False)
    assert first["status"] == "added"
    original = first["artifact_manifest"][0]
    path = Path(unquote(urlparse(original["uri"]).path))
    initial_mtime = path.stat().st_mtime_ns
    same = pipeline.ingest(engine, adapter, store, index_embeddings=False)
    assert same["status"] == "unchanged"
    assert path.stat().st_mtime_ns == initial_mtime
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt fixture bytes")
    repaired = pipeline.ingest(engine, adapter, store, index_embeddings=False)
    assert repaired["status"] == "amended"
    assert repaired["source_version_id"] != first["source_version_id"]
    fixed = repaired["artifact_manifest"][0]
    assert hashlib.sha256(store.get(fixed["key"])).hexdigest() == original["sha256"]
    if damage == "corrupt":
        assert path.read_bytes() == b"corrupt fixture bytes"
        assert fixed["key"] != original["key"]
    final = pipeline.ingest(engine, adapter, store, index_embeddings=False)
    assert final["status"] == "unchanged"
    assert final["source_version_id"] == repaired["source_version_id"]


def test_finra_fragment_cannot_claim_invented_markup():
    from app.clhear.l1.adapters.finra import parse
    body = b'<html><body><h1>2210. Communications with the Public</h1><p>(a) Fixture source wording.</p></body></html>'
    key = 'finra/rule/2210'
    tree = parse(body, key)
    assert proof(key, 'finra', body, tree)['verified']
    node = next(n for r in tree for n in r.walk() if n.source_fragment)
    node.source_fragment = '<h1>2210. Different fixture title</h1>'
    result = originals.verify_original_projection(key, 'finra', [Artifact('document.html', body)], tree)
    assert not result['verified']
    assert any(f['code'] == 'source_fragment_mismatch' for f in result['findings'])


def test_s3_original_store_uses_conditional_create_and_reuses_valid_object():
    class S3:
        def __init__(self):
            self.objects = {}
            self.puts = []
        def get_object(self, *, Bucket, Key):
            import io
            from botocore.exceptions import ClientError
            if Key not in self.objects:
                raise ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
            return {'Body': io.BytesIO(self.objects[Key])}
        def put_object(self, **kwargs):
            assert kwargs['IfNoneMatch'] == '*'
            self.puts.append(kwargs['Key'])
            self.objects[kwargs['Key']] = kwargs['Body']
    store = object.__new__(pipeline.S3Store)
    store.bucket, store._client = 'test-fixture', S3()
    store.put('original', b'actual fixture', 'text/plain')
    store.put('original', b'actual fixture', 'text/plain')
    assert store._client.puts == ['original']
    with pytest.raises(ValueError, match='different bytes'):
        store.put('original', b'changed fixture', 'text/plain')
    assert store._client.objects['original'] == b'actual fixture'
