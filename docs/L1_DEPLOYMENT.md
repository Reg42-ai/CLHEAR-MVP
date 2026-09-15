# Deploy and verify L1 without waiting for midnight

`deploy-l1` deploys code and runs the owning CLHEAR workers immediately. It does
not grant document permissions, accept the full FINRA inventory, promote a
release, or enable downstream processing. Its database is the existing
`clhear-record` Aurora cluster. The Lambda viewer receives a separate private
candidate at `webui/l1/candidate.db`; the legacy snapshot is not overwritten.

## Fast iteration

Use [local private preview](PREVIEW.md) for immediate edits. After the one-time
[hosted preview bootstrap](../infra/bootstrap/PREVIEW.md), a push to a `codex/`
branch automatically updates the isolated preview when its changes are confined
to presentation files under `app/clhear/web/`. Stable main still requires the
owner's merge; preview iterations require neither a merge nor an environment
reviewer. The preview environment itself allows **main only**: the trusted main
workflow processes the branch as input. It never executes branch scripts,
workflows, dependency files, or branch CI artifacts with AWS credentials.

Main deployment automatically chooses one of two lanes:

- **Presentation:** reuse the last verified Lambda ZIP's backend and dependency
  bytes, replace only regular HTML/CSS/JS/image/font files, conditionally update
  the viewer, verify completion/hash/configuration and access restrictions, and
  roll back code on failure. Workers, schedules, concurrency, snapshot and corpus
  acceptance are unchanged. No worker image build, migration, import or snapshot
  reconstruction runs for this lane.
- **Full:** API, authentication, permissions, worker, schema, packaging,
  dependency or workflow changes use the coordinated L0/L1 process below.
  Missing verified code-baseline evidence also selects full deployment.

`deployments/l1/viewer-release.json` records the last verified viewer SHA, worker
SHA, exact versioned ZIP/hash, and full and viewer deployment identities. It is
a code pointer, separate from every corpus/accepted-release pointer. Only a
successful main deployment advances it with an S3 conditional write. The first
deployment of this change must be full to establish it. Failure to publish this
pointer is an explicit deployment-job failure; it must never be reconstructed
from a guessed current commit. Fast selection compares the **entire deployed
baseline to the candidate**, and the controller independently verifies the
actual ZIP bytes and live Lambda hash. Preview cannot change this pointer.

Enable `CLHEAR_FAST_DEPLOY_ENABLED=true` in `clhear-l1` only after the bootstrap
grants the dedicated role access to this code record. Until then the existing
full deployment works without reading or publishing it. This opt-in is a
one-time enrollment switch, not a per-deployment approval.

CI keeps the existing three required job names. A fast test selection compares
against a completed, successful **full main CI** anchor, not merely the preceding
push. This prevents a canceled backend test run from being hidden by a later UI
edit. Missing or inaccessible anchor evidence runs the full suite. Presentation
checks retain authentication, permissions, snapshot access, packaging and real
browser/accessibility tests. Both full images use separate persistent Docker
cache scopes. Fast packaging reuses tested dependencies without rebuilding them.

Preview and main cutovers share the non-cancelling deployment lock. Eligibility
is rechecked after acquiring the lock and immediately before cutover. New test
runs can cancel superseded CI; running deployments are never interrupted. An
old branch or failed/rerun CI cannot authorize a preview. The hosted preview may
refresh an older backend only from the latest verified stable artifact; branch
backend changes first require the normal full main deployment.

The deployment report lists viewer and worker commits separately, measured
steps, rollback outcome and anonymous access probes. Signed-in browser tests run
against test data; an actual authenticated hosted walkthrough is explicitly
pending until a reviewer completes it. Code checks do not certify FINRA coverage,
grant permissions, alter publisher freshness, or satisfy the two nightly cycles.

This iteration path is entirely in CLHEAR-MVP. It has no workforce or Paperclip
dependency. AWS enrollment remains a single owner-run infrastructure operation;
routine code updates use the dedicated GitHub OIDC identities.

## Deployment authority and prerequisites

The owner enrollment template and console procedure are in
[`infra/bootstrap`](../infra/bootstrap/README.md). The template creates only
the new deployment role; CloudFormation owns that role, while Terraform keeps
ownership of the existing viewer/worker infrastructure. Its creation requires
the authorized owner's reviewed execution and is not a workforce-seat action.

