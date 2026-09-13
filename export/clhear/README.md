# CLHEAR — the open data standard for compliance programs

CLHEAR is the open data standard for compliance programs: a live, eight-layer system that turns the world's regulations into one proven-complete obligation registry, decomposes every obligation into the concrete things an organization must have and do, and composes — for any regulated organization, in under a minute — the leanest complete compliance program with evidence from every item back to the verbatim source. Every determination is versioned, dated, explained and reproducible. The agnostic blueprint is free and open; the engine that keeps it current, the instance mode that shows your gaps, and the benchmarks that fill it are how Reg42 makes a living. Anyone who checks it makes it better, and is credited for doing so.

## What is in this repository

| Path | What | Licence |
|---|---|---|
| `standard/` | The standard: the eight layers, the invariants every record obeys, the identifier scheme, the openness rule | CC BY 4.0 |
| `schema/` | JSON Schema for every record table, per layer, generated from the reference implementation | CC BY 4.0 |
| `vault/<release>/` | Obsidian vault packs — obligations, blocks and activities as linked notes | ODC-By 1.0 |
| `snapshots/<release>/` | The allow-list release snapshot (refs, hashes, family graphs, eval scores) | ODC-By 1.0 |
| `evals/` | Golden sets per layer, contributed cases, the scoring harness, and each release's gate scores | Apache-2.0 (harness) · CC BY 4.0 (cases) |
| `sdks/` | Python and TypeScript clients for the public API | Apache-2.0 |
| `governance/` | Charter, contribution guide, CLA, code of conduct, release / deprecation / conflict-of-interest policies, how the steering group votes, how a working group forms, trademark policy | CC BY 4.0 |
| `ROADMAP.md` | The public roadmap, by build item | CC BY 4.0 |
| `RELEASE_NOTES/` | What shipped in each release, with contributor attribution and impact | CC BY 4.0 |

Releases are dated (`YYYY.MM.DD`), signed (Sigstore) and carry a model manifest. A layer appears in a release only when its evals gate passed; below-gate layers are reserved, not published.

## Use it

- Browse the live standard: https://clhear.org — describe your organization, get a blueprint, follow every item to its source.
- API: https://clhear.org/build — the agnostic blueprint needs no key; a key adds rate and scope.
- SDKs: `pip install clhear` · `npm install @reg42/clhear` (source in `sdks/`).
- Evals: `python evals/harness.py --golden evals/golden/l7/linker/core.json --predictions my_links.json`.

## Contribute

Corrections to obligations and mappings, missing sources, equivalence edges, block characteristics, ontology entries, fills, translations, golden cases, conformance evidence templates and enforcement links — all flow through one path: **proposal → automated checks → the fleet re-derives with your proposal as evidence → two reviewers accept → the next release ships it with your name and the impact it had**. Read `governance/CONTRIBUTING.md`, sign the CLA (`governance/CLA.md`) once, then use the web form at https://clhear.org/contribute, the API (`POST /contributions`), or a pull request against `vault/` or `evals/`.

RFCs are GitHub Discussions in the *RFCs* category (`.github/DISCUSSION_TEMPLATE/rfc.yml`). The community forum runs on Discourse; the monthly open call and the change digest newsletter are announced there.

## Governance in one line

Reg42 is the originating creator and a steering-group member; the steering group is vendor-neutral by rule, the evals are public, everything open is exportable, and the conformance program (CL1 Mapped → CL4 Automated) is independently assessed at CL3/CL4. See `governance/CHARTER.md`.
