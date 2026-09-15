"""Capacity-only recovery plans reviewed on main before protected deployment.

This module never calls AWS. Source versions/hashes document the operator's
review; they do not assert that the deployment role reread the S3 manifest.
Loading a plan is not authorization: the caller must retain its main/workflow,
CI, environment-approval and deployment-identity gates.
"""
from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
import re

ACCOUNT = "730649732189"
REGION = "us-east-1"
CLUSTER = "clhear-cluster"
BUCKET = f"clhear-deploy-{ACCOUNT}"
FLEETS = tuple(f"l{i}" for i in range(9))
SUSPENDED = ("DynamicScalingInSuspended", "DynamicScalingOutSuspended", "ScheduledScalingSuspended")
REVIEW_REQUIREMENT = "owner-reviewed-main-and-protected-environment"
PLAN_DIRECTORY = Path(__file__).resolve().parents[1] / "infra" / "recovery"
MAX_PLAN_BYTES = 64 * 1024
_ID = re.compile(r"l1-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}\Z")
_CLUSTER_ARN = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/{CLUSTER}"
_FUNCTION_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:clhear-webui"


class RecoveryPlanError(ValueError):
    """Fixed diagnostic messages contain no caller-supplied configuration."""


def _require(ok, message):
    if not ok:
        raise RecoveryPlanError(message)


def _keys(value, expected, message):
    _require(type(value) is dict and set(value) == set(expected), message)


def _plan_id(value):
    _require(isinstance(value, str) and bool(_ID.fullmatch(value)), "Invalid recovery plan ID")
    return value


def _integer(value):
    # ECS/Lambda capacity API fields are integers; bool is not a capacity.
    return type(value) is int and 0 <= value <= 2**31 - 1


def _flags(value):
    _keys(value, SUSPENDED, "Invalid recovery suspension flags")
    _require(all(type(v) is bool for v in value.values()), "Invalid recovery suspension flags")


def _concurrency(value):
    _require(value is None or _integer(value), "Invalid recovery viewer concurrency")


def _source(value):
    _keys(value, ("deployment_id", "sha", "bucket", "key", "version_id", "sha256", "verification"),
          "Invalid recovery source provenance")
    ident = _plan_id(value["deployment_id"])
    _require(value["bucket"] == BUCKET and value["key"] == f"deployments/l1/{ident}/rollback.json",
             "Recovery source is outside the exact rollback location")
    _require(isinstance(value["sha"], str) and bool(re.fullmatch(r"[0-9a-f]{40}", value["sha"])),
             "Invalid recovery source commit")
    _require(isinstance(value["sha256"], str) and bool(re.fullmatch(r"[0-9a-f]{64}", value["sha256"])),
             "Invalid recovery source hash")
    version = value["version_id"]
    _require(isinstance(version, str) and version != "null" and
             bool(re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,1024}", version)), "Invalid recovery source version")
    _require(isinstance(value["verification"], str) and value["verification"] in {"operator_verified", "controller_emitted"},
             "Invalid recovery source verification method")


def _capacity(row):
    _keys(row, ("desired_count", "min_capacity", "max_capacity", "suspended_state"),
          "Invalid recovery capacity fields")
    _require(all(_integer(row[k]) for k in ("desired_count", "min_capacity", "max_capacity")),
             "Invalid recovery fleet capacity")
    _require(row["min_capacity"] <= row["max_capacity"] and row["desired_count"] <= row["max_capacity"],
             "Recovery fleet capacity exceeds its original maximum")
    _flags(row["suspended_state"])


def _digest(value):
    try:
        raw = base64.b64decode(value, validate=True) if isinstance(value, str) else b""
    except (ValueError, TypeError):
        raw = b""
    _require(len(raw) == 32 and base64.b64encode(raw).decode() == value, "Invalid recovery viewer code hash")


def _validate_schema(plan):
    _keys(plan, ("schema_version", "plan_id", "account", "region", "cluster", "review_requirement",
                 "runtime_source_read", "source", "root_source", "fleets", "viewer"),
          "Invalid recovery plan fields")
    _require(type(plan["schema_version"]) is int and plan["schema_version"] == 1, "Unsupported recovery plan schema")
    _plan_id(plan["plan_id"])
    _require((plan["account"], plan["region"], plan["cluster"]) == (ACCOUNT, REGION, CLUSTER),
             "Recovery plan identifies another account, region or cluster")
    _require(plan["review_requirement"] == REVIEW_REQUIREMENT and plan["runtime_source_read"] is False,
             "Recovery plan must disclose owner review and source-verification limits")
    _source(plan["source"])
    _source(plan["root_source"])
    _require(plan["source"]["deployment_id"] == plan["plan_id"], "Recovery plan and source IDs differ")
    _keys(plan["fleets"], FLEETS, "Recovery plan must contain exactly nine fleets")
    seen_scalers = set()
    for fleet, row in plan["fleets"].items():
        _keys(row, ("service_arn", "task_definition_arn", "scalable_target_arn", "scalable_resource_id",
                    "desired_count", "min_capacity", "max_capacity", "suspended_state"),
              "Invalid recovery fleet fields")
        _require(row["service_arn"] == f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/{CLUSTER}/clhear-fleet-{fleet}",
                 "Recovery plan service is not its exact CLHEAR fleet")
        _require(isinstance(row["task_definition_arn"], str) and bool(re.fullmatch(
            rf"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/clhear-fleet-{fleet}:[1-9][0-9]*",
            row["task_definition_arn"])), "Invalid recovery task definition")
        _require(row["scalable_resource_id"] == f"service/{CLUSTER}/clhear-fleet-{fleet}",
                 "Recovery plan scaler is not its exact CLHEAR fleet")
        arn = row["scalable_target_arn"]
        _require(isinstance(arn, str) and bool(re.fullmatch(
            rf"arn:aws:application-autoscaling:{REGION}:{ACCOUNT}:scalable-target/[0-9a-f]{{36}}", arn)),
            "Invalid recovery scalable-target ARN")
        _require(arn not in seen_scalers, "Recovery plan reuses a scalable target")
        seen_scalers.add(arn)
        _capacity({k: row[k] for k in ("desired_count", "min_capacity", "max_capacity", "suspended_state")})
    _keys(plan["viewer"], ("function_arn", "code_sha256_base64", "reserved_concurrency"),
          "Invalid recovery viewer fields")
    _require(plan["viewer"]["function_arn"] == _FUNCTION_ARN, "Invalid recovery viewer identity")
    _digest(plan["viewer"]["code_sha256_base64"])
    _concurrency(plan["viewer"]["reserved_concurrency"])


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate recovery JSON key")
        result[key] = value
    return result


def load_plan(plan_id: str, *, directory: Path | None = None) -> dict:
    """Load only a constrained ID from the reviewed recovery directory.

    ``directory`` supports fixture roots; callers must not expose it as input.
    Symlinks, oversized files, duplicate keys and unknown fields are rejected.
    """
    ident = _plan_id(plan_id)
    root = (PLAN_DIRECTORY if directory is None else Path(directory)).resolve()
    path = root / f"{ident}.json"
    _require(not path.is_symlink() and path.resolve().parent == root, "Recovery plan path is not a regular reviewed file")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_PLAN_BYTES + 1)
        _require(len(raw) <= MAX_PLAN_BYTES, "Recovery plan is too large")
        plan = json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryPlanError("Recovery plan is missing or invalid JSON") from exc
    _validate_schema(plan)
    _require(plan["plan_id"] == ident, "Recovery filename and plan IDs differ")
    return plan


