# Vulnerability Disclosure Policy

Reg42 Ltd operates CLHEAR (`clhear.reg42.ai`, the `clhear` public repository, the
SDKs and this repository). We want to hear about security problems before anyone
else does, and we will not pursue people who tell us in good faith.

Machine-readable version: `/.well-known/security.txt` ([RFC 9116](https://www.rfc-editor.org/rfc/rfc9116)).
Human version: `/security`.

## How to report

Email **security@reg42.ai**. If you need to encrypt, ask for our key in a first
plain email and we will send it within one working day. Include:

- what you found and where (URL, endpoint, package, commit),
- steps to reproduce, or a proof of concept,
- what impact you believe it has,
- whether you want to be credited, and under what name.

You will get a human acknowledgement within **72 hours** and a triage decision
(severity, whether it is in scope, what happens next) within **7 days**.

## What is in scope

- `https://clhear.reg42.ai` and every path under it, including `/graphql`,
  `/interop/*`, `/l1`–`/l8`, `/auth/*`, `/audit`, `/status.json`, `/metrics`.
- The `clhear` public repository and its published artefacts: release snapshots,
  `manifest.json` and its Sigstore signature, `sbom.spdx.json`.
- The Python and TypeScript SDKs (`export/clhear/sdks/`).
- This repository's infrastructure code (`infra/`) and CI (`.github/workflows/`).

## What is out of scope

- Denial of service, volumetric or otherwise. Tell us about an amplification
  vector; do not run it.
- Findings that require physical access, a compromised device, or social
  engineering of Reg42 staff, contributors or design partners.
- Third-party services we use but do not operate (AWS, GitHub, Discourse, esm.sh),
  unless the issue is in how CLHEAR configures or uses them.
- Missing best-practice headers, version banners, `robots.txt`, rate-limit
  observations and other reports with no demonstrated security impact.
- Content of regulatory texts. If a source text is wrong or its rights basis is
  wrong, that is a data problem: use the contribution flow (`/contribute`).

## Safe harbour

Research that follows this policy is authorised. We will not initiate legal
action, or refer you to law enforcement, for good-faith research that:

- only touches accounts and data you own or have explicit permission to test
  (create your own account; never read another user's contributions, API keys,
  benchmark inputs or audit entries),
- stops and reports as soon as you can demonstrate the issue, without
  extracting more data than needed to show it,
- does not degrade the service for others,
- keeps the details private until we have fixed it or the disclosure window
  below has passed.

If a third party takes action against you for research covered by this policy,
we will make it known that your research was authorised.

## Our commitments

| Severity (CVSS 3.1) | Acknowledge | Triage | Fix target | Public note |
|---|---|---|---|---|
| Critical (9.0–10.0) | 72 h | 2 days | **7 days** | with the fix |
| High (7.0–8.9) | 72 h | 7 days | **30 days** | with the fix |
| Medium (4.0–6.9) | 72 h | 7 days | **90 days** | next release notes |
| Low (0.1–3.9) | 72 h | 14 days | **180 days** | next release notes |

We will keep you informed as we work, tell you when the fix ships, and credit
you (with consent) in the hall of thanks below and in the release notes. If we
miss a target we will tell you why and what the new date is.

## Coordinated disclosure

You may publish after we ship the fix, or **90 days** after your report if we
have not — whichever is sooner. If a fix needs longer (for example a
coordinated change with a design partner's SAML IdP), we will ask, not assume.

## Rewards

CLHEAR is an open standard built by a small company. We do not currently run a
paid bounty. We do credit, we do write reference letters on request, and we
publish a summary of every fixed report so the work is visible.

## Hall of thanks

Researchers who reported under this policy, with their consent. None yet — be
the first.

## Related

- Pen-test scope: `PENTEST_SCOPE.md`
- SOC 2 Type I readiness pack: `SOC2_TYPE_I_READINESS.md`
- Incident response: `policies/INCIDENT_RESPONSE.md`
