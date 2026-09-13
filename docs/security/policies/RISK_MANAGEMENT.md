# Risk Management

Owner: CISO, reporting to the Steering group. Annual assessment; interim review after
any SEV1/SEV2 or change of scope.

## Method

Risks are stated as *threat → asset → consequence*, scored on likelihood (1–5) and
impact (1–5), and mapped to the control that treats them. The register lives outside
this repository (it names people and partners); this file fixes the method and the
standing risks so an auditor can see the reasoning is stable.

## Standing risks and treatments

| Risk | Consequence | Treatment | Evidence |
|---|---|---|---|
| Licensed text served without a rights basis | publisher claim; loss of source access | rights basis per source; `republishable()` before every text response; similarity guard in CI; reads audited | `tests/test_rights.py`, `tests/test_audit.py` |
| Wrong derivation published | firm relies on a wrong obligation | evals gates per layer; second-model review; human review on low confidence; never-delete with why-trails | `tests/test_gates.py`, `docs/HLD_V2_TRACE.md` |
| Member re-identification from benchmarks | breach of member trust | k ≥ 5, Laplace noise, differencing refusal, re-identification gate | `tests/test_l8_fills.py` |
| Organisation data leaks into the agnostic store | I5 broken; contractual breach | agnostic scan on every release; instance mode in client account | `tests/test_never_list.py` |
| Compromised release artefact | consumers import bad data | signed manifest, SBOM, pinned deps, verify script | `scripts/verify_release.py` |
| Credential compromise | unauthorised writes | short-lived signed sessions; Cognito; SAML for enterprises; per-org keys; full audit log | `tests/test_metrics.py`, `tests/test_audit.py` |
| Region loss | outage beyond SLO | cross-region replication; nightly restore drill | `.github/workflows/dr_drill.yml` |
| Vendor dependence (Bedrock, GitHub) | derivation or distribution pauses | single inference path with graceful pause; signed artefacts re-hostable | `VENDOR_MANAGEMENT.md` |
| Key-person dependence | knowledge loss | everything in the repo: HLD, trace, runbooks, tests as specification | this directory |

## Acceptance

Residual risk ≥ 15 (likelihood × impact) requires a Steering decision to accept;
below that, the CISO accepts and records it.
