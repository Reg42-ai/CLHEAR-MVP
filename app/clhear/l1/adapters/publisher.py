"""Publisher adapter bases (HLD v2 §4.1 starter corpus).

Two reusable structural parsers on top of the verbatim-text contract:

``NumberedHtmlAdapter``
    Walks an official HTML page in document order. Headings become
    ``section`` nodes; blocks that *start* with the publisher's provision
    numbering (``PRIN 2.1.1 R``, ``CRE20.1``, ``3110(a)``) become
    ``provision`` nodes with a stable ref; other blocks attach to the current
    provision (or section) as paragraphs. Visible text the walk did not
    capture is appended as a flagged note so the fidelity oracle stays whole.

``NumberedPdfAdapter``
    Same idea over PDF page text (Docling when installed, pypdf otherwise):
    line-based scanning with a heading pattern and a provision pattern.

Subclasses only declare ``key``, the patterns, and how to build a ref. Both
expose ``parse(content)`` so the clause-boundary golden set can score the
parser without network access, and both accept an optional
``renderer="crawl4ai"`` (used only when the package is installed) for pages
that need a browser to render their text.
"""
import re
from datetime import date

from bs4 import BeautifulSoup, Tag

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta
from app.clhear.l1.adapters.official_html import _BLOCK, _HEADING, _strip_chrome, _visible_strings
from app.clhear.l1.adapters.pdf_docling import extract_pdf_pages

__all__ = ["NumberedHtmlAdapter", "NumberedPdfAdapter", "render_html", "unique_ref"]


def render_html(url: str, *, renderer: str = "", headers: dict | None = None) -> bytes:
    """Fetch a page; with ``renderer='crawl4ai'`` and the package present, use a
    headless browser so JS-rendered publisher pages (FCA Handbook tabs, Basel
    Framework) yield their full text. Falls back to the polite HTTP client."""
    if renderer == "crawl4ai":
        try:  # pragma: no cover - optional heavy dependency
            import asyncio

            from crawl4ai import AsyncWebCrawler

            async def _run() -> bytes:
                async with AsyncWebCrawler() as crawler:
                    result = await crawler.arun(url=url)
                    return (result.html or "").encode()

            html = asyncio.run(_run())
            if html.strip():
                return html
        except Exception:
            pass
    return http.get(url, headers=headers)


def _promote_groups_without_provisions(nodes: list[DocNode]) -> None:
    """Headings are containers (``group``) when numbered provisions exist under
    them — provisions are the clause grain and must not nest inside another
    clause. A page with headings but no numbered provisions keeps its text
    clause-addressable by promoting the headings to ``section`` clauses."""
    if any(n.node_type == "provision" for node in nodes for n in node.walk()):
        return
    for node in nodes:
        if node.node_type == "group":
            node.node_type = "section"


def unique_ref(ref: str, seen: set[str]) -> str:
    """Refs must be unique per version (fidelity lint); suffix repeats."""
    if ref not in seen:
        seen.add(ref)
        return ref
    n = 2
    while f"{ref}#{n}" in seen:
        n += 1
    out = f"{ref}#{n}"
    seen.add(out)
    return out


