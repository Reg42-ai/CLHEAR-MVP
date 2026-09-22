"""Controller recovery integration using only the existing mocked AWS cloud."""
import base64
import copy
import hashlib
import json
import stat

import pytest

import scripts.deploy_l1 as controller
from scripts.deployment_recovery import RecoveryPlanError, emit_plan
from tests.test_l1_deployment import (ACCOUNT, BUCKET, CLUSTER, FLEETS, REGION, SUSPENDED,
                                     Cloud, DeploymentError, environment, inputs)

ORIGINAL = "l1-123-1"
RECOVERY = "l1-124-1"
RETRY = "l1-125-1"


def original_plan(cloud):
    """Build the pre-maintenance review artifact from the same real preflight."""
    for fleet in FLEETS:
        cloud.services[fleet]["desiredCount"] = 1 if fleet in {"l0", "l1"} else 0
        cloud.scaling[fleet]["MaxCapacity"] = 2 if fleet in {"l2", "l3"} else 1
    deployer = cloud.deployer(environ=environment())
    deployer.preflight()
    return emit_plan(ORIGINAL, deployer.state, rollback_provenance={
        "deployment_id": ORIGINAL, "sha": inputs().sha, "bucket": BUCKET,
        "key": f"deployments/l1/{ORIGINAL}/rollback.json", "version_id": "original-test-version",
        "sha256": "d" * 64, "verification": "controller_emitted"})


def maintenance(cloud):
    for fleet in FLEETS:
        cloud.services[fleet].update(desiredCount=0, runningCount=0, pendingCount=0)
        cloud.scaling[fleet].update(MinCapacity=0, SuspendedState=dict(SUSPENDED))
    cloud.concurrency = 0
    cloud.config["RevisionId"] += "-prior-maintenance"


def reviewed_plan(monkeypatch, plan):
    def load(ident):
        assert ident == plan["plan_id"], "Controller must load the selected reviewed plan"
        return copy.deepcopy(plan)
    monkeypatch.setattr(controller, "load_plan", load)


def assert_targets(plan, emitted):
    for fleet, row in plan["fleets"].items():
        assert {key: emitted["fleets"][fleet][key] for key in
                ("desired_count", "min_capacity", "max_capacity", "suspended_state")} == {
                    key: row[key] for key in ("desired_count", "min_capacity", "max_capacity", "suspended_state")}
    assert emitted["viewer"]["reserved_concurrency"] == plan["viewer"]["reserved_concurrency"]
    assert emitted["root_source"] == plan["root_source"]


def assert_no_release_writes(cloud):
    assert not any(operation == "put_object" and args["Key"].startswith("releases/")
                   for _, operation, args in cloud.mutations)
    assert not any(operation == "get_object" and args["Key"].endswith("rollback.json")
                   for _, operation, args in cloud.calls), "Recovery must not assume an ungranted S3 read"


def test_normal_deployment_emits_capacity_only_plan_with_immutable_backup_binding():
    cloud = Cloud()
    original_desired = {fleet: value["desiredCount"] for fleet, value in cloud.services.items()}
    deployer = cloud.deployer()
    assert deployer.deploy()["status"] == "verified"
    plan = deployer.recovery_plan_output
    assert plan["plan_id"] == ORIGINAL and plan["root_source"] == plan["source"]
    assert plan["source"]["version_id"] == "private-version"
    backup_key = f"deployments/l1/{ORIGINAL}/rollback.json"
    assert plan["source"]["key"] == backup_key
    assert plan["source"]["sha256"] == hashlib.sha256(cloud.objects[backup_key]).hexdigest()
    assert {fleet: row["desired_count"] for fleet, row in plan["fleets"].items()} == original_desired
    assert plan["viewer"]["reserved_concurrency"] is None
    encoded = json.dumps(plan)
    assert not any(value in encoded for value in ("private-env-value", "private-oauth-value", "test-only-private-session", "test-secret-not-for-logs"))
    assert_no_release_writes(cloud)


