import json
import pytest

from test_agent_studio import studio, profile
from test_studio_metrics import store, run
from ap_mind.contracts import ContractError
from ap_mind.studio_guidance import DEFAULT_GUIDANCE, StudioGuidance
from ap_mind.studio_routing import rank
from ap_mind.studio_routing_service import Router, recommendations
from ap_mind.studio_price_reference import catalog, reference_for


def test_user_guidance_persists_retries_and_experience_never_overwrites_it(studio):
    request={'request_id':'habit-1','expected_revision':0,'guidance':'先按领域选人，普通检查用经济伙伴。',
             'premium_routine_penalty':0.05,'experience_scale':2}
    saved=studio.guidance.save(request)
    assert saved['settings']['revision']==1
    assert studio.guidance.save(request)['replayed']
    assert StudioGuidance(studio).settings()==saved['settings']
    with pytest.raises(ContractError,match='request_conflict'):
        studio.guidance.save({**request,'guidance':'changed'})
    with pytest.raises(ContractError,match='revision_conflict'):
        studio.guidance.save({**request,'request_id':'stale'})
    p=profile(studio,model='grok-4.6')
    store(studio,run(agent_id=p['agent_id'],logical_task_id='proof',profile_revision=p['revision'],
                    task_snapshot={'tags':['routine_code']},review={'accepted':True,'reviewer':'codex:independent'}))
    result=recommendations(studio,{'tags':['routine_code']})
    assert request['guidance'] in result['policy']
    assert result['recommendations'][0]['experience_weight']==pytest.approx(1/7)
    assert result['recommendations'][0]['experience_evidence']['reviewed_attempts']==1
    assert studio.guidance.settings()['guidance']==request['guidance']
    assert '模块' in DEFAULT_GUIDANCE


def test_guidance_weights_affect_fallback_without_forcing_cheapest():
    profiles=[{'agent_id':'low','routing_profile':{'cost_tier':'economy','strengths':{'review':.1}}},
              {'agent_id':'high','routing_profile':{'cost_tier':'premium','strengths':{'review':.9}}}]
    assert rank(profiles,{'tags':['review']},[])[0]['agent_id']=='high'
    assert rank(profiles,{'tags':['review']},[],{'premium_routine_penalty':1})[0]['agent_id']=='low'
    assert rank(profiles,{'tags':['review','critical']},[],{'premium_routine_penalty':1})[0]['agent_id']=='high'


@pytest.mark.parametrize('field,value',[('premium_routine_penalty',float('nan')),('experience_scale',float('inf')),('experience_scale',-1)])
def test_invalid_guidance_does_not_mutate(studio,field,value):
    with pytest.raises(ContractError):
        studio.guidance.save({'request_id':'bad','expected_revision':0,'guidance':'test',field:value})
    assert studio.guidance.settings()['revision']==0


def test_screenshot_units_and_distinct_channels():
    p={'base_url':'https://api.yinziapi.top/v1','model':'claude-opus-5'}
    assert reference_for(p) is None
    cheap=reference_for({**p,'connection_label':'Kiro'})
    full=reference_for({**p,'connection_label':'CC Max'})
    assert cheap['effective_per_million_input_cny']==pytest.approx(.758139)
    assert full['effective_per_million_input_cny']==pytest.approx(3.335594)
    assert full['blended_per_million_total_cny']==pytest.approx(99.5434/29.99)
    assert len(set(full['prices'].values()))==1  # cache benefit already included
    assert reference_for({**p,'base_url':'https://different.example/v1','connection_label':'CC Max'}) is None
    assert reference_for({**p,'model':'gemini-3.8-flash'}) is None
    assert len(catalog())==6


def test_reference_defaults_never_reprice_old_ledger_or_user_settings(studio):
    p=profile(studio,base_url='https://api.yinziapi.top/v1',model='grok-4.6')
    before=studio.budget.status(p['agent_id'])
    assert before['reference_default'] and before['token_limit'] is None
    studio.budget.observe(p['agent_id'],'run','old',{'input_tokens':100,'output_tokens':20})
    old=studio.budget.status(p['agent_id'])['amount_used']
    saved=studio.budget.save({'agent_id':p['agent_id'],'request_id':'free-user','expected_revision':0,
                            'token_limit':500,'amount_limit':None,'prices':{}})['budget']
    assert saved['prices']['input'] is None and saved['token_limit']==500
    assert studio.budget.status(p['agent_id'])['amount_used']==old
    assert studio.budget.status(p['agent_id'])['price_status']=='unpriced'
