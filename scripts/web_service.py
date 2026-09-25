"""Operate the long-lived CLHEAR web service (infra/webui_service.tf).

    python scripts/web_service.py sync-secrets
    python scripts/web_service.py roll --image <repo@sha256:...> --sha <40-hex> [--desired 2]
    python scripts/web_service.py cutover --to service|lambda

The served task definition mirrors the verified clhear-webui Lambda
configuration: the same environment, except that secret values move to SSM
SecureString parameters and are referenced by ARN. No secret value is printed.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import time
import urllib.error
import urllib.request

ACCOUNT = "730649732189"
REGION = "us-east-1"
CLUSTER = "clhear-cluster"
FUNCTION = "clhear-webui"
SERVICE = "clhear-webui-service"
FAMILY = "clhear-webui-service"
API_NAME = "clhear-webui"
PUBLIC_URL = "https://clhear.reg42.ai"
SECRET_PREFIX = "/clhear/web/"
# Lambda environment names whose values are credentials.
SECRET_ENV = ("CLHEAR_APP_KEYS", "CLHEAR_SESSION_SECRET", "GOOGLE_OAUTH_CLIENT_SECRET",
              "CLHEAR_BEEHIIV_API_KEY", "SENTRY_DSN")
# Kept only in SSM: the Lambda hydrates them at cold start and the service
# references them, so a rotation is an SSM write plus a roll.
SSM_OWNED = ("CLHEAR_APP_KEYS",)
IDENTITY_ENV = "CLHEAR_IDENTITY_DATABASE_URL"
ORIGIN_ENV = "CLHEAR_ORIGIN_VERIFY_SECRET"
# Settings the service owns (not copied from the Lambda); a roll keeps them.
SERVICE_OWNED = ("CLHEAR_ACCESS_MODE", "CLHEAR_CORS_ORIGINS", "CLHEAR_RELEASE_DB_S3_URI", "CLHEAR_RELEASES_S3_PREFIX",
                 "CLHEAR_PRIVATE_COMPLETENESS")
# The Lambda keeps its snapshot in /tmp; the service keeps it on ephemeral storage.
SERVICE_ENV = {"CLHEAR_DB_LOCAL_PATH": "/tmp/clhear.db", "CLHEAR_SNAPSHOT_POLL_S": "60", "PORT": "8080"}
TASK_FIELDS = {
    "family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions", "volumes",
    "placementConstraints", "requiresCompatibilities", "cpu", "memory", "tags", "pidMode", "ipcMode",
    "proxyConfiguration", "ephemeralStorage", "runtimePlatform",
}
IMAGE_RE = re.compile(rf"^{ACCOUNT}\.dkr\.ecr\.{REGION}\.amazonaws\.com/clhear-workers@sha256:[0-9a-f]{{64}}$")


def secret_parameter(name: str) -> str:
    return SECRET_PREFIX + name


def secret_arn(name: str) -> str:
    return f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter{secret_parameter(name)}"


def task_definition(base: dict, lambda_env: dict, *, image: str, sha: str, overrides: dict | None = None,
                    require_edge: bool | None = None) -> dict:
    """The next served revision: base task shape, Lambda environment, new code.

    Service-owned settings and the edge requirement carry over from ``base``
    unless ``overrides`` / ``require_edge`` change them."""
    if not IMAGE_RE.match(image or ""):
        raise ValueError("image must be a clhear-workers digest reference")
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise ValueError("sha must be a 40-character commit id")
    task = {k: copy.deepcopy(v) for k, v in base.items() if k in TASK_FIELDS}
    web = next(c for c in task["containerDefinitions"] if c["name"] == "web")
    previous = {e["name"]: e["value"] for e in web.get("environment", [])}
    had_edge = any(item["name"] == ORIGIN_ENV for item in web.get("secrets", []))
    web["image"] = image
    env = {k: v for k, v in lambda_env.items() if k not in SECRET_ENV}
    env.update(SERVICE_ENV, AWS_REGION=REGION, CLHEAR_CODE_REVISION=sha,
               CLHEAR_WORKER_IMAGE_DIGEST=image.split("@", 1)[1])
    env.update({k: previous[k] for k in SERVICE_OWNED if k in previous})
    env.update({k: v for k, v in (overrides or {}).items() if k in SERVICE_OWNED})
    web["environment"] = [{"name": k, "value": v} for k, v in sorted(env.items())]
    web["secrets"] = [{"name": name, "valueFrom": secret_arn(name)} for name in SECRET_ENV
                      if lambda_env.get(name) or name in SSM_OWNED]
    # The service, unlike the Lambda, writes accounts and keys to Aurora as clhear_web.
    web["secrets"].append({"name": IDENTITY_ENV, "valueFrom": secret_arn(IDENTITY_ENV)})
    if had_edge if require_edge is None else require_edge:
        web["secrets"].append({"name": ORIGIN_ENV, "valueFrom": secret_arn(ORIGIN_ENV)})
    task["tags"] = [t for t in task.get("tags", []) if t.get("key") != "clhear:git-sha"] + [{"key": "clhear:git-sha", "value": sha}]
    return task


def _clients():
    import boto3

    return {name: boto3.client(name, region_name=REGION) for name in ("ecs", "lambda", "ssm", "apigatewayv2")}


def lambda_environment(clients) -> dict:
    config = clients["lambda"].get_function_configuration(FunctionName=FUNCTION)
    return dict((config.get("Environment") or {}).get("Variables") or {})


def sync_secrets(clients) -> dict:
    """Copy the Lambda's credential values into SSM without printing them."""
    env, report = lambda_environment(clients), {}
    for name in SECRET_ENV:
        if name in SSM_OWNED:
            report[name] = "ssm_owned"
            continue
        value = env.get(name, "")
        if not value:
            report[name] = "unset"
            continue
        clients["ssm"].put_parameter(Name=secret_parameter(name), Value=value, Type="SecureString", Overwrite=True,
                                     Description=f"CLHEAR web service {name}")
        report[name] = "stored"
    return report


