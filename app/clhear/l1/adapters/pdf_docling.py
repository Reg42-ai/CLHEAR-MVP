"""PDF → DocNode helper (Class C).

Prefers Docling when installed; otherwise uses pypdf page text. The oracle is
the same page-level extraction used as a dumb concatenation — the structural
parse only groups pages/paragraphs. Publisher text is never rewritten.
"""
from datetime import date

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta


def extract_pdf_pages(content: bytes) -> list[str]:
    """Return one string per PDF page (Docling if present, else pypdf)."""
    try:
        from docling.document_converter import DocumentConverter

        import tempfile
        import os

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(content)
            path = tmp.name
        try:
            doc = DocumentConverter().convert(path).document
            text = doc.export_to_text() if hasattr(doc, "export_to_text") else str(doc)
            pages = [p.strip() for p in text.split("\f") if p.strip()]
            return pages or ([text] if text.strip() else [])
        finally:
            os.unlink(path)
    except Exception:
        pass
    from pypdf import PdfReader
    import io

    reader = PdfReader(io.BytesIO(content))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return pages


def pages_to_tree(pages: list[str], source_key: str, title: str) -> list[DocNode]:
    root = DocNode(node_type="title", ref=source_key, heading=title)
    for i, page in enumerate(pages, start=1):
        chapter = DocNode(
            node_type="chapter",
            ref=f"{source_key}/p{i}",
            label=f"Page {i}",
            heading=f"Page {i}",
        )
        for para in [p.strip() for p in page.split("\n\n") if p.strip()]:
            chapter.children.append(DocNode(node_type="paragraph", raw_text=para))
        if not chapter.children and page.strip():
            chapter.children.append(DocNode(node_type="paragraph", raw_text=page.strip()))
        root.children.append(chapter)
    return [root]


class PdfOfficialAdapter:
    """Fetch a PDF (or HTML landing page that links one) and emit a DocNode tree."""

    key = "pdf_official"

    def __init__(
        self,
        source_key: str,
        title: str,
        url: str,
        *,
        adapter: str = "pdf_official",
        meta: SourceMeta | None = None,
        jurisdiction: str = "",
        issuer: str = "",
        kind: str = "guidance",
        license: str = "open",
        license_ref: str = "",
        family_key: str = "",
        family_name: str = "",
        short_name: str = "",
        about: str = "",
        topics: list[str] | None = None,
    ):
        self._source_key = source_key
        self._title = title
        self._url = url
        self.key = adapter
        self._meta = meta
        self._jurisdiction = jurisdiction
        self._issuer = issuer
        self._kind = kind
        self._license = license
        self._license_ref = license_ref
        self._family_key = family_key or adapter
        self._family_name = family_name or title
        self._short_name = short_name or title
        self._about = about
        self._topics = topics or []

    def meta(self) -> SourceMeta:
        if self._meta is not None:
            return self._meta
        return SourceMeta(
            family_key=self._family_key,
            family_name=self._family_name,
            source_key=self._source_key,
            name=self._title,
            kind=self._kind,
            issuer=self._issuer,
            jurisdiction=self._jurisdiction,
            license=self._license,
            license_ref=self._license_ref,
            canonical_url=self._url,
            adapter=self.key,
            short_name=self._short_name,
            about=self._about,
            topics=list(self._topics),
            version_policy="edition",
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        content = http.get(self._url)
        ctype = "application/pdf" if content[:5] == b"%PDF-" else "application/octet-stream"
        if content[:5] != b"%PDF-":
            # Landing page: keep the bytes as an HTML artifact and parse as PDF-or-text.
            from app.clhear.l1.adapters.official_html import OfficialHtmlAdapter

            html = OfficialHtmlAdapter(
                source_key=self._source_key,
                title=self._title,
                url=self._url,
                adapter=self.key,
                meta=self.meta(),
            )
            result = html.fetch(since_version)
            if result is None:
                return None
            result.artifacts = [Artifact(name="page.html", content=content, content_type="text/html")]
            return result
        pages = extract_pdf_pages(content)
        tree = pages_to_tree(pages, self._source_key, self._title)
        today = date.today()
        return FetchResult(
            version_label=f"edition:{today.isoformat()}",
            artifacts=[Artifact(name="document.pdf", content=content, content_type=ctype)],
            tree=tree,
            version_kind="edition",
            as_of_date=today,
        )

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        spans: list[str] = []
        for artifact in artifacts:
            if artifact.content[:5] == b"%PDF-":
                for page in extract_pdf_pages(artifact.content):
                    spans.extend(p.strip() for p in page.splitlines() if p.strip())
            else:
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(artifact.content, "html.parser")
                for el in list(soup.find_all(["script", "style", "nav", "header", "footer"])):
                    el.decompose()
                spans.extend(s.strip() for s in soup.stripped_strings if s.strip())
        return spans
