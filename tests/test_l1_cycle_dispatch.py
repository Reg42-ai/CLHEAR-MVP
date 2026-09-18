"""Verify-only AWS controller checks; all cloud operations are mocked."""
import copy
import json

import pytest

from scripts.deploy_l1 import (ACCOUNT, REGION, CLUSTER, FLEETS, QUEUES, WORKER_ENTRYPOINT,
                              DeploymentError, VerificationDispatcher)
from tests.test_l1_deployment import Cloud, SHA, DIGEST, environment


WORKER_SHA = "c" * 40
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/clhear-workers@sha256:{DIGEST}"


class VerificationCloud(Cloud):
    def __init__(self):
        super().__init__()
        self.tags = [f"{WORKER_SHA}-123-1"]
        self.running = {}
        self.submit_exit = 0
        self.submit_failed = False
        self.submission = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/{CLUSTER}/submission"
        for name, target in self.targets.items():
            target.pop("Input")
            target["InputTransformer"] = VerificationDispatcher._transformer(name.removeprefix("clhear-adapter-"))
        for fleet, service in self.services.items():
            service.update(desiredCount=1, runningCount=1, pendingCount=0)
            service["deployments"] = [{"status": "PRIMARY", "rolloutState": "COMPLETED", "taskDefinition": service["taskDefinition"], "runningCount": 1, "desiredCount": 1, "pendingCount": 0}]
            worker = self.worker(fleet)
            worker["image"] = IMAGE
            values = {row["name"]: row["value"] for row in worker["environment"]}
            values.update(CLHEAR_L1_ONLY="true", CLHEAR_CODE_REVISION=WORKER_SHA,
                          CLHEAR_WORKER_IMAGE_DIGEST=f"sha256:{DIGEST}", CLHEAR_L1_CYCLE_CONTRACT="1",
                          CLHEAR_HTTP_MODE="live", CLHEAR_ARTIFACT_STORE="s3",
                          CLHEAR_VIEWER_SNAPSHOT_S3_URI="s3://fixture/webui/l1/candidate.db")
            worker["environment"] = [{"name": key, "value": value} for key, value in values.items()]
            self.running[fleet] = [{"taskArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/{CLUSTER}/{fleet}-running",
                "taskDefinitionArn": service["taskDefinition"], "lastStatus": "RUNNING", "desiredStatus": "RUNNING",
                "containers": [{"name": "worker", "image": IMAGE, "imageDigest": f"sha256:{DIGEST}", "lastStatus": "RUNNING"}]}]

    def worker(self, fleet):
        return self.definitions[self.services[fleet]["taskDefinition"]]["containerDefinitions"][0]

    def setting(self, fleet, name, value):
        worker = self.worker(fleet)
        worker["environment"] = [row for row in worker["environment"] if row["name"] != name]
        if value is not None:
            worker["environment"].append({"name": name, "value": value})

    def call(self, service, operation, args):
        if operation == "list_tasks":
            self.calls.append((service, operation, copy.deepcopy(args)))
            fleet = args["serviceName"].rsplit("-", 1)[-1]
            return {"taskArns": [task["taskArn"] for task in self.running[fleet]]}
        if operation == "describe_tasks":
            self.calls.append((service, operation, copy.deepcopy(args)))
            if args["tasks"] == [self.submission]:
                return {"tasks": [{"taskArn": self.submission, "taskDefinitionArn": self.services["l0"]["taskDefinition"],
                    "lastStatus": "STOPPED", "containers": [{"name": "worker", "exitCode": self.submit_exit,
                    "image": IMAGE, "imageDigest": f"sha256:{DIGEST}"}]}]}
            return {"tasks": [copy.deepcopy(task) for group in self.running.values() for task in group if task["taskArn"] in args["tasks"]]}
        if operation == "run_task":
            self.calls.append((service, operation, copy.deepcopy(args)))
            if self.submit_failed:
                return {"tasks": [], "failures": [{"reason": "fixture capacity unavailable"}]}
            command = args["overrides"]["containerOverrides"][0]["command"]
            effective = self.worker("l0")["entryPoint"] + command
            expected = list(WORKER_ENTRYPOINT) + list(getattr(self, "expected_arguments", ["--request-l1-cycle"])) + ["--verification-id", getattr(self, "operation_id", "l1-cycle-123-1")]
            if effective != expected:
                # ECS accepts the task, then Python exits when its module was lost.
                self.submit_exit = 1
            return {"tasks": [{"taskArn": self.submission}], "failures": []}
        return super().call(service, operation, args)

    def dispatcher(self, env=None):
        return VerificationDispatcher(SHA, "l1-cycle-123-1", clients=self.clients, environ=environment() if env is None else env)


