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
            soup = BeautifulSoup(content, "html.parser")
            head = soup.find("h3", class_="section-head")
            children: list[DocNode] = []
            current: DocNode | None = None
            # statute field: subsection heads + statutory body paragraphs; the
            # notes fields (note-head etc.) are deliberately excluded.
            for el in soup.find_all(["h4", "p"]):
                if not isinstance(el, Tag):
                    continue
                classes = el.get("class") or []
                if el.name == "h4" and "subsection-head" in classes:
                    heading = "".join(el.strings).strip()
                    match = re.match(r"\(([a-z0-9]+)\)", heading)
                    sub_ref = f"sec{sec}({match.group(1)})" if match else f"sec{sec}-{len(children)}"
                    current = DocNode(
                        node_type="subsection",
                        ref=sub_ref,
                        label=f"({match.group(1)})" if match else heading,
                        heading=heading,
                        source_fragment=str(el),
                    )
                    children.append(current)
                elif el.name == "p" and any(str(c).startswith("statutory-body") for c in classes):
                    text = "".join(el.strings)
                    para = DocNode(
                        node_type="paragraph",
                        raw_text=text,
                        source_fragment=str(el),
                    )
                    if current is None:
                        current = DocNode(
                            node_type="subsection",
                            ref=f"sec{sec}(pre)",
                            label="",
                            source_fragment=str(el),
                            children=[para],
                        )
                        children.append(current)
                    else:
                        current.children.append(para)
            tree.append(
                DocNode(
                    node_type="section",
                    ref=f"sec{sec}",
                    label=f"§{sec}",
                    heading="".join(head.strings).strip() if head else f"§{sec}",
                    source_fragment=str(head) if head else "",
                    children=children,
                )
            )
        return FetchResult(version_label=version_label, artifacts=artifacts, tree=tree)


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
            for p in root.iter("P"):
                text = "".join(p.itertext())
                if not text.strip():
                    continue
                fragment = ET.tostring(p, encoding="unicode")
                if expected is not None and text.lstrip().startswith(f"({expected})"):
                    current = DocNode(
                        node_type="subsection",
                        ref=f"{section}({expected})",
                        label=f"({expected})",
                        source_fragment=fragment,
                        children=[DocNode(node_type="paragraph", raw_text=text, source_fragment=fragment)],
                    )
                    children.append(current)
                    expected = chr(ord(expected) + 1) if expected != "z" else None
                elif current is not None:
                    current.children.append(DocNode(node_type="paragraph", raw_text=text, source_fragment=fragment))
                else:
                    current = DocNode(
                        node_type="subsection",
                        ref=f"{section}(pre)",
                        source_fragment=fragment,
                        children=[DocNode(node_type="paragraph", raw_text=text, source_fragment=fragment)],
                    )
                    children.append(current)
            tree.append(
                DocNode(
                    node_type="section",
                    ref=section,
                    label=f"§ {section}",
                    heading=head,
                    source_fragment=ET.tostring(root, encoding="unicode"),
                    children=children,
                )
            )
        return FetchResult(version_label=version_label, artifacts=artifacts, tree=tree)
