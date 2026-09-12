"""Legal protections: per-source attributions, accuracy tiers, disclaimers.

The risk lives per verbatim source (different publishers, different reuse
terms), so attribution is computed per source and travels with every document
view and API payload. Global texts (/terms, /disclaimer) are DRAFTS for
counsel review before any marketing push — the HLD requires IP review before
public promotion.
"""
from __future__ import annotations

DISCLAIMER_SHORT = (
    "CLHEAR republishes regulatory texts from their official sources and derives "
    "structured compliance data from them. Nothing here is legal advice; verify "
    "against the official source (linked with every clause) and consult counsel "
    "before acting. Accuracy tiers: LIVE = fetched verbatim and hash-verified; "
    "DERIVED = machine-extracted, may contain errors, not yet human-validated; "
    "CURATED = human-authored mappings; COMPUTED = deterministic engine output "
    "over DERIVED + CURATED inputs; LOCKED = not published."
)

CONTRIBUTION_LICENSE = (
    "By submitting a case, vote, or suggestion you license your contribution to "
    "Reg42 under CC BY 4.0 and attest (DCO-style) that you have the right to "
    "submit it and that it contains no confidential or licensed text — in "
    "particular no verbatim text from restricted standards (ISO, AICPA TSC, "
    "PCI DSS, IFRS)."
)

# Ordered: first key-prefix match wins; adapter fallbacks after.
_PREFIX_LICENSES: tuple[tuple[str, dict], ...] = (
    ("uksi/", {"name": "Open Government Licence v3.0", "holder": "Crown copyright, via legislation.gov.uk",
               "url": "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"}),
    ("ukpga/", {"name": "Open Government Licence v3.0", "holder": "Crown copyright, via legislation.gov.uk",
                "url": "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"}),
    ("eur/", {"name": "Open Government Licence v3.0 (retained EU law)", "holder": "Crown copyright, via legislation.gov.uk",
              "url": "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"}),
    ("celex/", {"name": "EUR-Lex reuse policy (Decision 2011/833/EU)", "holder": "© European Union, via EUR-Lex",
                "url": "https://eur-lex.europa.eu/content/legal-notice/legal-notice.html",
                "note": "Only EU legislation published in the Official Journal is authentic."}),
    ("nist/", {"name": "US public domain (17 U.S.C. §105)", "holder": "NIST",
               "url": "https://www.nist.gov/open/copyright-fair-use-and-licensing-statements-srd-data-software-and-technical-series-publications"}),
    ("usc/", {"name": "US public domain", "holder": "US Government Publishing Office (govinfo)",
              "url": "https://www.govinfo.gov/about/policies"}),
    ("cfr/", {"name": "US public domain", "holder": "US Government Publishing Office (eCFR/govinfo)",
              "url": "https://www.govinfo.gov/about/policies"}),
    ("irs/", {"name": "US public domain", "holder": "Internal Revenue Service", "url": "https://www.irs.gov"}),
    ("fca/", {"name": "FCA Handbook terms", "holder": "© Financial Conduct Authority",
              "url": "https://www.handbook.fca.org.uk/legal", "note": "Redistributed excerpts link to the authoritative Handbook."}),
    ("finra/", {"name": "Publisher terms", "holder": "© FINRA", "url": "https://www.finra.org/rules-guidance"}),
    ("nasdaq/", {"name": "Publisher terms", "holder": "© Nasdaq", "url": "https://listingcenter.nasdaq.com"}),
    ("nydfs/", {"name": "NY State public records", "holder": "New York DFS", "url": "https://www.dfs.ny.gov"}),
    ("fatf/", {"name": "Publisher terms", "holder": "© FATF/OECD", "url": "https://www.fatf-gafi.org/en/pages/terms-conditions.html"}),
    ("wolfsberg/", {"name": "Publisher terms", "holder": "© The Wolfsberg Group", "url": "https://db.wolfsberg-group.org"}),
    ("lists/", {"name": "Official sanctions list terms", "holder": "issuing authority (UN/EU/OFAC/OFSI)",
                "url": "https://www.un.org/securitycouncil/sanctions/information"}),
    ("iso/", {"name": "Licensed — verbatim text withheld", "holder": "© ISO/IEC", "restricted": True,
              "url": "https://www.iso.org/terms-conditions-licence-agreement.html",
              "note": "CLHEAR stores refs + hashes only. BYOL: licensed users may unlock their own copy."}),
    ("aicpa/", {"name": "Licensed — verbatim text withheld", "holder": "© AICPA & CIMA", "restricted": True,
                "url": "https://www.aicpa-cima.com"}),
    ("pci/", {"name": "Licensed — verbatim text withheld", "holder": "© PCI Security Standards Council", "restricted": True,
              "url": "https://www.pcisecuritystandards.org"}),
    ("ifrs/", {"name": "Licensed — verbatim text withheld", "holder": "© IFRS Foundation", "restricted": True,
               "url": "https://www.ifrs.org"}),
)

_DEFAULT = {"name": "Official publisher terms", "holder": "the issuing authority",
            "url": "", "note": "Verbatim text republished for reference; verify against the official source."}


def attribution_for(source_key: str, license: str = "open") -> dict:
    for prefix, meta in _PREFIX_LICENSES:
        if source_key.startswith(prefix):
            out = dict(meta)
            break
    else:
        out = dict(_DEFAULT)
    out["source_key"] = source_key
    out["restricted"] = bool(out.get("restricted")) or license == "restricted"
    return out


def api_legal_block(source_keys: list[str] | None = None) -> dict:
    block = {
        "disclaimer": DISCLAIMER_SHORT,
        "terms_url": "/terms",
        "not_legal_advice": True,
    }
    if source_keys:
        block["attributions"] = [attribution_for(k) for k in sorted(set(source_keys))]
    return block
