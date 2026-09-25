"""Build the compliance-program demo corpus in its own database, as a one-off ECS task.

    python scripts/demo_build.py register --image <repo@sha256:...> --sha <40-hex>
    python scripts/demo_build.py run [--skip-import] [--layers L2,L3,...] [--profile s3://...]

The task runs ``python -m app.clhear.scope_build`` with the production worker
image and roles, against the demo database (/clhear/demo/DATABASE_URL). It
keeps nothing that reaches production state: no fleet queues, event bus,
graph, community or newsletter endpoints, and no viewer or snapshot object.
Releases go to the demo prefix. No secret value is printed.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys

ACCOUNT = "730649732189"
REGION = "us-east-1"
CLUSTER = "clhear-cluster"
BASE_SERVICE = "clhear-fleet-l2"
FAMILY = "clhear-demo-build"
SCOPE = "compliance-program-demo"
RELEASES = f"s3://clhear-deploy-{ACCOUNT}/releases/demo"
PROFILE = f"{RELEASES}/profiles/galaxy.json"
DATABASE_PARAMETER = f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/clhear/demo/DATABASE_URL"
IMAGE_RE = re.compile(rf"^{ACCOUNT}\.dkr\.ecr\.{REGION}\.amazonaws\.com/clhear-workers@sha256:[0-9a-f]{{64}}$")
# Worker settings that name production queues, buses, graphs, snapshots or services.
PRODUCTION_ONLY = ("CLHEAR_EVENTS_QUEUE_URL", "CLHEAR_EVENTS_DLQ_URL", "CLHEAR_FLEET_QUEUE_URLS",
                   "CLHEAR_EVENT_BUS_NAME", "CLHEAR_NEO4J_URI", "CLHEAR_NEO4J_USER", "CLHEAR_SNAPSHOT_S3_URI",
                   "CLHEAR_VIEWER_SNAPSHOT_S3_URI", "CLHEAR_DISCOURSE_URL", "CLHEAR_PRIVATE_COMPLETENESS",
                   "CLHEAR_L1_CYCLE_CONTRACT", "CLHEAR_FLEET")
PRODUCTION_SECRETS = ("DATABASE_URL", "CLHEAR_NEO4J_PASSWORD", "CLHEAR_BEEHIIV_API_KEY", "CLHEAR_BEEHIIV_PUBLICATION_ID")
TASK_FIELDS = ("taskRoleArn", "executionRoleArn", "networkMode", "requiresCompatibilities", "runtimePlatform")


def task_definition(base: dict, *, image: str, sha: str) -> dict:
    if not IMAGE_RE.match(image or ""):
        raise ValueError("image must be a clhear-workers digest reference")
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise ValueError("sha must be a 40-character commit id")
    worker = copy.deepcopy(base["containerDefinitions"][0])
    env = {e["name"]: e["value"] for e in worker.get("environment", []) if e["name"] not in PRODUCTION_ONLY}
    env.update(CLHEAR_SOURCE_SCOPE=SCOPE, CLHEAR_L1_ONLY="false", CLHEAR_RELEASES_S3_PREFIX=RELEASES,
               CLHEAR_CODE_REVISION=sha, CLHEAR_WORKER_IMAGE_DIGEST=image.split("@", 1)[1])
    secrets = [s for s in worker.get("secrets", []) if s["name"] not in PRODUCTION_SECRETS]
    secrets.append({"name": "DATABASE_URL", "valueFrom": DATABASE_PARAMETER})
    log = copy.deepcopy(worker["logConfiguration"])
    log["options"]["awslogs-stream-prefix"] = "demo-build"
    container = {"name": "build", "image": image, "essential": True,
                 "entryPoint": ["python", "-m", "app.clhear.scope_build"],
                 "command": ["--scope", SCOPE, "--profile", PROFILE, "--publish-release"],
                 "environment": [{"name": k, "value": v} for k, v in sorted(env.items())],
                 "secrets": sorted(secrets, key=lambda s: s["name"]), "logConfiguration": log}
    task = {k: copy.deepcopy(base[k]) for k in TASK_FIELDS if k in base}
    task.update(family=FAMILY, cpu="2048", memory="8192", containerDefinitions=[container],
                tags=[{"key": "clhear:git-sha", "value": sha}, {"key": "clhear:purpose", "value": "demo-build"}])
    return task


def _clients():
    import boto3

    return {name: boto3.client(name, region_name=REGION) for name in ("ecs", "logs")}


def register(clients, *, image: str, sha: str) -> dict:
    service = clients["ecs"].describe_services(cluster=CLUSTER, services=[BASE_SERVICE])["services"][0]
    base = clients["ecs"].describe_task_definition(taskDefinition=service["taskDefinition"])["taskDefinition"]
    arn = clients["ecs"].register_task_definition(**task_definition(base, image=image, sha=sha))["taskDefinition"]["taskDefinitionArn"]
    return {"task_definition": arn}


def run(clients, *, skip_import: bool = False, layers: str = "", profile: str = PROFILE, wait: bool = True) -> dict:
    service = clients["ecs"].describe_services(cluster=CLUSTER, services=[BASE_SERVICE])["services"][0]
    command = ["--scope", SCOPE, "--profile", profile, "--publish-release"]
    if skip_import:
        command.append("--skip-import")
    if layers:
        command += ["--layers", layers]
    task = clients["ecs"].run_task(cluster=CLUSTER, taskDefinition=FAMILY, launchType="FARGATE", count=1,
                                   networkConfiguration=service["networkConfiguration"], startedBy="demo-build",
                                   overrides={"containerOverrides": [{"name": "build", "command": command}]})["tasks"][0]
    report = {"task": task["taskArn"], "command": command}
    if wait:
        clients["ecs"].get_waiter("tasks_stopped").wait(cluster=CLUSTER, tasks=[task["taskArn"]],
                                                         WaiterConfig={"Delay": 30, "MaxAttempts": 480})
        stopped = clients["ecs"].describe_tasks(cluster=CLUSTER, tasks=[task["taskArn"]])["tasks"][0]
        report.update(exit_code=stopped["containers"][0].get("exitCode"), reason=stopped.get("stoppedReason"))
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("register")
    r.add_argument("--image", required=True)
    r.add_argument("--sha", required=True)
    b = sub.add_parser("run")
    b.add_argument("--skip-import", action="store_true")
    b.add_argument("--layers", default="")
    b.add_argument("--profile", default=PROFILE)
    b.add_argument("--no-wait", action="store_true")
    args = parser.parse_args(argv)
    clients = _clients()
    if args.command == "register":
        result = register(clients, image=args.image, sha=args.sha)
    else:
        result = run(clients, skip_import=args.skip_import, layers=args.layers, profile=args.profile,
                     wait=not args.no_wait)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("exit_code", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
