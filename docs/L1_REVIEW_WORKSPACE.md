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
Migration 24 creates an empty source-permission ledger. Migration 25 adds
immutable inventories, audits and scope/artifact reviews. Migration 26 adds
durable workflow jobs, source tasks and leases. None grants rights or seeds a
successful audit. The Terraform web configuration now enables restricted access
and disables auth debugging; the worker configuration holds downstream layers.

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
always skip embeddings and inline downstream refresh. Layer change events and
publication requests are held without a handled marker or acknowledgement.
Monitor the existing SQS retry/dead-letter queues and redrive held events after
acceptance; message retention limits still apply. This is a temporary operator
hold, not an automated proof that L1 is ready.

Fleet cards distinguish configured roles/tools from observed runs. New workflow
steps record actual starts, finishes, durations, source task IDs, attempts,
heartbeats and queue wait. Earlier stage markers remain explicitly measured
intervals between reports. Adapters currently combine acquisition and parsing;
their workflow step is accurately named `acquisition_parse`.

Daily EventBridge requests carry the actual event occurrence ID and timestamp.
Retries reuse a frozen document set and job identity, skipping completed source
tasks. Each source has one renewable lease across jobs. Attempts are bounded at
three, with 30/60-second retry backoff; SQS visibility is renewed during work.
Only L0 relays the outbox, routing commands to their owner and layer events to
EventBridge. Unsupported consumers and failed mandatory checks stay explicit.

Source eval results bind to the source-version ID, label and content hash. A
version change during a suite invalidates its result. Older unbound evals are
not accepted as proof for the selected version. Skipped or not-applicable tests
are not green checks. Ordered subtree and character-span round trips reject
word reordering even when all words are still present.

The inventory audit also reads each archived artifact, checks its individual and
aggregate hashes, reconstructs the stored tree, and checks every clause/span.
FINRA numeric rules receive the exact official-body comparison. Other document
adapters without an independent original-text verifier remain explicit gaps.
Source evals are diagnostic; complete L1 acceptance additionally requires the
reviewed full inventory, current permissions, exact bindings, fresh publisher
checks and the boundary evaluation. Missing/blocked sources are not excluded.

Live HTTP never treats a replay fixture as current data. Conditional responses
must validate cached bytes; an outage anywhere in a multi-artifact fetch makes
the whole acquisition stale. All HTTP caches are private. Original archives are
content-addressed. An unchanged run preserves version IDs; missing legacy
manifests or repaired projections create new versions without deleting history.

## Operational commands

Run from the configured CLHEAR worker environment, with its existing database,
artifact store and identity. The manual entrypoint and SQS use the same handler:

```sh
python -m app.clhear.workers --once --envelope-file /path/to/request.json
```

An audit-only request has this envelope shape (give each actual request a fresh
event ID and UTC timestamp):

```json
{
  "event_id": "unique-audit-request-id",
  "layer": "l1",
  "kind": "L1InventoryAuditRequested",
  "subject_ref": "finra",
  "producer": "authorized-operator",
  "ts": "2026-09-15T00:00:00Z",
  "payload": {"scope": "finra", "discover": false}
}
```

`scope` is `finra` or `registered`. `discover: true` enables permission-gated
official enumeration. `AdapterRunRequested` with `payload.adapter: "finra"`
performs discovery, reconciliation, ingestion and evaluations through the L1
fleet. Exact per-source permissions also apply to discovery categories such as
`finra/catalog/rules`; approving one rule never approves the entire website.

L0 accepts `L1EvidenceReviewRecorded` for three explicit review kinds:

- `permissions`: an operation permission snapshot with evidence, approver and
  validity dates; no omitted operation is granted.
- `artifact`: exact source key/content hash, publisher edition/canonical URL,
  `full`, `preview` or `excerpt` coverage, evidence and reviewer approval.
- `scope`: approval or revocation of one exact inventory hash with evidence.

These commands record an existing review; they do not establish publisher
authorization themselves. No approved decisions are supplied with this change.
Artifact reviews do not grant permissions. Any review change requires a new
audit. Preserve real source documents and their notices; do not use simulated
standards to populate these ledgers.

## Candidate viewer and accepted releases

The private viewer consumes a worker-built candidate projection. Its origin,
revision and any redacted/omitted outputs are explicit; it is not the accepted
release pointer. L0 owns its refresh, including inventory and workflow tables.
The authoritative PostgreSQL database is never replaced by the web SQLite file.

Viewer publication compares the previously observed object ETag, preventing an
older concurrent export from replacing newer evidence. It also rechecks the
exact storage/display grants before upload. The web service checks for a newer
snapshot within 300 seconds of its last successful check; restricted requests
return 503 when a due check fails, preserving the cached file without serving
its old grants. Grant expirations are checked on each text read. Revocations
still require the L0 refresh job and this bounded cache window; they are not an
instantaneous push feed. Response headers expose the last successful snapshot
check and cache window. A missing configured snapshot cannot become an empty
fallback database.

`PublishReleaseRequested` on L0 supports `prepare` and `promote`. Preparation
compiles an allowlisted L1 snapshot, freezes the inventory/version bindings,
SBOM digest and signature declaration, and writes a candidate manifest. A failed
inventory produces a blocked candidate without a text snapshot. Candidate IDs
are immutable; use a new ID for a changed attempt.

The release workflow signs the finalized manifest bytes and then asks L0 to
promote. Promotion checks actual snapshot bytes, exact trusted GitHub signer,
SBOM, current inventory and database bindings. Only then does it advance
`latest`; the detached promotion receipt records signature verification. Failed
verification retains the preceding accepted release. No automatic public
export or downstream derivation is part of this workflow.

The release runner requires network access to the authoritative PostgreSQL
database and the configured private S3 destination. Missing configuration fails
explicitly; it never builds a fresh fixture corpus as a production release.

## Remaining acceptance work

1. Record source access evidence and supply actual authorized protected texts.
2. Execute and independently review the FINRA enumeration, including structural
   entries, reserved rules, supplementary material, notices and historical scope.
3. Run the CLHEAR worker against the authoritative database and reconcile its
   records with the viewer's database/projection. The previously inspected web
   snapshot and Aurora worker database were different stores.
4. Implement and validate document-specific original-text comparisons for
   non-rule FINRA documents and remaining publishers. Discovery exposes those
   adapter gaps rather than importing navigation or claiming universal coverage.
5. Validate the deployed queue, viewer projection and signed release workflow
   against Aurora and real authorized artifacts.
6. Deploy and run manual L1 verification immediately using
   [the deployment workflow](L1_DEPLOYMENT.md). Two consecutive successful
   00:00 UTC daily cycles are a later reliability check, not a prerequisite
   for deploying or reviewing L1. Keep downstream processing held until its
   separate acceptance decision; manual success does not grant full registered
   L1 acceptance or prove that the scheduler ran.

No production database, snapshot, worker configuration or deployment has been
changed by this implementation. This document is not a declaration of complete L1.
