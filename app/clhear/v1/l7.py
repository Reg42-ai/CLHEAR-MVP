"""L7 public API — risk and priority scoring (HLD v2 §4.7 / §5).

    GET  /l7/method                                  the published method: weights, dimensions, bands, likelihood fit
    GET  /l7/scores?obligation=&kind=&band=&blueprint=   current scores (obligation or item), highest first
    GET  /l7/scores/{id}                             one score: dimensions, evidence, calibration run, why-trail, history
    GET  /l7/enforcement?regulator=&jurisdiction=&kind=&since=&q=&obligation=
                                                     enforcement explorer: who, when, how much, which obligations
    GET  /l7/enforcement/{id}                        one event with its links, versions and why-trail
    GET  /l7/obligations/{ref}/enforcement           "who was fined for this obligation, when, how much"
    GET  /l7/obligations/{ref}/score                 the obligation's current score (+ history)
    GET  /l7/blueprints/{id}/priorities              priority view over a blueprint: items ranked by composite
    GET  /l7/calibrations                            every calibration run (Brier, baseline, reliability)
    GET  /l7/scorecard                               gates, bands, latest calibration

Open by mode (I9): base priorities, enforcement events and the method are open;
instance priorities (the organisation's Actual overlay) are instance-mode only.
Nothing here serves clause text.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.clhear.db import get_engine
from app.clhear.derived_models import blueprint_items, blueprints, obligations
from app.clhear.l7 import enforcement as enf
from app.clhear.l7 import score as l7_score
from app.clhear.l7.models import BANDS, EVENT_KINDS, METHOD_VERSION, enforcement_events, method, risk_calibrations, risk_scores
from app.clhear.platform import record

router = APIRouter(prefix="/l7", tags=["l7"])
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _why_rows(conn, ids: list[str]) -> list[dict]:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return []
    rows = {r["id"]: r for r in conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id.in_(ids))).mappings()}
    return [
        {"id": r["id"], "layer": r["layer"], "subject_ref": r["subject_ref"], "reasoning_summary": r["reasoning_summary"],
         "evidence_refs": r["evidence_refs"], "model_manifest": r["model_manifest"], "skill_version": r["skill_version"],
         "confidence": float(r["confidence"]) if r["confidence"] is not None else None, "agent_id": r["agent_id"],
         "created_at": str(r["created_at"])}
        for wid in ids if (r := rows.get(wid)) is not None
    ]


def _obligation_cards(conn, ids: list[str]) -> dict[str, dict]:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    rows = conn.execute(sa.select(obligations.c.id, obligations.c.stable_id, obligations.c.source_key, obligations.c.clause_ref,
                                  obligations.c.title, obligations.c.jurisdiction, obligations.c.regulator, obligations.c.status)
                        .where(obligations.c.id.in_(ids))).mappings()
    return {r["id"]: dict(r) for r in rows}


def _resolve(conn, ref: str) -> str:
    oid = l7_score.resolve_obligation_ref(conn, ref)
    if oid is None:
        raise HTTPException(status_code=404, detail=f"unknown obligation {ref}")
    return oid


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def l7_page() -> HTMLResponse:
    """HLD v2 §4.7 L7 UI: priority view over a blueprint, enforcement explorer, published method."""
    return HTMLResponse((WEB_DIR / "l7.html").read_text(), headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/method")
def get_method() -> dict:
    with get_engine().connect() as conn:
        cal = l7_score.latest_calibration(conn)
    return {**method(), "scorer_version": l7_score.SCORER_VERSION, "half_life_years": l7_score.HALF_LIFE_YEARS,
            "attention_window_years": l7_score.ATTENTION_YEARS, "latest_calibration": cal,
            "parameters": ({k: cal["parameters"][k] for k in ("a", "b", "c")} if cal else l7_score.DEFAULT_PARAMETERS)}


# ----------------------------------------------------------------- scores


@router.get("/scores")
def list_scores(obligation: str | None = None, kind: str | None = Query(default=None, pattern="^(obligation|item)$"),
                band: str | None = None, blueprint: str | None = None, min_composite: float | None = None,
                limit: int = Query(default=200, le=1000)) -> dict:
    if band and band not in {b for b, _ in BANDS}:
        raise HTTPException(status_code=422, detail=f"band must be one of {[b for b, _ in BANDS]}")
    with get_engine().connect() as conn:
        subject = _resolve(conn, obligation) if obligation else None
        items = l7_score.list_scores(conn, kind=kind or ("obligation" if obligation else None), subject_ref=subject, band=band,
                                     blueprint_id=blueprint, min_composite=min_composite, limit=limit)
        cards = _obligation_cards(conn, [s["subject_ref"] for s in items if s["subject_kind"] == "obligation"])
    for s in items:
        if s["subject_kind"] == "obligation":
            s["obligation"] = cards.get(s["subject_ref"])
    return {"count": len(items), "method_version": METHOD_VERSION, "items": items}


@router.get("/scores/{score_id}")
def get_score(score_id: str) -> dict:
    with get_engine().connect() as conn:
        s = l7_score.get_score(conn, score_id)
        if s is None:
            raise HTTPException(status_code=404, detail=f"unknown score {score_id}")
        s["history"] = l7_score.list_scores(conn, kind=s["subject_kind"], subject_ref=s["subject_ref"], include_history=True, limit=50)
        s["why"] = _why_rows(conn, [s["why_trail_id"]])
        if s["subject_kind"] == "obligation":
            s["obligation"] = _obligation_cards(conn, [s["subject_ref"]]).get(s["subject_ref"])
            s["enforcement"] = enf.events_for_obligation(conn, s["subject_ref"])
        cal = conn.execute(sa.select(risk_calibrations).where(risk_calibrations.c.id == s["calibration_set_ref"])).mappings().first()
        s["calibration"] = l7_score._plain(dict(cal)) if cal else None
    return s


# ----------------------------------------------------------------- enforcement explorer


@router.get("/enforcement")
def list_enforcement(regulator: str | None = None, jurisdiction: str | None = None, kind: str | None = None,
                     since: date | None = None, q: str | None = None, obligation: str | None = None,
                     limit: int = Query(default=200, le=1000)) -> dict:
    if kind and kind not in EVENT_KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {list(EVENT_KINDS)}")
    with get_engine().connect() as conn:
        oid = _resolve(conn, obligation) if obligation else None
        events = enf.list_events(conn, regulator=regulator, jurisdiction=jurisdiction, kind=kind, since=since, q=q,
                                 obligation_id=oid, limit=limit)
        links = enf.links_for_events(conn, [e["id"] for e in events])
        cards = _obligation_cards(conn, [l["obligation_id"] for ls in links.values() for l in ls])
    for e in events:
        e["links"] = [{**l, "obligation": cards.get(l["obligation_id"])} for l in links.get(e["id"], [])]
    total_amount = sum(e["amount"] or 0.0 for e in events)
    return {"count": len(events), "total_amount": round(total_amount, 2), "items": events}


@router.get("/enforcement/{event_id}")
def get_enforcement(event_id: str) -> dict:
    with get_engine().connect() as conn:
        rows = [enf._plain(dict(r)) for r in conn.execute(sa.select(enforcement_events).where(enforcement_events.c.id == event_id)
                                                          .order_by(enforcement_events.c.version.desc())).mappings()]
        if not rows:
            raise HTTPException(status_code=404, detail=f"unknown enforcement event {event_id}")
        live = next((r for r in rows if r["valid_to"] is None), rows[0])
        links = enf.links_for_events(conn, [event_id]).get(event_id, [])
        cards = _obligation_cards(conn, [l["obligation_id"] for l in links])
        why = _why_rows(conn, [live["why_trail_id"]] + [l["why_trail_id"] for l in links])
    live["links"] = [{**l, "obligation": cards.get(l["obligation_id"])} for l in links]
    live["versions"] = [{k: r[k] for k in ("version", "valid_from", "valid_to", "amount", "kind", "text_hash", "derived_at")} for r in rows]
    live["why"] = why
    return live


@router.get("/obligations/{ref:path}/enforcement")
def obligation_enforcement(ref: str) -> dict:
    with get_engine().connect() as conn:
        oid = _resolve(conn, ref)
        events = enf.events_for_obligation(conn, oid)
        card = _obligation_cards(conn, [oid]).get(oid)
    fines = [e for e in events if e["amount"]]
    return {"obligation": card, "count": len(events), "total_amount": round(sum(e["amount"] for e in fines), 2),
            "largest": max((e["amount"] for e in fines), default=None),
            "regulators": sorted({e["regulator"] for e in events}), "items": events}


@router.get("/obligations/{ref:path}/score")
def obligation_score(ref: str) -> dict:
    with get_engine().connect() as conn:
        oid = _resolve(conn, ref)
        current = l7_score.list_scores(conn, kind="obligation", subject_ref=oid, limit=1)
        history = l7_score.list_scores(conn, kind="obligation", subject_ref=oid, include_history=True, limit=50)
        card = _obligation_cards(conn, [oid]).get(oid)
    return {"obligation": card, "score": current[0] if current else None, "history": history, "method_version": METHOD_VERSION}


# ----------------------------------------------------------------- priority view


@router.get("/blueprints/{blueprint_id}/priorities")
def blueprint_priorities(blueprint_id: str, band: str | None = None) -> dict:
    with get_engine().connect() as conn:
        bp = conn.execute(sa.select(blueprints.c.stable_id, blueprints.c.status, blueprints.c.profile_id)
                          .where(blueprints.c.stable_id == blueprint_id)).mappings().first()
        if bp is None:
            raise HTTPException(status_code=404, detail=f"unknown blueprint {blueprint_id}")
        items = {r["id"]: dict(r) for r in conn.execute(sa.select(blueprint_items.c.id, blueprint_items.c.block_id, blueprint_items.c.kind,
                                                                  blueprint_items.c.name, blueprint_items.c.basis,
                                                                  blueprint_items.c.obligations_satisfied)
                                                        .where(blueprint_items.c.blueprint_id == blueprint_id)).mappings()}
        scores = l7_score.list_scores(conn, kind="item", blueprint_id=blueprint_id, band=band, limit=1000)
        ob_ids = sorted({o for it in items.values() for o in l7_score._json(it["obligations_satisfied"], [])})
        ob_scores = {s["subject_ref"]: s for s in l7_score.list_scores(conn, kind="obligation", limit=5000) if s["subject_ref"] in set(ob_ids)}
        cards = _obligation_cards(conn, ob_ids)
    ranked = []
    for s in scores:
        it = items.get(s["subject_ref"], {})
        ranked.append({**s, "item": {k: it.get(k) for k in ("id", "block_id", "kind", "name", "basis")},
                       "obligations": [{"id": o, **({"score": ob_scores[o]["composite"], "band": ob_scores[o]["band"]} if o in ob_scores else {}),
                                        "card": cards.get(o)} for o in l7_score._json(it.get("obligations_satisfied"), [])]})
    unscored = [i for i in items if i not in {s["subject_ref"] for s in scores}]
    bands: dict[str, int] = {}
    for s in scores:
        bands[s["band"]] = bands.get(s["band"], 0) + 1
    return {"blueprint_id": blueprint_id, "status": bp["status"], "profile_id": bp["profile_id"], "method_version": METHOD_VERSION,
            "count": len(ranked), "bands": bands, "unscored_items": unscored, "items": ranked,
            "openness": method()["openness"]}


# ----------------------------------------------------------------- calibration + scorecard


@router.get("/calibrations")
def list_calibrations(limit: int = Query(default=50, le=500)) -> dict:
    with get_engine().connect() as conn:
        rows = [l7_score._plain(dict(r)) for r in conn.execute(sa.select(risk_calibrations)
                                                               .order_by(risk_calibrations.c.ran_at.desc(), risk_calibrations.c.id.desc())
                                                               .limit(limit)).mappings()]
    for r in rows:
        for k in ("training_years", "parameters", "reliability"):
            r[k] = l7_score._json(r.get(k), [] if k != "parameters" else {})
        for k in ("brier", "baseline_brier"):
            if r.get(k) is not None:
                r[k] = float(r[k])
    return {"count": len(rows), "items": rows}


@router.get("/scorecard")
def l7_scorecard() -> dict:
    from app.clhear.platform.gates import GATE_THRESHOLDS, gate_status

    engine = get_engine()
    with engine.connect() as conn:
        card = l7_score.scorecard(conn)
        top = l7_score.list_scores(conn, kind="obligation", limit=10)
        cards = _obligation_cards(conn, [s["subject_ref"] for s in top])
        superseded = conn.execute(sa.select(sa.func.count()).select_from(risk_scores).where(risk_scores.c.status == "superseded")).scalar_one()
    for s in top:
        s["obligation"] = cards.get(s["subject_ref"])
    return {"gate": gate_status(engine, "L7"), "thresholds": GATE_THRESHOLDS.get("L7", {}), **card,
            "superseded_scores": int(superseded), "top": top, "weights": method()["weights"]}
