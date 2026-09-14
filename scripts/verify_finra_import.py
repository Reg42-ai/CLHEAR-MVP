"""Audit selected FINRA HTML snapshots against an existing SQLite corpus.

No network, migrations, registry seeding, model calls, event relay or publication.
Use --import to ingest the explicitly selected local snapshots before auditing.
An optional acquisition.json beside <rule>.html binds URLs, hashes and retrieval
times; without it, acquisition provenance is explicitly unverified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clhear.db import make_engine
from app.clhear.l1 import fidelity, models, pipeline, spans
from app.clhear.l1.adapters.base import CLAUSE_TYPES, FetchResult, flatten
from app.clhear.l1.adapters.sec_edgar import SecEdgarAdapter
from app.clhear.l1.registry_etoro import S, source_meta
from app.clhear.models import events, proposals, runs, schema_migrations
from app.clhear.platform.audit import audit_log


REGISTRY = {e["key"].rsplit("/", 1)[-1]: e for e in S if e["adapter"] == "finra" and e["key"].startswith("finra/rule/")}
DEFAULT_RULES = tuple(REGISTRY)


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class OfflineFinraAdapter(SecEdgarAdapter):
    key = "finra"
    fetch_origin = "local_snapshot"

    def __init__(self, rule: str, content: bytes, retrieved_at: str | None):
        entry = REGISTRY[rule]
        super().__init__(channel="finra", source_key=entry["key"], title=entry["name"],
                         url=entry["canonical_url"], meta=source_meta(entry))
        self.content = content
        label_date = retrieved_at[:10] if retrieved_at else date.today().isoformat()
        # A unique suffix prevents pipeline's same-label projection replacement,
        # including when re-importing corrupted rows from identical source bytes.
        self.version_label = f"consolidated:offline-{label_date}-{digest(content)[:16]}-{uuid.uuid4().hex[:12]}"

    def fetch_bytes(self) -> list[tuple[str, bytes]]:
        return [("page.html", self.content)]

    def version_of(self, content: bytes) -> tuple[str, None]:
        # Retrieval time is not a publisher's legal consolidation/effective date.
        return self.version_label, None


@dataclass
class Snapshot:
    rule: str
    path: Path
    adapter: OfflineFinraAdapter
    result: FetchResult
    provenance: dict
    coverage: float


def load_provenance(directory: Path) -> dict[str, dict]:
    manifest = directory / "acquisition.json"
    if not manifest.exists():
        return {}
    rows = json.loads(manifest.read_text())
    if not isinstance(rows, list):
        raise ValueError("acquisition.json must contain a list of acquisition records")
    records: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("rule"), str):
            raise ValueError("acquisition.json has a record without a string rule")
        if row["rule"] in records:
            raise ValueError(f"duplicate acquisition record for rule {row['rule']}")
        records[row["rule"]] = row
    return records


def prepare_snapshot(rule: str, directory: Path, records: dict[str, dict], *, manifest_present: bool) -> Snapshot:
    path = directory / f"{rule}.html"
    if not path.is_file():
        raise ValueError(f"missing local snapshot: {path}")
    content = path.read_bytes()
    record = records.get(rule)
    if manifest_present and record is None:
        raise ValueError(f"acquisition.json has no record for selected rule {rule}")
    url = REGISTRY[rule]["canonical_url"]
    provenance = {"status": "unverified", "url": url, "retrieved_at": None,
                  "sha256": digest(content), "bytes": len(content), "local_path": str(path)}
    if record is not None:
        if record.get("url") != url or record.get("final_url") != url:
            raise ValueError(f"rule {rule}: acquisition URL does not match the registered FINRA rule")
        if record.get("sha256") != digest(content) or record.get("bytes") != len(content):
            raise ValueError(f"rule {rule}: acquisition hash/byte count does not match the local snapshot")
        try:
            retrieved = datetime.fromisoformat(record["retrieved_at"].replace("Z", "+00:00"))
            if retrieved.tzinfo is None:
                raise ValueError("timezone missing")
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"rule {rule}: acquisition retrieved_at must include a timezone") from exc
        provenance.update(status="manifest_matched", retrieved_at=retrieved.isoformat(),
                          final_url=record["final_url"])
    adapter = OfflineFinraAdapter(rule, content, provenance["retrieved_at"])
    result = adapter.fetch()
    issues = adapter.validate_tree(result.tree, result.artifacts)
    gate = fidelity.check(result.tree, adapter.expected_text(result.artifacts))
    if issues or not gate.ok(1.0):
        detail = issues or [f"fidelity coverage={gate.coverage}; violations={gate.violations}"]
        raise ValueError(f"rule {rule}: snapshot failed strict FINRA validation: {'; '.join(detail)}")
    return Snapshot(rule, path, adapter, result, provenance, gate.coverage)


def open_existing_database(path: Path, *, writable: bool):
    if not path.is_file():
        raise ValueError(f"--database must be an existing local SQLite file: {path}")
    with path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise ValueError("--database is not a SQLite database")
    # SQLite mode=rw/ro never creates a missing file, even after the precheck.
    mode = "rw" if writable else "ro"
    engine = make_engine(f"sqlite:///file:{quote(str(path), safe='/')}?mode={mode}&uri=true")
    try:
        inspector = sa.inspect(engine)
        existing = set(inspector.get_table_names())
        required = (*models.ALL_TABLES, runs, events, proposals, schema_migrations, audit_log)
        issues = []
        for table in required:
            if table.name not in existing:
                issues.append(f"missing table {table.name}")
                continue
            columns = {column["name"] for column in inspector.get_columns(table.name)}
            missing = set(table.c.keys()) - columns
            if missing:
                issues.append(f"{table.name} missing columns {', '.join(sorted(missing))}")
        if issues:
            raise ValueError("unsupported SQLite schema; this command does not migrate or seed: " + "; ".join(issues))
        with engine.connect() as conn:
            if conn.exec_driver_sql("PRAGMA quick_check").scalar_one() != "ok":
                raise ValueError("SQLite quick_check failed")
        return engine
    except Exception:
        engine.dispose()
        raise


def verify_roundtrip(engine, snapshot: Snapshot, artifact_root: Path) -> dict:
    key = snapshot.adapter.meta().source_key
    result = {"source": key, "ok": False, "errors": [], "nodes": 0, "clauses": 0}
    errors = result["errors"]
    with engine.connect() as conn:
        source = conn.execute(sa.select(models.sources).where(models.sources.c.key == key)).mappings().first()
        if source is None:
            errors.append("source missing from database")
            return result
        version = conn.execute(sa.select(models.source_versions)
                               .where(models.source_versions.c.source_id == source["id"])
                               .order_by(models.source_versions.c.id.desc()).limit(1)).mappings().first()
        if version is None:
            errors.append("source has no stored version")
            return result
        nodes = conn.execute(sa.select(models.doc_nodes).where(models.doc_nodes.c.source_version_id == version["id"])
                             .order_by(models.doc_nodes.c.seq)).mappings().all()
        clauses = conn.execute(sa.select(models.clauses).where(models.clauses.c.source_version_id == version["id"])
                               .order_by(models.clauses.c.ordering)).mappings().all()
        basis = conn.execute(sa.select(models.rights_records).where(models.rights_records.c.source_id == source["id"])
                             .order_by(models.rights_records.c.id.desc()).limit(1)).mappings().first()
    result.update(version_id=version["id"], version_label=version["version_label"],
                  content_hash=version["content_hash"], nodes=len(nodes), clauses=len(clauses),
                  rights_basis=source["rights_basis"], artifact_uri=version["s3_uri"],
                  retrieved_at=version["retrieved_at"], as_of_date=version["as_of_date"],
                  effective_date=version["effective_date"])
    if version["content_hash"] != snapshot.provenance["sha256"]:
        errors.append("stored source content_hash differs from the supplied artifact")
    if version["status"] != "in_force":
        errors.append("latest stored version is not in_force")
    if snapshot.provenance["retrieved_at"]:
        retrieved = version["retrieved_at"]
        if retrieved is not None and retrieved.tzinfo is None:
            retrieved = retrieved.replace(tzinfo=timezone.utc)
        expected_retrieved = datetime.fromisoformat(snapshot.provenance["retrieved_at"])
        if retrieved != expected_retrieved:
            errors.append("stored retrieved_at differs from the acquisition manifest")
    meta = snapshot.adapter.meta()
    for field in ("canonical_url", "adapter", "publisher", "rights_basis"):
        if source[field] != getattr(meta, field):
            errors.append(f"source {field} differs from registered FINRA metadata")
    if source["rights_basis"] != "derived_only" or basis is None or basis["rights_basis"] != "derived_only" or basis["republish_text"] is not False:
        errors.append("FINRA derived-only rights record is missing or permits republication")

    expected = flatten(snapshot.result.tree)
    expected_parents: dict[int, int | None] = {}

    def parents(node, parent=None):
        expected_parents[id(node)] = id(parent) if parent else None
        for child in node.children:
            parents(child, node)

    for node in snapshot.result.tree:
        parents(node)
    expected_seq = {id(node): index for index, node in enumerate(expected, 1)}
    stored_seq = {row["id"]: row["seq"] for row in nodes}
    if len(nodes) != len(expected):
        errors.append("stored doc_nodes count differs from parsed snapshot")
    fields = ("node_type", "ref", "label", "heading", "raw_text", "source_fragment")
    for seq, (row, node) in enumerate(zip(nodes, expected), 1):
        if row["seq"] != seq:
            errors.append(f"doc_node {seq}: sequence mismatch")
        expected_parent = expected_seq.get(expected_parents[id(node)])
        if (row["parent_id"] is not None and row["parent_id"] not in stored_seq) or stored_seq.get(row["parent_id"]) != expected_parent:
            errors.append(f"doc_node {seq}: parent mismatch")
        for field in fields:
            if row[field] != getattr(node, field):
                errors.append(f"doc_node {seq}: {field} mismatch")
        payload = "\n".join(getattr(node, field) for field in fields[:-1]).encode()
        if row["text_hash"] != digest(payload):
            errors.append(f"doc_node {seq}: text_hash mismatch")
        if row["public_ok"] is not False:
            errors.append(f"doc_node {seq}: public_ok must be false")

    expected_clauses = {seq: node for seq, node in enumerate(expected, 1) if node.node_type in CLAUSE_TYPES and node.ref}
    canonical = spans.canonical_text(snapshot.result.tree)
    layout = spans.span_layout(snapshot.result.tree)
    if len(clauses) != len(expected_clauses) or not clauses:
        errors.append("stored clause count differs from parsed snapshot or is empty")
    seen = set()
    for row in clauses:
        seq = stored_seq.get(row["doc_node_id"])
        node = expected_clauses.get(seq)
        if node is None or seq in seen:
            errors.append(f"clause {row['id']}: unexpected or duplicate doc_node reference")
            continue
        seen.add(seq)
        text = node.subtree_text()
        if row["ordering"] != seq or row["ref"] != node.ref or row["text"] != text:
            errors.append(f"clause {row['id']}: ordering/ref/text mismatch")
        if row["text_hash"] != digest(text.encode()):
            errors.append(f"clause {row['id']}: text_hash mismatch")
        start, end = row["span_start"], row["span_end"]
        if (start, end) != layout.get(id(node)) or not isinstance(start, int) or not isinstance(end, int) or canonical[start:end] != row["text"]:
            errors.append(f"clause {row['id']}: canonical span mismatch")
        if row["public_ok"] is not False:
            errors.append(f"clause {row['id']}: public_ok must be false")
    if seen != set(expected_clauses):
        errors.append("stored clauses do not cover all parsed clause nodes")

    uri = urlparse(version["s3_uri"] or "")
    result["archived_bytes_checked"] = False
    if uri.scheme == "file":
        archived = Path(unquote(uri.path)).resolve()
        if not archived.is_relative_to(artifact_root):
            errors.append("local archived artifact is outside the selected artifact directory")
        elif not archived.is_file():
            errors.append("local archived artifact is missing")
        else:
            result["archived_bytes_checked"] = True
            if digest(archived.read_bytes()) != snapshot.provenance["sha256"]:
                errors.append("archived local artifact bytes differ from the supplied snapshot")
    else:
        result["archive_note"] = "Remote or absent archived artifact was not fetched; archived bytes are unverified."
    result["canonical_characters"] = len(canonical)
    result["ok"] = not errors
    result["all_bytes_verified"] = result["ok"] and result["archived_bytes_checked"]
    return result


def preserve_acquisition_time(engine, snapshot: Snapshot) -> None:
    """Bind supplied acquisition time only to this import's new unique version."""
    if not snapshot.provenance["retrieved_at"]:
        return
    with engine.begin() as conn:
        source_id = conn.execute(sa.select(models.sources.c.id)
                                 .where(models.sources.c.key == snapshot.adapter.meta().source_key)).scalar_one()
        updated = conn.execute(models.source_versions.update().where(
            models.source_versions.c.source_id == source_id,
            models.source_versions.c.version_label == snapshot.adapter.version_label,
            models.source_versions.c.content_hash == snapshot.provenance["sha256"],
        ).values(retrieved_at=datetime.fromisoformat(snapshot.provenance["retrieved_at"]).astimezone(timezone.utc)))
        if updated.rowcount != 1:
            raise ValueError("could not bind acquisition time to exactly one newly imported version")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True, help="existing local SQLite corpus; never created or migrated")
    parser.add_argument("--artifact-dir", type=Path, required=True, help="directory containing <rule>.html and optional acquisition.json")
    parser.add_argument("--rules", nargs="+", default=list(DEFAULT_RULES), help="selected registered rule numbers (space- or comma-separated)")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--import", dest="do_import", action="store_true", help="import selected snapshots before auditing; default is read-only audit")
    args = parser.parse_args(argv)
    database, directory, report_path = args.database.resolve(), args.artifact_dir.resolve(), args.report.resolve()
    rules = list(dict.fromkeys(rule for value in args.rules for rule in value.split(",")))
    report = {"mode": "import" if args.do_import else "audit", "database": str(database),
              "rules": rules, "scope": "selected individual FINRA rules only; not the complete rulebook",
              "checked_at": datetime.now(timezone.utc).isoformat(), "ok": False, "results": [], "errors": [],
              "boundaries": {"network": False, "migrations": False, "registry_seed": False,
                             "models": False, "embedding_index": False, "event_relay": False, "publication": False},
              "acquisition_note": "Manifest matching checks supplied provenance, not independent authentication of the publisher or current legal effect."}
    engine = None
    try:
        if not rules or any(rule not in REGISTRY for rule in rules):
            raise ValueError(f"--rules must select registered individual FINRA rules: {', '.join(DEFAULT_RULES)}")
        protected = {database, directory / "acquisition.json", *(directory / f"{rule}.html" for rule in rules)}
        if report_path in {path.resolve() for path in protected}:
            raise ValueError("--report must not overwrite the database, snapshots or acquisition manifest")
        engine = open_existing_database(database, writable=args.do_import)
        records = load_provenance(directory)
        # Validate every requested artifact before the first database mutation.
        snapshots = [prepare_snapshot(rule, directory, records, manifest_present=(directory / "acquisition.json").exists()) for rule in rules]
        store_root = (directory / "imported").resolve()
        for snapshot in snapshots:
            before = verify_roundtrip(engine, snapshot, store_root)
            ingest_result = None
            if args.do_import and not before["ok"]:
                ingest_result = pipeline.ingest(engine, snapshot.adapter, pipeline.LocalStore(store_root),
                                                trigger="finra-offline-import", gateway=None, force=True,
                                                job_id=f"finra-offline-{uuid.uuid4().hex}", index_embeddings=False)
                if ingest_result.get("status") in {"added", "amended"}:
                    preserve_acquisition_time(engine, snapshot)
            after = verify_roundtrip(engine, snapshot, store_root)
            if ingest_result is not None and ingest_result.get("status") not in {"added", "amended", "unchanged", "up-to-date"}:
                after["ok"] = False
                after["errors"].append(f"pipeline import failed: {ingest_result.get('status')}")
            report["results"].append({"rule": snapshot.rule, "acquisition": snapshot.provenance,
                                      "fidelity_coverage": snapshot.coverage, "import": ingest_result, "audit": after})
        report["ok"] = all(item["audit"]["ok"] for item in report["results"])
        report["all_bytes_verified"] = report["ok"] and all(item["audit"].get("all_bytes_verified", False) for item in report["results"])
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {str(exc)[:2000]}")
    finally:
        if engine is not None:
            engine.dispose()
    # Invalid output paths must never destroy an input, including on failure.
    protected = {database, directory / "acquisition.json", *(directory / f"{rule}.html" for rule in REGISTRY)}
    if report_path in {path.resolve() for path in protected}:
        print(json.dumps(report, indent=2, default=str))
        return 2
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps({"ok": report["ok"], "mode": report["mode"], "rules": rules, "report": str(report_path), "errors": report["errors"]}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
