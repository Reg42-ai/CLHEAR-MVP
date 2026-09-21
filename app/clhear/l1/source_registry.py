"""Company-independent regulatory source declarations and worker metadata seed.

These declarations are a starting set, not proof of publisher completeness.
Publisher profiles and worker discovery define the complete accepted inventory.
Existing keys and historical versions remain stable across metadata corrections.
"""
import json

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.l1.adapters.base import SourceMeta
from app.clhear.l1.models import family_members, source_families, sources

# (family_key, family_name, charter_note)
FAMILIES = [
    ("eu-mifid", "EU MiFID II framework", "MiFID II + MiFIR + delegated regs + RTS series (registry F1: EU-001..011)"),
    ("eu-markets", "EU market integrity & post-trade", "MAR, SSR, EMIR/EMIR3, SFTR, CSDR, BMR, PRIIPs (F2: EU-012..019)"),
    ("eu-mica", "EU crypto (MiCA stack)", "MiCA + TFR travel rule + L2/L3 (F3: EU-030..034)"),
    ("eu-aml", "EU financial crime", "AMLD + AML package 2024 + whistleblowing (F4: EU-040..042)"),
    ("eu-prudential", "EU prudential & resilience", "IFR/IFD + DORA (F5: EU-050..054)"),
    ("eu-consumer", "EU consumer, marketing & platform", "DMFSD recast, UCPD, DSA, SFDR, CSRD, ePrivacy (F6: EU-060..065, GRP-038)"),
    ("eu-gdpr", "EU General Data Protection Regulation", "GDPR OJ + corrigenda (F7: GRP-030)"),
    ("eu-data", "EU data protection & AI", "AI Act, Accessibility Act, EU sanctions regs (F7: GRP-036/037/022; GDPR lives in eu-gdpr)"),
    ("uk-fca", "UK conduct & prudential (FCA)", "FSMA, FCA Handbook, UK MiFIR/EMIR/MAR/SSR, promotions (F8: UK-001..024)"),
    ("uk-fincrime", "UK financial crime", "POCA, TACT, CFA 2017, ECCTA, Bribery (F9: UK-030..034; MLRs live in uk-mlr)"),
    ("uk-data-products", "UK data, e-money & wrappers", "UK GDPR/DPA/PECR, ISA regs, EMRs/PSRs, safeguarding (F10: GRP-031, UK-040..052)"),
    ("us-broker-dealer", "US broker-dealer & listed company", "Exchange Act BD rules, Reg BI/S-P/S-ID, FINRA, CAT, Securities Act/SOX/Nasdaq (F11: US-001..010, GRP-001..008)"),
    ("us-marketing", "US advertising & adviser marketing", "FTC Act §5, 16 CFR Part 255 Endorsement Guides, Advisers Act marketing rule"),
    ("us-crypto-msb", "US crypto MSB & state", "BSA/31 CFR X, MTLs, NYDFS 200/500, GENIUS radar (F12: US-020..025)"),
    ("au-afsl", "Australia (ASIC/AUSTRAC)", "Corporations Act Ch 7, DDO, CFD PIO, DTR 2024, AML/CTF reform, RE stack (F13: AU-001..010, GRP-034)"),
    ("me-adgm", "ADGM / UAE (FSRA)", "FSMR + rulebooks, VA framework, UAE AML, ADGM DPR (F14: ME-001..005)"),
    ("sg-mas", "Singapore (MAS)", "SFA + LCB regs, MAS notices, PDPA, DPT boundary (F15: SG-001..007)"),
    ("small-entities", "Seychelles / Malta / Gibraltar / BVI", "F16: SC-*, MT-*, GI-*, GRP-010 (Israel lives in il-isa)"),
    ("il-isa", "Israel (ISA)", "Securities Law 5728-1968 (ISA English translation) + ISA rules (IL-*)"),
    ("sanctions-lists", "Global sanctions & screening lists", "F17: UN, OFAC, EU, OFSI, NBCTF, DFAT, EOCN — structured LIST feeds (separate lists pipeline, class E)"),
    ("intl-tax-aeoi", "International tax reporting & transaction taxes", "F18: FATCA IGAs, CRS/DAC8/CARF, QI/871(m), FTT & stamp layer (TAX-001..031; FATCA statute/regs live in us-fatca)"),
    ("host-state-overlays", "EU/EEA host-state overlays", "F20: BE/FR/ES/DE/IT/NL/PL… product-intervention & marketing overlays (OVL-*)"),
    ("standards", "Standards & frameworks (SC4)", "F19: FATF, Wolfsberg, ISO, SOC 2, PCI, IFRS — NIST lives in nist-spine (STD-*, GRP-009/026/027)"),
]

FAMILY_NAMES = {key: name for key, name, _ in FAMILIES}

S: list[dict] = []


def _src(family, key, short_name, name, kind, jurisdiction, issuer, url, adapter,
         relation, tier, topics, ids, wave, license="open", fetch=None,
         rights_basis="", publisher="", instrument=""):
    S.append(dict(
        family=family, key=key, short_name=short_name, name=name, kind=kind,
        license=license, jurisdiction=jurisdiction, issuer=issuer, canonical_url=url,
        adapter=adapter, relation=relation, tier=tier, topics=topics,
        registry_ids=ids, wave=wave, fetch=fetch,
        rights_basis=rights_basis, publisher=publisher or issuer, instrument=instrument or short_name,
    ))


EURLEX = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:"
EURLEX_HTML = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:"
UKLEG = "https://www.legislation.gov.uk/"
EU_ISSUER = "European Parliament and Council (Publications Office)"
UK_ISSUER = "UK Parliament / HM Government (The National Archives)"


def _eu(family, celex, short_name, name, kind, relation, topics, ids):
    """Wave-1 EU source: eur_lex adapter, as-published OJ text."""
    _src(family, f"celex/{celex}", short_name, name, kind, "EU", EU_ISSUER,
         EURLEX + celex, "eur_lex", relation, "binding", topics, ids, 1,
         fetch={"celex": celex})


def _uk(family, doc, short_name, name, kind, relation, topics, ids, key=None):
    """Wave-1 UK source: uk_legislation adapter, current consolidated text."""
    _src(family, key or doc, short_name, name, kind, "UK", UK_ISSUER,
         UKLEG + doc, "uk_legislation", relation, "binding", topics, ids, 1,
         fetch={"doc": doc})


# ---- F1 eu-mifid ----
_eu("eu-mifid", "32014L0065", "MiFID II", "Directive 2014/65/EU on markets in financial instruments", "law", "root", ["markets", "conduct", "eu"], ["EU-001"])
_eu("eu-mifid", "32014R0600", "MiFIR", "Regulation (EU) 600/2014 on markets in financial instruments", "regulation", "supplements", ["markets", "reporting", "eu"], ["EU-002"])
_eu("eu-mifid", "32017R0565", "MiFID II Org Reg", "Commission Delegated Regulation (EU) 2017/565", "regulation", "implements", ["conduct", "records", "eu"], ["EU-004"])
_eu("eu-mifid", "32017R0587", "RTS 1", "Delegated Regulation (EU) 2017/587 — equity transparency / SI quoting", "regulation", "implements", ["transparency", "eu"], ["EU-005"])
_eu("eu-mifid", "32017R0583", "RTS 2", "Delegated Regulation (EU) 2017/583 — non-equity transparency", "regulation", "implements", ["transparency", "eu"], ["EU-006"])
_eu("eu-mifid", "32017R0590", "RTS 22", "Delegated Regulation (EU) 2017/590 — transaction reporting", "regulation", "implements", ["reporting", "eu"], ["EU-007"])
_eu("eu-mifid", "32017R0585", "RTS 23", "Delegated Regulation (EU) 2017/585 — instrument reference data", "regulation", "implements", ["reporting", "eu"], ["EU-008"])
_eu("eu-mifid", "32017R0574", "RTS 25", "Delegated Regulation (EU) 2017/574 — clock synchronisation", "regulation", "implements", ["markets", "eu"], ["EU-009"])
_eu("eu-mifid", "32017R0589", "RTS 6", "Delegated Regulation (EU) 2017/589 — algo trading controls", "regulation", "implements", ["markets", "eu"], ["EU-010"])
_eu("eu-mifid", "32017L0593", "MiFID PG Dir", "Commission Delegated Directive (EU) 2017/593 — product governance & safeguarding", "law", "implements", ["products", "client-assets", "eu"], ["EU-011", "EU-054"])
_src("eu-mifid", "cy/l87i-2017", "Cyprus ISL", "CySEC investment-services legislation collection", "law", "CY", "Republic of Cyprus / CySEC", "https://www.cysec.gov.cy/en-GB/legislation/investment-services/", "cysec", "implements", "binding", ["conduct", "cyprus"], ["EU-003"], 3, fetch={"url": "https://www.cysec.gov.cy/en-GB/legislation/investment-services/", "kind": "pdf"})

