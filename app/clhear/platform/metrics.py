"""Observability surface (HLD v2 §3 "Observability", §7.1 SLOs; items 10 and 17).

Two views of the same numbers:

* :func:`status` — the JSON the public status page and the Upptime probe read:
  API up, per-layer freshness against the ≤ 24 h SLO, per-layer gate status, the
  current release, and the SLO table with "met" flags.
* :func:`prometheus` — the same as Prometheus text exposition for the AMP scrape
  (``/metrics``): freshness seconds, gate status 0/1, audit counters, fleet runs.

Freshness is measured from the data itself (latest ``retrieved_at`` for L1, latest
``derived_at`` for L2–L8), not from a heartbeat, so a fleet that runs but writes
nothing shows as stale. Gate status comes from ``gates.gate_status`` — the same
source the evals dashboard uses — so the status page can never disagree with it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

SLOS = (
    {"name": "api_availability", "description": "API availability over 30 days", "target": 0.999, "unit": "ratio",
     "measured_by": "Upptime probe of /status.json every 5 minutes (status/.upptimerc.yml)"},
    {"name": "freshness_tier_a", "description": "Tier-A sources: publication → ingestion lag", "target": 24 * 3600, "unit": "seconds",
     "measured_by": "clhear_layer_freshness_seconds{layer=\"L1\"} and the L1 currency eval"},
    {"name": "derived_freshness", "description": "Derived layers (L2–L8) re-derived after an L1 change", "target": 48 * 3600, "unit": "seconds",
     "measured_by": "clhear_layer_freshness_seconds{layer!=\"L1\"}"},
    {"name": "gates_green", "description": "Every layer gate passing on the current release", "target": 1.0, "unit": "ratio",
     "measured_by": "clhear_gate_passed"},
    {"name": "dr_drilled", "description": "Nightly restore drill (record + graph + datalake replica) passed within 48 h", "target": 1.0, "unit": "ratio",
     "measured_by": "clhear_dr_drill_passed and clhear_dr_drill_age_seconds (dr_drill.yml, l0_platform.dr_drills)"},
)
FRESHNESS_SLO_S = 24 * 3600
DERIVED_FRESHNESS_SLO_S = 48 * 3600
LAYERS = ("L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _layer_latest(conn, layer: str) -> datetime | None:
    """Latest data timestamp for a layer, or None when the layer has no rows."""
    from app.clhear.platform import record

    if layer == "L1":
        from app.clhear.l1.models import source_versions

        return _as_utc(conn.execute(sa.select(sa.func.max(source_versions.c.retrieved_at))).scalar())
    latest: datetime | None = None
    for table in record.layer_tables():
        if not (table.schema or "").startswith(layer.lower() + "_") or "derived_at" not in table.c:
            continue
        ts = _as_utc(conn.execute(sa.select(sa.func.max(table.c.derived_at))).scalar())
        if ts and (latest is None or ts > latest):
            latest = ts
    return latest


def freshness(engine: Engine, now: datetime | None = None) -> dict[str, dict]:
    now = now or _now()
    out: dict[str, dict] = {}
    with engine.connect() as conn:
        for layer in LAYERS:
            latest = _layer_latest(conn, layer)
            slo = FRESHNESS_SLO_S if layer == "L1" else DERIVED_FRESHNESS_SLO_S
            age = None if latest is None else max(0, int((now - latest).total_seconds()))
            out[layer] = {"latest": latest.isoformat() if latest else None, "age_seconds": age, "slo_seconds": slo,
                          "ok": age is not None and age <= slo, "empty": latest is None}
    return out


def gates(engine: Engine, release: str | None = None) -> dict[str, dict]:
    from app.clhear.platform.gates import LAYER_GATES, gate_status

    out = {}
    for layer in LAYER_GATES:
        st = gate_status(engine, layer, release)
        out[layer] = {"passed": bool(st["passed"]), "missing": list(st.get("missing") or []),
                      "suites": {s: bool(r["passed"]) for s, r in st["suites"].items()}}
    return out


def _release(engine: Engine) -> dict:
    """The release the ``latest`` pointer names: one GetObject on ``latest.json`` plus
    its manifest, rather than listing and reading every release folder on each
    status probe. ``latest`` is what the API serves, so it is the release to report."""
    try:
        from app.clhear.releases import get_latest, list_releases

        rel = get_latest(engine) or (list_releases(engine) or [{}])[0]
        return {"id": rel.get("release") or rel.get("id"), "generated_at": rel.get("generated_at")}
    except Exception:  # noqa: BLE001 — the status page must render even when the release store is unreachable
        log.warning("status: release store unreachable", exc_info=True)
        return {"id": None, "generated_at": None}


def _audit_counts(engine: Engine, since: datetime) -> dict[str, int]:
    from app.clhear.platform.audit import audit_log

    with engine.connect() as conn:
        rows = conn.execute(sa.select(audit_log.c.action, sa.func.count()).where(audit_log.c.at >= since).group_by(audit_log.c.action))
        return {a: int(n) for a, n in rows}


def _fleet_runs(engine: Engine, since: datetime) -> dict[str, int]:
    from app.clhear.models import runs

    with engine.connect() as conn:
        rows = conn.execute(sa.select(runs.c.fleet, sa.func.count()).where(runs.c.created_at >= since).group_by(runs.c.fleet))
        return {f: int(n) for f, n in rows}


def status(engine: Engine, *, release: str | None = None, now: datetime | None = None) -> dict:
    """What the /status page and the Upptime probe read."""
    from app.clhear.platform.dr import last_drill

    now = now or _now()
    fresh = freshness(engine, now)
    g = gates(engine, release)
    drill = last_drill(engine, now=now)
    populated = {k: v for k, v in fresh.items() if not v["empty"]}
    l1_ok = fresh["L1"]["ok"]
    derived_ok = all(v["ok"] for k, v in populated.items() if k != "L1") if populated else False
    gates_ratio = (sum(1 for v in g.values() if v["passed"]) / len(g)) if g else 0.0
    slos = []
    for s in SLOS:
        if s["name"] == "api_availability":
            current, met = 1.0, True  # this response *is* the probe; the 30-day ratio is Upptime's to compute
        elif s["name"] == "freshness_tier_a":
            current, met = fresh["L1"]["age_seconds"], l1_ok
        elif s["name"] == "derived_freshness":
            ages = [v["age_seconds"] for k, v in populated.items() if k != "L1"]
            current, met = (max(ages) if ages else None), derived_ok
        elif s["name"] == "dr_drilled":
            met = drill["drilled"] and drill["passed"]
            current = 1.0 if met else 0.0
        else:
            current, met = gates_ratio, gates_ratio >= 1.0
        slos.append({**s, "current": current, "met": bool(met)})
    overall = "operational" if all(x["met"] for x in slos) else ("degraded" if l1_ok else "outage")
    day_ago = now - timedelta(days=1)
    return {
        "status": overall, "generated_at": now.isoformat(), "release": _release(engine),
        "api": {"up": True}, "freshness": fresh, "gates": g, "slos": slos, "dr": drill,
        "last_24h": {"audit": _audit_counts(engine, day_ago), "fleet_runs": _fleet_runs(engine, day_ago)},
        "links": {"evals": "/evals", "metrics": "/metrics", "upptime": "status/.upptimerc.yml", "dr_drill": ".github/workflows/dr_drill.yml"},
    }


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def prometheus(engine: Engine, *, release: str | None = None, now: datetime | None = None) -> str:
    """Prometheus text exposition format 0.0.4 — no client library needed."""
    st = status(engine, release=release, now=now)
    lines: list[str] = []

    def metric(name: str, help_: str, type_: str, samples: list[tuple[dict, float | int]]):
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {type_}")
        for labels, value in samples:
            lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
            lines.append(f"{name}{{{lab}}} {value}" if lab else f"{name} {value}")

    metric("clhear_up", "1 when the API answered this scrape", "gauge", [({}, 1)])
    metric("clhear_status", "overall status: 0 outage, 1 degraded, 2 operational", "gauge",
           [({}, {"outage": 0, "degraded": 1, "operational": 2}[st["status"]])])
    rel = st["release"]["id"] or "live"
    metric("clhear_release_info", "current release", "gauge", [({"release": rel}, 1)])
    metric("clhear_layer_freshness_seconds", "seconds since the layer's newest row (-1 when the layer is empty)", "gauge",
           [({"layer": k}, -1 if v["age_seconds"] is None else v["age_seconds"]) for k, v in st["freshness"].items()])
    metric("clhear_layer_freshness_slo_seconds", "freshness SLO per layer", "gauge",
           [({"layer": k}, v["slo_seconds"]) for k, v in st["freshness"].items()])
    metric("clhear_layer_fresh", "1 when the layer is within its freshness SLO", "gauge",
           [({"layer": k}, 1 if v["ok"] else 0) for k, v in st["freshness"].items()])
    metric("clhear_gate_passed", "1 when every suite in the layer's gate passed on the current release", "gauge",
           [({"layer": k}, 1 if v["passed"] else 0) for k, v in st["gates"].items()])
    metric("clhear_gate_suite_passed", "per-suite gate result", "gauge",
           [({"layer": k, "suite": s}, 1 if ok else 0) for k, v in st["gates"].items() for s, ok in v["suites"].items()])
    metric("clhear_slo_met", "1 when the SLO is currently met", "gauge", [({"slo": s["name"]}, 1 if s["met"] else 0) for s in st["slos"]])
    metric("clhear_audit_entries_24h", "audit log entries in the last 24 h by action", "gauge",
           [({"action": a}, n) for a, n in sorted(st["last_24h"]["audit"].items())])
    metric("clhear_fleet_runs_24h", "fleet runs in the last 24 h", "gauge",
           [({"fleet": f}, n) for f, n in sorted(st["last_24h"]["fleet_runs"].items())])
    dr = st["dr"]
    metric("clhear_dr_drill_passed", "1 when the latest restore drill passed", "gauge", [({}, 1 if dr["passed"] else 0)])
    metric("clhear_dr_drill_age_seconds", "seconds since the latest restore drill finished (-1 when never drilled)", "gauge",
           [({}, -1 if dr["age_seconds"] is None else dr["age_seconds"])])
    metric("clhear_dr_rpo_seconds", "recovery point measured by the latest drill (-1 when unknown)", "gauge",
           [({}, -1 if dr["rpo_seconds"] is None else dr["rpo_seconds"])])
    metric("clhear_dr_rto_seconds", "recovery time measured by the latest drill (-1 when unknown)", "gauge",
           [({}, -1 if dr["rto_seconds"] is None else dr["rto_seconds"])])
    return "\n".join(lines) + "\n"
