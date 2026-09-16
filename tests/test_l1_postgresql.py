"""Opt-in disposable PostgreSQL checks; never accepts a remote/live DSN."""
from datetime import datetime, timezone
import uuid

import sqlalchemy as sa

from app.clhear import db
from app.clhear.l1 import cycles, discovery, inventory, origin, source_registry
from app.clhear.l1.models import sources
from app.clhear.l1.permissions import record_permission
from app.clhear.l1.pipeline import LocalStore
from app.clhear.platform.events import Envelope
from tests.test_migration_concurrency import real_postgresql  # noqa: F401


def test_real_postgresql_l1_migrations_discovery_cycle_and_origin(real_postgresql, tmp_path, monkeypatch):
    engine = real_postgresql
    assert {27, 28, 29, 30} <= set(db.run_migrations(engine))
    with engine.connect() as conn:
        inspection = sa.inspect(conn)
        assert inspection.has_table('l1_discovery_pages', schema='l1_sources')
        assert inspection.has_table('l1_cycles', schema='l0_platform')
        assert inspection.has_table('l1_cycle_children', schema='l0_platform')
        assert inspection.has_table('source_origin_reviews', schema='l1_sources')

    # Exercise the upgrade path, not just fresh metadata.create_all().
    from migrations.m0029_l1_source_locations import upgrade
    with engine.begin() as conn:
        conn.exec_driver_sql('ALTER TABLE l1_sources.doc_nodes DROP COLUMN source_locator')
        upgrade(conn)
        upgrade(conn)
        column = next(c for c in sa.inspect(conn).get_columns('doc_nodes', schema='l1_sources') if c['name'] == 'source_locator')
        assert str(column['type']) == 'JSONB' and column['nullable'] is False

    catalog = 'fixture/catalog'
    record_permission(engine, source_key=catalog, permissions={'acquire': True, 'store': True, 'parse': True},
                      evidence_ref='test:authored-metadata', approved_by='fixture-reviewer', approved=True)
    seed = 'https://example.invalid/catalog'
    child = 'https://example.invalid/document'
    entry = {'key': 'fixture/document', 'discovered_category': 'fixture'}
    arguments = dict(publisher_id='fixture', profile={'test': True},
        seeds=[{'url': seed, 'source_key': catalog, 'category': 'fixture'}], job_id='fixture-discovery',
        fetcher=lambda url: (f'<main><a href="{child}">Authored fixture</a></main>'.encode(), 'live'),
        classify=lambda url, parent: {'url': child, 'source_key': entry['key'], 'category': 'fixture', 'role': 'document', 'entry': entry},
        max_pages=1, cycle_date='2026-09-16')
    _, first = discovery.run_batch(engine, LocalStore(tmp_path), **arguments)
    assert first['pending_pages'] == 1
    entries, second = discovery.run_batch(engine, LocalStore(tmp_path), **arguments)
    assert second['cycle_id'] == first['cycle_id'] and second['pending_pages'] == 0
    assert entry['key'] in entries and not second['complete']

    source_registry.seed(engine)
    with engine.begin() as conn:
        conn.execute(sources.update().where(sources.c.key == 'finra/rule/2210').values(issuer='Test fixture'))
    assert origin.reconcile_origins(engine)['classified'] == 1
    source_registry.seed(engine)
    with engine.connect() as conn:
        assert conn.execute(sa.select(sources.c.issuer).where(sources.c.key == 'finra/rule/2210')).scalar_one() == 'Test fixture'

    audit = inventory.run_inventory_audit(engine, LocalStore(tmp_path), job_id='fixture-audit', discover=False)
    assert audit['expected_total'] is None and not audit['full_scope_verified']
    assert inventory.planned_entries(engine, scope='all_publishers', audit_id=audit['audit_id']) == []
    assert inventory.inventory_summary(engine)['inventory_hash'] == audit['inventory_hash']

    envelope = Envelope(event_id=str(uuid.uuid4()), layer='l0', kind='L1CycleRequested', subject_ref='scope',
                        payload={'scope': 'all_publishers'}, producer='fixture.manual', ts=datetime.now(timezone.utc).isoformat())
    started = cycles.start(engine, envelope)
    assert cycles.discovered(engine, started['cycle_id'], audit)['status'] == 'planned'
    assert cycles.advance(engine, started['cycle_id'])['status'] == 'running'
    state = cycles.cycle_summary(engine, started['cycle_id'])
    assert len(state['children']) == len(cycles.adapter_keys())
    assert not cycles.output_bindings(engine, started['cycle_id'], audit)['passed']