# ---- F2 eu-markets ----
_eu("eu-markets", "32014R0596", "MAR", "Market Abuse Regulation (EU) 596/2014", "regulation", "root", ["market-abuse", "surveillance", "eu"], ["EU-014"])
_eu("eu-markets", "32012R0236", "EU SSR", "Short Selling Regulation (EU) 236/2012", "regulation", "supplements", ["markets", "eu"], ["EU-015"])
_eu("eu-markets", "32012R0648", "EMIR", "Regulation (EU) 648/2012 (EMIR) incl. Refit and EMIR 3", "regulation", "supplements", ["derivatives", "reporting", "eu"], ["EU-016"])
_eu("eu-markets", "32015R2365", "SFTR", "Securities Financing Transactions Regulation (EU) 2015/2365", "regulation", "supplements", ["reporting", "eu"], ["EU-017"])
_eu("eu-markets", "32014R0909", "CSDR", "Central Securities Depositories Regulation (EU) 909/2014", "regulation", "supplements", ["settlement", "eu"], ["EU-018"])
_eu("eu-markets", "32016R1011", "BMR", "Benchmarks Regulation (EU) 2016/1011", "regulation", "supplements", ["markets", "eu"], ["EU-019"])
_eu("eu-markets", "32014R1286", "PRIIPs", "PRIIPs Regulation (EU) 1286/2014", "regulation", "supplements", ["products", "disclosure", "eu"], ["EU-012"])

# ---- F3 eu-mica ----
_eu("eu-mica", "32023R1114", "MiCA", "Markets in Crypto-Assets Regulation (EU) 2023/1114", "regulation", "root", ["crypto", "conduct", "eu"], ["EU-030"])
_eu("eu-mica", "32023R1113", "TFR (travel rule)", "Transfer of Funds Regulation (recast) (EU) 2023/1113", "regulation", "supplements", ["crypto", "aml", "eu"], ["EU-032"])

# ---- F4 eu-aml ----
_eu("eu-aml", "32015L0849", "AMLD", "Directive (EU) 2015/849 (AMLD4/5 consolidated)", "law", "root", ["aml", "eu"], ["EU-040"])
_eu("eu-aml", "32024R1624", "AMLR 2024", "Regulation (EU) 2024/1624 — AML single rulebook (applies Jul 2027)", "regulation", "supplements", ["aml", "eu"], ["EU-041"])
_eu("eu-aml", "32024L1640", "AMLD6", "Directive (EU) 2024/1640", "law", "supplements", ["aml", "eu"], ["EU-041"])
_eu("eu-aml", "32019L1937", "EU Whistleblower Dir", "Directive (EU) 2019/1937 — whistleblower protection", "law", "supplements", ["governance", "eu"], ["EU-042"])

# ---- F5 eu-prudential ----
_eu("eu-prudential", "32019R2033", "IFR", "Investment Firms Regulation (EU) 2019/2033", "regulation", "root", ["prudential", "eu"], ["EU-050"])
_eu("eu-prudential", "32019L2034", "IFD", "Investment Firms Directive (EU) 2019/2034", "law", "supplements", ["prudential", "eu"], ["EU-050"])
_eu("eu-prudential", "32022R2554", "DORA", "Digital Operational Resilience Act (EU) 2022/2554", "regulation", "supplements", ["resilience", "ict", "eu"], ["EU-051"])

# ---- F6 eu-consumer ----
_eu("eu-consumer", "32023L2673", "DMFSD recast", "Directive (EU) 2023/2673 — distance marketing of financial services (applies Jun 2026)", "law", "root", ["marketing", "consumer", "eu"], ["EU-060"])
_eu("eu-consumer", "32005L0029", "UCPD", "Unfair Commercial Practices Directive 2005/29/EC", "law", "supplements", ["consumer", "eu"], ["EU-061"])
_eu("eu-consumer", "32022R2065", "DSA", "Digital Services Act (EU) 2022/2065", "regulation", "supplements", ["platform", "eu"], ["EU-062"])
_eu("eu-consumer", "32019R2088", "SFDR", "Sustainable Finance Disclosure Regulation (EU) 2019/2088", "regulation", "supplements", ["esg", "disclosure", "eu"], ["EU-063"])
# Pre-2004 acts have no as-published XHTML in Cellar; ingest the final
# consolidation instead (CONVEX format, same adapter).
_src("eu-consumer", "celex/32002L0058", "ePrivacy", "Directive 2002/58/EC (ePrivacy)", "law", "EU", EU_ISSUER,
     EURLEX + "32002L0058", "eur_lex", "supplements", "binding", ["privacy", "marketing", "eu"], ["GRP-038"], 1,
     fetch={"celex": "32002L0058", "celex_version": "02002L0058-20091219"})

# ---- F7 eu-gdpr (corrigenda are ingested instruments, not citator stubs) ----
_eu("eu-gdpr", "32016R0679", "GDPR", "Regulation (EU) 2016/679 (General Data Protection Regulation)", "regulation", "root", ["data-protection", "privacy", "eu"], ["GRP-030"])
_eu("eu-gdpr", "32016R0679R(01)", "GDPR corr. 1", "Corrigendum to Regulation (EU) 2016/679 (first)", "regulation", "corrects", ["data-protection", "eu"], ["GRP-030"])
_eu("eu-gdpr", "32016R0679R(02)", "GDPR corr. 2", "Corrigendum to Regulation (EU) 2016/679 (second)", "regulation", "corrects", ["data-protection", "eu"], ["GRP-030"])
_eu("eu-gdpr", "32016R0679R(03)", "GDPR corr. 3", "Corrigendum to Regulation (EU) 2016/679 (third)", "regulation", "corrects", ["data-protection", "eu"], ["GRP-030"])

# ---- F7 eu-data ----
_eu("eu-data", "32024R1689", "EU AI Act", "Artificial Intelligence Act (EU) 2024/1689 (tranche 2 live Aug 2026)", "regulation", "root", ["ai", "eu"], ["GRP-036"])
_eu("eu-data", "32019L0882", "EU Accessibility Act", "Directive (EU) 2019/882 — accessibility requirements", "law", "supplements", ["accessibility", "consumer", "eu"], ["GRP-037"])
_eu("eu-data", "32014R0269", "EU sanctions 269/2014", "Regulation (EU) 269/2014 — Ukraine territorial integrity measures", "regulation", "supplements", ["sanctions", "eu"], ["GRP-022"])