def test_verify_only_runs_existing_l0_submission_and_reports_separate_identity():
    cloud = VerificationCloud()
    result = cloud.dispatcher().dispatch()
    assert result["status"] == "cycle_submitted"
    assert result["controller_sha"] == SHA and result["code_revision"] == WORKER_SHA
    assert result["worker_image_digest"] == f"sha256:{DIGEST}"
    assert result["configured_schedules"] == 32
    assert result["nightly_schedule_validation"] == "pending" and result["corpus_acceptance"] == "pending"
    assert result["deployment_performed"] is False and result["accepted_release_changed"] is False
    assert [(service, op) for service, op, _ in cloud.mutations] == [("ecs", "run_task")]
    run = cloud.mutations[0][2]
    assert run["taskDefinition"] == cloud.services["l0"]["taskDefinition"]
    assert run["networkConfiguration"] == cloud.services["l0"]["networkConfiguration"]
    assert run["clientToken"] == "l1-cycle-123-1"
    assert run["overrides"]["memory"] == "2048"
    assert set(run["overrides"]["containerOverrides"][0]) == {"name", "command", "memory"}
    assert run["overrides"]["containerOverrides"][0]["memory"] == 2048


@pytest.mark.parametrize("changes", [{"GITHUB_REF": "refs/heads/feature"}, {"GITHUB_REPOSITORY": "outside/repo"},
                                   {"GITHUB_SHA": "d" * 40}, {"CLHEAR_DEPLOY_ENVIRONMENT": "outside"}])
def test_context_rejected_before_aws_writes(changes):
    cloud = VerificationCloud()
    with pytest.raises(DeploymentError):
        cloud.dispatcher(environment(**changes)).dispatch()
    assert not cloud.mutations


@pytest.mark.parametrize("change", ["missing_lane", "disabled", "different_time", "legacy_target", "wrong_queue"])
def test_exact_scheduled_configuration_required_without_repairing_it(change):
    cloud = VerificationCloud()
    name = "clhear-adapter-cysec"
    if change == "missing_lane":
        del cloud.rules[name]
    elif change == "disabled":
        cloud.rules[name]["State"] = "DISABLED"
    elif change == "different_time":
        cloud.rules[name]["ScheduleExpression"] = "cron(30 0 * * ? *)"
    elif change == "legacy_target":
        cloud.targets[name].pop("InputTransformer")
        cloud.targets[name]["Input"] = json.dumps(VerificationDispatcher._schedule_payload("cysec", event_id="schedule-cysec", event_time=""))
    else:
        cloud.targets[name]["Arn"] = "arn:aws:sqs:us-east-1:730649732189/wrong"
    with pytest.raises(DeploymentError):
        cloud.dispatcher().dispatch()
    assert not cloud.mutations


@pytest.mark.parametrize("name,value", [("CLHEAR_L1_ONLY", "false"), ("CLHEAR_HTTP_MODE", "replay"),
    ("CLHEAR_ARTIFACT_STORE", "local"), ("CLHEAR_SNAPSHOT_S3_URI", "s3://fixture/legacy.db"),
    ("CLHEAR_L1_CYCLE_CONTRACT", None), ("CLHEAR_CODE_REVISION", "d" * 40),
    ("CLHEAR_WORKER_IMAGE_DIGEST", "sha256:" + "f" * 64), ("CLHEAR_EVENTS_QUEUE_URL", QUEUES["l0"])])
def test_current_worker_contract_must_match_before_submit(name, value):
    cloud = VerificationCloud()
    cloud.setting("l1", name, value)
    with pytest.raises(DeploymentError):
        cloud.dispatcher().dispatch()
    assert not cloud.mutations


@pytest.mark.parametrize("field,value", [("taskDefinitionArn", "old-definition"), ("imageDigest", "sha256:" + "f" * 64)])
def test_running_l1_tasks_must_actually_use_reviewed_definition_and_image(field, value):
    cloud = VerificationCloud()
    if field == "taskDefinitionArn":
        cloud.running["l1"][0][field] = value
    else:
        cloud.running["l1"][0]["containers"][0][field] = value
    with pytest.raises(DeploymentError):
        cloud.dispatcher().dispatch()
    assert not cloud.mutations


def test_split_entrypoint_keeps_module_when_command_is_overridden():
    cloud = VerificationCloud()
    cloud.worker("l0").update(entryPoint=["python"], command=["-m", "app.clhear.workers"])
    assert cloud.dispatcher().dispatch()["status"] == "cycle_submitted"
    assert cloud.mutations[0][2]["overrides"]["containerOverrides"][0]["command"][:2] == ["-m", "app.clhear.workers"]


