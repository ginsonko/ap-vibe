import json
import pytest

from test_agent_studio import studio, profile
from ap_mind.agent_studio import activation
from ap_mind.contracts import ContractError


def test_templates_are_drafts_and_cannot_claim_work(studio):
    result=studio.agent_setup.install({'request_id':'templates','template_ids':['grok-executor','codex-local']})
    grok,codex=result['agents']
    assert not activation(grok)['activated'] and activation(codex)['activated']
    assert grok['missing_configuration']==['api_key']
    task=studio.tasks.save({'request_id':'task','project_id':'test-project','title':'Example','goal':'Example',
        'acceptance':'Real file','eligible_agents':[grok['agent_id']]})['task']
    with pytest.raises(ContractError,match='agent_configuration_incomplete'):
        studio.tasks.claim({'request_id':'claim','task_id':task['task_id'],'agent_id':grok['agent_id'],'expected_version':task['version']})
    again=studio.agent_setup.install({'request_id':'templates','template_ids':['grok-executor','codex-local']})
    assert again['replayed'] and len(studio.profiles()['agents'])==2


def test_batch_credentials_atomic_conflict_idempotency_and_no_plaintext(studio):
    a,b=profile(studio),profile(studio)
    raw={'request_id':'bulk','agents':[{'agent_id':a['agent_id'],'expected_revision':a['revision']},
        {'agent_id':b['agent_id'],'expected_revision':0}],'api_key':'new-fixture-key-value'}
    with pytest.raises(ContractError,match='revision_conflict'):
        studio.agent_setup.connections(raw)
    assert all(v['revision']==1 for v in studio.profiles()['agents'])
    raw['agents'][1]['expected_revision']=1
    result=studio.agent_setup.connections(raw)
    assert len(result['agents'])==2 and all(a['key_saved'] for a in result['agents'])
    assert studio.agent_setup.connections(raw)['replayed']
    assert all(v['revision']==2 for v in studio.profiles()['agents'])
    with studio.registry._connect() as c:
        receipts=c.execute('SELECT * FROM studio_agent_setup_requests').fetchall()
        assert 'new-fixture-key-value' not in str([tuple(row) for row in receipts])
    assert 'new-fixture-key-value' not in json.dumps(result)


def test_edit_blank_key_preserved_explicit_clear_and_restore(studio):
    a=profile(studio)
    kept=studio.save({**a,'expected_revision':1,'api_key':''})['agent']
    assert kept['key_saved']
    empty=studio.save({**kept,'expected_revision':2,'clear_api_key':True})['agent']
    assert not empty['key_saved'] and not empty['activated']
    saved=studio.save({**empty,'expected_revision':3,'api_key':'restored-fixture'})['agent']
    assert saved['activated']


def test_first_templates_connect_dedicated_manager_but_wait_for_key(studio):
    raw={'request_id':'m'*200,'template_ids':['grok-manager','grok-executor']}
    result=studio.agent_setup.install(raw)
    manager=next(a for a in result['agents'] if a['template_id']=='grok-manager')
    assert result['manager_setup']=={'configured':True,'agent_id':manager['agent_id']}
    assert studio.manager.settings()['agent_id']==manager['agent_id']
    assert not manager['activated'] and not manager['key_saved']
    assert next(a for a in studio.profiles()['agents'] if a['agent_id']==manager['agent_id'])['management_reserved']
    from ap_mind.studio_presence import snapshot
    assert snapshot(studio)['manager_actor']['room']=='rest'
    assert snapshot(studio)['manager_actor']['state']=='inactive'
    studio.agent_setup.connections({'request_id':'manager-key','agents':[{
        'agent_id':manager['agent_id'],'expected_revision':1}],'api_key':'fixture-manager-key'})
    assert next(a for a in studio.profiles()['agents'] if a['agent_id']==manager['agent_id'])['activated']
    assert snapshot(studio)['manager_actor']['room']=='management'
    assert studio.agent_setup.install(raw)['replayed']
    repeated=studio.agent_setup.install({**raw,'request_id':'new-install-click'})
    assert not repeated['agents'] and studio.manager.settings()['revision']==1


@pytest.mark.parametrize('enabled,with_agent',[(False,False),(True,True),(True,False)])
def test_templates_preserve_any_user_manager_configuration(studio,enabled,with_agent):
    original=profile(studio)['agent_id'] if with_agent else None
    before=studio.manager.configure({'request_id':'user-manager','expected_revision':0,
        'enabled':enabled,'agent_id':original,'name':'我的管家','persona':'使用我的语气',
        'appearance_id':'gemini-v3','decision_timeout_seconds':60})['settings']
    result=studio.agent_setup.install({'request_id':'first-default-manager','template_ids':['grok-manager']})
    assert result['manager_setup']=={'configured':False,'reason':'existing_settings_preserved'}
    assert studio.manager.settings()==before


def test_plan_manager_presence_follows_actual_run_without_incident(studio,monkeypatch):
    from ap_mind import agent_studio
    from ap_mind.studio_presence import snapshot
    monkeypatch.setattr(agent_studio,'claude_executable',lambda:'/fixture/claude')
    monkeypatch.setattr(studio,'_execute',lambda *args:None)
    manager=profile(studio)
    studio.manager.configure({'request_id':'presence-manager','expected_revision':0,
                              'enabled':True,'agent_id':manager['agent_id']})
    run=studio.start({'request_id':'plan-coordination','agent_id':manager['agent_id'],
        'project_id':'test-project','prompt':'fixture plan decision','coordination_only':True})
    studio._state(run['run_id'],'running')
    assert not studio.manager.list()['incidents']
    actor=snapshot(studio)['manager_actor']
    assert actor['active'] and actor['activity_label']=='正在协调' and actor['animation']=='read'
    studio._state(run['run_id'],'awaiting_review',exit_code=0)
    assert not snapshot(studio)['manager_actor']['active']
