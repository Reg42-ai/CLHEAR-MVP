# Annex E — Self-assessment protocol (CL1 Mapped, CL2 Traceable)

Licence: CC BY 4.0. Version 1.0. This Annex is normative for CL1 and CL2 and is the criteria document for CL3 and CL4 assessments. Criteria are identified `E.<area>.<n>` and published machine-readably in [`criteria.json`](criteria.json).

## E.0 Purpose

A self-assessment lets an organisation state, on its own authority and with a signatory, how its compliance program stands against the CLHEAR agnostic blueprint composed for it. The program verifies the statement against the blueprint the organisation names, not against the organisation's evidence itself (which stays with the organisation — I5). The result is a granted, declined or withdrawn mark on the public register.

## E.1 Before you start

1. Build the profile at https://clhear.org/build and confirm every predicate (jurisdictions, licences, products, client types, channels). A wrong predicate makes the blueprint wrong and the assessment void.
2. Take the current blueprint for a named release: `GET /l6/blueprints/{BLU-id}` or the `blueprint` SDK call. Record the release id and the blueprint's fingerprint.
3. Export the item list (`composition.items`): every `ITM-` id with its block, kind, characteristics and `obligations_satisfied`.
4. Read [`evidence_templates/`](evidence_templates/) for the evidence a CL2 assessment expects per block kind.

## E.2 The form

Submit at https://clhear.org/conformance or `POST /conformance/self-assessments` with a body that validates against [`self_assessment.schema.json`](self_assessment.schema.json):

| Field | CL1 | CL2 | Notes |
|---|---|---|---|
| `organization`, `program`, `scope` | required | required | Scope names entities, business lines, jurisdictions and exclusions with reasons (E.1.3). |
| `release`, `blueprint_id` | required | required | E.1.1, E.1.2. |
| `profile_confirmed` | required `true` | required | The signatory confirms the predicates describe the organisation. |
| `level_claimed` | `CL1` | `CL2` | The level you are asking the program to verify. |
| `items[]` | required | required | One entry per blueprint item: `item_id`, `status` (`operated` \| `out_of_scope`), `owner` (operated), `reason` (out_of_scope). |
| `items[].evidence[]` | — | required for operated | `{ref, kind, obligation_ids[]}`; `ref` is a document id, URL, system name or file hash — never the content. |
| `items[].deviations[]` | — | as applicable | `{characteristic, blueprint_value, program_value, rationale}` (E.3.3). |
| `change_tracking` | — | required | `{watchlist_id?, feed?, sdk?, last_reconciled_release}` (E.4.1). |
| `reconciliation_days` | — | required, ≤ 30 | E.4.2. |
| `last_review` | — | required | `{date, reviewer_role, outcome}` (E.5.1). |
| `signatory` | required | required | `{name, role, date}` (E.5.2). |

## E.3 Criteria

### Area 1 — Scope
- **E.1.1 Named release.** The self-assessment names the CLHEAR release the program is mapped to.
- **E.1.2 Named profile and blueprint.** The program is composed from a stored profile and its current blueprint; the organisation confirms the predicates.
- **E.1.3 Scope statement.** Entities, business lines and jurisdictions covered; exclusions with reasons.

### Area 2 — Mapping (CL1)
- **E.2.1 Complete blueprint.** The blueprint covers 100 % of the obligations applicable to the profile in the named release. The program checks this against the L6 completeness gate; a blueprint below the gate cannot anchor a mark.
- **E.2.2 Every item mapped.** Every `ITM-` is `operated` or `out_of_scope`. Missing items fail the criterion.
- **E.2.3 Named owners.** Every operated item has an owner (a role).
- **E.2.4 Out-of-scope items justified.** A reason is a fact about the organisation ("no retail clients", "no EU entity"), never a judgement about the obligation's importance. Verifiers decline reasons of the second kind.

### Area 3 — Traceability (CL2)
- **E.3.1 Evidence per item.** At least one evidence reference per operated item.
- **E.3.2 Evidence traces to obligations.** Every operated item's evidence, taken together, names every obligation in that item's `obligations_satisfied`.
- **E.3.3 Characteristics compared.** Each deviation from a blueprint characteristic carries a rationale.

### Area 4 — Change (CL2)
- **E.4.1 Release tracking.** A watchlist, feed subscription or SDK integration, and the last reconciled release.
- **E.4.2 Reconciliation window.** ≤ 30 days.

### Area 5 — Governance
- **E.5.1 Annual review (CL2).** A recorded review within the last 12 months.
- **E.5.2 Accountable signatory (CL1).** Name, role and date.

Areas 6 (Automation) and 7 (Assessment) apply to CL4 and CL3 and are verified by the assessor — see [`ASSESSOR_GUIDE.md`](ASSESSOR_GUIDE.md).

## E.4 Automated checks

On submission the program evaluates every criterion with an `automated_check` in `criteria.json` and returns, per criterion, `ok`, `detail` and the level the evidence supports (`level_supported`). The checks are deterministic and published; they are necessary, not sufficient: a verifier still reads the mapping.

| Check | Fails when |
|---|---|
| `release_named` | no release id |
| `blueprint_current` | the `BLU-` id is unknown or superseded |
| `scope_stated` | scope shorter than 40 characters or without a jurisdiction |
| `blueprint_complete` | the blueprint's coverage is below 100 % |
| `items_mapped` | any blueprint item is missing from `items[]` or has an unknown status |
| `owners_named` | an operated item has no owner |
| `exclusions_reasoned` | an out-of-scope item has no reason, or the reason argues importance ("not material", "low risk", "not relevant") |
| `evidence_per_item` | an operated item has no evidence reference |
| `evidence_traces` | an operated item's evidence does not name all its `obligations_satisfied` |
| `deviations_reasoned` | a deviation has no rationale |
| `change_tracking` | no watchlist / feed / SDK and last reconciled release |
| `reconciliation_window` | `reconciliation_days` missing or > 30 |
| `review_recorded` | no review in the last 12 months |
| `signed` | signatory name, role or date missing |

## E.5 Verification and grant

1. A program verifier (maintainer or steering role) reads the submission with the checks' results.
2. The verifier grants the mark at the level supported (which may be lower than claimed), declines with a note, or asks for changes. Every decision is recorded with the verifier's name and is visible to the submitter.
3. A granted CL1/CL2 mark enters the register with the scope, release, blueprint and a 12-month validity.
4. The organisation re-submits on material change to its profile, when the annual review falls due, or to move up a level.

## E.6 What the program stores

The form as submitted (references, not artefacts), the checks, the verifier's decision and the mark. No document contents, no client data. Submissions and decisions are never deleted; a withdrawn mark stays visible as withdrawn (I2).
