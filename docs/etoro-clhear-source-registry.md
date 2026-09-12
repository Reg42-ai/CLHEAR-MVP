# CLHEAR L1 Source Registry — eToro Group

**Purpose:** Complete inventory of binding and quasi-binding sources (laws, regulations, technical standards, regulator guidance, industry standards, tax-reporting regimes) applicable to the eToro group, for ingestion into CLHEAR L1 (verbatim source datalake) and provenance-linking in L2.
**Status:** Draft v0.1 — compiled 24 Aug 2026. Verified against eToro's published regulation & licence page (updated Aug 2026) and SEC filings. Items needing internal confirmation are tagged `[verify]` and collected in §19.

## §1. How to read this registry

**Source classes** (drives crawler priority and change-inference behaviour in L2):
- **SC1** — Primary law / regulation (binding statute, EU regulation/directive, act)
- **SC2** — Delegated / technical standards (RTS, ITS, delegated regulations, SEC/FINRA rules, ASIC rules/instruments, statutory instruments)
- **SC3** — Regulator guidance with comply-or-explain force (ESMA/EBA guidelines, CySEC circulars, FCA guidance, FINRA notices, ASIC RGs, MAS notices/guidelines)
- **SC4** — Industry standards & frameworks (ISO, SOC 2, FATF, Wolfsberg, COSO, NIST, PCI)
- **SC5** — SRO / scheme / contractual-with-regulatory-force (Nasdaq listing rules, SIPC, FSCS/ICF/AFCA scheme rules, QI agreement, exchange rulebooks)

**Cluster tags** map each source to CLHEAR clusters: `[A]` Financial Crime · `[B]` Markets & Trading · `[C]` Reporting · `[D]` Clients & Products · `[E]` Prudential & Resilience · `[F]` Enterprise.

`(★)` = already in the agreed L1 starter corpus (MiFIR UK, GDPR, SOC 2, ISO 27001, FATCA).

**Scope decision (explicit):** employment law, general corporate housekeeping, and commercial contracts are OUT of scope except where they land in cluster F (governance, privacy, tech, listed-company). Client-side tax duties are out; broker-side collection/reporting duties are IN (§16).

## §2. Entity → licence spine (applicability anchor)

| # | Legal entity | Regulator / register | Licence & perimeter |
|---|---|---|---|
| E1 | eToro Group Ltd | SEC (Nasdaq: ETOR); BVI incorporation `[verify domicile detail]` | Foreign Private Issuer, files 20-F/6-K; listed May 2025 |
| E2 | eToro (Europe) Ltd | CySEC | CIF licence 109/10 (MiFID II investment services incl. portfolio mgmt for CopyTrader); **MiCA CASP licence (Jan 2025)** — crypto trading + custody across EEA; German crypto custody sub-delegated to Tangany GmbH |
| E3 | eToro (UK) Ltd | FCA | FRN 583263 (investment firm); FCA cryptoasset registration under MLRs 2017 |
| E4 | eToro Money UK Ltd | FCA | FRN 900203 — e-money issuance + payment services |
| E5 | eToro Money Malta Ltd | MFSA | E-money institution + payment services |
| E6 | eToro (ME) Limited | ADGM FSRA | FSP 220073 — dealing as principal (matched), arranging, custody, arranging custody, managing assets; accepted virtual assets (AVA) list published |
| E7 | eToro AUS Capital Ltd | ASIC | AFSL 491139 |
| E8 | eToro Asset Management Ltd (AU) | ASIC | Responsible Entity (Ch 5C), registered scheme "eToro Service" ARSN 637 489 466 |
| E9 | eToro (Seychelles) Ltd | FSA Seychelles | SD076 — securities dealer (agent or principal), Securities Act 2007 |
| E10 | eToro Singapore Pte Ltd | MAS | CMS101824 — dealing in securities, CIS units, OTC derivatives, leveraged FX spot, custody |
| E11 | eToro USA LLC | FinCEN + states | MSB registration, NMLS 1769299, state MTLs (crypto platform) |
| E12 | eToro NY LLC | NYDFS | Money transmitter + BitLicense (virtual currency) |
| E13 | eToro USA Securities Inc | SEC / FINRA | BD 8-70212, CRD 298361, SIPC member; fully-disclosed introducing to Apex Clearing `[verify clearing model]` |
| E14 | eToro X Ltd (Gibraltar) | GFSC | DLT provider licence 1333B (wallet) — `[verify: live vs winding down]` |
| E15 | eToro Ltd / group companies, Israel | — | No ISA retail licence (Trading Arena application withdrawn 2016); Israel = HQ/enterprise jurisdiction only |

## §3. S0 — Group-wide sources (all entities / eToro Group Ltd)

