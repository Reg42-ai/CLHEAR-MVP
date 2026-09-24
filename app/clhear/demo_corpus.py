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
from datetime import datetime, timezone

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
# Evidence for L7 (SEC Marketing Rule sweeps) and L8 (the examination risk alert).
ENFORCEMENT_SOURCE_KEYS = ("sec/enforcement/2023-173", "sec/enforcement/2024-46", "sec/enforcement/2024-121")
REFERENCE_SOURCE_KEYS = ("sec/exams/risk-alert-041724",)
DEMO_IMPORTS = (
    ("govinfo_us", DEMO_SOURCE_KEYS),
    ("sec_enforcement", ENFORCEMENT_SOURCE_KEYS),
    ("sec_edgar", REFERENCE_SOURCE_KEYS),
)
DERIVE_KIND = "DemoDeriveRequested"

CLAUSE_STATUTE = "sec45(a)"
CLAUSE_HONEST = "255.1"
CLAUSE_TYPICAL = "255.2"
CLAUSE_EXPERT = "255.3"
CLAUSE_ORGANISATION = "255.4"
CLAUSE_GUIDES = "255.5"
CLAUSE_MARKETING = "275.206(4)-1"

GALAXY_ATTRIBUTES = {
    "jurisdictions": ["US"],
    "authorisations": ["SEC-registered investment adviser (Advisers Act s.203)"],
    "customer_base": ["retail"],
    "channels": ["social and finfluencer marketing"],
}

_STATUTE = obligation_id("usc/15/ftc-act-45", CLAUSE_STATUTE)
_HONEST = obligation_id("cfr/16/255", CLAUSE_HONEST)
_TYPICAL = obligation_id("cfr/16/255", CLAUSE_TYPICAL)
_EXPERT = obligation_id("cfr/16/255", CLAUSE_EXPERT)
_ORGANISATION = obligation_id("cfr/16/255", CLAUSE_ORGANISATION)
_GUIDES = obligation_id("cfr/16/255", CLAUSE_GUIDES)
_MARKETING = obligation_id("cfr/17/ia-marketing", CLAUSE_MARKETING)
_JUDGED = [_STATUTE, _HONEST, _TYPICAL, _GUIDES, _MARKETING]

# Synthetic Galaxy Securities posts. The workspace keeps the post; CLHEAR keeps
# the observation. "cleared" is a vendor word: stored, never scored.
POST_A = {
    "subject": [*_JUDGED, "galaxy-post-a"],
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
        "The post claims the app prints money, with no substantiation and no typical-results basis, "
        f"which is the deception {_STATUTE} prohibits and {_HONEST} and {_TYPICAL} rule out. "
        "The disclosure is only in the bio, and there is no promoter agreement or oversight record. "
        f"{_GUIDES} requires a clear and conspicuous material-connection disclosure. "
        f"{_MARKETING} requires compensation disclosure, a written agreement, and oversight."
    ),
    "performer": {"kind": "app", "id": "safeluence", "protocol": "mcp"},
    "observed_at": "2026-09-21T12:00:00+00:00",
}

POST_B = {
    "subject": [*_JUDGED, "galaxy-post-b"],
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
        "The earnings claim is removed, so nothing unsubstantiated or atypical remains "
        f"({_STATUTE}, {_HONEST}, {_TYPICAL}). The paid relationship is stated in the same medium as "
        f"the endorsement, and the agreement and approval are on the record. {_GUIDES} and "
        f"{_MARKETING} are satisfied."
    ),
    "performer": {"kind": "app", "id": "safeluence", "protocol": "mcp"},
    "observed_at": "2026-09-21T13:00:00+00:00",
}

# Same post, the sections it does not engage. A reasoned not_applicable leaves
# the score's denominator; it is not a pass.
POST_B_SCOPE = {
    "subject": [_EXPERT, _ORGANISATION, "galaxy-post-b"],
    "result": "not_applicable",
    "evidence": {"pointer": "galaxy:posts/b"},
    "reasoning": (
        f"The endorser is a retail user, not presented as an expert or an organisation, so {_EXPERT} "
        f"and {_ORGANISATION} do not apply to this post."
    ),
    "performer": {"kind": "app", "id": "safeluence", "protocol": "mcp"},
    "observed_at": "2026-09-21T13:00:30+00:00",
}


