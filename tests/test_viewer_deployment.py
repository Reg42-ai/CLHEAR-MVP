"""Viewer cutovers use real ZIPs and the SDK's asynchronous Lambda waiter model."""
import copy
import io
import json
import zipfile

import pytest
from botocore.exceptions import ClientError

from scripts import deploy_viewer as viewer
from tests.test_l1_deployment import Cloud, environment

MAIN_SHA, CANDIDATE_SHA, WORKER_SHA = "a" * 40, "b" * 40, "c" * 40
BASE_KEY = f"webui/deployments/{MAIN_SHA}/100-1/webui.zip"
NEXT_KEY = f"webui/deployments/{CANDIDATE_SHA}/101-1/webui.zip"
FILES = {"app/main.py": b"trusted main", "app/clhear/lambda_web.py": b"trusted handler",
         "migrations/m0026.py": b"trusted migration", "dependency/__init__.py": b"locked dependency",
         "app/clhear/web/stack.html": b"old presentation", "app/clhear/web/theme.css": b"old style"}


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return output.getvalue()


class ViewerCloud(Cloud):
    def __init__(self, *, preview=False):
        super().__init__()
        self.preview = preview
        self.baseline = archive(FILES)
        self.candidate = archive(FILES | {"app/clhear/web/stack.html": b"new presentation"})
        self.current_code = self.baseline
        self.artifacts = {BASE_KEY: (self.baseline, "baseline-version", MAIN_SHA)}
        self.next_key = NEXT_KEY if not preview else f"webui/previews/{CANDIDATE_SHA}/101-1/webui.zip"
        self.artifacts[self.next_key] = (self.candidate, "candidate-version", CANDIDATE_SHA)
        self.pointer = {"schema": "clhear.viewer-release.v1", "viewer_sha": MAIN_SHA, "worker_sha": WORKER_SHA,
            "ui": {"key": BASE_KEY, "version": "baseline-version", "sha256": viewer.sha256(self.baseline)},
            "full_deployment_id": "l1-100-1", "viewer_deployment_id": "l1-100-1"}
        self.pointer_etag = '"pointer-one"'
        function = viewer.PREVIEW_FUNCTION if preview else viewer.FUNCTION
        self.config.update(FunctionName=function, FunctionArn=f"arn:aws:lambda:{viewer.REGION}:{viewer.ACCOUNT}:function:{function}",
            Runtime="python3.12", Architectures=["x86_64"], CodeSha256=viewer.code_hash(viewer.sha256(self.current_code)))
        env = self.config["Environment"]["Variables"]
        env.update(CLHEAR_RESTRICTED_ACCESS="true", CLHEAR_AUTH_DEBUG="false", CLHEAR_DB_S3_URI=viewer.SNAPSHOT_URI,
                   CLHEAR_REVIEWER_EMAILS="reviewer@example.test", CLHEAR_EVENTS_QUEUE_URL=viewer.QUEUES["l0"],
                   CLHEAR_RELEASES_S3_PREFIX=viewer.RELEASES)
        if preview:
            env.update(CLHEAR_PREVIEW_MODE="true", CLHEAR_EVENTS_QUEUE_URL="", CLHEAR_RELEASES_S3_PREFIX="")
            self.role = f"arn:aws:sts::{viewer.ACCOUNT}:assumed-role/clhear-github-preview/github-101"
        self.hook = None
        self.update_calls = 0

    def call(self, service, operation, args):
        if self.hook:
            self.hook(service, operation, args)
        if "FunctionName" in args:
            assert args["FunctionName"] == self.config["FunctionName"]
        if operation == "get_object":
            self.calls.append((service, operation, copy.deepcopy(args)))
            assert args["Bucket"] == viewer.BUCKET and args["ExpectedBucketOwner"] == viewer.ACCOUNT
            if args["Key"] == viewer.POINTER_KEY:
                return {"Body": io.BytesIO(json.dumps(self.pointer).encode()), "ETag": self.pointer_etag}
            data, version, commit = self.artifacts[args["Key"]]
            assert args["VersionId"] == version
            return {"Body": io.BytesIO(data), "VersionId": version, "Metadata": {"git-sha": commit}}
        if operation == "update_function_code":
            self.calls.append((service, operation, copy.deepcopy(args)))
            self.require_no_lambda_update("UpdateFunctionCode")
            if args["RevisionId"] != self.config["RevisionId"]:
                raise ClientError({"Error": {"Code": "PreconditionFailedException", "Message": "private AWS details"}}, "UpdateFunctionCode")
            self.update_calls += 1
            rollback = args["S3Key"].endswith("previous-viewer.zip")
            data = self.objects[args["S3Key"]] if rollback else self.artifacts[args["S3Key"]][0]
            self.current_code = data
            self.config.update(CodeSha256=viewer.code_hash(viewer.sha256(data)), CodeSize=len(data))
            return self.start_lambda_update("rollback" if rollback else "code")
        if operation == "invoke":
            self.calls.append((service, operation, copy.deepcopy(args)))
            path = json.loads(args["Payload"])["rawPath"]
            status = self.anonymous_status if path == "/api/clhear/sources" else 200
            return {"Payload": io.BytesIO(json.dumps({"statusCode": status, "body": "do not persist private content"}).encode())}
        return super().call(service, operation, args)

    def inputs(self, **overrides):
        values = dict(target="preview" if self.preview else "live", sha=CANDIDATE_SHA, worker_sha=WORKER_SHA,
            baseline_sha=MAIN_SHA, baseline_key=BASE_KEY, baseline_version="baseline-version", baseline_sha256=viewer.sha256(self.baseline),
            ui_key=self.next_key, ui_version="candidate-version", ui_sha256=viewer.sha256(self.candidate), deployment_id="viewer-101-1")
        return viewer.Inputs(**(values | overrides))

    def deployer(self, *, inputs=None, env=None):
        context = environment(GITHUB_SHA=CANDIDATE_SHA)
        if self.preview:
            context.update(GITHUB_SHA=MAIN_SHA, GITHUB_WORKFLOW_REF=viewer.PREVIEW_WORKFLOW,
                CLHEAR_DEPLOY_ENVIRONMENT="clhear-preview",
                CLHEAR_PREVIEW_DEPLOY_ROLE_ARN=f"arn:aws:iam::{viewer.ACCOUNT}:role/clhear-github-preview")
        return viewer.ViewerDeployer(inputs or self.inputs(), self.clients, environ=context | (env or {}), code_fetch=lambda _: self.current_code)