def current_task_definition(clients) -> dict:
    service = clients["ecs"].describe_services(cluster=CLUSTER, services=[SERVICE])["services"][0]
    return clients["ecs"].describe_task_definition(taskDefinition=service["taskDefinition"], include=["TAGS"])


def roll(clients, *, image: str, sha: str, desired: int | None = None, wait: bool = True,
         overrides: dict | None = None, require_edge: bool | None = None) -> dict:
    described = current_task_definition(clients)
    base = {**described["taskDefinition"], "tags": described.get("tags", [])}
    task = task_definition(base, lambda_environment(clients), image=image, sha=sha, overrides=overrides,
                           require_edge=require_edge)
    arn = clients["ecs"].register_task_definition(**task)["taskDefinition"]["taskDefinitionArn"]
    service = clients["ecs"].describe_services(cluster=CLUSTER, services=[SERVICE])["services"][0]
    count = service["desiredCount"] if desired is None else desired
    clients["ecs"].update_service(cluster=CLUSTER, service=SERVICE, taskDefinition=arn, desiredCount=count)
    evidence = {"task_definition": arn, "desired": count, "rollout": "requested"}
    if wait and count:
        clients["ecs"].get_waiter("services_stable").wait(cluster=CLUSTER, services=[SERVICE],
                                                          WaiterConfig={"Delay": 20, "MaxAttempts": 75})
        service = clients["ecs"].describe_services(cluster=CLUSTER, services=[SERVICE])["services"][0]
        primary = [d for d in service["deployments"] if d["status"] == "PRIMARY"]
        healthy = (service["taskDefinition"] == arn and service["runningCount"] == count and len(primary) == 1
                   and primary[0].get("rolloutState") == "COMPLETED")
        evidence.update(rollout="completed" if healthy else "not_healthy", running=service["runningCount"])
        if not healthy:
            raise RuntimeError("Web service did not reach a healthy rollout on the new revision")
    return evidence


