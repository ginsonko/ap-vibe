import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

from test_agent_studio import studio, profile
from ap_mind.studio_sessions import actor_id, StudioSessions
from ap_mind import agent_studio
from ap_mind.contracts import ContractError


def event(studio,kind='UserPromptSubmit',**kwargs):
    return studio.sessions.observe({'event_id':'event-'+kind,'session_id':'ordinary','harness':'codex',
        'kind':kind,'project_id':'test-project',**kwargs})


def test_lifecycle_reordering_idempotence_and_restart_never_implies_failure(studio,monkeypatch):
    monkeypatch.setattr(studio.service.session_directory,'catalog',lambda **kw:{'sessions':[],'total':0})
    start = datetime.now(timezone.utc) - timedelta(minutes=3)
    stamp = lambda minute: (start + timedelta(minutes=minute)).isoformat()
    event(studio,occurred_at=stamp(0))
    event(studio,'Stop',occurred_at=stamp(2))
    event(studio,event_id='late-start',occurred_at=stamp(1))
    assert studio.sessions.snapshot()['actors'][0]['state']=='idle'
    event(studio,event_id='new-start',occurred_at=stamp(3))
    assert studio.sessions.snapshot()['actors'][0]['state']=='running'
    restarted=StudioSessions(studio)
    assert restarted.snapshot()['actors'][0]['state']=='unknown'
    with studio.registry._connect() as c:
        assert c.execute('SELECT COUNT(*) FROM studio_session_events').fetchone()[0]==4


def test_policy_precedence_global_pause_read_access_and_explicit_delegation(studio,monkeypatch):
    identity=event(studio)
    assert not studio.sessions.effective('codex','ordinary','test-project')['enabled']
    def set_(scope,target,enabled,rev=0,paused=False):
        return studio.sessions.configure({'request_id':f'{scope}-{rev}-{enabled}-{paused}',
            'scope':scope,'target':target,'enabled':enabled,'paused':paused,'expected_revision':rev})
    set_('project','test-project',True)
    assert studio.sessions.effective('codex','ordinary','test-project')['basis']=='project'
    set_('session',identity,False)
    assert not studio.sessions.effective('codex','ordinary','test-project')['enabled']
    set_('session',identity,None,1)
    assert studio.sessions.effective('codex','ordinary','test-project')['enabled']
    set_('global','',False,paused=True)
    effective=studio.sessions.effective('codex','ordinary','test-project')
    assert not effective['enabled'] and effective['read_allowed'] and effective['explicit_delegation_allowed']
    a=profile(studio)['agent_id']
    t=studio.tasks.save({'request_id':'task','project_id':'test-project','title':'title','goal':'goal',
        'acceptance':'read','eligible_agents':[a],'collaboration_origin':{'harness':'codex','session_id':'ordinary'}})['task']
    with pytest.raises(ContractError,match='automatic_collaboration_paused'):
        studio.tasks.claim({'request_id':'claim','task_id':t['task_id'],'agent_id':a,'expected_version':t['version']})
    explicit=studio.tasks.save({'request_id':'explicit','project_id':'test-project','title':'title','goal':'goal','acceptance':'read','eligible_agents':[a]})['task']
    assert studio.tasks.claim({'request_id':'explicit-claim','task_id':explicit['task_id'],'agent_id':a,'expected_version':explicit['version']})['task']['owner']==a


def test_return_inbox_is_once_after_author_output_and_keeps_wake_failure(studio,monkeypatch):
    monkeypatch.setattr(agent_studio,'claude_executable',lambda:'/fixture/claude')
    monkeypatch.setattr(studio,'_execute',lambda *args:None)
    monkeypatch.setattr(studio.service.codex_messages,'enqueue',lambda raw:(_ for _ in ()).throw(ContractError('source-missing')))
    a=profile(studio)['agent_id']
    task=studio.tasks.save({'request_id':'return-task','project_id':'test-project','title':'result','goal':'write',
        'acceptance':'read','eligible_agents':[a],'auto_run':True,
        'return_to':{'harness':'codex','session_id':'parent','wake':True}})['task']
    studio.tasks.tick(); task=studio.tasks.list(task['task_id'])['tasks'][0]
    studio.returns.tick()
    assert not studio.sessions.inbox('codex','parent')['messages']
    studio._state(task['run_id'],'awaiting_review',exit_code=0)
    studio.tasks.tick();studio.returns.tick();studio.returns.tick()
    inbox=studio.sessions.inbox('codex','parent')
    assert len(inbox['messages'])==1 and '等待你验收' in inbox['messages'][0]['body']
    assert not studio.sessions.inbox('claude','parent')['messages']
    assert not studio.sessions.inbox('codex','parent',inbox['next_cursor'])['messages']
    receipt=studio.returns.list(task['task_id'])[0]
    assert receipt['state']=='inbox_saved' and receipt['wake_issue']=='source-missing'


