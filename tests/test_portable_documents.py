from pathlib import Path
from copy import deepcopy
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.studio_server import StudioEpisodeService
from ap_mind.product import portable_content_hash
from ap_mind.project_documents import DIMENSIONS, SECTION_INFO
from ap_mind.contracts import ContractError


@pytest.fixture
def portable(tmp_path):
    roots = [tmp_path / x for x in ('source', 'landing', 'imported')]
    for root in roots: root.mkdir()
    source = StudioEpisodeService(tmp_path/'source-data', project_root=roots[0], codex_project_id='source')
    sections = {key: {'summary': f'Known facts for {key}'} for key in SECTION_INFO}
    sections['identity'] = {'name': 'Equipment register', 'summary': 'Offline equipment loan design'}
    sections['decisions'] = {'items': [{'id':'D-1', 'status':'revoked', 'reason':'Blank dates are unknown; never fill today'}], 'user_note':'Keep duplicates by ID'}
    sections['risks'] = {'assessment':[{'key':key, 'score':None, 'reason':f'{key} untested', 'risk':'No executable app', 'improvement':'Verify implementation', 'evidence_refs':['README.md']} for key, _ in DIMENSIONS]}
    source.task_context.documents.update('source','author','receipt',{'request_id':'doc','expected_revision':0,'sections':sections},authority='user_edited')
    bundle = source.export_project('source')['bundle']; source.close()
    service = StudioEpisodeService(tmp_path/'target-data', project_root=roots[1], codex_project_id='landing')
    bundle['target_root'] = str(roots[2])
    yield service, bundle, tmp_path, sections
    service.close()


def test_dossier_cold_import_replay_and_manual_edit_survive(portable, monkeypatch):
    service, bundle, tmp, sections = portable
    preview = service.preview_project_import('preview', bundle)['draft']
    assert preview['documents']['chapter_count'] == 11
    assert preview['documents']['assessment_documented_count'] == 10
    assert preview['documents']['assessment_count'] == 0
    # A dropped preview response can replay without a client-supplied target ID.
    assert service.preview_project_import('preview', bundle)['replayed']
    confirmation = {'draft_id':preview['draft_id'], 'bundle_hash':preview['content_hash']}
    def outage(*args, **kwargs): raise RuntimeError('disk temporarily unavailable after dossier commit')
    monkeypatch.setattr(service.product_registry, 'mark_import_confirmed', outage)
    with pytest.raises(RuntimeError, match='disk temporarily'):
        service.confirm_project_import('confirm', confirmation)
    pid = preview['target_project_id']
    doc = service.task_context.documents.latest(pid)
    assert doc['sections'] == sections and doc['revision'] == 1
    assert set(doc['section_authorities'].values()) == {'user_edited'}
    assert doc['import_origin']['revision'] == 1
    service.task_context.documents.update(pid,'human','receipt',{'request_id':'human-edit','expected_revision':1,'sections':{'work':{'summary':'Human edit after interrupted import'}}},authority='user_edited')
    service.close()
    restarted = StudioEpisodeService(tmp/'target-data', project_root=tmp/'landing', codex_project_id='landing')
    try:
        result = restarted.confirm_project_import('confirm', confirmation)
        assert result['draft']['status'] == 'confirmed'
        assert restarted.confirm_project_import('confirm', confirmation)['replayed']
        doc = restarted.task_context.documents.latest(pid)
        assert doc['revision'] == 2 and doc['sections']['work']['summary'].startswith('Human edit')
        assert restarted.task_context.documents.latest(pid,1)['sections'] == sections
        assert restarted.task_context.documents.profile(restarted.product_registry.get(pid))['display_name'].endswith('（导入）')
        assert restarted.task_context.documents.read(pid)['import_origin']['revision'] == 1
        assert len([p for p in restarted.product_registry.list() if p.project_id == pid]) == 1
    finally: restarted.close()


@pytest.mark.parametrize('bad', [[], {'sections':{'risks':[]}}, {'sections':{'identity':{} }}, {'sections':{'identity':{'summary':'x'}}, 'section_authorities':[]}, {'sections':{'risks':{'assessment':[{'key':'intent','score':900}]}}}])
def test_bad_dossier_fails_at_preview_before_creating_project(portable, bad):
    service, bundle, _, _ = portable
    changed = deepcopy(bundle); changed['payload']['documents'] = bad
    changed['content_hash'] = portable_content_hash(changed)
    before = len(service.product_registry.list())
    with pytest.raises(ContractError): service.preview_project_import('bad',changed)
    assert len(service.product_registry.list()) == before


def test_old_package_without_dossier_still_imports(portable):
    service,bundle,_,_=portable
    bundle['payload'].pop('documents');bundle['content_hash']=portable_content_hash(bundle)
    preview=service.preview_project_import('legacy',bundle)['draft']
    assert preview['documents']['chapter_count']==0
    result=service.confirm_project_import('legacy-confirm',{'draft_id':preview['draft_id'],'bundle_hash':preview['content_hash']})
    assert service.task_context.documents.latest(result['project_id']) is None


def test_export_preserves_decisions_and_detects_tampering(portable):
    service,bundle,_,_=portable
    assert bundle['payload']['documents']['sections']['decisions']['items'][0]['id']=='D-1'
    bundle['payload']['documents']['sections']['identity']['summary']='tampered'
    with pytest.raises(ContractError,match='hash_mismatch'):service.preview_project_import('tamper',bundle)


def test_http_raw_file_preserves_float_and_large_integer_content_hash(portable):
    import json, threading, urllib.request
    from ap_mind.studio_server import StudioHTTPServer, StudioRequestHandler
    service,bundle,_,_=portable
    bundle['payload']['documents']['sections']['evidence']={'summary':'numeric evidence', 'fraction':1.0, 'counter':9007199254740993}
    bundle['content_hash']=portable_content_hash(bundle)
    server=StudioHTTPServer(('127.0.0.1',0),StudioRequestHandler,service=service)
    t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
    try:
        raw=json.dumps({'request_id':'raw-file','file_text':json.dumps({'bundle':bundle}), 'target_root':bundle['target_root'], 'target_display_name':'Numeric copy'}).encode()
        req=urllib.request.Request(f'http://127.0.0.1:{server.server_address[1]}/v1/ap-vibe/portable/import/preview',data=raw,headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=20) as response:result=json.load(response)
        assert result['draft']['content_hash']==bundle['content_hash']
        stored=service.product_registry.import_draft(result['draft']['draft_id'])['bundle']
        assert type(stored['payload']['documents']['sections']['evidence']['fraction']) is float
        assert stored['payload']['documents']['sections']['evidence']['counter']==9007199254740993
    finally:server.shutdown();server.server_close();t.join(5)


def test_export_redacts_outer_summary_and_source_reference(portable, monkeypatch):
    service,_,_,_=portable
    data=service.project_data('landing')
    data['memories']=[{'activity_id':'outer','summary':r'File C:\Private\work\notes.txt', 'source_ref':r'C:\Private\source.jsonl', 'activity':{}}]
    monkeypatch.setattr(service,'project_data',lambda *_:data)
    exported=service.export_project('landing')['bundle']
    assert 'C:\\Private' not in str(exported)
    assert exported['payload']['memories'][0]['summary']=='File [LOCAL_PATH_REDACTED]'