An authorized infrastructure owner must create a dedicated GitHub OIDC role in
account `730649732189`, region `us-east-1`. The role must trust only the GitHub
subject `repo:Reg42-ai/CLHEAR-MVP:environment:clhear-l1`, with audience
`sts.amazonaws.com`. Configure a three-hour maximum session duration. Protect
the GitHub environment `clhear-l1` with main-only deployments. The owner's PR
merge is the authorization for routine deployment; the environment has no
second required-reviewer approval. Set its variables:

- `CLHEAR_DEPLOY_ROLE_ARN`: the approved dedicated role ARN.
- `CLHEAR_REVIEWER_EMAILS`: the exact approved comma-separated viewer emails.

The existing `clhear-github-release` role is a release publisher; its current
policy has no ECS/Lambda deployment permissions. Do not reuse a different
product's deployment identity, static administrator credentials, or an agent
shell Terraform apply. This workflow creates no IAM roles, databases, networks,
queues, or infrastructure stacks. No database credential is stored in GitHub.

The dedicated role needs code-deployment actions limited to these resources:

- ECR authorization and image push/read for `clhear-workers`.
- S3 bucket privacy/versioning checks and versioned code/evidence reads/writes
  under `clhear-deploy-730649732189/webui/` and `deployments/l1/`.
- ECS describe/list/register/update/run/stop for `clhear-cluster` and its nine
  `clhear-fleet-l0` through `clhear-fleet-l8` services/task families, including
  task tagging. Pass only their existing worker execution and task roles.
- Application Auto Scaling describe/register for those fleet service targets.
- EventBridge list/describe and target updates for the existing
  `clhear-adapter-*` rules on the default bus; no rule creation or cron changes.
- Lambda get/code/configuration/concurrency/invoke for `clhear-webui` only.
- Read the `clhear-record` cluster configuration and the existing worker
  DATABASE_URL secret reference, including decryption where required.
- Read/simulate the viewer's existing IAM permissions to verify its L0 queue
  routing before cutover; the controller must not grant those permissions.

Where an AWS action cannot be resource-scoped, constrain supported request
conditions and keep its dependent PassRole/resource actions restricted. The
deployment controller validates the exact account, workflow, commit, assumed
role and fleet identities before a write. It never records secret values.

Existing worker roles must read/write their normal Aurora, artifact, queue and
private viewer resources. The viewer must have a strong existing session
secret, a functioning existing sign-in provider and private snapshot read
access. Missing prerequisites fail before changing live service capacity.
Its `CommunityWrite` commands belong to L0. The authorized infrastructure
owner must align the viewer's queue and `sqs:SendMessage` grant with that L0
queue; the CLHEAR Terraform definition now records the correct owner. All
worker queue maps must likewise retain their actual owning-fleet routes.
Source authorization evidence remains a separate L0 review process; the
deployment never manufactures grants or approval records.

## Automatic deployment and verification

Protect main with required PRs, passing `tests`, `a11y-axe` and `terraform`
checks, administrator enforcement, and no force pushes or branch deletion.
Require zero additional approving reviews, no code-owner review and no
last-push approval, so the sole owner can merge their own PR after CI. Keep
the environment's exact main branch rule and disabled administrator bypass.
The verified repository owner is `Reg42-ai` (numeric ID `250850977`); only that
owner is currently listed as a write-capable collaborator. Reassess merge
authority before adding writers or installing applications with write access.

Merge the reviewed CLHEAR changes. Successful main CI automatically starts
`deploy-l1`; no Run workflow click or environment approval is required. Its
read-only eligibility job requires the exact current main SHA, a successful
main-push CI run with all three required jobs, and an associated PR whose final
merge SHA matches and whose merger is the numeric owner identity. Failed CI,
PR/fork CI, direct pushes and superseded commits cannot deploy. The deployed
SHA is never substituted into GitHub's trusted workflow context.

Only eligible deployment jobs enter the shared deployment concurrency group.
Eligibility is checked again after acquiring it and immediately before runtime
cutover. An active cutover is never cancelled by a newer merge. Completed
same-commit automatic deployments are not repeated; manual dispatch on main
remains available for deliberate recovery with the same authorization checks.
There is no arbitrary deployment ref input. The workflow:

