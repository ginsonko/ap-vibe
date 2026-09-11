import json
from pathlib import Path
import pytest
from test_agent_studio import studio,profile
from ap_mind import agent_studio


def setup(studio,monkeypatch):
    monkeypatch.setattr(agent_studio,'claude_executable',lambda:'/fixture/claude')
    monkeypatch.setattr(studio,'_execute',lambda *args:None)
    author=profile(studio,name='author')['agent_id'];backup=profile(studio,name='backup')['agent_id']
    manager=profile(studio,name='manager')['agent_id']
    studio.manager.configure({'request_id':'configure','expected_revision':0,'enabled':True,'agent_id':manager})
    t=studio.tasks.save({'request_id':'task','project_id':'test-project','title':'a task','goal':'write artifact',
        'acceptance':'read artifact','eligible_agents':[author,backup],'auto_run':True})['task']
    studio.tasks.tick();t=studio.tasks.list(t['task_id'])['tasks'][0]
    studio._threads.clear();studio._state(t['run_id'],'failed',exit_code=1,error='connection ended')
    studio.tasks.tick()
    return t,author,backup,manager


def test_manager_once_decision_and_unique_handoff(studio,monkeypatch):
    task,author,backup,manager=setup(studio,monkeypatch)
    incident=studio.manager.list()['incidents'][0]
    manager_run=studio.runs(incident['manager_run_id'])['runs'][0]
    assert manager_run['coordination_only'] and incident['state']=='deliberating'
    for _ in range(3):studio.tasks.tick()
    assert len([r for r in studio.runs()['runs'] if r['agent_id']==manager])==1
    path=Path(manager_run['workspace']);path.mkdir(parents=True,exist_ok=True)
    (path/'manager-decision.json').write_text(json.dumps({'action':'takeover','agent_id':backup,'reason':'备选负责同类工作','message':'交给备选继续。'}),encoding='utf8')
    studio._state(manager_run['run_id'],'awaiting_review',exit_code=0)
    studio.tasks.tick()
    updated=studio.tasks.list(task['task_id'])['tasks'][0]
    assert updated['owner']==backup and updated['assignment_epoch']==2
    assert studio.runs(updated['run_id'])['runs'][0]['handoff_from_run_id']==task['run_id']
    assert studio.manager.list()['incidents'][0]['state']=='applied'
    for _ in range(3):studio.tasks.tick()
    assert len(studio.tasks.list(task['task_id'])['tasks'][0]['attempts'])==2


def test_manager_failure_falls_back_without_recursive_manager(studio,monkeypatch):
    task,_,backup,manager=setup(studio,monkeypatch)
    incident=studio.manager.list()['incidents'][0]
    studio._state(incident['manager_run_id'],'failed',exit_code=1)
    studio.tasks.tick()
    updated=studio.tasks.list(task['task_id'])['tasks'][0]
    assert updated['owner']==backup
    assert len(studio.manager.list()['incidents'])==1
    assert studio.manager.list()['incidents'][0]['decision']['action']=='fallback'


def test_late_manager_cannot_override_manual_cancellation(studio,monkeypatch):
    task,_,backup,_=setup(studio,monkeypatch)
    incident=studio.manager.list()['incidents'][0]
    with studio.registry.transaction():
        c=studio.registry._connect();current=studio.tasks._read(c,task['task_id'])
        studio.tasks._write(c,{**current,'state':'paused','auto_run':False},'user_cancelled',{})
    run=studio.runs(incident['manager_run_id'])['runs'][0]
    root=Path(run['workspace']);root.mkdir(parents=True,exist_ok=True)
    (root/'manager-decision.json').write_text(json.dumps({'action':'takeover','agent_id':backup,'reason':'late'}),encoding='utf8')
    studio._state(run['run_id'],'awaiting_review',exit_code=0)
    studio.tasks.tick()
    assert studio.tasks.list(task['task_id'])['tasks'][0]['state']=='paused'
    assert len(studio.tasks.list(task['task_id'])['tasks'][0]['attempts'])==1