@pytest.mark.parametrize("failure", ["launch", "exit"])
def test_submission_failure_never_deploys_or_changes_capacity(failure):
    cloud = VerificationCloud()
    if failure == "launch":
        cloud.submit_failed = True
    else:
        cloud.submit_exit = 1
    with pytest.raises(DeploymentError):
        cloud.dispatcher().dispatch()
    assert [(service, op) for service, op, _ in cloud.mutations] == [("ecs", "run_task")]


def test_recover_queues_runs_the_bounded_l0_recovery_pass_and_never_purges():
    cloud = VerificationCloud()
    cloud.expected_arguments = ["--recover-queues", "--max-messages", "2500"]
    cloud.operation_id = "l1-queues-123-1"
    dispatcher = VerificationDispatcher(SHA, "l1-queues-123-1", clients=cloud.clients, environ=environment(),
                                        operation="recover-queues", max_messages=2500)
    result = dispatcher.dispatch()
    assert result["status"] == "recovery_pass_completed" and result["operation"] == "recover-queues"
    assert result["recovery_id"] == "l1-queues-123-1" and result["queues_purged"] is False and result["resumable"] is True
    assert result["deployment_performed"] is False and result["accepted_release_changed"] is False
    assert [(service, op) for service, op, _ in cloud.mutations] == [("ecs", "run_task")]
    run = cloud.mutations[0][2]
    assert run["overrides"]["containerOverrides"][0]["command"][-5:] == ["--recover-queues", "--max-messages", "2500", "--verification-id", "l1-queues-123-1"]
    with pytest.raises(DeploymentError):
        VerificationDispatcher(SHA, "l1-cycle-123-1", clients=cloud.clients, environ=environment(), operation="recover-queues")
    with pytest.raises(DeploymentError):
        VerificationDispatcher(SHA, "l1-queues-123-1", clients=cloud.clients, environ=environment(), operation="recover-queues", max_messages=0)


def test_poc_private_review_dispatch_is_l0_only_and_grants_public_display():
    cloud = VerificationCloud()
    cloud.expected_arguments = ["--poc-private-review", "activate", "--evidence-ref", "poc:test"]
    cloud.operation_id = "l1-poc-123-1"
    dispatcher = VerificationDispatcher(SHA, "l1-poc-123-1", clients=cloud.clients, environ=environment(),
                                        operation="poc-private-review", poc_action="activate", evidence_ref="poc:test")
    result = dispatcher.dispatch()
    assert result["status"] == "poc_review_recorded" and result["operation"] == "poc-private-review"
    assert result["display_public"] is True and result["acceptance"] == "not_claimed"
    assert result["deployment_performed"] is False and result["accepted_release_changed"] is False
    run = cloud.mutations[0][2]
    assert run["overrides"]["containerOverrides"][0]["command"][-6:] == [
        "--poc-private-review", "activate", "--evidence-ref", "poc:test", "--verification-id", "l1-poc-123-1"]
    with pytest.raises(DeploymentError):
        VerificationDispatcher(SHA, "l1-cycle-123-1", clients=cloud.clients, environ=environment(),
                               operation="poc-private-review", poc_action="activate", evidence_ref="poc:test")
    with pytest.raises(DeploymentError):
        VerificationDispatcher(SHA, "l1-poc-123-1", clients=cloud.clients, environ=environment(),
                               operation="poc-private-review", poc_action="activate")


def test_approve_inventory_dispatch_requires_a_sha256():
    cloud = VerificationCloud()
    digest = "a" * 64
    cloud.expected_arguments = ["--approve-inventory", digest]
    cloud.operation_id = "l1-inventory-123-1"
    dispatcher = VerificationDispatcher(SHA, "l1-inventory-123-1", clients=cloud.clients, environ=environment(),
                                        operation="approve-inventory", inventory_hash=digest)
    result = dispatcher.dispatch()
    assert result["status"] == "inventory_review_recorded" and result["acceptance"] == "not_claimed"
    run = cloud.mutations[0][2]
    assert run["overrides"]["containerOverrides"][0]["command"][-4:] == [
        "--approve-inventory", digest, "--verification-id", "l1-inventory-123-1"]
    with pytest.raises(DeploymentError):
        VerificationDispatcher(SHA, "l1-inventory-123-1", clients=cloud.clients, environ=environment(),
                               operation="approve-inventory", inventory_hash="nope")
