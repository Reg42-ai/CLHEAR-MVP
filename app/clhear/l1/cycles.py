"""Worker-owned L1 cycles. Commands use the ordinary durable L0 outbox.

This module plans and accounts for work; acquisition and encoding remain in
the existing L1 adapter handler. A completed cycle is not a published release.
"""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
import re
import uuid

import sqlalchemy as sa

from app.clhear.models import metadata, Json
from app.clhear.platform import events
from app.clhear.l1 import workflow


cycles = sa.Table("l1_cycles", metadata,
    sa.Column("cycle_id", sa.Text, primary_key=True),
    sa.Column("request_event_id", sa.Text, nullable=False),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("scheduled_for", sa.DateTime(timezone=True)),
    sa.Column("code_revision", sa.Text),
    sa.Column("worker_image_digest", sa.Text),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("manifest", Json, nullable=False, default=dict),
    sa.Column("result", Json, nullable=False, default=dict),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
)
children = sa.Table("l1_cycle_children", metadata,
    sa.Column("child_id", sa.Text, primary_key=True),
    sa.Column("cycle_id", sa.Text, nullable=False, index=True),
    sa.Column("adapter_key", sa.Text, nullable=False),
    sa.Column("event_id", sa.Text),
    sa.Column("event_time", sa.Text),
    sa.Column("command_event_id", sa.Text),
    sa.Column("job_id", sa.Text),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("source_keys", Json, nullable=False, default=list),
    sa.Column("inventory_hash", sa.Text),
    sa.Column("result", Json, nullable=False, default=dict),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.UniqueConstraint("cycle_id", "adapter_key"),
)
TERMINAL_CHILD = {"completed", "completed_for_review", "failed"}
TERMINAL_CYCLE = {"candidate_verified", "completed_for_review", "failed"}


class CycleRevisionChanged(ValueError):
    """Pending work belongs to a different immutable worker deployment."""


def verify_runtime(engine, cycle_id):
    """An interrupted cycle cannot silently resume under a different parser."""
    from app.clhear.l1.viewer_snapshot import request_refresh
    with engine.begin() as conn:
        row = _row(conn, cycle_id, lock=True)
        matches = all(row[key] == value for key, value in runtime_identity().items())
        if matches:
            return True
        if row["status"] not in TERMINAL_CYCLE:
            result = {"cycle_id": cycle_id, "status": "failed", "execution_failed": True,
                      "reason": "worker_revision_changed_requires_new_cycle", "accepted_release": False,
                      "downstream": "held", "original_code_revision": row["code_revision"],
                      "original_worker_image_digest": row["worker_image_digest"], **runtime_identity()}
            conn.execute(children.update().where(children.c.cycle_id == cycle_id,
                children.c.status.not_in(TERMINAL_CHILD)).values(status="failed", result=result, finished_at=workflow.utcnow()))
            conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id).values(
                status="failed", result=result, finished_at=workflow.utcnow()))
            request_refresh(conn, reason="l1_cycle_worker_revision_changed", job_id=cycle_id)
        return False


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def runtime_identity():
    revision = os.environ.get("CLHEAR_CODE_REVISION", "")
    image = os.environ.get("CLHEAR_WORKER_IMAGE_DIGEST", "")
    return {"code_revision": revision if re.fullmatch(r"[0-9a-f]{40}", revision) else None,
            "worker_image_digest": image if re.fullmatch(r"sha256:[0-9a-f]{64}", image) else None}


def adapter_keys():
    from app.clhear.l1.fleet import fleet_adapter_keys
    from app.clhear.l1.models import FLEET_SCHEDULES
    # A declared lane remains expected when its documents are not configured.
    return sorted(set(fleet_adapter_keys()) | set(FLEET_SCHEDULES))


def child_id_for(cycle_id, adapter):
    return "child-l1-" + digest([cycle_id, adapter])[:24]


