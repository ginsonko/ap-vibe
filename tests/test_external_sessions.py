"""Native format contracts, isolated from the user's installed applications."""
import json
from pathlib import Path
import sqlite3
import sys

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.external_sessions import ExternalSessions
from ap_mind.contracts import ContractError
from ap_mind.studio_server import StudioEpisodeService
from test_task_projects import bootstrap, creation


def test_hermes_native_wal_history_structured_content_and_no_private_fields(tmp_path):
    path=tmp_path/'hermes/state.db';path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('CREATE TABLE sessions(id,model,started_at,title,cwd,parent_session_id,system_prompt,model_config);'
            'CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id,role,content,timestamp,active,_compressed_summary,display_kind,reasoning,api_content);')
        db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)',('real','grok',1,'Native product','C:/product',None,'PRIVATE_SYSTEM','PRIVATE_CONFIG'))
        structured='\x00json:'+json.dumps([{'type':'thinking','thinking':'PRIVATE_THINKING'},
            {'type':'text','text':'Visible progress'},{'type':'image_url','image_url':{'url':'PRIVATE_IMAGE'}}])
        rows=[(1,'real','user','Old requirement',1,0,0,None,'PRIVATE_REASONING','PRIVATE_API'),
            (2,'real','assistant',structured,2,1,0,None,'PRIVATE_REASONING','PRIVATE_API'),
            (3,'real','assistant','PRIVATE_HIDDEN',3,1,0,'hidden',None,None),
            (4,'real','user','PRIVATE_COMPACTION',4,1,1,None,None,None),
            (5,'real','tool','PRIVATE_ARGUMENT',5,1,0,None,None,None)]
        db.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)',rows);db.commit()
        monitor=ExternalSessions(tmp_path/'vibe',roots={'hermes':[path.parent]},cache_seconds=0)
        source,=monitor.discover()['sources']
        assert source['harness']=='hermes' and source['cwd']=='C:/product'
        page=monitor.read(source['source_id'],limit=1)
        assert [e['text'] for e in page['events']]==['Visible progress']
        history=monitor.read(source['source_id'],before=page['history_before'])
        assert history['events'][0]['text']=='Old requirement' and history['events'][0]['context_archived']
        assert 'PRIVATE' not in json.dumps([source,page,history])
        db.execute('UPDATE messages SET content=? WHERE id=2',('Updated progress',));db.commit()
        refreshed=monitor.read(source['source_id'],after=page['cursor'],expected_generation=page['generation'])
        assert refreshed['reset'] and refreshed['events'][-1]['text']=='Updated progress'
        assert db.execute('SELECT count(*) FROM messages').fetchone()[0]==5
    from ap_mind.hermes_sessions import message_text
    assert message_text(b'\x00json:[{"type":"thinking","thinking":"PRIVATE')==''
    assert message_text('[1,2]')=='[1,2]'


def test_pi_desktop_native_blocks_and_index_are_not_pi_cli(tmp_path):
    sessions = tmp_path / 'pi-desktop/sessions'
    write(sessions / 'desktop-session.jsonl', [
        {'type': 'session', 'schema': 1, 'sessionId': 'desktop-session', 'createdAt': '2026-09-12T01:00:00Z'},
        {'type': 'message', 'id': 'u', 'role': 'user', 'blocks': [{'type': 'text', 'text': 'Continue our product'}]},
        {'type': 'message', 'id': 'a', 'role': 'assistant', 'blocks': [
            {'type': 'thinking', 'text': 'PRIVATE_REASONING'}, {'type': 'text', 'text': 'The fix is ready'},
            {'type': 'tool_call', 'arguments': {'secret': 'PRIVATE_ARGUMENT'}}], 'meta': {'modelId': 'native-model'}}])
    write(sessions / 'desktop-session.revisions.jsonl', [{'type': 'revision', 'messages': []}])
    (sessions / 'broken.jsonl').write_bytes(b'not json\n')
    with sqlite3.connect(sessions.parent / 'pi.sqlite') as db:
        db.executescript('CREATE TABLE sessions(id,title,model_id,project_id,deleted_at); CREATE TABLE projects(id,path);')
        db.execute('INSERT INTO projects VALUES (?,?)', (1, 'C:/long-term-product'))
        db.execute('INSERT INTO sessions VALUES (?,?,?,?,?)', ('desktop-session', 'Actual title', 'native-model', 1, None))
    adapter = ExternalSessions(tmp_path / 'vibe', roots={'pi-desktop': [sessions]}, cache_seconds=0)
    source, = adapter.discover()['sources']
    assert source['harness'] == 'pi-desktop'
    assert source['title'] == 'Actual title' and source['cwd'] == 'C:/long-term-product'
    page = adapter.read(source['source_id'])
    assert [e['text'] for e in page['events']] == ['Continue our product', 'The fix is ready']
    assert page['events'][1]['model'] == 'native-model'
    assert 'PRIVATE' not in json.dumps(page)


