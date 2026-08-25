"""eToro design-partner L1 blueprint: families, sources, and the Wave-1 plan.

Machine-readable form of docs/ETORO_L1_SCOPE.md (which maps
docs/etoro-clhear-source-registry.md). Three consumers:
  - scripts/seed_registry.py  -> seed(engine): blueprint into the corpus
  - scripts/ingest_wave1.py   -> wave1_adapters(): class-A ingestion plan
  - workers / build_corpus    -> the same plan drives the daily fleet

Sources carry an optional `fetch` dict: present = Wave-1 ingestable today
via an existing adapter (eur_lex takes any CELEX id, uk_legislation any
legislation.gov.uk doc). Absent = reference-level until its wave lands.
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
    ("eu-data", "EU data protection & AI", "AI Act, Accessibility Act, EU sanctions regs (F7: GRP-036/037/022; GDPR lives in eu-gdpr)"),
    ("uk-fca", "UK conduct & prudential (FCA)", "FSMA, FCA Handbook, UK MiFIR/EMIR/MAR/SSR, promotions (F8: UK-001..024)"),
    ("uk-fincrime", "UK financial crime", "POCA, TACT, CFA 2017, ECCTA, Bribery (F9: UK-030..034; MLRs live in uk-mlr)"),
    ("uk-data-products", "UK data, e-money & wrappers", "UK GDPR/DPA/PECR, ISA regs, EMRs/PSRs, safeguarding (F10: GRP-031, UK-040..052)"),
    ("us-broker-dealer", "US broker-dealer & listed company", "Exchange Act BD rules, Reg BI/S-P/S-ID, FINRA, CAT, Securities Act/SOX/Nasdaq (F11: US-001..010, GRP-001..008)"),
    ("us-crypto-msb", "US crypto MSB & state", "BSA/31 CFR X, MTLs, NYDFS 200/500, GENIUS radar (F12: US-020..025)"),
    ("au-afsl", "Australia (ASIC/AUSTRAC)", "Corporations Act Ch 7, DDO, CFD PIO, DTR 2024, AML/CTF reform, RE stack (F13: AU-001..010, GRP-034)"),
    ("me-adgm", "ADGM / UAE (FSRA)", "FSMR + rulebooks, VA framework, UAE AML, ADGM DPR (F14: ME-001..005)"),
    ("sg-mas", "Singapore (MAS)", "SFA + LCB regs, MAS notices, PDPA, DPT boundary (F15: SG-001..007)"),
    ("small-entities", "Seychelles / Malta / Gibraltar / Israel / BVI", "F16: SC-*, MT-*, GI-* (gated on eToroX status), IL-*, GRP-010"),
    ("sanctions-lists", "Global sanctions & screening lists", "F17: UN, OFAC, EU, OFSI, NBCTF, DFAT, EOCN — structured LIST feeds (separate lists pipeline, class E)"),
    ("intl-tax-aeoi", "International tax reporting & transaction taxes", "F18: FATCA IGAs, CRS/DAC8/CARF, QI/871(m), FTT & stamp layer (TAX-001..031; FATCA statute/regs live in us-fatca)"),
    ("host-state-overlays", "EU/EEA host-state overlays", "F20: BE/FR/ES/DE/IT/NL/PL… product-intervention & marketing overlays (OVL-*)"),
    ("standards", "Standards & frameworks (SC4)", "F19: FATF, Wolfsberg, ISO, SOC 2, PCI, IFRS — NIST lives in nist-spine (STD-*, GRP-009/026/027)"),
]

FAMILY_NAMES = {key: name for key, name, _ in FAMILIES}

S: list[dict] = []


def _src(family, key, short_name, name, kind, jurisdiction, issuer, url, adapter,
         relation, tier, topics, ids, wave, license="open", fetch=None):
    S.append(dict(
        family=family, key=key, short_name=short_name, name=name, kind=kind,
        license=license, jurisdiction=jurisdiction, issuer=issuer, canonical_url=url,
        adapter=adapter, relation=relation, tier=tier, topics=topics,
        registry_ids=ids, wave=wave, fetch=fetch,
    ))


EURLEX = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:"
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
_src("eu-mifid", "cy/l87i-2017", "Cyprus ISL", "Cyprus Investment Services Law L.87(I)/2017", "law", "CY", "Republic of Cyprus / CySEC", "https://www.cysec.gov.cy/", "cysec", "implements", "binding", ["conduct", "cyprus"], ["EU-003"], 3)

# ---- F2 eu-markets ----
_eu("eu-markets", "32014R0596", "MAR", "Market Abuse Regulation (EU) 596/2014", "regulation", "root", ["market-abuse", "surveillance", "eu"], ["EU-014"])
_eu("eu-markets", "32012R0236", "EU SSR", "Short Selling Regulation (EU) 236/2012", "regulation", "supplements", ["markets", "eu"], ["EU-015"])
_eu("eu-markets", "32012R0648", "EMIR", "Regulation (EU) 648/2012 (EMIR) incl. Refit and EMIR 3", "regulation", "supplements", ["derivatives", "reporting", "eu"], ["EU-016"])
_eu("eu-markets", "32015R2365", "SFTR", "Securities Financing Transactions Regulation (EU) 2015/2365", "regulation", "supplements", ["reporting", "eu"], ["EU-017"])
_eu("eu-markets", "32014R0909", "CSDR", "Central Securities Depositories Regulation (EU) 909/2014", "regulation", "supplements", ["settlement", "eu"], ["EU-018"])
_eu("eu-markets", "32016R1011", "BMR", "Benchmarks Regulation (EU) 2016/1011", "regulation", "supplements", ["markets", "eu"], ["EU-019"])
_eu("eu-markets", "32014R1286", "PRIIPs", "PRIIPs Regulation (EU) 1286/2014 + KID RTS 2017/653", "regulation", "supplements", ["products", "disclosure", "eu"], ["EU-012"])

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

# ---- F7 eu-data ----
_eu("eu-data", "32024R1689", "EU AI Act", "Artificial Intelligence Act (EU) 2024/1689 (tranche 2 live Aug 2026)", "regulation", "root", ["ai", "eu"], ["GRP-036"])
_eu("eu-data", "32019L0882", "EU Accessibility Act", "Directive (EU) 2019/882 — accessibility requirements", "law", "supplements", ["accessibility", "consumer", "eu"], ["GRP-037"])
_eu("eu-data", "32014R0269", "EU sanctions 269/2014", "Regulation (EU) 269/2014 — Ukraine territorial integrity measures", "regulation", "supplements", ["sanctions", "eu"], ["GRP-022"])

# ---- F8 uk-fca ----
_uk("uk-fca", "ukpga/2000/8", "FSMA 2000", "Financial Services and Markets Act 2000", "law", "root", ["conduct", "uk"], ["UK-001"])
_uk("uk-fca", "ukpga/2023/29", "FSMA 2023", "Financial Services and Markets Act 2023", "law", "amends", ["conduct", "uk"], ["UK-001"])
_uk("uk-fca", "uksi/2001/544", "RAO 2001", "FSMA (Regulated Activities) Order 2001", "regulation", "implements", ["perimeter", "uk"], ["UK-001", "UK-041"])
_uk("uk-fca", "uksi/2005/1529", "FPO 2005", "FSMA (Financial Promotion) Order 2005", "regulation", "implements", ["marketing", "uk"], ["UK-020", "UK-021"])
_src("uk-fca", "fca/handbook", "FCA Handbook", "FCA Handbook (PRIN incl. Consumer Duty, SYSC, COBS, CASS, PROD, SUP, DISP, MIFIDPRU)", "regulation", "UK", "Financial Conduct Authority", "https://www.handbook.fca.org.uk/", "fca_handbook", "implements", "binding", ["conduct", "client-assets", "prudential", "uk"], ["UK-002", "UK-008", "UK-009", "UK-010", "UK-011"], 2)
_uk("uk-fca", "eur/2014/600", "UK MiFIR", "UK MiFIR — onshored Regulation 600/2014", "regulation", "supplements", ["reporting", "uk"], ["UK-003"], key="eur/2014/600/uk")
_uk("uk-fca", "eur/2012/648", "UK EMIR", "UK EMIR — onshored Regulation 648/2012", "regulation", "supplements", ["derivatives", "reporting", "uk"], ["UK-004"], key="eur/2012/648/uk")
_uk("uk-fca", "eur/2014/596", "UK MAR", "UK MAR — onshored Regulation 596/2014", "regulation", "supplements", ["market-abuse", "uk"], ["UK-005"], key="eur/2014/596/uk")

# ---- F9 uk-fincrime ----
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
_src("us-broker-dealer", "usc/15/exchange-act", "Exchange Act 1934", "Securities Exchange Act of 1934 (15 USC ch. 2B)", "law", "US", "US Congress (GPO)", "https://www.govinfo.gov/", "govinfo_us", "root", "binding", ["securities", "us"], ["US-001", "GRP-002", "GRP-005", "GRP-006"], 2)
_src("us-broker-dealer", "cfr/17/240-bd", "SEC BD rules (17 CFR 240)", "SEC broker-dealer rules — 15c3-1, 15c3-3, 17a-3/4/5, 10b-10, 606", "regulation", "US", "SEC (published by GPO/eCFR)", "https://www.ecfr.gov/current/title-17/chapter-II/part-240", "govinfo_us", "implements", "binding", ["broker-dealer", "us"], ["US-001", "US-003"], 2)
_src("us-broker-dealer", "cfr/17/reg-bi-sp", "Reg BI / S-P / S-ID", "SEC Regulations Best Interest, S-P (as amended 2024), S-ID", "regulation", "US", "SEC (eCFR)", "https://www.ecfr.gov/current/title-17", "govinfo_us", "implements", "binding", ["conduct", "privacy", "us"], ["US-002", "US-004", "US-005"], 2)
_src("us-broker-dealer", "finra/rulebook", "FINRA Rulebook", "FINRA rules 3110/3120/3130, 2210, 2090/2111, 3310, 4511, 4530, 1210", "regulation", "US", "FINRA", "https://www.finra.org/rules-guidance/rulebooks/finra-rules", "finra", "supplements", "binding", ["supervision", "aml", "communications", "us"], ["US-006", "US-007", "US-008"], 2)
_src("us-broker-dealer", "usc/15/securities-act", "Securities Act 1933", "Securities Act of 1933", "law", "US", "US Congress (GPO)", "https://www.govinfo.gov/", "govinfo_us", "supplements", "binding", ["securities", "us"], ["GRP-001"], 2)
_src("us-broker-dealer", "usc/15/sox", "SOX 2002", "Sarbanes-Oxley Act 2002 (§302/404/906)", "law", "US", "US Congress (GPO)", "https://www.govinfo.gov/", "govinfo_us", "supplements", "binding", ["governance", "icfr", "us"], ["GRP-003"], 2)
_src("us-broker-dealer", "nasdaq/5600", "Nasdaq 5600", "Nasdaq Listing Rules — 5600 governance series", "regulation", "US", "Nasdaq", "https://listingcenter.nasdaq.com/rulebook/nasdaq/rules", "nasdaq", "supplements", "binding", ["listed-company", "us"], ["GRP-004"], 2)

# ---- F12 us-crypto-msb ----
_src("us-crypto-msb", "cfr/31/chapter-x", "BSA / 31 CFR Ch. X", "Bank Secrecy Act rules — 31 CFR Chapter X (MSB: AML program, SAR/CTR, travel rule)", "regulation", "US", "FinCEN (eCFR)", "https://www.ecfr.gov/current/title-31/subtitle-B/chapter-X", "govinfo_us", "root", "binding", ["aml", "msb", "us"], ["US-020"], 2)
_src("us-crypto-msb", "nydfs/part200-500", "NYDFS 200 + 500", "NYDFS 23 NYCRR Part 200 (BitLicense) + Part 500 (cybersecurity, as amended)", "regulation", "US-NY", "NYDFS", "https://www.dfs.ny.gov/", "nydfs", "supplements", "binding", ["crypto", "cyber", "us"], ["US-022"], 2)

# ---- F13 au-afsl ----
_src("au-afsl", "au/corporations-act-ch7", "Corporations Act Ch 7", "Corporations Act 2001 — Chapter 7 (AFSL, disclosure, client money)", "law", "AU", "Commonwealth of Australia", "https://www.legislation.gov.au/C2004A00818/latest", "au_legislation", "root", "binding", ["conduct", "au"], ["AU-001", "AU-002", "AU-004"], 2)
_src("au-afsl", "au/aml-ctf-act", "AML/CTF Act", "Anti-Money Laundering and Counter-Terrorism Financing Act 2006 (2024 reform live Mar 2026)", "law", "AU", "Commonwealth of Australia / AUSTRAC", "https://www.legislation.gov.au/C2006A00169/latest", "au_legislation", "supplements", "binding", ["aml", "au"], ["AU-007"], 2)
_src("au-afsl", "au/asic-dtr-2024", "ASIC DTR 2024", "ASIC Derivative Transaction Rules (Reporting) 2024", "regulation", "AU", "ASIC", "https://www.legislation.gov.au/", "au_legislation", "implements", "binding", ["reporting", "au"], ["AU-005"], 2)
_src("au-afsl", "au/privacy-act-1988", "AU Privacy Act", "Privacy Act 1988 (incl. 2024 amendments)", "law", "AU", "Commonwealth of Australia", "https://www.legislation.gov.au/C2004A03712/latest", "au_legislation", "supplements", "binding", ["privacy", "au"], ["GRP-034"], 2)

# ---- F14 me-adgm ----
_src("me-adgm", "adgm/fsmr", "ADGM FSMR", "ADGM Financial Services and Markets Regulations 2015 + FSRA rulebooks (GEN/COBS/PRU/AML)", "regulation", "AE-ADGM", "ADGM FSRA", "https://en.adgm.thomsonreuters.com/rulebook", "adgm", "root", "binding", ["conduct", "prudential", "uae"], ["ME-001", "ME-002"], 2)
_src("me-adgm", "ae/aml-decree-20-2018", "UAE AML law", "UAE Federal Decree-Law 20/2018 on AML/CFT (as amended)", "law", "AE", "UAE Federal Government", "https://uaelegislation.gov.ae/", "uae", "supplements", "binding", ["aml", "uae"], ["ME-003"], 3)

# ---- F15 sg-mas ----
_src("sg-mas", "sg/sfa-2001", "SFA 2001", "Securities and Futures Act 2001 + LCB Regulations", "law", "SG", "Republic of Singapore / MAS", "https://sso.agc.gov.sg/Act/SFA2001", "sg_legislation", "root", "binding", ["conduct", "sg"], ["SG-001", "SG-004"], 3)
_src("sg-mas", "sg/mas-aml-sfa04-n02", "MAS AML Notice", "MAS Notice SFA04-N02 — AML/CFT for capital markets intermediaries", "guidance", "SG", "MAS", "https://www.mas.gov.sg/regulation/notices/notice-sfa04-n02", "mas", "supplements", "binding", ["aml", "sg"], ["SG-002"], 3)
_src("sg-mas", "sg/pdpa-2012", "PDPA", "Personal Data Protection Act 2012", "law", "SG", "Republic of Singapore", "https://sso.agc.gov.sg/Act/PDPA2012", "sg_legislation", "supplements", "binding", ["privacy", "sg"], ["SG-005", "GRP-035"], 3)

# ---- F16 small-entities ----
_src("small-entities", "sc/securities-act-2007", "SC Securities Act", "Seychelles Securities Act 2007 + Conduct of Business Regulations (SD076 conditions)", "law", "SC", "Republic of Seychelles / FSA", "https://fsaseychelles.sc/", "seychelles", "root", "binding", ["conduct", "seychelles"], ["SC-001", "SC-005"], 3)
_src("small-entities", "mt/cap376", "Malta FIA", "Malta Financial Institutions Act (Cap 376) + EMD2/PSD2 transpositions + safeguarding", "law", "MT", "Republic of Malta / MFSA", "https://legislation.mt/", "malta", "supplements", "binding", ["e-money", "malta"], ["MT-001", "MT-002"], 3)
_src("small-entities", "mt/pmlftr", "Malta PMLFTR", "Prevention of Money Laundering and Funding of Terrorism Regulations (Cap 373) + FIAU procedures", "regulation", "MT", "Republic of Malta / FIAU", "https://legislation.mt/", "malta", "supplements", "binding", ["aml", "malta"], ["MT-003"], 3)
_src("small-entities", "gi/fsa-2019-dlt", "Gibraltar DLT", "Gibraltar Financial Services Act 2019 — DLT provider framework [GATED: verify eToroX status]", "law", "GI", "HM Government of Gibraltar / GFSC", "https://www.gfsc.gi/", "gibraltar", "supplements", "binding", ["crypto", "gibraltar"], ["GI-001"], 3)
_src("small-entities", "il/privacy-5741", "IL Privacy Law", "Israeli Privacy Protection Law 5741-1981 incl. Amendment 13 (in force Aug 2025)", "law", "IL", "State of Israel", "https://www.gov.il/he/departments/the_privacy_protection_authority", "israel", "supplements", "binding", ["privacy", "israel"], ["GRP-032", "IL-002"], 3)

# ---- F17 sanctions-lists ----
_src("sanctions-lists", "lists/un-consolidated", "UN sanctions list", "UN Security Council Consolidated List", "guidance", "INTL", "United Nations Security Council", "https://www.un.org/securitycouncil/content/un-sc-consolidated-list", "lists", "root", "binding", ["sanctions", "screening"], ["GRP-020"], 3)
_src("sanctions-lists", "lists/ofac-sdn", "OFAC SDN", "US OFAC SDN + SSI lists + 50% rule guidance", "guidance", "US", "US Treasury OFAC", "https://sanctionslist.ofac.treas.gov/", "lists", "supplements", "binding", ["sanctions", "screening", "us"], ["GRP-021"], 3)
_src("sanctions-lists", "lists/eu-consolidated", "EU consolidated list", "EU consolidated list of persons subject to financial sanctions", "guidance", "EU", "European Commission", "https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions", "lists", "supplements", "binding", ["sanctions", "screening", "eu"], ["GRP-022"], 3)
_src("sanctions-lists", "lists/uk-ofsi", "UK OFSI list", "UK OFSI consolidated list of financial sanctions targets", "guidance", "UK", "HM Treasury OFSI", "https://www.gov.uk/government/publications/financial-sanctions-consolidated-list-of-targets", "lists", "supplements", "binding", ["sanctions", "screening", "uk"], ["GRP-023"], 3)

# ---- F18 intl-tax-aeoi ----
_eu("intl-tax-aeoi", "32023L2226", "DAC8", "Directive (EU) 2023/2226 — crypto-asset reporting (in force Jan 2026)", "law", "root", ["tax-reporting", "crypto", "eu"], ["TAX-003"])
_uk("intl-tax-aeoi", "uksi/2015/878", "UK ITC Regs (CRS)", "International Tax Compliance Regulations 2015 (CRS/FATCA; CARF/CRS 2.0 amendments)", "regulation", "supplements", ["tax-reporting", "uk"], ["TAX-002", "TAX-004"])
_src("intl-tax-aeoi", "irs/qi-agreement", "QI Agreement", "IRS Qualified Intermediary agreement (Rev. Proc. 2022-43) + Forms 1042/1042-S", "agreement", "US", "IRS", "https://www.irs.gov/businesses/international-businesses/qualified-intermediary-system", "irs_gov", "supplements", "binding", ["withholding", "us"], ["TAX-010"], 3)
_src("intl-tax-aeoi", "cfr/26/871m", "§871(m) regs", "26 CFR §1.871-15 — dividend-equivalent withholding on derivatives", "regulation", "US", "Treasury/IRS (eCFR)", "https://www.ecfr.gov/current/title-26/chapter-I/subchapter-A/part-1", "govinfo_us", "supplements", "binding", ["withholding", "cfd", "us"], ["TAX-011"], 2)
# uksi/1986/1711 has no digital text at legislation.gov.uk (data.xml -> 504,
# image-only era) — reference-level until the P3 Docling/PDF importer lands.
_src("intl-tax-aeoi", "uksi/1986/1711", "UK SDRT", "Stamp Duty Reserve Tax Regulations 1986 + FA 1986 Part IV", "regulation", "UK", UK_ISSUER,
     UKLEG + "uksi/1986/1711", "uk_legislation", "supplements", "binding", ["transaction-tax", "uk"], ["TAX-020"], 3)

# ---- F19 standards ----
_src("standards", "fatf/40-recommendations", "FATF 40", "FATF Recommendations incl. VASP guidance and travel rule", "standard", "INTL", "FATF", "https://www.fatf-gafi.org/en/publications/Fatfrecommendations/Fatf-recommendations.html", "fatf", "root", "guidance", ["aml", "standards"], ["GRP-026", "STD-008"], 3)
_src("standards", "wolfsberg/standards", "Wolfsberg", "Wolfsberg Group standards (CBDDQ, payment transparency, monitoring)", "standard", "INTL", "Wolfsberg Group", "https://db.wolfsberg-group.org/", "wolfsberg", "supplements", "guidance", ["aml", "standards"], ["GRP-027"], 3)
_src("standards", "iso/27001-2022", "ISO 27001", "ISO/IEC 27001:2022 + Amd 1:2024 [RESTRICTED — P3 importer + BYOL]", "standard", "INTL", "ISO/IEC", "https://www.iso.org/standard/27001", "restricted_file", "supplements", "guidance", ["infosec", "standards"], ["STD-001"], 4, license="restricted")
_src("standards", "aicpa/soc2-tsc", "SOC 2 TSC", "AICPA Trust Services Criteria 2017 (2022 points of focus) [RESTRICTED]", "standard", "US", "AICPA", "https://www.aicpa-cima.com/", "restricted_file", "supplements", "guidance", ["infosec", "assurance", "standards"], ["STD-002"], 4, license="restricted")
_src("standards", "pci/dss-v4", "PCI DSS v4", "PCI DSS v4.x [RESTRICTED — license check]", "standard", "INTL", "PCI SSC", "https://www.pcisecuritystandards.org/", "restricted_file", "supplements", "guidance", ["payments", "infosec"], ["STD-004"], 4, license="restricted")
_src("standards", "ifrs/standards", "IFRS", "IFRS as issued by the IASB [RESTRICTED — IFRS Foundation license]", "standard", "INTL", "IFRS Foundation", "https://www.ifrs.org/", "restricted_file", "supplements", "guidance", ["financial-reporting"], ["GRP-009"], 4, license="restricted")

# ---- F20 host-state-overlays ----
for _cc, _name, _ids in [
    ("be", "Belgium — FSMA OTC-CFD/leveraged retail distribution ban (2016)", ["OVL-BE"]),
    ("fr", "France — Sapin II advertising ban + Loi Influenceurs 2023-451 + AMF doctrine", ["OVL-FR"]),
    ("es", "Spain — CNMV 2023 CFD marketing resolution + crypto-ad Circular 1/2022", ["OVL-ES"]),
    ("de", "Germany — BaFin CFD general administrative act + marketing rules", ["OVL-DE"]),
    ("it", "Italy — Consob measures + FTT interaction", ["OVL-IT"]),
]:
    _src("host-state-overlays", f"ovl/{_cc}", f"Overlay {_cc.upper()}", _name, "guidance", _cc.upper(), "National regulator", "https://www.esma.europa.eu/", "overlay", "supplements" if _cc != "be" else "root", "binding", ["product-intervention", "marketing", _cc], _ids, 3)


def _about(entry: dict) -> str:
    return f"eToro blueprint (registry {', '.join(entry['registry_ids'])}; import wave {entry['wave']})."


def source_meta(entry: dict) -> SourceMeta:
    """Build the adapter SourceMeta for a registry entry (Wave-1 ingestion)."""
    license_ref = "OGL-UK-3.0" if entry["adapter"] == "uk_legislation" else "Commission Decision 2011/833/EU"
    return SourceMeta(
        family_key=entry["family"],
        family_name=FAMILY_NAMES[entry["family"]],
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
        version_policy="as_published" if entry["adapter"] == "eur_lex" else "consolidated",
    )


def wave1_entries() -> list[dict]:
    """Registry entries ingestable today (class A: existing-adapter reuse)."""
    return [e for e in S if e.get("fetch")]


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
    """Idempotent seed of blueprint families + reference-level sources."""
    created_f = created_s = skipped = 0
    with engine.begin() as conn:
        family_ids: dict[str, int] = {}
        for key, name, charter in FAMILIES:
            existing = conn.execute(sa.select(source_families.c.id).where(source_families.c.key == key)).scalar()
            if existing:
                family_ids[key] = existing
                continue
            family_ids[key] = conn.execute(
                source_families.insert()
                .values(key=key, name=name, scope_charter={"registry": charter, "partner": "etoro"})
                .returning(source_families.c.id)
            ).scalar_one()
            created_f += 1
        for s in S:
            existing_id = conn.execute(sa.select(sources.c.id).where(sources.c.key == s["key"])).scalar()
            if existing_id:
                # Source already known (e.g. citator-discovered): backfill curated
                # context if missing and attach it to the blueprint family too.
                row = conn.execute(
                    sa.select(sources.c.short_name, sources.c.about).where(sources.c.id == existing_id)
                ).one()
                if not row.short_name:
                    conn.execute(
                        sources.update()
                        .where(sources.c.id == existing_id)
                        .values(short_name=s["short_name"], about=_about(s), topics=s["topics"])
                    )
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
    return {"families_created": created_f, "sources_created": created_s, "skipped_existing": skipped}


if __name__ == "__main__":
    from app.clhear.db import get_engine, run_migrations

    engine = get_engine()
    run_migrations(engine)
    print(json.dumps(seed(engine)))
