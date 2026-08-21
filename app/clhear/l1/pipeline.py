"""L1 ingestion pipeline (HLD §7.2).

For each adapter run: store verbatim artifacts (datalake, public-ok/ or
restricted/ by license) -> upsert version + clauses -> clause-level diff vs the
previous version (aligned by ref) -> change_events row + outbox SourceChanged
in the SAME transaction -> run ledger entry. Deterministic end to end: no LLM
anywhere in this module (HLD principle 2).
"""
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.l1.adapters.base import Adapter, ClauseNode, SourceMeta, flatten
from app.clhear.l1.models import change_events, clauses, family_members, source_families, source_versions, sources
from app.clhear.models import runs
from app.clhear.platform import events as l0_events

log = logging.getLogger("clhear.l1.pipeline")


class ArtifactStore(Protocol):
    def put(self, key: str, content: bytes, content_type: str) -> str: ...


class LocalStore:
    """Filesystem stand-in for the datalake (offline dev/tests)."""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)

    def put(self, key: str, content: bytes, content_type: str) -> str:
        path = self.base_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path.as_uri()


class S3Store:
    """The real datalake: versioned + Object Lock, per the P0 terraform."""

    def __init__(self, bucket: str, region: str):
        import boto3

        self._client = boto3.client("s3", region_name=region)
        self.bucket = bucket

    def put(self, key: str, content: bytes, content_type: str) -> str:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=content_type)
        return f"s3://{self.bucket}/{key}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_source(conn: Connection, meta: SourceMeta) -> tuple[int, int]:
    """Upsert family + source (+ root membership). Returns (family_id, source_id)."""
    family_id = conn.execute(
        sa.select(source_families.c.id).where(source_families.c.key == meta.family_key)
    ).scalar()
    if family_id is None:
        family_id = conn.execute(
            source_families.insert()
            .values(key=meta.family_key, name=meta.family_name, scope_charter=meta.scope_charter)
            .returning(source_families.c.id)
        ).scalar_one()
    source_id = conn.execute(sa.select(sources.c.id).where(sources.c.key == meta.source_key)).scalar()
    if source_id is None:
        source_id = conn.execute(
            sources.insert()
            .values(
                family_id=family_id,
                key=meta.source_key,
                name=meta.name,
                kind=meta.kind,
                issuer=meta.issuer,
                jurisdiction=meta.jurisdiction,
                license=meta.license,
                license_ref=meta.license_ref,
                adapter=meta.adapter,
                canonical_url=meta.canonical_url,
            )
            .returning(sources.c.id)
        ).scalar_one()
        conn.execute(
            family_members.insert().values(
                family_id=family_id,
                source_id=source_id,
                relation="root",
                tier="binding",
                status="active",
                added_via="manual",
            )
        )
    return family_id, source_id


def _latest_version(conn: Connection, source_id: int):
    return conn.execute(
        sa.select(source_versions)
        .where(source_versions.c.source_id == source_id)
        .order_by(source_versions.c.id.desc())
        .limit(1)
    ).first()


def _clause_map(conn: Connection, source_version_id: int) -> dict[str, str]:
    rows = conn.execute(
        sa.select(clauses.c.ref, clauses.c.text_hash).where(clauses.c.source_version_id == source_version_id)
    ).all()
    return {row.ref: row.text_hash for row in rows}


def diff_clauses(old: dict[str, str], new: dict[str, str]) -> dict[str, list[str]]:
    """Clause-level diff aligned by ref (HLD §7.2)."""
    added = sorted(ref for ref in new if ref not in old)
    removed = sorted(ref for ref in old if ref not in new)
    amended = sorted(ref for ref in new if ref in old and new[ref] != old[ref])
    return {"added": added, "removed": removed, "amended": amended}


