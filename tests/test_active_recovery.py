"""The committed selection never becomes an arbitrary file or guessed plan."""
import json

import pytest

from scripts.deployment_recovery import RecoveryPlanError, load_plan
from scripts.l1_recovery import load_active_plan_id


def test_committed_default_references_the_reviewed_original_plan():
    plan_id = load_active_plan_id()
    plan = load_plan(plan_id)
    assert plan_id == "l1-34967901665-1"
    assert plan["viewer"]["reserved_concurrency"] is None


def test_missing_or_explicitly_disabled_selection(tmp_path):
    assert load_active_plan_id(directory=tmp_path) is None
    (tmp_path / "active-plan.json").write_text('{"plan_id": null}')
    assert load_active_plan_id(directory=tmp_path) is None


def test_explicit_selection_is_returned_without_guessing_a_different_plan(tmp_path):
    (tmp_path / "active-plan.json").write_text('{"plan_id":"l1-123-2"}')
    assert load_active_plan_id(directory=tmp_path) == "l1-123-2"


@pytest.mark.parametrize("selection", [
    {}, [], {"plan_id": "../../other"}, {"plan_id": True}, {"plan_id": 3},
    {"plan_id": "l1-123-2", "path": "another.json"}, {"plan_id": "l1-0-1"},
    {"plan_id": "l1-123-2\n"},
])
def test_invalid_selection_fails_closed(tmp_path, selection):
    (tmp_path / "active-plan.json").write_text(json.dumps(selection))
    with pytest.raises(RecoveryPlanError):
        load_active_plan_id(directory=tmp_path)


@pytest.mark.parametrize("raw", [
    b'{"plan_id":"l1-123-2","plan_id":null}', b"invalid JSON", b"\xff", b" " * 4097,
])
def test_ambiguous_malformed_or_oversized_selection_fails_closed(tmp_path, raw):
    (tmp_path / "active-plan.json").write_bytes(raw)
    with pytest.raises(RecoveryPlanError):
        load_active_plan_id(directory=tmp_path)


@pytest.mark.parametrize("dangling", [False, True])
def test_symlink_selection_is_rejected_even_when_target_is_missing(tmp_path, dangling):
    target = tmp_path / "other.json"
    if not dangling:
        target.write_text('{"plan_id":"l1-123-2"}')
    (tmp_path / "active-plan.json").symlink_to(target)
    with pytest.raises(RecoveryPlanError, match="regular reviewed file"):
        load_active_plan_id(directory=tmp_path)
