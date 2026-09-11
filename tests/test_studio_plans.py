"""Graph/storage/failure guarantees, separate from real-model acceptance."""
import json
from pathlib import Path
import pytest
from test_agent_studio import studio, profile
from ap_mind import agent_studio
from ap_mind.contracts import ContractError


def graph(studio,monkeypatch,manager=False):
    monkeypatch.setattr(agent_studio,'claude_executable',lambda:'/fixture/claude')
    monkeypatch.setattr(studio,'_execute',lambda *a:None)
    a=profile(studio,name='author')['agent_id'];b=profile(studio,name='reviewer')['agent_id']
    if manager:
        m=profile(studio,name='manager')['agent_id']
        studio.manager.configure({'request_id':'manager','expected_revision':0,'enabled':True,'agent_id':m})
    return {'request_id':'graph','project_id':'test-project','title':'real graph','goal':'two dependent deliverables',
        'return_to':{'harness':'codex','session_id':'fixture-parent','wake':False},
        'tasks':[{'key':'b','title':'use source','goal':'use checked source','acceptance':'inspect merged file',
                  'dependencies':['a'],'eligible_agents':[a,b]},
                 {'key':'a','title':'source','goal':'write source.txt','acceptance':'inspect source.txt',
                  'eligible_agents':[a],'reviewer_agent_id':b}]}


def stored(studio,plan):return studio.plans.list(plan['plan_id'])['plans'][0]


def test_graph_atomic_forward_dependencies_idempotency(studio,monkeypatch):
    raw=graph(studio,monkeypatch)
    plan=studio.plans.submit(raw)['plan'];nodes=plan['nodes']
    assert [n['key'] for n in nodes]==['a','b']
    task=studio.tasks.list(nodes[1]['task_id'])['tasks'][0]
    assert task['dependencies']==[nodes[0]['task_id']] and not task['auto_run'] and 'return_to' not in task
    assert studio.plans.submit(raw)['replayed']
    with pytest.raises(ContractError,match='request_conflict'):studio.plans.submit({**raw,'title':'different'})
    with pytest.raises(ContractError,match='plan_not_released'):
        studio.tasks.claim({'request_id':'premature','task_id':nodes[0]['task_id'],
            'expected_version':nodes[0]['version'],'agent_id':raw['tasks'][1]['eligible_agents'][0]})
    broken={**raw,'request_id':'half','tasks':[raw['tasks'][1],{**raw['tasks'][0],'acceptance':''}]}
    with pytest.raises(ContractError):studio.plans.submit(broken)
    assert len(studio.tasks.list()['tasks'])==2 and len(studio.plans.list()['plans'])==1
    for tasks in ([{**raw['tasks'][1],'dependencies':['b']},raw['tasks'][0]],
                  [{**raw['tasks'][1],'dependencies':['missing']}], [raw['tasks'][1],raw['tasks'][1]]):
        with pytest.raises(ContractError):studio.plans.submit({**raw,'request_id':'bad','tasks':tasks})
    assert len(studio.tasks.list()['tasks'])==2


def test_fallback_has_independent_review_and_single_return(studio,monkeypatch):
    plan=studio.plans.submit(graph(studio,monkeypatch))['plan']
    studio.plans.tick();plan=stored(studio,plan)
    assert plan['state']=='running' and not plan['manager_acknowledged'] and plan['assignment_source']=='fallback'
    studio.tasks.tick();plan=stored(studio,plan)
    assert plan['nodes'][0]['state']=='running' and plan['nodes'][1]['state']=='queued'
    # A normal process exit cannot release the downstream graph.
    first=plan['nodes'][0];studio._threads.clear();studio._state(first['run_id'],'awaiting_review',exit_code=0)
    studio.tasks.tick();plan=stored(studio,plan)
    assert plan['nodes'][1]['state']=='queued'
    # Test fixture supplies a checked upstream state without claiming model proof.
    with studio.registry.transaction():
        c=studio.registry._connect()
        for node in plan['nodes']:
            t=studio.tasks._read(c,node['task_id']);studio.tasks._write(c,{**t,'state':'completed'},'test_verified',{})
    studio.plans.tick();studio.plans.tick()
    assert stored(studio,plan)['state']=='completed'
    assert len(studio.returns.list(plan['plan_id']))==1
    assert not any(studio.returns.list(n['task_id']) for n in plan['nodes'])


