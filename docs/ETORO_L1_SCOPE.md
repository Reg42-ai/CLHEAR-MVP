# eToro L1 Import Scope — registry → CLHEAR mapping

**Input:** [etoro-clhear-source-registry.md](etoro-clhear-source-registry.md) (draft v0.1, 24 Aug 2026, ~200 source IDs across 15 entities).
**Output:** this mapping — every registry ID assigned to a CLHEAR family, an adapter (existing or planned), a feasibility class, and an import wave. The machine-readable version is seeded into the corpus by `scripts/seed_registry.py` (reference-level sources, `added_via='watchlist'`), so the Explorer library shows the full blueprint with ingested-vs-planned status.

## Feasibility classes (drives the waves)

| Class | Meaning | Adapter |
|---|---|---|
| **A** | Publisher already covered — parameter reuse only | `eur_lex` (any CELEX id), `uk_legislation` (any legislation.gov.uk doc), `govinfo_us`/eCFR (US titles), `nist` |
| **B** | New adapter, structured HTML/XML publisher | FCA Handbook, FINRA rulebook, ADGM rulebooks, legislation.gov.au, eCFR generalization (17/31 CFR), Nasdaq rules |
| **C** | PDF-first publisher — needs Docling (P3) | CySEC circulars, ASIC RGs, MAS notices, FATF, Wolfsberg, IOSCO |
| **D** | Restricted / licensed text — P3 `restricted_file` importer + BYOL | ISO 27001/27701, SOC 2 TSC, PCI DSS, COSO, IIA, IFRS |
| **E** | Structured LIST feeds, not documents — separate lists pipeline | OFAC SDN, EU consolidated, UN, OFSI, DFAT, NBCTF, EOCN |
| **W** | Continuous guidance libraries — P3 watcher fleet | §19 GDL (ESMA/EBA Q&As, Dear-CEO letters, FINRA notices, …) |

**Starter-corpus delta (honest note):** the registry's `(★)` assumes a starter of *MiFIR UK, GDPR, SOC 2, ISO 27001, FATCA*. The corpus actually live today is **GDPR (EU-030/GRP-030 ✓), UK MLRs 2017 (UK-030 ✓), FATCA statute + 26 CFR (US-030s/TAX-001 partially ✓), NIST 800-53 + CSF (STD-003 ✓)**. MiFIR UK is Wave 1 below; SOC 2 / ISO 27001 are class D (restricted) and land with the P3 importer.

## Family layout and full ID mapping

### F1 `eu-mifid` — EU MiFID II framework (Wave 1, class A — all CELEX)
MiFID II Dir 2014/65 as amended (EU-001) · MiFIR 600/2014 as amended (EU-002) · Del Reg 2017/565 (EU-004) · RTS 1 2017/587 (EU-005) · RTS 2 2017/583 (EU-006) · RTS 22 2017/590 (EU-007) · RTS 23 2017/585 (EU-008) · RTS 25 2017/574 (EU-009) · RTS 6/7 2017/589+584 (EU-010 `[verify]`) · Del Dir 2017/593 (EU-011, also EU-054) · Cyprus transposition L.87(I)/2017 (EU-003) → **class C** (CySEC PDF), Wave 3.

### F2 `eu-markets` — EU market integrity & post-trade (Wave 1, class A)
MAR 596/2014 + CSMAD 2014/57 (EU-014) · SSR 236/2012 (EU-015) · EMIR 648/2012 + Refit + EMIR 3 2024/2987 (EU-016) · SFTR 2015/2365 (EU-017 `[verify lending]`) · CSDR 909/2014 (EU-018) · Benchmarks 2016/1011 (EU-019) · PRIIPs 1286/2014 + RTS 2017/653 (EU-012).

### F3 `eu-mica` — EU crypto (Wave 1, class A + Wave 3 guidance)
MiCA 2023/1114 (EU-030) · TFR recast 2023/1113 (EU-032) · MiCA L2/L3 RTS/ITS + ESMA guidelines (EU-031) → RTS class A, guidelines class W · staking positions (EU-034 `[verify]`, W) · Tangany/BaFin awareness (EU-033, reference-only).

