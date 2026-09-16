"""AWS control-plane deployment invariants; every AWS call is mocked.

These tests neither contact AWS nor open a corpus database. Worker exit codes
stand for the separate worker verification tests, not for a successful import.
"""
import base64
import copy
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from botocore.loaders import Loader
from botocore.waiter import Waiter, WaiterModel

from scripts.deploy_l1 import (ACCOUNT, ADAPTER_SCHEDULES, BUCKET, CLUSTER, ENVIRONMENT, FLEETS, FUNCTION,
                              QUEUES, REGION, SUSPENDED, WORKFLOW, Deployer, DeploymentError, Inputs)

SHA = "a" * 40
DIGEST = "b" * 64
ROLE = f"arn:aws:iam::{ACCOUNT}:role/clhear-l1-deployment"
DSN_SECRET = f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/clhear/DATABASE_URL"
ENDPOINT = f"clhear-record.cluster-abcdef.{REGION}.rds.amazonaws.com"
NEW_CODE, OLD_CODE = b"new test-only code artifact", b"previous test-only code artifact"
LAMBDA_WAITER_MODEL = Loader().load_service_model("lambda", "waiters-2")
WRITES = {"put_object", "register_scalable_target", "put_function_concurrency", "delete_function_concurrency",
          "update_service", "stop_task", "register_task_definition", "run_task", "update_function_code",
          "update_function_configuration", "invoke", "put_targets"}


def inputs(**overrides):
    values = dict(sha=SHA, image=f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/clhear@sha256:{DIGEST}",
        ui_key=f"webui/{SHA}-123-1.zip", ui_sha256=hashlib.sha256(NEW_CODE).hexdigest(),
        ui_version="immutable-new-version", deployment_id="l1-123-1", reviewer_emails="reviewer@example.test")
    return Inputs(**(values | overrides))