# ---- F8 uk-fca ----
_uk("uk-fca", "ukpga/2000/8", "FSMA 2000", "Financial Services and Markets Act 2000", "law", "root", ["conduct", "uk"], ["UK-001"])
_uk("uk-fca", "ukpga/2023/29", "FSMA 2023", "Financial Services and Markets Act 2023", "law", "amends", ["conduct", "uk"], ["UK-001"])
_uk("uk-fca", "uksi/2001/544", "RAO 2001", "FSMA (Regulated Activities) Order 2001", "regulation", "implements", ["perimeter", "uk"], ["UK-001", "UK-041"])
_uk("uk-fca", "uksi/2005/1529", "FPO 2005", "FSMA (Financial Promotion) Order 2005", "regulation", "implements", ["marketing", "uk"], ["UK-020", "UK-021"])
_src("uk-fca", "fca/handbook", "FCA PRIN", "FCA Handbook — PRIN (Principles for Businesses incl. Consumer Duty)", "regulation", "UK", "Financial Conduct Authority", "https://www.handbook.fca.org.uk/handbook/PRIN/", "fca_handbook", "implements", "binding", ["conduct", "consumer-duty", "uk"], ["UK-002"], 2, fetch={"sourcebook": "PRIN", "chapters": ["1", "2", "2A", "3", "4"]})
# HLD v2 starter corpus: FCA Handbook sourcebooks as first-class sources (one
# artifact per chapter; rule status R/G/E kept for the normative flag).
for _sb, _chapters, _topics, _ids in [
    ("SYSC", ["1", "3", "4", "6", "7", "10", "18", "19F"], ["governance", "systems-controls", "uk"], ["UK-008"]),
    ("COBS", ["2", "4", "6", "9A", "10A", "11", "16A", "22"], ["conduct", "uk"], ["UK-009"]),
    ("CASS", ["1", "6", "7", "7A", "10"], ["client-assets", "uk"], ["UK-010"]),
    ("PROD", ["1", "3", "4"], ["products", "uk"], ["UK-011"]),
    ("SUP", ["10A", "10C", "15", "16", "17A"], ["supervision", "reporting", "uk"], ["UK-011"]),
    ("DISP", ["1", "2"], ["complaints", "uk"], ["UK-011"]),
    ("MIFIDPRU", ["1", "4", "7"], ["prudential", "uk"], ["UK-011"]),
]:
    _src("uk-fca", f"fca/handbook/{_sb}", f"FCA {_sb}", f"FCA Handbook — {_sb}", "regulation", "UK", "Financial Conduct Authority",
         f"https://www.handbook.fca.org.uk/handbook/{_sb}/", "fca_handbook", "implements", "binding", _topics, _ids, 2,
         fetch={"sourcebook": _sb, "chapters": _chapters})
_uk("uk-fca", "eur/2014/600", "UK MiFIR", "UK MiFIR — onshored Regulation 600/2014", "regulation", "supplements", ["reporting", "uk"], ["UK-003"], key="eur/2014/600/uk")
_uk("uk-fca", "eur/2012/648", "UK EMIR", "UK EMIR — onshored Regulation 648/2012", "regulation", "supplements", ["derivatives", "reporting", "uk"], ["UK-004"], key="eur/2012/648/uk")
_uk("uk-fca", "eur/2014/596", "UK MAR", "UK MAR — onshored Regulation 596/2014", "regulation", "supplements", ["market-abuse", "uk"], ["UK-005"], key="eur/2014/596/uk")

# ---- F9 uk-fincrime ----
_uk("uk-fincrime", "uksi/2017/692", "UK MLRs 2017", "The Money Laundering, Terrorist Financing and Transfer of Funds (Information on the Payer) Regulations 2017", "regulation", "implements", ["aml", "kyc", "uk"], ["UK-030"])
_uk("uk-fincrime", "ukpga/2002/29", "POCA 2002", "Proceeds of Crime Act 2002", "law", "root", ["aml", "uk"], ["UK-031"])
_uk("uk-fincrime", "ukpga/2000/11", "Terrorism Act 2000", "Terrorism Act 2000", "law", "supplements", ["cft", "uk"], ["UK-031"])
_uk("uk-fincrime", "ukpga/2017/22", "Criminal Finances Act", "Criminal Finances Act 2017", "law", "supplements", ["tax-evasion", "uk"], ["UK-032"])
_uk("uk-fincrime", "ukpga/2023/56", "ECCTA 2023", "Economic Crime and Corporate Transparency Act 2023 (FTP fraud in force Sep 2025)", "law", "supplements", ["fraud", "uk"], ["UK-033"])
_uk("uk-fincrime", "ukpga/2010/23", "Bribery Act 2010", "Bribery Act 2010", "law", "supplements", ["anti-corruption", "uk"], ["UK-034", "GRP-008"])
_uk("uk-fincrime", "ukpga/2018/13", "SAMLA 2018", "Sanctions and Anti-Money Laundering Act 2018", "law", "supplements", ["sanctions", "uk"], ["GRP-023"])

# ---- F10 uk-data-products ----
_uk("uk-data-products", "ukpga/2018/12", "DPA 2018 (UK GDPR)", "Data Protection Act 2018", "law", "root", ["privacy", "uk"], ["GRP-031"])
_uk("uk-data-products", "uksi/1998/1870", "ISA Regulations", "Individual Savings Account Regulations 1998", "regulation", "supplements", ["tax-wrappers", "uk"], ["UK-040"])
_uk("uk-data-products", "uksi/2011/99", "EMRs 2011", "Electronic Money Regulations 2011", "regulation", "supplements", ["e-money", "uk"], ["UK-050"])
_uk("uk-data-products", "uksi/2017/752", "PSRs 2017", "Payment Services Regulations 2017", "regulation", "supplements", ["payments", "uk"], ["UK-050"])

# ---- F11 us-broker-dealer ----
_src("us-broker-dealer", "usc/15/exchange-act", "Exchange Act 1934", "Securities Exchange Act of 1934 (15 USC ch. 2B)", "law", "US", "US Congress (GPO)", "https://www.govinfo.gov/content/pkg/USCODE-2023-title15/html/USCODE-2023-title15-chap2B.htm", "govinfo_us", "root", "binding", ["securities", "us"], ["US-001", "GRP-002", "GRP-005", "GRP-006"], 2, fetch={"url": "https://www.govinfo.gov/content/pkg/USCODE-2023-title15/html/USCODE-2023-title15-chap2B.htm"})

# Influencer demo corpus. One HTML page and two pinned eCFR sections. No finra.org.
# 16 CFR 255 is guidance (it interprets FTC Act §5) and is still in the binding
# derivation set: the procedure cites §255.5, so L2 has to extract that clause.
_FTC_ACT_45 = "https://www.govinfo.gov/content/pkg/USCODE-2023-title15/html/USCODE-2023-title15-chap2-subchapI-sec45.htm"
_src("us-marketing", "usc/15/ftc-act-45", "FTC Act §5",
     "Federal Trade Commission Act §5 (15 USC §45) — unfair or deceptive acts or practices",
     "law", "US", "US Congress (GPO)", _FTC_ACT_45, "govinfo_us", "root", "binding",
     ["marketing", "consumer", "us"], ["US-MKT-001"], 2, rights_basis="public_domain",
     fetch={"url": _FTC_ACT_45})
_src("us-marketing", "cfr/16/255", "Endorsement Guides",
     "16 CFR Part 255 — Guides Concerning the Use of Endorsements and Testimonials in Advertising",
     "guidance", "US", "FTC (eCFR)",
     "https://www.ecfr.gov/current/title-16/chapter-I/subchapter-B/part-255",
     "govinfo_us", "interprets", "binding",
     ["marketing", "endorsement", "us"], ["US-MKT-002"], 2, rights_basis="public_domain",
     fetch={"ecfr_title": "16", "ecfr_sections": ["255.0", "255.1", "255.2", "255.3", "255.4", "255.5", "255.6"],
            "chapter": "I", "subchapter": "B", "part": "255"})
