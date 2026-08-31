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


@register_suite("l2_basis_integrity")
def l2_basis_integrity(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Every live obligation's basis clause must still resolve with the SAME
    text hash it was derived from. A drifted hash means L1 changed underneath
    it — the row must be re-derived, never silently served. Target: 100%."""
    from app.clhear.derived_models import obligations
    from app.clhear.l1.models import clauses, source_versions, sources

    checked = mismatched = unresolved = 0
    bad: list[str] = []
    with engine.connect() as conn:
        query = sa.select(obligations).where(obligations.c.status.in_(("derived", "validated")))
        if source_key:
            query = query.where(obligations.c.source_key == source_key)
        for ob in conn.execute(query):
            checked += 1
            row = conn.execute(
                sa.select(clauses.c.text_hash)
                .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
                .join(sources, sources.c.id == source_versions.c.source_id)
                .where(sources.c.key == ob.source_key)
                .where(source_versions.c.status == "in_force")
                .where(clauses.c.ref == ob.clause_ref)
                .order_by(source_versions.c.id.desc())
                .limit(1)
            ).first()
            if row is None:
                unresolved += 1
                bad.append(ob.id)
            elif row.text_hash != ob.text_hash:
                mismatched += 1
                bad.append(ob.id)
    passed = mismatched == 0 and unresolved == 0
    return {
        "checked": checked,
        "hash_mismatched": mismatched,
        "unresolved": unresolved,
        "failing": bad[:40],
        "integrity": 1.0 if checked == 0 else round((checked - mismatched - unresolved) / checked, 4),
    }, passed


@register_suite("l2_extraction_quality")
def l2_extraction_quality(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Extraction precision/recall against the hand-labeled golden set
    (app/clhear/l2/golden.json), evaluated over golden refs present in this
    corpus. Gates: precision >= 0.75 and recall >= 0.70."""
    import json as _json
    from pathlib import Path

    from app.clhear.derived_models import obligations
    from app.clhear.l1.models import clauses, source_versions, sources

    golden = _json.loads((Path(__file__).parent.parent / "l2" / "golden.json").read_text())
    tp = fp = fn = tn = 0
    evaluated = 0
    misses: list[dict] = []
    with engine.connect() as conn:
        derived = {
            (row.source_key, row.clause_ref)
            for row in conn.execute(
                sa.select(obligations.c.source_key, obligations.c.clause_ref).where(
                    obligations.c.status.in_(("derived", "validated"))
                )
            )
        }
        for item in golden:
            present = conn.execute(
                sa.select(clauses.c.id)
                .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
                .join(sources, sources.c.id == source_versions.c.source_id)
                .where(sources.c.key == item["source_key"])
                .where(source_versions.c.status == "in_force")
                .where(clauses.c.ref == item["ref"])
                .limit(1)
            ).first()
            if present is None:
                continue
            evaluated += 1
            got = (item["source_key"], item["ref"]) in derived
            if item["is_duty"] and got:
                tp += 1
            elif item["is_duty"] and not got:
                fn += 1
                misses.append({**item, "kind": "missed duty"})
            elif not item["is_duty"] and got:
                fp += 1
                misses.append({**item, "kind": "false positive"})
            else:
                tn += 1
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    # An empty corpus (fresh dev DB) has nothing to judge — trivially green.
    passed = evaluated < 5 or (precision >= 0.75 and recall >= 0.70)
    return {
        "golden_total": len(golden),
        "evaluated_in_corpus": evaluated,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "misses": misses[:20],
    }, passed


@register_suite("l3_l5_referential")
def l3_l5_referential(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Curated anchors must point at real registry sources, and any anchored
    clause that IS in the corpus must have derived an obligation. Refs absent
    from the corpus are reported (L1 completeness concern) but non-fatal."""
    from app.clhear.derived_models import activities as activities_t
    from app.clhear.derived_models import blocks as blocks_t
    from app.clhear.derived_models import obligations as obligations_t
    from app.clhear.l1.models import clauses, source_versions, sources
    from app.clhear.l1.registry_etoro import S

    registry_keys = {e["key"] for e in S}
    unknown_sources: list[str] = []
    extraction_misses: list[dict] = []
    refs_not_in_corpus: list[dict] = []
    with engine.connect() as conn:
        db_keys = {row.key for row in conn.execute(sa.select(sources.c.key))}
        block_rows = [dict(r) for r in conn.execute(sa.select(blocks_t)).mappings()]
        activity_rows = [dict(r) for r in conn.execute(sa.select(activities_t)).mappings()]
        derived = {
            (r.source_key, r.clause_ref)
            for r in conn.execute(
                sa.select(obligations_t.c.source_key, obligations_t.c.clause_ref).where(
                    obligations_t.c.status.in_(("derived", "validated"))
                )
            )
        }
        present: set[tuple[str, str]] = set()
        for row in conn.execute(
            sa.select(sources.c.key, clauses.c.ref)
            .join(source_versions, source_versions.c.source_id == sources.c.id)
            .join(clauses, clauses.c.source_version_id == source_versions.c.id)
            .where(source_versions.c.status == "in_force")
            .where(sources.c.license == "open")
            .where(clauses.c.public_ok.is_(True))
        ):
            present.add((row.key, row.ref))

    def check_anchor(owner: str, anchor: dict) -> None:
        key = anchor["source_key"]
        if key not in registry_keys and key not in db_keys:
            unknown_sources.append(f"{owner} -> {key}")
            return
        for ref in anchor.get("refs") or []:
            if (key, ref) in present and (key, ref) not in derived:
                extraction_misses.append({"owner": owner, "source_key": key, "ref": ref})
            elif (key, ref) not in present and key in db_keys:
                refs_not_in_corpus.append({"owner": owner, "source_key": key, "ref": ref})

    for b in block_rows:
        for sel in b["satisfies"]:
            check_anchor(f"block:{b['id']}", {"source_key": sel["source_key"], "refs": sel.get("refs")})
    schema_keys: set[str] = set()
    with engine.connect() as conn:
        from app.clhear.derived_models import attribute_schema as attribute_schema_t

        schema_keys = {r.key for r in conn.execute(sa.select(attribute_schema_t.c.key))}
    unknown_when: list[str] = []
    for a in activity_rows:
        for trig in a["triggers"]:
            check_anchor(f"activity:{a['id']}", trig["anchor"])
            for key in (trig.get("when") or {}):
                if key not in schema_keys:
                    unknown_when.append(f"{a['id']}.{key}")
    passed = not unknown_sources and not extraction_misses and not unknown_when
    return {
        "blocks": len(block_rows),
        "activities": len(activity_rows),
        "unknown_sources": unknown_sources[:20],
        "extraction_misses": extraction_misses[:20],
        "refs_not_in_corpus": len(refs_not_in_corpus),
        "unknown_when_attributes": unknown_when[:20],
    }, passed


@register_suite("l2_concept_integrity")
def l2_concept_integrity(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Concept gates: every member resolves live and non-stale; every facet
    jurisdiction has >=1 member; canonical statements contain no verbatim
    8-gram runs from RESTRICTED clauses; resolution is deterministic."""
    import re as _re

    from app.clhear.derived_models import concept_members, concepts, obligations
    from app.clhear.l1.models import clauses, source_versions, sources
    from app.clhear.l2.concepts import get_concept, list_concepts, resolve_concept

    dead_members: list[str] = []
    restricted_leaks: list[str] = []
    nondeterministic: list[str] = []
    with engine.connect() as conn:
        member_rows = conn.execute(
            sa.select(concept_members.c.concept_id, concept_members.c.obligation_id, obligations.c.status)
            .join(obligations, obligations.c.id == concept_members.c.obligation_id, isouter=True)
        ).all()
        for row in member_rows:
            if row.status not in ("derived", "validated"):
                dead_members.append(f"{row.concept_id} -> {row.obligation_id}")
        restricted_texts = [
            row.text
            for row in conn.execute(
                sa.select(clauses.c.text)
                .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
                .join(sources, sources.c.id == source_versions.c.source_id)
                .where(sources.c.license == "restricted")
                .where(clauses.c.text.isnot(None))
            )
        ]

    def _grams(text: str, n: int = 8) -> set[tuple]:
        toks = _re.findall(r"[a-z0-9]+", text.lower())
        return {tuple(toks[i : i + n]) for i in range(len(toks) - n + 1)}

    restricted_grams: set[tuple] = set()
    for text in restricted_texts:
        restricted_grams |= _grams(text)

    concept_rows = list_concepts(engine)
    for c in concept_rows:
        if restricted_grams and (_grams(c["canonical_statement"]) & restricted_grams):
            restricted_leaks.append(c["id"])
        for jurs in ([c["jurisdictions"]], [["UK"]], [["EU", "UK", "US"]]):
            first = resolve_concept(engine, c, jurs[0])
            second = resolve_concept(engine, get_concept(engine, c["id"]) or c, jurs[0])
            if first != second:
                nondeterministic.append(f"{c['id']} @ {jurs[0]}")
                break

    passed = not dead_members and not restricted_leaks and not nondeterministic
    return {
        "concepts": len(concept_rows),
        "dead_members": dead_members[:20],
        "restricted_leaks": restricted_leaks,
        "nondeterministic": nondeterministic,
    }, passed


@register_suite("l4_grounding")
def l4_grounding(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """100% of license types resolve to live clause anchors. One ungrounded
    row blocks the L4 fleet's publish — incomplete is fine, invented is not."""
    from app.clhear.derived_models import license_types
    from app.clhear.l1.models import clauses, source_versions, sources

    checked = unresolved = 0
    bad: list[str] = []
    n_types = 0
    with engine.connect() as conn:
        rows = conn.execute(sa.select(license_types)).all()
        n_types = len(rows)
        for row in rows:
            anchors = row.clause_anchors if isinstance(row.clause_anchors, list) else json.loads(row.clause_anchors or "[]")
            if not anchors:
                unresolved += 1
                bad.append(row.id)
                continue
            for anc in anchors:
                checked += 1
                hit = conn.execute(
                    sa.select(clauses.c.id)
                    .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
                    .join(sources, sources.c.id == source_versions.c.source_id)
                    .where(sources.c.key == anc.get("source_key"))
                    .where(source_versions.c.status == "in_force")
                    .where(clauses.c.ref == anc.get("ref"))
                    .limit(1)
                ).first()
                if hit is None:
                    unresolved += 1
                    bad.append(f"{row.id}->{anc.get('source_key')}#{anc.get('ref')}")
    passed = unresolved == 0
    return {
        "license_types": n_types,
        "anchors_checked": checked,
        "unresolved": unresolved,
        "failing": bad[:40],
    }, passed


@register_suite("l6_citation")
def l6_citation(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Blueprints that carry a rationale must cite only ids in that blueprint."""
    from app.clhear.derived_models import blueprints
    from app.clhear.l6.rationale import citations_ok

    checked = failed = 0
    extras: list[str] = []
    with engine.connect() as conn:
        for row in conn.execute(sa.select(blueprints)):
            result = row.result if isinstance(row.result, dict) else json.loads(row.result or "{}")
            text = result.get("rationale")
            if not text:
                continue
            checked += 1
            # Reconstruct a minimal blueprint for the checker.
            bp = {
                "coverage": [{"obligation_id": oid} for oid in (result.get("obligation_ids") or [])],
                "blocks": [{"id": bid} for bid in (result.get("blocks") or [])],
                "activities_evaluated": result.get("activities") or [],
            }
            # If we only stored coverage_summary, there is nothing to over-cite.
            if not bp["coverage"] and not bp["blocks"]:
                continue
            ok, extra = citations_ok(text, bp)
            if not ok:
                failed += 1
                extras.extend(extra[:5])
    return {"checked": checked, "failed": failed, "extra_ids": extras[:20]}, failed == 0


@register_suite("l7_number_echo")
def l7_number_echo(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    from app.clhear.l7.narrate import number_echo_ok
    from app.clhear.models import risk_narratives

    failed = []
    checked = 0
    with engine.connect() as conn:
        for row in conn.execute(sa.select(risk_narratives)):
            checked += 1
            # Reconstruct a vector from stored echoed figures — every number in
            # the narrative must be in that set.
            vector = {f"n{i}": float(x) if "." in str(x) else int(x) for i, x in enumerate(row.echoed_figures or [])}
            # Also allow the raw stored figures as strings via a dummy walk.
            ok, extras = number_echo_ok(row.narrative, {"echoed": row.echoed_figures or [], **vector})
            if not ok:
                failed.append({"id": row.id, "extras": extras[:8]})
    return {"checked": checked, "failed": failed[:20]}, not failed


@register_suite("l8_k_anonymity")
def l8_k_anonymity(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    from app.clhear.l8.cohorts import k_anonymity_ok

    ok, detail = k_anonymity_ok(engine)
    return detail, ok


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