def environment(**overrides):
    return {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "Reg42-ai/CLHEAR-MVP",
            "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": SHA, "GITHUB_WORKFLOW_REF": WORKFLOW,
            "CLHEAR_DEPLOY_ENVIRONMENT": ENVIRONMENT, "CLHEAR_DEPLOY_ROLE_ARN": ROLE,
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://actions.example.test/id-token",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "test-only-oidc-token", **overrides}


class Client:
    def __init__(self, cloud, service):
        self.cloud, self.service = cloud, service

    def __getattr__(self, operation):
        if operation == "get_waiter":
            return lambda name: SimpleNamespace(wait=lambda **kw: self.cloud.call(self.service, f"wait:{name}", kw))
        return lambda **kw: self.cloud.call(self.service, operation, kw)


class Cloud:
    def __init__(self):
        self.calls, self.objects = [], {}
        self.account = ACCOUNT
        self.role = f"arn:aws:sts::{ACCOUNT}:assumed-role/clhear-l1-deployment/github-123"
        self.bucket_private, self.bucket_versioned = True, True
        self.dsn = f"postgresql+psycopg://clhear:test-secret-not-for-logs@{ENDPOINT}:5432/clhear?sslmode=require"
        self.ui_metadata = {"git-sha": SHA}
        self.ui_code = NEW_CODE
        self.tags = [f"{SHA}-123-1"]
        self.database_backup_overrides = {}
        self.exits, self.task_actions = {}, {}
        self.fail_action, self.fail_stop, self.fail_scaler = None, False, None
        self.fail_pause_after_probe, self.probe_failed = False, False
        self.viewer_queue_authorized = True
        self.event_failed_rule, self.event_bad_readback = None, False
        self.event_failure_response = {"FailedEntryCount": 1, "FailedEntries": [{"TargetId": "existing-target-id", "ErrorCode": "InternalException"}]}
        self.rules, self.targets = {}, {}
        for adapter in ADAPTER_SCHEDULES:
            name = f"clhear-adapter-{adapter}"
            self.rules[name] = {"Name": name, "Arn": f"arn:aws:events:{REGION}:{ACCOUNT}:rule/{name}",
                                "State": "ENABLED", "ScheduleExpression": "cron(0 0 * * ? *)"}
            self.targets[name] = {"Id": f"existing-{adapter}", "Arn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:clhear-events",
                "Input": json.dumps(Deployer._schedule_payload(adapter, event_id=f"schedule-{adapter}", event_time="")),
                "RetryPolicy": {"MaximumRetryAttempts": 5, "MaximumEventAgeInSeconds": 300},
                "DeadLetterConfig": {"Arn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:clhear-events-dlq"}}
        self.anonymous_status = 401
        self.old_tasks = {fleet: [] for fleet in FLEETS}
        self.scaling, self.services, self.definitions = {}, {}, {}
        for fleet in FLEETS:
            arn = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/clhear-fleet-{fleet}:1"
            self.services[fleet] = {"serviceName": f"clhear-fleet-{fleet}", "status": "ACTIVE", "taskDefinition": arn,
                "serviceArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/{CLUSTER}/clhear-fleet-{fleet}",
                "clusterArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/{CLUSTER}",
                "desiredCount": 0 if fleet == "l0" else 1, "runningCount": 0, "pendingCount": 0,
                "networkConfiguration": {"awsvpcConfiguration": {"subnets": ["subnet-private"], "securityGroups": ["sg-existing"], "assignPublicIp": "DISABLED"}},
                "capacityProviderStrategy": [{"capacityProvider": "FARGATE_SPOT", "weight": 1}], "platformVersion": "1.4.0"}
            self.definitions[arn] = {"taskDefinitionArn": arn, "family": f"clhear-fleet-{fleet}", "networkMode": "awsvpc",
                "requiresCompatibilities": ["FARGATE"], "cpu": "256", "memory": "512",
                "taskRoleArn": f"arn:aws:iam::{ACCOUNT}:role/clhear-worker", "executionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/clhear-execution",
                "containerDefinitions": [{"name": "worker", "image": "legacy:latest", "essential": True,
                    "entryPoint": ["python", "-m", "app.clhear.workers"],
                    "environment": [{"name": "CLHEAR_FLEET", "value": fleet.upper()}, {"name": "CLHEAR_SNAPSHOT_S3_URI", "value": ""},
                                    {"name": "CLHEAR_EVENTS_QUEUE_URL", "value": QUEUES[fleet]},
                                    {"name": "CLHEAR_FLEET_QUEUE_URLS", "value": json.dumps(QUEUES)},
                                    {"name": "LEGACY_UNKNOWN_SETTING", "value": "private-env-value"}],
                    "secrets": [{"name": "DATABASE_URL", "valueFrom": DSN_SECRET}, {"name": "INFER_TOKEN", "valueFrom": "existing-secret-ref"}]}]}
            self.scaling[fleet] = {"ResourceId": f"service/{CLUSTER}/clhear-fleet-{fleet}", "MinCapacity": 0,
                "ServiceNamespace": "ecs", "ScalableDimension": "ecs:service:DesiredCount",
                "ScalableTargetARN": f"arn:aws:application-autoscaling:{REGION}:{ACCOUNT}:scalable-target/{int(fleet[1:]) + 1:036x}",
                "MaxCapacity": 2, "SuspendedState": {key: False for key in SUSPENDED}}
        self.config = {"FunctionName": FUNCTION, "State": "Active", "LastUpdateStatus": "Successful", "PackageType": "Zip",
            "FunctionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{FUNCTION}",
            "Handler": "app.clhear.lambda_web.handler", "Role": f"arn:aws:iam::{ACCOUNT}:role/clhear-webui",
            "RevisionId": "old-revision", "CodeSha256": base64.b64encode(hashlib.sha256(OLD_CODE).digest()).decode(),
            "Environment": {"Variables": {"CLHEAR_SESSION_SECRET": "test-only-private-session-value-at-least-32-chars",
                "GOOGLE_OAUTH_CLIENT_SECRET": "private-oauth-value", "CLHEAR_SES_SENDER": "review@example.test",
                "CLHEAR_RESTRICTED_ACCESS": "false", "CLHEAR_AUTH_DEBUG": "true", "CLHEAR_DB_S3_URI": f"s3://{BUCKET}/webui/legacy.db"}}}
        self.concurrency = None
        self.lambda_account_concurrency_limit = 1000
        self.lambda_other_reserved_concurrency = 0
        self.lambda_unreserved_minimum = 100
        self.pending_lambda_update = None
        self.lambda_update_evidence = []
        self.advance_completion_revision = True
        self.lambda_failure_modes = {}
        self.clients = {name: Client(self, name) for name in
                        ("sts", "s3", "ecr", "rds", "ssm", "secretsmanager", "ecs", "lambda", "application-autoscaling", "sqs", "iam", "events")}

    def call(self, service, operation, args):
        self.calls.append((service, operation, copy.deepcopy(args)))
        if operation == "wait:function_updated_v2":
            # Use the SDK's real acceptors. Reads must observe InProgress and
            # then Successful; completion changes revision like live Lambda.
            model = copy.deepcopy(LAMBDA_WAITER_MODEL)
            model["waiters"]["FunctionUpdatedV2"]["delay"] = 0
            waiter = Waiter("FunctionUpdatedV2", WaiterModel(model).get_waiter("FunctionUpdatedV2"),
                            lambda **kw: self.call("lambda", "get_function", kw))
            return waiter.wait(**args)
        if operation == "wait:services_stable":
            for current in self.services.values():
                current.update(runningCount=current["desiredCount"], pendingCount=0,
                    deployments=[{"status": "PRIMARY", "rolloutState": "COMPLETED",
                                  "taskDefinition": current["taskDefinition"]}])
            return None
        if operation.startswith("wait:"):
            return None
        if operation == "get_caller_identity":
            return {"Account": self.account, "Arn": self.role}
        if operation == "get_public_access_block":
            return {"PublicAccessBlockConfiguration": {k: self.bucket_private for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")}}
        if operation == "get_bucket_versioning":
            return {"Status": "Enabled" if self.bucket_versioned else "Suspended"}
        if operation == "head_object":
            return {"Metadata": self.ui_metadata, "VersionId": "version", "ContentLength": 123}
        if operation == "get_object":
            return {"Body": io.BytesIO(self.ui_code)}
        if operation == "describe_images":
            return {"imageDetails": [{"imageDigest": f"sha256:{DIGEST}", "imageTags": self.tags}]}
        if operation == "describe_db_clusters":
            now = datetime.now(timezone.utc)
            return {"DBClusters": [{"DBClusterIdentifier": "clhear-record", "Status": "available", "Engine": "aurora-postgresql", "Endpoint": ENDPOINT,
                                   "BackupRetentionPeriod": 35, "EarliestRestorableTime": now - timedelta(days=2),
                                   "LatestRestorableTime": now - timedelta(minutes=5), **self.database_backup_overrides}]}
        if operation == "get_parameter":
            return {"Parameter": {"Type": "SecureString", "Value": self.dsn}}
        if operation == "list_rules":
            return {"Rules": copy.deepcopy(list(self.rules.values()))}
        if operation == "describe_rule":
            return copy.deepcopy(self.rules[args["Name"]])
        if operation == "list_targets_by_rule":
            return {"Targets": [copy.deepcopy(self.targets[args["Rule"]])]} if args["Rule"] in self.targets else {"Targets": []}
        if operation == "put_targets":
            if args["Rule"] == self.event_failed_rule:
                return self.event_failure_response
            if not self.event_bad_readback:
                self.targets[args["Rule"]] = copy.deepcopy(args["Targets"][0])
            return {"FailedEntryCount": 0, "FailedEntries": []}
        if operation == "get_queue_attributes":
            return {"Attributes": {"QueueArn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:{args['QueueUrl'].rsplit('/', 1)[1]}"}}
        if operation == "simulate_principal_policy":
            return {"EvaluationResults": [{"EvalDecision": "allowed" if self.viewer_queue_authorized else "implicitDeny"}]}
        if operation == "describe_services":
            return {"services": copy.deepcopy(list(self.services.values()))}
        if operation == "describe_task_definition":
            return {"taskDefinition": copy.deepcopy(self.definitions[args["taskDefinition"]]), "tags": [{"key": "existing-tag", "value": "retained"}]}
        if operation == "describe_scalable_targets":
            return {"ScalableTargets": [copy.deepcopy(self.scaling[args["ResourceIds"][0].rsplit("-", 1)[-1]])]}
        if operation == "get_function":
            if self.pending_lambda_update is not None:
                failure = self.lambda_failure_modes.get(self.pending_lambda_update["kind"])
                if failure == "waiter_failed":
                    self.config.update(LastUpdateStatus="Failed", LastUpdateStatusReason="private-service-status-detail",
                                       LastUpdateStatusReasonCode="InternalError")
                    self.lambda_update_evidence[-1]["completed"] = copy.deepcopy(self.config)
                    self.pending_lambda_update = None
                elif failure == "waiter_timeout":
                    # The actual modeled operation stays pending. A rollback
                    # write must conflict instead of mutating code under it.
                    pass
                elif self.pending_lambda_update["reads_remaining"]:
                    self.pending_lambda_update["reads_remaining"] -= 1
                else:
                    self.config["LastUpdateStatus"] = "Successful"
                    self.config.pop("LastUpdateStatusReason", None)
                    self.config.pop("LastUpdateStatusReasonCode", None)
                    if self.advance_completion_revision:
                        self.config["RevisionId"] += "-completed"
                    self.lambda_update_evidence[-1]["completed"] = copy.deepcopy(self.config)
                    self.pending_lambda_update = None
            return {"Configuration": copy.deepcopy(self.config), "Code": {"Location": "https://awslambda-us-east-1.s3.amazonaws.com/test-code?test-signed-query"}}
        if operation == "get_function_configuration":
            return copy.deepcopy(self.config)
        if operation == "get_function_concurrency":
            return {} if self.concurrency is None else {"ReservedConcurrentExecutions": self.concurrency}
        if operation == "put_object":
            assert args["Bucket"] == BUCKET and args["ServerSideEncryption"] == "AES256"
            assert args["IfNoneMatch"] == "*"
            self.objects[args["Key"]] = args["Body"]
            return {"VersionId": "private-version"}
        if operation == "put_function_concurrency":
            requested = args["ReservedConcurrentExecutions"]
            if requested > 0 and self.lambda_account_concurrency_limit - self.lambda_other_reserved_concurrency - requested < self.lambda_unreserved_minimum:
                raise ClientError({"Error": {"Code": "InvalidParameterValueException",
                    "Message": "The requested reservation would reduce unreserved concurrency below its minimum."},
                    "ResponseMetadata": {"HTTPStatusCode": 400}}, "PutFunctionConcurrency")
            if self.fail_pause_after_probe and self.probe_failed and args["ReservedConcurrentExecutions"] == 0:
                raise RuntimeError("test-only pause failure")
            self.concurrency = args["ReservedConcurrentExecutions"]
            self.config["RevisionId"] += "-concurrency"
            return {}
        if operation == "delete_function_concurrency":
            self.concurrency = None
            self.config["RevisionId"] += "-concurrency-deleted"
            return {}
        if operation == "register_scalable_target":
            fleet = args["ResourceId"].rsplit("-", 1)[-1]
            if fleet == self.fail_scaler:
                raise RuntimeError("test-only scaler failure")
            self.scaling[fleet].update(copy.deepcopy(args))
            return {}
        if operation == "update_service":
            fleet = args["service"].rsplit("-", 1)[-1]
            self.services[fleet].update({key: args[key] for key in ("desiredCount", "taskDefinition") if key in args})
            if args.get("desiredCount") == 0:
                self.services[fleet].update(runningCount=0, pendingCount=0)
            return {}
        if operation == "list_tasks":
            return {"taskArns": self.old_tasks[args["family"].rsplit("-", 1)[-1]][:]}
        if operation == "stop_task":
            if not self.fail_stop:
                for tasks in self.old_tasks.values():
                    if args["task"] in tasks:
                        tasks.remove(args["task"])
            return {}
        if operation == "register_task_definition":
            arn = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{args['family']}:2"
            self.definitions[arn] = copy.deepcopy(args) | {"taskDefinitionArn": arn}
            return {"taskDefinition": {"taskDefinitionArn": arn}}
        if operation == "run_task":
            command = args["overrides"]["containerOverrides"][0]["command"]
            action = command[1]
            if action == self.fail_action:
                return {"tasks": [], "failures": [{"reason": "test-only failure"}]}
            arn = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/{CLUSTER}/{action}"
            self.task_actions[arn] = action
            return {"tasks": [{"taskArn": arn}]}
        if operation == "describe_tasks":
            tasks = []
            for arn in args["tasks"]:
                action = self.task_actions.get(arn)
                if action:
                    tasks.append({"taskArn": arn, "lastStatus": "STOPPED", "containers": [{"name": "worker", "exitCode": self.exits.get(action, 0)}]})
                else:
                    tasks.append({"taskArn": arn, "lastStatus": "RUNNING" if any(arn in old for old in self.old_tasks.values()) else "STOPPED"})
            return {"tasks": tasks}
        if operation == "update_function_code":
            self.require_no_lambda_update("UpdateFunctionCode")
            if args["RevisionId"] != self.config["RevisionId"]:
                raise ClientError({"Error": {"Code": "PreconditionFailedException", "Message": "test-only-private-sdk-message"}},
                                  "UpdateFunctionCode")
            code = OLD_CODE if args["S3Key"].endswith("previous-viewer.zip") else NEW_CODE
            self.config["CodeSha256"] = base64.b64encode(hashlib.sha256(code).digest()).decode()
            self.config["CodeSize"] = len(code)
            return self.start_lambda_update("rollback" if code == OLD_CODE else "code")
        if operation == "update_function_configuration":
            self.require_no_lambda_update("UpdateFunctionConfiguration")
            if args["RevisionId"] != self.config["RevisionId"]:
                raise ClientError({"Error": {"Code": "PreconditionFailedException", "Message": "test-only-private-sdk-message"}},
                                  "UpdateFunctionConfiguration")
            self.config["Environment"] = copy.deepcopy(args["Environment"])
            return self.start_lambda_update("configuration")
        if operation == "invoke":
            if self.concurrency == 0:
                raise ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "The function is throttled."}}, "Invoke")
            path = json.loads(args["Payload"])["rawPath"]
            if not path.endswith("health"):
                self.probe_failed = self.anonymous_status != 401
            result = {"statusCode": 200 if path.endswith("health") else self.anonymous_status, "body": "{}"}
            return {"Payload": io.BytesIO(json.dumps(result).encode())}
        raise AssertionError(f"Unexpected mocked AWS operation {service}.{operation}")

    def require_no_lambda_update(self, operation):
        if self.pending_lambda_update is not None:
            raise ClientError({"Error": {"Code": "ResourceConflictException", "Message": "A function update is in progress."},
                               "ResponseMetadata": {"HTTPStatusCode": 409}}, operation)

    def start_lambda_update(self, kind):
        assert self.pending_lambda_update is None, "Lambda does not accept another update while one is pending"
        self.config.update(LastUpdateStatus="InProgress", LastUpdateStatusReason="The function is being updated.",
                           LastUpdateStatusReasonCode="Creating")
        self.config["RevisionId"] += f"-{kind}-in-progress"
        self.pending_lambda_update = {"reads_remaining": 1, "kind": kind}
        response = copy.deepcopy(self.config)
        self.lambda_update_evidence.append({"kind": kind, "response": response})
        return response

    def deployer(self, **kw):
        return Deployer(kw.pop("input", inputs()), self.clients, environ=kw.pop("environ", environment()),
                        sleep=lambda _: None, code_fetch=kw.pop("code_fetch", lambda _: OLD_CODE), **kw)

    @property
    def mutations(self):
        return [(service, operation, args) for service, operation, args in self.calls if operation in WRITES]


