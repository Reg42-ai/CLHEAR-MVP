# CLHEAR Charter

Version 1.0 · effective with the first public release · licence CC BY 4.0

## 1. Purpose

CLHEAR exists so that any regulated organization can obtain, for free, a proven-complete and minimal compliance program with evidence from every item back to the verbatim source, and so that the registry behind it is checked by the people who know the rules best. The standard's credibility comes from who checks it, not who wrote it.

## 2. Principles

1. **Open by mode, not by layer.** Everything in agnostic mode — sources, obligations, blocks, profiles, activities, blueprints, base risk priorities, evals — is open. Instance mode (an organization's own data) and member benchmarks are not, and never leak into the open store.
2. **Evidence before opinion.** Every determination carries a why-trail; every change is versioned and dated; nothing is deleted.
3. **Evals gate publication.** A layer is published only when its public golden sets pass; drift below the gate freezes the layer.
4. **Rights before text.** Verbatim text appears only where the source's rights basis allows it.
5. **Vendor neutrality.** No vendor — including Reg42 — gains a preference in the standard, the schema or the conformance program by virtue of commercial position.

## 3. Roles

| Role | Who | How obtained | Powers |
|---|---|---|---|
| Reader | anyone | none | read, export, cite, build on it |
| Contributor | anyone who signed the CLA | sign `CLA.md` once | propose corrections, sources, mappings, fills, translations, evals cases, conformance templates, enforcement links |
| Reviewer | verified compliance, legal or audit professionals | nominated by a maintainer, confirmed by the steering group; identity and professional standing verified | review contributions — two reviewers accept a contribution; never their own |
| Maintainer | per cluster (jurisdiction, regime or layer); Reg42 staff and elected reviewers | elected by the cluster's reviewers annually; Reg42 appoints at most half | run the fleet for the cluster, decide escalations, grant reviewer role, curate golden sets |
| Steering group | Reg42 as originating creator plus representatives of institutions and firms | see §4 | charter, licences, conformance program, working groups, roadmap priorities |

Role grants are recorded with the granting identity and a validity window; revocations close the window and the record stays.

## 4. Steering group

- Seven to eleven seats. Reg42 holds two seats permanently as originating creator. At least three seats are held by regulated institutions and at least two by firms or individuals with no commercial relationship to Reg42. No single organization other than Reg42 holds more than one seat.
- Members serve two-year terms, staggered; a member may serve at most two consecutive terms.
- The steering group elects a chair (not from Reg42) for one year.
- Vendor neutrality is a rule: a member with a conflict on a matter (see `CONFLICT_OF_INTEREST.md`) declares it and does not vote.
- Voting: see `STEERING_VOTING.md`.

## 5. Working groups

A working group owns a cluster (e.g. *UK AML*, *EU markets*, *US broker-dealer*, *infosec crosswalks*). It is formed as described in `WORKING_GROUPS.md`, has a maintainer as convener, meets monthly in the open, and reports to the steering group quarterly.

## 6. What Reg42 commits to

- Operate the fleets and publish a dated, signed release at least weekly.
- Keep the agnostic blueprint free and without a paywall.
- Publish the golden sets, the gate results and the model manifest of every release.
- Hold the trademark for the standard's benefit under `TRADEMARK_POLICY.md`, and license the patent royalty-free for agnostic-mode use under the CLA.
- Separate its commercial layers (instance mode, benchmarks, Reg42 OS) visibly from the standard.

## 7. Amendments

This charter is amended by a two-thirds vote of the steering group after a public RFC open for at least 30 days. Changes to §2 (principles) or §6 (Reg42's commitments) additionally require that no more than one institution seat votes against.
