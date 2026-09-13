"""Instance-mode contract with Reg42 OS (HLD v2 I5, I9, §4.6 "Blueprint/Actual
overlay", §4.7 "instance priorities"; build item 18).

CLHEAR gives away the diagnosis and sells the cure: the agnostic blueprint is open;
what an *actual* organization has, where its gaps are and what it should fix first
is **instance mode** — delivered through Reg42 OS, running only in the client's own
AWS account. This module is the contract between the two:

* the **shapes** Reg42 OS sends and receives (:class:`ActualOverlay`,
  :class:`GapDiff`, :class:`InstancePriorities`), published as OpenAPI
  (:func:`openapi`) and prose (``docs/INSTANCE_MODE_CONTRACT.md``);
* the **computation** — :func:`gap_diff` and :func:`instance_priorities` — pure
  functions over an open blueprint, open base priorities and the client's overlay;
* the **guarantee** (I5): a session never writes to the CLHEAR record. The overlay
  is held in memory for the request, the results are returned, and nothing —
  not a row, not an event, not an audit detail — carries an organization
  identifier into the agnostic store. :func:`session` enforces this by diffing
  the store before and after, and the release pipeline's agnostic scan proves it
  again on every release (``app/clhear/platform/agnostic_scan.py``).

The instance endpoints (``app/clhear/v1/instance.py``) answer only when the
deployment is in instance mode *and* :func:`deployment_guard` accepts it (client
account pinned by ``CLHEAR_INSTANCE_ACCOUNT_ID``); on the public deployment they
return 403 with the contract location, so a mis-configured public node can never
accidentally become an instance.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Literal

import sqlalchemy as sa
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.engine import Connection, Engine

CONTRACT_VERSION = "1.0.0"
CONTRACT_DOC = "docs/INSTANCE_MODE_CONTRACT.md"

ActualState = Literal["present", "partial", "absent", "planned", "not_applicable"]

# How much of an item's base priority a gap state carries into the instance ranking.
# "absent" is the full base priority; "partial" half; "planned" a quarter (the
# work is scheduled, the exposure is not gone); present / n.a. contribute nothing.
GAP_WEIGHT: dict[str, float] = {"absent": 1.0, "partial": 0.5, "planned": 0.25, "present": 0.0, "not_applicable": 0.0}
# A required (L3 `requires`) block that is missing is never just "selected" work.
REQUIRED_BOOST = 1.25
# Each obligation that only this item satisfies adds to its instance priority.
LOAD_BEARING_STEP = 0.05
METHOD_VERSION = "instance-priority-1.0"


# --------------------------------------------------------------------------- shapes (the contract)


class ActualItem(BaseModel):
    """What the organization actually has for one blueprint block."""

    block_id: str = Field(description="CLHEAR block id (BLK-…) the actual maps to")
    state: ActualState = Field(description="present | partial | absent | planned | not_applicable")
    characteristics_met: list[str] = Field(default_factory=list, description="Characteristic keys the actual satisfies (for partial)")
    evidence_refs: list[str] = Field(default_factory=list, description="Client-side evidence locators (opaque to CLHEAR; never stored)")
    owner: str | None = Field(default=None, description="Accountable owner in the organization (never stored)")
    notes: str | None = Field(default=None, description="Free text for the organization's own use (never stored)")
    target_date: str | None = Field(default=None, description="ISO date for planned items")


class ActualOverlay(BaseModel):
    """The organization's Actual overlay for one blueprint (the closed input)."""

    contract_version: str = Field(default=CONTRACT_VERSION)
    blueprint_id: str | None = Field(default=None, description="Stored blueprint (BLU-…) to overlay; or give a profile")
    profile: dict | None = Field(default=None, description="Profile attributes to compose on the fly when no blueprint_id")
    release: str | None = Field(default=None, description="CLHEAR release the blueprint was composed from")
    as_of: str | None = Field(default=None, description="When the actuals were assessed (ISO 8601)")
    actuals: list[ActualItem] = Field(default_factory=list)

    @field_validator("actuals")
    @classmethod
    def _unique_blocks(cls, v: list[ActualItem]) -> list[ActualItem]:
        seen = set()
        for a in v:
            if a.block_id in seen:
                raise ValueError(f"duplicate actual for {a.block_id}")
            seen.add(a.block_id)
        return v


