from copy import deepcopy
import pytest

from scripts import preview_eligibility as gate
from scripts.deployment_eligibility import EligibilityError

MAIN, CANDIDATE = "a" * 40, "b" * 40


def context():
    return {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": gate.REPOSITORY, "GITHUB_REF": "refs/heads/main",
            "GITHUB_WORKFLOW_REF": gate.WORKFLOW, "GITHUB_EVENT_NAME": "workflow_run", "GITHUB_SHA": MAIN}


class API:
    def __init__(self):
        self.main, self.branch = MAIN, CANDIDATE
        self.run = {"id": 100, "run_attempt": 1, "head_sha": CANDIDATE, "head_branch": "codex/viewer",
                    "head_repository": {"full_name": gate.REPOSITORY}, "event": "push", "name": "ci",
                    "workflow_id": 10, "status": "completed", "conclusion": "success"}
        self.jobs = [{"name": name, "status": "completed", "conclusion": "success"} for name in gate.REQUIRED_JOBS]

    def get(self, path, **query):
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": self.main}}
        if path.endswith("/git/ref/heads/codex/viewer"):
            return {"object": {"sha": self.branch}}
        if path.endswith("/actions/workflows/ci.yml"):
            return {"id": 10, "path": ".github/workflows/ci.yml"}
        if path.endswith("/workflows/10/runs"):
            return {"workflow_runs": [self.run]}
        if path.endswith("/actions/runs/100"):
            return self.run
        if path.endswith("/attempts/1/jobs"):
            return {"jobs": self.jobs}
        raise AssertionError(path)


def test_success_uses_candidate_separately_from_trusted_main():
    api = API()
    result = gate.evaluate(api, context(), {"workflow_run": api.run})
    assert result["eligible"] and result["sha"] == CANDIDATE and result["trusted_main_sha"] == MAIN


@pytest.mark.parametrize("change", [{"event": "pull_request"}, {"conclusion": "failure"}, {"head_branch": "main"},
                                      {"head_repository": {"full_name": "someone/fork"}}])
def test_fork_or_failed_or_other_event_cannot_preview(change):
    api = API()
    assert not gate.evaluate(api, context(), {"workflow_run": dict(api.run, **change)})["eligible"]


@pytest.mark.parametrize("field", ["main", "branch"])
def test_superseded_main_or_branch_skips(field):
    api = API()
    setattr(api, field, "c" * 40)
    assert not gate.evaluate(api, context(), {"workflow_run": api.run})["eligible"]


def test_rerun_failed_after_success_event_does_not_fall_back():
    api = API()
    event = {"workflow_run": deepcopy(api.run)}
    api.run.update(run_attempt=2, conclusion="failure")
    assert not gate.evaluate(api, context(), event)["eligible"]


def test_missing_required_job_or_untrusted_controller_fails_closed():
    api = API()
    api.jobs.pop()
    with pytest.raises(EligibilityError):
        gate.evaluate(api, context(), {"workflow_run": api.run})
    with pytest.raises(EligibilityError):
        gate.evaluate(API(), dict(context(), GITHUB_WORKFLOW_REF=gate.WORKFLOW.replace("refs/heads/main", "refs/heads/codex/viewer")), {"workflow_run": api.run})