### Listed-company layer (Nasdaq FPI) — cluster [F] unless noted
- **GRP-001** — US Securities Act 1933 (SC1) — [F] — registration statements, prospectus liability (F-1 done; shelf/secondaries)
- **GRP-002** — US Securities Exchange Act 1934 — FPI reporting regime (SC1) — [F][C] — Form 20-F annual (filed Mar 2026 for FY2025), 6-K current reports, ICFR
- **GRP-003** — Sarbanes-Oxley Act 2002 (SC1) — [F] — §302/§906 certifications, §404(a) mgmt assessment; §404(b) auditor attestation once EGC status lost — **with FY2025 total revenue >$1.235bn the EGC exemption almost certainly falls away → confirm 404(b) timing with finance/audit** `[verify]`
- **GRP-004** — Nasdaq Listing Rules incl. 5600 governance series (SC5) — [F] — FPI home-country-practice elections must be disclosed in 20-F
- **GRP-005** — SEC Rule 10b-5 / insider-trading law + Rule 10b5-1 (SC1/SC2) — [F][B] — group insider trading policy (filed as 20-F Exhibit 11.1), blackout windows, trading plans
- **GRP-006** — Exchange Act §13(d)/(g) beneficial ownership; Reg FD as market practice (FPIs formally exempt) (SC1) — [F]
- **GRP-007** — Dodd-Frank whistleblower provisions (SC1) — [F] — SEC WB programme interacts with internal speak-up channels
- **GRP-008** — US FCPA + UK Bribery Act 2010 (via E3) (SC1) — [A][F] — anti-corruption programme
- **GRP-009** — IFRS as issued by IASB (SC4) — [C][F] — FPI financial reporting basis
- **GRP-010** — BVI Business Companies Act + BOSS Act / economic substance rules (SC1) — [F] — parent-company housekeeping `[verify domicile]`

### Financial crime & sanctions layer — cluster [A]
- **GRP-020** — UN Security Council sanctions resolutions (SC1) — [A] — baseline for all list screening
- **GRP-021** — US OFAC programmes (SDN, SSI, 50% rule) (SC1/SC2) — [A] — USD nexus + US persons reach the whole group
- **GRP-022** — EU restrictive measures (consolidated list, Reg 269/2014, 833/2014 et al.) (SC1) — [A]
- **GRP-023** — UK sanctions — SAMLA 2018 + OFSI regimes and reporting duties (SC1) — [A]
- **GRP-024** — Israeli sanctions/terror designations — NBCTF lists, Trading with the Enemy Ordinance, Israeli CFT law (SC1) — [A] — HQ-jurisdiction screening source
- **GRP-025** — Local sanction regimes per operating entity: AU DFAT, UAE Executive Office/local lists, MAS lists, Seychelles, Gibraltar (SC1) — [A]
- **GRP-026** — FATF 40 Recommendations + guidance (VASPs, travel rule) (SC4) — [A] — anchoring standard behind every AML law below
- **GRP-027** — Wolfsberg Group standards (CBDDQ, payment transparency, monitoring) (SC4) — [A]

### Privacy / data / technology layer — cluster [F]
- **GRP-030** — EU GDPR 2016/679 (★) (SC1) — [F][D] — E2/E5 controllers + group processors; cross-border transfer rules (SCCs, adequacy)
- **GRP-031** — UK GDPR + Data Protection Act 2018 + PECR (SC1) — [F][D]
- **GRP-032** — Israeli Privacy Protection Law 5741-1981 incl. **Amendment 13 (in force Aug 2025)** — enforcement powers, DPO duty, breach rules (SC1) — [F] — group HQ data processing
- **GRP-033** — US state privacy laws — CCPA/CPRA + successors (SC1) — [F][D] — US client base
- **GRP-034** — Australian Privacy Act 1988 + 2024 amendment wave (SC1) — [F][D]
- **GRP-035** — ADGM Data Protection Regulations 2021; Singapore PDPA 2012; Seychelles DPA 2023; Gibraltar DPA (SC1) — [F]
- **GRP-036** — EU AI Act 2024/1689 (SC1) — [F][D] — phased: prohibitions+literacy Feb 2025, GPAI Aug 2025, **high-risk & transparency tranche 2 Aug 2026 (now)** — assess Tori assistant, "Agentic Portfolio", copy-recommendation engines against Annex III / transparency duties
- **GRP-037** — EU Accessibility Act 2019/882 (applies since 28 Jun 2025) (SC1) — [D][F] — consumer financial services websites/apps in scope
- **GRP-038** — ePrivacy Directive 2002/58 + national cookie/marketing rules (SC1) — [D][F]

### Group tax (finance-owned; register for completeness) — cluster [F][C]
- **GRP-040** — OECD Pillar Two GloBE rules + local IIR/QDMTT implementations (SC1) — group revenue far above €750m threshold — [F]
- **GRP-041** — OECD CbCR + transfer-pricing documentation regimes (SC1/SC3) — [F][C]
- **GRP-042** — UK Finance Act 2016 Sch 19 — published UK tax strategy duty (already published) (SC1) — [F]

## §4. S1 — eToro (Europe) Ltd — CySEC CIF 109/10 + MiCA CASP