def test_preflight_only_reads_live_configuration_and_artifact_bytes():
    cloud = Cloud()
    assert cloud.deployer().preflight()["status"] == "preflight_passed"
    assert not cloud.mutations
    assert len([c for c in cloud.calls if c[1] == "get_parameter"]) == 1


@pytest.mark.parametrize("overrides", [
    {"BackupRetentionPeriod": 0}, {"BackupRetentionPeriod": None},
    {"EarliestRestorableTime": None}, {"LatestRestorableTime": None},
    {"LatestRestorableTime": datetime(2020, 1, 1, tzinfo=timezone.utc)},
    {"LatestRestorableTime": datetime(2099, 1, 1, tzinfo=timezone.utc)},
    {"LatestRestorableTime": datetime(2026, 1, 1)},
])
def test_missing_or_stale_aurora_recovery_window_blocks_deployment_before_writes(overrides):
    cloud = Cloud()
    cloud.database_backup_overrides = overrides
    with pytest.raises(DeploymentError, match="Aurora"):
        cloud.deployer().preflight()
    assert not cloud.mutations


def test_aurora_recovery_evidence_is_read_back_before_migrating(monkeypatch):
    cloud = Cloud()
    controller = cloud.deployer()
    register = controller._register

    def expired_after_preflight():
        register()
        cloud.database_backup_overrides = {"LatestRestorableTime": datetime(2020, 1, 1, tzinfo=timezone.utc)}

    monkeypatch.setattr(controller, "_register", expired_after_preflight)
    result = controller.deploy()
    assert result["status"] == "failed_maintenance"
    assert result["database_recovery"]["restore_test_performed"] is False
    assert "Aurora" in result["reason"]
    assert not any(op == "run_task" for _, op, _ in cloud.calls)


@pytest.mark.parametrize("invocation", [
    {"entryPoint": ["python", "-m", "app.clhear.workers"]},
    {"entryPoint": ["python", "-m", "app.clhear.workers"], "command": []},
    {"entryPoint": ["python"], "command": ["-m", "app.clhear.workers"]},
    {"entryPoint": ["python", "-m"], "command": ["app.clhear.workers"]},
])
def test_preflight_accepts_only_equivalent_explicit_worker_argv_splits(invocation):
    cloud = Cloud()
    worker = cloud.definitions[cloud.services["l0"]["taskDefinition"]]["containerDefinitions"][0]
    worker.update(invocation)
    assert cloud.deployer().preflight()["status"] == "preflight_passed"
    assert not cloud.mutations


@pytest.mark.parametrize("invocation", [
    {"entryPoint": ["python"], "command": ["-m", "app.other.workers"]},
    {"entryPoint": ["python", "-m", "app.clhear.workers"], "command": ["--once"]},
    {"entryPoint": ["sh", "-c"], "command": ["python -m app.clhear.workers"]},
    {"entryPoint": ["env", "python"], "command": ["-m", "app.clhear.workers"]},
    {"entryPoint": ["python", "-c"], "command": ["import app.clhear.workers"]},
    {"entryPoint": ["python", "-m"], "command": ["app.clhear.workers;unexpected"]},
    {"entryPoint": ["python", "-m", "app.clhear.workers", "unexpected"], "command": []},
    {"entryPoint": [], "command": ["python", "-m", "app.clhear.workers"]},
    {"entryPoint": None, "command": ["python", "-m", "app.clhear.workers"]},
    {"entryPoint": "python -m app.clhear.workers", "command": []},
    {"entryPoint": ["python"], "command": "-m app.clhear.workers"},
    {"entryPoint": ["python", "-m", "app.clhear.workers"], "command": None},
])
def test_preflight_rejects_unrecognized_worker_invocations_before_writes(invocation):
    cloud = Cloud()
    worker = cloud.definitions[cloud.services["l0"]["taskDefinition"]]["containerDefinitions"][0]
    worker.update(invocation)
    with pytest.raises(DeploymentError, match="worker entrypoint"):
        cloud.deployer().deploy()
    assert not cloud.mutations


def test_split_worker_entrypoint_is_canonicalized_before_phase_command_overrides():
    cloud = Cloud()
    for definition in cloud.definitions.values():
        definition["containerDefinitions"][0].update(entryPoint=["python"], command=["-m", "app.clhear.workers"])
    original = copy.deepcopy(cloud.definitions)
    result = cloud.deployer().deploy()
    assert result["status"] == "verified"
    for fleet in FLEETS:
        worker = cloud.definitions[cloud.services[fleet]["taskDefinition"]]["containerDefinitions"][0]
        assert worker["entryPoint"] == ["python", "-m", "app.clhear.workers"] and worker["command"] == []
    launches = [args for _, operation, args in cloud.calls if operation == "run_task"]
    for action, args in zip(("bootstrap", "verify", "publish"), launches, strict=True):
        definition = cloud.definitions[args["taskDefinition"]]
        worker = next(c for c in definition["containerDefinitions"] if c["name"] == "worker")
        override = args["overrides"]["containerOverrides"][0]["command"]
        assert worker["entryPoint"] + override == [
            "python", "-m", "app.clhear.workers", "--verify-deployment", action,
            "--verification-id", inputs().deployment_id,
        ]
    assert all(cloud.definitions[arn] == definition for arn, definition in original.items())