def write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r)+'\n' for r in records), encoding='utf-8')


def pi_records(session='same-id', cwd='C:/work'):
    return [{'type':'session','id':session,'cwd':cwd,'timestamp':'2026-09-12T00:00:00Z'},
            {'type':'message','id':'u','message':{'role':'user','content':'Please continue the project'}},
            {'type':'message','id':'a','message':{'role':'assistant','model':'example-model','content':[
                {'type':'thinking','thinking':'PRIVATE_SENTINEL'}, {'type':'text','text':'Public progress'},
                {'type':'toolCall','name':'exec','arguments':{'key':'SECRET_SENTINEL'}}]}}]


def test_native_pi_pages_partial_append_and_no_private_payload(tmp_path):
    path=tmp_path/'agents/main/sessions/s.jsonl'
    write(path, pi_records())
    monitor=ExternalSessions(tmp_path, roots={'openclaw':[tmp_path/'agents']},cache_seconds=0)
    source=monitor.discover()['sources'][0]
    assert source['model']=='example-model'
    page=monitor.read(source['source_id'],limit=1)
    assert page['events'][0]['text']=='Public progress'
    assert 'SENTINEL' not in json.dumps(page)
    old=monitor.read(source['source_id'],before=page['history_before'])
    assert old['events'][0]['role']=='user'
    with path.open('a',encoding='utf-8') as f:f.write('{"type":"message"')
    partial=monitor.read(source['source_id'],after=page['cursor'],expected_generation=page['generation'])
    assert partial['events']==[] and partial['trailing_partial']
    with path.open('a',encoding='utf-8') as f:f.write(',"id":"b","message":{"role":"assistant","content":"Later"}}\n')
    later=monitor.read(source['source_id'],after=partial['cursor'],expected_generation=page['generation'])
    assert later['events'][0]['text']=='Later'
    with pytest.raises(ContractError):monitor.read('openclaw-../../secrets')


def test_application_identity_is_namespaced_and_curation_keeps_dossier(tmp_path):
    s=StudioEpisodeService(tmp_path/'data',project_root=tmp_path,codex_project_id='landing')
    try:
        write(tmp_path/'pi/x/s.jsonl',pi_records())
        write(tmp_path/'claw/main/sessions/s.jsonl',pi_records())
        s.external_sessions=ExternalSessions(tmp_path/'data',roots={'pi':[tmp_path/'pi'],'openclaw':[tmp_path/'claw']},cache_seconds=0)
        initial=s.task_context.projects.classify(creation(bootstrap(s,kind='claude')))
        before=s.task_context.documents.latest(initial['project_id'])
        directory=s.session_directory.catalog(harness='openclaw')
        source=directory['sessions'][0]
        assert len(directory['sessions'])==1 and source['project_id'] is None
        request={'request_id':'cross-application-assign','source_key':source['source_id'],'session_id':source['session_id'],
                 'project_id':initial['project_id'],'rationale':'Same real project evidence','evidence_refs':['test://native-session']}
        assert s.organization.assign(request)['ok']
        assert s.organization.assign(request)['replayed']
        assert s.session_directory.catalog(harness='openclaw')['sessions'][0]['project_id']==initial['project_id']
        assert s.session_directory.catalog(harness='pi')['sessions'][0]['project_id'] is None
        assert s.task_context.documents.latest(initial['project_id'])==before
        catalog=s.organization.catalog('rebuild_all')
        assert source['source_id'] in {i['source_key'] for i in catalog['items']}
        ctx=bootstrap(s,session='peer',kind='openclaw',request='peer-boot')
        assert ctx['studio_context']['harness']=='openclaw'
    finally:s.close()


