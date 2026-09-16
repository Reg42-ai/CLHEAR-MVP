"""Offline English-view contracts; no remote provider or publisher calls."""
import json
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.clhear.l1 import translation as t, pipeline, originals, permissions
from app.clhear.l1.adapters.base import Artifact, FetchResult
from app.clhear.l1.adapters.official_html import OfficialHtmlAdapter
from app.clhear.l1.adapters.html_document import parse
from app.clhear.l1.models import sources, source_versions, doc_nodes, clauses
from app.clhear.l1.translation_models import english_views, english_segments, language_bindings
from app.clhear.l1.inventory import inventory_audits, inventory_snapshots, _projection_digest
from app.clhear.platform.gateway import Gateway, InferProvider, LlmResult, SpendCapExceeded
from app.clhear.platform.task_classes import MISTRAL_LARGE_3, NOVA_LITE


def imported(engine, tmp_path, key='fixture/es', language='es', body=None, authority='authoritative', matches=None):
    body = body or '<h1>Regla 7</h1><p>Debe conservar 7 registros.</p>'
    class Adapter(OfficialHtmlAdapter):
        def fetch(self, since_version=None):
            return FetchResult('fixture:1', [Artifact('source.html', body.encode())], parse(body.encode(), key))
        def expected_text(self, artifacts):
            return [originals.html_text(a.content) for a in artifacts]
    result = pipeline.ingest(engine, Adapter(key, 'Test fixture', 'https://example.invalid'), pipeline.LocalStore(tmp_path), index_embeddings=False)
    assert result['status'] == 'added', result
    vid = result['source_version_id']
    with engine.begin() as conn:
        t.record_language_binding(conn, source_version_id=vid, language=language, document_key='test-document', authority=authority,
                                  evidence_ref='fixture:publisher-contract', approved_by='fixture-reviewer', matches_original_version_id=matches)
        nodes = conn.execute(sa.select(doc_nodes).where(doc_nodes.c.source_version_id == vid).order_by(doc_nodes.c.seq)).mappings().all()
        projected = conn.execute(sa.select(clauses).where(clauses.c.source_version_id == vid).order_by(clauses.c.ordering)).mappings().all()
        sid = str(uuid.uuid4()); now = datetime.now(timezone.utc)
        conn.execute(inventory_snapshots.insert().values(id=sid, scope='fixture', scope_version='fixture', inventory_hash=sid, definition={}))
        # A version-bound persisted audit fixture, using the real parser and digest.
        conn.execute(inventory_audits.insert().values(id=str(uuid.uuid4()), inventory_id=sid, scope='fixture', job_id='test', started_at=now, finished_at=now,
            summary={'sources':[{'source_key':key, 'source_version_id':vid, 'content_hash':result['content_hash'], 'verified':True,
                                 'projection_hash':_projection_digest(nodes, projected)}]}))
    return vid


def grant(engine, key='fixture/es', **changes):
    flags = {op: True for op in permissions.OPERATIONS}
    flags.update(changes)
    with engine.begin() as conn:
        permissions.record_permission(conn, source_key=key, permissions=flags, evidence_ref='fixture:permission', approved_by='fixture-reviewer', approved=True)


class OfflineInfer(InferProvider):
    """Local scripted double of the provider boundary; never owns an HTTP client."""
    def __init__(self, mode='ok'):
        self.mode, self.calls, self.headers = mode, [], []
    def route_explain(self, task_class):
        if self.mode == 'unregistered':
            return {'task_class':'unknown'}
        model = MISTRAL_LARGE_3 if task_class == 'l1_translate' else NOVA_LITE
        return {'task_class':task_class, 'ladder':[model], 'selected':model}
    def complete(self, **kw):
        self.calls.append(kw['task_class']); self.headers.append(kw.get('data_class'))
        data = json.loads(kw['prompt'])
        translating = kw['task_class'] == 'l1_translate'
        if self.mode == 'budget' and len(self.calls) > 2:
            raise SpendCapExceeded('fixture cap')
        if translating:
            rows = [{'id':s['id'], 'text':s['text'].replace('Regla', 'Rule').replace('Debe conservar', 'Must retain').replace('registros', 'records')} for s in data['segments']]
            if self.mode == 'missing': rows.pop()
            if self.mode == 'duplicate': rows.append(rows[0])
            if self.mode == 'numeral': rows[0]['text'] = rows[0]['text'].replace('7', '8')
            output = {'segments':rows}
        else:
            output = {'reviews':[{'id':s['id'], 'passed':self.mode != 'disagree', 'findings':[] if self.mode != 'disagree' else ['meaning']} for s in data['segments']]}
        model = MISTRAL_LARGE_3 if translating or self.mode == 'same_judge' else NOVA_LITE
        return LlmResult(json.dumps(output), model, 'infer', 100, 100, 0.001,
                         model_reported=self.mode != 'unknown_model', finish_reason='length' if self.mode == 'truncated' else 'stop', request_id='fixture-call')