def test_missing_legacy_queue_map_is_checked_read_only_then_registered_for_every_fleet():
    cloud = Cloud()
    for definition in cloud.definitions.values():
        worker = definition["containerDefinitions"][0]
        worker["environment"] = [item for item in worker["environment"] if item["name"] != "CLHEAR_FLEET_QUEUE_URLS"]
    assert cloud.deployer().preflight()["status"] == "preflight_passed"
    assert not cloud.mutations
    assert cloud.deployer().deploy()["status"] == "verified"
    for fleet in FLEETS:
        worker = cloud.definitions[cloud.services[fleet]["taskDefinition"]]["containerDefinitions"][0]
        env = {item["name"]: item["value"] for item in worker["environment"]}
        assert json.loads(env["CLHEAR_FLEET_QUEUE_URLS"]) == QUEUES
        assert env["CLHEAR_EVENTS_QUEUE_URL"] == QUEUES[fleet]


@pytest.mark.parametrize("queue_map", ["", "not-json", "{}", "null", "[]", json.dumps(QUEUES | {"l0": QUEUES["l1"]})])
def test_explicitly_invalid_queue_maps_still_fail_before_writes(queue_map):
    cloud = Cloud()
    worker = cloud.definitions[cloud.services["l0"]["taskDefinition"]]["containerDefinitions"][0]
    next(item for item in worker["environment"] if item["name"] == "CLHEAR_FLEET_QUEUE_URLS")["value"] = queue_map
    with pytest.raises(DeploymentError, match="owning queues"):
        cloud.deployer().deploy()
    assert not cloud.mutations


@pytest.mark.parametrize("change", ["account", "role", "workflow", "sha", "environment", "oidc"])
def test_apply_guard_rejects_wrong_context_before_any_write(change):
    cloud, env = Cloud(), environment()
    if change == "account": cloud.account = "999999999999"
    if change == "role": cloud.role = f"arn:aws:sts::{ACCOUNT}:assumed-role/clhear-github-release/session"
    if change == "workflow": env["GITHUB_WORKFLOW_REF"] = "untrusted-workflow"
    if change == "sha": env["GITHUB_SHA"] = "c" * 40
    if change == "environment": env["CLHEAR_DEPLOY_ENVIRONMENT"] = "other"
    if change == "oidc": env.pop("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    with pytest.raises(DeploymentError): cloud.deployer(environ=env).deploy()
    assert not cloud.mutations


@pytest.mark.parametrize("change", ["fallback", "inline_dsn", "sqlite", "other_db", "no_tls", "weak_session", "public_bucket", "unversioned_bucket", "wrong_ui_hash", "wrong_ui_sha", "wrong_image_sha", "wrong_owning_queue", "missing_queue_map", "viewer_iam"])
def test_preflight_blocks_configuration_and_identity_gaps_without_writes(change):
    cloud = Cloud()
    worker = next(iter(cloud.definitions.values()))["containerDefinitions"][0]
    if change == "fallback": worker["environment"][1]["value"] = f"s3://{BUCKET}/old.db"
    if change == "inline_dsn": worker["environment"].append({"name": "DATABASE_URL", "value": cloud.dsn})
    if change == "sqlite": cloud.dsn = "sqlite:////tmp/fallback.db"
    if change == "other_db": cloud.dsn = cloud.dsn.replace(ENDPOINT, "other.example.test")
    if change == "no_tls": cloud.dsn = cloud.dsn.replace("?sslmode=require", "")
    if change == "weak_session": cloud.config["Environment"]["Variables"]["CLHEAR_SESSION_SECRET"] = "CHANGEME"
    if change == "public_bucket": cloud.bucket_private = False
    if change == "unversioned_bucket": cloud.bucket_versioned = False
    if change == "wrong_ui_hash": cloud.ui_code = b"tampered"
    if change == "wrong_ui_sha": cloud.ui_metadata = {"git-sha": "c" * 40}
    if change == "wrong_image_sha": cloud.tags = [SHA]
    if change == "wrong_owning_queue": worker["environment"][2]["value"] = QUEUES["l1"]
    if change == "missing_queue_map": worker["environment"][3]["value"] = "{}"
    if change == "viewer_iam": cloud.viewer_queue_authorized = False
    with pytest.raises(DeploymentError): cloud.deployer().deploy()
    assert not cloud.mutations


def test_success_preserves_configuration_and_orders_hold_bootstrap_cutover_verification():
    cloud = Cloud()
    cloud.old_tasks["l2"] = ["old-downstream-task"]
    original = copy.deepcopy(cloud.definitions)
    result = cloud.deployer().deploy()
    assert result["status"] == "verified" and result["accepted_release_changed"] is False
    operations = [op for _, op, _ in cloud.calls]
    launches = [(index, args) for index, (_, op, args) in enumerate(cloud.calls) if op == "run_task"]
    assert [args["overrides"]["containerOverrides"][0]["command"][1] for _, args in launches] == ["bootstrap", "verify", "publish"]
    assert operations.index("put_function_concurrency") < operations.index("stop_task") < launches[0][0] < operations.index("update_function_code") < launches[1][0]
    assert len([op for op in operations[:launches[0][0]] if op == "register_task_definition"]) == 9
    assert all(args["networkConfiguration"] == cloud.services["l0"]["networkConfiguration"] for _, args in launches)
    assert all(args["launchType"] == "FARGATE" and "capacityProviderStrategy" not in args for _, args in launches)
    old_wait = next(index for index, (_, op, args) in enumerate(cloud.calls) if op == "wait:tasks_stopped" and "old-downstream-task" in args["tasks"])
    assert old_wait < launches[0][0]
    verify_wait = next(args for _, op, args in cloud.calls if op == "wait:tasks_stopped" and any(arn.endswith("/verify") for arn in args["tasks"]))
    assert verify_wait["WaiterConfig"]["Delay"] * verify_wait["WaiterConfig"]["MaxAttempts"] == 5400
    for fleet in FLEETS:
        current = cloud.definitions[cloud.services[fleet]["taskDefinition"]]
        old = original[f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/clhear-fleet-{fleet}:1"]
        assert current["taskRoleArn"] == old["taskRoleArn"] and current["executionRoleArn"] == old["executionRoleArn"]
        worker = current["containerDefinitions"][0]
        assert worker["image"] == inputs().image and worker["secrets"] == old["containerDefinitions"][0]["secrets"]
        env = {v["name"]: v["value"] for v in worker["environment"]}
        assert env["CLHEAR_L1_ONLY"] == "true" and env["CLHEAR_SNAPSHOT_S3_URI"] == "" and env["CLHEAR_ARTIFACT_STORE"] == "s3"
        assert env["LEGACY_UNKNOWN_SETTING"] == "private-env-value"
        assert cloud.scaling[fleet]["SuspendedState"] == {key: False for key in SUSPENDED}
    for fleet in ("l0", "l1"):
        assert cloud.services[fleet]["desiredCount"] == cloud.scaling[fleet]["MinCapacity"] == 1
    env = cloud.config["Environment"]["Variables"]
    assert env["CLHEAR_RESTRICTED_ACCESS"] == "true" and env["CLHEAR_AUTH_DEBUG"] == "false"
    assert env["CLHEAR_SESSION_SECRET"].startswith("test-only-private-session") and env["GOOGLE_OAUTH_CLIENT_SECRET"] == "private-oauth-value"
    assert env["CLHEAR_EVENTS_QUEUE_URL"] == QUEUES["l0"]
    assert cloud.concurrency is None


def test_review_ready_runs_final_snapshot_without_claiming_acceptance():
    cloud = Cloud()
    cloud.exits["verify"] = 2
    result = cloud.deployer().deploy()
    assert result["status"] == "review_ready" and result["accepted_release_changed"] is False
    assert result["steps"][-1]["action"] == "publish"


def test_unreserved_viewer_resumes_without_requesting_a_positive_reservation():
    cloud = Cloud()
    cloud.lambda_account_concurrency_limit = cloud.lambda_unreserved_minimum
    cloud.exits["verify"] = 2
    result = cloud.deployer().deploy()
    assert result["status"] == "review_ready" and cloud.concurrency is None
    assert all(args["ReservedConcurrentExecutions"] == 0 for _, op, args in cloud.calls if op == "put_function_concurrency")
    operations = [op for _, op, _ in cloud.calls]
    assert operations.index("delete_function_concurrency") < operations.index("invoke")
    assert len([op for op in operations if op == "invoke"]) == 2


def test_quota_rejection_does_not_mutate_mock_lambda_configuration():
    cloud = Cloud()
    cloud.lambda_account_concurrency_limit = cloud.lambda_unreserved_minimum
    before = copy.deepcopy(cloud.config)
    with pytest.raises(ClientError) as error:
        cloud.call("lambda", "put_function_concurrency", {"ReservedConcurrentExecutions": 1})
    assert error.value.response["Error"]["Code"] == "InvalidParameterValueException"
    assert cloud.concurrency is None and cloud.config == before


@pytest.mark.parametrize("reserved", [0, 4])
def test_reserved_policy_is_restored_exactly_and_zero_is_repaused(reserved):
    cloud = Cloud()
    cloud.concurrency = reserved
    result = cloud.deployer().deploy()
    assert result["status"] == "verified" and cloud.concurrency == reserved
    assert not any(op == "delete_function_concurrency" for _, op, _ in cloud.calls)
    reservations = [args["ReservedConcurrentExecutions"] for _, op, args in cloud.calls if op == "put_function_concurrency"]
    assert reservations == ([0, 1, 0] if reserved == 0 else [0, reserved])
    assert [args for _, op, args in cloud.calls if op == "get_function_concurrency"]


@pytest.mark.parametrize("reserved", [0, 4])
def test_reservation_quota_failure_never_falls_back_to_unreserved_or_resumes_fleets(monkeypatch, reserved):
    cloud = Cloud()
    cloud.concurrency = reserved
    cloud.lambda_account_concurrency_limit = cloud.lambda_unreserved_minimum + reserved
    original_call = cloud.call

    def call(service, operation, args):
        result = original_call(service, operation, args)
        if operation == "put_function_concurrency" and args["ReservedConcurrentExecutions"] == 0:
            # Another function can reserve the released capacity while we run
            # the worker checks. Restoration must not change the approved cap.
            cloud.lambda_other_reserved_concurrency = reserved
        return result

    monkeypatch.setattr(cloud, "call", call)
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance" and cloud.concurrency == 0
    assert result["failure_operation"] == "PutFunctionConcurrency" and result["failure_code"] == "InvalidParameterValueException"
    assert not any(op in {"delete_function_concurrency", "invoke"} for _, op, _ in cloud.calls)
    assert all(service["desiredCount"] == 0 for service in cloud.services.values())


@pytest.mark.parametrize("failure", ["delete", "unreserved_readback", "reserved_readback", "re_pause_readback", "probe"])
def test_concurrency_restore_and_probe_failures_leave_maintenance_held(monkeypatch, failure):
    cloud = Cloud()
    cloud.concurrency = 4 if failure == "reserved_readback" else 0 if failure == "re_pause_readback" else None
    original_call = cloud.call
    restore_started, probes = False, 0

    def call(service, operation, args):
        nonlocal restore_started, probes
        if operation == "delete_function_concurrency" and failure == "delete":
            cloud.calls.append((service, operation, copy.deepcopy(args)))
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "Denied"}}, "DeleteFunctionConcurrency")
        result = original_call(service, operation, args)
        if operation == "delete_function_concurrency" or (operation == "put_function_concurrency" and args["ReservedConcurrentExecutions"] > 0):
            restore_started = True
        if operation == "get_function_concurrency" and restore_started:
            if failure == "unreserved_readback":
                return {"ReservedConcurrentExecutions": 0}
            if failure == "reserved_readback":
                return {"ReservedConcurrentExecutions": 0}
            if failure == "re_pause_readback" and probes == 2:
                # One wrong re-pause read is enough to require maintenance;
                # subsequent recovery readbacks reflect the actual hold.
                probes += 1
                return {"ReservedConcurrentExecutions": 1}
        if operation == "invoke":
            probes += 1
            if failure == "probe":
                raise ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "Throttled"}}, "Invoke")
        return result

    monkeypatch.setattr(cloud, "call", call)
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance" and cloud.concurrency == 0
    assert all(service["desiredCount"] == 0 for service in cloud.services.values())
    if failure in {"delete", "unreserved_readback", "reserved_readback"}:
        assert not any(op == "invoke" for _, op, _ in cloud.calls)


