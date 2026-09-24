"""SEC.gov publications: the press release or staff page, not the site around it.

sec.gov wraps each publication in navigation, breadcrumbs and a resources rail.
The publication is the page-title block and the main content region. One
publication is one provision: heading = the title block, raw text = the body,
which is also how the L7 ingestor reads an outcome (title line, then body).

``SecPageAdapter`` reads the page with BeautifulSoup. ``original_text`` and
``verify`` re-read the same two regions with the standard-library parser.
"""
from __future__ import annotations

import hashlib

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult
from app.clhear.l1.adapters.sec_edgar import SEC_HEADERS

TITLE_CLASS = "page-title"
BODY_CLASS = "node-details-layout__main-region__content"
DOCUMENT_TYPE = "sec_page"
STRUCTURE = "sec-publication"


def is_sec_page(content: bytes) -> bool:
    return BODY_CLASS.encode() in content and f'"{TITLE_CLASS}'.encode() in content


def _classes(value) -> list[str]:
    return value if isinstance(value, list) else (value or "").split()


def _bs4_text(el) -> str:
    from app.clhear.l1.originals import _HTMLText

    parts: list[str] = []

    def walk(node):
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if not isinstance(node, Tag) or node.name in _HTMLText.OMIT:
            return
        block = node.name in _HTMLText.BLOCKS
        if block:
            parts.append("\n")
        for child in node.children:
            walk(child)
        if block:
            parts.append("\n")

    walk(el)
    return " ".join("".join(parts).split())


def parse(content: bytes, source_key: str, title: str) -> list[DocNode]:
    soup = BeautifulSoup(content, "html.parser")
    heading_el = next((el for el in soup.find_all("div") if TITLE_CLASS in _classes(el.get("class"))), None)
    body_el = next((el for el in soup.find_all("div") if BODY_CLASS in _classes(el.get("class"))), None)
    if heading_el is None or body_el is None:
        raise ValueError("Expected an SEC publication page with a title block and a content region")
    heading, body = _bs4_text(heading_el), _bs4_text(body_el)
    if not heading or not body:
        raise ValueError("SEC publication title or body is empty")
    root = DocNode(node_type="title", ref=source_key, heading=title,
                   source_locator={"structure": STRUCTURE, "region": "document"})
    root.children.append(DocNode(node_type="provision", ref=f"{source_key}/publication", label=heading, heading=heading,
                                 raw_text=body, source_locator={"structure": STRUCTURE, "region": "publication"}))
    return [root]


def _regions(content: bytes) -> tuple[str, str]:
    """Independent read: stdlib DOM, the first title block and content region."""
    from app.clhear.l1.originals import _HTMLStructure, _HTMLText, _decode_html

    reader = _HTMLStructure()
    reader.feed(_decode_html(content))

    def find(node, cls):
        if isinstance(node, dict):
            if node["tag"] == "div" and cls in node.get("attrs", {}).get("class", "").split():
                return node
            for child in node["children"]:
                found = find(child, cls)
                if found is not None:
                    return found
        return None

    def text(node) -> str:
        parts: list[str] = []

        def walk(n):
            if isinstance(n, str):
                parts.append(n)
                return
            if n["tag"] in _HTMLText.OMIT:
                return
            block = n["tag"] in _HTMLText.BLOCKS
            if block:
                parts.append("\n")
            for child in n["children"]:
                walk(child)
            if block:
                parts.append("\n")

        walk(node)
        return " ".join("".join(parts).split())

    title, body = find(reader.root, TITLE_CLASS), find(reader.root, BODY_CLASS)
    if title is None or body is None:
        raise ValueError("SEC publication regions are missing")
    return text(title), text(body)


def original_text(content: bytes) -> str:
    return " ".join(_regions(content))


def verify(artifacts: list[Artifact], source_key: str, tree: list[DocNode]) -> bool:
    if len(artifacts) != 1 or len(tree) != 1:
        return False
    heading, body = _regions(artifacts[0].content)
    root = tree[0]
    if root.node_type != "title" or root.ref != source_key or root.raw_text or len(root.children) != 1:
        return False
    node = root.children[0]
    return (node.node_type == "provision" and node.ref == f"{source_key}/publication" and not node.children
            and node.source_locator.get("structure") == STRUCTURE
            and " ".join(node.heading.split()) == heading and " ".join(node.raw_text.split()) == body)


class SecPageAdapter:
    """One sec.gov publication page on an existing SEC lane (``sec_enforcement`` or ``sec_edgar``)."""

    def __init__(self, source_key: str, title: str, url: str, *, meta, adapter: str):
        self.key = adapter
        self._source_key, self._title, self._url, self._meta = source_key, title, url, meta

    def meta(self):
        return self._meta

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        content = http.get(self._url, headers=SEC_HEADERS)
        if not is_sec_page(content):
            raise ValueError("Expected an SEC publication page; a listing or landing page cannot stand in")
        label = "as_published:acquired-sha256-" + hashlib.sha256(content).hexdigest()
        if since_version == label:
            return None
        return FetchResult(version_label=label,
                           artifacts=[Artifact(name="publication.html", content=content, content_type="text/html")],
                           tree=parse(content, self._source_key, self._title), version_kind="as_published")

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        return [original_text(artifact.content) for artifact in artifacts]
