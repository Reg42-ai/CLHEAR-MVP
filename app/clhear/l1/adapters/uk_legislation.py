"""legislation.gov.uk adapter (HLD §7.2 adapter 1): CLML XML for MLRs 2017 +
the official effects feed as the family citator.

Point-in-time support: legislation.gov.uk serves the consolidated text as of a
date (/uksi/2017/692/{date}/data.xml), which is how historical amendments are
replayed through the diff engine (P1 done-test).

License: Open Government Licence v3.0 — verbatim reproduction permitted.
"""
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, ClauseNode, EffectRecord, FetchResult, SourceMeta

BASE = "https://www.legislation.gov.uk"
CLML = "{http://www.legislation.gov.uk/namespaces/legislation}"
UKM = "{http://www.legislation.gov.uk/namespaces/metadata}"
DCT = "{http://purl.org/dc/terms/}"
DC = "{http://purl.org/dc/elements/1.1/}"
ATOM = "{http://www.w3.org/2005/Atom}"

MLR_DOC = "uksi/2017/692"
MLR_NAME = (
    "The Money Laundering, Terrorist Financing and Transfer of Funds "
    "(Information on the Payer) Regulations 2017"
)


def _norm(text: str) -> str:
    return " ".join(text.split())


def _txt(el: ET.Element) -> str:
    return _norm("".join(el.itertext()))


def _tag(el: ET.Element) -> str:
    return el.tag.split("}")[-1]


@dataclass
class _Renderer:
    """CLML block elements -> display lines. Verbatim text; only whitespace and
    provision-number presentation (`3.`, `(1)`, `(a)`) are normalized."""

    lines: list[str] = field(default_factory=list)
    pending: str = ""

    def visit(self, el: ET.Element, parent_tag: str = "") -> None:
        t = _tag(el)
        if t in ("Title", "TitleBlock"):
            self._flush()
            title = _txt(el)
            if title:
                self.lines.append(title)
            return
        if t == "Pnumber":
            num = _txt(el)
            fmt = f"{num}." if parent_tag == "P1" else f"({num})"
            self.pending = f"{self.pending}\u2014{fmt}" if self.pending else fmt
            return
        if t == "Text":
            text = _txt(el)
            self.lines.append(f"{self.pending} {text}".strip() if self.pending else text)
            self.pending = ""
            return
        for child in el:
            self.visit(child, t)

    def _flush(self) -> None:
        if self.pending:
            self.lines.append(self.pending)
            self.pending = ""

    def render(self, el: ET.Element) -> str:
        self.lines, self.pending = [], ""
        for child in el:
            self.visit(child, _tag(el))
        self._flush()
        return "\n".join(self.lines)


def _render(el: ET.Element) -> str:
    return _Renderer().render(el)