def test_hook_outbox_is_local_scalar_only_and_drains_once(studio,monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from tools import task_client
    monkeypatch.setattr(task_client,'_installed_config',lambda:{'data_dir':str(studio.service.data_dir)})
    task_client.record_lifecycle({'hook_event_name':'Stop','tool_input':{'api_key':'secret'}},str(studio.root),'ordinary')
    files=list((studio.root/'incoming').glob('*.json'))
    assert len(files)==1 and 'secret' not in files[0].read_text(encoding='utf8')
    studio.sessions.drain_hooks();studio.sessions.drain_hooks()
    assert not list((studio.root/'incoming').glob('*.json'))
    with studio.registry._connect() as c:
        assert c.execute('SELECT COUNT(*) FROM studio_session_events').fetchone()[0]==1


def test_failed_task_notifies_parent_once_after_recovery_exhausted(studio,monkeypatch):
    monkeypatch.setattr(agent_studio,'claude_executable',lambda:'/fixture/claude')
    monkeypatch.setattr(studio,'_execute',lambda *args:None)
    monkeypatch.setattr(studio.service.codex_messages,'enqueue',lambda raw: {'delivery':{'request_id':raw['request_id'],'status':'queued'}})
    monkeypatch.setattr(studio.service.codex_messages,'read_delivery',lambda *a: {'status':'completed','matched_turn_id':'parent-recovered'})
    a=profile(studio)['agent_id']
    task=studio.tasks.save({'request_id':'failed-return-task','project_id':'test-project','title':'核验文档','goal':'read',
        'acceptance':'read','eligible_agents':[a],'auto_run':True,'max_author_retries':0,
        'return_to':{'harness':'codex','session_id':'parent','wake':True}})['task']
    studio.tasks.tick();task=studio.tasks.list(task['task_id'])['tasks'][0]
    studio._threads.clear()
    studio._state(task['run_id'],'uncertain',exit_code=1,error='upstream closed')
    studio.tasks.tick();studio.returns.tick();studio.returns.tick()
    messages=studio.sessions.inbox('codex','parent')['messages']
    assert len(messages)==1 and '未能完成' in messages[0]['body'] and 'upstream closed' in messages[0]['body']
    receipt=studio.returns.list(task['task_id'])[0]
    assert receipt['wake_status']=='completed' and receipt['delivery']['matched_turn_id']=='parent-recovered'
    assert studio.tasks.list(task['task_id'])['tasks'][0]['state']=='needs_help'


def test_automatic_message_needs_both_sides_but_manual_is_available(studio):
    sender=event(studio)
    target=event(studio,event_id='target',session_id='target')
    raw={'request_id':'advice','sender':sender,'recipient':target,'task_id':target,
         'body':'看过文件后发现一项具体问题','automatic':True,
         'collaboration_origin':{'harness':'codex','session_id':'ordinary'}}
    with pytest.raises(ContractError,match='automatic_collaboration_paused'):studio.send_message(raw)
    studio.sessions.configure({'request_id':'open','scope':'global','target':'','enabled':True,'expected_revision':0})
    studio.sessions.configure({'request_id':'target-off','scope':'session','target':target,'enabled':False,'expected_revision':0})
    with pytest.raises(ContractError,match='recipient_automatic_collaboration_paused'):studio.send_message(raw)
    assert studio.send_message({**raw,'automatic':False})['message_id']


def test_current_session_policy_survives_unrelated_directory_filter(studio,monkeypatch):
    monkeypatch.setattr(studio.service.session_directory,'catalog',lambda **kw:{'sessions':[],'total':0})
    identity=event(studio)
    studio.sessions.configure({'request_id':'session-on','scope':'session','target':identity,
        'enabled':True,'expected_revision':0})
    view=studio.sessions.snapshot('unrelated-project','codex','ordinary')
    assert view['actors']==[] and not view['settings']['enabled']
    assert view['current_session']['policy']['enabled']
    assert view['current_session']['session_id']=='ordinary'
    assert studio.sessions.snapshot()['current_session'] is None
    # After restoring inheritance, the recorded project wins over a UI filter.
    studio.sessions.configure({'request_id':'inherit','scope':'session','target':identity,
        'enabled':None,'expected_revision':1})
    studio.sessions.configure({'request_id':'project-on','scope':'project','target':'test-project',
        'enabled':True,'expected_revision':0})
    assert studio.sessions.snapshot('unrelated-project','codex','ordinary')['current_session']['policy']['basis']=='project'