### Core conduct & licensing
- **EU-001** — MiFID II Directive 2014/65/EU as amended by Dir (EU) 2024/790 (SC1) — [B][D][F] — authorisation, org requirements, conduct, product governance, inducements, best execution (Art 27), taping (Art 16(7)), record-keeping (Art 16(6))
- **EU-002** — MiFIR Reg 600/2014 as amended by Reg (EU) 2024/791 (SC1) — [B][C] — transparency, **SI regime Arts 14/15/17a (live TRADEcho project)**, transaction reporting Art 26, reference data Art 27
- **EU-003** — Cyprus Investment Services Law L.87(I)/2017 (SC1) — [B][D][F] — national transposition + CySEC powers
- **EU-004** — Commission Delegated Reg (EU) 2017/565 (SC2) — [B][D][F] — organisational requirements, suitability/appropriateness, records; SI definitional anchors
- **EU-005** — RTS 1 (Del Reg 2017/587 as amended) — equity transparency/SI quoting (SC2) — [B]
- **EU-006** — RTS 2 (2017/583 as amended) — non-equity transparency (SC2) — [B]
- **EU-007** — RTS 22 (2017/590) — transaction reporting fields (SC2) — [C] — rewrite in flight under MiFIR review (see §17)
- **EU-008** — RTS 23 (2017/585) — instrument reference data / FIRDS (SC2) — [C] — daily submissions under ETOR MIC post SI go-live
- **EU-009** — RTS 25 (2017/574) — clock synchronisation (SC2) — [B][C]
- **EU-010** — RTS 6/7 (2017/589, 2017/584) — algo-trading controls if in perimeter (SC2) — [B] — `[verify: internalisation engine's algo classification]`
- **EU-011** — MiFID product-governance delegated directive 2017/593 → Cyprus transposition (SC2) — [D] — target market, distribution strategy
- **EU-012** — PRIIPs Reg 1286/2014 + KID RTS 2017/653 (SC1/SC2) — [D] — KIDs for CFDs and Smart/Alpha Portfolios
- **EU-013** — CySEC product-intervention national measures (permanent CFD leverage/margin/negative-balance/marketing rules, ex-ESMA 2018) (SC3) — [D]
- **EU-014** — Market Abuse Reg 596/2014 + CSMAD 2014/57 + STOR ITS (SC1/SC2) — [B] — surveillance, STORs, insider lists (core Tracepoint domain)
- **EU-015** — Short Selling Reg 236/2012 (SC1) — [B][C] — net-short notification for principal book if thresholds crossed
- **EU-016** — EMIR 648/2012 + Refit 2019/834 + **EMIR 3 Reg (EU) 2024/2987** (SC1) — [C][B] — CFD/derivative reporting to TRs, reconciliation, margin/threshold monitoring
- **EU-017** — SFTR 2015/2365 (SC1) — [C] — **applies only if EU entity runs securities lending of client stock** `[verify current lending programme scope per entity]`
- **EU-018** — CSDR 909/2014 settlement discipline regime (SC1) — [B][C] — cash penalties handling as trading party
- **EU-019** — Benchmarks Reg 2016/1011 (SC1) — [B] — use of third-party benchmarks in products
- **EU-020** — Securities Financing / title transfer restrictions for retail (MiFID Art 16(10)) (SC1) — [D]

### Crypto (MiCA stack)
- **EU-030** — MiCA Reg (EU) 2023/1114 (SC1) — [B][D][A][C] — CASP authorisation, custody, conflicts, complaints, market-abuse-in-crypto Title VI, white-paper duties, marketing communications
- **EU-031** — MiCA Level-2/3 package (RTS/ITS on complaints, conflicts, custody, incident reporting; ESMA guidelines incl. reverse solicitation, suitability of staff) (SC2/SC3) — [B][D]
- **EU-032** — Transfer of Funds Reg (recast) 2023/1113 (SC1) — [A] — crypto travel rule (applied Dec 2024)
- **EU-033** — Tangany GmbH sub-custody arrangement for DE clients — BaFin crypto-custody perimeter awareness (SC1/SC5) — [D][E] — outsourcing oversight source
- **EU-034** — Staking service treatment under MiCA + national positions (SC3) — [D] — `[verify: which entity offers staking to EEA clients and under what analysis]`

### Financial crime
- **EU-040** — AMLD framework: Dir 2015/849 as amended (AMLD5 2018/843) + Cyprus AML Law L.188(I)/2007 + CySEC AML Directive (SC1/SC3) — [A]
- **EU-041** — **EU AML package 2024**: AMLR Reg (EU) 2024/1624 (single rulebook, applies 10 Jul 2027), AMLD6 2024/1640, AMLA Reg 2024/1620 (SC1) — [A] — major L2 change-inference workload 2026-27
- **EU-042** — EU Whistleblower Directive 2019/1937 + Cyprus L.6(I)/2022 (SC1) — [F]

### Prudential & resilience
- **EU-050** — IFR Reg 2019/2033 + IFD Dir 2019/2034 + Cyprus L.165(I)/2021 (SC1) — [E] — K-factors, ICARA, remuneration, disclosures (Pillar 3 published)
- **EU-051** — **DORA Reg 2022/2554** + Dir 2022/2556 + RTS/ITS suite (ICT risk, incident classification/reporting, register of information, TLPT) (SC1/SC2) — [E][F] — applied Jan 2025; annual RoI submissions
- **EU-052** — EBA/ESMA outsourcing + ICT guidelines predating DORA where still cited by CySEC (SC3) — [E]
- **EU-053** — Investor Compensation Fund (Cyprus ICF) directive — contributions + client disclosures (SC3/SC5) — [D][E]
- **EU-054** — CySEC safeguarding of client funds/assets rules (MiFID Art 16(8)-(9) + DR 2017/593 + circulars) (SC2/SC3) — [D][E]

### Consumer / marketing / platform
- **EU-060** — Distance Marketing of Financial Services: Dir 2002/65/EC → **replaced by Dir (EU) 2023/2673, applies from 19 Jun 2026 (now live)** (SC1) — [D]
- **EU-061** — Unfair Commercial Practices Dir 2005/29 + Consumer Rights Dir 2011/83 (SC1) — [D]
- **EU-062** — **Digital Services Act Reg 2022/2065** (SC1) — [D][F] — eToro Europe already publishes DSA information → social feed treated as intermediary/online platform: notice-and-action, transparency reporting, average-MAU publication
- **EU-063** — SFDR Reg 2019/2088 (+ Taxonomy Reg 2020/852 Arts 5-7) (SC1) — [D][C] — applies to portfolio-management activity (CopyTrader/Smart Portfolios) — entity + product disclosures; ESG feature claims
- **EU-064** — ESMA marketing-communications and social-media expectations; finfluencer supervisory statements (SC3) — [D] — feeds affiliate-marketing monitoring domain
- **EU-065** — CSRD 2022/2464 as amended by 2025 Omnibus "stop-the-clock" (SC1) — [F][C] — assess Cyprus subsidiary + non-EU parent thresholds; timing pushed to ~FY2027+ `[verify with finance]`

