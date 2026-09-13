# Data Classification and Handling

Owner: CISO. Applies to everything CLHEAR stores, derives or publishes.

## Classes

| Class | What | Where it may live | Who may read | Handling |
|---|---|---|---|---|
| **Open** | derived layers L2–L7, schemas, evals summaries, governance artefacts, public-domain and open-licence source text | agnostic store, public `clhear` repo, releases | anyone | ODC-By 1.0 (data), CC BY 4.0 (schemas); signed releases |
| **Rights-restricted** | source text with `licensed` basis; identifiers of `byol_only` / `derived_only` sources | agnostic store, data lake (immutable) | anyone for `licensed` with attribution; text withheld for `byol_only` / `derived_only` | `republishable()` gate; every read of `licensed`/`byol_only` text audited; no verbatim republication for `derived_only` |
| **Member** | L8 fills, benchmark inputs (HMAC-keyed), benchmark aggregates | member schema `l8_benchmarks` | members and maintainers | open by mode (I9); inputs never leave as individual rows; aggregates k ≥ 5 with noise |
| **Personal** | user accounts (email, display name, provider), contributor CLA signatures, watchlists, audit actors, salted IP hashes | `community` schema, `l0_platform.audit_log` | the user; maintainers for support and audit | minimisation (no raw IPs); deterministic ids; deletion requests honoured for account data while audit rows keep the actor string as a legal record |
| **Instance** | a client's Actual overlay, gap diffs, instance priorities | **only** the client's account (Reg42 OS) | the client | never enters the agnostic store; scan on every release (I5) |
| **Secret** | session secret, API key secrets (hashed), Cognito client secrets, Google OAuth secrets, SAML metadata | SSM parameters, environment of the web tier | the service | never in the repo (`CHANGEME` placeholders); rotated on suspicion |

## Rules

1. Text follows rights, not layers (I8). No code path returns clause text without
   consulting the rights basis of its source.
2. Nothing is deleted from the record (I2); invalidation closes `valid_to`. The
   exception is personal account data on a lawful request, which is anonymised in place
   with a why-trail.
3. The audit log is append-only and holds no raw network addresses.
4. Member data is opened by mode, never by layer (I9): the same table is closed in
   agnostic mode and open in instance mode.
5. Exports and snapshots carry only Open and, where the basis allows, Rights-restricted
   data; the exporter refuses anything else (`tests/test_exporter_public.py`).

## Personal data (GDPR / UK GDPR / Israeli PPL Amendment 13)

| Purpose | Data | Lawful basis | Retention |
|---|---|---|---|
| Account and sign-in | email, display name, provider sub | contract | life of account + 30 days |
| Contribution and attribution | name, CLA signature, contributions | contract; legitimate interest (attribution) | indefinitely for accepted contributions (public record); CLA 7 years |
| Watchlists and notifications | watched instruments, email | consent | until withdrawn |
| Audit | actor identifier, salted IP hash, user agent | legal obligation; legitimate interest (security) | 7 years |
| Membership and benchmarks | member org label, HMAC of member id | contract | membership + 12 months; aggregates indefinitely (no individual data) |

Controller: Reg42 Ltd. Processors: see `VENDOR_MANAGEMENT.md`. Requests: privacy@reg42.ai.
