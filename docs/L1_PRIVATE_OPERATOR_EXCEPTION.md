# FINRA private operator exception

This control is an operator decision for private candidate review. It does not
record a publisher licence or convert FINRA text into open content. The strict
`source_permissions` ledger and `permissions.decision` remain unchanged.

An authenticated authorized operator requests the control through CLHEAR. L0
records the activation and its exact manifest of FINRA source keys and official
HTTPS URLs. Discovery and imports remain L1 worker tasks. A newly discovered
source must acquire an exact manifest binding through that workflow; a prefix,
company name or sign-in session alone is insufficient. The ledger retains the
actor, reason, evidence reference, manifest hash and activation/binding identity.
The exception remains active until an explicit append-only revocation; the UI
must show that state without implying an expiry date or a publisher grant.

The exception is limited to acquisition, storage, parsing and restricted
internal display. It cannot authorize public display, redistribution, export,
embedding, model inference/training, derived downstream layers or translation.
It applies to FINRA, not ISO, SOC 2, PCI DSS, IFRS or other protected publishers.
Those sources continue to require their actual document and operation evidence.

## Activation and revocation

The owner-merged deployment runs the additive migration and then L0 bootstrap.
Bootstrap records the authorization from this change once, with the deployed
commit as its evidence reference. Its stable command ID is
`finra-private-review-owner-authorization-2026-09-16`. Later deployments preserve
an explicit revocation; they do not reactivate it. L0 initially binds the declared
FINRA catalogs and registered documents. L1 requests additional exact bindings
through `L1ExceptionBindingsRequested` as discovery finds documents, archive pages
and attachments. L0 checks them against the reviewed manifest before L1 resumes.

To revoke, submit the existing `L1EvidenceReviewRecorded` command to L0 with a
fresh event ID and this payload, supplying the operator's actual identity,
decision reference and reason:

```json
{
  "review_kind": "operator_exception",
  "exception_id": "finra-private-review",
  "command_id": "unique-owner-revocation-id",
  "action": "revoke",
  "approved_by": "actual-authorized-operator",
  "evidence_ref": "actual-owner-decision-reference",
  "rationale": "actual-revocation-reason"
}
```

The worker first invalidates the small access-control object, then commits the
ledger event and snapshot-refresh request, then publishes current control state.
An interrupted command can resume with the same event ID. A different command
or ordinary snapshot publisher cannot clear a pending invalidation. Missing or
unreadable control evidence denies exception access. Replacing a source's
manifest binding also invalidates its older snapshot binding; adding another
source leaves existing bindings available. No Secrets Manager value activates
or revokes this control.

## Reading results

A private candidate can pass byte, document-tree, clause and span checks while
publisher permissions remain unresolved. The source audit reports both:

- `technical_verified`: the exact preserved original and encoded projection
  passed the available technical checks under current candidate authorization.
- `verified`: the normal source acceptance result, including strict publisher
  permissions. The exception does not turn this field green.
- `operator_exception_used`: inspection relied on the exception, with its exact
  candidate permission bindings recorded beside the strict permission decisions.
- `release_eligible: false`: the candidate cannot become an accepted L1 release.

Per-source eval records use the same separation. A technically passing E2 result
is a check of the stored document, not proof of full FINRA publication coverage
or processing rights. Global inventory acceptance, preparation of an accepted
snapshot and promotion remain closed while publisher permissions are unresolved.
Existing accepted release pointers and history stay unchanged. A subsequent
actual publisher grant requires a fresh strict audit before release acceptance;
historical use of an exception does not itself establish that grant.

Revocation invalidates affected candidate authorization and audit bindings.
Private viewer refresh and read-time gates must apply the current control state;
a previously passing technical eval cannot reinstate a revoked exception. Evals
also recheck their operation authorization after execution and fail if it changed
while the checks were running.

## Deployment prerequisite

The deployed `clhear-worker-task` role's inline policy of the same name needs
`sqs:ChangeMessageVisibility` added to its existing `FleetQueues` statement.
Preserve the existing actions and exact resource list covering the nine fleet
queues and the dead-letter queue. `infra/iam.tf` already declares this action;
the owner must reconcile the deployed policy with that declaration. No Terraform
source change or broad stack apply from stale local variables is required.

The viewer's L0 `sqs:SendMessage` prerequisite is already fulfilled. Continue
submitting operator requests to L0; neither this worker policy repair nor the
exception permits an ad hoc database write, direct viewer Aurora access, or seat
IAM administration.

All local validation uses authored test fixtures and the actual exception
ledger/worker permission interfaces. Tests are not a publisher import or a live
activation. Production activation, imports and publication evidence must come
from CLHEAR's deployed workers.
