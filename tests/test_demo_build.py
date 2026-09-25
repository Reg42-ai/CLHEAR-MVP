"""The demo build task reaches only the demo database and the demo release prefix."""
import pytest

from scripts import demo_build

SHA = "c" * 40
IMAGE = f"{demo_build.ACCOUNT}.dkr.ecr.{demo_build.REGION}.amazonaws.com/clhear-workers@sha256:{'d' * 64}"
BASE = {
    "family": "clhear-fleet-l2", "networkMode": "awsvpc", "requiresCompatibilities": ["FARGATE"], "cpu": "512",
    "memory": "1024", "taskRoleArn": "arn:aws:iam::730649732189:role/clhear-worker-task",
    "executionRoleArn": "arn:aws:iam::730649732189:role/clhear-worker-execution",
    "containerDefinitions": [{
        "name": "worker", "image": "old", "entryPoint": ["python", "-m", "app.clhear.workers"],
        "environment": [{"name": n, "value": v} for n, v in {
            "CLHEAR_EVENTS_QUEUE_URL": "https://sqs/prod-l0", "CLHEAR_FLEET_QUEUE_URLS": "{}",
            "CLHEAR_EVENT_BUS_NAME": "clhear", "CLHEAR_NEO4J_URI": "neo4j+s://prod",
            "CLHEAR_VIEWER_SNAPSHOT_S3_URI": "s3://clhear-deploy-730649732189/webui/l1/candidate.db",
            "CLHEAR_SNAPSHOT_S3_URI": "s3://prod/snapshot", "CLHEAR_L1_ONLY": "true", "CLHEAR_FLEET": "l2",
            "CLHEAR_RELEASES_S3_PREFIX": "s3://clhear-deploy-730649732189/releases", "CLHEAR_HTTP_MODE": "live",
            "CLHEAR_ARTIFACT_STORE": "s3", "CLHEAR_LLM_PROVIDER": "infer", "CLHEAR_PRIVATE_COMPLETENESS": "true",
        }.items()],
        "secrets": [{"name": n, "valueFrom": f"arn:aws:ssm:us-east-1:730649732189:parameter/clhear/{n}"}
                    for n in ("DATABASE_URL", "INFER_TOKEN", "CLHEAR_NEO4J_PASSWORD", "SENTRY_DSN")],
        "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": "/ecs/clhear-fleet-l2",
                                                                 "awslogs-region": "us-east-1", "awslogs-stream-prefix": "l2"}},
    }],
}


def test_the_demo_task_keeps_nothing_that_reaches_production_state():
    task = demo_build.task_definition(BASE, image=IMAGE, sha=SHA)
    build = task["containerDefinitions"][0]
    env = {e["name"]: e["value"] for e in build["environment"]}
    secrets = {s["name"]: s["valueFrom"] for s in build["secrets"]}
    assert not set(demo_build.PRODUCTION_ONLY) & set(env)
    assert env["CLHEAR_SOURCE_SCOPE"] == demo_build.SCOPE and env["CLHEAR_L1_ONLY"] == "false"
    assert env["CLHEAR_RELEASES_S3_PREFIX"] == demo_build.RELEASES and env["CLHEAR_HTTP_MODE"] == "live"
    assert secrets["DATABASE_URL"].endswith("parameter/clhear/demo/DATABASE_URL")
    assert "CLHEAR_NEO4J_PASSWORD" not in secrets and secrets["INFER_TOKEN"].endswith("/clhear/INFER_TOKEN")
    assert build["image"] == IMAGE and build["entryPoint"] == ["python", "-m", "app.clhear.scope_build"]
    assert build["logConfiguration"]["options"]["awslogs-stream-prefix"] == "demo-build"
    assert task["family"] == demo_build.FAMILY and task["taskRoleArn"] == BASE["taskRoleArn"]


@pytest.mark.parametrize("image,sha", [("latest", SHA), (IMAGE, "abc")])
def test_only_a_pinned_image_and_commit_are_accepted(image, sha):
    with pytest.raises(ValueError):
        demo_build.task_definition(BASE, image=image, sha=sha)


def test_a_first_pass_can_build_without_a_profile_or_a_release():
    started = {}

    class Ecs:
        def describe_services(self, cluster, services):
            return {"services": [{"networkConfiguration": {"awsvpcConfiguration": {"subnets": ["s"]}}}]}

        def run_task(self, **kwargs):
            started.update(kwargs)
            return {"tasks": [{"taskArn": "arn:task/1"}]}

    report = demo_build.run({"ecs": Ecs()}, layers="L1,L2,L3,L4", profile=None, publish=False, wait=False)
    assert report["command"] == ["--scope", demo_build.SCOPE, "--layers", "L1,L2,L3,L4"]
    assert started["overrides"]["containerOverrides"][0]["command"] == report["command"]
    assert started["taskDefinition"] == demo_build.FAMILY


def test_the_production_task_uses_the_production_database_and_asks_for_a_viewer_refresh():
    task = demo_build.task_definition(BASE, image=IMAGE, sha=SHA, target="production")
    build = task["containerDefinitions"][0]
    env = {e["name"]: e["value"] for e in build["environment"]}
    secrets = {s["name"]: s["valueFrom"] for s in build["secrets"]}
    assert "CLHEAR_EVENTS_QUEUE_URL" not in env and "CLHEAR_NEO4J_URI" not in env
    assert "CLHEAR_SNAPSHOT_S3_URI" not in env
    assert env["CLHEAR_VIEWER_SNAPSHOT_S3_URI"].endswith("webui/l1/candidate.db")
    assert secrets["DATABASE_URL"] == demo_build.PRODUCTION_DATABASE
    assert "--refresh-viewer" in build["command"]
    assert task["family"] == demo_build.PRODUCTION_FAMILY
    started = {}

    class Ecs:
        def describe_services(self, cluster, services):
            return {"services": [{"networkConfiguration": {"awsvpcConfiguration": {"subnets": ["s"]}}}]}

        def run_task(self, **kwargs):
            started.update(kwargs)
            return {"tasks": [{"taskArn": "arn:task/prod"}]}

    report = demo_build.run({"ecs": Ecs()}, target="production", skip_import=True, wait=False)
    assert started["taskDefinition"] == demo_build.PRODUCTION_FAMILY
    assert report["command"][-1] == "--refresh-viewer"
