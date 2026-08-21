"""NIST spine adapter (HLD §7.3): SP 800-53 rev 5 (OSCAL catalog) and CSF 2.0
(CPRT export). Public domain — the open canonical infosec text.

Emits a typed DocNode tree:
  800-53: group / control / enhancement / statement (raw OSCAL prose, params
          left as `{{ insert: param, … }}` — no substitution)
  CSF:    part (function) / group (category) / provision (subcategory)
"""
import json

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta

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

        tree: list[DocNode] = []
        for group in catalog.get("groups", []):
            controls = []
            for control in group.get("controls", []):
                node = self._control_node(control, "control")
                node.children.extend(
                    self._control_node(enh, "enhancement") for enh in control.get("controls", [])
                )
                controls.append(node)
            tree.append(
                DocNode(
                    node_type="group",
                    ref=group["id"],
                    label=group["id"].upper(),
                    heading=group.get("title", ""),
                    source_fragment=json.dumps({"id": group["id"], "title": group.get("title")}, ensure_ascii=False),
                    children=controls,
                )
            )
        return FetchResult(
            version_label=version_label,
            artifacts=[Artifact(name="catalog.json", content=content, content_type="application/json")],
            tree=tree,
        )

    def _control_node(self, control: dict, node_type: str) -> DocNode:
        statements = []
        for part in control.get("parts", []):
            if part.get("name") == "statement":
                statements.extend(self._statement_nodes(part, control["id"]))
        return DocNode(
            node_type=node_type,
            ref=control["id"],
            label=control["id"].upper(),
            heading=control.get("title", ""),
            source_fragment=json.dumps(
                {"id": control["id"], "title": control.get("title"), "parts": control.get("parts", [])},
                ensure_ascii=False,
            ),
            children=statements,
        )

    def _statement_nodes(self, part: dict, parent_ref: str) -> list[DocNode]:
        label = next((p["value"] for p in part.get("props", []) if p.get("name") == "label"), "")
        prose = part.get("prose") or ""
        # Nested statement parts (a. / a.1. / …) become child statements.
        children = []
        for sub in part.get("parts", []):
            children.extend(self._statement_nodes(sub, parent_ref))
        if not prose and not children:
            return []
        return [
            DocNode(
                node_type="statement",
                ref=f"{parent_ref}:{label}" if label else "",
                label=label,
                raw_text=prose,
                source_fragment=json.dumps(part, ensure_ascii=False),
                children=children,
            )
        ]


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

        tree: list[DocNode] = []
        for fn in functions:
            fn_id = fn["element_identifier"]
            cat_nodes = []
            for cat in [c for c in categories if c["element_identifier"].startswith(f"{fn_id}.")]:
                cat_id = cat["element_identifier"]
                subs = [
                    DocNode(
                        node_type="provision",
                        ref=sub["element_identifier"],
                        label=sub["element_identifier"],
                        heading=sub.get("title", "").strip(),
                        raw_text=sub.get("text", "").strip(),
                        source_fragment=json.dumps(sub, ensure_ascii=False),
                    )
                    for sub in subcategories
                    if sub["element_identifier"].startswith(f"{cat_id}-")
                ]
                cat_nodes.append(
                    DocNode(
                        node_type="group",
                        ref=cat_id,
                        label=cat_id,
                        heading=cat.get("title", "").strip(),
                        raw_text=cat.get("text", "").strip(),
                        source_fragment=json.dumps(cat, ensure_ascii=False),
                        children=subs,
                    )
                )
            tree.append(
                DocNode(
                    node_type="part",
                    ref=fn_id,
                    label=fn_id,
                    heading=fn.get("title", "").strip(),
                    raw_text=fn.get("text", "").strip(),
                    source_fragment=json.dumps(fn, ensure_ascii=False),
                    children=cat_nodes,
                )
            )
        return FetchResult(
            version_label=version_label,
            artifacts=[Artifact(name="csf-export.json", content=content, content_type="application/json")],
            tree=tree,
        )