# --------------------------------------------------------------------------- import persistence
# The deployment of 16 Sep 2026 (run 35106288646) lost every FINRA import on Aurora:
# the SQLite-only FTS probe aborted the PostgreSQL transaction and the document rows
# never committed. SQLite tests cannot see that; these run the real worker handler,
# the real pipeline and read the rows back from a disposable PostgreSQL.

def _l1_worker(monkeypatch, tmp_path, adapters):
    from app.clhear import workers
    from app.clhear.l1 import fleet, inventory, registry_etoro
    from app.clhear.platform import evals
    from app.clhear.settings import get_settings
    monkeypatch.setenv("CLHEAR_FLEET", "L1")
    monkeypatch.setenv("CLHEAR_L1_ONLY", "true")
    monkeypatch.setenv("CLHEAR_ARTIFACTS_DIR", str(tmp_path / "lake"))
    monkeypatch.delenv("CLHEAR_ARTIFACT_STORE", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(fleet, "fleet_plan", lambda key=None: [({"key": a.meta().source_key}, a) for a in adapters])
    monkeypatch.setattr(registry_etoro, "seed", lambda engine: None)
    monkeypatch.setattr(inventory, "planned_entries", lambda *a, **k: [])
    # Acceptance and evals are separate gates with their own tests; persistence is under test here.
    monkeypatch.setattr(inventory, "acceptance_status", lambda *a, **k: {"passed": True, "inventory_hash": None})
    monkeypatch.setattr(evals, "run_suite", lambda *a, **k: {"passed": True, "scores": {}})
    monkeypatch.setattr(evals, "run_source_evals", lambda *a, **k: [{"passed": True, "suite": "fixture"}])
    monkeypatch.setattr(workers, "_put_schedule_metric", lambda *a, **k: None)
    return workers


def _adapter_run(workers, engine, event_id):
    from app.clhear.l1 import workflow
    envelope = Envelope(event_id=event_id, layer="l1", kind="AdapterRunRequested", subject_ref="synthetic",
                        payload={"adapter": "synthetic"}, producer="test.manual", ts=datetime.now(timezone.utc).isoformat())
    job_id = workflow.job_id_for(event_id, "synthetic")
    try:
        result = workers.handle_envelope(engine, None, envelope.model_dump_json())
    except workers.AdapterRunIncomplete:
        with engine.connect() as conn:
            result = conn.execute(sa.select(workflow.jobs.c.summary).where(workflow.jobs.c.job_id == job_id)).scalar_one()
        result = {**result, "incomplete": True}
    with engine.connect() as conn:
        tasks = {t["source_key"]: dict(t) for t in conn.execute(
            sa.select(workflow.tasks).where(workflow.tasks.c.job_id == job_id)).mappings()}
    return result, tasks


def test_real_postgresql_import_persists_reads_back_repeats_and_amends(real_postgresql, tmp_path, monkeypatch):
    from app.clhear.l1 import retrieval
    from app.clhear.l1.models import clauses, search_units, source_versions
    from app.clhear.platform import record
    from tests.test_l1_synthetic_amendment import V1, V2, SyntheticAdapter
    engine = real_postgresql
    db.run_migrations(engine)
    with engine.connect() as conn:
        assert not record.fts_supported(conn) and not record.fts_available(conn, "search_units_fts")
        assert record.drop_fts_rows(conn, "search_units_fts", [1, 2]) == 0  # no statement issued
        assert conn.execute(sa.text("SELECT 1")).scalar_one() == 1  # the transaction is still usable

    restricted = SyntheticAdapter(V1, "2026-01-01", source_key="synthetic/restricted")
    restricted.meta = lambda m=restricted.meta: m().__class__(**{**m().__dict__, "license": "restricted"})  # type: ignore[misc]
    workers = _l1_worker(monkeypatch, tmp_path, [SyntheticAdapter(V1, "2026-01-01"), restricted])

    result, tasks = _adapter_run(workers, engine, "pg-import-1")
    # A restricted source without a recorded permission is rights-blocked before any text is stored.
    assert result.get("statuses") == {"added": 1, "rights-blocked": 1}, result
    assert {k: t["status"] for k, t in tasks.items()} == {"synthetic/prin": "completed", "synthetic/restricted": "blocked"}
    assert tasks["synthetic/prin"]["error"] is None
    with engine.connect() as conn:
        sid = conn.execute(sa.select(sources.c.id).where(sources.c.key == "synthetic/prin")).scalar_one()
        versions = conn.execute(sa.select(source_versions.c.id, source_versions.c.content_hash)
                                .where(source_versions.c.source_id == sid)).all()
        assert len(versions) == 1
        stored = conn.execute(sa.select(clauses.c.ref, clauses.c.text).where(clauses.c.source_version_id == versions[0].id)).all()
        assert {r.ref: r.text for r in stored if r.ref in V1} == V1  # byte-exact readback
        units = conn.execute(sa.select(sa.func.count()).select_from(search_units).where(search_units.c.source_id == sid)).scalar_one()
        assert units >= len(V1)
        rid = conn.execute(sa.select(sources.c.id).where(sources.c.key == "synthetic/restricted")).scalar_one_or_none()
        if rid is not None:
            assert conn.execute(sa.select(sa.func.count()).select_from(search_units).where(search_units.c.source_id == rid)).scalar_one() == 0
            assert conn.execute(sa.select(sa.func.count()).select_from(source_versions).where(source_versions.c.source_id == rid)).scalar_one() == 0
    hits = retrieval.search(engine, "due regard to the interests")
    assert hits and all(h["source_key"] == "synthetic/prin" for h in hits)  # PostgreSQL fallback legs, restricted text excluded

    repeat, tasks = _adapter_run(workers, engine, "pg-import-2")
    assert repeat["statuses"] == {"unchanged": 1, "rights-blocked": 1} and tasks["synthetic/prin"]["status"] == "completed"
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(source_versions).where(source_versions.c.source_id == sid)).scalar_one() == 1

    workers = _l1_worker(monkeypatch, tmp_path, [SyntheticAdapter(V2, "2026-06-01")])
    amended, tasks = _adapter_run(workers, engine, "pg-import-3")
    assert amended["statuses"] == {"amended": 1} and tasks["synthetic/prin"]["status"] == "completed"
    with engine.connect() as conn:
        after = conn.execute(sa.select(source_versions.c.id, source_versions.c.status)
                             .where(source_versions.c.source_id == sid).order_by(source_versions.c.id)).all()
        assert len(after) == 2 and after[-1].status == "in_force"
        latest = conn.execute(sa.select(clauses.c.ref, clauses.c.text).where(clauses.c.source_version_id == after[-1].id)).all()
        assert {r.ref: r.text for r in latest if r.ref in V2} == V2


