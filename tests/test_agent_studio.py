"""Deterministic process/storage checks, not evidence of real model success."""
import io
import json
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.contracts import ContractError
from ap_mind.studio_server import StudioEpisodeService
from ap_mind import agent_studio


@pytest.fixture
def studio(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    s = StudioEpisodeService(tmp_path / 'data', project_root=root, codex_project_id='test-project')
    yield s.agent_studio
    s.close()


def profile(studio, **changes):
    return studio.save({'name': '同名伙伴', 'base_url': 'https://example.invalid/v1',
                        'api_key': 'fixture-secret-only', 'model': 'arbitrary-model', **changes})['agent']


def test_profiles_are_separate_encrypted_and_versioned(studio):
    a, b = profile(studio), profile(studio)
    assert a['agent_id'] != b['agent_id']
    assert 'fixture-secret-only' not in json.dumps(studio.profiles())
    with studio.registry._connect() as c:
        rows = c.execute('SELECT secret FROM studio_agents').fetchall()
    assert all(b'fixture-secret-only' not in r[0] for r in rows)
    with pytest.raises(ContractError, match='revision_conflict'):
        profile(studio, agent_id=a['agent_id'], expected_revision=0)
    saved = profile(studio, agent_id=a['agent_id'], expected_revision=1, name='新的名字', api_key='')
    assert saved['revision'] == 2
    studio.archive({'agent_id': a['agent_id'], 'expected_revision': 2})
    assert len(studio.profiles()['agents']) == 2


def test_same_model_distinct_connection_tiers_keep_identity_and_notes(studio):
    cheap=profile(studio,name='Claude Kiro',connection_label='Kiro',capability_notes='clear small tasks')
    full=profile(studio,name='Claude CC Max',connection_label='CC Max',capability_notes='difficult tasks')
    assert cheap['model']==full['model'] and cheap['agent_id']!=full['agent_id']
    edited=profile(studio,agent_id=full['agent_id'],expected_revision=1,name='CC Max renamed',api_key='')
    assert edited['connection_label']=='CC Max' and edited['capability_notes']=='difficult tasks' and edited['key_saved']
    copy=studio.save({'name':'another CC Max','model':full['model'],'base_url':full['base_url'],
        'copy_from_agent_id':full['agent_id'],'copy_from_revision':2})['agent']
    assert copy['connection_label']=='CC Max' and copy['key_saved']
    assert 'fixture-secret-only' not in json.dumps(studio.profiles())


def test_retry_configuration_copy_and_running_snapshot(studio, monkeypatch):
    monkeypatch.setattr(agent_studio, 'claude_executable', lambda: '/fixture/claude')
    monkeypatch.setattr(agent_studio.AgentStudio, '_execute', lambda *_: None)
    default = profile(studio)
    assert default['max_request_retries'] == 5
    zero = profile(studio, max_request_retries=0)
    copied = profile(studio, api_key='', copy_from_agent_id=zero['agent_id'], copy_from_revision=1)
    assert copied['max_request_retries'] == 0
    started = studio.start({'request_id': 'frozen-retries', 'agent_id': default['agent_id'],
                            'project_id': 'test-project', 'prompt': 'test frozen execution policy'})
    profile(studio, agent_id=default['agent_id'], expected_revision=1, max_request_retries=2)
    assert studio.runs(started['run_id'])['runs'][0]['max_request_retries'] == 5
    for invalid in (-1, 1.5, True, 'five'):
        with pytest.raises(ContractError):
            profile(studio, max_request_retries=invalid)


def test_appearance_is_independent_preserved_and_frozen_in_runs(studio, monkeypatch):
    monkeypatch.setattr(agent_studio, 'claude_executable', lambda: '/fixture/claude')
    monkeypatch.setattr(agent_studio.AgentStudio, '_execute', lambda *_: None)
    first = profile(studio, appearance_id='deepseek-v1')
    started = studio.start({'request_id':'appearance-once','agent_id':first['agent_id'],
        'project_id':'test-project','prompt':'Appearance does not change task semantics'})
    saved = profile(studio, agent_id=first['agent_id'], expected_revision=1, api_key='')
    assert saved['appearance_id'] == 'deepseek-v1' and saved['model'] == 'arbitrary-model'
    changed = profile(studio, agent_id=first['agent_id'], expected_revision=2, appearance_id='user-installed-new-family')
    assert changed['appearance_id'] == 'user-installed-new-family'
    assert studio.runs(started['run_id'])['runs'][0]['appearance_id'] == 'deepseek-v1'
    cloned = profile(studio, api_key='', copy_from_agent_id=changed['agent_id'], copy_from_revision=3)
    assert cloned['appearance_id'] == 'user-installed-new-family' and cloned['agent_id'] != changed['agent_id']
    cleared = profile(studio, agent_id=first['agent_id'], expected_revision=3, appearance_id='')
    assert cleared['appearance_id'] == ''
    with pytest.raises(ContractError, match='agent_appearance_invalid'):
        profile(studio, appearance_id='x' * 121)


def test_process_contract_result_and_redaction(studio, monkeypatch):
    a = profile(studio)
    monkeypatch.setenv('API_TIMEOUT_MS', '1')
    monkeypatch.setenv('CLAUDE_STREAM_IDLE_TIMEOUT_MS', '1')
    studio.service.collaboration.send({'request_id': 'context-msg', 'sender': 'planner',
                                       'recipient': a['agent_id'], 'body': '共享验收重点：检查回滚路径'})
    seen = {}
    class Process:
        pid = 123456
        def __init__(self, args, **kwargs):
            seen.update(args=args, **kwargs)
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(('\n'.join(json.dumps(x) for x in [
                {'type': 'system', 'subtype': 'init'},
                *[{'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': f'normal tool round {n}'}]}} for n in range(25)],
                {'type': 'assistant', 'message': {'content': [
                    {'type': 'thinking', 'thinking': 'private-reasoning'},
                    {'type': 'tool_use', 'name': 'Write', 'id': 'tool-1', 'input': {'secret': 'private-input'}},
                    {'type': 'text', 'text': 'answer fixture-secret-only'}]}},
                {'type': 'result', 'subtype': 'success', 'is_error': False, 'usage': {'input_tokens': 4}}
            ])+'\n').encode())
            self.stderr = io.BytesIO()
        def wait(self): return 0
        def poll(self): return 0
    monkeypatch.setattr(agent_studio, 'claude_executable', lambda: '/fixture/claude')
    monkeypatch.setattr(agent_studio.subprocess, 'Popen', Process)
    request = {'request_id': 'once', 'agent_id': a['agent_id'], 'project_id': 'test-project', 'prompt': 'make a file'}
    started = studio.start(request)
    deadline = time.monotonic()+5
    while time.monotonic()<deadline:
        snapshot = studio.runs(started['run_id'])
        if snapshot['runs'][0]['state'] not in {'starting', 'running'}: break
        time.sleep(.01)
    assert snapshot['runs'][0]['state'] == 'awaiting_review'
    assert '--max-turns' not in seen['args']
    assert not snapshot['runs'][0].get('max_turns')
    assert 'normal tool round 24' in json.dumps(snapshot)
    public = json.dumps(snapshot)
    assert 'private-reasoning' not in public and 'private-input' not in public
    assert 'fixture-secret-only' not in public
    assert seen['env']['ANTHROPIC_MODEL'] == a['model']
    assert seen['env']['ANTHROPIC_DEFAULT_HAIKU_MODEL'] == a['model']
    # CC's own deadline must cover six upstream attempts plus backoff,
    # otherwise its SDK can abandon the gateway while recovery is working.
    assert int(seen['env']['API_TIMEOUT_MS']) > 6 * 300 * 1000 + 31000
    assert seen['env']['CLAUDE_STREAM_IDLE_TIMEOUT_MS'] == seen['env']['API_TIMEOUT_MS']
    assert seen['env']['CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS'] == seen['env']['API_TIMEOUT_MS']
    assert 'fixture-secret-only' not in str(seen['args'])
    assert 'Bash' in seen['args'][seen['args'].index('--tools')+1].split(',')
    assert 'Bash' in seen['args'][seen['args'].index('--allowedTools')+1].split(',')
    assert 'mcp__yinzi-media__*' not in seen['args'][seen['args'].index('--allowedTools')+1]
    assert '共享验收重点：检查回滚路径' not in ' '.join(str(x) for x in seen['args'])
    assert studio.service.collaboration.list(a['agent_id'])['messages'][0]['body'] == '共享验收重点：检查回滚路径'
    assert Path(seen['cwd']).parent.name == started['run_id']
    again = studio.start(request)
    assert again['replayed'] and again['run_id'] == started['run_id']
    with pytest.raises(ContractError, match='request_conflict'):
        studio.start({**request, 'prompt': 'different'})
    cursor = snapshot['next_cursor']
    assert not studio.runs(started['run_id'], cursor)['events']
    acceptance = {'run_id': started['run_id'], 'request_id': 'review-once', 'accepted': True,
                  'note': 'Fixture validation only', 'reviewer': 'test', 'expected_revision': 0}
    assert studio.review(acceptance)['state'] == 'completed'
    assert studio.review(acceptance)['replayed']
    with pytest.raises(ContractError, match='request_conflict'):
        studio.review({**acceptance, 'accepted': False})
    continued = studio.start({**request, 'request_id': 'next-turn', 'prompt': 'continue work',
                              'continue_run_id': started['run_id']})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        next_run = studio.runs(continued['run_id'])['runs'][0]
        if next_run['state'] not in {'starting', 'running'}: break
        time.sleep(.01)
    assert '--resume' in seen['args'] and '--session-id' not in seen['args']
    assert next_run['session_id'] == snapshot['runs'][0]['session_id']
    assert next_run['workspace'] == snapshot['runs'][0]['workspace']
    assert next_run['parent_run_id'] == started['run_id']
    profile(studio, agent_id=a['agent_id'], expected_revision=1)
    with pytest.raises(ContractError, match='profile_changed'):
        studio.start({**request, 'request_id': 'changed-profile', 'continue_run_id': started['run_id']})


