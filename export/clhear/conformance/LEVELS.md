# Conformance levels CL1–CL4

Licence: CC BY 4.0. Normative criteria live in [`criteria.json`](criteria.json); this file explains them. A level is achieved only when every criterion at that level **and every lower level** is met for the whole assessed scope.

## CL1 Mapped — "we know what the standard asks of us, and who owns each part"

**Adds.** A named CLHEAR release, profile and blueprint (`E.1.1`–`E.1.3`); a complete blueprint (`E.2.1`); every blueprint item mapped to an operated program element with an owner, or declared out of scope for a stated fact about the organisation (`E.2.2`–`E.2.4`); an accountable signatory (`E.5.2`).

**Verified by** the program: a verifier reviews the mapping and exclusions against the blueprint and grants or declines. Automated checks run on submission (release named, blueprint current and complete, every item mapped, owners named, exclusions reasoned, form signed).

**Does not claim** that any item operates, that evidence exists, or that the program is current after the release named.

**Typical time.** A profile of 40–120 items maps in a few working sessions using the blueprint export.

## CL2 Traceable — "we can show evidence for each part, back to the source"

**Adds.** Evidence for every operated item (`E.3.1`) that traces to the obligations the item satisfies (`E.3.2`, HLD v2 I3 applied to the organisation's own program); a deviation register comparing the program's characteristics with the blueprint's (`E.3.3`); release tracking and a ≤ 30-day reconciliation window (`E.4.1`, `E.4.2`); annual review (`E.5.1`).

**Verified by** the program: a verifier samples evidence references and deviations. Evidence itself stays with the organisation; the program sees references, hashes or excerpts, never client data (I5).

**Does not claim** that an independent party tested the operation of items.

## CL3 Assessed — "an independent assessor tested it over time"

**Adds.** An ISAE 3000 (Revised) reasonable-assurance engagement by an assessor on the accredited register, independent of the organisation and of Reg42 (`E.7.1`), testing the mapping's completeness, the traceability of evidence and the operation of a sample of items over at least six months (`E.7.2`), reported in the program's form (`E.7.3`). The subject matter is the organisation's program; the criteria are this Annex; the CLHEAR release and blueprint are fixed for the period.

**Verified by** the assessor's signed report lodged with the program. The program records the mark; it does not re-perform the assessment.

**Does not claim** compliance with any law, nor that the program will stay current without the CL2 change process.

## CL4 Automated — "the program keeps itself current"

**Adds.** The program consumes CLHEAR by machine against a pinned release (`E.6.1`), ingests blueprint diffs automatically (`E.6.2`), produces evidence continuously from systems rather than assembling it for the assessment (`E.6.3`), and alerts an owner within 24 hours of a deviation or a lapsed item (`E.6.4`). The assessor tests these automation controls in addition to CL3.

Instance mode of a Reg42 OS deployment is one way to meet CL4; it is not the only way. The criteria are stated in terms of behaviour, not product, so any implementation that consumes the open API and SDKs can qualify.

## Scope, validity, downgrade

A mark names its scope (entities, business lines, jurisdictions), the CLHEAR release and blueprint assessed, and a 12-month validity. A material change to the organisation's profile, a lapse of the annual review, or a failed reconciliation window withdraws CL2+ until re-assessed. A withdrawn mark stays on the register as withdrawn.
