"""Migration serialization and PostgreSQL failure semantics without live writes.

The facade supplies PostgreSQL advisory-lock/aborted-transaction behavior over
real SQLite transactions. Migration writes and rollback assertions use a real
database; no external database or service is needed for these regressions.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
import threading
import os
import uuid

import pytest
import sqlalchemy as sa

from app.clhear import db
from app.clhear.l1.models import L1_SCHEMA
from app.clhear.platform.audit import audit_log


class PostgreSQLFacade:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, engine, *, extension_unavailable=False):
        self.real_engine = engine
        self.lock = threading.Lock()
        self.extension_unavailable = extension_unavailable
        self.attempts = 0
        self.second_waiting = threading.Event()
        self.savepoints = 0

    @contextmanager
    def begin(self):
        held = False
        owner = self
        def release():
            if held:
                self.lock.release()
        with ExitStack() as stack:
            stack.callback(release)
            real = stack.enter_context(self.real_engine.begin())
            class Connection:
                aborted = False
                engine = owner

                def __getattr__(self, key):
                    return getattr(real, key)

                def execute(self, statement, *args, **kwargs):
                    nonlocal held
                    sql = str(statement)
                    if "pg_advisory_xact_lock" in sql:
                        owner.attempts += 1
                        if owner.attempts == 2:
                            owner.second_waiting.set()
                        assert owner.lock.acquire(timeout=5), "migration lock was not released"
                        held = True
                        return None
                    if sql.startswith("CREATE SCHEMA"):
                        assert held, "schema creation raced before the lock"
                        return None
                    if sql.startswith("CREATE EXTENSION"):
                        if owner.extension_unavailable:
                            self.aborted = True
                            raise RuntimeError("extension unavailable")
                        return None
                    if self.aborted:
                        raise RuntimeError("current transaction is aborted")
                    assert held, "migration work ran without the shared transaction lock"
                    if "FROM pg_extension" in sql:
                        return real.execute(sa.text("SELECT 1 WHERE 0"))
                    if sql.startswith("CREATE INDEX"):
                        return real.execute(sa.text(sql.replace(f"{L1_SCHEMA}.", "")))
                    return real.execute(statement, *args, **kwargs)

                @contextmanager
                def begin_nested(self):
                    owner.savepoints += 1
                    try:
                        with real.begin_nested():
                            yield
                    except Exception:
                        self.aborted = False
                        raise

            yield Connection()


@pytest.fixture
def migration_db(tmp_path, monkeypatch):
    engine = db.make_engine(f"sqlite:///{tmp_path / 'migrations.db'}")
    with engine.begin() as conn:
        db.schema_migrations.create(conn)
        audit_log.create(conn)
        conn.exec_driver_sql("CREATE TABLE migration_probe (version INTEGER NOT NULL)")
    monkeypatch.setattr(db, "all_schemas", lambda: ())
    yield engine
    engine.dispose()


def use_migrations(monkeypatch, *upgrades):
    names = [f"m{9001 + i}_test" for i in range(len(upgrades))]
    monkeypatch.setattr(db.pkgutil, "iter_modules", lambda _: [SimpleNamespace(name=n) for n in names])
    original = db.importlib.import_module
    modules = {f"migrations.{n}": SimpleNamespace(upgrade=upgrade) for n, upgrade in zip(names, upgrades)}
    monkeypatch.setattr(db.importlib, "import_module", lambda name, *a, **k: modules[name] if name in modules else original(name, *a, **k))


def test_two_fleet_boots_apply_each_migration_once(migration_db, monkeypatch):
    first_started, release_first = threading.Event(), threading.Event()
    def first(conn):
        conn.execute(sa.text("INSERT INTO migration_probe VALUES (9001)"))
        first_started.set()
        assert release_first.wait(5)
    def second(conn):
        conn.execute(sa.text("INSERT INTO migration_probe VALUES (9002)"))
    use_migrations(monkeypatch, first, second)
    engine = PostgreSQLFacade(migration_db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        boot1 = pool.submit(db.run_migrations, engine)
        assert first_started.wait(5)
        boot2 = pool.submit(db.run_migrations, engine)
        assert engine.second_waiting.wait(5)
        assert not boot2.done()
        release_first.set()
        assert boot1.result(5) == [9001, 9002]
        assert boot2.result(5) == []
    with migration_db.connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM migration_probe ORDER BY version").scalars().all() == [9001, 9002]
    assert not engine.lock.locked()


def test_optional_extension_failure_rolls_back_savepoint_only(migration_db, monkeypatch):
    use_migrations(monkeypatch, lambda conn: conn.execute(sa.text("INSERT INTO migration_probe VALUES (9001)")))
    engine = PostgreSQLFacade(migration_db, extension_unavailable=True)
    assert db.run_migrations(engine) == [9001]
    assert engine.savepoints == 1 and not engine.lock.locked()
    with migration_db.connect() as conn:
        assert conn.exec_driver_sql("SELECT version FROM migration_probe").scalar_one() == 9001


def test_failed_migration_rolls_back_ledger_and_releases_lock(migration_db, monkeypatch):
    def first(conn):
        conn.execute(sa.text("INSERT INTO migration_probe VALUES (9001)"))
    def fail(conn):
        raise RuntimeError("test migration failure")
    use_migrations(monkeypatch, first, fail)
    engine = PostgreSQLFacade(migration_db)
    with pytest.raises(RuntimeError, match="test migration failure"):
        db.run_migrations(engine)
    assert not engine.lock.locked()
    with migration_db.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM migration_probe").scalar_one() == 0
        assert conn.execute(sa.select(sa.func.count()).select_from(db.schema_migrations)).scalar_one() == 0
    use_migrations(monkeypatch, first)
    assert db.run_migrations(engine) == [9001]


def test_graph_migration_keeps_text_embeddings_when_extension_unavailable(migration_db, monkeypatch):
    from migrations import m0016_graph_vectors as graph
    with migration_db.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE clauses (embedding TEXT, embedding_model TEXT)")
        conn.exec_driver_sql("INSERT INTO clauses VALUES ('[0.1,0.2]', 'existing-model')")
    monkeypatch.setattr(graph, "_columns", lambda *args: {
        "embedding": "text", "embedding_hash": "text", "embedded_at": "timestamp"})
    use_migrations(monkeypatch, graph.upgrade)
    engine = PostgreSQLFacade(migration_db, extension_unavailable=True)
    assert db.run_migrations(engine) == [9001]
    assert engine.savepoints == 2  # optional runner check and migration's own check
    with migration_db.connect() as conn:
        assert conn.exec_driver_sql("SELECT embedding FROM clauses").scalar_one() == "[0.1,0.2]"
        indexes = {row[1] for row in conn.exec_driver_sql("PRAGMA index_list(clauses)")}
        assert "clauses_embedding_model_idx" in indexes
        assert "clauses_embedding_hnsw" not in indexes


@pytest.fixture
def real_postgresql():
    """Opt-in disposable local database; never accepts a remote/live DSN.

    CLHEAR_TEST_POSTGRES_URL must point to a local test server and a database
    whose name starts clhear_test_. Each run creates and drops its own unique
    database; the URL's existing database is never migrated or changed.
    """
    value = os.environ.get("CLHEAR_TEST_POSTGRES_URL")
    if not value:
        pytest.skip("Set CLHEAR_TEST_POSTGRES_URL for isolated real PostgreSQL migration checks")
    url = sa.engine.make_url(value)
    if url.host not in {"127.0.0.1", "localhost", "::1"} or not (url.database or "").startswith("clhear_test_"):
        pytest.fail("Migration tests require an explicitly named local clhear_test_ database")
    name = "clhear_test_" + uuid.uuid4().hex
    server = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    engine = db.make_engine(url.set(database=name).render_as_string(hide_password=False))
    try:
        yield engine
    finally:
        engine.dispose()
        with server.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        server.dispose()


def test_real_postgresql_concurrent_boot_without_vector_and_failure_rollback(real_postgresql, monkeypatch):
    engine = real_postgresql
    barrier = threading.Barrier(2)
    def boot():
        barrier.wait(timeout=5)
        return db.run_migrations(engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: boot(), range(2)))
    assert sum(not result for result in results) == 1
    with engine.connect() as conn:
        versions = conn.execute(sa.select(db.schema_migrations.c.version)).scalars().all()
        assert len(versions) >= 26 and len(versions) == len(set(versions))
        assert conn.exec_driver_sql("SELECT 1 FROM pg_extension WHERE extname='vector'").first() is None
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE public.migration_probe (version INTEGER NOT NULL)")
    def first(conn):
        conn.execute(sa.text("INSERT INTO public.migration_probe VALUES (9001)"))
    def fail(conn):
        raise RuntimeError("real PostgreSQL test migration failure")
    use_migrations(monkeypatch, first, fail)
    with pytest.raises(RuntimeError, match="real PostgreSQL test migration failure"):
        db.run_migrations(engine)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM public.migration_probe").scalar_one() == 0
        assert conn.execute(sa.select(db.schema_migrations.c.version).where(db.schema_migrations.c.version >= 9001)).first() is None
    use_migrations(monkeypatch, first)
    assert db.run_migrations(engine) == [9001]  # rollback released the real xact lock