### Cyprus misc
- **EU-070** — Cyprus GDPR law L.125(I)/2018; Cyprus UBO register; CySEC governance & fitness circulars (SC1/SC3) — [F]

## §5. S2 — eToro (UK) Ltd (FCA 583263) + eToro Money UK Ltd (FRN 900203)

### Core conduct & licensing
- **UK-001** — FSMA 2000 + FSMA 2023 + RAO 2001 (SC1) — [B][D][F]
- **UK-002** — FCA Handbook: PRIN (incl. **PRIN 2A Consumer Duty**), SYSC, COBS, CASS, PROD, SUP, DISP, COMP, TC, FIT, COCON, FEES (SC2/SC3) — [B][D][E][F] — Consumer Duty annual board report + outcomes monitoring
- **UK-003** — UK MiFIR (onshored Reg 600/2014) (★ starter as "MiFIR UK") + UK RTS 22/23 — transaction reporting & reference data to FCA (SC1/SC2) — [C]
- **UK-004** — UK EMIR + UK EMIR Refit (reporting rewrite live since 30 Sep 2024) (SC1/SC2) — [C]
- **UK-005** — UK MAR (onshored 596/2014) (SC1) — [B] — surveillance/STORs to FCA
- **UK-006** — UK Short Selling regime (new UK SSR framework under FSMA 2023) (SC1) — [B][C]
- **UK-007** — UK PRIIPs → transition to **Consumer Composite Investments (CCI) regime** (FCA CP24/30 → final rules; transition window) (SC1/SC2) — [D]
- **UK-008** — CASS client money & custody rules + FRC Client Asset Assurance Standard (annual CASS audit) (SC2/SC5) — [D][E]
- **UK-009** — SM&CR: senior managers regime, certification, conduct rules (SC2) — [F]
- **UK-010** — MIFIDPRU (IFPR): ICARA, K-factors, MIF00x returns, MIFIDPRU 8 disclosures (published) (SC2) — [E]
- **UK-011** — FCA operational resilience (SYSC 15A / PS21/3 — impact tolerances fully in force Mar 2025) + Critical Third Parties regime (SC2) — [E]
- **UK-012** — FOS jurisdiction + FSCS levy/disclosure duties (SC5) — [D]

### Financial promotions & crypto
- **UK-020** — FSMA s21 + Financial Promotion Order 2005 + COBS 4 (SC1/SC2) — [D]
- **UK-021** — **Cryptoasset financial promotions regime** (FSMA 2023 + FPO amendment, PS23/6: risk warnings, cooling-off, banned incentives) (SC1/SC2) — [D]
- **UK-022** — FCA FG24/1 social media / finfluencer guidance (SC3) — [D] — affiliate-marketing monitoring source
- **UK-023** — FCA cryptoasset registration under MLRs 2017 (held) + travel rule for cryptoassets (MLRs Part 7A) (SC1) — [A]
- **UK-024** — Incoming UK crypto regulated-activities regime (HMT SI + FCA CP wave 2025-26) (SC1) — [B][D] — radar item, §17

### Financial crime
- **UK-030** — Money Laundering Regulations 2017 (as amended) (SC1) — [A]
- **UK-031** — POCA 2002 + Terrorism Act 2000 (SARs to NCA, tipping-off) (SC1) — [A]
- **UK-032** — Criminal Finances Act 2017 — corporate failure-to-prevent tax-evasion facilitation (SC1) — [A][F]
- **UK-033** — **ECCTA 2023 — failure-to-prevent-fraud offence (in force 1 Sep 2025)** + Companies House ID verification reforms (SC1) — [A][F]
- **UK-034** — Bribery Act 2010 (SC1) — [A][F]

### ISA & product wrappers
- **UK-040** — Individual Savings Account Regulations 1998 + HMRC ISA manager guidance (SC1/SC3) — [D][C] — eToro ISA is live; `[verify: ISA manager of record — eToro UK vs partner (Moneyfarm arrangement) — determines whether HMRC returns/eligibility duties sit in-house]`
- **UK-041** — UK Futures offering perimeter (RAO permissions vs partner execution) (SC1) — [B][D] — `[verify structure of new futures product]`

### eToro Money UK (E4)
- **UK-050** — Electronic Money Regulations 2011 + Payment Services Regulations 2017 (SC1) — [D][E]
- **UK-051** — **FCA safeguarding reform for payments/e-money (CP24/20 → interim & end-state rules, phasing 2025-26)** (SC2) — [E][D]
- **UK-052** — PSR/FCA APP-fraud reimbursement regime (Faster Payments) where in scope (SC2) — [A][D] — `[verify applicability to eToro Money flows]`

## §6. S3 — United States (E11 eToro USA LLC · E12 eToro NY LLC · E13 eToro USA Securities Inc)