def gateway(engine, mode='ok'):
    provider = OfflineInfer(mode)
    return Gateway(engine, provider), provider


def test_authoritative_english_reuses_original_and_is_idempotent(engine, tmp_path):
    vid = imported(engine, tmp_path, language='en', body='<h1>Rule 7</h1><p>Must retain 7 records.</p>')
    first = t.build_english_view(engine, None, vid)
    assert first['english_ready'] and first['origin'] == 'original_english'
    again = t.build_english_view(engine, None, vid)
    assert again['view_id'] == first['view_id'] and again['reused_view']
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(english_segments)).scalar_one() == 0
    assert t.english_acceptance(engine, [vid])['passed']


def test_matching_publisher_english_preferred_without_inference(engine, tmp_path):
    original = imported(engine, tmp_path)
    english = imported(engine, tmp_path, key='fixture/en', language='en', body='<h1>Rule 7</h1><p>Must retain 7 records.</p>', authority='official_translation', matches=original)
    result = t.build_english_view(engine, None, original)
    assert result['english_ready'] and result['origin'] == 'publisher_english'
    assert t.english_summary(engine, original)['english_version_id'] == english
    gate = t.english_acceptance(engine, [original, english])
    assert gate['passed'] and gate['total'] == 1 and gate['linked_expression_versions'] == [english]


def test_complete_translation_preserves_original_and_records_calls(engine, tmp_path):
    vid = imported(engine, tmp_path); grant(engine)
    with engine.connect() as conn:
        before = [dict(r) for r in conn.execute(sa.select(doc_nodes)).mappings()]
    g, p = gateway(engine)
    result = t.build_english_view(engine, g, vid)
    assert result['english_ready'], result
    assert p.calls == ['l1_translate','judge']
    assert p.headers == ['public','public']
    with engine.connect() as conn:
        after = [dict(r) for r in conn.execute(sa.select(doc_nodes)).mappings()]
        rows = conn.execute(sa.select(english_segments)).mappings().all()
    assert before == after and len(rows) == result['segment_count']
    assert all(r['translation_provenance']['call_id'] and r['evaluation']['call_id'] for r in rows)
    assert t.english_summary(engine, vid)['english_ready']
    count = len(p.calls)
    assert t.build_english_view(engine, g, vid)['reused_view']
    assert len(p.calls) == count


@pytest.mark.parametrize('mode', ['missing','duplicate','numeral','disagree','same_judge','unknown_model','truncated','unregistered'])
def test_fail_closed_translation_controls(engine, tmp_path, mode):
    vid = imported(engine, tmp_path); grant(engine)
    g, p = gateway(engine, mode)
    result = t.build_english_view(engine, g, vid)
    assert not result['english_ready'], mode
    assert not t.english_summary(engine, vid)['english_ready']
    if mode == 'unregistered': assert not p.calls


def test_missing_permission_blocks_before_provider(engine, tmp_path):
    vid = imported(engine, tmp_path)
    g, p = gateway(engine)
    result = t.build_english_view(engine, g, vid)
    assert result['status'] == 'blocked' and 'permission:translate' in result['findings']
    assert not p.calls


@pytest.mark.parametrize('mutation', ['source_text','output_text','evaluation','permission','language','superseded'])
def test_ready_view_stales_on_evidence_change(engine, tmp_path, mutation):
    vid = imported(engine, tmp_path); grant(engine)
    g, _ = gateway(engine)
    assert t.build_english_view(engine, g, vid)['english_ready']
    with engine.begin() as conn:
        if mutation == 'source_text':
            conn.execute(doc_nodes.update().where(doc_nodes.c.raw_text != '').values(raw_text='tampered'))
        elif mutation == 'output_text': conn.execute(english_segments.update().values(text='tampered'))
        elif mutation == 'evaluation': conn.execute(english_segments.update().values(evaluation={'passed':True}))
        elif mutation == 'language':
            t.record_language_binding(conn, source_version_id=vid, language='fr', document_key='test-document', authority='authoritative', evidence_ref='fixture:new', approved_by='fixture-reviewer')
        elif mutation == 'superseded': conn.execute(source_versions.update().where(source_versions.c.id == vid).values(status='superseded'))
    if mutation == 'permission': grant(engine, translate=False)
    assert not t.english_summary(engine, vid)['english_ready']
    assert not t.english_acceptance(engine, [vid])['passed']


