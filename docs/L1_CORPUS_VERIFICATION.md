# L1 corpus verification

CLHEAR is an institution-independent source corpus. Scope derives from publisher
contracts and source identities, not a customer's licensing footprint, import
wave or planned demonstration. Genuine document content and acquisition history
remain unchanged when registry presentation metadata is reconciled.

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

## Acceptance and current limits

This implementation adds diagnostic and verification capability. It does not
assert that existing live data passes, that every publisher catalog adapter is
implemented, or that missing licensed artifacts and permissions have been
obtained. Publisher contracts and UI evidence expose those dependencies.

The registry currently declares 126 entries across 45 publisher profiles. Nine
profiles have catalog traversal implementations, with explicitly incomplete
category, archive, language, attachment or cutoff contracts; the other 36 still
need publisher-specific discovery adapters. These are remaining implementation
tasks, not merely missing licences. Full publisher acceptance is therefore
unavailable in this revision, including for FINRA. The current discovery date
is a cycle identity, not proof of a consistent historical publication cutoff.

“Ready for sampling” requires a known complete frozen inventory, every expected
document verified against its exact originals, no unresolved inventory gaps,
and a successful immediate repeat demonstrating unchanged behavior. A
`completed_for_review` cycle is incomplete corpus acceptance. Deployment,
manual-cycle verification, accepted release and nightly validation remain
separate results. Two actual 00:00 UTC cycles must still be observed before
scheduled operation is accepted.
