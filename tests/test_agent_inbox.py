"""Real local HTTP/stdio delivery; fixtures do not prove model adoption."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from ap_mind.collaboration import CollaborationStore
from ap_mind.studio_server import create_server
from ap_mind import agent_studio
from tools import ap_vibe_mcp, agent_inbox


def test_ordered_inbox_pages_do_not_lose_bursts_or_mix_targets(tmp_path):
    path = tmp_path / 'messages.sqlite'
    store = CollaborationStore(path)
    ids = []
    for i in range(45):
        store.send({'request_id': f'neighbor-{i}', 'sender': 'peer', 'recipient': 'worker',
                    'task_id': 'other-task', 'body': 'unrelated'})
        item = store.send({'request_id': f'current-{i}', 'sender': 'peer', 'recipient': 'worker',
                          'task_id': 'logical-current', 'body': f'消息 {i}'})
        ids.append(item['message_id'])
    store.send({'request_id': 'unscoped', 'sender': 'peer', 'recipient': 'worker', 'body': 'old rules'})
    store.send({'request_id': 'other-recipient', 'sender': 'peer', 'recipient': 'neighbor',
                'task_id': 'logical-current', 'body': 'another recipient'})
    found, cursor = [], 0
    while True:
        page = store.inbox('run-current', 'worker', ['logical-current'], after=cursor)
        found.extend(m['message_id'] for m in page['messages'])
        cursor = page['next_cursor']
        if not page['has_more']:
            break
    assert found == ids
    assert not store.inbox('run-current', 'worker', ['logical-current'], after=cursor)['messages']
    # Reopening keeps the original stream and cursors. Read-only consumption
    # never erases messages needed by another reader or a resumed process.
    restored = CollaborationStore(path)
    assert restored.inbox('run-current', 'worker', ['logical-current']) == store.inbox('run-current', 'worker', ['logical-current'])
    assert len(restored.list()['messages']) == 92


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    root = tmp_path / 'project'; root.mkdir()
    server = create_server(port=0, data_dir=tmp_path/'data', project_root=root, codex_project_id='test-project')
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    studio = server.service.agent_studio
    monkeypatch.setattr(studio, '_execute', lambda *_: None)
    monkeypatch.setattr(studio, 'tick', lambda: None)
    monkeypatch.setattr(agent_studio, 'claude_executable', lambda: '/fixture/claude')
    agent = studio.save({'name': 'worker', 'model': 'fixture', 'api_key': 'fixture-secret',
                         'base_url': 'https://example.invalid/v1'})['agent']
    run = studio.start({'request_id': 'fixture-run', 'agent_id': agent['agent_id'],
                        'project_id': 'test-project', 'prompt': 'Local transport fixture'})
    config = tmp_path/'client.json'
    config.write_text(json.dumps({'host':'127.0.0.1', 'port':server.server_port, 'auto_start':False}), encoding='utf-8')
    monkeypatch.setenv('AP_VIBE_CONFIG_PATH', str(config))
    monkeypatch.setenv('AP_VIBE_RUN_ID', run['run_id'])
    monkeypatch.setenv('AP_VIBE_AGENT_ID', agent['agent_id'])
    yield studio, agent['agent_id'], run['run_id']
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_real_stdio_gets_message_sent_after_start_without_polling(endpoint):
    studio, agent, run = endpoint
    def launch():
        return subprocess.Popen([sys.executable, str(Path(ap_vibe_mcp.__file__))], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', env=dict(os.environ))
    def ask(proc, identity, name='ap_vibe_agents', arguments=None):
        proc.stdin.write(json.dumps({'id': identity, 'method':'tools/call',
            'params':{'name':name, 'arguments':arguments or {}}})+'\n'); proc.stdin.flush()
        return json.loads(proc.stdout.readline())['result']
    proc = launch()
    try:
        first = ask(proc, 1)
        assert not first['isError'] and len(first['content']) == 1
        sent = studio.send_message({'request_id':'after-start', 'sender':'peer', 'recipient':agent,
            'task_id':run, 'body':'新发现：汇总按 cents，不能猜币种。'})
        second = ask(proc, 2)
        extra = json.loads(second['content'][1]['text'])
        assert extra['messages'][0]['message_id'] == sent['message_id']
        assert extra['delivery'] == 'returned_with_tool_result' and not second['isError']
        assert len(ask(proc, 3)['content']) == 1
        read = ask(proc, 4, 'ap_vibe_inbox')
        assert len(read['content']) == 1 and json.loads(read['content'][0]['text'])['messages'][0]['message_id'] == sent['message_id']
    finally:
        proc.stdin.close(); proc.wait(timeout=10)
    proc = launch()
    try:
        repeated = ask(proc, 5)
        assert json.loads(repeated['content'][1]['text'])['messages'][0]['message_id'] == sent['message_id']
    finally:
        proc.stdin.close(); proc.wait(timeout=10)


def test_optional_failure_preserves_committed_result_and_recovers(endpoint, monkeypatch):
    studio, agent, run = endpoint
    delivery = agent_inbox.InboxDelivery()
    monkeypatch.setattr(ap_vibe_mcp, 'inbox_delivery', delivery)
    real_invoke = ap_vibe_mcp.invoke
    monkeypatch.setattr(ap_vibe_mcp, 'invoke', lambda *_: {'ok':True,'revision':9,'request_id':'committed-once'})
    actual = agent_inbox.task_client.request.build_opener
    class Down:
        def open(self, *_args, **_kwargs):
            raise TimeoutError('optional inbox unavailable')
    monkeypatch.setattr(agent_inbox.task_client.request, 'build_opener', lambda *_: Down())
    call = {'id':1, 'method':'tools/call','params':{'name':'ap_vibe_update','arguments':{}}}
    failed = ap_vibe_mcp.handle(call)['result']
    assert not failed['isError'] and json.loads(failed['content'][0]['text'])['revision'] == 9
    assert json.loads(failed['content'][1]['text'])['delivery'] == 'temporarily_unavailable'
    assert delivery.cursor == 0 and len(ap_vibe_mcp.handle(call)['result']['content']) == 1
    studio.send_message({'request_id':'during-outage','sender':'peer','recipient':agent,'task_id':run,'body':'仍可恢复'})
    monkeypatch.setattr(agent_inbox.task_client.request, 'build_opener', actual)
    recovered = ap_vibe_mcp.handle(call)['result']
    assert not recovered['isError'] and json.loads(recovered['content'][1]['text'])['messages'][0]['body'] == '仍可恢复'
    monkeypatch.setattr(ap_vibe_mcp, 'invoke', real_invoke)


def test_logical_task_bubbles_and_broadcast_do_not_touch_other_runs(endpoint):
    studio, agent, run = endpoint
    studio._state(run, 'running', logical_task_id='logical-current')
    # A second persisted history row is deliberately outside current scope.
    with studio.registry.transaction():
        c = studio.registry._connect()
        c.execute("INSERT INTO studio_runs(run_id,request_id,fingerprint,agent_id,state,payload_json) VALUES (?,?,?,?,?,?)",
            ('other-run','other-request','fixture-fingerprint',agent,'completed',json.dumps({'agent_id':agent,'logical_task_id':'other'})))
    sent = studio.send_message({'request_id':'logical','sender':'peer','recipient':agent,
        'task_id':'logical-current','body':'当前任务消息'})
    studio.broadcast_message({'request_id':'wrong-scope','sender':'peer','recipients':[agent],
        'task_id':'other','body':'其它任务广播'})
    assert studio.runs(run)['events'][-1]['message_id'] == sent['message_id']
    assert studio.runs('other-run')['events'] == []
    inbox = ap_vibe_mcp.invoke('ap_vibe_inbox', {})
    assert [m['body'] for m in inbox['messages']] == ['当前任务消息']
    assert ap_vibe_mcp.invoke('ap_vibe_inbox', {'run_id':'other-run'})['messages'][0]['body'] == '其它任务广播'