def test_original_english_projection_corruption_is_not_ready(engine, tmp_path):
    vid = imported(engine, tmp_path, language='en')
    assert t.build_english_view(engine, None, vid)['english_ready']
    with engine.begin() as conn: conn.execute(doc_nodes.update().where(doc_nodes.c.raw_text != '').values(raw_text='tampered'))
    assert not t.english_summary(engine, vid)['english_ready']


def test_budget_stop_resumes_verified_batches(engine, tmp_path):
    body='<h1>Regla 7</h1>'+''.join('<p>Debe conservar 7 registros.</p>' for _ in range(6))
    vid=imported(engine,tmp_path,body=body);grant(engine)
    g,p=gateway(engine,'budget')
    first=t.build_english_view(engine,g,vid)
    assert first['status']=='blocked' and first['resumable']
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(english_segments)).scalar_one()==4
    p.mode='ok';p.calls.clear()
    second=t.build_english_view(engine,g,vid)
    assert second['english_ready'],second
    assert len(p.calls)==2  # only the remaining three segments
    assert second['resumed_from_view_id']==first['view_id']


def test_snapshot_omits_translation_after_translation_permission_revocation(engine,tmp_path):
    vid=imported(engine,tmp_path);grant(engine);g,_=gateway(engine)
    assert t.build_english_view(engine,g,vid)['english_ready']
    with engine.connect() as conn:
        assert conn.execute(t.snapshot_queries(conn,[vid])[english_segments.name]).first()
    grant(engine,translate=False)
    with engine.connect() as conn:
        assert conn.execute(t.snapshot_queries(conn,[vid])[english_segments.name]).first() is None


def test_old_snapshot_reports_evidence_unavailable(engine):
    with engine.begin() as conn: english_segments.drop(conn)
    assert t.english_summary(engine,1)['status']=='unavailable'


@pytest.mark.parametrize('compiler', ['candidate','release'])
@pytest.mark.parametrize('machine', [False,True])
def test_ready_english_survives_worker_snapshot(engine,tmp_path,compiler,machine):
    from app.clhear.db import make_engine
    from app.clhear.l1 import viewer_snapshot,release_snapshot
    # Publisher-role fixture in a disposable DB; never acquired from a network.
    key='es/worker-snapshot-contract'
    vid=imported(engine,tmp_path/'originals',key=key,language='es' if machine else 'en')
    grant(engine,key)
    g,_=gateway(engine)
    built=t.build_english_view(engine,g if machine else None,vid)
    assert built['english_ready'],built
    path=tmp_path/(compiler+'.db')
    (viewer_snapshot.compile_viewer_snapshot if compiler=='candidate' else release_snapshot.compile_snapshot)(engine,path)
    snapshot=make_engine(f'sqlite:///{path}')
    try:
        with snapshot.connect() as conn:
            assert conn.exec_driver_sql('PRAGMA foreign_key_check').all()==[]
        summary=t.english_summary(snapshot,vid)
        assert summary['english_ready'],summary
        assert summary['view_id']==built['view_id']
        assert summary.get('output_manifest_hash')==built.get('output_manifest_hash')
    finally:
        snapshot.dispose()


def test_snapshot_drops_orphaned_publisher_english_view(engine,tmp_path):
    original=imported(engine,tmp_path/'orig',key='es/document')
    english=imported(engine,tmp_path/'english',key='en/document',language='en',authority='official_translation',matches=original)
    assert t.build_english_view(engine,None,original)['english_ready']
    with engine.connect() as conn:
        queries=t.snapshot_queries(conn,[original])
        assert conn.execute(queries[english_views.name]).first() is None
        queries=t.snapshot_queries(conn,[original,english])
        assert conn.execute(queries[english_views.name]).first() is not None


def test_new_publisher_english_replaces_prior_machine_preference(engine,tmp_path):
    original=imported(engine,tmp_path/'orig');grant(engine);g,_=gateway(engine)
    assert t.build_english_view(engine,g,original)['origin']=='machine_translation'
    imported(engine,tmp_path/'english',key='fixture/en',language='en',authority='official_translation',matches=original)
    assert not t.english_summary(engine,original)['english_ready']
    assert t.build_english_view(engine,None,original)['origin']=='publisher_english'