def test_recovery_never_replays_uncertain_run(studio):
    with studio.registry._connect() as c:
        c.execute('INSERT INTO studio_runs VALUES (?,?,?,?,?,?)', ('run-fixture','request','hash','agent-fixture','running','{}'))
        c.commit()
    cold = agent_studio.AgentStudio(studio.service)
    assert cold.runs()['runs'][0]['state'] == 'interrupted'
    assert not cold._processes


def test_late_provider_usage_does_not_revive_cancelled_execution(studio):
    with studio.registry._connect() as c:
        c.execute('INSERT INTO studio_runs VALUES (?,?,?,?,?,?)',
                  ('run-late-usage','late-request','hash','agent-fixture','cancelled','{}'))
    studio._state('run-late-usage', None, provider_usage=[{'request_id':'original','usage':{'input_tokens':12}}])
    saved=studio.runs('run-late-usage')['runs'][0]
    assert saved['state']=='cancelled' and saved['provider_usage'][0]['usage']['input_tokens']==12


def test_copy_reuses_encrypted_key_without_exposing_it(studio):
    source = profile(studio)
    copy = profile(studio, api_key='', copy_from_agent_id=source['agent_id'], copy_from_revision=1)
    assert copy['agent_id'] != source['agent_id'] and copy['key_saved']
    assert 'fixture-secret-only' not in json.dumps(copy)
    with pytest.raises(ContractError, match='agent_copy_source_changed'):
        profile(studio, api_key='', copy_from_agent_id=source['agent_id'], copy_from_revision=0)


