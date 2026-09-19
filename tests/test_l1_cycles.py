"""Normal worker/outbox paths, no publisher network or live corpus operations."""
import json
import uuid
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa

from app.clhear import workers
from app.clhear.l1 import cycles, workflow
from app.clhear.models import events, runs
from app.clhear.platform.events import Envelope, _row_to_envelope
from app.clhear.settings import get_settings


def envelope(kind, payload=None, *, producer="test.manual", subject="scope", event_id=None, ts=None):
    return Envelope(event_id=event_id or str(uuid.uuid4()), layer="l0", kind=kind, subject_ref=subject,
                    payload=payload or {}, producer=producer, ts=ts or datetime.now(timezone.utc).isoformat())


def dispatch(engine, monkeypatch, env, fleet):
    monkeypatch.setenv("CLHEAR_FLEET", fleet)
    return workers.handle_envelope(engine, None, env.model_dump_json())


def queued(engine, kind=None):
    with engine.connect() as conn:
        query = sa.select(events)
        if kind:
            query = query.where(events.c.kind == kind)
        return [_row_to_envelope(row) for row in conn.execute(query.order_by(events.c.id))]


def fake_cycle(monkeypatch, keys=("alpha", "beta"), blocked=()):
    from app.clhear.l1 import fleet, inventory, pipeline, registry_etoro, translation
    from app.clhear.platform import evals
    monkeypatch.setattr(cycles, "adapter_keys", lambda: list(keys))
    def plan(key):
        return [({"key": key + "/doc"}, SimpleNamespace(key=key, meta=lambda: SimpleNamespace(source_key=key + "/doc")))]
    monkeypatch.setattr(fleet, "fleet_plan", plan)
    monkeypatch.setattr(registry_etoro, "seed", Mock())
    # These orchestration tests stub ingestion/version IDs. Real translation
    # and language/source binding are covered by the translation worker tests.
    monkeypatch.setattr(translation, "build_english_view", lambda *a, **k: {"english_ready": True})
    monkeypatch.setattr(inventory, "planned_entries", lambda *a, **k: [])
    audit = {"audit_id": "audit-1", "inventory_hash": "hash-1", "sources": [{"source_key": key + "/doc"} for key in keys],
             "verified": len(keys) - len(blocked), "unresolved": len(blocked), "known_expected": len(keys),
             "known_expected_is_lower_bound": True, "status": "gaps"}
    audits = Mock(return_value=audit)
    monkeypatch.setattr(inventory, "run_inventory_audit", audits)
    monkeypatch.setattr(inventory, "inventory_summary", lambda *a, **k: audit)
    monkeypatch.setattr(inventory, "acceptance_status", lambda *a, **k: {"passed": False, "reasons": ["scope_not_verified"]})
    monkeypatch.setattr(evals, "run_suite", lambda *a, **k: {"passed": False, "scores": {}})
    monkeypatch.setattr(evals, "run_source_evals", lambda *a, **k: [])
    calls = []
    def ingest(engine, adapter, store, **kwargs):
        import os
        assert os.environ["CLHEAR_FLEET"] == "l1"
        assert kwargs["index_embeddings"] is False
        calls.append(adapter.key)
        return {"status": "rights-blocked" if adapter.key in blocked else "unchanged", "source": adapter.meta().source_key}
    monkeypatch.setattr(pipeline, "ingest", ingest)
    return calls, audits, audit


def start_and_plan(engine, monkeypatch):
    request = envelope("L1CycleRequested", {"scope": "all_publishers"})
    result = dispatch(engine, monkeypatch, request, "l0")
    discovery = queued(engine, "L1CycleDiscoveryRequested")[0]
    dispatch(engine, monkeypatch, discovery, "l1")
    advance = queued(engine, "L1CycleAdvanceRequested")[0]
    dispatch(engine, monkeypatch, advance, "l0")
    return result["cycle_id"], request


def finish(engine, monkeypatch):
    for advance in queued(engine, "L1CycleAdvanceRequested"):
        dispatch(engine, monkeypatch, advance, "l0")
    final = queued(engine, "L1CycleEvaluationRequested")
    assert len(final) == 1
    return dispatch(engine, monkeypatch, final[0], "l1")