### F4 `eu-aml` — EU financial crime (Wave 1, class A)
AMLD 2015/849 as amended (EU-040) · **AML package 2024**: AMLR 2024/1624, AMLD6 2024/1640, AMLA 2024/1620 (EU-041; radar 2027) · Whistleblower Dir 2019/1937 (EU-042) · Cyprus AML law + CySEC AML directive (EU-040 national) → class C, Wave 3.

### F5 `eu-prudential` — EU prudential & resilience (Wave 1, class A)
IFR 2019/2033 + IFD 2019/2034 (EU-050) · **DORA 2022/2554** + Dir 2022/2556 (EU-051; RTS/ITS class A as published) · EBA/ESMA legacy guidelines (EU-052, W) · Cyprus ICF + safeguarding circulars (EU-053/054 national parts → C).

### F6 `eu-consumer` — EU consumer/marketing/platform (Wave 1, class A)
Distance Marketing recast 2023/2673 (EU-060, live) · UCPD 2005/29 + CRD 2011/83 (EU-061) · DSA 2022/2065 (EU-062) · SFDR 2019/2088 + Taxonomy Arts 5-7 (EU-063) · ESMA marketing/finfluencer statements (EU-064, W) · CSRD 2022/2464 + Omnibus (EU-065 `[verify]`) · ePrivacy 2002/58 (GRP-038).

### F7 `eu-data` — EU/EEA data & AI (Wave 1, class A)
GDPR (**✓ ingested**, GRP-030) · **AI Act 2024/1689** (GRP-036; tranche 2 live Aug 2026) · Accessibility Act 2019/882 (GRP-037) · EU sanctions regs 269/2014 + 833/2014 (GRP-022 — the REGULATIONS class A; the consolidated LIST class E).

### F8 `uk-fca` — UK conduct & prudential (Wave 1 statutes class A; Wave 2 Handbook class B)
FSMA 2000 + FSMA 2023 + RAO 2001 (UK-001, A) · **FCA Handbook** PRIN/SYSC/COBS/CASS/PROD/SUP/DISP/COMP/TC/FIT/COCON/MIFIDPRU/SYSC15A (UK-002/008/009/010/011, **B — flagship Wave-2 adapter**) · UK MiFIR + UK RTS 22/23 (UK-003, A via legislation.gov.uk onshored EUR) · UK EMIR (UK-004, A) · UK MAR (UK-005, A) · UK SSR (UK-006, A) · UK PRIIPs→CCI (UK-007, radar) · FOS/FSCS (UK-012, SC5 reference) · FPO 2005 + s21 (UK-020, A) · crypto promotions (UK-021, A SI + B PS23/6) · FG24/1 finfluencer (UK-022, W) · MLRs cryptoasset registration + travel rule (UK-023 — **MLRs ✓ ingested**) · incoming crypto RAO regime (UK-024, radar).

### F9 `uk-fincrime` — UK financial crime (Wave 1, class A — all legislation.gov.uk)
MLRs 2017 (**✓ ingested**, UK-030) · POCA 2002 (UK-031) · Terrorism Act 2000 (UK-031) · Criminal Finances Act 2017 (UK-032) · **ECCTA 2023** (UK-033, in force) · Bribery Act 2010 (UK-034/GRP-008) · SAMLA 2018 (GRP-023; OFSI list = E).

### F10 `uk-data-products` — UK data, e-money, wrappers (Wave 1, class A)
UK GDPR + DPA 2018 + PECR (GRP-031) · ISA Regulations 1998 (UK-040 `[verify manager]`) · EMRs 2011 + PSRs 2017 (UK-050) · FCA safeguarding reform (UK-051, radar/B) · APP-fraud regime (UK-052 `[verify]`).

