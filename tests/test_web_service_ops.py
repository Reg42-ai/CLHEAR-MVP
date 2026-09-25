"""The served web task mirrors the verified viewer, with credentials by SSM reference."""
import pytest
from botocore.exceptions import ClientError

from scripts import deploy_l1, web_service

SHA = "a" * 40
IMAGE = f"{web_service.ACCOUNT}.dkr.ecr.{web_service.REGION}.amazonaws.com/clhear-workers@sha256:{'b' * 64}"
BASE = {
    "family": "clhear-webui-service", "networkMode": "awsvpc", "cpu": "2048", "memory": "8192",
    "requiresCompatibilities": ["FARGATE"], "ephemeralStorage": {"sizeInGiB": 50},
    "taskRoleArn": "arn:aws:iam::730649732189:role/clhear-webui-service-task",
    "executionRoleArn": "arn:aws:iam::730649732189:role/clhear-worker-execution",
    "containerDefinitions": [{"name": "web", "image": "old", "portMappings": [{"containerPort": 8080}],
                              "environment": [{"name": "AWS_REGION", "value": "us-east-1"}]}],
    "revision": 7, "status": "ACTIVE", "taskDefinitionArn": "arn:old",
    "tags": [{"key": "clhear:git-sha", "value": "0" * 40}, {"key": "owner", "value": "clhear"}],
}
LAMBDA_ENV = {
    "CLHEAR_RESTRICTED_ACCESS": "true", "CLHEAR_DB_S3_URI": "s3://bucket/webui/l1/candidate.db",
    "CLHEAR_APP_KEYS": "private-app-keys", "CLHEAR_SESSION_SECRET": "private-session-secret",
    "GOOGLE_OAUTH_CLIENT_SECRET": "", "SENTRY_DSN": "private-dsn", "CLHEAR_REVIEWER_EMAILS": "r@example.test",
}


def test_task_definition_moves_credentials_to_ssm_references_and_pins_the_image():
    task = web_service.task_definition(BASE, LAMBDA_ENV, image=IMAGE, sha=SHA)
    web = task["containerDefinitions"][0]
    env = {e["name"]: e["value"] for e in web["environment"]}
    assert web["image"] == IMAGE
    assert env["CLHEAR_DB_S3_URI"] == LAMBDA_ENV["CLHEAR_DB_S3_URI"] and env["CLHEAR_RESTRICTED_ACCESS"] == "true"
    assert env["CLHEAR_CODE_REVISION"] == SHA and env["CLHEAR_DB_LOCAL_PATH"] == "/tmp/clhear.db"
    assert not set(web_service.SECRET_ENV) & set(env)
    assert all("private" not in value for value in env.values())
    assert {s["name"] for s in web["secrets"]} == {"CLHEAR_APP_KEYS", "CLHEAR_SESSION_SECRET", "SENTRY_DSN",
                                                   "CLHEAR_IDENTITY_DATABASE_URL"}
    assert all(s["valueFrom"].startswith("arn:aws:ssm:us-east-1:730649732189:parameter/clhear/web/") for s in web["secrets"])
    assert {"revision", "status", "taskDefinitionArn"}.isdisjoint(task)
    assert {"key": "clhear:git-sha", "value": SHA} in task["tags"] and {"key": "owner", "value": "clhear"} in task["tags"]


def test_app_keys_come_from_ssm_even_when_the_lambda_no_longer_carries_them():
    lambda_env = {k: v for k, v in LAMBDA_ENV.items() if k != "CLHEAR_APP_KEYS"}
    web = web_service.task_definition(BASE, lambda_env, image=IMAGE, sha=SHA)["containerDefinitions"][0]
    assert {"name": "CLHEAR_APP_KEYS", "valueFrom": web_service.secret_arn("CLHEAR_APP_KEYS")} in web["secrets"]


def test_secret_sync_never_overwrites_rotated_app_keys_with_the_lambda_copy():
    written = {}

    class Lambda:
        def get_function_configuration(self, FunctionName):
            return {"Environment": {"Variables": LAMBDA_ENV}}

    class Ssm:
        def put_parameter(self, Name, Value, **_):
            written[Name] = Value

    report = web_service.sync_secrets({"lambda": Lambda(), "ssm": Ssm()})
    assert report["CLHEAR_APP_KEYS"] == "ssm_owned"
    assert web_service.secret_parameter("CLHEAR_APP_KEYS") not in written
    assert written[web_service.secret_parameter("CLHEAR_SESSION_SECRET")] == LAMBDA_ENV["CLHEAR_SESSION_SECRET"]


