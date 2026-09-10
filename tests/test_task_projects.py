import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import pytest

from ap_mind.contracts import ContractError
from ap_mind.project_documents import KNOWLEDGE_SECTIONS, DIMENSIONS
from ap_mind.studio_server import StudioEpisodeService


@pytest.fixture
def service(tmp_path):
    s = StudioEpisodeService(tmp_path / 'data', project_root=tmp_path, codex_project_id='landing')
    yield s
    s.close()


def bootstrap(s, session='one', kind='claude', request='boot', **extra):
    return s.task_context.bootstrap({'request_id':request, 'cwd':str(s.default_project.root_path),
                                    'client_kind':kind, 'session_id':session, 'goal':'Maintain an offline receipt reader', **extra})


def creation(receipt, request='create'):
    sections = {key:{'summary':'Offline receipt reader design; executable is not implemented.'} for key in KNOWLEDGE_SECTIONS}
    sections['identity'] = {'name':'Receipt reader', 'summary':'Offline receipt viewing design',
                            'purpose':'Explain unknown values accurately', 'audience':'Local users'}
    sections['work']['user_note'] = 'Keep missing amounts unknown'
    sections['risks']['assessment'] = [{'key':key, 'score':None, 'reason':label+' has no implementation evidence',
        'risk':'Design only; runtime unknown', 'improvement':'Implement and measure', 'evidence_refs':['test://design']} for key,label in DIMENSIONS]
    return {'receipt_id':receipt['receipt_id'],'session_id':receipt['session_id'],'request_id':request,
            'expected_membership_version':receipt['membership_version'],'display_name':'Receipt reader',
            'sections':sections,'project_key':'test://receipt-reader','rationale':'A maintainable offline tool with its own design', 'evidence_refs':['test://design']}


def attach(receipt, project_id, request='attach'):
    result = creation(receipt, request)
    result.pop('sections');result.pop('display_name');result.pop('project_key');result['project_id']=project_id
    return result


@pytest.mark.parametrize('kind', ['claude', 'codex'])
def test_create_second_session_share_readback_and_preserve(service, kind):
    ctx=service.task_context
    first=bootstrap(service, kind=kind)
    if kind == 'claude':
        assert first['organization']['classification_advisory']
    request=creation(first)
    result=ctx.projects.classify(request)
    assert ctx.projects.classify(request)['replayed']
    assert len([p for p in ctx.projects.catalog({})['projects'] if p['project_id']!='landing'])==1
    second=bootstrap(service, session='two',request='second', kind=kind)
    ctx.projects.classify(attach(second,result['project_id']))
    fresh=bootstrap(service,session='two',request='new', kind=kind)
    assert fresh['project_id']==result['project_id'] and not fresh['organization']['classification_advisory']
    doc=ctx.knowledge({'receipt_id':fresh['receipt_id'],'session_id':'two','sections':['work']})
    assert doc['sections']['work']['user_note']=='Keep missing amounts unknown'
    assert len(ctx.documents.latest(result['project_id'])['sections'])==11
    assert ctx.projects.catalog({'project_id':result['project_id'],'sections':['risks']})['sections']['risks']['assessment'][0]['score'] is None


