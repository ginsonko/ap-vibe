import json
import time

from tools import task_client
from tools.install_task_context import install
from pathlib import Path


def test_stop_is_local_bounded_and_requires_current_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(task_client, '_has_task_activity', lambda *_:True)
    monkeypatch.setattr(task_client, 'cache_path', lambda *_: tmp_path/'receipt.json')
    monkeypatch.setattr(task_client, 'call', lambda *_: (_ for _ in ()).throw(AssertionError('Stop must not call service')))
    assert task_client.stop_hook_output({}, 'cwd', 'session') == {}
    task_client.save_receipt('cwd', 'session', {'receipt_id':'r1','project_id':'p','session_id':'session','created_at':'now'})
    assert task_client.stop_hook_output({}, 'cwd', 'session')['decision'] == 'block'
    assert task_client.stop_hook_output({'stop_hook_active':True}, 'cwd', 'session') == {}
    assert task_client.stop_hook_output({}, 'cwd', 'another') == {}
    task_client.mark_document_maintained('cwd', 'session', 'r1')
    assert task_client.stop_hook_output({}, 'cwd', 'session') == {}
    task_client.save_receipt('cwd', 'session', {'receipt_id':'r2','project_id':'p','session_id':'session','created_at':'now'})
    assert task_client.stop_hook_output({}, 'cwd', 'session')['decision'] == 'block'
    monkeypatch.setenv('AP_VIBE_READONLY_CURATION','1')
    assert task_client.stop_hook_output({}, 'cwd', 'session') == {}


def test_expired_receipt_and_corrupt_cache_do_not_prevent_delivery(tmp_path, monkeypatch):
    path = tmp_path/'receipt.json'
    monkeypatch.setattr(task_client, 'cache_path', lambda *_: path)
    for value in ['{broken', json.dumps({'receipt_id':'r','session_id':'s','client_saved_at':time.time()-90000})]:
        path.write_text(value,encoding='utf8')
        assert task_client.stop_hook_output({},'cwd','s') == {}


def test_reinstall_preserves_other_stop_hooks_without_duplicate(tmp_path):
    codex=tmp_path/'.codex';codex.mkdir()
    path=codex/'hooks.json'
    path.write_text(json.dumps({'hooks':{'Stop':[{'hooks':[{'type':'command','command':'other-stop'}]}]}}),encoding='utf8')
    for _ in range(2): install(Path(__file__).resolve().parents[1],codex,'C:/Python/python.exe')
    commands=[h['command'] for g in json.loads(path.read_text(encoding='utf8'))['hooks']['Stop'] for h in g['hooks']]
    assert commands.count('other-stop') == 1
    assert len(commands) == 2
    hooks = json.loads(path.read_text(encoding='utf8'))['hooks']
    assert all('additionalContextLimit' not in h for group in hooks['Stop'] for h in group['hooks'])
    assert hooks['SessionStart'][-1]['hooks'][0]['additionalContextLimit'] == 2000


def test_plain_answer_and_previous_turn_do_not_trigger_document_continuation(tmp_path):
    transcript=tmp_path/'task.jsonl'
    meta={'type':'session_meta','payload':{'id':'s','cwd':str(tmp_path)}}
    events=[meta,{'type':'event_msg','payload':{'type':'item_completed','turn_id':'old','item':{'type':'FileChange'}}}]
    transcript.write_text('\n'.join(json.dumps(e) for e in events)+'\n',encoding='utf8')
    hook={'transcript_path':str(transcript),'turn_id':'now'}
    assert not task_client._has_task_activity(hook,str(tmp_path),'s')
    with transcript.open('a',encoding='utf8') as stream:
        stream.write(json.dumps({'type':'event_msg','payload':{'type':'item_completed','turn_id':'now','item':{'type':'CommandExecution'}}})+'\n')
    assert task_client._has_task_activity(hook,str(tmp_path),'s')
    assert not task_client._has_task_activity(hook,str(tmp_path),'other-task')