@pytest.mark.parametrize("preview", [False, True])
def test_preflight_compares_exact_artifacts_without_writes(preview):
    cloud = ViewerCloud(preview=preview)
    result = cloud.deployer().preflight()
    assert result["status"] == "preflight_passed" and result["backend_files_verified"] == 4
    assert not cloud.mutations


@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize("reservation", [None, 2])
def test_viewer_only_update_preserves_every_other_resource(preview, reservation):
    cloud = ViewerCloud(preview=preview)
    cloud.concurrency = reservation
    before = copy.deepcopy(cloud.config)
    result = cloud.deployer().deploy()
    assert result["status"] == "verified" and result["current_code_sha256"] == viewer.sha256(cloud.candidate)
    assert result["viewer_sha"] == CANDIDATE_SHA and result["worker_sha"] == WORKER_SHA
    assert result["baseline"] == cloud.pointer and result["baseline_pointer_etag"] == cloud.pointer_etag
    assert result["accepted_release_changed"] is False and result["snapshot_changed"] is False
    assert result["worker_verification"] == "not_run_presentation_only"
    assert result["nightly_schedule_validation"] == "not_run_presentation_only"
    assert cloud.config["Environment"] == before["Environment"] and cloud.concurrency == reservation
    assert cloud.config["Role"] == before["Role"]
    assert {(service, op) for service, op, _ in cloud.mutations} == {("s3", "put_object"), ("lambda", "update_function_code"), ("lambda", "invoke")}
    assert {service for service, _, _ in cloud.calls} <= {"sts", "s3", "lambda"}
    assert result["lambda_updates"][0]["revision_transition"] is True
    prefix = "webui/previews" if preview else "deployments/l1"
    assert set(cloud.objects) == {f"{prefix}/viewer-101-1/previous-viewer.zip"}
    assert "private-session" not in json.dumps(result) and "private-oauth" not in json.dumps(result)
    assert "do not persist private content" not in json.dumps(result)


