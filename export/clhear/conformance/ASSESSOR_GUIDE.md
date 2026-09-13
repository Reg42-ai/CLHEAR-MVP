# Assessor guide — CL3 Assessed and CL4 Automated

Licence: CC BY 4.0. Version 1.0. For accredited assessors performing an ISAE 3000 (Revised) reasonable-assurance engagement whose criteria are Annex E of the CLHEAR standard.

## 1. Engagement

**Subject matter.** The organisation's compliance program within a stated scope, as mapped to a named CLHEAR release and blueprint (`BLU-` id, fingerprint fixed for the period).

**Criteria.** [`ANNEX_E_SELF_ASSESSMENT.md`](ANNEX_E_SELF_ASSESSMENT.md) / [`criteria.json`](criteria.json) at the version current at the engagement start. Criteria are suitable under ISAE 3000 ¶ A45–A50: relevant, complete, reliable, neutral, understandable; they are public and versioned.

**Responsible party.** The organisation's accountable signatory (E.5.2). **Intended users.** The organisation's board and regulators, the CLHEAR program (for the register), counterparties reading the register.

**Independence.** The assessor is independent of the organisation *and of Reg42 Ltd* under the IESBA Code Part 4B. Reg42 staff, contractors and steering-group members may not assess. An assessor who contributed to the blueprint's derivation (accepted contributions on the obligations in scope in the last 24 months) discloses this and declines if the contribution is material to the scope.

**Level of assurance.** Reasonable. CL3 covers design and operating effectiveness over a period of at least six months (E.7.2). CL4 adds the automation controls in Area 6 over the same period.

## 2. Planning

1. Obtain the self-assessment at CL2 (a CL3 engagement requires a granted or concurrently submitted CL2) and the program's automated-check results.
2. Fix the release and blueprint: record `release`, `blueprint_id`, `fingerprint`, and the item list with `obligations_satisfied` per item.
3. Materiality: an item is material if it is load-bearing for any obligation in scope (`load_bearing_for` non-empty in the minimality proof) or its block kind is Role, Body or Document at the program level. All material items are tested; the rest sampled (§ 4).
4. Understand the organisation's change process (E.4) and, for CL4, its automation (Area 6).

## 3. Procedures per area

| Area | Procedures |
|---|---|
| 1 Scope | Agree the scope statement to legal-entity and licence records; confirm the profile predicates against those records (E.1.2). A wrong predicate is a scope limitation — report it and stop until corrected. |
| 2 Mapping | Re-perform `items_mapped`: reconcile the blueprint's item list to the mapping. Inspect every out-of-scope reason for factual basis (E.2.4); corroborate a sample against records (e.g. "no retail clients" against client-type data). Confirm owners exist as roles in the organisation chart. |
| 3 Traceability | For each tested item, obtain the evidence artefacts behind the references, confirm they exist, are the organisation's, and name (or can be traced to) every obligation in `obligations_satisfied` (E.3.2). Recompute deviations: compare the artefact's actual characteristic (e.g. the review cadence written in the policy) with the blueprint's and with the deviation register (E.3.3). |
| 4 Change | Obtain the release history for the period (`GET /v1/releases`, `GET /watch/feed`). For each release with a change affecting the scope, obtain the reconciliation record and compute the interval; any interval > 30 days is an exception (E.4.2). |
| 5 Governance | Inspect the review record (E.5.1) and the signatory's authority (E.5.2). |
| 6 Automation (CL4) | Observe the integration: application key, pinned release, upgrade change records (E.6.1). Re-perform diff ingestion on a release pair: the work items created by the system must match the blueprint diff (E.6.2). For continuous evidence, test that artefacts carry system timestamps distributed across the period, not clustered before the assessment (E.6.3). Test alerting: inspect configuration and a sample of alerts with response times ≤ 24 h (E.6.4). |
| 7 Assessment | Document independence, period, sampling and exceptions in the report (E.7.1–E.7.3). |

## 4. Sampling

Operating-effectiveness tests over the period: for a population of *n* operated non-material items, test max(10, ⌈√n⌉) selected at random with the seed recorded; all material items are tested. A single exception on a material item, or exceptions on more than 5 % of the sample, precludes an unqualified opinion at CL3; at CL4 a single failure of E.6.2 or E.6.4 precludes CL4 (a CL3 opinion may still be given).

## 5. Evidence handling

Assessors see the organisation's artefacts; the program does not. Lodge with the program only the report and the references tested. Do not upload artefacts to CLHEAR; the agnostic store holds no organisation data (I5) and the program's PII/organisation scan will reject a release that contains any.

## 6. Report

Form: ISAE 3000 (Revised) reasonable-assurance report, with the following elements in this order.

1. Title, addressee, and the CLHEAR mark applied for (CL3 or CL4).
2. Subject matter and scope: organisation, program, scope statement, CLHEAR release, blueprint id and fingerprint.
3. Criteria: "Annex E of the CLHEAR standard, version X.Y, available at https://clhear.org/conformance".
4. Responsibilities of the responsible party and of the assessor; independence statement including independence from Reg42 Ltd.
5. Summary of work: period, items tested (material and sampled, with counts), procedures by area.
6. Inherent limitations (including that conformance is not compliance with law).
7. Opinion: unqualified / qualified / adverse / disclaimer, at the level applied for; where qualified, the level the evidence supports.
8. Exceptions and their disposition.
9. Assessor's register id, signature, date, location.

Lodge the report through `POST /conformance/marks` (program staff record it against the assessor's register entry) or by e-mail to conformance@clhear.org. The program records the mark with the report reference; the report itself is not published unless the organisation chooses to publish it.

## 7. Accreditation of assessors

The register of accredited assessors (`GET /conformance/assessors`) lists individuals or firms who:

- are licensed to issue ISAE 3000 reports in their jurisdiction (or the national equivalent — SSAE 18/AT-C 105 & 205, ISAE (NZ) 3000, ASAE 3000);
- have completed the program's assessor briefing on the eight layers, the invariants and this guide;
- have declared conflicts under `../governance/CONFLICT_OF_INTEREST.md`;
- are not Reg42 staff, contractors or steering-group members.

Accreditation is granted by the steering group, is valid for 24 months, and is withdrawn for a report the program finds materially wrong. Withdrawn entries stay on the register as withdrawn.