_src("us-marketing", "cfr/17/ia-marketing", "IA Marketing Rule",
     "17 CFR §275.206(4)-1 — Investment Adviser Marketing",
     "regulation", "US", "SEC (eCFR)",
     "https://www.ecfr.gov/current/title-17/chapter-II/part-275/section-275.206(4)-1",
     "govinfo_us", "implements", "binding",
     ["marketing", "adviser", "us"], ["US-MKT-003"], 2, rights_basis="public_domain",
     fetch={"ecfr_title": "17", "ecfr_sections": ["275.206(4)-1"],
            "chapter": "II", "subchapter": "", "part": "275"})
_src("us-broker-dealer", "cfr/17/240-bd", "SEC BD rules (17 CFR 240)", "SEC broker-dealer rules — 15c3-1, 15c3-3, 17a-3/4/5, 10b-10, 606", "regulation", "US", "SEC (published by GPO/eCFR)", "https://www.ecfr.gov/current/title-17/chapter-II/part-240", "govinfo_us", "implements", "binding", ["broker-dealer", "us"], ["US-001", "US-003"], 2, fetch={"url": "https://www.ecfr.gov/current/title-17/chapter-II/part-240"})
_src("us-broker-dealer", "cfr/17/reg-bi-sp", "Reg BI / S-P / S-ID", "SEC Regulations Best Interest, S-P (as amended 2024), S-ID", "regulation", "US", "SEC (eCFR)", "https://www.ecfr.gov/current/title-17/chapter-II/part-240/subpart-N", "govinfo_us", "implements", "binding", ["conduct", "privacy", "us"], ["US-002", "US-004", "US-005"], 2, fetch={"ecfr_title": "17", "ecfr_sections": ["240.15l-1", "248.30", "248.201"], "chapter": "II", "part": "240"})
_src("us-broker-dealer", "finra/rulebook", "FINRA rulebook collection", "FINRA rulebook collection index — constituent rules are separate documents", "regulation", "US", "FINRA", "https://www.finra.org/rules-guidance/rulebooks/finra-rules", "finra", "supplements", "binding", ["supervision", "aml", "communications", "us"], ["US-006", "US-007", "US-008"], 2, fetch={"url": "https://www.finra.org/rules-guidance/rulebooks/finra-rules", "channel": "finra"}, rights_basis="derived_only", publisher="FINRA (official rulebook on finra.org)")
# FINRA text is retrieved directly from finra.org; SEC releases use sec.gov.
for _rule, _ids in [("3110", ["US-006"]), ("2111", ["US-007"]), ("3310", ["US-008"]), ("2210", ["US-007"]), ("4511", ["US-006"])]:
    _src("us-broker-dealer", f"finra/rule/{_rule}", f"FINRA {_rule}", f"FINRA Rule {_rule} (current official rulebook text)", "regulation", "US", "FINRA",
         f"https://www.finra.org/rules-guidance/rulebooks/finra-rules/{_rule}", "finra", "supplements", "binding", ["supervision", "us"], _ids, 2,
         fetch={"url": f"https://www.finra.org/rules-guidance/rulebooks/finra-rules/{_rule}", "channel": "finra"}, rights_basis="derived_only", publisher="FINRA (official rulebook on finra.org)")
_src("us-broker-dealer", "sec/release/34-86031", "Reg BI adopting release", "SEC Release 34-86031 — Regulation Best Interest (adopting release)", "regulation", "US", "U.S. Securities and Exchange Commission",
     "https://www.sec.gov/files/rules/final/2019/34-86031.pdf", "sec_edgar", "implements", "guidance", ["conduct", "us"], ["US-002"], 2,
     fetch={"url": "https://www.sec.gov/files/rules/final/2019/34-86031.pdf", "channel": "sec"}, rights_basis="public_domain", publisher="U.S. Securities and Exchange Commission")
_src("us-broker-dealer", "sec/release/34-100155", "Reg S-P amendments 2024", "SEC Release 34-100155 — Regulation S-P amendments (customer information safeguards, 2024)", "regulation", "US", "U.S. Securities and Exchange Commission",
     "https://www.sec.gov/files/rules/final/2024/34-100155.pdf", "sec_edgar", "implements", "guidance", ["privacy", "cyber", "us"], ["US-004"], 2,
     fetch={"url": "https://www.sec.gov/files/rules/final/2024/34-100155.pdf", "channel": "sec"}, rights_basis="public_domain", publisher="U.S. Securities and Exchange Commission")
_src("us-broker-dealer", "usc/15/securities-act", "Securities Act 1933", "Securities Act of 1933", "law", "US", "US Congress (GPO)", "https://www.govinfo.gov/content/pkg/USCODE-2023-title15/html/USCODE-2023-title15-chap2A.htm", "govinfo_us", "supplements", "binding", ["securities", "us"], ["GRP-001"], 2, fetch={"url": "https://www.govinfo.gov/content/pkg/USCODE-2023-title15/html/USCODE-2023-title15-chap2A.htm"})
_src("us-broker-dealer", "usc/15/sox", "SOX 2002", "Sarbanes-Oxley Act 2002 (§302/404/906)", "law", "US", "US Congress (GPO)", "https://www.govinfo.gov/content/pkg/COMPS-1883/html/COMPS-1883.htm", "govinfo_us", "supplements", "binding", ["governance", "icfr", "us"], ["GRP-003"], 2, fetch={"url": "https://www.govinfo.gov/content/pkg/COMPS-1883/html/COMPS-1883.htm"})
_src("us-broker-dealer", "nasdaq/5600", "Nasdaq 5600", "Nasdaq Listing Rules — 5600 governance series", "regulation", "US", "Nasdaq", "https://listingcenter.nasdaq.com/rulebook/nasdaq/rules/nasdaq-5600-series", "nasdaq", "supplements", "binding", ["listed-company", "us"], ["GRP-004"], 2, fetch={"url": "https://listingcenter.nasdaq.com/rulebook/nasdaq/rules/nasdaq-5600-series"})

# ---- F12 us-crypto-msb ----
_src("us-crypto-msb", "cfr/31/chapter-x", "BSA / 31 CFR Ch. X", "Bank Secrecy Act rules — 31 CFR Chapter X (MSB: AML program, SAR/CTR, travel rule)", "regulation", "US", "FinCEN (eCFR)", "https://www.ecfr.gov/current/title-31/subtitle-B/chapter-X", "govinfo_us", "root", "binding", ["aml", "msb", "us"], ["US-020"], 2, fetch={"url": "https://www.ecfr.gov/current/title-31/subtitle-B/chapter-X"})
_src("us-crypto-msb", "nydfs/part200-500", "NYDFS 200 + 500", "NYDFS regulations collection — individual regulations require discovery", "regulation", "US-NY", "NYDFS", "https://www.dfs.ny.gov/industry_guidance/regulations", "nydfs", "supplements", "binding", ["crypto", "cyber", "us"], ["US-022"], 2, fetch={"url": "https://www.dfs.ny.gov/industry_guidance/regulations"})

