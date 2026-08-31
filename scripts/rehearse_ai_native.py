#!/usr/bin/env python3
"""Offline nightly rehearsal against a corpus snapshot.

Uses a grounded FakeProvider (quotes only ids/spans present in the prompt)
so fleets, eval gates, GPU dry-run, and Eval Studio sampling run without
frontier keys or a real GPU. Intended for deploy: migrate the live
snapshot, populate AI-native tables, then upload via deploy_webui.sh.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Keep DATABASE_URL pointing at the snapshot before settings/engine cache.
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "deploy" / "clhear.db"


def _grounded_script(*, prompt: str, system: str | None, model: str) -> str:
    """Return structurally valid JSON that cannot invent closed-world refs."""
    # L4 license extract: [source_key#ref]
    lic = re.findall(r"\[([^\]#\s]+)#([^\]]+)\]", prompt)
    if "license_types" in prompt:
        types = []
        for key, ref in lic[:3]:
            types.append({
                "name": f"{key.split('/')[-1]} authorisation",
                "issuing_regime": key,
                "source_key": key,
                "ref": ref,
            })
        return json.dumps({"license_types": types})

    # L3 blocks: "- OBL:… [source_key #ref] title"
    blks = re.findall(r"- (OBL:\S+) \[([^\]#]+) #([^\]]+)\]", prompt)
    if "satisfies" in prompt and "building block" in prompt:
        sat = [{"source_key": sk.strip(), "refs": [ref.strip()]} for _, sk, ref in blks[:4]]
        title = blks[0][2] if blks else "control"
        return json.dumps({
            "name": f"AI-designed {title[:40]} programme",
            "description": "Reusable control covering the clustered live obligations.",
            "capability": "compliance-programme",
            "evidence_artifacts": ["policy-pack", "attestation-log"],
            "satisfies": sat,
        })

    # L2 consolidate: "[OBL:…] (jur, source)"
    oids = re.findall(r"\[(OBL:[^\]]+)\]", prompt)
    if "canonical_statement" in prompt or "member_notes" in prompt:
        notes = {oid: "same underlying duty" for oid in oids[:6]}
        return json.dumps({
            "name": "Keep prescribed records across jurisdictions",
            "canonical_statement": "Keep the records the applicable regime requires for the stated period.",
            "member_notes": notes,
        })

    # L2 duty triage: quote a span from CLAUSE:
    if "evidence_span" in prompt:
        clause = prompt.split("CLAUSE:\n", 1)[-1]
        words = clause.split()
        span = " ".join(words[3:12]) if len(words) >= 12 else " ".join(words[:8])
        if len(span) < 12:
            span = clause[:40]
        return json.dumps({
            "is_duty": True,
            "modality": "should",
            "evidence_span": span,
            "addressee": "relevant person",
        })

    # L5 activity map — empty `when` is always a subset of the schema.
    if "activity_name" in prompt or "business activity" in prompt:
        return json.dumps({
            "activity_name": "Customer due-diligence operations",
            "description": "Mapped from a live obligation; no invented attributes.",
            "business_owner": "compliance",
            "when": {},
        })

    # L6 rationale — cite only ids listed in the prompt.
    ids = re.findall(r"\b(?:OBL:[A-Za-z0-9_./#-]+|BLK:[A-Za-z0-9_-]+|ACT:[A-Za-z0-9_-]+|CON:[A-Za-z0-9_-]+)\b", prompt)
    if "rationale" in prompt or "program rationale" in prompt:
        cited = ids[:4] or ["OBL:unknown"]
        text = (
            f"The programme covers the sampled obligations ({cited[0]}). "
            + (f"Control {cited[1]} is in the blueprint. " if len(cited) > 1 else "")
            + "Gaps are those the composer listed; no id outside the blueprint is cited."
        )
        return json.dumps({"rationale": text})

    # L7 narrative — copy figures from INPUT VECTOR.
    if "INPUT VECTOR" in prompt or "narrative" in prompt.lower():
        nums = re.findall(r"\d+(?:\.\d+)?", prompt.split("INPUT VECTOR", 1)[-1][:800])
        score = nums[0] if nums else "0"
        return json.dumps({
            "narrative": f"The computed score is {score} using only figures from the input vector.",
            "facts_used": [],
        })

    # Revalidation / eval judge
    if "accept" in prompt.lower() or "verdict" in prompt.lower():
        return json.dumps({"verdict": "accept", "rationale": "Grounded rehearsal accept."})

    return json.dumps({"ok": True})


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags = {a for a in sys.argv[1:] if a.startswith("-")}
    # --full runs FakeProvider through every fleet (dev). Default --safe
    # migrates, GPU-dry-runs, L6/L7/L8, and samples Eval Studio without
    # writing junk L3/L4/L5 names onto a production snapshot.
    safe = "--full" not in flags
    db = Path(os.environ.get("CLHEAR_REHEARSE_DB") or (args[0] if args else DEFAULT_DB))
    if not db.exists():
        print(f"missing snapshot: {db}", file=sys.stderr)
        return 2
    os.environ["DATABASE_URL"] = f"sqlite:///{db.resolve()}"
    os.environ.setdefault("CLHEAR_LLM_PROVIDER", "fake")
    # No GPU subnet → launch_nightly_gpu records a dry-run (no EC2 spend).
    os.environ.pop("CLHEAR_GPU_SUBNET_ID", None)
    os.environ.pop("CLHEAR_GPU_SECURITY_GROUP_ID", None)

    from app.clhear.db import get_engine, run_migrations
    from app.clhear.eval_studio import sample_tasks
    from app.clhear.platform.gateway import FakeProvider
    from app.clhear.platform.router import Router

    engine = get_engine()
    applied = run_migrations(engine)
    print(f"migrations applied: {applied or 'none (already current)'}")

    fake = FakeProvider(script=_grounded_script)
    llm = Router(
        engine,
        providers={"ollama": fake, "anthropic": fake, "openai": fake, "xai": fake, "fake": fake},
        gpu_open=True,
    )
    if safe:
        from app.clhear.fleets import run_nightly_stack
        from app.clhear.platform.gpu import launch_nightly_gpu, orphan_guard, terminate_gpu
        from app.clhear import curated
        from app.clhear.l2.extract import run_extraction
        from app.clhear.l8.cohorts import refresh_cohorts
        from app.clhear.l6.rationale import narrate_blueprint
        from app.clhear.l7.narrate import narrate_risk
        from app.clhear import layer_service, ai_ops
        from app.clhear.derived_models import sample_profiles
        from app.clhear.models import runs
        from datetime import datetime, timezone
        import sqlalchemy as sa

        orphan_guard(engine)
        gpu = launch_nightly_gpu(engine)
        started = datetime.now(timezone.utc)
        seeded = curated.seed(engine)
        extraction = run_extraction(engine)
        cohorts = refresh_cohorts(engine)
        rationales = []
        for item in layer_service.layer_items(engine, "L6")[:3]:
            with engine.connect() as conn:
                prow = conn.execute(sa.select(sample_profiles).where(sample_profiles.c.id == item["profile_id"])).first()
            if prow is None:
                continue
            bp = layer_service._profile_blueprint(engine, prow)
            rationales.append(narrate_blueprint(engine, llm, bp))
        narratives = [narrate_risk(engine, llm, it) for it in layer_service.risk_items(engine)[:4]]
        outputs = {
            "extraction": extraction,
            "curated": seeded,
            "rationales": rationales,
            "narratives": [{"written": n.get("written"), "id": n.get("id")} for n in narratives],
            "cohorts": cohorts,
            "gpu": gpu,
            "mode": "safe",
        }
        reasoning = (
            f"Safe rehearsal: extraction unchanged={extraction.get('unchanged', 0)}, "
            f"{sum(1 for r in rationales if r.get('written'))} L6 rationales, "
            f"{sum(1 for n in narratives if n.get('written'))} L7 narratives, "
            f"L8 synthetic={cohorts.get('synthetic', 0)}; GPU {gpu.get('status')}"
        )
        ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        with engine.begin() as conn:
            conn.execute(
                runs.insert().values(
                    fleet="ai.nightly", trigger="rehearsal", inputs={"safe": True},
                    outputs=outputs, duration_ms=ms, reasoning=reasoning,
                )
            )
        ai_ops.record(engine, kind="fleet_generation", layer="L0", fleet="ai.nightly",
                      reasoning=reasoning, detail={"mode": "safe"})
        out = outputs
        terminate_gpu(engine, gpu.get("id") if isinstance(gpu, dict) else None)
    else:
        from app.clhear.fleets import run_nightly_if_due

        out = run_nightly_if_due(engine, llm, force=True)
    print(json.dumps(out, default=str, indent=2)[:4000])
    sampled = sample_tasks(engine)
    print(f"eval studio sampled: {sampled}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