### Broker-dealer (E13)
- **US-001** — Securities Exchange Act 1934 + SEC BD rules: 15c3-1 net capital; 15c3-3 (exemption per fully-disclosed intro model `[verify (k)(2)(ii)]`); 17a-3/17a-4 books & records incl. WORM/e-comms retention; 17a-5 FOCUS + annual audited reports (SC1/SC2) — [E][C][B]
- **US-002** — Regulation Best Interest + **Form CRS** (SC2) — [D] — scope turns on whether copy-features = "recommendation" in US offering `[verify current US copy/portfolio scope]`
- **US-003** — SEC Rule 606 order-routing disclosures; Rule 10b-10 confirmations; Reg NMS interaction via clearing broker (SC2) — [B][C][D]
- **US-004** — **Regulation S-P as amended 2024** — safeguards + incident response & customer notification (compliance dates Dec 2025 / Jun 2026 — live) (SC2) — [F][E]
- **US-005** — Regulation S-ID (identity-theft red flags) + GLBA privacy notices (SC1/SC2) — [A][F]
- **US-006** — FINRA rulebook: 3110/3120/3130 supervision & CEO certification; 2210 communications with the public; 2090/2111 KYC & suitability; 3310 AML (incl. FinCEN CDD rule); 4511 records; 4530 reporting; 1210 registration/CE; U4/U5 (SC2/SC5) — [D][A][F][B]
- **US-007** — **CAT reporting obligations** (SEC Rule 613 / CAT NMS plan — order-event reporting incl. introducing-broker duties) (SC2/SC5) — [C] — existing eToro domain
- **US-008** — Trade reporting to FINRA facilities (TRF/ORF) per clearing arrangement (SC5) — [C] — `[verify allocation vs Apex]`
- **US-009** — SIPC membership; Section 31 fees; FINRA TAF/GIA assessments (SC5) — [E][C]
- **US-010** — State blue-sky BD/agent registrations (SC1) — [F]

### Crypto MSB (E11/E12)
- **US-020** — Bank Secrecy Act + 31 CFR Chapter X (MSB rules 1022): AML programme, SAR/CTR, funds travel rule 1010.410(f), FinCEN registration renewals (SC1/SC2) — [A]
- **US-021** — State money-transmission statutes (MTL portfolio; NMLS reporting; Money Transmission Modernization Act adoptions) (SC1) — [E][D]
- **US-022** — NYDFS 23 NYCRR Part 200 (BitLicense conduct, coin-listing, consumer protection) + **23 NYCRR Part 500 cybersecurity (as amended 2023, phased through 2025)** (SC2) — [B][D][E][F]
- **US-023** — OFAC compliance programme expectations for virtual currency (SC3) — [A]
- **US-024** — Federal crypto legislation wave: **GENIUS Act 2025** (payment stablecoins) + market-structure bill(s) status (SC1) — [B][D] — radar §17; affects stablecoin offering and future SEC/CFTC perimeter
- **US-025** — Negative perimeter guards (SC1) — [B] — no leveraged retail crypto (CEA 2(c)(2)(D)), no retail forex (no RFED), no margin lending `[verify all three remain true]`

### US tax reporting (see also §16)
- **US-030** — IRC §6045 broker reporting: 1099-B/DIV/INT/MISC + cost-basis rules (E13) (SC1/SC2) — [C]
- **US-031** — **1099-DA digital-asset broker reporting** — gross proceeds from 1 Jan 2025 trades (first filings Jan 2026, done), basis reporting for 2026 acquisitions (E11) (SC2) — [C]
- **US-032** — Backup withholding (§3406) + W-9/W-8 collection & validation (SC1) — [C][A]

## §7. S4 — Australia (E7 eToro AUS · E8 eToro Asset Management)

- **AU-001** — Corporations Act 2001 Ch 7: AFSL general obligations s912A, disclosure (PDS Pt 7.9), hawking prohibition s992A (SC1) — [B][D][F]
- **AU-002** — Design & Distribution Obligations Pt 7.8A + ASIC RG 274 — TMDs, distribution monitoring, significant-dealing reports (SC1/SC3) — [D][C]
- **AU-003** — **ASIC CFD Product Intervention Order (leverage caps etc., extended to 2027)** + RG 227 CFD disclosure (SC2/SC3) — [D]
- **AU-004** — Client money: Pt 7.8 Div 2 + ASIC Client Money Reporting Rules 2017 (SC1/SC2) — [D][E][C]
- **AU-005** — **ASIC Derivative Transaction Rules (Reporting) 2024 rewrite** (UTI/UPI/ISO 20022, live Oct 2024) (SC2) — [C] — existing eToro domain
- **AU-006** — Breach reporting / reportable situations (RG 78) + IDR (RG 271) + AFCA scheme (SC3/SC5) — [F][D]
- **AU-007** — AML/CTF Act 2006 + Rules + **AML/CTF Amendment Act 2024 — reformed obligations commencing 31 Mar 2026 (live: new programme structure, CDD rules; tranche-2 entities join)** (SC1/SC2) — [A]
- **AU-008** — E8 Responsible Entity stack: Ch 5C scheme registration (ARSN 637 489 466), compliance plan + audit, scheme constitution, RG 132/133 custody standards, member reporting (SC1/SC3) — [D][E][C][F]
- **AU-009** — ATO third-party regimes: TFN/ABN withholding, annual investment income reporting, CRS/FATCA lodgment via ATO (SC1) — [C]
- **AU-010** — Spam Act 2003 + ASIC social-media/finfluencer guidance (INFO 269) (SC1/SC3) — [D]

## §8. S5 — ADGM (E6 eToro (ME) Ltd)