def test_waiting_run_is_woken_after_dependency_commit(studio, monkeypatch):
    """The lifecycle remains deterministic even when the worker is delayed."""
    a = profile(studio)
    monkeypatch.setattr(agent_studio, 'claude_executable', lambda: '/fixture/claude')
    launched = []

    def delayed_execute(run_id, value, profile_value, key, project):
        launched.append(run_id)

    monkeypatch.setattr(studio, '_execute', delayed_execute)
    upstream = studio.start({'request_id': 'wake-upstream', 'agent_id': a['agent_id'],
                             'project_id': 'test-project', 'prompt': '先完成基础任务'})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not launched:
        time.sleep(.01)
    assert launched == [upstream['run_id']]
    downstream = studio.start({'request_id': 'wake-downstream', 'agent_id': a['agent_id'],
                               'project_id': 'test-project', 'prompt': '读取基础成果继续处理',
                               'depends_on': upstream['run_id']})
    assert downstream['state'] == 'waiting'
    assert studio.runs(downstream['run_id'])['runs'][0]['state'] == 'waiting'
    studio._state(upstream['run_id'], 'awaiting_review')
    result = studio.wake_ready()
    assert result == {'ok': True, 'woken': [downstream['run_id']]}
    assert launched == [upstream['run_id'], downstream['run_id']]
    assert studio.runs(downstream['run_id'])['runs'][0]['state'] == 'starting'
    assert studio.wake_ready() == {'ok': True, 'woken': []}
    late = studio.start({'request_id': 'late-downstream', 'agent_id': a['agent_id'],
                         'project_id': 'test-project', 'prompt': '登记前上游已完成',
                         'depends_on': upstream['run_id']})
    assert late['state'] == 'starting'


