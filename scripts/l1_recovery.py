"""Read the optional recovery selection committed with an owner-merged fix.

Selection alone grants no authority. The deployment controller uses this only
in a confirmed maintenance hold and still validates every recovery binding.
"""
from __future__ import annotations

import json
from pathlib import Path

if __package__:
    from .deployment_recovery import PLAN_DIRECTORY, RecoveryPlanError, _plan_id, _unique_object
else:  # Direct execution of scripts/deploy_l1.py.
    from deployment_recovery import PLAN_DIRECTORY, RecoveryPlanError, _plan_id, _unique_object


def load_active_plan_id(*, directory: Path | None = None) -> str | None:
    """Read a fixed, bounded selection file; directory is for tests only.

    Missing files and an explicit null disable automatic recovery. An invalid
    selection fails closed rather than searching for a different recovery plan.
    """
    root = (PLAN_DIRECTORY if directory is None else Path(directory)).resolve()
    path = root / "active-plan.json"
    if path.is_symlink() or path.resolve().parent != root:
        raise RecoveryPlanError("Active recovery selection must be a regular reviewed file")
    try:
        with path.open("rb") as stream:
            raw = stream.read(4097)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RecoveryPlanError("Active recovery selection is unreadable") from exc
    if len(raw) > 4096:
        raise RecoveryPlanError("Active recovery selection is too large")
    try:
        selected = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryPlanError("Active recovery selection is invalid JSON") from exc
    if type(selected) is not dict or set(selected) != {"plan_id"}:
        raise RecoveryPlanError("Invalid active recovery selection fields")
    return None if selected["plan_id"] is None else _plan_id(selected["plan_id"])