- **ME-001** — ADGM FSMR 2015 + FSRA Rulebooks: GEN, COBS, PRU (category per matched-principal + custody + managing assets), AML (SC1/SC2) — [B][D][E][A]
- **ME-002** — FSRA Virtual Asset framework — accepted-virtual-assets governance (AVA list published), VA custody & conduct chapters (SC2/SC3) — [B][D]
- **ME-003** — UAE federal AML law (Decree-Law 20/2018 as amended) + UAE targeted financial sanctions (EOCN) (SC1) — [A]
- **ME-004** — ADGM Data Protection Regulations 2021 (SC1) — [F]
- **ME-005** — FSRA reporting: prudential returns, conduct reports, client-money auditor reporting (SC2/SC3) — [C][E]

## §9. S6 — Singapore (E10 eToro Singapore Pte Ltd, CMS101824)

- **SG-001** — Securities and Futures Act 2001 + SF(Licensing and Conduct of Business) Regs — incl. client-money/asset rules (SC1/SC2) — [B][D][E]
- **SG-002** — MAS AML/CFT Notice SFA04-N02 + guidelines (SC3) — [A]
- **SG-003** — MAS conduct & risk notices/guidelines: business conduct, risk-fact statements for CFD-type products, Technology Risk Management Guidelines, Outsourcing, Cyber Hygiene Notice (SC3) — [D][E][F]
- **SG-004** — Leveraged FX perimeter conditions on the CMSL (SC2) — [B][D]
- **SG-005** — PDPA 2012 (SC1) — [F]
- **SG-006** — Crypto boundary: Payment Services Act DPT licensing NOT held → no DPT services from SG entity `[verify]` (SC1) — [B]
- **SG-007** — Singapore CRS/FATCA regs (IRAS) (SC1) — [C]

## §10. S7 — Seychelles (E9)

- **SC-001** — Securities Act 2007 + Securities (Conduct of Business) Regulations + licence conditions SD076 (SC1/SC2) — [B][D]
- **SC-002** — Seychelles AML/CFT Act 2020 + FIU rules (SC1) — [A]
- **SC-003** — Beneficial Ownership Act 2020 (SC1) — [F]
- **SC-004** — Seychelles Data Protection Act 2023 (SC1) — [F]
- **SC-005** — FSA prudential/returns + circulars for securities dealers (SC3) — [C][E]

## §11. S8 — Malta (E5 eToro Money Malta)

- **MT-001** — Financial Institutions Act (Cap 376) + EMD2/PSD2 transpositions + CBM directives (SC1/SC2) — [D][E]
- **MT-002** — Safeguarding of e-money holder funds rules (SC2) — [E][D]
- **MT-003** — PMLFTR (Cap 373) + FIAU Implementing Procedures (SC1/SC3) — [A]
- **MT-004** — MFSA conduct/returns for EMIs; Malta GDPR act (SC2/SC3) — [C][F]
- **MT-005** — EU payments radar: PSD3/PSR package + instant payments Reg 2024/886 as applicable to EMI flows (SC1) — [D][E] — §17

## §12. S9 — Gibraltar (E14 eToroX)

- **GI-001** — Financial Services Act 2019 (Gib) + DLT Provider regulatory framework (9 GFSC principles + 10th market-integrity principle) (SC1/SC2) — [B][D][F]
- **GI-002** — Gibraltar POCA + AML guidance notes (SC1) — [A]
- **GI-003** — Gibraltar DPA 2004 (GDPR-aligned) (SC1) — [F]
- *Action:* `[verify whether eToroX licence is being maintained, migrated under MiCA-equivalent plans, or wound down — determines whether this whole section stays in scope]`

## §13. S10 — Israel (HQ layer, E15)

- **IL-001** — No retail licence: ISA Trading Arena application withdrawn 2016 — Israeli residents are NOT onboarded under an Israeli licence; maintain the cross-border/reverse-solicitation position as a documented legal source (SC1) — [D][F]
- **IL-002** — Israeli Privacy Protection Law + Amendment 13 (see GRP-032) — database registration/DPO duties for HQ processing (SC1) — [F]
- **IL-003** — Israeli AML/CFT + NBCTF designations (screening source, GRP-024) (SC1) — [A]
- **IL-004** — Israeli Companies Law + tax residency of group companies HQ'd in Bnei Brak (finance-owned) (SC1) — [F]

## §14. OVL — EU/EEA host-state overlay register (passporting, cross-border marketing & product intervention)

Maintain per-country overlay files for every marketing-active market; minimum set = the declared key markets (UK, DE, FR, ES, IT, AU, UAE) plus known intervention states:
- **OVL-BE** — Belgium FSMA regulation banning distribution of OTC CFDs/leveraged products to retail (2016) — [D]
- **OVL-FR** — France: Sapin II electronic-advertising ban on high-risk CFDs; **Loi Influenceurs 2023-451** (influencer marketing of financial/crypto products); AMF doctrine — [D]
- **OVL-ES** — Spain: CNMV 2023 resolution restricting CFD advertising/marketing incentives; crypto-ad rules (Circular 1/2022, now interacting with MiCA marketing) — [D]
- **OVL-DE** — Germany: BaFin CFD general administrative act (negative balance protection etc.); crypto custody via Tangany; German marketing/imprint rules — [D]
- **OVL-IT** — Italy: Consob measures + Italian FTT interaction (§16) — [D]
- **OVL-NL / OVL-PL / OVL-NO / OVL-PT / OVL-RO …** — national leverage/marketing variations and experienced-retail carve-outs — [D]
- **OVL-general** — ESMA opinions on third-country/reverse solicitation (MiFID + MiCA) governing which residents each entity may serve — [D][F]

