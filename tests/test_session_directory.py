"""Ordinary harness context, without model calls or collector mutations."""
import hashlib
import json
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from ap_mind.claude_sessions import ClaudeSessions
from ap_mind.product import DiscoveredCodexSession
from ap_mind.session_window import public_window
from ap_mind.studio_server import create_server
from tools import ap_vibe_mcp, task_client


def codex(i, text=None, **extra):
    return {'timestamp':'2026-09-10T01:00:00Z', 'type':'response_item', 'payload':{
        'type':'message', 'role':'assistant', 'channel':'final', 'id':str(i),
        'content':[{'type':'output_text', 'text': text or f'message-{i}'}], **extra}}


def claude(i, text=None):
    return {'type':'assistant', 'uuid':str(i), 'sessionId':'ordinary-claude', 'cwd':'C:/fixture',
        'message':{'content':[{'type':'text', 'text': text or f'message-{i}'}]}}


def write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b''.join((json.dumps(item, ensure_ascii=False) + '\n').encode() for item in records))


@pytest.fixture
def server(tmp_path, monkeypatch):
    root = tmp_path/'work'; root.mkdir()
    endpoint = create_server(port=0, data_dir=tmp_path/'data', project_root=root, codex_project_id='fixture-project')
    service = endpoint.service
    service.claude_sessions = ClaudeSessions([tmp_path/'claude-projects'], cache_seconds=0)
    monkeypatch.setattr(service.agent_studio, 'tick', lambda: None)
    monkeypatch.setattr(service, '_codex_titles', lambda:{'one':'Same title', 'two':'Same title'})
    monkeypatch.setattr(task_client, '_installed_config', lambda:{'host':'127.0.0.1', 'port':endpoint.server_address[1], 'auto_start':False})
    thread = threading.Thread(target=endpoint.serve_forever, daemon=True); thread.start()
    yield service, root
    endpoint.shutdown(); endpoint.server_close(); thread.join(timeout=2)


def register(service, root, name, session, records):
    path = root/(name+'.jsonl'); write(path, records)
    key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    service.product_registry.register_source(DiscoveredCodexSession(
        source_key=key, source_path=str(path), source_name=path.name, session_id=session,
        cwd=str(root), source_size=path.stat().st_size, modified_at='2026-09-10T01:00:00Z'), 'fixture-project')
    return path, 'codex-'+key


def test_latest_not_first32_and_history_incremental_preserve_all(server):
    service, root = server
    path, sid = register(service, root, 'first', 'one', [codex(i) for i in range(75)])
    before = service.product_registry.source(sid[6:])
    page = ap_vibe_mcp.invoke('ap_vibe_session_read', {'source_id':sid, 'limit':7})
    assert [e['text'] for e in page['events']] == [f'message-{i}' for i in range(68,75)]
    latest = page
    seen = {e['text'] for e in page['events']}
    for _ in range(20):
        if not page['has_older']:
            break
        page = service.session_directory.read(sid, before=page['history_before'], expected_generation=page['generation'], limit=7)
        seen.update(e['text'] for e in page['events'])
    assert seen == {f'message-{i}' for i in range(75)}
    for i in range(75,80):
        with path.open('ab') as stream:
            stream.write((json.dumps(codex(i))+'\n').encode())
    incremental = service.session_directory.read(sid, after=latest['cursor'], expected_generation=latest['generation'], limit=2)
    assert [e['text'] for e in incremental['events']] == ['message-75','message-76']
    assert incremental['has_more']
    remaining = service.session_directory.read(sid, after=incremental['cursor'], expected_generation=incremental['generation'])
    assert [e['text'] for e in remaining['events']] == ['message-77','message-78','message-79']
    after = service.product_registry.source(sid[6:])
    assert after.cursor == before.cursor and after.last_batch == before.last_batch


def test_unclassified_cross_harness_discovery_title_collision_and_filters(server):
    service, root = server
    _, one = register(service, root, 'one', 'one', [codex(1)])
    _, newer = register(service, root, 'resumed', 'one', [codex(2)])
    register(service, root, 'two', 'two', [codex(3)])
    write(service.claude_sessions.roots[0]/'encoded'/'ordinary-claude.jsonl', [claude(0)])
    page = ap_vibe_mcp.invoke('ap_vibe_sessions', {'limit':1})
    assert page['total'] == 3 and page['next_offset'] == 1
    directory = ap_vibe_mcp.invoke('ap_vibe_sessions', {'harness':'codex', 'cwd':str(root)})
    assert len(directory['sessions']) == 2
    same = next(s for s in directory['sessions'] if s['session_id']=='one')
    assert {s['source_id'] for s in same['sources']} == {one,newer}
    assert len(ap_vibe_mcp.invoke('ap_vibe_sessions', {'query':'Same title'})['sessions']) == 2
    cc = ap_vibe_mcp.invoke('ap_vibe_sessions', {'harness':'claude'})['sessions'][0]
    assert cc['project_id'] is None and cc['membership_basis'] == 'unclassified'
    assert ap_vibe_mcp.invoke('ap_vibe_session_read', {'source_id':cc['source_id']})['events'][0]['text'] == 'message-0'
    assert ap_vibe_mcp.invoke('ap_vibe_sessions', {'cwd':str(root/'missing')})['sessions'] == []
    assert ap_vibe_mcp.invoke('ap_vibe_sessions', {'session_id':'two'})['total'] == 1