def _row(conn, cycle_id, *, lock=False):
    query = sa.select(cycles).where(cycles.c.cycle_id == cycle_id)
    if lock:
        query = query.with_for_update()
    return dict(conn.execute(query).mappings().one())


def _emit(conn, kind, cycle_id, payload=None):
    return events.emit(conn, layer="l0" if kind in {"L1CycleRequested", "L1CycleAdvanceRequested"} else "l1",
                       kind=kind, subject_ref=cycle_id, payload={"cycle_id": cycle_id, **(payload or {})}, producer="l0.l1_cycle")


def request_cycle(engine, verification_id, *, scope="all_publishers"):
    """L0 CLI receipt only. No discovery, imports or acceptance in the caller."""
    if os.environ.get("CLHEAR_FLEET", "").lower() != "l0":
        raise ValueError("Only the L0 worker may request an L1 cycle")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", verification_id or ""):
        raise ValueError("A safe, unique verification ID is required")
    cycle_id = "cycle-manual-" + verification_id
    with engine.begin() as conn:
        # Stable request UUID prevents repeat CLI dispatch from creating work twice.
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, cycle_id))
        workflow._insert_once(conn, events.events, dict(event_id=event_id, layer="l0", kind="L1CycleRequested",
            subject_ref=cycle_id, payload={"cycle_id": cycle_id, "scope": scope}, producer="worker.cli", schema_version=1))
        original = conn.execute(sa.select(events.events.c.payload).where(events.events.c.event_id == event_id)).scalar_one()
        if original != {"cycle_id": cycle_id, "scope": scope}:
            raise ValueError("Verification ID is already bound to another scope")
    return {"cycle_id": cycle_id, "event_id": event_id, "status": "requested", "origin": "manual",
            "scheduler_delivery_verified": False, **runtime_identity()}


def _create(conn, cycle_id, event_id, origin, scope, scheduled_for=None):
    workflow._insert_once(conn, cycles, dict(cycle_id=cycle_id, request_event_id=event_id,
        origin=origin, scope=scope, scheduled_for=scheduled_for, status="requested", manifest={}, result={},
        created_at=workflow.utcnow(), **runtime_identity()))
    row = _row(conn, cycle_id, lock=True)
    if row["origin"] != origin or row["scope"] != scope:
        raise ValueError("Cycle identity belongs to another request scope or origin")
    return row


def start(engine, envelope):
    if envelope.producer == "eventbridge":
        raise ValueError("Per-adapter scheduler deliveries must retain their individual occurrence identities")
    scope = envelope.payload.get("scope", "all_publishers")
    if scope not in {"registered", "all_publishers"}:
        raise ValueError("L1 cycles cover the complete declared publisher scope")
    cycle_id = envelope.payload.get("cycle_id") or "cycle-manual-" + digest(envelope.event_id)[:24]
    if not re.fullmatch(r"cycle-manual-[A-Za-z0-9._-]{1,101}", cycle_id):
        raise ValueError("Invalid manual cycle ID")
    with engine.begin() as conn:
        row = _create(conn, cycle_id, envelope.event_id, "manual", scope)
        if row["request_event_id"] != envelope.event_id:
            raise ValueError("Cycle ID already belongs to another request event")
        if row["status"] == "requested":
            claimed = conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id,
                                   cycles.c.status == "requested").values(status="discovering")).rowcount
            if claimed:
                _emit(conn, "L1CycleDiscoveryRequested", cycle_id)
    return {"cycle_id": cycle_id, "status": "requested", "origin": "manual"}


def plan_sources(engine, scope, audit_id=None):
    from app.clhear.l1.fleet import fleet_plan
    from app.clhear.l1 import inventory
    plans = {}
    for key in adapter_keys():
        source_keys = {adapter.meta().source_key for _, adapter in fleet_plan(key)} - {"finra/rulebook"}
        source_keys.update(entry["key"] for entry in inventory.planned_entries(engine, scope=scope, adapter_key=key,
                          **({"audit_id": audit_id} if audit_id else {})))
        plans[key] = sorted(source_keys)
    return plans