## §15. Existing eToro solution domains → source cross-walk (sanity anchor)

| Current department domain | Primary sources above |
|---|---|
| Regulatory trade reporting | EU-002/007/008/016, UK-003/004/006, US-007/008, AU-005, ME-005, SC/SG returns |
| Trade surveillance | EU-014, UK-005, MiCA Title VI (EU-030), FINRA/SEC (US-001/006), ASIC MI expectations |
| Communications surveillance | EU-001 Art 16(7) taping + records, UK SYSC/COBS records, US-001 17a-3/4 + FINRA 2210/3110, AU s912A records |
| Affiliate marketing monitoring | EU-060/061/062/064, UK-020/021/022, US-006 (2210), AU-010, OVL-* (all) |
| AML | EU-040/041, UK-030..034, US-020/023, AU-007, ME-003, SG-002, SC-002, MT-003, GRP-020..027 |

## §16. TAX — client-facing tax reporting & transaction-tax layer (explicit deep-dive)

### Automatic exchange of information (per entity as reporting FI)
- **TAX-001** — **FATCA** (★) — IRC ch. 4 + local Model 1 IGAs (Cyprus, UK, Malta, Australia, Singapore, UAE, Gibraltar, Seychelles) — GIIN maintenance, W-8/W-9 self-certs, annual reporting to each local tax authority (SC1) — [C][A]
- **TAX-002** — **CRS/DAC2** — Cyprus AEOI law, UK International Tax Compliance Regs 2015, ATO, IRAS, Malta, Gibraltar, UAE, Seychelles implementations; **CRS 2.0 amendments phasing 2026-27** (SC1) — [C]
- **TAX-003** — **DAC8 (Dir (EU) 2023/2226) — crypto-asset reporting, in force 1 Jan 2026; first exchanges 2027** — eToro Europe as reporting CASP (SC1) — [C]
- **TAX-004** — **UK CARF + CRS 2.0 — data collection from 1 Jan 2026, first reports 2027** — eToro UK crypto activity (SC1) — [C]
- **TAX-005** — CARF adoption tracker for other operating jurisdictions (UAE, SG, AU commitments ~2027-28) (SC1) — [C]

### US withholding at source (non-US entities serving US-securities exposure)
- **TAX-010** — **QI regime** (Rev. Proc. 2022-43 QI agreement) or NQI documentation chain for EU/UK/AU/ME entities holding US securities for clients; Forms 1042/1042-S; periodic QI certification `[verify which entities are QIs vs rely on upstream custodian]` (SC1/SC5) — [C][A]
- **TAX-011** — **IRC §871(m)** — dividend-equivalent withholding on CFDs/derivatives referencing US equities (delta-one in scope; current transition-relief state) (SC1/SC2) — [C] — direct driver of CFD dividend-adjustment logic
- **TAX-012** — US Forms 1099 suite + 1099-DA and backup withholding for E11/E13 (US-030..032) (SC1/SC2) — [C]

### Transaction taxes & levies collected/remitted by the broker chain
- **TAX-020** — UK Stamp Duty Reserve Tax 0.5% (CREST-collected) + PTM levy (SC1/SC5) — [C][B]
- **TAX-021** — Irish stamp duty 1% (SC1) — [C]
- **TAX-022** — French FTT (rate raised 2025 `[confirm current 0.3%→0.4% state]`) incl. in-scope issuer list maintenance (SC1) — [C]
- **TAX-023** — Italian FTT — equities + derivatives schedule, filing via intermediary chain (SC1) — [C]
- **TAX-024** — Spanish FTT 0.2% (SC1) — [C]
- **TAX-025** — Belgian TOB — `[verify whether any eToro entity registered as Belgian intermediary; otherwise client-side]` (SC1) — [C]
- **TAX-026** — Hong Kong stamp duty via custody chain for HK-listed stocks (SC1/SC5) — [C]
- **TAX-027** — US Section 31 fees + FINRA TAF (E13, dup US-009) (SC5) — [C]

### Localized client tax outputs (product feature with statutory hooks)
- **TAX-030** — Per-market annual tax reports (DE/FR/ES/IT/UK/AU/IL etc.) — mostly service-level, but wrapper products create statutory duties: **UK ISA reporting (UK-040)**, AU AMIT/attribution if any scheme distributions (AU-008), Israeli clients' capital-gains documentation (no withholding by foreign broker) (SC1/SC3) — [C][D]
- **TAX-031** — Cost-basis/corporate-actions data integrity as a tax-reporting dependency (feeds F-fabric evidence) — [C]

## §17. RADAR — in-flight regulatory change (L2 change-inference seed list, as of Aug 2026)

