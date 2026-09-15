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
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlparse
import urllib.request

import boto3

ACCOUNT = "730649732189"
REGION = "us-east-1"
CLUSTER = "clhear-cluster"
FUNCTION = "clhear-webui"
BUCKET = f"clhear-deploy-{ACCOUNT}"
FLEETS = tuple(f"l{i}" for i in range(9))
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


class DeploymentError(RuntimeError):
    """An operator-readable error that never contains secret/configuration values."""


def require(condition, message):
    if not condition:
        raise DeploymentError(message)


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
        require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.deployment_id)), "Invalid deployment identifier")


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
        self.report = {"deployment_id": inputs.deployment_id, "sha": inputs.sha,
                       "account": ACCOUNT, "region": REGION, "accepted_release_changed": False,
                       "steps": [], "status": "not_started"}
        self.prefix = f"deployments/l1/{inputs.deployment_id}"

    def _write(self, client, operation, **kwargs):
        require(self.authorized, "AWS mutations require the guarded apply path")
        return getattr(self.clients[client], operation)(**kwargs)

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
            if adapter == "finra":
                require(rule["State"] == "ENABLED" and rule["ScheduleExpression"] == "cron(0 0 * * ? *)",
                        "FINRA must already be enabled on its daily 00:00 UTC schedule")
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
            require(worker.get("entryPoint") == ["python", "-m", "app.clhear.workers"], "Unsupported worker entrypoint")
            environment = {v["name"]: v["value"] for v in worker.get("environment", [])}
            secrets = {v["name"]: v["valueFrom"] for v in worker.get("secrets", [])}
            require(environment.get("CLHEAR_FLEET") == fleet.upper(), "Fleet identity does not match its service")
            require(environment.get("CLHEAR_EVENTS_QUEUE_URL") == QUEUES[fleet], "A fleet is not bound to its owning queue")
            try:
                queue_map = json.loads(environment.get("CLHEAR_FLEET_QUEUE_URLS", "{}"))
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
        self._put("rollback.json", json.dumps(manifest, sort_keys=True, default=str).encode())
        self.report["rollback_manifest"] = f"s3://{BUCKET}/{self.prefix}/rollback.json"

    def _scaling(self, fleet, suspended, *, minimum=None):
        old = self.state["fleets"][fleet]["scaling"]
        self._write("application-autoscaling", "register_scalable_target", ServiceNamespace="ecs",
                    ResourceId=old["ResourceId"], ScalableDimension="ecs:service:DesiredCount",
                    MinCapacity=old["MinCapacity"] if minimum is None else minimum,
                    MaxCapacity=max(old["MaxCapacity"], minimum or 0), SuspendedState=suspended)

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

    def _hold(self):
        self._write("lambda", "put_function_concurrency", FunctionName=FUNCTION, ReservedConcurrentExecutions=0)
        require(self.clients["lambda"].get_function_concurrency(FunctionName=FUNCTION).get("ReservedConcurrentExecutions") == 0,
                "Viewer traffic pause could not be confirmed")
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
            worker["command"] = []  # services poll normally; one-offs use explicit commands
            env = {v["name"]: v["value"] for v in worker.get("environment", [])}
            env.update(CLHEAR_L1_ONLY="true", CLHEAR_SNAPSHOT_S3_URI="", CLHEAR_HTTP_MODE="live", CLHEAR_ARTIFACT_STORE="s3",
                       CLHEAR_VIEWER_SNAPSHOT_S3_URI=f"s3://{BUCKET}/{self.inputs.viewer_key}", CLHEAR_RELEASES_S3_PREFIX=RELEASES)
            worker["environment"] = [{"name": k, "value": v} for k, v in env.items()]
            task["tags"] = [v for v in task.get("tags", []) if v["key"] != "clhear:git-sha"] + [{"key": "clhear:git-sha", "value": self.inputs.sha}]
            arn = self._write("ecs", "register_task_definition", **task)["taskDefinition"]["taskDefinitionArn"]
            old["new_task_definition"] = arn
            self._write("ecs", "update_service", cluster=CLUSTER, service=f"clhear-fleet-{fleet}", taskDefinition=arn, desiredCount=0)

    def _worker(self, fleet, action):
        values = self.state["fleets"][fleet]
        service = values["service"]
        args = {"cluster": CLUSTER, "taskDefinition": values["new_task_definition"], "count": 1,
                "networkConfiguration": service["networkConfiguration"],
                "clientToken": hashlib.sha256(f"{self.inputs.deployment_id}:{action}".encode()).hexdigest(),
                "startedBy": f"clhear-l1-{self.inputs.sha[:12]}",
                "overrides": {"containerOverrides": [{"name": "worker", "command":
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
        self.report["steps"].append({"fleet": fleet.upper(), "action": action, "task_arn": arn,
                                    "exit_code": code, "duration_ms": round((time.monotonic() - started) * 1000)})
        require(task.get("lastStatus") == "STOPPED" and code in ({0, 2} if action == "verify" else {0}), f"{action} worker failed; deployment remains held")
        return code

    def _viewer(self):
        # Compare revisions so a concurrent operator change cannot be overwritten.
        old = self.state["function"]["Configuration"]
        updated = self._write("lambda", "update_function_code", FunctionName=FUNCTION, S3Bucket=BUCKET,
            S3Key=self.inputs.ui_key, S3ObjectVersion=self.inputs.ui_version, RevisionId=old["RevisionId"], Publish=False)
        self.state["viewer_code_changed"] = True
        self.clients["lambda"].get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        config = self.clients["lambda"].get_function_configuration(FunctionName=FUNCTION)
        require(config.get("RevisionId") == updated.get("RevisionId")
                and config.get("Environment", {}).get("Variables", {}) == old.get("Environment", {}).get("Variables", {}),
                "Viewer configuration changed during the code cutover")
        require(config.get("CodeSha256") == base64.b64encode(bytes.fromhex(self.inputs.ui_sha256)).decode(), "Deployed viewer code hash does not match")
        env = dict(old.get("Environment", {}).get("Variables", {}))
        env.update(CLHEAR_RESTRICTED_ACCESS="true", CLHEAR_AUTH_DEBUG="false", CLHEAR_REVIEWER_EMAILS=self.state["reviewers"],
                   CLHEAR_DB_S3_URI=f"s3://{BUCKET}/{self.inputs.viewer_key}", CLHEAR_RELEASES_S3_PREFIX=RELEASES,
                   CLHEAR_EVENTS_QUEUE_URL=QUEUES["l0"])
        self._write("lambda", "update_function_configuration", FunctionName=FUNCTION, RevisionId=config["RevisionId"], Environment={"Variables": env})
        self.clients["lambda"].get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        current = self.clients["lambda"].get_function_configuration(FunctionName=FUNCTION)
        require(current.get("Environment", {}).get("Variables") == env, "Restricted viewer configuration was not retained")

    def _resume_viewer(self):
        old = self.state["concurrency"]
        # A previously paused viewer stays paused: deployment does not grant a
        # new traffic policy. One temporary slot permits the anonymous probes.
        self._write("lambda", "put_function_concurrency", FunctionName=FUNCTION, ReservedConcurrentExecutions=max(old or 0, 1))
        for path, expected in (("/api/clhear/health", 200), ("/api/clhear/sources", 401)):
            event = {"version": "2.0", "routeKey": "$default", "rawPath": path, "rawQueryString": "",
                     "headers": {"accept": "application/json", "host": "clhear.org"},
                     "requestContext": {"stage": "$default", "http": {"method": "GET", "path": path,
                        "sourceIp": "127.0.0.1", "protocol": "HTTP/1.1", "userAgent": "clhear-deployment-verifier"}}, "isBase64Encoded": False}
            response = self._write("lambda", "invoke", FunctionName=FUNCTION, InvocationType="RequestResponse", Payload=json.dumps(event).encode())
            result = json.loads(response["Payload"].read())
            require(not response.get("FunctionError") and result.get("statusCode") == expected, "Viewer anonymous access verification failed")
        if old is None:
            self._write("lambda", "delete_function_concurrency", FunctionName=FUNCTION)
        elif old == 0:
            self._write("lambda", "put_function_concurrency", FunctionName=FUNCTION, ReservedConcurrentExecutions=0)

    def _restore_capacity(self):
        for fleet, old in self.state["fleets"].items():
            desired = max(1, old["service"]["desiredCount"]) if fleet == "l0" else old["service"]["desiredCount"]
            self._write("ecs", "update_service", cluster=CLUSTER, service=f"clhear-fleet-{fleet}",
                        taskDefinition=old["new_task_definition"], desiredCount=desired)
            self._scaling(fleet, old["scaling"].get("SuspendedState", {k: False for k in SUSPENDED}),
                          minimum=max(1, old["scaling"]["MinCapacity"]) if fleet == "l0" else None)
        self.clients["ecs"].get_waiter("services_stable").wait(cluster=CLUSTER,
            services=[f"clhear-fleet-{f}" for f in FLEETS], WaiterConfig={"Delay": 15, "MaxAttempts": 40})

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
                self._write("lambda", "update_function_code", FunctionName=FUNCTION, S3Bucket=BUCKET,
                    S3Key=f"{self.prefix}/previous-viewer.zip", S3ObjectVersion=self.state["previous_code_version"],
                    RevisionId=config["RevisionId"], Publish=False)
                self.clients["lambda"].get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
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
            self._hold()
            self._configure_schedules()
            self._register()
            self._worker("l0", "bootstrap")
            self._viewer()
            result = self._worker("l1", "verify")
            self._worker("l0", "publish")
            self.clients["s3"].head_object(Bucket=BUCKET, Key=self.inputs.viewer_key, ExpectedBucketOwner=ACCOUNT)
            self._resume_viewer()
            self._restore_capacity()
            self.report.update(status="review_ready" if result == 2 else "verified", recovery_required=False,
                               l0_relay_minimum=1, traffic_policy="previous_capacity_restored", fleet_policy="new_code_l1_only")
            self._put("result.json", json.dumps(self.report, sort_keys=True).encode())
        except Exception as error:
            self.report["failure_type"] = type(error).__name__
            self._rollback()
            try:
                self._put("failure.json", json.dumps(self.report, sort_keys=True).encode())
            except Exception:
                self.report["recovery_errors"].append("private_failure_report_write_failed")
        return self.report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("sha", "image", "ui-key", "ui-sha256", "ui-version", "deployment-id"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--reviewer-emails", default="")
    parser.add_argument("--viewer-key", default="webui/l1/candidate.db")
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
        report = {**deployer.report, "status": "preflight_failed", "failure_type": type(error).__name__}
        if isinstance(error, DeploymentError):
            report["reason"] = str(error)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.chmod(args.output, 0o600)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in {"preflight_passed", "verified", "review_ready"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
