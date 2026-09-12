"""Nightly AI fleet orchestrator — one pass after L1 ingest."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

from app.clhear.models import runs

log = logging.getLogger("clhear.fleets")

FLEET_RUN = "ai.nightly"


def _already_ran_today(engine: Engine) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    import sqlalchemy as sa

    with engine.connect() as conn:
        for row in conn.execute(
            sa.select(runs).where(runs.c.fleet == FLEET_RUN).order_by(runs.c.id.desc()).limit(8)
        ):
            ts = str(row.created_at)
            if not ts.startswith(today):
                continue
            # Safe FakeProvider rehearsals must not block a live GPU night.
            if (row.trigger or "") == "rehearsal":
                continue
            return True
    return False


def run_nightly_stack(engine: Engine, llm, *, force: bool = False) -> dict:
    """Extract → triage → consolidate → L3/L4/L5/L6/L7/L8 → eval gates."""
    from app.clhear import curated
    from app.clhear.l2.concepts import flag_stale_concepts
    from app.clhear.l2.consolidate import draft_and_propose
    from app.clhear.l2.extract import run_extraction
    from app.clhear.l2.triage import triage_duties
    from app.clhear.l3.generate import generate_blocks
    from app.clhear.l4.licenses import extract_licenses
    from app.clhear.l5.map import map_activities
    from app.clhear.l7.narrate import narrate_risk
    from app.clhear.l8.cohorts import refresh_cohorts
    from app.clhear.platform import evals as ev

    started = datetime.now(timezone.utc)
    seeded = curated.seed(engine)
    extraction = run_extraction(engine)
    triage = triage_duties(engine, llm)
    concepts_seed = curated.seed_concepts(engine)
    flagged = flag_stale_concepts(engine)
    consolidation = draft_and_propose(engine, llm)
    blocks = generate_blocks(engine, llm)
    licenses = extract_licenses(engine, llm)
    activities = map_activities(engine, llm)
    # L6 rationale: narrate the latest sample-profile blueprints (computed).
    from app.clhear import layer_service
    from app.clhear.l6.rationale import narrate_blueprint

    rationales = []
    for item in layer_service.layer_items(engine, "L6")[:3]:
        # Re-compose to get a full blueprint with coverage ids.
        from app.clhear.derived_models import sample_profiles
        import sqlalchemy as sa

        with engine.connect() as conn:
            prow = conn.execute(sa.select(sample_profiles).where(sample_profiles.c.id == item["profile_id"])).first()
        if prow is None:
            continue
        bp = layer_service._profile_blueprint(engine, prow)
        rationales.append(narrate_blueprint(engine, llm, bp))
    risk_items = layer_service.risk_items(engine)[:4]
    narratives = [narrate_risk(engine, llm, it) for it in risk_items]
    cohorts = refresh_cohorts(engine)
    gates = {}
    for suite in (
        "l2_basis_integrity", "l2_extraction_quality", "l2_concept_integrity",
        "l3_l5_referential", "l4_grounding", "l6_citation", "l7_number_echo", "l8_k_anonymity",
    ):
        try:
            gates[suite] = ev.run_suite(engine, suite, release=started.strftime("%Y%m%dT%H%M%SZ"))
        except Exception as exc:
            log.exception("suite %s failed", suite)
            gates[suite] = {"suite": suite, "passed": False, "error": str(exc)[:200]}
    outputs = {
        "extraction": extraction,
        "triage": triage,
        "curated": seeded,
        "concepts": concepts_seed,
        "consolidation": consolidation,
        "flagged_concepts": flagged,
        "blocks": blocks,
        "licenses": licenses,
        "activities": activities,
        "rationales": rationales,
        "narratives": [{"written": n.get("written"), "id": n.get("id")} for n in narratives],
        "cohorts": cohorts,
        "gates": {k: {"passed": v.get("passed")} for k, v in gates.items()},
    }
    reasoning = (
        f"Nightly fleets: {extraction.get('inserted', 0)} obligations extracted, "
        f"{triage.get('inserted', 0)} triaged, {consolidation.get('applied', 0)} concepts applied, "
        f"{blocks.get('written', 0)} blocks, {licenses.get('written', 0)} licenses, "
        f"{activities.get('written', 0)} activities; "
        f"{sum(1 for g in gates.values() if g.get('passed'))}/{len(gates)} eval gates green"
    )
    import time

    # duration approximated
    ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    import sqlalchemy as sa

    with engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                fleet=FLEET_RUN, trigger="schedule", inputs={"force": force},
                outputs=outputs, duration_ms=ms, reasoning=reasoning,
            )
        )
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L0", fleet=FLEET_RUN,
            reasoning=reasoning, detail={"gates": outputs["gates"]},
        )
    except Exception:
        log.exception("nightly ai_ops failed")
    return outputs


def run_nightly_if_due(
    engine: Engine,
    llm,
    *,
    force: bool = False,
    client_factory=None,
    sleeper=None,
    http_get=None,
) -> dict | None:
    if not force and _already_ran_today(engine):
        return None
    from app.clhear.platform import gpu as gpu_mod

    gpu_mod.orphan_guard(engine, client_factory=client_factory)
    gpu = gpu_mod.launch_nightly_gpu(engine, client_factory=client_factory, sleeper=sleeper)
    previous = None
    gpu_ready = {"ready": False}
    try:
        url = (gpu.get("detail") or {}).get("ollama_url") if isinstance(gpu, dict) else None
        if url and gpu.get("status") in ("launching", "running"):
            gpu_ready = gpu_mod.wait_for_ollama(
                engine,
                gpu.get("id"),
                http_get=http_get,
                sleeper=sleeper,
            )
            if gpu_ready.get("ready"):
                previous = gpu_mod.attach_router(llm, gpu_ready.get("url") or url)
        outputs = run_nightly_stack(engine, llm, force=force)
        return {**outputs, "gpu": gpu, "gpu_ready": bool(gpu_ready.get("ready"))}
    finally:
        gpu_mod.detach_router(llm, previous)
        gpu_mod.terminate_gpu(
            engine,
            gpu.get("id") if isinstance(gpu, dict) else None,
            client_factory=client_factory,
        )