class GapItem(BaseModel):
    item_id: str | None
    block_id: str
    kind: str
    name: str
    basis: Literal["required", "selected"]
    state: ActualState
    gap: bool
    missing_characteristics: list[str]
    obligations_at_risk: list[str] = Field(description="Obligations this item satisfies that the gap leaves exposed")
    load_bearing_for: list[str] = Field(description="Obligations only this item satisfies (open, from the minimality proof)")
    base_priority: float | None = Field(description="Open L7 item priority (composite 0..1) or null when unscored")
    base_band: str | None
    explanation: str


class GapSummary(BaseModel):
    items: int
    present: int
    partial: int
    absent: int
    planned: int
    not_applicable: int
    unassessed: int = Field(description="Blueprint items with no actual supplied — treated as absent")
    gaps: int
    coverage_ratio: float = Field(description="present / (items − not_applicable)")
    obligations_total: int
    obligations_at_risk: int


class GapDiff(BaseModel):
    contract_version: str
    blueprint_id: str | None
    release: str | None
    as_of: str | None
    computed_at: str
    overlay_hash: str = Field(description="sha256 of the canonical overlay — lets the client pin the input without CLHEAR keeping it")
    summary: GapSummary
    items: list[GapItem]
    unknown_blocks: list[str] = Field(description="Actuals whose block is not in the blueprint (ignored, reported)")


class PriorityItem(BaseModel):
    rank: int
    item_id: str | None
    block_id: str
    kind: str
    name: str
    state: ActualState
    basis: str
    base_priority: float | None
    instance_priority: float
    factors: dict = Field(description="gap_weight, required_boost, load_bearing — how instance_priority was formed")
    obligations_at_risk: list[str]


class InstancePriorities(BaseModel):
    contract_version: str
    method_version: str
    blueprint_id: str | None
    computed_at: str
    overlay_hash: str
    method: str
    items: list[PriorityItem]


# --------------------------------------------------------------------------- computation (pure)


def overlay_hash(overlay: ActualOverlay) -> str:
    """Canonical hash of the overlay so the client can pin an input CLHEAR does not keep.
    Free-text fields (owner, notes, evidence) are excluded: the hash pins *what*
    was assessed, not who said it."""
    canon = {"blueprint_id": overlay.blueprint_id, "profile": overlay.profile, "release": overlay.release,
             "actuals": sorted(({"block_id": a.block_id, "state": a.state, "characteristics_met": sorted(a.characteristics_met)}
                                for a in overlay.actuals), key=lambda a: a["block_id"])}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, default=str).encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def gap_diff(composition: dict, overlay: ActualOverlay, base_priorities: dict[str, dict] | None = None) -> GapDiff:
    """Blueprint (ghost nodes = required) ∘ Actual overlay (solid = present) → delta = gap.

    ``composition`` is the open L6 blueprint composition (``items`` + ``coverage``);
    ``base_priorities`` maps item id / block id → open L7 score row ({composite, band}).
    """
    base_priorities = base_priorities or {}
    actual_by_block = {a.block_id: a for a in overlay.actuals}
    items_out: list[GapItem] = []
    counts = {"present": 0, "partial": 0, "absent": 0, "planned": 0, "not_applicable": 0}
    unassessed = 0
    at_risk: set[str] = set()
    for it in composition.get("items", []):
        a = actual_by_block.get(it["block_id"])
        if a is None:
            state: ActualState = "absent"
            unassessed += 1
        else:
            state = a.state
        counts[state] += 1
        chars = [c["key"] for c in it.get("characteristics", []) if c.get("in_profile", True)]
        if state == "present" or state == "not_applicable":
            missing = []
        elif state == "partial":
            met = set(a.characteristics_met) if a else set()
            missing = [k for k in chars if k not in met]
        else:
            missing = list(chars)
        gap = state in ("partial", "absent", "planned")
        exposed = sorted(it.get("obligations_satisfied", [])) if gap else []
        at_risk.update(exposed)
        score = base_priorities.get(it.get("id") or "") or base_priorities.get(it["block_id"]) or {}
        why = _explain_gap(it, state, missing, a is None)
        items_out.append(GapItem(
            item_id=it.get("id"), block_id=it["block_id"], kind=it.get("kind", ""), name=it.get("name", ""),
            basis=it.get("basis", "selected"), state=state, gap=gap, missing_characteristics=missing,
            obligations_at_risk=exposed, load_bearing_for=sorted(it.get("load_bearing_for", [])),
            base_priority=(float(score["composite"]) if score.get("composite") is not None else None),
            base_band=score.get("band"), explanation=why))
    total = len(items_out)
    applicable = total - counts["not_applicable"]
    obligations_total = len({o for it in composition.get("items", []) for o in it.get("obligations_satisfied", [])})
    summary = GapSummary(items=total, unassessed=unassessed, gaps=sum(1 for g in items_out if g.gap),
                         coverage_ratio=(counts["present"] / applicable) if applicable else 1.0,
                         obligations_total=obligations_total, obligations_at_risk=len(at_risk), **counts)
    known = {it["block_id"] for it in composition.get("items", [])}
    return GapDiff(contract_version=CONTRACT_VERSION, blueprint_id=overlay.blueprint_id or composition.get("blueprint_id"),
                   release=overlay.release or composition.get("release"), as_of=overlay.as_of, computed_at=_now(),
                   overlay_hash=overlay_hash(overlay), summary=summary, items=items_out,
                   unknown_blocks=sorted(b for b in actual_by_block if b not in known))