def test_own_concurrency_revision_change_is_refreshed_before_conditional_code_cutover():
    cloud = Cloud()
    old_revision = cloud.config["RevisionId"]
    deployer = cloud.deployer()
    result = deployer.deploy()
    assert result["status"] == "verified"
    writes = [args for _, op, args in cloud.calls if op == "update_function_code"]
    assert len(writes) == 1
    assert writes[0]["RevisionId"] == old_revision + "-concurrency"
    # Refreshing the runtime token must not rewrite the reviewed rollback basis.
    assert deployer.state["function"]["Configuration"]["RevisionId"] == old_revision


def test_async_code_and_configuration_completion_revisions_drive_next_conditional_write():
    cloud = Cloud()
    deployer = cloud.deployer()
    result = deployer.deploy()
    assert result["status"] == "verified"
    assert [row["kind"] for row in cloud.lambda_update_evidence] == ["code", "configuration"]
    for row in cloud.lambda_update_evidence:
        assert row["response"]["LastUpdateStatus"] == "InProgress"
        assert row["completed"]["LastUpdateStatus"] == "Successful"
        assert row["response"]["RevisionId"] != row["completed"]["RevisionId"]
    config_write = next(args for _, op, args in cloud.calls if op == "update_function_configuration")
    assert config_write["RevisionId"] == cloud.lambda_update_evidence[0]["completed"]["RevisionId"]
    assert deployer.state["verified_viewer_configuration"]["RevisionId"] == cloud.lambda_update_evidence[1]["completed"]["RevisionId"]


def test_async_completion_also_accepts_a_revision_that_did_not_change():
    cloud = Cloud()
    cloud.advance_completion_revision = False
    result = cloud.deployer().deploy()
    assert result["status"] == "verified"
    assert all(row["result"] == "verified" and row["revision_transition"] is False for row in result["lambda_updates"])


@pytest.mark.parametrize("operation", ["update_function_code", "update_function_configuration"])
def test_mock_lambda_rejects_overlapping_writes_before_any_mutation(operation):
    cloud = Cloud()
    cloud.call("lambda", "update_function_code", {"RevisionId": cloud.config["RevisionId"], "S3Key": "new-viewer.zip"})
    before = copy.deepcopy(cloud.config)
    args = {"RevisionId": before["RevisionId"]}
    args.update({"S3Key": "previous-viewer.zip"} if operation == "update_function_code" else {"Environment": {"Variables": {"BAD": "value"}}})
    with pytest.raises(ClientError) as error:
        cloud.call("lambda", operation, args)
    assert error.value.response["Error"]["Code"] == "ResourceConflictException"
    assert cloud.config == before and cloud.config["LastUpdateStatus"] == "InProgress"


def test_configuration_cas_rejects_change_after_verified_code_completion(monkeypatch):
    cloud = Cloud()
    original_call = cloud.call

    def call(service, operation, args):
        if operation == "update_function_configuration":
            cloud.config["RevisionId"] += "-external"
            cloud.config["Environment"]["Variables"]["EXTERNAL_SETTING"] = "private-race-value"
        return original_call(service, operation, args)

    monkeypatch.setattr(cloud, "call", call)
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance" and cloud.concurrency == 0
    assert result["failure_operation"] == "UpdateFunctionConfiguration"
    assert result["failure_code"] == "PreconditionFailedException"
    assert cloud.config["Environment"]["Variables"]["EXTERNAL_SETTING"] == "private-race-value"
    assert cloud.config["Environment"]["Variables"]["CLHEAR_RESTRICTED_ACCESS"] == "false"
    assert not any(op == "invoke" for _, op, _ in cloud.calls)
    assert "private-race-value" not in json.dumps(result)


