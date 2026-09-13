# Contributing to CLHEAR

Thank you. Every accepted contribution is attributed to you on the record and in the release notes, and you are told what it changed.

## Before your first contribution

1. Read the `CODE_OF_CONDUCT.md`.
2. Sign the Contributor License Agreement once: https://clhear.org/contribute (the *Sign the CLA* button) or `POST /cla/sign`. Signing grants the Contributor role. The CLA is Apache-style with a patent grant limited to use of the standard — see `CLA.md`.

## What you can contribute

| Kind | Target | What `proposed` carries |
|---|---|---|
| `correction` | an obligation (`OBL-…`) or block (`BLK-…`) | `field`, `value` |
| `missing_source` | — | `url`, `title`, `jurisdiction`, optional `regulator`, `rights` |
| `equivalence` | — | `obligation_a`, `obligation_b` |
| `characteristic` | a block | `key`, `value` (keys are the block kind's fixed schema) |
| `ontology_entry` | — | `collection` (licences / products_services / client_types / channels), `name`, `jurisdiction` |
| `fill` | a block | `text` (policy language, procedure, typology set) |
| `translation` | an obligation | `language`, `text` |
| `golden_case` | — | `suite`, `case` (`{id, …, expected}`) |
| `evidence_template` | — | `conformance_level`, `text` |
| `enforcement_link` | an enforcement event (`ENF-…`) | `obligation_id`, plus a `quote` in `evidence` |

Always add `evidence`: `[{"url": "...", "quote": "..."}]`. The quote must be from a source whose rights allow it (see *Rights* below).

## How a contribution flows

```
proposal ──▶ automated checks ──▶ fleet re-derivation ──▶ two reviewers ──▶ release ──▶ impact
 web form      schema · rights ·   the layer's fleet       accept (never    shipped with   "your correction
 API · PR      duplicates          re-reads the source     the author)      attribution    changed 41 blueprints"
                                   with your proposal
                                   as evidence and
                                   reports agree /
                                   disagree with reasons
```

1. **Automated checks** run on submission: the proposal is well-formed for its kind; the target exists and the field is editable; no verbatim text from a source without a republication basis; no open duplicate and no no-op.
2. **Re-derivation**: the fleet responsible for the layer re-reads the source with your proposal as evidence and records *agree*, *disagree* or *unverified* with reasons. A *disagree* does not block you — reviewers see both.
3. **Review**: two distinct Reviewers (or Maintainers) accept. A single reject closes the contribution with a note; you may resubmit with more evidence. Reviewers never review their own contributions.
4. **Applied**: acceptance writes through the record path — versioned, why-trailed, recorded as a human edit under both reviewers' names. Contributions never write to a layer table directly (invariant I12).
5. **Released**: the next dated release ships it. The release notes and the record carry your attribution; your profile shows rank by accepted impact.
6. **Impact**: you are notified with what it reached — blueprints and obligations changed.

## Channels

- **Web**: https://clhear.org/contribute
- **API**: `POST /contributions` with your session or Cognito token; `GET /contributions/{id}` shows checks, the fleet's verdict and reviews. SDKs: `client.contribute(...)`.
- **Pull request** to this repository against `vault/<release>/` or `evals/`: the PR body must contain a `contribution:` block with the same fields; a maintainer files it through the same flow and links the PR.

## Rights

Quote only from sources whose rights basis allows republication (public-domain and open-licence instruments: EU, UK, US federal, FATF, most regulators). For licensed or derived-only sources (FINRA rulebook text, ISO, SOC 2) cite the provision reference and describe the change — do not paste text. The rights check will reject verbatim restricted text.

## Style for corrections

- One field per contribution.
- The value should be readable in the clause: the reader grounds it word by word.
- Say why in `rationale`: "regulation 28(3) applies to the *relevant person*, not the *customer*".

## Reviewers

Reviewers are verified compliance, legal or audit professionals. To become one, ask a maintainer in the forum or via `governance@clhear.org`; the steering group confirms. Recognized reviewers are listed on the contributors page.

## Recognition

Attribution on every accepted change and in release notes; reputation and rank by accepted impact on your profile; recognized-reviewer status; early access to aggregate L8 benchmarks and new clusters; free instance-mode seats on Reg42 OS for individual contributors; review bounties on high-value clusters; co-authorship on research; a say in the roadmap through working groups.
