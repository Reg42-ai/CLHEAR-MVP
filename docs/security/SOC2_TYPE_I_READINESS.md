# SOC 2 Type I Readiness — Evidence Pack

HLD v2 §7.1 and §8 item 17: *SOC 2 Type I readiness by first design partner; Type II
and ISO 27001 under the reseller engagement.* A Type I report is an auditor's
opinion, at a point in time, that the controls are **suitably designed** and
**implemented**. This pack is what we hand the auditor: for each Trust Services
Criterion in scope, the control we claim, where it is implemented, and the evidence
that proves it exists on the date of the report.

Scope: the CLHEAR service (`clhear.reg42.ai`), its public artefacts and the AWS
account that runs it. Trust Services Categories: **Security** (Common Criteria,
mandatory), **Availability**, **Confidentiality**. Processing Integrity and Privacy
are out of scope for Type I and are noted at the end.

Legend — Status: `ready` (control implemented, evidence in this repo or linked),
`external` (control lives outside the repo; the evidence owner is named),
`gap` (not yet implemented; the closing action is named). `scripts/trace_check.py`
verifies that every repo path cited here exists.

## How evidence is produced

Almost every control below is *enforced by code and proven by a test*, which is the
strongest evidence a Type I auditor can get: the control cannot be skipped without
CI failing. Where a control is a policy or a human process, the policy is in
`docs/security/policies/` and the record of its operation is in the audit log,
GitHub history or the named external system.

| Evidence type | Where | How the auditor verifies |
|---|---|---|
| Code that enforces the control | `app/`, `infra/`, `.github/workflows/` | read the cited path at the release tag |
| Test that proves it | `tests/` | `pytest tests/<file>::<test>` on the tag; CI run for the tag |
| Audit log | `l0_platform.audit_log` via `/audit` | maintainer export for the report date |
| Policy | `docs/security/policies/*.md` | version history in git |
| External system | AWS console, GitHub settings, Discourse | screenshots dated on the report date, owner named |

## CC1 — Control environment

| Criterion | Control | Implementation | Evidence | Status |
|---|---|---|---|---|
| CC1.1 Integrity and ethical values | Published governance and code of conduct binding staff and contributors | `export/clhear/governance/CODE_OF_CONDUCT.md`, `export/clhear/governance/CHARTER.md`, `export/clhear/governance/CONFLICT_OF_INTEREST.md` | git history; Discourse acceptance log | ready |
| CC1.2 Board oversight | Steering group with institution seats; vendor-neutral by rule | `export/clhear/governance/CHARTER.md` | minutes (external, Steering secretary) | external |
| CC1.3 Structures and reporting lines | Roles Reader → Contributor → Reviewer → Maintainer → Steering with defined grants | `app/clhear/community_models.py` (`ROLES`), `app/clhear/platform/contributions.py` (`GRANTING_ROLES`) | `tests/test_community.py` | ready |
| CC1.4 Competence | Reviewer = verified compliance/legal/audit professional; assessor accreditation register | `app/clhear/conformance.py` (assessors), `export/clhear/conformance/ASSESSOR_GUIDE.md` | `tests/test_conformance.py` | ready |
| CC1.5 Accountability | Every write attributed to an actor; maintainer actions in the audit log | `app/clhear/platform/audit.py`, `app/clhear/platform/audit_middleware.py` | `tests/test_audit.py` | ready |

## CC2 — Communication and information

| Criterion | Control | Implementation | Evidence | Status |
|---|---|---|---|---|
| CC2.1 Quality information | Bi-temporal record with why-trails; evals gates before publication | `app/clhear/platform/record.py`, `app/clhear/platform/gates.py` | `tests/test_record.py`, `tests/test_gates.py` | ready |
| CC2.2 Internal communication | Security policies in repo; incident runbook | `docs/security/policies/` | git history | ready |
| CC2.3 External communication | Status page, security page, VDP, release notes, deprecation policy | `app/clhear/v1/security.py`, `docs/security/VDP.md`, `export/clhear/governance/DEPRECATION_POLICY.md`, `export/clhear/governance/RELEASE_POLICY.md` | `tests/test_metrics.py` | ready |

