# CLHEAR — High-Level Design v2 (finalized, build-ready)

**CLHEAR** — Compliance Lifecycle Harmonization, Efficiency, Assurance and Reliability. The open data standard for compliance programs, run as a live eight-layer system.
**Owner:** Avner Yoffe, Regulation Forty Two Ltd (Reg42) · **Builder:** Cursor / OpenHands in `reg42-os`, `reg42-infra`, `clhear` (public) · **Date:** 11 September 2026
**Supersedes:** `CLHEAR_HLD.md` v1.0 (L0+L1), `CLHEAR — Live System Architecture`, `CLHEAR_L1_BUILD_MANUAL.md`. When this file and any of those disagree, this file wins.
**Related:** `Reg42-Workforce-HLD-v5.1` (company OS — separate plane), `Reg42-Infer-Model-Picker` (inference plane — reused here), `SOLON_PLAN.md` (Solon entity UI — reused here).
**Disclosure:** the US provisional is filed 11 Sep 2026; nothing in the public repo or site goes live before Maya confirms filing. NYC demo 28 Sep 2026.

**How to read this as the implementer.** §1 says what CLHEAR is and what "usable by the largest financial institutions" means as requirements. §2 is the invariants. §3 is the platform plane (L0). §4 is one section per layer with the same eight headings: purpose, inputs, outputs and schema, agent fleet and daily re-derivation, evals gate, UI, API, openness. §5 is the interface (what makes it easy, fascinating, educational). §6 is the community and contributor model. §7 is trust, security and legal. §8 is the build order with acceptance tests and the NYC demo scope. §9 is the never-list. Build §8 top to bottom. If anything in code, a ticket, or a comment conflicts with §2 or §9, stop and surface it.

---

## 1. What CLHEAR is, and the bar it must clear

CLHEAR takes the world's laws, regulations and standards, turns them into one deduplicated registry of obligations with proof of coverage, decomposes each obligation into the concrete things an organization must have and do, enumerates every kind of regulated organization, and composes — for any profile, in under a minute — the leanest complete compliance program with a full evidence chain from every item back to the verbatim source clause. It re-derives itself daily as sources change. It runs in two modes: **agnostic** (any profile, no company data — open, free via web and API) and **instance** (a real organization's Actual overlay, gaps and priorities — closed, delivered through Reg42 OS in the client's own AWS account). *We give away the diagnosis; we sell the cure.*

### 1.1 What "usable by the world's largest financial institutions" means as requirements

Tier-1 firms will not adopt a compliance standard because it is clever. They adopt what their second line, internal audit, external auditor and regulator will accept. That translates into the following, each of which appears again as a concrete mechanism in the layers below:

| Requirement | Mechanism in this HLD |
|---|---|
| **Provenance to the byte.** Every obligation traces to verbatim official text with a hash, publisher, version and retrieval time | L1 immutable corpus; clause-level hashes; `cites` edges carry span offsets |
| **Proof of completeness.** "Complying with CLHEAR = complying with all sources" must be demonstrable, not asserted | L2 coverage evals: every normative clause maps to ≥ 1 obligation; published scorecards; expert-audited samples |
| **Reproducibility.** The same inputs and the same release produce the same outputs, years later | Every determination records inputs hash + model manifest (frozen model version) + skill version + seed; releases are immutable snapshots |
| **Explainability.** Every node answers "why" — the reasoning and the evidence — in one click and one API call | Why-trail on every determination; bidirectional graph query obligation ↔ evidence in one hop [CLHEAR-6.1-R2] |
| **Change control.** A regulatory change is detected at clause level, propagated through every layer, and announced with effective dates | L1 change detection → L2 change inference → cascade with `valid_from/valid_to` on every node and edge; change feed |
| **Independence and neutrality.** Vendor-neutral standard, governed transparently, no lock-in | Open outputs under an open licence; public governance (§6); OSCAL/JSON-LD export; conformance levels with independent assessment (CL3/CL4) |
| **Accuracy with a number.** A stated, measured, per-layer accuracy with a method an auditor can repeat | Per-layer golden sets and gates (§4); public evals dashboard; drift alarms |
| **Procurement-clean AI.** Model origin, hosting and data handling that pass a bank's third-party risk review | Bedrock-only inside the AWS account; procurement-clean model defaults (US/EU-origin) for derivations; per-release model manifest; no client data in agnostic mode, ever |
| **Security posture.** SOC 2 / ISO 27001 path, SSO, audit logs, signed artefacts, SBOM, pen test | §7 |
| **Interoperability.** Fits the tools banks already run (GRC platforms, control libraries, regulatory-change tools) | OSCAL export for controls, identifier crosswalks (NIST CSF, CCM, ISO 27001 clauses), stable IDs, REST + GraphQL + bulk snapshots |
| **Multi-jurisdiction by construction.** UK, EU, US, APAC, offshore — one registry, jurisdiction-tagged | Jurisdiction, regulator and instrument as first-class nodes; equivalence edges across sources |
| **Availability and support.** SLOs, status page, versioned API with deprecation policy | §7 |

