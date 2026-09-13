"""record_copy: every declared table moves between engines with counts intact, the
copy is idempotent (upsert, never delete), the Postgres release path exports a
SQLite snapshot, and the web tier hydrates DATABASE_URL from SSM (I2, I7)."""
import sqlalchemy as sa

from app.clhear.db import make_engine, run_migrations
from app.clhear.platform import record
from app.clhear.platform.record_copy import copy_record, declared_tables, export_sqlite_snapshot
from app.clhear.secrets import hydrate_ssm_env


def _write_blocks(engine):
    from app.clhear.derived_models import blocks as blocks_t

    with engine.begin() as conn:
        for i in range(3):
            record.write(
                conn, blocks_t,
                {"id": f"BLK-COPY-{i}", "name": f"Copy block {i}", "description": "", "capability": "", "evidence_artifacts": [],
                 "satisfies": [], "implements_controls": [], "status": "curated", "kind": "Document", "purpose": "p"},
                why=record.WhyTrail(layer="L3", reasoning_summary="copy test", agent_id="test:copy"),
            )


def test_round_trip_preserves_every_table_and_is_idempotent(engine, tmp_path):
    _write_blocks(engine)
    tables = declared_tables()
    assert len(tables) >= 80 and any(t.name == "audit_log" for t in tables)

    dest_path = tmp_path / "export.db"
    report = export_sqlite_snapshot(engine, dest_path)
    assert report.ok, report.summary()
    assert not report.skipped and not report.absent
    dest = make_engine(f"sqlite:///{dest_path}")
    with engine.connect() as a, dest.connect() as b:
        for name in ("blocks", "why_trails", "id_sequences", "audit_log"):
            src_n = a.execute(sa.text(f"select count(*) from {name}")).scalar()
            dst_n = b.execute(sa.text(f"select count(*) from {name}")).scalar()
            assert dst_n >= src_n, name
        assert {r[0] for r in b.execute(sa.text("select id from blocks where id like 'BLK-COPY-%'"))} == {"BLK-COPY-0", "BLK-COPY-1", "BLK-COPY-2"}
        before = b.execute(sa.text("select count(*) from audit_log")).scalar()

    again = copy_record(engine, dest)
    assert again.ok
    with dest.connect() as b:
        assert b.execute(sa.text("select count(*) from blocks where id like 'BLK-COPY-%'")).scalar() == 3
        # Upsert, not insert-or-fail and not delete-and-reload.
        assert b.execute(sa.text("select count(*) from audit_log")).scalar() == before
    dest.dispose()


def test_export_refuses_to_overwrite(engine, tmp_path):
    p = tmp_path / "exists.db"
    p.write_bytes(b"")
    import pytest

    with pytest.raises(FileExistsError):
        export_sqlite_snapshot(engine, p)


def test_publish_release_exports_snapshot_when_engine_is_postgres(engine, tmp_path, monkeypatch):
    """The Postgres branch is exercised by faking the dialect name: what matters is
    that publish_release produces l1/snapshot.db from the engine itself."""
    from app.clhear import releases

    monkeypatch.setenv("CLHEAR_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.delenv("CLHEAR_RELEASES_S3_PREFIX", raising=False)
    from app.clhear.settings import get_settings

    get_settings.cache_clear()

    class _Dialect:
        name = "postgresql"

    class _Engine:
        dialect = _Dialect()

        def __getattr__(self, item):
            return getattr(engine, item)

    calls = {}

    def fake_export(src, dest):
        calls["dest"] = dest
        return export_sqlite_snapshot(engine, dest)

    monkeypatch.setattr("app.clhear.platform.record_copy.export_sqlite_snapshot", fake_export)
    manifest = releases.publish_release(_Engine(), release_id="2026.09.13")
    assert calls["dest"].name == "snapshot.db"
    assert manifest["l1"]["snapshot_uri"].endswith("2026.09.13/l1/snapshot.db")
    assert manifest["l1"]["content_hash"]
    get_settings.cache_clear()


def test_database_url_hydrates_from_ssm_only_when_a_param_is_named():
    env = {"CLHEAR_DATABASE_URL_SSM_PARAM": "/clhear/DATABASE_URL", "CLHEAR_LLM_PROVIDER": "fake"}
    filled = hydrate_ssm_env(environ=env, getter=lambda n: "postgresql+psycopg://u:p@h/clhear" if n.endswith("DATABASE_URL") else "")
    assert filled == {"DATABASE_URL": "/clhear/DATABASE_URL"}
    assert env["DATABASE_URL"].startswith("postgresql")
    # CHANGEME in SSM (pre-cutover) leaves snapshot mode untouched.
    env2 = {"CLHEAR_DATABASE_URL_SSM_PARAM": "/clhear/DATABASE_URL"}
    assert hydrate_ssm_env(environ=env2, getter=lambda n: "CHANGEME") == {}
    assert "DATABASE_URL" not in env2
    # Already set (fleets via ECS, local dev) is never overridden.
    env3 = {"CLHEAR_DATABASE_URL_SSM_PARAM": "/clhear/DATABASE_URL", "DATABASE_URL": "sqlite:///x.db"}
    hydrate_ssm_env(environ=env3, getter=lambda n: "postgresql://nope")
    assert env3["DATABASE_URL"] == "sqlite:///x.db"
