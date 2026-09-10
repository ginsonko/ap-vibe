"""Reviewer scheduling: public APIs, real SQLite, isolated executor boundary."""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind import agent_studio
from ap_mind.contracts import ContractError
from ap_mind.studio_server import StudioEpisodeService


@pytest.fixture
def rig(tmp_path, monkeypatch):
    launches = []
    monkeypatch.setattr(agent_studio, 'claude_executable', lambda: '/fixture/claude')
    monkeypatch.setattr(agent_studio.AgentStudio, '_execute', lambda self, run_id, *args: launches.append(run_id))
    root = tmp_path / 'project'; root.mkdir()
    services = []
    def open_service():
        service = StudioEpisodeService(tmp_path / 'data', project_root=root, codex_project_id='test-project')
        original_state = service.agent_studio._state
        def executor_state(run_id, state, **extra):
            if state == 'awaiting_review':
                extra.setdefault('exit_code', 0)
                worker_thread = service.agent_studio._threads.pop(run_id, None)
                if worker_thread:
                    worker_thread.join()
            original_state(run_id, state, **extra)
        monkeypatch.setattr(service.agent_studio, '_state', executor_state)
        services.append(service)
        return service
    service = open_service()
    studio = service.agent_studio
    def profile(name):
        return studio.save({'name': name, 'base_url': 'https://example.invalid/v1', 'model': 'fixture', 'api_key': 'fixture-key'})['agent']
    worker, reviewer = profile('作者'), profile('检查员')
    task = studio.tasks.save({'request_id': 'parent', 'project_id': 'test-project', 'title': '编写教程',
        'goal': '教程必须说明离线基础模式与可选网络模式', 'acceptance': '包含两种模式及实例',
        'eligible_agents': [worker['agent_id']], 'reviewer_agent_id': reviewer['agent_id'], 'auto_run': True})['task']
    studio.tasks.tick()
    task = studio.tasks.list(task['task_id'])['tasks'][0]
    workspace = Path(studio.runs(task['run_id'])['runs'][0]['workspace']); workspace.mkdir(parents=True)
    (workspace / 'guide.md').write_text('基础模式离线；模型增强联网。', encoding='utf-8')
    yield service, task, worker, reviewer, launches, open_service
    for service in services:
        service.close()


def test_returned_result_starts_review_without_preaccepting_and_has_real_file_context(rig):
    service, parent, worker, reviewer, launches, _ = rig
    studio = service.agent_studio
    studio._state(parent['run_id'], 'awaiting_review')
    studio.tasks.tick()
    current = studio.tasks.list(parent['task_id'])['tasks'][0]
    child = studio.tasks.list(current['review_task_id'])['tasks'][0]
    assert current['state'] == 'waiting_review'
    assert child['state'] == 'running' and child['owner'] == reviewer['agent_id']
    assert child['review_source_run_id'] == parent['run_id']
    run = studio.runs(child['run_id'])['runs'][0]
    context = studio.dependency_context(run)
    assert context['run_id'] == parent['run_id']
    assert any(Path(f['path']).name == 'guide.md' for f in context['files'])
    assert parent['goal'] in run['prompt'] and parent['acceptance'] in run['prompt']
    assert 'review_history' not in studio.runs(parent['run_id'])['runs'][0]
    studio._state(child['run_id'], 'awaiting_review')
    studio.tasks.tick(); studio.tasks.tick()
    assert len(studio.tasks.list()['tasks']) == 2
    assert studio.tasks.list(child['task_id'])['tasks'][0]['state'] == 'waiting_review'


@pytest.mark.parametrize('state', ['failed', 'uncertain', 'interrupted', 'cancelled', 'changes_requested'])
def test_non_successful_parent_does_not_launch_review(rig, state):
    service, parent, *rest = rig
    studio = service.agent_studio
    studio._state(parent['run_id'], state)
    studio.tasks.tick()
    assert len(studio.tasks.list()['tasks']) == 1


def test_reconciliation_recovers_crash_between_state_write_and_review_registration(rig, monkeypatch):
    service, parent, worker, reviewer, launches, reopen = rig
    studio = service.agent_studio
    studio._state(parent['run_id'], 'awaiting_review')
    monkeypatch.setattr(studio.tasks, '_ensure_review_task', lambda task: None)
    studio.tasks.tick()
    assert studio.tasks.list(parent['task_id'])['tasks'][0]['state'] == 'waiting_review'
    service.close()
    cold = reopen().agent_studio
    cold.tasks.tick()
    linked = cold.tasks.list(parent['task_id'])['tasks'][0]
    child = cold.tasks.list(linked['review_task_id'])['tasks'][0]
    cold._state(child['run_id'], 'awaiting_review')
    cold.shutdown()
    colder = reopen().agent_studio
    colder.tasks.tick()
    assert len(colder.tasks.list()['tasks']) == 2
    assert colder.tasks.list(parent['task_id'])['tasks'][0]['review_task_id'] == child['task_id']
    assert len(colder.tasks.list(child['task_id'])['tasks'][0]['attempts']) == 1


def test_registration_rolls_back_both_links_and_retries_after_crash(rig, monkeypatch):
    service, parent, *rest = rig
    studio = service.agent_studio
    studio._state(parent['run_id'], 'awaiting_review')
    original = studio.tasks._write
    def fail_parent(c, task, kind, detail):
        if kind == 'review_linked': raise OSError('simulated disk write failure')
        return original(c, task, kind, detail)
    monkeypatch.setattr(studio.tasks, '_write', fail_parent)
    with pytest.raises(OSError): studio.tasks.tick()
    assert len(studio.tasks.list()['tasks']) == 1
    monkeypatch.setattr(studio.tasks, '_write', original)
    studio.tasks.tick()
    assert len(studio.tasks.list()['tasks']) == 2