@pytest.mark.parametrize("file,content", [("app/main.py", b"changed"), ("dependency/__init__.py", b"changed"),
    ("migrations/m0027.py", b"added"), ("app/clhear/web/new_server.py", b"Python in a presentation path")])
def test_backend_or_dependency_change_cannot_use_fast_lane(file, content):
    cloud = ViewerCloud()
    cloud.candidate = archive(FILES | {file: content})
    cloud.artifacts[cloud.next_key] = (cloud.candidate, "candidate-version", CANDIDATE_SHA)
    with pytest.raises(viewer.DeploymentError, match="backend"):
        cloud.deployer().deploy()
    assert not cloud.mutations


def test_deleted_backend_file_is_rejected():
    cloud = ViewerCloud()
    cloud.candidate = archive({k: v for k, v in FILES.items() if k != "dependency/__init__.py"})
    cloud.artifacts[cloud.next_key] = (cloud.candidate, "candidate-version", CANDIDATE_SHA)
    with pytest.raises(viewer.DeploymentError, match="backend"):
        cloud.deployer().preflight()


@pytest.mark.parametrize("path", ["../app/main.py", "/app/main.py", "app//main.py", "app/./main.py", "app\\main.py"])
def test_zip_path_aliases_cannot_hide_backend_changes(path):
    with pytest.raises(viewer.DeploymentError, match="unsafe"):
        viewer.backend_manifest(archive(FILES | {path: b"ambiguous"}))


def test_duplicate_zip_entry_cannot_hide_original_backend():
    data = io.BytesIO(archive(FILES))
    with pytest.warns(UserWarning), zipfile.ZipFile(data, "a") as bundle:
        bundle.writestr("app/main.py", b"changed")
    with pytest.raises(viewer.DeploymentError, match="duplicate"):
        viewer.backend_manifest(data.getvalue())


@pytest.mark.parametrize("overrides", [{"worker_sha": "d" * 40}, {"baseline_sha": "d" * 40}, {"baseline_version": "other"}, {"baseline_sha256": "d" * 64}])
def test_caller_cannot_substitute_authoritative_baseline(overrides):
    cloud = ViewerCloud()
    with pytest.raises(viewer.DeploymentError, match="authoritative"):
        cloud.deployer(inputs=cloud.inputs(**overrides)).deploy()
    assert not cloud.mutations


@pytest.mark.parametrize("setting,value", [("CLHEAR_RESTRICTED_ACCESS", "false"), ("CLHEAR_AUTH_DEBUG", "true"),
    ("CLHEAR_DB_S3_URI", "s3://other/snapshot.db"), ("CLHEAR_SESSION_SECRET", "short"),
    ("CLHEAR_REVIEWER_EMAILS", ""), ("CLHEAR_EVENTS_QUEUE_URL", "other"), ("CLHEAR_RELEASES_S3_PREFIX", "other")])
def test_existing_auth_snapshot_and_routing_are_required_before_writes(setting, value):
    cloud = ViewerCloud()
    cloud.config["Environment"]["Variables"][setting] = value
    with pytest.raises(viewer.DeploymentError):
        cloud.deployer().deploy()
    assert not cloud.mutations


