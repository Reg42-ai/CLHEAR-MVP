import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from scripts import viewer_release as release


def commit():
    subprocess.run(["git", "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "test"], check=True)
    return release.git("rev-parse", "HEAD").decode().strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    web = Path("app/clhear/web")
    web.mkdir(parents=True)
    (web / "l1.html").write_text("old")
    Path("app/main.py").write_text("approved backend")
    return tmp_path, commit()


def test_whole_deployed_diff_catches_prior_backend_change(repo):
    _, baseline = repo
    Path("app/main.py").write_text("backend change")
    intermediate = commit()
    Path("app/clhear/web/l1.html").write_text("new UI")
    candidate = commit()
    assert release.classify(intermediate, candidate) == "viewer"
    assert release.classify(baseline, candidate) == "full"


@pytest.mark.parametrize("path", ["app/clhear/l1/routes.py", "app/clhear/web/server.py", "requirements.lock", ".github/workflows/ci.yml", "migrations/999.sql"])
def test_any_backend_packaging_or_workflow_change_uses_full_lane(repo, path):
    _, base = repo
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("changed")
    assert release.classify(base, commit()) == "full"


def test_web_symlink_cannot_enter_fast_package(repo):
    _, base = repo
    Path("app/clhear/web/link.html").symlink_to("../../main.py")
    assert release.classify(base, commit()) == "full"


def make_zip(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        for name, data in files:
            z.writestr(name, data)
    return output.getvalue()


def test_package_reuses_exact_backend_and_applies_asset_add_delete(repo):
    _, base = repo
    original = make_zip([("app/main.py", b"approved backend"), ("numpy/native.so", b"locked binary"),
                         ("app/clhear/web/l1.html", b"old")])
    Path("app/clhear/web/l1.html").unlink()
    Path("app/clhear/web/new.html").write_text("new page")
    candidate = commit()
    assert release.classify(base, candidate) == "viewer"
    release.overlay(original, candidate, Path("output/webui.zip"))
    with zipfile.ZipFile("output/webui.zip") as z:
        assert z.read("app/main.py") == b"approved backend"
        assert z.read("numpy/native.so") == b"locked binary"
        assert z.read("app/clhear/web/new.html") == b"new page"
        assert "app/clhear/web/l1.html" not in z.namelist()


@pytest.mark.parametrize("entries", [[("../escape", "bad")], [("x", "1"), ("x", "2")], [("/absolute", "bad")]])
def test_unsafe_baseline_zip_fails_before_packaging(repo, entries):
    _, base = repo
    with pytest.raises(ValueError):
        release.overlay(make_zip(entries), base, Path("out.zip"))
    assert not Path("out.zip").exists()


def pointer():
    return {"schema": release.SCHEMA, "viewer_sha": "a" * 40, "worker_sha": "b" * 40,
            "ui": {"key": f"webui/deployments/{'a'*40}/1-1/webui.zip", "version": "v1", "sha256": "c" * 64},
            "full_deployment_id": "l1-1-1", "viewer_deployment_id": "viewer-2-1"}


class S3:
    def __init__(self, error=None):
        self.error = error
        self.writes = []

    def list_objects_v2(self, **kwargs):
        assert kwargs["Prefix"] == release.POINTER and kwargs["MaxKeys"] == 1
        if self.error == "NoSuchKey":
            return {"Contents": []}
        if self.error:
            raise ClientError({"Error": {"Code": self.error}}, "ListObjectsV2")
        return {"Contents": [{"Key": release.POINTER}]}

    def get_object(self, **kwargs):
        if self.error:
            raise ClientError({"Error": {"Code": self.error}}, "GetObject")
        return {"Body": io.BytesIO(json.dumps(pointer()).encode()), "ETag": '"old-etag"'}

    def put_object(self, **kwargs):
        self.writes.append(kwargs)
        return {"VersionId": "new-version"}


def test_missing_baseline_means_full_deployment_not_guessed_fast_lane():
    result = release.select(S3("NoSuchKey"), "d" * 40)
    assert result["lane"] == "full" and result["baseline"] is None


@pytest.mark.parametrize("code", ["AccessDenied", "NoSuchBucket", "ServiceUnavailable"])
def test_unreadable_evidence_is_not_treated_as_absent(code):
    with pytest.raises(ClientError):
        release.select(S3(code), "d" * 40)


def test_pointer_cas_tracks_viewer_and_worker_independently():
    selected = {"target": "live", "lane": "viewer", "baseline": pointer(), "baseline_etag": '"old-etag"', "sha": "d" * 40}
    report = {"status": "verified", "sha": "d" * 40, "deployment_id": "viewer-3-1", "accepted_release_changed": False,
              "ui": {"key": f"webui/deployments/{'d'*40}/3-1/webui.zip", "version": "v2", "sha256": "e" * 64}}
    s3 = S3()
    result = release.publish(s3, selected, report)
    assert result["worker_sha"] == "b" * 40 and result["viewer_sha"] == "d" * 40
    assert s3.writes[0]["Key"] == release.POINTER and s3.writes[0]["IfMatch"] == '"old-etag"'
    for invalid in [dict(report, status="failed"), dict(report, accepted_release_changed=True), dict(report, sha="f"*40)]:
        with pytest.raises(ValueError):
            release.publish(S3(), selected, invalid)
    with pytest.raises(ValueError):
        release.publish(S3(), dict(selected, target="preview"), report)


def test_full_publish_establishes_new_worker_baseline():
    selected = {"target": "live", "lane": "full", "baseline": None, "baseline_etag": None, "sha": "a" * 40}
    report = {"status": "review_ready", "sha": "a" * 40, "deployment_id": "l1-1-1", "accepted_release_changed": False, "ui": pointer()["ui"]}
    s3 = S3()
    result = release.publish(s3, selected, report)
    assert result["viewer_sha"] == result["worker_sha"] == "a" * 40
    assert s3.writes[0]["IfNoneMatch"] == "*"


class CI:
    def __init__(self, baseline, *, completed_full=True):
        self.baseline = baseline
        self.completed_full = completed_full

    def get(self, path, **query):
        if path.endswith("/workflows/ci.yml"):
            return {"id": 1, "path": ".github/workflows/ci.yml"}
        if path.endswith("/workflows/1/runs"):
            # The newer backend CI was cancelled by the next UI push.
            return {"workflow_runs": [{"id": 11, "status": "completed", "conclusion": "cancelled"},
                                      {"id": 10, "status": "completed", "conclusion": "success"}]}
        if path.endswith("/runs/10"):
            return {"id": 10, "run_attempt": 1, "status": "completed", "conclusion": "success", "workflow_id": 1,
                    "event": "push", "head_branch": "main", "head_sha": self.baseline,
                    "head_repository": {"full_name": "Reg42-ai/CLHEAR-MVP"}}
        if path.endswith("/attempts/1/jobs"):
            return {"jobs": [{"name": "tests", "status": "completed", "conclusion": "success", "steps": [
                {"name": "Full application suite", "status": "completed", "conclusion": "success" if self.completed_full else "skipped"}]}]}
        raise AssertionError(path)


def test_cancelled_backend_ci_followed_by_ui_edit_still_requires_full_suite(repo):
    _, baseline = repo
    Path("app/main.py").write_text("untested backend")
    prior = commit()
    Path("app/clhear/web/l1.html").write_text("next UI")
    candidate = commit()
    assert release.classify(prior, candidate) == "viewer"
    anchor = release.full_ci_anchor(CI(baseline))
    assert anchor == baseline and release.classify(anchor, candidate) == "full"


def test_fast_or_skipped_job_does_not_claim_full_coverage(repo):
    assert release.full_ci_anchor(CI(repo[1], completed_full=False)) is None


def test_local_or_preview_identity_cannot_publish_stable_pointer():
    env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "Reg42-ai/CLHEAR-MVP", "GITHUB_REF": "refs/heads/main",
           "GITHUB_SHA": "a" * 40, "GITHUB_WORKFLOW_REF": "Reg42-ai/CLHEAR-MVP/.github/workflows/deploy-l1.yml@refs/heads/main",
           "CLHEAR_DEPLOY_ENVIRONMENT": "clhear-l1", "CLHEAR_DEPLOY_ROLE_ARN": f"arn:aws:iam::{release.ACCOUNT}:role/clhear-github-deploy",
           "ACTIONS_ID_TOKEN_REQUEST_URL": "present", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "present"}
    identity = {"Account": release.ACCOUNT, "Arn": f"arn:aws:sts::{release.ACCOUNT}:assumed-role/clhear-github-deploy/session"}
    release.publish_guard(env, identity, {"sha": "a" * 40})
    for altered in [dict(env, GITHUB_ACTIONS="false"), dict(env, CLHEAR_DEPLOY_ENVIRONMENT="clhear-preview"),
                    dict(env, GITHUB_WORKFLOW_REF=env["GITHUB_WORKFLOW_REF"].replace("deploy-l1", "deploy-preview"))]:
        with pytest.raises(ValueError):
            release.publish_guard(altered, identity, {"sha": "a" * 40})