def test_all_32_normal_lanes_fanout_once_and_final_review_after_children(engine, monkeypatch):
    keys = cycles.adapter_keys()
    assert len(keys) == 32
    calls, audits, _ = fake_cycle(monkeypatch, keys, blocked=(keys[0],))
    monkeypatch.setenv("CLHEAR_VIEWER_SNAPSHOT_S3_URI", "s3://fixture/private/candidate.db")
    cycle_id, request = start_and_plan(engine, monkeypatch)
    assert dispatch(engine, monkeypatch, request, "l0") is None
    children = queued(engine, "AdapterRunRequested")
    assert len(children) == 32
    assert {e.payload["adapter"] for e in children} == set(keys)
    assert not queued(engine, "L1CycleEvaluationRequested")
    for env in children:
        result = dispatch(engine, monkeypatch, env, "l1")
        assert result["acceptance"] == "pending_cycle_evaluation"
        assert dispatch(engine, monkeypatch, env, "l1") is None
    assert len(calls) == 32
    result = finish(engine, monkeypatch)
    assert result["status"] == "completed_for_review"
    assert not result["scheduler_delivery_verified"]
    assert not result["accepted_release"]
    assert result["nightly_schedule_validation"] == "pending"
    assert audits.call_count == 2  # discovery then final audit, not 64 full-corpus audits
    snapshots = queued(engine, "ViewerSnapshotRequested")
    assert len(snapshots) == 1
    assert snapshots[0].payload["job_id"] == cycle_id
    summary = cycles.cycle_summary(engine, cycle_id)
    assert len(summary["children"]) == 32
    assert all(c["status"] in {"completed", "completed_for_review"} for c in summary["children"])
    assert summary["cycles"][0]["manifest"]["manifest_hash"]


def test_finra_scoped_cycle_runs_only_the_finra_lane(engine, monkeypatch):
    """The rulebook cycle the deployment requests first: FINRA discovery, one
    AdapterRunRequested for the finra lane, review-complete without touching the
    other 31 lanes or waiting for the whole-publisher cycle."""
    keys = cycles.adapter_keys()
    calls, audits, _ = fake_cycle(monkeypatch, keys)
    monkeypatch.setenv("CLHEAR_VIEWER_SNAPSHOT_S3_URI", "s3://fixture/private/candidate.db")
    request = envelope("L1CycleRequested", {"scope": "finra", "cycle_id": "cycle-manual-l1-finra-9-1"})
    result = dispatch(engine, monkeypatch, request, "l0")
    assert result["cycle_id"] == "cycle-manual-l1-finra-9-1"
    dispatch(engine, monkeypatch, queued(engine, "L1CycleDiscoveryRequested")[0], "l1")
    dispatch(engine, monkeypatch, queued(engine, "L1CycleAdvanceRequested")[0], "l0")
    assert dispatch(engine, monkeypatch, request, "l0") is None
    children = queued(engine, "AdapterRunRequested")
    assert [e.payload["adapter"] for e in children] == ["finra"]
    assert cycles.cycle_summary(engine, result["cycle_id"])["cycles"][0]["manifest"]["adapter_keys"] == ["finra"]
    for env in children:
        dispatch(engine, monkeypatch, env, "l1")
    assert calls == ["finra"]
    final = finish(engine, monkeypatch)
    assert final["status"] == "completed_for_review" and not final["accepted_release"]
    with pytest.raises(ValueError, match="FINRA rulebooks"):
        dispatch(engine, monkeypatch, envelope("L1CycleRequested", {"scope": "nasdaq", "cycle_id": "cycle-manual-x"}), "l0")


def test_retry_preserves_completed_source_and_resumes_same_child(engine, monkeypatch):
    from app.clhear.l1 import fleet, pipeline
    fake_cycle(monkeypatch, ("alpha",))
    adapters = [SimpleNamespace(key="alpha", meta=lambda key=key: SimpleNamespace(source_key=key))
                for key in ("alpha/doc", "alpha/second")]
    monkeypatch.setattr(fleet, "fleet_plan", lambda key: [({"key": a.meta().source_key}, a) for a in adapters])
    calls = []
    def ingest(engine, adapter, store, **kwargs):
        key = adapter.meta().source_key
        calls.append(key)
        return {"status": "failed" if key == "alpha/second" and calls.count(key) == 1 else "unchanged"}
    monkeypatch.setattr(pipeline, "ingest", ingest)
    cycle_id, _ = start_and_plan(engine, monkeypatch)
    child = queued(engine, "AdapterRunRequested")[0]
    with pytest.raises(workers.AdapterRunIncomplete):
        dispatch(engine, monkeypatch, child, "l1")
    assert cycles.cycle_summary(engine, cycle_id)["children"][0]["status"] == "retrying"
    with engine.begin() as conn:
        conn.execute(workflow.tasks.update().values(next_attempt_at=workflow.utcnow()))
    dispatch(engine, monkeypatch, child, "l1")
    assert calls == ["alpha/doc", "alpha/second", "alpha/second"]
    assert finish(engine, monkeypatch)["status"] == "completed_for_review"


