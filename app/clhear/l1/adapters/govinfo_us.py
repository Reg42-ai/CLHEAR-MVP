"""govinfo + eCFR adapter (HLD §7.2 adapter 3): FATCA — 26 USC ch. 4 (statute,
GPO govinfo HTML) and 26 CFR ch. 4 regulations (eCFR versioner API).

Emits a typed DocNode tree (section / subsection / paragraph) with raw text
and the exact HTML/XML fragment per node. IGAs stay reference-level per the
v1 charter (HLD §7.3).
"""
import re
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup, Tag

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta

USC_EDITION = "2023"
USC_SECTIONS = ("1471", "1472", "1473", "1474")
USC_URL = (
    "https://www.govinfo.gov/content/pkg/USCODE-{ed}-title26/html/"
    "USCODE-{ed}-title26-subtitleA-chap4-sec{sec}.htm"
)

# 26 CFR ch. 4 regulation series (T.D. 9610 et seq.). The eCFR structure API
# returns the whole 60MB+ title; the charter pins the section list instead.
# ARCH: switch to structure-API enumeration when the P2 reconciliation job lands.
ECFR_DATE = "2025-12-31"
ECFR_SECTIONS = (
    "1.1471-0", "1.1471-1", "1.1471-2", "1.1471-3", "1.1471-4", "1.1471-5", "1.1471-6",
    "1.1472-1",
    "1.1473-1",
    "1.1474-1", "1.1474-2", "1.1474-3", "1.1474-4", "1.1474-5", "1.1474-6", "1.1474-7",
)
ECFR_URL = (
    "https://www.ecfr.gov/api/versioner/v1/full/{date}/title-26.xml"
    "?chapter=I&subchapter=A&part=1&section={section}"
)

FAMILY = dict(
    family_key="us-fatca",
    family_name="US FATCA (26 USC ch. 4 + regulations)",
    issuer="US Congress / Treasury-IRS (published by GPO)",
    jurisdiction="US",
    license="open",
    license_ref="public domain (17 U.S.C. 105)",
)


def _field(html: str, name: str) -> str:
    """Slice one GPO field (the page's own section delimiters)."""
    start = html.find(f"<!-- field-start:{name} -->")
    end = html.find(f"<!-- field-end:{name} -->")
    if start == -1 or end == -1:
        return ""
    return html[start + len(f"<!-- field-start:{name} -->") : end]