def test_codex_sources_late_rollout_manual_correction_and_atomic_rollback(service, tmp_path, monkeypatch):
    from ap_mind.product import DiscoveredCodexSession
    ctx = service.task_context
    root = str(service.default_project.root_path)
    sessions = tmp_path/'sessions'; sessions.mkdir()
    service.codex_sessions_root = sessions
    def source(key):
        path = sessions/(key+'.jsonl')
        path.write_text(json.dumps({'type':'session_meta','payload':{'id':'one','cwd':root}})+'\n', encoding='utf-8')
        return DiscoveredCodexSession(source_key=key*64, source_path=str(path), source_name=path.name,
            session_id='one', cwd=root, source_size=path.stat().st_size, modified_at='2026-09-10T00:00:00Z')
    original = source('a')
    service.product_registry.register_source(original, 'landing')
    service.product_registry.update_source(original.source_key, cursor=17)
    request = creation(bootstrap(service, kind='codex'))
    assign = service.product_registry.assign_source
    def fail(**kwargs):
        assign(**kwargs)
        raise RuntimeError('assignment failure after write')
    monkeypatch.setattr(service.product_registry, 'assign_source', fail)
    with pytest.raises(RuntimeError, match='assignment failure'):
        ctx.projects.classify(request)
    assert not ctx.projects.membership('codex', 'one')
    assert service.product_registry.source(original.source_key).project_id == 'landing'
    assert len(service.product_registry.list()) == 1
    monkeypatch.setattr(service.product_registry, 'assign_source', assign)
    value = ctx.projects.classify(request)
    assert ctx.projects.classify(request)['replayed']
    saved = service.product_registry.source(original.source_key)
    assert saved.project_id == value['project_id'] and saved.cursor == 17
    # A later rollout must follow the exact runtime membership despite cwd
    # still matching the provisional source container.
    later = source('b')
    found = service.discover_codex_sources()
    assert not found['unassigned']
    later_row = next(item for item in found['assigned'] if item['source_name'] == later.source_name)
    assert later_row['project_id'] == value['project_id']
    fresh = bootstrap(service, kind='codex', request='after-create')
    assert fresh['project_id'] == value['project_id']
    result = service.organization.assign({'request_id':'manual-correction','source_key':original.source_key,
        'session_id':'one','project_id':'landing','rationale':'User corrected the project', 'evidence_refs':['test://user'], 'confidence':None})
    assert result['ok']
    assert {s.project_id for s in service.product_registry.sources_for_session('one')} == {'landing'}
    assert ctx.projects.membership('codex', 'one')['project_id'] == 'landing'
    assert ctx.projects.membership('codex', 'one')['version'] == 2
    service.organization.assign({'request_id':'manual-correction','source_key':original.source_key,
        'session_id':'one','project_id':'landing','rationale':'User corrected the project', 'evidence_refs':['test://user'], 'confidence':None})
    assert ctx.projects.membership('codex', 'one')['version'] == 2
    with pytest.raises(ContractError, match='membership_changed'):
        ctx.update_knowledge({'receipt_id':fresh['receipt_id'],'session_id':'one','request_id':'stale','expected_revision':1,'sections':{'work':{'summary':'old writer'}}})
    assert ctx.knowledge({'receipt_id':fresh['receipt_id'], 'session_id':'one'})['revision'] == 1


def test_invalid_or_failed_create_is_atomic(service,monkeypatch):
    ctx=service.task_context;r=creation(bootstrap(service))
    bad={**r,'sections':{'identity':r['sections']['identity']}}
    with pytest.raises(ContractError,match='incomplete'):ctx.projects.classify(bad)
    original=ctx.documents.update
    def fail(*a,**kw):
        original(*a,**kw)
        raise RuntimeError('simulated failure after document write')
    monkeypatch.setattr(ctx.documents,'update',fail)
    with pytest.raises(RuntimeError):ctx.projects.classify(r)
    assert len(service.product_registry.list())==1
    assert not ctx.projects.membership('claude','one')
    monkeypatch.setattr(ctx.documents,'update',original)
    assert ctx.projects.classify(r)['revision']==1


def test_racing_classification_cas_runtime_namespace_and_stale_writers(service):
    ctx=service.task_context;r=bootstrap(service)
    def run(n):
        try:return ctx.projects.classify(creation(r,'compete-'+str(n)))
        except ContractError as e:return str(e)
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,range(2)))
    assert sum(isinstance(v,dict) for v in results)==1
    assert len([p for p in ctx.projects.catalog({})['projects'] if p['project_id']!='landing'])==1
    fresh=bootstrap(service,request='fresh')
    codex=bootstrap(service,kind='codex',request='codex')
    assert codex['project_id']=='landing'
    assert fresh['project_id']!='landing'
    patch={'receipt_id':r['receipt_id'],'session_id':'one','request_id':'stale-write','expected_revision':0,'sections':{'work':{'summary':'bad'}}}
    with pytest.raises(ContractError,match='membership_changed'):ctx.update_knowledge(patch)
    assert ctx.knowledge({'receipt_id':r['receipt_id'],'session_id':'one'})['ok']
    other=ctx.projects.classify({**creation(bootstrap(service,session='other',request='other'),'other-project'),'project_key':'test://other'})
    ctx.projects.classify(attach(fresh,other['project_id'],'move'))
    with pytest.raises(ContractError,match='membership_changed'):
        ctx.update_knowledge({**patch,'receipt_id':fresh['receipt_id'],'expected_revision':1})


