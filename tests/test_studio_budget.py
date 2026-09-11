"""Local accounting and scheduler behavior; no paid model calls."""
import json
import urllib.request
import urllib.error

import pytest

from test_agent_studio import studio, profile
from ap_mind.contracts import ContractError
from ap_mind.claude_gateway import ClaudeGateway
from ap_mind import agent_studio


def test_no_prices_runs_normally_even_with_amount_limit_and_unknown_usage(studio):
    agent = profile(studio)['agent_id']
    studio.budget.save({'agent_id':agent,'request_id':'save','expected_revision':0,'amount_limit':0})
    studio.budget.observe(agent,'run-1','r1',{'input_tokens':90,'output_tokens':10})
    studio.budget.observe(agent,'run-1','r2')
    result = studio.budget.status(agent)
    assert result['tokens_used'] == 100 and result['amount_used'] == 0
    assert result['unknown_usage_requests'] == 1 and result['unestimated_requests'] == 2
    assert result['price_status'] == 'unpriced' and not result['exhausted']
    assert studio.budget.check(agent) is None


def test_settlement_dedup_prices_frozen_feed_idempotent_and_restart(studio):
    agent = profile(studio)['agent_id']
    studio.budget.save({'agent_id':agent,'request_id':'save','expected_revision':0,'token_limit':100,
        'prices':{'input':2,'output':4,'cache_read':1,'cache_write':3}})
    studio.budget.observe(agent,'run-1','r1')
    # OpenAI input is inclusive: 60 uncached + 20 read + 10 write + 10 output.
    raw = {'prompt_tokens':90,'completion_tokens':10,'prompt_tokens_details':{'cached_tokens':20,'cache_write_tokens':10}}
    studio.budget.observe(agent,'run-1','r1',raw,'openai')
    studio.budget.observe(agent,'run-1','r1',raw,'openai')
    studio.budget.observe(agent,'run-1','r1')
    result = studio.budget.status(agent)
    assert result['tokens_used'] == 100 and result['request_count'] == 1
    assert result['amount_used'] == pytest.approx(.00021) and result['exhausted']
    feed = {'agent_id':agent,'request_id':'feed','expected_revision':1,'tokens':50}
    assert not studio.budget.feed(feed)['budget']['exhausted']
    assert studio.budget.feed(feed)['replayed']
    with pytest.raises(ContractError, match='request_conflict'):
        studio.budget.feed({**feed,'tokens':999})
    studio.budget.save({'agent_id':agent,'request_id':'reprice','expected_revision':2,'token_limit':150,
        'prices':{'input':200,'output':400}})
    from ap_mind.studio_budget import StudioBudget
    reopened = StudioBudget(studio)
    assert reopened.status(agent)['amount_used'] == pytest.approx(.00021)
    assert reopened.status(agent)['token_remaining'] == 50
    with pytest.raises(ContractError, match='identity_conflict'):
        reopened.observe(agent,'different-run','r1',raw,'openai')


def test_budget_gateway_refuses_before_submission_without_provider_failure():
    events=[]
    gateway = ClaudeGateway('http://127.0.0.1:1/v1','test','test',events.append,
        before_request=lambda:'budget empty').start()
    try:
        for _ in range(2):
            req=urllib.request.Request(gateway.url+'/v1/messages',data=json.dumps({'messages':[]}).encode(),headers={'x-api-key':gateway.token})
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req,timeout=2)
            assert caught.value.code == 400
        assert gateway.failure is None and gateway.paused == 'budget empty'
        assert not any(e['phase']=='submitted' for e in events)
    finally:
        gateway.close()


def test_auto_task_waits_for_feed_and_preserves_artifacts_without_failure_fallback(studio, monkeypatch):
    monkeypatch.setattr(agent_studio,'claude_executable',lambda:'/fixture/claude')
    monkeypatch.setattr(studio,'_execute',lambda *args:None)
    agent = profile(studio)['agent_id']
    task = studio.tasks.save({'request_id':'work','project_id':'test-project','title':'work','goal':'file',
        'acceptance':'read file','eligible_agents':[agent],'auto_run':True})['task']
    studio.tasks.tick()
    task = studio.tasks.list(task['task_id'])['tasks'][0]
    run_id=task['run_id']
    studio.budget.save({'agent_id':agent,'request_id':'budget','expected_revision':0,'token_limit':0})
    studio._threads.clear()
    studio._state(run_id,'budget_paused',exit_code=1)
    studio.tasks.tick()
    paused=studio.tasks.list(task['task_id'])['tasks'][0]
    assert paused['state']=='budget_waiting' and len(paused['attempts'])==1
    studio.tasks.tick()
    assert studio.tasks.list(task['task_id'])['tasks'][0]['version']==paused['version']
    studio.budget.feed({'agent_id':agent,'request_id':'feed','expected_revision':1,'tokens':100})
    studio.tasks.tick()
    resumed=studio.tasks.list(task['task_id'])['tasks'][0]
    assert resumed['state']=='running' and len(resumed['attempts'])==2
    run=studio.runs(resumed['run_id'])['runs'][0]
    assert run['handoff_from_run_id']==run_id and run['fork_workspace']


def test_persona_is_optional_preserved_and_not_changed_by_budget(studio):
    a=profile(studio,persona='平和，解释有依据。')
    saved=profile(studio,agent_id=a['agent_id'],expected_revision=1,api_key='')
    assert saved['persona']==a['persona']
    studio.budget.save({'agent_id':a['agent_id'],'request_id':'price','expected_revision':0,'prices':{'input':1,'output':2}})
    assert studio.profiles()['agents'][0]['revision']==2
    for field in ('token_limit','amount_limit'):
        with pytest.raises(ContractError,match='invalid'):
            studio.budget.save({'agent_id':a['agent_id'],'request_id':'bad-'+field,'expected_revision':1,field:float('nan')})
