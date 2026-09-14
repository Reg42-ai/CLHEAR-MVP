# L1 review workspace

This change restores the eight-layer homepage at `/`, retains `/stack`, and
moves the existing Solon page to `/solon`. Existing downstream output remains
available as a preview. Stored counts are not publication or completeness claims.

The canonical L1 workspace is `/l1`; `/sources` serves the same reader. Sources,
Fleet, Evals and Changes keep their separate purposes within one workspace.
Document and node links carry source, version and node identity. Inspectors show
database record IDs, hierarchy, hashes and clause character spans. A selected
historical version must never silently resolve to current text or current evals.

## Restricted deployment

Configure the web service with:

- `CLHEAR_RESTRICTED_ACCESS=true`.
- `CLHEAR_REVIEWER_EMAILS` containing the exact approved email addresses.
  `CLHEAR_MAINTAINERS` is the fallback if the reviewer list is empty.
- A private `CLHEAR_SESSION_SECRET` and `CLHEAR_AUTH_DEBUG=false`.
- Working existing email, Google or Cognito authentication settings. Google and
  Cognito reviewers require verified email addresses.

Restricted mode protects application pages and APIs, including export and
GraphQL routes, and returns private/no-store responses. Identity headers such
as `X-Reg42-User` do not establish reviewer access. Health, sign-in, authentication
and static resources remain reachable. Signed-in users still need source-level
text permission. Internal display does not change the public-text flags.

The restricted viewer does not run migrations or seed the corpus at startup.
Run migrations through the existing CLHEAR worker before starting the viewer.
Migration 24 creates an empty source-permission ledger and grants no rights.

## Real protected content

No simulated ISO, SOC 2, IFRS or FINRA standards are supplied. Test fixtures
remain test-only code. Missing documents must remain missing in product output.

The append-only source-permission ledger records reviewed evidence and an
approver for each exact source. Acquisition, storage, parsing, embedding, model
inference, training, derivation, internal display, public display and
redistribution are distinct permissions. Missing flags deny the operation.
Expired or revoked permissions do not revive an earlier grant. The ledger
records evidence; it cannot itself establish that a publisher authorized use.

The L1 ingestion worker checks protected acquisition/storage/parsing before
fetching. A blocked run preserves the earlier version and reports its cause.
An uploaded file or a changed license label is not permission. Protected-file
ingestion requires an actual document; no placeholder is an imported version.

Publisher acquisition paths reviewed on 2026-09-15:

- FINRA official rule pages and its permission process:
  https://www.finra.org/contact-finra/permission-use-finra-copyrighted-material
- ISO Store and official previews: https://www.iso.org/standards.html .
  The ISO terms separately address database integration and AI use:
  https://www.iso.org/terms-conditions-licence-agreement.html .
- AICPA's actual Trust Services Criteria, with a free-account download:
  https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022 .
  Download access is distinct from permission to integrate the text into CLHEAR.
- IFRS integration-ready licensing and continuing updates:
  https://www.ifrs.org/products-and-services/ifrs-accounting-licensing/ .

No accounts were created, licenses purchased, vendor messages sent or protected
documents newly imported as part of this change.

## Worker operation and evidence

Continue using `AdapterRunRequested` through the existing CLHEAR worker; do not
introduce a separate importer or schedule a Codex task to perform ingestion.
Set `CLHEAR_L1_ONLY=true` on the worker while accepting L1. Scheduled adapter runs
then skip embeddings and the inline downstream refresh. Layer change events and
publication requests are held without a handled marker or acknowledgement.
Monitor the existing SQS retry/dead-letter queues and redrive held events after
acceptance; message retention limits still apply. This is a temporary operator
hold, not an automated proof that L1 is ready.

Fleet cards distinguish configured roles/tools from observed runs. An absent
status is unknown; no matching run is explicitly absent. Existing stage timings
are recorded intervals between stage markers, not independent distributed spans.
Adapters currently combine fetching and parsing, so those intervals must not be
presented as precise independent network and parser durations.

Source eval results bind to the source-version ID, label and content hash. A
version change during a suite invalidates its result. Older unbound evals are
not accepted as proof for the selected version. Skipped or not-applicable tests
are not green checks. Ordered subtree and character-span round trips reject
word reordering even when all words are still present.

These are stored-projection checks. They do not establish an independently
complete FINRA inventory or verify every artifact against the publisher. L2
readiness therefore remains false in the L1 source evidence response.

## Remaining acceptance work

1. Record source access evidence and supply actual authorized protected texts.
2. Finalize a versioned FINRA inventory, including structural entries, reserved
   rules, supplementary material, notices and historical scope decisions.
3. Run the CLHEAR worker against the authoritative database and reconcile its
   records with the viewer's database/projection. The previously inspected web
   snapshot and Aurora worker database were different stores.
4. Implement independent artifact checks and inventory denominators, exhaustive
   hash/span checks, source-specific retrieval tests, freshness thresholds and
   a coherent run/release acceptance manifest. Do not substitute parser coverage
   for full publisher completeness.
5. Split fetch/parse timing instrumentation where adapters currently combine them;
   validate queue retry, job idempotency, publication and recovery end to end.
6. Rehearse a complete daily run and only then lift the downstream hold.

No production database, snapshot, worker configuration or deployment has been
changed by this implementation. This document is not a declaration of complete L1.