def request_demo_import(engine, verification_id: str) -> dict:
    """L0 receipt: one fixed run per demo lane (GovInfo sources, SEC sweeps, SEC
    risk alert). Repeat calls keep the same events."""
    from app.clhear.l1 import workflow

    if os.environ.get("CLHEAR_FLEET", "").lower() != "l0":
        raise ValueError("Only the L0 worker may request the demo import")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", verification_id or ""):
        raise ValueError("A safe, unique verification ID is required")
    runs = []
    with engine.begin() as conn:
        for adapter, keys in DEMO_IMPORTS:
            # The GovInfo lane keeps its original event identity.
            seed = verification_id if adapter == "govinfo_us" else f"{verification_id}:{adapter}"
            event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "clhear-demo-import:" + seed))
            payload = {"adapter": adapter, "source_keys": list(keys), "discover": False,
                       "job_id": workflow.job_id_for(event_id, adapter)}
            workflow._insert_once(conn, events, dict(
                event_id=event_id, layer="l1", kind="AdapterRunRequested",
                subject_ref=adapter, payload=payload, producer="l0.demo", schema_version=1,
            ))
            original = conn.execute(sa.select(events.c.payload).where(events.c.event_id == event_id)).scalar_one()
            if list(original.get("source_keys") or []) != list(keys) or original.get("discover") is not False:
                raise ValueError("Verification ID is already bound to another import")
            runs.append({"event_id": event_id, "adapter": adapter, "source_keys": list(keys), "job_id": payload["job_id"]})
    first = runs[0]
    return {**first, "status": "requested", "discover": False, "runs": runs}


def request_demo_derive(engine, *, job_id: str) -> str:
    """Queue one L0 derivation after the demo import job returns. Keyed on the
    job, so a redelivered import does not derive twice."""
    from app.clhear.l1 import workflow

    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "clhear-demo-derive:" + job_id))
    with engine.begin() as conn:
        workflow._insert_once(conn, events, dict(
            event_id=event_id, layer="l0", kind=DERIVE_KIND, subject_ref="demo",
            payload={"job_id": job_id}, producer="l1.demo", schema_version=1,
        ))
    return event_id


def derive_demo(engine) -> dict:
    """One-shot L2–L5 and L7 derivation for the demo sources already in L1.

    L6 is composed and L8 reference rows are read per request. L7 ingests the
    SEC sweep outcomes that are in force, links them, and scores only the demo
    obligations; posts never feed it. Does not start a cycle and does not lift
    the downstream hold for every publisher. NIST CSF is included when an
    in-force version is already stored; it is a control catalog, not an
    advertising code, and it contributes an obligation only when a duty is present.
    """
    from app.clhear import curated
    from app.clhear.derived_models import obligations
    from app.clhear.l1.source_registry import seed
    from app.clhear.l2.extract import run_extraction
    from app.clhear.l3.decompose import decompose
    from app.clhear.l4 import predicates
    from app.clhear.l5.map import map_activities
    from app.clhear.l7.enforcement import ingest_events, link_events
    from app.clhear.l7.score import score_obligations

    curated.seed(engine)
    seed(engine)
    extracted = {key: run_extraction(engine, source_key=key) for key in DEMO_SOURCE_KEYS}
    with engine.begin() as conn:
        onto = predicates._Onto(conn)
        rows = conn.execute(sa.select(obligations).where(
            obligations.c.source_key.in_(LEGAL_SOURCE_KEYS), obligations.c.valid_to.is_(None))).mappings()
        stamped = 0
        for row in rows:
            predicates.sync_obligation(conn, dict(row), onto, reason="demo.derive")
            stamped += 1
    decomposed = {key: decompose(engine, source_key=key) for key in LEGAL_SOURCE_KEYS}
    mapped = {key: map_activities(engine, source_key=key)["deterministic"] for key in LEGAL_SOURCE_KEYS}
    enforcement = {key: ingest_events(engine, source_key=key) for key in ENFORCEMENT_SOURCE_KEYS}
    linked = {key: link_events(engine, source_key=key) for key in ENFORCEMENT_SOURCE_KEYS}
    scored = score_obligations(engine, source_keys=LEGAL_SOURCE_KEYS)
    return {"extracted": extracted, "predicates": stamped, "decomposed": decomposed, "mapped": mapped,
            "enforcement": enforcement, "linked": linked, "scored": scored,
            "l7": "enforcement exposure, not scored from posts", "l8": "reference"}