@pytest.mark.parametrize("stage", ["before_pause", "during_pause", "after_pause"])
@pytest.mark.parametrize("field,value", [
    ("CodeSha256", "external-code-digest"),
    ("Environment", {"Variables": {"CHANGED_SECRET": "test-only-concurrent-private-secret"}}),
    ("Role", f"arn:aws:iam::{ACCOUNT}:role/changed-viewer-role"),
    ("Handler", "app.other.handler"),
    ("Runtime", "python3.14"),
    ("VpcConfig", {"SubnetIds": ["subnet-external"]}),
    ("KMSKeyArn", f"arn:aws:kms:{REGION}:{ACCOUNT}:key/external-key"),
    ("FutureConfigurationField", {"changed": True}),
    ("LastUpdateStatus", "InProgress"),
])
def test_revision_refresh_rejects_code_or_configuration_drift(stage, field, value, monkeypatch):
    cloud = Cloud()
    original_call, injected = cloud.call, False

    def call(service, operation, args):
        nonlocal injected
        result = original_call(service, operation, args)
        inject = ((stage == "before_pause" and operation == "put_object" and args["Key"].endswith("/rollback.json"))
                  or (stage == "during_pause" and operation == "put_function_concurrency")
                  or (stage == "after_pause" and operation == "run_task"))
        if inject and not injected:
            injected = True
            cloud.config[field] = copy.deepcopy(value)
            cloud.config["RevisionId"] += "-external"
        return result

    monkeypatch.setattr(cloud, "call", call)
    result = cloud.deployer().deploy()
    assert injected and result["status"] == "failed_maintenance"
    assert result["reason"] in {"Viewer code or configuration changed after preflight", "The viewer has an unfinished Lambda update"}
    assert not any(op in {"update_function_code", "update_function_configuration"} for _, op, _ in cloud.calls)
    assert cloud.config[field] == value
    assert cloud.concurrency == 0 and all(service["desiredCount"] == 0 for service in cloud.services.values())
    assert "test-only-concurrent-private-secret" not in json.dumps(result)


@pytest.mark.parametrize("stage", ["before_pause", "after_pause"])
def test_revision_only_drift_outside_own_pause_is_not_adopted(stage, monkeypatch):
    cloud = Cloud()
    original_call, injected = cloud.call, False

    def call(service, operation, args):
        nonlocal injected
        result = original_call(service, operation, args)
        inject = ((stage == "before_pause" and operation == "put_object" and args["Key"].endswith("/rollback.json"))
                  or (stage == "after_pause" and operation == "run_task"))
        if inject and not injected:
            injected = True
            cloud.config["RevisionId"] += "-external"
        return result

    monkeypatch.setattr(cloud, "call", call)
    result = cloud.deployer().deploy()
    assert injected and result["status"] == "failed_maintenance"
    assert result["reason"] == "Viewer revision changed outside the deployment traffic pause"
    assert not any(op == "update_function_code" for _, op, _ in cloud.calls)


def test_conditional_code_update_still_rejects_race_after_final_revision_check(monkeypatch):
    cloud = Cloud()
    original_call, injected = cloud.call, False

    def call(service, operation, args):
        nonlocal injected
        if operation == "update_function_code" and not injected:
            injected = True
            cloud.config["RevisionId"] += "-external"
            cloud.config["CodeSha256"] = "external-code-digest"
        return original_call(service, operation, args)

    monkeypatch.setattr(cloud, "call", call)
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance"
    assert result["failure_type"] == "ClientError"
    assert result["failure_operation"] == "UpdateFunctionCode"
    assert result["failure_code"] == "PreconditionFailedException"
    assert cloud.config["CodeSha256"] == "external-code-digest"
    assert len([op for _, op, _ in cloud.calls if op == "update_function_code"]) == 1
    assert not any(op == "update_function_configuration" for _, op, _ in cloud.calls)
    assert cloud.concurrency == 0
    documents = json.dumps(result) + "\n".join(body.decode() for key, body in cloud.objects.items() if key.endswith(".json"))
    assert "test-only-private-sdk-message" not in documents


@pytest.mark.parametrize("drift", ["code", "configuration"])
def test_configuration_cutover_cannot_accept_concurrent_code_update(drift, monkeypatch):
    cloud = Cloud()
    original_call = cloud.call

    def call(service, operation, args):
        result = original_call(service, operation, args)
        if operation == "update_function_configuration":
            if drift == "code":
                cloud.config["CodeSha256"] = "external-code-digest"
            else:
                cloud.config["Role"] = f"arn:aws:iam::{ACCOUNT}:role/external-role"
        return result

    monkeypatch.setattr(cloud, "call", call)
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance"
    assert cloud.concurrency == 0
    if drift == "code":
        assert cloud.config["CodeSha256"] == "external-code-digest"
        assert "viewer_code_rollback_skipped_code_drift" in result["recovery_errors"]
    assert not any(op == "invoke" for _, op, _ in cloud.calls)


def test_failed_worker_does_not_roll_back_an_external_code_update(monkeypatch):
    cloud = Cloud()
    cloud.exits["verify"] = 1
    original_call = cloud.call

    def call(service, operation, args):
        result = original_call(service, operation, args)
        if operation == "run_task" and args["overrides"]["containerOverrides"][0]["command"][1] == "verify":
            cloud.config["CodeSha256"] = "external-code-digest"
            cloud.config["RevisionId"] += "-external"
        return result

    monkeypatch.setattr(cloud, "call", call)
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance" and cloud.concurrency == 0
    assert cloud.config["CodeSha256"] == "external-code-digest"
    assert "viewer_code_rollback_skipped_code_drift" in result["recovery_errors"]
    assert len([op for _, op, _ in cloud.calls if op == "update_function_code"]) == 1


@pytest.mark.parametrize("phase", ["code", "configuration"])
@pytest.mark.parametrize("field,value", [
    ("CodeSha256", "external-code-digest"),
    ("Environment", {"Variables": {"UNEXPECTED": "private-completion-drift-secret"}}),
    ("Role", f"arn:aws:iam::{ACCOUNT}:role/external-role"),
    ("Runtime", "python3.14"),
    ("Handler", "external.handler"),
    ("Architectures", ["arm64"]),
    ("VpcConfig", {"SubnetIds": ["subnet-external"]}),
    ("KMSKeyArn", f"arn:aws:kms:{REGION}:{ACCOUNT}:key/external-key"),
    ("MemorySize", 4096),
    ("Timeout", 600),
    ("Layers", [{"Arn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:layer:external:1"}]),
    ("FutureConfigurationField", {"unexpected": True}),
])
def test_async_completion_rejects_true_code_or_configuration_drift(phase, field, value, monkeypatch):
    cloud = Cloud()
    original_call, injected = cloud.call, False

    def call(service, operation, args):
        nonlocal injected
        result = original_call(service, operation, args)
        if (not injected and operation == "get_function" and cloud.lambda_update_evidence
                and cloud.lambda_update_evidence[-1]["kind"] == phase
                and result["Configuration"]["LastUpdateStatus"] == "Successful"):
            injected = True
            cloud.config[field] = copy.deepcopy(value)
            cloud.config["RevisionId"] += "-external"
            result["Configuration"] = copy.deepcopy(cloud.config)
        return result

    monkeypatch.setattr(cloud, "call", call)
    result = cloud.deployer().deploy()
    assert injected and result["status"] == "failed_maintenance" and cloud.concurrency == 0
    evidence = next(row for row in result["lambda_updates"] if row["phase"] == phase)
    assert evidence["result"] == ("mismatch_code" if field == "CodeSha256" else "mismatch_configuration")
    assert not any(op == "invoke" for _, op, _ in cloud.calls)
    if phase == "code":
        assert not any(op == "update_function_configuration" for _, op, _ in cloud.calls)
    assert "private-completion-drift-secret" not in json.dumps(result)


