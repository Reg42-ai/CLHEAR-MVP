"""Read-only GitHub evidence for the single-founder CLHEAR deployment path.

No AWS client, credentials, deployment dispatch, or source data belongs here.
An eligible result is rechecked after serialization and immediately before the
existing guarded controller runs. GitHub context is never rewritten to select
another commit. The main branch and repository access rules remain the actual
authority boundary; this check also records the founder's exact merged PR.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPOSITORY = "Reg42-ai/CLHEAR-MVP"
OWNER_ID = 250850977
WORKFLOW = f"{REPOSITORY}/.github/workflows/deploy-l1.yml@refs/heads/main"
APPLY_STEP = "Deploy code and execute CLHEAR L0/L1 verification"
REQUIRED_JOBS = ("tests", "a11y-axe", "terraform")
ROOT = f"/repos/{REPOSITORY}"


class EligibilityError(RuntimeError):
    """A fixed safe reason; never an API response, URL, or token."""


def require(condition, reason="github_evidence_unavailable"):
    if not condition:
        raise EligibilityError(reason)


def positive(value):
    return type(value) is int and value > 0


class GitHub:
    def __init__(self, token):
        require(isinstance(token, str) and bool(token), "github_token_unavailable")
        self.token = token

    def get(self, path, **query):
        # Every route is built below from pinned names, hashes and integer IDs.
        require(path.startswith(ROOT + "/") and "?" not in path and ".." not in path)
        url = "https://api.github.com" + path
        if query:
            url += "?" + urlencode(query)
        request = Request(url, headers={"Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read(8 * 1024 * 1024 + 1)
                require(len(payload) <= 8 * 1024 * 1024)
                return json.loads(payload)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            raise EligibilityError("github_evidence_unavailable") from None


def pages(api, path, *, key=None, **query):
    """Bounded complete reads; an exhausted limit never becomes positive evidence."""
    result = []
    for page in range(1, 11):
        data = api.get(path, per_page=100, page=page, **query)
        require(key is None or isinstance(data, dict))
        rows = data.get(key) if key else data
        require(isinstance(rows, list))
        require(all(isinstance(row, dict) for row in rows))
        result.extend(rows)
        if len(rows) < 100:
            return result
    raise EligibilityError("github_evidence_pagination_limit")


def result(status, reason, sha, **evidence):
    return {"eligible": status == "eligible", "status": status, "reason": reason,
            "sha": sha, **evidence}


def same_repo(run):
    return run.get("head_repository", {}).get("full_name") == REPOSITORY


def valid_run(run, workflow_id, sha):
    return (run.get("workflow_id") == workflow_id and run.get("head_sha") == sha
            and run.get("head_branch") == "main" and run.get("event") == "push"
            and same_repo(run) and positive(run.get("id")) and positive(run.get("run_attempt")))


def latest_ci(api, sha):
    workflow = api.get(f"{ROOT}/actions/workflows/ci.yml")
    require(positive(workflow.get("id")) and workflow.get("path") == ".github/workflows/ci.yml")
    runs = pages(api, f"{ROOT}/actions/workflows/{workflow['id']}/runs", key="workflow_runs",
                 head_sha=sha, branch="main", event="push")
    candidates = [run for run in runs if valid_run(run, workflow["id"], sha)]
    if not candidates:
        return None
    # Run IDs identify creation order; reruns retain their ID and advance their
    # attempt. Never fall back from a newer failed/running run to an older pass.
    candidate = max(candidates, key=lambda row: row["id"])
    current = api.get(f"{ROOT}/actions/runs/{candidate['id']}")
    require(valid_run(current, workflow["id"], sha) and current["id"] == candidate["id"])
    return current


def owner_merge(api, sha):
    associated = pages(api, f"{ROOT}/commits/{sha}/pulls")
    candidates = {row.get("number") for row in associated if row.get("merge_commit_sha") == sha
                  and positive(row.get("number"))}
    for number in sorted(candidates, reverse=True):
        pr = api.get(f"{ROOT}/pulls/{number}")
        if (pr.get("number") == number and pr.get("merged") is True
                and pr.get("state") == "closed" and pr.get("merge_commit_sha") == sha
                and pr.get("base", {}).get("ref") == "main"
                and pr.get("base", {}).get("repo", {}).get("full_name") == REPOSITORY
                and pr.get("merged_by", {}).get("id") == OWNER_ID):
            return number
    return None


def already_deployed(api, sha, current_run_id, current_run_attempt):
    """Only the newest actual apply may prove duplication, never a skipped job.

    Inspect across SHAs: an intervening failed deployment must not be hidden by
    an older successful run. A currently executing run is rechecked after this
    job acquires the same deployment concurrency group.
    """
    workflow = api.get(f"{ROOT}/actions/workflows/deploy-l1.yml")
    require(positive(workflow.get("id")) and workflow.get("path") == ".github/workflows/deploy-l1.yml")
    # Deduplication is optional: inspect recent history, without making a long
    # lived project permanently undeployable once its history exceeds a limit.
    data = api.get(f"{ROOT}/actions/workflows/{workflow['id']}/runs", branch="main", per_page=30, page=1)
    runs = data.get("workflow_runs")
    require(isinstance(runs, list) and all(isinstance(row, dict) for row in runs))
    # An old run can be rerun recently. If any run falls outside the window we
    # cannot prove that its latest apply did not supersede a visible success.
    # Proceed through normal preflight instead of claiming a positive duplicate.
    if type(data.get("total_count")) is not int or data["total_count"] > len(runs):
        return None
    candidates = [row for row in runs if positive(row.get("id")) and row["id"] != current_run_id
                  and row.get("workflow_id") == workflow["id"] and row.get("head_branch") == "main"
                  and row.get("event") in {"workflow_run", "workflow_dispatch"} and same_repo(row)]
    histories = []
    for row in candidates:
        run = api.get(f"{ROOT}/actions/runs/{row['id']}")
        require(run.get("id") == row["id"] and run.get("workflow_id") == workflow["id"]
                and run.get("head_sha") == row.get("head_sha") and run.get("head_branch") == "main"
                and same_repo(run) and positive(run.get("run_attempt")))
        if run.get("status") != "completed":
            return None
        histories.append((run["id"], run["run_attempt"], run))
    # A workflow rerun has the same run ID. Exclude only the current active
    # attempt; its earlier successful attempt still proves a duplicate.
    if current_run_attempt > 1:
        histories.append((current_run_id, current_run_attempt - 1, None))
    applies = []
    for ident, latest_attempt, latest in histories:
        for attempt in range(latest_attempt, max(0, latest_attempt - 30), -1):
            run = latest if latest and attempt == latest_attempt else api.get(f"{ROOT}/actions/runs/{ident}/attempts/{attempt}")
            require(run.get("id") == ident and run.get("workflow_id") == workflow["id"]
                    and run.get("run_attempt") == attempt and run.get("head_branch") == "main"
                    and run.get("event") in {"workflow_run", "workflow_dispatch"} and same_repo(run))
            if run.get("status") != "completed":
                return None
            jobs = pages(api, f"{ROOT}/actions/runs/{ident}/attempts/{attempt}/jobs", key="jobs")
            deploys = [job for job in jobs if job.get("name") == "deploy"]
            require(len(deploys) <= 1)
            if not deploys:
                continue
            job = deploys[0]
            steps = [step for step in job.get("steps", []) if step.get("name") == APPLY_STEP]
            require(len(steps) <= 1)
            if not steps or steps[0].get("conclusion") == "skipped":
                continue
            # Creation IDs do not order reruns. Use when the actual apply step
            # executed; malformed/tied/incomplete history cannot prove a skip.
            try:
                started = datetime.fromisoformat(steps[0]["started_at"].replace("Z", "+00:00"))
                if started.tzinfo is None:
                    return None
            except (KeyError, TypeError, ValueError, AttributeError):
                return None
            succeeded = (run.get("head_sha") == sha and run.get("conclusion") == "success"
                         and job.get("status") == "completed" and job.get("conclusion") == "success"
                         and steps[0].get("status") == "completed" and steps[0].get("conclusion") == "success")
            applies.append((started, ident, attempt, succeeded))
            break
        else:
            if latest_attempt > 30:
                return None
    if not applies:
        return None
    newest = max(row[0] for row in applies)
    winners = [row for row in applies if row[0] == newest]
    if len(winners) == 1 and winners[0][3]:
        return {"run_id": winners[0][1], "run_attempt": winners[0][2]}
    return None


def evaluate(api, context):
    sha = context.get("sha", "")
    require(isinstance(sha, str) and bool(re.fullmatch(r"[0-9a-f]{40}", sha)), "invalid_github_context")
    if (context.get("repository") != REPOSITORY or context.get("ref") != "refs/heads/main"
            or context.get("workflow_ref") != WORKFLOW
            or context.get("event_name") not in {"workflow_run", "workflow_dispatch"}):
        return result("skipped", "irrelevant_event", sha)
    require(positive(context.get("run_id")) and positive(context.get("run_attempt")), "invalid_github_context")
    triggered = None
    if context["event_name"] == "workflow_run":
        triggered = context.get("payload", {}).get("workflow_run", {})
        if (triggered.get("name") != "ci" or triggered.get("event") != "push"
                or triggered.get("head_branch") != "main" or not same_repo(triggered)
                or triggered.get("status") != "completed" or triggered.get("conclusion") != "success"):
            return result("skipped", "irrelevant_ci_completion", sha)
        if triggered.get("head_sha") != sha:
            return result("skipped", "superseded_commit", sha)
        require(positive(triggered.get("id")) and positive(triggered.get("run_attempt")), "invalid_ci_event")
    head = api.get(f"{ROOT}/git/ref/heads/main").get("object", {}).get("sha")
    require(isinstance(head, str) and bool(re.fullmatch(r"[0-9a-f]{40}", head)))
    if head != sha:
        return result("skipped", "superseded_commit", sha)
    ci = latest_ci(api, sha)
    if ci is None or ci.get("status") != "completed" or ci.get("conclusion") != "success":
        return result("blocked", "latest_main_ci_not_successful", sha)
    if triggered and (triggered["id"] != ci["id"] or triggered["run_attempt"] != ci["run_attempt"]
                      or triggered.get("workflow_id") != ci["workflow_id"]):
        return result("skipped", "superseded_ci_attempt", sha)
    jobs = pages(api, f"{ROOT}/actions/runs/{ci['id']}/attempts/{ci['run_attempt']}/jobs", key="jobs")
    if any(len(matches := [job for job in jobs if job.get("name") == name]) != 1
           or matches[0].get("status") != "completed" or matches[0].get("conclusion") != "success"
           for name in REQUIRED_JOBS):
        return result("blocked", "required_ci_jobs_not_successful", sha)
    pr = owner_merge(api, sha)
    if pr is None:
        return result("blocked", "exact_founder_merge_required", sha)
    evidence = {"ci_run_id": ci["id"], "ci_run_attempt": ci["run_attempt"],
                "merged_pr_number": pr, "merged_by_id": OWNER_ID}
    if triggered:
        previous = already_deployed(api, sha, context["run_id"], context["run_attempt"])
        if previous:
            return result("skipped", "already_deployed", sha, deployment_run_id=previous["run_id"],
                          deployment_run_attempt=previous["run_attempt"], **evidence)
    # API reads are not a transaction. Catch a merge or CI rerun during evidence
    # collection, then repeat this entire check after the lock and before apply.
    head = api.get(f"{ROOT}/git/ref/heads/main").get("object", {}).get("sha")
    if head != sha:
        return result("skipped", "superseded_commit", sha, **evidence)
    current_ci = latest_ci(api, sha)
    if (current_ci is None or current_ci.get("id") != ci["id"]
            or current_ci.get("run_attempt") != ci["run_attempt"]
            or current_ci.get("status") != "completed" or current_ci.get("conclusion") != "success"):
        return result("blocked", "ci_changed_during_verification", sha, **evidence)
    return result("eligible", "founder_merge_and_exact_main_ci_verified", sha, **evidence)


def main(argv=None, *, api=None, environ=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=("precheck", "locked", "apply"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        require(env.get("GITHUB_ACTIONS") == "true", "invalid_github_context")
        payload = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())
        context = {"repository": env.get("GITHUB_REPOSITORY"), "ref": env.get("GITHUB_REF"),
                   "sha": env.get("GITHUB_SHA"), "workflow_ref": env.get("GITHUB_WORKFLOW_REF"),
                   "event_name": env.get("GITHUB_EVENT_NAME"), "run_id": int(env.get("GITHUB_RUN_ID", "")),
                   "run_attempt": int(env.get("GITHUB_RUN_ATTEMPT", "")),
                   "payload": payload}
        outcome = evaluate(api or GitHub(env.get("GH_TOKEN")), context)
    except EligibilityError as error:
        outcome = {"eligible": False, "status": "error", "reason": str(error)}
    except (KeyError, ValueError, TypeError, OSError):
        outcome = {"eligible": False, "status": "error", "reason": "invalid_github_context"}
    except Exception:
        outcome = {"eligible": False, "status": "error", "reason": "github_evidence_unavailable"}
    outcome["stage"] = args.stage
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(outcome, indent=2, sort_keys=True) + "\n")
    args.output.chmod(0o600)
    if env.get("GITHUB_OUTPUT"):
        with open(env["GITHUB_OUTPUT"], "a") as output:
            output.write(f"eligible={str(outcome['eligible']).lower()}\n")
            output.write(f"reason={outcome['reason']}\n")
    line = f"Deployment eligibility ({args.stage}): {outcome['status']} — {outcome['reason']}"
    print(line)
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(line + "\n\n")
    return 1 if outcome["status"] in {"blocked", "error"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
