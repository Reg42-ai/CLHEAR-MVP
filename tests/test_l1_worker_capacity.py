"""L0/L1 stay available through an empty visible queue; AWS is entirely mocked."""
import copy

import pytest

from scripts.deploy_l1 import DeploymentError, FLEETS, SUSPENDED
from scripts.deployment_recovery import load_plan
from tests.test_l1_deployment import Cloud


def assert_maintenance(cloud, result):
    assert result["status"] == "failed_maintenance"
    assert result["recovery_required"] and not result["accepted_release_changed"]
    assert cloud.concurrency == 0
    assert all(service["desiredCount"] == 0 for service in cloud.services.values())
    assert all(target["MinCapacity"] == 0 and target["SuspendedState"] == SUSPENDED
               for target in cloud.scaling.values())
    assert not any(operation == "put_object" and args["Key"].startswith("releases/")
                   for _, operation, args in cloud.mutations)


def test_idle_l0_and_l1_resume_with_floor_without_increasing_reviewed_maximum():
    cloud = Cloud()
    for fleet in ("l0", "l1"):
        cloud.services[fleet].update(desiredCount=0, runningCount=0, pendingCount=0)
        cloud.scaling[fleet].update(MinCapacity=0, MaxCapacity=1)
    maxima = {fleet: target["MaxCapacity"] for fleet, target in cloud.scaling.items()}
    downstream = {fleet: copy.deepcopy(cloud.services[fleet]) for fleet in FLEETS if fleet not in {"l0", "l1"}}

    result = cloud.deployer().deploy()

    assert result["status"] == "verified" and not result["accepted_release_changed"]
    for fleet in ("l0", "l1"):
        assert cloud.services[fleet]["desiredCount"] == cloud.services[fleet]["runningCount"] == 1
        assert cloud.services[fleet]["pendingCount"] == 0
        assert cloud.scaling[fleet]["MinCapacity"] == 1
    assert {fleet: target["MaxCapacity"] for fleet, target in cloud.scaling.items()} == maxima
    for fleet, previous in downstream.items():
        assert cloud.services[fleet]["desiredCount"] == previous["desiredCount"]
        assert cloud.scaling[fleet]["MinCapacity"] == 0


def test_capacity_floor_is_read_back_before_positive_service_capacity():
    cloud = Cloud()
    deployer = cloud.deployer()
    assert deployer.deploy()["status"] == "verified"

    for fleet in ("l0", "l1"):
        new_definition = deployer.state["fleets"][fleet]["new_task_definition"]
        floor = next(index for index, (_, operation, args) in enumerate(cloud.calls)
                     if operation == "register_scalable_target" and args["ResourceId"].endswith(f"fleet-{fleet}")
                     and args["MinCapacity"] == 1 and args["SuspendedState"] == SUSPENDED)
        release = next(index for index, (_, operation, args) in enumerate(cloud.calls)
                       if index > floor and operation == "update_service" and args["service"] == f"clhear-fleet-{fleet}"
                       and args.get("desiredCount", 0) >= 1)
        assert any(operation == "update_service" and args["service"] == f"clhear-fleet-{fleet}"
                   and args.get("desiredCount") == 0 and args.get("taskDefinition") == new_definition
                   for _, operation, args in cloud.calls[:floor])
        assert any(operation == "describe_scalable_targets"
                   and any(resource.endswith(f"fleet-{fleet}") for resource in args["ResourceIds"])
                   for _, operation, args in cloud.calls[floor + 1:release])
        restored_flags = next(index for index, (_, operation, args) in enumerate(cloud.calls)
                              if index > release and operation == "register_scalable_target"
                              and args["ResourceId"].endswith(f"fleet-{fleet}")
                              and args["SuspendedState"] != SUSPENDED)
        assert any(operation == "describe_scalable_targets"
                   and any(resource.endswith(f"fleet-{fleet}") for resource in args["ResourceIds"])
                   for _, operation, args in cloud.calls[restored_flags + 1:])


def test_empty_visible_queue_cannot_scale_in_the_worker_with_an_inflight_final_job():
    cloud = Cloud()
    assert cloud.deployer().deploy()["status"] == "verified"
    visible, in_flight = 0, 1  # Final cycle evaluation is the only SQS message.
    assert visible == 0 and in_flight == 1
    target = cloud.scaling["l1"]
    assert target["SuspendedState"]["DynamicScalingInSuspended"] is False
    # Model Application Auto Scaling's capacity bounds on ExactCapacity=0,
    # the repository's visible-queue-empty policy. No task protection assumed.
    alarm_requested_capacity = 0
    resulting_capacity = max(target["MinCapacity"], min(target["MaxCapacity"], alarm_requested_capacity))
    assert resulting_capacity == cloud.services["l1"]["desiredCount"] == 1