@pytest.mark.parametrize("phase", ["code", "configuration"])
def test_documented_server_output_transitions_are_verified_and_captured(phase, monkeypatch):
    cloud = Cloud()
    cloud.config.update(Runtime="python3.12", RuntimeVersionConfig={"RuntimeVersionArn": "arn:before"},
                        LastModified="before", ConfigSha256="before")
    original_call, injected = cloud.call, False

    def call(service, operation, args):
        nonlocal injected
        result = original_call(service, operation, args)
        if (not injected and operation == "get_function" and cloud.lambda_update_evidence
                and cloud.lambda_update_evidence[-1]["kind"] == phase
                and result["Configuration"]["LastUpdateStatus"] == "Successful"):
            injected = True
            cloud.config.update(RuntimeVersionConfig={"RuntimeVersionArn": "arn:after"},
                                LastModified="after", ConfigSha256="after")
            result["Configuration"] = copy.deepcopy(cloud.config)
        return result

    monkeypatch.setattr(cloud, "call", call)
    deployer = cloud.deployer()
    result = deployer.deploy()
    assert injected and result["status"] == "verified"
    assert all(row["result"] == "verified" and row["revision_transition"] for row in result["lambda_updates"])
    assert deployer.state["verified_viewer_configuration"]["RuntimeVersionConfig"] == {"RuntimeVersionArn": "arn:after"}


@pytest.mark.parametrize("phase", ["code", "configuration"])
@pytest.mark.parametrize("failure", ["waiter_failed", "waiter_timeout", "readback_in_progress", "readback_inactive"])
def test_async_completion_failure_never_advances_to_configuration_or_traffic(phase, failure, monkeypatch):
    cloud = Cloud()
    if failure.startswith("waiter_"):
        cloud.lambda_failure_modes[phase] = failure
    original_call = cloud.call

    def call(service, operation, args):
        result = original_call(service, operation, args)
        if cloud.lambda_update_evidence and cloud.lambda_update_evidence[-1]["kind"] == phase:
            if operation == "get_function_configuration" and failure.startswith("readback_"):
                cloud.config["LastUpdateStatus" if failure == "readback_in_progress" else "State"] = "InProgress" if failure == "readback_in_progress" else "Inactive"
                if failure == "readback_in_progress":
                    cloud.pending_lambda_update = {"kind": "external", "reads_remaining": 1}
                result = copy.deepcopy(cloud.config)
        return result

    monkeypatch.setattr(cloud, "call", call)
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance" and cloud.concurrency == 0
    evidence = next(row for row in result["lambda_updates"] if row["phase"] == phase)
    assert evidence["result"] == ("completion_read_or_wait_failed" if failure.startswith("waiter_") else "mismatch_status")
    assert not any(op == "invoke" for _, op, _ in cloud.calls)
    if phase == "code":
        assert not any(op == "update_function_configuration" for _, op, _ in cloud.calls)
    if failure in {"waiter_timeout", "readback_in_progress"}:
        assert cloud.pending_lambda_update is not None and cloud.config["LastUpdateStatus"] == "InProgress"
        assert "viewer_code_rollback_failed" in result["recovery_errors"]
        assert cloud.config["CodeSha256"] == base64.b64encode(hashlib.sha256(NEW_CODE).digest()).decode()
    assert "private-service-status-detail" not in json.dumps(result)


def test_rollback_completion_proves_original_code_with_distinct_completed_revision():
    cloud = Cloud()
    cloud.exits["verify"] = 1
    result = cloud.deployer().deploy()
    evidence = result["lambda_updates"][-1]
    assert result["status"] == "failed_maintenance" and cloud.concurrency == 0
    assert evidence["phase"] == "rollback" and evidence["result"] == "verified" and evidence["revision_transition"]
    assert cloud.config["CodeSha256"] == base64.b64encode(hashlib.sha256(OLD_CODE).digest()).decode()
    assert cloud.lambda_update_evidence[-1]["completed"]["RevisionId"] == cloud.config["RevisionId"]


@pytest.mark.parametrize("field,value", [("CodeSha256", "external-code-digest"), ("Role", "external-role")])
def test_rollback_completion_reports_wrong_artifact_or_config_instead_of_success(field, value, monkeypatch):
    cloud = Cloud()
    cloud.exits["verify"] = 1
    original_call = cloud.call

    def call(service, operation, args):
        result = original_call(service, operation, args)
        if (operation == "get_function" and cloud.lambda_update_evidence
                and cloud.lambda_update_evidence[-1]["kind"] == "rollback"
                and result["Configuration"]["LastUpdateStatus"] == "Successful"):
            cloud.config[field] = value
            result["Configuration"] = copy.deepcopy(cloud.config)
        return result

    monkeypatch.setattr(cloud, "call", call)
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance" and cloud.concurrency == 0
    assert "viewer_code_rollback_failed" in result["recovery_errors"]
    assert result["lambda_updates"][-1]["result"] == ("mismatch_code" if field == "CodeSha256" else "mismatch_configuration")


@pytest.mark.parametrize("field,value", [
    ("CodeSha256", "external-code-digest"),
    ("Environment", {"Variables": {"CHANGED_SECRET": "private-drift-secret"}}),
    ("Role", f"arn:aws:iam::{ACCOUNT}:role/external-viewer-role"),
    ("Handler", "app.other.handler"),
    ("Runtime", "python3.14"),
    ("VpcConfig", {"SubnetIds": ["subnet-external"]}),
    ("LastModified", "2026-09-15T13:00:00.000+0000"),
    ("FutureConfigurationField", {"changed": True}),
    ("RevisionId", "external-revision"),
])
def test_drift_during_worker_verification_blocks_traffic_resume(field, value, monkeypatch):
    cloud = Cloud()
    original_call = cloud.call

    def call(service, operation, args):
        result = original_call(service, operation, args)
        if operation == "run_task" and args["overrides"]["containerOverrides"][0]["command"][1] == "publish":
            cloud.config[field] = copy.deepcopy(value)
        return result

    monkeypatch.setattr(cloud, "call", call)
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance" and cloud.concurrency == 0
    assert result["reason"] == "Viewer code or configuration changed before traffic resume"
    assert not any(op == "invoke" or (op == "put_function_concurrency" and args["ReservedConcurrentExecutions"] > 0)
                   for _, op, args in cloud.calls)
    assert all(service["desiredCount"] == 0 for service in cloud.services.values())
    if field == "CodeSha256":
        assert cloud.config[field] == value
        assert "viewer_code_rollback_skipped_code_drift" in result["recovery_errors"]
    assert "private-drift-secret" not in json.dumps(result)


def test_cutover_snapshots_completed_service_fields_and_ignores_only_response_metadata(monkeypatch):
    cloud = Cloud()
    cloud.config.update(LastModified="before-cutover", ConfigSha256="before-env-change")
    original_call = cloud.call
    read_count = 0

    def call(service, operation, args):
        nonlocal read_count
        result = original_call(service, operation, args)
        if operation == "update_function_code":
            cloud.config["LastModified"] = "after-code-change"
        if operation == "update_function_configuration":
            cloud.config.update(LastModified="after-env-change", ConfigSha256="new-configuration-hash")
        if operation == "get_function_configuration":
            read_count += 1
            result["ResponseMetadata"] = {"RequestId": f"different-per-read-{read_count}"}
        return result

    monkeypatch.setattr(cloud, "call", call)
    deployer = cloud.deployer()
    assert deployer.deploy()["status"] == "verified"
    verified = deployer.state["verified_viewer_configuration"]
    assert verified["LastModified"] == "after-env-change" and verified["ConfigSha256"] == "new-configuration-hash"
    assert "ResponseMetadata" not in verified


def test_rollback_cas_rejects_code_change_after_ownership_check(monkeypatch):
    cloud = Cloud()
    cloud.exits["verify"] = 1
    original_call = cloud.call

    def call(service, operation, args):
        if operation == "update_function_code" and args["S3Key"].endswith("previous-viewer.zip"):
            cloud.config["CodeSha256"] = "external-code-digest"
            cloud.config["RevisionId"] += "-external"
        return original_call(service, operation, args)

    monkeypatch.setattr(cloud, "call", call)
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance" and cloud.concurrency == 0
    assert cloud.config["CodeSha256"] == "external-code-digest"
    assert "viewer_code_rollback_failed" in result["recovery_errors"]


