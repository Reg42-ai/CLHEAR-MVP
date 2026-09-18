"""Private POC permission rows cover exactly the protected registry set."""
import pytest
import sqlalchemy as sa

from app.clhear.l1 import inventory, permissions, poc_review, source_registry
from app.clhear.l1.models import sources
from app.clhear.settings import get_settings


PROTECTED_PREFIXES = ("finra/", "iso/", "aicpa/", "pci/", "ifrs/")


def _l0(monkeypatch, engine):
    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    get_settings.cache_clear()
    from app.clhear import db
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(db, "run_migrations", lambda e: [])


def test_activate_records_protected_set_and_grants_public_display(engine, monkeypatch):
    _l0(monkeypatch, engine)
    source_registry.seed(engine)
    keys = poc_review.protected_source_keys()
    assert keys and all(k.startswith(PROTECTED_PREFIXES) for k in keys)
    assert "iso/27001-2022" in keys and "aicpa/soc2-tsc" in keys
    assert any(k.startswith("finra/rule/") for k in keys)
    from app.clhear import workers
    assert workers.cli(["--poc-private-review", "activate", "--verification-id", "l1-poc-1-1",
                        "--evidence-ref", "poc:test"]) == 0
    with engine.connect() as conn:
        rows = list(conn.execute(sa.select(permissions.source_permissions)
                                 .order_by(permissions.source_permissions.c.source_key,
                                           permissions.source_permissions.c.id)).mappings())
        latest = {}
        for row in rows:
            latest[row["source_key"]] = row
        assert set(latest) == set(keys)
        for row in latest.values():
            assert row["approved"] is True
            assert row["approved_by"] == poc_review.POC_APPROVED_BY
            assert row["permissions"]["display_public"] is True
            assert row["permissions"]["display_internal"] is True
            assert row["permissions"]["acquire"] is True
            assert permissions.decision(conn, row["source_key"], "display_public")["allowed"] is True
            assert permissions.decision(conn, row["source_key"], "display_internal")["allowed"] is True
        open_keys = [k for k in conn.execute(sa.select(sources.c.key)).scalars()
                     if not k.startswith(PROTECTED_PREFIXES)]
        assert open_keys
        assert all(permissions.decision(conn, key, "acquire")["reason"] == "missing_permission" for key in open_keys[:5])


def test_revoke_denies_internal_display_and_is_idempotent_per_verification(engine, monkeypatch):
    _l0(monkeypatch, engine)
    source_registry.seed(engine)
    from app.clhear import workers
    workers.cli(["--poc-private-review", "activate", "--verification-id", "l1-poc-2-1",
                 "--evidence-ref", "poc:test"])
    assert workers.cli(["--poc-private-review", "revoke", "--verification-id", "l1-poc-2-2",
                        "--evidence-ref", "poc:test-revoke"]) == 0
    with engine.connect() as conn:
        for key in poc_review.protected_source_keys():
            assert permissions.decision(conn, key, "display_internal")["allowed"] is False
            assert permissions.decision(conn, key, "acquire")["allowed"] is False
            assert permissions.decision(conn, key, "display_public")["allowed"] is False
    # Same verification id + evidence is a no-op delivery, not a second grant.
    assert workers.cli(["--poc-private-review", "revoke", "--verification-id", "l1-poc-2-2",
                        "--evidence-ref", "poc:test-revoke"]) == 0
    with engine.connect() as conn:
        counts = dict(conn.execute(sa.select(permissions.source_permissions.c.source_key,
                                             sa.func.count())
                                   .group_by(permissions.source_permissions.c.source_key)).all())
    assert set(counts) == set(poc_review.protected_source_keys())
    assert all(n == 2 for n in counts.values())  # activate + one revoke


def test_cli_is_l0_only_and_rejects_combined_actions(engine, monkeypatch):
    from app.clhear import workers
    monkeypatch.setenv("CLHEAR_FLEET", "l1")
    with pytest.raises(SystemExit) as err:
        workers.cli(["--poc-private-review", "activate", "--verification-id", "l1-poc-3-1",
                     "--evidence-ref", "poc:test"])
    assert err.value.code == 1
    _l0(monkeypatch, engine)
    with pytest.raises(SystemExit):
        workers.cli(["--poc-private-review", "activate", "--approve-inventory", "a" * 64,
                     "--verification-id", "l1-poc-3-1", "--evidence-ref", "poc:test"])


def test_approve_inventory_binds_the_frozen_hash(engine, monkeypatch):
    _l0(monkeypatch, engine)
    from app.clhear.l1.pipeline import LocalStore
    from pathlib import Path
    audit = inventory.run_inventory_audit(engine, LocalStore(Path("/tmp/clhear-poc-inventory")),
                                          job_id="poc-scope", discover=False)
    digest = audit["inventory_hash"]
    from app.clhear import workers
    assert workers.cli(["--approve-inventory", digest, "--verification-id", "l1-inventory-1-1",
                        "--evidence-ref", "poc:scope"]) == 0
    with engine.connect() as conn:
        review = conn.execute(sa.select(inventory.inventory_reviews)
                              .order_by(inventory.inventory_reviews.c.id.desc())).mappings().first()
    assert review["inventory_hash"] == digest and review["approved"] is True
    assert review["approved_by"] == poc_review.POC_APPROVED_BY