def test_real_postgresql_persistence_failure_leaves_no_partial_version_and_no_poisoned_connection(real_postgresql, tmp_path, monkeypatch):
    from app.clhear.l1 import pipeline, retrieval
    from app.clhear.l1.models import source_versions
    from tests.test_l1_synthetic_amendment import V1, SyntheticAdapter
    engine = real_postgresql
    db.run_migrations(engine)
    workers = _l1_worker(monkeypatch, tmp_path, [SyntheticAdapter(V1, "2026-01-01")])
    real_build = retrieval.build_units_for_version
    calls = []

    def failing_build(conn, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            conn.exec_driver_sql("SELECT * FROM l1_sources.no_such_table")  # a genuine database error mid-transaction
        return real_build(conn, *args, **kwargs)
    monkeypatch.setattr(retrieval, "build_units_for_version", failing_build)

    result, tasks = _adapter_run(workers, engine, "pg-fail-1")
    assert result.get("incomplete") and tasks["synthetic/prin"]["status"] in {"retrying", "failed"}
    error = tasks["synthetic/prin"]["error"] or ""
    assert "UndefinedTable" in error or "no_such_table" in error
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(source_versions)).scalar_one() == 0  # nothing half-written
        assert conn.execute(sa.text("SELECT 1")).scalar_one() == 1

    # The same worker process imports successfully afterwards: no aborted transaction survived.
    summary = pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01"), pipeline.LocalStore(tmp_path / "lake"), index_embeddings=False)
    assert summary["status"] == "added" and len(calls) == 2
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(source_versions)).scalar_one() == 1
