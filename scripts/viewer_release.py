"""Select and package presentation-only releases from a verified code baseline.

This is deployment tooling, never a corpus reader/importer. The code pointer is
separate from L0's accepted-release and snapshot pointers. Missing baseline
evidence takes the full deployment path; denied or corrupt evidence fails closed.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile

from botocore.exceptions import ClientError

ACCOUNT = "730649732189"
BUCKET = f"clhear-deploy-{ACCOUNT}"
POINTER = "deployments/l1/viewer-release.json"
SCHEMA = "clhear.viewer-release.v1"
WEB = "app/clhear/web/"
EXTENSIONS = {".html", ".css", ".js", ".mjs", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2"}
MAX_ZIP = 240 * 1024 * 1024
FAST_TESTS = (
    "tests/test_l1_demo_ui.py", "tests/test_review_access.py",
    "tests/test_l1_evidence_api.py", "tests/test_lambda_snapshot_access.py",
    "tests/test_lambda_packaging.py", "tests/test_preview.py",
)


def full_ci_anchor(api):
    """A canceled backend CI must not be hidden by a subsequent UI-only push."""
    if __package__:
        from .deployment_eligibility import ROOT, same_repo
    else:
        from deployment_eligibility import ROOT, same_repo
    workflow = api.get(f"{ROOT}/actions/workflows/ci.yml")
    require(type(workflow.get("id")) is int and workflow.get("path") == ".github/workflows/ci.yml", "CI identity unavailable")
    runs = api.get(f"{ROOT}/actions/workflows/{workflow['id']}/runs", branch="main", event="push", per_page=30).get("workflow_runs", [])
    require(isinstance(runs, list), "CI history unavailable")
    for row in runs:
        if (row.get("status") != "completed" or row.get("conclusion") != "success"
                or type(row.get("id")) is not int):
            continue
        run = api.get(f"{ROOT}/actions/runs/{row['id']}")
        if (not same_repo(run) or run.get("workflow_id") != workflow["id"] or run.get("head_branch") != "main"
                or run.get("event") != "push" or run.get("status") != "completed" or run.get("conclusion") != "success"
                or type(run.get("run_attempt")) is not int or run["run_attempt"] < 1):
            continue
        jobs = api.get(f"{ROOT}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", per_page=100).get("jobs", [])
        tests = [j for j in jobs if j.get("name") == "tests"]
        if len(tests) != 1 or tests[0].get("status") != "completed" or tests[0].get("conclusion") != "success":
            continue
        if any(s.get("name") in {"Full application suite", "pytest tests/ -v"}
               and s.get("status") == "completed" and s.get("conclusion") == "success" for s in tests[0].get("steps", [])):
            return sha(run.get("head_sha"))
    return None


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(value):
    require(isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{40}", value)), "Invalid commit hash")
    return value


def presentation(path):
    parts = PurePosixPath(path).parts
    return (path.startswith(WEB) and PurePosixPath(path).suffix.lower() in EXTENSIONS
            and ".." not in parts and not any(p.startswith(".") for p in parts))


def git(*args):
    return subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE).stdout


def classify(base, candidate):
    """Use the whole deployed-to-candidate diff, never just the newest commit."""
    sha(base); sha(candidate)
    changed = git("diff", "--no-renames", "--name-only", "-z", base, candidate, "--").decode().split("\0")
    changed = [p for p in changed if p]
    if not changed or not all(presentation(p) for p in changed):
        return "full"
    # A symlink or submodule under the web tree is executable packaging input.
    for row in git("ls-tree", "-rz", candidate, "--", WEB).split(b"\0"):
        if row:
            meta, path = row.split(b"\t", 1)
            mode, kind, _ = meta.decode().split()
            if mode != "100644" or kind != "blob" or not presentation(path.decode()):
                return "full"
    return "viewer"


def validate_pointer(value):
    require(isinstance(value, dict) and value.get("schema") == SCHEMA, "Invalid viewer release evidence")
    sha(value.get("viewer_sha")); sha(value.get("worker_sha"))
    ui = value.get("ui", {})
    require(isinstance(ui, dict) and re.fullmatch(r"[0-9a-f]{64}", ui.get("sha256", "")), "Invalid code artifact hash")
    require(ui.get("version") not in (None, "", "null") and
            re.fullmatch(r"webui/deployments/[0-9a-f]{40}/[1-9][0-9]*-[1-9][0-9]*/webui.zip", ui.get("key", "")),
            "Invalid versioned stable code artifact")
    for key in ("full_deployment_id", "viewer_deployment_id"):
        require(isinstance(value.get(key), str) and re.fullmatch(r"(?:l1|viewer)-[1-9][0-9]*-[1-9][0-9]*", value[key]),
                "Invalid deployment evidence identity")
    return value


def read_pointer(s3):
    listing = s3.list_objects_v2(Bucket=BUCKET, Prefix=POINTER, MaxKeys=1, ExpectedBucketOwner=ACCOUNT)
    if not any(obj.get("Key") == POINTER for obj in listing.get("Contents", [])):
        return None, None
    try:
        response = s3.get_object(Bucket=BUCKET, Key=POINTER, ExpectedBucketOwner=ACCOUNT)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "NoSuchKey":
            return None, None
        raise
    with response["Body"] as body:
        data = body.read(65537)
    require(len(data) <= 65536 and response.get("ETag"), "Invalid release pointer length or version")
    return validate_pointer(json.loads(data)), response["ETag"]


def select(s3, candidate, target="live", trusted_main=None):
    sha(candidate)
    pointer, etag = read_pointer(s3)
    result = {"sha": candidate, "target": target, "lane": "full", "baseline": pointer,
              "baseline_etag": etag, "reason": "verified_baseline_missing"}
    if pointer:
        result.update(lane=classify(pointer["viewer_sha"], candidate), reason="deployed_to_candidate_diff")
    if target == "preview":
        sha(trusted_main)
        require(classify(trusted_main, candidate) == "viewer", "Preview accepts presentation changes against current main only")
        require(pointer is not None and result["lane"] == "viewer", "Deploy current main fully before previewing this branch")
    return result


def code_bytes(s3, ui, expected_sha):
    response = s3.get_object(Bucket=BUCKET, Key=ui["key"], VersionId=ui["version"], ExpectedBucketOwner=ACCOUNT)
    require(response.get("Metadata", {}).get("git-sha") == expected_sha, "Baseline artifact commit metadata mismatch")
    with response["Body"] as body:
        data = body.read(MAX_ZIP + 1)
    require(len(data) <= MAX_ZIP and hashlib.sha256(data).hexdigest() == ui["sha256"], "Baseline artifact hash mismatch")
    return data


def overlay(baseline, candidate, output):
    """Reuse tested backend/dependency bytes; replace only regular web assets."""
    files = {}
    with zipfile.ZipFile(io.BytesIO(baseline)) as archive:
        require(sum(i.file_size for i in archive.infolist()) <= MAX_ZIP, "Baseline exceeds uncompressed limit")
        for entry in archive.infolist():
            name = entry.filename
            require(name not in files and not name.startswith("/") and ".." not in PurePosixPath(name).parts
                    and "\\" not in name and (entry.external_attr >> 16 & 0o170000) in (0, 0o100000),
                    "Unsafe baseline ZIP entry")
            files[name] = archive.read(entry)
    files = {name: data for name, data in files.items() if not name.startswith(WEB)}
    for row in git("ls-tree", "-rz", sha(candidate), "--", WEB).split(b"\0"):
        if not row:
            continue
        meta, raw = row.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        path = raw.decode()
        require(mode == "100644" and kind == "blob" and presentation(path), "Non-presentation file in candidate web tree")
        require(int(git("cat-file", "-s", oid)) <= 20 * 1024 * 1024, "Presentation asset exceeds 20 MiB limit")
        files[path] = git("cat-file", "blob", oid)
    require(any(p.startswith(WEB) for p in files), "Candidate contains no viewer assets")
    size = sum(len(data) for data in files.values())
    require(size <= MAX_ZIP, "Viewer package exceeds uncompressed limit")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    manifest = {"sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "files": len(files),
                "uncompressed_bytes": size, "kind": "application-code", "packaging": "verified-backend-reuse"}
    output.with_suffix(".json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def publish(s3, selection, report):
    require(selection["target"] == "live", "Preview cannot update the stable code pointer")
    require(report.get("status") in {"verified", "review_ready", "viewer_verified"} and
            report.get("sha") == selection["sha"] and report.get("accepted_release_changed") is False,
            "Only successfully verified code can become a baseline")
    old = selection["baseline"]
    viewer_only = selection["lane"] == "viewer"
    require(not viewer_only or old is not None, "Viewer deployment has no full baseline")
    value = {"schema": SCHEMA, "viewer_sha": report["sha"], "worker_sha": old["worker_sha"] if viewer_only else report["sha"],
             "ui": report["ui"], "full_deployment_id": old["full_deployment_id"] if viewer_only else report["deployment_id"],
             "viewer_deployment_id": report["deployment_id"]}
    validate_pointer(value)
    # Last accepted corpus pointer and data timestamps are never touched here.
    conditional = {"IfMatch": selection["baseline_etag"]} if selection["baseline_etag"] else {"IfNoneMatch": "*"}
    response = s3.put_object(Bucket=BUCKET, Key=POINTER, Body=json.dumps(value, sort_keys=True).encode(),
                             ExpectedBucketOwner=ACCOUNT, ServerSideEncryption="AES256", **conditional)
    require(response.get("VersionId") not in (None, "", "null"), "Code pointer requires bucket versioning")
    return value


def publish_guard(env, identity, report):
    role = env.get("CLHEAR_DEPLOY_ROLE_ARN", "")
    require(bool(re.fullmatch(rf"arn:aws:iam::{ACCOUNT}:role/[A-Za-z0-9+=,.@_/-]+", role)), "Approved deployment role required")
    require(env.get("GITHUB_ACTIONS") == "true" and env.get("GITHUB_REPOSITORY") == "Reg42-ai/CLHEAR-MVP"
            and env.get("GITHUB_REF") == "refs/heads/main" and env.get("GITHUB_SHA") == report.get("sha")
            and env.get("GITHUB_WORKFLOW_REF") == "Reg42-ai/CLHEAR-MVP/.github/workflows/deploy-l1.yml@refs/heads/main"
            and env.get("CLHEAR_DEPLOY_ENVIRONMENT") == "clhear-l1"
            and env.get("ACTIONS_ID_TOKEN_REQUEST_URL") and env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
            and identity.get("Account") == ACCOUNT
            and identity.get("Arn", "").startswith(f"arn:aws:sts::{ACCOUNT}:assumed-role/{role.rsplit('/', 1)[1]}/"),
            "Stable code publication requires the authorized main deployment workflow")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("ci", "select", "package", "publish"))
    parser.add_argument("--sha")
    parser.add_argument("--target", choices=("live", "preview"), default="live")
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "ci":
            if __package__:
                from .deployment_eligibility import GitHub
            else:
                from deployment_eligibility import GitHub
            base = None
            try:
                base = full_ci_anchor(GitHub(os.environ.get("GH_TOKEN")))
                lane = classify(base, os.environ["GITHUB_SHA"]) if base else "full"
            except Exception:
                lane = "full"
            result = {"lane": lane, "full_ci_baseline": base}
        else:
            import boto3
            s3 = boto3.client("s3", region_name="us-east-1")
            if args.action == "select":
                if args.target == "live" and os.environ.get("CLHEAR_FAST_DEPLOY_ENABLED") != "true":
                    result = {"sha": sha(args.sha), "target": "live", "lane": "full", "fast_enabled": False,
                              "baseline": None, "baseline_etag": None, "reason": "fast_path_bootstrap_not_enabled"}
                else:
                    result = select(s3, args.sha, args.target, os.environ.get("GITHUB_SHA"))
            else:
                selection = json.loads(args.selection.read_text())
                if args.action == "package":
                    pointer, etag = read_pointer(s3)
                    require(pointer == selection["baseline"] and etag == selection["baseline_etag"], "Stable baseline changed during packaging")
                    require(selection["lane"] == "viewer", "Packaging requires a presentation-only selection")
                    overlay(code_bytes(s3, pointer["ui"], pointer["viewer_sha"]), selection["sha"], args.output)
                    return 0
                report = json.loads(args.report.read_text())
                publish_guard(os.environ, boto3.client("sts", region_name="us-east-1").get_caller_identity(), report)
                result = publish(s3, selection, report)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        if os.environ.get("GITHUB_OUTPUT") and "lane" in result:
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                output.write(f"lane={result['lane']}\n")
        print(json.dumps({key: result[key] for key in ("lane", "reason") if key in result}))
        return 0
    except Exception:
        # SDK bodies and presigned URLs must not enter the build log.
        print("Viewer release evidence or packaging failed; no fallback approval was granted.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
