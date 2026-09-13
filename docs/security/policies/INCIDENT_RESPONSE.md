# Incident Response

Scope: security and availability incidents affecting CLHEAR (`clhear.reg42.ai`, the
public `clhear` repository, SDKs, the AWS account). Owner: CISO (Reg42). Reviewed
annually and after every SEV1/SEV2.

## Severity

| Level | Definition | Examples | Response |
|---|---|---|---|
| SEV1 | Confirmed exposure of licensed text without rights basis, member data, benchmark inputs, credentials, or a compromised release artefact | derived-only text served; `/l8/fills` reachable anonymously; signed manifest forged | page on-call now; incident lead within 30 min; status page within 1 h; design partners notified within 24 h |
| SEV2 | Loss of an SLO for > 1 h, or a vulnerability with a working exploit and no exposure yet | API down; L1 stale > 24 h; High CVSS finding reproduced | on-call within 1 h; status page within 2 h |
| SEV3 | Vulnerability report without exposure; degraded but within SLO; failed DR drill | Medium CVSS; one gate red on a non-published layer; `dr_drill` red | next working day; tracked in a `security` issue |
| SEV4 | Informational | scanner noise; VDP report out of scope | acknowledge within VDP terms |

## Roles

- **Incident lead** — the on-call maintainer; owns the timeline and decisions.
- **Communications** — status page (`/status`), design partners, the VDP reporter, Discourse.
- **Scribe** — keeps the timeline in the incident record from the first minute.
- **CISO** — declares SEV1/SEV2, approves external notification, signs the review.

## Procedure

1. **Detect** — CloudWatch alarm, `/status.json` probe, `/audit` anomaly, VDP report, gate failure.
2. **Triage** — assign severity from the table; open the incident record (title, severity, lead, start time).
3. **Contain** — the levers, in order of blast radius: revoke API keys (`api_keys.revoke`); revoke membership (`/l8/members`); flip Cognito app-client to disable an IdP; scale the web tier to zero via Terraform; freeze releases (`freeze_below_gate` / do not tag).
4. **Preserve** — export `/audit` for the window; snapshot Aurora; keep the release artefacts. Nothing is deleted (I2 applies to evidence too).
5. **Eradicate and recover** — fix forward through the normal PR path (CI, review, signed release). Emergency changes still go through CI; they skip only the waiting.
6. **Communicate** — status page from SEV2 up; VDP reporter kept informed; design partners for SEV1 within 24 h with what happened, what was affected, what to do.
7. **Review** — within 5 working days: timeline, root cause, what detected it, what should have, actions with owners. Filed in `docs/security/incidents/<date>-<slug>.md` (public unless it names a partner).

## Notification duties

- Personal data breach (user accounts, contributors): UK ICO / EU authority within 72 h where required; Israeli PPA per Amendment 13.
- Design partners: per contract, no later than 24 h for SEV1.
- Publishers whose licensed text was exposed: per the rights record (`l1/rights.py`) evidence URL contact.

## Related

`../VDP.md` · `BUSINESS_CONTINUITY.md` · `VULNERABILITY_MANAGEMENT.md`