def _explain_gap(it: dict, state: str, missing: list[str], unassessed: bool) -> str:
    name, kind = it.get("name", it["block_id"]), it.get("kind", "item")
    n_ob = len(it.get("obligations_satisfied", []))
    if state == "present":
        return f"{name} is in place; the {n_ob} obligation(s) it satisfies are covered."
    if state == "not_applicable":
        return f"{name} marked not applicable by the organization; CLHEAR still lists it because the profile triggers it — record the reasoning."
    if state == "planned":
        return f"{name} is planned, not yet operating: the {n_ob} obligation(s) it satisfies stay exposed until it is."
    if state == "partial":
        return f"{name} exists but {len(missing)} required characteristic(s) are not met ({', '.join(missing[:4])}{'…' if len(missing) > 4 else ''})."
    head = "No actual was supplied for" if unassessed else "Missing:"
    tail = " It is a required block (L3 requires), not a design choice." if it.get("basis") == "required" else ""
    return f"{head} {name} ({kind}); {n_ob} obligation(s) exposed.{tail}"


def instance_priorities(diff: GapDiff) -> InstancePriorities:
    """Rank the gaps: base priority × gap weight, boosted for required blocks and for
    every obligation only this item satisfies. Unscored items rank by structure alone
    (treated as base 0.5 so they are never silently last)."""
    ranked: list[PriorityItem] = []
    for g in diff.items:
        w = GAP_WEIGHT[g.state]
        if w == 0.0:
            continue
        base = g.base_priority if g.base_priority is not None else 0.5
        boost = REQUIRED_BOOST if g.basis == "required" else 1.0
        lb = 1.0 + LOAD_BEARING_STEP * len(g.load_bearing_for)
        score = round(min(1.0, base * w * boost * lb), 4)
        ranked.append(PriorityItem(rank=0, item_id=g.item_id, block_id=g.block_id, kind=g.kind, name=g.name, state=g.state,
                                   basis=g.basis, base_priority=g.base_priority, instance_priority=score,
                                   factors={"base": base, "base_assumed": g.base_priority is None, "gap_weight": w,
                                            "required_boost": boost, "load_bearing": lb},
                                   obligations_at_risk=g.obligations_at_risk))
    ranked.sort(key=lambda p: (-p.instance_priority, -(len(p.obligations_at_risk)), p.block_id))
    for i, p in enumerate(ranked, 1):
        p.rank = i
    return InstancePriorities(
        contract_version=CONTRACT_VERSION, method_version=METHOD_VERSION, blueprint_id=diff.blueprint_id, computed_at=_now(),
        overlay_hash=diff.overlay_hash, items=ranked,
        method=("instance_priority = min(1, base_priority × gap_weight[state] × required_boost × (1 + 0.05 × |load_bearing_for|)); "
                f"gap_weight={GAP_WEIGHT}; required_boost={REQUIRED_BOOST}; base_priority is the open L7 item composite "
                "(method at /l7/method); unscored items assume 0.5."))


# --------------------------------------------------------------------------- store access (read-only)


