"""Trusted-main gate for automatic presentation previews; no branch code runs here."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
from urllib.parse import quote

if __package__:
    from .deployment_eligibility import GitHub, ROOT, REPOSITORY, REQUIRED_JOBS, pages, require, same_repo
else:
    from deployment_eligibility import GitHub, ROOT, REPOSITORY, REQUIRED_JOBS, pages, require, same_repo

WORKFLOW = f"{REPOSITORY}/.github/workflows/deploy-preview.yml@refs/heads/main"


def evaluate(api, env, event):
    require(env.get("GITHUB_ACTIONS") == "true" and env.get("GITHUB_REPOSITORY") == REPOSITORY
            and env.get("GITHUB_REF") == "refs/heads/main" and env.get("GITHUB_WORKFLOW_REF") == WORKFLOW
            and env.get("GITHUB_EVENT_NAME") == "workflow_run", "trusted_main_workflow_required")
    trigger = event.get("workflow_run", {})
    candidate, branch = trigger.get("head_sha", ""), trigger.get("head_branch", "")
    if (not same_repo(trigger) or trigger.get("name") != "ci" or trigger.get("event") != "push"
            or trigger.get("conclusion") != "success" or trigger.get("status") != "completed"
            or not re.fullmatch(r"codex/[A-Za-z0-9][A-Za-z0-9/_-]{0,150}", branch)):
        return {"eligible": False, "reason": "irrelevant_ci_completion"}
    require(bool(re.fullmatch(r"[0-9a-f]{40}", candidate)), "invalid_candidate_sha")
    main = env.get("GITHUB_SHA")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", main or "")), "invalid_main_sha")
    main_route = f"{ROOT}/git/ref/heads/main"
    branch_route = f"{ROOT}/git/ref/heads/{quote(branch, safe='/')}"
    if api.get(main_route).get("object", {}).get("sha") != main or api.get(branch_route).get("object", {}).get("sha") != candidate:
        return {"eligible": False, "reason": "superseded_commit"}
    workflow = api.get(f"{ROOT}/actions/workflows/ci.yml")
    require(type(workflow.get("id")) is int and workflow.get("path") == ".github/workflows/ci.yml")
    runs = pages(api, f"{ROOT}/actions/workflows/{workflow['id']}/runs", key="workflow_runs",
                 branch=branch, head_sha=candidate, event="push")
    runs = [r for r in runs if same_repo(r) and r.get("head_sha") == candidate and r.get("head_branch") == branch
            and r.get("workflow_id") == workflow["id"] and r.get("event") == "push" and type(r.get("id")) is int]
    require(bool(runs))
    latest = api.get(f"{ROOT}/actions/runs/{max(r['id'] for r in runs)}")
    if (latest.get("id") != trigger.get("id") or latest.get("run_attempt") != trigger.get("run_attempt")
            or latest.get("status") != "completed" or latest.get("conclusion") != "success"):
        return {"eligible": False, "reason": "superseded_or_failed_ci"}
    require(latest.get("workflow_id") == workflow["id"] and latest.get("head_sha") == candidate
            and latest.get("head_branch") == branch and latest.get("event") == "push" and same_repo(latest)
            and type(latest.get("run_attempt")) is int and latest["run_attempt"] > 0)
    jobs = pages(api, f"{ROOT}/actions/runs/{latest['id']}/attempts/{latest['run_attempt']}/jobs", key="jobs")
    require(all(len(matches := [j for j in jobs if j.get("name") == name]) == 1
                and matches[0].get("status") == "completed" and matches[0].get("conclusion") == "success"
                for name in REQUIRED_JOBS), "required_ci_checks_missing")
    if api.get(main_route).get("object", {}).get("sha") != main or api.get(branch_route).get("object", {}).get("sha") != candidate:
        return {"eligible": False, "reason": "superseded_commit"}
    return {"eligible": True, "reason": "current_same_repo_branch_ci_passed", "sha": candidate,
            "trusted_main_sha": main, "branch": branch, "ci_run_id": latest["id"], "ci_run_attempt": latest["run_attempt"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = evaluate(GitHub(os.environ.get("GH_TOKEN")), os.environ,
                          json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()))
    except Exception:
        result = {"eligible": False, "reason": "preview_github_evidence_unavailable", "error": True}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"eligible={str(result['eligible']).lower()}\nsha={result.get('sha', '')}\n")
    print(result["reason"])
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
