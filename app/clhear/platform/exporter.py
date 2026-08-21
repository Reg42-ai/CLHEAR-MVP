"""Release snapshot -> public `clhear` repo (HLD §7.1).

Allow-list export, not deny-list: the snapshot is COMPILED from explicitly
public fields (refs, hashes, family graphs, eval scores). No code path here
reads clause text, so restricted material is excluded by construction; when
clause refs join in P1+ they must come through the clauses_public view only.
"""
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
import yaml
from sqlalchemy.engine import Engine

from app.clhear.models import eval_runs
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.exporter")

RELEASE_TAG_PREFIX = "clhear-v"


def compile_snapshot(engine: Engine, release: str) -> dict:
    """Compile the public snapshot. P0: empty-but-valid structure + eval scores.

    P1+ adds sources/families/version-hashes from l1_sources (public rows only).
    """
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(eval_runs).where(eval_runs.c.release == release).order_by(eval_runs.c.id)
        ).all()
    scores = [
        {
            "suite": row.suite,
            "source_key": row.source_key,
            "scores": row.scores if isinstance(row.scores, dict) else json.loads(row.scores),
            "passed": bool(row.passed),
            "ran_at": str(row.ran_at),
        }
        for row in rows
    ]
    return {
        "release": release,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "spec_version": 1,
        "source_families": [],
        "sources": [],
        "source_versions": [],
        "eval_scores": scores,
        "all_evals_passed": bool(scores) and all(s["passed"] for s in scores),
    }


def write_snapshot(snapshot: dict, repo_dir: Path) -> list[Path]:
    """Write snapshot into the public-repo layout: /spec /evals /snapshots."""
    release = snapshot["release"]
    written = []
    snap_dir = repo_dir / "snapshots" / release
    snap_dir.mkdir(parents=True, exist_ok=True)
    for name, dump in (
        ("snapshot.json", lambda p: p.write_text(json.dumps(snapshot, indent=2, default=str))),
        ("snapshot.yaml", lambda p: p.write_text(yaml.safe_dump(snapshot, sort_keys=False))),
    ):
        path = snap_dir / name
        dump(path)
        written.append(path)
    evals_dir = repo_dir / "evals"
    evals_dir.mkdir(parents=True, exist_ok=True)
    scores_path = evals_dir / f"{release}.json"
    scores_path.write_text(
        json.dumps({"release": release, "eval_scores": snapshot["eval_scores"]}, indent=2, default=str)
    )
    written.append(scores_path)
    return written


def export_release(engine: Engine, release: str, repo_dir: Path | None = None, push: bool = False) -> dict:
    """Compile + write a release snapshot; optionally commit and push.

    Called on git tag `clhear-vX.Y.Z` (CI) or manually. Refuses to export a
    release whose evals did not all pass (evals are gates).
    """
    from app.clhear.platform.evals import release_gate

    if not release_gate(engine, release):
        raise RuntimeError(f"release gate failed for {release}: evals missing or not all passed")

    settings = get_settings()
    repo_dir = repo_dir or Path(settings.clhear_public_repo_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)

    snapshot = compile_snapshot(engine, release)
    written = write_snapshot(snapshot, repo_dir)

    if push:
        remote = settings.clhear_public_repo_url
        if not remote:
            raise RuntimeError("CLHEAR_PUBLIC_REPO_URL is not configured")
        if settings.clhear_export_git_token and remote.startswith("https://"):
            remote = remote.replace("https://", f"https://x-access-token:{settings.clhear_export_git_token}@")
        if not (repo_dir / ".git").exists():
            subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=clhear-exporter", "-c", "user.email=clhear@reg42.ai",
             "commit", "-m", f"snapshot {release}", "--allow-empty"],
            cwd=repo_dir, check=True, capture_output=True,
        )
        subprocess.run(["git", "push", remote, "HEAD:main"], cwd=repo_dir, check=True, capture_output=True)
        log.info("pushed snapshot %s to public repo", release)

    return {"release": release, "files": [str(p) for p in written], "snapshot": snapshot}


def main() -> int:
    """CLI: python -m app.clhear.platform.exporter <release> [--push]."""
    import sys

    from app.clhear.db import get_engine, run_migrations

    release = sys.argv[1]
    push = "--push" in sys.argv
    engine = get_engine()
    run_migrations(engine)
    result = export_release(engine, release, push=push)
    print(json.dumps({"release": result["release"], "files": result["files"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
