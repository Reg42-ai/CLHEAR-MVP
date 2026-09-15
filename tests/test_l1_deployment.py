"""AWS control-plane deployment invariants; every AWS call is mocked.

These tests neither contact AWS nor open a corpus database. Worker exit codes
stand for the separate worker verification tests, not for a successful import.
"""
import base64
import copy
import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from scripts.deploy_l1 import (ACCOUNT, ADAPTER_SCHEDULES, BUCKET, CLUSTER, ENVIRONMENT, FLEETS, FUNCTION,
                              QUEUES, REGION, SUSPENDED, WORKFLOW, Deployer, DeploymentError, Inputs)

SHA = "a" * 40
DIGEST = "b" * 64
ROLE = f"arn:aws:iam::{ACCOUNT}:role/clhear-l1-deployment"
DSN_SECRET = f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/clhear/DATABASE_URL"
ENDPOINT = f"clhear-record.cluster-abcdef.{REGION}.rds.amazonaws.com"
NEW_CODE, OLD_CODE = b"new test-only code artifact", b"previous test-only code artifact"
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
                "MaxCapacity": 2, "SuspendedState": {key: False for key in SUSPENDED}}
        self.config = {"FunctionName": FUNCTION, "State": "Active", "LastUpdateStatus": "Successful", "PackageType": "Zip",
            "Handler": "app.clhear.lambda_web.handler", "Role": f"arn:aws:iam::{ACCOUNT}:role/clhear-webui",
            "RevisionId": "old-revision", "CodeSha256": base64.b64encode(hashlib.sha256(OLD_CODE).digest()).decode(),
            "Environment": {"Variables": {"CLHEAR_SESSION_SECRET": "test-only-private-session-value-at-least-32-chars",
                "GOOGLE_OAUTH_CLIENT_SECRET": "private-oauth-value", "CLHEAR_SES_SENDER": "review@example.test",
                "CLHEAR_RESTRICTED_ACCESS": "false", "CLHEAR_AUTH_DEBUG": "true", "CLHEAR_DB_S3_URI": f"s3://{BUCKET}/webui/legacy.db"}}}
        self.concurrency = None
        self.clients = {name: Client(self, name) for name in
                        ("sts", "s3", "ecr", "rds", "ssm", "secretsmanager", "ecs", "lambda", "application-autoscaling", "sqs", "iam", "events")}

    def call(self, service, operation, args):
        self.calls.append((service, operation, copy.deepcopy(args)))
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
            return {"DBClusters": [{"DBClusterIdentifier": "clhear-record", "Status": "available", "Engine": "aurora-postgresql", "Endpoint": ENDPOINT}]}
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
            if self.fail_pause_after_probe and self.probe_failed and args["ReservedConcurrentExecutions"] == 0:
                raise RuntimeError("test-only pause failure")
            self.concurrency = args["ReservedConcurrentExecutions"]
            return {}
        if operation == "delete_function_concurrency":
            self.concurrency = None
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
            assert args["RevisionId"] == self.config["RevisionId"]
            code = OLD_CODE if args["S3Key"].endswith("previous-viewer.zip") else NEW_CODE
            self.config["CodeSha256"] = base64.b64encode(hashlib.sha256(code).digest()).decode()
            self.config["RevisionId"] += "-code"
            return copy.deepcopy(self.config)
        if operation == "update_function_configuration":
            assert args["RevisionId"] == self.config["RevisionId"]
            self.config["Environment"] = copy.deepcopy(args["Environment"])
            self.config["RevisionId"] += "-config"
            return copy.deepcopy(self.config)
        if operation == "invoke":
            path = json.loads(args["Payload"])["rawPath"]
            if not path.endswith("health"):
                self.probe_failed = self.anonymous_status != 401
            result = {"statusCode": 200 if path.endswith("health") else self.anonymous_status, "body": "{}"}
            return {"Payload": io.BytesIO(json.dumps(result).encode())}
        raise AssertionError(f"Unexpected mocked AWS operation {service}.{operation}")

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
    assert cloud.services["l0"]["desiredCount"] == cloud.scaling["l0"]["MinCapacity"] == 1
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
