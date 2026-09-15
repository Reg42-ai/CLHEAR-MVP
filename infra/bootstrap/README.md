# Owner enrollment of the CLHEAR deployment role

`clhear-deployment-role.json` is an owner-reviewed CloudFormation template for
account `730649732189`, region `us-east-1`. It creates one resource: the
`clhear-github-deploy` IAM role and its inline deployment policy. It reuses the
existing GitHub OIDC provider. It does not deploy application code or change
Aurora, networks, source permissions, fleet services, or existing IAM roles.

CloudFormation owns this new role. CLHEAR Terraform continues to own the
existing worker/viewer resources. Do not also declare or import this role in
Terraform without an explicit ownership transfer. Review and founder-merge
privileged infrastructure changes before execution, under the workforce
contract. A workforce seat must not apply this template or grant itself access.

## Review the authority being granted

Only GitHub jobs in `Reg42-ai/CLHEAR-MVP` using environment `clhear-l1` may
assume this role, with audience `sts.amazonaws.com` and sessions up to three
hours. The environment must retain the founder reviewer, disabled administrator
bypass, and the exact `main` branch rule. The OIDC environment subject does not
itself encode a branch or workflow filename. Workflow changes therefore remain
privileged review items; `deploy-l1.yml` additionally checks its repository,
branch, exact workflow and tested commit before deployment.

The role can publish worker/UI code, pause and update CLHEAR fleet services and
the viewer, run L0/L1 verification tasks, and update the targets of the existing
adapter schedules. It can pass only the existing CLHEAR worker execution/task
roles to ECS. It cannot provision IAM roles or policies, change database
infrastructure, or directly update deployment resources outside the enumerated
CLHEAR resources.

Read permissions include the existing database connection parameter for
configuration validation, Lambda configuration, and the candidate viewer
object (S3 uses GetObject permission for the controller's HEAD request). Treat
this as a production deployment identity. The controller keeps credential
values and corpus text out of logs; all imports, migrations, evaluations and
viewer snapshot construction remain CLHEAR worker operations.

These restrictions describe the role's direct AWS API grants. The role can
replace and execute application code with the existing runtime roles, and it
can read database credentials. Its effective authority therefore includes
runtime effects on application data and releases. The L1 hold, worker-only
operations and accepted-release protections rely on reviewed code as well as
IAM; they are not guaranteed solely by this deployment policy.

Some AWS read actions require `Resource: "*"`: task-definition descriptions,
scaling-target discovery, schedule listing and ECR authentication. These can
expose account metadata beyond CLHEAR, even though the controller reads its
configured CLHEAR resources. Review those grants along with the scoped writes.

The policy pins the existing autoscaling target ARNs and SSM KMS key discovered
on 2026-09-15. Reconcile the template if these resources are recreated. Do not
replace exact resources with broad wildcards to work around drift. If the
platform requires a permissions boundary, supply its approved existing ARN.

The repository was created before GitHub's July 2026 immutable-subject rollout
and currently uses the default OIDC subject customization. If its subject
format changes through opt-in, rename or transfer, review the trust policy
against the actual new subject before deployment. Never allow arbitrary
repositories or environment subjects as a fallback.

## Founder console procedure

1. Review the template and its PR. Sign in with the existing authorized owner
   identity; confirm account `730649732189` and select US East (N. Virginia).
2. Open CloudFormation, choose **Create stack → With new resources (standard)**,
   then **Choose an existing template → Upload a template file**. Select
   `clhear-deployment-role.json` and choose **Next**.
3. Use stack name `clhear-deployment-access`. Supply an existing required
   permissions boundary only if applicable. Keep any service-role field empty
   unless an approved bootstrap executor has explicitly been provided; the
   deployment role being created is not a bootstrap executor.
4. Review the named-IAM acknowledgement. Create a change set to inspect the
   proposed resources. The only resource addition must be the deployment role;
   no existing resource should be modified or removed. Execute after founder
   review/merge and approval of this exact change set.
5. Wait for `CREATE_COMPLETE`. Copy output `DeploymentRoleArn` into the GitHub
   environment variable `CLHEAR_DEPLOY_ROLE_ARN` for `clhear-l1`. A role ARN is
   configuration, not a static AWS access key.

Uploading a template through the CloudFormation console can create/use its
template-storage S3 bucket. This is console-managed storage, not a resource
declared in this template. Do not upload corpus data or credentials.

## Remaining prerequisite and deployment

Role creation alone does not finish setup. The existing `clhear-webui` role
must allow `sqs:SendMessage` to `clhear-fleet-l0`. That correction belongs to
its Terraform-owned `clhear-webui-db` policy in `infra/webui.tf`; reconcile it
through the authorized owner process, preserving the other grants. This
bootstrap template deliberately does not manage that existing policy.

Then follow [the deployment runbook](../../docs/L1_DEPLOYMENT.md). Merge the
reviewed application changes, wait for CI on the resulting main commit, and
approve the `deploy-l1` run. Keep L2 held and nightly validation pending until
the separate acceptance evidence is available.

## Validation boundaries

The [2026-09-15 validation record](validation-2026-09-15.json) binds the
checks to the template hash: four local tests, AWS template validation, no
identity-policy findings, two reviewed non-error trust-policy findings, and
14 selected permission simulations. It is not a deployment success record.

CloudFormation template validation and IAM Access Analyzer policy validation
check structure and policy findings. Policy simulations check selected allowed
and denied requests. These checks do not prove that an OIDC exchange or an
end-to-end deployment has succeeded. Record those outcomes separately after
the owner grant and actual workflow execution.