## CC3 — Risk assessment

| Criterion | Control | Implementation | Evidence | Status |
|---|---|---|---|---|
| CC3.1 Objectives | SLOs defined and measured | `app/clhear/platform/metrics.py` (`SLOS`) | `/status.json`; `tests/test_metrics.py` | ready |
| CC3.2 Risk identification | Annual risk assessment; pen-test scope reviewed each release | `docs/security/policies/RISK_MANAGEMENT.md`, `docs/security/PENTEST_SCOPE.md` | risk register (external, CISO) | external |
| CC3.3 Fraud risk | Contributions never write directly; two-reviewer accept; disclosure gate | `app/clhear/platform/contributions.py` | `tests/test_community.py`, `tests/test_never_list.py::test_disclosure_gate` | ready |
| CC3.4 Change risk | Never-list lint in CI; requirement trace must stay honest | `tests/test_never_list.py`, `scripts/trace_check.py` | CI run | ready |

## CC4 — Monitoring activities

| Criterion | Control | Implementation | Evidence | Status |
|---|---|---|---|---|
| CC4.1 Ongoing evaluation | Per-layer evals gates on every release; freshness and gate status public | `app/clhear/platform/evals.py`, `app/clhear/platform/metrics.py` | `/evals`, `/metrics`; `tests/test_gates.py` | ready |
| CC4.2 Deficiency communication | Below-gate events and CloudWatch alarms; status page state | `app/clhear/platform/gates.py` (`freeze_below_gate`), `infra/cloudwatch.tf` | alarm history (external, on-call) | ready |

## CC5 — Control activities

| Criterion | Control | Implementation | Evidence | Status |
|---|---|---|---|---|
| CC5.1 Control selection | Invariants I1–I12 each traced to code and test | `docs/HLD_V2_TRACE.md` | `scripts/trace_check.py` in CI | ready |
| CC5.2 Technology controls | Infrastructure as code, validated in CI | `infra/`, `.github/workflows/ci.yml` (terraform validate) | CI run | ready |
| CC5.3 Policies and procedures | Security policy set | `docs/security/policies/` | git history | ready |

## CC6 — Logical and physical access

| Criterion | Control | Implementation | Evidence | Status |
|---|---|---|---|---|
| CC6.1 Access security | Cognito user pool; per-org API keys with scopes; maintainer allow-list; sessions HMAC-signed with TTL | `infra/cognito.tf`, `app/clhear/api_keys.py`, `app/clhear/accounts.py`, `app/clhear/settings.py` (`maintainer_set`) | `tests/test_api_keys.py`, `tests/test_metrics.py` | ready |
| CC6.1 Enterprise SSO | SAML 2.0 federation per enterprise, routed by email domain | `infra/cognito.tf` (`aws_cognito_identity_provider.saml`), `app/clhear/accounts.py` (`/auth/sso`) | `tests/test_metrics.py::test_enterprise_sso_routes_by_email_domain` | ready |
| CC6.2 Registration and authorisation | Membership grants by maintainers, recorded; CLA before contribution | `app/clhear/platform/mode.py`, `app/clhear/v1/l8.py` (`/l8/members`), `app/clhear/community_models.py` (`cla_signatures`) | `tests/test_l8_fills.py`, `tests/test_community.py` | ready |
| CC6.3 Access removal | Membership revocation; API key revocation; audit trail of both | `app/clhear/v1/l8.py` (`revoke_member`), `app/clhear/api_keys.py` (`revoke`) | `tests/test_l8_fills.py`, `tests/test_api_keys.py` | ready |
| CC6.4 Physical access | AWS data centres | AWS SOC 2 report (inherited) | AWS Artifact | external |
| CC6.5 Disposal | Object Lock data lake; never-delete record; no customer data in agnostic store | `infra/s3.tf`, `app/clhear/platform/record.py` (`DeletionForbidden`), `app/clhear/platform/agnostic_scan.py` | `tests/test_record.py::test_no_delete_anywhere`, `tests/test_never_list.py` | ready |
| CC6.6 External threats | API Gateway in front of the web tier; rate limits per key; GraphQL depth/list bounds | `infra/webui.tf`, `app/clhear/api_keys.py`, `app/clhear/interop/graphql_api.py` | `tests/test_interop_standard.py` | ready |
| CC6.7 Data in transit | TLS only (API Gateway, Cognito hosted UI); SDKs default to https | `infra/webui.tf`, `export/clhear/sdks/` | configuration screenshot | ready |
| CC6.8 Malicious software | Pinned, hash-checked dependencies; SBOM per release; signed release manifest | `requirements.lock`, `.github/workflows/ci.yml`, `.github/workflows/release.yml` (syft, cosign), `scripts/verify_release.py` | CI run; `tests/test_supply_chain.py` | ready |