def test_manager_can_choose_single_retry_before_backup(studio,monkeypatch):
    task,author,backup,manager=setup(studio,monkeypatch)
    incident=studio.manager.list()['incidents'][0]
    run=studio.runs(incident['manager_run_id'])['runs'][0]
    root=Path(run['workspace']);root.mkdir(parents=True,exist_ok=True)
    (root/'manager-decision.json').write_text(json.dumps({'action':'retry','agent_id':author,
        'reason':'Transient interruption; inspect saved artifacts before continuing.'}),encoding='utf8')
    studio._state(run['run_id'],'awaiting_review',exit_code=0)
    studio.tasks.tick()
    updated=studio.tasks.list(task['task_id'])['tasks'][0]
    assert updated['owner']==author and updated['assignment_epoch']==2
    assert updated['author_retry_count']==1
    assert studio.runs(updated['run_id'])['runs'][0]['handoff_from_run_id']==task['run_id']
    studio._threads.clear();studio._state(updated['run_id'],'failed',exit_code=1)
    studio.tasks.tick()
    incident=studio.manager.list()['incidents'][0]
    assert incident['retry_agent_id'] is None
    run=studio.runs(incident['manager_run_id'])['runs'][0]
    root=Path(run['workspace']);root.mkdir(parents=True,exist_ok=True)
    (root/'manager-decision.json').write_text(json.dumps({'action':'retry','agent_id':author,'reason':'again'}),encoding='utf8')
    studio._state(run['run_id'],'awaiting_review',exit_code=0)
    studio.tasks.tick()
    updated=studio.tasks.list(task['task_id'])['tasks'][0]
    assert updated['owner']==backup and updated['author_retry_count']==1
    assert studio.manager.list()['incidents'][0]['decision']['action']=='fallback'


def test_manager_identity_is_reserved_for_coordination(studio,monkeypatch):
    _,_,_,manager=setup(studio,monkeypatch)
    task=studio.tasks.save({'request_id':'ordinary','project_id':'test-project','title':'ordinary',
        'goal':'implement code','acceptance':'verify code','eligible_agents':[manager]})['task']
    with pytest.raises(Exception,match='studio_task_manager_reserved'):
        studio.tasks.claim({'request_id':'wrong-role','task_id':task['task_id'],
            'expected_version':task['version'],'agent_id':manager})


def test_coordination_filename_is_validated_and_part_of_request(studio,monkeypatch):
    monkeypatch.setattr(agent_studio,'claude_executable',lambda:'/fixture/claude')
    monkeypatch.setattr(studio,'_execute',lambda *args:None)
    manager=profile(studio)['agent_id']
    raw={'request_id':'plan','agent_id':manager,'project_id':'test-project','prompt':'coordinate',
        'coordination_only':True,'coordination_output':'manager-plan.json'}
    first=studio.start(raw)
    assert studio.runs(first['run_id'])['runs'][0]['coordination_output']=='manager-plan.json'
    assert studio.start(raw)['replayed']
    with pytest.raises(Exception,match='agent_request_conflict'):
        studio.start({**raw,'coordination_output':'other.json'})
    for name in ('../elsewhere.json','C:escape.json','a/b.json','a\\b.json','x.json\n','manager.txt'):
        with pytest.raises(Exception,match='manager_output_filename_invalid'):
            studio.start({**raw,'request_id':'invalid','coordination_output':name})


def test_empty_delivery_rejection_reaches_manager_and_keeps_review(studio,monkeypatch):
    monkeypatch.setattr(agent_studio,'claude_executable',lambda:'/fixture/claude')
    monkeypatch.setattr(studio,'_execute',lambda *args:None)
    author=profile(studio,name='author')['agent_id'];backup=profile(studio,name='backup')['agent_id']
    manager=profile(studio,name='manager')['agent_id']
    studio.manager.configure({'request_id':'configure','expected_revision':0,'enabled':True,'agent_id':manager})
    task=studio.tasks.save({'request_id':'empty','project_id':'test-project','title':'write real code',
        'goal':'deliver code.py','acceptance':'read code.py','eligible_agents':[author,backup],
        'auto_run':True,'max_rework_rounds':1})['task']
    studio.tasks.tick();task=studio.tasks.list(task['task_id'])['tasks'][0]
    studio._threads.clear();studio._state(task['run_id'],'awaiting_review',exit_code=0)
    studio.review({'run_id':task['run_id'],'request_id':'reject-empty','expected_revision':0,
        'accepted':False,'reviewer':'user-test','note':'No code file was delivered.'})
    studio.tasks.tick()
    incident=studio.manager.list()['incidents'][0]
    assert incident['recovery_kind']=='review_changes'
    run=studio.runs(incident['manager_run_id'])['runs'][0]
    assert 'No code file was delivered.' in run['prompt']
    root=Path(run['workspace']);root.mkdir(parents=True,exist_ok=True)
    (root/'manager-decision.json').write_text(json.dumps({'action':'takeover','agent_id':backup,
        'reason':'The current author did not implement the requested file.'}),encoding='utf8')
    studio._state(run['run_id'],'awaiting_review',exit_code=0)
    studio.tasks.tick();updated=studio.tasks.list(task['task_id'])['tasks'][0]
    assert updated['owner']==backup and updated['rework_round']==1
    assert 'No code file was delivered.' in updated['handoff_note']
    studio._threads.clear();studio._state(updated['run_id'],'awaiting_review',exit_code=0)
    studio.review({'run_id':updated['run_id'],'request_id':'reject-again','expected_revision':0,
        'accepted':False,'reviewer':'user-test','note':'Still missing.'})
    studio.tasks.tick()
    assert studio.tasks.list(task['task_id'])['tasks'][0]['state']=='changes_requested'
    assert len(studio.manager.list()['incidents'])==1


