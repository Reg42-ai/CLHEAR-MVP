"""FINRA publications and attachments; catalog pages never stand in for text."""
import hashlib
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, FetchResult, SourceMeta

HOSTS = {"www.finra.org", "finra.org", "files.finra.org"}
BODY_SELECTORS = ("#block-body .field--name-body", "article .field--name-body", ".node--type-regulatory-notice .field--name-body", ".node--type-notice .field--name-body")


class NavigationPage(ValueError):
    """A rulebook container (e.g. By-Laws "ARTICLE IV") whose only content is
    the list of its child sections. Its children are the documents; the page
    itself is catalog structure and must not be recorded as an import failure."""


def document_markup(content):
    soup = BeautifulSoup(content, "html.parser")
    title = soup.find("h1")
    body = next((soup.select_one(selector) for selector in BODY_SELECTORS if soup.select_one(selector) is not None), None)
    if title is None or body is None or not title.get_text(strip=True):
        navigation = soup.select_one(".node__content .book-navigation, main .book-navigation")
        if title is not None and body is None and navigation is not None and navigation.find_all("a", href=True):
            raise NavigationPage("FINRA rulebook page lists child sections only; constituent pages carry the text")
        raise ValueError("FINRA publication lacks an identified title and official article body")
    text = body.get_text(" ", strip=True)
    link_text = " ".join(a.get_text(" ", strip=True) for a in body.find_all("a"))
    if len(text) < 40 or len(link_text) >= len(text) * 0.9:
        raise ValueError("FINRA publication body is empty or a document catalog")
    return ("<main>" + str(title) + str(body) + "</main>").encode()


class FinraDocumentAdapter:
    key = "finra"

    def __init__(self, source_key, title, url, meta=None, document_type="publication"):
        if document_type not in {"publication", "attachment"}:
            raise ValueError("FINRA document type must be publication or attachment")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in HOSTS:
            raise ValueError("FINRA publication must use an official HTTPS publisher host")
        if parsed.path.rstrip("/") in {"", "/rules-guidance", "/rules-guidance/notices", "/rules-guidance/rulebooks", "/rules-guidance/rule-filings"}:
            raise ValueError("Catalog URLs are discovery inputs, not publication documents")
        self._source_key, self._title, self._url, self._meta = source_key, title, url, meta
        self.document_type = document_type

    def meta(self):
        return self._meta or SourceMeta(family_key="us-broker-dealer", family_name="US broker-dealer regulation",
            source_key=self._source_key, name=self._title, kind="guidance", issuer="FINRA", jurisdiction="US",
            license="restricted", canonical_url=self._url, adapter="finra", publisher="FINRA",
            rights_basis="derived_only", version_policy="as_published")

    def fetch(self, since_version=None):
        content = http.get(self._url)
        if content.startswith(b"%PDF-"):
            from app.clhear.l1.adapters.pdf_docling import extract_pdf_pages, pages_to_tree
            tree = pages_to_tree(extract_pdf_pages(content), self._source_key, self._title)
            artifact = Artifact("publication.pdf", content, "application/pdf")
        else:
            if self.document_type == "attachment" or not re.search(br"<(?:!doctype\s+html|html|main|article)\b", content[:4096], re.I):
                raise ValueError("FINRA attachment is not an actual PDF or publication HTML")
            from app.clhear.l1.adapters.html_document import parse
            tree = parse(document_markup(content), self._source_key)
            artifact = Artifact("publication.html", content, "text/html")
        return FetchResult(version_label=f"as-published:acquired-sha256-{hashlib.sha256(content).hexdigest()}",
                           artifacts=[artifact], tree=tree, version_kind="as_published", as_of_date=None)

    def expected_text(self, artifacts):
        from app.clhear.l1.originals import html_text, pdf_original
        return [pdf_original(a.content)[0] if a.content.startswith(b"%PDF-") else html_text(document_markup(a.content)) for a in artifacts]
