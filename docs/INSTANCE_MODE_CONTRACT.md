# CLHEAR instance-mode contract with Reg42 OS

Version `1.0.0` · method `instance-priority-1.0` · HLD v2 I5, I9, §4.6, §4.7, §8 item 18
Code: `app/clhear/instance_contract.py` · API: `app/clhear/v1/instance.py` · OpenAPI: `GET /instance/contract/openapi.json`

CLHEAR runs in two modes. **Agnostic** — any profile, no company data — is the open
product: the blueprint, its evidence chain and the base priorities. **Instance** — a real
organization's *Actual overlay*, its *gaps* and its *instance priorities* — is closed and
is delivered only through Reg42 OS, running in the client's own AWS account. This document
is the contract between the two. *We give away the diagnosis; we sell the cure.*

## 1. Where it runs

| | Agnostic (public CLHEAR) | Instance (Reg42 OS, client account) |
|---|---|---|
| Deployment | `clhear.reg42.ai`; `CLHEAR_MODE=agnostic` | client VPC; `CLHEAR_MODE=instance` **and** `CLHEAR_INSTANCE_ACCOUNT_ID=<client account>` |
| Holds | the record (L1–L8), releases, community, audit | a read-only copy or pinned release of the record **plus** the organization's overlay, evidence, owners |
| Instance endpoints | `403 instance_only` — body never read | served, inside a read-only session |
| Organization identifiers | **zero** (I5, scanned every release) | the client's own, in the client's account |

`GET /instance/guard` reports whether a deployment may serve instance mode. The guard
requires all three: instance mode set, the client account pinned, and the pinned account
equal to the account the process actually runs in (STS `GetCallerIdentity`). Terraform for
the public deployment never sets `CLHEAR_MODE` or `CLHEAR_INSTANCE_ACCOUNT_ID`
(`tests/test_agnostic_scan.py::test_public_infra_never_enables_instance_mode`).

## 2. What is open and what is closed

Open (I9): the L6 blueprint composition for any profile; the L7 base priority per
obligation and per item with its published method (`/l7/method`); this contract, its
shapes and OpenAPI; the instance-priority formula.

Closed: the Actual overlay; the gap diff; the instance priorities; evidence locators,
owners and notes. None of these is stored, logged, audited or published by CLHEAR.

## 3. Shapes

All shapes are Pydantic models in `app/clhear/instance_contract.py`; JSON Schema is
embedded in `GET /instance/contract` and in the OpenAPI document.

### `ActualOverlay` (input)

```json
{
  "contract_version": "1.0.0",
  "blueprint_id": "BLU-000123",            // or "profile": {"attributes": {...}} to compose on the fly (not stored)
  "release": "2026.09.13",
  "as_of": "2026-09-12",
  "actuals": [
    {"block_id": "BLK-AML-CDD-PROCEDURE", "state": "present",  "evidence_refs": ["sharepoint://…"], "owner": "Head of Compliance"},
    {"block_id": "BLK-CASS-RECONCILIATION", "state": "partial", "characteristics_met": ["frequency"]},
    {"block_id": "BLK-CONFLICTS-POLICY",   "state": "planned", "target_date": "2026-12-31"},
    {"block_id": "BLK-RECORD-RETENTION",   "state": "absent"}
  ]
}
```

`state` ∈ `present | partial | absent | planned | not_applicable`. A blueprint item with
no actual is treated as `absent` and counted as `unassessed`. Duplicate `block_id`s are
rejected (422).

### `GapDiff` (output of `POST /instance/overlay/diff`)

Per blueprint item: `state`, `gap` (true for partial / absent / planned),
`missing_characteristics` (profile-relevant characteristic keys not met),
`obligations_at_risk` (the obligations the item satisfies, exposed by the gap),
`load_bearing_for` (obligations only this item satisfies — from the open minimality
proof), `base_priority` / `base_band` (open L7), `explanation`.
Summary: counts per state, `unassessed`, `gaps`, `coverage_ratio = present / (items −
not_applicable)`, `obligations_total`, `obligations_at_risk`. `unknown_blocks` lists
actuals for blocks not in the blueprint (ignored, reported). `overlay_hash` is
`sha256` of the canonical overlay **without** owners, notes, evidence locators or
dates, so a client can pin the input it sent without disclosing it.

### `InstancePriorities` (output of `POST /instance/overlay/priorities`)

Gaps ranked by

```
instance_priority = min(1, base_priority × gap_weight[state] × required_boost × (1 + 0.05 × |load_bearing_for|))
gap_weight = {absent: 1.0, partial: 0.5, planned: 0.25, present: 0.0, not_applicable: 0.0}
required_boost = 1.25 when the item is an L3-required block, else 1.0
base_priority = open L7 item composite (0..1); unscored items assume 0.5 (flagged base_assumed)
```

Ties break on more obligations at risk, then block id. Every item carries its `factors`
so Reg42 OS can show *why* something is first. The formula and constants are part of the
contract and are returned by `GET /instance/contract`.

## 4. Guarantees (I5)

1. **Read-only session.** Every computation runs inside `instance_contract.session`: a
   transaction that is always rolled back, followed by a fingerprint comparison (row count
   + content hash) of every layer table, `why_trails`, `events` and `audit_log`. Any
   difference raises `StoreLeak` and the request fails with 500 — never a 200 that left
   data behind.
2. **Nothing of the overlay is persisted.** Not as rows, not in events, not in audit
   detail (the HTTP audit row carries method, path, status, duration only), not in logs.
3. **Composition on the fly is not stored.** A profile-based overlay composes with
   `composer.compose_with` (pure) and returns no `blueprint_id`.
4. **Scanned every release.** `app/clhear/platform/agnostic_scan.py` runs on every
   release artefact set (`release.yml`) and — in CI — on the store after a full
   instance-mode session with a synthetic organization's identifiers
   (`tests/test_agnostic_scan.py::test_agnostic_store_has_zero_identifiers_after_an_instance_session`).
   Acceptance: **zero identifiers**.
5. **Public node cannot serve instance mode.** The guard needs a pinned account matching
   the running account; the public Terraform sets neither variable.

## 5. Versioning

`contract_version` follows semver. Additive fields are minor; a change to a state name,
the formula or a guarantee is major and ships with a migration note in the public repo's
`CHANGELOG`. Reg42 OS sends `contract_version` in every overlay; CLHEAR answers with the
version it computed under.

## 6. Reg42 OS obligations under this contract

* Deploy CLHEAR instance nodes only in the client's account; never point one at the public
  record with write credentials.
* Keep evidence, owners, notes and the overlay in the client's own store; CLHEAR is a
  function over them, not a home for them.
* Pin the CLHEAR release (`release`) the overlay was assessed against; re-run the diff
  when a new release changes the blueprint (the change feed says when).
* Surface `base_assumed` and `unassessed` to users — an unscored or unassessed item is
  a question, not a finding.