def test_manager_failure_cools_down_but_new_incident_can_recover(studio,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from ap_mind.studio_tasks import encoded
    task,author,backup,manager=setup(studio,monkeypatch)
    incident=studio.manager.list()['incidents'][0]
    rid=incident['manager_run_id']
    studio._threads.clear();studio._state(rid,'uncertain',exit_code=1,error='upstream returned empty content')
    studio.tasks.tick()
    assert studio.tasks.list(task['task_id'])['tasks'][0]['owner']==backup
    # No recursive manager and no fabricated model decision during cooldown.
    value={'created_at':datetime.now(timezone.utc).isoformat(),'prompt':'coordinate'}
    decision=studio.manager._advance_decision('new-during-cooldown','test-project',value,studio.manager.settings(),lambda x:None)
    assert decision['action']=='fallback' and not value.get('manager_run_id')
    with studio.registry.transaction():
        c=studio.registry._connect()
        row=c.execute('SELECT payload_json FROM studio_runs WHERE run_id=?',(rid,)).fetchone()
        saved=json.loads(row[0]);saved['updated_at']=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        c.execute('UPDATE studio_runs SET payload_json=? WHERE run_id=?',(encoded(saved),rid))
    value={'created_at':datetime.now(timezone.utc).isoformat(),'prompt':'coordinate'}
    decision=studio.manager._advance_decision('new-after-cooldown','test-project',value,studio.manager.settings(),lambda x:None)
    assert decision['action']=='pending' and value.get('manager_run_id')!=rid


def test_edit_after_review_does_not_wait_for_background_tick(studio,monkeypatch):
    monkeypatch.setattr(agent_studio,'claude_executable',lambda:'/fixture/claude')
    monkeypatch.setattr(studio,'_execute',lambda *args:None)
    author=profile(studio)['agent_id']
    raw={'request_id':'before-review','project_id':'test-project','title':'write real file',
         'goal':'deliver code','acceptance':'check code','eligible_agents':[author],'auto_run':True}
    task=studio.tasks.save(raw)['task'];studio.tasks.tick()
    task=studio.tasks.list(task['task_id'])['tasks'][0]
    studio._threads.clear();studio._state(task['run_id'],'awaiting_review',exit_code=0)
    studio.tasks.tick();task=studio.tasks.list(task['task_id'])['tasks'][0]
    assert task['state']=='waiting_review'
    studio.review({'run_id':task['run_id'],'request_id':'review-missing','expected_revision':0,
                   'accepted':False,'reviewer':'user-test','note':'missing file'})
    saved=studio.tasks.save({**raw,'request_id':'after-review','task_id':task['task_id'],
        'expected_version':task['version'],'auto_run':False})['task']
    assert saved['state']=='changes_requested' and saved['run_id']==task['run_id']


def test_restarted_executor_requires_absence_evidence_before_manager(studio,monkeypatch):
    monkeypatch.setattr(agent_studio,'claude_executable',lambda:'/fixture/claude')
    monkeypatch.setattr(studio,'_execute',lambda *args:None)
    a=profile(studio)['agent_id'];b=profile(studio)['agent_id'];m=profile(studio)['agent_id']
    studio.manager.configure({'request_id':'manager','expected_revision':0,'enabled':True,'agent_id':m})
    t=studio.tasks.save({'request_id':'crash','project_id':'test-project','title':'recover','goal':'keep files',
        'acceptance':'inspect','eligible_agents':[a,b],'auto_run':True})['task']
    studio.tasks.tick();t=studio.tasks.list(t['task_id'])['tasks'][0]
    studio._threads.clear();studio._state(t['run_id'],'interrupted',pid=765432,exit_code=None)
    monkeypatch.setattr('ap_mind.studio_processes.process_absent',lambda _:False)
    studio.tasks.tick();assert not studio.manager.list()['incidents']
    monkeypatch.setattr('ap_mind.studio_processes.process_absent',lambda _:True)
    studio.tasks.tick();assert studio.manager.list()['incidents'][0]['manager_run_id']
