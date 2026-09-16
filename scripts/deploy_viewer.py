"""Conditional presentation-only Lambda deployment; never operates CLHEAR workers.

The approved S3 release pointer and exact ZIP contents are independent gates.
Only application-code backup/upload, Lambda code update, and read-only probes
are permitted here. Configuration, concurrency, corpus and acceptance stay as-is.
"""
from __future__ import annotations

import argparse
import base64
import copy
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
import zipfile

import boto3

if __package__:
    from .deploy_l1 import (ACCOUNT, REGION, BUCKET, FUNCTION, WORKFLOW, QUEUES, RELEASES,
                           Deployer, DeploymentError, LAMBDA_UPDATE_OUTPUTS, require, _failure_details)
else:
    from deploy_l1 import (ACCOUNT, REGION, BUCKET, FUNCTION, WORKFLOW, QUEUES, RELEASES,
                          Deployer, DeploymentError, LAMBDA_UPDATE_OUTPUTS, require, _failure_details)

POINTER_KEY = "deployments/l1/viewer-release.json"
SNAPSHOT_URI = f"s3://{BUCKET}/webui/l1/candidate.db"
PREVIEW_FUNCTION = "clhear-preview-webui"
PREVIEW_WORKFLOW = "Reg42-ai/CLHEAR-MVP/.github/workflows/deploy-preview.yml@refs/heads/main"
MAX_CODE_BYTES = 300 * 1024 * 1024
MAX_EXPANDED_BYTES = 240 * 1024 * 1024
PRESENTATION_SUFFIXES = {".html", ".css", ".js", ".mjs", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2"}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def code_hash(digest):
    return base64.b64encode(bytes.fromhex(digest)).decode()


def _read(stream, limit):
    try:
        data = stream.read(limit + 1)
        require(0 < len(data) <= limit, "Artifact has an invalid size")
        return data
    finally:
        stream.close()


def backend_manifest(data):
    """Compare exact file identities/content without extracting any ZIP member."""
    require(0 < len(data) <= MAX_CODE_BYTES, "Code artifact has an invalid size")
    manifest, names, expanded = {}, set(), 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            require(len(archive.infolist()) <= 20000, "Code artifact contains too many entries")
            for item in archive.infolist():
                name = item.filename
                path = PurePosixPath(name)
                require(name and not name.startswith("/") and "\\" not in name and "\x00" not in name
                        and str(path) == name.rstrip("/") and ".." not in path.parts
                        and name not in names and not stat.S_ISLNK(item.external_attr >> 16)
                        and not item.flag_bits & 1, "Code artifact contains an unsafe or duplicate entry")
                names.add(name)
                if item.is_dir():
                    continue
                expanded += item.file_size
                require(expanded <= MAX_EXPANDED_BYTES, "Expanded code artifact exceeds its size budget")
                body = archive.read(item)  # Also verifies the CRC of presentation files.
                require(len(body) == item.file_size, "Code artifact entry is truncated")
                presentation = (name.startswith("app/clhear/web/") and path.suffix.lower() in PRESENTATION_SUFFIXES
                                and not any(part.startswith(".") for part in path.parts))
                if not presentation:
                    manifest[name] = sha256(body)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        if isinstance(exc, DeploymentError):
            raise
        raise DeploymentError("Code artifact is not a valid supported ZIP") from exc
    require({"app/main.py", "app/clhear/lambda_web.py"} <= manifest.keys(), "Code artifact lacks the viewer application")
    return manifest


@dataclass(frozen=True)
class Inputs:
    target: str
    sha: str
    worker_sha: str
    baseline_sha: str
    baseline_key: str
    baseline_version: str
    baseline_sha256: str
    ui_key: str
    ui_version: str
    ui_sha256: str
    deployment_id: str

    def validate(self):
        require(self.target in {"live", "preview"}, "Unknown viewer deployment target")
        for value in (self.sha, self.worker_sha, self.baseline_sha):
            require(bool(re.fullmatch(r"[0-9a-f]{40}", value)), "Full source and baseline commit identifiers are required")
        for value in (self.ui_sha256, self.baseline_sha256):
            require(bool(re.fullmatch(r"[0-9a-f]{64}", value)), "Full code artifact hashes are required")
        for key, version in ((self.ui_key, self.ui_version), (self.baseline_key, self.baseline_version)):
            prefix = "(?:deployments|previews)" if self.target == "preview" and key == self.ui_key else "deployments"
            require(bool(re.fullmatch(rf"webui/{prefix}/[A-Za-z0-9/_-]+/webui\.zip", key)) and ".." not in key,
                    "Viewer code must use the immutable deployment artifact prefix")
            require(isinstance(version, str) and version not in {"", "null"}, "Versioned code artifacts are required")
        require(bool(re.fullmatch(r"viewer-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", self.deployment_id)),
                "Invalid viewer deployment identifier")


class ViewerDeployer:
    def __init__(self, inputs: Inputs, clients=None, *, environ=None, code_fetch=None):
        self.inputs = inputs
        self.env = dict(os.environ if environ is None else environ)
        session = None if clients is not None else boto3.Session(region_name=REGION)
        self.clients = clients if clients is not None else {name: session.client(name, region_name=REGION) for name in ("sts", "s3", "lambda")}
        self.code_fetch = code_fetch or Deployer._download_code
        self.function = FUNCTION if inputs.target == "live" else PREVIEW_FUNCTION
        self.authorized = False
        self.state = None
        self.report = {"deployment_id": inputs.deployment_id, "target": inputs.target, "function": self.function,
                       "sha": inputs.sha, "viewer_sha": inputs.sha, "worker_sha": inputs.worker_sha,
                       "trusted_workflow_sha": self.env.get("GITHUB_SHA"), "status": "not_started",
                       "accepted_release_changed": False, "worker_verification": "not_run_presentation_only",
                       "nightly_schedule_validation": "not_run_presentation_only", "snapshot_changed": False,
                       "ui": {"key": inputs.ui_key, "version": inputs.ui_version, "sha256": inputs.ui_sha256}}

    def _guard(self):
        preview = self.inputs.target == "preview"
        role = self.env.get("CLHEAR_PREVIEW_DEPLOY_ROLE_ARN" if preview else "CLHEAR_DEPLOY_ROLE_ARN", "")
        require(bool(re.fullmatch(rf"arn:aws:iam::{ACCOUNT}:role/[A-Za-z0-9+=,.@_/-]+", role)), "An approved viewer deployment role is required")
        require(self.env.get("GITHUB_ACTIONS") == "true" and self.env.get("GITHUB_REPOSITORY") == "Reg42-ai/CLHEAR-MVP"
                and self.env.get("GITHUB_REF") == "refs/heads/main"
                and bool(re.fullmatch(r"[0-9a-f]{40}", self.env.get("GITHUB_SHA", "")))
                and (preview or self.env["GITHUB_SHA"] == self.inputs.sha)
                and self.env.get("GITHUB_WORKFLOW_REF") == (PREVIEW_WORKFLOW if preview else WORKFLOW)
                and self.env.get("CLHEAR_DEPLOY_ENVIRONMENT") == ("clhear-preview" if preview else "clhear-l1")
                and self.env.get("ACTIONS_ID_TOKEN_REQUEST_URL") and self.env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN"),
                "Viewer apply requires its pinned main workflow and protected environment")
        identity = self.clients["sts"].get_caller_identity()
        expected = f"arn:aws:sts::{ACCOUNT}:assumed-role/{role.rsplit('/', 1)[1]}/"
        require(identity.get("Account") == ACCOUNT and identity.get("Arn", "").startswith(expected), "The active viewer deployment role is incorrect")
        self.authorized = True

    def _write(self, service, operation, **kwargs):
        require(self.authorized and (service, operation) in {
            ("s3", "put_object"), ("lambda", "update_function_code"), ("lambda", "invoke")},
            "Viewer deployment cannot modify this resource")
        return getattr(self.clients[service], operation)(**kwargs)

    def _pointer(self):
        response = self.clients["s3"].get_object(Bucket=BUCKET, Key=POINTER_KEY, ExpectedBucketOwner=ACCOUNT)
        data = _read(response["Body"], 16384)
        def unique(pairs):
            value = {}
            for key, item in pairs:
                require(key not in value, "Release pointer has duplicate fields")
                value[key] = item
            return value
        try:
            pointer = json.loads(data, object_pairs_hook=unique)
        except (ValueError, TypeError):
            raise DeploymentError("Release pointer is invalid") from None
        require(isinstance(pointer, dict) and set(pointer) == {"schema", "viewer_sha", "worker_sha", "ui", "full_deployment_id", "viewer_deployment_id"}
                and pointer.get("schema") == "clhear.viewer-release.v1", "A verified viewer release pointer is required")
        require(pointer["viewer_sha"] == self.inputs.baseline_sha and pointer["worker_sha"] == self.inputs.worker_sha
                and pointer["ui"] == {"key": self.inputs.baseline_key, "version": self.inputs.baseline_version, "sha256": self.inputs.baseline_sha256},
                "Requested baseline does not match the authoritative viewer release")
        require(bool(re.fullmatch(r"l1-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", str(pointer["full_deployment_id"])))
                and bool(re.fullmatch(r"(?:l1|viewer)-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", str(pointer["viewer_deployment_id"]))),
                "Release pointer lacks verified deployment identities")
        require(isinstance(response.get("ETag"), str) and response["ETag"], "Release pointer lacks a concurrency token")
        return pointer, response["ETag"]

    def _artifact(self, key, version, digest, commit):
        response = self.clients["s3"].get_object(Bucket=BUCKET, Key=key, VersionId=version, ExpectedBucketOwner=ACCOUNT)
        data = _read(response["Body"], MAX_CODE_BYTES)
        require(response.get("VersionId") == version and response.get("Metadata", {}).get("git-sha") == commit
                and sha256(data) == digest, "Versioned code bytes or source metadata differ from the approved artifact")
        return data

    def _concurrency(self):
        result = self.clients["lambda"].get_function_concurrency(FunctionName=self.function)
        value = result.get("ReservedConcurrentExecutions")
        require("ReservedConcurrentExecutions" not in result or (type(value) is int and value > 0), "Viewer-only deployment cannot resume a paused viewer")
        return value

    def _configuration(self, config):
        require(config.get("FunctionName") == self.function and config.get("FunctionArn") == f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{self.function}"
                and config.get("State") == "Active" and config.get("LastUpdateStatus") == "Successful"
                and config.get("PackageType") == "Zip" and config.get("Handler") == "app.clhear.lambda_web.handler"
                and config.get("Runtime") == "python3.12" and config.get("Architectures") == ["x86_64"]
                and config.get("Role", "").startswith(f"arn:aws:iam::{ACCOUNT}:role/")
                and isinstance(config.get("RevisionId"), str) and config["RevisionId"], "Viewer runtime must already be healthy and supported")
        env = config.get("Environment", {}).get("Variables", {})
        require(env.get("CLHEAR_RESTRICTED_ACCESS") == "true" and env.get("CLHEAR_AUTH_DEBUG") == "false"
                and env.get("CLHEAR_DB_S3_URI") == SNAPSHOT_URI,
                "Viewer requires the existing restricted candidate snapshot configuration")
        secret = env.get("CLHEAR_SESSION_SECRET", "").strip()
        require(len(secret) >= 32 and "CHANGEME" not in secret.upper(), "Viewer requires an existing private session secret")
        reviewers = env.get("CLHEAR_REVIEWER_EMAILS", "")
        require(reviewers and all(re.fullmatch(r"[^\s,@]+@[^\s,@]+\.[^\s,@]+", item.strip()) for item in reviewers.split(",")), "Viewer requires an explicit reviewer allowlist")
        require(env.get("CLHEAR_SES_SENDER") or (env.get("GOOGLE_OAUTH_CLIENT_ID") and env.get("GOOGLE_OAUTH_CLIENT_SECRET"))
                or (env.get("CLHEAR_COGNITO_CLIENT_ID") and env.get("CLHEAR_COGNITO_USER_POOL_ID") and env.get("CLHEAR_COGNITO_DOMAIN")), "Viewer requires existing production authentication")
        if self.inputs.target == "preview":
            require(env.get("CLHEAR_PREVIEW_MODE") == "true" and not env.get("CLHEAR_EVENTS_QUEUE_URL")
                    and not env.get("CLHEAR_RELEASES_S3_PREFIX"), "Preview must already disable writes and use only its candidate snapshot")
        else:
            require(env.get("CLHEAR_PREVIEW_MODE", "false") == "false" and env.get("CLHEAR_EVENTS_QUEUE_URL") == QUEUES["l0"]
                    and env.get("CLHEAR_RELEASES_S3_PREFIX") == RELEASES, "Live viewer routing must remain unchanged")

    def _unchanged(self, expected):
        current = self.clients["lambda"].get_function_configuration(FunctionName=self.function)
        require({k: v for k, v in current.items() if k != "ResponseMetadata"} == {k: v for k, v in expected.items() if k != "ResponseMetadata"},
                "Viewer code or configuration changed outside this deployment")
        require(self._concurrency() == self.state["concurrency"], "Viewer concurrency changed outside this deployment")
        return current

    def preflight(self):
        self.inputs.validate()
        require(self.clients["sts"].get_caller_identity().get("Account") == ACCOUNT, "Wrong AWS account")
        private = self.clients["s3"].get_public_access_block(Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT).get("PublicAccessBlockConfiguration", {})
        require(all(private.get(key) is True for key in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")), "Deployment bucket must remain private")
        require(self.clients["s3"].get_bucket_versioning(Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT).get("Status") == "Enabled", "Deployment bucket must have versioning")
        pointer, etag = self._pointer()
        candidate = self._artifact(self.inputs.ui_key, self.inputs.ui_version, self.inputs.ui_sha256, self.inputs.sha)
        baseline = self._artifact(self.inputs.baseline_key, self.inputs.baseline_version, self.inputs.baseline_sha256, self.inputs.baseline_sha)
        expected = backend_manifest(baseline)
        require(backend_manifest(candidate) == expected, "Candidate changes backend code or dependencies; use a full deployment")
        function = self.clients["lambda"].get_function(FunctionName=self.function)
        current = function["Configuration"]
        self._configuration(current)
        concurrency = self._concurrency()
        old = self.code_fetch(function.get("Code", {}).get("Location", ""))
        require(code_hash(sha256(old)) == current.get("CodeSha256"), "Current viewer ZIP does not match the live Lambda hash")
        backend_refreshed = backend_manifest(old) != expected
        if self.inputs.target == "live":
            require(not backend_refreshed, "Existing viewer backend differs from the approved baseline; use a full deployment")
            require(current["CodeSha256"] == code_hash(self.inputs.baseline_sha256), "Live viewer differs from the authoritative release pointer")
        self.state = {"configuration": copy.deepcopy(current), "concurrency": concurrency, "old_code": old, "pointer": pointer, "pointer_etag": etag}
        self._unchanged(current)
        self.report.update(status="preflight_passed", baseline_pointer_etag=etag, baseline=pointer,
                           full_deployment_id=pointer["full_deployment_id"], current_code_sha256=sha256(old),
                           backend_manifest_sha256=sha256(json.dumps(expected, sort_keys=True).encode()),
                           backend_files_verified=len(expected), snapshot_uri=SNAPSHOT_URI,
                           preview_backend_refreshed=self.inputs.target == "preview" and backend_refreshed,
                           authenticated_browser_verification="pending")
        return self.report

    def _complete(self, before, response, *, phase, digest):
        return Deployer._complete_lambda_update(self, before, response, phase=phase,
                expected_code_hash=code_hash(digest), function_name=self.function)

    def _probe(self):
        for path, status in (("/api/clhear/health", 200), ("/signin", 200), ("/api/clhear/sources", 401)):
            payload = {"version": "2.0", "routeKey": "$default", "rawPath": path, "rawQueryString": "", "headers": {"accept": "application/json"},
                       "requestContext": {"http": {"method": "GET", "path": path, "protocol": "HTTP/1.1", "sourceIp": "127.0.0.1", "userAgent": "clhear-viewer-deployment"}}}
            response = self._write("lambda", "invoke", FunctionName=self.function, InvocationType="RequestResponse", Payload=json.dumps(payload).encode())
            result = json.loads(_read(response["Payload"], 2 * 1024 * 1024))
            require(not response.get("FunctionError") and result.get("statusCode") == status, "Viewer read/access probe did not pass")
        self.report["read_access_probes"] = "passed"

    def _rollback(self):
        # An accepted-but-unconfirmed update must not retain the old preflight
        # hash as a claim about the currently executing code.
        self.report["current_code_sha256"] = None
        try:
            current = self.clients["lambda"].get_function_configuration(FunctionName=self.function)
            require(current.get("CodeSha256") == code_hash(self.inputs.ui_sha256), "Code drift prevents conditional viewer rollback")
            # Never undo someone else's configuration or concurrency change.
            require(self._concurrency() == self.state["concurrency"], "Concurrency drift prevents conditional viewer rollback")
            ignored = LAMBDA_UPDATE_OUTPUTS | {"CodeSha256", "CodeSize", "SigningJobArn", "SigningProfileVersionArn"}
            require({k: v for k, v in current.items() if k not in ignored} == {k: v for k, v in self.state["configuration"].items() if k not in ignored}, "Configuration drift prevents conditional viewer rollback")
            updated = self._write("lambda", "update_function_code", FunctionName=self.function, S3Bucket=BUCKET,
                S3Key=self.state["backup_key"], S3ObjectVersion=self.state["backup_version"], RevisionId=current["RevisionId"], Publish=False)
            completed = self._complete(current, updated, phase="rollback", digest=sha256(self.state["old_code"]))
            self._unchanged(completed)
            self.report.update(rollback="verified", current_code_sha256=sha256(self.state["old_code"]))
        except Exception as error:
            self.report.update(rollback="not_verified", rollback_error=_failure_details(error), recovery_required=True)

    def deploy(self):
        self._guard()
        self.preflight()
        started = time.monotonic()
        try:
            prefix = "deployments/l1" if self.inputs.target == "live" else "webui/previews"
            key = f"{prefix}/{self.inputs.deployment_id}/previous-viewer.zip"
            backup = self._write("s3", "put_object", Bucket=BUCKET, Key=key, Body=self.state["old_code"],
                IfNoneMatch="*", ServerSideEncryption="AES256", ExpectedBucketOwner=ACCOUNT)
            require(backup.get("VersionId") not in {None, "", "null"}, "Previous code backup must have an immutable version")
            self.state.update(backup_key=key, backup_version=backup["VersionId"])
            pointer, etag = self._pointer()
            require(pointer == self.state["pointer"] and etag == self.state["pointer_etag"], "Approved release changed during viewer preflight")
            current = self._unchanged(self.state["configuration"])
            # Mark uncertainty before the request: a timeout may follow acceptance.
            self.state["update_attempted"] = True
            updated = self._write("lambda", "update_function_code", FunctionName=self.function, S3Bucket=BUCKET,
                S3Key=self.inputs.ui_key, S3ObjectVersion=self.inputs.ui_version, RevisionId=current["RevisionId"], Publish=False)
            completed = self._complete(current, updated, phase="code", digest=self.inputs.ui_sha256)
            self._unchanged(completed)
            self._probe()
            self._unchanged(completed)
            self.report.update(status="verified", current_code_sha256=self.inputs.ui_sha256, completed_revision=completed["RevisionId"],
                               recovery_required=False, traffic_policy="unchanged", fleet_policy="unchanged")
        except Exception as error:
            self.report.update(status="failed", **_failure_details(error))
            if self.state.get("update_attempted"):
                self._rollback()
        finally:
            self.report["duration_ms"] = round((time.monotonic() - started) * 1000)
        return self.report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in Inputs.__dataclass_fields__:
        parser.add_argument("--" + name.replace("_", "-"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    deployer = ViewerDeployer(Inputs(**{name: getattr(args, name) for name in Inputs.__dataclass_fields__}))
    try:
        report = deployer.deploy() if args.apply else deployer.preflight()
    except Exception as error:
        report = {**deployer.report, "status": "preflight_failed", **_failure_details(error)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    args.output.chmod(0o600)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in {"verified", "preflight_passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
