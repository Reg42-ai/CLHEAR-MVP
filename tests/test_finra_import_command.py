"""Behavioral checks for the explicitly local, L1-only FINRA command."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.clhear.l1 import models
from app.clhear.models import events, runs
from scripts import verify_finra_import as command


def snapshot(directory, rule="2210", text="Each member must retain the original communication."):
    directory.mkdir(parents=True, exist_ok=True)
    content = f'''<html><body><nav>Not rule text</nav><h1>{rule}. Test rule</h1>
    <div id="the-rule"><div id="block-body"><div class="field--name-body">
    <p>(a) Scope</p><p>A continuation with <em>inline</em> text &amp; punctuation.</p>
    <p>(1) {text}</p><p>(b) Records must be complete.</p>
    </div></div></div><footer>Excluded navigation</footer></body></html>'''.encode()
    (directory / f"{rule}.html").write_bytes(content)
    return content


def manifest(directory, rule="2210", **changes):
    content = (directory / f"{rule}.html").read_bytes()
    url = command.REGISTRY[rule]["canonical_url"]
    row = {"rule": rule, "url": url, "final_url": url, "sha256": command.digest(content),
           "bytes": len(content), "retrieved_at": "2026-09-14T21:27:12.682228+00:00"}
    row.update(changes)
    (directory / "acquisition.json").write_text(json.dumps([row]))
    return row


def invoke(engine, tmp_path, *extra, rules=("2210",)):
    directory = tmp_path / "snapshots"
    report = tmp_path / "report.json"
    code = command.main(["--database", str(engine.url.database), "--artifact-dir", str(directory),
                         "--rules", *rules, "--report", str(report), *extra])
    return code, json.loads(report.read_text())


def rows(engine, table):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(sa.select(table).order_by(table.c.id)).mappings()]


def test_default_audit_does_not_change_database_or_archive(engine, tmp_path):
    directory = tmp_path / "snapshots"
    snapshot(directory)
    database = Path(engine.url.database)
    before = database.read_bytes()
    code, report = invoke(engine, tmp_path)
    assert code == 1
    assert report["mode"] == "audit"
    assert report["results"][0]["audit"]["errors"] == ["source missing from database"]
    assert database.read_bytes() == before
    assert not (directory / "imported").exists()


def test_missing_or_unsupported_database_is_never_created_or_migrated(tmp_path):
    database = tmp_path / "absent.db"
    report_path = tmp_path / "report.json"
    args = ["--database", str(database), "--artifact-dir", str(tmp_path),
            "--report", str(report_path), "--import"]
    assert command.main(args) == 1
    assert not database.exists()
    assert "existing local SQLite" in json.loads(report_path.read_text())["errors"][0]
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
    before = database.read_bytes()
    assert command.main(args) == 1
    assert "unsupported SQLite schema" in json.loads(report_path.read_text())["errors"][0]
    assert database.read_bytes() == before


def test_all_requested_snapshots_preflight_before_first_write(engine, tmp_path):
    snapshot(tmp_path / "snapshots")
    code, report = invoke(engine, tmp_path, "--import", rules=("2210", "3110"))
    assert code == 1
    assert "missing local snapshot" in report["errors"][0]
    assert rows(engine, models.sources) == []
    assert rows(engine, events) == []


@pytest.mark.parametrize("changes,error", [
    ({"sha256": "0" * 64}, "hash/byte count"),
    ({"final_url": "https://example.com/2210"}, "acquisition URL"),
    ({"retrieved_at": "2026-09-14"}, "timezone"),
])
def test_manifest_mismatch_blocks_import(engine, tmp_path, changes, error):
    directory = tmp_path / "snapshots"
    snapshot(directory)
    manifest(directory, **changes)
    code, report = invoke(engine, tmp_path, "--import")
    assert code == 1
    assert error in report["errors"][0]
    assert rows(engine, models.sources) == []


def test_offline_import_roundtrip_and_idempotence(engine, tmp_path, monkeypatch):
    from app.clhear import db
    from app.clhear.l1 import http
    from app.clhear.platform import embeddings, events as platform_events, router

    def forbidden(*args, **kwargs):
        raise AssertionError("offline L1 command invoked a forbidden dependency")

    monkeypatch.setattr(http, "get", forbidden)
    monkeypatch.setattr(embeddings, "rebuild_index", forbidden)
    monkeypatch.setattr(router, "complete", forbidden)
    monkeypatch.setattr(platform_events, "relay_once", forbidden)
    monkeypatch.setattr(db, "run_migrations", forbidden)
    directory = tmp_path / "snapshots"
    snapshot(directory)
    acquisition = manifest(directory)
    code, report = invoke(engine, tmp_path, "--import")
    assert code == 0, report
    assert report["all_bytes_verified"]
    item = report["results"][0]
    assert item["acquisition"]["status"] == "manifest_matched"
    assert item["audit"]["nodes"] > item["audit"]["clauses"] >= 4
    assert item["fidelity_coverage"] == 1.0
    assert item["import"]["freshness"] == "local_snapshot"
    assert rows(engine, runs)[-1]["outputs"]["freshness"] == "local_snapshot"
    versions = rows(engine, models.source_versions)
    assert versions[0]["retrieved_at"] == datetime.fromisoformat(acquisition["retrieved_at"]).replace(tzinfo=None)
    assert versions[0]["as_of_date"] is None
    assert versions[0]["effective_date"] is None
    assert all(not row["public_ok"] for row in rows(engine, models.doc_nodes) + rows(engine, models.clauses))
    original_nodes, original_events = rows(engine, models.doc_nodes), rows(engine, events)
    code, second = invoke(engine, tmp_path, "--import")
    assert code == 0, second
    assert second["results"][0]["import"] is None
    assert rows(engine, models.source_versions) == versions
    assert rows(engine, models.doc_nodes) == original_nodes
    assert rows(engine, events) == original_events
    before = Path(engine.url.database).read_bytes()
    assert invoke(engine, tmp_path)[0] == 0
    assert Path(engine.url.database).read_bytes() == before


@pytest.mark.parametrize("table,change,error", [
    (models.clauses, {"text": "Altered wording"}, "ordering/ref/text mismatch"),
    (models.clauses, {"text_hash": "f" * 64}, "text_hash mismatch"),
    (models.clauses, {"span_start": 900000}, "canonical span mismatch"),
    (models.clauses, {"public_ok": True}, "public_ok must be false"),
    (models.doc_nodes, {"raw_text": "Altered wording"}, "raw_text mismatch"),
    (models.doc_nodes, {"public_ok": True}, "public_ok must be false"),
])
def test_audit_detects_corrupted_stored_projection(engine, tmp_path, table, change, error):
    snapshot(tmp_path / "snapshots")
    assert invoke(engine, tmp_path, "--import")[0] == 0
    with engine.begin() as conn:
        first_id = conn.execute(sa.select(sa.func.min(table.c.id))).scalar_one()
        conn.execute(table.update().where(table.c.id == first_id).values(**change))
    code, report = invoke(engine, tmp_path)
    assert code == 1
    assert any(error in message for message in report["results"][0]["audit"]["errors"])


def test_changed_artifact_keeps_old_projection_and_other_sources(engine, tmp_path):
    directory = tmp_path / "snapshots"
    snapshot(directory)
    snapshot(directory, "3110")
    assert invoke(engine, tmp_path, "--import", rules=("2210", "3110"))[0] == 0
    old_versions = rows(engine, models.source_versions)
    old_nodes = rows(engine, models.doc_nodes)
    old_clauses = rows(engine, models.clauses)
    snapshot(directory, text="Each member must retain the amended communication for three years.")
    code, report = invoke(engine, tmp_path, "--import")
    assert code == 0, report
    versions = rows(engine, models.source_versions)
    assert len(versions) == len(old_versions) + 1
    assert versions[-1]["version_label"] != old_versions[0]["version_label"]
    assert rows(engine, models.doc_nodes)[:len(old_nodes)] == old_nodes
    assert rows(engine, models.clauses)[:len(old_clauses)] == old_clauses
    assert versions[1] == old_versions[1]  # unselected 3110 version is untouched


def test_repairing_identical_snapshot_preserves_old_version_ids(engine, tmp_path):
    snapshot(tmp_path / "snapshots")
    assert invoke(engine, tmp_path, "--import")[0] == 0
    with engine.begin() as conn:
        conn.execute(models.clauses.update().values(text_hash="0" * 64))
    old_nodes, old_clauses = rows(engine, models.doc_nodes), rows(engine, models.clauses)
    old_version = rows(engine, models.source_versions)[0]
    code, report = invoke(engine, tmp_path, "--import")
    assert code == 0, report
    versions = rows(engine, models.source_versions)
    assert len(versions) == 2
    assert versions[-1]["content_hash"] == old_version["content_hash"]
    assert versions[-1]["version_label"] != old_version["version_label"]
    assert rows(engine, models.doc_nodes)[:len(old_nodes)] == old_nodes
    assert rows(engine, models.clauses)[:len(old_clauses)] == old_clauses


def test_missing_archived_bytes_and_input_overwrite_are_rejected(engine, tmp_path):
    directory = tmp_path / "snapshots"
    snapshot(directory)
    assert invoke(engine, tmp_path, "--import")[0] == 0
    with engine.begin() as conn:
        conn.execute(models.source_versions.update().values(s3_uri=(directory / "imported" / "missing.html").as_uri()))
    code, report = invoke(engine, tmp_path)
    assert code == 1
    assert "local archived artifact is missing" in report["results"][0]["audit"]["errors"]
    database = Path(engine.url.database)
    before = database.read_bytes()
    assert command.main(["--database", str(database), "--artifact-dir", str(directory),
                         "--report", str(database), "--import"]) == 2
    assert database.read_bytes() == before