def test_archive_read_and_reregistration_without_resurrecting(service):
    ctx=service.task_context;r=creation(bootstrap(service));result=ctx.projects.classify(r)
    fresh=bootstrap(service,request='fresh')
    service.product_registry.set_status(result['project_id'],'archived')
    assert not [p for p in ctx.projects.catalog({})['projects'] if p['project_id']!='landing']
    assert bootstrap(service,request='after-archive')['organization']['classification_advisory']
    assert ctx.knowledge({'receipt_id':fresh['receipt_id'],'session_id':'one','sections':['identity']})['ok']
    with pytest.raises(ContractError,match='project_not_found'):
        ctx.update_knowledge({'receipt_id':fresh['receipt_id'],'session_id':'one','request_id':'archived-write',
                              'expected_revision':1,'sections':{'work':{'summary':'must not write'}}})
    replacement=ctx.projects.classify(creation(fresh,'replacement'))
    assert replacement['project_id']!=result['project_id']
    assert service.product_registry.get(result['project_id']).status=='archived'


def test_managed_selection_and_missing_identity(service):
    ctx=service.task_context
    managed=bootstrap(service,selected_project_id='landing')
    with pytest.raises(ContractError,match='managed_project'):ctx.projects.classify(creation(managed))
    assert ctx.update_knowledge({'receipt_id':managed['receipt_id'],'session_id':'one','request_id':'managed-update',
             'expected_revision':0,'sections':{'work':{'summary':'Selected project'}}})['ok']
    ordinary=bootstrap(service,session='ordinary',request='ordinary')
    with pytest.raises(ContractError,match='classify_first'):
        ctx.update_knowledge({'receipt_id':ordinary['receipt_id'],'session_id':'ordinary','request_id':'unknown-write',
            'expected_revision':1,'sections':{'work':{'summary':'Must not touch fallback'}}})
    readonly=ctx.bootstrap({'client_kind':'claude'})
    with pytest.raises(ContractError,match='write_identity'):ctx.projects.classify(creation(readonly))


def test_retry_conflict_and_cold_recovery(service):
    ctx=service.task_context;r=creation(bootstrap(service));created=ctx.projects.classify(r)
    with pytest.raises(ContractError,match='request_conflict'):ctx.projects.classify({**r,'rationale':'Changed'})
    from ap_mind.task_context import TaskContext
    recovered=TaskContext(service)
    assert recovered.projects.classify(r)['replayed']
    assert recovered.projects.membership('claude','one')['project_id']==created['project_id']
    projected=recovered.projects.annotate_claude_sources({'sources':[{'session_id':'one'},{'session_id':'unknown'}]})
    assert projected['sources'][0]['project_membership']['project_id']==created['project_id']
    assert projected['sources'][1]['project_membership'] is None


def test_two_new_sessions_same_project_anchor_do_not_duplicate_or_overwrite(service):
    ctx=service.task_context
    a=creation(bootstrap(service,session='first',request='first'),'first-create')
    b=creation(bootstrap(service,session='second',request='second'),'second-create')
    a['sections']['work']['user_note']='First proposal'
    b['sections']['work']['user_note']='Second proposal must not overwrite'
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(ctx.projects.classify,[a,b]))
    assert results[0]['project_id']==results[1]['project_id']
    assert sum(r['created'] for r in results)==1
    assert sum(r['existing_project_reused'] for r in results)==1
    doc=ctx.documents.latest(results[0]['project_id'])
    assert doc['revision']==1
    winner=0 if results[0]['created'] else 1
    assert doc['sections']['work']['user_note']==[a,b][winner]['sections']['work']['user_note']