@pytest.mark.parametrize("original_concurrency", [None, 0, 4])
def test_recovery_restores_original_capacity_after_worker_phases_and_keeps_observed_zeros(monkeypatch, original_concurrency):
    cloud = Cloud()
    cloud.concurrency = original_concurrency
    plan = original_plan(cloud)
    maintenance(cloud)
    reviewed_plan(monkeypatch, plan)
    deployer = cloud.deployer(input=inputs(deployment_id=RECOVERY, recovery_plan=ORIGINAL))
    assert deployer.preflight()["status"] == "preflight_passed"
    assert not cloud.mutations
    assert deployer.state["restoration_targets"]["fleets"]["l1"]["desired_count"] == 1
    result = deployer.deploy()
    assert result["status"] == "verified" and result["recovery_required"] is False
    launches = [args for _, operation, args in cloud.calls if operation == "run_task"]
    phases = [args for args in launches if args["overrides"]["containerOverrides"][0]["command"][0] == "--verify-deployment"]
    assert [args["overrides"]["containerOverrides"][0]["command"][1] for args in phases] == ["bootstrap", "verify", "publish"]
    assert [args["overrides"]["containerOverrides"][0]["command"][0] for args in launches[len(phases):]] == ["--request-demo-import"]
    for fleet in FLEETS:
        expected = plan["fleets"][fleet]
        assert cloud.services[fleet]["desiredCount"] == (max(1, expected["desired_count"]) if fleet in {"l0", "l1"} else expected["desired_count"])
        assert cloud.scaling[fleet]["MinCapacity"] == (max(1, expected["min_capacity"]) if fleet in {"l0", "l1"} else expected["min_capacity"])
        assert cloud.scaling[fleet]["MaxCapacity"] == expected["max_capacity"]
        assert cloud.scaling[fleet]["SuspendedState"] == expected["suspended_state"]
        observed = deployer.state["fleets"][fleet]
        assert observed["service"]["desiredCount"] == 0 and observed["scaling"]["MinCapacity"] == 0
        assert observed["scaling"]["SuspendedState"] == SUSPENDED
        definition = cloud.definitions[cloud.services[fleet]["taskDefinition"]]
        env = {row["name"]: row["value"] for row in definition["containerDefinitions"][0]["environment"]}
        assert env["CLHEAR_L1_ONLY"] == "true"
    assert cloud.concurrency == original_concurrency and deployer.state["concurrency"] == 0
    assert_targets(plan, deployer.recovery_plan_output)
    observed_backup = json.loads(cloud.objects[f"deployments/l1/{RECOVERY}/rollback.json"])
    assert all(row["desired_count"] == 0 for row in observed_backup["fleets"].values())
    assert observed_backup["viewer"]["reserved_concurrency"] == 0
    assert_no_release_writes(cloud)


def test_failed_recovery_emits_original_targets_for_another_successful_retry(monkeypatch):
    cloud = Cloud()
    plan = original_plan(cloud)
    maintenance(cloud)
    reviewed_plan(monkeypatch, plan)
    cloud.fail_action = "verify"
    second = cloud.deployer(input=inputs(deployment_id=RECOVERY, recovery_plan=ORIGINAL))
    result = second.deploy()
    assert result["status"] == "failed_maintenance" and result["recovery_required"] is True
    emitted = copy.deepcopy(second.recovery_plan_output)
    assert emitted["source"]["deployment_id"] == RECOVERY
    assert_targets(plan, emitted)
    assert cloud.concurrency == 0
    assert all(row["desiredCount"] == 0 for row in cloud.services.values())
    assert all(row["SuspendedState"] == SUSPENDED for row in cloud.scaling.values())
    cloud.fail_action = None
    reviewed_plan(monkeypatch, emitted)
    third = cloud.deployer(input=inputs(deployment_id=RETRY, recovery_plan=RECOVERY))
    assert third.deploy()["status"] == "verified"
    assert_targets(plan, third.recovery_plan_output)
    assert cloud.services["l1"]["desiredCount"] == 1 and cloud.concurrency is None
    assert_no_release_writes(cloud)