def discovered(engine, cycle_id, audit):
    with engine.connect() as conn:
        row = _row(conn, cycle_id)
    pending = audit.get("discovery", {}).get("pending_pages", 0)
    if pending:
        # A bounded page batch is not a finished traversal. Commit the next
        # command with its progress before acknowledging the current one.
        with engine.begin() as conn:
            row = _row(conn, cycle_id, lock=True)
            if row["status"] != "discovering":
                return {"cycle_id": cycle_id, "status": row["status"]}
            batches = list(row["result"].get("discovery_batches", []))
            progress_hash = digest({"pending_pages": pending, "inventory_hash": audit["inventory_hash"],
                "pages": sorted((p.get("publisher_id", ""), p.get("url", ""), p.get("sha256", ""))
                                for p in audit.get("discovery", {}).get("pages", []))})
            if batches and batches[-1].get("progress_hash") == progress_hash:
                # Let normal SQS visibility/backoff wait for active page leases;
                # do not generate an unbounded stream of identical continuations.
                raise workflow.RetryDeferred("Discovery frontier has no new completed progress; retry the persisted command")
            if audit["audit_id"] not in {batch["audit_id"] for batch in batches}:
                batches.append({"audit_id": audit["audit_id"], "inventory_hash": audit["inventory_hash"],
                                "pending_pages": pending, "duration_ms": audit.get("duration_ms"), "progress_hash": progress_hash})
                conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id).values(
                    result={**row["result"], "discovery_batches": batches, "pending_pages": pending}))
                _emit(conn, "L1CycleDiscoveryRequested", cycle_id, {"batch": len(batches) + 1})
        return {"cycle_id": cycle_id, "status": "discovering", "pending_pages": pending,
                "audit_id": audit["audit_id"], "next_batch_requested": True}
    plans = plan_sources(engine, row["scope"], audit["audit_id"])
    manifest = {"adapter_keys": sorted(plans), "sources_by_adapter": plans,
                "expected_source_keys": sorted(s["source_key"] for s in audit["sources"]),
                "inventory_hash": audit["inventory_hash"], "audit_id": audit["audit_id"],
                "known_expected_is_lower_bound": audit.get("known_expected_is_lower_bound", True),
                "discovery_cycle_date": discovery_date(row),
                "frozen_at": workflow.utcnow().isoformat(), **runtime_identity()}
    manifest["manifest_hash"] = digest(manifest)
    with engine.begin() as conn:
        row = _row(conn, cycle_id, lock=True)
        if row["status"] != "discovering":
            return {"cycle_id": cycle_id, "status": row["status"]}
        claimed = conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id,
                               cycles.c.status == "discovering").values(status="planned", manifest=manifest,
                               result={**row["result"], "pending_pages": 0,
                                       "final_discovery_audit_id": audit["audit_id"]})).rowcount
        if claimed:
            _emit(conn, "L1CycleAdvanceRequested", cycle_id)
    return {"cycle_id": cycle_id, "status": "planned", "manifest_hash": manifest["manifest_hash"]}


def discovery_date(row):
    return workflow._aware(row["scheduled_for"] or row["created_at"]).astimezone(timezone.utc).date().isoformat()


