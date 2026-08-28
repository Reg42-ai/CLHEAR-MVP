"""Layer registry service: per-layer stats, demo items, and lineage resolution.

The lineage resolver is the "reasoning" backbone of the Stack UI: any item in
any layer can be walked down the derivation chain until it bottoms out in real
L1 clauses (verbatim text via the clauses_public discipline — restricted
sources resolve to refs + hashes only, never text).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.demo import demo_counts, load_layer_items
from app.clhear.l1.models import change_events, clauses, source_families, source_versions, sources
from app.clhear.layers import LAYER_CATALOG, LAYER_ORDER, demo_banner, layer_public_meta
from app.clhear.models import events, llm_calls, proposals, runs

# ------------------------------------------------------------------ registry


def _count(conn, table, *where) -> int:
    stmt = sa.select(sa.func.count()).select_from(table)
    for clause in where:
        stmt = stmt.where(clause)
    return int(conn.execute(stmt).scalar() or 0)


def layer_counts(engine: Engine) -> dict[str, dict]:
    """Live counts for L0/L1; demo item counts for L2-L8."""
    out: dict[str, dict] = {}
    with engine.connect() as conn:
        out["L0"] = {
            "events": _count(conn, events),
            "runs": _count(conn, runs),
            "proposals": _count(conn, proposals),
            "llm_calls": _count(conn, llm_calls),
        }
        ingested = conn.execute(
            sa.select(sa.func.count(sa.func.distinct(source_versions.c.source_id))).where(
                source_versions.c.status == "in_force"
            )
        ).scalar()
        out["L1"] = {
            "families": _count(conn, source_families),
            "sources": _count(conn, sources),
            "sources_ingested": int(ingested or 0),
            "clauses": _count(conn, clauses),
            "clauses_public": _count(conn, clauses, clauses.c.public_ok.is_(True)),
            "change_events": _count(conn, change_events),
        }
    demo = demo_counts()
    out["L2"] = {"obligations": demo.get("L2", 0)}
    out["L3"] = {"building_blocks": demo.get("L3", 0)}
    out["L4"] = {"profiles": demo.get("L4", 0)}
    out["L5"] = {"activities": demo.get("L5", 0)}
    out["L6"] = {"programs": demo.get("L6", 0)}
    out["L7"] = {"risk_scores": demo.get("L7", 0)}
    out["L8"] = {"benchmark_definitions": demo.get("L8", 0), "aggregates_published": 0}
    return out


def layer_index(engine: Engine) -> list[dict]:
    counts = layer_counts(engine)
    items = []
    for code in LAYER_ORDER:
        entry = layer_public_meta(code)
        entry["counts"] = counts.get(code, {})
        if LAYER_CATALOG[code]["status"] == "demo":
            entry["banner"] = demo_banner(code)
        items.append(entry)
    return items


# ----------------------------------------------------------- clause resolver


def resolve_clause(engine: Engine, source_key: str, ref: str) -> dict:
    """Resolve (source_key, ref) to the real L1 clause in the current corpus.

    Open sources return verbatim text; restricted sources return refs/hashes
    only (clauses_public discipline). Unresolvable refs degrade gracefully —
    the corpus snapshot may not contain every cited instrument.
    """
    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == source_key)).first()
        if source is None:
            return {"resolved": False, "source_key": source_key, "ref": ref, "note": "source not in this corpus snapshot"}
        version = conn.execute(
            sa.select(source_versions)
            .where(source_versions.c.source_id == source.id)
            .where(source_versions.c.status == "in_force")
            .order_by(source_versions.c.id.desc())
            .limit(1)
        ).first()
        base = {
            "resolved": False,
            "source_key": source_key,
            "ref": ref,
            "source_name": source.name,
            "short_name": source.short_name,
            "license": source.license,
            "locked": source.license != "open",
        }
        if version is None:
            base["note"] = "restricted: refs only (no ingested text)" if source.license != "open" else "no ingested version in this corpus snapshot"
            return base
        clause = conn.execute(
            sa.select(clauses)
            .where(clauses.c.source_version_id == version.id)
            .where(clauses.c.ref == ref)
            .limit(1)
        ).first()
        if clause is None:
            base["version_label"] = version.version_label
            base["note"] = "ref not present in the ingested version"
            return base
        public = bool(clause.public_ok) and source.license == "open"
        return {
            **base,
            "resolved": True,
            "clause_id": clause.id,
            "doc_node_id": clause.doc_node_id,
            "path": clause.path,
            "text": clause.text if public else None,
            "text_hash": clause.text_hash,
            "version_label": version.version_label,
            "as_of_date": str(version.as_of_date) if version.as_of_date else None,
            "retrieved_at": str(version.retrieved_at),
            "content_hash": version.content_hash,
            "s3_uri": version.s3_uri,
            "permalink": f"/sources?source={source_key}&node={clause.doc_node_id}" if clause.doc_node_id else f"/sources?source={source_key}",
        }


def churn_inputs(engine: Engine, source_keys: list[str], window_days: int = 365) -> dict:
    """Live L1 change velocity for the given sources — an L7 scoring input."""
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(sources.c.key, change_events.c.detected_at, change_events.c.clause_refs)
            .join(sources, sources.c.id == change_events.c.source_id)
            .where(sources.c.key.in_(source_keys))
        ).all()
    recent = 0
    changed_clauses = 0
    for row in rows:
        detected = row.detected_at
        if isinstance(detected, str):
            try:
                detected = datetime.fromisoformat(detected)
            except ValueError:
                detected = None
        if detected is not None and detected.tzinfo is None:
            detected = detected.replace(tzinfo=timezone.utc)
        if detected is None or detected >= since:
            recent += 1
            refs = row.clause_refs if isinstance(row.clause_refs, list) else json.loads(row.clause_refs or "[]")
            changed_clauses += len(refs)
    return {
        "watch_sources": source_keys,
        "window_days": window_days,
        "change_events": recent,
        "changed_clauses": changed_clauses,
        "computed_live": True,
    }


def risk_score(inputs: dict, churn: dict) -> dict:
    """Versioned demo formula (v1): published with its inputs, per the L7 contract."""
    deficit = max(0.0, 1.0 - float(inputs.get("coverage_ratio", 0.0)))
    total = len(inputs.get("obligations", [])) or 1
    open_ratio = min(1.0, float(inputs.get("open_obligations", 0)) / total)
    churn_pressure = min(1.0, churn.get("change_events", 0) / 10.0)
    score = round(100 * (deficit * 0.6 + churn_pressure * 0.3 + open_ratio * 0.1), 1)
    band = "low" if score < 15 else "elevated" if score < 40 else "high"
    return {
        "score": score,
        "band": band,
        "formula": "100 x (coverage_deficit x 0.6 + churn_pressure x 0.3 + open_ratio x 0.1)",
        "formula_version": "demo-v1",
        "components": {
            "coverage_deficit": round(deficit, 3),
            "churn_pressure": round(churn_pressure, 3),
            "open_ratio": round(open_ratio, 3),
        },
    }


# -------------------------------------------------------------------- items


def _index_items(layer: str) -> dict[str, dict]:
    return {item["id"]: item for item in load_layer_items(layer)}


def layer_items(engine: Engine, layer: str) -> list[dict]:
    items = [dict(item) for item in load_layer_items(layer)]
    if layer == "L7":
        for item in items:
            churn = churn_inputs(engine, item.get("watch_sources", []))
            score_inputs = {**item.get("inputs", {}), "obligations": item.get("obligations", [])}
            item["live_inputs"] = churn
            item["result"] = risk_score(score_inputs, churn)
    if layer == "L6":
        for item in items:
            states = [c["state"] for c in item.get("coverage", [])]
            item["coverage_summary"] = {
                "covered": states.count("covered"),
                "partial": states.count("partial"),
                "gaps": states.count("gap"),
                "total": len(states),
            }
    return items


# ------------------------------------------------------------------ lineage


def _node(layer: str, kind: str, item_id: str, title: str, detail: str = "", meta: dict | None = None, children: list | None = None) -> dict:
    return {
        "layer": layer,
        "kind": kind,
        "id": item_id,
        "title": title,
        "detail": detail,
        "meta": meta or {},
        "children": children or [],
    }


def _clause_leaf(engine: Engine, basis: dict) -> dict:
    resolved = resolve_clause(engine, basis["source_key"], basis["ref"])
    title = f"{resolved.get('short_name') or basis['source_key']} · {basis['ref']}"
    detail = basis.get("role", "")
    return _node("L1", "clause", f"{basis['source_key']}#{basis['ref']}", title, detail, meta=resolved)


def _obligation_node(engine: Engine, obligation: dict, deep: bool = True) -> dict:
    children = [_clause_leaf(engine, b) for b in obligation.get("basis", [])] if deep else []
    return _node(
        "L2",
        "obligation",
        obligation["id"],
        obligation["title"],
        obligation.get("summary") or "(restricted: summary withheld)",
        meta={"derivation": obligation.get("derivation"), "jurisdiction": obligation.get("jurisdiction"), "theme": obligation.get("theme"), "restricted": obligation.get("restricted", False)},
        children=children,
    )


def _block_node(engine: Engine, block: dict, obligations_by_id: dict[str, dict], deep: bool = True) -> dict:
    children = [
        _obligation_node(engine, obligations_by_id[oid], deep=deep)
        for oid in block.get("satisfies", [])
        if oid in obligations_by_id
    ]
    for control in block.get("implements_controls", []):
        leaf = _clause_leaf(engine, {**control, "role": "implements control"})
        children.append(leaf)
    return _node(
        "L3",
        "building_block",
        block["id"],
        block["name"],
        block.get("description", ""),
        meta={"capability": block.get("capability"), "derivation": block.get("derivation")},
        children=children,
    )


def lineage(engine: Engine, layer: str, item_id: str) -> dict:
    """Walk one item's derivation chain down to real L1 clauses."""
    items = _index_items(layer)
    item = items.get(item_id)
    if item is None:
        raise KeyError(item_id)
    obligations = _index_items("L2")
    blocks = _index_items("L3")
    activities = _index_items("L5")
    programs = _index_items("L6")

    if layer == "L2":
        return _obligation_node(engine, item)

    if layer == "L3":
        return _block_node(engine, item, obligations)

    if layer == "L4":
        children = []
        for act_id in item.get("activities", []):
            act = activities.get(act_id)
            if not act:
                continue
            act_children = [
                _obligation_node(engine, obligations[t["obligation"]], deep=True)
                for t in act.get("triggers", [])
                if t["obligation"] in obligations
            ]
            children.append(
                _node("L5", "activity", act["id"], act["name"], act.get("description", ""), children=act_children)
            )
        return _node("L4", "profile", item["id"], item["name"], item.get("description", ""),
                     meta={"attributes": item.get("attributes"), "derivation": item.get("derivation")}, children=children)

    if layer == "L5":
        children = []
        for trigger in item.get("triggers", []):
            oid = trigger["obligation"]
            if oid in obligations:
                node = _obligation_node(engine, obligations[oid])
                node["detail"] = f"triggered when: {trigger.get('condition', 'always')}"
                children.append(node)
        return _node("L5", "activity", item["id"], item["name"], item.get("description", ""),
                     meta={"derivation": item.get("derivation")}, children=children)

    if layer == "L6":
        children = [
            _block_node(engine, blocks[bid], obligations, deep=True)
            for bid in item.get("blocks", [])
            if bid in blocks
        ]
        return _node("L6", "program", item["id"], item["name"], item.get("description", ""),
                     meta={"profile": item.get("profile"), "coverage": item.get("coverage"), "derivation": item.get("derivation")},
                     children=children)

    if layer == "L7":
        churn = churn_inputs(engine, item.get("watch_sources", []))
        score = risk_score({**item.get("inputs", {}), "obligations": item.get("obligations", [])}, churn)
        children = [
            _node("L1", "live_input", "churn", "Live regulatory churn (L1 change events)",
                  f"{churn['change_events']} change event(s), {churn['changed_clauses']} clause(s) touched in {churn['window_days']}d",
                  meta=churn),
        ]
        program = programs.get(item.get("program", ""))
        if program:
            children.append(
                _node("L6", "program", program["id"], program["name"], "coverage input", meta={"coverage": program.get("coverage")})
            )
        for oid in item.get("obligations", []):
            if oid in obligations:
                children.append(_obligation_node(engine, obligations[oid]))
        return _node("L7", "risk_score", item["id"], f"{item['area']} — {score['score']} ({score['band']})",
                     item["derivation"]["method"], meta={"result": score, "inputs": item.get("inputs"), "derivation": item.get("derivation")},
                     children=children)

    if layer == "L8":
        return _node("L8", "benchmark", item["id"], item["name"], item.get("definition", ""),
                     meta={"locked": True, "k_anonymity": item.get("k_anonymity"), "cluster": item.get("cluster"),
                           "note": "Closed by design: raw peer data never leaves the enclave; no aggregates are published today."})

    raise KeyError(layer)
