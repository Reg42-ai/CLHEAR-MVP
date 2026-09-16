"""Explicit private-POC permission and inventory-scope reviews.

This is an operator-authorized private-environment grant, not a publisher
licence and not L1 acceptance. ``display_public`` is never set. Revoke writes
an ``approved=False`` replacement snapshot and requests a viewer refresh so
protected text is redacted. Every row is recorded through the existing L0
command ``L1EvidenceReviewRecorded``.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

from app.clhear.platform.events import Envelope

POC_APPROVED_BY = "owner: private POC test environment"
POC_GRANT = {
    "acquire": True, "store": True, "parse": True, "display_internal": True,
    "display_public": False,
}


def protected_source_keys() -> list[str]:
    from app.clhear.l1.permissions import required_for
    from app.clhear.l1.source_registry import S

    return sorted({entry["key"] for entry in S if required_for(entry)})


def _event_id(*parts: str) -> str:
    return "poc-" + hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]


def _dispatch(engine: Engine, *, event_id: str, kind: str, subject_ref: str, payload: dict):
    from app.clhear import workers

    envelope = Envelope(
        event_id=event_id, layer="l0", kind="L1EvidenceReviewRecorded",
        subject_ref=subject_ref, payload=payload, producer="operator.poc",
        ts=datetime.now(timezone.utc).isoformat(),
    )
    return workers.handle_envelope(engine, None, envelope.model_dump_json())


def apply_private_review(engine: Engine, action: str, evidence_ref: str, *,
                         verification_id: str, approved_by: str = POC_APPROVED_BY) -> dict:
    """Activate or revoke POC permissions for every protected registry source."""
    if action not in {"activate", "revoke"}:
        raise ValueError("action must be activate or revoke")
    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        raise ValueError("evidence_ref is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", verification_id or ""):
        raise ValueError("verification_id is required")
    approved = action == "activate"
    permissions = dict(POC_GRANT if approved else {key: False for key in POC_GRANT})
    permissions["display_public"] = False
    keys = protected_source_keys()
    recorded, skipped = [], []
    for source_key in keys:
        event_id = _event_id(verification_id, action, source_key, evidence_ref.strip())
        result = _dispatch(engine, event_id=event_id, kind="permissions", subject_ref=source_key, payload={
            "review_kind": "permissions", "source_key": source_key, "permissions": permissions,
            "evidence_ref": evidence_ref.strip(), "approved_by": approved_by, "approved": approved,
        })
        (skipped if result is None else recorded).append(source_key)
    return {
        "status": "recorded", "action": action, "verification_id": verification_id,
        "approved_by": approved_by, "evidence_ref": evidence_ref.strip(),
        "sources": keys, "recorded": recorded, "already_recorded": skipped,
        "display_public": False, "acceptance": "not_claimed",
    }


def approve_inventory(engine: Engine, inventory_hash: str, *, verification_id: str,
                      evidence_ref: str = "poc:private-scope-review",
                      approved_by: str = POC_APPROVED_BY, approved: bool = True) -> dict:
    """Record a scope review for one frozen inventory hash."""
    if not re.fullmatch(r"[a-f0-9]{64}", inventory_hash or ""):
        raise ValueError("inventory_hash must identify one frozen SHA-256 inventory")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", verification_id or ""):
        raise ValueError("verification_id is required")
    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        raise ValueError("evidence_ref is required")
    event_id = _event_id(verification_id, "scope", inventory_hash, evidence_ref.strip())
    result = _dispatch(engine, event_id=event_id, kind="scope", subject_ref="inventory", payload={
        "review_kind": "scope", "inventory_hash": inventory_hash,
        "evidence_ref": evidence_ref.strip(), "approved_by": approved_by, "approved": approved,
    })
    return {
        "status": "already_recorded" if result is None else "recorded",
        "verification_id": verification_id, "inventory_hash": inventory_hash,
        "approved": approved, "approved_by": approved_by,
        "evidence_ref": evidence_ref.strip(), "acceptance": "not_claimed",
        "record": None if result is None else result.get("record"),
    }