def advance(engine, cycle_id):
    if not verify_runtime(engine, cycle_id):
        return {"cycle_id": cycle_id, "status": "failed", "reason": "worker_revision_changed_requires_new_cycle"}
    with engine.begin() as conn:
        row = _row(conn, cycle_id, lock=True)
        if row["status"] == "collecting":
            received = set(conn.execute(sa.select(children.c.adapter_key).where(children.c.cycle_id == cycle_id,
                children.c.event_time.is_not(None), children.c.event_id.is_not(None))).scalars())
            if received == set(row["manifest"]["adapter_keys"]):
                claimed = conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id,
                    cycles.c.status == "collecting").values(status="discovering")).rowcount
                if claimed:
                    _emit(conn, "L1CycleDiscoveryRequested", cycle_id)
        elif row["status"] == "planned":
            claimed = conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id,
                                   cycles.c.status == "planned").values(status="running")).rowcount
            for adapter in row["manifest"]["adapter_keys"] if claimed else []:
                child_id = child_id_for(cycle_id, adapter)
                job_id = workflow.job_id_for(child_id, adapter)
                event_id = events.emit(conn, layer="l1", kind="AdapterRunRequested", subject_ref=adapter,
                    producer="l0.l1_cycle", payload={"adapter": adapter, "cycle_id": cycle_id,
                                                     "child_id": child_id, "job_id": job_id})
                workflow._insert_once(conn, children, dict(child_id=child_id, cycle_id=cycle_id, adapter_key=adapter,
                    event_id=event_id, command_event_id=event_id, job_id=job_id, status="queued", result={},
                    source_keys=row["manifest"]["sources_by_adapter"][adapter], inventory_hash=row["manifest"]["inventory_hash"]))
                # A scheduled receipt's event ID/time are immutable provenance;
                # the work command has a different outbox identity.
                conn.execute(children.update().where(children.c.child_id == child_id).values(
                    command_event_id=event_id, job_id=job_id, status="queued",
                    source_keys=row["manifest"]["sources_by_adapter"][adapter], inventory_hash=row["manifest"]["inventory_hash"]))
        elif row["status"] == "running":
            found = list(conn.execute(sa.select(children).where(children.c.cycle_id == cycle_id)).mappings())
            expected = set(row["manifest"]["adapter_keys"])
            if {c["adapter_key"] for c in found} == expected and all(c["status"] in TERMINAL_CHILD for c in found):
                claimed = conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id,
                                       cycles.c.status == "running").values(status="evaluating")).rowcount
                if claimed:
                    _emit(conn, "L1CycleEvaluationRequested", cycle_id)
        state = _row(conn, cycle_id)
    return {"cycle_id": cycle_id, "status": state["status"]}


def scheduled_child(engine, envelope):
    """Group real adapter events by the advertised UTC slot, never by receipt day."""
    adapter = envelope.payload.get("adapter", envelope.subject_ref)
    if adapter not in adapter_keys():
        raise ValueError("Unknown scheduled L1 adapter")
    at = datetime.fromisoformat(envelope.ts.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("Scheduled occurrence must contain a timezone")
    at = at.astimezone(timezone.utc)
    if (at.hour, at.minute) != (0, 0):
        raise ValueError("Adapter event does not match its declared 00:00 UTC schedule")
    slot = at.replace(second=0, microsecond=0)
    cycle_id = "cycle-scheduled-" + slot.strftime("%Y%m%dT%H%MZ")
    child_id = child_id_for(cycle_id, adapter)
    with engine.begin() as conn:
        row = _create(conn, cycle_id, "eventbridge:" + slot.isoformat(), "scheduled", "all_publishers", slot)
        if not row["manifest"]:
            manifest = {"adapter_keys": adapter_keys(), "plan_mode": "discovery_after_all_scheduler_receipts",
                        "scheduled_for": slot.isoformat(), **runtime_identity()}
            manifest["manifest_hash"] = digest(manifest)
            conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id).values(status="collecting", manifest=manifest))
        existing = conn.execute(sa.select(children).where(children.c.child_id == child_id)).mappings().first()
        if existing and existing["event_id"] != envelope.event_id:
            raise ValueError("A different scheduler event already owns this adapter occurrence")
        workflow._insert_once(conn, children, dict(child_id=child_id, cycle_id=cycle_id, adapter_key=adapter,
            event_id=envelope.event_id, event_time=envelope.ts, status="received", source_keys=[], result={}))
        _emit(conn, "L1CycleAdvanceRequested", cycle_id)
    return cycle_id, child_id


