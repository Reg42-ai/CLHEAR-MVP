"""L1 ingestion pipeline (HLD §7.2) with the fidelity gate + repair loop.

For each adapter run: fetch verbatim artifacts -> structural parse -> FIDELITY
GATE (coverage vs the adapter's dumb oracle + contract invariants) -> on
failure, escalate deterministically (learned parse hints -> bounded salvage ->
re-fetch -> LLM-proposed hints via the L0 gateway) until the threshold is met
or attempts are exhausted -> store artifacts -> persist the DocNode tree ->
derive the `clauses` projection -> clause-level diff -> change_events +
outbox SourceChanged in the SAME transaction -> run ledger entry.

Every run is recorded from START with appended stage transitions (the Fleet
visualizer reads these). On exhaustion NOTHING is persisted: the failure is
logged, recorded, emitted as IngestFidelityFailed, and filed as an
`ingest_rectification` proposal for a maintainer (agents propose, humans
ratify). Daily jobs stay LLM-free unless tiers 1-3 cannot reach the goal.
"""
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.l1 import fidelity
from app.clhear.l1.adapters.base import CLAUSE_TYPES, Adapter, DocNode, FetchResult, SourceMeta
from app.clhear.l1.models import (
    change_events,
    clauses,
    doc_nodes,
    family_members,
    parse_hints,
    source_families,
    source_versions,
    sources,
)
from app.clhear.models import runs
from app.clhear.platform import events as l0_events
from app.clhear.platform import proposals as l0_proposals
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.l1.pipeline")

REPAIR_FLEET = "l1.repair"


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