### 1.2 Why horizon-scanning vendors failed, and what CLHEAR does instead

| Failure pattern | CLHEAR's answer |
|---|---|
| Republished verbatim text → licensing exposure, no added value | Derived outputs (obligations, blocks, mappings) are the product; verbatim is stored, hashed and linked, shown only where rights allow (§7.3) |
| Alert firehoses nobody reads | Changes propagate to *your profile's* blueprint; you see "three items changed in your program", not "412 regulatory updates" |
| Closed taxonomies that never match the firm | Profile-driven applicability (L4/L5) resolves obligations into an executable program for *this* kind of organization |
| Outputs stop at "you should comply with X" | L3 decomposes to Systems, Documents, Roles, Processes, Workflows, Assets, Bodies with characteristics; L8 fills them with benchmarked content |
| No proof of completeness or accuracy | Public evals, coverage proofs, reproducible releases |
| No community; one vendor's opinion | Open standard with contributors, reviewers, working groups, and a conformance program |
| Analysts as the engine → cost and lag | Agent fleets re-derive every layer daily; humans intervene where confidence is low or a modification request is raised |

---

## 2. Invariants

| # | Invariant |
|---|---|
| I1 | **Layers are derived strictly in order.** L(n) reads only L(n−1) and below, never a later layer. Cross-layer edges point downward. |
| I2 | **Every node and edge is versioned and dated.** `id` is stable for life; `version` increments; `valid_from` / `valid_to` carry regulatory effect dates; `derived_at` carries derivation time. Nothing is deleted; it is invalidated. |
| I3 | **Every determination has a why-trail.** Inputs hash, model manifest, skill version, reasoning summary, confidence, evals run id. No trail, no write. |
| I4 | **Full AI determination, human-in-the-loop by exception.** Agents decide at first entry and on every change. Humans intervene when confidence < threshold (approval console) or when a modification request is submitted. Determinations with accepted human edits are re-derived on the next cycle and must reproduce the edit or escalate. |
| I5 | **Agnostic mode contains no organization's data.** Instance data (Actual overlay, gaps, priorities, evidence) lives only in the client's Reg42 OS deployment, in the client's account. The public CLHEAR store is provably free of it. |
| I6 | **Bedrock-only inference via Reg42 Infer.** Frozen model versions per release; procurement-clean defaults for derivation classes; model manifest published with every release. |
| I7 | **Postgres is the record; Neo4j is the query graph; vectors are an index.** All three rebuildable from Postgres. |
| I8 | **Rights before text.** A source's verbatim text is stored and displayed only under a recorded rights basis (public domain, licence, BYOL viewer). Derived outputs never quote beyond what the basis allows. |
| I9 | **Open by mode, not by layer.** Agnostic outputs of L1 (pointers/hashes; verbatim where rights allow) through L6 plus base priorities are open. The re-derivation engine, instance mode, L8 fills, Reg42 OS and Solon are closed. |
| I10 | **Evals gate publication.** No layer output enters a public release below its gate. Drift below gate freezes that layer's publication and alarms. |
| I11 | **Stable requirement and object identifiers.** `CLHEAR-<layer>.<n>` for standard requirements; `OBL-`, `BLK-`, `PRF-`, `ACT-`, `BLU-`, `RSK-`, `FIL-` prefixes for objects; never reused. |
| I12 | **Contributions never write directly.** A contribution is a proposal; the agent fleet re-derives with it as evidence; a reviewer accepts; the release includes it with attribution. |

---

## 3. L0 — Platform plane

Runs in the Reg42 platform account today (`730649732189`, `clhear-*` resources) and moves with the fleet plan. One deployment serves all layers; layers are packages, not microservices.