def _identities(state):
    """Validate and copy just public identifiers from authoritative preflight."""
    _require(type(state) is dict, "Missing current deployment state")
    _keys(state.get("fleets"), FLEETS, "Current state must contain exactly nine fleets")
    rows = {}
    for fleet, value in state["fleets"].items():
        _require(type(value) is dict, "Invalid current fleet state")
        service, task, scaling = (value.get(k) for k in ("service", "task", "scaling"))
        _require(all(type(item) is dict for item in (service, task, scaling)), "Incomplete current fleet state")
        _require(service.get("serviceName") == f"clhear-fleet-{fleet}" and
                 service.get("clusterArn") == _CLUSTER_ARN, "Current service identity differs from CLHEAR")
        _require(service.get("taskDefinition") == task.get("taskDefinitionArn") and
                 task.get("family") == f"clhear-fleet-{fleet}", "Current service and task definition disagree")
        _require(scaling.get("ServiceNamespace") == "ecs" and
                 scaling.get("ScalableDimension") == "ecs:service:DesiredCount", "Unexpected current scaler type")
        rows[fleet] = {"service_arn": service.get("serviceArn"),
                       "task_definition_arn": task.get("taskDefinitionArn"),
                       "scalable_target_arn": scaling.get("ScalableTargetARN"),
                       "scalable_resource_id": scaling.get("ResourceId")}
    function = state.get("function")
    _require(type(function) is dict and type(function.get("Configuration")) is dict, "Missing current viewer state")
    config = function["Configuration"]
    _require(config.get("FunctionName") == "clhear-webui" and config.get("FunctionArn") == _FUNCTION_ARN,
             "Current viewer identity differs from CLHEAR")
    _require("concurrency" in state, "Missing current viewer concurrency")
    return rows, {"function_arn": config["FunctionArn"], "code_sha256_base64": config.get("CodeSha256")}


