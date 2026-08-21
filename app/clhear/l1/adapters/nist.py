"""NIST spine adapter (HLD §7.3): SP 800-53 rev 5 (OSCAL catalog) and CSF 2.0
(CPRT export). Public domain — the open canonical infosec text.

Two adapter instances share this module: registry keys `nist_sp800_53` and
`nist_csf` (sources.adapter is `nist` for both).
"""
import json

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, ClauseNode, FetchResult, SourceMeta

OSCAL_URL = (
    "https://raw.githubusercontent.com/usnistgov/oscal-content/main/"
    "nist.gov/SP800-53/rev5/json/NIST_SP-800-53_rev5_catalog.json"
)
CSF_URL = (
    "https://csrc.nist.gov/extensions/nudp/services/json/nudp/framework/"
    "version/csf_2_0_0/export/json?element=all"
)

FAMILY = dict(
    family_key="nist-spine",
    family_name="NIST spine (SP 800-53 r5 + CSF 2.0)",
    issuer="NIST",
    jurisdiction="US",
    license="open",
    license_ref="public domain (17 U.S.C. 105)",
)


class NistSp80053Adapter:
    key = "nist_sp800_53"

    def meta(self) -> SourceMeta:
        return SourceMeta(
            source_key="nist/sp800-53r5",
            name="NIST SP 800-53 Revision 5 — Security and Privacy Controls",
            kind="standard",
            canonical_url="https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final",
            adapter="nist",
            scope_charter={"binding": "full text (public domain)"},
            **FAMILY,
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        content = http.get(OSCAL_URL)
        catalog = json.loads(content)["catalog"]
        version_label = f"oscal-{catalog['metadata']['version']}"
        if since_version == version_label:
            return None

        tree: list[ClauseNode] = []
        ordering = 0
        for group in catalog.get("groups", []):
            group_label = f"{group['id'].upper()} — {group['title']}"
            for control in group.get("controls", []):
                ordering += 1
                tree.append(self._control_node(control, group_label, ordering))
                for enhancement in control.get("controls", []):
                    ordering += 1
                    tree.append(
                        self._control_node(enhancement, f"{group_label} > {control['id'].upper()}", ordering)
                    )
        return FetchResult(
            version_label=version_label,
            artifacts=[Artifact(name="catalog.json", content=content, content_type="application/json")],
            clause_tree=tree,
        )

    def _control_node(self, control: dict, path: str, ordering: int) -> ClauseNode:
        lines = [f"{control['id'].upper()} {control['title']}"]
        params = {}
        for p in control.get("params", []):
            choices = p.get("select", {}).get("choice", [])
            params[p["id"]] = p.get("label") or "; ".join(
                c if isinstance(c, str) else c.get("value", "") for c in choices
            )
        for part in control.get("parts", []):
            if part.get("name") == "statement":
                lines.extend(self._prose(part, params))
        return ClauseNode(ref=control["id"], path=path, ordering=ordering, text="\n".join(lines))

    def _prose(self, part: dict, params: dict) -> list[str]:
        lines = []
        label = next((p["value"] for p in part.get("props", []) if p.get("name") == "label"), "")
        prose = part.get("prose", "")
        if prose:
            for pid, replacement in params.items():
                prose = prose.replace("{{ insert: param, " + pid + " }}", f"[{replacement}]")
            lines.append(f"{label} {prose}".strip())
        for sub in part.get("parts", []):
            lines.extend(self._prose(sub, params))
        return lines


class NistCsfAdapter:
    key = "nist_csf"

    def meta(self) -> SourceMeta:
        return SourceMeta(
            source_key="nist/csf-2.0",
            name="NIST Cybersecurity Framework (CSF) 2.0",
            kind="standard",
            canonical_url="https://www.nist.gov/cyberframework",
            adapter="nist",
            scope_charter={"binding": "full text (public domain)"},
            **FAMILY,
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        content = http.get(CSF_URL)
        payload = json.loads(content)["response"]["elements"]
        doc = payload["documents"][0]
        version_label = "-".join(doc["version"].split())  # "Version  2.0" -> "Version-2.0"
        if since_version == version_label:
            return None

        elements = payload["elements"]
        functions = [e for e in elements if e["element_type"] == "function"]
        categories = [e for e in elements if e["element_type"] == "category"]
        subcategories = [e for e in elements if e["element_type"] == "subcategory"]

        tree: list[ClauseNode] = []
        ordering = 0
        for fn in functions:
            fn_id = fn["element_identifier"]
            fn_label = f"{fn_id} — {fn.get('title', '')}".strip(" —")
            ordering += 1
            tree.append(
                ClauseNode(
                    ref=fn_id,
                    path=fn_label,
                    ordering=ordering,
                    text=_text_of(fn),
                )
            )
            for cat in [c for c in categories if c["element_identifier"].startswith(f"{fn_id}.")]:
                cat_id = cat["element_identifier"]
                ordering += 1
                tree.append(ClauseNode(ref=cat_id, path=fn_label, ordering=ordering, text=_text_of(cat)))
                for sub in [s for s in subcategories if s["element_identifier"].startswith(f"{cat_id}-")]:
                    ordering += 1
                    tree.append(
                        ClauseNode(
                            ref=sub["element_identifier"],
                            path=f"{fn_label} > {cat_id}",
                            ordering=ordering,
                            text=_text_of(sub),
                        )
                    )
        return FetchResult(
            version_label=version_label,
            artifacts=[Artifact(name="csf-export.json", content=content, content_type="application/json")],
            clause_tree=tree,
        )


def _text_of(element: dict) -> str:
    ident = element["element_identifier"]
    title = element.get("title", "").strip()
    text = element.get("text", "").strip()
    head = f"{ident}: {title}" if title else ident
    return f"{head}\n{text}" if text else head
