# The standard

- [`CLHEAR-STANDARD.md`](CLHEAR-STANDARD.md) — the eight layers, the twelve invariants, the shared record shape, identifiers, openness, evals, conformance, interoperability. Licence CC BY 4.0.
- `../schema/` — JSON Schema (draft 2020-12) for every record table, generated from the reference implementation on each release; `schema/index.json` lists them per layer.
- `../vault/<release>/` — the same records as an Obsidian vault.

Requirement identifiers `CLHEAR-<layer>.<n>` are stable; the reference implementation's requirement trace maps each to code and tests.

Changes to the standard are RFCs (Discussions → RFCs) decided by the steering group under `../governance/STEERING_VOTING.md`.