1. Builds locked dependencies into an ECR image referenced by digest and an
   immutable, versioned Lambda ZIP built on the Lambda Python 3.12 runtime.
   Neither bundle contains a corpus database.
   The worker image includes the existing clause-boundary golden fixtures and
   runs CLHEAR's offline boundary evaluator with network disabled during the
   build. Missing or failing fixtures stop the build before any runtime cutover.
   These are evaluation inputs, not accepted regulatory records.
2. Checks the live Aurora writer configuration, private/versioned storage,
   fleet identities, reviewer access and code artifact hashes. Archives the
   previous Lambda code and non-secret rollback metadata privately.
3. Pauses viewer traffic and fleet consumers/autoscaling, then installs the
   new worker task definitions with `CLHEAR_L1_ONLY=true`. It preserves the
   existing roles, secrets and worker networking. Existing adapter targets
   receive the same occurrence-ID/time transformer as the Terraform
   configuration, preserving queue destinations and schedule expressions.
   Lambda can change its revision when traffic is paused. The controller
   refreshes that revision only after confirming the code and configuration
   still match the reviewed baseline, then uses conditional updates at cutover.
   Code and configuration updates are asynchronous: their initial response
   revision can differ from the revision after completion. After each update,
   the controller waits for success and verifies the expected code hash and
   unchanged configuration, including any intended environment change. It uses
   that verified completed revision for the next conditional write. Only
   documented service-generated update fields are excluded from the comparison;
   unknown fields remain checked. The completed configuration is checked again
   before viewer traffic resumes.
4. Runs L0 `bootstrap`: migrations and registered-source metadata, followed by
   a worker-built candidate snapshot. Switches Lambda code/configuration to
   restricted review against that candidate.
5. Runs L1 `verify`: inventory audit, FINRA ingestion, and an immediate repeat
   through the normal durable handlers. Runs L0 `publish` to expose results.
6. Restores the reviewed viewer capacity and verifies it by readback before
   checking anonymous health/access behavior. An originally unreserved viewer
   uses `DeleteFunctionConcurrency`, without requesting a temporary reserved
   slot. Positive reservations are restored exactly. An explicitly paused
   viewer uses the existing temporary probe slot and is paused again; failure
   never silently widens an explicit reservation to shared capacity. It then
   restores the fleet's new held code. L0 retains minimum capacity one because database
   outbox events cannot wake an SQS-only autoscaler.

The worker entrypoint for each phase is:

```sh
python -m app.clhear.workers --verify-deployment bootstrap --verification-id ID
python -m app.clhear.workers --verify-deployment verify --verification-id ID
python -m app.clhear.workers --verify-deployment publish --verification-id ID
```

Run bootstrap/publish as L0 and verify as L1, in the existing worker environment.
The workflow launches these as ECS tasks. Do not run them against local files or
substitute a separate import script. Each deployment uses a new ID; retries of
the same worker phase retain its durable child event identities.

`verify` exit 0 means the FINRA candidate and unchanged-run check passed; exit 2
means review evidence is available with explicit coverage/permission gaps; exit
1 means an operational check failed. Bootstrap/publish require exit 0. The
controller permits review-ready deployment without calling it accepted L1.
Two entirely blocked imports cannot pass the unchanged-import check.

