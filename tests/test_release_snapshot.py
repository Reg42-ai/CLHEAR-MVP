"""Private release projection must not export denied text or embeddings."""
import pytest
import sqlalchemy as sa

from app.clhear.db import make_engine
from app.clhear.l1 import permissions, pipeline, release_snapshot
from app.clhear.l1.models import clauses
from tests.test_l1_pipeline import _StubAdapter, _tree


def test_private_snapshot_checks_storage_and_omits_embeddings(engine, tmp_path):
    key = "stub/source-restricted"
    def grant(**operations):
        permissions.record_permission(engine, source_key=key, permissions=operations,
                                      approved=True, evidence_ref="test-only:fixture", approved_by="test-reviewer")
    grant(acquire=True, store=True, parse=True, display_internal=True)
    pipeline.ingest(engine, _StubAdapter("v1", _tree(("r1", "Private unit-test prose.")), "restricted"),
                    pipeline.LocalStore(tmp_path / "originals"), index_embeddings=False)
    with engine.begin() as conn:
        conn.execute(clauses.update().values(embedding=b"test-only-vector", embedding_model="test-model"))
    destination = tmp_path / "release.db"
    result = release_snapshot.compile_snapshot(engine, destination)
    assert result["audience"] == "restricted-reviewers"
    assert destination.stat().st_mode & 0o077 == 0
    assert release_snapshot.verify_snapshot_bindings(destination, result["bindings"])
    absent = tmp_path / "absent.db"
    assert release_snapshot.verify_snapshot_bindings(absent, result["bindings"]) is False
    assert not absent.exists()
    target = make_engine(f"sqlite:///{destination}")
    try:
        with target.connect() as conn:
            row = conn.execute(sa.select(clauses)).one()
            assert row.text == "Private unit-test prose."
            assert row.embedding is None and row.embedding_model is None
    finally:
        target.dispose()
    with pytest.raises(FileExistsError):
        release_snapshot.compile_snapshot(engine, destination)
    grant(display_internal=True)
    denied = tmp_path / "denied.db"
    with pytest.raises(PermissionError, match="storage"):
        release_snapshot.compile_snapshot(engine, denied)
    assert not denied.exists()