@pytest.mark.parametrize("stage", ["bootstrap", "verify", "publish"])
def test_failed_worker_leaves_traffic_paused_and_legacy_fleets_stopped(stage):
    cloud = Cloud()
    cloud.exits[stage] = 1
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance" and result["recovery_required"]
    assert cloud.concurrency == 0
    assert all(s["desiredCount"] == 0 and s["taskDefinition"].endswith(":1") for s in cloud.services.values())
    assert all(s["SuspendedState"] == SUSPENDED and s["MinCapacity"] == 0 for s in cloud.scaling.values())
    assert all(key.startswith("deployments/l1/") for key in cloud.objects)
    assert cloud.config["CodeSha256"] == base64.b64encode(hashlib.sha256(OLD_CODE).digest()).decode()
    if stage == "bootstrap":
        assert not any(op == "update_function_code" for _, op, _ in cloud.calls)
    else:
        assert cloud.config["Environment"]["Variables"]["CLHEAR_RESTRICTED_ACCESS"] == "true"


def test_backup_and_result_artifacts_do_not_contain_plaintext_secrets_or_signed_urls():
    cloud = Cloud()
    result = cloud.deployer().deploy()
    documents = "\n".join(body.decode() for key, body in cloud.objects.items() if key.endswith(".json")) + json.dumps(result)
    for secret in ("test-secret-not-for-logs", "private-env-value", "private-oauth-value", "test-only-private-session", "test-signed-query"):
        assert secret not in documents
    backup = json.loads(next(value for key, value in cloud.objects.items() if key.endswith("rollback.json")))
    assert backup["fleets"]["l0"]["task_definition"]["containers"][0]["secret_references"][0]["valueFrom"] == DSN_SECRET


def test_bad_previous_code_hash_prevents_any_live_resource_change():
    cloud = Cloud()
    with pytest.raises(DeploymentError, match="Previous viewer code bytes"):
        cloud.deployer(code_fetch=lambda _: b"wrong prior code").deploy()
    assert not cloud.mutations


def test_old_tasks_that_do_not_stop_block_new_code_and_worker_execution():
    cloud = Cloud()
    cloud.old_tasks["l5"] = ["old-task"]
    cloud.fail_stop = True
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance"
    assert not any(op in {"register_task_definition", "run_task", "update_function_code"} for _, op, _ in cloud.calls)
    assert cloud.concurrency == 0


def test_failed_scaler_does_not_restore_legacy_definition_under_active_autoscaling():
    cloud = Cloud()
    cloud.fail_scaler = "l4"
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance" and "l4_code_rollback_failed" in result["recovery_errors"]
    assert not any(op == "update_service" and args["service"] == "clhear-fleet-l4" and "taskDefinition" in args for _, op, args in cloud.calls)
    assert cloud.concurrency == 0


def test_failed_anonymous_denial_rolls_back_while_preserving_private_configuration():
    cloud = Cloud()
    cloud.anonymous_status = 200
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance" and cloud.concurrency == 0
    assert cloud.config["Environment"]["Variables"]["CLHEAR_RESTRICTED_ACCESS"] == "true"
    assert all(s["desiredCount"] == 0 for s in cloud.services.values())


def test_unconfirmed_pause_never_restores_public_legacy_viewer_code():
    cloud = Cloud()
    cloud.anonymous_status, cloud.fail_pause_after_probe = 200, True
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance" and result["traffic_policy"] == "pause_unconfirmed"
    assert "viewer_code_rollback_skipped_pause_unconfirmed" in result["recovery_errors"]
    assert cloud.config["CodeSha256"] == base64.b64encode(hashlib.sha256(NEW_CODE).digest()).decode()
    assert not any(op == "update_function_code" and args["S3Key"].endswith("previous-viewer.zip") for _, op, args in cloud.calls)


def test_existing_schedule_upgrade_preserves_destinations_retries_dlq_and_cron():
    cloud = Cloud()
    original_targets, original_rules = copy.deepcopy(cloud.targets), copy.deepcopy(cloud.rules)
    result = cloud.deployer().deploy()
    assert result["status"] == "verified"
    assert result["schedule_targets"]["configuration"] == "configured"
    assert len(result["schedule_targets"]["confirmed_updated"]) == 32
    assert result["nightly_schedule_validation"] == "pending"
    for name, target in cloud.targets.items():
        original = original_targets[name]
        assert {key: value for key, value in target.items() if key != "InputTransformer"} == {key: value for key, value in original.items() if key != "Input"}
        assert "Input" not in target and "InputPath" not in target
        assert target["InputTransformer"]["InputPathsMap"] == {"event_id": "$.id", "event_time": "$.time"}
        assert Deployer._valid_transformer(name.removeprefix("clhear-adapter-"), target["InputTransformer"])
    assert cloud.rules == original_rules
    operations = [op for _, op, _ in cloud.calls]
    assert operations.index("put_function_concurrency") < operations.index("put_targets") < operations.index("run_task")
    backup = json.loads(next(value for key, value in cloud.objects.items() if key.endswith("rollback.json")))
    assert backup["adapter_schedules"][0]["previous_target"] == original_targets["clhear-adapter-uk_legislation"]


def test_already_current_transformers_do_not_receive_unnecessary_updates():
    cloud = Cloud()
    for adapter in ADAPTER_SCHEDULES:
        target = cloud.targets[f"clhear-adapter-{adapter}"]
        target.pop("Input")
        target["InputTransformer"] = Deployer._transformer(adapter)
    result = cloud.deployer().deploy()
    assert result["schedule_targets"]["configuration"] == "configured"
    assert result["schedule_targets"]["already_current"] == 32 and result["schedule_targets"]["attempted"] == []
    assert not any(op == "put_targets" for _, op, _ in cloud.calls)


@pytest.mark.parametrize("change", ["missing_rule", "wrong_adapter", "wrong_queue", "disabled_finra", "wrong_cron", "input_path", "missing_target", "static_transformer"])
def test_unknown_schedule_configurations_fail_preflight_without_mutations(change):
    cloud = Cloud()
    target = cloud.targets["clhear-adapter-finra"]
    if change == "missing_rule": cloud.rules.pop("clhear-adapter-finra")
    if change == "wrong_adapter": target["Input"] = target["Input"].replace('"adapter": "finra"', '"adapter": "esma"')
    if change == "wrong_queue": target["Arn"] = f"arn:aws:sqs:{REGION}:{ACCOUNT}:clhear-fleet-l2"
    if change == "disabled_finra": cloud.rules["clhear-adapter-finra"]["State"] = "DISABLED"
    if change == "wrong_cron": cloud.rules["clhear-adapter-finra"]["ScheduleExpression"] = "cron(0 12 * * ? *)"
    if change == "input_path": target["InputPath"] = "$.detail"
    if change == "missing_target": cloud.targets.pop("clhear-adapter-finra")
    if change == "static_transformer":
        target.pop("Input")
        target["InputTransformer"] = Deployer._transformer("finra")
        target["InputTransformer"]["InputPathsMap"]["event_time"] = "$.wrong"
    with pytest.raises(DeploymentError): cloud.deployer().deploy()
    assert not cloud.mutations


@pytest.mark.parametrize("failure", ["failed_entry", "inconsistent_failure_count", "readback"])
def test_partial_target_updates_never_claim_configured_or_resume_workers(failure):
    cloud = Cloud()
    cloud.event_failed_rule = "clhear-adapter-eur_lex"
    if failure == "inconsistent_failure_count": cloud.event_failure_response["FailedEntryCount"] = 0
    if failure == "readback":
        cloud.event_failed_rule = None
        cloud.event_bad_readback = True
    result = cloud.deployer().deploy()
    assert result["status"] == "failed_maintenance"
    assert result["schedule_targets"]["configuration"] == "partial_or_failed"
    assert result["nightly_schedule_validation"] == "pending"
    assert cloud.concurrency == 0 and all(s["desiredCount"] == 0 for s in cloud.services.values())
    assert not any(op in {"run_task", "register_task_definition"} for _, op, _ in cloud.calls)
    assert result["schedule_targets"]["confirmed_updated"] == ([] if failure == "readback" else ["clhear-adapter-uk_legislation"])


@pytest.mark.parametrize("url", ["http://s3.amazonaws.com/code", "https://evil.example/code", "https://s3.amazonaws.com.evil.test/code", "https://user:password@s3.amazonaws.com/code"])
def test_previous_code_download_rejects_non_aws_urls_without_network(url):
    with pytest.raises(DeploymentError): Deployer._download_code(url)