class RunRecorder:
    """Run-ledger row written at START; stage transitions appended as the run
    progresses (append-only within the row — the audit trail the Fleet view
    renders). finish() stamps the final status + summary. When the caller is
    part of a fleet execution, inputs carry its job_id (the Fleet job canvas
    groups tasks by it)."""

    def __init__(self, engine: Engine, fleet: str, trigger: str, inputs: dict):
        self._engine = engine
        self._started = time.monotonic()
        self._last_stage_at = self._started
        self.stages: list[dict] = []
        with engine.begin() as conn:
            self.run_id = conn.execute(
                runs.insert()
                .values(fleet=fleet, trigger=trigger, inputs=inputs, outputs={"status": "running", "stages": []})
                .returning(runs.c.id)
            ).scalar_one()

    def stage(self, name: str, **detail) -> None:
        now = time.monotonic()
        entry = {
            "stage": name,
            "ts": datetime.now(timezone.utc).isoformat(),
            "ms": int((now - self._last_stage_at) * 1000),
            **detail,
        }
        self._last_stage_at = now
        self.stages.append(entry)
        with self._engine.begin() as conn:
            conn.execute(
                runs.update()
                .where(runs.c.id == self.run_id)
                .values(outputs={"status": "running", "stages": self.stages})
            )

    def finish(self, status: str, summary: dict) -> dict:
        outputs = {**summary, "status": status, "stages": self.stages}
        with self._engine.begin() as conn:
            conn.execute(
                runs.update()
                .where(runs.c.id == self.run_id)
                .values(outputs=outputs, duration_ms=int((time.monotonic() - self._started) * 1000))
            )
        return outputs


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
                about=meta.about,
                topics=meta.topics,
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
    else:
        # Curated context is authored in code; keep the row in sync.
        conn.execute(
            sources.update().where(sources.c.id == source_id).values(about=meta.about, topics=meta.topics)
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


def _load_active_hints(conn: Connection, source_id: int) -> list[dict]:
    rows = conn.execute(
        sa.select(parse_hints)
        .where(parse_hints.c.source_id == source_id)
        .where(parse_hints.c.status.in_(("candidate", "approved")))
        .order_by(parse_hints.c.id)
    ).all()
    out = []
    for row in rows:
        hint = row.hint if isinstance(row.hint, dict) else json.loads(row.hint)
        out.append({**hint, "hint_id": row.id})
    return out


def _markup_window(artifacts, span: str, width: int = 600) -> str:
    """Locate the span's leading text inside an artifact and return the raw
    markup around it — context for the LLM, straight from the original."""
    needle = fidelity.ws(span)[:80]
    for artifact in artifacts:
        try:
            text = artifact.content.decode("utf-8", errors="replace")
        except Exception:
            continue
        idx = text.find(needle[:40])
        if idx == -1:
            idx = fidelity.ws(text).find(needle)
            if idx == -1:
                continue
            return fidelity.ws(text)[max(0, idx - width // 2) : idx + width]
        return text[max(0, idx - width // 2) : idx + width]
    return ""


def _llm_propose_hints(gateway, artifacts, missing_spans: list[str]) -> list[dict]:
    """Tier-4 escalation: ask the gateway for parse hints. The LLM only ever
    CLASSIFIES artifact text (node_type/label per span) — it never writes it."""
    from app.clhear.l1.models import NODE_TYPES

    settings = get_settings()
    samples = []
    for span in missing_spans[:12]:
        samples.append(
            {
                "span": fidelity.ws(span)[:400],
                "markup_context": _markup_window(artifacts, span)[:800],
            }
        )
    prompt = (
        "You are repairing a deterministic legal-document parser. The following text spans exist in the "
        "official artifact but were missed by the structural parse. For each span, propose a parse hint.\n"
        f"Allowed node_type values: {', '.join(NODE_TYPES)}.\n"
        'Respond with JSON only: {"hints": [{"match": "<distinctive substring of the span>", '
        '"node_type": "...", "label": "<printed marker if the span starts with one, else empty>", '
        '"ref": "<stable ref if inferable, else empty>"}]}\n\n'
        f"Missed spans with surrounding original markup:\n{json.dumps(samples, ensure_ascii=False, indent=1)}"
    )
    result = gateway.call(
        fleet=REPAIR_FLEET,
        model=settings.clhear_model_repair,
        prompt=prompt,
        system="You classify document structure. You never rewrite or invent text. JSON only.",
        max_tokens=2000,
        required_keys=["hints"],
    )
    hints = json.loads(result.text).get("hints", [])
    return [h for h in hints if isinstance(h, dict) and h.get("match") and h.get("node_type")]


def ingest(
    engine: Engine,
    adapter: Adapter,
    store: ArtifactStore,
    *,
    trigger: str = "manual",
    gateway=None,
    job_id: str | None = None,
) -> dict:
    """Run one adapter through fetch -> fidelity gate/repair loop -> persist.

    Returns the run summary. status: added|amended|unchanged|up-to-date|
    not-fully-successful. `llm_assisted`/`recovered_spans`/`hints_used` mark
    degraded-but-successful runs (warnings in the Activity feed).
    """
    settings = get_settings()
    meta = adapter.meta()
    inputs = {"source": meta.source_key}
    if job_id:
        inputs["job_id"] = job_id
    recorder = RunRecorder(engine, f"l1.{meta.adapter}", trigger, inputs)

    with engine.begin() as conn:
        family_id, source_id = ensure_source(conn, meta)
        previous = _latest_version(conn, source_id)
        stored_hints = _load_active_hints(conn, source_id)

    result = adapter.fetch(previous.version_label if previous else None)
    recorder.stage("fetch", artifacts=len(result.artifacts) if result else 0)
    if result is None:
        summary = {"source": meta.source_key, "version": previous.version_label if previous else None}
        outputs = recorder.finish("up-to-date", summary)
        return {**summary, "status": "up-to-date", "run_id": recorder.run_id, "stages": outputs["stages"]}

    content_hash = sha256(b"".join(a.content for a in sorted(result.artifacts, key=lambda a: a.name)))
    if previous is not None and previous.content_hash == content_hash:
        summary = {"source": meta.source_key, "version": previous.version_label}
        outputs = recorder.finish("unchanged", summary)
        return {**summary, "status": "unchanged", "run_id": recorder.run_id, "stages": outputs["stages"]}

    # ---- fidelity gate + escalation loop -----------------------------------
    threshold = settings.clhear_fidelity_threshold
    max_attempts = max(1, settings.clhear_ingest_max_attempts)
    salvage_cap = settings.clhear_salvage_cap

    report = None
    hints_used: list[int] = []
    new_llm_hints: list[dict] = []
    recovered_spans = 0
    llm_assisted = False
    last_attempt_coverage = -1.0

    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            refetched = adapter.fetch(None)  # fresh fetch: guards against a corrupted download
            recorder.stage("fetch", attempt=attempt, artifacts=len(refetched.artifacts) if refetched else 0)
            if refetched is not None:
                result = refetched
        tree = result.tree
        expected = adapter.expected_text(result.artifacts)
        node_count = sum(1 for n in tree for _ in n.walk())
        recorder.stage("parse", attempt=attempt, nodes=node_count)

        report = fidelity.check(tree, expected)
        recorder.stage("gate", attempt=attempt, **report.summary())

        # Tier 1b: learned hints (deterministic; zero LLM).
        if not report.ok(threshold) and not report.violations and report.missing_spans and stored_hints:
            remaining, used = fidelity.apply_hints(tree, report.missing_spans, stored_hints)
            if used:
                hints_used = sorted(set(hints_used) | set(used))
                report = fidelity.check(tree, expected)
                recorder.stage("hints", attempt=attempt, hints_used=used, **report.summary())

        # Tier 4: LLM-proposed hints for NOVEL gaps (only if deterministic tiers
        # can't close the gap within the salvage cap).
        if (
            not report.ok(threshold)
            and not report.violations
            and report.missing_spans
            and gateway is not None
            and fidelity.span_tokens(report.missing_spans) / max(1, report.total_tokens) > salvage_cap
        ):
            try:
                proposed = _llm_propose_hints(gateway, result.artifacts, report.missing_spans)
            except Exception as exc:  # spend cap, provider error — degrade, never crash the fleet
                log.warning("LLM repair tier unavailable for %s: %s", meta.source_key, exc)
                proposed = []
            if proposed:
                remaining, _ = fidelity.apply_hints(tree, report.missing_spans, proposed)
                new_llm_hints = proposed
                llm_assisted = True
                report = fidelity.check(tree, expected)
                recorder.stage("llm_repair", attempt=attempt, hints_proposed=len(proposed), **report.summary())

        # Tier 2: bounded salvage for small residual gaps.
        if not report.ok(threshold) and not report.violations and report.missing_spans:
            residual_share = fidelity.span_tokens(report.missing_spans) / max(1, report.total_tokens)
            if residual_share <= salvage_cap:
                recovered_spans += fidelity.salvage(tree, report.missing_spans)
                report = fidelity.check(tree, expected)
                recorder.stage("salvage", attempt=attempt, recovered=recovered_spans, **report.summary())

        if report.ok(threshold):
            break
        if report.violations:
            break  # structural contract bugs: retrying cannot help
        if report.coverage <= last_attempt_coverage:
            break  # deterministic no-progress: further attempts are identical
        last_attempt_coverage = report.coverage

    if report is None or not report.ok(threshold):
        # ---- exhaustion: nothing persisted, loudly visible ------------------
        detail = report.summary() if report else {"coverage": 0.0}
        log.error(
            "ingest NOT fully successful for %s: coverage %.4f after %d attempts — %s",
            meta.source_key,
            detail.get("coverage", 0.0),
            max_attempts,
            detail,
        )
        with engine.begin() as conn:
            l0_events.emit(
                conn,
                layer="l1",
                kind="IngestFidelityFailed",
                subject_ref=meta.source_key,
                payload={"source": meta.source_key, "attempts": max_attempts, **detail},
                producer=f"l1.pipeline.{meta.adapter}",
            )
            l0_proposals.create_proposal(
                conn,
                layer="l1",
                kind="ingest_rectification",
                subject_ref=meta.source_key,
                draft={"attempts": max_attempts, **detail},
                rationale=(
                    f"Ingest of {meta.source_key} did not reach the fidelity threshold "
                    f"({threshold:.3%}) after {max_attempts} attempts — manual rectification needed."
                ),
            )
        summary = {"source": meta.source_key, "version": result.version_label, **detail}
        outputs = recorder.finish("failed", summary)
        return {**summary, "status": "not-fully-successful", "run_id": recorder.run_id, "stages": outputs["stages"]}

    # ---- persist (gate green) ------------------------------------------------
    try:
        return _persist(
            engine, store, meta, source_id, previous, result, content_hash, report,
            hints_used, new_llm_hints, recovered_spans, llm_assisted, recorder,
        )
    except Exception as exc:
        recorder.finish("failed", {"source": meta.source_key, "error": str(exc)[:300]})
        raise


def _persist(
    engine: Engine,
    store: ArtifactStore,
    meta: SourceMeta,
    source_id: int,
    previous,
    result: FetchResult,
    content_hash: str,
    report,
    hints_used: list[int],
    new_llm_hints: list[dict],
    recovered_spans: int,
    llm_assisted: bool,
    recorder: RunRecorder,
) -> dict:
    prefix = "public-ok" if meta.license == "open" else "restricted"
    artifact_uris = []
    for artifact in result.artifacts:
        key = f"{prefix}/{meta.source_key}/{result.version_label}/{artifact.name}"
        artifact_uris.append(store.put(key, artifact.content, artifact.content_type))

    public_ok = meta.license == "open"
    tree = result.tree

    with engine.begin() as conn:
        if previous is not None:
            conn.execute(
                source_versions.update().where(source_versions.c.id == previous.id).values(status="superseded")
            )
        version_id = conn.execute(
            source_versions.insert()
            .values(
                source_id=source_id,
                version_label=result.version_label,
                version_kind=result.version_kind,
                as_of_date=result.as_of_date,
                effective_date=result.effective_date,
                s3_uri=artifact_uris[0] if artifact_uris else "",
                content_hash=content_hash,
                status="in_force",
            )
            .returning(source_versions.c.id)
        ).scalar_one()

        clause_rows = persist_tree(conn, version_id, tree, public_ok)
        if clause_rows:
            conn.execute(clauses.insert(), clause_rows)

        new_map = {row["ref"]: row["text_hash"] for row in clause_rows}
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

        now = datetime.now(timezone.utc)
        if hints_used:
            conn.execute(
                parse_hints.update()
                .where(parse_hints.c.id.in_(hints_used))
                .values(times_used=parse_hints.c.times_used + 1, last_used_at=now, last_needed_at=now)
            )
        if new_llm_hints:
            # Learn once: persist gate-passing hints for all future runs, and
            # file a proposal so a maintainer ratifies a permanent adapter fix.
            proposal_id = l0_proposals.create_proposal(
                conn,
                layer="l1",
                kind="parse_hint",
                subject_ref=meta.source_key,
                draft={"hints": new_llm_hints},
                rationale=(
                    f"LLM-proposed parse hints repaired the {meta.source_key} ingest "
                    "(gate-validated). Approve to keep + fold into the adapter; reject to retire."
                ),
            )
            for hint in new_llm_hints:
                conn.execute(
                    parse_hints.insert().values(
                        source_id=source_id,
                        hint={k: hint[k] for k in ("match", "node_type", "label", "ref") if k in hint},
                        origin="llm",
                        status="candidate",
                        proposal_id=proposal_id,
                        times_used=1,
                        last_used_at=now,
                        last_needed_at=now,
                    )
                )

    node_count = sum(1 for n in tree for _ in n.walk())
    recorder.stage("persist", version=result.version_label, nodes=node_count, clauses=len(clause_rows))
    recorder.stage(
        "diff",
        old_version=previous.version_label if previous else None,
        new_version=result.version_label,
        added=len(diff["added"]),
        removed=len(diff["removed"]),
        amended=len(diff["amended"]),
    )
    summary = {
        "source": meta.source_key,
        "version": result.version_label,
        "version_kind": result.version_kind,
        "nodes": node_count,
        "clauses": len(clause_rows),
        "coverage": round(report.coverage, 5),
        "diff": diff,
        "artifacts": artifact_uris,
        "content_hash": content_hash,
    }
    if hints_used:
        summary["hints_used"] = hints_used
    if recovered_spans:
        summary["recovered_spans"] = recovered_spans
    if llm_assisted:
        summary["llm_assisted"] = True
    degraded = bool(hints_used or recovered_spans or llm_assisted)
    if degraded:
        log.warning("ingest of %s needed repair (hints=%s salvage=%d llm=%s) — fix the adapter",
                    meta.source_key, hints_used, recovered_spans, llm_assisted)
    outputs = recorder.finish("warning" if degraded else "succeeded", {**summary, "change": change_kind})
    log.info(
        "ingested %s %s: %d nodes / %d clauses (%s, coverage %.4f)",
        meta.source_key, result.version_label, node_count, len(clause_rows), change_kind, report.coverage,
    )
    return {**summary, "status": change_kind, "run_id": recorder.run_id, "stages": outputs["stages"]}


def persist_tree(
    conn: Connection,
    version_id: int,
    tree: list[DocNode],
    public_ok: bool,
) -> list[dict]:
    """Insert the DocNode tree; return clause-projection rows (not yet inserted)."""
    seq = 0
    clause_rows: list[dict] = []

    def visit(node: DocNode, parent_id: int | None, depth: int, path_parts: list[str]) -> None:
        nonlocal seq
        seq += 1
        payload = "\n".join([node.node_type, node.ref, node.label, node.heading, node.raw_text]).encode()
        node_id = conn.execute(
            doc_nodes.insert()
            .values(
                source_version_id=version_id,
                parent_id=parent_id,
                seq=seq,
                depth=depth,
                node_type=node.node_type,
                ref=node.ref,
                label=node.label,
                heading=node.heading,
                raw_text=node.raw_text,
                source_fragment=node.source_fragment,
                text_hash=sha256(payload),
                public_ok=public_ok,
            )
            .returning(doc_nodes.c.id)
        ).scalar_one()

        crumb = node.heading or node.label or node.ref
        child_path = path_parts + (
            [crumb] if crumb and node.node_type in {"part", "chapter", "section", "group", "schedule"} else []
        )
        for child in node.children:
            visit(child, node_id, depth + 1, child_path)

        if node.node_type in CLAUSE_TYPES and node.ref:
            clause_text = node.subtree_text()
            clause_rows.append(
                {
                    "source_version_id": version_id,
                    "doc_node_id": node_id,
                    "ref": node.ref,
                    "path": " > ".join(p for p in path_parts if p),
                    "ordering": seq,
                    "text": clause_text,
                    "text_hash": sha256(clause_text.encode()),
                    "public_ok": public_ok,
                }
            )

    for root in tree:
        visit(root, None, 0, [])
    return clause_rows
