"""Recovery configuration tests; no AWS, corpus databases or source text."""
import base64
import copy
import hashlib
import json

import pytest

from scripts.deployment_recovery import (ACCOUNT, BUCKET, CLUSTER, FLEETS, REGION, SUSPENDED,
    RecoveryPlanError, emit_plan, load_plan, validate_plan)

INCIDENT = "l1-34967901665-1"
NEXT = "l1-34967909999-1"


@pytest.fixture
def plan():
    return load_plan(INCIDENT)


def state_for(plan, *, held=True):
    state = {"fleets": {}, "concurrency": 0 if held else plan["viewer"]["reserved_concurrency"],
        "function": {"Configuration": {"FunctionName": "clhear-webui",
            "FunctionArn": plan["viewer"]["function_arn"], "CodeSha256": plan["viewer"]["code_sha256_base64"],
            "Environment": {"Variables": {"SECRET": "must-not-escape-into-plan"}}}}}
    for fleet, row in plan["fleets"].items():
        state["fleets"][fleet] = {
            "service": {"serviceName": f"clhear-fleet-{fleet}", "serviceArn": row["service_arn"],
                "clusterArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/{CLUSTER}",
                "taskDefinition": row["task_definition_arn"], "desiredCount": 0 if held else row["desired_count"],
                "runningCount": 0 if held else row["desired_count"], "pendingCount": 0},
            "task": {"taskDefinitionArn": row["task_definition_arn"], "family": f"clhear-fleet-{fleet}",
                "containerDefinitions": [{"environment": [{"name": "SECRET", "value": "must-not-escape-into-plan"}]}]},
            "scaling": {"ResourceId": row["scalable_resource_id"], "ScalableTargetARN": row["scalable_target_arn"],
                "ServiceNamespace": "ecs", "ScalableDimension": "ecs:service:DesiredCount",
                "MinCapacity": 0 if held else row["min_capacity"], "MaxCapacity": row["max_capacity"],
                "SuspendedState": {k: True for k in SUSPENDED} if held else copy.deepcopy(row["suspended_state"])}
        }
    return state


def provenance(plan, ident=NEXT):
    return {**plan["source"], "deployment_id": ident, "sha": "b" * 40,
            "key": f"deployments/l1/{ident}/rollback.json", "version_id": "new-immutable-test-version",
            "sha256": "c" * 64, "verification": "controller_emitted"}


def test_checked_in_incident_keeps_verified_source_and_original_capacity(plan):
    assert plan["source"]["version_id"] == "3NLgqKbI995W_LCzOTJC6nZFS5mUrN3F"
    assert plan["source"]["sha256"] == "30388f2b37fb82beaa9130a3f651ff818ccec25db676220b74d29b277b9841d8"
    assert plan["source"]["verification"] == "operator_verified"
    assert plan["root_source"] == plan["source"] and plan["runtime_source_read"] is False
    assert plan["viewer"]["reserved_concurrency"] is None
    assert [plan["fleets"][f]["desired_count"] for f in FLEETS] == [1, 1, 0, 0, 0, 0, 0, 0, 0]
    assert [plan["fleets"][f]["max_capacity"] for f in FLEETS] == [1, 1, 2, 2, 1, 1, 1, 1, 1]
    assert all(row["min_capacity"] == 0 and not any(row["suspended_state"].values()) for row in plan["fleets"].values())


def test_validation_returns_separate_targets_without_mutating_observed_state(plan):
    state = state_for(plan)
    original_state, original_plan = copy.deepcopy(state), copy.deepcopy(plan)
    targets = validate_plan(plan, state)
    assert targets["fleets"]["l0"]["desired_count"] == targets["fleets"]["l1"]["desired_count"] == 1
    assert targets["viewer_reserved_concurrency"] is None
    targets["fleets"]["l1"]["desired_count"] = 99
    targets["root_source"]["sha"] = "changed"
    assert state == original_state and plan == original_plan


