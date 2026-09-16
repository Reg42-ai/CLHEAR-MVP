"""English reader and additive migration checks using authored local fixtures."""
import sqlalchemy as sa

from app.clhear import db
from app.clhear.l1 import translation
from app.clhear.l1.translation_models import TABLES
from tests.test_l1_demo_ui import run_js
from tests.test_l1_translation import imported, grant, gateway
from tests.test_migration_concurrency import real_postgresql  # noqa: F401


def test_english_reader_uses_derived_label_exact_links_and_current_evidence():
    run_js(r'''
setStates([{status:'ready',english_ready:true,origin:'machine_translation',source_language:'es',total:1,
  segments:[{segment_key:'one',doc_node_id:42,text:'Must retain 7 records.',input_hash:'original-hash',text_hash:'english-hash',
    translation_provenance:{model:'translator'},evaluation:{model:'independent-judge'}}]},null,0]);
const rendered=EnglishView({sourceKey:'fixture/spanish',version:'v1+a&b',sourceVersionId:1,contentHash:'bound'});
assert.match(rendered,/Machine-translated English/);
assert.match(rendered,/derived reading aid/);
assert.match(rendered,/Must retain 7 records/);
assert.match(rendered,/translator/);assert.match(rendered,/independent-judge/);
assert.match(rendered,/version=v1%2Ba%26b&node=42/);
setStates([{status:'blocked',english_ready:false,findings:['bilingual_disagreement'],segments:[],total:0},null,0]);
const blocked=EnglishView({sourceKey:'fixture/spanish',version:'v1',sourceVersionId:1,contentHash:'bound'});
assert.match(blocked,/bilingual_disagreement/);
assert.doesNotMatch(blocked,/Must retain 7 records/);
''')


def test_english_api_pages_actual_segments_and_rechecks_staleness(engine,client,tmp_path):
    vid=imported(engine,tmp_path,body='<h1>Regla 7</h1>'+''.join('<p>Debe conservar 7 registros.</p>' for _ in range(6)))
    grant(engine);g,_=gateway(engine)
    built=translation.build_english_view(engine,g,vid)
    assert built['english_ready']
    path='/api/clhear/sources/fixture/es/english?version_label=fixture:1&limit=4'
    first=client.get(path).json()
    second=client.get(path+'&offset=4').json()
    assert first['source_version_id']==second['source_version_id']==vid
    assert first['total']==second['total']==7 and first['has_more'] and not second['has_more']
    keys=[row['segment_key'] for row in first['segments']+second['segments']]
    assert len(keys)==len(set(keys))==7
    grant(engine,translate=False)
    blocked=client.get(path).json()
    assert not blocked['english_ready'] and blocked['segments']==[]


def test_postgresql_migrations_31_32_33_are_additive_and_repeatable(real_postgresql):
    from app.clhear.l1 import cycles
    from migrations import (m0031_l1_cycle_serialization as m31, m0032_l1_english_views as m32,
                            m0033_l1_cycle_configuration as m33)
    engine=real_postgresql
    assert {31,32,33} <= set(db.run_migrations(engine))
    with engine.begin() as conn:
        for table in reversed((cycles.queue,cycles.slot,*TABLES)):
            table.drop(conn)
        conn.exec_driver_sql(f'ALTER TABLE {cycles.cycles.schema}.l1_cycles DROP COLUMN parser_configuration_digest')
        # Exercise each upgrade directly twice as well as the migration ledger.
        for _ in range(2):
            m31.upgrade(conn);m32.upgrade(conn);m33.upgrade(conn)
        inspector=sa.inspect(conn)
        for table in (cycles.queue,cycles.slot,*TABLES):
            assert inspector.has_table(table.name,schema=table.schema)
        assert str(next(c for c in inspector.get_columns(TABLES[-1].name,schema=TABLES[-1].schema)
                        if c['name']=='translation_provenance')['type'])=='JSONB'
        assert next(c for c in inspector.get_columns(cycles.cycles.name,schema=cycles.cycles.schema)
                    if c['name']=='parser_configuration_digest')['nullable']
    assert db.run_migrations(engine)==[]


def test_old_readonly_cycle_projection_compiles_without_fabricating_configuration(engine,tmp_path):
    from app.clhear.l1 import cycles,viewer_snapshot
    from tests.test_l1_cycles import envelope
    cycle_id=cycles.start(engine,envelope('L1CycleRequested'))['cycle_id']
    with engine.begin() as conn:
        conn.exec_driver_sql('ALTER TABLE l1_cycles DROP COLUMN parser_configuration_digest')
    from pathlib import Path
    readonly=db.make_engine(f'sqlite:///{Path(engine.url.database).resolve().as_uri()}?mode=ro&uri=true')
    destination=tmp_path/'old-cycle-projection.db'
    try:
        assert cycles.cycle_summary(readonly,cycle_id)['cycles'][0]['parser_configuration_digest'] is None
        viewer_snapshot.compile_viewer_snapshot(readonly,destination,job_id=cycle_id)
        with readonly.connect() as conn:
            assert 'parser_configuration_digest' not in {c['name'] for c in sa.inspect(conn).get_columns('l1_cycles')}
    finally:
        readonly.dispose()
    projected=db.make_engine(f'sqlite:///{destination}')
    try:
        assert cycles.cycle_summary(projected,cycle_id)['cycles'][0]['parser_configuration_digest'] is None
        with projected.connect() as conn:
            assert conn.exec_driver_sql('PRAGMA foreign_key_check').all()==[]
    finally:
        projected.dispose()
