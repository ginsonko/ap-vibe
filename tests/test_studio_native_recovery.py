"""Isolated ledger/host-event contracts. Never call a real model or stop a PID."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid

import pytest

from test_agent_studio import studio, profile
from ap_mind import agent_studio, studio_sessions, studio_native_recovery
from ap_mind.contracts import ContractError
from ap_mind.session_observation import inspect
from ap_mind.studio_native_recovery import StudioNativeRecovery
from ap_mind.studio_sessions import actor_id


@pytest.fixture
def scene(studio, monkeypatch):
    monkeypatch.setattr(agent_studio, 'claude_executable', lambda: '/fixture/claude')
    monkeypatch.setattr(agent_studio, 'codex_command', lambda: ['/fixture/codex'])
    monkeypatch.setattr(studio, '_execute', lambda *args: None)
    monkeypatch.setattr(studio.service.codex_messages, 'enqueue', lambda *a, **k: pytest.fail('host wake forbidden'))
    monkeypatch.setattr(studio, 'cancel', lambda *a, **k: pytest.fail('process cancellation forbidden'))
    catalog = []
    observations = {}
    monkeypatch.setattr(studio.service.session_directory, 'catalog', lambda **kw: {'sessions': catalog, 'total': len(catalog)})
    monkeypatch.setattr(studio.service.session_directory, 'observation', lambda source: observations[source])
    now = [datetime.now(timezone.utc) + timedelta(seconds=1)]
    clock = lambda: now[0].isoformat()
    monkeypatch.setattr(studio_sessions, 'utc_now', clock)
    monkeypatch.setattr(studio_native_recovery, 'utc_now', clock)
    sid = str(uuid.uuid4())
    worker = profile(studio, name='fixture worker')['agent_id']

    def advance(seconds=1):
        now[0] += timedelta(seconds=seconds)
        return clock()

    def event(kind='failure', **changes):
        raw = {'event_id': str(uuid.uuid4()), 'harness': 'codex', 'session_id': sid,
               'kind': kind, 'occurred_at': advance(), 'project_id': 'test-project',
               'cwd': str(studio.service.data_dir.parent / 'original-read-only'),
               'source_id': 'source-' + sid, 'summary': '修复用户原目标中的解析错误', **changes}
        studio.sessions.observe(raw)
        return raw

    def enable(value=True, scope='global', target='', paused=False):
        advance()
        studio.sessions.configure({'request_id': str(uuid.uuid4()), 'scope': scope, 'target': target,
            'enabled': value, 'paused': paused, 'expected_revision': studio.sessions.settings(scope, target)['revision']})

    return studio, sid, worker, event, enable, advance, catalog, observations


def incidents(studio):
    return studio.native_recovery.list()['incidents']


def test_manager_decides_before_ordinary_failure_is_dispatched(scene):
    s, sid, worker, event, enable, *_ = scene
    manager = profile(s, name='coordinator')['agent_id']
    s.manager.configure({'request_id':'native-manager','expected_revision':0,'enabled':True,'agent_id':manager})
    enable(); event('UserPromptSubmit'); event()
    s.native_recovery.tick()
    incident = incidents(s)[0]
    assert incident['state']=='deliberating' and not tasks(s)
    run = s.runs(incident['manager_run_id'])['runs'][0]
    assert run['coordination_only'] and '是否停止未知' in run['prompt']
    assert 'prompt' not in incident['management']
    for _ in range(3): s.native_recovery.tick()
    assert len(s.runs()['runs'])==1
    root = Path(run['workspace']); root.mkdir(parents=True,exist_ok=True)
    (root/'manager-decision.json').write_text(json.dumps({'action':'takeover','agent_id':worker,
        'reason':'Only authorized candidate, isolated patch work.','message':'安排伙伴在独立目录继续。'}),encoding='utf-8')
    s._state(run['run_id'],'awaiting_review',exit_code=0)
    s.native_recovery.tick(); s.tasks.tick()
    assert tasks(s)[0]['owner']==worker and tasks(s)[0]['assignment_epoch']==1
    assert incidents(s)[0]['decision']['action']=='takeover'


def test_native_manager_late_decision_does_not_revive_cancelled_host(scene):
    s, _, worker, event, enable, *_ = scene
    manager = profile(s, name='coordinator')['agent_id']
    s.manager.configure({'request_id':'native-manager','expected_revision':0,'enabled':True,'agent_id':manager})
    enable(); event('UserPromptSubmit'); event(); s.native_recovery.tick()
    run = s.runs(incidents(s)[0]['manager_run_id'])['runs'][0]
    root = Path(run['workspace']); root.mkdir(parents=True,exist_ok=True)
    (root/'manager-decision.json').write_text(json.dumps({'action':'takeover','agent_id':worker,'reason':'late'}),encoding='utf-8')
    event('cancelled'); s._state(run['run_id'],'awaiting_review',exit_code=0)
    s.native_recovery.tick(); s.tasks.tick()
    assert not tasks(s) and incidents(s)[0]['state']=='superseded'


def test_new_profile_revision_is_not_excluded_by_old_connection_failure(scene):
    s, _, worker, event, enable, *_ = scene
    run = s.start({'request_id':'old-failed-config','agent_id':worker,'project_id':'test-project','prompt':'old config attempt'})
    s._threads.clear(); s._state(run['run_id'],'failed',exit_code=1)
    with s.registry._connect() as c:
        assert worker not in s.native_recovery.candidates(c)
    profile(s,agent_id=worker,expected_revision=1,api_key='',name='reconfigured')
    with s.registry._connect() as c:
        assert worker in s.native_recovery.candidates(c)


def tasks(studio):
    return studio.tasks.list()['tasks']


@pytest.mark.parametrize('harness', ['codex', 'claude'])
def test_failure_to_unique_task_and_return_inbox(scene, harness):
    s, sid, worker, event, enable, advance, _, _ = scene
    enable(); event('UserPromptSubmit', harness=harness); failed = event(harness=harness)
    for _ in range(3):
        s.native_recovery.tick()
    assert len(incidents(s)) == len(tasks(s)) == 1
    incident, task = incidents(s)[0], tasks(s)[0]
    assert task['return_to'] == {'harness': harness, 'session_id': sid, 'wake': False}
    assert task['collaboration_origin'] == {'harness': harness, 'session_id': sid}
    assert task['project_id'] == 'test-project' and task['eligible_agents'] == [worker]
    assert task['max_turns'] is None and s.budget.status(worker)['token_limit'] is None
    assert incident['source_event_id'] == failed['event_id'] and incident['process_stopped'] is None
    for text in (sid, failed['source_id'], failed['summary'], 'ap_vibe_session_read', '不重放', '禁止修改原目录'):
        assert text in task['goal']
    s.tasks.tick(); task = tasks(s)[0]
    run = s.runs(task['run_id'])['runs'][0]
    assert run['workspace'] != failed['cwd'] and not run.get('handoff_from_run_id')
    assert task['assignment_epoch'] == 1
    output = Path(run['workspace']); output.mkdir(parents=True, exist_ok=True)
    (output / 'implementation.md').write_text('isolated fixture patch for ' + sid, encoding='utf8')
    (output / 'acceptance.md').write_text('fixture only; waiting_review', encoding='utf8')
    files = s.artifacts.list(run['run_id'])['files']
    assert {f['name'] for f in files} == {'implementation.md', 'acceptance.md'}
    assert s.artifacts.read(run['run_id'], 'implementation.md')['sha256']
    s._state(run['run_id'], 'awaiting_review', exit_code=0)
    s.tasks.tick(); s.returns.tick(); s.returns.tick(); s.native_recovery.tick()
    assert tasks(s)[0]['state'] == 'waiting_review'
    messages = s.sessions.inbox(harness, sid)['messages']
    assert len(messages) == 2
    assert any(task['task_id'] in m['body'] and '等待你验收' in m['body'] for m in messages)
    assert any(incident['incident_id'] in m['body'] for m in messages)
    assert s.returns.list(task['task_id'])[0]['state'] == 'inbox_saved'
    assert any(i['incident_id'] == incident['incident_id'] for i in s.manager.list()['incidents'])


@pytest.mark.parametrize('kind', ['cancelled', 'Stop', 'SessionEnd', 'SessionStart', 'context', 'unknown', 'UserPromptSubmit'])
def test_non_failure_never_dispatches(scene, kind):
    s, _, _, event, enable, *_ = scene
    enable(); event(kind); s.native_recovery.tick(); s.tasks.tick()
    assert not incidents(s) and not tasks(s) and not s.runs()['runs']


def test_policy_at_failure_not_later_enable_and_install_baseline(scene):
    s, _, _, event, enable, *_ = scene
    event(); enable(); s.native_recovery.tick()
    assert not incidents(s)
    event(occurred_at='2000-01-01T00:00:00Z'); s.native_recovery.tick()
    assert not incidents(s)
    event('UserPromptSubmit'); event(); s.native_recovery.tick()
    assert len(tasks(s)) == 1


def test_project_policy_inherited_hook_metadata_and_session_override(scene):
    s, sid, _, event, enable, *_ = scene
    enable(scope='project', target='test-project')
    event('UserPromptSubmit'); event(project_id=None, source_id=None)
    s.native_recovery.tick()
    assert len(tasks(s)) == 1
    enable(False, scope='session', target=actor_id('codex', sid))
    s.native_recovery.tick(); s.tasks.tick()
    assert tasks(s)[0]['state'] == 'paused' and not s.runs()['runs']


@pytest.mark.parametrize('action', ['UserPromptSubmit', 'cancelled', 'Stop', 'global_off', 'global_pause'])
def test_queued_incident_invalidates_before_dispatch(scene, action):
    s, _, _, event, enable, *_ = scene
    enable(); event(); s.native_recovery.tick()
    if action == 'global_off': enable(False)
    elif action == 'global_pause': enable(True, paused=True)
    else: event(action)
    s.native_recovery.tick(); s.tasks.tick()
    assert incidents(s)[0]['state'] == 'superseded'
    assert tasks(s)[0]['state'] == 'paused' and not s.runs()['runs']
    enable(); s.native_recovery.tick(); s.tasks.tick()
    assert len(tasks(s)) == 1 and not s.runs()['runs']


def test_claim_and_final_start_guard_recheck_activity(scene):
    s, _, worker, event, enable, *_ = scene
    enable(); event(); s.native_recovery.tick()
    t = tasks(s)[0]
    s.tasks.claim({'request_id': 'fixture-claim', 'task_id': t['task_id'], 'agent_id': worker, 'expected_version': t['version']})
    event('UserPromptSubmit')
    with pytest.raises(ContractError, match='native_incident_superseded'):
        s.tasks.dispatch(t['task_id'])
    assert not s.runs()['runs']
    s.native_recovery.tick()
    assert tasks(s)[0]['state'] == 'paused'


def test_out_of_order_equal_time_and_late_predecessor(scene):
    s, _, _, event, enable, *_ = scene
    enable(); failure = event(); s.native_recovery.tick()
    s.native_recovery = StudioNativeRecovery(s)
    event('UserPromptSubmit', occurred_at=(datetime.fromisoformat(failure['occurred_at']) - timedelta(microseconds=1)).isoformat())
    event(occurred_at=failure['occurred_at'])
    s.native_recovery.tick()
    assert len(incidents(s)) == len(tasks(s)) == 1
    event('cancelled', occurred_at=failure['occurred_at'])
    s.native_recovery.tick(); s.tasks.tick()
    assert not s.runs()['runs']
    assert incidents(s)[0]['state'] == 'superseded'


def test_newer_activity_arrives_before_older_failure(scene):
    s, _, _, event, enable, advance, *_ = scene
    enable(); failure_at = advance(); event('UserPromptSubmit'); event(occurred_at=failure_at)
    s.native_recovery.tick(); s.tasks.tick()
    assert incidents(s)[0]['state'] == 'superseded' and not tasks(s)


def test_unclassified_can_later_bind_but_title_never_classifies(scene):
    s, _, _, event, enable, *_ = scene
    enable(); event(project_id=None, title='test-project')
    s.native_recovery.tick()
    assert incidents(s)[0]['state'] == 'unclassified' and not tasks(s)
    assert '不创建项目' in s.sessions.inbox('codex', incidents(s)[0]['session_id'])['messages'][0]['body']
    with closing(s.registry._connect()) as c:
        before = c.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
    event('context', project_id='test-project'); s.native_recovery.tick()
    assert len(tasks(s)) == 1
    with closing(s.registry._connect()) as c:
        assert c.execute('SELECT COUNT(*) FROM projects').fetchone()[0] == before


def test_restart_and_atomic_rollback(scene, monkeypatch):
    s, _, _, event, enable, *_ = scene
    enable(); event()
    original = s.native_recovery._save
    def crash(c, value):
        if value.get('task_id'):
            raise RuntimeError('crash after task save')
        return original(c, value)
    monkeypatch.setattr(s.native_recovery, '_save', crash)
    with pytest.raises(RuntimeError, match='crash after'):
        s.native_recovery.tick()
    assert not tasks(s) and not incidents(s)
    baseline = s.native_recovery.installed_at
    s.native_recovery = StudioNativeRecovery(s)
    for _ in range(3): s.native_recovery.tick()
    assert s.native_recovery.installed_at == baseline
    assert len(tasks(s)) == len(incidents(s)) == 1
    with closing(s.registry._connect()) as c:
        receipts = c.execute('SELECT request_id FROM studio_task_requests').fetchall()
    assert [r[0] for r in receipts] == [incidents(s)[0]['request_id']]


@pytest.mark.parametrize('manager_mode', ['busy', 'unavailable', 'disabled'])
def test_manager_does_not_block_local_queue_and_bad_candidates_excluded(scene, manager_mode):
    s, _, worker, event, enable, *_ = scene
    archived = profile(s, name='archived'); s.archive({'agent_id': archived['agent_id'], 'expected_revision': archived['revision']})
    failing = profile(s, name='failing')['agent_id']
    failure_run = s.start({'request_id':'failed-worker', 'agent_id':failing,'project_id':'test-project','prompt':'fixture'})
    s._state(failure_run['run_id'], 'failed', exit_code=1)
    manager = profile(s, name='manager')['agent_id']
    s.manager.configure({'request_id':'manager-settings','expected_revision':0,'enabled':manager_mode != 'disabled','agent_id':manager})
    if manager_mode != 'disabled':
        run = s.start({'request_id':'manager-fixture','agent_id':manager,'project_id':'test-project','prompt':'fixture'})
        if manager_mode == 'unavailable': s._state(run['run_id'], 'failed', exit_code=1)
    before = len(s.runs()['runs'])
    enable(); event(); s.native_recovery.tick()
    assert tasks(s)[0]['eligible_agents'] == [worker]
    assert incidents(s)[0]['decision']['action'] == 'local_queue'
    assert len(s.runs()['runs']) == before
    s.tasks.tick()
    assert tasks(s)[0]['owner'] == worker


def test_busy_worker_queues_then_manual_task_cancel_is_final(scene):
    s, _, worker, event, enable, *_ = scene
    s.start({'request_id':'busy-worker','agent_id':worker,'project_id':'test-project','prompt':'fixture'})
    enable(); event(); s.native_recovery.tick(); s.tasks.tick()
    t = tasks(s)[0]
    assert t['state'] == 'queued'
    with s.registry.transaction():
        s.tasks._write(s.registry._connect(), {**t,'state':'paused','auto_run':False}, 'manual_cancel', {})
    s.native_recovery.tick()
    assert incidents(s)[0]['state'] == 'superseded'


def test_current_catalog_membership_and_public_failure(scene):
    s, sid, _, event, enable, advance, catalog, observations = scene
    enable(); event('context')
    source = 'source-' + sid
    catalog.append({'harness':'codex','session_id':sid,'source_id':source,'project_id':None,'title':'test-project','cwd':'source-root'})
    observations[source] = {'state':'failed','explicit_failure':True,'event_at':advance(),'status_basis':'public_task_failed'}
    s.native_recovery.tick()
    assert incidents(s)[0]['state'] == 'unclassified' and not tasks(s)
    catalog[0]['project_id'] = 'test-project'; s.native_recovery.tick()
    assert tasks(s)[0]['project_id'] == 'test-project'
    catalog[0]['membership_conflict'] = True
    with pytest.raises(ContractError, match='native_incident_superseded'):
        t=tasks(s)[0];s.tasks.claim({'request_id':'bad-membership','task_id':t['task_id'],'expected_version':t['version'],'agent_id':t['eligible_agents'][0]})


def test_source_unavailable_never_launches(scene, monkeypatch):
    s, _, _, event, enable, *_ = scene
    enable(); event(); s.native_recovery.tick()
    def offline(**kwargs): raise OSError('fixture source unavailable')
    monkeypatch.setattr(s.service.session_directory, 'catalog', offline)
    assert not s.native_recovery.tick()['ok']
    s.tasks.tick()
    assert not s.runs()['runs']


@pytest.mark.parametrize('record,state,explicit', [
    ({'type':'result','is_error':True,'subtype':'error_during_execution'}, 'failed', True),
    ({'type':'result','is_error':True,'subtype':'cancelled'}, 'cancelled', False),
    ({'type':'result','is_error':True,'subtype':'error_during_execution','is_cancelled':True}, 'cancelled', False),
    ({'type':'result','is_error':True}, 'failed', True),
    ({'type':'result'}, 'unknown', False),
    ({'type':'result','subtype':'success','is_error':False}, 'idle', False),
])
def test_claude_explicit_failure_contract(tmp_path, record, state, explicit):
    path=tmp_path/'claude.jsonl'
    path.write_text(json.dumps({**record,'timestamp':'2026-09-11T00:00:00Z'}),encoding='utf8')
    result=inspect(path,'claude',str(uuid.uuid4()))
    assert result['state']==state and result['explicit_failure'] is explicit


def test_public_parser_uses_event_time_not_append_order(tmp_path):
    sid=str(uuid.uuid4()); path=tmp_path/'codex.jsonl'
    path.write_text('\n'.join(json.dumps(r) for r in [
        {'type':'session_meta','payload':{'id':sid}},
        {'type':'event_msg','timestamp':'2026-09-11T00:02:00Z','payload':{'type':'task_started'}},
        {'type':'event_msg','timestamp':'2026-09-11T00:01:00Z','payload':{'type':'task_failed'}}]),encoding='utf8')
    assert inspect(path,'codex',sid)['state']=='running'



def test_concurrent_ticks_two_instances_create_one_task_and_notice(scene):
    from concurrent.futures import ThreadPoolExecutor
    s, sid, _, event, enable, *_ = scene
    enable(); event()
    second = StudioNativeRecovery(s)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda module: module.tick(), [s.native_recovery, second]))
    assert all(r['ok'] for r in results)
    assert len(tasks(s)) == len(incidents(s)) == 1
    assert len(s.sessions.inbox('codex', sid)['messages']) == 1


def test_full_agent_studio_restart_preserves_incident_and_request(scene):
    s, sid, _, event, enable, *_ = scene
    enable(); event(); s.native_recovery.tick()
    before = incidents(s)[0]
    replacement = agent_studio.AgentStudio(s.service)
    replacement.native_recovery.tick()
    assert incidents(replacement)[0]['incident_id'] == before['incident_id']
    assert incidents(replacement)[0]['task_id'] == before['task_id']
    assert len(tasks(replacement)) == 1
    assert len(replacement.sessions.inbox('codex', sid)['messages']) == 1


def test_tick_integration_runs_recovery_before_task_dispatch(scene):
    s, _, _, event, enable, *_ = scene
    enable(); event(); s.tick()
    assert len(tasks(s)) == 1 and tasks(s)[0]['state'] == 'running'
    assert len(s.runs()['runs']) == 1


def test_no_suitable_executor_and_budget_wait_never_force_launch(scene, monkeypatch):
    s, _, worker, event, enable, *_ = scene
    monkeypatch.setattr(agent_studio, 'claude_executable', lambda: None)
    enable(); event(); s.native_recovery.tick(); s.tasks.tick()
    assert incidents(s)[0]['state'] == 'waiting_candidates' and not tasks(s)
    monkeypatch.setattr(agent_studio, 'claude_executable', lambda: '/fixture/claude')
    monkeypatch.setattr(s.budget, 'check', lambda *args: 'fixture budget exhausted')
    s.native_recovery.tick(); s.tasks.tick()
    assert not tasks(s) and not s.runs()['runs']


def test_available_candidate_fails_while_task_waits(scene):
    s, _, worker, event, enable, *_ = scene
    enable(); event(); s.native_recovery.tick()
    run=s.start({'request_id':'later-failure','agent_id':worker,'project_id':'test-project','prompt':'fixture'})
    s._state(run['run_id'],'failed',exit_code=1)
    s.tasks.tick()
    assert tasks(s)[0]['state']=='queued' and len(s.runs()['runs'])==1


def test_new_failure_after_new_activity_gets_distinct_epoch(scene):
    s, _, _, event, enable, *_ = scene
    enable(); event(); s.native_recovery.tick()
    event('UserPromptSubmit'); event(); s.native_recovery.tick()
    assert len(incidents(s)) == 2 and len(tasks(s)) == 2
    assert len({i['source_epoch'] for i in incidents(s)}) == 2
    assert sorted(t['state'] for t in tasks(s)) == ['paused','queued']


def test_explicit_membership_beats_stale_context_and_title(scene):
    s, sid, _, event, enable, *_ = scene
    enable(); event(project_id='stale-project', title='also-not-a-project')
    with s.registry.transaction():
        s.registry._connect().execute('INSERT INTO task_project_memberships VALUES (?,?,?,?,?)',
                                     ('codex',sid,'test-project',1,'2000-01-01T00:00:00Z'))
    s.native_recovery.tick()
    assert tasks(s)[0]['project_id']=='test-project'


def test_unreadable_registered_source_blocks_old_stored_failure(scene, monkeypatch):
    s, sid, _, event, enable, _, catalog, _ = scene
    enable(); event(); s.native_recovery.tick()
    catalog.append({'harness':'codex','session_id':sid,'source_id':'unreadable','project_id':'test-project','title':'source'})
    def unavailable(source): raise OSError('fixture inaccessible')
    monkeypatch.setattr(s.service.session_directory,'observation',unavailable)
    s.native_recovery.tick();s.tasks.tick()
    assert not s.runs()['runs'] and tasks(s)[0]['state']=='queued'


def test_new_unknown_public_result_invalidates_stored_failure(scene):
    s, sid, _, event, enable, advance, catalog, observations = scene
    enable(); event(); s.native_recovery.tick()
    source='source-'+sid
    catalog.append({'harness':'codex','session_id':sid,'source_id':source,'project_id':'test-project','title':'source'})
    observations[source]={'state':'unknown','event_at':advance(),'status_basis':'public_claude_result_unclassified'}
    s.native_recovery.tick();s.tasks.tick()
    assert incidents(s)[0]['state']=='superseded' and not s.runs()['runs']


def test_future_and_timeless_failure_do_not_trigger_from_public_directory(scene):
    s,sid,_,_,enable,advance,catalog,observations=scene
    enable();source='source-'+sid
    catalog.append({'harness':'codex','session_id':sid,'source_id':source,'project_id':'test-project','title':'source'})
    observations[source]={'state':'failed','explicit_failure':True,'event_at':None}
    s.native_recovery.tick();assert not incidents(s)
    observations[source]['event_at']='2999-01-01T00:00:00Z'
    s.native_recovery.tick();assert not incidents(s)



def test_policy_disabled_interval_cannot_be_retroactively_authorized(scene):
    s,_,_,event,enable,advance,*_=scene
    enable();enable(False);at=advance();enable()
    event(occurred_at=at)
    s.native_recovery.tick();assert not tasks(s) and not incidents(s)


def test_auxiliary_and_managed_sessions_never_create_native_incidents(scene):
    s,sid,worker,event,enable,advance,catalog,observations=scene
    enable();event()
    source='source-'+sid
    catalog.append({'harness':'codex','session_id':sid,'source_id':source,'project_id':'test-project','title':'auxiliary'})
    observations[source]={'state':'failed','explicit_failure':True,'auxiliary':True,'event_at':advance()}
    s.native_recovery.tick();assert not incidents(s)
    run=s.start({'request_id':'managed','agent_id':worker,'project_id':'test-project','prompt':'fixture'})
    managed=s.runs(run['run_id'])['runs'][0]
    event(harness='claude',session_id=managed['session_id'])
    s.native_recovery.tick();assert not incidents(s)
