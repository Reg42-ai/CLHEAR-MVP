"""Pass the persisted lane selection to the guarded viewer deployment controller."""
import argparse
import json
import os
from pathlib import Path

if __package__:
    from . import deploy_viewer
    from .viewer_release import require, validate_pointer
else:
    import deploy_viewer
    from viewer_release import require, validate_pointer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("live", "preview"), required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    selected = json.loads(args.selection.read_text())
    require(selected.get("lane") == "viewer" and selected.get("sha") == args.sha
            and selected.get("target") == args.target, "Viewer selection does not match the requested deployment")
    baseline = validate_pointer(selected["baseline"])
    values = {"target": args.target, "sha": args.sha, "worker-sha": baseline["worker_sha"],
              "baseline-sha": baseline["viewer_sha"], "baseline-key": baseline["ui"]["key"],
              "baseline-version": baseline["ui"]["version"], "baseline-sha256": baseline["ui"]["sha256"],
              "ui-key": os.environ["UI_KEY"], "ui-version": os.environ["UI_VERSION"],
              "ui-sha256": os.environ["UI_SHA256"],
              "deployment-id": f"viewer-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}",
              "output": str(args.output)}
    return deploy_viewer.main([item for key, value in values.items() for item in ("--" + key, value)] + ["--apply"])


if __name__ == "__main__":
    raise SystemExit(main())
