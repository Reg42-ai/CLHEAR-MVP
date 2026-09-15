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