class UkLegislationAdapter:
    key = "uk_legislation"

    def __init__(self, doc: str = MLR_DOC, name: str = MLR_NAME, snapshot: str | None = None):
        self.doc = doc
        self.name = name
        # Point-in-time date (YYYY-MM-DD) for historical replay; None = current.
        self.snapshot = snapshot

    def meta(self) -> SourceMeta:
        return SourceMeta(
            family_key="uk-mlr",
            family_name="UK Money Laundering Regulations (MLRs 2017)",
            source_key=self.doc,
            name=self.name,
            kind="regulation",
            issuer="HM Treasury (published by The National Archives)",
            jurisdiction="UK",
            license="open",
            license_ref="OGL-UK-3.0",
            canonical_url=f"{BASE}/{self.doc}",
            adapter=self.key,
            scope_charter={
                "binding": "principal SI + all amending SIs (citator feed)",
                "guidance": "JMLSG (later)",
                "out": ["FCA speeches", "consultations"],
            },
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        path = f"{self.doc}/{self.snapshot}/data.xml" if self.snapshot else f"{self.doc}/data.xml"
        content = http.get(f"{BASE}/{path}")
        root = ET.fromstring(content)

        valid = root.findtext(f"{UKM}Metadata/{DCT}valid") or root.findtext(f"{UKM}Metadata/{DC}modified")
        version_label = f"consolidated-{self.snapshot or valid}"
        if since_version == version_label:
            return None

        doc_el = root.find(f"{CLML}Secondary")
        if doc_el is None:
            doc_el = root.find(f"{CLML}Primary")
        if doc_el is None:
            raise ValueError(f"no Secondary/Primary element in CLML for {self.doc}")
        tree: list[ClauseNode] = []
        ordering = 0

        body = doc_el.find(f"{CLML}Body")
        for part in body.findall(f"{CLML}Part") if body is not None else []:
            part_label = _heading(part)
            for group in part.findall(f"{CLML}P1group"):
                for node in self._p1group_nodes(group, part_label):
                    node.ordering = ordering = ordering + 1
                    tree.append(node)
            for chapter in part.findall(f"{CLML}Chapter"):
                chapter_label = _join_path(part_label, _heading(chapter))
                for group in chapter.iter(f"{CLML}P1group"):
                    for node in self._p1group_nodes(group, chapter_label):
                        node.ordering = ordering = ordering + 1
                        tree.append(node)
        # P1groups directly in the body (no Part wrapper), e.g. small SIs.
        for group in body.findall(f"{CLML}P1group") if body is not None else []:
            for node in self._p1group_nodes(group, ""):
                node.ordering = ordering = ordering + 1
                tree.append(node)

        schedules = doc_el.find(f"{CLML}Schedules")
        for schedule in schedules.findall(f"{CLML}Schedule") if schedules is not None else []:
            ref = schedule.get("id") or f"schedule-{ordering}"
            title_block = schedule.find(f"{CLML}TitleBlock")
            number = schedule.find(f"{CLML}Number")
            label = _norm(
                " — ".join(
                    x
                    for x in (
                        _txt(number) if number is not None else "",
                        _txt(title_block) if title_block is not None else "",
                    )
                    if x
                )
            )
            node = ClauseNode(ref=ref, path=label or ref, ordering=(ordering := ordering + 1), text=_render(schedule))
            tree.append(node)

        return FetchResult(
            version_label=version_label,
            artifacts=[Artifact(name="data.xml", content=content, content_type="application/xml")],
            clause_tree=tree,
        )

    def _p1group_nodes(self, group: ET.Element, part_label: str) -> list[ClauseNode]:
        title_el = group.find(f"{CLML}Title")
        title = _txt(title_el) if title_el is not None else ""
        p1s = group.findall(f"{CLML}P1")
        if not p1s:
            # Group without numbered provisions: emit the group itself.
            ref = group.get("id") or f"group-{title}"
            return [ClauseNode(ref=ref, path=_join_path(part_label, title), ordering=0, text=_render(group))]
        nodes = []
        for p1 in p1s:
            ref = p1.get("id") or f"{title}-{len(nodes)}"
            text = _render(p1)
            if title:
                text = f"{title}\n{text}"
            nodes.append(ClauseNode(ref=ref, path=_join_path(part_label, title), ordering=0, text=text))
        return nodes

    # --- citator (official effects feed) -------------------------------------
    def family_effects(self) -> list[EffectRecord]:
        seen: dict[str, EffectRecord] = {}
        page = 1
        while page <= 10:  # politeness cap; MLRs currently needs 3 pages
            url = f"{BASE}/changes/affected/{self.doc}/data.feed?results-count=200&page={page}"
            root = ET.fromstring(http.get(url))
            for effect in root.iter(f"{UKM}Effect"):
                uri = effect.get("AffectingURI") or ""
                key = uri.split("/id/")[-1].strip("/")
                if not key or key == self.doc or key in seen:
                    continue
                title_el = effect.find(f"{UKM}AffectingTitle")
                affecting_class = effect.get("AffectingClass", "")
                seen[key] = EffectRecord(
                    affecting_key=key,
                    affecting_name=_txt(title_el) if title_el is not None else key,
                    affecting_url=f"{BASE}/{key}",
                    relation="amends",
                    kind="law" if "Act" in affecting_class else "regulation",
                )
            more = root.findtext(f"{CLML}morePages")
            total = root.findtext(f"{CLML}totalPages")
            if total and page >= int(total):
                break
            if more is not None and int(more) <= 0:
                break
            page += 1
        return sorted(seen.values(), key=lambda e: e.affecting_key)


def _join_path(*parts: str) -> str:
    return " > ".join(p for p in parts if p)


def _heading(el: ET.Element) -> str:
    number = el.find(f"{CLML}Number")
    title = el.find(f"{CLML}Title")
    parts = [_txt(x) for x in (number, title) if x is not None]
    return _norm(" — ".join(p for p in parts if p))