def test_opencode_sqlite_stream_changes_reset_and_history(tmp_path):
    path=tmp_path/'opencode.db'
    conn=sqlite3.connect(path)
    conn.executescript('CREATE TABLE session(id TEXT,title TEXT,directory TEXT,time_updated INTEGER);'
        'CREATE TABLE message(id TEXT,session_id TEXT,data TEXT,time_created INTEGER);'
        'CREATE TABLE part(id TEXT,message_id TEXT,session_id TEXT,data TEXT,time_updated INTEGER);')
    conn.execute('INSERT INTO session VALUES (?,?,?,?)',('ses-a','A real title','C:/work',1789142400000))
    for i in range(60):
        conn.execute('INSERT INTO message VALUES (?,?,?,?)',(str(i),'ses-a',json.dumps({'role':'assistant','modelID':'model-a'}),i))
        conn.execute('INSERT INTO part VALUES (?,?,?,?,?)',(str(i),str(i),'ses-a',json.dumps({'type':'text','text':'line '+str(i)}),i))
    conn.execute('INSERT INTO part VALUES (?,?,?,?,?)',('secret','59','ses-a',json.dumps({'type':'reasoning','text':'PRIVATE_SENTINEL'}),60))
    conn.commit()
    monitor=ExternalSessions(tmp_path,roots={'opencode':[path]},cache_seconds=0)
    source=monitor.discover()['sources'][0]
    page=monitor.read(source['source_id'],limit=7)
    assert [e['text'] for e in page['events']]==['line '+str(i) for i in range(53,60)]
    assert 'SENTINEL' not in json.dumps(page)
    older=monitor.read(source['source_id'],before=page['history_before'],limit=7)
    assert older['events'][-1]['text']=='line 52'
    conn.execute('UPDATE part SET data=?,time_updated=100 WHERE id=?',(json.dumps({'type':'text','text':'Stream finished'}),'59'))
    conn.commit()
    updated=monitor.read(source['source_id'],after=page['cursor'],expected_generation=page['generation'])
    assert updated['reset'] and updated['events'][-1]['text']=='Stream finished'
    conn.close()


def test_ga_public_snapshot_retains_valid_page_while_native_writer_is_incomplete(tmp_path):
    root = tmp_path / 'ga'; root.mkdir()
    path = root / 'session.json'
    doc = {'id': 'native-ga', 'title': 'GA task', 'workspace': str(tmp_path), 'updated_at': 1000,
        'raw_history': [{'role': 'assistant', 'content': 'PRIVATE_RAW'}],
        'messages': [{'id': str(i), 'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'public ' + str(i), 'model_id': 'model-a', 'created_at': i} for i in range(9)]}
    doc['messages'].append({'id': 'tool', 'role': 'tool', 'content': 'PRIVATE_TOOL'})
    doc['messages'].append({'id': 'final', 'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'PRIVATE_REASONING'}, {'type': 'text', 'text': 'finished'}], 'model_id': 'model-b'})
    path.write_text(json.dumps(doc), encoding='utf-8')
    monitor = ExternalSessions(tmp_path, roots={'ga-admin': [root]}, cache_seconds=0)
    source = monitor.discover()['sources'][0]
    assert source['harness'] == 'ga-admin' and source['model'] == 'model-b'
    page = monitor.read(source['source_id'], limit=3)
    assert [e['text'] for e in page['events']] == ['public 7', 'public 8', 'finished']
    assert 'PRIVATE_' not in json.dumps(page)
    older = monitor.read(source['source_id'], before=page['history_before'], limit=3)
    assert older['events'][-1]['text'] == 'public 6'
    path.write_text('{"id":', encoding='utf-8')
    stale = monitor.read(source['source_id'])
    assert stale['stale'] and stale['events'][-1]['text'] == 'finished'
    doc['messages'].append({'role': 'assistant', 'content': 'next turn'})
    path.write_text(json.dumps(doc), encoding='utf-8')
    refreshed = monitor.read(source['source_id'], after=page['cursor'], expected_generation=page['generation'])
    assert refreshed['reset'] and not refreshed['stale'] and refreshed['events'][-1]['text'] == 'next turn'


def test_mimo_import_lineage_does_not_merge_native_identities(tmp_path):
    path = tmp_path / 'mimocode.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('CREATE TABLE session (id TEXT,title TEXT,directory TEXT,time_updated INTEGER,parent_id TEXT); CREATE TABLE message (session_id TEXT,data TEXT,time_created INTEGER); CREATE TABLE external_import (session_id TEXT,source TEXT,source_key TEXT);')
        conn.executemany('INSERT INTO session VALUES (?,?,?,?,?)', [('native-copy', 'Imported', str(tmp_path), 1, None), ('new-child', 'Checkpoint', str(tmp_path), 2, 'native-copy')])
        conn.execute('INSERT INTO external_import VALUES (?,?,?)', ('native-copy', 'cc', 'claude-original'))
    monitor = ExternalSessions(tmp_path, roots={'mimocode': [path]}, cache_seconds=0)
    sources = {s['session_id']: s for s in monitor.discover()['sources']}
    assert sources['native-copy']['imported_from'] == {'harness': 'claude', 'session_id': 'claude-original'}
    assert sources['native-copy']['harness'] == 'mimocode'
    assert sources['new-child']['parent_session_id'] == 'native-copy'
    assert sources['new-child']['imported_from'] is None