def test_committed_recovery_plan_zero_minima_cannot_remove_l1_execution_floor():
    plan = load_plan("l1-34967901665-1")
    assert plan["fleets"]["l0"]["min_capacity"] == plan["fleets"]["l1"]["min_capacity"] == 0
    original_plan = copy.deepcopy(plan)
    cloud = Cloud()
    for fleet, reviewed in plan["fleets"].items():
        cloud.scaling[fleet]["MaxCapacity"] = reviewed["max_capacity"]
    deployer = cloud.deployer()
    deployer._guard()
    deployer.preflight()
    deployer._hold()
    deployer._register()
    # Use the real reviewed capacity values. Historical identity/hash matching
    # is tested by recovery integration, not replaced with fabricated ZIP bytes.
    deployer.state["restoration_targets"] = {"fleets": copy.deepcopy(plan["fleets"])}

    deployer._restore_capacity()

    for fleet, reviewed in plan["fleets"].items():
        floor = 1 if fleet in {"l0", "l1"} else 0
        assert cloud.services[fleet]["desiredCount"] == max(floor, reviewed["desired_count"])
        assert cloud.scaling[fleet]["MinCapacity"] == max(floor, reviewed["min_capacity"])
        assert cloud.scaling[fleet]["MaxCapacity"] == reviewed["max_capacity"]
        assert cloud.scaling[fleet]["SuspendedState"] == reviewed["suspended_state"]
    assert plan == original_plan


@pytest.mark.parametrize("fleet", ["l0", "l1"])
def test_incompatible_worker_maximum_rejected_before_any_deployment_write(fleet):
    cloud = Cloud()
    cloud.services[fleet].update(desiredCount=0, runningCount=0)
    cloud.scaling[fleet].update(MinCapacity=0, MaxCapacity=0)
    with pytest.raises(DeploymentError):
        cloud.deployer().deploy()
    assert not cloud.mutations


@pytest.mark.parametrize("fault", ["minimum", "maximum", "suspension", "read_failure"])
def test_failed_capacity_readback_restores_maintenance_without_releasing_l1(monkeypatch, fault):
    cloud = Cloud()
    original_call = cloud.call
    armed, injected = False, False

    def call(service, operation, args):
        nonlocal armed, injected
        response = original_call(service, operation, args)
        if (operation == "register_scalable_target" and args["ResourceId"].endswith("fleet-l1")
                and args["MinCapacity"] == 1 and args["SuspendedState"] == SUSPENDED):
            armed = True
        if (armed and not injected and operation == "describe_scalable_targets"
                and any(resource.endswith("fleet-l1") for resource in args["ResourceIds"])):
            injected = True
            if fault == "read_failure":
                raise RuntimeError("fixture scaler readback unavailable")
            response = copy.deepcopy(response)
            target = response["ScalableTargets"][0]
            if fault == "minimum":
                target["MinCapacity"] = 0
            elif fault == "maximum":
                target["MaxCapacity"] += 1
            else:
                target["SuspendedState"]["DynamicScalingInSuspended"] = False
        return response

    monkeypatch.setattr(cloud, "call", call)
    result = cloud.deployer().deploy()
    assert injected
    assert_maintenance(cloud, result)
    assert not any(operation == "update_service" and args["service"] == "clhear-fleet-l1"
                   and args.get("desiredCount", 0) > 0 for _, operation, args in cloud.calls)


@pytest.mark.parametrize("fault", ["stale_definition", "running", "pending", "rollout_failed", "waiter_failed"])
def test_unhealthy_restored_service_cannot_report_success(monkeypatch, fault):
    cloud = Cloud()
    original_call = cloud.call
    stable_returned, injected = False, False

    def call(service, operation, args):
        nonlocal stable_returned, injected
        response = original_call(service, operation, args)
        if operation == "wait:services_stable":
            stable_returned = True
            if fault == "waiter_failed":
                injected = True
                raise RuntimeError("fixture service waiter failed")
        if operation == "describe_services" and stable_returned and not injected:
            response = copy.deepcopy(response)
            row = next(row for row in response["services"] if row["serviceName"] == "clhear-fleet-l1")
            injected = True
            if fault == "stale_definition":
                row["taskDefinition"] = row["taskDefinition"].rsplit(":", 1)[0] + ":1"
            elif fault == "running":
                row["runningCount"] = 0
            elif fault == "pending":
                row["pendingCount"] = 1
            else:
                row["deployments"][0]["rolloutState"] = "FAILED"
        return response

    monkeypatch.setattr(cloud, "call", call)
    result = cloud.deployer().deploy()
    assert injected
    assert_maintenance(cloud, result)
