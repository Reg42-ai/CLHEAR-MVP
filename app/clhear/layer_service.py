"""Layer registry service over REAL data: derived L2, curated L3/L4/L5,
computed L6/L7, locked L8 — plus the lineage resolver that walks any item
down to verbatim L1 clauses (clauses_public discipline: restricted sources
resolve to refs + hashes only, never text).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.curated import load as load_curated
from app.clhear.derived_models import activities as activities_t
from app.clhear.derived_models import attribute_schema as attribute_schema_t
from app.clhear.derived_models import blocks as blocks_t
from app.clhear.derived_models import blueprints, obligations
from app.clhear.derived_models import sample_profiles as sample_profiles_t
from app.clhear.l1.models import change_events, clauses, source_families, source_versions, sources
from app.clhear.layers import LAYER_CATALOG, LAYER_ORDER, layer_public_meta, status_banner
from app.clhear.models import events, llm_calls, proposals, runs

# ------------------------------------------------------------------ registry


def _count(conn, table, *where) -> int:
    stmt = sa.select(sa.func.count()).select_from(table)
    for clause in where:
        stmt = stmt.where(clause)
    return int(conn.execute(stmt).scalar() or 0)


def layer_counts(engine: Engine) -> dict[str, dict]:
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
        out["L2"] = {
            "obligations": _count(conn, obligations, obligations.c.status.in_(("derived", "validated"))),
            "validated": _count(conn, obligations, obligations.c.status == "validated"),
            "derived_unreviewed": _count(conn, obligations, obligations.c.status == "derived"),
            "stale": _count(conn, obligations, obligations.c.status == "stale"),
        }
        out["L3"] = {"building_blocks": _count(conn, blocks_t)}
        out["L4"] = {
            "profile_attributes": _count(conn, attribute_schema_t),
            "sample_profiles": _count(conn, sample_profiles_t),
        }
        out["L5"] = {"activities": _count(conn, activities_t)}
        out["L6"] = {
            "sample_programs": _count(conn, sample_profiles_t),
            "blueprints_requested": _count(conn, blueprints),
        }
        out["L7"] = {"risk_areas": _count(conn, sample_profiles_t) * 2}
    out["L8"] = {"benchmark_definitions": len(load_curated("l8_benchmarks")), "aggregates_published": 0}
    return out


def layer_index(engine: Engine) -> list[dict]:
    counts = layer_counts(engine)
    items = []
    for code in LAYER_ORDER:
        entry = layer_public_meta(code)
        entry["counts"] = counts.get(code, {})
        if LAYER_CATALOG[code]["status"] != "live":
            entry["banner"] = status_banner(code)
        items.append(entry)
    return items


# ----------------------------------------------------------- clause resolver


def resolve_clause(engine: Engine, source_key: str, ref: str) -> dict:
    """Resolve (source_key, ref) to the real L1 clause in the current corpus."""
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
            base["note"] = (
                "restricted: refs only (no ingested text)"
                if source.license != "open"
                else "no ingested version in this corpus snapshot"
            )
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


def risk_score(coverage_ratio: float, open_ratio: float, churn: dict) -> dict:
    """Versioned formula: published with its inputs (L7 contract)."""
    deficit = max(0.0, 1.0 - coverage_ratio)
    churn_pressure = min(1.0, churn.get("change_events", 0) / 10.0)
    score = round(100 * (deficit * 0.6 + churn_pressure * 0.3 + min(1.0, open_ratio) * 0.1), 1)
    band = "low" if score < 15 else "elevated" if score < 40 else "high"
    return {
        "score": score,
        "band": band,
        "formula": "100 x (coverage_deficit x 0.6 + churn_pressure x 0.3 + open_ratio x 0.1)",
        "formula_version": "risk-v1",
        "components": {
            "coverage_deficit": round(deficit, 3),
            "churn_pressure": round(churn_pressure, 3),
            "open_ratio": round(min(1.0, open_ratio), 3),
        },
    }


# -------------------------------------------------------------------- items


def _obligation_dict(row) -> dict:
    d = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    d["confidence"] = float(d["confidence"])
    for k in ("derived_at", "validated_at"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    return d


def obligation_items(
    engine: Engine,
    q: str | None = None,
    source_key: str | None = None,
    status: str | None = None,
    limit: int = 60,
    offset: int = 0,
) -> dict:
    stmt = sa.select(obligations)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(sa.or_(obligations.c.title.ilike(like), obligations.c.statement.ilike(like), obligations.c.clause_ref.ilike(like)))
    if source_key:
        stmt = stmt.where(obligations.c.source_key == source_key)
    if status:
        stmt = stmt.where(obligations.c.status == status)
    else:
        stmt = stmt.where(obligations.c.status != "rejected")
    with engine.connect() as conn:
        total = int(conn.execute(sa.select(sa.func.count()).select_from(stmt.subquery())).scalar() or 0)
        rows = conn.execute(stmt.order_by(obligations.c.source_key, obligations.c.id).limit(limit).offset(offset)).all()
        per_source = [
            {"source_key": r.source_key, "count": r.n}
            for r in conn.execute(
                sa.select(obligations.c.source_key, sa.func.count().label("n"))
                .where(obligations.c.status.in_(("derived", "validated")))
                .group_by(obligations.c.source_key)
                .order_by(sa.desc("n"))
            )
        ]
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "per_source": per_source,
        "items": [_obligation_dict(r) for r in rows],
    }


def _profile_blueprint(engine: Engine, profile_row) -> dict:
    from app.clhear.l6.composer import compose

    profile = {
        "attributes": profile_row.attributes if isinstance(profile_row.attributes, dict) else json.loads(profile_row.attributes),
        "activities": profile_row.activities if isinstance(profile_row.activities, list) else json.loads(profile_row.activities),
    }
    return compose(engine, profile, requested_by="stack-ui-sample", log_request=False)


def layer_items(engine: Engine, layer: str, **filters) -> list[dict] | dict:
    if layer == "L2":
        return obligation_items(engine, **filters)
    if layer == "L3":
        with engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(sa.select(blocks_t)).mappings()]
        for b in rows:
            b["updated_at"] = str(b.get("updated_at"))
            resolved = 0
            for sel in b["satisfies"]:
                from app.clhear.l6.composer import resolve_anchor

                resolved += len(resolve_anchor(engine, {"source_key": sel["source_key"], "refs": sel.get("refs")}))
            b["satisfies_resolved_obligations"] = resolved
        return rows
    if layer == "L4":
        with engine.connect() as conn:
            schema_rows = [dict(r) for r in conn.execute(sa.select(attribute_schema_t)).mappings()]
            profile_rows = [dict(r) for r in conn.execute(sa.select(sample_profiles_t)).mappings()]
        return {"attribute_schema": schema_rows, "sample_profiles": profile_rows}
    if layer == "L5":
        with engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(sa.select(activities_t)).mappings()]
        for a in rows:
            a["updated_at"] = str(a.get("updated_at"))
        return rows
    if layer == "L6":
        with engine.connect() as conn:
            profiles = conn.execute(sa.select(sample_profiles_t)).all()
        out = []
        for p in profiles:
            bp = _profile_blueprint(engine, p)
            out.append(
                {
                    "id": f"PRG:{p.id}",
                    "name": f"Sample program — {p.name}",
                    "profile_id": p.id,
                    "status": "computed-sample",
                    "engine_version": bp["engine_version"],
                    "coverage_summary": bp["coverage_summary"],
                    "obligations_triggered": bp["obligations_triggered"],
                    "blocks": bp["blocks"],
                    "coverage": bp["coverage"][:40],
                    "unmapped_obligations": bp["unmapped_obligations"]["count"],
                }
            )
        return out
    if layer == "L7":
        return risk_items(engine)
    if layer == "L8":
        return load_curated("l8_benchmarks")
    raise KeyError(layer)


def risk_items(engine: Engine) -> list[dict]:
    """Computed risk per sample profile x theme, from live coverage + churn."""
    with engine.connect() as conn:
        profiles = conn.execute(sa.select(sample_profiles_t)).all()
    out = []
    for p in profiles:
        bp = _profile_blueprint(engine, p)
        by_theme: dict[str, list[dict]] = {}
        for cov in bp["coverage"]:
            with engine.connect() as conn:
                row = conn.execute(
                    sa.select(obligations.c.themes).where(obligations.c.id == cov["obligation_id"])
                ).first()
            themes = row.themes if row and isinstance(row.themes, list) else []
            theme = themes[0] if themes else "general"
            by_theme.setdefault(theme, []).append(cov)
        for theme, covs in sorted(by_theme.items()):
            covered = sum(1 for c in covs if c["state"] == "covered")
            src_keys = sorted({c["source_key"] for c in covs})
            churn = churn_inputs(engine, src_keys)
            unreviewed = sum(1 for c in covs if c["status"] == "derived")
            result = risk_score(covered / len(covs) if covs else 0.0, unreviewed / len(covs) if covs else 0.0, churn)
            out.append(
                {
                    "id": f"RSK:{p.id}:{theme}",
                    "profile_id": p.id,
                    "area": theme,
                    "name": f"{p.name} — {theme}",
                    "obligations": [c["obligation_id"] for c in covs],
                    "watch_sources": src_keys,
                    "inputs": {"coverage_ratio": round(covered / len(covs), 3) if covs else 0.0,
                               "obligation_count": len(covs), "derived_unreviewed": unreviewed},
                    "live_inputs": churn,
                    "result": result,
                    "status": "computed",
                }
            )
    return out


# ------------------------------------------------------------------ lineage


def _node(layer, kind, item_id, title, detail="", meta=None, children=None) -> dict:
    return {"layer": layer, "kind": kind, "id": item_id, "title": title, "detail": detail,
            "meta": meta or {}, "children": children or []}


def _clause_leaf(engine: Engine, source_key: str, ref: str, role: str = "") -> dict:
    resolved = resolve_clause(engine, source_key, ref)
    title = f"{resolved.get('short_name') or source_key} · {ref}"
    return _node("L1", "clause", f"{source_key}#{ref}", title, role, meta=resolved)


def _obligation_row(engine: Engine, obligation_id: str):
    with engine.connect() as conn:
        return conn.execute(sa.select(obligations).where(obligations.c.id == obligation_id)).first()


def _obligation_node(engine: Engine, row) -> dict:
    return _node(
        "L2", "obligation", row.id, row.title,
        row.statement or "(restricted or non-public basis: statement withheld)",
        meta={
            "status": row.status, "confidence": float(row.confidence), "modality": row.modality,
            "addressee": row.addressee, "method": row.method, "jurisdiction": row.jurisdiction,
            "derivation": {
                "status": row.status,
                "method": f"{row.method} deterministic extraction from the anchored clause",
                "confidence": float(row.confidence),
                "validated_by": row.validated_by,
            },
        },
        children=[_clause_leaf(engine, row.source_key, row.clause_ref, "basis (hash-anchored)")],
    )


def _anchor_nodes(engine: Engine, anchor: dict, detail: str = "") -> list[dict]:
    from app.clhear.l6.composer import resolve_anchor

    resolved = resolve_anchor(engine, anchor)
    nodes = []
    for ob in resolved:
        row = _obligation_row(engine, ob["id"])
        if row is not None:
            node = _obligation_node(engine, row)
            if detail:
                node["detail"] = detail
            nodes.append(node)
    if not resolved:
        for ref in anchor.get("refs", []) or ["(all)"]:
            leaf = _clause_leaf(engine, anchor["source_key"], ref, "anchor (no derived obligation)")
            nodes.append(leaf)
    return nodes


def lineage(engine: Engine, layer: str, item_id: str) -> dict:
    if layer == "L2":
        row = _obligation_row(engine, item_id)
        if row is None:
            raise KeyError(item_id)
        return _obligation_node(engine, row)

    if layer == "L3":
        with engine.connect() as conn:
            row = conn.execute(sa.select(blocks_t).where(blocks_t.c.id == item_id)).first()
        if row is None:
            raise KeyError(item_id)
        children = []
        for sel in row.satisfies:
            children.extend(_anchor_nodes(engine, {"source_key": sel["source_key"], "refs": sel.get("refs")}, "satisfied by this block"))
        for control in row.implements_controls:
            children.append(_clause_leaf(engine, control["source_key"], control["ref"], "implements control"))
        return _node("L3", "building_block", row.id, row.name, row.description,
                     meta={"status": row.status, "capability": row.capability}, children=children)

    if layer == "L5":
        with engine.connect() as conn:
            row = conn.execute(sa.select(activities_t).where(activities_t.c.id == item_id)).first()
        if row is None:
            raise KeyError(item_id)
        children = []
        for trig in row.triggers:
            cond = ", ".join(f"{k}={v}" for k, v in (trig.get("when") or {}).items()) or "always"
            children.extend(_anchor_nodes(engine, trig["anchor"], f"triggered when {cond}"))
        return _node("L5", "activity", row.id, row.name, row.description,
                     meta={"status": row.status}, children=children)

    if layer == "L4":
        with engine.connect() as conn:
            row = conn.execute(sa.select(sample_profiles_t).where(sample_profiles_t.c.id == item_id)).first()
        if row is None:
            raise KeyError(item_id)
        children = [lineage(engine, "L5", act_id) for act_id in row.activities]
        return _node("L4", "profile", row.id, row.name, row.description,
                     meta={"attributes": row.attributes, "status": row.status}, children=children)

    if layer == "L6":
        profile_id = item_id.split(":", 1)[1] if item_id.startswith("PRG:") else item_id
        with engine.connect() as conn:
            row = conn.execute(sa.select(sample_profiles_t).where(sample_profiles_t.c.id == profile_id)).first()
        if row is None:
            raise KeyError(item_id)
        bp = _profile_blueprint(engine, row)
        children = []
        with engine.connect() as conn:
            for b in bp["blocks"]:
                block_row = conn.execute(sa.select(blocks_t).where(blocks_t.c.id == b["id"])).first()
                if block_row is not None:
                    children.append(lineage(engine, "L3", block_row.id))
        gaps = [c for c in bp["coverage"] if c["state"] == "gap"]
        return _node(
            "L6", "program", item_id, f"Sample program — {row.name}",
            f"{bp['coverage_summary']['covered']}/{bp['coverage_summary']['total']} obligations covered · engine {bp['engine_version']}",
            meta={"coverage_summary": bp["coverage_summary"], "gaps": gaps[:15],
                  "unmapped_obligations": bp["unmapped_obligations"], "status": "computed-sample"},
            children=children,
        )

    if layer == "L7":
        items = {i["id"]: i for i in risk_items(engine)}
        item = items.get(item_id)
        if item is None:
            raise KeyError(item_id)
        children = [
            _node("L1", "live_input", "churn", "Live regulatory churn (L1 change events)",
                  f"{item['live_inputs']['change_events']} change event(s), {item['live_inputs']['changed_clauses']} clause(s) in {item['live_inputs']['window_days']}d",
                  meta=item["live_inputs"]),
        ]
        for oid in item["obligations"][:20]:
            row = _obligation_row(engine, oid)
            if row is not None:
                children.append(_obligation_node(engine, row))
        return _node("L7", "risk_score", item_id, f"{item['name']} — {item['result']['score']} ({item['result']['band']})",
                     item["result"]["formula"], meta={"result": item["result"], "inputs": item["inputs"]},
                     children=children)

    if layer == "L8":
        for item in load_curated("l8_benchmarks"):
            if item["id"] == item_id:
                return _node("L8", "benchmark", item["id"], item["name"], item.get("definition", ""),
                             meta={"locked": True, "k_anonymity": item.get("k_anonymity"), "cluster": item.get("cluster"),
                                   "note": "Closed by design: raw peer data never leaves the enclave; no aggregates are published today."})
        raise KeyError(item_id)

    raise KeyError(layer)