def test_partial_rotation_and_invalid_json_do_not_stick_or_skip(server):
    service, root = server
    path, sid = register(service, root, 'one', 'one', [codex(1)])
    original = service.session_directory.read(sid)
    second = (json.dumps(codex(2))+'\n').encode()
    with path.open('ab') as stream:
        stream.write(second[:40])
    partial = service.session_directory.read(sid, after=original['cursor'], expected_generation=original['generation'])
    assert partial['cursor'] == original['cursor'] and partial['events'] == []
    with path.open('ab') as stream:
        stream.write(second[40:]+b'bad-json\n'+(json.dumps(codex(3))+'\n').encode())
    next_page = service.session_directory.read(sid, after=partial['cursor'], expected_generation=partial['generation'])
    assert [e['text'] for e in next_page['events']] == ['message-2','message-3']
    assert next_page['invalid_lines'] == 1
    replacement = root/'replacement'; write(replacement,[codex(4)])
    replacement.replace(path)
    reset = service.session_directory.read(sid, after=next_page['cursor'], expected_generation=next_page['generation'])
    assert reset['reset'] and reset['events'][0]['text'] == 'message-4'
    with pytest.raises(ValueError, match='cursor_conflict'):
        ap_vibe_mcp.invoke('ap_vibe_session_read', {'source_id':sid,'after':0,'before':1})


def test_claude_multi_block_is_one_record_and_public_only(server):
    service, root = server
    record = claude(0)
    record['message']['content'] = [{'type':'thinking','thinking':'PRIVATE_REASONING'},
        {'type':'text','text':'First'}, {'type':'tool_use','name':'Read','input':{'secret':'PRIVATE_INPUT'}},
        {'type':'text','text':'Last'}]
    write(service.claude_sessions.roots[0]/'encoded'/'ordinary-claude.jsonl', [record,claude(1),
        {**claude(2,'PRIVATE_SKILL'),'isMeta':True}])
    sid = service.claude_sessions.discover()['sources'][0]['source_id']
    page = service.session_directory.read(sid, after=0, limit=1)
    assert 'First' in page['events'][0]['text'] and 'Last' in page['events'][0]['text']
    assert 'PRIVATE' not in json.dumps(page)
    assert service.session_directory.read(sid, after=page['cursor'])['events'][0]['text'] == 'message-1'


def test_response_budget_holds_for_long_unicode_messages(server):
    service, root = server
    records = [claude(i, '\u6c49'*64000) for i in range(10)]
    write(service.claude_sessions.roots[0]/'encoded'/'ordinary-claude.jsonl', records)
    sid = service.claude_sessions.discover()['sources'][0]['source_id']
    page = ap_vibe_mcp.invoke('ap_vibe_session_read', {'source_id':sid, 'limit':50})
    assert page['events'] and page['has_older']
    assert len(json.dumps(page,ensure_ascii=False).encode()) < 512*1024
    assert page['events'][-1]['text_may_be_truncated']


def test_claude_tool_result_is_not_user_speech(server):
    service, root = server
    result = {**claude(0), 'type': 'user', 'message': {'content': [
        {'type': 'tool_result', 'content': 'PRIVATE_TOOL_PAYLOAD'}]}}
    mixed = {**claude(1), 'type': 'user', 'message': {'content': [
        {'type': 'text', 'text': 'Please continue'}, {'type': 'tool_result', 'is_error': True}]}}
    write(service.claude_sessions.roots[0]/'encoded'/'ordinary-claude.jsonl', [result, mixed])
    sid = service.claude_sessions.discover()['sources'][0]['source_id']
    page = service.session_directory.read(sid)
    assert [event['role'] for event in page['events']] == ['tool', 'user']
    assert 'PRIVATE_TOOL_PAYLOAD' not in json.dumps(page)
    assert 'Please continue' in page['events'][1]['text']


def test_small_byte_windows_keep_crossing_records(tmp_path):
    path = tmp_path/'source.jsonl'; write(path,[{'text':'x'*330+str(i)} for i in range(35)])
    project = lambda record,start,end:{'text':record['text']}
    page = public_window(path,project,window_bytes=4096,limit=4)
    seen = {e['text'] for e in page['events']}
    for _ in range(25):
        if not page['has_older']:
            break
        page = public_window(path,project,before=page['history_before'],expected_generation=page['generation'],window_bytes=4096,limit=4)
        seen.update(e['text'] for e in page['events'])
    assert seen == {'x'*330+str(i) for i in range(35)}


def test_hook_and_installed_reference_keep_discovery_entry(server):
    service, root = server
    value = service.task_context.bootstrap({'request_id':'sessions-hook', 'session_id':'new', 'cwd':str(root), 'goal':'Continue other application'})
    assert value['session_context']['list_tool'] == 'ap_vibe_sessions'
    assert 'ap_vibe_sessions' in task_client.hook_context(value)
    repo = Path(__file__).resolve().parents[1]
    for name in ('ap-vibe-task-context','ap-vibe-claude'):
        assert 'session-continuation.md' in (repo/'skills'/name/'SKILL.md').read_text(encoding='utf-8')


def test_local_recovery_preserves_artifact_paths_but_never_credentials(server):
    from ap_mind.product import redact_portable
    service, root = server
    text = 'Result C:\\work\\project\\result.md with sk-test-secret-1234567890'
    _, sid = register(service, root, 'paths', 'one', [codex(1,text)])
    result = service.session_directory.read(sid)['events'][0]['text']
    assert 'C:\\work\\project\\result.md' in result and 'sk-test' not in result
    assert 'C:\\work' not in redact_portable(text)
    write(service.claude_sessions.roots[0]/'encoded'/'ordinary-claude.jsonl', [claude(1,text)])
    cc = service.claude_sessions.discover()['sources'][0]['source_id']
    result = service.session_directory.read(cc)['events'][0]['text']
    assert 'C:\\work\\project\\result.md' in result and 'sk-test' not in result