def ingest(engine: Engine, adapter: Adapter, store: ArtifactStore, *, trigger: str = "manual") -> dict:
    """Run one adapter through the full pipeline. Returns a run summary."""
    started = time.monotonic()
    meta = adapter.meta()

    with engine.begin() as conn:
        family_id, source_id = ensure_source(conn, meta)
        previous = _latest_version(conn, source_id)

    result = adapter.fetch(previous.version_label if previous else None)
    if result is None:
        summary = {"source": meta.source_key, "status": "up-to-date", "version": previous.version_label if previous else None}
        _record_run(engine, meta, trigger, summary, started)
        return summary

    content_hash = sha256(b"".join(a.content for a in sorted(result.artifacts, key=lambda a: a.name)))
    if previous is not None and previous.content_hash == content_hash:
        summary = {"source": meta.source_key, "status": "unchanged", "version": previous.version_label}
        _record_run(engine, meta, trigger, summary, started)
        return summary

    prefix = "public-ok" if meta.license == "open" else "restricted"
    artifact_uris = []
    for artifact in result.artifacts:
        key = f"{prefix}/{meta.source_key}/{result.version_label}/{artifact.name}"
        artifact_uris.append(store.put(key, artifact.content, artifact.content_type))

    nodes = flatten(result.clause_tree)
    public_ok = meta.license == "open"

    with engine.begin() as conn:
        if previous is not None:
            conn.execute(
                source_versions.update()
                .where(source_versions.c.id == previous.id)
                .values(status="superseded")
            )
        version_id = conn.execute(
            source_versions.insert()
            .values(
                source_id=source_id,
                version_label=result.version_label,
                effective_date=result.effective_date,
                s3_uri=artifact_uris[0] if artifact_uris else "",
                content_hash=content_hash,
                status="in_force",
            )
            .returning(source_versions.c.id)
        ).scalar_one()
        new_map: dict[str, str] = {}
        clause_rows = []
        for node in nodes:
            text_hash = sha256(node.text.encode())
            new_map[node.ref] = text_hash
            clause_rows.append(
                {
                    "source_version_id": version_id,
                    "ref": node.ref,
                    "path": node.path,
                    "ordering": node.ordering,
                    "text": node.text,
                    "text_hash": text_hash,
                    "public_ok": public_ok,
                }
            )
        if clause_rows:
            conn.execute(clauses.insert(), clause_rows)

        old_map = _clause_map(conn, previous.id) if previous is not None else {}
        diff = diff_clauses(old_map, new_map)
        changed_refs = diff["added"] + diff["removed"] + diff["amended"]
        change_kind = "amended" if previous is not None else "added"

        diff_uri = ""
        if previous is not None:
            diff_doc = json.dumps(
                {
                    "source": meta.source_key,
                    "old_version": previous.version_label,
                    "new_version": result.version_label,
                    **diff,
                },
                indent=2,
            ).encode()
            diff_uri = store.put(
                f"{prefix}/{meta.source_key}/{result.version_label}/diff.json", diff_doc, "application/json"
            )

        conn.execute(
            change_events.insert().values(
                source_id=source_id,
                kind=change_kind,
                old_version=previous.version_label if previous else None,
                new_version=result.version_label,
                clause_refs=changed_refs,
                diff_s3_uri=diff_uri,
            )
        )
        l0_events.emit(
            conn,
            layer="l1",
            kind="SourceChanged",
            subject_ref=meta.source_key,
            payload={
                "source": meta.source_key,
                "change": change_kind,
                "old_version": previous.version_label if previous else None,
                "new_version": result.version_label,
                "clause_refs": changed_refs,
                "content_hash": content_hash,
            },
            producer=f"l1.pipeline.{meta.adapter}",
        )

    summary = {
        "source": meta.source_key,
        "status": change_kind,
        "version": result.version_label,
        "clauses": len(nodes),
        "diff": diff,
        "artifacts": artifact_uris,
        "content_hash": content_hash,
    }
    _record_run(engine, meta, trigger, summary, started)
    log.info("ingested %s %s: %d clauses (%s)", meta.source_key, result.version_label, len(nodes), change_kind)
    return summary


def _record_run(engine: Engine, meta: SourceMeta, trigger: str, summary: dict, started: float) -> None:
    with engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                fleet=f"l1.{meta.adapter}",
                trigger=trigger,
                inputs={"source": meta.source_key},
                outputs=summary,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )


def build_tree(items: list[dict]) -> list[ClauseNode]:
    """Helper for adapters: nested dicts -> ClauseNode tree (used by fixtures)."""

    def node(item: dict, ordering: int) -> ClauseNode:
        children = [node(c, i) for i, c in enumerate(item.get("children", []))]
        return ClauseNode(
            ref=item["ref"],
            path=item.get("path", ""),
            ordering=item.get("ordering", ordering),
            text=item.get("text", ""),
            children=children,
        )

    return [node(item, i) for i, item in enumerate(items)]