class GovInfoUscAdapter:
    key = "govinfo_us_usc"

    def meta(self) -> SourceMeta:
        return SourceMeta(
            source_key="usc/26/ch4",
            name="26 USC §§1471–1474 — Taxes to enforce reporting on certain foreign accounts",
            kind="law",
            canonical_url="https://uscode.house.gov/view.xhtml?path=/prelim@title26/subtitleA/chapter4",
            adapter="govinfo_us",
            scope_charter={
                "binding": "statute + 26 CFR ch.4 + current FFI-agreement Rev. Proc. + form instructions",
                "out": ["IGAs (reference-level stubs in v1)"],
            },
            **FAMILY,
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        version_label = f"USCODE-{USC_EDITION}"
        if since_version == version_label:
            return None
        tree: list[DocNode] = []
        artifacts: list[Artifact] = []
        for sec in USC_SECTIONS:
            content = http.get(USC_URL.format(ed=USC_EDITION, sec=sec))
            artifacts.append(Artifact(name=f"sec{sec}.htm", content=content, content_type="text/html"))
            html = content.decode("utf-8", errors="replace")
            # The GPO page delimits its own sections with field comments; the
            # statute field is the law, the notes fields are annotations.
            statute = _field(html, "statute")
            head_html = _field(html, "head")
            head = BeautifulSoup(head_html, "html.parser").get_text(" ", strip=True) if head_html else f"§{sec}"
            children: list[DocNode] = []
            current: DocNode | None = None
            for el in BeautifulSoup(statute, "html.parser").find_all(["h4", "p"]):
                if not isinstance(el, Tag):
                    continue
                classes = [str(c) for c in (el.get("class") or [])]
                heading_class = next((c for c in classes if c.endswith("-head")), None)
                if el.name == "h4" and heading_class == "subsection-head":
                    heading = "".join(el.strings).strip()
                    match = re.match(r"\(([a-z0-9]+)\)", heading)
                    sub_ref = f"sec{sec}({match.group(1)})" if match else f"sec{sec}-{len(children)}"
                    current = DocNode(
                        node_type="subsection",
                        ref=sub_ref,
                        heading=heading,  # heading carries the printed "(a)" marker
                        source_fragment=str(el),
                    )
                    children.append(current)
                    continue
                if el.name == "h4" and heading_class:
                    # deeper heads inside a subsection: (1) In general, (A) …
                    node = DocNode(node_type="heading", raw_text="".join(el.strings).strip(), source_fragment=str(el))
                elif el.name == "p" and any(c.startswith("statutory-body") for c in classes):
                    node = DocNode(node_type="paragraph", raw_text="".join(el.strings), source_fragment=str(el))
                else:
                    continue
                if current is None:
                    current = DocNode(node_type="subsection", ref=f"sec{sec}(pre)", children=[node])
                    children.append(current)
                else:
                    current.children.append(node)
            tree.append(
                DocNode(
                    node_type="section",
                    ref=f"sec{sec}",
                    heading=head,  # heading carries the printed "§1471." marker
                    source_fragment=head_html,
                    children=children,
                )
            )
        return FetchResult(version_label=version_label, artifacts=artifacts, tree=tree)

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        """Fidelity oracle: the statute field of each page (between GPO's
        field-start/end comment markers) + the section heading. Declared
        exclusions: notes/source-credit/amendment fields (annotation, not law)."""
        spans: list[str] = []
        for artifact in artifacts:
            html = artifact.content.decode("utf-8", errors="replace")
            for field_name in ("head", "statute"):
                fragment = _field(html, field_name)
                if fragment:
                    parsed = BeautifulSoup(fragment, "html.parser")
                    spans.extend(str(s) for s in parsed.strings if str(s).strip())
        return spans


class GovInfoEcfrAdapter:
    key = "govinfo_us_ecfr"

    def __init__(self, as_of: str = ECFR_DATE):
        self.as_of = as_of

    def meta(self) -> SourceMeta:
        return SourceMeta(
            source_key="cfr/26/ch4",
            name="26 CFR §§1.1471–1.1474 — FATCA regulations",
            kind="regulation",
            issuer=FAMILY["issuer"],
            jurisdiction=FAMILY["jurisdiction"],
            license=FAMILY["license"],
            license_ref=FAMILY["license_ref"],
            family_key=FAMILY["family_key"],
            family_name=FAMILY["family_name"],
            canonical_url="https://www.ecfr.gov/current/title-26/chapter-I/subchapter-A/part-1",
            adapter="govinfo_us",
            scope_charter={"binding": "26 CFR ch.4 regulation series (T.D. 9610 et seq.)"},
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        version_label = f"eCFR-{self.as_of}"
        if since_version == version_label:
            return None
        tree: list[DocNode] = []
        artifacts: list[Artifact] = []
        for section in ECFR_SECTIONS:
            content = http.get(ECFR_URL.format(date=self.as_of, section=section))
            artifacts.append(Artifact(name=f"{section}.xml", content=content, content_type="application/xml"))
            root = ET.fromstring(content)
            head = (root.findtext("HEAD") or f"§ {section}").strip()
            children: list[DocNode] = []
            current: DocNode | None = None
            expected = "a"

            def emit(node: DocNode) -> None:
                nonlocal current
                if current is None:
                    current = DocNode(node_type="subsection", ref=f"{section}(pre)", children=[node])
                    children.append(current)
                else:
                    current.children.append(node)

            # Document order over the section's block elements: P paragraphs,
            # HD*/HED headings (outline + examples), PSPACE example bodies.
            nested_skip = set()
            for wrapper in root.iter("PSPACE"):
                for descendant in wrapper.iter():
                    if descendant is not wrapper:
                        nested_skip.add(id(descendant))
            for el in root.iter():
                if id(el) in nested_skip:
                    continue
                tag = el.tag
                text = "".join(el.itertext())
                if not text.strip():
                    continue
                fragment = ET.tostring(el, encoding="unicode")
                if tag == "P":
                    if expected is not None and text.lstrip().startswith(f"({expected})"):
                        current = DocNode(
                            node_type="subsection",
                            ref=f"{section}({expected})",
                            source_fragment=fragment,
                            children=[DocNode(node_type="paragraph", raw_text=text, source_fragment=fragment)],
                        )
                        children.append(current)
                        expected = chr(ord(expected) + 1) if expected != "z" else None
                    else:
                        emit(DocNode(node_type="paragraph", raw_text=text, source_fragment=fragment))
                elif tag in ("HD1", "HD2", "HD3", "HED"):
                    emit(DocNode(node_type="heading", raw_text=text, source_fragment=fragment))
                elif tag in ("PSPACE", "FP"):
                    emit(DocNode(node_type="paragraph", raw_text=text, source_fragment=fragment))
                elif tag == "CITA":
                    children.append(DocNode(node_type="note", raw_text=text, source_fragment=fragment))
            tree.append(
                DocNode(
                    node_type="section",
                    ref=section,
                    heading=head,  # heading carries the printed "§ 1.1471-0" marker
                    source_fragment=ET.tostring(root, encoding="unicode"),
                    children=children,
                )
            )
        return FetchResult(version_label=version_label, artifacts=artifacts, tree=tree)

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        """Fidelity oracle: every text piece of each eCFR section XML."""
        spans: list[str] = []
        for artifact in artifacts:
            root = ET.fromstring(artifact.content)
            spans.extend(piece for piece in root.itertext() if piece.strip())
        return spans
