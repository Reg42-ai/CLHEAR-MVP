# How a working group is formed

Version 1.0 · licence CC BY 4.0

A working group owns a **cluster**: a jurisdiction, a regime, a layer concern or a crosswalk (examples: *UK AML*, *EU markets (MiFID II / MiCA)*, *US broker-dealer*, *infosec crosswalks (NIST CSF / CCM / ISO 27001)*, *L8 policy fills*). It is where jurisdiction expertise turns into accepted contributions, golden cases and reviewer capacity.

## Forming

1. **Proposal.** Any Contributor opens an RFC (GitHub Discussion, *RFCs* category, template `working-group`) naming the cluster, its scope in terms of instruments and layers, the expected outputs for the first six months, and at least three prospective members including one Reviewer.
2. **Convener.** A Maintainer of the cluster (or, for a new cluster, a Reviewer who will be nominated for Maintainer) agrees to convene.
3. **Vote.** The steering group confirms by simple majority (`STEERING_VOTING.md`).
4. **Charter page.** The group publishes a one-page charter in `governance/working-groups/<slug>.md`: scope, members, convener, meeting cadence, decision rule.

## Working

- Meets at least monthly, in the open (call link in the forum; notes in the Discussion thread).
- Decides by consensus of members present; the convener records disagreements and, where consensus fails, escalates to the steering group.
- Owns the golden sets for its cluster: proposes cases, reviews contributed cases, and signs off changes to the gate thresholds (which the steering group ratifies).
- Reviews contributions in its cluster; two of its Reviewers accept.
- Reports quarterly to the steering group: contributions accepted, gate status, reviewer capacity, open questions.

## Membership

Open to any Contributor. Reviewer and Maintainer roles within the group follow the charter's role rules. A member inactive for six months is moved to *alumni* on the charter page (the record of their contributions stays).

## Institutions

Regulated institutions may hold a **working-group seat** per cluster through a named representative; the seat carries no extra vote but guarantees agenda time and early access to the cluster's aggregate benchmarks when the L8 layer publishes them.

## Dissolving

A working group dissolves when its cluster is complete and stable (no open contributions for six months and gate green for three releases) or on its own motion; the steering group confirms. Its golden sets and charter page remain.