def demo_status(engine) -> dict:
    """Per-layer readback for the demo sources, as Galaxy would see them."""
    from app.clhear.derived_models import activities, applies_to, blocks, obligations, requires
    from app.clhear.l1.models import clauses, source_versions, sources
    from app.clhear.l6.composer import compose
    from app.clhear.l7.models import enforcement_events, enforcement_links, risk_scores
    from app.clhear.l8.reference import reference_rows
    from app.clhear.observations import attach_performance

    with engine.connect() as conn:
        l1 = {}
        for key in (*DEMO_SOURCE_KEYS, *ENFORCEMENT_SOURCE_KEYS, *REFERENCE_SOURCE_KEYS):
            version = conn.execute(
                sa.select(source_versions.c.id, source_versions.c.version_label, source_versions.c.retrieved_at)
                .join(sources, sources.c.id == source_versions.c.source_id)
                .where(sources.c.key == key, source_versions.c.status == "in_force")
                .order_by(source_versions.c.id.desc()).limit(1)).first()
            count = conn.execute(sa.select(sa.func.count()).select_from(clauses).where(
                clauses.c.source_version_id == version.id)).scalar_one() if version else 0
            l1[key] = {"in_force": version is not None, "version_label": version.version_label if version else None,
                       "retrieved_at": version.retrieved_at if version else None, "clauses": count}
        obligation_ids = [r for (r,) in conn.execute(sa.select(obligations.c.id).where(
            obligations.c.source_key.in_(LEGAL_SOURCE_KEYS), obligations.c.valid_to.is_(None)).order_by(obligations.c.id))]
        edges = conn.execute(sa.select(requires.c.obligation_id, requires.c.block_id).where(
            requires.c.obligation_id.in_(obligation_ids), requires.c.valid_to.is_(None))).all()
        block_ids = sorted({block_id for _, block_id in edges})
        names = dict(conn.execute(sa.select(blocks.c.id, blocks.c.name).where(blocks.c.id.in_(block_ids))).all())
        predicates = conn.execute(sa.select(sa.func.count()).select_from(applies_to).where(
            applies_to.c.obligation_id.in_(obligation_ids), applies_to.c.valid_to.is_(None))).scalar_one()
        triggered = sorted(row.id for row in conn.execute(sa.select(activities.c.id, activities.c.triggers).where(
            activities.c.valid_to.is_(None))) if set(_anchored(row.triggers)) & set(obligation_ids))
        linked = [dict(row) for row in conn.execute(
            sa.select(enforcement_links.c.obligation_id, enforcement_links.c.citation, enforcement_links.c.method,
                      enforcement_events.c.id, enforcement_events.c.source_key, enforcement_events.c.decided_on,
                      enforcement_events.c.amount, enforcement_events.c.kind)
            .join(enforcement_events, sa.and_(enforcement_events.c.id == enforcement_links.c.event_id,
                                              enforcement_events.c.valid_to.is_(None)))
            .where(enforcement_links.c.obligation_id.in_(obligation_ids), enforcement_links.c.valid_to.is_(None))
            .order_by(enforcement_events.c.decided_on)).mappings()]
        scores = {row.subject_ref: {"composite": row.composite, "band": row.band} for row in conn.execute(
            sa.select(risk_scores.c.subject_ref, risk_scores.c.composite, risk_scores.c.band).where(
                risk_scores.c.subject_ref.in_(obligation_ids), risk_scores.c.valid_to.is_(None)))}
    blueprint = attach_performance(engine, compose(engine, {"attributes": dict(GALAXY_ATTRIBUTES)},
                                                   requested_by="demo.status", log_request=False))
    score = blueprint.get("compliance_score") or {}
    reference = reference_rows(engine, blueprint=blueprint)
    return {
        "schema": "clhear.demo-status.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "L1": l1,
        "L2": {"obligations": obligation_ids},
        "L3": {"blocks": [{"id": b, "name": names.get(b, "")} for b in block_ids]},
        "L4": {"applies_to": predicates, "profile": dict(GALAXY_ATTRIBUTES)},
        "L5": {"activities": triggered},
        "L6": {"items": [item.get("block_id") for item in blueprint.get("items") or []],
               "compliance_score": {k: score.get(k) for k in ("value", "passed", "required", "not_applicable")}},
        "L7": {"events": linked, "risk_scores": scores},
        "L8": {"status": "reference", "rows": [{"id": row["id"], "block_id": row["block_id"],
                                                 "on_blueprint": row["on_blueprint"]} for row in reference]},
    }


def _anchored(triggers) -> list[str]:
    out = []
    for trigger in triggers or []:
        if not isinstance(trigger, dict) or trigger.get("superseded_by"):
            continue
        if trigger.get("obligation"):
            out.append(trigger["obligation"])
        anchor = trigger.get("anchor")
        if isinstance(anchor, dict) and anchor.get("source_key"):
            out.extend(obligation_id(anchor["source_key"], ref) for ref in anchor.get("refs") or [])
    return out


def publish_demo_status(engine, *, s3_client=None) -> str | None:
    """Write ``demo/status.json`` beside the candidate viewer; None off S3."""
    import json

    from app.clhear.l1.viewer_snapshot import configured_uri

    uri = configured_uri()
    if not uri or not uri.startswith("s3://"):
        return None
    bucket, key = uri[len("s3://"):].split("/", 1)
    target = key.rsplit("/", 1)[0] + "/demo/status.json"
    if s3_client is None:
        import boto3

        from app.clhear.settings import get_settings
        s3_client = boto3.client("s3", region_name=get_settings().aws_region)
    body = json.dumps(demo_status(engine), sort_keys=True, default=str).encode("utf-8")
    s3_client.put_object(Bucket=bucket, Key=target, Body=body, ContentType="application/json",
                         ServerSideEncryption="AES256")
    return f"s3://{bucket}/{target}"
