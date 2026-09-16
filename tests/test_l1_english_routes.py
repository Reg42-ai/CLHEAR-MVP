"""Explicit English views use the same version and audience boundaries."""
import json

import sqlalchemy as sa

from app.clhear import workers
from app.clhear.l1 import inventory, translation
from app.clhear.l1.pipeline import LocalStore
from app.clhear.l1.translation_models import language_bindings
from app.clhear.platform.events import Envelope
from tests.test_l1_inventory import ENTRY, KEY, grant, imported


def _english_original(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "_declared_entries", lambda scope: {KEY: dict(ENTRY)})
    grant(engine, permissions={"acquire": True, "store": True, "parse": True, "display_public": True})
    store = LocalStore(tmp_path / "originals")
    version_id, _ = imported(engine, store)
    inventory.run_inventory_audit(engine, store, job_id="english-route-fixture", scope="finra")
    with engine.begin() as conn:
        translation.record_language_binding(conn, source_version_id=version_id, language="en",
            document_key=KEY, authority="authoritative", evidence_ref="test:authored-English",
            approved_by="test-reviewer")
    assert translation.build_english_view(engine, None, version_id)["english_ready"]
    return version_id


def test_english_route_uses_exact_original_version_without_inference(engine, client, tmp_path, monkeypatch):
    version_id = _english_original(engine, tmp_path, monkeypatch)
    monkeypatch.setattr(translation, "build_english_view", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read must not generate")))
    response = client.get(f"/api/clhear/sources/{KEY}/english?version_label=test-edition-1")
    assert response.status_code == 200
    result = response.json()
    assert result["english_ready"] and result["origin"] == "original_english"
    assert result["source_version_id"] == result["publisher_document"]["source_version_id"] == version_id
    assert result["source_content_hash"] == result["publisher_document"]["content_hash"]
    assert result["segments"] == []
    assert client.get(f"/api/clhear/sources/{KEY}/english?version_label=missing").status_code == 404


def test_english_permission_revocation_hides_text_and_publisher_link(engine, client, tmp_path, monkeypatch):
    _english_original(engine, tmp_path, monkeypatch)
    grant(engine, approved=False)
    response = client.get(f"/api/clhear/sources/{KEY}/english").json()
    assert response["locked"] and not response["english_ready"]
    assert response["segments"] == [] and "publisher_document" not in response


def test_language_evidence_is_recorded_only_for_exact_imported_artifact(engine, tmp_path):
    grant(engine)
    version_id, _ = imported(engine, LocalStore(tmp_path))
    entry = {"language_evidence": {"language": "en", "document_key": KEY, "authority": "authoritative",
        "method": "publisher_metadata", "evidence_ref": "test:authored-metadata", "artifact_sha256": "a" * 64}}
    summary = {"source_version_id": version_id, "artifact_manifest": [{"sha256": "b" * 64}]}
    workers._record_import_language(engine, entry, summary)
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(language_bindings)).scalar_one() == 0
    summary["artifact_manifest"][0]["sha256"] = "a" * 64
    workers._record_import_language(engine, entry, summary)
    workers._record_import_language(engine, entry, summary)
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(language_bindings)).scalar_one() == 1


def test_l0_language_review_result_serializes_without_changing_originals(engine, tmp_path):
    grant(engine)
    version_id, _ = imported(engine, LocalStore(tmp_path))
    envelope = Envelope(event_id="test-language-review", kind="L1EvidenceReviewRecorded", layer="l0",
        subject_ref=KEY, producer="test-only", ts="2026-09-16T00:00:00Z", payload={"review_kind": "language", "source_version_id": version_id,
            "language": "en", "document_key": KEY, "authority": "authoritative", "evidence_ref": "test:authored",
            "approved_by": "test-reviewer", "approved": True})
    result = workers.handle_l1_evidence_review(engine, None, envelope)
    assert json.loads(json.dumps(result))["record"]["source_version_id"] == version_id


def test_worker_binds_actual_publisher_html_language_only_after_byte_readback(engine, tmp_path):
    import hashlib
    body = b'<html lang="en-US"><body><main id="the-rule"><h1>2210. Authored fixture</h1><div id="block-body"><div class="field--name-body"><p>(a) Test-only complete clause used to check language metadata.</p></div></div></main></body></html>'
    grant(engine)
    store = LocalStore(tmp_path)
    version_id, key = imported(engine, store, body=body)
    summary = {"source_version_id": version_id, "source": KEY,
               "artifact_manifest": [{"key": key, "content_type": "text/html", "sha256": hashlib.sha256(body).hexdigest()}]}
    workers._record_import_language(engine, ENTRY, summary, store=store)
    with engine.connect() as conn:
        evidence = conn.execute(sa.select(language_bindings)).mappings().one()
    assert evidence["language"] == "en" and evidence["authority"] == "authoritative"
    assert evidence["source_version_id"] == version_id


def test_worker_rejects_changed_bytes_and_multipart_language_inference(engine, tmp_path):
    import hashlib
    body = b'<html lang="en"><body><main id="the-rule"><h1>2210. Authored fixture</h1><div id="block-body"><div class="field--name-body"><p>(a) Test-only complete clause used to check language metadata.</p></div></div></main></body></html>'
    grant(engine)
    store = LocalStore(tmp_path)
    version_id, key = imported(engine, store, body=body)
    summary = {"source_version_id": version_id, "source": KEY,
               "artifact_manifest": [{"key": key, "content_type": "text/html", "sha256": "0" * 64}]}
    workers._record_import_language(engine, ENTRY, summary, store=store)
    summary["artifact_manifest"][0]["sha256"] = hashlib.sha256(body).hexdigest()
    summary["artifact_manifest"].append(dict(summary["artifact_manifest"][0], key="unknown-second-original"))
    workers._record_import_language(engine, ENTRY, summary, store=store)
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(language_bindings)).scalar_one() == 0
