"""The public ``clhear`` repository layout (HLD v2 §6 tooling, §3 CLHEAR-0.32).

    clhear/
      README.md  LICENSES/  governance/  ROADMAP.md  .github/   ← static, from export/clhear/
      standard/                       the standard text (layers, invariants, identifiers)
      schema/<layer>/<table>.schema.json   JSON Schema generated from the record tables
      vault/<release>/L2|L3|L5/*.md   Obsidian vault pack (wikilinks across layers)
      evals/golden/                   the per-layer golden sets (public — I10)
      evals/contributed/<suite>.json  golden cases accepted from contributors
      evals/harness.py                stdlib scorer for candidate outputs
      evals/<release>.json            gate scores of the release
      sdks/python  sdks/typescript
      snapshots/<release>/            the allow-list snapshot (exporter.write_snapshot)
      RELEASE_NOTES/<release>.md      what shipped, with contributor attribution + impact

Allow-list discipline (I8): the vault carries obligation statements only when the
anchoring clause is ``public_ok`` under a republishable rights basis; otherwise the
note says so and carries the span + hash. Nothing else here reads clause text.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import activities, asserts, blocks, obligations, operates, requires
from app.clhear.l1 import rights as l1_rights
from app.clhear.l1.models import clauses, source_versions, sources

REPO_ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = REPO_ROOT / "export" / "clhear"
GOLDEN_DIR = REPO_ROOT / "clhear-evals"

STATIC_ENTRIES = ("README.md", "ROADMAP.md", "CONTRIBUTING.md", "LICENSES", "governance", "standard", "sdks",
                  "evals", ".github", "conformance", "SECURITY.md")
VAULT_LAYERS = ("L2", "L3", "L5")

_JSON_TYPES = {
    "TEXT": "string", "VARCHAR": "string", "UUID": "string", "CHAR": "string",
    "INTEGER": "integer", "BIGINT": "integer", "SMALLINT": "integer",
    "NUMERIC": "number", "FLOAT": "number", "DECIMAL": "number",
    "BOOLEAN": "boolean",
    "DATE": "string", "DATETIME": "string", "TIMESTAMP": "string",
    "JSON": ["object", "array", "string", "number", "boolean", "null"],
    "BLOB": "string", "LARGEBINARY": "string",
}


# --------------------------------------------------------------------------- static tree


def copy_static(repo_dir: Path) -> list[Path]:
    written: list[Path] = []
    for name in STATIC_ENTRIES:
        src = STATIC_DIR / name
        if not src.exists():
            continue
        dest = repo_dir / name
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "node_modules", "*.pyc"))
            written += [p for p in dest.rglob("*") if p.is_file()]
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            written.append(dest)
    return written


# --------------------------------------------------------------------------- JSON Schema


def _column_schema(col: sa.Column) -> dict:
    tname = type(col.type).__name__.upper()
    generic = None
    try:
        generic = type(col.type.as_generic()).__name__.upper()
    except (NotImplementedError, AttributeError):
        pass
    jtype = _JSON_TYPES.get(generic or "", None) or _JSON_TYPES.get(tname, "string")
    out: dict = {"type": jtype}
    if generic in ("DATE",):
        out["format"] = "date"
    elif generic in ("DATETIME", "TIMESTAMP"):
        out["format"] = "date-time"
    elif tname == "UUID":
        out["format"] = "uuid"
    if col.nullable and isinstance(jtype, str):
        out["type"] = [jtype, "null"]
    if col.comment:
        out["description"] = col.comment
    return out


def table_schema(table: sa.Table, *, layer: str, base_id: str = "https://clhear.org/schema") -> dict:
    props = {c.name: _column_schema(c) for c in table.columns}
    required = [c.name for c in table.columns if not c.nullable and c.default is None and c.server_default is None
                and not c.primary_key and not c.autoincrement is True]
    checks = []
    for cons in table.constraints:
        if isinstance(cons, sa.CheckConstraint):
            checks.append(str(cons.sqltext))
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{base_id}/{layer.lower()}/{table.name}.schema.json",
        "title": f"CLHEAR {layer} {table.name}",
        "description": f"Record shape of {table.schema}.{table.name} (bi-temporal: valid_from / valid_to, version, why_trail_id).",
        "type": "object",
        "properties": props,
        "required": sorted(required),
        "additionalProperties": False,
        "x-clhear": {"layer": layer, "schema": table.schema, "checks": checks,
                     "primary_key": [c.name for c in table.primary_key.columns]},
    }


def _layer_of(table: sa.Table) -> str:
    schema = (table.schema or "").lower()
    for n in range(1, 9):
        if schema.startswith(f"l{n}_"):
            return f"L{n}"
    return "L0"


def write_schemas(repo_dir: Path) -> list[Path]:
    from app.clhear.platform import record

    from app.clhear.l7 import models as l7_models

    tables = list(record.layer_tables())
    if l7_models.risk_calibrations not in tables:
        tables.append(l7_models.risk_calibrations)  # append-only method ledger: published, no shared columns
    written: list[Path] = []
    index: dict[str, list[str]] = {}
    for table in tables:
        layer = _layer_of(table)
        path = repo_dir / "schema" / layer.lower() / f"{table.name}.schema.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(table_schema(table, layer=layer), indent=2) + "\n", encoding="utf-8")
        written.append(path)
        index.setdefault(layer, []).append(f"{layer.lower()}/{table.name}.schema.json")
    idx = repo_dir / "schema" / "index.json"
    idx.write_text(json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema",
                               "title": "CLHEAR record schemas", "licence": "CC-BY-4.0",
                               "layers": {k: sorted(v) for k, v in sorted(index.items())}}, indent=2) + "\n", encoding="utf-8")
    written.append(idx)
    return written


# --------------------------------------------------------------------------- Obsidian vault pack


def _fm(fields: dict) -> str:
    lines = ["---"]
    for k, v in fields.items():
        if isinstance(v, (list, tuple)):
            lines.append(f"{k}: [{', '.join(json.dumps(str(x)) for x in v)}]")
        elif v is None or v == "":
            lines.append(f"{k}: null")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, str):
            lines.append(f"{k}: {json.dumps(v) if (':' in v or v.startswith('[') or v.strip() != v) else v}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def _public_statement(conn: Connection, ob: dict) -> tuple[bool, dict]:
    """Whether the obligation's statement may be republished, with the anchoring clause's span + hash."""
    row = conn.execute(
        sa.select(clauses.c.public_ok, clauses.c.text_hash, clauses.c.span_start, clauses.c.span_end, sources.c.rights_basis, sources.c.key)
        .select_from(asserts.join(clauses, clauses.c.id == asserts.c.clause_id)
                     .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
                     .join(sources, sources.c.id == source_versions.c.source_id))
        .where(asserts.c.obligation_id == ob["id"], asserts.c.valid_to.is_(None)).order_by(asserts.c.id.desc()).limit(1)
    ).first()
    if row is None:
        return False, {"rights_basis": "unknown", "text_hash": ob.get("text_hash", "")}
    ok = bool(row.public_ok) and l1_rights.republishable(row.rights_basis or "")
    return ok, {"rights_basis": row.rights_basis, "text_hash": row.text_hash, "span": [row.span_start, row.span_end],
                "source_key": row.key}


def _slug(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in s)[:120]


def write_vault(engine: Engine, repo_dir: Path, release: str) -> list[Path]:
    root = repo_dir / "vault" / release
    written: list[Path] = []
    with engine.connect() as conn:
        obs = [dict(r) for r in conn.execute(sa.select(obligations).where(obligations.c.valid_to.is_(None),
                                                                            obligations.c.status != "stale")).mappings()]
        req = {}
        for r in conn.execute(sa.select(requires.c.obligation_id, requires.c.block_id).where(requires.c.valid_to.is_(None))):
            req.setdefault(r.obligation_id, []).append(r.block_id)
        blks = [dict(r) for r in conn.execute(sa.select(blocks).where(blocks.c.valid_to.is_(None))).mappings()]
        acts = [dict(r) for r in conn.execute(sa.select(activities).where(activities.c.valid_to.is_(None))).mappings()]
        ops = {}
        for r in conn.execute(sa.select(operates.c.activity_id, operates.c.block_id).where(operates.c.valid_to.is_(None))):
            ops.setdefault(r.activity_id, []).append(r.block_id)
        (root / "L2").mkdir(parents=True, exist_ok=True)
        (root / "L3").mkdir(parents=True, exist_ok=True)
        (root / "L5").mkdir(parents=True, exist_ok=True)
        ob_by_id = {o["id"]: o for o in obs}
        for ob in obs:
            name = ob.get("stable_id") or _slug(ob["id"])
            public, basis = _public_statement(conn, ob)
            body = [f"# {ob.get('title') or name}", ""]
            if public:
                body += [ob.get("statement") or "", ""]
            else:
                body += [f"> Text withheld — rights basis `{basis.get('rights_basis')}` does not allow republication. "
                         f"Span {basis.get('span')} of clause `{basis.get('text_hash')}` in `{basis.get('source_key') or ob['source_key']}`.", ""]
            body += ["## Structure", f"- Subject: {ob.get('subject') or '—'}", f"- Action: {ob.get('action') or '—'}",
                     f"- Condition: {ob.get('condition') or '—'}", f"- Object: {ob.get('object') or '—'}",
                     f"- Type: {ob.get('obligation_type') or '—'}", ""]
            body += ["## Requires (L3)"] + ([f"- [[{b}]]" for b in req.get(ob["id"], [])] or ["- —"])
            body += ["", "## Source (L1)", f"- `{ob['source_key']}` § `{ob['clause_ref']}`", "",
                     f"Why: `{ob.get('why_trail_id') or '—'}` · confidence {ob.get('confidence')} · status {ob.get('status')}"]
            fm = _fm({"id": ob["id"], "stable_id": ob.get("stable_id"), "layer": "L2", "jurisdiction": ob.get("jurisdiction"),
                      "source": ob["source_key"], "clause": ob["clause_ref"], "status": ob.get("status"),
                      "valid_from": str(ob.get("valid_from") or ""), "text_public": public, "licence": "ODC-By-1.0"})
            p = root / "L2" / f"{name}.md"
            p.write_text(fm + "\n" + "\n".join(body) + "\n", encoding="utf-8")
            written.append(p)
        back: dict[str, list[str]] = {}
        for oid, bl in req.items():
            for b in bl:
                back.setdefault(b, []).append(ob_by_id.get(oid, {}).get("stable_id") or _slug(oid))
        for b in blks:
            body = [f"# {b['name']}", "", b.get("purpose") or b.get("description") or "", "",
                    f"Kind: **{b.get('kind')}** · status {b.get('status')}", "", "## Required by (L2)"]
            body += [f"- [[{o}]]" for o in back.get(b["id"], [])] or ["- —"]
            fm = _fm({"id": b["id"], "layer": "L3", "kind": b.get("kind"), "status": b.get("status"),
                      "canonical_id": b.get("canonical_id"), "licence": "ODC-By-1.0"})
            p = root / "L3" / f"{_slug(b['id'])}.md"
            p.write_text(fm + "\n" + "\n".join(body) + "\n", encoding="utf-8")
            written.append(p)
        for a in acts:
            body = [f"# {a['name']}", "", a.get("description") or "", "",
                    f"Side: **{a.get('side')}** · action type `{a.get('action_type')}`", "", "## Operates (L3)"]
            body += [f"- [[{b}]]" for b in ops.get(a["id"], [])] or ["- —"]
            fm = _fm({"id": a["id"], "layer": "L5", "side": a.get("side"), "action_type": a.get("action_type"),
                      "status": a.get("status"), "licence": "ODC-By-1.0"})
            p = root / "L5" / f"{_slug(a['id'])}.md"
            p.write_text(fm + "\n" + "\n".join(body) + "\n", encoding="utf-8")
            written.append(p)
    index = root / "index.md"
    index.write_text(
        f"# CLHEAR vault pack — release {release}\n\n"
        f"Open this folder as an Obsidian vault. Notes link across layers with wikilinks: an obligation links the blocks it "
        f"requires, a block links back to the obligations that require it, an activity links the blocks it operates.\n\n"
        f"- L2 obligations: {sum(1 for p in written if p.parent.name == 'L2')}\n"
        f"- L3 blocks: {sum(1 for p in written if p.parent.name == 'L3')}\n"
        f"- L5 activities: {sum(1 for p in written if p.parent.name == 'L5')}\n\n"
        f"Data licence: ODC-By 1.0 (see LICENSES/). Obligation text appears only where the source's rights basis allows it (I8).\n",
        encoding="utf-8")
    written.append(index)
    return written


# --------------------------------------------------------------------------- evals


def write_evals(engine: Engine | None, repo_dir: Path) -> list[Path]:
    written: list[Path] = []
    golden = repo_dir / "evals" / "golden"
    if GOLDEN_DIR.exists():
        shutil.copytree(GOLDEN_DIR, golden, dirs_exist_ok=True)
        written += [p for p in golden.rglob("*") if p.is_file()]
    if engine is not None:
        from app.clhear.community_models import contributions

        by_suite: dict[str, list[dict]] = {}
        with engine.connect() as conn:
            rows = conn.execute(sa.select(contributions).where(contributions.c.kind == "golden_case",
                                                               contributions.c.status.in_(("accepted", "released")))).mappings()
            for r in rows:
                proposed = r["proposed"] if isinstance(r["proposed"], dict) else json.loads(r["proposed"] or "{}")
                raw = proposed.get("case") or {}
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except ValueError:
                        raw = {"value": raw}
                case = dict(raw) if isinstance(raw, dict) else {"value": raw}
                case.setdefault("contributed_by", r["contributor_email"].split("@")[0])
                case.setdefault("contribution", r["id"])
                by_suite.setdefault(str(proposed.get("suite") or "misc"), []).append(case)
        for suite, cases in sorted(by_suite.items()):
            p = repo_dir / "evals" / "contributed" / f"{_slug(suite)}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"suite": suite, "licence": "CC-BY-4.0", "cases": cases}, indent=2) + "\n", encoding="utf-8")
            written.append(p)
    return written


# --------------------------------------------------------------------------- release notes


def write_release_notes(repo_dir: Path, release: str, *, snapshot: dict, attribution: list[dict]) -> Path:
    path = repo_dir / "RELEASE_NOTES" / f"{release}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# CLHEAR release {release}", "",
             f"Generated {snapshot.get('generated_at', datetime.now(timezone.utc).isoformat())}. "
             f"Evals: {'all gates passed' if snapshot.get('all_evals_passed') else 'see evals/' + release + '.json'}.", "",
             "## Contributions in this release", ""]
    if attribution:
        lines.append("| Contribution | Kind | Target | Contributor | Impact |")
        lines.append("|---|---|---|---|---|")
        for a in attribution:
            imp = a.get("impact") or {}
            impact = f"{imp.get('blueprints_changed', 0)} blueprints, {imp.get('obligations_changed', 0)} obligations"
            lines.append(f"| {a['contribution_id']} | {a['kind']} | `{a.get('target_ref') or a.get('layer')}`"
                         f"{(' · ' + a['field']) if a.get('field') else ''} | {a['contributor']} | {impact} |")
        lines += ["", f"Thank you to {', '.join(sorted({a['contributor'] for a in attribution}))}. "
                      "Attribution follows the CLA; every accepted change carries its contributor on the record."]
    else:
        lines.append("No community contributions shipped in this release.")
    lines += ["", "## Licences", "",
              "Standard text and schemas CC BY 4.0 · data outputs ODC-By 1.0 · evals harness and SDKs Apache-2.0 (see `LICENSES/`)."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- all together


def build(engine: Engine, repo_dir: Path, release: str, *, snapshot: dict, attribution: list[dict] | None = None) -> dict:
    repo_dir.mkdir(parents=True, exist_ok=True)
    out = {"static": copy_static(repo_dir), "schema": write_schemas(repo_dir), "vault": write_vault(engine, repo_dir, release),
           "evals": write_evals(engine, repo_dir)}
    out["release_notes"] = [write_release_notes(repo_dir, release, snapshot=snapshot, attribution=attribution or [])]
    return out
