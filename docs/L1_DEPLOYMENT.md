# Deploy and verify L1 without waiting for midnight

`deploy-l1` deploys code and runs the owning CLHEAR workers immediately. It does
not grant document permissions, accept the full FINRA inventory, promote a
release, or enable downstream processing. Its database is the existing
`clhear-record` Aurora cluster. The Lambda viewer receives a separate private
candidate at `webui/l1/candidate.db`; the legacy snapshot is not overwritten.

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
the GitHub environment `clhear-l1` with main-only deployments and the required
deployment reviewer. Set its variables:

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

## Dispatch and verification

Merge the reviewed CLHEAR changes and wait for the exact main commit's `tests`,
`a11y-axe` and `terraform` CI jobs to pass. Manually dispatch `deploy-l1` on main.
There is no arbitrary deployment ref input. The workflow:

1. Builds locked dependencies into an ECR image referenced by digest and an
   immutable, versioned Lambda ZIP built on the Lambda Python 3.12 runtime.
   Neither bundle contains a corpus database.
2. Checks the live Aurora writer configuration, private/versioned storage,
   fleet identities, reviewer access and code artifact hashes. Archives the
   previous Lambda code and non-secret rollback metadata privately.
3. Pauses viewer traffic and fleet consumers/autoscaling, then installs the
   new worker task definitions with `CLHEAR_L1_ONLY=true`. It preserves the
   existing roles, secrets and worker networking. Existing adapter targets
   receive the same occurrence-ID/time transformer as the Terraform
   configuration, preserving queue destinations and schedule expressions.
4. Runs L0 `bootstrap`: migrations and registered-source metadata, followed by
   a worker-built candidate snapshot. Switches Lambda code/configuration to
   restricted review against that candidate.
5. Runs L1 `verify`: inventory audit, FINRA ingestion, and an immediate repeat
   through the normal durable handlers. Runs L0 `publish` to expose results.
6. Checks anonymous health/access behavior, restores viewer capacity and the
   fleet's new held code. L0 retains minimum capacity one because database
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

## Viewer acceptance and later daily validation

Sign in with an approved reviewer and verify the homepage → L1 Sources → Fleet
run → original document → clause inspector → bound evaluation evidence journey.
Inspect historical versions, denied text, unresolved inventory items, workflow
durations and the candidate's origin. The automated anonymous probes complement
this authenticated review; they do not claim an authenticated live walkthrough.

Manual evidence explicitly records `nightly_schedule_validation: pending`.
Keep the full source inventory denominator and the downstream hold. Check the
next two actual 00:00 UTC scheduled cycles later for occurrence identity,
publisher freshness, unchanged imports, retries and the L0 viewer refresh.
Manual runs are not evidence that EventBridge fired. Scheduling configuration
and full L1 acceptance remain independently verifiable requirements.
The pre-deployment FINRA target used a fixed `schedule-finra` event ID and an
empty timestamp. Already-queued legacy messages remain explicit failures;
they must not be relabelled as fresh runs or purged to make a check green.

The GitHub artifact contains code hashes, task references, measured durations
and deployment outcome only. Source text, database URLs and full Lambda
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
manifest. Correct the failed prerequisite or worker, then dispatch a fresh
deployment attempt. If restoring previous behavior manually, the authorized
operator must independently verify its authentication, compatible snapshot
and worker hold before resuming traffic; code rollback alone is insufficient.
An interrupted GitHub job can leave maintenance active. Check the live holds
and in-flight ECS tasks before recovery; do not purge or blindly replay queues.

Reconcile later infrastructure changes with the deployed task/image/snapshot
settings. The old local `terraform.auto.tfvars` is not live infrastructure
truth and must not disable Aurora or restore a mutable worker tag. The retired
`scripts/deploy_webui.sh` refuses execution and points here.
