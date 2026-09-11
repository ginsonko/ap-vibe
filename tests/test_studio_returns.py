import json

import pytest

from test_agent_studio import studio
from ap_mind import studio_returns
from ap_mind.contracts import ContractError


def enqueue(studio, return_id='return:plan'):
    studio.returns.enqueue(return_id,'plan',{'harness':'codex','session_id':'parent','wake':True},'Original result')


def test_missing_source_recovers_same_return_after_backoff_and_restart(studio,monkeypatch):
    now=[1000.0];attempts=[];records={}
    monkeypatch.setattr(studio_returns.time,'time',lambda:now[0])
    messages=studio.service.codex_messages
    monkeypatch.setattr(messages,'read_delivery',lambda sid,rid:records.get(rid))
    monkeypatch.setattr(messages,'resume_queued_delivery',lambda *a:True)
    def deliver(raw):
        attempts.append(raw)
        if len(attempts)==1:raise ContractError('message_session_not_found')
        records[raw['request_id']]={'status':'queued','request_id':raw['request_id']}
        return {'delivery':records[raw['request_id']]}
    monkeypatch.setattr(messages,'enqueue',deliver)
    enqueue(studio);studio.returns.tick();studio.returns.tick()
    first=studio.returns.list('plan')[0]
    assert len(attempts)==1 and first['wake_retry_at']==1005
    studio.returns=studio_returns.StudioReturns(studio)
    now[0]=1006;studio.returns.tick();studio.returns.tick()
    result=studio.returns.list('plan')[0]
    assert len(attempts)==2 and attempts[0]==attempts[1]
    assert result['state']=='wake_queued' and 'wake_issue' not in result
    assert result['message_id']==first['message_id']
    assert len(studio.sessions.inbox('codex','parent')['messages'])==1


@pytest.mark.parametrize('status',['submitted','uncertain','running','failed','completed','cancelled','queued'])
def test_ack_loss_uses_existing_delivery_and_never_enqueues_again(studio,monkeypatch,status):
    messages=studio.service.codex_messages
    records={};resumed=[]
    monkeypatch.setattr(messages,'read_delivery',lambda sid,rid:records.get(rid))
    monkeypatch.setattr(messages,'resume_queued_delivery',lambda *a:resumed.append(a))
    def enqueue_lost(raw):
        records[raw['request_id']]={'status':status}
        raise OSError('ack lost')
    monkeypatch.setattr(messages,'enqueue',enqueue_lost)
    enqueue(studio);studio.returns.tick()
    monkeypatch.setattr(messages,'enqueue',lambda raw:pytest.fail('duplicate enqueue'))
    studio.returns=studio_returns.StudioReturns(studio)
    studio.returns.tick();result=studio.returns.list('plan')[0]
    assert result['state']=='wake_queued' and result['wake_status']==status
    assert bool(resumed)==(status=='queued')
    assert len(studio.sessions.inbox('codex','parent')['messages'])==1


def test_read_is_passive_and_bad_return_does_not_block_the_next(studio,monkeypatch):
    enqueue(studio,'broken');enqueue(studio,'working')
    messages=studio.service.codex_messages;calls=[]
    def read(sid,rid):
        if rid.startswith('broken'):raise OSError('unavailable local file')
    monkeypatch.setattr(messages,'read_delivery',read)
    monkeypatch.setattr(messages,'enqueue',lambda raw: calls.append(raw) or {'delivery':{'status':'queued'}})
    studio.returns.tick()
    assert [x['request_id'] for x in calls]==['working:wake']
    studio.returns.list('plan')
    assert len(calls)==1


def test_invalid_target_retains_issue_without_endless_retry(studio,monkeypatch):
    enqueue(studio);messages=studio.service.codex_messages;calls=[]
    monkeypatch.setattr(messages,'enqueue',lambda raw:calls.append(raw) or (_ for _ in ()).throw(ContractError('message_session_id_invalid')))
    studio.returns.tick()
    monkeypatch.setattr(studio_returns.time,'time',lambda:99999999999)
    studio.returns.tick()
    assert len(calls)==1 and studio.returns.list('plan')[0]['wake_retry_blocked']