def test_ordinary_redispatch_of_maintenance_state_fails_before_writes(monkeypatch):
    cloud = Cloud()
    maintenance(cloud)
    monkeypatch.setattr(controller, "load_active_plan_id", lambda: None)
    with pytest.raises((DeploymentError, RecoveryPlanError)):
        cloud.deployer(input=inputs(deployment_id=RECOVERY)).deploy()
    assert not cloud.mutations


def test_default_recovery_is_read_only_then_restores_reviewed_targets_under_reservation_quota(monkeypatch):
    cloud = Cloud()
    cloud.lambda_account_concurrency_limit = cloud.lambda_unreserved_minimum
    plan = original_plan(cloud)
    maintenance(cloud)
    reviewed_plan(monkeypatch, plan)
    monkeypatch.setattr(controller, "load_active_plan_id", lambda: ORIGINAL)
    cloud.exits["verify"] = 2
    deployer = cloud.deployer(input=inputs(deployment_id=RECOVERY))
    result = deployer.preflight()
    assert result["recovery"]["selection"] == "default" and result["recovery"]["plan_id"] == ORIGINAL
    assert deployer.inputs.recovery_plan == "" and not cloud.mutations
    assert deployer.state["concurrency"] == 0
    result = deployer.deploy()
    assert result["status"] == "review_ready" and result["accepted_release_changed"] is False
    assert cloud.concurrency is None and cloud.services["l0"]["desiredCount"] == cloud.services["l1"]["desiredCount"] == 1
    assert all(cloud.services[fleet]["desiredCount"] == 0 for fleet in FLEETS if fleet not in {"l0", "l1"})
    assert all(args["ReservedConcurrentExecutions"] == 0 for _, op, args in cloud.calls if op == "put_function_concurrency")
    assert_targets(plan, deployer.recovery_plan_output)
    assert_no_release_writes(cloud)


@pytest.mark.parametrize("explicit", [True, False])
def test_explicit_recovery_or_healthy_deployment_never_consults_invalid_default(monkeypatch, explicit):
    cloud = Cloud()
    if explicit:
        plan = original_plan(cloud)
        maintenance(cloud)
        reviewed_plan(monkeypatch, plan)

    def invalid_default():
        raise AssertionError("Default selection must not be read for this deployment")

    monkeypatch.setattr(controller, "load_active_plan_id", invalid_default)
    deployer = cloud.deployer(input=inputs(deployment_id=RECOVERY, recovery_plan=ORIGINAL if explicit else ""))
    result = deployer.deploy()
    assert result["status"] == "verified"
    if explicit:
        assert result["recovery"]["selection"] == "explicit"
    else:
        assert "recovery" not in result


@pytest.mark.parametrize("drift", ["task_binding", "viewer_code", "maximum", "detached_task", "same_attempt", "running_task", "invalid_default"])
def test_default_recovery_rejects_drift_or_unconfirmed_hold_before_writes(monkeypatch, drift):
    cloud = Cloud()
    plan = original_plan(cloud)
    maintenance(cloud)
    reviewed_plan(monkeypatch, plan)
    if drift == "task_binding":
        plan["fleets"]["l1"]["task_definition_arn"] = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/clhear-fleet-l1:99"
    elif drift == "viewer_code":
        plan["viewer"]["code_sha256_base64"] = base64.b64encode(hashlib.sha256(b"wrong original").digest()).decode()
    elif drift == "maximum":
        plan["fleets"]["l1"]["max_capacity"] += 1
    elif drift == "detached_task":
        cloud.old_tasks["l4"] = ["active-old-one-off"]
    elif drift == "running_task":
        cloud.services["l4"]["runningCount"] = 1

    def selected_default():
        assert drift != "running_task", "A partially held deployment cannot select the default"
        if drift == "invalid_default":
            raise RecoveryPlanError("Invalid active recovery selection fields")
        return ORIGINAL

    monkeypatch.setattr(controller, "load_active_plan_id", selected_default)
    with pytest.raises(DeploymentError):
        cloud.deployer(input=inputs(deployment_id=ORIGINAL if drift == "same_attempt" else RECOVERY)).deploy()
    assert not cloud.mutations


