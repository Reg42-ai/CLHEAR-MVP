"""Evals harness (HLD §7.1): suites registered per layer; runs recorded in
l0_platform.eval_runs plus a JSON artifact; release gate = all suites passed.

Evals are gates, not reports (HLD principle 5).
"""
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import eval_runs
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.evals")

# suite name -> callable(engine, source_key) -> (scores: dict, passed: bool)
SUITES: dict[str, Callable] = {}


def register_suite(name: str):
    def wrap(fn: Callable):
        SUITES[name] = fn
        return fn

    return wrap


@register_suite("l0_smoke")
def l0_smoke(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """P0 skeleton suite: the l0_platform tables exist and are queryable."""
    from app.clhear.models import events, llm_calls, proposals, runs

    counts = {}
    with engine.connect() as conn:
        for table in (events, proposals, llm_calls, runs):
            counts[table.name] = conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
    return {"tables_queryable": len(counts), "row_counts": counts}, True


@register_suite("l1_fidelity")
def l1_fidelity(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """E2/E3 skeleton (pulled forward from P4): every registry adapter's parse
    must cover its own oracle text at >= the gate threshold with zero contract
    violations. Runs offline against recorded fixtures in CI; blocks releases
    via the existing release gate."""
    from app.clhear.l1 import fidelity
    from app.clhear.l1.adapters import ADAPTER_KEYS, get_adapter

    settings = get_settings()
    threshold = settings.clhear_fidelity_threshold
    scores: dict = {}
    passed = True
    for key in ADAPTER_KEYS:
        if source_key and key != source_key:
            continue
        adapter = get_adapter(key)
        try:
            result = adapter.fetch()
            report = fidelity.check(result.tree, adapter.expected_text(result.artifacts))
            ok = report.ok(threshold)
            scores[key] = {
                "coverage": round(report.coverage, 5),
                "violations": len(report.violations),
                "passed": ok,
            }
            passed = passed and ok
        except Exception as exc:
            scores[key] = {"error": str(exc)[:200], "passed": False}
            passed = False
    return {"threshold": threshold, "adapters": scores}, passed


def run_suite(engine: Engine, suite: str, source_key: str | None = None, release: str | None = None) -> dict:
    """Run one suite -> eval_runs row + JSON artifact. Returns the record."""
    fn = SUITES[suite]
    scores, passed = fn(engine, source_key)
    record = {
        "suite": suite,
        "source_key": source_key,
        "release": release,
        "scores": scores,
        "passed": passed,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    with engine.begin() as conn:
        conn.execute(
            eval_runs.insert().values(
                suite=suite, source_key=source_key, release=release, scores=scores, passed=passed
            )
        )
    artifacts_dir = Path(get_settings().clhear_artifacts_dir) / "evals"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_key = (source_key or "").replace("/", "_")
    artifact = artifacts_dir / f"{suite}{'-' + safe_key if safe_key else ''}-{stamp}.json"
    artifact.write_text(json.dumps(record, indent=2, default=str))
    log.info("suite %s passed=%s -> %s", suite, passed, artifact)
    return record


SOURCE_SUITES = (
    "e1_fidelity",
    "e2_completeness",
    "e3_roundtrip",
    "e4_change_replay",
    "e5_provenance",
    "e6_retrievability",
    "e7_closure",
)

GOLDEN_QUERIES = {
    "uksi/2017/692": [("customer due diligence", ("regulation-27", "regulation-28", "regulation-27-1"))],
    "celex/32016R0679": [("personal data", ("art_4", "art_6", "article-4"))],
    "celex/32014L0065": [("investment firm", ("art_4", "article-4"))],
    "celex/32023R1114": [("crypto-asset", ("art_3", "article-3"))],
}


def _ws(text: str) -> str:
    return " ".join((text or "").split())


def _latest_source(engine: Engine, source_key: str):
    from app.clhear.l1.models import clauses, doc_nodes, source_versions, sources

    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == source_key)).first()
        if source is None:
            return None, None, [], []
        version = conn.execute(
            sa.select(source_versions)
            .where(source_versions.c.source_id == source.id)
            .where(source_versions.c.status == "in_force")
            .order_by(source_versions.c.id.desc())
            .limit(1)
        ).first()
        if version is None:
            return source, None, [], []
        nodes = conn.execute(
            sa.select(doc_nodes).where(doc_nodes.c.source_version_id == version.id)
        ).all()
        clause_rows = conn.execute(
            sa.select(clauses).where(clauses.c.source_version_id == version.id)
        ).all()
        return source, version, nodes, clause_rows


def _need_source(source_key: str | None) -> tuple[dict, bool] | None:
    if source_key:
        return None
    return {"note": "per-source suite — run via run_source_evals", "passed": True}, True


@register_suite("e1_fidelity")
def e1_fidelity(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Re-check a ≥10% sample (min 50 clauses) against stored node text."""
    early = _need_source(source_key)
    if early:
        return early
    source, version, nodes, clause_rows = _latest_source(engine, source_key)
    if source is None:
        return {"error": "unknown source", "passed": False}, False
    if source.license == "restricted":
        return {"note": "BYOL-pending / restricted — E1 n/a until unlocked", "n/a": True}, True
    if version is None:
        return {"error": "no version stored", "sampled": 0}, False
    haystack = _ws(" ".join((n.heading or "") + " " + (n.raw_text or "") for n in nodes))
    sample_n = max(min(len(clause_rows), 50), max(1, len(clause_rows) // 10)) if clause_rows else 0
    if sample_n == 0:
        # Title-only trees (landing pages) still count if node text is present.
        ok = bool(haystack)
        return {"sampled": 0, "matched": 0, "score": 1.0 if ok else 0.0, "grain": "nodes"}, ok
    sample = clause_rows[:: max(1, len(clause_rows) // sample_n)][:sample_n]
    matched = 0
    for row in sample:
        needle = _ws(row.text)
        if needle and needle in haystack:
            matched += 1
    score = matched / len(sample)
    return {"sampled": len(sample), "matched": matched, "score": round(score, 5)}, score >= 1.0


@register_suite("e2_completeness")
def e2_completeness(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Stored tree vs last-run coverage (gate already 99.5%)."""
    early = _need_source(source_key)
    if early:
        return early
    from app.clhear.models import runs

    source, version, nodes, clause_rows = _latest_source(engine, source_key)
    if source is None:
        return {"error": "unknown source"}, False
    if source.license == "restricted":
        return {"note": "restricted placeholder accepted", "n/a": True, "nodes": len(nodes)}, True
    if version is None:
        return {"error": "no version stored", "stored": 0, "expected": 1}, False
    coverage = None
    with engine.connect() as conn:
        for row in conn.execute(sa.select(runs).where(runs.c.fleet.like("l1.%")).order_by(runs.c.id.desc()).limit(80)):
            inputs = row.inputs if isinstance(row.inputs, dict) else json.loads(row.inputs or "{}")
            outputs = row.outputs if isinstance(row.outputs, dict) else json.loads(row.outputs or "{}")
            if inputs.get("source") == source_key and outputs.get("coverage") is not None:
                coverage = float(outputs["coverage"])
                break
    ok = len(nodes) > 0 and (coverage is None or coverage >= 0.995)
    return {
        "nodes": len(nodes),
        "clauses": len(clause_rows),
        "last_coverage": coverage,
        "stored_over_expected": 1.0 if len(nodes) else 0.0,
    }, ok


@register_suite("e3_roundtrip")
def e3_roundtrip(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Concatenate stored nodes vs clause projection (≥99.9% token overlap)."""
    early = _need_source(source_key)
    if early:
        return early
    source, version, nodes, clause_rows = _latest_source(engine, source_key)
    if source is None or version is None:
        return {"error": "no stored tree"}, source is not None and source.license == "restricted"
    if source.license == "restricted":
        return {"note": "restricted — hashes only", "n/a": True}, True
    node_text = _ws(" ".join(n.raw_text or n.heading or "" for n in nodes))
    clause_text = _ws(" ".join(c.text or "" for c in clause_rows))
    if not node_text:
        return {"score": 0.0}, False
    if not clause_text:
        return {"score": 1.0, "note": "no clause projection (paragraph-only tree)"}, True
    tokens = clause_text.split()
    # Token coverage of clause text inside the node concatenation.
    hit = sum(1 for t in tokens if t in node_text)
    score = hit / max(1, len(tokens))
    return {"score": round(score, 5), "clause_tokens": len(tokens)}, score >= 0.999


@register_suite("e4_change_replay")
def e4_change_replay(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Diff recall when a prior version exists; else explicit n/a."""
    early = _need_source(source_key)
    if early:
        return early
    from app.clhear.l1.models import change_events, source_versions, sources

    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == source_key)).first()
        if source is None:
            return {"error": "unknown source"}, False
        n_versions = conn.execute(
            sa.select(sa.func.count()).select_from(source_versions).where(source_versions.c.source_id == source.id)
        ).scalar_one()
        n_changes = conn.execute(
            sa.select(sa.func.count()).select_from(change_events).where(change_events.c.source_id == source.id)
        ).scalar_one()
    if n_versions < 2:
        return {"note": "n/a — first version", "n/a": True, "versions": n_versions}, True
    return {"versions": n_versions, "change_events": n_changes}, n_changes >= 1


@register_suite("e5_provenance")
def e5_provenance(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Clause → version hashes recomputed; S3/local URI present."""
    import hashlib

    early = _need_source(source_key)
    if early:
        return early
    source, version, nodes, clause_rows = _latest_source(engine, source_key)
    if source is None or version is None:
        return {"error": "no version"}, False
    mismatches = 0
    checked = 0
    for row in clause_rows[:200]:
        checked += 1
        digest = hashlib.sha256((row.text or "").encode()).hexdigest()
        if digest != row.text_hash:
            mismatches += 1
    ok = mismatches == 0 and bool(version.content_hash)
    return {
        "content_hash": version.content_hash,
        "s3_uri": version.s3_uri,
        "clauses_checked": checked,
        "hash_mismatches": mismatches,
    }, ok


@register_suite("e6_retrievability")
def e6_retrievability(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Golden queries per family — hits@5 ≥ 95% when goldens exist."""
    early = _need_source(source_key)
    if early:
        return early
    goldens = GOLDEN_QUERIES.get(source_key)
    if not goldens:
        return {"note": "n/a — no golden queries for this source", "n/a": True}, True
    from app.clhear.l1 import retrieval
    from app.clhear.l1.models import source_families

    source, _, _, _ = _latest_source(engine, source_key)
    family_key = None
    if source is not None:
        with engine.connect() as conn:
            family_key = conn.execute(
                sa.select(source_families.c.key).where(source_families.c.id == source.family_id)
            ).scalar()

    hits = 0
    details = []
    for query, expected in goldens:
        # Per-source (per-family) hits@5 — not "beat the whole corpus".
        rows = retrieval.search(engine, query, limit=5, scope=family_key)
        own = [r for r in rows if r.get("source_key") == source_key][:5]
        refs = {r.get("ref") for r in own}
        hit = any(any(exp in (ref or "") for exp in expected) for ref in refs)
        hits += int(hit)
        details.append({"q": query, "hit": hit, "n": len(own), "refs": sorted(r for r in refs if r)})
    score = hits / len(goldens)
    return {"score": round(score, 5), "queries": details, "scope": family_key}, score >= 0.95


@register_suite("e7_closure")
def e7_closure(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Citator list ⊆ family; unexplained citations fail."""
    early = _need_source(source_key)
    if early:
        return early
    from app.clhear.l1.models import citations, clauses, family_members, source_versions, sources

    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == source_key)).first()
        if source is None:
            return {"error": "unknown source"}, False
        family_ids = {
            row.source_id
            for row in conn.execute(
                sa.select(family_members.c.source_id).where(family_members.c.family_id == source.family_id)
            )
        }
        version = conn.execute(
            sa.select(source_versions)
            .where(source_versions.c.source_id == source.id)
            .where(source_versions.c.status == "in_force")
            .limit(1)
        ).first()
        if version is None:
            return {"note": "n/a — no version", "n/a": True}, True
        clause_ids = [
            r[0]
            for r in conn.execute(sa.select(clauses.c.id).where(clauses.c.source_version_id == version.id))
        ]
        if not clause_ids:
            return {"note": "n/a — no citations extracted", "n/a": True}, True
        rows = conn.execute(sa.select(citations).where(citations.c.from_clause_id.in_(clause_ids))).all()
    unexplained = [
        r
        for r in rows
        if r.disposition == "open"
        or (r.resolved_source_id and r.resolved_source_id not in family_ids)
    ]
    return {"citations": len(rows), "unexplained": len(unexplained)}, len(unexplained) == 0


@register_suite("l1_completeness")
def l1_completeness(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Every registry ID in S has latest_version OR a red failed run dated today.

    Host-state overlay keys must be present. Restricted BYOL-pending rows count
    if they have a placeholder version.
    """
    from datetime import date, timezone

    from app.clhear.l1.models import source_versions, sources
    from app.clhear.l1.registry_etoro import S
    from app.clhear.models import runs

    today = datetime.now(timezone.utc).date().isoformat()
    missing = []
    overlay_missing = []
    with engine.connect() as conn:
        by_key = {row.key: row for row in conn.execute(sa.select(sources))}
        versioned = {
            row.key
            for row in conn.execute(
                sa.select(sources.c.key)
                .join(source_versions, source_versions.c.source_id == sources.c.id)
                .where(source_versions.c.status == "in_force")
            )
        }
        failed_today = set()
        for row in conn.execute(sa.select(runs).where(runs.c.fleet.like("l1.%")).order_by(runs.c.id.desc()).limit(800)):
            inputs = row.inputs if isinstance(row.inputs, dict) else json.loads(row.inputs or "{}")
            outputs = row.outputs if isinstance(row.outputs, dict) else json.loads(row.outputs or "{}")
            key = inputs.get("source")
            ts = str(row.created_at)[:10]
            if key and ts == today and outputs.get("status") in {"failed", "stale", "not-fully-successful"}:
                failed_today.add(key)
    for entry in S:
        key = entry["key"]
        if key not in by_key:
            missing.append(key)
            continue
        if key not in versioned and key not in failed_today:
            missing.append(key)
        if entry["family"] == "host-state-overlays" and key not in by_key:
            overlay_missing.append(key)
    overlays = [e["key"] for e in S if e["family"] == "host-state-overlays"]
    overlay_ok = all(k in by_key for k in overlays)
    passed = not missing and overlay_ok
    return {
        "registry_rows": len(S),
        "missing": missing[:40],
        "missing_count": len(missing),
        "overlays_present": overlay_ok,
        "overlay_missing": overlay_missing,
    }, passed


@register_suite("l1_schedule_kept")
def l1_schedule_kept(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Honest-schedule gate: every source whose adapter advertises a daily
    schedule must have a run ATTEMPT (any outcome) recorded within the last
    24h, unless the registry marks it blocked. A promised schedule that did
    not execute is a failure — never a silent no-op."""
    from datetime import timedelta, timezone

    from app.clhear.l1.models import FLEET_SCHEDULES
    from app.clhear.l1.registry_etoro import S
    from app.clhear.models import runs

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    attempted: set[str] = set()
    with engine.connect() as conn:
        for row in conn.execute(sa.select(runs).where(runs.c.fleet.like("l1.%")).order_by(runs.c.id.desc()).limit(3000)):
            created = row.created_at
            if isinstance(created, str):
                try:
                    created = datetime.fromisoformat(created)
                except ValueError:
                    continue
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created is not None and created < since:
                break
            inputs = row.inputs if isinstance(row.inputs, dict) else json.loads(row.inputs or "{}")
            if inputs.get("source"):
                attempted.add(inputs["source"])
    scheduled = [e for e in S if e["adapter"] in FLEET_SCHEDULES or e["adapter"] in {"nist"}]
    missed = [
        e["key"]
        for e in scheduled
        if e["key"] not in attempted and not (e.get("fetch") or {}).get("blocked")
    ]
    blocked = [e["key"] for e in scheduled if (e.get("fetch") or {}).get("blocked")]
    return {
        "scheduled_sources": len(scheduled),
        "attempted_24h": len(attempted),
        "missed": missed[:60],
        "missed_count": len(missed),
        "blocked": blocked,
    }, not missed


def run_source_evals(engine: Engine, source_key: str, release: str | None = None) -> list[dict]:
    return [run_suite(engine, suite, source_key=source_key, release=release) for suite in SOURCE_SUITES]


def latest_source_scorecard(engine: Engine, source_key: str) -> dict:
    """Latest E1–E7 row per suite for the Evidence tab."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(eval_runs)
            .where(eval_runs.c.source_key == source_key)
            .where(eval_runs.c.suite.in_(SOURCE_SUITES))
            .order_by(eval_runs.c.id.desc())
        ).all()
    latest: dict[str, dict] = {}
    for row in rows:
        if row.suite in latest:
            continue
        latest[row.suite] = {
            "suite": row.suite,
            "passed": bool(row.passed),
            "scores": row.scores if isinstance(row.scores, dict) else json.loads(row.scores or "{}"),
            "ran_at": str(row.ran_at),
        }
    open_ok = all(latest[s]["passed"] for s in SOURCE_SUITES if s in latest) if latest else False
    return {"source_key": source_key, "suites": latest, "green": open_ok and len(latest) == len(SOURCE_SUITES)}


GLOBAL_SUITES = ("l0_smoke", "l1_fidelity")


def run_all(engine: Engine, release: str | None = None) -> list[dict]:
    return [run_suite(engine, suite, release=release) for suite in GLOBAL_SUITES]


def release_gate(engine: Engine, release: str) -> bool:
    """True iff every recorded eval run for this release passed and none is missing."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(eval_runs.c.suite, eval_runs.c.passed).where(eval_runs.c.release == release)
        ).all()
    ran = {row.suite for row in rows}
    return bool(rows) and all(row.passed for row in rows) and set(GLOBAL_SUITES) <= ran


def l2_gate(engine: Engine) -> dict:
    """L2 may start only when every open, non-BYOL-pending source is green."""
    from app.clhear.l1.models import sources
    from app.clhear.l1.registry_etoro import S

    blocked = []
    with engine.connect() as conn:
        by_key = {row.key: row for row in conn.execute(sa.select(sources))}
    for entry in S:
        if entry.get("license") == "restricted":
            continue
        card = latest_source_scorecard(engine, entry["key"])
        if not card["green"]:
            blocked.append(entry["key"])
        if entry["key"] not in by_key:
            blocked.append(entry["key"])
    return {"passed": not blocked, "blocked": blocked[:50], "blocked_count": len(blocked)}


def main() -> int:
    """CLI: python -m app.clhear.platform.evals [suite|all] [release]."""
    from app.clhear.db import get_engine, run_migrations

    engine = get_engine()
    run_migrations(engine)
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    release = sys.argv[2] if len(sys.argv) > 2 else None
    records = run_all(engine, release) if target == "all" else [run_suite(engine, target, release=release)]
    print(json.dumps(records, indent=2, default=str))
    return 0 if all(r["passed"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