@pytest.mark.parametrize("setting,value", [("CLHEAR_PREVIEW_MODE", "false"), ("CLHEAR_EVENTS_QUEUE_URL", viewer.QUEUES["l0"]), ("CLHEAR_RELEASES_S3_PREFIX", viewer.RELEASES)])
def test_preview_must_be_read_only_before_update(setting, value):
    cloud = ViewerCloud(preview=True)
    cloud.config["Environment"]["Variables"][setting] = value
    with pytest.raises(viewer.DeploymentError):
        cloud.deployer().deploy()
    assert not cloud.mutations


@pytest.mark.parametrize("env", [{"GITHUB_REF": "refs/heads/feature"}, {"GITHUB_SHA": MAIN_SHA},
    {"GITHUB_WORKFLOW_REF": viewer.PREVIEW_WORKFLOW}, {"CLHEAR_DEPLOY_ENVIRONMENT": "clhear-preview"},
    {"GITHUB_REPOSITORY": "untrusted/repo"}, {"GITHUB_ACTIONS": "false"}, {"ACTIONS_ID_TOKEN_REQUEST_TOKEN": ""}])
def test_live_context_cannot_be_spoofed(env):
    cloud = ViewerCloud()
    with pytest.raises(viewer.DeploymentError):
        cloud.deployer(env=env).deploy()
    assert not cloud.mutations


def test_paused_target_is_not_resumed():
    cloud = ViewerCloud()
    cloud.concurrency = 0
    with pytest.raises(viewer.DeploymentError, match="paused"):
        cloud.deployer().deploy()
    assert not cloud.mutations and cloud.concurrency == 0


def test_preview_old_backend_can_refresh_only_from_the_approved_stable_artifact():
    cloud = ViewerCloud(preview=True)
    cloud.current_code = archive(FILES | {"app/main.py": b"older preview backend"})
    cloud.config["CodeSha256"] = viewer.code_hash(viewer.sha256(cloud.current_code))
    original = cloud.current_code
    result = cloud.deployer().deploy()
    assert result["status"] == "verified" and result["preview_backend_refreshed"] is True
    assert cloud.objects["webui/previews/viewer-101-1/previous-viewer.zip"] == original
    assert cloud.current_code == cloud.candidate


def test_live_backend_drift_requires_full_deployment():
    cloud = ViewerCloud()
    cloud.current_code = archive(FILES | {"app/main.py": b"different backend"})
    cloud.config["CodeSha256"] = viewer.code_hash(viewer.sha256(cloud.current_code))
    with pytest.raises(viewer.DeploymentError, match="backend differs"):
        cloud.deployer().deploy()
    assert not cloud.mutations


def test_downloaded_current_zip_must_match_aws_reported_code_hash():
    cloud = ViewerCloud(preview=True)
    cloud.current_code = archive(FILES | {"app/main.py": b"mismatched download"})
    with pytest.raises(viewer.DeploymentError, match="live Lambda hash"):
        cloud.deployer().deploy()
    assert not cloud.mutations


@pytest.mark.parametrize("drift", ["code", "configuration", "concurrency", "pointer"])
def test_preupdate_drift_does_not_write_lambda(drift):
    cloud = ViewerCloud()
    def hook(service, operation, args):
        if operation == "put_object":
            if drift == "code":
                cloud.config["CodeSha256"] = viewer.code_hash("f" * 64)
            elif drift == "configuration":
                cloud.config["Environment"]["Variables"]["UNEXPECTED"] = "private"
            elif drift == "concurrency":
                cloud.concurrency = 3
            else:
                cloud.pointer_etag = '"new pointer"'
    cloud.hook = hook
    result = cloud.deployer().deploy()
    assert result["status"] == "failed" and cloud.update_calls == 0


def test_probe_failure_rolls_back_with_completed_revision_and_no_other_mutation():
    cloud = ViewerCloud()
    cloud.anonymous_status = 200
    result = cloud.deployer().deploy()
    assert result["status"] == "failed" and result["rollback"] == "verified"
    assert cloud.current_code == cloud.baseline and cloud.concurrency is None
    updates = [args for _, op, args in cloud.mutations if op == "update_function_code"]
    assert updates[1]["RevisionId"].endswith("code-in-progress-completed")
    assert {phase["phase"] for phase in result["lambda_updates"]} == {"code", "rollback"}


