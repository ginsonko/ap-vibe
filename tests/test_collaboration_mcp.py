"""Real loopback MCP routing; no external model requests."""
import json
import threading

import pytest

from tools import ap_vibe_mcp
from ap_mind.studio_server import create_server


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    root = tmp_path / 'project'; root.mkdir()
    server = create_server(port=0, data_dir=tmp_path / 'data', project_root=root, codex_project_id='test-project')
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setattr(ap_vibe_mcp.task_client, '_installed_config', lambda: {
        'host':'127.0.0.1', 'port':server.server_address[1], 'auto_start':False})
    studio = server.service.agent_studio
    monkeypatch.setattr(studio, 'tick', lambda: None)
    agents = [studio.save({'name':name, 'model':'fixture', 'api_key':'fixture-secret',
        'base_url':'https://example.invalid/v1', 'role':'configured preference'})['agent'] for name in ('planner','worker','reviewer')]
    monkeypatch.setenv('AP_VIBE_AGENT_ID', agents[0]['agent_id'])
    yield studio, agents
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def save(request_id, **extra):
    return ap_vibe_mcp.invoke('ap_vibe_task_save', {'request_id':request_id, 'project_id':'test-project',
        'title':'A concrete deliverable', 'goal':'Read source and write result.md', 'acceptance':'Actual file review', **extra})


def test_task_directory_paging_claim_identity_and_archive_receipts(endpoint):
    studio, agents = endpoint
    first = save('first', eligible_agents=[agents[1]['agent_id']], reviewer_agent_id=agents[2]['agent_id'])['task']
    save('second')
    page = ap_vibe_mcp.invoke('ap_vibe_task_list', {'project_id':'test-project', 'limit':1})
    assert page['next_offset'] == 1 and 'acceptance' not in page['tasks'][0]
    following = ap_vibe_mcp.invoke('ap_vibe_task_list', {'project_id':'test-project', 'limit':1, 'offset':1})
    assert following['tasks'][0]['task_id'] == first['task_id'] and following['next_offset'] is None
    assert ap_vibe_mcp.invoke('ap_vibe_task_list', {'project_id':'another'})['tasks'] == []
    claim = {'request_id':'claim', 'task_id':first['task_id'], 'expected_version':first['version'], 'agent_id':agents[1]['agent_id']}
    result = ap_vibe_mcp.invoke('ap_vibe_task_claim', claim)
    assert result['task']['owner'] == agents[1]['agent_id']
    assert ap_vibe_mcp.invoke('ap_vibe_task_claim', claim)['replayed']
    actual = ap_vibe_mcp.invoke('ap_vibe_task_list', {'task_id':first['task_id']})
    assert actual['events'][-1]['payload']['requested_by'] == agents[0]['agent_id']
    with pytest.raises(ValueError, match='studio_task_already_owned'):
        ap_vibe_mcp.invoke('ap_vibe_task_claim', {**claim, 'request_id':'race', 'agent_id':agents[2]['agent_id'], 'expected_version':result['task']['version']})
    with pytest.raises(ValueError, match='studio_task_release_requires_stopped'):
        ap_vibe_mcp.invoke('ap_vibe_task_archive', {'request_id':'busy-archive', 'task_id':first['task_id'], 'expected_version':result['task']['version']})
    second = page['tasks'][0]
    archive = {'request_id':'archive', 'task_id':second['task_id'], 'expected_version':second['version']}
    assert ap_vibe_mcp.invoke('ap_vibe_task_archive', archive)['task']['state'] == 'archived'
    assert ap_vibe_mcp.invoke('ap_vibe_task_archive', archive)['replayed']
    assert save('second')['replayed']


def test_public_agents_and_broadcast_are_attributed_and_deduplicated(endpoint):
    _, agents = endpoint
    directory = ap_vibe_mcp.invoke('ap_vibe_agents', {})
    assert len(directory['agents']) == 3
    assert 'fixture-secret' not in json.dumps(directory) and 'base_url' not in json.dumps(directory)
    assert directory['agents'][0]['measured_capability'] is None
    metrics = ap_vibe_mcp.invoke('ap_vibe_agent_metrics', {'configuration': 'current', 'days': 7})
    assert metrics['summary']['attempts'] == 0 and metrics['summary']['estimated_cost_usd'] is None
    assert len(metrics['agents']) == 3 and 'fixture-secret' not in json.dumps(metrics)
    assert len(ap_vibe_mcp.invoke('ap_vibe_agents', {'agent_id':agents[1]['agent_id']})['agents']) == 1
    broadcast = {'request_id':'broadcast', 'sender':'not-the-managed-identity',
        'recipients':[a['agent_id'] for a in agents[1:]], 'body':'Use the saved contract; do not duplicate work.'}
    sent = ap_vibe_mcp.invoke('ap_vibe_collaboration_broadcast', broadcast)
    assert len(sent['messages']) == 2
    assert ap_vibe_mcp.invoke('ap_vibe_collaboration_broadcast', broadcast)['replayed']
    messages = ap_vibe_mcp.invoke('ap_vibe_collaboration_list', {})['messages']
    assert len(messages) == 2 and all(m['sender'] == agents[0]['agent_id'] for m in messages)
    with pytest.raises(ValueError, match='collaboration_request_conflict'):
        ap_vibe_mcp.invoke('ap_vibe_collaboration_broadcast', {**broadcast, 'body':'Different body'})


def test_unavailable_candidates_and_safe_release_through_mcp(endpoint):
    studio, agents = endpoint
    with pytest.raises(ValueError, match='studio_task_candidate_not_found'):
        save('missing', eligible_agents=['missing-agent'], auto_run=True)
    task = save('release', eligible_agents=[agents[1]['agent_id']])['task']
    # A stopped fixture exercises release without executing a model.
    with studio.registry.transaction():
        c = studio.registry._connect()
        task = studio.tasks._write(c, {**task, 'state':'needs_help'}, 'fixture-stopped', {})
    request = {'request_id':'release-stopped', 'task_id':task['task_id'], 'expected_version':task['version'], 'note':'Continue from saved evidence'}
    released = ap_vibe_mcp.invoke('ap_vibe_task_release', request)
    assert released['task']['state'] == 'queued' and released['task']['handoff_note'] == request['note']
    assert ap_vibe_mcp.invoke('ap_vibe_task_release', request)['replayed']


def test_client_retries_same_archive_after_lost_response(endpoint, monkeypatch):
    task = save('lost-response')['task']
    request = {'request_id':'lost-archive', 'task_id':task['task_id'], 'expected_version':task['version']}
    actual = ap_vibe_mcp.task_client.request.build_opener
    opener = actual(ap_vibe_mcp.task_client.request.ProxyHandler({}))
    attempts = []
    class LostResponse:
        def open(self, req, **kwargs):
            attempts.append(req.data)
            result = opener.open(req, **kwargs)
            if len(attempts) == 1:
                result.read(); result.close()
                raise TimeoutError('Simulated lost response after committed write')
            return result
    monkeypatch.setattr(ap_vibe_mcp.task_client.request, 'build_opener', lambda *_:LostResponse())
    monkeypatch.setattr(ap_vibe_mcp.task_client, '_start_daemon', lambda *_:{'status':'unavailable'})
    result = ap_vibe_mcp.invoke('ap_vibe_task_archive', request)
    assert result['replayed'] and attempts[0] == attempts[1]