@pytest.mark.parametrize("image,sha", [("legacy:latest", SHA), (IMAGE, "main"), ("", SHA)])
def test_task_definition_rejects_unpinned_code(image, sha):
    with pytest.raises(ValueError):
        web_service.task_definition(BASE, LAMBDA_ENV, image=image, sha=sha)


class DeniedEcs:
    def describe_services(self, **kw):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "DescribeServices")


def _deployer(clients):
    deployer = deploy_l1.Deployer.__new__(deploy_l1.Deployer)
    deployer.clients = clients
    deployer.inputs = type("Inputs", (), {"image": IMAGE, "sha": SHA})()
    return deployer


def test_deploy_records_a_web_roll_it_is_not_permitted_to_make():
    receipt = _deployer({"ecs": DeniedEcs(), "lambda": object()})._roll_web_service()
    assert receipt == {"service": "clhear-webui-service", "status": "not_permitted", "error_code": "AccessDeniedException"}


def test_deploy_requests_the_roll_without_waiting(monkeypatch):
    calls = []

    def roll(clients, **kw):
        calls.append(kw)
        return {"task_definition": "arn:new", "desired": 2, "rollout": "requested"}

    monkeypatch.setattr(web_service, "roll", roll)
    receipt = _deployer({"ecs": object(), "lambda": object()})._roll_web_service()
    assert receipt["status"] == "roll_requested" and receipt["task_definition"] == "arn:new"
    assert calls == [{"image": IMAGE, "sha": SHA, "wait": False}]


def test_service_owned_settings_and_the_edge_requirement_survive_a_roll():
    first = web_service.task_definition(BASE, LAMBDA_ENV, image=IMAGE, sha=SHA,
                                        overrides={"CLHEAR_ACCESS_MODE": "accounts"}, require_edge=True)
    second = web_service.task_definition(first, {**LAMBDA_ENV, "CLHEAR_ACCESS_MODE": "reviewers"}, image=IMAGE, sha="b" * 40)
    env = {e["name"]: e["value"] for e in second["containerDefinitions"][0]["environment"]}
    assert env["CLHEAR_ACCESS_MODE"] == "accounts"
    assert "CLHEAR_ORIGIN_VERIFY_SECRET" in {s["name"] for s in second["containerDefinitions"][0]["secrets"]}
    third = web_service.task_definition(second, LAMBDA_ENV, image=IMAGE, sha=SHA, require_edge=False)
    assert "CLHEAR_ORIGIN_VERIFY_SECRET" not in {s["name"] for s in third["containerDefinitions"][0]["secrets"]}


class _RollingEcs:
    """Answers the way ECS does right after UpdateService: the old deployment first."""

    def __init__(self, states):
        self.states, self.calls = list(states), 0

    def describe_services(self, cluster, services):
        state = self.states[min(self.calls, len(self.states) - 1)]
        self.calls += 1
        return {"services": [{"taskDefinition": "arn:old", "desiredCount": 1, "runningCount": 1,
                              "deployments": state}]}

    def describe_task_definition(self, taskDefinition, include=None):
        return {"taskDefinition": {**BASE, "taskDefinitionArn": "arn:old"}, "tags": BASE["tags"]}

    def register_task_definition(self, **task):
        return {"taskDefinition": {"taskDefinitionArn": "arn:new"}}

    def update_service(self, **kwargs):
        pass


class _Lambda:
    def get_function_configuration(self, FunctionName):
        return {"Environment": {"Variables": LAMBDA_ENV}}


OLD = {"taskDefinition": "arn:old", "status": "PRIMARY", "rolloutState": "COMPLETED", "runningCount": 1}


@pytest.mark.parametrize("final,expected", [
    ({"taskDefinition": "arn:new", "status": "PRIMARY", "rolloutState": "COMPLETED", "runningCount": 1}, "completed"),
    ({"taskDefinition": "arn:new", "status": "ACTIVE", "rolloutState": "FAILED", "runningCount": 0}, None),
])
def test_a_roll_follows_its_own_deployment_not_the_one_before_it(final, expected):
    rolling = {"taskDefinition": "arn:new", "status": "PRIMARY", "rolloutState": "IN_PROGRESS", "runningCount": 0}
    ecs = _RollingEcs([[OLD], [rolling, {**OLD, "status": "ACTIVE"}], [final]])
    clients = {"ecs": ecs, "lambda": _Lambda()}
    if expected is None:
        with pytest.raises(RuntimeError):
            web_service.roll(clients, image=IMAGE, sha=SHA, desired=1, sleep=lambda s: None)
    else:
        assert web_service.roll(clients, image=IMAGE, sha=SHA, desired=1, sleep=lambda s: None)["rollout"] == expected
    assert ecs.calls >= 3
