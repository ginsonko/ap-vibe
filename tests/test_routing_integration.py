import json
import pytest
from test_agent_studio import studio, profile
from test_studio_metrics import store, run
from ap_mind.studio_metrics import snapshot
from ap_mind.studio_routing_service import Router, compatible


def test_cosmetic_save_keeps_experience_and_connection_change_separates(studio):
    p=profile(studio, model='grok-4.6')
    store(studio, run(agent_id=p['agent_id'], logical_task_id='one', profile_revision=p['revision'],
        task_snapshot={'tags':['routine_code']}, review={'accepted':True,'reviewer':'codex:independent'}))
    renamed=studio.save({**p,'expected_revision':p['revision'],'name':'新名字','role':'新职责'})['agent']
    assert renamed['capability_revision']==p['capability_revision']
    assert snapshot(studio, configuration='current')['total']==1
    assert Router(studio).recommend({'tags':['routine_code']})[0]['effective_samples']==0.5
    changed=studio.save({**renamed,'expected_revision':renamed['revision'],'api_key':'different-fixture'})['agent']
    assert changed['capability_revision']!=renamed['capability_revision']
    assert snapshot(studio, configuration='current')['total']==0
    assert snapshot(studio)['total']==1


def test_legacy_same_revision_can_migrate_but_not_unknown_history(studio):
    p=profile(studio)
    with studio.registry.transaction():
        c=studio.registry._connect()
        old={k:v for k,v in p.items() if k not in {'capability_revision','capability_legacy_revision','routing_profile'}}
        c.execute('UPDATE studio_agents SET public_json=? WHERE agent_id=?',(json.dumps(old),p['agent_id']))
    updated=studio.save({**old,'expected_revision':old['revision'],'role':'changed'})['agent']
    assert compatible({'profile_revision':old['revision']},updated)
    assert not compatible({'profile_revision':old['revision']-1},updated)


def test_directory_exposes_routing_and_retains_manual_preferences(studio):
    p=profile(studio,model='claude-opus-5',connection_label='Kiro · 经济渠道',role='困难任务交给 CC Max')
    assert p['routing_profile']['cost_tier']=='economy'
    custom={'cost_tier':'standard','prior_strength':12,'strengths':{'custom-field':0.731},'source':'人工偏好'}
    saved=studio.save({**p,'expected_revision':p['revision'],'routing_profile':custom})['agent']
    again=studio.save({**{k:v for k,v in saved.items() if k!='routing_profile'},'expected_revision':saved['revision'],'name':'改名'})['agent']
    assert again['routing_profile']==custom
    public=studio.directory(p['agent_id'])['agents'][0]
    assert public['routing_profile']==custom and public['connection_label']==p['connection_label']
    assert 'fixture-secret-only' not in json.dumps(public)


def test_plan_fallback_prefers_economy_and_keeps_explicit_premium(studio,monkeypatch):
    from ap_mind import agent_studio
    monkeypatch.setattr(agent_studio,'claude_executable',lambda:'/fixture/claude')
    premium=profile(studio,name='CC Max',model='claude-opus-5')
    economy=profile(studio,name='Grok',model='grok-4.6')
    for number,candidates in enumerate(([],[premium['agent_id']])):
        plan=studio.plans.submit({'request_id':'routing-'+str(number),'project_id':'test-project','title':'明确修复','goal':'常规小代码',
          'return_to':{'harness':'codex','session_id':'fixture','wake':False},
          'tasks':[{'key':'fix','title':'fix','goal':'correct one calculation','acceptance':'verify result','tags':['routine_code'],'eligible_agents':candidates}]})['plan']
        assigned=studio.plans._assignments(plan,studio.plans._profiles())[0]
        assert assigned['agent_id']==(premium if candidates else economy)['agent_id']


def test_blended_price_does_not_reprice_old_requests_or_change_limits(studio):
    p=profile(studio)
    raw={'input_tokens':10,'output_tokens':5}
    studio.budget.observe(p['agent_id'],'old','req-old',raw)
    result=studio.budget.save({'request_id':'price','agent_id':p['agent_id'],'expected_revision':0,
        'token_limit':100,'amount_limit':None,'currency':'CNY','price_basis':'blended','price_source':'用户报告100元，部分用量未知',
        'prices':{k:3.334508 for k in ('input','output','cache_read','cache_write')}})['budget']
    assert result['token_limit']==100 and result['amount_limit'] is None and result['amount_used']==0
    studio.budget.observe(p['agent_id'],'new','req-new',raw)
    now=studio.budget.status(p['agent_id'])
    assert now['amount_used']==pytest.approx(15*3.334508/1e6)
    assert now['unestimated_requests']==1 and now['price_basis']=='blended'