@pytest.mark.parametrize("mode", ["waiter_failed", "waiter_timeout"])
def test_async_failure_or_timeout_is_not_a_success(mode):
    cloud = ViewerCloud()
    cloud.lambda_failure_modes["code"] = mode
    result = cloud.deployer().deploy()
    assert result["status"] == "failed"
    if mode == "waiter_failed":
        assert result["rollback"] == "verified" and cloud.current_code == cloud.baseline
    else:
        assert result["rollback"] == "not_verified" and result["recovery_required"] is True
        assert cloud.current_code == cloud.candidate  # Pending update rejects rollback before mutation.
    assert not any(op in {"put_function_concurrency", "delete_function_concurrency", "update_function_configuration"} for _, op, _ in cloud.calls)


@pytest.mark.parametrize("drift", ["code", "configuration", "concurrency"])
def test_postupdate_drift_is_reported_without_overwriting_another_operators_change(drift):
    cloud = ViewerCloud()
    def hook(service, operation, args):
        if operation == "invoke":
            if drift == "code":
                cloud.config["CodeSha256"] = viewer.code_hash("f" * 64)
            elif drift == "configuration":
                cloud.config["Environment"]["Variables"]["UNEXPECTED"] = "private"
            else:
                cloud.concurrency = 3
            cloud.config["RevisionId"] += "-operator"
    cloud.hook = hook
    result = cloud.deployer().deploy()
    assert result["status"] == "failed" and result["rollback"] == "not_verified"
    assert cloud.update_calls == 1 and result["recovery_required"] is True
    assert result["current_code_sha256"] is None


@pytest.mark.parametrize("drift", ["code", "configuration", "status"])
def test_completion_must_prove_code_configuration_and_active_state(drift):
    cloud = ViewerCloud()
    changed = False
    def hook(service, operation, args):
        nonlocal changed
        if operation == "get_function_configuration" and cloud.update_calls == 1 and cloud.pending_lambda_update is None and not changed:
            changed = True
            if drift == "code":
                cloud.config["CodeSha256"] = viewer.code_hash("f" * 64)
            elif drift == "configuration":
                cloud.config["MemorySize"] = 2048
            else:
                cloud.config["State"] = "Inactive"
    cloud.hook = hook
    result = cloud.deployer().deploy()
    assert result["status"] == "failed"
    assert result["lambda_updates"][0]["result"] == {"code": "mismatch_code", "configuration": "mismatch_configuration", "status": "mismatch_status"}[drift]
    assert not any(op == "invoke" for _, op, _ in cloud.calls)


def test_last_revision_compare_and_swap_rejects_race_before_code_write():
    cloud = ViewerCloud()
    def hook(service, operation, args):
        if operation == "update_function_code" and cloud.update_calls == 0:
            cloud.config["RevisionId"] += "-operator"
    cloud.hook = hook
    result = cloud.deployer().deploy()
    assert result["status"] == "failed" and result["failure_code"] == "PreconditionFailedException"
    assert cloud.update_calls == 0 and cloud.current_code == cloud.baseline


def test_same_revision_at_completion_is_valid_too():
    cloud = ViewerCloud()
    cloud.advance_completion_revision = False
    result = cloud.deployer().deploy()
    assert result["status"] == "verified" and result["lambda_updates"][0]["revision_transition"] is False


def test_rollback_failure_never_reports_old_code_as_current():
    cloud = ViewerCloud()
    cloud.anonymous_status = 200
    cloud.lambda_failure_modes["rollback"] = "waiter_failed"
    result = cloud.deployer().deploy()
    assert result["status"] == "failed" and result["rollback"] == "not_verified"
    assert result["current_code_sha256"] is None and result["recovery_required"] is True