def test_failed_run_can_be_handed_off_without_replaying_model_request(studio):
    source = profile(studio, name='执行Agent')
    target = profile(studio, name='接手Agent')
    with studio.registry._connect() as c:
        c.execute('INSERT INTO studio_runs VALUES (?,?,?,?,?,?)',
                  ('run-failed', 'failed-request', 'fingerprint', source['agent_id'],
                   'failed', json.dumps({'agent_id': source['agent_id'], 'project_id': 'test-project'}, ensure_ascii=False)))
        c.commit()
    result = studio.handoff_failed({'run_id': 'run-failed', 'to_agent': target['agent_id'], 'note': '请复核文件后继续'})
    assert result['ok'] and result['to_agent'] == target['agent_id']
    state = studio.service.collaboration.state()
    assert state['claims'][0]['agent_id'] == target['agent_id']
    assert studio.runs('run-failed')['runs'][0]['state'] == 'failed'


def test_uncertain_dependency_never_launches_and_wait_can_be_cancelled(studio, monkeypatch):
    a=profile(studio)
    monkeypatch.setattr(agent_studio, 'claude_executable', lambda: '/fixture/claude')
    monkeypatch.setattr(studio, '_execute', lambda *args: None)
    base={'agent_id':a['agent_id'],'project_id':'test-project','prompt':'dependency test'}
    upstream=studio.start({**base,'request_id':'negative-parent'})
    studio._state(upstream['run_id'],'uncertain')
    child=studio.start({**base,'request_id':'negative-child','depends_on':upstream['run_id']})
    assert child['state']=='waiting' and studio.wake_ready()['woken']==[]
    assert studio.cancel({'run_id':child['run_id']})['state']=='cancelled'
    studio._state(upstream['run_id'],'awaiting_review')
    assert studio.wake_ready()['woken']==[]
    with pytest.raises(ContractError,match='dependency_not_found'):
        studio.start({**base,'request_id':'unknown-parent','depends_on':'missing'})


def test_dependency_manifest_points_to_actual_public_artifact(studio, monkeypatch):
    a=profile(studio)
    monkeypatch.setattr(agent_studio, 'claude_executable', lambda: '/fixture/claude')
    monkeypatch.setattr(studio, '_execute', lambda *args: None)
    up=studio.start({'request_id':'artifact-parent','agent_id':a['agent_id'],
                     'project_id':'test-project','prompt':'write artifact'})
    run=studio.runs(up['run_id'])['runs'][0]
    path=Path(run['workspace']);path.mkdir(parents=True)
    (path/'report.md').write_text('actual artifact',encoding='utf-8')
    studio._state(up['run_id'],'awaiting_review')
    manifest=studio.dependency_context({'depends_on':up['run_id']})
    assert manifest['files']==[{'path':str(path/'report.md'),'size':15}]
    assert manifest['state']=='awaiting_review' and manifest['agent_id']==a['agent_id']


def test_parallel_join_needs_all_results_and_recovers_after_restart(studio, monkeypatch):
    a=profile(studio)
    monkeypatch.setattr(agent_studio, 'claude_executable', lambda: '/fixture/claude')
    launches=[]
    monkeypatch.setattr(agent_studio.AgentStudio,'_execute',lambda self,run_id,*args:launches.append(run_id))
    base={'agent_id':a['agent_id'],'project_id':'test-project','prompt':'parallel work'}
    one=studio.start({**base,'request_id':'parallel-one'})
    two=studio.start({**base,'request_id':'parallel-two'})
    join=studio.start({**base,'request_id':'parallel-join','depends_on':[one['run_id'],two['run_id']]})
    assert join['state']=='waiting'
    studio._state(one['run_id'],'awaiting_review')
    assert studio.wake_ready()['woken']==[]
    studio._state(two['run_id'],'awaiting_review')
    cold=agent_studio.AgentStudio(studio.service)
    assert cold.wake_ready()['woken']==[join['run_id']]
    assert cold.wake_ready()['woken']==[]
    deadline=time.monotonic()+2
    while join['run_id'] not in launches and time.monotonic()<deadline: time.sleep(.01)
    assert launches.count(join['run_id'])==1
