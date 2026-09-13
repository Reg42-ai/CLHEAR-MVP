# The CLHEAR Standard

Version 2 · licence CC BY 4.0 · normative requirement identifiers `CLHEAR-<layer>.<n>` are stable for life

CLHEAR is a data standard for compliance programs. It defines eight layers of records, the invariants every record obeys, the identifiers that make records citable, and the openness rule that says what is public. A conforming implementation produces records of these shapes with these guarantees; the reference implementation is operated by Reg42 and publishes dated, signed releases of the open layers in this repository.

The words *must*, *must not*, *should* and *may* are used as in RFC 2119.

## 1. The eight layers

Each layer is derived from the layers below it and never from a layer above it.

| Layer | Name | Records | What it answers |
|---|---|---|---|
| L1 | Verbatim Source Corpus | `sources`, `source_versions`, `clauses`, `families`, `citations`, `change_events` | What does the law say, verbatim, and when did it change? |
| L2 | Obligation Registry | `obligations` (`OBL-`), `asserts`, `equivalences`, `supersessions`, `change_events` | What does each clause require of whom, once, across jurisdictions? |
| L3 | Building Blocks | `blocks` (`BLK-`), `requires`, `characteristics` | What must an organization *have* — systems, documents, roles, configurations, processes, workflows, assets, bodies — and with what characteristics? |
| L4 | Profile Permutation Space | `licences`, `products_services`, `client_types`, `channels`, `permits`, `validity_rules`, `profiles` (`PRF-`), `applies_to` | Which organizations can exist, and which obligations apply to each? |
| L5 | Activities Junction | `activities` (`ACT-`), `implies`, `operates`, `mitigates` | What does an organization *do*, on the business side and the compliance side, and how do the two meet? |
| L6 | Blueprint Composer | `blueprints` (`BLU-`), `blueprint_items`, `minimality_proofs` | What is the leanest complete program for this profile, and why is every item in it? |
| L7 | Risk and Priority Scoring | `enforcement_events` (`ENF-`), `enforcement_links`, `risk_scores` (`RSK-`), `risk_calibrations` | What matters most, on published evidence, with a published and calibrated method? |
| L8 | Benchmarks and Fills | `fills` (`FIL-`), `benchmark_aggregates` | How do good programs fill each slot, and how does mine compare? |

### 1.1 L1 — Verbatim Source Corpus

Official texts fetched from authoritative publishers, split into clauses with byte-exact spans, stored immutably, watched forever. Every source carries a **rights basis** (`public_domain`, `open_licence`, `licensed`, `byol_only`, `derived_only`) recorded before any text is stored. Change detection records the **effective date** and its basis separately from the detection date. Enforcement notices are L1 sources in the *informative* tier: evidence for L7, never law for L2.

### 1.2 L2 — Obligation Registry

One deduplicated set of atomic obligations covering every normative clause, each anchored by an `asserts` edge with span and strength (explicit / implied), structured as determination · subject · action · condition · object, with effective dates, cross-jurisdiction `equivalences`, `supersessions` and change events that name their L1 cause. An obligation's stable id (`OBL-000123`) never changes; its derivation key (`OBL:<source>#<ref>`) says where it came from.

### 1.3 L3 — Building Blocks

Every obligation decomposes into one or more type-agnostic deliverables of exactly one of eight **kinds** — System, Document, Role, Configuration, Process, Workflow, Asset, Body — each with a fixed characteristic schema per kind (e.g. Process: trigger, performing role, system, cadence, output, record). A characteristic value is `backed` by obligation text, `not_specified` by the source, or `unbacked`; an unbacked value is never presented as fact. Blocks harmonise onto a canonical block; nothing is deleted.

### 1.4 L4 — Profile Permutation Space

The ontology of regulated organizations: jurisdictions → regulators → licences → permitted products and services → client types → channels, with validity rules for impossible combinations and every licence citing the public register it was read from. A **profile** is a valid attribute set with a stable id. `applies_to` edges give every obligation a conjunctive predicate over profile attributes, each conjunct carrying the words it was read from.

### 1.5 L5 — Activities Junction

Activities have a **side** — `business` (onboarding, order handling, marketing, payments, custody, advice, lending, data processing, outsourcing, distribution) or `compliance` (screen, monitor, investigate, report, notify, train, attest, assess, record, control, test) — and an action type from that closed vocabulary. `implies` (product → business activity), `operates` (compliance activity → block) and `mitigates` (compliance activity → business activity, lit by obligations) form the junction. *Offense* and *defense* are narrative metaphors and must not appear as schema terms.

### 1.6 L6 — Blueprint Composer

For any valid profile, the leanest complete program: every applicable obligation satisfied by at least one item, every item load-bearing for at least one obligation (the **minimality proof**), gaps surfaced and never hidden, an explanation per item that cites nothing outside the blueprint. A blueprint is composed in under a minute and exportable as OSCAL.

### 1.7 L7 — Risk and Priority Scoring

Enforcement events are read from public notices with only what the notice prints; they link to obligations only where the notice cites the provision. Scores have six published dimensions with published weights (`enforcement_history`, `likelihood`, `financial_impact`, `reputational_impact`, `operational_impact`, `regulatory_attention`), a composite, a band, and a reference to the calibration run that fitted the likelihood parameters. A calibration publishes its Brier score against a base-rate baseline on a held-out year. Base priorities and the method are open; instance priorities need the organization's own data and are not.

