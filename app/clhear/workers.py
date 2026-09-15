"""SQS consumer entrypoint (ECS clhear-workers): python -m app.clhear.workers

The worker loop does two jobs:
  1. relay the l0 outbox to SQS (events.relay_forever semantics, interleaved)
  2. consume envelopes from SQS and dispatch to per-kind handlers

Consumers are idempotent on event_id: each handled envelope is recorded in the
l0_platform.runs ledger and skipped if already present (HLD §6.1, §7.1).
"""
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import runs
from app.clhear.platform import events as l0_events
from app.clhear.platform import proposals as l0_proposals
from app.clhear.platform.events import Envelope
from app.clhear.platform.gateway import Gateway, Provider, parse_json_object
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.workers")

DUMMY_FLEET = "dummy"


def run_dummy_fleet(engine: Engine, subject_ref: str = "dummy/rehearsal-1") -> str:
    """P0 rehearsal fleet: one data change + outbox event in the same transaction."""
    started = time.monotonic()
    with engine.begin() as conn:
        event_id = l0_events.emit(
            conn,
            layer="l0",
            kind="DummyChanged",
            subject_ref=subject_ref,
            payload={"note": "dummy-fleet rehearsal"},
            producer="fleet.dummy",
        )
        conn.execute(
            runs.insert().values(
                fleet=DUMMY_FLEET,
                trigger="manual",
                inputs={"subject_ref": subject_ref},
                outputs={"event_id": event_id},
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )
    return event_id


def handle_dummy_changed(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """Rehearsal consumer: one routed call (triage-shaped) -> one proposal."""
    from app.clhear.platform.router import complete

    result = complete(
        gateway,
        "dummy.triage",
        prompt=f"Classify this candidate for {envelope.subject_ref}: {json.dumps(envelope.payload)}",
        system="Respond with JSON: {\"classification\": ..., \"confidence\": ...}",
        required_keys=["classification", "confidence"],
    )
    triage = parse_json_object(result.text)
    with engine.begin() as conn:
        proposal_id = l0_proposals.create_proposal(
            conn,
            layer="l0",
            kind="dummy_candidate",
            subject_ref=envelope.subject_ref,
            draft={"triage": triage, "event_id": envelope.event_id},
            rationale="dummy-fleet rehearsal proposal",
            confidence=float(triage.get("confidence", 0)),
        )
    return {"proposal_id": proposal_id, "cost_usd": result.cost_usd}


class AdapterRunIncomplete(RuntimeError):
    """Persisted candidate work is resumable; this event has not succeeded."""


def run_adapter_fleet(
    engine: Engine, adapter_key: str, gateway: Gateway | None = None, *,
    force_nightly: bool = False, nightly_only: bool = False,
    job_id: str | None = None, event_key: str | None = None, trigger: str = "manual",
) -> dict:
    """Execute the same durable L1 workflow for manual and scheduled requests.

    Source tasks finish independently. Redelivery skips completed imports and
    reruns final audits/evals; a failed task never produces a handled marker.
    Downstream derivation has its own fleet and is never run inline here.
    """
    from app.clhear.l1 import families, inventory, pipeline, registry_etoro, workflow
    from app.clhear.l1.adapters import CITATOR_KEYS
    from app.clhear.l1.fleet import adapter_for, fleet_plan

    if nightly_only:
        raise ValueError("AdapterRunRequested executes L1 only; submit downstream work to its owning fleet")
    settings = get_settings()
    if os.environ.get("CLHEAR_ARTIFACT_STORE") == "s3" or settings.clhear_snapshot_s3_uri:
        store = pipeline.S3Store(settings.clhear_datalake_bucket, settings.aws_region)
    else:
        store = pipeline.LocalStore(settings.clhear_artifacts_dir)
    registry_etoro.seed(engine)
    event_key = event_key or str(uuid.uuid4())
    job_id = job_id or workflow.job_id_for(event_key, adapter_key)
    job = workflow.ensure_job(engine, job_id, adapter_key, trigger, event_key)
    workflow.update_job(engine, job_id, "running")
    scope = "finra" if adapter_key.startswith("finra") else "registered"
    statuses, failures = {}, []
    try:
        with workflow.bind_execution(engine, job_id):
            with workflow.stage("discovery", {"scope": scope, "operation": "discovery_and_database_reconciliation"}) as step:
                before = inventory.run_inventory_audit(engine, store, job_id=job_id, scope=scope, discover=adapter_key == "finra")
                step.details.update(audit_id=before.get("audit_id"), inventory_hash=before.get("inventory_hash"))
        plan = [(entry, adapter) for entry, adapter in fleet_plan(adapter_key)
                if adapter.meta().source_key != "finra/rulebook"]
        seen = {adapter.meta().source_key for _, adapter in plan}
        for entry in inventory.planned_entries(engine, scope=scope, adapter_key=adapter_key):
            if entry["key"] not in seen:
                plan.append((entry, adapter_for(entry)))
                seen.add(entry["key"])
        frozen = job.get("summary") or {}
        if "source_keys" in frozen:
            expected_keys = set(frozen["source_keys"])
            plan = [(entry, adapter) for entry, adapter in plan if adapter.meta().source_key in expected_keys]
            missing_adapters = expected_keys - {adapter.meta().source_key for _, adapter in plan}
            failures.extend(f"Frozen document adapter unavailable: {key}" for key in sorted(missing_adapters))
            inventory_hash = frozen["inventory_hash"]
        else:
            inventory_hash = before.get("inventory_hash")
            workflow.update_job(engine, job_id, "running", {
                "source_keys": [adapter.meta().source_key for _, adapter in plan],
                "inventory_hash": inventory_hash, "frozen_at": workflow.utcnow().isoformat(),
                "scope": scope,
            })
        if before.get("inventory_hash") != inventory_hash:
            failures.append("Inventory changed since this job was frozen; newly discovered documents require a new job")
        if not plan:
            failures.append("No importable document adapters in requested scope")
        for entry, adapter in plan:
            source_key = adapter.meta().source_key
            task_id = workflow.ensure_task(engine, job_id, source_key, f"l1.{adapter.key}")
            token = None
            try:
                token = workflow.claim_task(engine, task_id)
                if token is None:
                    previous = workflow.task_info(engine, task_id)
                    status = previous["summary"].get("status", previous["status"])
                    if previous["status"] == "blocked":
                        failures.append(source_key)
                else:
                    with workflow.bind_execution(engine, job_id, task_id, token), workflow.heartbeat(
                        lambda: workflow.heartbeat_task(engine, task_id, token)
                    ):
                        summary = pipeline.ingest(engine, adapter, store, trigger=trigger, gateway=gateway,
                                                  job_id=job_id, index_embeddings=False)
                        status = summary.get("status", "failed")
                        # Citator writes belong to the same source task and failure boundary.
                        if entry is None and adapter.key in CITATOR_KEYS and status in {"added", "amended", "unchanged", "up-to-date"}:
                            families.sync_citator(engine, adapter, trigger=trigger, job_id=job_id)
                    success = status in {"added", "amended", "unchanged", "up-to-date"}
                    workflow.finish_task(engine, task_id, token,
                        status="completed" if success else "blocked" if status == "rights-blocked" else "failed",
                        summary=summary, error=None if success else summary.get("error", status))
                    token = None
                    if not success:
                        failures.append(source_key)
            except Exception as exc:
                log.exception("L1 source task failed for %s", source_key)
                status = "failed"
                failures.append(source_key)
                if token:
                    try:
                        workflow.finish_task(engine, task_id, token, status="failed", error=exc)
                    except workflow.LeaseLost:
                        log.warning("source task lease was lost: %s", task_id)
            statuses[status] = statuses.get(status, 0) + 1
        with workflow.bind_execution(engine, job_id):
            with workflow.stage("database_reconciliation", {"scope": scope}) as step:
                after = inventory.run_inventory_audit(engine, store, job_id=job_id, scope=scope, discover=False)
                step.details.update(audit_id=after.get("audit_id"), inventory_hash=after.get("inventory_hash"))
            with workflow.stage("readback_evals") as step:
                from app.clhear.platform import evals as l1_evals
                completeness = l1_evals.run_suite(engine, "l1_completeness", release=job_id)
                global_acceptance = l1_evals.run_suite(engine, "l1_inventory_acceptance", release=job_id)
                boundary = l1_evals.run_suite(engine, "l1_boundary_f1", release=job_id)
                schedule = l1_evals.run_suite(engine, "l1_schedule_kept", release=job_id)
                _put_schedule_metric(schedule["scores"].get("missed_count", 0))
                source_results = {}
                for _entry, adapter in plan:
                    key = adapter.meta().source_key
                    source_results[key] = l1_evals.run_source_evals(engine, key, release=job_id)
                source_checks = [row for rows in source_results.values() for row in rows]
                def passed(checks):
                    return bool(checks) and all(row.get("passed") and not row.get("scores", {}).get("not_evaluated")
                                               and not row.get("scores", {}).get("n/a") for row in checks)
                # FINRA's candidate gate is its own full scope. Global suites
                # remain visible release blockers until all registered L1 is ready.
                # E1–E7 remain transparent diagnostic evidence. Some legitimately
                # have no historical amendment fixture on a first version. The
                # acceptance audit supplies mandatory original/binding checks.
                mandatory = [boundary]
                if scope == "registered":
                    mandatory += [completeness, global_acceptance, schedule]
                evals_passed = passed(mandatory)
                scope_acceptance = inventory.acceptance_status(engine, scope=scope)
                registered_acceptance = scope_acceptance if scope == "registered" else inventory.acceptance_status(engine, scope="registered")
                step.details.update(completeness=completeness, inventory_acceptance=global_acceptance,
                                    boundary=boundary, schedule=schedule, sources=source_results,
                                    source_suites_role="diagnostic; original/binding checks are mandatory in scope acceptance",
                                    scope_acceptance={k: v for k, v in scope_acceptance.items() if k != "evidence"},
                                    passed=evals_passed)
                if not evals_passed:
                    step.status = "blocked"
                    failures.append("Mandatory scope evaluations failed or were not evaluated")
                if not scope_acceptance.get("passed"):
                    step.status = "blocked"
                    failures.append("Scope acceptance unresolved: " + ", ".join(scope_acceptance.get("reasons", [])))
                if after.get("inventory_hash") != inventory_hash:
                    failures.append("Post-import inventory differs from the frozen job inventory")
                if scope_acceptance.get("inventory_hash") != inventory_hash:
                    failures.append("Scope acceptance is not bound to the frozen job inventory")
            ready = not failures and registered_acceptance.get("passed") and passed([completeness, global_acceptance, boundary, schedule])
            with workflow.stage("publication", {"accepted": False, "readiness": "ready_for_l0" if ready else "blocked",
                    "reason": "Only the L0 release worker may promote an immutable, gated release."}) as step:
                step.status = "ready" if ready else "blocked"
        verified = not failures and scope_acceptance.get("passed") and evals_passed
        result = {"adapter": adapter_key, "job_id": job_id, "ran": len(plan), "statuses": statuses,
                  "failures": failures, "inventory_audit_id": after.get("audit_id"),
                  "downstream": "held", "acceptance": "candidate_verified" if verified else "awaiting_verification",
                  "evals_passed": evals_passed, "publication": "ready_for_l0" if ready else "blocked", "error": None}
        workflow.update_job(engine, job_id, "candidate_verified" if verified else "blocked", result)
        if failures:
            raise AdapterRunIncomplete(f"{job_id}: {len(failures)} unresolved source tasks; inspect workflow evidence")
        return result
    except Exception as exc:
        if not isinstance(exc, AdapterRunIncomplete):
            workflow.update_job(engine, job_id, "failed", {"error": str(exc)[:1000], "failures": failures})
        raise
    finally:
        # Candidate evidence is useful even when rights/evals block acceptance.
        # L0 refreshes the reviewer projection; Aurora remains the record.
        from app.clhear.l1.viewer_snapshot import request_refresh
        request_refresh(engine, reason="adapter_job_finished", job_id=job_id)


def _put_schedule_metric(missed_count: int) -> None:
    """CLHEAR/ScheduleMissedSources: alarmed in CloudWatch when > 0."""
    try:
        import boto3

        boto3.client("cloudwatch", region_name=get_settings().aws_region).put_metric_data(
            Namespace="CLHEAR",
            MetricData=[{"MetricName": "ScheduleMissedSources", "Value": float(missed_count), "Unit": "Count"}],
        )
    except Exception:
        log.exception("could not publish ScheduleMissedSources metric")


def handle_adapter_run(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    payload = envelope.payload or {}
    event_key = delivery_event_key(envelope)
    return run_adapter_fleet(
        engine, payload.get("adapter", envelope.subject_ref), gateway,
        force_nightly=bool(payload.get("force_nightly") or payload.get("force")),
        nightly_only=bool(payload.get("nightly_only")),
        job_id=payload.get("job_id") or None, event_key=event_key,
        trigger="schedule" if envelope.producer == "eventbridge" else "manual",
    )


SNAPSHOT_LOCAL = "/tmp/clhear.db"


def handle_publish_release(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """Prepare or strictly verify and promote a private L1 release through L0."""
    from app.clhear.releases import publish_release, promote_release

    payload = envelope.payload or {}
    action = payload.get("action", "prepare")
    if action == "prepare":
        return publish_release(engine, release_id=payload.get("release_id"), sbom_path=payload.get("sbom_path"))
    if action == "promote":
        return promote_release(engine, payload.get("release_id", ""))
    raise ValueError("Unknown release action; expected prepare or promote")


def handle_l1_inventory_audit(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """Audit-only execution uses the same authoritative engine and L1 store."""
    from app.clhear.l1 import inventory, pipeline, workflow

    settings = get_settings()
    scope = (envelope.payload or {}).get("scope", "registered")
    store = (pipeline.S3Store(settings.clhear_datalake_bucket, settings.aws_region)
             if os.environ.get("CLHEAR_ARTIFACT_STORE") == "s3" or settings.clhear_snapshot_s3_uri
             else pipeline.LocalStore(settings.clhear_artifacts_dir))
    event_key = delivery_event_key(envelope)
    job_id = workflow.job_id_for(event_key, f"inventory.{scope}")
    workflow.ensure_job(engine, job_id, f"inventory.{scope}", "manual", event_key)
    workflow.update_job(engine, job_id, "running")
    try:
        with workflow.bind_execution(engine, job_id), workflow.stage("database_reconciliation", {"scope": scope}) as step:
            result = inventory.run_inventory_audit(engine, store, job_id=job_id, scope=scope,
                                                   discover=bool((envelope.payload or {}).get("discover", False)))
            step.details.update(audit_id=result["audit_id"], inventory_hash=result["inventory_hash"],
                                verified=result["verified"], unresolved=result["unresolved"])
            if result["status"] != "verified":
                step.status = "blocked"
        workflow.update_job(engine, job_id, "verified" if result["status"] == "verified" else "blocked", result)
        return result
    except Exception as exc:
        workflow.update_job(engine, job_id, "failed", {"error_type": type(exc).__name__})
        raise
    finally:
        from app.clhear.l1.viewer_snapshot import request_refresh
        request_refresh(engine, reason="inventory_audit_finished", job_id=job_id)


def handle_l1_evidence_review(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """Record explicit reviewer evidence from the trusted L0 command channel.

    This is an operator's reviewed decision, never an inferred licence grant.
    Artifact and scope reviews do not grant source-operation permissions.
    """
    from app.clhear.l1 import inventory, permissions

    payload = dict(envelope.payload or {})
    kind = payload.pop("review_kind", None)
    from app.clhear.l1.viewer_snapshot import request_refresh
    # The review and its refresh request commit together. A revoked grant must
    # never become durable without the outbox work that updates the viewer.
    with engine.begin() as conn:
        if kind == "permissions":
            record = permissions.record_permission(conn, **payload)
        elif kind == "artifact":
            record = inventory.record_artifact_review(conn, **payload)
        elif kind == "scope":
            record = inventory.record_scope_review(conn, **payload)
        else:
            raise ValueError("review_kind must be permissions, artifact or scope")
        request_refresh(conn, reason="source_evidence_updated")
    return {"review_kind": kind, "record": record, "requires_new_audit": True}


def handle_viewer_snapshot(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """L0 projects allowlisted candidate evidence into the private web viewer."""
    from app.clhear.l1.viewer_snapshot import configured_uri, publish_viewer_snapshot
    uri = configured_uri()
    if not uri:
        raise ValueError("CLHEAR_VIEWER_SNAPSHOT_S3_URI must identify the private reviewer object")
    return publish_viewer_snapshot(engine, uri, get_settings().aws_region,
                                   job_id=(envelope.payload or {}).get("job_id"))


def handle_community_write(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """Apply a community op from the read-only web app (single-writer rule)."""
    from app.clhear import community_writes

    return community_writes.apply_op(engine, envelope.payload)


def handle_l1_changed(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """HLD v2 §4.2: an L1 clause change -> L2 change inference for that source."""
    from app.clhear.l2.change import on_l1_changed
    from app.clhear.platform.router import Router, is_router

    llm = gateway
    if gateway is not None and not is_router(gateway):
        llm = Router(engine, providers={getattr(gateway._provider, "name", "fake"): gateway._provider})
    return on_l1_changed(engine, envelope.payload or {}, llm)


def handle_l2_changed(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """HLD v2 §4.3 / §4.4 / §4.5: an L2 obligation change -> L3 requires / characteristics
    propagation, L4 applicability-edge re-stamping and L5 junction re-derivation
    (I1: derive downward)."""
    from app.clhear.l3.decompose import on_l2_changed
    from app.clhear.l4.predicates import on_l2_changed as l4_on_l2_changed
    from app.clhear.l5.map import on_l2_changed as l5_on_l2_changed

    out = on_l2_changed(engine, envelope.payload or {})
    out["l4"] = l4_on_l2_changed(engine, envelope.payload or {})
    out["l5"] = l5_on_l2_changed(engine, envelope.payload or {})
    out["l6"] = l6_on_changed(engine, envelope.payload or {}, layer="L2")
    return out


def handle_l4_changed(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """HLD v2 §4.4 / §4.5 / §4.6: an ontology change re-judges every stored profile
    (never deletes), re-derives the L5 implies edges (products may have come or
    gone) and recomposes the stored blueprints."""
    from app.clhear.l4.validate import revalidate_profiles
    from app.clhear.l5.map import on_l4_changed

    out = revalidate_profiles(engine)
    out["l5"] = on_l4_changed(engine, envelope.payload or {})
    out["l6"] = l6_on_changed(engine, envelope.payload or {}, layer="L4")
    return out


def handle_l5_changed(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """HLD v2 §4.6: a junction change recomposes the stored blueprints (diff engine)."""
    return {"l6": l6_on_changed(engine, envelope.payload or {}, layer="L5")}


def handle_graph_rebuild(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """HLD v2 I7: rebuild the query graph and the vector index from the record
    (scheduled nightly; also on demand). Idempotent by construction."""
    from app.clhear.platform import embeddings, graph

    payload = envelope.payload or {}
    out = {"graph": graph.rebuild(engine, release=payload.get("release", ""), trigger="event")}
    if payload.get("index", True):
        out["index"] = embeddings.rebuild_index(engine, release=payload.get("release", ""), trigger="event",
                                                force=bool(payload.get("force")))
    return out


def handle_dr_drill(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """HLD v2 §7.1 (item 17): nightly restore drill — backup the record, restore it
    into the scratch target, verify record + graph + datalake replica, log RPO/RTO."""
    from app.clhear.platform import dr

    payload = envelope.payload or {}
    return dr.run(engine, release=payload.get("release", ""), scratch_url=payload.get("scratch_url"),
                  neo4j_database=payload.get("neo4j_database"), trigger="event",
                  skip_datalake=bool(payload.get("skip_datalake", False)))


def l6_on_changed(engine: Engine, payload: dict, *, layer: str) -> dict:
    from app.clhear.l6.diff import on_lower_layer_changed

    return on_lower_layer_changed(engine, payload, layer=layer)


HANDLERS = {
    "DummyChanged": handle_dummy_changed,
    "AdapterRunRequested": handle_adapter_run,
    "PublishReleaseRequested": handle_publish_release,
    "L1InventoryAuditRequested": handle_l1_inventory_audit,
    "L1EvidenceReviewRecorded": handle_l1_evidence_review,
    "ViewerSnapshotRequested": handle_viewer_snapshot,
    "GraphRebuildRequested": handle_graph_rebuild,
    "DrDrillRequested": handle_dr_drill,
    "CommunityWrite": handle_community_write,
    "clhear.l1.changed": handle_l1_changed,
    "clhear.l2.changed": handle_l2_changed,
    "clhear.l4.changed": handle_l4_changed,
    "clhear.l5.changed": handle_l5_changed,
    # Later layers: add kinds here. handle_envelope already ignores unknown kinds.
}

class L1AcceptanceHold(RuntimeError):
    """Retryable event held until an operator accepts the L1 scope."""


class WrongFleet(RuntimeError):
    """An event must not be acknowledged by a fleet that does not own it."""


def delivery_event_key(envelope):
    if envelope.producer == "eventbridge":
        # Old static EventBridge inputs lack an occurrence identity. Never
        # synthesize today's date: delayed retries would become a new job.
        if not envelope.ts:
            raise ValueError("Scheduled event is missing occurrence timestamp; deploy the EventBridge input transformer")
        return f"{envelope.event_id}:{envelope.ts}"
    return envelope.event_id


def _owned_handler(kind, fleet):
    if fleet == "all":  # Explicit local/test worker compatibility.
        handler = HANDLERS.get(kind)
        if handler is None:
            raise WrongFleet(f"No implemented handler for {kind}")
        return handler
    owners = {"DummyChanged": "l0", "CommunityWrite": "l0", "AdapterRunRequested": "l1",
              "L1InventoryAuditRequested": "l1", "L1EvidenceReviewRecorded": "l0",
              "ViewerSnapshotRequested": "l0",
              "PublishReleaseRequested": "l0", "GraphRebuildRequested": "l0", "DrDrillRequested": "l0",
              "clhear.l1.changed": "l2", "clhear.l4.changed": "l6", "clhear.l5.changed": "l6"}
    if kind == "clhear.l2.changed":
        if fleet == "l3":
            from app.clhear.l3.decompose import on_l2_changed
        elif fleet == "l4":
            from app.clhear.l4.predicates import on_l2_changed
        elif fleet == "l5":
            from app.clhear.l5.map import on_l2_changed
        else:
            raise WrongFleet(f"{fleet} has no implemented consumer for {kind}")
        return lambda engine, gateway, envelope: on_l2_changed(engine, envelope.payload or {})
    if owners.get(kind) != fleet:
        raise WrongFleet(f"{fleet} does not own {kind}; retain for correct routing or handler implementation")
    if kind in {"clhear.l4.changed", "clhear.l5.changed"}:
        return lambda engine, gateway, env: {"l6": l6_on_changed(engine, env.payload or {}, layer=env.layer.upper())}
    return HANDLERS[kind]


def handle_envelope(engine: Engine, gateway: Gateway, body: str) -> dict | None:
    from app.clhear.l1 import workflow
    envelope = l0_events.resolve_envelope(engine, body)
    if get_settings().clhear_l1_only and envelope.kind in {
        "clhear.l1.changed", "clhear.l2.changed", "clhear.l4.changed", "clhear.l5.changed",
        "PublishReleaseRequested", "GraphRebuildRequested", "DrDrillRequested"
    }:
        raise L1AcceptanceHold("L1 acceptance hold: retain this event for replay after verification")
    fleet = os.environ.get("CLHEAR_FLEET", "all").lower()
    handler = _owned_handler(envelope.kind, fleet)
    event_key = delivery_event_key(envelope)
    consumer = f"fleet.{fleet}:{envelope.kind}"
    token = workflow.claim_delivery(engine, consumer, event_key)
    if token is None:
        return None
    try:
        with workflow.heartbeat(lambda: workflow.heartbeat_delivery(engine, consumer, event_key, token)):
            started = time.monotonic()
            outputs = handler(engine, gateway, envelope)
            outputs["duration_ms"] = int((time.monotonic() - started) * 1000)
        # Only successfully completed handlers receive a durable handled marker.
        workflow.finish_delivery(engine, consumer, event_key, token)
        with engine.begin() as conn:
            conn.execute(runs.insert().values(fleet="worker", trigger=envelope.kind,
                inputs={"event_id": envelope.event_id, "event_key": event_key, "consumer": consumer,
                        "subject_ref": envelope.subject_ref}, outputs=outputs, duration_ms=outputs.get("duration_ms")))
        return outputs
    except Exception as exc:
        try:
            workflow.finish_delivery(engine, consumer, event_key, token, error=exc)
        except workflow.LeaseLost:
            log.warning("delivery ownership lost: %s", event_key)
        raise


class RoutedOutboxTransport:
    """L0 sends commands to the owning queue and layer events to the event bus."""
    def __init__(self, queue_urls, region):
        import boto3
        from app.clhear.platform.events import EventBridgeTransport
        self.queues = queue_urls
        self.sqs = boto3.client("sqs", region_name=region)
        self.bus = EventBridgeTransport(region, bus_name=os.environ.get("CLHEAR_EVENT_BUS_NAME", "clhear"))

    def send(self, body):
        env = l0_events.parse_transport(body)
        if env.kind.startswith("clhear."):
            # EventBridge's HTTP 200 can contain per-entry errors; do not let
            # relay_once mark an event relayed unless the entry was accepted.
            result = self.bus._client.put_events(Entries=[{"EventBusName": self.bus._bus,
                "Source": "clhear", "DetailType": env.kind, "Detail": body}])
            entries = result.get("Entries", [])
            if result.get("FailedEntryCount") or len(entries) != 1 or not entries[0].get("EventId") or entries[0].get("ErrorCode"):
                raise RuntimeError(f"EventBridge did not confirm acceptance of {env.kind}")
            return
        owner = "l1" if env.kind in {"AdapterRunRequested", "L1InventoryAuditRequested"} else "l0"
        if owner not in self.queues:
            raise WrongFleet(f"No configured queue for {owner}; retain outbox row")
        self.sqs.send_message(QueueUrl=self.queues[owner], MessageBody=body)


def _snapshot_pull(uri: str, region: str) -> None:
    import boto3

    bucket, key = uri[len("s3://") :].split("/", 1)
    boto3.client("s3", region_name=region).download_file(bucket, key, SNAPSHOT_LOCAL)
    log.info("snapshot pulled from %s", uri)


def _snapshot_push(uri: str, region: str) -> None:
    import boto3

    bucket, key = uri[len("s3://") :].split("/", 1)
    boto3.client("s3", region_name=region).upload_file(SNAPSHOT_LOCAL, bucket, key)
    log.info("snapshot published to %s", uri)


def dispatch_once(envelope_file: str) -> dict | None:
    """Manual operational entrypoint: the same envelope handler as SQS."""
    from pathlib import Path
    from app.clhear.db import get_engine, run_migrations
    from app.clhear.platform.router import Router, build_providers

    engine = get_engine()
    run_migrations(engine)
    settings = get_settings()
    gateway = Router(engine, build_providers(settings))
    return handle_envelope(engine, gateway, Path(envelope_file).read_text())


def main() -> None:
    """Long-running ECS worker: relay outbox + consume SQS.

    Snapshot mode (CLHEAR_SNAPSHOT_S3_URI set): the corpus SQLite is pulled
    from S3 at startup, every scheduled fleet run mutates it, and it is
    published back after each handled batch — the public explorer picks the
    new snapshot up on its next TTL check.
    """
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    fleet = os.environ.get("CLHEAR_FLEET", "all").lower()
    if settings.clhear_l1_only and fleet in {f"l{layer}" for layer in range(2, 9)}:
        # A dispatch-time rejection is too late: ReceiveMessage increments the
        # retry count and can send intentionally held work to the dead-letter queue.
        # Keep the service alive without touching its queue, DB, or providers.
        # The deployment setting is fixed for this process; releasing the hold
        # requires starting the task with CLHEAR_L1_ONLY disabled.
        log.warning("L1 acceptance hold: fleet %s paused before startup and queue polling", fleet)
        while True:
            time.sleep(60)

    import boto3

    from app.clhear import db
    from app.clhear.db import get_engine, run_migrations
    from app.clhear.platform.events import SqsTransport, relay_once
    from app.clhear.platform.router import Router, build_providers, record_missing_providers

    from app.clhear.platform import errors

    errors.init(component=f"fleet-{os.environ.get('CLHEAR_FLEET', 'L0').lower()}")

    snapshot_uri = settings.clhear_snapshot_s3_uri
    # Aurora remains authoritative even if an old snapshot setting survives.
    if snapshot_uri and not settings.database_url.startswith("sqlite"):
        log.warning("Ignoring snapshot database override for authoritative PostgreSQL")
        snapshot_uri = ""
    if snapshot_uri:
        try:
            _snapshot_pull(snapshot_uri, settings.aws_region)
        except Exception:
            log.exception("no existing snapshot at %s; starting fresh", snapshot_uri)
        os.environ["DATABASE_URL"] = f"sqlite:///{SNAPSHOT_LOCAL}"
        get_settings.cache_clear()
        settings = get_settings()
        db.dispose_engine()

    engine = get_engine()
    run_migrations(engine)

    providers = build_providers(settings)
    if not providers:
        record_missing_providers(engine)
    gateway = Router(engine, providers)
    if fleet == "l0":
        transport = RoutedOutboxTransport(json.loads(os.environ.get("CLHEAR_FLEET_QUEUE_URLS", "{}")), settings.aws_region)
    else:
        transport = SqsTransport(settings.clhear_events_queue_url, settings.aws_region)
    should_relay = fleet in {"l0", "all"}
    sqs = boto3.client("sqs", region_name=settings.aws_region)

    log.info("clhear worker started (providers=%s, snapshot=%s)", ",".join(providers) or "none", snapshot_uri or "off")
    while True:
        try:
            if should_relay:
                relay_once(engine, transport)
            resp = sqs.receive_message(
                QueueUrl=settings.clhear_events_queue_url,
                MaxNumberOfMessages=1,
                VisibilityTimeout=180,
                WaitTimeSeconds=10,
            )
            messages = resp.get("Messages", [])
            for message in messages:
                try:
                    from app.clhear.l1.workflow import heartbeat
                    with heartbeat(lambda: sqs.change_message_visibility(
                        QueueUrl=settings.clhear_events_queue_url, ReceiptHandle=message["ReceiptHandle"],
                        VisibilityTimeout=180,
                    )):
                        outputs = handle_envelope(engine, gateway, message["Body"])
                        if snapshot_uri:
                            if should_relay:
                                relay_once(engine, transport)
                            # Persist candidate and delivery marker before ACK. A failed
                            # upload leaves the envelope available for recovery.
                            _snapshot_push(snapshot_uri, settings.aws_region)
                    sqs.delete_message(QueueUrl=settings.clhear_events_queue_url,
                                       ReceiptHandle=message["ReceiptHandle"])
                except (L1AcceptanceHold, WrongFleet) as exc:
                    log.warning("Message retained: %s", exc)
                except Exception:
                    log.exception("Message failed; retained for retry/dead-letter policy")
                    if snapshot_uri:
                        try:
                            _snapshot_push(snapshot_uri, settings.aws_region)
                        except Exception:
                            log.exception("Could not persist resumable candidate state")
        except Exception:
            log.exception("worker iteration failed; backing off")
            time.sleep(10)


def cli(argv=None) -> int:
    import argparse
    import sys

    class WorkerArgumentParser(argparse.ArgumentParser):
        def error(self, message):
            # Exit 2 is reserved for a completed review-ready verification.
            # Invalid ECS command arguments must never look like that result.
            self.print_usage(sys.stderr)
            self.exit(1, f"{self.prog}: error: {message}\n")

    parser = WorkerArgumentParser(description="CLHEAR worker: SQS consumer or one durable manual envelope")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--envelope-file")
    parser.add_argument("--verify-deployment", choices=("bootstrap", "verify", "publish"))
    parser.add_argument("--verification-id")
    args = parser.parse_args(argv)
    if args.verify_deployment:
        if args.once or args.envelope_file or not args.verification_id:
            parser.error("--verify-deployment requires --verification-id and cannot be combined with --once")
        from app.clhear.deployment_verification import execute
        result = execute(args.verify_deployment, args.verification_id)
        print(json.dumps(result, default=str))
        return result["exit_code"]
    if args.verification_id:
        parser.error("--verification-id requires --verify-deployment")
    if args.once != bool(args.envelope_file):
        parser.error("--once and --envelope-file must be supplied together")
    if args.once:
        print(json.dumps(dispatch_once(args.envelope_file), default=str))
    else:
        main()
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