AWS describes these transitions in [function update states](https://docs.aws.amazon.com/lambda/latest/dg/functions-states.html#functions-states-updates).
The `RevisionId` argument to [UpdateFunctionCode](https://docs.aws.amazon.com/lambda/latest/api/API_UpdateFunctionCode.html)
guards the write; its response is not a promise that the revision stays unchanged
through asynchronous completion. Either update can also install a newer
[managed runtime patch](https://docs.aws.amazon.com/lambda/latest/dg/runtimes-update.html).
Regression tests exercise the SDK waiter's pending/success/failure states,
completion revision changes and unexpected configuration or code changes.
They also reproduce the account's concurrency constraint: all 10 regional slots
must remain unreserved, so even reserving one slot fails. Restoring the original
shared-capacity policy requires no quota increase or additional IAM grant.

## Viewer acceptance and later daily validation

Sign in with an approved reviewer and verify the homepage → L1 Sources → Fleet
run → original document → clause inspector → bound evaluation evidence journey.
Inspect historical versions, denied text, unresolved inventory items, workflow
durations and the candidate's origin. The automated anonymous probes complement
this authenticated review; they do not claim an authenticated live walkthrough.

Deployment evidence explicitly records `nightly_schedule_validation: pending`.
Keep the full source inventory denominator and the downstream hold. Check the
next two actual 00:00 UTC scheduled cycles later for occurrence identity,
publisher freshness, unchanged imports, retries and the L0 viewer refresh.
Manual runs are not evidence that EventBridge fired. Scheduling configuration
and full L1 acceptance remain independently verifiable requirements.
The pre-deployment FINRA target used a fixed `schedule-finra` event ID and an
empty timestamp. Already-queued legacy messages remain explicit failures;
they must not be relabelled as fresh runs or purged to make a check green.

The GitHub artifact contains code hashes, task references, measured durations,
deployment outcome and a capacity-only recovery plan. Treat these as repository
deployment metadata, not as a private corpus store. Source text, database URLs and full Lambda
environment values stay out of GitHub logs/artifacts. Detailed data evidence
is stored by the workers and exposed through the restricted viewer.

## Failure and recovery

Preserve Aurora, all history and the accepted-release pointer on failure.
Failures during cutover keep traffic and consumers paused. Previous code can
be restored only under a confirmed hold; never reactivate the old public
viewer against internal-text candidate data. The private rollback manifest
records previous task definitions, image references, code version, capacity
and autoscaling state. It intentionally omits secret plaintext.

Read `artifacts/deployment-result.json` and the referenced private recovery
manifest. A fully paused failed deployment must not be rerun using the observed
zero capacities as the intended restoration target. In a confirmed full hold,
the controller selects the owner-merged `infra/recovery/active-plan.json` plan
automatically. Its only field is `plan_id`; a missing file or null disables
automatic selection. The workflow's optional `recovery_plan` input overrides
the default. Healthy deployments ignore the default. Invalid selections,
missing plans, mismatched resource/code bindings or active one-off tasks stop
deployment before writes; the controller never searches for another plan.

The recovery plan records exact resource and starting-code identities, the
original desired/minimum/maximum capacities and scaler flags, the original
nullable viewer reservation, and its rollback object's version/hash. The
controller checks the current held state and identities before any write.
It backs up the observed configuration separately and applies the approved
capacity targets only after worker verification and viewer access checks pass.
The normal L0 minimum of one and the L1-only processing hold still apply.

The deployment role does not read the private source rollback JSON. Provenance
in a committed plan is an operator audit record; founder review/merge of that
configuration authorizes its use through the main-only environment. No new IAM permission
is granted. Future attempts emit `artifacts/recovery-plan.json`; review its
capacity-only contents and source binding into `infra/recovery/<plan-id>.json`
before using it. Repeated failures retain the approved original targets instead
of replacing them with the temporary maintenance zeros.

For the current incident, the active selection is `l1-34967901665-1`. Its audited
plan restores L0/L1 desired capacity one, L2–L8 zero, and removes the temporary
viewer concurrency reservation before access checks. L0 bootstrap already applied migrations 24–26;
recovery reruns that work through the same idempotent L0 worker handler.
The subsequent failed attempt `l1-34973791949-1` preserved those same original
targets. Its L0 bootstrap completed, then the old completion-revision check
rejected the Lambda update and rolled back code. The original reviewed recovery
plan remains usable only if fresh preflight verifies its exact resource/code
bindings and maintenance hold; a failed-run report alone does not establish that.
Attempt `l1-34978249013-1` verified both Lambda updates, completed L0 bootstrap
and publish, and returned L1 `review_ready` with acceptance pending. Traffic
restoration then attempted an unnecessary reservation of one, which AWS rejected
because the account's 10 slots must stay shared. Code rollback succeeded and
preserved the same original recovery bindings. The concurrency fix addresses
that specific failure without treating the pending L1 evidence as accepted.

Correct the failed prerequisite or worker and merge the fix; successful main CI
starts the fresh attempt. A future failure against newer starting code needs
its emitted recovery plan and updated active selection in the next owner-merged
fix. Do not loosen old bindings or adopt temporary zero capacities to avoid that
review. If restoring previous behavior manually, the authorized
operator must independently verify its authentication, compatible snapshot
and worker hold before resuming traffic; code rollback alone is insufficient.
An interrupted GitHub job can leave maintenance active. Check the live holds
and in-flight ECS tasks before recovery; do not purge or blindly replay queues.

Reconcile later infrastructure changes with the deployed task/image/snapshot
settings. The old local `terraform.auto.tfvars` is not live infrastructure
truth and must not disable Aurora or restore a mutable worker tag. The retired
`scripts/deploy_webui.sh` refuses execution and points here.
