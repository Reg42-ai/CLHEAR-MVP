# CLHEAR conformance program

Licence: CC BY 4.0. Programme owner: the CLHEAR steering group (`../governance/CHARTER.md`); operated by Reg42 on its behalf.

A compliance program — not a product — claims conformance with the CLHEAR standard at one of four levels. Each level says something a regulator, a board or a counterparty can rely on:

| Level | Mark | Claim | How it is verified |
|---|---|---|---|
| **CL1 Mapped** | CLHEAR CL1 Mapped | Every item of the organisation's agnostic blueprint is operated with a named owner, or declared out of scope for a stated fact. | Self-assessment under Annex E, verified by the program |
| **CL2 Traceable** | CLHEAR CL2 Traceable | CL1, and every operated item has evidence traceable to the obligations it satisfies; deviations are recorded with rationale; releases are reconciled within 30 days. | Self-assessment under Annex E, verified by the program |
| **CL3 Assessed** | CLHEAR CL3 Assessed | CL2, tested by an accredited independent assessor over ≥ 6 months. | ISAE 3000 (Revised) reasonable-assurance engagement, `ASSESSOR_GUIDE.md` |
| **CL4 Automated** | CLHEAR CL4 Automated | CL3, and the program is kept current by machine. | ISAE 3000 (Revised), `ASSESSOR_GUIDE.md` § CL4 |

Files:

- [`LEVELS.md`](LEVELS.md) — the four levels, what each adds, what each does not claim.
- [`ANNEX_E_SELF_ASSESSMENT.md`](ANNEX_E_SELF_ASSESSMENT.md) — the self-assessment protocol for CL1 and CL2: steps, the form, the criteria, how the program verifies.
- [`criteria.json`](criteria.json) — the criteria, machine-readable. The web form at https://clhear.org/conformance and `POST /conformance/self-assessments` evaluate against this file; it is the single source of truth.
- [`self_assessment.schema.json`](self_assessment.schema.json) — JSON Schema of the submitted form.
- [`ASSESSOR_GUIDE.md`](ASSESSOR_GUIDE.md) — CL3/CL4 assessor guide: engagement, subject matter, testing, sampling, report form, accreditation of assessors.
- [`MARKS_POLICY.md`](MARKS_POLICY.md) — marks and usage policy: what a granted mark permits, scope statements, validity, withdrawal, the public register.
- [`evidence_templates/`](evidence_templates/) — templates for the evidence CL2+ asks for, per block kind. Contributed templates arrive through the contribution flow (kind `evidence_template`).

## Process

1. **Compose.** Build your profile at https://clhear.org/build (L4) and take the current agnostic blueprint (L6, a `BLU-` id) for a named release.
2. **Map.** For every blueprint item (`ITM-`): operated — with owner, and from CL2 evidence and deviations — or out of scope with a factual reason.
3. **Submit.** The form at `/conformance` (or the API) runs the automated checks in `criteria.json` immediately and shows the level the evidence supports.
4. **Verify (CL1/CL2).** A program verifier reviews the mapping, exclusions and evidence references and grants or declines the mark. Verification is recorded with the verifier's name.
5. **Assess (CL3/CL4).** Engage an assessor from the accredited register (`GET /conformance/assessors`). The assessor lodges the signed report; the program records the mark.
6. **Register.** Every granted mark appears in the public register at https://clhear.org/conformance/register with scope, level, release and validity. Only that register is authoritative (`../governance/TRADEMARK_POLICY.md`).

Marks are valid for 12 months from grant and are withdrawn when the scope changes materially, the assessment lapses, or the organisation asks. Nothing on the register is ever deleted; withdrawn marks stay visible as withdrawn (I2).

## What conformance does not mean

A mark says the program is mapped, traceable, assessed or automated against the CLHEAR standard for a named release and scope. It is not legal advice, does not certify compliance with any law, and does not replace the judgement of the organisation's regulator. The standard's method and evals are public so the claim can be checked (`../evals/`).
