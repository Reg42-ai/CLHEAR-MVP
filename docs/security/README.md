# Security programme (HLD v2 §7.1; build item 17)

Everything an auditor, a design partner's second line or a security researcher needs,
in one directory. The live index is `/security` on the service; `/.well-known/security.txt`
is the machine-readable contact.

| Document | What it is |
|---|---|
| [`SOC2_TYPE_I_READINESS.md`](SOC2_TYPE_I_READINESS.md) | The evidence pack: every Trust Services Criterion in scope → control → code → test → status. Item 17's acceptance criterion. |
| [`VDP.md`](VDP.md) | Vulnerability disclosure policy with safe harbour and fix SLAs. |
| [`PENTEST_SCOPE.md`](PENTEST_SCOPE.md) | Scope, rules of engagement and deliverables for the annual penetration test. |
| [`policies/INCIDENT_RESPONSE.md`](policies/INCIDENT_RESPONSE.md) | Severity ladder, roles, procedure, notification duties. |
| [`policies/VULNERABILITY_MANAGEMENT.md`](policies/VULNERABILITY_MANAGEMENT.md) | Sources of findings, targets, dependency pinning and SBOM rules. |
| [`policies/BUSINESS_CONTINUITY.md`](policies/BUSINESS_CONTINUITY.md) | RPO/RTO per component, the nightly DR drill, the region-loss runbook. |
| [`policies/VENDOR_MANAGEMENT.md`](policies/VENDOR_MANAGEMENT.md) | Vendor register with data seen, assurance and fallback. |
| [`policies/RISK_MANAGEMENT.md`](policies/RISK_MANAGEMENT.md) | Method and standing risks with treatments. |
| [`policies/DATA_CLASSIFICATION.md`](policies/DATA_CLASSIFICATION.md) | Data classes, handling rules, records of processing. |

## Controls in code

| Control | Code | Test |
|---|---|---|
| Audit log — every write, every licensed read, every mutating request | `app/clhear/platform/audit.py`, `app/clhear/platform/audit_middleware.py` | `tests/test_audit.py` |
| Enterprise SSO — SAML federation, routed by email domain | `infra/cognito.tf`, `app/clhear/accounts.py` (`/auth/sso`) | `tests/test_metrics.py` |
| Status page, SLOs, Prometheus | `app/clhear/platform/metrics.py`, `app/clhear/v1/security.py`, `status/.upptimerc.yml`, `infra/observability.tf` | `tests/test_metrics.py` |
| DR drill | `app/clhear/platform/dr.py` (`scripts/dr_drill.py`), `.github/workflows/dr_drill.yml`, `infra/eventbridge.tf` | `tests/test_dr_drill.py` |
| Supply chain — lock, SBOM, signing | `requirements.lock`, `.github/workflows/ci.yml`, `.github/workflows/release.yml`, `scripts/verify_release.py` | `tests/test_supply_chain.py` |
| Rights before text; similarity guard | `app/clhear/l1/rights.py`, `app/clhear/l1/guard.py` | `tests/test_rights.py` |

Incident reviews, when there are any, go in `incidents/<date>-<slug>.md`. Pen-test
executive summaries go in `PENTEST_SUMMARY_<year>.md`.