def test_exhausted_child_is_failed_not_accepted_and_other_lanes_finish(engine, monkeypatch):
    from app.clhear.l1 import pipeline
    fake_cycle(monkeypatch)
    monkeypatch.setattr(pipeline, "ingest", lambda engine, adapter, store, **kwargs: {"status": "failed" if adapter.key == "alpha" else "unchanged"})
    cycle_id, _ = start_and_plan(engine, monkeypatch)
    child, other = queued(engine, "AdapterRunRequested")
    for _ in range(3):
        with pytest.raises(workers.AdapterRunIncomplete):
            dispatch(engine, monkeypatch, child, "l1")
        with engine.begin() as conn:
            conn.execute(workflow.tasks.update().values(next_attempt_at=workflow.utcnow()))
    dispatch(engine, monkeypatch, other, "l1")
    assert finish(engine, monkeypatch)["status"] == "failed"
    evidence = cycles.cycle_summary(engine, cycle_id)
    assert {c["status"] for c in evidence["children"]} == {"failed", "completed"}


def test_manual_outcomes_cannot_satisfy_scheduled_occurrence(engine, monkeypatch):
    fake_cycle(monkeypatch)
    start_and_plan(engine, monkeypatch)
    for child in queued(engine, "AdapterRunRequested"):
        dispatch(engine, monkeypatch, child, "l1")
    finish(engine, monkeypatch)
    scores, passed = cycles.schedule_evidence(engine)
    assert not passed
    assert scores["attempted_24h"] == 0
    assert scores["missing_adapters"] == ["alpha", "beta"]


def test_scheduled_identities_group_real_events_and_count_blocked_attempts(engine, monkeypatch):
    calls, audits, _ = fake_cycle(monkeypatch, blocked=("alpha",))
    at = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for key in ("alpha", "beta"):
        env = envelope("AdapterRunRequested", {"adapter": key}, producer="eventbridge", subject=key, ts=at.isoformat())
        dispatch(engine, monkeypatch, env, "l1")
    assert not calls
    for command in queued(engine, "L1CycleAdvanceRequested"):
        dispatch(engine, monkeypatch, command, "l0")
    dispatch(engine, monkeypatch, queued(engine, "L1CycleDiscoveryRequested")[0], "l1")
    for command in queued(engine, "L1CycleAdvanceRequested"):
        dispatch(engine, monkeypatch, command, "l0")
    for command in queued(engine, "AdapterRunRequested"):
        dispatch(engine, monkeypatch, command, "l1")
    scores, passed = cycles.schedule_evidence(engine, now=at + timedelta(hours=1))
    assert passed  # attempted is distinct from accepted; permission blockers remain visible
    assert scores["delivery_verified"] and scores["attempted_24h"] == 2
    assert scores["blocked"] == ["alpha/doc"]
    result = finish(engine, monkeypatch)
    assert result["scheduler_delivery_verified"] and result["status"] == "completed_for_review"
    assert audits.call_count == 2
    assert all(c["command_event_id"] != c["event_id"] for c in cycles.cycle_summary(engine, result["cycle_id"])["children"])


