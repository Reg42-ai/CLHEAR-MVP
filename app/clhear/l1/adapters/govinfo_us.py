"""govinfo + eCFR adapter (HLD §7.2 adapter 3): FATCA — 26 USC ch. 4 (statute,
GPO govinfo HTML) and 26 CFR ch. 4 regulations (eCFR versioner API).

US federal statute and regulation text is public domain. IGAs are stubbed at
reference level per the v1 charter (HLD §7.3); the IRS administrative layer
(Rev. Procs, form instructions) is the P3 `irs_gov` adapter.

Two adapter instances share this module: registry keys `govinfo_us_usc` and
`govinfo_us_ecfr` (sources.adapter is `govinfo_us` for both).
"""
import re
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, ClauseNode, FetchResult, SourceMeta

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
        tree: list[ClauseNode] = []
        artifacts: list[Artifact] = []
        ordering = 0
        for sec in USC_SECTIONS:
            content = http.get(USC_URL.format(ed=USC_EDITION, sec=sec))
            artifacts.append(Artifact(name=f"sec{sec}.htm", content=content, content_type="text/html"))
            soup = BeautifulSoup(content, "html.parser")
            head = soup.find("h3", class_="section-head")
            section_title = head.get_text(" ", strip=True) if head else f"§{sec}"
            children: list[ClauseNode] = []
            current: ClauseNode | None = None
            # statute field: subsection heads + statutory body paragraphs; the
            # notes fields (note-head etc.) are deliberately excluded.
            for el in soup.find_all(["h4", "p"]):
                classes = el.get("class") or []
                if el.name == "h4" and "subsection-head" in classes:
                    heading = el.get_text(" ", strip=True)
                    match = re.match(r"\(([a-z0-9]+)\)", heading)
                    sub_ref = f"sec{sec}({match.group(1)})" if match else f"sec{sec}-{len(children)}"
                    ordering += 1
                    current = ClauseNode(
                        ref=sub_ref,
                        path=f"26 USC §{sec}",
                        ordering=ordering,
                        text=heading,
                    )
                    children.append(current)
                elif el.name == "p" and any(c.startswith("statutory-body") for c in classes):
                    text = el.get_text(" ", strip=True)
                    if current is None:
                        ordering += 1
                        current = ClauseNode(
                            ref=f"sec{sec}(pre)", path=f"26 USC §{sec}", ordering=ordering, text=text
                        )
                        children.append(current)
                    else:
                        current.text += f"\n{text}"
            ordering += 1
            tree.append(
                ClauseNode(
                    ref=f"sec{sec}",
                    path="26 USC ch. 4",
                    ordering=ordering,
                    text=section_title,
                    children=children,
                )
            )
        return FetchResult(version_label=version_label, artifacts=artifacts, clause_tree=tree)


class GovInfoEcfrAdapter:
    key = "govinfo_us_ecfr"

    def __init__(self, as_of: str = ECFR_DATE):
        self.as_of = as_of

    def meta(self) -> SourceMeta:
        return SourceMeta(
            source_key="cfr/26/ch4",
            name="26 CFR §§1.1471–1.1474 — FATCA regulations",
            kind="regulation",
            canonical_url="https://www.ecfr.gov/current/title-26/chapter-I/subchapter-A/part-1",
            adapter="govinfo_us",
            scope_charter={"binding": "26 CFR ch.4 regulation series (T.D. 9610 et seq.)"},
            **FAMILY,
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        version_label = f"eCFR-{self.as_of}"
        if since_version == version_label:
            return None
        tree: list[ClauseNode] = []
        artifacts: list[Artifact] = []
        ordering = 0
        for section in ECFR_SECTIONS:
            content = http.get(ECFR_URL.format(date=self.as_of, section=section))
            artifacts.append(Artifact(name=f"{section}.xml", content=content, content_type="application/xml"))
            root = ET.fromstring(content)
            head = root.findtext("HEAD", default=f"§ {section}").strip()
            children: list[ClauseNode] = []
            current: ClauseNode | None = None
            # Top-level subsections arrive in order (a), (b), (c)…; a new child
            # starts only when the NEXT expected letter appears, so nested
            # markers like (1) or roman (i) stay inside their parent subsection.
            expected = "a"
            for p in root.iter("P"):
                text = " ".join("".join(p.itertext()).split())
                if not text:
                    continue
                if expected is not None and text.startswith(f"({expected})"):
                    ordering += 1
                    current = ClauseNode(
                        ref=f"{section}({expected})",
                        path=f"26 CFR § {section}",
                        ordering=ordering,
                        text=text,
                    )
                    children.append(current)
                    expected = chr(ord(expected) + 1) if expected != "z" else None
                elif current is not None:
                    current.text += f"\n{text}"
                else:
                    ordering += 1
                    current = ClauseNode(
                        ref=f"{section}(pre)", path=f"26 CFR § {section}", ordering=ordering, text=text
                    )
                    children.append(current)
            ordering += 1
            tree.append(
                ClauseNode(
                    ref=section,
                    path="26 CFR part 1 (FATCA series)",
                    ordering=ordering,
                    text=head,
                    children=children,
                )
            )
        return FetchResult(version_label=version_label, artifacts=artifacts, clause_tree=tree)
