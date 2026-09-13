# Contributing to CLHEAR

The full guide lives in [`governance/CONTRIBUTING.md`](governance/CONTRIBUTING.md).
The short version:

1. Sign the [CLA](governance/CLA.md) once (web: `/contribute`; API: `POST /cla/sign`).
2. File a contribution — a correction, a missing source, an equivalence, a
   characteristic, an ontology entry, a fill, a translation, a golden case, an
   evidence template or an enforcement link — from `/contribute`, the SDKs, or a
   pull request using the `contribution` block in the PR template.
3. Automated checks run (schema, target exists, no verbatim restricted text, no
   duplicate or no-op), the layer's fleet re-derives your proposal from the
   source, and two reviewers decide. Authors never review their own work.
4. Accepted contributions are written through the record path — versioned,
   with a why-trail, nothing deleted — and ship in the next release with your
   handle and the count of blueprints and obligations they changed.

Conduct: [`governance/CODE_OF_CONDUCT.md`](governance/CODE_OF_CONDUCT.md).
Licences: [`LICENSES/`](LICENSES/README.md).