def test_schedule_evaluation_after_midnight_uses_own_frozen_occurrence(engine, monkeypatch):
    from app.clhear.l1 import inventory
    from app.clhear.platform import evals
    fake_cycle(monkeypatch)
    at = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    cycle_id = "cycle-scheduled-" + at.strftime("%Y%m%dT%H%MZ")
    for key in ("alpha", "beta"):
        dispatch(engine, monkeypatch, envelope("AdapterRunRequested", {"adapter": key},
                 producer="eventbridge", ts=at.isoformat()), "l1")
    for command in queued(engine, "L1CycleAdvanceRequested"):
        dispatch(engine, monkeypatch, command, "l0")
    dispatch(engine, monkeypatch, queued(engine, "L1CycleDiscoveryRequested")[0], "l1")
    for command in queued(engine, "L1CycleAdvanceRequested"):
        dispatch(engine, monkeypatch, command, "l0")
    for command in queued(engine, "AdapterRunRequested"):
        dispatch(engine, monkeypatch, command, "l1")
    # A newer inventory cannot rewrite an earlier occurrence's frozen tasks.
    monkeypatch.setattr(inventory, "inventory_summary", lambda *a, **k: {
        "sources": [{"source_key": "later/doc"}], "known_expected_is_lower_bound": True})
    scores, passed = evals.l1_schedule_kept(engine, cycle_id)
    assert passed
    assert scores["cycle_id"] == cycle_id
    assert scores["scheduled_sources"] == 2 and scores["missed"] == []
    assert not cycles.schedule_evidence(engine)[1]  # today's delivery has not occurred
    requested = []
    def evaluate(engine, name, **kwargs):
        requested.append((name, kwargs.get("source_key")))
        return {"passed": False, "scores": {}}
    monkeypatch.setattr(evals, "run_suite", evaluate)
    finish(engine, monkeypatch)
    assert ("l1_schedule_kept", cycle_id) in requested


def test_schedule_evidence_rejects_manual_cycle_binding(engine):
    from app.clhear.platform import evals
    with pytest.raises(ValueError, match="scheduled occurrence"):
        evals.l1_schedule_kept(engine, "cycle-manual-fixture")


def test_duplicate_occurrence_and_revision_mix_fail_closed(engine, monkeypatch):
    fake_cycle(monkeypatch)
    monkeypatch.setenv("CLHEAR_CODE_REVISION", "a" * 40)
    at = "2026-09-16T00:00:00Z"
    one = envelope("AdapterRunRequested", {"adapter": "alpha"}, producer="eventbridge", ts=at)
    cycle_id, child_id = cycles.scheduled_child(engine, one)
    assert cycles.scheduled_child(engine, one) == (cycle_id, child_id)
    with pytest.raises(ValueError, match="different scheduler event"):
        cycles.scheduled_child(engine, one.model_copy(update={"event_id": "other"}))
    with engine.begin() as conn:
        conn.execute(cycles.children.update().where(cycles.children.c.child_id == child_id).values(command_event_id=one.event_id))
    monkeypatch.setenv("CLHEAR_CODE_REVISION", "b" * 40)
    with pytest.raises(ValueError, match="mix deployed"):
        cycles.child_context(engine, cycle_id, child_id, one)


def test_request_cli_only_emits_idempotent_l0_command(engine, monkeypatch, capsys):
    monkeypatch.setenv("CLHEAR_FLEET", "l1")
    with pytest.raises(SystemExit) as error:
        workers.cli(["--request-l1-cycle", "--verification-id", "fixture"])
    assert error.value.code == 1
    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    assert workers.cli(["--request-l1-cycle", "--verification-id", "fixture"]) == 0
    assert workers.cli(["--request-l1-cycle", "--verification-id", "fixture"]) == 0
    assert len(queued(engine, "L1CycleRequested")) == 1
    assert workers.cli(["--request-l1-cycle", "--scope", "finra", "--verification-id", "finra-fixture"]) == 0
    assert queued(engine, "L1CycleRequested")[-1].payload["scope"] == "finra"
    with pytest.raises(ValueError, match="another scope"):
        cycles.request_cycle(engine, "finra-fixture", scope="all_publishers")
    assert not cycles.cycle_summary(engine)["cycles"]


def test_workflow_pagination_has_exact_totals_and_no_hidden_tasks(engine):
    workflow.ensure_job(engine, "wide", "test", "manual", "event")
    with engine.begin() as conn:
        conn.execute(workflow.tasks.insert(), [{"task_id": f"task-{n:04}", "job_id": "wide", "source_key": f"doc/{n}",
            "worker": "l1.test", "status": "blocked", "attempt": 1, "max_attempts": 3,
            "created_at": workflow.utcnow(), "summary": {}} for n in range(405)])
    first = workflow.workflow_summary(engine, job_id="wide")
    second = workflow.workflow_summary(engine, job_id="wide", task_offset=200)
    third = workflow.workflow_summary(engine, job_id="wide", task_offset=400)
    assert first["task_total"] == second["task_total"] == third["task_total"] == 405
    assert first["pagination"]["tasks"]["has_more"]
    assert not third["pagination"]["tasks"]["has_more"]
    assert len({t["task_id"] for page in (first, second, third) for t in page["tasks"]}) == 405


