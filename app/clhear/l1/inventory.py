"""Worker-owned, immutable L1 scope and reconciliation evidence.

The registry is a declared minimum, never an independent publisher inventory.
FINRA collection indexes are discovery inputs, not regulatory documents. This
module has no CLI, request-time ingestion, credential discovery or permission
shortcut. Workers call ``run_inventory_audit``; HTTP readers only call the two
read functions. Audit outputs contain metadata and finding codes, never text.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections import Counter, deque
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import sqlalchemy as sa
from bs4 import BeautifulSoup

from app.clhear.l1 import permissions
from app.clhear.l1.models import BigId, Json, L1_SCHEMA, clauses, doc_nodes, source_versions, sources
from app.clhear.models import runs

SCOPE_VERSION = "2026-09-15.1"
SCOPES = frozenset({"registered", "finra"})
FINRA_CATEGORIES = (
    ("manual", "Manual and governing documents", "https://www.finra.org/rules-guidance/rulebooks"),
    ("governing", "Corporate organization and governing documents", "https://www.finra.org/rules-guidance/rulebooks/corporate-organization"),
    ("rules", "Current FINRA rules", "https://www.finra.org/rules-guidance/rulebooks/finra-rules"),
    ("nasd_archive", "Published NASD rule archive", "https://www.finra.org/rules-guidance/rulebooks/nasd-rules"),
    ("nyse_archive", "Published incorporated NYSE rule archive", "https://www.finra.org/rules-guidance/rulebooks/incorporated-nyse-rules"),
    ("filings", "Rule filings and amendments", "https://www.finra.org/rules-guidance/rule-filings"),
    ("notices", "Regulatory notices", "https://www.finra.org/rules-guidance/notices"),
    ("guidance", "Published interpretive guidance", "https://www.finra.org/rules-guidance/guidance"),
    ("examinations", "Examination and oversight reports", "https://www.finra.org/rules-guidance/guidance/reports"),
    ("enforcement", "Disciplinary actions and enforcement publications", "https://www.finra.org/rules-guidance/oversight-enforcement/disciplinary-actions"),
)
FINRA_BOUNDARIES = {
    "include": ["Manual", "governing documents", "current rules", "published rule archives",
                "filings and amendments", "notices and interpretive guidance", "examination reports",
                "disciplinary/enforcement publications", "linked official attachments"],
    "exclude": ["unrelated website material", "unpublished history", "exhaustive historical rule reconstruction"],
    "attachment_policy": "Only official FINRA attachments linked by an in-scope page; no mirror hosts.",
    "completeness_policy": "Bounded traversal is evidence, not proof of publisher completeness; an exact inventory review is required.",
}
EXPECTED_EDITIONS = {
    "iso/27001-2022": "ISO/IEC 27001:2022",
    "iso/27001-2022-amd1-2024": "ISO/IEC 27001:2022/Amd 1:2024",
    "aicpa/soc2-tsc": "Trust Services Criteria 2017; revised points of focus 2022",
}
metadata = sa.MetaData(schema=L1_SCHEMA)
inventory_snapshots = sa.Table(
    "l1_inventory_snapshots", metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
    sa.Column("scope", sa.Text, nullable=False, index=True),
    sa.Column("scope_version", sa.Text, nullable=False),
    sa.Column("inventory_hash", sa.Text, nullable=False, unique=True),
    sa.Column("definition", Json, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
)
inventory_audits = sa.Table(
    "l1_inventory_audits", metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
    sa.Column("inventory_id", sa.Uuid(as_uuid=False), sa.ForeignKey(f"{L1_SCHEMA}.l1_inventory_snapshots.id"), nullable=False),
    sa.Column("scope", sa.Text, nullable=False, index=True),
    sa.Column("job_id", sa.Text, nullable=False, index=True),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("summary", Json, nullable=False),
)
inventory_reviews = sa.Table(
    "l1_inventory_reviews", metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("inventory_hash", sa.Text, nullable=False, index=True),
    sa.Column("evidence_ref", sa.Text, nullable=False),
    sa.Column("approved_by", sa.Text, nullable=False),
    sa.Column("approved", sa.Boolean, nullable=False),
    sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
)
artifact_reviews = sa.Table(
    "l1_artifact_reviews", metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("source_key", sa.Text, nullable=False, index=True),
    sa.Column("content_hash", sa.Text, nullable=False, index=True),
    sa.Column("publisher_edition", sa.Text, nullable=False),
    sa.Column("canonical_url", sa.Text, nullable=False),
    sa.Column("coverage", sa.Text, nullable=False),
    sa.Column("evidence_ref", sa.Text, nullable=False),
    sa.Column("approved_by", sa.Text, nullable=False),
    sa.Column("approved", sa.Boolean, nullable=False),
    sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("coverage in ('full','preview','excerpt')", name="l1_artifact_coverage_check"),
)


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value) -> str:
    return _hash(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


def _iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _recent(value, now, *, hours=24):
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return at.tzinfo is not None and now - timedelta(hours=hours) <= at <= now + timedelta(minutes=5)
    except (AttributeError, TypeError, ValueError):
        return False


def _scope(scope):
    if scope not in SCOPES:
        raise ValueError("scope must be registered or finra")
    return scope


def _finding(code, detail, **extra):
    return {"code": code, "detail": detail, **extra}


def _available(engine) -> bool:
    with engine.connect() as conn:
        schema = L1_SCHEMA if engine.dialect.name == "postgresql" else None
        return sa.inspect(conn).has_table(inventory_audits.name, schema=schema)


def _declared_entries(scope):
    from app.clhear.l1.registry_etoro import S
    entries = {e["key"]: dict(e) for e in S if e["key"] != "finra/rulebook"
               and (scope == "registered" or e["key"].startswith("finra/"))}
    if scope == "registered":
        # Include real starter declarations outside S, without fetching them.
        from app.clhear.l1.fleet import fleet_plan
        for _, adapter in fleet_plan():
            meta = adapter.meta()
            if meta.source_key not in entries and meta.source_key != "finra/rulebook":
                entries[meta.source_key] = {
                    "key": meta.source_key, "name": meta.name, "canonical_url": meta.canonical_url,
                    "adapter": meta.adapter, "license": meta.license, "family": meta.family_key,
                    "issuer": meta.issuer, "kind": meta.kind, "jurisdiction": meta.jurisdiction,
                }
    return entries


def _url(value):
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname not in {"www.finra.org", "finra.org"} or port not in (None, 443):
        return None
    if parsed.username or parsed.password or ".." in unquote(parsed.path).split("/"):
        return None
    # Only observed numeric pagination/year filters, never arbitrary search or
    # tracking queries that can turn traversal into unbounded duplicate pages.
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(k not in {"page", "year"} or not v.isdigit() for k, v in pairs):
        return None
    return urlunparse(("https", "www.finra.org", parsed.path.rstrip("/") or "/", "", urlencode(sorted(pairs)), ""))


def _in_scope_url(value, *, attachment=False):
    parsed = urlparse(value)
    return (parsed.path.startswith(("/rules-guidance/rulebooks", "/rules-guidance/rule-filings",
                                    "/rules-guidance/notices", "/rules-guidance/guidance",
                                    "/rules-guidance/oversight-enforcement/disciplinary-actions"))
            or (attachment and parsed.path.startswith("/sites/default/files/")))


def _source_key(url):
    match = re.fullmatch(r"/rules-guidance/rulebooks/finra-rules/(\d{4,5}[A-Z]?)", urlparse(url).path)
    if match:
        return f"finra/rule/{match.group(1)}"
    return "finra/document/" + _hash(url.encode())[:24]


def _discovered_entry(url, category):
    key = _source_key(url)
    rule = key.startswith("finra/rule/")
    return {
        "key": key, "family": "us-broker-dealer", "name": "FINRA Rule " + key.rsplit("/", 1)[-1] if rule else "FINRA publication " + urlparse(url).path.rsplit("/", 1)[-1],
        "short_name": "FINRA " + key.rsplit("/", 1)[-1], "canonical_url": url,
        "kind": "regulation" if rule else "guidance", "issuer": "FINRA", "publisher": "FINRA",
        "jurisdiction": "US", "license": "restricted", "rights_basis": "derived_only",
        "adapter": "finra" if rule else "unconfigured_finra_document", "relation": "supplements",
        "tier": "binding" if rule else "informative", "topics": ["us", "finra"], "registry_ids": [],
        "wave": 2, "fetch": {"url": url, "channel": "finra"}, "discovered_category": category,
    }


def _fetch_discovery(url):
    """Bounded official discovery; no redirects, shared public cache or fallback.

    Replay reads existing fixtures only. It cannot establish publisher freshness.
    Bodies are stored by the permission-gated caller under the restricted prefix.
    """
    from app.clhear.l1 import http
    if os.environ.get("CLHEAR_HTTP_MODE", "replay") == "replay":
        path = http._fixture_path(url)
        if not path.exists():
            raise http.FixtureMissing("Discovery fixture unavailable")
        return http._read_fixture(path), "fixture"
    import httpx
    with httpx.stream("GET", url, headers={"User-Agent": http.USER_AGENT}, timeout=20, follow_redirects=False) as response:
        if response.is_redirect:
            raise ValueError("Publisher redirect requires an independently validated official discovery URL")
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > 2 * 1024 * 1024:
                raise ValueError("Discovery page exceeds the 2 MiB limit")
        if not body:
            raise ValueError("Publisher returned an empty discovery page")
        return bytes(body), "live"


def _discover(engine, store):
    max_pages = max(1, min(int(os.environ.get("CLHEAR_L1_DISCOVERY_MAX_PAGES", "100")), 10000))
    max_documents = max(1, min(int(os.environ.get("CLHEAR_L1_DISCOVERY_MAX_DOCUMENTS", "10000")), 50000))
    seed_map = {_url(url): (key, label) for key, label, url in FINRA_CATEGORIES}
    queue = deque((url, key, f"finra/catalog/{key}") for url, (key, _) in seed_map.items())
    seen, entries, pages, findings = set(), {}, [], []
    categories = {key: {"key": key, "name": label, "url": url, "status": "not_checked", "documents": 0}
                  for key, label, url in FINRA_CATEGORIES}
    while queue and len(seen) < max_pages and len(entries) < max_documents:
        url, category, key = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        with engine.connect() as conn:
            checks = {op: permissions.decision(conn, key, op) for op in ("acquire", "store", "parse")}
        blocked = [op for op, choice in checks.items() if not choice["allowed"]]
        if blocked:
            findings.append(_finding("discovery_permission_blocked", "Official discovery requires explicit permission.", source_key=key, url=url, category=category, operations=blocked))
            categories[category]["status"] = "permission_blocked"
            continue
        try:
            body, origin = _fetch_discovery(url)
            digest = _hash(body)
            artifact_uri = store.put(f"restricted/_l1_inventory/{digest}.bin", body, "application/octet-stream")
            page = {"url": url, "source_key": key, "category": category, "sha256": digest,
                    "byte_count": len(body), "artifact_uri": artifact_uri, "origin": origin,
                    "publisher_checked_at": datetime.now(timezone.utc).isoformat() if origin == "live" else None}
            pages.append(page)
            if origin != "live":
                findings.append(_finding("discovery_not_live", "Fixture evidence cannot establish current publisher inventory.", source_key=key, url=url, category=category))
            if body[:5] == b"%PDF-":
                continue  # attachments are inventoried; this is not an importer.
            soup = BeautifulSoup(body, "html.parser")
            area = soup.select_one("main") or soup.body or soup
            if area.select("form select, [data-drupal-views-infinite-scroll-content-wrapper]"):
                findings.append(_finding("dynamic_enumeration_unverified", "Publisher filters/dynamic results require explicit complete enumeration evidence.", url=url, category=category))
            accepted_links = 0
            for link in area.find_all("a", href=True):
                raw = urljoin(url, str(link["href"]))
                target = _url(raw)
                if not target:
                    if "next" in (link.get("rel") or []):
                        findings.append(_finding("unsupported_pagination", "Next-page URL is outside permitted pagination or official host boundaries.", url=url, category=category))
                    continue
                if not _in_scope_url(target, attachment=True):
                    continue
                accepted_links += 1
                target_seed = seed_map.get(target)
                target_category = target_seed[0] if target_seed else category
                target_key = f"finra/catalog/{target_category}" if target_seed else _source_key(target)
                if not target_seed and not urlparse(target).query:
                    entry = _discovered_entry(target, target_category)
                    entries.setdefault(entry["key"], entry)
                if target not in seen:
                    queue.append((target, target_category, target_key))
                if len(entries) >= max_documents:
                    break
            if url in seed_map and not accepted_links:
                findings.append(_finding("empty_discovery_index", "No in-scope documents or pagination found in this collection index.", url=url, category=category))
            categories[category]["status"] = "checked"
        except Exception as exc:
            # Avoid echoing responses or vendor exception text into metadata.
            findings.append(_finding("discovery_failed", "Publisher discovery failed; retry or adapter investigation required.", source_key=key, url=url, category=category, error_type=type(exc).__name__))
            categories[category]["status"] = "failed"
    pending_pages = len({url for url, _, _ in queue if url not in seen})
    if pending_pages:
        findings.append(_finding("discovery_limit", "Unvisited publisher pages remain; the inventory is incomplete.", pending_pages=pending_pages, max_pages=max_pages, max_documents=max_documents))
    for item in entries.values():
        categories[item["discovered_category"]]["documents"] += 1
    complete = not findings and bool(entries) and not pending_pages
    return entries, {"complete": complete, "checked_at": datetime.now(timezone.utc).isoformat(),
                     "categories": list(categories.values()), "pages": pages, "findings": findings,
                     "limits": {"max_pages": max_pages, "max_documents": max_documents}}


def _latest(engine, scope):
    with engine.connect() as conn:
        row = conn.execute(sa.select(inventory_audits).where(inventory_audits.c.scope == scope)
                           .order_by(inventory_audits.c.finished_at.desc(), inventory_audits.c.id.desc()).limit(1)).mappings().first()
    return dict(row) if row else None


def planned_entries(engine, scope="finra", adapter_key=None):
    """Discovered supported documents for the existing fleet adapter factory."""
    _scope(scope)
    if not _available(engine):
        return []
    prior = _latest(engine, scope)
    if not prior:
        return []
    with engine.connect() as conn:
        definition = conn.execute(sa.select(inventory_snapshots.c.definition)
                                  .where(inventory_snapshots.c.id == prior["inventory_id"])).scalar_one()
    return [entry for entry in definition["entries"] if entry.get("discovered_category")
            and entry.get("adapter") == "finra" and (adapter_key is None or adapter_key == "finra")]


def record_scope_review(engine, inventory_hash, evidence_ref, approved_by, approved):
    """Append reviewed external enumeration evidence from trusted worker code.

    This never grants source permissions. A review binds the exact frozen
    finite list, scope version and category boundaries. Revocation overrides an
    older approval. No reviews are seeded or inferred from a successful crawl.
    An Engine owns its transaction; a Connection joins the caller's transaction.
    """
    if not re.fullmatch(r"[a-f0-9]{64}", inventory_hash or ""):
        raise ValueError("inventory_hash must identify one frozen SHA-256 inventory")
    if type(approved) is not bool or not isinstance(evidence_ref, str) or not evidence_ref.strip() or not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError("Explicit approval, reviewer and external enumeration evidence are required")
    with (engine.begin() if isinstance(engine, sa.engine.Engine) else nullcontext(engine)) as conn:
        if conn.execute(sa.select(inventory_snapshots.c.id).where(inventory_snapshots.c.inventory_hash == inventory_hash)).first() is None:
            raise ValueError("Review must reference an existing frozen inventory")
        review = conn.execute(inventory_reviews.insert().values(inventory_hash=inventory_hash, evidence_ref=evidence_ref.strip(),
                              approved_by=approved_by.strip(), approved=approved).returning(inventory_reviews)).mappings().one()
    return {**dict(review), "reviewed_at": _iso(review["reviewed_at"])}


def _review(conn, digest):
    row = conn.execute(sa.select(inventory_reviews).where(inventory_reviews.c.inventory_hash == digest)
                       .order_by(inventory_reviews.c.id.desc()).limit(1)).mappings().first()
    return {**dict(row), "reviewed_at": _iso(row["reviewed_at"])} if row else None


def record_artifact_review(engine, source_key, content_hash, publisher_edition, canonical_url,
                           coverage="full", evidence_ref=None, approved_by=None, approved=False):
    """Record human-reviewed artifact identity from trusted worker code only.

    Exact acquired bytes, edition, publisher reference and extent must all be
    reviewed. A full approval is neither an acquisition/display permission nor
    a parser pass. Preview/excerpt evidence can never stand for a full document.
    An uploaded filename or a successful sign-in is not identity evidence.
    An Engine owns its transaction; a Connection joins the caller's transaction.
    """
    if not isinstance(source_key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}", source_key):
        raise ValueError("An exact source key is required")
    if not re.fullmatch(r"[a-f0-9]{64}", content_hash or ""):
        raise ValueError("content_hash must identify the acquired artifact set")
    if (type(approved) is not bool or coverage not in {"full", "preview", "excerpt"}
            or any(not isinstance(v, str) or not v.strip() for v in (publisher_edition, canonical_url, evidence_ref, approved_by))):
        raise ValueError("Explicit edition, coverage, approval, reviewer and evidence are required")
    parsed = urlparse(canonical_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("An HTTPS publisher canonical reference is required")
    with (engine.begin() if isinstance(engine, sa.engine.Engine) else nullcontext(engine)) as conn:
        row = conn.execute(artifact_reviews.insert().values(source_key=source_key, content_hash=content_hash,
                           publisher_edition=publisher_edition.strip(), canonical_url=canonical_url.strip(), coverage=coverage,
                           evidence_ref=evidence_ref.strip(), approved_by=approved_by.strip(), approved=approved)
                           .returning(artifact_reviews)).mappings().one()
    return {**dict(row), "reviewed_at": _iso(row["reviewed_at"])}


def _artifact_review(conn, source_key, content_hash):
    row = conn.execute(sa.select(artifact_reviews).where(artifact_reviews.c.source_key == source_key,
                       artifact_reviews.c.content_hash == content_hash).order_by(artifact_reviews.c.id.desc()).limit(1)).mappings().first()
    return {**dict(row), "reviewed_at": _iso(row["reviewed_at"])} if row else None


def _store_key(store, uri):
    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        if parsed.netloc != getattr(store, "bucket", None):
            raise ValueError("Artifact belongs to a different store")
        return unquote(parsed.path.lstrip("/"))
    if parsed.scheme == "file" and not parsed.netloc and hasattr(store, "base_dir"):
        return str(Path(unquote(parsed.path)).resolve().relative_to(store.base_dir))
    raise ValueError("Artifact URI is outside the configured store")


_NODE_FIELDS = ("id", "parent_id", "seq", "depth", "node_type", "ref", "label", "heading", "raw_text", "source_fragment", "text_hash")
_CLAUSE_FIELDS = ("id", "doc_node_id", "ref", "ordering", "text", "text_hash", "span_start", "span_end")


def _projection_digest(nodes, clause_rows):
    return _digest({"nodes": [{k: n[k] for k in _NODE_FIELDS} for n in nodes],
                    "clauses": [{k: c[k] for k in _CLAUSE_FIELDS} for c in clause_rows]})


def _audit_source(conn, store, entry, now):
    key = entry["key"]
    findings = []
    out = {"source_key": key, "name": entry["name"], "canonical_url": entry.get("canonical_url", ""),
           "expected_edition": EXPECTED_EDITIONS.get(key), "source_version_id": None, "version_label": None,
           "content_hash": None, "ingested_at": None, "publisher_checked_at": None, "permissions": {},
           "artifacts": [], "findings": findings, "verified": False, "node_count": 0, "clause_count": 0}
    if permissions.required_for(entry):
        out["permissions"] = {op: permissions.decision(conn, key, op, now=now)
                              for op in ("acquire", "store", "parse", "display_internal", "display_public", "infer", "embed")}
        for op in ("acquire", "store", "parse"):
            choice = out["permissions"][op]
            if not choice["allowed"]:
                code = "permission_unverified" if choice["reason"] in {"missing_permission", "not_yet_valid"} else "permission_blocked"
                findings.append(_finding(code, "Required operation has no current approval.", operation=op, reason=choice["reason"]))
    source = conn.execute(sa.select(sources).where(sources.c.key == key)).mappings().first()
    versions = list(conn.execute(sa.select(source_versions).where(source_versions.c.source_id == source["id"], source_versions.c.status == "in_force")
                                .order_by(source_versions.c.id.desc())).mappings()) if source else []
    if not versions:
        findings.append(_finding("awaiting_artifact", "No current imported document exists for this expected source."))
        return _source_status(out)
    if len(versions) != 1:
        findings.append(_finding("duplicate_current_versions", "Several versions are marked current; select the publisher state explicitly.", version_ids=[v["id"] for v in versions]))
    version = versions[0]
    out.update(source_version_id=version["id"], version_label=version["version_label"], content_hash=version["content_hash"], ingested_at=_iso(version["retrieved_at"]))
    artifact_review = _artifact_review(conn, key, version["content_hash"])
    out["artifact_review"] = artifact_review
    out["node_count"] = conn.execute(sa.select(sa.func.count()).select_from(doc_nodes).where(doc_nodes.c.source_version_id == version["id"])).scalar_one()
    out["clause_count"] = conn.execute(sa.select(sa.func.count()).select_from(clauses).where(clauses.c.source_version_id == version["id"])).scalar_one()
    ledger = list(conn.execute(sa.select(runs.c.id, runs.c.outputs).where(runs.c.inputs["source"].as_string() == key)
                              .order_by(runs.c.created_at.desc(), runs.c.id.desc())).mappings())
    matched = [row for row in ledger if (row["outputs"] or {}).get("source_version_id") == version["id"]
               and (row["outputs"] or {}).get("content_hash") == version["content_hash"]]
    evidence = next((row["outputs"] for row in matched if row["outputs"].get("artifact_manifest")), {})
    checked = [str(row["outputs"]["publisher_checked_at"]) for row in matched if row["outputs"].get("publisher_checked_at")]
    out["publisher_checked_at"] = max(checked, default=None)
    if not out["publisher_checked_at"]:
        findings.append(_finding("publisher_check_unverified", "No successful live publisher check is bound to this exact version and artifact hash."))
    else:
        if not _recent(out["publisher_checked_at"], now, hours=26):
            findings.append(_finding("publisher_check_overdue", "The version's publisher check is invalid, in the future, or older than the daily freshness window."))
    reviewed_identity = bool(artifact_review and artifact_review["approved"]
                             and artifact_review["canonical_url"] == entry.get("canonical_url"))
    if out["expected_edition"] and (not reviewed_identity or artifact_review["publisher_edition"] != out["expected_edition"]):
        findings.append(_finding("edition_unverified", "Acquired bytes are not independently bound to the required publisher edition."))
    if entry.get("adapter") == "restricted_file" and not reviewed_identity:
        findings.append(_finding("artifact_identity_unverified", "An explicit review must bind the actual artifact hash to its publisher, edition and extent."))
    if artifact_review and artifact_review["coverage"] != "full":
        findings.append(_finding("partial_artifact", "A preview or excerpt cannot fulfill a complete-document requirement.", coverage=artifact_review["coverage"]))
    if artifact_review and not artifact_review["approved"]:
        findings.append(_finding("artifact_review_not_approved", "The latest artifact identity review does not approve this document."))
    if artifact_review and artifact_review["canonical_url"] != entry.get("canonical_url"):
        findings.append(_finding("artifact_review_reference_mismatch", "The artifact review refers to a different publisher document."))
    if source["canonical_url"] != entry.get("canonical_url", ""):
        findings.append(_finding("source_reference_mismatch", "Stored source metadata differs from the declared publisher reference."))
    if not evidence.get("parser_identity"):
        findings.append(_finding("parser_provenance_unverified", "No parser identity is bound to this exact version."))
    manifest = evidence.get("artifact_manifest")
    if manifest and (not isinstance(manifest, list) or any(not isinstance(item, dict) for item in manifest)):
        findings.append(_finding("artifact_manifest_invalid", "The bound run does not contain a valid artifact manifest."))
        manifest = None
    if not manifest:
        findings.append(_finding("artifact_manifest_unverified", "Legacy version lacks a complete version-bound artifact manifest; rerun through the worker."))
    if key.startswith("finra/rule/") and manifest:
        observations = [observation for row in matched for observation in row["outputs"].get("fetch_evidence", [])
                        if observation.get("origin") in {"live", "revalidated"}]
        if any(not any(observation.get("sha256") == item.get("sha256")
                       and _url(observation.get("url", "")) == _url(entry.get("canonical_url", ""))
                       for observation in observations) for item in manifest):
            findings.append(_finding("publisher_provenance_unverified", "Original hashes lack matching live acquisition evidence for this official rule URL."))
    if entry.get("adapter") == "unconfigured_finra_document":
        findings.append(_finding("parser_not_configured", "Discovered FINRA document requires a validated document adapter."))
    # Metadata is inspectable even when permission has not been granted. Do not
    # read protected original or parsed text merely to satisfy an audit.
    if any(f["code"] in {"permission_unverified", "permission_blocked"} for f in findings):
        return _source_status(out)
    artifacts = []
    from app.clhear.l1.adapters.base import Artifact, DocNode
    for item in manifest or []:
        record = {k: item.get(k) for k in ("name", "uri", "sha256", "byte_count", "content_type")}
        record["verified"] = False
        out["artifacts"].append(record)
        try:
            blob = store.get(_store_key(store, item.get("uri", "")))
            if blob is None:
                findings.append(_finding("artifact_missing_or_unreadable", "A manifest artifact cannot be read from the configured store.", name=item.get("name")))
                continue
            actual_hash = _hash(blob)
            record.update(observed_sha256=actual_hash, observed_byte_count=len(blob))
            if not blob or actual_hash != item.get("sha256") or len(blob) != item.get("byte_count"):
                findings.append(_finding("artifact_hash_mismatch", "Stored original differs from the acquired manifest.", name=item.get("name")))
                continue
            record["verified"] = True
            artifacts.append(Artifact(name=item["name"], content=blob, content_type=item.get("content_type") or "application/octet-stream"))
        except Exception as exc:
            findings.append(_finding("artifact_store_error", "Artifact retrieval failed without changing stored history.", name=item.get("name"), error_type=type(exc).__name__))
    if manifest and len(artifacts) == len(manifest):
        names = [a.name for a in artifacts]
        if len(set(names)) != len(names) or _hash(b"".join(a.content for a in sorted(artifacts, key=lambda a: a.name))) != version["content_hash"]:
            findings.append(_finding("artifact_composite_mismatch", "Complete original artifact set differs from the version hash."))
    nodes = list(conn.execute(sa.select(doc_nodes).where(doc_nodes.c.source_version_id == version["id"]).order_by(doc_nodes.c.seq)).mappings())
    clause_rows = list(conn.execute(sa.select(clauses).where(clauses.c.source_version_id == version["id"]).order_by(clauses.c.ordering)).mappings())
    out["projection_hash"] = _projection_digest(nodes, clause_rows)
    if not nodes or not clause_rows:
        findings.append(_finding("empty_projection", "Expected document has no complete addressable node and clause projection."))
        return _source_status(out)
    tree_nodes = {n["id"]: DocNode(node_type=n["node_type"], ref=n["ref"], label=n["label"], heading=n["heading"], raw_text=n["raw_text"], source_fragment=n["source_fragment"]) for n in nodes}
    roots = []
    try:
        for n in nodes:
            if n["parent_id"] is None:
                roots.append(tree_nodes[n["id"]])
            else:
                tree_nodes[n["parent_id"]].children.append(tree_nodes[n["id"]])
        from app.clhear.l1.spans import canonical_text
        canonical = canonical_text(roots)
        # Disconnected cycles must also be detected; they are not reachable
        # through canonical_text(roots).
        visited = [id(n) for root in roots for n in root.walk()]
        if len(visited) != len(nodes) or len(set(visited)) != len(nodes):
            raise ValueError("Disconnected or repeated nodes")
        if _hash(canonical.encode()) != evidence.get("canonical_text_hash"):
            findings.append(_finding("canonical_hash_mismatch", "Stored ordered document text differs from the ingestion evidence or lacks its digest."))
        if any(_hash("\n".join(n[k] for k in ("node_type", "ref", "label", "heading", "raw_text")).encode()) != n["text_hash"] for n in nodes):
            findings.append(_finding("node_hash_mismatch", "Stored node content differs from its recorded digest."))
        for clause in clause_rows:
            start, end = clause["span_start"], clause["span_end"]
            node = tree_nodes.get(clause["doc_node_id"])
            if (node is None or start is None or end is None or not 0 <= start <= end <= len(canonical)
                    or canonical[start:end] != clause["text"] or node.subtree_text() != clause["text"]
                    or _hash(clause["text"].encode()) != clause["text_hash"]):
                findings.append(_finding("clause_roundtrip_mismatch", "Stored clause, subtree, hash or character span disagree.", clause_id=clause["id"]))
        if key.startswith("finra/rule/") and artifacts and len(artifacts) == len(manifest or []):
            from app.clhear.l1.adapters.finra import validate_tree
            violations = validate_tree(roots, artifacts, key, entry.get("canonical_url", ""))
            if violations:
                findings.append(_finding("publisher_text_mismatch", "Stored FINRA wording, ordering or hierarchy differs from the preserved official original.", violation_count=len(violations)))
        else:
            findings.append(_finding("independent_text_comparison_unverified", "Exact publisher-original comparison is not yet independently certified for this adapter."))
    except (KeyError, ValueError, RecursionError):
        findings.append(_finding("invalid_document_tree", "Stored tree is disconnected, cyclic, or cannot be reconstructed."))
    return _source_status(out)


def _source_status(out):
    codes = {f["code"] for f in out["findings"]}
    if "permission_blocked" in codes:
        status = "permission_blocked"
    elif "permission_unverified" in codes:
        status = "permissions_unverified"
    elif "awaiting_artifact" in codes:
        status = "awaiting_artifact"
    else:
        status = "gaps" if codes else "verified"
    return {**out, "status": status, "verified": not codes}


def run_inventory_audit(engine, store, *, job_id, scope="registered", discover=False):
    """Reconcile the worker's authoritative engine; never seed or mutate corpus.

    A no-discovery post-import audit carries the previous discovery evidence
    forward, including its timestamp and unresolved failures. It does not make
    a cached inventory freshly checked. All audit rows are append-only.
    """
    _scope(scope)
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("The owning worker job_id is required")
    started = datetime.now(timezone.utc)
    tick = time.monotonic()
    entries = _declared_entries(scope)
    prior = _latest(engine, scope)
    discovery = {"complete": False, "checked_at": None, "categories": [
        {"key": key, "name": label, "url": url, "status": "not_checked", "documents": 0} for key, label, url in FINRA_CATEGORIES],
        "pages": [], "findings": [_finding("discovery_not_run", "Publisher collections have not been enumerated by the worker.")]}
    discovered = {}
    if prior:
        discovery = prior["summary"]["discovery"]
        with engine.connect() as conn:
            old = conn.execute(sa.select(inventory_snapshots.c.definition).where(inventory_snapshots.c.id == prior["inventory_id"])).scalar_one()
        discovered = {e["key"]: e for e in old["entries"] if e.get("discovered_category")}
    if scope == "registered":
        finra_prior = _latest(engine, "finra")
        if finra_prior and (not prior or finra_prior["finished_at"] > prior["finished_at"]):
            discovery = finra_prior["summary"]["discovery"]
            with engine.connect() as conn:
                finra_definition = conn.execute(sa.select(inventory_snapshots.c.definition)
                                     .where(inventory_snapshots.c.id == finra_prior["inventory_id"])).scalar_one()
            discovered.update({e["key"]: e for e in finra_definition["entries"] if e.get("discovered_category")})
    if discover:
        new_entries, discovery = _discover(engine, store)
        # A failed/partial crawl cannot silently remove previously expected
        # documents from the denominator. Removal requires a new scope review.
        discovered.update(new_entries)
    for key, entry in discovered.items():
        entries.setdefault(key, entry)
    definition = {"scope": scope, "scope_version": SCOPE_VERSION, "boundaries": FINRA_BOUNDARIES,
                  "categories": [{"key": k, "name": n, "url": u} for k, n, u in FINRA_CATEGORIES],
                  "required_editions": {key: edition for key, edition in EXPECTED_EDITIONS.items() if key in entries},
                  "entries": [entries[key] for key in sorted(entries)]}
    digest = _digest(definition)
    with engine.begin() as conn:
        inventory_id = conn.execute(sa.select(inventory_snapshots.c.id).where(inventory_snapshots.c.inventory_hash == digest)).scalar()
        if inventory_id is None:
            inventory_id = str(uuid.uuid4())
            # Concurrent worker audits may freeze the same inventory. The
            # existing unique key is the arbiter; upsert is dialect-specific.
            if engine.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            conn.execute(insert(inventory_snapshots).values(id=inventory_id, scope=scope, scope_version=SCOPE_VERSION,
                         inventory_hash=digest, definition=definition).on_conflict_do_nothing(index_elements=["inventory_hash"]))
            inventory_id = conn.execute(sa.select(inventory_snapshots.c.id).where(inventory_snapshots.c.inventory_hash == digest)).scalar_one()
    with engine.connect() as conn:
        evidence = [_audit_source(conn, store, e, started) for e in definition["entries"]]
        review = _review(conn, digest)
        actual_keys = set(conn.execute(sa.select(sources.c.key)).scalars())
    findings = list(discovery["findings"])
    outside = sorted(key for key in actual_keys - entries.keys() - {"finra/rulebook"}
                     if scope == "registered" or key.startswith("finra/"))
    if outside:
        findings.append(_finding("outside_declared_scope", "Existing sources are outside this declared inventory and need scope classification.", source_keys=outside))
    if not review or not review["approved"]:
        findings.append(_finding("scope_review_required", "An independent reviewed publisher inventory must confirm the exact categories and expected document list."))
    full_scope_verified = bool(review and review["approved"] and discovery["complete"] and not outside)
    count = Counter(f["code"] for e in evidence for f in e["findings"])
    verified = sum(e["verified"] for e in evidence)
    finished = datetime.now(timezone.utc)
    summary = {"audit_id": str(uuid.uuid4()), "job_id": job_id, "scope": scope, "scope_version": SCOPE_VERSION,
               "inventory_hash": digest, "audited_at": finished.isoformat(), "duration_ms": round((time.monotonic() - tick) * 1000),
               "status": "verified" if full_scope_verified and verified == len(evidence) else "gaps",
               "known_expected": len(evidence), "known_expected_is_lower_bound": not full_scope_verified,
               "verified": verified, "unresolved": len(evidence) - verified, "discovery_complete": discovery["complete"],
               "full_scope_verified": full_scope_verified, "scope_review": review, "discovery": discovery,
               "findings": findings, "sources": evidence, "counts": dict(count), "current_binding_valid": True,
               "bindings_hash": _digest([{k: e.get(k) for k in ("source_key", "source_version_id", "content_hash", "projection_hash", "permissions", "artifact_review")} for e in evidence]),
               "collection_sources": [{"source_key": "finra/rulebook", "status": "discovery_index", "counts_as_document": False,
                                       "detail": "A landing-page import does not supply the FINRA rulebook's constituent documents."}]}
    with engine.begin() as conn:
        conn.execute(inventory_audits.insert().values(id=summary["audit_id"], inventory_id=inventory_id,
                     scope=scope, job_id=job_id, started_at=started, finished_at=finished, summary=summary))
    return summary


def inventory_summary(engine, scope="registered"):
    """Latest immutable audit, plus read-time checks that prevent stale passes."""
    _scope(scope)
    blank = {"status": "not_run", "scope": scope, "scope_version": SCOPE_VERSION, "audit_id": None, "job_id": None,
             "inventory_hash": None, "audited_at": None, "known_expected": len(_declared_entries(scope)),
             "known_expected_is_lower_bound": True, "verified": 0, "unresolved": len(_declared_entries(scope)),
             "discovery_complete": False, "full_scope_verified": False, "current_binding_valid": False,
             "findings": [], "sources": [], "counts": {}, "discovery": {"complete": False, "categories": [], "pages": [], "findings": [], "checked_at": None}}
    if not _available(engine):
        return {**blank, "status": "unavailable", "reason": "migration_required"}
    row = _latest(engine, scope)
    if not row:
        return blank
    summary = row["summary"]
    invalid = []
    with engine.connect() as conn:
        for source in summary["sources"]:
            current = list(conn.execute(sa.select(source_versions.c.id, source_versions.c.content_hash)
                         .join(sources, sources.c.id == source_versions.c.source_id)
                         .where(sources.c.key == source["source_key"], source_versions.c.status == "in_force")).mappings())
            expected = [(source["source_version_id"], source["content_hash"])] if source["source_version_id"] is not None else []
            if [(v["id"], v["content_hash"]) for v in current] != expected:
                invalid.append(source["source_key"])
            review_now = _artifact_review(conn, source["source_key"], source["content_hash"]) if source["content_hash"] else None
            if (source.get("artifact_review") or {}).get("id") != (review_now or {}).get("id"):
                invalid.append(source["source_key"])
            for op in ("acquire", "store", "parse"):
                before = source.get("permissions", {}).get(op)
                if before:
                    now = permissions.decision(conn, source["source_key"], op)
                    if (before["allowed"], before["permission_id"]) != (now["allowed"], now["permission_id"]):
                        invalid.append(source["source_key"])
        review = _review(conn, summary["inventory_hash"])
    if not review or not review["approved"]:
        summary = {**summary, "full_scope_verified": False, "known_expected_is_lower_bound": True}
    if not _recent(summary["audited_at"], datetime.now(timezone.utc), hours=26):
        invalid.append("audit_overdue")
    # Registry edits also invalidate an old frozen report, without a GET
    # endpoint making writes or enumerating publisher websites.
    declared = _declared_entries(scope)
    with engine.connect() as conn:
        frozen = conn.execute(sa.select(inventory_snapshots.c.definition).where(inventory_snapshots.c.id == row["inventory_id"])).scalar_one()
    old_entries = {e["key"]: e for e in frozen["entries"]}
    editions = {key: edition for key, edition in EXPECTED_EDITIONS.items() if key in old_entries}
    if (frozen["scope_version"] != SCOPE_VERSION or frozen.get("required_editions") != editions
            or any(old_entries.get(k) != v for k, v in declared.items())):
        invalid.append("declared_scope_changed")
    if invalid:
        return {**summary, "status": "stale", "current_binding_valid": False, "full_scope_verified": False,
                "findings": [*summary["findings"], _finding("audit_binding_changed", "Stored evidence must be rerun against current source versions, permissions, scope or freshness.", source_keys=sorted(set(invalid)))]}
    if not summary["full_scope_verified"] and summary["status"] == "verified":
        summary = {**summary, "status": "gaps"}
    return {**summary, "current_binding_valid": True}


def source_inventory_evidence(engine, source_key):
    """Metadata-only current-source evidence, explicitly version/hash bound."""
    scope = "finra" if source_key.startswith("finra/") else "registered"
    summaries = [inventory_summary(engine, scope)]
    if scope == "finra":
        summaries.append(inventory_summary(engine, "registered"))
    summaries.sort(key=lambda row: row.get("audited_at") or "", reverse=True)
    for summary in summaries:
        for source in summary["sources"]:
            if source["source_key"] == source_key:
                return {**source, "audit_id": summary["audit_id"], "job_id": summary["job_id"],
                        "audited_at": summary["audited_at"], "inventory_hash": summary["inventory_hash"],
                        "audit_verified": source["verified"], "verified": source["verified"] and summary["current_binding_valid"],
                        "current_binding_valid": summary["current_binding_valid"], "scope_verified": summary["full_scope_verified"]}
    return {"source_key": source_key, "status": summaries[0]["status"] if not summaries[0].get("audit_id") else "not_in_inventory",
            "reason": summaries[0].get("reason"), "source_version_id": None, "content_hash": None,
            "audit_id": None, "verified": False, "current_binding_valid": False, "findings": [], "artifacts": [], "permissions": {}}


def acceptance_status(engine, scope="registered"):
    """Release gate: exact current bindings, fresh audit and reviewed full scope.

    This never runs acquisition or a new audit. A release gate re-hashes the
    current projection to reject in-place corruption of an otherwise unchanged
    version ID. Protected text is only read after current parse/store grants.
    """
    summary = inventory_summary(engine, scope)
    reasons = []
    if summary["status"] != "verified":
        reasons.append("inventory_not_verified")
    if not summary["full_scope_verified"]:
        reasons.append("scope_not_verified")
    if not summary["current_binding_valid"]:
        reasons.append("current_binding_invalid")
    if not summary["known_expected"] or summary["unresolved"] or summary["verified"] != summary["known_expected"]:
        reasons.append("expected_documents_unresolved")
    now = datetime.now(timezone.utc)
    if not _recent(summary.get("audited_at"), now):
        reasons.append("audit_overdue")
    discovery_at = summary.get("discovery", {}).get("checked_at")
    if not _recent(discovery_at, now):
        reasons.append("publisher_inventory_overdue")
    for source in summary["sources"]:
        checked_at = source.get("publisher_checked_at")
        if not _recent(checked_at, now):
            reasons.append("publisher_check_overdue:" + source["source_key"])
    if not reasons:
        with engine.connect() as conn:
            for source in summary["sources"]:
                if any(not permissions.decision(conn, source["source_key"], op)["allowed"] for op in ("store", "parse") if op in source["permissions"]):
                    reasons.append("permission_changed")
                    break
                nodes = list(conn.execute(sa.select(doc_nodes).where(doc_nodes.c.source_version_id == source["source_version_id"]).order_by(doc_nodes.c.seq)).mappings())
                clause_rows = list(conn.execute(sa.select(clauses).where(clauses.c.source_version_id == source["source_version_id"]).order_by(clauses.c.ordering)).mappings())
                if _projection_digest(nodes, clause_rows) != source.get("projection_hash"):
                    reasons.append("projection_changed:" + source["source_key"])
    return {"passed": not reasons, "reasons": reasons, "audit_id": summary.get("audit_id"),
            "inventory_hash": summary.get("inventory_hash"), "bindings_hash": summary.get("bindings_hash"),
            "scope": scope, "audited_at": summary.get("audited_at"), "evidence": summary,
            "method": "Reviewed exact inventory; complete artifact hashes; ordered original and clause/span checks; current permissions, versions and projection digests"}
