"""EUR-Lex / Cellar adapter (HLD §7.2 adapter 2): GDPR (CELEX 32016R0679).

Emits a typed DocNode tree (chapter / article / paragraph / preamble) with
raw text and the exact XHTML fragment per node. Family relations: corrigenda
probed as CELEX identifiers (the full RDF relations notice is ~60MB —
# ARCH: switch to the Cellar work-tree notice when relations are needed at scale).

License: EUR-Lex legal notice permits reuse of legal texts (Commission
Decision 2011/833/EU) — verbatim reproduction with source acknowledgement.
"""
import re

from bs4 import BeautifulSoup, Tag

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, DocNode, EffectRecord, FetchResult, SourceMeta

CELLAR = "http://publications.europa.eu/resource/celex"
GDPR_CELEX = "32016R0679"
# GDPR's only consolidation; new consolidations get new date suffixes.
# ARCH: consolidation discovery via the Cellar work tree lands with the P2
# relations work; until then the adapter pins the current consolidated version.
GDPR_CONSOLIDATED = "02016R0679-20160504"

_HEADERS = {"Accept": "application/xhtml+xml", "Accept-Language": "eng"}


class EurLexAdapter:
    key = "eur_lex"

    def __init__(self, celex: str = GDPR_CELEX, consolidated: str = GDPR_CONSOLIDATED):
        self.celex = celex
        self.consolidated = consolidated

    def meta(self) -> SourceMeta:
        return SourceMeta(
            family_key="eu-gdpr",
            family_name="EU General Data Protection Regulation",
            source_key=f"celex/{self.celex}",
            name="Regulation (EU) 2016/679 (General Data Protection Regulation)",
            kind="regulation",
            issuer="European Parliament and Council (Publications Office)",
            jurisdiction="EU",
            license="open",
            license_ref="Commission Decision 2011/833/EU",
            canonical_url="https://eur-lex.europa.eu/eli/reg/2016/679/oj",
            adapter=self.key,
            scope_charter={
                "binding": "regulation + corrigenda (CELEX relations)",
                "guidance": "EDPB guidelines (watcher, later)",
                "out": ["national DPA guidance"],
            },
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        version_label = f"consolidated-{self.consolidated}"
        if since_version == version_label:
            return None
        content = http.get(f"{CELLAR}/{self.consolidated}", headers=_HEADERS)
        soup = BeautifulSoup(content, "html.parser")

        tree: list[DocNode] = []
        current_chapter: DocNode | None = None

        # CONVEX output: chapter divs (id=cpt_*) contain article divs (id=art_N)
        # as descendants. Articles also appear in document-order find_all.
        for div in soup.find_all("div", id=True):
            if not isinstance(div, Tag):
                continue
            div_id = str(div.get("id", ""))
            if re.fullmatch(r"cpt_[IVXLC]+", div_id):
                num = div.find("p", class_="title-division-1")
                heading = div.find("p", class_="title-division-2")
                current_chapter = DocNode(
                    node_type="chapter",
                    ref=div_id,
                    label=_raw(num) if num else "",
                    heading=_raw(heading) if heading else "",
                    source_fragment=str(div),
                )
                tree.append(current_chapter)
                continue
            if not re.fullmatch(r"art_\d+", div_id):
                continue
            number_el = div.find("p", class_="title-article-norm")
            subtitle_el = div.find("p", class_="stitle-article-norm")
            paragraphs = []
            for block in div.find_all("div", class_="norm", recursive=False):
                if not isinstance(block, Tag):
                    continue
                text = _raw(block)
                if not text:
                    continue
                match = re.match(r"^(\d+)\.\s*", text)
                paragraphs.append(
                    DocNode(
                        node_type="paragraph",
                        ref=f"{div_id}.{match.group(1)}" if match else "",
                        label=f"{match.group(1)}." if match else "",
                        raw_text=text,
                        source_fragment=str(block),
                    )
                )
            article = DocNode(
                node_type="article",
                ref=div_id,
                label=_raw(number_el) if number_el else f"Article {div_id.split('_')[1]}",
                heading=_raw(subtitle_el) if subtitle_el else "",
                source_fragment=str(div),
                children=paragraphs,
            )
            if current_chapter is not None:
                current_chapter.children.append(article)
            else:
                tree.append(article)

        return FetchResult(
            version_label=version_label,
            artifacts=[Artifact(name="consolidated.xhtml", content=content, content_type="application/xhtml+xml")],
            tree=tree,
        )

    def family_effects(self) -> list[EffectRecord]:
        """Corrigenda: probe CELEX 3…R(01)… identifiers against Cellar."""
        from urllib.parse import quote

        records: list[EffectRecord] = []
        for i in range(1, 10):
            corrigendum = f"{self.celex}R({i:02d})"
            try:
                http.get(
                    f"{CELLAR}/{quote(corrigendum)}",  # Cellar requires %28/%29 parens
                    headers={"Accept": "application/xml;notice=identifiers"},
                )
            except Exception:
                break
            records.append(
                EffectRecord(
                    affecting_key=f"celex/{corrigendum}",
                    affecting_name=f"Corrigendum {corrigendum} to Regulation (EU) 2016/679",
                    affecting_url=f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{corrigendum}",
                    relation="corrects",
                    kind="regulation",
                )
            )
        return records


def _raw(el) -> str:
    """Element text with source whitespace preserved (only ends stripped)."""
    return "".join(el.strings).strip() if el is not None else ""
