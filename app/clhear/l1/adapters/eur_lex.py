"""EUR-Lex / Cellar adapter (HLD §7.2 adapter 2): GDPR (CELEX 32016R0679).

Fetches the consolidated English text from the Publications Office Cellar
(application/xhtml+xml) and parses the article tree. Family relations:
corrigenda discovered by probing CELEX corrigendum identifiers against Cellar
(the full RDF relations notice is ~60MB — deliberately avoided; # ARCH: switch
to the Cellar work-tree notice when EUR-Lex relations are needed at scale).

License: EUR-Lex legal notice permits reuse of legal texts (Commission
Decision 2011/833/EU) — verbatim reproduction with source acknowledgement.
"""
import re

from bs4 import BeautifulSoup

from app.clhear.l1 import http
from app.clhear.l1.adapters.base import Artifact, ClauseNode, EffectRecord, FetchResult, SourceMeta

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
            canonical_url=f"https://eur-lex.europa.eu/eli/reg/2016/679/oj",
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

        tree: list[ClauseNode] = []
        ordering = 0
        chapter_label = ""
        # CONVEX output: chapter divs (id=cpt_*) contain article divs (id=art_N).
        # Article number: p.title-article-norm; subtitle: p.stitle-article-norm;
        # paragraph blocks: DIRECT-child div.norm (nested .norm.inline-element
        # duplicates the text, hence recursive=False).
        for div in soup.find_all("div", id=True):
            div_id = div["id"]
            if re.fullmatch(r"cpt_[IVXLC]+", div_id):
                num = div.find("p", class_="title-division-1")
                heading = div.find("p", class_="title-division-2")
                parts = [t.get_text(" ", strip=True) for t in (num, heading) if t is not None]
                chapter_label = _norm(" — ".join(parts)) or div_id
                continue
            if not re.fullmatch(r"art_\d+", div_id):
                continue
            number_el = div.find("p", class_="title-article-norm")
            subtitle_el = div.find("p", class_="stitle-article-norm")
            number = _norm(number_el.get_text(" ", strip=True)) if number_el else f"Article {div_id.split('_')[1]}"
            lines = [number]
            if subtitle_el is not None:
                lines.append(_norm(subtitle_el.get_text(" ", strip=True)))
            for block in div.find_all("div", class_="norm", recursive=False):
                text = _norm(block.get_text(" ", strip=True))
                if text:
                    lines.append(text)
            ordering += 1
            tree.append(
                ClauseNode(
                    ref=div_id,
                    path=_join(chapter_label, number),
                    ordering=ordering,
                    text="\n".join(lines),
                )
            )

        return FetchResult(
            version_label=version_label,
            artifacts=[Artifact(name="consolidated.xhtml", content=content, content_type="application/xhtml+xml")],
            clause_tree=tree,
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


def _norm(text: str) -> str:
    return " ".join(text.split())


def _join(*parts: str) -> str:
    return " > ".join(p for p in parts if p)