# ---- F13 au-afsl ----
_src("au-afsl", "au/corporations-act-ch7", "Corporations Act Ch 7", "Corporations Act 2001 — Chapter 7 (AFSL, disclosure, client money)", "law", "AU", "Commonwealth of Australia", "https://www.legislation.gov.au/C2004A00818/latest/text", "au_legislation", "root", "binding", ["conduct", "au"], ["AU-001", "AU-002", "AU-004"], 2, fetch={"url": "https://www.legislation.gov.au/C2004A00818/latest/text"})
_src("au-afsl", "au/aml-ctf-act", "AML/CTF Act", "Anti-Money Laundering and Counter-Terrorism Financing Act 2006 (2024 reform live Mar 2026)", "law", "AU", "Commonwealth of Australia / AUSTRAC", "https://www.legislation.gov.au/C2006A00169/latest/text", "au_legislation", "supplements", "binding", ["aml", "au"], ["AU-007"], 2, fetch={"url": "https://www.legislation.gov.au/C2006A00169/latest/text"})
_src("au-afsl", "au/asic-dtr-2024", "ASIC DTR 2024", "ASIC Derivative Transaction Rules (Reporting) 2024", "regulation", "AU", "ASIC", "https://www.legislation.gov.au/F2024L00661/latest/text", "au_legislation", "implements", "binding", ["reporting", "au"], ["AU-005"], 2, fetch={"url": "https://www.legislation.gov.au/F2024L00661/latest/text"})
_src("au-afsl", "au/privacy-act-1988", "AU Privacy Act", "Privacy Act 1988 (incl. 2024 amendments)", "law", "AU", "Commonwealth of Australia", "https://www.legislation.gov.au/C2004A03712/latest/text", "au_legislation", "supplements", "binding", ["privacy", "au"], ["GRP-034"], 2, fetch={"url": "https://www.legislation.gov.au/C2004A03712/latest/text"})

# ---- F14 me-adgm ----
_src("me-adgm", "adgm/fsmr", "ADGM FSMR", "ADGM Financial Services and Markets Regulations 2015", "regulation", "AE-ADGM", "ADGM FSRA", "https://en.adgm.thomsonreuters.com/rulebook/financial-services-and-markets-regulations-2015", "adgm", "root", "binding", ["conduct", "prudential", "uae"], ["ME-001", "ME-002"], 2, fetch={"url": "https://en.adgm.thomsonreuters.com/rulebook/financial-services-and-markets-regulations-2015"})
_src("me-adgm", "ae/aml-decree-20-2018", "UAE AML law", "UAE Federal Decree-Law 20/2018 on AML/CFT (as amended)", "law", "AE", "UAE Federal Government", "https://uaelegislation.gov.ae/en/legislations/1514", "uae", "supplements", "binding", ["aml", "uae"], ["ME-003"], 3, fetch={"url": "https://uaelegislation.gov.ae/en/legislations/1514"})

# ---- F15 sg-mas ----
_src("sg-mas", "sg/sfa-2001", "SFA 2001", "Securities and Futures Act 2001", "law", "SG", "Republic of Singapore / MAS", "https://sso.agc.gov.sg/Act/SFA2001", "sg_legislation", "root", "binding", ["conduct", "sg"], ["SG-001", "SG-004"], 3, fetch={"url": "https://sso.agc.gov.sg/Act/SFA2001"})
_src("sg-mas", "sg/mas-aml-sfa04-n02", "MAS AML Notice", "MAS Notice SFA04-N02 — AML/CFT for capital markets intermediaries", "guidance", "SG", "MAS", "https://www.mas.gov.sg/regulation/notices/notice-sfa04-n02", "mas", "supplements", "binding", ["aml", "sg"], ["SG-002"], 3, fetch={"url": "https://www.mas.gov.sg/regulation/notices/notice-sfa04-n02", "kind": "pdf"})
_src("sg-mas", "sg/pdpa-2012", "PDPA", "Personal Data Protection Act 2012", "law", "SG", "Republic of Singapore", "https://sso.agc.gov.sg/Act/PDPA2012", "sg_legislation", "supplements", "binding", ["privacy", "sg"], ["SG-005", "GRP-035"], 3, fetch={"url": "https://sso.agc.gov.sg/Act/PDPA2012"})

# ---- F16 small-entities ----
_src("small-entities", "sc/securities-act-2007", "SC Securities Act", "Seychelles legislation collection — individual acts require discovery", "law", "SC", "Republic of Seychelles / FSA", "https://fsaseychelles.sc/legal-framework/legislation", "seychelles", "root", "binding", ["conduct", "seychelles"], ["SC-001", "SC-005"], 3, fetch={"url": "https://fsaseychelles.sc/legal-framework/legislation", "kind": "pdf"})
_src("small-entities", "mt/cap376", "Malta FIA", "Malta Financial Institutions Act (Cap 376)", "law", "MT", "Republic of Malta / MFSA", "https://legislation.mt/eli/cap/376/eng", "malta", "supplements", "binding", ["e-money", "malta"], ["MT-001", "MT-002"], 3, fetch={"url": "https://legislation.mt/eli/cap/376/eng"})
_src("small-entities", "mt/pmlftr", "Malta PMLFTR", "Malta Prevention of Money Laundering and Funding of Terrorism Regulations (S.L. 373.01)", "regulation", "MT", "Republic of Malta / FIAU", "https://legislation.mt/eli/sl/373.1/eng", "malta", "supplements", "binding", ["aml", "malta"], ["MT-003"], 3, fetch={"url": "https://legislation.mt/eli/sl/373.1/eng"})
_src("small-entities", "gi/fsa-2019-dlt", "Gibraltar DLT", "Gibraltar Financial Services Act 2019 — DLT provider framework", "law", "GI", "HM Government of Gibraltar / GFSC", "https://www.gibraltarlaws.gov.gi/legislations/financial-services-act-2019-4681", "gibraltar", "supplements", "binding", ["crypto", "gibraltar"], ["GI-001"], 3, fetch={"url": "https://www.gibraltarlaws.gov.gi/legislations/financial-services-act-2019-4681", "kind": "pdf"})
_src("small-entities", "il/privacy-5741", "IL Privacy Law", "Israeli Privacy Protection Law 5741-1981 incl. Amendment 13 (in force Aug 2025)", "law", "IL", "State of Israel", "https://www.gov.il/en/departments/legalInfo/privacy_protection_law", "israel", "supplements", "binding", ["privacy", "israel"], ["GRP-032", "IL-002"], 3, fetch={"url": "https://www.gov.il/en/departments/legalInfo/privacy_protection_law", "kind": "pdf"})

# ---- F17 sanctions-lists ----
_src("sanctions-lists", "lists/un-consolidated", "UN sanctions list", "UN Security Council Consolidated List", "guidance", "INTL", "United Nations Security Council", "https://scsanctions.un.org/resources/xml/en/consolidated.xml", "lists", "root", "binding", ["sanctions", "screening"], ["GRP-020"], 3, fetch={"url": "https://scsanctions.un.org/resources/xml/en/consolidated.xml"})
_src("sanctions-lists", "lists/ofac-sdn", "OFAC SDN", "US OFAC Specially Designated Nationals list (SDN only)", "guidance", "US", "US Treasury OFAC", "https://www.treasury.gov/ofac/downloads/sdn.xml", "lists", "supplements", "binding", ["sanctions", "screening", "us"], ["GRP-021"], 3, fetch={"url": "https://www.treasury.gov/ofac/downloads/sdn.xml"})
_src("sanctions-lists", "lists/eu-consolidated", "EU consolidated list", "EU consolidated list of persons subject to financial sanctions", "guidance", "EU", "European Commission", "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw", "lists", "supplements", "binding", ["sanctions", "screening", "eu"], ["GRP-022"], 3, fetch={"url": "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw"})
_src("sanctions-lists", "lists/uk-ofsi", "UK OFSI list", "UK OFSI consolidated list of financial sanctions targets", "guidance", "UK", "HM Treasury OFSI", "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv", "lists", "supplements", "binding", ["sanctions", "screening", "uk"], ["GRP-023"], 3, fetch={"url": "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv"})

