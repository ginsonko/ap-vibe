import json

import pytest

from ap_mind.session_observation import inspect
from ap_mind.contracts import ContractError


def test_codex_public_lifecycle_and_tool_metadata_only(tmp_path):
    path=tmp_path/'session.jsonl'
    records=[{'type':'session_meta','payload':{'id':'s'}},
        {'type':'event_msg','payload':{'type':'user_message','message':'整理电商素材'}},
        {'type':'event_msg','timestamp':'2026-09-11T00:00:00Z','payload':{'type':'task_started'}},
        {'type':'response_item','payload':{'type':'function_call','name':'Read','arguments':'secret'}},
        {'type':'response_item','payload':{'type':'reasoning','summary':'secret'}}]
    path.write_text('\n'.join(json.dumps(r) for r in records),encoding='utf8')
    value=inspect(path,'codex','s')
    assert value['state']=='running' and value['tool']=='Read'
    assert value['first_message']=='整理电商素材' and 'secret' not in str(value)
    with path.open('a',encoding='utf8') as f:
        f.write('\n'+json.dumps({'type':'event_msg','timestamp':'2026-09-11T00:10:00Z','payload':{'type':'turn_aborted'}}))
    assert inspect(path,'codex','s')['state']=='cancelled'
    with pytest.raises(ContractError,match='identity_changed'):inspect(path,'codex','other')


def test_claude_tool_result_is_not_turn_completion(tmp_path):
    path=tmp_path/'claude.jsonl'
    path.write_text(json.dumps({'type':'user','message':{'content':[{'type':'tool_result','content':'done'}]}}))
    assert inspect(path,'claude','c')['state']=='unknown'
    with path.open('a') as f:f.write('\n'+json.dumps({'type':'assistant','message':{'stop_reason':'end_turn'},'timestamp':'2026-09-11T00:00:00Z'}))
    assert inspect(path,'claude','c')['state']=='idle'


def test_long_turn_tool_activity_survives_missing_start_and_later_end_wins(tmp_path):
    path=tmp_path/'long.jsonl'
    records=[{'type':'session_meta','payload':{'id':'s'}},
        {'type':'event_msg','timestamp':'2026-09-11T00:00:00Z','payload':{'type':'task_started'}},
        {'type':'response_item','payload':{'type':'reasoning','summary':'x'*600000}},
        {'type':'response_item','timestamp':'2026-09-11T00:50:00Z','payload':{'type':'function_call','name':'Read','arguments':'private'}}]
    path.write_text('\n'.join(json.dumps(r) for r in records),encoding='utf8')
    value=inspect(path,'codex','s')
    assert value['state']=='running' and value['status_basis']=='public_tool_call'
    assert value['event_at']=='2026-09-11T00:50:00Z'
    assert 'private' not in str(value)
    for marker,state in [('task_complete','idle'),('task_failed','failed'),('turn_aborted','cancelled')]:
        with path.open('a') as f:
            f.write('\n'+json.dumps({'type':'event_msg','timestamp':'2026-09-11T00:51:00Z','payload':{'type':marker}}))
        assert inspect(path,'codex','s')['state']==state


def test_new_claude_tool_after_old_completed_turn_is_active(tmp_path):
    path=tmp_path/'claude.jsonl'
    path.write_text('\n'.join(json.dumps(r) for r in [
        {'type':'assistant','message':{'stop_reason':'end_turn'},'timestamp':'2026-09-11T00:00:00Z'},
        {'type':'assistant','message':{'content':[{'type':'tool_use','name':'Read','input':{'secret':'hidden'}}]},'timestamp':'2026-09-11T00:10:00Z'}]))
    value=inspect(path,'claude','c')
    assert value['state']=='running' and value['status_basis']=='public_claude_tool_use'
    assert 'hidden' not in str(value)


def test_actual_claude_api_error_marker_and_new_user_activity(tmp_path):
    path=tmp_path/'claude.jsonl'
    failure={'type':'assistant','sessionId':'c','timestamp':'2026-09-11T01:03:53Z',
             'isApiErrorMessage':True,'error':'unknown','message':{'content':[{'type':'text','text':'provider error'}]}}
    path.write_text(json.dumps(failure),encoding='utf-8')
    result=inspect(path,'claude','c')
    assert result['state']=='failed' and result['explicit_failure']
    assert result['status_basis']=='public_claude_api_error'
    with path.open('a',encoding='utf-8') as f:
        f.write('\n'+json.dumps({'type':'user','sessionId':'c','timestamp':'2026-09-11T01:04:01Z',
                                 'message':{'content':'continue the task'}}))
    assert inspect(path,'claude','c')['state']=='running'


@pytest.mark.parametrize('record,state', [
    ({'type':'result','subtype':'success','is_error':True}, 'failed'),
    ({'type':'result','subtype':'success','is_error':False}, 'idle'),
    ({'type':'result','subtype':'cancelled','is_error':True}, 'cancelled'),
    ({'type':'result','subtype':'error_max_budget_usd','is_error':True}, 'unknown'),
])
def test_claude_result_explicit_error_precedes_success_label(tmp_path,record,state):
    path=tmp_path/'result.jsonl'
    path.write_text(json.dumps(record|{'timestamp':'2026-09-11T01:03:53Z'}),encoding='utf-8')
    value=inspect(path,'claude','c')
    assert value['state']==state
    assert value['explicit_failure']==(state=='failed')
