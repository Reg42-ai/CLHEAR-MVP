# L1 corpus verification

CLHEAR is an institution-independent source corpus. Scope derives from publisher
contracts and source identities, not a customer's licensing footprint, import
wave or planned demonstration. Genuine document content and acquisition history
remain unchanged when registry presentation metadata is reconciled.

## In-place development

Use the existing Aurora record, S3 artifact store, queues and L0/L1 fleets for
real corpus work. There is no second maintained corpus database, local importer
or staging-to-production data transfer. Unit and destructive integration tests
use disposable fixtures/databases and never contribute corpus acceptance evidence.

An owner merge with passing CI authorizes the normal code deployment while L1
acceptance remains pending. Deployments retain the L2–L8 hold and the private
reviewer interface. A review snapshot may expose unresolved gaps; it is not an
accepted release. Deployment success, original-document verification, English
readiness, manual sampling and actual scheduled delivery are separate outcomes.

Before worker migrations, the deployment controller reads Aurora's existing
backup retention and point-in-time recovery window. It requires enabled
retention and a latest restore point no more than one hour old, checking again
immediately before L0 bootstrap. The report records this evidence, not a claim
that a restore drill was performed. The controller never creates or restores a
database. Migrations must remain additive and backward-compatible: code rollback
does not undo a migration or remove newly imported document versions.

L0 serializes complete manual and scheduled cycles, retaining their original
request and scheduler identities. A deployment cannot silently continue a cycle
under a different worker/parser revision or parser configuration digest.
Interrupted older cycles remain in the
ledger; new verification uses a new cycle identity and can reuse preserved bytes.
The queue is FIFO. A database execution fence prevents an expired lease from
admitting a second cycle while the first worker is still executing. Contention
before execution does not spend the failed-work retry budget. Completed command
redeliveries are acknowledged without executing a later phase twice.
The supported waiting path is an L0 `L1CycleRequested` command or a real scheduled
cycle. Legacy standalone adapter/audit/translation envelopes are maintenance
commands and require an idle cycle slot; they are not another durable queue.
Sending them to SQS during a long cycle can exhaust the queue's five-receipt
redrive limit even though no source execution attempt has started. Use a queued
cycle for ordinary verification and retries; inspect/replay any legacy maintenance
delivery only after the active cycle ends. No current viewer action emits those
standalone envelopes.

## Inventory and denominator

`app/clhear/l1/source_registry.py` declares stable source/family identities.
`registry_etoro.py` is a compatibility import only. `publishers.py` declares
versioned financial compliance boundaries for represented publishers, while
`catalogs.py` and `discovery.py` own supported official collection traversal.

Contracts cover current material, officially exposed archives, amendments and
attachments. Financially relevant standards include governance, reporting,
security, privacy and resilience. Unrelated content, company-filing databases
and reconstruction of unavailable history are outside scope. Unknown effective
and publication dates remain unknown.

A collection page is not a document edition. A reference to an unresolved
national instrument is not a copy of another regulator's instrument. Entries
with missing permission evidence, edition artifacts or parser support remain
in the inventory. A traversal limit, unsupported collection category or missing
catalog implementation leaves the full denominator unknown.

The durable discovery frontier stores URLs, categories, pagination/checkpoints,
leases, attempts, publisher profile hashes and retrieval results. Existing
source and artifact records are preserved. Register acquisition/processing
permissions and licensed artifacts through existing L0 commands; sign-in does
not grant these rights.

## Cycle execution

The normal daily EventBridge deliveries retain their individual occurrence IDs
and timestamps. L0 groups the expected adapter lanes into one daily cycle, then
coordinates discovery and freezes the work before imports. A manual
`L1CycleRequested` command creates a separate cycle. Neither manual imports nor
recent legacy runs can satisfy actual scheduled-delivery checks.

L0 dispatches `AdapterRunRequested` for the 32 configured lanes. L1 performs
acquisition, parsing, persistence, document checks and readback in source tasks.
Completed tasks are reused on redelivery; transient failures retry within the
worker's lease and attempt policy. Permission/artifact/implementation gaps are
recorded terminal review outcomes rather than endless transport failures.

After all children finish, L1 evaluates the corpus once against the frozen
inventory and exact output bindings. L0 publishes a private review snapshot
only after the result commits. Its manifest records the completed cycle ID,
completion time and revision. Publication failure leaves the previous viewer
object and accepted release intact.

After a successful deployment, L0 and L1 each retain at least one desired task
and a minimum autoscaling capacity of one. L0 must relay database outbox work
even when its queue is empty. A long L1 import holds an invisible SQS message;
zero visible messages must not stop its last worker. Keeping both tasks
available incurs idle compute cost and uses existing IAM permissions. Maximum
capacities are unchanged, and Terraform keeps L2–L8 minimums at zero. A failed
deployment still suspends autoscaling and holds all consumers at zero until
an approved recovery succeeds.

The cycle records worker code revision and immutable image digest. The
verification-only dispatcher also checks the actual running task definitions
and image digests, ensuring a partially rolled-out fleet cannot receive a new
verification request under the wrong identity.

## Starting a cycle after deployment

Use **GitHub Actions → deploy-l1 → Run workflow** on `main`, selecting
`operation=verify-l1`. This uses the same protected environment and existing
deployment role. No new importer, database repair endpoint or local AWS write
path is introduced.