def child_context(engine, cycle_id, child_id, envelope):
    if not verify_runtime(engine, cycle_id):
        raise CycleRevisionChanged("Cycle cannot mix deployed worker revisions or image digests")
    with engine.begin() as conn:
        row = _row(conn, cycle_id)
        child = dict(conn.execute(sa.select(children).where(children.c.child_id == child_id,
                                                          children.c.cycle_id == cycle_id)).mappings().one())
        if child["command_event_id"] != envelope.event_id or child["adapter_key"] != envelope.payload.get("adapter", envelope.subject_ref):
            raise ValueError("Child request does not match its durable cycle binding")
        if child["job_id"] != envelope.payload.get("job_id"):
            raise ValueError("Child job identity differs from its frozen work command")
        conn.execute(children.update().where(children.c.child_id == child_id).values(
            status="running" if child["status"] not in TERMINAL_CHILD else child["status"],
            started_at=sa.func.coalesce(children.c.started_at, workflow.utcnow())))
    return {"cycle_id": cycle_id, "child_id": child_id, "scope": row["scope"], "origin": row["origin"],
            "source_keys": child["source_keys"] if child["inventory_hash"] else None,
            "inventory_hash": child["inventory_hash"], "audit_id": row["manifest"].get("audit_id"), "job_id": child["job_id"]}


def freeze_child(engine, context, job_id, keys, inventory_hash):
    with engine.begin() as conn:
        row = dict(conn.execute(sa.select(children).where(children.c.child_id == context["child_id"])
                               .with_for_update()).mappings().one())
        if row["inventory_hash"] and row["source_keys"] != sorted(keys):
            raise ValueError("Cycle child source set differs from its frozen manifest")
        conn.execute(children.update().where(children.c.child_id == context["child_id"]).values(
            job_id=job_id, source_keys=sorted(keys), inventory_hash=row["inventory_hash"] or inventory_hash))


def output_bindings(engine, cycle_id, audit):
    """A different concurrent cycle must not supply this cycle's claimed output."""
    from app.clhear.l1.source_registry import source_role
    current = {s["source_key"]: s for s in audit["sources"]}
    with engine.connect() as conn:
        frozen = set(_row(conn, cycle_id)["manifest"].get("expected_source_keys", []))
        rows = conn.execute(sa.select(workflow.tasks).where(workflow.tasks.c.job_id.in_(
            sa.select(children.c.job_id).where(children.c.cycle_id == cycle_id)))).mappings().all()
    bindings, missing, verified = [], set(), set()
    for row in rows:
        if row["source_key"] not in frozen:
            if source_role(row["source_key"]) not in {"collection", "reference"}:
                missing.add(row["source_key"])
            continue
        if row["status"] != "completed":
            missing.add(row["source_key"])
            continue
        summary = row["summary"] or {}
        expected = current.get(row["source_key"], {})
        binding = {"source_key": row["source_key"], "task_id": row["task_id"], "job_id": row["job_id"],
                   "source_version_id": summary.get("source_version_id"), "content_hash": summary.get("content_hash")}
        bindings.append(binding)
        if (not binding["source_version_id"] or not binding["content_hash"] or
                any(binding[k] != expected.get(k) for k in ("source_version_id", "content_hash"))):
            missing.add(row["source_key"])
        else:
            verified.add(row["source_key"])
    missing.update(frozen - verified)
    bindings.sort(key=lambda row: row["task_id"])
    return {"passed": bool(frozen) and not missing, "bindings_hash": digest(bindings),
            "bindings": bindings, "unverified_sources": sorted(missing)}


def finish_child(engine, context, result, *, retryable=False):
    status = "retrying" if retryable else "failed" if result.get("execution_failed") else (
        "completed_for_review" if result.get("failures") else "completed")
    with engine.begin() as conn:
        conn.execute(children.update().where(children.c.child_id == context["child_id"]).values(
            status=status, result=result, finished_at=None if retryable else workflow.utcnow()))
        _emit(conn, "L1CycleAdvanceRequested", context["cycle_id"])
    return status