# ---- F18 intl-tax-aeoi ----
_eu("intl-tax-aeoi", "32023L2226", "DAC8", "Directive (EU) 2023/2226 — crypto-asset reporting (in force Jan 2026)", "law", "root", ["tax-reporting", "crypto", "eu"], ["TAX-003"])
_uk("intl-tax-aeoi", "uksi/2015/878", "UK ITC Regs (CRS)", "International Tax Compliance Regulations 2015 (CRS/FATCA; CARF/CRS 2.0 amendments)", "regulation", "supplements", ["tax-reporting", "uk"], ["TAX-002", "TAX-004"])
_src("intl-tax-aeoi", "irs/qi-agreement", "QI Agreement", "IRS Qualified Intermediary agreement (Rev. Proc. 2022-43)", "agreement", "US", "IRS", "https://www.irs.gov/pub/irs-drop/rp-22-43.pdf", "irs_gov", "supplements", "binding", ["withholding", "us"], ["TAX-010"], 3, fetch={"url": "https://www.irs.gov/pub/irs-drop/rp-22-43.pdf", "kind": "pdf"})
_src("intl-tax-aeoi", "cfr/26/871m", "§871(m) regs", "26 CFR §1.871-15 — dividend-equivalent withholding on derivatives", "regulation", "US", "Treasury/IRS (eCFR)", "https://www.ecfr.gov/current/title-26/chapter-I/subchapter-A/part-1/section-1.871-15", "govinfo_us", "supplements", "binding", ["withholding", "cfd", "us"], ["TAX-011"], 2, fetch={"ecfr_title": "26", "ecfr_sections": ["1.871-15"], "chapter": "I", "part": "1"})
# Image-only SI PDF at legislation.gov.uk — ingest the HTML contents page instead.
_src("intl-tax-aeoi", "uksi/1986/1711", "UK SDRT", "Stamp Duty Reserve Tax Regulations 1986 + FA 1986 Part IV", "regulation", "UK", UK_ISSUER,
     UKLEG + "uksi/1986/1711/contents", "uk_legislation", "supplements", "binding", ["transaction-tax", "uk"], ["TAX-020"], 3,
     fetch={"kind": "html", "url": UKLEG + "uksi/1986/1711/contents"})

# ---- F19 standards ----
_src("standards", "fatf/40-recommendations", "FATF 40", "FATF Recommendations and included interpretive notes", "standard", "INTL", "FATF", "https://www.fatf-gafi.org/content/dam/fatf-gafi/recommendations/FATF%20Recommendations%202012.pdf.coredownload.inline.pdf", "fatf", "root", "guidance", ["aml", "standards"], ["GRP-026", "STD-008"], 3, fetch={"url": "https://www.fatf-gafi.org/content/dam/fatf-gafi/recommendations/FATF%20Recommendations%202012.pdf.coredownload.inline.pdf", "kind": "pdf"})
# HLD v2 starter corpus: standard-setter and regulator publications.
_src("standards", "bis/basel/CRE20", "Basel CRE20", "Basel Framework — CRE20 Standardised approach: individual exposures", "standard", "INTL", "Basel Committee on Banking Supervision", "https://www.bis.org/basel_framework/chapter/CRE/20.htm", "bis_basel", "supplements", "guidance", ["prudential", "standards"], ["STD-010"], 3, fetch={"url": "https://www.bis.org/basel_framework/chapter/CRE/20.htm"})
_src("standards", "bis/basel/OPE25", "Basel OPE25", "Basel Framework — OPE25 Standardised approach to operational risk", "standard", "INTL", "Basel Committee on Banking Supervision", "https://www.bis.org/basel_framework/chapter/OPE/25.htm", "bis_basel", "supplements", "guidance", ["operational-risk", "standards"], ["STD-010"], 3, fetch={"url": "https://www.bis.org/basel_framework/chapter/OPE/25.htm"})
_src("standards", "iosco/objectives-principles", "IOSCO Principles", "IOSCO Objectives and Principles of Securities Regulation (2017)", "standard", "INTL", "IOSCO", "https://www.iosco.org/library/pubdocs/pdf/IOSCOPD561.pdf", "iosco", "supplements", "guidance", ["securities", "standards"], ["STD-011"], 3, fetch={"url": "https://www.iosco.org/library/pubdocs/pdf/IOSCOPD561.pdf", "kind": "pdf"})
_src("eu-mifid", "esma/guidelines/suitability", "ESMA suitability GL", "ESMA Guidelines on certain aspects of the MiFID II suitability requirements (ESMA35-43-3172)", "guidance", "EU", "European Securities and Markets Authority", "https://www.esma.europa.eu/sites/default/files/library/esma35-43-3172_final_report_on_mifid_ii_guidelines_on_suitability.pdf", "esma", "interprets", "guidance", ["conduct", "suitability", "eu"], ["EU-004"], 3, fetch={"url": "https://www.esma.europa.eu/sites/default/files/library/esma35-43-3172_final_report_on_mifid_ii_guidelines_on_suitability.pdf", "kind": "pdf"})
_src("eu-mifid", "esma/guidelines/product-governance", "ESMA PG GL", "ESMA Guidelines on MiFID II product governance requirements (ESMA35-43-3448)", "guidance", "EU", "European Securities and Markets Authority", "https://www.esma.europa.eu/sites/default/files/2023-08/ESMA35-43-3448_Guidelines_on_MiFID_II_product_governance_requirements.pdf", "esma", "interprets", "guidance", ["products", "eu"], ["EU-011"], 3, fetch={"url": "https://www.esma.europa.eu/sites/default/files/2023-08/ESMA35-43-3448_Guidelines_on_MiFID_II_product_governance_requirements.pdf", "kind": "pdf"})
_src("eu-markets", "esma/guidelines/mar-delay", "ESMA MAR delay GL", "ESMA Guidelines on delay in the disclosure of inside information (MAR, ESMA70-159-4966)", "guidance", "EU", "European Securities and Markets Authority", "https://www.esma.europa.eu/sites/default/files/library/esma70-159-4966_guidelines_on_delay_in_the_disclosure_of_inside_information_and_interactions_with_prudential_supervision.pdf", "esma", "interprets", "guidance", ["market-abuse", "eu"], ["EU-014"], 3, fetch={"url": "https://www.esma.europa.eu/sites/default/files/library/esma70-159-4966_guidelines_on_delay_in_the_disclosure_of_inside_information_and_interactions_with_prudential_supervision.pdf", "kind": "pdf"})
_src("au-afsl", "au/asic-rg227", "ASIC RG 227", "ASIC Regulatory Guide 227 — Over-the-counter contracts for difference: improving disclosure for retail investors", "guidance", "AU", "ASIC", "https://asic.gov.au/regulatory-resources/find-a-document/regulatory-guides/rg-227-over-the-counter-contracts-for-difference-improving-disclosure-for-retail-investors/", "asic", "interprets", "guidance", ["cfd", "disclosure", "au"], ["AU-003"], 3, fetch={"url": "https://download.asic.gov.au/media/5kipk33q/rg227-published-12-august-2011_20230906.pdf", "kind": "pdf"})
_src("au-afsl", "au/asic-rg271", "ASIC RG 271", "ASIC Regulatory Guide 271 — Internal dispute resolution", "guidance", "AU", "ASIC", "https://asic.gov.au/regulatory-resources/find-a-document/regulatory-guides/rg-271-internal-dispute-resolution/", "asic", "interprets", "guidance", ["complaints", "au"], ["AU-009"], 3, fetch={"url": "https://download.asic.gov.au/media/3olo5aq5/rg271-published-2-september-2021.pdf", "kind": "pdf"})
_src("il-isa", "il/securities-law-5728", "IL Securities Law", "ISA English legislation collection — translations require document identification", "law", "IL", "Israel Securities Authority", "https://www.isa.gov.il/sites/ISAEng/1489/1511/Pages/default.aspx", "isa", "root", "binding", ["securities", "israel"], ["IL-001"], 3, fetch={"url": "https://www.isa.gov.il/sites/ISAEng/1489/1511/Pages/default.aspx", "kind": "pdf"}, rights_basis="derived_only")
_src("sg-mas", "sg/mas-psn02", "MAS PSN02", "MAS Notice PSN02 — Prevention of Money Laundering and Countering the Financing of Terrorism (digital payment token services)", "guidance", "SG", "MAS", "https://www.mas.gov.sg/regulation/notices/psn02-aml-cft-notice---digital-payment-token-service", "mas", "supplements", "binding", ["aml", "crypto", "sg"], ["SG-003"], 3, fetch={"url": "https://www.mas.gov.sg/regulation/notices/psn02-aml-cft-notice---digital-payment-token-service", "kind": "pdf"})
_src("standards", "wolfsberg/standards", "Wolfsberg", "Wolfsberg standards collection — individual documents require discovery", "standard", "INTL", "Wolfsberg Group", "https://www.wolfsberg-principles.com/wolfsberg-group-standards", "wolfsberg", "supplements", "guidance", ["aml", "standards"], ["GRP-027"], 3, fetch={"url": "https://www.wolfsberg-principles.com/wolfsberg-group-standards", "kind": "pdf"})
# Licensed originals stay identified by canonical_url. fetch.url is a public
# stand-in the live demo can scrape now (NIST / EUR-Lex). Swap back to the
# licensed file when redistribution rights are in place.
_NIST_CSF = "https://nvlpubs.nist.gov/nistpubs/CSWP/NIST.CSWP.29.pdf"
_NIST_171 = "https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-171r3.pdf"
_NIST_PRIVACY = "https://nvlpubs.nist.gov/nistpubs/CSWP/NIST.CSWP.01162020.pdf"
_src("standards", "iso/27001-2022", "ISO 27001:2022", "ISO/IEC 27001:2022 — demo uses NIST CSF 2.0 (public domain) until the licensed ISO file is attached", "standard", "INTL", "ISO/IEC", "https://www.iso.org/standard/27001", "restricted_file", "supplements", "guidance", ["infosec", "standards"], ["STD-001"], 4, license="restricted", fetch={"url": _NIST_CSF, "kind": "pdf"})
_src("standards", "iso/27001-2022-amd1-2024", "ISO 27001 Amd 1:2024", "ISO/IEC 27001:2022/Amd 1:2024 — demo uses the NIST Privacy Framework (public domain) until the licensed amendment is attached", "standard", "INTL", "ISO/IEC", "https://www.iso.org/standard/88435.html", "restricted_file", "amends", "guidance", ["infosec", "standards"], ["STD-001-A1"], 4, license="restricted", fetch={"url": _NIST_PRIVACY, "kind": "pdf"})
_src("standards", "aicpa/soc2-tsc", "SOC 2 TSC", "AICPA Trust Services Criteria — demo uses NIST SP 800-171 (public domain) until the licensed TSC file is attached", "standard", "US", "AICPA", "https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022", "restricted_file", "supplements", "guidance", ["infosec", "assurance", "standards"], ["STD-002"], 4, license="restricted", fetch={"url": _NIST_171, "kind": "pdf"})
_src("standards", "pci/dss-v4", "PCI DSS v4", "PCI DSS v4 — demo uses the public EU PSD2 RTS on strong customer authentication (OJ L 69, 2018) until a PCI SSC artifact is attached", "standard", "INTL", "PCI SSC", "https://www.pcisecuritystandards.org/", "restricted_file", "supplements", "guidance", ["payments", "infosec"], ["STD-004"], 4, license="restricted",      fetch={"url": EURLEX_HTML + "32018R0389", "kind": "html", "celex": "32018R0389"})
_src("standards", "ifrs/standards", "IFRS", "IFRS — demo uses Commission Regulation (EU) 2023/2468 (IAS 12 / OECD Pillar Two, Official Journal) until licensed IFRS text is attached", "standard", "INTL", "IFRS Foundation", "https://www.ifrs.org/", "restricted_file", "supplements", "guidance", ["financial-reporting"], ["GRP-009"], 4, license="restricted", fetch={"url": EURLEX_HTML + "32023R2468", "kind": "html", "celex": "32023R2468"})

