"""Exact GitHub deployment authorization using read-only, offline API fixtures."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat

import pytest
import yaml

from scripts import deployment_eligibility as eligibility

SHA = "a" * 40
OTHER = "b" * 40
REPO = eligibility.REPOSITORY
ROOT = eligibility.ROOT


def run(ident=101, sha=SHA, *, workflow_id=11, attempt=1, event="push", **overrides):
    return {"id": ident, "head_sha": sha, "workflow_id": workflow_id, "run_attempt": attempt,
            "head_branch": "main", "head_repository": {"full_name": REPO}, "event": event,
            "status": "completed", "conclusion": "success", "name": "ci", **overrides}


def context(event="workflow_run", **overrides):
    return {"repository": REPO, "ref": "refs/heads/main", "sha": SHA,
            "workflow_ref": eligibility.WORKFLOW, "event_name": event, "run_id": 500,
            "run_attempt": 1,
            "payload": {"workflow_run": run()}, **overrides}


def deploy_job(*, job_conclusion="success", apply_conclusion="success"):
    return {"name": "deploy", "status": "completed", "conclusion": job_conclusion,
            "steps": [{"name": eligibility.APPLY_STEP, "status": "completed", "conclusion": apply_conclusion}]}


class API:
    def __init__(self):
        self.calls = []
        self.head = SHA
        self.ci = [run()]
        self.runs = {101: run()}
        self.ci_jobs = [{"name": name, "status": "completed", "conclusion": "success"}
                        for name in eligibility.REQUIRED_JOBS]
        self.pr = {"number": 21, "merged": True, "state": "closed", "merge_commit_sha": SHA,
                   "merged_by": {"id": eligibility.OWNER_ID, "login": "Reg42-ai"},
                   "base": {"ref": "main", "repo": {"full_name": REPO}}}
        self.associated = [{"number": 21, "merge_commit_sha": SHA}]
        self.deployments = []
        self.jobs = {}
        self.attempts = {}

    def add_deployment(self, ident=400, *, sha=SHA, conclusion="success", job=None, attempt=1, **overrides):
        value = run(ident, sha, workflow_id=22, attempt=attempt, event="workflow_run", conclusion=conclusion, **overrides)
        self.deployments.append(value)
        self.runs[ident] = value
        self.jobs[(ident, attempt)] = [job or deploy_job()]
        for step in self.jobs[(ident, attempt)][0].get("steps", []):
            step["started_at"] = (datetime(2026, 9, 15, tzinfo=timezone.utc) + timedelta(seconds=ident)).isoformat()
        self.attempts[(ident, attempt)] = copy.deepcopy(value)

    def add_attempt(self, ident, attempt, *, started="2026-09-15T01:00:00Z", job=None, **overrides):
        value = dict(self.runs[ident], run_attempt=attempt, **overrides)
        self.runs[ident] = value
        self.deployments = [value if row["id"] == ident else row for row in self.deployments]
        self.attempts[(ident, attempt)] = copy.deepcopy(value)
        self.jobs[(ident, attempt)] = [job or deploy_job()]
        for step in self.jobs[(ident, attempt)][0].get("steps", []):
            step["started_at"] = started

    def get(self, path, **query):
        self.calls.append((path, copy.deepcopy(query)))
        if path == f"{ROOT}/git/ref/heads/main":
            return {"object": {"sha": self.head}}
        if path == f"{ROOT}/actions/workflows/ci.yml":
            return {"id": 11, "path": ".github/workflows/ci.yml"}
        if path == f"{ROOT}/actions/workflows/deploy-l1.yml":
            return {"id": 22, "path": ".github/workflows/deploy-l1.yml"}
        if path == f"{ROOT}/actions/workflows/11/runs":
            assert query == {"per_page": 100, "page": 1, "head_sha": SHA, "branch": "main", "event": "push"}
            return {"workflow_runs": copy.deepcopy(self.ci)}
        if path == f"{ROOT}/actions/workflows/22/runs":
            assert query == {"per_page": 30, "page": 1, "branch": "main"}
            return {"workflow_runs": copy.deepcopy(self.deployments), "total_count": len(self.deployments)}
        if path == f"{ROOT}/commits/{SHA}/pulls":
            return copy.deepcopy(self.associated)
        if path == f"{ROOT}/pulls/21":
            return copy.deepcopy(self.pr)
        prefix = f"{ROOT}/actions/runs/"
        if path.startswith(prefix):
            parts = path[len(prefix):].split("/")
            ident = int(parts[0])
            if len(parts) == 1:
                return copy.deepcopy(self.runs[ident])
            assert parts[1] == "attempts"
            attempt = int(parts[2])
            if len(parts) == 3:
                return copy.deepcopy(self.attempts[(ident, attempt)])
            assert parts[3] == "jobs"
            if ident in {row["id"] for row in self.ci}:
                assert attempt == self.runs[ident]["run_attempt"]
                return {"jobs": copy.deepcopy(self.ci_jobs)}
            return {"jobs": copy.deepcopy(self.jobs[(ident, attempt)])}
        raise AssertionError(f"Unexpected read-only fixture route: {path}")


@pytest.mark.parametrize("event", ["workflow_run", "workflow_dispatch"])
def test_founder_exact_merged_sha_and_latest_main_ci_are_eligible(event):
    api = API()
    value = eligibility.evaluate(api, context(event))
    assert value == {"eligible": True, "status": "eligible", "reason": "founder_merge_and_exact_main_ci_verified",
                     "sha": SHA, "ci_run_id": 101, "ci_run_attempt": 1, "merged_pr_number": 21,
                     "merged_by_id": eligibility.OWNER_ID}
    assert sum(path.endswith("/git/ref/heads/main") for path, _ in api.calls) == 2
    assert not any("/workflows/22/" in path for path, _ in api.calls) if event == "workflow_dispatch" else True


@pytest.mark.parametrize("change", [
    {"repository": "other/repository"}, {"ref": "refs/heads/topic"},
    {"workflow_ref": eligibility.WORKFLOW.replace("deploy-l1.yml", "other.yml")},
    {"event_name": "pull_request"},
])
def test_irrelevant_context_never_reads_github_or_acquires_deployment_authority(change):
    api = API()
    value = eligibility.evaluate(api, context(**change))
    assert value["status"] == "skipped" and value["reason"] == "irrelevant_event"
    assert api.calls == []


@pytest.mark.parametrize("change", [
    {"name": "unrelated"}, {"event": "pull_request"}, {"head_branch": "topic"},
    {"head_repository": {"full_name": "attacker/fork"}},
    {"status": "in_progress"}, {"conclusion": "failure"}, {"conclusion": "cancelled"},
])
def test_irrelevant_failed_or_fork_ci_completion_is_a_visible_noop(change):
    api = API()
    value = eligibility.evaluate(api, context(payload={"workflow_run": run(**change)}))
    assert value["status"] == "skipped" and value["reason"] == "irrelevant_ci_completion"
    assert api.calls == []


@pytest.mark.parametrize("source", ["event", "main"])
def test_superseded_sha_does_not_choose_another_commit(source):
    api = API()
    ctx = context()
    if source == "event":
        ctx["payload"]["workflow_run"]["head_sha"] = OTHER
    else:
        api.head = OTHER
    value = eligibility.evaluate(api, ctx)
    assert value["status"] == "skipped" and value["reason"] == "superseded_commit"
    assert value["sha"] == SHA
    assert len(api.calls) == (0 if source == "event" else 1)


@pytest.mark.parametrize("state", ["failed", "running", "absent"])
def test_latest_ci_failure_or_running_attempt_cannot_fall_back_to_old_success(state):
    api = API()
    if state == "absent":
        api.ci = []
    else:
        newer = run(102, conclusion="failure" if state == "failed" else None,
                    status="completed" if state == "failed" else "in_progress")
        api.ci.append(newer)
        api.runs[102] = newer
    value = eligibility.evaluate(api, context("workflow_dispatch"))
    assert value["status"] == "blocked" and value["reason"] == "latest_main_ci_not_successful"


def test_completed_event_from_an_old_attempt_is_skipped():
    api = API()
    api.runs[101] = run(attempt=2)
    value = eligibility.evaluate(api, context())
    assert value["reason"] == "superseded_ci_attempt"
    ctx = context(payload={"workflow_run": run(attempt=2)})
    assert eligibility.evaluate(api, ctx)["eligible"]
    assert any(path.endswith("/101/attempts/2/jobs") for path, _ in api.calls)


@pytest.mark.parametrize("problem", ["failed", "missing", "duplicate", "unfinished"])
def test_all_required_ci_jobs_must_succeed_in_exact_latest_attempt(problem):
    api = API()
    if problem == "missing":
        api.ci_jobs.pop()
    elif problem == "duplicate":
        api.ci_jobs.append(copy.deepcopy(api.ci_jobs[0]))
    elif problem == "unfinished":
        api.ci_jobs[0]["status"] = "in_progress"
    else:
        api.ci_jobs[0]["conclusion"] = "failure"
    value = eligibility.evaluate(api, context())
    assert value["reason"] == "required_ci_jobs_not_successful" and not value["eligible"]


@pytest.mark.parametrize("problem", ["direct_push", "wrong_merge_sha", "not_merged", "wrong_owner",
                                      "wrong_base", "wrong_repository", "spoofed_owner_login"])
def test_only_exact_founder_merge_authorizes_the_sha(problem):
    api = API()
    if problem == "direct_push":
        api.associated = []
    elif problem == "wrong_merge_sha":
        api.pr["merge_commit_sha"] = OTHER
    elif problem == "not_merged":
        api.pr["merged"] = False
    elif problem in {"wrong_owner", "spoofed_owner_login"}:
        api.pr["merged_by"] = {"id": 123, "login": "Reg42-ai" if problem == "spoofed_owner_login" else "other"}
    elif problem == "wrong_base":
        api.pr["base"]["ref"] = "topic"
    else:
        api.pr["base"]["repo"]["full_name"] = "other/repo"
    value = eligibility.evaluate(api, context())
    assert value["reason"] == "exact_founder_merge_required" and value["status"] == "blocked"


def test_old_associated_pr_cannot_authorize_a_direct_push_descendant():
    api = API()
    api.associated[0]["merge_commit_sha"] = OTHER
    assert eligibility.evaluate(api, context())["reason"] == "exact_founder_merge_required"


def test_successful_apply_and_entire_job_deduplicate_auto_but_not_manual_recovery():
    api = API()
    api.add_deployment()
    value = eligibility.evaluate(api, context())
    assert value["reason"] == "already_deployed" and value["deployment_run_id"] == 400
    assert eligibility.evaluate(api, context("workflow_dispatch"))["eligible"]


@pytest.mark.parametrize("problem", ["failed_apply", "failed_later_step", "failed_run", "skipped_apply", "missing_apply",
                                      "other_sha", "other_repo", "in_progress"])
def test_success_or_history_without_confirmed_complete_apply_is_not_a_duplicate(problem):
    api = API()
    api.add_deployment()
    row, job = api.runs[400], api.jobs[(400, 1)][0]
    if problem == "failed_apply":
        job["steps"][0]["conclusion"] = "failure"
    elif problem == "failed_later_step":
        job["conclusion"] = "failure"
    elif problem == "failed_run":
        row["conclusion"] = "failure"
    elif problem == "skipped_apply":
        job["steps"][0]["conclusion"] = "skipped"
    elif problem == "missing_apply":
        job["steps"] = []
    elif problem == "other_sha":
        row["head_sha"] = OTHER
    elif problem == "other_repo":
        row["head_repository"]["full_name"] = "other/repo"
    else:
        row["status"] = "in_progress"
    assert eligibility.evaluate(api, context())["eligible"]


def test_later_failed_apply_is_not_hidden_by_older_success():
    api = API()
    api.add_deployment(399)
    api.add_deployment(400, conclusion="failure", job=deploy_job(job_conclusion="failure", apply_conclusion="failure"))
    assert eligibility.evaluate(api, context())["eligible"]


def test_skipped_attempt_does_not_fabricate_duplicate_success():
    api = API()
    api.add_deployment(399, conclusion="failure", job=deploy_job(job_conclusion="failure", apply_conclusion="failure"))
    api.add_deployment(400, job=deploy_job(apply_conclusion="skipped"))
    assert eligibility.evaluate(api, context())["eligible"]


def test_latest_deploy_attempt_is_read_instead_of_old_success():
    api = API()
    api.add_deployment()
    api.runs[400]["run_attempt"] = 2
    api.jobs[(400, 2)] = [deploy_job(job_conclusion="failure", apply_conclusion="failure")]
    api.jobs[(400, 2)][0]["steps"][0]["started_at"] = "2026-09-15T01:00:00Z"
    assert eligibility.evaluate(api, context())["eligible"]


@pytest.mark.parametrize("previous", ["success", "failure"])
def test_current_auto_run_prior_attempt_is_checked_without_current_active_attempt(previous):
    api = API()
    api.add_deployment(500, conclusion=previous,
                       job=deploy_job(job_conclusion=previous, apply_conclusion=previous))
    api.add_attempt(500, 2, status="in_progress", conclusion=None)
    value = eligibility.evaluate(api, context(run_attempt=2))
    if previous == "success":
        assert value["reason"] == "already_deployed" and value["deployment_run_id"] == 500
        assert value["deployment_run_attempt"] == 1
    else:
        assert value["eligible"]
    assert not any(path.endswith("/500/attempts/2/jobs") for path, _ in api.calls)


def test_current_auto_run_scans_past_prior_skipped_attempt():
    api = API()
    api.add_deployment(500)
    api.add_attempt(500, 2, job=deploy_job(apply_conclusion="skipped"))
    value = eligibility.evaluate(api, context(run_attempt=3))
    assert value["reason"] == "already_deployed" and value["deployment_run_attempt"] == 1


@pytest.mark.parametrize("newer_skipped_attempt", [False, True])
def test_old_run_new_failed_attempt_supersedes_newer_run_success_by_actual_apply_time(newer_skipped_attempt):
    api = API()
    api.add_deployment(398)
    api.add_deployment(400)
    api.add_attempt(398, 2, conclusion="failure",
                    job=deploy_job(job_conclusion="failure", apply_conclusion="failure"))
    if newer_skipped_attempt:
        api.add_attempt(398, 3, job=deploy_job(apply_conclusion="skipped"))
    assert eligibility.evaluate(api, context())["eligible"]


@pytest.mark.parametrize("problem", ["malformed_timestamp", "tied_timestamp", "partial_runs", "partial_attempts"])
def test_incomplete_or_ambiguous_history_never_proves_duplicate(problem):
    api = API()
    api.add_deployment(400)
    if problem == "malformed_timestamp":
        api.jobs[(400, 1)][0]["steps"][0]["started_at"] = "unknown"
    elif problem == "tied_timestamp":
        api.add_deployment(399)
        api.jobs[(399, 1)][0]["steps"][0]["started_at"] = api.jobs[(400, 1)][0]["steps"][0]["started_at"]
    elif problem == "partial_runs":
        original = api.get
        def get(path, **query):
            value = original(path, **query)
            if path.endswith("/workflows/22/runs"):
                value["total_count"] = 31
            return value
        api.get = get
    else:
        for attempt in range(2, 33):
            api.add_attempt(400, attempt, job=deploy_job(apply_conclusion="skipped"))
    assert eligibility.evaluate(api, context())["eligible"]


@pytest.mark.parametrize("race", ["main_merge", "ci_rerun", "newer_ci"])
def test_evidence_collection_races_do_not_authorize_stale_state(race):
    api = API()
    original = api.get
    def get(path, **query):
        response = original(path, **query)
        if path.endswith("/pulls/21"):
            if race == "main_merge":
                api.head = OTHER
            elif race == "ci_rerun":
                api.runs[101] = run(attempt=2, status="in_progress", conclusion=None)
            else:
                api.ci.append(run(102))
                api.runs[102] = run(102)
        return response
    api.get = get
    value = eligibility.evaluate(api, context())
    assert not value["eligible"] and value["reason"] in {"superseded_commit", "ci_changed_during_verification"}


def test_after_lock_and_preapply_rechecks_observe_state_changes():
    api = API()
    ctx = context()
    assert eligibility.evaluate(api, ctx)["eligible"]
    api.add_deployment()
    assert eligibility.evaluate(api, ctx)["reason"] == "already_deployed"
    api.deployments.clear()
    api.head = OTHER
    assert eligibility.evaluate(api, ctx)["reason"] == "superseded_commit"


def test_paginated_reads_are_complete_and_bounded():
    class Paged:
        def get(self, path, **query):
            return [{"number": query["page"]}] * (100 if query["page"] == 1 else 1)
    assert len(eligibility.pages(Paged(), f"{ROOT}/pulls")) == 101
    class Unlimited:
        def get(self, path, **query):
            return [{}] * 100
    with pytest.raises(eligibility.EligibilityError, match="pagination_limit"):
        eligibility.pages(Unlimited(), f"{ROOT}/pulls")


@pytest.mark.parametrize("failure", ["api", "context"])
def test_cli_errors_are_closed_and_secret_safe(tmp_path, capsys, failure):
    api = API()
    if failure == "api":
        def get(*args, **kwargs):
            raise RuntimeError("private-token-or-body-must-not-leak")
        api.get = get
    event = tmp_path / "event.json"
    event.write_text(json.dumps(context()["payload"]))
    env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": REPO,
           "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": SHA, "GITHUB_WORKFLOW_REF": eligibility.WORKFLOW,
           "GITHUB_EVENT_NAME": "workflow_run", "GITHUB_RUN_ID": "500", "GH_TOKEN": "private-token-or-body-must-not-leak",
           "GITHUB_RUN_ATTEMPT": "1",
           "GITHUB_OUTPUT": str(tmp_path / "outputs"), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary")}
    if failure == "context":
        env["GITHUB_RUN_ID"] = "bad-id"
    output = tmp_path / "artifacts" / "eligibility.json"
    assert eligibility.main(["--stage", "precheck", "--output", str(output)], api=api, environ=env) == 1
    value = json.loads(output.read_text())
    assert value["status"] == "error" and value["eligible"] is False
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "eligible=false" in (tmp_path / "outputs").read_text()
    documents = capsys.readouterr().out + output.read_text() + (tmp_path / "summary").read_text()
    assert "private-token-or-body-must-not-leak" not in documents


def test_workflow_keeps_eligibility_unprivileged_and_rechecks_before_all_mutations():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/deploy-l1.yml"
    workflow = yaml.safe_load(path.read_text())
    events = workflow.get("on", workflow.get(True))
    assert events["workflow_run"] == {"workflows": ["ci"], "branches": ["main"], "types": ["completed"]}
    assert "workflow_dispatch" in events and "concurrency" not in workflow
    assert workflow["permissions"] == {}
    before, deploy = workflow["jobs"]["eligibility"], workflow["jobs"]["deploy"]
    assert before["permissions"] == {"contents": "read", "actions": "read", "pull-requests": "read"}
    assert "environment" not in before and "concurrency" not in before
    assert deploy["needs"] == "eligibility" and "outputs.eligible == 'true'" in deploy["if"]
    assert deploy["concurrency"] == {"group": "clhear-l1-deployment", "cancel-in-progress": False}
    assert deploy["environment"] == "clhear-l1" and deploy["permissions"]["id-token"] == "write"
    steps = deploy["steps"]
    locked = next(i for i, step in enumerate(steps) if step.get("id") == "locked")
    apply_guard = next(i for i, step in enumerate(steps) if step.get("id") == "apply_guard")
    apply = next(i for i, step in enumerate(steps) if step.get("name") == eligibility.APPLY_STEP)
    assert apply == apply_guard + 1
    for step in steps[locked + 1:apply + 1]:
        assert "steps.locked.outputs.eligible == 'true'" in step.get("if", "")
    assert "steps.apply_guard.outputs.eligible == 'true'" in steps[apply]["if"]
    assert '--sha "$GITHUB_SHA"' in steps[apply]["run"]
    assert "GITHUB_SHA:" not in path.read_text() and "GITHUB_WORKFLOW_REF:" not in path.read_text()
    for job in (before, deploy):
        checkout = job["steps"][0]
        assert checkout["with"] == {"ref": "${{ github.sha }}", "persist-credentials": False}