### 1.8 L8 — Benchmarks and Fills

Fills give L3 slots best-practice content (policy text, procedure steps, typology sets, workflow definitions) with provenance (`agent`, `contributor`, `member-benchmark`) and maturity (`draft`, `reviewed`, `endorsed`). Member benchmarks are aggregated with k ≥ 5 and differential-privacy noise, and re-identification-tested on every aggregate. Fill content is a member layer; fill *existence* and maturity are public metadata.

## 2. Invariants

Every conforming record store must satisfy all twelve.

| # | Invariant | Requirement |
|---|---|---|
| I1 | Layers are derived strictly in order | L(n) reads only L(n−1) and below. Every why-trail names its input layers; a why-trail citing the same or a higher layer is invalid. |
| I2 | Every node and edge is versioned and dated | `id` stable for life; `version` increments; `valid_from` / `valid_to` are regulatory effect dates; `derived_at` is derivation time. Nothing is deleted; it is invalidated. |
| I3 | Every determination has a why-trail | Inputs hash, model manifest, skill version, reasoning summary, confidence, evals run id. No trail, no write. |
| I4 | Full AI determination, human-in-the-loop by exception | Humans intervene below a confidence threshold or on a modification request. Accepted human edits are re-derived on the next cycle and must be reproduced or escalated, never silently overwritten. |
| I5 | Agnostic mode contains no organization's data | The open store is provably free of instance data; an automated identifier scan runs on every release. |
| I6 | Inference is governed | Model ids and versions are frozen per release and published in the model manifest; derivation classes use procurement-clean defaults. |
| I7 | One record, rebuildable projections | The relational record is the truth; the query graph and vector index are rebuildable from it. |
| I8 | Rights before text | Verbatim text is stored and shown only under a recorded rights basis; derived outputs never quote beyond what the basis allows. |
| I9 | Open by mode, not by layer | Agnostic outputs of L1 (pointers, hashes, verbatim where rights allow) through L6, and L7 base priorities and method, are open. Instance mode, L8 fill content and the derivation engine are not. |
| I10 | Evals gate publication | No layer output enters a release below its gate; drift below gate freezes that layer's publication and alarms. |
| I11 | Stable identifiers | `CLHEAR-<layer>.<n>` for requirements; `OBL- BLK- PRF- ACT- BLU- RSK- FIL- ENF- CON-` for objects, six digits, never reused. |
| I12 | Contributions never write directly | A contribution is a proposal; automated checks run; the fleet re-derives with it as evidence; two reviewers accept; the release includes it with attribution. |

## 3. The shared record shape

Every layer table carries these columns (`schema/` gives the JSON Schema per table):

```
id              text        stable, prefixed (I11)
version         integer     increments on every change
valid_from      date        regulatory effect; null = not yet in force
valid_to        date        null = in force; set on invalidation, never deleted
derived_at      timestamp   derivation time
derived_by      text        agent id + skill version
model_manifest  object      model id, version, parameters
inputs_hash     text        hash of the inputs the determination was made on
confidence      number      0..1
why_trail_id    text        → why_trails.id (I3)
review          array       human review events, contribution references
jurisdictions   array       ISO 3166 / "EU"
```

A **why-trail** carries `layer`, `subject_ref`, `reasoning_summary`, `evidence_refs`, `inputs_hash`, `model_manifest`, `skill_version`, `confidence`, `evals_run_id`, `agent_id` and the input layers it read.

## 4. Identifiers

`<PREFIX>-<six or more digits>`, assigned from a monotonic sequence per prefix and never reused. A record keeps its id across versions; a re-derived record is a new version of the same id. Requirement ids `CLHEAR-<layer>.<n>` identify clauses of this standard and the reference implementation's trace.

## 5. Openness

Open outputs are published in dated releases under ODC-By 1.0 (data), CC BY 4.0 (standard text and schemas) and Apache-2.0 (harness and SDKs). Anyone may read, export, embed and build on them without an account. Access controls apply to modes (instance, member), never to layers.

## 6. Evals

Each layer has a public gate: named suites with thresholds and golden sets kept in `evals/golden/`. A release lists, per layer, the suites run and whether the gate passed; a layer below its gate is *reserved* in that release. Contributed golden cases (`evals/contributed/`) enter a gate only after two reviewers accept them.

## 7. Conformance

An implementation or a compliance program may claim conformance at four levels — CL1 Mapped, CL2 Traceable, CL3 Assessed, CL4 Automated — under the conformance program (`conformance/`). CL1 and CL2 are verified self-assessments; CL3 and CL4 are assessed by an accredited independent assessor under ISAE 3000. Conformance marks are granted only through the program (`governance/TRADEMARK_POLICY.md`).

## 8. Interoperability

Blueprints export as OSCAL system security plans and the block catalogue as an OSCAL component definition; identifier crosswalks to NIST CSF, CSA CCM and ISO 27001, a JSON-LD context and a GraphQL endpoint are published as they land (`ROADMAP.md`).