## CC7 — System operations

| Criterion | Control | Implementation | Evidence | Status |
|---|---|---|---|---|
| CC7.1 Vulnerability management | Annual pen test with published summary; VDP with fix SLAs; dependency pinning reviewed per release | `docs/security/PENTEST_SCOPE.md`, `docs/security/VDP.md`, `docs/security/policies/VULNERABILITY_MANAGEMENT.md` | pen-test letter (external); GitHub `security` issues | ready |
| CC7.2 Anomaly monitoring | Audit log of every write, licensed read and mutating request; Prometheus metrics; CloudWatch alarms | `app/clhear/platform/audit.py`, `app/clhear/platform/metrics.py`, `infra/cloudwatch.tf`, `infra/observability.tf` | `tests/test_audit.py`, `tests/test_metrics.py` | ready |
| CC7.3 Incident evaluation | Incident response runbook with severity ladder | `docs/security/policies/INCIDENT_RESPONSE.md` | incident records (external) | ready |
| CC7.4 Incident response | Same runbook; status page communication; VDP disclosure terms | `docs/security/policies/INCIDENT_RESPONSE.md`, `app/clhear/web/status.html` | — | ready |
| CC7.5 Recovery | Nightly DR drill (in-VPC via EventBridge + out-of-band via GitHub Actions): Postgres restore into scratch, Neo4j projection rebuilt and checksum-matched, datalake replica sampled; RPO/RTO recorded in `l0_platform.dr_drills` | `app/clhear/platform/dr.py`, `.github/workflows/dr_drill.yml`, `infra/eventbridge.tf`, `docs/security/policies/BUSINESS_CONTINUITY.md` | `tests/test_dr_drill.py`; `/status.json` `dr`; workflow artefacts (90 d) | ready |

## CC8 — Change management

| Criterion | Control | Implementation | Evidence | Status |
|---|---|---|---|---|
| CC8.1 Changes authorised, tested, approved | PR review; CI (tests, never-list, trace, a11y, terraform validate); release only on green gates; signed manifest | `.github/workflows/ci.yml`, `.github/workflows/release.yml`, `export/clhear/governance/RELEASE_POLICY.md` | CI history; branch protection screenshot (external) | ready |
| CC8.1 Model changes | Frozen model ids per release in the model manifest; procurement-clean ladders | `app/clhear/platform/manifest.py` | `tests/test_never_list.py::test_derivation_ladders_are_procurement_clean` | ready |

## CC9 — Risk mitigation

| Criterion | Control | Implementation | Evidence | Status |
|---|---|---|---|---|
| CC9.1 Business disruption | Cross-region S3 replication; Aurora backups (35 days); DR drill | `infra/s3.tf`, `infra/rds.tf`, `.github/workflows/dr_drill.yml` | drill history | ready |
| CC9.2 Vendors | Bedrock-only inference through Reg42 Infer; vendor list with data-handling terms | `docs/security/policies/VENDOR_MANAGEMENT.md`, `app/clhear/platform/gateway.py` (`InferProvider`) | `tests/test_never_list.py::test_only_infer_provider_in_prod` | ready |