| Component | Choice | Notes |
|---|---|---|
| Record store | RDS Postgres (Aurora Serverless v2 acceptable), one schema per layer, bi-temporal columns on every table | Source of truth. Row-level `derived_at`, `valid_from`, `valid_to`, `version`, `why_trail_id` |
| Query graph | Neo4j Community (single node now; Enterprise cluster later if p95 > 500 ms) | Projection of Postgres; rebuilt nightly; answers bidirectional obligation ↔ evidence in one Cypher query |
| Retrieval index | pgvector in Postgres (**replaces Weaviate** — one fewer stateful system for bank security reviews; same recall at this scale) | Founder decision: keep Weaviate only if a benchmark shows a material recall gap |
| Object store | S3 with Object Lock (WORM) for L1 originals and release snapshots | Versioned; cross-region replica |
| Events | SQS + EventBridge (`clhear.<layer>.<event>`) | Every layer publishes `derived`, `changed`, `invalidated`, `below_gate` |
| Workers | ECS Fargate Spot `clhear-workers` (exists) — one task definition per layer fleet | Event-driven; scale 0 → N on queue depth |
| Inference | **Reg42 Infer** (router + tools + model picker) — CLHEAR task classes added to `tasks.yaml` | Frozen model ids per release; procurement-clean defaults for derivation classes: gpt-oss-120b, Mistral Large 3, Claude Sonnet 5 / Opus 5 on Bedrock, Nova; Chinese-origin open weights permitted only for non-derivation classes (classification, summarization) unless a client policy allows more |
| Evals | Langfuse (self-hosted) + per-layer golden sets in `clhear-evals` | Public dashboard reads from a published summary table, never from Langfuse directly |
| Approval console | Existing Lambda UI at `clhear.reg42.ai/console`, extended | Low-confidence determinations, modification requests, contribution reviews |
| Release pipeline | GitHub Actions: nightly derivation → evals → snapshot → signed release (Sigstore) → publish | Semantic-date versions `2026.09.28`; daily deltas between releases |
| Identity | Cognito for public users; Google SSO for Reg42; API keys per organization | SSO/SAML for enterprise API accounts |
| Observability | Prometheus + Grafana, GlitchTip, CloudWatch; status page | Per-layer freshness and gate status are public metrics |
| Public repo | `clhear` on GitHub: standard text, schema, Obsidian-vault packs, evals harness, SDKs | Everything in §2 I9 "open" |