def test_real_manager_file_contract_and_launch_reconciliation(studio,monkeypatch):
    raw=graph(studio,monkeypatch,True);plan=studio.plans.submit(raw)['plan']
    studio.plans.tick();plan=stored(studio,plan);rid=plan['manager_run_id']
    assert not plan['manager_acknowledged'] and all(n['state']=='queued' for n in plan['nodes'])
    # Simulate loss after start but before persisting the run acknowledgement.
    with studio.registry.transaction():
        c=studio.registry._connect();value=studio.plans._read(c,plan['plan_id']);value.pop('manager_run_id')
        studio.plans._write(c,value)
    studio.plans.tick();assert stored(studio,plan)['manager_run_id']==rid
    run=studio.runs(rid)['runs'][0];root=Path(run['workspace']);root.mkdir(parents=True,exist_ok=True)
    a,b=raw['tasks'][0]['eligible_agents']
    (root/'manager-plan.json').write_text(json.dumps({'message':'assign by roles','assignments':[
        {'key':'a','agent_id':a,'reviewer_agent_id':b,'reason':'author and independent reviewer'},
        {'key':'b','agent_id':b,'reviewer_agent_id':None,'reason':'summarize'}]}),encoding='utf8')
    studio._threads.clear();studio._state(rid,'awaiting_review',exit_code=0)
    studio.plans.tick();plan=stored(studio,plan)
    assert plan['manager_acknowledged'] and plan['assignment_source']=='manager' and plan['manager_decision_sha256']
    assert plan['assignments'][1]['agent_id']==b


def test_bad_manager_proposal_cannot_skip_upstream_review(studio,monkeypatch):
    raw=graph(studio,monkeypatch,True);plan=studio.plans.submit(raw)['plan']
    studio.plans.tick();plan=stored(studio,plan);run=studio.runs(plan['manager_run_id'])['runs'][0]
    root=Path(run['workspace']);root.mkdir(parents=True,exist_ok=True)
    (root/'manager-plan.json').write_text(json.dumps({'message':'skip review','assignments':[
        {'key':key,'agent_id':raw['tasks'][1]['eligible_agents'][0],'reviewer_agent_id':None,'reason':'fast'} for key in ['a','b']]}),encoding='utf8')
    studio._threads.clear();studio._state(run['run_id'],'awaiting_review',exit_code=0)
    studio.plans.tick();plan=stored(studio,plan)
    assert not plan['manager_acknowledged'] and plan['manager_issue']
    assert plan['nodes'][0]['reviewer_agent_id']


def test_cancel_only_its_nodes_and_no_late_manager_resurrection(studio,monkeypatch):
    raw=graph(studio,monkeypatch,True);plan=studio.plans.submit(raw)['plan']
    other=studio.tasks.save({'request_id':'unrelated','project_id':'test-project','title':'other','goal':'other',
        'acceptance':'inspect','eligible_agents':raw['tasks'][0]['eligible_agents'],'auto_run':True})['task']
    studio.plans.tick();plan=stored(studio,plan)
    studio._threads.clear();studio._state(plan['manager_run_id'],'awaiting_review',exit_code=0)
    request={'request_id':'cancel','plan_id':plan['plan_id'],'expected_revision':plan['revision']}
    assert studio.plans.cancel(request)['plan']['state']=='cancelled'
    assert studio.plans.cancel(request)['replayed']
    studio.plans.tick();assert stored(studio,plan)['state']=='cancelled'
    assert studio.tasks.list(other['task_id'])['tasks'][0]['auto_run']
    assert all(not studio.tasks.list(n['task_id'])['tasks'][0]['auto_run'] for n in plan['nodes'])


def test_missing_configuration_waits_without_spending(studio,monkeypatch):
    raw=graph(studio,monkeypatch,True)
    # Registered but incomplete partner cannot be scheduled.
    draft=studio.save({'name':'draft','base_url':'https://example.invalid/v1','model':'','api_key':''})['agent']
    raw['tasks'][1]['eligible_agents']=[draft['agent_id']]
    plan=studio.plans.submit(raw)['plan'];studio.plans.tick();plan=stored(studio,plan)
    assert plan['state']=='needs_configuration' and not plan.get('manager_run_id')
    assert not studio.runs()['runs']