def _targets_schema(targets):
    _keys(targets, ("fleets", "viewer_reserved_concurrency", "root_source", "plan_id"),
          "Invalid recovery restoration targets")
    _plan_id(targets["plan_id"])
    _source(targets["root_source"])
    _keys(targets["fleets"], FLEETS, "Restoration requires exactly nine fleets")
    for row in targets["fleets"].values():
        _capacity(row)
    _concurrency(targets["viewer_reserved_concurrency"])


def validate_plan(plan: dict, state: dict) -> dict:
    """Require exact original identities and a confirmed hold; return a copy.

    Live MaxCapacity must equal the reviewed original maximum: a recreated or
    resized scaler is drift, not permission to increase capacity. This does not
    query tasks independently; preflight/hold must also establish no one-offs.
    """
    _validate_schema(plan)
    identities, viewer = _identities(state)
    _require(type(state["concurrency"]) is int and state["concurrency"] == 0, "Recovery requires paused viewer traffic")
    for fleet, row in plan["fleets"].items():
        _require(all(row[k] == v for k, v in identities[fleet].items()), "Recovery fleet resource binding changed")
        service = state["fleets"][fleet]["service"]
        scaling = state["fleets"][fleet]["scaling"]
        _require(all(type(service.get(k)) is int and service[k] == 0 for k in ("desiredCount", "runningCount", "pendingCount")),
                 "Recovery requires every fleet to have zero desired, running and pending tasks")
        _require(type(scaling.get("MinCapacity")) is int and scaling["MinCapacity"] == 0 and
                 type(scaling.get("MaxCapacity")) is int and scaling["MaxCapacity"] == row["max_capacity"],
                 "Recovery scaler capacity differs from the held original")
        _flags(scaling.get("SuspendedState"))
        _require(all(scaling["SuspendedState"].values()), "Recovery requires every scaler to be suspended")
    _require(all(plan["viewer"][k] == v for k, v in viewer.items()), "Recovery viewer code binding changed")
    result = {"fleets": {fleet: {k: copy.deepcopy(row[k]) for k in
               ("desired_count", "min_capacity", "max_capacity", "suspended_state")}
               for fleet, row in plan["fleets"].items()},
              "viewer_reserved_concurrency": plan["viewer"]["reserved_concurrency"],
              "root_source": copy.deepcopy(plan["root_source"]), "plan_id": plan["plan_id"]}
    _targets_schema(result)
    return result


def emit_plan(deployment_id: str, state: dict, *, rollback_provenance: dict,
              restoration_targets: dict | None = None) -> dict:
    """Create a capacity-only review artifact from the starting preflight state.

    Call before mutating ``state``. Pass prior validated restoration targets on
    recovery attempts so a repeated failure never records maintenance zeros as
    the intended capacity. This returns data only; it does not save or approve.
    """
    ident = _plan_id(deployment_id)
    _source(rollback_provenance)
    _require(rollback_provenance["deployment_id"] == ident, "Emitted recovery source and run IDs differ")
    rows, viewer = _identities(state)
    if restoration_targets is not None:
        _targets_schema(restoration_targets)
        targets = copy.deepcopy(restoration_targets)
        _require(all(type(state["fleets"][fleet]["scaling"].get("MaxCapacity")) is int and
                     state["fleets"][fleet]["scaling"]["MaxCapacity"] == row["max_capacity"]
                     for fleet, row in targets["fleets"].items()),
                 "Restoration maximum differs from current original scaler")
    else:
        held = state["concurrency"] == 0 and all(
            value["service"].get("desiredCount") == 0 and value["scaling"].get("MinCapacity") == 0 and
            all(value["scaling"].get("SuspendedState", {}).get(k) is True for k in SUSPENDED)
            for value in state["fleets"].values())
        _require(not held, "Held deployment requires explicit original restoration targets")
        targets = {"fleets": {}, "viewer_reserved_concurrency": state["concurrency"],
                   "root_source": copy.deepcopy(rollback_provenance), "plan_id": ident}
        for fleet, value in state["fleets"].items():
            scaling = value["scaling"]
            targets["fleets"][fleet] = {"desired_count": value["service"].get("desiredCount"),
                "min_capacity": scaling.get("MinCapacity"), "max_capacity": scaling.get("MaxCapacity"),
                "suspended_state": copy.deepcopy(scaling.get("SuspendedState"))}
        _targets_schema(targets)
    for fleet, row in rows.items():
        row.update(copy.deepcopy(targets["fleets"][fleet]))
    viewer["reserved_concurrency"] = targets["viewer_reserved_concurrency"]
    result = {"schema_version": 1, "plan_id": ident, "account": ACCOUNT, "region": REGION, "cluster": CLUSTER,
              "review_requirement": REVIEW_REQUIREMENT, "runtime_source_read": False,
              "source": copy.deepcopy(rollback_provenance), "root_source": copy.deepcopy(targets["root_source"]),
              "fleets": rows, "viewer": viewer}
    _validate_schema(result)
    return result