# ---- F20 host-state-overlays ----
for _cc, _name, _ids in [
    ("be", "Belgium — FSMA OTC-CFD/leveraged retail distribution ban (2016)", ["OVL-BE"]),
    ("fr", "France — Sapin II advertising ban + Loi Influenceurs 2023-451 + AMF doctrine", ["OVL-FR"]),
    ("es", "Spain — CNMV 2023 CFD marketing resolution + crypto-ad Circular 1/2022", ["OVL-ES"]),
    ("de", "Germany — BaFin CFD general administrative act + marketing rules", ["OVL-DE"]),
    ("it", "Italy — Consob measures + FTT interaction", ["OVL-IT"]),
]:
    _src("host-state-overlays", f"ovl/{_cc}", f"Overlay {_cc.upper()}", _name, "guidance", _cc.upper(), "National regulator", "", "overlay", "supplements" if _cc != "be" else "root", "binding", ["product-intervention", "marketing", _cc], _ids, 3, fetch={"blocked": "official_document_inventory_required"})


# Collection/reference records remain visible but cannot count as imported
# documents or be passed through a whole-page text adapter as a substitute.
COLLECTION_SOURCE_KEYS = frozenset({
    "finra/rulebook", "nydfs/part200-500", "cy/l87i-2017", "sc/securities-act-2007",
    "wolfsberg/standards", "il/securities-law-5728",
})
REFERENCE_SOURCE_KEYS = frozenset({"ovl/be", "ovl/fr", "ovl/es", "ovl/de", "ovl/it"})


def is_collection(key):
    return key in COLLECTION_SOURCE_KEYS


def source_role(key):
    return "collection" if is_collection(key) else "reference" if key in REFERENCE_SOURCE_KEYS else "document"


for _entry in S:
    _entry["source_role"] = source_role(_entry["key"])
    if _entry["source_role"] != "document":
        _entry["fetch"] = {**(_entry.get("fetch") or {}), "blocked": "collection_requires_document_discovery" if is_collection(_entry["key"]) else "official_document_inventory_required"}
    if is_collection(_entry["key"]):
        _entry["short_name"] = {"finra/rulebook": "FINRA collection", "nydfs/part200-500": "NYDFS regulations collection",
            "cy/l87i-2017": "CySEC legislation collection", "sc/securities-act-2007": "Seychelles legislation collection",
            "wolfsberg/standards": "Wolfsberg collection", "il/securities-law-5728": "ISA legislation collection"}[_entry["key"]]
        _entry["instrument"] = _entry["short_name"]
    if _entry["key"].startswith("finra/"):
        _entry["license"] = "restricted"
    if _entry["adapter"] == "fca_handbook":
        _chapters = ", ".join((_entry.get("fetch") or {}).get("chapters", []))
        _entry["name"] += " — declared chapters " + _chapters