def test_concurrent_advance_publishes_each_child_once(engine, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    fake_cycle(monkeypatch)
    request = envelope("L1CycleRequested", {"scope": "all_publishers"})
    cycle_id = dispatch(engine, monkeypatch, request, "l0")["cycle_id"]
    dispatch(engine, monkeypatch, queued(engine, "L1CycleDiscoveryRequested")[0], "l1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: cycles.advance(engine, cycle_id), range(2)))
    assert all(r["status"] == "running" for r in results)
    assert len(queued(engine, "AdapterRunRequested")) == 2


def test_discovery_failure_is_bounded_and_never_dispatches_imports(engine, monkeypatch):
    calls, audits, _ = fake_cycle(monkeypatch)
    audits.side_effect = RuntimeError("fixture unavailable")
    cycle_id = dispatch(engine, monkeypatch, envelope("L1CycleRequested"), "l0")["cycle_id"]
    command = queued(engine, "L1CycleDiscoveryRequested")[0]
    for _ in range(3):
        with pytest.raises(RuntimeError, match="fixture unavailable"):
            dispatch(engine, monkeypatch, command, "l1")
    assert cycles.cycle_summary(engine, cycle_id)["cycles"][0]["status"] == "failed"
    assert not queued(engine, "AdapterRunRequested")
    assert not calls


def test_empty_lane_and_declaration_are_explicit_review_gaps_not_network_calls(engine, monkeypatch):
    from app.clhear.l1 import fleet
    calls, _, _ = fake_cycle(monkeypatch)
    gap = SimpleNamespace(key="alpha", meta=lambda: SimpleNamespace(source_key="alpha/catalog"),
                          declaration_gap={"code": "collection_requires_discovery", "reason": "not a document"})
    monkeypatch.setattr(fleet, "fleet_plan", lambda key: [({"key": "alpha/catalog"}, gap)] if key == "alpha" else [])
    cycle_id, _ = start_and_plan(engine, monkeypatch)
    for command in queued(engine, "AdapterRunRequested"):
        dispatch(engine, monkeypatch, command, "l1")
    result = finish(engine, monkeypatch)
    assert result["status"] == "completed_for_review"
    assert not calls
    assert {c["status"] for c in cycles.cycle_summary(engine, cycle_id)["children"]} == {"completed_for_review"}


def test_cycle_identity_comes_from_deployed_environment_not_request(engine, monkeypatch):
    fake_cycle(monkeypatch)
    monkeypatch.setenv("CLHEAR_CODE_REVISION", "a" * 40)
    monkeypatch.setenv("CLHEAR_WORKER_IMAGE_DIGEST", "sha256:" + "b" * 64)
    requested = envelope("L1CycleRequested", {"code_revision": "f" * 40, "worker_image_digest": "forged"})
    cycle_id = dispatch(engine, monkeypatch, requested, "l0")["cycle_id"]
    state = cycles.cycle_summary(engine, cycle_id)["cycles"][0]
    assert state["code_revision"] == "a" * 40
    assert state["worker_image_digest"] == "sha256:" + "b" * 64


def test_final_snapshot_request_failure_rolls_back_terminal_cycle_state(engine, monkeypatch):
    from app.clhear.l1 import viewer_snapshot
    fake_cycle(monkeypatch)
    cycle_id, _ = start_and_plan(engine, monkeypatch)
    for command in queued(engine, "AdapterRunRequested"):
        dispatch(engine, monkeypatch, command, "l1")
    for command in queued(engine, "L1CycleAdvanceRequested"):
        dispatch(engine, monkeypatch, command, "l0")
    monkeypatch.setattr(viewer_snapshot, "request_refresh", Mock(side_effect=RuntimeError("outbox transaction failed")))
    with pytest.raises(RuntimeError, match="outbox transaction"):
        cycles.finish_cycle(engine, cycle_id, {"acceptance_passed": False})
    assert cycles.cycle_summary(engine, cycle_id)["cycles"][0]["status"] == "evaluating"


def test_missing_scheduler_receipt_never_synthesizes_other_lanes(engine, monkeypatch):
    calls, audits, _ = fake_cycle(monkeypatch)
    at = "2026-09-16T00:00:00Z"
    real = envelope("AdapterRunRequested", {"adapter": "alpha"}, producer="eventbridge", ts=at)
    result = dispatch(engine, monkeypatch, real, "l1")
    for command in queued(engine, "L1CycleAdvanceRequested"):
        dispatch(engine, monkeypatch, command, "l0")
    assert cycles.cycle_summary(engine, result["cycle_id"])["cycles"][0]["status"] == "collecting"
    assert not queued(engine, "AdapterRunRequested") and not queued(engine, "L1CycleDiscoveryRequested")
    assert not calls and not audits.called


def test_missing_authorized_artifact_is_terminal_review_dependency(engine, monkeypatch):
    from app.clhear.l1 import pipeline
    fake_cycle(monkeypatch, ("alpha",))
    ingest = Mock(return_value={"status": "awaiting-artifact", "error_type": "FileNotFoundError"})
    monkeypatch.setattr(pipeline, "ingest", ingest)
    cycle_id, _ = start_and_plan(engine, monkeypatch)
    command = queued(engine, "AdapterRunRequested")[0]
    dispatch(engine, monkeypatch, command, "l1")
    assert dispatch(engine, monkeypatch, command, "l1") is None
    assert ingest.call_count == 1
    assert finish(engine, monkeypatch)["status"] == "completed_for_review"


@pytest.mark.parametrize("changed_binding", [False, True])
def test_acceptance_binds_exact_child_output_and_deployed_code(engine, monkeypatch, changed_binding):
    from app.clhear.l1 import pipeline, inventory
    from app.clhear.platform import evals
    _, _, audit = fake_cycle(monkeypatch, ("alpha",))
    monkeypatch.setenv("CLHEAR_CODE_REVISION", "a" * 40)
    monkeypatch.setenv("CLHEAR_WORKER_IMAGE_DIGEST", "sha256:" + "b" * 64)
    audit["sources"][0].update(source_version_id=2 if changed_binding else 1, content_hash="exact-original")
    audit.update(verified=1, unresolved=0)
    monkeypatch.setattr(pipeline, "ingest", lambda *a, **k: {"status": "unchanged", "source_version_id": 1, "content_hash": "exact-original"})
    monkeypatch.setattr(inventory, "acceptance_status", lambda *a, **k: {"passed": True, "reasons": [], "audit_id": audit["audit_id"]})
    monkeypatch.setattr(evals, "run_suite", lambda *a, **k: {"passed": True, "scores": {}})
    start_and_plan(engine, monkeypatch)
    command = queued(engine, "AdapterRunRequested")[0]
    dispatch(engine, monkeypatch, command, "l1")
    # Collection declarations are not expected verbatim documents and cannot
    # either satisfy or prevent complete document output binding.
    task_id = workflow.ensure_task(engine, command.payload["job_id"], "finra/rulebook")
    with engine.begin() as conn:
        conn.execute(workflow.tasks.update().where(workflow.tasks.c.task_id == task_id).values(status="blocked"))
    result = finish(engine, monkeypatch)
    assert result["acceptance_passed"] is not changed_binding
    assert result["status"] == ("completed_for_review" if changed_binding else "candidate_verified")
    assert [b["source_key"] for b in result["output_bindings"]["bindings"]] == ["alpha/doc"]
    assert not result["accepted_release"]


@pytest.mark.parametrize("gap", ["blocked", "missing", "superseded_audit"])
def test_fresh_previous_versions_cannot_fill_this_cycles_unverified_outputs(engine, monkeypatch, gap):
    from app.clhear.l1 import pipeline, inventory
    from app.clhear.platform import evals
    _, _, audit = fake_cycle(monkeypatch)
    monkeypatch.setenv("CLHEAR_CODE_REVISION", "a" * 40)
    monkeypatch.setenv("CLHEAR_WORKER_IMAGE_DIGEST", "sha256:" + "b" * 64)
    for source in audit["sources"]:
        source.update(source_version_id=1 if source["source_key"] == "alpha/doc" else 2,
                      content_hash="fresh-prior-original")
    audit.update(verified=2, unresolved=0)
    def ingest(engine, adapter, *args, **kwargs):
        return {"status": "rights-blocked" if adapter.key == "beta" and gap == "blocked" else "unchanged",
                "source_version_id": 1 if adapter.key == "alpha" else 2, "content_hash": "fresh-prior-original"}
    monkeypatch.setattr(pipeline, "ingest", ingest)
    monkeypatch.setattr(inventory, "acceptance_status", lambda *a, **k: {
        "passed": True, "reasons": [], "audit_id": "newer-global-audit" if gap == "superseded_audit" else audit["audit_id"]})
    monkeypatch.setattr(evals, "run_suite", lambda *a, **k: {"passed": True, "scores": {}})
    cycle_id, _ = start_and_plan(engine, monkeypatch)
    for command in queued(engine, "AdapterRunRequested"):
        dispatch(engine, monkeypatch, command, "l1")
    if gap == "missing":
        with engine.begin() as conn:
            conn.execute(workflow.tasks.delete().where(workflow.tasks.c.source_key == "beta/doc"))
    result = finish(engine, monkeypatch)
    assert result["acceptance_passed"] is False
    assert result["status"] == "completed_for_review" and not result["accepted_release"]
    if gap == "superseded_audit":
        assert result["acceptance_audit_current"] is False
    else:
        assert result["output_bindings"]["unverified_sources"] == ["beta/doc"]
        assert result["output_bindings"]["passed"] is False


def test_discovery_continues_before_freezing_and_keeps_initial_date_and_job(engine, monkeypatch):
    import copy
    _, audits, audit = fake_cycle(monkeypatch)
    one, two = copy.deepcopy(audit), copy.deepcopy(audit)
    one.update(audit_id="batch-one", discovery={"pending_pages": 7})
    two.update(audit_id="batch-two", discovery={"pending_pages": 0})
    audits.side_effect = [one, two]
    result = dispatch(engine, monkeypatch, envelope("L1CycleRequested"), "l0")
    cycle_id = result["cycle_id"]
    with engine.begin() as conn:
        conn.execute(cycles.cycles.update().where(cycles.cycles.c.cycle_id == cycle_id).values(
            created_at=datetime(2026, 9, 15, 23, 59, tzinfo=timezone.utc)))
    first = dispatch(engine, monkeypatch, queued(engine, "L1CycleDiscoveryRequested")[0], "l1")
    assert first["pending_pages"] == 7
    state = cycles.cycle_summary(engine, cycle_id)["cycles"][0]
    assert state["status"] == "discovering" and not state["manifest"]
    assert not queued(engine, "L1CycleAdvanceRequested") and not queued(engine, "AdapterRunRequested")
    second = queued(engine, "L1CycleDiscoveryRequested")[1]
    dispatch(engine, monkeypatch, second, "l1")
    assert cycles.cycle_summary(engine, cycle_id)["cycles"][0]["status"] == "planned"
    assert {c.kwargs["discovery_cycle_date"] for c in audits.call_args_list} == {"2026-09-15"}
    assert len({c.kwargs["job_id"] for c in audits.call_args_list}) == 1


def test_stalled_frontier_retries_existing_command_without_outbox_storm(engine, monkeypatch):
    _, _, audit = fake_cycle(monkeypatch)
    audit["discovery"] = {"pending_pages": 2}
    result = dispatch(engine, monkeypatch, envelope("L1CycleRequested"), "l0")
    dispatch(engine, monkeypatch, queued(engine, "L1CycleDiscoveryRequested")[0], "l1")
    continuation = queued(engine, "L1CycleDiscoveryRequested")[1]
    with pytest.raises(workflow.RetryDeferred):
        dispatch(engine, monkeypatch, continuation, "l1")
    assert len(queued(engine, "L1CycleDiscoveryRequested")) == 2
    assert cycles.cycle_summary(engine, result["cycle_id"])["cycles"][0]["status"] == "discovering"


def test_deployment_change_fails_pending_cycle_without_reimporting_completed_sources(engine, monkeypatch):
    calls, _, _ = fake_cycle(monkeypatch)
    monkeypatch.setenv("CLHEAR_CODE_REVISION", "a" * 40)
    cycle_id, _ = start_and_plan(engine, monkeypatch)
    first, second = queued(engine, "AdapterRunRequested")
    dispatch(engine, monkeypatch, first, "l1")
    monkeypatch.setenv("CLHEAR_CODE_REVISION", "b" * 40)
    with pytest.raises(cycles.CycleRevisionChanged, match="original worker revision"):
        dispatch(engine, monkeypatch, second, "l1")
    cycles.reconcile(engine)
    assert calls == ["alpha"]
    summary = cycles.cycle_summary(engine, cycle_id)
    assert summary["cycles"][0]["status"] == "failed"
    assert [c["status"] for c in summary["children"]] == ["completed", "failed"]


def test_full_cycle_with_unchanged_repeat_chains_one_follow_up_and_compares_versions(engine, monkeypatch):
    """The post-deployment request: a full cycle, then the same scope again that must
    change nothing. The repeat is requested once, from the first cycle's terminal
    result, and its evaluation compares every bound source version with the first."""
    keys = ("alpha", "beta")
    calls, audits, audit = fake_cycle(monkeypatch, keys)
    from app.clhear.l1 import pipeline
    versions = {"alpha/doc": (11, "a" * 64), "beta/doc": (12, "b" * 64)}
    def ingest(engine, adapter, store, **kwargs):
        calls.append(adapter.key)
        vid, digest = versions[adapter.meta().source_key]
        return {"status": "unchanged", "source": adapter.meta().source_key, "source_version_id": vid, "content_hash": digest}
    monkeypatch.setattr(pipeline, "ingest", ingest)
    audit["sources"] = [{"source_key": k + "/doc", "source_version_id": versions[k + "/doc"][0], "content_hash": versions[k + "/doc"][1]} for k in keys]
    monkeypatch.setenv("CLHEAR_VIEWER_SNAPSHOT_S3_URI", "s3://fixture/private/candidate.db")
    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    receipt = cycles.request_cycle(engine, "l1-cycle-77-1", unchanged_repeat=True)
    assert receipt["follow_up"] == {"kind": "unchanged_repeat", "cycle_id": "cycle-manual-l1-cycle-77-1-repeat"}
    assert cycles.request_cycle(engine, "l1-cycle-77-1", unchanged_repeat=True)["event_id"] == receipt["event_id"]  # idempotent
    with pytest.raises(ValueError):
        cycles.request_cycle(engine, "l1-cycle-77-1", scope="registered")

    def run_cycle():
        request = queued(engine, "L1CycleRequested")[-1]
        dispatch(engine, monkeypatch, request, "l0")
        for discovery in queued(engine, "L1CycleDiscoveryRequested"):
            dispatch(engine, monkeypatch, discovery, "l1")
        for advance in queued(engine, "L1CycleAdvanceRequested"):
            dispatch(engine, monkeypatch, advance, "l0")
        for child in queued(engine, "AdapterRunRequested"):
            dispatch(engine, monkeypatch, child, "l1")
        for advance in queued(engine, "L1CycleAdvanceRequested"):
            dispatch(engine, monkeypatch, advance, "l0")
        final = queued(engine, "L1CycleEvaluationRequested")[-1]
        return dispatch(engine, monkeypatch, final, "l1")

    first = run_cycle()
    assert first["cycle_id"] == "cycle-manual-l1-cycle-77-1" and first["status"] == "completed_for_review"
    assert first["follow_up_requested"] == "cycle-manual-l1-cycle-77-1-repeat"
    requests = queued(engine, "L1CycleRequested")
    assert len(requests) == 2 and requests[-1].payload == {"cycle_id": "cycle-manual-l1-cycle-77-1-repeat", "scope": "all_publishers",
                                                          "repeat_of": "cycle-manual-l1-cycle-77-1"}
    repeat = run_cycle()
    assert repeat["cycle_id"] == "cycle-manual-l1-cycle-77-1-repeat" and "follow_up_requested" not in repeat
    assert repeat["unchanged_repeat"]["passed"] and repeat["unchanged_repeat"]["compared"] == 2
    assert repeat["unchanged_repeat"] == {**repeat["unchanged_repeat"], "changed": [], "missing": [], "added": [], "repeat_of": first["cycle_id"]}
    assert len(queued(engine, "L1CycleRequested")) == 2  # the repeat does not chain another repeat
    summary = cycles.cycle_summary(engine, repeat["cycle_id"])
    assert summary["cycles"][0]["manifest"]["repeat_of"] == first["cycle_id"]

    # a repeat that sees a different version is reported, not accepted
    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    cycles.request_cycle(engine, "l1-cycle-77-2", unchanged_repeat=True)
    run_cycle()
    versions["beta/doc"] = (13, "c" * 64)  # the publisher changed between the first cycle and its repeat
    audit["sources"][1] = {"source_key": "beta/doc", "source_version_id": 13, "content_hash": "c" * 64}
    changed = run_cycle()
    assert not changed["unchanged_repeat"]["passed"] and changed["unchanged_repeat"]["changed"] == ["beta/doc"]
    assert changed["unchanged_repeat"]["reason"] == "repeat_not_verified"