### F11 `us-broker-dealer` — US BD/SEC/FINRA (Wave 2, class B via eCFR generalization + FINRA adapter)
Exchange Act BD rules 17 CFR 240.15c3-1/15c3-3/17a-3/4/5 (US-001) · Reg BI + Form CRS (US-002 `[verify]`) · Rule 606/10b-10/Reg NMS (US-003) · **Reg S-P as amended** (US-004, live) · Reg S-ID + GLBA (US-005) · **FINRA rulebook** 3110/3120/3130/2210/2090/2111/3310/4511/4530/1210 (US-006, B) · CAT (US-007, SC5) · TRF/ORF (US-008 `[verify]`) · SIPC/Sec 31/TAF (US-009) · blue-sky (US-010, reference) · Securities Act 1933 + Exchange Act FPI regime + SOX + 10b-5 + 13(d) + Dodd-Frank WB (GRP-001/002/003/005/006/007 — statutes class A via govinfo USC; SEC forms/rules class B) · Nasdaq 5600 (GRP-004, B/D) · FCPA (GRP-008, A).

### F12 `us-crypto-msb` — US MSB/state crypto (Wave 2, class B — 31 CFR via eCFR + NYDFS HTML)
BSA + 31 CFR Ch. X (US-020) · state MTLs (US-021, reference until per-state need) · NYDFS Part 200 + **Part 500 as amended** (US-022) · OFAC VC expectations (US-023, W) · GENIUS Act + market-structure (US-024, radar) · negative perimeter guards (US-025 `[verify]`, reference memo) · IRC §6045 + **1099-DA** + §3406 (US-030/031/032 — class A, title 26 already wired).

### F13 `au-afsl` — Australia (Wave 2, class B — legislation.gov.au; RGs class C)
Corporations Act Ch 7 (AU-001) · DDO Pt 7.8A + RG 274 (AU-002) · ASIC CFD PIO + RG 227 (AU-003) · client money rules (AU-004) · **ASIC Derivative Transaction Rules 2024** (AU-005) · RG 78/271 + AFCA (AU-006, C/SC5) · AML/CTF Act + **2024 reform live Mar 2026** (AU-007) · E8 RE stack Ch 5C + RG 132/133 (AU-008) · ATO regimes (AU-009) · Spam Act + INFO 269 (AU-010) · Privacy Act 1988 + amendments (GRP-034).

### F14 `me-adgm` — ADGM/UAE (Wave 2, class B — ADGM rulebook HTML)
FSMR 2015 + FSRA GEN/COBS/PRU/AML (ME-001) · FSRA VA framework + AVA list (ME-002) · UAE AML Decree-Law 20/2018 (ME-003) · ADGM DPR 2021 (ME-004/GRP-035) · FSRA reporting (ME-005, W).

### F15 `sg-mas` — Singapore (Wave 3, class B/C — SSO HTML + MAS PDFs)
SFA 2001 + LCB Regs (SG-001) · MAS AML notice SFA04-N02 (SG-002, C) · MAS conduct/TRM/outsourcing/cyber notices (SG-003, C) · leveraged-FX conditions (SG-004) · PDPA (SG-005/GRP-035) · DPT boundary memo (SG-006 `[verify]`, reference) · IRAS CRS/FATCA (SG-007).

### F16 `small-entities` — Seychelles, Malta, Gibraltar, Israel (Wave 3, class B/C mixed)
SC-001..005 (Seychelles Securities Act + AML + BO + DPA + FSA circulars) · MT-001..005 (Cap 376, safeguarding, PMLFTR + FIAU, MFSA, PSD3 radar) · GI-001..003 (**gated on `[verify eToroX status]`** — do not build until §20.7 resolves) · IL-001..004 (privacy Amendment 13, NBCTF, cross-border memo) · BVI BC Act + BOSS (GRP-010 `[verify domicile]`).

### F17 `sanctions-lists` — global screening lists (class E — separate LISTS pipeline, Wave 3)
UN SC resolutions (GRP-020) · OFAC SDN/SSI/50% (GRP-021) · EU consolidated list (GRP-022) · UK OFSI (GRP-023) · Israel NBCTF (GRP-024/IL-003) · AU DFAT, UAE EOCN, MAS, SC, GI locals (GRP-025). *Design note: these are versioned structured datasets, not clause documents — model as a new `grain` in the datalake with list-diff change events; the L1 pipeline concept (fetch→gate→diff→event) transfers directly, the DocNode tree does not.*