def load_blueprint(conn: Connection, overlay: ActualOverlay, *, compose_missing: bool = False) -> dict:
    """The open blueprint the overlay is laid on. By id from the store, or composed
    from a profile *without storing* (instance sessions leave no rows behind)."""
    from app.clhear.l6 import composer

    if overlay.blueprint_id:
        bp = composer.get_blueprint(conn, overlay.blueprint_id)
        if bp is None:
            raise LookupError(f"unknown blueprint {overlay.blueprint_id}")
        comp = dict(bp["composition"])
        comp.setdefault("blueprint_id", overlay.blueprint_id)
        comp.setdefault("release", bp.get("release"))
        return comp
    if overlay.profile is None:
        raise ValueError("overlay needs blueprint_id or profile")
    if not compose_missing:
        raise ValueError("composition from a profile is not enabled for this session")
    result = composer.compose_with(conn, {"attributes": overlay.profile.get("attributes", overlay.profile)}, release=overlay.release or "")
    result.pop("blueprint_id", None)  # not stored → no id
    return result


def base_priorities(conn: Connection, composition: dict) -> dict[str, dict]:
    """Open L7 scores for the blueprint's items (by ITM- id) with a fall-back to the
    riskiest obligation each block satisfies (by block id)."""
    from app.clhear.l7.models import risk_scores

    out: dict[str, dict] = {}
    try:
        rows = conn.execute(sa.select(risk_scores.c.subject_kind, risk_scores.c.subject_ref, risk_scores.c.composite, risk_scores.c.band)
                            .where(risk_scores.c.status == "current", risk_scores.c.valid_to.is_(None))).mappings().all()
    except sa.exc.DBAPIError:
        return out
    by_item = {r["subject_ref"]: r for r in rows if r["subject_kind"] == "item"}
    by_ob = {r["subject_ref"]: r for r in rows if r["subject_kind"] == "obligation"}
    for it in composition.get("items", []):
        r = by_item.get(it.get("id") or "")
        if r is not None:
            out[it["id"]] = {"composite": float(r["composite"]), "band": r["band"]}
            continue
        obs = [by_ob[o] for o in it.get("obligations_satisfied", []) if o in by_ob]
        if obs:
            top = max(obs, key=lambda r: float(r["composite"]))
            out[it["block_id"]] = {"composite": float(top["composite"]), "band": top["band"]}
    return out


# --------------------------------------------------------------------------- the session guarantee (I5)


class StoreLeak(RuntimeError):
    """An instance-mode session changed the agnostic store."""


def _store_fingerprint(engine: Engine) -> dict[str, tuple[int, str]]:
    """Row count + content hash of every table that could carry instance data."""
    from app.clhear.models import events
    from app.clhear.platform import record
    from app.clhear.platform.audit import audit_log

    out: dict[str, tuple[int, str]] = {}
    tables = list(record.layer_tables()) + [record.why_trails, events, audit_log]
    with engine.connect() as conn:
        for t in tables:
            try:
                rows = conn.execute(sa.select(t).order_by(*t.primary_key.columns) if t.primary_key.columns else sa.select(t)).fetchall()
            except sa.exc.DBAPIError:
                continue
            h = hashlib.sha256()
            for r in rows:
                h.update(json.dumps([str(v) for v in r], sort_keys=True).encode())
            out[t.name] = (len(rows), h.hexdigest())
    return out


@contextmanager
def session(engine: Engine, *, strict: bool = True) -> Iterator[Connection]:
    """A read-only instance-mode session over the agnostic store.

    Yields a connection whose transaction is always rolled back, then compares the
    store's fingerprint before and after; any difference raises :class:`StoreLeak`
    (I5 acceptance: zero identifiers after an instance-mode session).
    """
    before = _store_fingerprint(engine) if strict else {}
    conn = engine.connect()
    tx = conn.begin()
    try:
        yield conn
    finally:
        tx.rollback()
        conn.close()
    if strict:
        after = _store_fingerprint(engine)
        changed = sorted(t for t in set(before) | set(after) if before.get(t) != after.get(t))
        if changed:
            raise StoreLeak(f"instance session changed the agnostic store: {', '.join(changed)}")