def test_ordinary_dependency_cannot_forge_review_exception(rig):
    service, parent, worker, reviewer, *_ = rig
    studio = service.agent_studio
    studio._state(parent['run_id'], 'awaiting_review'); studio.tasks.tick()
    raw = {'request_id': 'ordinary', 'project_id': parent['project_id'], 'title': '下游', 'goal': '等待验收',
           'acceptance': '不得提前执行', 'dependencies': [parent['task_id']], 'tags': ['review']}
    child = studio.tasks.save(raw)['task']
    with pytest.raises(ContractError, match='dependencies_pending'):
        studio.tasks.claim({'request_id': 'claim-ordinary', 'task_id': child['task_id'],
                            'expected_version': child['version'], 'agent_id': worker['agent_id']})
    with pytest.raises(ContractError, match='server_managed'):
        studio.tasks.save({**raw, 'review_of_task_id': parent['task_id']})


def test_archived_reviewer_preserves_parent_then_recovers_after_restore(rig):
    service, parent, worker, reviewer, *_ = rig
    studio = service.agent_studio
    archived = studio.archive({'agent_id': reviewer['agent_id'], 'expected_revision': reviewer['revision']})['agent']
    studio._state(parent['run_id'], 'awaiting_review'); studio.tasks.tick(); studio.tasks.tick()
    waiting = studio.tasks.list(parent['task_id'])['tasks'][0]
    assert waiting['state'] == 'waiting_review' and waiting['review_issue']
    assert len(studio.tasks.list()['tasks']) == 1
    studio.save({**archived, 'expected_revision': archived['revision'], 'api_key': ''})
    studio.tasks.tick()
    assert len(studio.tasks.list()['tasks']) == 2


def test_no_self_review_and_missing_reviewer_rejected(rig):
    service, parent, worker, reviewer, *_ = rig
    studio = service.agent_studio
    for reviewer_id, code in ((worker['agent_id'], 'reviewer_must_differ'), ('missing', 'reviewer_agent_not_found')):
        with pytest.raises(ContractError, match=code):
            studio.tasks.save({'request_id': reviewer_id, 'project_id': parent['project_id'], 'title': 'test',
                               'goal': 'test', 'acceptance': 'test', 'eligible_agents': [worker['agent_id']],
                               'reviewer_agent_id': reviewer_id})


def test_old_tasks_are_not_starved_by_display_limit_and_concurrent_ticks(rig):
    service, parent, *_ = rig
    studio = service.agent_studio
    with studio.registry.transaction():
        c = studio.registry._connect()
        for i in range(205):
            item = {**parent, 'task_id': f'history-{i}', 'state': 'archived', 'reviewer_agent_id': None}
            c.execute('INSERT INTO studio_tasks VALUES (?,?,?,?)', (item['task_id'], 1, 'archived', json.dumps(item)))
    studio._state(parent['run_id'], 'awaiting_review')
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: studio.tasks.tick(), range(2)))
    current = studio.tasks.list(parent['task_id'])['tasks'][0]
    assert current['review_task_id'] and len(current['review_task_history']) == 1
    child = studio.tasks.list(current['review_task_id'])['tasks'][0]
    assert len(child['attempts']) == 1


def test_reviewer_failure_does_not_accept_author_or_replay_failed_attempt(rig):
    service, parent, *_ = rig
    studio = service.agent_studio
    studio._state(parent['run_id'], 'awaiting_review'); studio.tasks.tick()
    linked = studio.tasks.list(parent['task_id'])['tasks'][0]
    child = studio.tasks.list(linked['review_task_id'])['tasks'][0]
    studio._state(child['run_id'], 'failed'); studio.tasks.tick(); studio.tasks.tick()
    assert studio.tasks.list(child['task_id'])['tasks'][0]['state'] == 'needs_help'
    assert studio.tasks.list(parent['task_id'])['tasks'][0]['state'] == 'waiting_review'
    assert len(studio.tasks.list(child['task_id'])['tasks'][0]['attempts']) == 1


def test_waiting_reviewer_does_not_switch_to_newer_author_attempt(rig, monkeypatch):
    service, parent, *_ = rig
    studio = service.agent_studio
    studio._state(parent['run_id'], 'awaiting_review')
    original = studio.tasks.dispatch
    monkeypatch.setattr(studio.tasks, 'dispatch', lambda task_id: None)
    studio.tasks.tick()
    linked = studio.tasks.list(parent['task_id'])['tasks'][0]
    child = studio.tasks.list(linked['review_task_id'])['tasks'][0]
    with studio.registry.transaction():
        c = studio.registry._connect()
        changed = studio.tasks._read(c, parent['task_id'])
        studio.tasks._write(c, {**changed, 'assignment_epoch': 2, 'run_id': 'new-attempt', 'state': 'running'}, 'fixture', {})
    monkeypatch.setattr(studio.tasks, 'dispatch', original)
    studio.tasks.tick()
    stale = studio.tasks.list(child['task_id'])['tasks'][0]
    assert stale['state'] == 'needs_help' and stale['run_id'] is None


def test_cross_project_dependency_cannot_be_used_as_review_input(rig, tmp_path):
    service, parent, *_ = rig
    root = tmp_path / 'other'; root.mkdir()
    service.product_registry.register(project_id='other', display_name='Other', root_path=root, source='test')
    with pytest.raises(ContractError, match='dependency_project_mismatch'):
        service.agent_studio.tasks.save({'request_id': 'cross-project', 'project_id': 'other', 'title': 'other',
                                        'goal': 'other', 'acceptance': 'other', 'dependencies': [parent['task_id']]})