def test_exhausted_reviewer_returns_plan_instead_of_waiting_forever(studio,monkeypatch):
    raw=graph(studio,monkeypatch);raw['tasks'][1]['max_review_retries']=0
    plan=studio.plans.submit(raw)['plan'];studio.plans.tick();studio.tasks.tick();plan=stored(studio,plan)
    author=plan['nodes'][0];studio._threads.clear();studio._state(author['run_id'],'awaiting_review',exit_code=0)
    studio.tasks.tick();parent=studio.tasks.list(author['task_id'])['tasks'][0]
    reviewer=studio.tasks.list(parent['review_task_id'])['tasks'][0]
    studio._threads.clear();studio._state(reviewer['run_id'],'failed',exit_code=1,error='reviewer upstream failure')
    studio.tasks.tick();studio.plans.tick()
    final=stored(studio,plan)
    assert final['state']=='needs_help' and len(final['returns'])==1
    assert final['nodes'][1]['state']=='queued'
    assert not studio.tasks.list(final['nodes'][1]['task_id'])['tasks'][0]['auto_run']


def test_manager_launch_ack_loss_waits_for_existing_run(studio,monkeypatch):
    raw=graph(studio,monkeypatch,True);plan=studio.plans.submit(raw)['plan']
    original=studio.start
    def lost(raw):
        original(raw)
        raise OSError('launch acknowledgement lost')
    monkeypatch.setattr(studio,'start',lost)
    studio.plans.tick();current=stored(studio,plan)
    assert current['state']=='planning' and current['manager_run_id']
    assert not current.get('assignment_source')
    studio.plans.tick()
    assert len(studio.runs()['runs'])==1 and all(n['state']=='queued' for n in stored(studio,plan)['nodes'])


@pytest.mark.parametrize('failure',[ContractError('agent_run_not_owned_or_finished'),OSError('process handle unavailable')])
def test_manager_timeout_after_executor_exit_falls_back_without_loop(studio,monkeypatch,failure):
    plan=studio.plans.submit(graph(studio,monkeypatch,True))['plan']
    studio.plans.tick();current=stored(studio,plan)
    studio.plans._persist({**current,'manager_started_at':'2000-01-01T00:00:00Z'})
    # Exact race: public state still running, process already gone. cancel
    # legitimately refuses a nonexistent Popen, not a plan-level failure.
    monkeypatch.setattr(studio,'cancel',lambda raw:(_ for _ in ()).throw(failure))
    studio.plans.tick();current=stored(studio,plan)
    assert current['state']=='running' and current['assignment_source']=='fallback'
    assert current['manager_cancel_issue']==str(failure)


def test_reviewer_unavailable_before_child_exists_returns_problem(studio,monkeypatch):
    plan=studio.plans.submit(graph(studio,monkeypatch))['plan']
    studio.plans.tick();studio.tasks.tick();current=stored(studio,plan)
    first=current['nodes'][0]
    with studio.registry.transaction():
        c=studio.registry._connect();task=studio.tasks._read(c,first['task_id'])
        studio.tasks._write(c,{**task,'state':'waiting_review','review_issue':'reviewer archived'},'fixture',{})
    studio.plans.tick();current=stored(studio,plan)
    assert current['state']=='needs_help' and len(current['returns'])==1
    assert current['nodes'][1]['state']=='queued'


def test_pending_manager_rework_is_not_terminal_review_failure(studio,monkeypatch):
    plan=studio.plans.submit(graph(studio,monkeypatch))['plan'];studio.plans.tick()
    current=stored(studio,plan)
    with studio.registry.transaction():
        c=studio.registry._connect();task=studio.tasks._read(c,current['nodes'][0]['task_id'])
        studio.tasks._write(c,{**task,'state':'changes_requested','review_issue':'waiting for manager to choose revision',
                              'rework_round':0,'max_rework_rounds':2},'fixture',{})
    studio.plans.tick();current=stored(studio,plan)
    assert current['state']=='running' and not current['returns']


def test_reviewer_loses_key_after_child_is_registered(studio,monkeypatch):
    raw=graph(studio,monkeypatch);plan=studio.plans.submit(raw)['plan'];studio.plans.tick();studio.tasks.tick()
    current=stored(studio,plan);author=current['nodes'][0]
    studio._threads.clear();studio._state(author['run_id'],'awaiting_review',exit_code=0)
    with studio.registry.transaction():
        c=studio.registry._connect();task=studio.tasks._read(c,author['task_id'])
        studio.tasks._write(c,{**task,'state':'waiting_review'},'fixture',{})
    review=studio.tasks._ensure_review_task(studio.tasks.list(author['task_id'])['tasks'][0])
    candidate=next(a for a in studio.profiles()['agents'] if a['agent_id']==review['eligible_agents'][0])
    studio.save({**candidate,'expected_revision':candidate['revision'],'clear_api_key':True})
    studio.plans.tick();current=stored(studio,plan)
    assert current['state']=='needs_help' and len(current['returns'])==1