def _api_id(clients) -> str:
    apis = [a for a in clients["apigatewayv2"].get_apis()["Items"] if a["Name"] == API_NAME]
    if len(apis) != 1:
        raise RuntimeError("Expected exactly one clhear-webui HTTP API")
    return apis[0]["ApiId"]


def _integrations(clients, api_id: str) -> dict:
    items = clients["apigatewayv2"].get_integrations(ApiId=api_id)["Items"]
    lam = [i for i in items if i["IntegrationType"] == "AWS_PROXY" and i["IntegrationUri"].endswith(f"function:{FUNCTION}/invocations")]
    svc = [i for i in items if i["IntegrationType"] == "HTTP_PROXY" and i.get("ConnectionType") == "VPC_LINK"]
    if len(lam) != 1 or len(svc) != 1:
        raise RuntimeError("Expected one Lambda and one VPC-link integration")
    return {"lambda": lam[0]["IntegrationId"], "service": svc[0]["IntegrationId"]}


def probe(base_url: str = PUBLIC_URL, *, service: bool = False) -> list[dict]:
    """Anonymous checks under restricted access; only the service answers the readiness path."""
    checks = [("/api/clhear/health", 200), ("/api/clhear/sources", 401)]
    if service:
        checks.append(("/api/clhear/ready", 200))
    results = []
    for path, expected in checks:
        request = urllib.request.Request(base_url + path, headers={"Accept": "application/json",
                                                                   "User-Agent": "clhear-web-service-cutover"})
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        except Exception:  # noqa: BLE001 — a timeout is a failed probe
            status = None
        results.append({"path": path, "expected": expected, "status": status,
                        "ms": round((time.monotonic() - started) * 1000)})
    return results


def cutover(clients, *, to: str) -> dict:
    api_id = _api_id(clients)
    integrations = _integrations(clients, api_id)
    routes = [r for r in clients["apigatewayv2"].get_routes(ApiId=api_id)["Items"] if r["RouteKey"] == "$default"]
    if len(routes) != 1:
        raise RuntimeError("Expected exactly one $default route")
    previous = routes[0].get("Target")
    target = f"integrations/{integrations[to]}"
    clients["apigatewayv2"].update_route(ApiId=api_id, RouteId=routes[0]["RouteId"], Target=target)
    time.sleep(5)
    results = probe(service=to == "service")
    ok = all(r["status"] == r["expected"] for r in results)
    if not ok and previous and previous != target:
        clients["apigatewayv2"].update_route(ApiId=api_id, RouteId=routes[0]["RouteId"], Target=previous)
    return {"to": to, "target": target, "previous": previous, "probes": results, "switched": ok}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync-secrets")
    r = sub.add_parser("roll")
    r.add_argument("--image", required=True)
    r.add_argument("--sha", required=True)
    r.add_argument("--desired", type=int)
    r.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                   help=f"service-owned setting ({', '.join(SERVICE_OWNED)})")
    r.add_argument("--require-edge", choices=("yes", "no"))
    c = sub.add_parser("cutover")
    c.add_argument("--to", choices=("service", "lambda"), required=True)
    args = parser.parse_args(argv)
    clients = _clients()
    if args.command == "sync-secrets":
        result = sync_secrets(clients)
    elif args.command == "roll":
        overrides = dict(item.split("=", 1) for item in args.set)
        unknown = set(overrides) - set(SERVICE_OWNED)
        if unknown:
            parser.error(f"not a service-owned setting: {', '.join(sorted(unknown))}")
        result = roll(clients, image=args.image, sha=args.sha, desired=args.desired, overrides=overrides,
                      require_edge=None if args.require_edge is None else args.require_edge == "yes")
    else:
        result = cutover(clients, to=args.to)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("switched", True) else 1


if __name__ == "__main__":
    sys.exit(main())
