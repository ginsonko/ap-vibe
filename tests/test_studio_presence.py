"""Projection evidence tests; fixtures do not establish real model performance."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from ap_mind.studio_presence import location, snapshot
from test_agent_studio import studio, profile


def test_messages_project_saved_text_only_and_broadcast_redacts(studio, tmp_path):
    from ap_mind.collaboration import CollaborationStore
    store = CollaborationStore(tmp_path / 'messages.sqlite')
    studio.service.collaboration = store
    a = profile(studio)
    sent = store.broadcast({'request_id': 'broadcast-test', 'sender': a['agent_id'],
                           'recipients': ['other'], 'body': 'sk-' + 'F' * 30 + '公开说明' * 100})
    value = snapshot(studio)
    assert len(value['messages']) == 1
    message = value['messages'][0]
    assert message['message_id'] == sent['messages'][0]['message_id']
    assert 'sk-' not in message['body']
    assert message['truncated'] and len(message['body']) == 300
    assert 'request_id' not in message


def test_locations_use_structured_events_not_prompt():
    run={'state':'running','prompt':'I am writing frontend tests and reading everything'}
    assert location(run,None)[0]=='planning'
    assert location(run,{'kind':'tool','tool':'Read'})[0]=='library'
    assert location(run,{'kind':'tool','tool':'mcp__ap-vibe__ap_vibe_read'})[0]=='library'
    assert location(run,{'kind':'tool_result','tool':'Read'})[0]=='planning'
    assert location(run,{'kind':'tool','tool':'file_change'})[0]=='engineering'
    assert location(run,{'kind':'tool','tool':'command_execution'})[0]=='planning'
    assert location(run,None,review=True)[0]=='review'
    for state in ['uncertain','failed','interrupted','cancelling']:
        assert location({'state':state},{'kind':'tool','tool':'Read'})==('waiting','需要留意','wait')
    assert location({'state':'awaiting_review'},None)[0]=='review'
    assert location({'state':'completed'},None)==('rest','已验收','rest')


def test_presence_keeps_older_active_runs_beyond_history_limit(studio):
    a=profile(studio)
    with studio.registry.transaction():
        c=studio.registry._connect()
        for i in range(85):
            run={'agent_id':a['agent_id'],'name':a['name'],'project_id':'test-project','prompt':'fixture '+str(i),'created_at':f'2026-09-01T00:00:{i:03d}Z'}
            c.execute('INSERT INTO studio_runs VALUES (?,?,?,?,?,?)',(f'run-{i}',f'request-{i}','fixture',a['agent_id'],'running' if i<2 else 'completed',json.dumps(run)))
    studio._event('run-0','tool',{'text':'Reading fixture','tool':'Read','private':'must not be projected'})
    result=snapshot(studio)
    assert result['active_count']==2
    assert {r['run_id'] for r in result['runs']}=={'run-0','run-1','run-84'}
    assert next(r for r in result['runs'] if r['run_id']=='run-0')['room']=='library'
    assert 'must not be projected' not in json.dumps(result)
    studio._state('run-0','uncertain')
    updated=snapshot(studio)
    assert {r['run_id'] for r in updated['runs']}=={'run-0','run-1'}
    assert next(r for r in updated['runs'] if r['run_id']=='run-0')['room']=='waiting'