@pytest.mark.parametrize("path,value", [
    (("fleets", "l0", "service", "desiredCount"), 1),
    (("fleets", "l1", "service", "runningCount"), 1),
    (("fleets", "l8", "service", "pendingCount"), 1),
    (("fleets", "l0", "service", "runningCount"), False),
    (("fleets", "l0", "service", "serviceArn"), "arn:another-product"),
    (("fleets", "l0", "service", "clusterArn"), "arn:another-cluster"),
    (("fleets", "l0", "task", "taskDefinitionArn"), f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/clhear-fleet-l0:8"),
    (("fleets", "l0", "scaling", "MinCapacity"), 1),
    (("fleets", "l0", "scaling", "MaxCapacity"), 2),
    (("fleets", "l0", "scaling", "MaxCapacity"), True),
    (("fleets", "l0", "scaling", "ScalableTargetARN"), f"arn:aws:application-autoscaling:{REGION}:{ACCOUNT}:scalable-target/" + "f" * 36),
    (("fleets", "l0", "scaling", "ResourceId"), f"service/{CLUSTER}/clhear-fleet-l1"),
    (("fleets", "l0", "scaling", "SuspendedState", "DynamicScalingInSuspended"), False),
    (("fleets", "l0", "scaling", "SuspendedState", "ScheduledScalingSuspended"), "true"),
    (("function", "Configuration", "CodeSha256"), base64.b64encode(hashlib.sha256(b"other code").digest()).decode()),
    (("function", "Configuration", "FunctionArn"), "arn:another-function"),
    (("concurrency",), None), (("concurrency",), 1), (("concurrency",), False),
])
def test_recovery_rejects_resource_drift_and_unconfirmed_hold(plan, path, value):
    state = state_for(plan)
    parent = state
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = value
    with pytest.raises(RecoveryPlanError):
        validate_plan(plan, state)


@pytest.mark.parametrize("ident", ["../l1-1-1", "l1-1-1/../../x", "/tmp/l1-1-1", "l1-0-1", "l1-1-0", "l1-1-1.json", "l1-1-1\n", "", None])
def test_loader_rejects_unconstrained_ids_before_file_access(ident, tmp_path):
    with pytest.raises(RecoveryPlanError, match="Invalid recovery plan ID"):
        load_plan(ident, directory=tmp_path)


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(command="forbidden-action"),
    lambda p: p.update(account="000000000000"),
    lambda p: p.update(runtime_source_read=True),
    lambda p: p["fleets"].pop("l8"),
    lambda p: p["fleets"].update(l9=p["fleets"]["l0"]),
    lambda p: p["fleets"]["l0"].update(role="arbitrary-role"),
    lambda p: p["fleets"]["l0"].update(desired_count=True),
    lambda p: p["fleets"]["l0"].update(min_capacity=-1),
    lambda p: p["fleets"]["l0"].update(desired_count=2),
    lambda p: p["fleets"]["l0"].update(scalable_target_arn=p["fleets"]["l1"]["scalable_target_arn"]),
    lambda p: p["source"].update(key="releases/latest.json"),
    lambda p: p["source"].update(version_id="null"),
    lambda p: p["source"].update(sha256="not-a-hash"),
    lambda p: p["source"].update(deployment_id=NEXT),
    lambda p: p["root_source"].update(bucket="another-bucket"),
    lambda p: p["source"].update(verification=[]),
    lambda p: p["viewer"].update(reserved_concurrency=True),
    lambda p: p["viewer"].update(code_sha256_base64="invalid"),
])
def test_plan_schema_cannot_carry_commands_secrets_or_unbounded_targets(plan, mutation, tmp_path):
    mutation(plan)
    (tmp_path / f"{INCIDENT}.json").write_text(json.dumps(plan))
    with pytest.raises(RecoveryPlanError):
        load_plan(INCIDENT, directory=tmp_path)


def test_loader_rejects_missing_symlink_duplicate_and_oversize_files(plan, tmp_path):
    with pytest.raises(RecoveryPlanError):
        load_plan(INCIDENT, directory=tmp_path)
    path = tmp_path / f"{INCIDENT}.json"
    target = tmp_path / "other.json"
    target.write_text(json.dumps(plan))
    path.symlink_to(target)
    with pytest.raises(RecoveryPlanError, match="regular reviewed file"):
        load_plan(INCIDENT, directory=tmp_path)
    path.unlink()
    path.write_text(json.dumps(plan).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'))
    with pytest.raises(RecoveryPlanError, match="Duplicate"):
        load_plan(INCIDENT, directory=tmp_path)
    path.write_text(" " * (64 * 1024 + 1))
    with pytest.raises(RecoveryPlanError, match="too large"):
        load_plan(INCIDENT, directory=tmp_path)


def test_emit_normal_plan_contains_only_capacity_and_provenance(plan):
    state = state_for(plan, held=False)
    original = copy.deepcopy(state)
    out = emit_plan(NEXT, state, rollback_provenance=provenance(plan))
    assert out["root_source"] == out["source"]
    assert out["fleets"] == plan["fleets"] and out["viewer"] == plan["viewer"]
    serialized = json.dumps(out)
    assert "must-not-escape" not in serialized and "Environment" not in serialized and "containerDefinitions" not in serialized
    assert state == original and out["review_requirement"] == "owner-reviewed-main-and-protected-environment"


def test_repeated_failures_keep_original_targets_instead_of_maintenance_zeros(plan):
    state = state_for(plan)
    targets = validate_plan(plan, state)
    targets_before = copy.deepcopy(targets)
    out = emit_plan(NEXT, state, rollback_provenance=provenance(plan), restoration_targets=targets)
    assert out["root_source"] == plan["root_source"] and out["source"]["deployment_id"] == NEXT
    assert out["fleets"] == plan["fleets"] and out["viewer"]["reserved_concurrency"] is None
    repeated_targets = validate_plan(out, state)
    again = emit_plan("l1-34967909999-2", state, rollback_provenance=provenance(plan, "l1-34967909999-2"),
                      restoration_targets=repeated_targets)
    assert again["fleets"] == plan["fleets"] and again["root_source"] == plan["root_source"]
    assert state["fleets"]["l0"]["service"]["desiredCount"] == 0 and state["concurrency"] == 0
    assert targets == targets_before


def test_emit_refuses_maintenance_baseline_without_reviewed_original_targets(plan):
    with pytest.raises(RecoveryPlanError, match="explicit original"):
        emit_plan(NEXT, state_for(plan), rollback_provenance=provenance(plan))


def test_emit_refuses_target_capacity_increase_or_wrong_source_run(plan):
    state = state_for(plan)
    targets = validate_plan(plan, state)
    targets["fleets"]["l0"]["max_capacity"] = 99
    with pytest.raises(RecoveryPlanError, match="maximum differs"):
        emit_plan(NEXT, state, rollback_provenance=provenance(plan), restoration_targets=targets)
    with pytest.raises(RecoveryPlanError, match="source and run IDs differ"):
        emit_plan(NEXT, state_for(plan, held=False), rollback_provenance=plan["source"])