| Change | Sources affected | Key dates |
|---|---|---|
| EU AI Act tranche 2 — high-risk + transparency obligations | GRP-036 | 2 Aug 2026 (now) |
| EU AML package (AMLR/AMLD6/AMLA) | EU-041 | applies 10 Jul 2027; AMLA operational ramp 2026 |
| MiFIR review level-2 (RTS 22/23 rewrite, SI regime recalibration, CTP) | EU-002/007/008 | drafts 2025-26, application ~2027 — directly hits SI/TRADEcho + Cappitech-replacement plans |
| EMIR 3 implementation (active accounts, reporting refinements) | EU-016 | phased 2025-26 |
| UK CCI regime replacing PRIIPs | UK-007 | final rules → transition through ~2027 |
| UK crypto regulated-activities regime (HMT SI + FCA rules) | UK-023/024 | CPs 2025-26, go-live expected 2027 |
| FCA safeguarding end-state for e-money | UK-051 | interim rules live; end-state to follow |
| ECCTA failure-to-prevent-fraud | UK-033 | in force 1 Sep 2025 — programme must exist now |
| AUSTRAC AML/CTF reform | AU-007 | commenced 31 Mar 2026 |
| DAC8 / UK CARF / CRS 2.0 | TAX-002..005 | collection 2026, first reporting 2027 |
| 1099-DA basis reporting phase | TAX-012 | 2026 acquisitions → 2027 filings |
| US market-structure legislation + GENIUS Act rulemaking | US-024 | Treasury/agency rulemakings 2026-27 |
| Distance-marketing recast | EU-060 | applies 19 Jun 2026 (now) |
| CSRD Omnibus re-scoping | EU-065 | FY2027+ `[finance]` |
| PSD3/PSR + instant payments | MT-005 | negotiation/phase-in 2026-28 |
| EGC status loss → SOX 404(b) | GRP-003 | likely FY2026 cycle `[verify]` |

## §18. STD — standards & frameworks layer (voluntary/anchoring, SC4)

- **STD-001** — ISO/IEC 27001 (★) + 27701 (privacy extension) — [F][E]
- **STD-002** — SOC 2 (★) (AICPA TSC) — as service organisation and as vendor-assurance input — [F][E]
- **STD-003** — NIST CSF 2.0 — [E][F]
- **STD-004** — PCI DSS v4.x — card acceptance flows (eToro Money / deposits) — [E][F]
- **STD-005** — COSO Internal Control & ERM frameworks — [F] — also SOX ICFR anchor
- **STD-006** — IIA Global Internal Audit Standards (2024) — [F]
- **STD-007** — ISAE 3000 / SOC-style assurance — CLHEAR CL3/CL4 conformance vehicle — [F]
- **STD-008** — FATF + Wolfsberg (GRP-026/027) — [A]
- **STD-009** — IOSCO principles (retail conduct, crypto-asset recommendations 2023) — [B][D]
- **STD-010** — LEI maintenance (GLEIF/ROC) + ISO 20022 message standards — reporting-infrastructure dependencies — [C]
- **STD-011** — SWIFT CSP if any SWIFT connectivity via eToro Money `[verify]` — [E]

## §19. GDL — level-3 guidance libraries to crawl continuously (SC3)

ESMA guidelines/Q&As/opinions · EBA guidelines (AML, outsourcing, ICT pre-DORA) · CySEC directives + circulars (full C-series) · FCA policy statements, finalised guidance, Dear-CEO letters, portfolio letters · FINRA regulatory notices + exam-priorities/annual report · SEC risk alerts + staff bulletins · ASIC regulatory guides + INFO sheets + corporate plan · AUSTRAC guidance · MAS notices, guidelines, circulars · FSRA guidance + Dear-SEO letters · MFSA circulars + FIAU guidance · GFSC guidance · FSA-Seychelles circulars · NYDFS industry letters · FATF mutual-evaluation follow-ups for each operating jurisdiction · ICO / EDPB / Israeli PPA guidance.

## §20. Verification checklist (internal knowledge required — resolve before L1 freeze)

1. Securities-lending programme: which entities lend client stock today → SFTR/UK-SFTR in or out (EU-017).
2. QI status per non-US entity vs reliance on upstream custodian chain (TAX-010); confirm 871(m) operational owner.
3. Commodity-CFD position-reporting analysis under MiFID Art 58 post-quick-fix for eToro Europe — in perimeter or documented out.
4. eToro USA Securities clearing model + CAT/TRF duty allocation vs Apex (US-001/007/008); Reg BI scope of copy features in US (US-002).
5. eToro ISA manager of record and HMRC return ownership (UK-040); futures product structure (UK-041).
6. Staking legal analysis per entity under MiCA / local law (EU-034).
7. eToroX Gibraltar — maintain, migrate, or retire (§12).
8. Singapore crypto boundary — confirm no DPT activity (SG-006).
9. EGC status loss timing → SOX 404(b) first year (GRP-003).
10. Belgian TOB intermediary registration status (TAX-025); current French FTT rate (TAX-022).
11. eToro Money UK — APP-fraud reimbursement applicability (UK-052); SWIFT CSP (STD-011).
12. DSA classification memo (online platform vs hosting) behind the published DSA information (EU-062) — determines transparency-report depth.
13. Interest-on-balance product — banking/deposit perimeter analysis per jurisdiction (touches E2/E3/E4).
14. Alpha Portfolios / Tori / Agentic Portfolio — confirm AI Act + advice-perimeter analyses exist per market (GRP-036, EU-001, US-002).
15. Confirm no Canada/Japan/other unlisted-jurisdiction client acceptance that would add regimes.

## §21. Coverage-eval note (how to prove "nothing is missing")

Completeness cannot be asserted from a list alone; run it as an eval, CLHEAR-style: (1) reconcile this registry against each regulator's public register entry for E1-E15 (permissions ↔ sources); (2) reconcile against the 20-F risk-factor + regulation sections (a listed-company legal team's own inventory — anything they cite that L1 lacks is a gap); (3) reconcile against invoice/vendor list (every RegTech vendor implies an obligation family); (4) reconcile against the five department domains (§15) and every recurring report the Cyprus ops team files; (5) host-state sweep: for each country with marketing spend, check an OVL file exists. Target: every permission, vendor, filed report, and marketed country maps to ≥1 source ID.