Shared schema (every layer's tables inherit it):

```
id            text  (stable, prefixed)
version       int
valid_from    date  (regulatory effect; null = not yet)
valid_to      date  (null = in force)
derived_at    timestamptz
derived_by    text  (agent id + skill version)
model_manifest jsonb (model id, version, params, seed)
inputs_hash   text
confidence    numeric
why_trail_id  text  → why_trails(id): reasoning summary, evidence refs, evals run id
review        jsonb (human review events, contribution refs)
jurisdictions text[]
```

---

## 4. The eight layers

Each layer: purpose · inputs · outputs and schema · fleet and daily cycle · evals gate · UI · API · openness.

### L1 — Verbatim Source Corpus

**Purpose.** The vault of regulatory truth: official texts fetched from authoritative publishers, split into clauses, stored immutably, watched forever.

**Inputs.** Publisher registries and endpoints (EUR-Lex, legislation.gov.uk, FCA Handbook, eCFR/Federal Register, SEC/EDGAR, ESMA, FATF, BIS/Basel, IOSCO, MAS, ASIC, ISA, NIST; ISO/AICPA under licence), amendment registries, citation mining from stored texts.

**Outputs / schema.** `sources` (instrument, publisher, jurisdiction, rights basis, family root), `source_versions` (retrieved_at, sha256, byte length, canonical URL, WORM URI), `clauses` (hierarchical path, text, span offsets, hash, normative flag), `families` (base act + amendments + delegated acts + guidance), `citations`.

**Fleet / daily cycle.** Fetchers (per publisher adapters; Crawl4AI/Docling for PDF/HTML parsing), family-completeness agents (amendment registries + citation mining), change detectors (clause-level diff on every re-fetch; effective-date extraction), rights recorder. Cycle: fetch → parse → diff → publish `clhear.l1.changed` with clause ids and dates.

**Evals gate.** Byte fidelity 100 % against publisher (hash match); family completeness ≥ 99 % against official amendment registries on the audited set; currency: median lag from publication to ingestion ≤ 24 h for tier-A publishers; parse quality: clause boundary F1 ≥ 0.98 on the golden set. Published per source as a scorecard.

**UI.** Source browser by jurisdiction/regulator/instrument; family tree; clause view with hash and retrieval time; change timeline; rights badge (public / licensed / BYOL — see §7.3); "watch this instrument".

**API.** `GET /l1/sources`, `/l1/sources/{id}/versions`, `/l1/clauses/{id}`, `/l1/changes?since=`; bulk snapshot per release.

**Openness.** Open: pointers, hashes, metadata, changes, and verbatim where the rights basis allows. Closed: licensed texts (served to BYOL holders only), fetcher fleet.

### L2 — Obligation Registry

**Purpose.** One deduplicated set of atomic obligations covering all sources, each linked to every clause that asserts it, with continuous change inference.

**Inputs.** L1 clauses and changes.

**Outputs / schema.** `obligations` (`OBL-`, plain-language determination text, subject, action, condition, object, jurisdictions, regulator, obligation type, effective dates), `asserts` edges (obligation ← clause, with span and strength: explicit/implied), `equivalences` (obligation ≈ obligation across jurisdictions; "prevent insider dealing" under MAR, FCA, SEC, ASIC), `supersessions`, `change_events` (added/updated/revoked, with cause clause ids).

**Fleet / daily cycle.** Extractors (clause → candidate obligations, structured output), consolidators (dedupe and merge into canonical obligations; equivalence detection), change inferencers (an L1 clause change → which obligations change how; revocation on expiry), reviewers (second-model check and confidence). Cycle: on `l1.changed` and nightly full pass on a rolling window.

**Evals gate.** Coverage: ≥ 99 % of normative clauses in the audited set map to ≥ 1 obligation (the "comply with CLHEAR = comply with all sources" proof); precision: ≥ 95 % of sampled obligations judged correct by two independent reviewers (expert panel quarterly, second-model weekly); dedupe: < 1 % duplicate rate; change inference: ≥ 95 % of golden change events correctly propagated with correct dates.

**UI.** Registry browser with filters; obligation page: determination text, every asserting clause with highlighted span, equivalents across jurisdictions, effective dates, history, why-trail, "request a modification"; cross-jurisdiction comparison view.

**API.** `GET /l2/obligations`, `/l2/obligations/{id}` (with sources), `/l2/obligations/{id}/history`, `/l2/changes?since=`, `/l2/equivalences`.

**Openness.** Open.

### L3 — Building Blocks (slots)

**Purpose.** Decompose every obligation into type-agnostic real-life deliverables — what an organization must *have* — each with a fixed characteristic schema per kind.

**Inputs.** L2 obligations.

**Outputs / schema.** `blocks` (`BLK-`, kind ∈ {System, Document, Role, Configuration, Process, Workflow, Asset, Body}, name, purpose), `requires` edges (obligation → block, with rationale span), `characteristics` per block kind (fixed schema: e.g. Process: trigger, performing role, system/tool, cadence, output, record; Document: owner, approver, review cadence, mandatory sections; Role: seniority, independence, competence, reporting line; Body: mandate, quorum, cadence; System: capability, data inputs, retention; Asset: quantity/threshold, custody; Configuration: parameter, allowed range; Workflow: composed processes, SLA), each characteristic value backed by obligation text where applicable.

**Fleet / daily cycle.** Decomposers (obligation → blocks), characterizers (fill characteristic schema from obligation text), harmonizers (one canonical block reused across many obligations — an AML Policy Document is one block cited by hundreds), change propagators (obligation change → block/characteristic change).

**Evals gate.** Completeness: every in-force obligation → ≥ 1 block (100 %); characteristic completeness ≥ 95 % of required characteristics filled with a backing span or an explicit "not specified by source"; expert sample precision ≥ 92 %; block reuse ratio monitored (explosion = defect).

**UI.** Block catalogue by kind; block page with characteristics, backing obligations, which profiles need it, history; "what does an MLRO role require, and why".

**API.** `GET /l3/blocks`, `/l3/blocks/{id}`, `/l3/obligations/{id}/blocks`, `/l3/kinds` (schemas).

**Openness.** Open.

### L4 — Profile Permutation Space

**Purpose.** The universal ontology of regulated organizations: jurisdictions → regulators → licences/authorizations → permitted products and services → client types → channels, and every valid combination.

**Inputs.** L1 (licensing regimes, permission taxonomies from regulators' registers), L2 (obligations conditioned on profile attributes).

**Outputs / schema.** `licences`, `products_services`, `client_types`, `channels`, `profiles` (`PRF-`, a valid attribute set), `permits` edges (licence → product), `applies_to` edges (obligation → profile predicate), validity rules (e.g. a MiFID investment firm without a CASS permission cannot hold client money).

**Fleet / daily cycle.** Ontology builders from regulator registers and permission taxonomies; predicate extractors (from obligation conditions to profile attributes); validators; change propagators.

**Evals gate.** Profile validity ≥ 99 % against regulator registers on the audited set (no impossible permutations offered); applicability precision/recall ≥ 95 % on golden profiles (Meridian Markets and three real anonymized profiles).

**UI.** Profile builder (guided, validating as you go); permutation explorer; "organizations like this exist in these jurisdictions".

**API.** `GET /l4/ontology`, `POST /l4/profiles/validate`, `GET /l4/profiles/{id}/obligations`.

**Openness.** Open.

### L5 — Activities Junction

**Purpose.** Bridge profiles and programs: what an organization *does*. Business Activities (side = business: onboarding, order handling, marketing, deposits/withdrawals, custody…) and Compliance Activities (side = compliance: screen, monitor, investigate, report, train, attest…). Offense/defense stays a narrative metaphor, never a schema term.

**Inputs.** L3 blocks (which activities a block performs or requires), L4 products/services (which business activities a product implies).

**Outputs / schema.** `activities` (`ACT-`, side, name, action type), `implies` (product/service → business activity), `operates` (compliance activity → block), `mitigates` (compliance activity ↔ business activity, with obligation refs).

**Fleet / daily cycle.** Activity mappers; consistency checkers (every compliance activity traces to an obligation via a block; every business activity traces to a product/service).

**Evals gate.** Junction completeness 100 % (no orphan activities); expert precision ≥ 92 %.

**UI.** Activity map: a profile's business activities on one side, the compliance activities that govern them on the other, edges lit by obligation.

**API.** `GET /l5/activities`, `/l5/profiles/{id}/activities`.

**Openness.** Open.

### L6 — Blueprint Composer

**Purpose.** For any profile, compose the leanest complete compliance program: the set of blocks, activities and characteristics that satisfy every applicable obligation, with a full evidence chain per item. The "aha" moment and the conversion point.

**Inputs.** L2–L5.

**Outputs / schema.** `blueprints` (`BLU-`, profile ref, release, composition), `blueprint_items` (block instance with characteristics resolved for this profile, obligations satisfied, activities operated), `minimality_proof` (which items are load-bearing for which obligations; removal impact).

**Fleet / daily cycle.** Composers (set-cover style minimization with hard constraints: every applicable obligation satisfied; reuse blocks maximally), explainers (per-item reasoning), diff engines (blueprint changes when any lower layer changes). Composition is deterministic given inputs; the agentic part is the explanation and the edge cases flagged for review.

**Evals gate.** Completeness 100 % (every applicable obligation satisfied by ≥ 1 item); minimality: no item removable without breaking coverage (checked); reference-program agreement ≥ 90 % against expert-authored programs (Annex G Meridian Markets; POC profiles); explanation quality ≥ 90 % on the rubric.

**UI.** The blueprint view: program tree by kind, each item with "why" (obligations → clauses), "what changed since", "compare with another profile", export. In instance mode (Reg42 OS only), the Blueprint/Actual overlay: ghost nodes = required, solid = present, delta = gap.

**API.** `POST /l6/blueprints` (profile → blueprint), `GET /l6/blueprints/{id}`, `/l6/blueprints/{id}/diff?against=`, OSCAL export.

**Openness.** Open (agnostic blueprint). Closed: instance overlay, gaps.

### L7 — Risk and Priority Scoring

**Purpose.** Rank what matters: per obligation and per blueprint item, a base priority from enforcement history and likelihood, financial, reputational and operational impact, and regulatory attention.

**Inputs.** L1 (enforcement notices, fines, thematic reviews as sources), L2 obligations, L6 items.

**Outputs / schema.** `risk_scores` (`RSK-`, obligation or item ref, dimensions, composite, calibration set ref, confidence), `enforcement_events` (regulator, date, amount, breached obligations — linked).

**Fleet / daily cycle.** Enforcement ingestors and linkers (case → obligations), scorers (calibrated model; not a black box: dimension weights published), reviewers.

**Evals gate.** Calibration: predicted enforcement likelihood vs observed on a held-out year, Brier score published; linker precision ≥ 90 %.

**UI.** Priority view over a blueprint; enforcement explorer ("who was fined for this obligation, when, how much").

**API.** `GET /l7/scores?obligation=`, `/l7/enforcement`.

**Openness.** Open: base priorities and method. Closed: instance priorities (require the organization's Actual overlay).

### L8 — Benchmarks and Fills

**Purpose.** Fill L3 slots with best-practice content: suggested policy text, procedure steps, surveillance typology sets with thresholds, workflow definitions, role descriptions, committee charters. Agents propose fills from sources and practice; contributors improve them; members' anonymized benchmarks refine them. L8 is never blocked on contributions.

**Inputs.** L3 blocks and characteristics, L6 blueprints, L1 guidance sources, contributor submissions, member benchmark aggregates (k-anonymized, opt-in).

**Outputs / schema.** `fills` (`FIL-`, block ref, kind ∈ {text, numeric, item set, workflow}, content, provenance ∈ {agent, contributor, member-benchmark}, maturity ∈ {draft, reviewed, endorsed}, jurisdictions, profile predicates), `benchmark_aggregates` (k ≥ 5, differential-privacy noise on numeric thresholds).

**Fleet / daily cycle.** Fill generators, benchmark aggregators, reviewers, drift detectors (a fill referencing an obligation that changed is re-derived).

**Evals gate.** Expert rubric ≥ 85 % for endorsed fills; every fill traceable to its block and obligations; no member-identifiable data (automated re-identification test on every aggregate).

**UI.** Fill library per block; side-by-side variants; adopt-and-adapt into instance mode; contributor credit.

**API.** `GET /l8/fills?block=` (members), `/l8/benchmarks` (members).

**Openness.** Closed (member/commercial); fill *existence* and maturity are public metadata so the free blueprint shows "fills available".

---

## 5. The interface — easy, fascinating, educational

One public product at `clhear.org` (or `clhear.reg42.ai` until the domain is set), one API, one Solon entity as the guide.

**Front door.** A single field: "Describe your organization" (or pick a profile). Solon (the entity UI from `SOLON_PLAN.md`, voice optional) asks the two or three questions L4 needs, validates against the ontology, and produces the blueprint in under 60 seconds with the evidence chain visible. This is the demo, the free tier, and the conversion moment.

**Explore.** Every layer is browsable as a graph and as a list; every node has "why" (reasoning + evidence), "history", "who else needs this", and "request a change". Cross-jurisdiction comparison is one click ("the same obligation in UK, EU and US"). The graph explorer renders the Blueprint as a constellation; in instance mode it becomes the Blueprint/Actual overlay.

**Learn.** Guided tours per cluster (Financial Crime, Markets, Reporting, Clients, Prudential, Enterprise); "obligation of the week" with the story of its enforcement history; scenario playgrounds ("add a crypto licence to this broker — what changes?"); a CLHEAR Analyst learning path with checkpoints and a credential issued as a verifiable badge; quizzes generated from the registry, always answerable from the evidence chain. Serious content, playful mechanics — bank compliance officers are professionals, not gamers, but they respond to clarity, mastery and recognition.

**Watch.** "What changed this week for profiles like mine" digest by email and API; a public change feed with effective dates; instrument watchlists.

**Build on it.** API keys in one click, SDKs (Python, TypeScript), OSCAL and JSON-LD export, a sandbox profile set, and public evals so integrators can cite CLHEAR's accuracy in their own assurance.

**Design rules.** Evidence is never more than one click away. Nothing is asserted without a "why". Every number links to its method. Loading a blueprint shows a progress narrative of the layers working, not a spinner. Dark/light, keyboard-first, accessible (WCAG 2.2 AA). No dark patterns, no paywall on the agnostic blueprint.

---

## 6. Community and contributor model

The standard's credibility comes from who checks it, not who wrote it. Design the community so that checking it is rewarding and cheap.

**Roles.** Reader (anyone) · Contributor (signed CLA; proposes changes, fills, mappings, translations, evals cases) · Reviewer (verified compliance, legal or audit professional; two reviewers accept a proposal) · Maintainer (per cluster; Reg42 staff and elected reviewers) · Steering group (Reg42 as originating creator plus institution and firm representatives; charter published; vendor-neutral by rule).

**What people contribute.** Corrections to obligations and mappings; missing sources; equivalence edges; block characteristics; profile ontology entries; L8 fills (policy language, procedures, typology sets); jurisdiction expertise; translations; golden-set cases; conformance evidence templates; enforcement-event links.

**How a contribution flows (I12).** Proposal (web form, API, or PR to the `clhear` repo vault) → automated checks (schema, rights, duplicates) → the relevant fleet re-derives with the proposal as evidence and reports agreement/disagreement with reasons → two reviewers accept → release includes it with attribution → contributor notified with impact ("your correction changed 41 blueprints").

**What contributors get.** Attribution on every accepted change and in release notes; reputation and rank by accepted impact, visible on profile; recognized-reviewer status and listing; early access to aggregate L8 benchmarks and to new clusters; free instance-mode seats on Reg42 OS for individual contributors; paid review bounties from the commercial side for high-value clusters; conference and research co-authorship; a say in the roadmap through working groups.

**What institutions get.** Working-group seats per cluster; conformance program and marks (CL1 Mapped → CL4 Automated; CL3/CL4 independently assessed under ISAE 3000); the ability to submit anonymized benchmarks and receive the aggregate; a public evals record they can cite to their regulator; no vendor lock-in — everything open is exportable.

**What Reg42 gets.** The best-checked registry in the market; the L8 network; the conversion funnel from free blueprint to instance mode; the standard's brand as the trust anchor for Reg42 OS and Solon.

**Tooling.** GitHub (`clhear` repo: standard, schema, vault packs, evals, SDKs; Discussions for RFCs); public roadmap; Discourse forum (Apache-2.0) for community; monthly open call; newsletter (beehiiv) with the change digest; Reg42's internal Zulip is not the community surface.

**Governance artefacts (published).** Charter; contribution guide; CLA (Apache-style, with patent grant limited to use of the standard); code of conduct; release policy; deprecation policy; conflict-of-interest rules; how the steering group votes; how a working group is formed.

**Licences.** Standard text and schemas: CC BY 4.0. Open data outputs (L2–L6 agnostic, L7 base): ODC-By (attribution) — chosen so banks and vendors can embed without copyleft fear. Evals harness and SDKs: Apache-2.0. Trademark "CLHEAR" held by Reg42 with a published usage policy (conformance marks only via the program). The patent (filed 11 Sep 2026) is licensed royalty-free for agnostic-mode use under the CLA's patent grant; commercial re-derivation engines are outside the grant.

---

## 7. Trust, security and legal

**7.1 Security posture.** SOC 2 Type I readiness by first design partner, Type II and ISO 27001 under the reseller engagement; SSO/SAML for enterprise accounts; API keys scoped per organization with rate limits; audit log of every read of licensed text and every write; signed releases (Sigstore), SBOM per release; dependency pinning; annual pen test with published summary; vulnerability disclosure policy; status page with SLOs (API 99.9 %, freshness ≤ 24 h tier-A); DR: cross-region S3, nightly Postgres and Neo4j restores tested monthly.

**7.2 Data handling.** Agnostic mode stores no organization data (I5) — verified by an automated PII/organization-identifier scan on every release. Instance mode runs in the client's account via Reg42 OS. Member benchmarks are k-anonymized (k ≥ 5) with noise; re-identification tests on every aggregate. GDPR/UK GDPR and Israeli Privacy Protection Law Amendment 13 records of processing for user accounts and contributor data.

**7.3 Rights and sourcing.** Rights basis recorded per source. Public-domain and open-licence texts (EU, UK, US federal, FATF, most regulators) shown verbatim. FINRA and similar: derived outputs only, sourced through the legally clean channel (SEC/EDGAR) — no verbatim republication. ISO 27001 and SOC 2 (AICPA TSC): licensed private ingestion for derivation; BYOL viewer (user proves licence, sees text); NIST public-domain spine as the open canonical infosec text with identifier crosswalks; similarity guard in CI on every derived output. Enforcement notices as sources under their publishers' terms.

**7.4 Model governance.** Bedrock-only via Reg42 Infer; frozen model ids and versions per release, published in the release's model manifest; procurement-clean defaults for derivation classes; per-client model policy in instance mode; golden sets and gates per layer public; drift alarms; second-model review on every L2 determination; human review on low confidence.

**7.5 Independence.** Steering group with institution seats; published evals; open outputs; conformance assessment by independent assessors; Reg42's commercial layers clearly separated (I9).

---

## 8. Build order and acceptance

Each item ends with a test. Items marked **NYC** are the 28 Sep demo scope: agnostic mode, one profile (global retail broker with UK/EU/US licences), L1–L6 live with evidence chains, Solon front door, public evals for L1/L2.

| # | Item | Acceptance |
|---|---|---|
| 1 | L0: shared schema, why-trails, bi-temporal columns, EventBridge topics, release pipeline with signing, model manifest per release | a release snapshot verifies (signature, hash) and lists frozen model ids |
| 2 | Infer task classes for CLHEAR (`l1_parse`, `l1_change`, `l2_extract`, `l2_consolidate`, `l2_change`, `l3_decompose`, `l3_characterize`, `l4_enumerate`, `l5_map`, `l6_explain`, `l7_score`, `l8_fill`, `judge`) with procurement-clean ladders | `/v1/route/explain` returns a ladder per class; Chinese-origin models absent from derivation classes |
| 3 | **NYC** L1 hardened for the starter corpus (MiFIR/MiFID II UK+EU, MAR, MLRs 2017 family, FCA Handbook selected sourcebooks, SEC/FINRA via EDGAR, FATCA statute + 26 CFR + Rev. Proc., GDPR, NIST spine) with families, change detection, scorecards | byte fidelity 100 %; family completeness scorecard published; a synthetic amendment is detected and dated within one cycle |
| 4 | **NYC** L2 registry on the starter corpus with `asserts`, equivalences, change inference, second-model review, coverage evals | coverage ≥ 99 % on the audited set; precision ≥ 95 % on the expert sample; a clause change propagates to obligations with correct dates |
| 5 | **NYC** L3 blocks and characteristic schemas; harmonization | every obligation → ≥ 1 block; characteristic completeness ≥ 95 %; block explosion check |
| 6 | **NYC** L4 ontology for UK/EU/US broker regimes; profile builder; validity rules | no invalid permutation offered on the golden set; applicability P/R ≥ 95 % on Meridian Markets |
| 7 | **NYC** L5 activities junction | no orphans; expert precision ≥ 92 % |
| 8 | **NYC** L6 composer with minimality proof, explanations, diff | completeness 100 %; minimality check passes; reference agreement ≥ 90 % on Meridian Markets |
| 9 | **NYC** Public UI: Solon front door → blueprint in < 60 s; layer browsers; why-trail on every node; change feed; API keys; evals dashboard for L1/L2 | demo script runs end to end from a fresh browser |
| 10 | Neo4j projection + bidirectional query; pgvector index; nightly rebuild from Postgres | obligation → evidence and evidence → obligation each in one query < 300 ms |
| 11 | Approval console: low-confidence queue, modification requests, contribution review; re-derivation with human edits | a human edit is reproduced by the next cycle or escalated |
| 12 | L7: enforcement ingestion, linking, calibrated scoring, method page | Brier score published on a held-out year |
| 13 | Community: `clhear` repo with standard, schema, vault packs, evals harness, SDKs; CLA; contribution flow with attribution; Discourse; roadmap | an external contributor's correction flows to a release with attribution and impact count |
| 14 | L8: fill generation, review, maturity, member aggregates with k-anonymity tests | endorsed fills ≥ 85 % on rubric; re-identification test passes |
| 15 | Conformance program: Annex E self-assessment protocol, CL3/CL4 assessor guide, marks and usage policy | first CL1 self-assessment completed by a design partner |
| 16 | Interoperability: OSCAL export, NIST CSF / CCM / ISO crosswalks, JSON-LD | a blueprint imports into an OSCAL-consuming GRC tool without loss |
| 17 | Security: SSO/SAML, audit log, SBOM, pen test, status page, DR drills | SOC 2 Type I readiness evidence pack complete |
| 18 | Instance mode contract with Reg42 OS: Actual overlay API, gap diff, instance priorities — runs only in the client account | agnostic store scan finds zero organization identifiers after an instance-mode session |

Items 1–2 first. 3–9 for NYC, in order. 10–11 immediately after. The rest in order.

---

## 9. Never-list

Client or organization data in the agnostic store · verbatim text without a recorded rights basis · a determination without a why-trail · deletion of any node or edge (invalidate instead) · a layer reading a higher layer · a public release below a layer's gate · Chinese-origin open weights in derivation classes without an explicit client policy · any inference outside Bedrock / Reg42 Infer · a contribution written directly to the store · L8 fills or member benchmarks exposed in agnostic mode · re-identifiable benchmark data · quoting licensed text beyond the rights basis · a conformance mark outside the program · a public disclosure of CLHEAR before the provisional filing is confirmed · offense/defense as schema terms · a second graph or vector store without a benchmark showing the gap · asserting accuracy without a published method.

---

## 10. One paragraph for the README

CLHEAR is the open data standard for compliance programs: a live, eight-layer system that turns the world's regulations into one proven-complete obligation registry, decomposes every obligation into the concrete things an organization must have and do, and composes — for any regulated organization, in under a minute — the leanest complete compliance program with evidence from every item back to the verbatim source. Every determination is versioned, dated, explained and reproducible. The agnostic blueprint is free and open; the engine that keeps it current, the instance mode that shows your gaps, and the benchmarks that fill it are how Reg42 makes a living. Anyone who checks it makes it better, and is credited for doing so.
