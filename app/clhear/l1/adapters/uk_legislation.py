"""legislation.gov.uk adapter (HLD §7.2 adapter 1): CLML XML for MLRs 2017 +
the official effects feed as the family citator.

Emits a typed DocNode tree (Part/Chapter/group/provision/paragraph/schedule)
with raw text and the exact CLML fragment per node — no renderer.

Point-in-time support: legislation.gov.uk serves the consolidated text as of a
date (/uksi/2017/692/{date}/data.xml), which is how historical amendments are
replayed through the diff engine (P1 done-test).

License: Open Government Licence v3.0 — verbatim reproduction permitted.
"""
import xml.etree.ElementTree as ET

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, DocNode, EffectRecord, FetchResult, SourceMeta

BASE = "https://www.legislation.gov.uk"
CLML = "{http://www.legislation.gov.uk/namespaces/legislation}"
UKM = "{http://www.legislation.gov.uk/namespaces/metadata}"
DCT = "{http://purl.org/dc/terms/}"
DC = "{http://purl.org/dc/elements/1.1/}"

MLR_DOC = "uksi/2017/692"
MLR_NAME = (
    "The Money Laundering, Terrorist Financing and Transfer of Funds "
    "(Information on the Payer) Regulations 2017"
)

# Structural children we recurse into; Number/Title/Pnumber become label/heading.
_SKIP = frozenset(
    {"Number", "Title", "TitleBlock", "Pnumber", "Reference", "Metadata", "Commentaries", "ExplanatoryNotes"}
)


def _tag(el: ET.Element) -> str:
    return el.tag.split("}")[-1]


def _txt(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext())


def _child_txt(el: ET.Element, tag: str) -> str:
    return _txt(el.find(f"{CLML}{tag}"))


def _fragment(el: ET.Element) -> str:
    return ET.tostring(el, encoding="unicode")


def _label_for(tag: str, number: str) -> str:
    """Printed marker. P1 provisions print as '3.'; nested as '(1)'."""
    number = number.strip()
    if not number:
        return ""
    if tag == "P1":
        return number if number.endswith(".") else f"{number}."
    if tag in {"P2", "P3", "P4", "P5"}:
        return number if number.startswith("(") else f"({number})"
    return number


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

        tree: list[DocNode] = []
        body = doc_el.find(f"{CLML}Body")
        if body is not None:
            tree.extend(self._children(body))
        schedules = doc_el.find(f"{CLML}Schedules")
        if schedules is not None:
            tree.extend(self._children(schedules))
        signed = doc_el.find(f"{CLML}SignedSection")
        if signed is not None:
            tree.append(self._node(signed, "signature", ref=signed.get("id") or "signed"))

        return FetchResult(
            version_label=version_label,
            artifacts=[Artifact(name="data.xml", content=content, content_type="application/xml")],
            tree=tree,
        )

    def _children(self, el: ET.Element) -> list[DocNode]:
        out: list[DocNode] = []
        for child in el:
            tag = _tag(child)
            if tag in _SKIP:
                continue
            node = self._from_element(child)
            if node is not None:
                out.append(node)
        return out

    def _from_element(self, el: ET.Element) -> DocNode | None:
        tag = _tag(el)
        if tag == "Part":
            return self._node(el, "part", ref=el.get("id") or "", label=_child_txt(el, "Number"), heading=_child_txt(el, "Title"))
        if tag == "Chapter":
            return self._node(el, "chapter", ref=el.get("id") or "", label=_child_txt(el, "Number"), heading=_child_txt(el, "Title"))
        if tag == "P1group":
            return self._node(
                el, "group", ref=el.get("id") or "", heading=_child_txt(el, "Title")
            )
        if tag == "P1":
            # Body P1s are numbered regulations (clause grain). P1s inside a
            # Schedule are numbered paragraphs of that schedule — the schedule
            # container is the clause, so these stay in the tree as paragraphs.
            el_id = el.get("id") or ""
            if el_id.startswith("schedule-") or el_id.startswith("sch-"):
                return self._node(
                    el,
                    "paragraph",
                    ref=el_id,
                    label=_label_for("P1", _child_txt(el, "Pnumber")),
                )
            return self._node(
                el,
                "provision",
                ref=el_id,
                label=_label_for("P1", _child_txt(el, "Pnumber")),
            )
        if tag == "P2":
            return self._node(
                el,
                "paragraph",
                ref=el.get("id") or "",
                label=_label_for("P2", _child_txt(el, "Pnumber")),
            )
        if tag in {"P3", "P4", "P5"}:
            kind = "subparagraph" if tag == "P3" else "point"
            return self._node(
                el, kind, ref=el.get("id") or "", label=_label_for(tag, _child_txt(el, "Pnumber"))
            )
        if tag == "P":
            return self._node(el, "paragraph", ref=el.get("id") or "")
        if tag in {"P1para", "P2para", "P3para", "P4para", "P5para", "ScheduleBody"}:
            # Transparent wrappers: lift children to the parent.
            kids = self._children(el)
            if len(kids) == 1 and not kids[0].children and kids[0].node_type == "paragraph":
                return kids[0]
            return None  # caller uses _children; we handle wrappers there
        if tag == "Text":
            return DocNode(
                node_type="paragraph",
                raw_text=_txt(el),
                source_fragment=_fragment(el),
            )
        if tag == "Schedule":
            number = _child_txt(el, "Number")
            title = _child_txt(el, "TitleBlock") or _child_txt(el, "Title")
            return self._node(
                el,
                "schedule",
                ref=el.get("id") or "",
                label=number,
                heading=title,
            )
        if tag == "Schedules":
            return None
        if tag == "SignedSection":
            return self._node(el, "signature", ref=el.get("id") or "signed")
        # Unknown block: if it has Text descendants, keep as a paragraph of raw text.
        text = _txt(el).strip()
        if text:
            return DocNode(node_type="paragraph", raw_text=_txt(el), source_fragment=_fragment(el))
        return None

    def _node(self, el: ET.Element, node_type: str, *, ref: str = "", label: str = "", heading: str = "") -> DocNode:
        children: list[DocNode] = []
        raw_parts: list[str] = []
        for child in el:
            tag = _tag(child)
            if tag in _SKIP:
                continue
            if tag in {"P1para", "P2para", "P3para", "P4para", "P5para", "ScheduleBody"}:
                for lifted in self._children(child):
                    children.append(lifted)
                continue
            parsed = self._from_element(child)
            if parsed is not None:
                children.append(parsed)
        # Leaf-ish blocks with no structured children keep their own text.
        if not children and node_type in {"paragraph", "subparagraph", "point", "signature"}:
            raw_parts.append(_txt(el))
        return DocNode(
            node_type=node_type,
            ref=ref,
            label=label,
            heading=heading,
            raw_text="".join(raw_parts),
            source_fragment=_fragment(el),
            children=children,
        )

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
