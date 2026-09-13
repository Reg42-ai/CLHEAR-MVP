# Evidence templates

Licence: CC BY 4.0. One template per L3 block kind. A CL2 self-assessment references artefacts that follow these templates; CL3/CL4 assessors test the artefacts against them. The templates say what an artefact must *show*, not how it must look — an existing policy that carries the fields qualifies.

Every artefact, whatever its kind, carries:

- **Identity** — a stable reference (document id, system name + object id, URL, or a SHA-256 of the file) — this is what goes in the self-assessment's `evidence[].ref`. The content stays with the organisation.
- **Obligations** — the CLHEAR obligation ids (`OBL-` / stable ids) the artefact evidences, or a pointer that resolves to them (the blueprint item id `ITM-` is enough when the item's `obligations_satisfied` covers them all).
- **Owner and date** — the accountable role and the date the artefact was produced or last approved.
- **Period** — for operating evidence, the period the artefact covers.

| Block kind | Template | What it evidences |
|---|---|---|
| Document | [`document.md`](document.md) | a policy or standard exists, is approved, current and owned |
| Process / Workflow | [`process.md`](process.md) | a procedure operates: trigger, performer, cadence, record |
| Role | [`role.md`](role.md) | a role is appointed, senior enough, independent, competent |
| Body | [`body.md`](body.md) | a committee exists, meets, has quorum and mandate |
| System / Configuration | [`system.md`](system.md) | a control system is configured as the characteristics require |
| Asset | [`asset.md`](asset.md) | a register, fund or record is held and maintained |

Contribute a better template (kind `evidence_template`) through `/contribute`; it ships here after two reviewers accept it, with your name.