@pytest.mark.parametrize("drift", ["task", "viewer_code", "maximum", "detached_task"])
def test_recovery_rejects_mismatched_plan_or_detached_writer_before_writes(monkeypatch, drift):
    cloud = Cloud()
    plan = original_plan(cloud)
    maintenance(cloud)
    if drift == "task":
        plan["fleets"]["l1"]["task_definition_arn"] = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/clhear-fleet-l1:99"
    elif drift == "viewer_code":
        plan["viewer"]["code_sha256_base64"] = base64.b64encode(hashlib.sha256(b"different-code").digest()).decode()
    elif drift == "maximum":
        plan["fleets"]["l0"]["max_capacity"] = 9
    else:
        cloud.old_tasks["l0"] = [f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/{CLUSTER}/detached-bootstrap"]
    reviewed_plan(monkeypatch, plan)
    with pytest.raises((DeploymentError, RecoveryPlanError)):
        cloud.deployer(input=inputs(deployment_id=RECOVERY, recovery_plan=ORIGINAL)).deploy()
    assert not cloud.mutations


@pytest.mark.parametrize("recover,fail", [(False, False), (True, False), (True, True)])
def test_cli_writes_private_exact_recovery_artifact_even_after_failed_recovery(monkeypatch, tmp_path, capsys, recover, fail):
    cloud = Cloud()
    plan = None
    if recover:
        plan = original_plan(cloud)
        maintenance(cloud)
        reviewed_plan(monkeypatch, plan)
    if fail:
        cloud.fail_action = "verify"
    selected = inputs(deployment_id=RECOVERY if recover else ORIGINAL, recovery_plan=ORIGINAL if recover else "")
    created = []

    def factory(actual_inputs):
        assert actual_inputs == selected
        deployer = cloud.deployer(input=actual_inputs)
        created.append(deployer)
        return deployer

    monkeypatch.setattr(controller, "Deployer", factory)
    output = tmp_path / "artifacts" / "deployment-result.json"
    argv = []
    for name in ("sha", "image", "ui_key", "ui_sha256", "ui_version", "deployment_id", "reviewer_emails"):
        argv.extend(["--" + name.replace("_", "-"), getattr(selected, name)])
    argv.extend(["--apply", "--output", str(output)])
    if recover:
        argv.extend(["--recovery-plan", ORIGINAL])
    assert controller.main(argv) == (1 if fail else 0)
    assert len(created) == 1
    report = json.loads(output.read_text())
    assert report["status"] == ("failed_maintenance" if fail else "verified")
    assert report["recovery_required"] is fail
    recovery_path = output.with_name("recovery-plan.json")
    recovery = json.loads(recovery_path.read_text())
    assert recovery == created[0].recovery_plan_output
    assert recovery["plan_id"] == selected.deployment_id
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(recovery_path.stat().st_mode) == 0o600
    if plan is not None:
        assert_targets(plan, recovery)
    if fail:
        assert cloud.concurrency == 0 and all(value["desiredCount"] == 0 for value in cloud.services.values())
    visible = output.read_text() + recovery_path.read_text() + capsys.readouterr().out
    assert not any(secret in visible for secret in ("private-env-value", "private-oauth-value", "test-only-private-session", "test-secret-not-for-logs"))
    assert_no_release_writes(cloud)