## A — Availability

| Criterion | Control | Implementation | Evidence | Status |
|---|---|---|---|---|
| A1.1 Capacity | Serverless web tier; ECS fleets per layer; CloudWatch alarms | `infra/webui.tf`, `infra/ecs.tf`, `infra/cloudwatch.tf` | — | ready |
| A1.2 Backup and recovery | Aurora automated backups (35 d); Object-Locked lake with cross-region replication; nightly restore drill | `infra/rds.tf`, `infra/s3.tf`, `app/clhear/platform/dr.py` | `l0_platform.dr_drills`; `dr-drill-failed` alarm | ready |
| A1.3 Recovery testing | Drill asserts row counts per layer table, identical migration ledger, resolvable why-trails, graph checksum equality and replica integrity on the restored copy; measures RPO/RTO against targets | `app/clhear/platform/dr.py` (`verify_record`, `verify_graph`, `verify_datalake`) | `tests/test_dr_drill.py` | ready |
| SLOs | API 99.9 % / 30 d; L1 freshness ≤ 24 h; derived ≤ 48 h; gates green; DR drilled ≤ 48 h | `app/clhear/platform/metrics.py`, `status/.upptimerc.yml`, `infra/observability.tf` (AMP, Grafana, GlitchTip, alarms) | `/status`; Upptime history; `tests/test_metrics.py` | ready |

## C — Confidentiality

| Criterion | Control | Implementation | Evidence | Status |
|---|---|---|---|---|
| C1.1 Identification | Rights basis per source; licensed text served only with a basis; reads audited | `app/clhear/l1/rights.py`, `app/clhear/v1/l1.py`, `app/clhear/platform/audit.py` | `tests/test_rights.py`, `tests/test_audit.py` | ready |
| C1.1 Member data | L8 fills and benchmarks members-only; k ≥ 5 with noise; re-identification test | `app/clhear/l8/aggregate.py`, `app/clhear/platform/mode.py` | `tests/test_l8_fills.py` | ready |
| C1.2 Disposal | No organisation data in the agnostic store (I5); scan on every release | `app/clhear/platform/agnostic_scan.py`, `.github/workflows/release.yml` | `tests/test_never_list.py` | ready |
| Data classification | Classification and handling rules | `docs/security/policies/DATA_CLASSIFICATION.md` | — | ready |

## Out of scope for Type I (noted for Type II / ISO 27001)

- **Processing Integrity** — the evals gates and why-trails are the natural
  controls; they will be sampled over the Type II period.
- **Privacy** — GDPR / UK GDPR and Israeli PPL Amendment 13 records of processing
  for user accounts and contributor data (`docs/security/policies/DATA_CLASSIFICATION.md`
  §Personal data). Formal DPIA is the reseller engagement's first deliverable.
- **Operating effectiveness over time** — Type II only. The audit log, CI history
  and drill history are already accumulating the evidence.

## Gaps and owners

| Gap | Closing action | Owner |
|---|---|---|
| Branch protection and required reviews are GitHub settings, not code | Screenshot on report date; consider `settings.yml` via probot | Maintainers |
| Risk register and Steering minutes live outside the repo | Named owners above; export on report date | CISO / Steering secretary |
| First external pen test not yet performed | Engage against `PENTEST_SCOPE.md` before the design-partner report date | CISO |
| Upptime repository not yet created | Create from `status/.upptimerc.yml`; link from `/status` | Maintainers |

## Auditor's checklist for the report date

1. Check out the release tag; confirm `git tag -v` / cosign verification of `manifest.json`.
2. Run `pytest tests/ -q` and `python scripts/trace_check.py --summary`; attach output.
3. Export `/audit/summary?since=<report date − 30 d>`; attach.
4. Capture `/status.json` and the Upptime history page.
5. Capture the latest `dr_drill` workflow run and its artefact.
6. Collect the external screenshots named above, dated.