### F18 `intl-tax-aeoi` — tax reporting & transaction taxes (Wave 1 partial A; Wave 2/3 rest)
FATCA (**✓ statute+regs ingested**; IGAs per jurisdiction = Wave 3 references, TAX-001) · CRS/DAC2 + **CRS 2.0** (TAX-002 — DAC amendments class A via CELEX; local implementations Wave 3) · **DAC8** 2023/2226 (TAX-003, A, in force) · **UK CARF** (TAX-004, A/B) · CARF tracker (TAX-005, radar) · QI regime Rev. Proc. 2022-43 (TAX-010 `[verify]`, C — IRS PDF) · §871(m) (TAX-011, A — title 26) · 1099 suite (TAX-012 = US-030..032) · FTT/stamp layer: UK SDRT (A), IE/FR/ES/IT FTT + BE TOB (Wave 3, national C) + HK stamp (reference) (TAX-020..027) · localized outputs + cost-basis dependency (TAX-030/031, reference memos).

### F19 `standards` — frameworks (existing family extends; Wave 4 restricted)
NIST 800-53 + CSF (**✓ ingested**, STD-003) · FATF 40 Recs (STD-008/GRP-026, C — open PDF) · Wolfsberg (GRP-027, C) · IOSCO (STD-009, C) · ISO 27001/27701 (STD-001, **D**) · SOC 2 TSC (STD-002, **D**) · PCI DSS (STD-004, **D — license check**) · COSO (STD-005, D) · IIA (STD-006, D) · ISAE 3000 (STD-007, D) · IFRS (GRP-009, **D — IFRS Foundation license**) · LEI/ISO 20022 (STD-010, reference) · SWIFT CSP (STD-011 `[verify]`, D) · OECD Pillar Two + CbCR (GRP-040/041, C — OECD PDFs) · UK tax strategy duty (GRP-042, A).

### F20 `host-state-overlays` — §14 OVL files (Wave 3, class C/W per country)
BE FSMA ban, FR Sapin II + Loi Influenceurs, ES CNMV, DE BaFin acts, IT Consob, NL/PL/NO/PT/RO variations, ESMA third-country opinions. One overlay source per marketing-active country; national-regulator PDFs → Docling + watchers.

## Wave summary

| Wave | Content | New engineering | Sources |
|---|---|---|---|
| **1 — now** | All class-A: ~35 EU CELEX instruments (F1–F7) + ~15 UK statutes/SIs (F8–F10) + DAC8/UK tax + §871(m)/1099 (title 26 wired) | Zero new adapters — parameter reuse + fixtures + fidelity gates per doc | ~50 |
| **2** | FCA Handbook adapter, FINRA rulebook adapter, eCFR generalization (17/31 CFR), ADGM rulebook, legislation.gov.au, NYDFS, Nasdaq 5600 | 5–6 new class-B adapters | ~25 |
| **3** | Docling wave (CySEC, ASIC RGs, MAS, FATF, Wolfsberg, QI, FTTs, overlays), sanctions LISTS pipeline, guidance watchers (§19) | P3 machinery (Docling, watchers, lists grain) | ~40 + lists |
| **4** | Restricted layer: ISO, SOC 2 TSC, PCI, COSO, IIA, IFRS + BYOL | P3 `restricted_file` importer (already chartered) | ~8 |

## Blockers requiring eToro input before freeze (from §20)
SFTR lending scope (EU-017) · QI entity map (TAX-010) · clearing/CAT split vs Apex (US-001/007/008) · Reg BI copy-trading scope (US-002) · ISA manager of record (UK-040) · staking analysis (EU-034) · **eToroX Gibraltar status — gates all of F16-GI** · SG DPT boundary (SG-006) · EGC/404(b) timing (GRP-003) · Belgian TOB registration (TAX-025).

## Coverage eval (§21 → CLHEAR)
The registry's own reconciliation recipe becomes an eval suite when the corpus matures: `l1_completeness` — (a) every E1–E15 register permission maps to ≥1 ingested/reference source; (b) every 20-F-cited regime exists in L1; (c) every §15 department domain resolves to green sources; (d) every marketing country has an OVL source. Same pattern as `l1_fidelity`: an eval that gates, not reports.