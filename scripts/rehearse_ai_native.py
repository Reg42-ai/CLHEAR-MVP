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
    db = Path(os.environ.get("CLHEAR_REHEARSE_DB") or (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB))
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
    from app.clhear.fleets import run_nightly_if_due
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
    out = run_nightly_if_due(engine, llm, force=True)
    print(json.dumps(out, default=str, indent=2)[:4000])
    sampled = sample_tasks(engine)
    print(f"eval studio sampled: {sampled}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
