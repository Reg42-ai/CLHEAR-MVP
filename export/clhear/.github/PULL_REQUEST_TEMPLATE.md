<!--
A pull request against vault/ or evals/ is a contribution (channel: pr). A maintainer files it through the
same flow every contribution follows — automated checks, fleet re-derivation, two reviewers, release with
attribution — and links this PR. Fill the block below so it can be filed without guesswork.
-->

```contribution
kind: correction | missing_source | equivalence | characteristic | ontology_entry | fill | translation | golden_case | evidence_template | enforcement_link
layer: L2
target_ref: OBL-000123
field: subject
proposed:
  value: "relevant person"
evidence:
  - url: https://www.legislation.gov.uk/uksi/2017/692/regulation/28
    quote: "A relevant person must ..."
rationale: >
  Regulation 28(3) addresses the relevant person, not the customer.
```

- [ ] I have signed the CLA (`governance/CLA.md`).
- [ ] Quoted text comes from a source whose rights basis allows republication, or I cite the provision only.
- [ ] One field per contribution.