def unhandled_child_error(engine, context, envelope, error):
    """Bound setup failures as well as source failures; never invent task success."""
    event_key = f"{envelope.event_id}:{envelope.ts}" if envelope.producer == "eventbridge" else envelope.event_id
    consumer = "fleet." + os.environ.get("CLHEAR_FLEET", "all").lower() + ":AdapterRunRequested"
    with engine.connect() as conn:
        child = conn.execute(sa.select(children).where(children.c.child_id == context["child_id"])).mappings().one()
        attempts = conn.execute(sa.select(workflow.deliveries.c.attempt).where(
            workflow.deliveries.c.event_key == event_key, workflow.deliveries.c.consumer == consumer)).scalar() or 1
    if child["status"] in TERMINAL_CHILD:
        return
    # Source retries have their own budget; do not overwrite their outcome.
    if isinstance(error, (workflow.LeaseBusy, workflow.RetryDeferred)) or child["result"].get("retryable"):
        return
    finish_child(engine, context, {"execution_failed": True, "error_type": type(error).__name__,
                                  "job_id": child["job_id"], "failures": ["child_setup_or_execution_failed"]},
                 retryable=attempts < workflow.MAX_ATTEMPTS)


def failed_phase(engine, cycle_id, phase, error, attempt):
    """After bounded infrastructure failures retain failed cycle evidence."""
    if attempt < workflow.MAX_ATTEMPTS:
        return
    from app.clhear.l1.viewer_snapshot import request_refresh
    with engine.begin() as conn:
        row = _row(conn, cycle_id, lock=True)
        if row["status"] in TERMINAL_CYCLE:
            return
        result = {"status": "failed", "cycle_id": cycle_id, "phase": phase,
                  "error_type": type(error).__name__, "execution_failed": True,
                  "accepted_release": False, "downstream": "held"}
        conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id).values(
            status="failed", result=result, finished_at=workflow.utcnow()))
        request_refresh(conn, reason="l1_cycle_failed", job_id=cycle_id)


def finish_cycle(engine, cycle_id, result):
    """Final result and viewer refresh commit together; no release promotion."""
    from app.clhear.l1.viewer_snapshot import request_refresh
    with engine.begin() as conn:
        row = _row(conn, cycle_id, lock=True)
        if row["status"] in TERMINAL_CYCLE:
            return row["result"]
        found = list(conn.execute(sa.select(children).where(children.c.cycle_id == cycle_id)).mappings())
        if (row["status"] != "evaluating" or {c["adapter_key"] for c in found} != set(row["manifest"]["adapter_keys"])
                or any(c["status"] not in TERMINAL_CHILD for c in found)):
            raise ValueError("All cycle children must be terminal before final evaluation")
        result = {**result, "cycle_id": cycle_id, "origin": row["origin"], "accepted_release": False,
                  "downstream": "held", "finished_at": workflow.utcnow().isoformat(),
                  "duration_ms": int((workflow.utcnow() - workflow._aware(row["created_at"])).total_seconds() * 1000),
                  "scheduler_delivery_verified": row["origin"] == "scheduled" and all(c["event_time"] for c in found),
                  **{k: row[k] for k in runtime_identity()}}
        status = "failed" if any(c["status"] == "failed" for c in found) or result.get("execution_failed") else (
            "candidate_verified" if result.get("acceptance_passed") else "completed_for_review")
        result["status"] = status
        conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id).values(
            status=status, result=result, finished_at=workflow.utcnow()))
        request_refresh(conn, reason="l1_cycle_finished", job_id=cycle_id)
    return result