def test_infer_classification_is_explicit_per_call():
    from tests.test_infer_provider import _provider,_Resp
    provider,client=_provider([_Resp(200,{'model':MISTRAL_LARGE_3,'id':'actual-call','choices':[{'message':{'content':'{}'},'finish_reason':'stop'}]})])
    result=provider.complete(model=MISTRAL_LARGE_3,prompt='fixture',system=None,max_tokens=8,task_class='l1_translate',data_class='restricted')
    assert client.calls[0]['headers']['X-Data-Class']=='restricted'
    assert provider.data_class=='public'  # no shared-provider mutation
    assert result.model_reported and result.finish_reason=='stop' and result.request_id=='actual-call'


@pytest.mark.parametrize('change', [{'origin':'original_english'}, {'source_language':'en'},
                                   {'document_key':'other'}, {'policy_version':'obsolete'}])
def test_inconsistent_english_metadata_is_not_ready(engine,tmp_path,change):
    vid=imported(engine,tmp_path);grant(engine);g,_=gateway(engine)
    assert t.build_english_view(engine,g,vid)['english_ready']
    with engine.begin() as conn: conn.execute(english_views.update().values(**change))
    assert not t.english_summary(engine,vid)['english_ready']


def test_corrupt_checkpoint_is_retranslated_without_reusing_bad_output(engine,tmp_path):
    body='<h1>Regla 7</h1>'+''.join('<p>Debe conservar 7 registros.</p>' for _ in range(6))
    vid=imported(engine,tmp_path,body=body);grant(engine);g,p=gateway(engine,'budget')
    first=t.build_english_view(engine,g,vid)
    assert first['resumable']
    with engine.begin() as conn:
        conn.execute(english_segments.update().where(english_segments.c.view_id==first['view_id']).values(text='corrupt'))
    p.mode='ok';p.calls.clear()
    second=t.build_english_view(engine,g,vid)
    assert second['english_ready'] and len(p.calls)==4
    assert t.english_summary(engine,vid)['english_ready']


def test_restricted_source_classification_and_publisher_permission_revocation(engine,tmp_path):
    original=imported(engine,tmp_path/'orig');grant(engine)
    with engine.begin() as conn: conn.execute(sources.update().where(sources.c.key=='fixture/es').values(license='restricted'))
    g,p=gateway(engine)
    assert t.build_english_view(engine,g,original)['english_ready']
    assert p.headers==['restricted','restricted']
    english=imported(engine,tmp_path/'english',key='fixture/en',language='en',authority='official_translation',matches=original)
    grant(engine,'fixture/en')
    with engine.begin() as conn: conn.execute(sources.update().where(sources.c.key=='fixture/en').values(license='restricted'))
    assert t.build_english_view(engine,None,original)['english_ready']
    grant(engine,'fixture/en',display_internal=False,display_public=False)
    assert not t.english_summary(engine,original)['english_ready']
    with engine.connect() as conn:
        assert conn.execute(t.snapshot_queries(conn,[original,english])[english_segments.name]).first() is None


def test_snapshot_excludes_publisher_english_from_test_origin(engine,tmp_path):
    original=imported(engine,tmp_path/'orig',key='es/document')
    english=imported(engine,tmp_path/'english',key='fixture/en',language='en',authority='official_translation',matches=original)
    assert t.build_english_view(engine,None,original)['english_ready']
    with engine.connect() as conn:
        assert conn.execute(t.snapshot_queries(conn,[original,english])[english_views.name]).first() is None


def test_translation_uses_production_router_task_and_caps(engine,tmp_path):
    from app.clhear.platform.router import Router,TASKS
    from app.clhear.models import llm_calls
    vid=imported(engine,tmp_path);grant(engine)
    provider=OfflineInfer()
    router=Router(engine,providers={'infer':provider})
    result=t.build_english_view(engine,router,vid)
    assert result['english_ready'],result
    with engine.connect() as conn:
        calls=conn.execute(sa.select(llm_calls.c.fleet,llm_calls.c.task_id).order_by(llm_calls.c.id)).all()
    assert [tuple(row) for row in calls]==[('l1.translate','l1.translate'),('l1.translation_review','l1.translation_review')]
    assert all(TASKS[row.task_id].layer=='L1' for row in calls)
