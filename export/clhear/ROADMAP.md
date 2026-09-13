# CLHEAR public roadmap

Licence CC BY 4.0. Items follow the build order of the standard's high-level design (§8). Status: `shipped` · `in progress` · `planned`. Priorities move by steering-group vote; propose changes as an RFC in Discussions.

| # | Item | Status | Notes |
|---|---|---|---|
| 1 | L0 platform: bi-temporal record, why-trails, stable identifiers, event bus, signed releases with model manifest | shipped | every write carries a why-trail; nothing is deleted |
| 2 | Inference through Reg42 Infer on Bedrock only; procurement-clean derivation ladders | shipped | frozen model ids per release |
| 3 | L1 verbatim source corpus — starter corpus (UK, EU, US, FATF, BIS, IOSCO, MAS, ASIC, ISA) with rights basis, spans, change detection | shipped | currency and boundary gates |
| 4 | L2 obligation registry with asserts, equivalences, supersessions, change events, second-model review | shipped | coverage, precision, dedupe, change-inference gates |
| 5 | L3 building blocks (eight kinds) with characteristics and harmonisation | shipped | completeness, characteristics, reuse gates |
| 6 | L4 profile permutation space, guided builder, applicability predicates | shipped | validity and applicability gates |
| 7 | L5 activities junction (business · compliance) | shipped | junction completeness gate |
| 8 | L6 blueprint composer with minimality proofs, explanations, diff, OSCAL | shipped | completeness, minimality, reference-agreement gates |
| 9 | Public interface: describe → blueprint in under a minute; explore, learn, watch, build on it; evals dashboard; WCAG 2.2 AA | shipped | no key, no paywall on the agnostic blueprint |
| 10 | Query graph (Neo4j) and vector index, rebuilt nightly from the record | shipped | < 300 ms both directions |
| 11 | Approval console — low-confidence queue, modification requests, human edits reproduced each cycle | shipped | |
| 12 | L7 enforcement-linked, calibrated risk priorities with a published method and Brier score | shipped | linker precision ≥ 90 % gate |
| 13 | Community: this repository, contribution flow with attribution and impact, governance artefacts, licences, forum, newsletter digest | in progress | public push gated on the disclosure confirmation |
| 14 | L8 fills and member benchmarks (k ≥ 5, differential-privacy noise, re-identification test) | planned | members-only API; public "fills available" metadata |
| 15 | Conformance program CL1–CL4, self-assessment protocol, assessor guide (ISAE 3000), marks | planned | |
| 16 | Interoperability: OSCAL component definitions, NIST CSF / CCM / ISO 27001 crosswalks, JSON-LD context, GraphQL | planned | round-trip into an OSCAL-consuming GRC tool without loss |
| 17 | Security: SAML federation, audit log, SBOM, dependency pinning, VDP, status page with SLOs, DR drills, SOC 2 Type I pack | planned | |
| 18 | Instance-mode contract with Reg42 OS; agnostic-store identifier scan on every release | planned | runs only in the client's account |

## Working groups wanted

- UK AML (MLRs 2017, JMLSG) — reviewers with FCA supervision experience
- EU markets — MiFID II / MiCA / DORA practitioners
- US broker-dealer — FINRA / SEC compliance and examination backgrounds
- Infosec crosswalks — NIST CSF 2.0, CSA CCM v4, ISO 27001:2022

## How to influence this roadmap

Open an RFC (Discussions → RFCs). The steering group votes on priorities quarterly (`governance/STEERING_VOTING.md`); working groups report what their cluster needs (`governance/WORKING_GROUPS.md`).