def cycle_summary(engine, cycle_id=None, *, offset=0, limit=100):
    offset, limit = max(0, offset), max(1, min(500, limit))
    with engine.connect() as conn:
        if not sa.inspect(conn).has_table(cycles.name, schema=cycles.schema if engine.dialect.name == "postgresql" else None):
            return {"status": "unavailable", "cycles": [], "children": [], "total": 0}
        query = sa.select(cycles)
        if cycle_id:
            query = query.where(cycles.c.cycle_id == cycle_id)
        total = conn.execute(sa.select(sa.func.count()).select_from(query.subquery())).scalar_one()
        rows = [dict(r) for r in conn.execute(query.order_by(cycles.c.created_at.desc()).offset(offset).limit(limit)).mappings()]
        child_rows = [dict(r) for r in conn.execute(sa.select(children).where(children.c.cycle_id.in_([r["cycle_id"] for r in rows]))
                                                  .order_by(children.c.adapter_key)).mappings()]
    return {"status": "available", "cycles": rows, "children": child_rows, "total": total,
            "offset": offset, "limit": limit, "has_more": offset + len(rows) < total}


def schedule_evidence(engine, now=None, *, cycle_id=None):
    """Measure genuine scheduled occurrences, not a 24-hour bag of import runs."""
    if cycle_id is not None:
        if not re.fullmatch(r"cycle-scheduled-\d{8}T0000Z", cycle_id):
            raise ValueError("Schedule evidence requires an exact scheduled occurrence ID")
        slot = datetime.strptime(cycle_id.removeprefix("cycle-scheduled-"), "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
    else:
        now = now or workflow.utcnow()
        slot = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        cycle_id = "cycle-scheduled-" + slot.strftime("%Y%m%dT%H%MZ")
    summary = cycle_summary(engine, cycle_id)
    state = summary["cycles"][0] if summary["cycles"] else None
    if state and (state["origin"] != "scheduled" or workflow._aware(state["scheduled_for"]) != slot):
        raise ValueError("Stored cycle does not match its scheduled occurrence ID")
    manifest = state["manifest"] if state else {}
    child_rows = summary["children"]
    expected_adapters = manifest.get("adapter_keys", adapter_keys())
    received = {c["adapter_key"] for c in child_rows if c["event_id"] and c["event_time"]}
    expected_sources, attempted, blocked = set(), set(), set()
    with engine.connect() as conn:
        for child in child_rows:
            expected_sources.update(child["source_keys"])
            if child["job_id"]:
                attempted.update(conn.execute(sa.select(workflow.tasks.c.source_key).where(
                    workflow.tasks.c.job_id == child["job_id"], workflow.tasks.c.attempt > 0)).scalars())
                blocked.update(conn.execute(sa.select(workflow.tasks.c.source_key).where(
                    workflow.tasks.c.job_id == child["job_id"], workflow.tasks.c.status == "blocked")).scalars())
    # Freeze the denominator to the occurrence. Later discovery cannot rewrite
    # an earlier day's task set; unresolved/unimportable documents stay included.
    if "expected_source_keys" in manifest:
        expected_sources.update(manifest["expected_source_keys"])
        lower_bound = manifest.get("known_expected_is_lower_bound", True)
    else:
        from app.clhear.l1 import inventory
        declared = inventory.inventory_summary(engine, scope="registered")
        expected_sources.update(s["source_key"] for s in declared.get("sources", []))
        if not declared.get("sources"):
            expected_sources.update(key for keys in plan_sources(engine, "registered").values() for key in keys)
        lower_bound = declared.get("known_expected_is_lower_bound", True)
    missing = sorted(expected_sources - attempted)
    missing_adapters = sorted(set(expected_adapters) - received)
    complete = not missing and not missing_adapters and bool(expected_sources)
    return {"cycle_id": cycle_id, "scheduled_for": slot.isoformat(), "origin": "scheduled",
            "scheduled_sources": len(expected_sources), "attempted_24h": len(attempted),
            "missed": missing, "missed_count": len(missing), "missing_adapters": missing_adapters,
            "received_adapters": len(received), "expected_adapters": len(expected_adapters),
            "blocked": sorted(blocked), "known_expected_is_lower_bound": lower_bound,
            "method": "scheduled occurrence IDs and frozen child task sets; manual attempts excluded",
            "delivery_verified": not missing_adapters, "all_documents_attempted": complete,
            "import_acceptance": (summary["cycles"][0]["status"] if summary["cycles"] else "not_started")}, complete