def run_session(engine: Engine, overlay: ActualOverlay, *, compose_missing: bool = True) -> dict:
    """Gap diff + instance priorities for one overlay, inside :func:`session`."""
    with session(engine) as conn:
        comp = load_blueprint(conn, overlay, compose_missing=compose_missing)
        diff = gap_diff(comp, overlay, base_priorities(conn, comp))
        pri = instance_priorities(diff)
    return {"gap_diff": diff.model_dump(), "priorities": pri.model_dump()}


# --------------------------------------------------------------------------- deployment guard


def deployment_guard(*, account_resolver=None) -> dict:
    """Is this deployment allowed to run instance mode?

    Requires ``CLHEAR_MODE=instance`` **and** ``CLHEAR_INSTANCE_ACCOUNT_ID`` set to
    the client's AWS account, which must match the account the process actually
    runs in (STS ``GetCallerIdentity``; injectable for tests). Anything else → the
    instance endpoints refuse. The public deployment sets neither.
    """
    from app.clhear.platform.mode import deployment_mode

    mode = deployment_mode()
    pinned = os.environ.get("CLHEAR_INSTANCE_ACCOUNT_ID", "").strip()
    out = {"mode": mode, "instance_account_pinned": bool(pinned), "account_match": None, "allowed": False, "reason": ""}
    if mode != "instance":
        out["reason"] = "deployment is not in instance mode (CLHEAR_MODE)"
        return out
    if not pinned:
        out["reason"] = "instance mode requires CLHEAR_INSTANCE_ACCOUNT_ID (the client's account)"
        return out
    actual = None
    try:
        if account_resolver is not None:
            actual = account_resolver()
        else:
            import boto3

            actual = boto3.client("sts").get_caller_identity()["Account"]
    except Exception as exc:  # noqa: BLE001 — no credentials = cannot prove the account = refuse
        out["reason"] = f"cannot resolve the running account: {type(exc).__name__}"
        return out
    out["account_match"] = actual == pinned
    if not out["account_match"]:
        out["reason"] = "running account does not match CLHEAR_INSTANCE_ACCOUNT_ID"
        return out
    out["allowed"] = True
    out["reason"] = "instance mode in the client account"
    return out


# --------------------------------------------------------------------------- the published contract


def contract() -> dict:
    """The machine-readable contract summary (also served at /instance/contract)."""
    return {
        "name": "CLHEAR instance-mode contract", "version": CONTRACT_VERSION, "method_version": METHOD_VERSION,
        "document": CONTRACT_DOC, "openapi": "/instance/contract/openapi.json",
        "runs_only_in": "the client's AWS account, through Reg42 OS (CLHEAR_MODE=instance + CLHEAR_INSTANCE_ACCOUNT_ID)",
        "open": ["blueprint composition (L6)", "base priorities and method (L7)", "this contract and its OpenAPI"],
        "closed": ["Actual overlay", "gap diff", "instance priorities", "evidence locators"],
        "guarantees": [
            "an instance session never writes to the CLHEAR record (StoreLeak otherwise)",
            "no organization identifier, owner, note or evidence locator is stored, logged or audited by CLHEAR",
            "the overlay hash excludes free text so a client can pin its input without disclosing it",
            "the agnostic-store scan runs on every release and after every instance session in CI",
        ],
        "shapes": {"ActualOverlay": ActualOverlay.model_json_schema(), "GapDiff": GapDiff.model_json_schema(),
                   "InstancePriorities": InstancePriorities.model_json_schema()},
        "gap_weight": GAP_WEIGHT, "required_boost": REQUIRED_BOOST, "load_bearing_step": LOAD_BEARING_STEP,
    }


def openapi() -> dict:
    """Standalone OpenAPI 3.1 document for the instance endpoints (what Reg42 OS codes against)."""
    from fastapi import FastAPI
    from fastapi.openapi.utils import get_openapi

    from app.clhear.v1.instance import router

    app = FastAPI()
    app.include_router(router)
    doc = get_openapi(title="CLHEAR instance-mode contract", version=CONTRACT_VERSION, routes=app.routes,
                      description="Actual overlay → gap diff → instance priorities. Runs only inside the client account via Reg42 OS. "
                                  f"Prose contract: {CONTRACT_DOC}.")
    doc["x-clhear-contract"] = {"version": CONTRACT_VERSION, "method_version": METHOD_VERSION, "document": CONTRACT_DOC}
    return doc