def _about(entry: dict) -> str:
    """Describe the actual declaration, without claiming its corpus is complete."""
    role = source_role(entry["key"])
    publisher = entry.get("publisher") or entry["issuer"]
    description = f"{publisher} ({entry['jurisdiction']}). {entry['name']}."
    fetch = entry.get("fetch") or {}
    if role == "collection":
        return description + " This is a publication collection; its individual documents and editions require enumeration."
    if role == "reference":
        return description + " Official national documents have not yet been identified for this reference."
    if fetch.get("chapters"):
        description += " Declared coverage: " + str(fetch.get("sourcebook", entry["short_name"])) + " chapters " + ", ".join(fetch["chapters"]) + "."
    elif fetch.get("usc_sections") or fetch.get("ecfr_sections"):
        field = "usc_sections" if fetch.get("usc_sections") else "ecfr_sections"
        description += " Declared sections: " + ", ".join(str(v) for v in fetch[field]) + "."
    elif entry.get("discovered_category"):
        description += " Discovered in the publisher's " + entry["discovered_category"].replace("_", " ") + " collection."
    else:
        instrument = entry.get("instrument") or entry["short_name"]
        if instrument.casefold() not in entry["name"].casefold():
            description += " Instrument: " + instrument + "."
    return description


_LICENSE_REF = {
    "uk_legislation": "OGL-UK-3.0",
    "eur_lex": "Commission Decision 2011/833/EU",
    "govinfo_us": "public domain (17 U.S.C. 105)",
    "lists": "publisher terms — list data reused as published",
    "restricted_file": "BYOL — public APIs omit raw_text until a licensed file is in restricted/",
    "fca_handbook": "FCA Handbook copyright notice",
    "finra": "FINRA rulebook copyright notice — derived facts only",
    "sec_edgar": "public domain (17 U.S.C. 105)",
    "au_legislation": "CC BY 4.0 (Federal Register of Legislation)",
    "sg_legislation": "Singapore legislation copyright",
    "esma": "ESMA legal notice — reuse with acknowledgement",
    "bis_basel": "BIS copyright — reproduction with acknowledgement",
    "iosco": "IOSCO copyright — reproduction with acknowledgement",
    "asic": "CC BY 4.0 (ASIC)",
    "isa": "ISA unofficial translation — derived facts only",
    "irs_gov": "public domain (17 U.S.C. 105)",
    "fatf": "FATF terms — non-commercial reproduction with acknowledgement",
    "mas": "MAS terms of use",
}


def source_meta(entry: dict) -> SourceMeta:
    """Build the adapter SourceMeta for a registry entry."""
    license_ref = _LICENSE_REF.get(entry["adapter"], entry.get("license_ref") or "")
    return SourceMeta(
        rights_basis=entry.get("rights_basis", ""),
        publisher=entry.get("publisher") or entry["issuer"],
        instrument=entry.get("instrument") or entry["short_name"],
        family_key=entry["family"],
        family_name=FAMILY_NAMES.get(entry["family"], entry.get("family_name") or entry["family"]),
        source_key=entry["key"],
        name=entry["name"],
        kind=entry["kind"],
        issuer=entry["issuer"],
        jurisdiction=entry["jurisdiction"],
        license=entry["license"],
        license_ref=license_ref,
        canonical_url=entry["canonical_url"],
        adapter=entry["adapter"],
        short_name=entry["short_name"],
        about=_about(entry),
        topics=entry["topics"],
        version_policy=("as_published" if entry["adapter"] == "eur_lex" else
                        "edition" if entry["adapter"] == "restricted_file" else "consolidated"),
    )


def wave1_entries() -> list[dict]:
    """Class-A subset: EUR-Lex / UK CLML rows with a structured fetch dict.

    HTML-only UK paths (e.g. SDRT) and later-class adapters are fleet rows,
    not Wave-1.
    """
    return [
        e
        for e in S
        if e.get("fetch")
        and e["adapter"] in {"eur_lex", "uk_legislation"}
        and (e.get("fetch") or {}).get("kind") != "html"
    ]


def wave1_adapters(adapter_key: str | None = None) -> list[tuple[dict, object]]:
    """Instantiate the Wave-1 ingestion plan: (entry, adapter) pairs.

    Optionally filtered to one adapter key (the per-adapter daily schedule).
    """
    from app.clhear.l1.adapters.eur_lex import EurLexAdapter
    from app.clhear.l1.adapters.uk_legislation import UkLegislationAdapter

    plan = []
    for entry in wave1_entries():
        if adapter_key and entry["adapter"] != adapter_key:
            continue
        meta = source_meta(entry)
        if entry["adapter"] == "eur_lex":
            celex = entry["fetch"]["celex"]
            version = entry["fetch"].get("celex_version", celex)
            adapter = EurLexAdapter(celex=celex, celex_version=version, meta=meta)
        else:
            adapter = UkLegislationAdapter(doc=entry["fetch"]["doc"], name=entry["name"], meta=meta)
        plan.append((entry, adapter))
    return plan


def seed(engine: Engine) -> dict:
    """Reconcile declared metadata without rewriting text/version history."""
    from app.clhear.l1.origin import is_test_source
    created_f = created_s = skipped = 0
    with engine.begin() as conn:
        family_ids: dict[str, int] = {}
        for key, name, charter in FAMILIES:
            existing = conn.execute(sa.select(source_families.c.id).where(source_families.c.key == key)).scalar()
            if existing:
                family_ids[key] = existing
                conn.execute(source_families.update().where(source_families.c.id == existing).values(
                    name=name, scope_charter={"registry": charter, "scope": "company_independent"}))
                continue
            family_ids[key] = conn.execute(
                source_families.insert()
                .values(key=key, name=name, scope_charter={"registry": charter, "scope": "company_independent"})
                .returning(source_families.c.id)
            ).scalar_one()
            created_f += 1
        for s in S:
            existing = conn.execute(sa.select(sources).where(sources.c.key == s["key"])).mappings().first()
            existing_id = existing["id"] if existing else None
            if existing and is_test_source(existing):
                # Renaming fixture metadata to a real publisher would hide its
                # origin while leaving the same invented historical text.
                skipped += 1
                continue
            if existing_id:
                # Metadata correction is idempotent; source IDs, version
                # statuses, artifacts, nodes and clauses are never rewritten.
                conn.execute(sources.update().where(sources.c.id == existing_id).values(
                    name=s["name"], short_name=s["short_name"], canonical_url=s["canonical_url"],
                    instrument=s["instrument"], about=_about(s), topics=s["topics"],
                    issuer=s["issuer"], publisher=s["publisher"], license=s["license"], adapter=s["adapter"]))
                member = conn.execute(
                    sa.select(family_members.c.source_id).where(
                        family_members.c.family_id == family_ids[s["family"]],
                        family_members.c.source_id == existing_id,
                    )
                ).scalar()
                if not member:
                    conn.execute(
                        family_members.insert().values(
                            family_id=family_ids[s["family"]],
                            source_id=existing_id,
                            relation=s["relation"],
                            tier=s["tier"],
                            status="active",
                            added_via="watchlist",
                        )
                    )
                skipped += 1
                continue
            source_id = conn.execute(
                sources.insert()
                .values(
                    family_id=family_ids[s["family"]],
                    key=s["key"],
                    name=s["name"],
                    short_name=s["short_name"],
                    kind=s["kind"],
                    issuer=s["issuer"],
                    jurisdiction=s["jurisdiction"],
                    license=s["license"],
                    adapter=s["adapter"],
                    canonical_url=s["canonical_url"],
                    about=_about(s),
                    topics=s["topics"],
                )
                .returning(sources.c.id)
            ).scalar_one()
            conn.execute(
                family_members.insert().values(
                    family_id=family_ids[s["family"]],
                    source_id=source_id,
                    relation=s["relation"],
                    tier=s["tier"],
                    status="active",
                    added_via="watchlist",
                )
            )
            created_s += 1
    from app.clhear.l1.origin import reconcile_origins
    reconcile_origins(engine)
    return {"families_created": created_f, "sources_created": created_s, "skipped_existing": skipped}
