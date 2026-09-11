"""Studio Claude attempts must be discoverable with ordinary sessions."""
import json

from test_session_directory import server, claude, write


def managed(service, session='claude-managed'):
    studio = service.agent_studio
    run_id = 'run-fixture-claude'
    payload = {'session_id': session, 'executor_kind': 'claude', 'project_id': 'fixture-project',
        'agent_id': 'agent-author', 'name': 'Opus', 'model': 'claude-opus-5',
        'created_at': '2026-09-11T00:00:00Z', 'updated_at': '2026-09-11T00:01:00Z',
        'workspace': str(studio.root / 'work'), 'task_snapshot': {'title': '地图空间改造'}}
    with studio.registry.transaction():
        studio.registry._connect().execute('INSERT INTO studio_runs VALUES (?,?,?,?,?,?)',
            (run_id, 'req-fixture', 'fingerprint', 'agent-author', 'uncertain', json.dumps(payload)))
    return run_id


def test_failed_claude_visible_in_unified_filter_and_not_duplicated(server):
    service, _ = server
    run_id = managed(service, 'ordinary-claude')
    path = service.claude_sessions.roots[0] / 'ordinary-claude.jsonl'
    write(path, [claude(1)])
    sessions = service.session_directory.catalog(harness='claude')['sessions']
    matches = [s for s in sessions if s['session_id'] == 'ordinary-claude']
    assert len(matches) == 1
    source = matches[0]
    assert source['managed'] and source['source_id'] == 'studio-' + run_id
    assert source['model'] == 'claude-opus-5' and source['title'] == '地图空间改造'
    assert source['state'] == 'uncertain'
    assert not service.session_directory.catalog(harness='codex')['sessions']
    assert service.session_directory.catalog(query='claude-opus-5')['total'] == 1


def test_managed_public_read_history_and_incremental_keep_tool_error(server):
    service, _ = server
    run_id = managed(service)
    studio = service.agent_studio
    for index in range(9):
        studio._event(run_id, 'assistant', {'text': f'public-{index}', 'raw_private': 'not-visible'})
    first = service.session_directory.read('studio-' + run_id, limit=3)
    assert [e['text'] for e in first['events']] == ['public-6', 'public-7', 'public-8']
    assert first['has_older']
    prior = service.session_directory.read('studio-' + run_id, before=first['history_before'], limit=3)
    assert [e['text'] for e in prior['events']] == ['public-3', 'public-4', 'public-5']
    studio._event(run_id, 'provider', {'text': '模型请求未成功', 'error': 'provider changed tool id for block 0'})
    fresh = service.session_directory.read('studio-' + run_id, after=first['cursor'], expected_generation=first['generation'])
    assert len(fresh['events']) == 1 and 'provider changed tool id' in fresh['events'][0]['text']
    assert 'not-visible' not in json.dumps(first)
    assert fresh['state'] == 'uncertain' and fresh['model_source'] == 'run_configuration'
