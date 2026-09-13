"""Identifier crosswalks — L3 blocks ↔ NIST CSF 2.0 / SP 800-53r5 / ISO 27001:2022 / CSA CCM v4
(standard §8; HLD v2 §7.3 rights: licensed frameworks appear as identifiers only).

Rows come from three places, and every row names its basis:

* the block's own ``implements_controls`` anchors (curated library or L3 fleet);
* ``export/clhear/crosswalks/seed.json`` block edges;
* transitive edges through another framework (block → CSF → ISO), marked ``via`` and
  degraded to the weakest relation in the chain.

SKOS mapping relations; ``exactMatch`` > ``closeMatch`` > ``broadMatch``/``narrowMatch`` > ``relatedMatch``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.clhear.derived_models import blocks as blocks_t
from app.clhear.derived_models import obligations as obligations_t
from app.clhear.derived_models import requires as requires_t

SEED_FILE = Path(__file__).resolve().parents[3] / "export" / "clhear" / "crosswalks" / "seed.json"
RELATIONS = ("exactMatch", "closeMatch", "broadMatch", "narrowMatch", "relatedMatch")
_STRENGTH = {"exactMatch": 4, "closeMatch": 3, "broadMatch": 2, "narrowMatch": 2, "relatedMatch": 1}


class UnknownFramework(KeyError):
    pass


def seed() -> dict:
    return json.loads(SEED_FILE.read_text(encoding="utf-8"))


def frameworks() -> dict[str, dict]:
    return seed()["frameworks"]


def _check_framework(key: str) -> dict:
    fw = frameworks().get(key)
    if fw is None:
        raise UnknownFramework(key)
    return fw


def title_for(framework: str, ref: str) -> str | None:
    """Only public-domain frameworks carry titles; licensed ones return None (identifiers only)."""
    fw = _check_framework(framework)
    if fw.get("identifiers_only"):
        return None
    return (seed()["titles"].get(framework) or {}).get(ref)


def _weaker(a: str, b: str) -> str:
    if a == b:
        return a
    return a if _STRENGTH[a] < _STRENGTH[b] else b


def _valid_ref(framework: str, ref: str) -> bool:
    pat = frameworks()[framework].get("id_pattern")
    return bool(re.match(pat, ref)) if pat else True


def _framework_graph() -> dict[tuple[str, str], list[dict]]:
    """Adjacency (framework, ref) -> [{framework, ref, relation, basis}], both directions."""
    g: dict[tuple[str, str], list[dict]] = {}
    for e in seed()["framework_edges"]:
        g.setdefault((e["from"], e["from_ref"]), []).append({"framework": e["to"], "ref": e["to_ref"], "relation": e["relation"], "basis": e["basis"]})
        inv = {"broadMatch": "narrowMatch", "narrowMatch": "broadMatch"}.get(e["relation"], e["relation"])
        g.setdefault((e["to"], e["to_ref"]), []).append({"framework": e["from"], "ref": e["from_ref"], "relation": inv, "basis": e["basis"]})
    return g


def _json(v, default):
    if v is None:
        return default
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v


def block_rows(conn: Connection, *, block_ids: list[str] | None = None) -> list[dict]:
    """Every crosswalk row for current blocks: direct anchors, seed edges, and one hop through another framework."""
    q = sa.select(blocks_t.c.id, blocks_t.c.name, blocks_t.c.kind, blocks_t.c.implements_controls).where(blocks_t.c.valid_to.is_(None)).order_by(blocks_t.c.id)
    if block_ids is not None:
        q = q.where(blocks_t.c.id.in_(block_ids))
    fws = frameworks()
    graph = _framework_graph()
    seed_edges: dict[str, list[dict]] = {}
    for e in seed()["block_edges"]:
        seed_edges.setdefault(e["block"], []).append(e)
    rows: list[dict] = []
    for b in conn.execute(q).mappings():
        direct: dict[tuple[str, str], dict] = {}
        for anchor in _json(b["implements_controls"], []) or []:
            fw, ref = anchor.get("source_key"), anchor.get("ref")
            if fw in fws and ref and _valid_ref(fw, ref):
                direct[(fw, ref)] = {"relation": anchor.get("relation") or "closeMatch", "basis": anchor.get("basis") or "block implements_controls anchor"}
        for e in seed_edges.get(b["id"], []):
            if e["framework"] in fws and _valid_ref(e["framework"], e["ref"]):
                key = (e["framework"], e["ref"])
                if key not in direct or _STRENGTH[e["relation"]] > _STRENGTH[direct[key]["relation"]]:
                    direct[key] = {"relation": e["relation"], "basis": e["basis"]}
        emitted: dict[tuple[str, str], dict] = {}
        for (fw, ref), d in direct.items():
            emitted[(fw, ref)] = {"block_id": b["id"], "block_name": b["name"], "block_kind": b["kind"], "framework": fw, "ref": ref,
                                  "title": title_for(fw, ref), "relation": d["relation"], "basis": d["basis"], "via": None}
        for (fw, ref), d in list(direct.items()):
            for nxt in graph.get((fw, ref), []):
                key = (nxt["framework"], nxt["ref"])
                rel = _weaker(d["relation"], nxt["relation"])
                if key in emitted and (emitted[key]["via"] is None or _STRENGTH[emitted[key]["relation"]] >= _STRENGTH[rel]):
                    continue
                emitted[key] = {"block_id": b["id"], "block_name": b["name"], "block_kind": b["kind"], "framework": nxt["framework"], "ref": nxt["ref"],
                                "title": title_for(nxt["framework"], nxt["ref"]), "relation": rel, "basis": nxt["basis"],
                                "via": {"framework": fw, "ref": ref, "relation": d["relation"]}}
        rows += sorted(emitted.values(), key=lambda r: (r["framework"], r["ref"]))
    return rows


def for_framework(conn: Connection, framework: str) -> list[dict]:
    _check_framework(framework)
    return [r for r in block_rows(conn) if r["framework"] == framework]


def for_ref(conn: Connection, framework: str, ref: str) -> dict:
    """Blocks for one framework identifier, with the obligations that require each block."""
    _check_framework(framework)
    rows = [r for r in block_rows(conn) if r["framework"] == framework and r["ref"] == ref]
    obligations_by_block: dict[str, list[dict]] = {}
    if rows:
        ids = [r["block_id"] for r in rows]
        q = (sa.select(requires_t.c.block_id, obligations_t.c.id, obligations_t.c.stable_id, obligations_t.c.source_key, obligations_t.c.clause_ref,
                       obligations_t.c.jurisdiction, obligations_t.c.title)
             .select_from(requires_t.join(obligations_t, obligations_t.c.id == requires_t.c.obligation_id))
             .where(requires_t.c.block_id.in_(ids), requires_t.c.valid_to.is_(None), obligations_t.c.valid_to.is_(None)))
        for r in conn.execute(q).mappings():
            obligations_by_block.setdefault(r["block_id"], []).append(
                {"id": r["id"], "stable_id": r["stable_id"], "source_key": r["source_key"], "clause_ref": r["clause_ref"],
                 "jurisdiction": r["jurisdiction"], "title": r["title"]})
    fw = frameworks()[framework]
    return {"framework": framework, "framework_name": fw["name"], "ref": ref, "title": title_for(framework, ref), "rights": fw["rights"],
            "blocks": [{**r, "required_by": obligations_by_block.get(r["block_id"], [])} for r in rows],
            "also": [{"framework": n["framework"], "ref": n["ref"], "relation": n["relation"], "basis": n["basis"], "title": title_for(n["framework"], n["ref"])}
                     for n in _framework_graph().get((framework, ref), [])]}


def for_block(conn: Connection, block_id: str) -> list[dict]:
    return block_rows(conn, block_ids=[block_id])


def summary(conn: Connection) -> dict:
    rows = block_rows(conn)
    out = []
    for key, fw in frameworks().items():
        mine = [r for r in rows if r["framework"] == key]
        out.append({"key": key, "name": fw["name"], "publisher": fw["publisher"], "rights": fw["rights"], "identifiers_only": fw["identifiers_only"],
                    "url": fw["url"], "rows": len(mine), "blocks": len({r["block_id"] for r in mine}), "refs": len({r["ref"] for r in mine}),
                    "direct": sum(1 for r in mine if r["via"] is None), "via": sum(1 for r in mine if r["via"] is not None)})
    return {"frameworks": out, "relations": list(RELATIONS), "seed_version": seed()["version"], "blocks_with_any": len({r["block_id"] for r in rows})}


def write(conn: Connection, repo_dir: Path, release: str) -> list[Path]:
    """Per-release crosswalk files for the public repo (identifiers only for licensed frameworks)."""
    out_dir = repo_dir / "crosswalks"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    rows = block_rows(conn)
    index = {"release": release, "seed_version": seed()["version"], "licence": "CC-BY-4.0", "frameworks": {}}
    for key, fw in frameworks().items():
        mine = [r for r in rows if r["framework"] == key]
        if fw.get("identifiers_only"):
            for r in mine:
                r["title"] = None
        name = key.replace("/", "_") + ".json"
        (out_dir / name).write_text(json.dumps({"framework": key, "name": fw["name"], "rights": fw["rights"], "release": release, "rows": mine},
                                               indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(out_dir / name)
        index["frameworks"][key] = {"file": name, "rows": len(mine), "identifiers_only": fw["identifiers_only"]}
    (out_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    written.append(out_dir / "index.json")
    return written
