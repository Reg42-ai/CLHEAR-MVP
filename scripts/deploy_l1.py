"""Guarded CLHEAR L1 deployment control plane; no corpus/database operations.

Without --apply this performs read-only preflight. Mutations require the pinned
GitHub Actions workflow, environment and assumed deployment role. All data work
is executed by the owning CLHEAR fleet through its deployment verification CLI.
Failures leave traffic paused and every fleet held; code rollback never resumes
legacy consumers or changes an accepted-release pointer.
"""
from __future__ import annotations

import argparse
import base64
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlparse
import urllib.request

import boto3
from botocore.exceptions import ClientError

if __package__:
    from .deployment_recovery import RecoveryPlanError, emit_plan, load_plan, validate_plan
    from .l1_recovery import load_active_plan_id
else:
    from deployment_recovery import RecoveryPlanError, emit_plan, load_plan, validate_plan
    from l1_recovery import load_active_plan_id

ACCOUNT = "730649732189"
REGION = "us-east-1"
CLUSTER = "clhear-cluster"
FUNCTION = "clhear-webui"
BUCKET = f"clhear-deploy-{ACCOUNT}"
FLEETS = tuple(f"l{i}" for i in range(9))
ALWAYS_ON_FLEETS = frozenset({"l0", "l1"})
WORKER_ENTRYPOINT = ("python", "-m", "app.clhear.workers")
ENVIRONMENT = "clhear-l1"
WORKFLOW = "Reg42-ai/CLHEAR-MVP/.github/workflows/deploy-l1.yml@refs/heads/main"
RELEASES = f"s3://{BUCKET}/releases"
QUEUES = {fleet: f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/" + ("clhear-events" if fleet == "l1" else f"clhear-fleet-{fleet}") for fleet in FLEETS}
ADAPTER_SCHEDULES = tuple("uk_legislation eur_lex govinfo_us fca_handbook sec_edgar fca_enforcement sec_enforcement finra_enforcement esma bis_basel iosco asic isa au_legislation sg_legislation finra adgm nydfs nasdaq malta uae cysec mas fatf wolfsberg irs_gov lists overlay restricted_file seychelles gibraltar israel".split())
TASK_FIELDS = {
    "family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions",
    "volumes", "placementConstraints", "requiresCompatibilities", "cpu", "memory",
    "tags", "pidMode", "ipcMode", "proxyConfiguration", "inferenceAccelerators",
    "ephemeralStorage", "runtimePlatform", "enableFaultInjection",
}
SUSPENDED = {"DynamicScalingInSuspended": True, "DynamicScalingOutSuspended": True,
             "ScheduledScalingSuspended": True}
# These service-generated outputs can change while an accepted update finishes.
# RuntimeVersionConfig is the managed runtime patch, not the Runtime setting:
# https://docs.aws.amazon.com/lambda/latest/dg/runtimes-update.html
LAMBDA_UPDATE_OUTPUTS = {"ResponseMetadata", "RevisionId", "LastModified", "ConfigSha256",
    "LastUpdateStatus", "LastUpdateStatusReason", "LastUpdateStatusReasonCode",
    "State", "StateReason", "StateReasonCode", "RuntimeVersionConfig"}


class DeploymentError(RuntimeError):
    """An operator-readable error that never contains secret/configuration values."""


def require(condition, message):
    if not condition:
        raise DeploymentError(message)


def _failure_details(error):
    details = {"failure_type": type(error).__name__}
    if isinstance(error, DeploymentError):
        details["reason"] = str(error)
    elif isinstance(error, ClientError):
        # Never include SDK messages or request parameters: they can contain
        # environment secrets, credentials or signed artifact URLs.
        for key, value in (("failure_operation", error.operation_name),
                           ("failure_code", error.response.get("Error", {}).get("Code"))):
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", value):
                details[key] = value
    return details


def _supported_worker_invocation(worker):
    # Recognize an explicit argv split, without interpreting shell syntax or
    # guessing an entrypoint inherited from an uninspected legacy image.
    entrypoint, command = worker.get("entryPoint"), worker.get("command", [])
    return (isinstance(entrypoint, list) and bool(entrypoint) and isinstance(command, list)
            and entrypoint + command == list(WORKER_ENTRYPOINT))


def digest_stream(stream, limit=300 * 1024 * 1024):
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(1024 * 1024):
        size += len(chunk)
        require(size <= limit, "Code artifact exceeds the allowed size")
        digest.update(chunk)
    require(size > 0, "Code artifact is empty")
    return digest.hexdigest()


@dataclass(frozen=True)
class Inputs:
    sha: str
    image: str
    ui_key: str
    ui_sha256: str
    ui_version: str
    deployment_id: str
    reviewer_emails: str = ""
    viewer_key: str = "webui/l1/candidate.db"
    recovery_plan: str = ""

    def validate(self):
        require(bool(re.fullmatch(r"[0-9a-f]{40}", self.sha)), "A full tested Git SHA is required")
        require(bool(re.fullmatch(rf"{ACCOUNT}\.dkr\.ecr\.{REGION}\.amazonaws\.com/[a-z0-9._/-]+@sha256:[0-9a-f]{{64}}", self.image)),
                "An immutable ECR digest in the pinned account and region is required")
        require(bool(re.fullmatch(r"[0-9a-f]{64}", self.ui_sha256)), "UI SHA256 must be a full hex digest")
        require(self.ui_version not in {"", "null"}, "A versioned UI artifact is required")
        require(self.ui_key.startswith("webui/") and self.ui_key.endswith(".zip") and ".." not in self.ui_key,
                "UI artifact must be a zip in the private webui prefix")
        require(self.viewer_key.startswith("webui/l1/") and self.viewer_key.endswith(".db") and ".." not in self.viewer_key,
                "Candidate viewer must use the separate private webui/l1 prefix")
        require(bool(re.fullmatch(r"l1-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", self.deployment_id)), "Invalid deployment identifier")
        require(not self.recovery_plan or bool(re.fullmatch(r"l1-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", self.recovery_plan)),
                "Invalid recovery plan identifier")
        require(self.recovery_plan != self.deployment_id, "Recovery requires a fresh deployment attempt")


class Deployer:
    def __init__(self, inputs: Inputs, clients=None, *, environ=None, sleep=time.sleep, code_fetch=None):
        self.inputs = inputs
        self.env = dict(os.environ if environ is None else environ)
        session = None if clients is not None else boto3.Session(region_name=REGION)
        self.clients = clients or {name: session.client(name, region_name=REGION) for name in
                                   ("sts", "ecs", "application-autoscaling", "lambda", "s3", "ecr", "ssm", "secretsmanager", "rds", "sqs", "iam", "events")}
        self.sleep = sleep
        self.code_fetch = code_fetch or self._download_code
        self.authorized = False
        self.state = None
        self.recovery_plan_output = None
        self.report = {"deployment_id": inputs.deployment_id, "sha": inputs.sha,
                       "account": ACCOUNT, "region": REGION, "accepted_release_changed": False,
                       "steps": [], "status": "not_started"}
        self.prefix = f"deployments/l1/{inputs.deployment_id}"

    def _write(self, client, operation, **kwargs):
        require(self.authorized, "AWS mutations require the guarded apply path")
        return getattr(self.clients[client], operation)(**kwargs)

    def _database_recovery_evidence(self, cluster=None):
        """Read existing PITR coverage; never create or restore a database here."""
        if cluster is None:
            clusters = self.clients["rds"].describe_db_clusters(DBClusterIdentifier="clhear-record")["DBClusters"]
            require(len(clusters) == 1, "The authoritative Aurora cluster is unavailable")
            cluster = clusters[0]
        require(cluster.get("DBClusterIdentifier") == "clhear-record"
                and cluster.get("Engine") == "aurora-postgresql" and cluster.get("Status") == "available",
                "The authoritative Aurora cluster is unavailable")
        retention = cluster.get("BackupRetentionPeriod")
        require(isinstance(retention, int) and not isinstance(retention, bool) and retention >= 1,
                "Aurora automated backup retention must be enabled before worker migrations")
        earliest, latest = cluster.get("EarliestRestorableTime"), cluster.get("LatestRestorableTime")
        require(all(isinstance(value, datetime) and value.tzinfo is not None
                    and value.utcoffset() is not None for value in (earliest, latest)),
                "Aurora must expose an existing point-in-time recovery window before worker migrations")
        now = datetime.now(timezone.utc)
        require(earliest <= latest <= now + timedelta(minutes=5)
                and latest >= now - timedelta(hours=1),
                "Aurora point-in-time recovery evidence is invalid or more than one hour behind")
        evidence = {"cluster": "clhear-record", "checked_at": now.isoformat(),
                    "retention_days": retention, "earliest_restorable_time": earliest.isoformat(),
                    "latest_restorable_time": latest.isoformat(), "restore_test_performed": False,
                    "rollback_scope": "code_only; migrations and corpus versions are retained"}
        self.report["database_recovery"] = evidence
        return evidence

    def _guard(self):
        role = self.env.get("CLHEAR_DEPLOY_ROLE_ARN", "")
        require(bool(re.fullmatch(rf"arn:aws:iam::{ACCOUNT}:role/[A-Za-z0-9+=,.@_/-]+", role)),
                "CLHEAR_DEPLOY_ROLE_ARN must identify the approved account role")
        require(self.env.get("GITHUB_ACTIONS") == "true" and self.env.get("GITHUB_REPOSITORY") == "Reg42-ai/CLHEAR-MVP"
                and self.env.get("GITHUB_REF") == "refs/heads/main" and self.env.get("GITHUB_SHA") == self.inputs.sha
                and self.env.get("GITHUB_WORKFLOW_REF") == WORKFLOW
                and self.env.get("CLHEAR_DEPLOY_ENVIRONMENT") == ENVIRONMENT
                and self.env.get("ACTIONS_ID_TOKEN_REQUEST_URL") and self.env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN"),
                "Apply requires the pinned GitHub Actions main workflow and clhear-l1 environment")
        identity = self.clients["sts"].get_caller_identity()
        expected = f"arn:aws:sts::{ACCOUNT}:assumed-role/{role.rsplit('/', 1)[1]}/"
        require(identity.get("Account") == ACCOUNT and identity.get("Arn", "").startswith(expected),
                "The active AWS role does not match the approved deployment role")
        self.authorized = True

    def _source_bytes_hash(self, key, version):
        response = self.clients["s3"].get_object(Bucket=BUCKET, Key=key, VersionId=version, ExpectedBucketOwner=ACCOUNT)
        stream = response["Body"]
        try:
            return digest_stream(stream)
        finally:
            stream.close()

    def _dsn(self, reference, endpoint):
        # Decrypt only for configuration validation; neither the value nor parsed
        # credentials are included in errors, reports or rollback artifacts.
        if reference.startswith(f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/"):
            parameter = self.clients["ssm"].get_parameter(Name=reference, WithDecryption=True)["Parameter"]
            require(parameter.get("Type") == "SecureString", "DATABASE_URL must use a SecureString secret reference")
            value = parameter["Value"]
        elif reference.startswith(f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:"):
            value = self.clients["secretsmanager"].get_secret_value(SecretId=reference).get("SecretString", "")
            if value.startswith("{"):
                value = json.loads(value).get("DATABASE_URL", "")
        else:
            raise DeploymentError("DATABASE_URL must reference a secret in the pinned account and region")
        try:
            parsed = urlparse(value)
            valid = (parsed.scheme in {"postgresql", "postgresql+psycopg", "postgresql+psycopg2"}
                     and parsed.hostname == endpoint and parsed.port in {None, 5432}
                     and parsed.path == "/clhear" and parsed.username and parsed.password
                     and parse_qs(parsed.query).get("sslmode", [""])[0] in {"require", "verify-ca", "verify-full"})
        except (ValueError, TypeError):
            valid = False
        require(valid, "DATABASE_URL must target the live Aurora writer database with TLS; local fallback is forbidden")

    @staticmethod
    def _schedule_payload(adapter, *, event_id, event_time):
        return {"event_id": event_id, "layer": "l1", "kind": "AdapterRunRequested", "subject_ref": adapter,
                "payload": {"adapter": adapter}, "schema_version": 1, "producer": "eventbridge", "ts": event_time}

    @classmethod
    def _transformer(cls, adapter):
        template = json.dumps(cls._schedule_payload(adapter, event_id="__event_id__", event_time="__event_time__"), separators=(",", ":"))
        return {"InputPathsMap": {"event_id": "$.id", "event_time": "$.time"},
                "InputTemplate": template.replace('"__event_id__"', "<event_id>").replace('"__event_time__"', "<event_time>")}

    @classmethod
    def _valid_transformer(cls, adapter, transformer):
        if transformer.get("InputPathsMap") != {"event_id": "$.id", "event_time": "$.time"}:
            return False
        try:
            template = transformer["InputTemplate"].replace("<event_id>", '"example-id"').replace("<event_time>", '"example-time"')
            return json.loads(template) == cls._schedule_payload(adapter, event_id="example-id", event_time="example-time")
        except (KeyError, TypeError, ValueError):
            return False

    def _schedule_target(self, name):
        targets, token = [], None
        while True:
            args = {"Rule": name, "EventBusName": "default"}
            if token:
                args["NextToken"] = token
            page = self.clients["events"].list_targets_by_rule(**args)
            targets.extend(page.get("Targets", []))
            token = page.get("NextToken")
            if not token:
                break
        require(len(targets) == 1, "Each existing adapter schedule must have exactly one known queue target")
        return targets[0]

    def _schedule_preflight(self):
        rules, token = [], None
        while True:
            args = {"NamePrefix": "clhear-adapter-", "EventBusName": "default", "Limit": 100}
            if token:
                args["NextToken"] = token
            page = self.clients["events"].list_rules(**args)
            rules.extend(page.get("Rules", []))
            token = page.get("NextToken")
            if not token:
                break
        expected = {f"clhear-adapter-{adapter}" for adapter in ADAPTER_SCHEDULES}
        require(len(rules) == len(expected) and {rule.get("Name") for rule in rules} == expected,
                "Existing adapter schedule inventory differs from the 32 declared rules; owner review is required")
        schedules = []
        for adapter in ADAPTER_SCHEDULES:
            name = f"clhear-adapter-{adapter}"
            rule = self.clients["events"].describe_rule(Name=name, EventBusName="default")
            require(rule.get("Arn") == f"arn:aws:events:{REGION}:{ACCOUNT}:rule/{name}"
                    and rule.get("ScheduleExpression") and rule.get("State") in {"ENABLED", "DISABLED"},
                    "Adapter schedule identity or configuration is invalid")
            require(rule["State"] == "ENABLED" and rule["ScheduleExpression"] == "cron(0 0 * * ? *)",
                    "Every L1 adapter must be enabled on its daily 00:00 UTC schedule")
            target = self._schedule_target(name)
            require(target.get("Id") and target.get("Arn") == f"arn:aws:sqs:{REGION}:{ACCOUNT}:clhear-events"
                    and set(target) <= {"Id", "Arn", "RoleArn", "Input", "InputTransformer", "RetryPolicy", "DeadLetterConfig", "SqsParameters"},
                    "Adapter schedule has an unsupported or unexpected target")
            current = "Input" not in target and self._valid_transformer(adapter, target.get("InputTransformer", {}))
            if not current:
                try:
                    legacy = json.loads(target.get("Input", ""))
                except (TypeError, ValueError):
                    legacy = None
                require("InputTransformer" not in target and legacy == self._schedule_payload(adapter, event_id=f"schedule-{adapter}", event_time=""),
                        "Adapter schedule payload is neither the known legacy form nor the correct dynamic transformer")
            replacement = {k: copy.deepcopy(v) for k, v in target.items() if k not in {"Input", "InputTransformer"}}
            replacement["InputTransformer"] = self._transformer(adapter)
            schedules.append({"name": name, "adapter": adapter, "rule_arn": rule["Arn"], "state": rule["State"],
                              "schedule_expression": rule["ScheduleExpression"], "previous_target": target,
                              "replacement": replacement, "needs_update": not current})
        return schedules

    def preflight(self):
        self.inputs.validate()
        identity = self.clients["sts"].get_caller_identity()
        require(identity.get("Account") == ACCOUNT, "Wrong AWS account")
        s3 = self.clients["s3"]
        access = s3.get_public_access_block(Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT)["PublicAccessBlockConfiguration"]
        require(all(access.get(key) is True for key in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")),
                "Deployment bucket must block all public access")
        require(s3.get_bucket_versioning(Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT).get("Status") == "Enabled",
                "Deployment bucket versioning must be enabled")
        schedules = self._schedule_preflight()
        head = s3.head_object(Bucket=BUCKET, Key=self.inputs.ui_key, VersionId=self.inputs.ui_version, ExpectedBucketOwner=ACCOUNT)
        require(head.get("Metadata", {}).get("git-sha") == self.inputs.sha, "UI artifact does not declare the tested Git SHA")
        require(self._source_bytes_hash(self.inputs.ui_key, self.inputs.ui_version) == self.inputs.ui_sha256,
                "UI artifact bytes do not match the requested SHA256")
        repository, image_digest = self.inputs.image.split("/", 1)[1].split("@")
        image = self.clients["ecr"].describe_images(registryId=ACCOUNT, repositoryName=repository,
                    imageIds=[{"imageDigest": image_digest}])["imageDetails"]
        require(len(image) == 1 and image[0].get("imageDigest") == image_digest
                and any(re.fullmatch(re.escape(self.inputs.sha) + r"-[0-9]+-[0-9]+", tag)
                        for tag in image[0].get("imageTags", [])), "Worker image is not bound to the tested Git SHA and run attempt")
        clusters = self.clients["rds"].describe_db_clusters(DBClusterIdentifier="clhear-record")["DBClusters"]
        require(len(clusters) == 1 and clusters[0].get("Engine") == "aurora-postgresql"
                and clusters[0].get("Status") == "available", "The authoritative Aurora cluster is unavailable")
        self._database_recovery_evidence(clusters[0])
        endpoint = clusters[0]["Endpoint"]
        for fleet, url in QUEUES.items():
            attributes = self.clients["sqs"].get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]
            expected = f"arn:aws:sqs:{REGION}:{ACCOUNT}:{url.rsplit('/', 1)[1]}"
            require(attributes.get("QueueArn") == expected, "A fleet queue is missing or has an unexpected identity")
        services = self.clients["ecs"].describe_services(cluster=CLUSTER, services=[f"clhear-fleet-{f}" for f in FLEETS])
        require(not services.get("failures") and len(services.get("services", [])) == len(FLEETS), "All nine fleet services must exist")
        by_name = {s["serviceName"]: s for s in services["services"]}
        fleets, checked_dsn = {}, set()
        for fleet in FLEETS:
            service = by_name.get(f"clhear-fleet-{fleet}")
            require(service and service.get("status") == "ACTIVE", "A fleet service is not active")
            definition = self.clients["ecs"].describe_task_definition(taskDefinition=service["taskDefinition"], include=["TAGS"])
            task = definition["taskDefinition"]
            task["tags"] = definition.get("tags", task.get("tags", []))
            require(task.get("family") == f"clhear-fleet-{fleet}" and task.get("networkMode") == "awsvpc"
                    and "FARGATE" in task.get("requiresCompatibilities", []), "Unexpected fleet task definition")
            for field in ("taskRoleArn", "executionRoleArn"):
                require(task.get(field, "").startswith(f"arn:aws:iam::{ACCOUNT}:role/"), "Fleet roles must remain in the pinned account")
            network = service.get("networkConfiguration", {}).get("awsvpcConfiguration", {})
            require(network.get("subnets") and network.get("securityGroups"), "Fleet network configuration is missing")
            workers = [c for c in task["containerDefinitions"] if c["name"] == "worker"]
            require(len(workers) == 1, "Each fleet requires exactly one worker container")
            worker = workers[0]
            require(_supported_worker_invocation(worker), "Unsupported worker entrypoint")
            environment = {v["name"]: v["value"] for v in worker.get("environment", [])}
            secrets = {v["name"]: v["valueFrom"] for v in worker.get("secrets", [])}
            require(environment.get("CLHEAR_FLEET") == fleet.upper(), "Fleet identity does not match its service")
            require(environment.get("CLHEAR_EVENTS_QUEUE_URL") == QUEUES[fleet], "A fleet is not bound to its owning queue")
            # Known legacy tasks omit the relay map. The owning queue and all
            # queue identities are checked above; registration adds the map.
            # An explicitly configured conflicting or malformed map is unsafe.
            if "CLHEAR_FLEET_QUEUE_URLS" in environment:
                try:
                    queue_map = json.loads(environment["CLHEAR_FLEET_QUEUE_URLS"])
                except (ValueError, TypeError):
                    queue_map = None
                require(queue_map == QUEUES, "All nine owning queues must be configured for the L0 outbox relay")
            require(environment.get("CLHEAR_LLM_PROVIDER", "").lower() != "fake", "Production workers cannot use fake-provider mode")
            require("DATABASE_URL" not in environment and secrets.get("DATABASE_URL"), "DATABASE_URL must be supplied only through a secret reference")
            require(environment.get("CLHEAR_SNAPSHOT_S3_URI", "") == "" and "CLHEAR_SNAPSHOT_S3_URI" not in secrets,
                    "Worker snapshot fallback must be disabled before deployment")
            require(not secrets.keys() & {"CLHEAR_L1_ONLY", "CLHEAR_FLEET", "CLHEAR_HTTP_MODE", "CLHEAR_ARTIFACT_STORE", "CLHEAR_EVENTS_QUEUE_URL", "CLHEAR_FLEET_QUEUE_URLS", "CLHEAR_VIEWER_SNAPSHOT_S3_URI", "CLHEAR_RELEASES_S3_PREFIX"},
                    "Deployment-controlled settings cannot be overridden through secrets")
            if secrets["DATABASE_URL"] not in checked_dsn:
                self._dsn(secrets["DATABASE_URL"], endpoint)
                checked_dsn.add(secrets["DATABASE_URL"])
            resource = f"service/{CLUSTER}/clhear-fleet-{fleet}"
            targets = self.clients["application-autoscaling"].describe_scalable_targets(ServiceNamespace="ecs",
                ResourceIds=[resource], ScalableDimension="ecs:service:DesiredCount")["ScalableTargets"]
            require(len(targets) == 1, "Every fleet needs an existing autoscaling target")
            fleets[fleet] = {"service": service, "task": task, "scaling": targets[0]}
        function = self.clients["lambda"].get_function(FunctionName=FUNCTION)
        config = function["Configuration"]
        require(config.get("State", "Active") == "Active" and config.get("LastUpdateStatus", "Successful") == "Successful",
                "The viewer has an unfinished Lambda update")
        require(config.get("PackageType", "Zip") == "Zip" and config.get("Handler") == "app.clhear.lambda_web.handler"
                and config.get("Role", "").startswith(f"arn:aws:iam::{ACCOUNT}:role/"), "Unexpected viewer runtime configuration")
        environment = config.get("Environment", {}).get("Variables", {})
        secret = environment.get("CLHEAR_SESSION_SECRET", "").strip()
        require(len(secret) >= 32 and "CHANGEME" not in secret.upper(), "Viewer requires a strong existing production session secret")
        reviewers = (self.inputs.reviewer_emails or environment.get("CLHEAR_REVIEWER_EMAILS")
                     or environment.get("CLHEAR_MAINTAINERS", ""))
        require(reviewers and all(re.fullmatch(r"[^\s,@]+@[^\s,@]+\.[^\s,@]+", x.strip()) for x in reviewers.split(",")),
                "An explicit approved reviewer allowlist is required")
        require(environment.get("CLHEAR_SES_SENDER") or
                (environment.get("GOOGLE_OAUTH_CLIENT_ID") and environment.get("GOOGLE_OAUTH_CLIENT_SECRET")) or
                (environment.get("CLHEAR_COGNITO_CLIENT_ID") and environment.get("CLHEAR_COGNITO_USER_POOL_ID") and environment.get("CLHEAR_COGNITO_DOMAIN")),
                "Viewer requires an existing production sign-in provider")
        require(config.get("CodeSha256") and function.get("Code", {}).get("Location"), "Previous viewer code cannot be backed up")
        queue_arn = f"arn:aws:sqs:{REGION}:{ACCOUNT}:clhear-fleet-l0"
        evaluation = self.clients["iam"].simulate_principal_policy(PolicySourceArn=config["Role"],
            ActionNames=["sqs:SendMessage"], ResourceArns=[queue_arn])["EvaluationResults"]
        require(len(evaluation) == 1 and evaluation[0].get("EvalDecision") == "allowed"
                and not evaluation[0].get("MissingContextValues")
                and evaluation[0].get("OrganizationsDecisionDetail", {}).get("AllowedByOrganizations", True),
                "Viewer role is not authorized to send CommunityWrite commands to L0; owner must update IAM first")
        concurrency = self.clients["lambda"].get_function_concurrency(FunctionName=FUNCTION).get("ReservedConcurrentExecutions")
        self.state = {"fleets": fleets, "function": function, "concurrency": concurrency, "reviewers": reviewers, "schedules": schedules}
        self._checked_viewer_configuration(expected_revision=config["RevisionId"])
        held = concurrency == 0 and all(
            values["service"]["desiredCount"] == 0 and values["scaling"]["MinCapacity"] == 0
            and all(values["scaling"].get("SuspendedState", {}).get(k) is True for k in SUSPENDED)
            for values in fleets.values())
        selected = self.inputs.recovery_plan
        selection = "explicit" if selected else "default"
        if not selected and held:
            require(all(values["service"].get("runningCount") == 0 and values["service"].get("pendingCount") == 0
                        for values in fleets.values()),
                    "Automatic recovery requires every fleet to have zero running and pending workers")
            try:
                selected = load_active_plan_id()
            except RecoveryPlanError as error:
                raise DeploymentError(str(error)) from error
        self.report.pop("recovery", None)
        if selected:
            require(selected != self.inputs.deployment_id, "Recovery requires a fresh deployment attempt")
            try:
                plan = load_plan(selected)
                self.state["restoration_targets"] = validate_plan(plan, self.state)
            except RecoveryPlanError as error:
                raise DeploymentError(str(error)) from error
            require(not self._active_tasks(), "Recovery requires all previous one-off worker tasks to be stopped")
            self.report["recovery"] = {"plan_id": plan["plan_id"], "selection": selection, "source": plan["source"],
                                       "root_source": plan["root_source"], "runtime_source_read": False}
        else:
            require(not held, "Maintenance hold requires an owner-reviewed recovery plan; do not capture zero capacity as the baseline")
        # Validate the availability floor before backups or runtime mutations.
        # Historical recovery targets remain immutable evidence, but cannot
        # restore the unsafe zero-worker policy for L0/L1.
        for fleet in FLEETS:
            self._capacity_target(fleet)
        self.report.update(status="preflight_passed", image=self.inputs.image,
            ui={"key": self.inputs.ui_key, "version": self.inputs.ui_version, "sha256": self.inputs.ui_sha256},
            viewer_uri=f"s3://{BUCKET}/{self.inputs.viewer_key}", fleet_count=len(fleets),
            viewer_l0_send_policy="simulation_allowed", nightly_schedule_validation="pending")
        self.report["schedule_targets"] = {"configuration": "preflight_only", "expected": len(schedules),
            "already_current": sum(not item["needs_update"] for item in schedules), "attempted": [], "confirmed_updated": []}
        return self.report

    @staticmethod
    def _download_code(url):
        parsed = urlparse(url)
        require(parsed.scheme == "https" and parsed.hostname and parsed.hostname.endswith(".amazonaws.com")
                and parsed.port in {None, 443} and not parsed.username, "Unexpected AWS code artifact URL")
        # Do not follow redirects to a different host and never log this signed URL.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise DeploymentError("AWS code artifact redirects are not allowed")
        with urllib.request.build_opener(NoRedirect).open(url, timeout=60) as response:
            data = response.read(300 * 1024 * 1024 + 1)
        require(0 < len(data) <= 300 * 1024 * 1024, "Previous code artifact has an invalid size")
        return data

    def _put(self, name, data):
        return self._write("s3", "put_object", Bucket=BUCKET, Key=f"{self.prefix}/{name}", Body=data,
                           ServerSideEncryption="AES256", ExpectedBucketOwner=ACCOUNT, IfNoneMatch="*")

    def _backup(self):
        function = self.state["function"]
        data = self.code_fetch(function["Code"]["Location"])
        code_hash = base64.b64encode(hashlib.sha256(data).digest()).decode()
        require(code_hash == function["Configuration"]["CodeSha256"], "Previous viewer code bytes do not match live Lambda hash")
        archive = self._put("previous-viewer.zip", data)
        self.state["previous_code_version"] = archive.get("VersionId")
        require(archive.get("VersionId"), "Previous code backup must have an immutable S3 version")
        fleets = {}
        for fleet, values in self.state["fleets"].items():
            # The immutable task-definition ARN is the complete previous version.
            # Environment values are not copied: legacy task definitions may hold
            # credentials in plaintext despite current secret-reference policy.
            original = values["task"]
            task = {key: original[key] for key in ("taskDefinitionArn", "family", "taskRoleArn", "executionRoleArn", "networkMode", "cpu", "memory") if key in original}
            task["containers"] = [{"name": c["name"], "image": c["image"],
                "environment_keys": sorted(item["name"] for item in c.get("environment", [])),
                "secret_references": c.get("secrets", [])} for c in original["containerDefinitions"]]
            fleets[fleet] = {"task_definition": task, "desired_count": values["service"]["desiredCount"],
                             "network": values["service"]["networkConfiguration"], "autoscaling": values["scaling"]}
        config = function["Configuration"]
        manifest = {"sha": self.inputs.sha, "deployment_id": self.inputs.deployment_id, "fleets": fleets,
                    "viewer": {"code_sha256_base64": code_hash, "previous_code_key": f"{self.prefix}/previous-viewer.zip",
                               "previous_code_version": archive["VersionId"], "revision_id": config["RevisionId"],
                               "reserved_concurrency": self.state["concurrency"],
                               "environment_keys": sorted(config.get("Environment", {}).get("Variables", {}))},
                    "rollback_policy": "code_only_keep_traffic_paused_and_all_fleets_held",
                    "adapter_schedules": self.state["schedules"],
                    "schedule_rollback_policy": "retain_backward_compatible_event_id_and_timestamp_transformers",
                    "accepted_release_pointer": "never_modified"}
        if "restoration_targets" in self.state:
            # Preserve the observed paused state separately from the reviewed
            # original capacities. A second failed attempt must retain both.
            manifest["restoration_targets"] = copy.deepcopy(self.state["restoration_targets"])
        manifest_bytes = json.dumps(manifest, sort_keys=True, default=str).encode()
        backup = self._put("rollback.json", manifest_bytes)
        require(backup.get("VersionId") and backup["VersionId"] != "null", "Rollback manifest must have an immutable S3 version")
        provenance = {"deployment_id": self.inputs.deployment_id, "sha": self.inputs.sha,
                      "bucket": BUCKET, "key": f"{self.prefix}/rollback.json", "version_id": backup["VersionId"],
                      "sha256": hashlib.sha256(manifest_bytes).hexdigest(), "verification": "controller_emitted"}
        try:
            self.recovery_plan_output = emit_plan(self.inputs.deployment_id, self.state,
                rollback_provenance=provenance, restoration_targets=self.state.get("restoration_targets"))
        except RecoveryPlanError as error:
            raise DeploymentError(str(error)) from error
        self.report["rollback_manifest"] = f"s3://{BUCKET}/{self.prefix}/rollback.json"

    def _scaling(self, fleet, suspended, *, minimum=None, maximum=None):
        old = self.state["fleets"][fleet]["scaling"]
        minimum = old["MinCapacity"] if minimum is None else minimum
        maximum = old["MaxCapacity"] if maximum is None else maximum
        require(type(minimum) is int and type(maximum) is int and 0 <= minimum <= maximum,
                "Fleet capacity must remain within its existing maximum")
        self._write("application-autoscaling", "register_scalable_target", ServiceNamespace="ecs",
                    ResourceId=old["ResourceId"], ScalableDimension="ecs:service:DesiredCount",
                    MinCapacity=minimum, MaxCapacity=maximum, SuspendedState=suspended)

    def _active_tasks(self):
        candidates = set()
        for fleet in FLEETS:
            for desired in ("RUNNING", "STOPPED"):
                token = None
                while True:
                    args = {"cluster": CLUSTER, "family": f"clhear-fleet-{fleet}", "desiredStatus": desired}
                    if token:
                        args["nextToken"] = token
                    page = self.clients["ecs"].list_tasks(**args)
                    candidates.update(page.get("taskArns", []))
                    token = page.get("nextToken")
                    if not token:
                        break
        active = {}
        candidates = sorted(candidates)
        for start in range(0, len(candidates), 100):
            response = self.clients["ecs"].describe_tasks(cluster=CLUSTER, tasks=candidates[start:start + 100])
            require(not response.get("failures"), "Cannot establish whether old worker tasks have stopped")
            active.update({task["taskArn"]: task.get("lastStatus") for task in response["tasks"] if task.get("lastStatus") != "STOPPED"})
        return active

    def _checked_viewer_configuration(self, *, expected_revision=None):
        current = self.clients["lambda"].get_function_configuration(FunctionName=FUNCTION)
        old = self.state["function"]["Configuration"]
        require(current.get("State", "Active") == "Active"
                and current.get("LastUpdateStatus", "Successful") == "Successful",
                "The viewer has an unfinished Lambda update")
        # Both Lambda reads return FunctionConfiguration. Only the revision
        # may change during our concurrency operation; even unknown future
        # configuration fields must continue to match the reviewed baseline.
        ignored = {"RevisionId", "ResponseMetadata"}
        require({k: v for k, v in current.items() if k not in ignored}
                == {k: v for k, v in old.items() if k not in ignored},
                "Viewer code or configuration changed after preflight")
        require(isinstance(current.get("RevisionId"), str) and current["RevisionId"],
                "The viewer revision is unavailable")
        if expected_revision is not None:
            require(current["RevisionId"] == expected_revision,
                    "Viewer revision changed outside the deployment traffic pause")
        return current

    def _hold(self, *, capture_viewer_revision=False):
        if capture_viewer_revision:
            self._checked_viewer_configuration(expected_revision=self.state["function"]["Configuration"]["RevisionId"])
        self._write("lambda", "put_function_concurrency", FunctionName=FUNCTION, ReservedConcurrentExecutions=0)
        require(self.clients["lambda"].get_function_concurrency(FunctionName=FUNCTION).get("ReservedConcurrentExecutions") == 0,
                "Viewer traffic pause could not be confirmed")
        if capture_viewer_revision:
            # PutFunctionConcurrency can issue a new revision without changing
            # code/configuration. Refresh only across this verified operation,
            # preserving the original preflight state for rollback evidence.
            current = self._checked_viewer_configuration()
            self.state["viewer_cutover_revision"] = current["RevisionId"]
        for fleet in FLEETS:
            self._scaling(fleet, SUSPENDED, minimum=0)
        known_tasks = set(self._active_tasks())
        for fleet in FLEETS:
            self._write("ecs", "update_service", cluster=CLUSTER, service=f"clhear-fleet-{fleet}", desiredCount=0)
        for _ in range(120):
            active = self._active_tasks()
            known_tasks.update(active)
            for arn, status in active.items():
                if status not in {"DEACTIVATING", "STOPPING", "DEPROVISIONING"}:
                    self._write("ecs", "stop_task", cluster=CLUSTER, task=arn, reason="CLHEAR L1 deployment maintenance hold")
            arns = sorted(known_tasks)
            for start in range(0, len(arns), 100):
                self.clients["ecs"].get_waiter("tasks_stopped").wait(cluster=CLUSTER, tasks=arns[start:start + 100],
                    WaiterConfig={"Delay": 5, "MaxAttempts": 120})
            services = self.clients["ecs"].describe_services(cluster=CLUSTER, services=[f"clhear-fleet-{f}" for f in FLEETS])
            if (not services.get("failures") and len(services.get("services", [])) == 9
                    and all(s.get("runningCount") == 0 and s.get("pendingCount") == 0 for s in services["services"])
                    and not self._active_tasks()):
                return
            self.sleep(5)
        raise DeploymentError("Old fleet tasks did not stop; traffic remains paused")

    def _configure_schedules(self):
        report = self.report["schedule_targets"]
        report["configuration"] = "partial_or_failed"
        for schedule in self.state["schedules"]:
            name = schedule["name"]
            rule = self.clients["events"].describe_rule(Name=name, EventBusName="default")
            require(rule.get("State") == schedule["state"] and rule.get("ScheduleExpression") == schedule["schedule_expression"],
                    "An adapter schedule changed after preflight")
            require(self._schedule_target(name) == schedule["previous_target"], "An adapter target changed after preflight")
            if schedule["needs_update"]:
                report["attempted"].append(name)
                result = self._write("events", "put_targets", Rule=name, EventBusName="default", Targets=[schedule["replacement"]])
                require(result.get("FailedEntryCount") == 0 and not result.get("FailedEntries"),
                        "EventBridge did not confirm every adapter target update")
                require(self._schedule_target(name) == schedule["replacement"], "Adapter target update readback did not match")
                report["confirmed_updated"].append(name)
        report["configuration"] = "configured"

    def _register(self):
        for fleet, old in self.state["fleets"].items():
            task = {k: copy.deepcopy(v) for k, v in old["task"].items() if k in TASK_FIELDS}
            worker = next(c for c in task["containerDefinitions"] if c["name"] == "worker")
            worker["image"] = self.inputs.image
            # ECS one-off overrides replace command, so the module invocation
            # must be entirely in entryPoint even for the legacy Python split.
            worker["entryPoint"] = list(WORKER_ENTRYPOINT)
            worker["command"] = []  # services poll normally; one-offs use explicit commands
            env = {v["name"]: v["value"] for v in worker.get("environment", [])}
            env.update(CLHEAR_L1_ONLY="true", CLHEAR_SNAPSHOT_S3_URI="", CLHEAR_HTTP_MODE="live", CLHEAR_ARTIFACT_STORE="s3",
                       CLHEAR_CODE_REVISION=self.inputs.sha, CLHEAR_WORKER_IMAGE_DIGEST=self.inputs.image.split("@", 1)[1],
                       CLHEAR_L1_CYCLE_CONTRACT="1",
                       CLHEAR_FLEET_QUEUE_URLS=json.dumps(QUEUES),
                       CLHEAR_VIEWER_SNAPSHOT_S3_URI=f"s3://{BUCKET}/{self.inputs.viewer_key}", CLHEAR_RELEASES_S3_PREFIX=RELEASES)
            worker["environment"] = [{"name": k, "value": v} for k, v in env.items()]
            task["tags"] = [v for v in task.get("tags", []) if v["key"] != "clhear:git-sha"] + [{"key": "clhear:git-sha", "value": self.inputs.sha}]
            arn = self._write("ecs", "register_task_definition", **task)["taskDefinition"]["taskDefinitionArn"]
            old["new_task_definition"] = arn
            self._write("ecs", "update_service", cluster=CLUSTER, service=f"clhear-fleet-{fleet}", taskDefinition=arn, desiredCount=0)

    def _worker(self, fleet, action, command=None):
        values = self.state["fleets"][fleet]
        service = values["service"]
        args = {"cluster": CLUSTER, "taskDefinition": values["new_task_definition"], "count": 1,
                "networkConfiguration": service["networkConfiguration"],
                "clientToken": hashlib.sha256(f"{self.inputs.deployment_id}:{action}".encode()).hexdigest(),
                "startedBy": f"clhear-l1-{self.inputs.sha[:12]}",
                "overrides": {"containerOverrides": [{"name": "worker", "command": command or
                    ["--verify-deployment", action, "--verification-id", self.inputs.deployment_id]}]}}
        # Deployment checks must not be interrupted by Spot reclamation. Normal
        # service capacity-provider settings remain untouched.
        args["launchType"] = "FARGATE"
        if service.get("platformVersion"):
            args["platformVersion"] = service["platformVersion"]
        started = time.monotonic()
        launched = self._write("ecs", "run_task", **args)
        require(not launched.get("failures") and len(launched.get("tasks", [])) == 1, f"{action} worker could not start")
        arn = launched["tasks"][0]["taskArn"]
        self.clients["ecs"].get_waiter("tasks_stopped").wait(cluster=CLUSTER, tasks=[arn],
            WaiterConfig={"Delay": 15, "MaxAttempts": 360 if action == "verify" else 120})
        response = self.clients["ecs"].describe_tasks(cluster=CLUSTER, tasks=[arn])
        require(not response.get("failures") and len(response.get("tasks", [])) == 1, f"{action} worker result is unavailable")
        task = response["tasks"][0]
        workers = [c for c in task.get("containers", []) if c.get("name") == "worker"]
        code = workers[0].get("exitCode") if len(workers) == 1 else None
        step = {"fleet": fleet.upper(), "action": action, "task_arn": arn,
                "exit_code": code, "duration_ms": round((time.monotonic() - started) * 1000),
                **self._worker_evidence(action)}
        self.report["steps"].append(step)
        require(task.get("lastStatus") == "STOPPED" and code in ({0, 2} if action == "verify" else {0}), f"{action} worker failed; deployment remains held")
        return code

    FULL_CYCLE_PUBLISHERS, FULL_CYCLE_LANES = 45, 32
    TRANSPORT_HEALTH_ATTEMPTS = 40  # × 15 s

    def _transport_healthy(self):
        """L0 (relay + coordination) and L1 (imports) are running on the deployed
        definition and their queues are being consumed: the precondition for asking
        the fleet to do real work."""
        response = self.clients["ecs"].describe_services(cluster=CLUSTER, services=["clhear-fleet-l0", "clhear-fleet-l1"])
        services = {svc["serviceName"]: svc for svc in response.get("services", [])}
        for fleet in ("l0", "l1"):
            svc = services.get(f"clhear-fleet-{fleet}")
            if not svc or svc.get("runningCount", 0) < 1 or svc.get("taskDefinition") != self.state["fleets"][fleet]["new_task_definition"]:
                return False
        return True

    def _request_full_cycle(self):
        """After a verified deployment: ask deployed L0 for the full diagnostic
        cycle over the whole publisher scope (45 publishers, 32 lanes) followed by an
        unchanged-source repeat. Results are published for sampling; unresolved gaps
        stay visible and block corpus acceptance. This never changes the deployment
        outcome: a request that cannot be made is recorded, not rolled back."""
        verification_id = "l1-cycle-" + self.inputs.deployment_id.removeprefix("l1-")
        receipt = {"verification_id": verification_id, "cycle_id": "cycle-manual-" + verification_id,
                   "repeat_cycle_id": "cycle-manual-" + verification_id + "-repeat",
                   "scope": "all_publishers", "publishers": self.FULL_CYCLE_PUBLISHERS, "lanes": self.FULL_CYCLE_LANES,
                   "unchanged_repeat": True, "corpus_acceptance": "pending", "status": "not_requested"}
        try:
            for _ in range(self.TRANSPORT_HEALTH_ATTEMPTS):
                if self._transport_healthy():
                    receipt["transport_health"] = "established"
                    break
                self.sleep(15)
            else:
                receipt.update(reason="L0/L1 workers did not reach running state on the deployed definition")
                return receipt
            code = self._worker("l0", "full_cycle_request",
                                ["--request-l1-cycle", "--unchanged-repeat", "--verification-id", verification_id])
            receipt.update(status="cycle_requested", exit_code=code)
        except Exception as error:  # noqa: BLE001 — recorded, never a deployment failure
            receipt.update(status="not_requested", **{k: v for k, v in _failure_details(error).items() if k != "status"})
        return receipt

    # Fields of a worker phase result that may enter a deployment artifact: statuses,
    # counts, codes and identities. Never messages, text or SQL.
    _EVIDENCE_FIELDS = ("verification_id", "phase", "job_id", "worker", "status", "exit_code", "error_type",
                        "deployment_checks", "scope", "scope_label", "evidence_mode", "finra_acceptance", "l1_acceptance",
                        "corpus_acceptance", "failure_summary", "evidence", "started_at", "finished_at", "duration_ms")
    _STEP_FIELDS = ("status", "passed", "scope_complete", "successful_sources", "failed_sources", "missing_sources",
                    "statuses", "verified_sources", "reason", "job_id", "audit_id", "inventory_hash", "verified", "unresolved",
                    "revision", "sha256", "byte_count", "snapshot_uri", "bindings_unchanged", "successful_source_count",
                    "version_count_before", "version_count_after")

    def _worker_evidence(self, action):
        """Read the phase result the worker published beside the candidate viewer.
        Missing or unreadable evidence is reported as such; it never fails the step
        by itself, and the private link is included either way."""
        prefix = self.inputs.viewer_key.rsplit("/", 1)[0]
        key = f"{prefix}/deployments/{self.inputs.deployment_id}/{action}.json"
        evidence = {"worker_result_uri": f"s3://{BUCKET}/{key}"}
        try:
            body = self.clients["s3"].get_object(Bucket=BUCKET, Key=key, ExpectedBucketOwner=ACCOUNT)["Body"].read()
            result = json.loads(body)
        except Exception as error:  # noqa: BLE001 — AccessDenied / NoSuchKey / malformed all mean "not readable here"
            evidence["worker_result"] = {"available": False, "reason": type(error).__name__}
            return evidence
        summary = {k: result.get(k) for k in self._EVIDENCE_FIELDS if k in result}
        summary["steps"] = {name: {k: v for k, v in step.items() if k in self._STEP_FIELDS}
                            for name, step in (result.get("steps") or {}).items() if isinstance(step, dict)}
        summary["failure_summary"] = [
            {k: row.get(k) for k in ("source", "worker", "task", "status", "attempt", "stage", "duration_ms",
                                     "error_type", "error_code", "sqlstate", "first_cause", "follow_on", "aborted_transaction")}
            for row in (result.get("failure_summary") or [])[:25]]
        summary["available"] = True
        evidence["worker_result"] = summary
        return evidence

    def _complete_lambda_update(self, before, response, *, phase, expected_code_hash, function_name=FUNCTION):
        started = time.monotonic()
        evidence = {"phase": phase, "response_revision": response.get("RevisionId"),
                    "response_update_status": response.get("LastUpdateStatus"), "result": "waiting"}
        self.report.setdefault("lambda_updates", []).append(evidence)
        try:
            # The response can be InProgress. Its revision is not a completion
            # token: Lambda may assign a different revision as it finishes.
            # https://docs.aws.amazon.com/lambda/latest/dg/functions-states.html
            self.clients["lambda"].get_waiter("function_updated_v2").wait(FunctionName=function_name)
            current = self.clients["lambda"].get_function_configuration(FunctionName=function_name)
            ignored = LAMBDA_UPDATE_OUTPUTS | {"CodeSha256"}
            if phase in {"code", "rollback"}:
                ignored |= {"CodeSize", "SigningJobArn", "SigningProfileVersionArn"}
            # Compare every other field, including unknown future fields. Only
            # the explicit code hash and intended Environment delta are allowed.
            configuration_matches = ({k: v for k, v in current.items() if k not in ignored}
                                     == {k: v for k, v in before.items() if k not in ignored})
            status_ok = current.get("State") == "Active" and current.get("LastUpdateStatus") == "Successful"
            evidence.update(completed_revision=current.get("RevisionId"),
                            revision_transition=current.get("RevisionId") != response.get("RevisionId"),
                            state=current.get("State"), completed_update_status=current.get("LastUpdateStatus"),
                            status_ok=status_ok, code_matches=current.get("CodeSha256") == expected_code_hash,
                            configuration_matches=configuration_matches)
            for valid, outcome, reason in (
                (status_ok, "mismatch_status", "Lambda update completion status is not successful"),
                (evidence["code_matches"], "mismatch_code", "Lambda update completed with unexpected code"),
                (configuration_matches, "mismatch_configuration", "Lambda update completed with unexpected configuration"),
                (isinstance(current.get("RevisionId"), str) and bool(current["RevisionId"]),
                 "missing_revision", "Lambda update completed without a revision"),
            ):
                if not valid:
                    evidence["result"] = outcome
                    raise DeploymentError(reason)
            evidence["result"] = "verified"
            return current
        except Exception:
            if evidence["result"] == "waiting":
                evidence["result"] = "completion_read_or_wait_failed"
            raise
        finally:
            # Never put environment values or raw SDK errors in diagnostics.
            evidence["duration_ms"] = round((time.monotonic() - started) * 1000)

    def _viewer(self):
        # Compare revisions so a concurrent operator change cannot be overwritten.
        old = self.state["function"]["Configuration"]
        require(self.state.get("viewer_cutover_revision"), "Viewer revision was not verified after the traffic pause")
        current = self._checked_viewer_configuration(expected_revision=self.state["viewer_cutover_revision"])
        updated = self._write("lambda", "update_function_code", FunctionName=FUNCTION, S3Bucket=BUCKET,
            S3Key=self.inputs.ui_key, S3ObjectVersion=self.inputs.ui_version, RevisionId=current["RevisionId"], Publish=False)
        self.state["viewer_code_changed"] = True
        code_hash = base64.b64encode(bytes.fromhex(self.inputs.ui_sha256)).decode()
        config = self._complete_lambda_update(current, updated, phase="code", expected_code_hash=code_hash)
        env = dict(old.get("Environment", {}).get("Variables", {}))
        env.update(CLHEAR_RESTRICTED_ACCESS="true", CLHEAR_AUTH_DEBUG="false", CLHEAR_REVIEWER_EMAILS=self.state["reviewers"],
                   CLHEAR_DB_S3_URI=f"s3://{BUCKET}/{self.inputs.viewer_key}", CLHEAR_RELEASES_S3_PREFIX=RELEASES,
                   CLHEAR_EVENTS_QUEUE_URL=QUEUES["l0"])
        updated_config = self._write("lambda", "update_function_configuration", FunctionName=FUNCTION,
            RevisionId=config["RevisionId"], Environment={"Variables": env})
        expected_config = copy.deepcopy(config)
        expected_config["Environment"] = {"Variables": env}
        current = self._complete_lambda_update(expected_config, updated_config, phase="configuration", expected_code_hash=code_hash)
        # Capture only after our conditional update has completed. Its service
        # fields (such as LastModified) may legitimately differ from preflight;
        # the completed configuration must stay unchanged until traffic resumes.
        self.state["verified_viewer_configuration"] = copy.deepcopy({k: v for k, v in current.items() if k != "ResponseMetadata"})

    def _resume_viewer(self):
        expected = self.state.get("verified_viewer_configuration")
        require(expected, "Completed viewer cutover has not been verified")
        current = self.clients["lambda"].get_function_configuration(FunctionName=FUNCTION)
        require({k: v for k, v in current.items() if k != "ResponseMetadata"} == expected,
                "Viewer code or configuration changed before traffic resume")
        old = self.state.get("restoration_targets", {}).get("viewer_reserved_concurrency", self.state["concurrency"])
        if old is None:
            # Restore the original unreserved policy directly. Even reserving
            # one temporary slot can exceed the account's reservation quota.
            self._write("lambda", "delete_function_concurrency", FunctionName=FUNCTION)
            self._confirm_viewer_concurrency(None)
        else:
            # A previously paused viewer gets one probe slot, then returns to
            # zero. Reservation failures must never fall back to unreserved.
            self._write("lambda", "put_function_concurrency", FunctionName=FUNCTION, ReservedConcurrentExecutions=max(old, 1))
            self._confirm_viewer_concurrency(max(old, 1))
        for path, expected in (("/api/clhear/health", 200), ("/api/clhear/sources", 401)):
            event = {"version": "2.0", "routeKey": "$default", "rawPath": path, "rawQueryString": "",
                     "headers": {"accept": "application/json", "host": "clhear.org"},
                     "requestContext": {"stage": "$default", "http": {"method": "GET", "path": path,
                        "sourceIp": "127.0.0.1", "protocol": "HTTP/1.1", "userAgent": "clhear-deployment-verifier"}}, "isBase64Encoded": False}
            response = self._write("lambda", "invoke", FunctionName=FUNCTION, InvocationType="RequestResponse", Payload=json.dumps(event).encode())
            result = json.loads(response["Payload"].read())
            require(not response.get("FunctionError") and result.get("statusCode") == expected, "Viewer anonymous access verification failed")
        if old == 0:
            self._write("lambda", "put_function_concurrency", FunctionName=FUNCTION, ReservedConcurrentExecutions=0)
            self._confirm_viewer_concurrency(0)

    def _confirm_viewer_concurrency(self, expected):
        current = self.clients["lambda"].get_function_concurrency(FunctionName=FUNCTION)
        confirmed = ("ReservedConcurrentExecutions" not in current if expected is None else
                     type(current.get("ReservedConcurrentExecutions")) is int and current["ReservedConcurrentExecutions"] == expected)
        require(confirmed, "Viewer concurrency restoration readback did not match")

    def _capacity_target(self, fleet):
        old = self.state["fleets"][fleet]
        target = self.state.get("restoration_targets", {}).get("fleets", {}).get(fleet)
        desired = target["desired_count"] if target else old["service"]["desiredCount"]
        minimum = target["min_capacity"] if target else old["scaling"]["MinCapacity"]
        maximum = target["max_capacity"] if target else old["scaling"]["MaxCapacity"]
        suspended = target["suspended_state"] if target else old["scaling"].get("SuspendedState", {k: False for k in SUSPENDED})
        require(all(type(value) is int and value >= 0 for value in (desired, minimum, maximum))
                and maximum == old["scaling"]["MaxCapacity"], "Invalid fleet restoration capacity")
        if fleet in ALWAYS_ON_FLEETS:
            minimum = max(1, minimum)
        desired = max(desired, minimum)
        require(desired <= maximum and minimum <= maximum,
                "Existing fleet maximum cannot support required worker availability")
        return {"desired": desired, "minimum": minimum, "maximum": maximum, "suspended": suspended}

    def _confirm_capacity(self, fleet, target, suspended):
        old = self.state["fleets"][fleet]["scaling"]
        rows = self.clients["application-autoscaling"].describe_scalable_targets(ServiceNamespace="ecs",
            ResourceIds=[old["ResourceId"]], ScalableDimension="ecs:service:DesiredCount")["ScalableTargets"]
        require(len(rows) == 1 and rows[0].get("ResourceId") == old["ResourceId"]
                and rows[0].get("ScalableTargetARN") == old["ScalableTargetARN"]
                and rows[0].get("MinCapacity") == target["minimum"]
                and rows[0].get("MaxCapacity") == target["maximum"]
                and rows[0].get("SuspendedState") == suspended,
                "Fleet capacity restoration readback did not match")

    def _restore_capacity(self):
        targets = {fleet: self._capacity_target(fleet) for fleet in FLEETS}
        for fleet, old in self.state["fleets"].items():
            # RegisterScalableTarget may raise desired count to its new minimum
            # even while scaling is suspended. Bind the verified code first.
            self._write("ecs", "update_service", cluster=CLUSTER, service=f"clhear-fleet-{fleet}",
                        taskDefinition=old["new_task_definition"], desiredCount=0)
            target = targets[fleet]
            self._scaling(fleet, SUSPENDED, minimum=target["minimum"], maximum=target["maximum"])
            self._confirm_capacity(fleet, target, SUSPENDED)
        for fleet, target in targets.items():
            self._write("ecs", "update_service", cluster=CLUSTER, service=f"clhear-fleet-{fleet}",
                        taskDefinition=self.state["fleets"][fleet]["new_task_definition"], desiredCount=target["desired"])
            self._scaling(fleet, target["suspended"], minimum=target["minimum"], maximum=target["maximum"])
            self._confirm_capacity(fleet, target, target["suspended"])
        self.clients["ecs"].get_waiter("services_stable").wait(cluster=CLUSTER,
            services=[f"clhear-fleet-{f}" for f in FLEETS], WaiterConfig={"Delay": 15, "MaxAttempts": 40})
        response = self.clients["ecs"].describe_services(cluster=CLUSTER, services=[f"clhear-fleet-{f}" for f in FLEETS])
        services = {s["serviceName"]: s for s in response.get("services", [])}
        require(not response.get("failures") and len(response.get("services", [])) == len(FLEETS)
                and set(services) == {f"clhear-fleet-{f}" for f in FLEETS}, "Fleet restoration health evidence is incomplete")
        evidence = {}
        for fleet, target in targets.items():
            self._confirm_capacity(fleet, target, target["suspended"])
            service = services[f"clhear-fleet-{fleet}"]
            definition = self.state["fleets"][fleet]["new_task_definition"]
            deployments = service.get("deployments", [])
            require(service.get("status") == "ACTIVE" and service.get("taskDefinition") == definition
                    and type(service.get("desiredCount")) is int
                    and target["minimum"] <= service["desiredCount"] <= target["maximum"]
                    and service.get("runningCount") == service["desiredCount"] and service.get("pendingCount") == 0
                    and len(deployments) == 1 and deployments[0].get("status") == "PRIMARY"
                    and deployments[0].get("taskDefinition") == definition and deployments[0].get("rolloutState") == "COMPLETED",
                    "Restored fleet is not healthy on the verified worker definition")
            evidence[fleet] = {"minimum": target["minimum"], "maximum": target["maximum"],
                               "desired": service["desiredCount"], "running": service["runningCount"],
                               "task_definition": definition, "capacity_readback": "verified", "service_health": "stable"}
        self.report["fleet_restoration"] = evidence

    def _rollback(self):
        errors = []
        try:
            self._hold()
        except Exception:
            errors.append("maintenance_hold_incomplete")
        for fleet, old in self.state["fleets"].items():
            try:
                # Try every hold independently even if an earlier AWS operation
                # failed. Old code is restored only after its scaler is held.
                self._scaling(fleet, SUSPENDED, minimum=0)
                self._write("ecs", "update_service", cluster=CLUSTER, service=f"clhear-fleet-{fleet}", taskDefinition=old["service"]["taskDefinition"], desiredCount=0)
            except Exception:
                errors.append(f"{fleet}_code_rollback_failed")
        try:
            pause_verified = self.clients["lambda"].get_function_concurrency(FunctionName=FUNCTION).get("ReservedConcurrentExecutions") == 0
        except Exception:
            pause_verified = False
        if self.state.get("viewer_code_changed") and pause_verified:
            try:
                config = self.clients["lambda"].get_function_configuration(FunctionName=FUNCTION)
                if config.get("CodeSha256") != base64.b64encode(bytes.fromhex(self.inputs.ui_sha256)).decode():
                    errors.append("viewer_code_rollback_skipped_code_drift")
                else:
                    # Only undo this deployment's code. The conditional write
                    # also prevents a new change after this ownership check.
                    updated = self._write("lambda", "update_function_code", FunctionName=FUNCTION, S3Bucket=BUCKET,
                        S3Key=f"{self.prefix}/previous-viewer.zip", S3ObjectVersion=self.state["previous_code_version"],
                        RevisionId=config["RevisionId"], Publish=False)
                    self._complete_lambda_update(config, updated, phase="rollback",
                        expected_code_hash=self.state["function"]["Configuration"]["CodeSha256"])
            except Exception:
                errors.append("viewer_code_rollback_failed")
        elif self.state.get("viewer_code_changed"):
            errors.append("viewer_code_rollback_skipped_pause_unconfirmed")
        self.report.update(status="failed_maintenance", recovery_required=True, recovery_errors=errors,
                           traffic_policy="paused" if pause_verified else "pause_unconfirmed",
                           fleet_policy="hold_requires_operator_verification" if errors else "zero_capacity_autoscaling_suspended")

    def deploy(self):
        self._guard()
        self.preflight()
        self._backup()  # no live resource changes if the rollback backup fails
        try:
            self._hold(capture_viewer_revision=True)
            self._configure_schedules()
            self._register()
            self._database_recovery_evidence()  # recheck immediately before L0 can migrate
            self._worker("l0", "bootstrap")
            self._viewer()
            result = self._worker("l1", "verify")
            self._worker("l0", "publish")
            self.clients["s3"].head_object(Bucket=BUCKET, Key=self.inputs.viewer_key, ExpectedBucketOwner=ACCOUNT)
            self._resume_viewer()
            self._restore_capacity()
            self.report.update(status="review_ready" if result == 2 else "verified", recovery_required=False,
                               l0_relay_minimum=1, l1_worker_minimum=1,
                               traffic_policy="previous_capacity_restored", fleet_policy="new_code_l1_only")
            self.report["full_cycle_request"] = self._request_full_cycle()
            self._put("result.json", json.dumps(self.report, sort_keys=True).encode())
        except Exception as error:
            self.report.update(_failure_details(error))
            self._rollback()
            try:
                self._put("failure.json", json.dumps(self.report, sort_keys=True).encode())
            except Exception:
                self.report["recovery_errors"].append("private_failure_report_write_failed")
        return self.report


class VerificationDispatcher:
    """Submit to the deployed L0 worker; never deploy code or import here."""

    _guard = Deployer._guard
    _write = Deployer._write
    _schedule_preflight = Deployer._schedule_preflight
    _schedule_target = Deployer._schedule_target
    _valid_transformer = Deployer._valid_transformer
    _schedule_payload = staticmethod(Deployer._schedule_payload)
    _transformer = Deployer._transformer

    OPERATIONS = {
        # operation: (id pattern, worker arguments, receipt status, waiter attempts)
        "verify": (r"l1-cycle-[1-9][0-9]*-[1-9][0-9]*", ("--request-l1-cycle",), "cycle_submitted", 60),
        "recover-queues": (r"l1-queues-[1-9][0-9]*-[1-9][0-9]*", ("--recover-queues",), "recovery_pass_completed", 240),
        "poc-private-review": (r"l1-poc-[1-9][0-9]*-[1-9][0-9]*", ("--poc-private-review",), "poc_review_recorded", 60),
        "approve-inventory": (r"l1-inventory-[1-9][0-9]*-[1-9][0-9]*", ("--approve-inventory",), "inventory_review_recorded", 60),
    }

    def __init__(self, sha, verification_id, *, clients=None, environ=None, operation="verify",
                 max_messages=None, poc_action=None, evidence_ref=None, inventory_hash=None):
        from types import SimpleNamespace
        require(bool(re.fullmatch(r"[0-9a-f]{40}", sha)), "A tested controller SHA is required")
        require(operation in self.OPERATIONS, "Unknown L0 operation")
        pattern, self.worker_arguments, self.receipt_status, self.waiter_attempts = self.OPERATIONS[operation]
        require(bool(re.fullmatch(pattern, verification_id)), "Invalid cycle verification ID")
        self.operation = operation
        if max_messages is not None:
            require(type(max_messages) is int and 1 <= max_messages <= 200_000, "max_messages must be within 1..200000")
            self.worker_arguments = (*self.worker_arguments, "--max-messages", str(max_messages))
        if operation == "poc-private-review":
            require(poc_action in {"activate", "revoke"}, "POC action must be activate or revoke")
            require(isinstance(evidence_ref, str) and bool(evidence_ref.strip()), "evidence_ref is required")
            self.worker_arguments = ("--poc-private-review", poc_action, "--evidence-ref", evidence_ref.strip())
        if operation == "approve-inventory":
            require(bool(re.fullmatch(r"[a-f0-9]{64}", inventory_hash or "")), "inventory_hash must be a SHA-256")
            extra = ("--approve-inventory", inventory_hash)
            if evidence_ref:
                extra = extra + ("--evidence-ref", str(evidence_ref).strip())
            self.worker_arguments = extra
        self.inputs = SimpleNamespace(sha=sha)
        self.verification_id = verification_id
        self.env = dict(os.environ if environ is None else environ)
        session = None if clients is not None else boto3.Session(region_name=REGION)
        self.clients = clients or {name: session.client(name, region_name=REGION) for name in ("sts", "ecs", "events", "ecr")}
        self.authorized = False

    def dispatch(self):
        self._guard()
        schedules = self._schedule_preflight()
        require(all(not s["needs_update"] for s in schedules), "Deploy the occurrence-aware schedule targets before verification")
        response = self.clients["ecs"].describe_services(cluster=CLUSTER, services=[f"clhear-fleet-{fleet}" for fleet in FLEETS])
        require(not response.get("failures") and len(response.get("services", [])) == len(FLEETS), "All owning fleet services must be available")
        service_by_name = {service["serviceName"]: service for service in response["services"]}
        definitions = {}
        identities = set()
        for fleet in FLEETS:
            service = service_by_name[f"clhear-fleet-{fleet}"]
            require(service.get("status") == "ACTIVE", "Fleet is not active")
            if fleet in {"l0", "l1"}:
                require(service.get("desiredCount", 0) >= 1 and service.get("runningCount", 0) >= 1,
                        "L0 and L1 must be running before submitting a corpus cycle")
            definition = self.clients["ecs"].describe_task_definition(taskDefinition=service["taskDefinition"])["taskDefinition"]
            workers = [c for c in definition.get("containerDefinitions", []) if c["name"] == "worker"]
            require(len(workers) == 1 and _supported_worker_invocation(workers[0]), "Invalid worker entrypoint")
            worker = workers[0]
            env = {item["name"]: item["value"] for item in worker.get("environment", [])}
            require(env.get("CLHEAR_L1_ONLY") == "true", "Every fleet must retain the L1-only hold")
            require(env.get("CLHEAR_FLEET") == fleet.upper() and not env.get("DATABASE_URL"), "Fleet identity or database binding is invalid")
            if fleet in {"l0", "l1"}:
                image = worker.get("image", "")
                prefix = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/clhear-workers@"
                require(image.startswith(prefix) and re.fullmatch(r"sha256:[0-9a-f]{64}", image[len(prefix):]), "Worker image must be an immutable CLHEAR digest")
                revision = env.get("CLHEAR_CODE_REVISION", "")
                require(re.fullmatch(r"[0-9a-f]{40}", revision) and env.get("CLHEAR_WORKER_IMAGE_DIGEST") == image[len(prefix):], "Worker identity evidence is missing")
                require(env.get("CLHEAR_L1_CYCLE_CONTRACT") == "1" and env.get("CLHEAR_HTTP_MODE") == "live"
                        and env.get("CLHEAR_ARTIFACT_STORE") == "s3" and not env.get("CLHEAR_SNAPSHOT_S3_URI")
                        and env.get("CLHEAR_EVENTS_QUEUE_URL") == QUEUES[fleet], "Deploy the authoritative full-cycle worker contract first")
                require(any(s.get("name") == "DATABASE_URL" for s in worker.get("secrets", [])), "Worker must use its existing database secret")
                identities.add((revision, image[len(prefix):]))
                task_arns, token = [], None
                while True:
                    listing = self.clients["ecs"].list_tasks(cluster=CLUSTER, serviceName=service["serviceName"],
                        desiredStatus="RUNNING", **({"nextToken": token} if token else {}))
                    task_arns.extend(listing.get("taskArns", []))
                    token = listing.get("nextToken")
                    if not token:
                        break
                require(bool(task_arns), "No running worker task can be verified")
                for offset in range(0, len(task_arns), 100):
                    live = self.clients["ecs"].describe_tasks(cluster=CLUSTER, tasks=task_arns[offset:offset + 100])
                    require(not live.get("failures") and len(live.get("tasks", [])) == len(task_arns[offset:offset + 100]), "Running worker evidence is incomplete")
                    for task in live["tasks"]:
                        containers = [c for c in task.get("containers", []) if c.get("name") == "worker"]
                        require(task.get("lastStatus") == "RUNNING" and task.get("taskDefinitionArn") == service["taskDefinition"]
                                and len(containers) == 1 and containers[0].get("imageDigest") == image[len(prefix):],
                                "A worker rollout is incomplete; running tasks must match the deployed identity")
            definitions[fleet] = definition
        require(len(identities) == 1, "L0 and L1 must execute the same verified worker image")
        revision, digest = identities.pop()
        images = self.clients["ecr"].describe_images(registryId=ACCOUNT, repositoryName="clhear-workers", imageIds=[{"imageDigest": digest}])["imageDetails"]
        require(len(images) == 1 and images[0].get("imageDigest") == digest and any(
            re.fullmatch(revision + r"-[1-9][0-9]*-[1-9][0-9]*", tag) for tag in images[0].get("imageTags", [])), "Worker image lacks its deployment build binding")
        service = service_by_name["clhear-fleet-l0"]
        l0_worker = next(c for c in definitions["l0"]["containerDefinitions"] if c["name"] == "worker")
        command = list(WORKER_ENTRYPOINT)[len(l0_worker.get("entryPoint", [])):] + [*self.worker_arguments, "--verification-id", self.verification_id]
        args = {"cluster": CLUSTER, "taskDefinition": service["taskDefinition"], "count": 1, "launchType": "FARGATE",
                "networkConfiguration": service["networkConfiguration"],
                "startedBy": self.verification_id[:36],
                "clientToken": self.verification_id,
                "overrides": {"containerOverrides": [{"name": "worker", "command": command}]}}
        if service.get("platformVersion"):
            args["platformVersion"] = service["platformVersion"]
        launched = self._write("ecs", "run_task", **args)
        require(not launched.get("failures") and len(launched.get("tasks", [])) == 1, "The L0 cycle submission task did not start")
        task_arn = launched["tasks"][0]["taskArn"]
        self.clients["ecs"].get_waiter("tasks_stopped").wait(cluster=CLUSTER, tasks=[task_arn], WaiterConfig={"Delay": 10, "MaxAttempts": self.waiter_attempts})
        stopped = self.clients["ecs"].describe_tasks(cluster=CLUSTER, tasks=[task_arn])
        require(not stopped.get("failures") and len(stopped.get("tasks", [])) == 1, "The submission task result is unavailable")
        task = stopped["tasks"][0]
        exits = [c.get("exitCode") for c in task.get("containers", []) if c.get("name") == "worker"]
        require(task.get("lastStatus") == "STOPPED" and exits == [0], "The L0 worker did not confirm the operation")
        receipt = {"status": self.receipt_status, "operation": self.operation,
                   "controller_sha": self.inputs.sha, "code_revision": revision, "worker_image_digest": digest,
                   "task_arn": task_arn, "configured_schedules": len(schedules), "origin": "manual",
                   "corpus_acceptance": "pending", "nightly_schedule_validation": "pending",
                   "deployment_performed": False, "accepted_release_changed": False}
        if self.operation == "verify":
            receipt["cycle_id"] = "cycle-manual-" + self.verification_id
        elif self.operation == "recover-queues":
            receipt.update(recovery_id=self.verification_id, evidence="deferred-delivery ledger (l0_platform.deferred_deliveries)",
                           queues_purged=False, resumable=True)
        elif self.operation == "poc-private-review":
            receipt.update(review_id=self.verification_id, display_public=False, acceptance="not_claimed")
        else:
            receipt.update(review_id=self.verification_id, acceptance="not_claimed")
        return receipt


def verification_main(argv, operation="verify"):
    parser = argparse.ArgumentParser(description="Run one L0 operation through the deployed L0 worker")
    parser.add_argument("--sha", required=True)
    if operation == "verify":
        parser.add_argument("--verification-id", required=True)
    elif operation == "recover-queues":
        parser.add_argument("--recovery-id", required=True, dest="verification_id")
        parser.add_argument("--max-messages", type=int, default=None)
    elif operation == "poc-private-review":
        parser.add_argument("--verification-id", required=True)
        parser.add_argument("--action", required=True, choices=("activate", "revoke"))
        parser.add_argument("--evidence-ref", required=True)
    else:
        parser.add_argument("--verification-id", required=True)
        parser.add_argument("--inventory-hash", required=True)
        parser.add_argument("--evidence-ref", default="poc:private-scope-review")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = VerificationDispatcher(
            args.sha, args.verification_id, operation=operation,
            max_messages=getattr(args, "max_messages", None),
            poc_action=getattr(args, "action", None),
            evidence_ref=getattr(args, "evidence_ref", None),
            inventory_hash=getattr(args, "inventory_hash", None),
        ).dispatch()
    except Exception as error:
        report = {"status": "submission_failed", "operation": operation, **_failure_details(error)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.chmod(args.output, 0o600)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in {"cycle_submitted", "recovery_pass_completed",
                                     "poc_review_recorded", "inventory_review_recorded"} else 1


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "verify":
        return verification_main(argv[1:])
    if argv and argv[0] == "recover-queues":
        return verification_main(argv[1:], operation="recover-queues")
    if argv and argv[0] == "poc-private-review":
        return verification_main(argv[1:], operation="poc-private-review")
    if argv and argv[0] == "approve-inventory":
        return verification_main(argv[1:], operation="approve-inventory")
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("sha", "image", "ui-key", "ui-sha256", "ui-version", "deployment-id"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--reviewer-emails", default="")
    parser.add_argument("--viewer-key", default="webui/l1/candidate.db")
    parser.add_argument("--recovery-plan", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    inputs = Inputs(**{name: getattr(args, name) for name in Inputs.__dataclass_fields__})
    deployer = Deployer(inputs)
    try:
        report = deployer.deploy() if args.apply else deployer.preflight()
    except Exception as error:
        # SDK exceptions may contain request parameters. Only our fixed error
        # messages are safe for user-visible output.
        report = {**deployer.report, "status": "preflight_failed", **_failure_details(error)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.chmod(args.output, 0o600)
    if deployer.recovery_plan_output is not None:
        recovery_output = args.output.with_name("recovery-plan.json")
        recovery_output.write_text(json.dumps(deployer.recovery_plan_output, indent=2, sort_keys=True) + "\n")
        os.chmod(recovery_output, 0o600)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in {"preflight_passed", "verified", "review_ready"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
