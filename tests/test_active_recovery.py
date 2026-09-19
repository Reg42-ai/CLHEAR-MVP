"""The committed selection never becomes an arbitrary file or guessed plan."""
import json

import pytest

from scripts.deployment_recovery import RecoveryPlanError, load_plan
from scripts.l1_recovery import load_active_plan_id


def test_committed_default_references_the_reviewed_plan_of_the_failed_deployment():
    """The pointer names the plan emitted for run 35433640762 attempt 1 — the
    #39 recovery apply whose viewer probe 503'd — not the earlier :18 plan."""
    plan_id = load_active_plan_id()
    plan = load_plan(plan_id)
    assert plan_id == "l1-35433640762-1"
    assert plan["source"]["deployment_id"] == plan_id and plan["viewer"]["reserved_concurrency"] is None
    # restoration restores at least one L0 and one L1 worker and keeps the unreserved shared viewer capacity
    assert plan["fleets"]["l0"]["desired_count"] >= 1 and plan["fleets"]["l1"]["desired_count"] >= 1
    assert plan["fleets"]["l0"]["min_capacity"] >= 1 and plan["fleets"]["l1"]["min_capacity"] >= 1
    assert all(row["task_definition_arn"].endswith(":21") for row in plan["fleets"].values())


def test_recovery_requires_the_exact_maintenance_state_before_restoring():
    """validate_plan is the gate: viewer paused, every fleet at zero, every scaler
    suspended, original identities unchanged. Any deviation keeps recovery held."""
    import copy
    from scripts.deployment_recovery import validate_plan
    plan = load_plan("l1-35433640762-1")
    cluster_arn = f"arn:aws:ecs:{plan['region']}:{plan['account']}:cluster/{plan['cluster']}"
    state = {"concurrency": 0, "fleets": {},
             "function": {"Configuration": {"FunctionName": "clhear-webui", "FunctionArn": plan["viewer"]["function_arn"],
                                            "CodeSha256": plan["viewer"]["code_sha256_base64"]}}}
    for fleet, row in plan["fleets"].items():
        state["fleets"][fleet] = {
            "service": {"serviceName": f"clhear-fleet-{fleet}", "clusterArn": cluster_arn, "serviceArn": row["service_arn"],
                        "taskDefinition": row["task_definition_arn"], "desiredCount": 0, "runningCount": 0, "pendingCount": 0},
            "task": {"taskDefinitionArn": row["task_definition_arn"], "family": f"clhear-fleet-{fleet}"},
            "scaling": {"ServiceNamespace": "ecs", "ScalableDimension": "ecs:service:DesiredCount",
                        "ScalableTargetARN": row["scalable_target_arn"], "ResourceId": row["scalable_resource_id"],
                        "MinCapacity": 0, "MaxCapacity": row["max_capacity"],
                        "SuspendedState": {"DynamicScalingInSuspended": True, "DynamicScalingOutSuspended": True, "ScheduledScalingSuspended": True}}}
    try:
        targets = validate_plan(plan, state)
    except RecoveryPlanError as error:
        pytest.fail(f"a held deployment must be recoverable from the committed plan: {error}")
    assert targets["plan_id"] == "l1-35433640762-1" and targets["fleets"]["l0"]["desired_count"] >= 1
    for change in ("viewer_traffic", "fleet_running", "scaler_active", "identity"):
        broken = copy.deepcopy(state)
        if change == "viewer_traffic":
            broken["concurrency"] = 5
        elif change == "fleet_running":
            broken["fleets"]["l1"]["service"]["runningCount"] = 1
        elif change == "scaler_active":
            broken["fleets"]["l2"]["scaling"]["SuspendedState"]["DynamicScalingOutSuspended"] = False
        else:
            for key in ("service", "task"):
                field = "taskDefinition" if key == "service" else "taskDefinitionArn"
                broken["fleets"]["l0"][key][field] = broken["fleets"]["l0"][key][field][:-2] + "99"
        with pytest.raises(RecoveryPlanError):
            validate_plan(plan, broken)


def test_missing_or_explicitly_disabled_selection(tmp_path):
    assert load_active_plan_id(directory=tmp_path) is None
    (tmp_path / "active-plan.json").write_text('{"plan_id": null}')
    assert load_active_plan_id(directory=tmp_path) is None


def test_explicit_selection_is_returned_without_guessing_a_different_plan(tmp_path):
    (tmp_path / "active-plan.json").write_text('{"plan_id":"l1-123-2"}')
    assert load_active_plan_id(directory=tmp_path) == "l1-123-2"


@pytest.mark.parametrize("selection", [
    {}, [], {"plan_id": "../../other"}, {"plan_id": True}, {"plan_id": 3},
    {"plan_id": "l1-123-2", "path": "another.json"}, {"plan_id": "l1-0-1"},
    {"plan_id": "l1-123-2\n"},
])
def test_invalid_selection_fails_closed(tmp_path, selection):
    (tmp_path / "active-plan.json").write_text(json.dumps(selection))
    with pytest.raises(RecoveryPlanError):
        load_active_plan_id(directory=tmp_path)


@pytest.mark.parametrize("raw", [
    b'{"plan_id":"l1-123-2","plan_id":null}', b"invalid JSON", b"\xff", b" " * 4097,
])
def test_ambiguous_malformed_or_oversized_selection_fails_closed(tmp_path, raw):
    (tmp_path / "active-plan.json").write_bytes(raw)
    with pytest.raises(RecoveryPlanError):
        load_active_plan_id(directory=tmp_path)


@pytest.mark.parametrize("dangling", [False, True])
def test_symlink_selection_is_rejected_even_when_target_is_missing(tmp_path, dangling):
    target = tmp_path / "other.json"
    if not dangling:
        target.write_text('{"plan_id":"l1-123-2"}')
    (tmp_path / "active-plan.json").symlink_to(target)
    with pytest.raises(RecoveryPlanError, match="regular reviewed file"):
        load_active_plan_id(directory=tmp_path)
