# CLHEAR L1 publisher scope

L1 is a company-independent regulatory corpus. Organization applicability belongs downstream; an organization's products, licences or demo needs never remove publisher documents from L1's denominator.

The scope covers every current publisher profile in `app/clhear/l1/publishers.py`: rules and legislation, financially relevant standards, official compliance guidance, rule filings and amendments, examination and enforcement publications, and their official attachments. It includes current material and officially exposed historical archives. It excludes unavailable historical reconstruction, unrelated news/research/speeches, company-filing databases, and unrelated industry standards. Relevant standards cover financial services, compliance, governance, reporting, security, privacy and resilience.

`source_registry.py` contains the known starting declarations. It is not a complete publisher inventory. The former `registry_etoro.py` import path remains compatible but points to these neutral declarations. Old research is retained under `docs/archive/`; its company-specific scope restrictions are inactive.

## Evidence and worker execution

Only L0/L1 system workers may discover, reconcile, acquire, parse or evaluate source content. Readers do not run discovery. `run_inventory_audit(..., scope="all_publishers", discover=True)` uses the same implementation as daily worker discovery. `all_publishers` and the legacy `registered` name address the same global audit stream.

Each discovery cycle persists a publisher/profile/date identity and leased page frontier. Successful page checkpoints retain the exact URL, source permission evidence, artifact hash and publisher-check timestamp. Interrupted batches resume; duplicate claims are fenced. Catalog pagination retains the reviewed catalog permission identity; constituent documents require their own operation grants. Permission checks occur before fetching and again before storing/parsing a returned response. Failures never refresh a prior publisher check.

The persisted cycle date identifies a resumable daily traversal; it is not a publisher-wide historical as-of filter. Catalogs can change during that traversal. A consistent publication cutoff and archive/language reconciliation remain acceptance gaps until the applicable publisher contract supports them.

A bounded batch is not a complete crawl. Remaining pages, unknown publication types, unsupported dynamic filters, alternate hosts, unavailable originals and permission failures remain explicit gaps. The global document total is `null` until the whole publisher inventory has been enumerated and independently reviewed. Known documents and blocked dependencies remain visible, without claiming the known count is the full denominator.

## Implemented catalog contracts

- FINRA: ten declared Manual/rules/archives/filings/notices/guidance/examination/enforcement categories, numeric page/year continuation, linked official documents and attachments. Non-rule documents dispatch to the FINRA document parser; collections cannot masquerade as rules.
- CySEC, NYDFS, Seychelles, Wolfsberg and ISA: official PDF-link libraries from existing collection references. Other categories, HTML libraries and alternate distribution hosts remain explicit unsupported gaps.
- EU Publications Office: CELEX metadata enumeration through the documented Cellar SPARQL endpoint, with ordered keyset continuation for secondary legislation and consolidated publications. Exact CELEX identifiers feed the existing EUR-Lex adapter. This does not independently certify all language manifestations, attachments or every legal-publication category. Protocol references: [Publications Office](https://op.europa.eu/en/web/webtools/linked-data-and-sparql-test-linda) and [European Commission LEOS source documentation](https://leos.pages.code.europa.eu/ai4drpm/_modules/ai4drpm/utils/sparql_utils.html).
- ESMA: typed, paginated [official library](https://www.esma.europa.eu/databases-library/esma-library?page=0) rows and linked PDFs. Speech/news rows are excluded. Ambiguous types remain classification gaps rather than being silently dropped or treated as compliance documents.
- GovInfo: [official collection sitemaps](https://www.govinfo.gov/sitemaps) for USCODE, annual CFR and Federal Register, followed to exposed package pages and exact linked originals. [GPO's sitemap protocol](https://github.com/usgpo/sitemap) defines those links. Package identity, granule completeness and remaining distribution/edition contracts still require review.

Other publisher profiles expose their missing catalog implementation explicitly. A profile is not a claim that a publisher is fully supported or imported. FCA's [Handbook API](https://handbook.fca.org.uk/handbook-api) requires a registered account; its [published API information](https://handbook.fca.org.uk/latest-news/news-details/8e0653c7-1376-44b8-8bf1-9b41130dc50c) states that historical content is excluded. Its API and exposed archive therefore require separate authenticated/current and historical contracts. No guessed API endpoint or unauthorized artifact substitute is used.

## Collection and document identities

The legacy FINRA Rulebook, NYDFS/CySEC/Seychelles/ISA libraries, Wolfsberg collection, and PCI/IFRS catalogs are collection records. Stored historical versions remain preserved but do not count as imports of their constituent documents. PCI and IFRS require explicit individual document/edition inventories. ISO 27001:2022 and its 2024 amendment remain separate documents; AICPA TSC retains its exact edition identity.

The five national-overlay references no longer fetch one shared ESMA PDF while claiming five national originals. Their source IDs remain stable and their missing official inventories remain visible. Other previously bundled names now describe the actual fetched document; omitted sibling publications belong in publisher discovery, not in inflated source labels.

## Acceptance

Every expected original must have current operation permission, exact edition/provenance evidence, preserved artifact hashes and a complete independently verified encoded projection. The inventory auditor calls the shared original/projection verifier; unsupported formats fail explicitly. Exact wording alone does not prove hierarchy, record multiplicity, complete clause sets or accurate source locations.

One verified source does not certify its publisher, and one reviewed publisher does not certify global L1. Acceptance requires complete publisher coverage, current version and permission bindings, fresh successful publisher checks, and verified wording/order/hierarchy/locations across every expected document. Only actual scheduled worker cycles establish daily scheduling evidence. Manual verification is labelled separately. Failed or incomplete cycles retain the last accepted release.