The controller validates all 32 schedules and their occurrence-aware queue
targets, confirms L0/L1 use the same deployed worker image and checks every fleet
retains the L1-only hold. It runs the existing L0 image with
`--request-l1-cycle --verification-id l1-cycle-RUN-ATTEMPT`. The CLI persists a
request through the ordinary outbox and exits. The uploaded receipt reports
`cycle_submitted`; corpus acceptance remains pending.

Inspect **L1 → Fleet → Verification cycles** for child execution and click a
child's workflow to inspect source tasks and measured step durations. Pages
show totals and separate pagination for jobs, source tasks and steps. The
cycle, source version, original hash and evaluation binding connect the
inspection trail. Refresh shows the latest L0-published evidence.

Repeat with a new workflow run after the first diagnostic cycle completes.
Unchanged-source verification must preserve versions and encoding while
recording successful new publisher checks. Cached content cannot imply a fresh
publisher check after a failed request.
For a reviewed immutable licensed edition, the worker separately records a
successful exact-artifact availability check. It never calls that a publisher
check or evidence that no newer edition exists. Publisher catalog coverage and
the edition-specific artifact and permission reviews remain separate gates.

## Fidelity and encoding

Every supported format requires an original-to-projection comparison, separate
from parser self-consistency. Preserved bytes and their hashes bind the result.
Multipart imports use an `artifact-set-v2` manifest hash over artifact names,
media types, individual byte hashes and lengths. Object keys also include each
artifact's byte hash, so changing attachment boundaries cannot overwrite earlier
originals with the same concatenated text. Legacy evidence retains its recorded
hash method; legacy multipart imports require worker revalidation.
A versioned normalization policy defines canonical text; offsets use Unicode
code points. Original locations (HTML/XML paths or PDF page/layout references)
are distinct from canonical character spans. Legacy versions without locations
remain readable but their missing evidence is explicit.

Checks compare complete ordered text and expected structural records, including
node/clause sets, references, hierarchy, ordering, links and text hashes. Missing,
duplicated, reordered or added text cannot be certified by checking only the
rows that happen to remain. List records have their own structure contract.
PDF checks use an independent extractor and layout evidence. Unsupported
layouts, scanned pages, extraction disagreement and incomplete standards
structure remain unresolved rather than receiving a synthetic pass.

Annotations are optional heuristic or model output. They do not change originals
or establish fidelity. Test fixtures live in isolated tests and do not count as
publisher evidence. Existing explicit test-origin rows remain in audit history
while being excluded from private corpus snapshots and production public reads.

## Original languages and English views

Every distinct document retains its original language and needs an English view.
An authoritative original in English satisfies both requirements. Another
publisher English edition must be explicitly matched to the original document
version; a similarly titled edition is insufficient. Workers bind reviewed
language metadata to exact artifact hashes. Missing language or edition evidence
remains an explicit gap rather than being inferred from a URL or text alphabet.

For documents without matching publisher English, the L1 worker can produce a
separately labelled machine translation after original-text verification. The
original nodes and clauses are never rewritten. Required translation, inference,
derivation and display permissions are checked independently. The external Infer
registry must expose the `l1_translate` task and the independent reviewer route;
missing registration blocks the view before a model call. The mirror in
`handoff/reg42-infra/tasks.clhear.yaml` describes the required task but does not
change that external service or authorize content processing.

Translation covers the entire ordered set of source segments, with exact source
hashes, original-record links, returned model identity and an independent
bilingual review. Missing, extra, duplicated or reordered segments, changed
references, incomplete model responses and reviewer disagreement block readiness.
Verified batches are checkpointed so retries reuse only identical inputs and
policy/permission bindings. Changed policy, artifacts or revoked rights invalidate
readiness. Reading the viewer never invokes a model.

The source inspector's **English view** tab shows authoritative source links or
labelled machine segments with their provenance and review evidence. Review and
release snapshots preserve these bindings and exclude test-origin records.
English readiness is a separate cycle gate and cannot certify original fidelity.

## Acceptance and current limits

This implementation adds diagnostic and verification capability. It does not
assert that existing live data passes or that missing licensed artifacts and
permissions have been obtained. Publisher contracts and UI evidence expose
those dependencies.

The registry currently declares 126 entries across 45 publisher profiles. Each
profile is connected to a worker discovery implementation. This is implementation
coverage, not proof that every collection has been enumerated. Explicit category,
archive, API, language, attachment and cutoff gaps remain; see
`L1_PUBLISHER_SCOPE.md` for publisher-specific contracts and outstanding work.
Full publisher acceptance is therefore unavailable until those gaps are resolved
through worker evidence, including for FINRA. The current discovery date is a
cycle identity, not proof of a consistent historical publication cutoff.

“Ready for sampling” requires a known complete frozen inventory, every expected
document verified against its exact originals, no unresolved inventory gaps,
and a successful immediate repeat demonstrating unchanged behavior. A
`completed_for_review` cycle is incomplete corpus acceptance. Deployment,
manual-cycle verification, accepted release and nightly validation remain
separate results. Two actual 00:00 UTC cycles must still be observed before
scheduled operation is accepted.