class _PublisherBase:
    key = "publisher"
    publisher = ""
    instrument = ""
    kind = "regulation"
    jurisdiction = ""
    issuer = ""
    license = "open"
    version_kind = "consolidated"
    version_policy = "consolidated"
    # Subclasses override: provision numbering at block/line start. Must define
    # a named group ``ref`` and may define ``status``.
    PROVISION: re.Pattern | None = None
    # Optional heading pattern for PDFs / non-<h*> headings.
    HEADING: re.Pattern | None = None
    renderer = ""
    headers: dict | None = None

    def __init__(
        self,
        source_key: str,
        title: str,
        url: str,
        *,
        meta: SourceMeta | None = None,
        family_key: str = "",
        family_name: str = "",
        short_name: str = "",
        about: str = "",
        topics: list[str] | None = None,
        instrument: str = "",
        renderer: str | None = None,
    ):
        self._source_key = source_key
        self._title = title
        self._url = url
        self._meta = meta
        self._family_key = family_key or self.key
        self._family_name = family_name or title
        self._short_name = short_name or title
        self._about = about
        self._topics = topics or []
        self._instrument = instrument or self.instrument or short_name or title
        if renderer is not None:
            self.renderer = renderer

    # -- meta -------------------------------------------------------------
    def meta(self) -> SourceMeta:
        if self._meta is not None:
            return self._meta
        return SourceMeta(
            family_key=self._family_key,
            family_name=self._family_name,
            source_key=self._source_key,
            name=self._title,
            kind=self.kind,
            issuer=self.issuer or self.publisher,
            jurisdiction=self.jurisdiction,
            license=self.license,
            canonical_url=self._url,
            adapter=self.key,
            short_name=self._short_name,
            about=self._about,
            topics=list(self._topics),
            version_policy=self.version_policy,
            publisher=self.publisher,
            instrument=self._instrument,
        )

    # -- ref building (subclasses may override) ---------------------------
    def make_ref(self, match: re.Match, context: dict) -> str:
        return " ".join(match.group("ref").split())

    def status_of(self, match: re.Match) -> str:
        try:
            return (match.group("status") or "").strip()
        except IndexError:
            return ""

    def version_of(self, content: bytes) -> tuple[str, date | None]:
        today = date.today()
        return f"{self.version_kind}:{today.isoformat()}", today

    def artifact_name(self) -> str:
        return "page.html"

    def fetch_bytes(self) -> list[tuple[str, bytes]]:
        """(artifact name, bytes) pairs — one per page/chapter fetched."""
        return [(self.artifact_name(), render_html(self._url, renderer=self.renderer, headers=self.headers))]

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        parts = self.fetch_bytes()
        artifacts = [
            Artifact(
                name=name,
                content=content,
                content_type="application/pdf" if content[:5] == b"%PDF-" else "text/html",
            )
            for name, content in parts
        ]
        tree = self.parse_many([c for _, c in parts])
        label, as_of = self.version_of(parts[0][1] if parts else b"")
        return FetchResult(
            version_label=label,
            artifacts=artifacts,
            tree=tree,
            version_kind=self.version_kind,
            as_of_date=as_of,
        )

    def parse_many(self, contents: list[bytes]) -> list[DocNode]:
        root = DocNode(node_type="title", ref=self._source_key, heading=self._title)
        seen: set[str] = {self._source_key}
        for i, content in enumerate(contents, start=1):
            root.children.extend(self._parse_into(content, seen, part=i))
        return [root]

    def parse(self, content: bytes) -> list[DocNode]:
        """Golden-set entry point: parse one artifact into a tree."""
        return self.parse_many([content])

    def parse_pages(self, pages: list[str]) -> list[DocNode]:
        """Golden-set entry point for text pages (no PDF bytes needed)."""
        root = DocNode(node_type="title", ref=self._source_key, heading=self._title)
        root.children.extend(self._parse_pages(pages, {self._source_key}, part=1))
        return [root]

    def _parse_into(self, content: bytes, seen: set[str], *, part: int) -> list[DocNode]:  # pragma: no cover
        raise NotImplementedError

    def _parse_pages(self, pages: list[str], seen: set[str], *, part: int) -> list[DocNode]:
        """Line-based structural parse shared by the PDF adapter and the HTML
        adapter's PDF fallback (publishers that serve a PDF from an HTML url)."""
        nodes: list[DocNode] = []
        section: DocNode | None = None
        provision: DocNode | None = None
        context = {"part": part, "section": "", "seen": seen}
        seq = 0
        for page in pages:
            for raw in page.splitlines():
                line = raw.strip()
                if not line:
                    continue
                seq += 1
                match = self.PROVISION.match(line) if self.PROVISION else None
                heading = self.HEADING.match(line) if (self.HEADING and not match) else None
                if heading:
                    section = DocNode(
                        node_type="group",
                        ref=unique_ref(f"{self._source_key}/p{part}s{seq}", seen),
                        heading=line,
                    )
                    context["section"] = line
                    nodes.append(section)
                    provision = None
                    continue
                if match:
                    ref = unique_ref(self.make_ref(match, context), seen)
                    provision = DocNode(
                        node_type="provision",
                        ref=ref,
                        label=line[: match.end()].strip(),
                        raw_text=line[match.end() :].strip(),
                        status=self.status_of(match),
                    )
                    (section.children if section is not None else nodes).append(provision)
                    continue
                if provision is not None:
                    provision.raw_text = f"{provision.raw_text}\n{line}" if provision.raw_text else line
                    continue
                para = DocNode(node_type="paragraph", raw_text=line)
                (section.children if section is not None else nodes).append(para)
        _promote_groups_without_provisions(nodes)
        return nodes


