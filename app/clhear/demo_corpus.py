"""Influencer demo corpus: four public sources, one fixed import, two posts.

The import is one ``AdapterRunRequested`` with ``source_keys`` set and
``discover`` false. It is not a FINRA cycle and it is not an all-publisher
cycle. Derivation reads only these sources. L7 stays the enforcement view
and is not fed by a post.
"""
from __future__ import annotations

import os
import re
import uuid

import sqlalchemy as sa

from app.clhear.l2.extract import obligation_id
from app.clhear.models import events

DEMO_SOURCE_KEYS = (
    "usc/15/ftc-act-45",
    "cfr/16/255",
    "cfr/17/ia-marketing",
    "nist/csf-2.0",
)
LEGAL_SOURCE_KEYS = DEMO_SOURCE_KEYS[:3]

CLAUSE_STATUTE = "45"
CLAUSE_GUIDES = "255.5"
CLAUSE_MARKETING = "275.206(4)-1"

GALAXY_ATTRIBUTES = {
    "jurisdictions": ["US"],
    "authorisations": ["SEC-registered investment adviser (Advisers Act s.203)"],
    "customer_base": ["retail"],
    "channels": ["social and finfluencer marketing"],
}

_STATUTE = obligation_id("usc/15/ftc-act-45", CLAUSE_STATUTE)
_GUIDES = obligation_id("cfr/16/255", CLAUSE_GUIDES)
_MARKETING = obligation_id("cfr/17/ia-marketing", CLAUSE_MARKETING)

# Synthetic Galaxy Securities posts. The workspace keeps the post; CLHEAR keeps
# the observation. "cleared" is a vendor word: stored, never scored.
POST_A = {
    "subject": [_STATUTE, _GUIDES, _MARKETING, "galaxy-post-a"],
    "result": "fail",
    "labels": ["cleared"],
    "characteristics": {
        "record": "no written promoter agreement on file",
        "output": "disclosure only in the profile bio",
        "mandatory_sections": "paid relationship omitted from the endorsement",
    },
    "evidence": {
        "pointer": "galaxy:posts/a",
        "text": "Paid promoter tells retail followers that Galaxy’s app prints money. The disclosure is only in the profile bio.",
    },
    "reasoning": (
        "The disclosure is avoidable, and there is no promoter agreement or oversight record. "
        f"{_GUIDES} requires a clear and conspicuous material-connection disclosure. "
        f"{_MARKETING} requires compensation disclosure, a written agreement, and oversight."
    ),
    "performer": {"kind": "app", "id": "safeluence", "protocol": "mcp"},
    "observed_at": "2026-09-21T12:00:00+00:00",
}

POST_B = {
    "subject": [_STATUTE, _GUIDES, _MARKETING, "galaxy-post-b"],
    "result": "pass",
    "characteristics": {
        "record": "written promoter agreement retained",
        "output": "paid relationship stated in the video, in the same medium as the endorsement",
        "approver": "compliance role approval on the record",
        "mandatory_sections": "compensation disclosure in the endorsement",
    },
    "evidence": {
        "pointer": "galaxy:posts/b",
        "text": "The earnings claim is removed. The paid relationship is stated in the video. A written promoter agreement and a compliance-role approval are on the record.",
    },
    "reasoning": (
        "The paid relationship is stated in the same medium as the endorsement, and the agreement "
        f"and approval are on the record. {_GUIDES} and {_MARKETING} are satisfied."
    ),
    "performer": {"kind": "app", "id": "safeluence", "protocol": "mcp"},
    "observed_at": "2026-09-21T13:00:00+00:00",
}


def request_demo_import(engine, verification_id: str) -> dict:
    """L0 receipt: one fixed govinfo_us run. Repeat calls keep the same event."""
    from app.clhear.l1 import workflow

    if os.environ.get("CLHEAR_FLEET", "").lower() != "l0":
        raise ValueError("Only the L0 worker may request the demo import")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", verification_id or ""):
        raise ValueError("A safe, unique verification ID is required")
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "clhear-demo-import:" + verification_id))
    payload = {
        "adapter": "govinfo_us",
        "source_keys": list(DEMO_SOURCE_KEYS),
        "discover": False,
        "job_id": workflow.job_id_for(event_id, "govinfo_us"),
    }
    with engine.begin() as conn:
        workflow._insert_once(conn, events, dict(
            event_id=event_id, layer="l1", kind="AdapterRunRequested",
            subject_ref="govinfo_us", payload=payload, producer="l0.demo", schema_version=1,
        ))
        original = conn.execute(sa.select(events.c.payload).where(events.c.event_id == event_id)).scalar_one()
    if list(original.get("source_keys") or []) != list(DEMO_SOURCE_KEYS) or original.get("discover") is not False:
        raise ValueError("Verification ID is already bound to another import")
    return {
        "event_id": event_id, "status": "requested", "adapter": "govinfo_us",
        "source_keys": list(DEMO_SOURCE_KEYS), "discover": False, "job_id": payload["job_id"],
    }


def derive_demo(engine) -> dict:
    """One-shot L2–L6 derivation for the demo sources already in L1.

    Does not start a cycle, does not score L7 from these posts, and does not
    lift the downstream hold for every publisher. NIST CSF is included when
    an in-force version is already stored; it is a control catalog, not an
    advertising code, and it contributes an obligation only when a duty is present.
    """
    from app.clhear import curated
    from app.clhear.derived_models import obligations
    from app.clhear.l1.source_registry import seed
    from app.clhear.l2.extract import run_extraction
    from app.clhear.l3.decompose import decompose
    from app.clhear.l4 import predicates

    curated.seed(engine)
    seed(engine)
    extracted = {key: run_extraction(engine, source_key=key) for key in DEMO_SOURCE_KEYS}
    with engine.begin() as conn:
        onto = predicates._Onto(conn)
        rows = conn.execute(sa.select(obligations).where(obligations.c.source_key.in_(LEGAL_SOURCE_KEYS))).mappings()
        stamped = 0
        for row in rows:
            predicates.sync_obligation(conn, dict(row), onto, reason="demo.derive")
            stamped += 1
    decomposed = {key: decompose(engine, source_key=key) for key in LEGAL_SOURCE_KEYS}
    return {"extracted": extracted, "predicates": stamped, "decomposed": decomposed, "l7": "enforcement exposure, not scored from posts", "l8": "locked"}