class NumberedHtmlAdapter(_PublisherBase):
    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        spans: list[str] = []
        for artifact in artifacts:
            if artifact.content[:5] == b"%PDF-":
                for page in extract_pdf_pages(artifact.content):
                    spans.extend(p.strip() for p in page.splitlines() if p.strip())
                continue
            soup = _strip_chrome(BeautifulSoup(artifact.content, "html.parser"))
            spans.extend(_visible_strings(soup))
        return spans

    def _blocks(self, soup: BeautifulSoup) -> list[tuple[str, str, Tag]]:
        """(kind, text, element) in document order; kind ∈ heading|block."""
        body = soup.body or soup
        out: list[tuple[str, str, Tag]] = []
        seen: set[int] = set()
        for el in body.find_all(list(_HEADING | _BLOCK)):
            if id(el) in seen:
                continue
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            ancestor = el.parent
            skip = False
            while ancestor is not None and ancestor is not body:
                if isinstance(ancestor, Tag) and ancestor.name in _BLOCK and ancestor.get_text(" ", strip=True) == text:
                    skip = True
                    break
                ancestor = ancestor.parent
            if skip:
                continue
            seen.add(id(el))
            out.append(("heading" if el.name in _HEADING else "block", text, el))
        return out

    def _parse_into(self, content: bytes, seen: set[str], *, part: int) -> list[DocNode]:
        if content[:5] == b"%PDF-":
            return self._parse_pages(extract_pdf_pages(content), seen, part=part)
        soup = _strip_chrome(BeautifulSoup(content, "html.parser"))
        title_el = soup.find("title")
        page_title = title_el.get_text(" ", strip=True) if title_el else ""
        nodes: list[DocNode] = []
        section: DocNode | None = None
        provision: DocNode | None = None
        context = {"part": part, "section": "", "seen": seen}
        seq = 0
        for kind, text, el in self._blocks(soup):
            seq += 1
            fragment = str(el)[:2000]
            match = self.PROVISION.match(text) if self.PROVISION else None
            if kind == "heading" and not match:
                section = DocNode(
                    node_type="group",
                    ref=unique_ref(f"{self._source_key}/p{part}s{seq}", seen),
                    heading=text,
                    source_fragment=fragment,
                )
                context["section"] = text
                nodes.append(section)
                provision = None
                continue
            if match:
                ref = unique_ref(self.make_ref(match, context), seen)
                label = text[: match.end()].strip()
                body = text[match.end() :].strip()
                provision = DocNode(
                    node_type="provision",
                    ref=ref,
                    label=label,
                    raw_text=body,
                    source_fragment=fragment,
                    status=self.status_of(match),
                )
                (section.children if section is not None else nodes).append(provision)
                continue
            para = DocNode(node_type="paragraph", raw_text=text, source_fragment=fragment)
            target = provision if provision is not None else section
            (target.children if target is not None else nodes).append(para)
        _promote_groups_without_provisions(nodes)
        # Fidelity: anything visible that the walk did not capture lands in a note.
        haystack = " ".join(piece for n in nodes for m in n.walk() for piece in (m.label, m.heading, m.raw_text) if piece)
        haystack = f"{self._title} {page_title} {haystack}"
        leftover = []
        for span in _visible_strings(soup):
            if span not in haystack:
                leftover.append(span)
                haystack += " " + span
        if leftover:
            note = DocNode(
                node_type="note",
                ref=unique_ref(f"{self._source_key}/p{part}/visible", seen),
                heading="Visible text not captured as a heading, provision or paragraph",
            )
            for span in leftover:
                note.children.append(DocNode(node_type="paragraph", raw_text=span))
            nodes.append(note)
        # One chapter per fetched page (FCA chapter, Basel chapter, FINRA rule page).
        chapter = DocNode(
            node_type="chapter",
            ref=unique_ref(f"{self._source_key}/p{part}", seen),
            heading=page_title or f"Part {part}",
            children=nodes,
        )
        return [chapter]


class NumberedPdfAdapter(_PublisherBase):
    version_kind = "edition"
    version_policy = "edition"

    def artifact_name(self) -> str:
        return "document.pdf"

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        spans: list[str] = []
        for artifact in artifacts:
            if artifact.content[:5] == b"%PDF-":
                for page in extract_pdf_pages(artifact.content):
                    spans.extend(p.strip() for p in page.splitlines() if p.strip())
            else:
                soup = _strip_chrome(BeautifulSoup(artifact.content, "html.parser"))
                spans.extend(_visible_strings(soup))
        return spans

    def _parse_into(self, content: bytes, seen: set[str], *, part: int) -> list[DocNode]:
        if content[:5] == b"%PDF-":
            pages = extract_pdf_pages(content)
        else:
            # Landing page or HTML edition: treat visible strings as lines.
            soup = _strip_chrome(BeautifulSoup(content, "html.parser"))
            pages = ["\n".join(_visible_strings(soup))]
        return self._parse_pages(pages, seen, part=part)
