"""Failure takeover must preserve work and require evidence of executor exit."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_studio_tasks import service, profile
from ap_mind.agent_studio import AgentStudio
from ap_mind.contracts import ContractError


def setup_task(service, monkeypatch, *, auto=True):
    studio = service.agent_studio
    first, second = profile(service, 'first'), profile(service, 'second')
    monkeypatch.setattr('ap_mind.agent_studio.claude_executable', lambda: 'fixture')
    monkeypatch.setattr(studio, '_execute', lambda *args: None)
    task = studio.tasks.save({'request_id': 'save', 'project_id': 'test-project',
        'title': 'Continue existing work', 'goal': 'Preserve partial file', 'acceptance': 'Read and finish',
        'eligible_agents': [first['agent_id'], second['agent_id']], 'auto_run': auto, 'max_author_retries': 0})['task']
    studio.tasks.claim({'request_id': 'claim', 'task_id': task['task_id'],
        'expected_version': task['version'], 'agent_id': first['agent_id']})
    studio.tasks.dispatch(task['task_id'])
    task = studio.tasks.list(task['task_id'])['tasks'][0]
    thread = studio._threads.pop(task['run_id']); thread.join()
    run = studio.runs(task['run_id'])['runs'][0]
    workspace = Path(run['workspace']); workspace.mkdir(parents=True)
    (workspace / 'draft.txt').write_text('Unique original evidence', encoding='utf-8')
    (workspace / '.env').write_text('excluded-fixture', encoding='utf-8')
    studio._event(run['run_id'], 'assistant', {'text': 'Draft saved; conclusion still needed.'})
    return studio, task, run, first, second


@pytest.mark.parametrize('state', ['failed', 'uncertain'])
def test_exited_failure_adopted_once_in_new_workspace(service, monkeypatch, state):
    studio, task, run, first, second = setup_task(service, monkeypatch)
    studio._state(run['run_id'], state, pid=123, exit_code=1, error='Provider outcome unknown')
    # The state may have been persisted while the executor is still cleaning up.
    studio._threads[run['run_id']] = object()
    studio.tasks.tick()
    assert studio.tasks.list(task['task_id'])['tasks'][0]['state'] == 'needs_help'
    studio._threads.pop(run['run_id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: studio.tasks.tick(), range(2)))
    current = studio.tasks.list(task['task_id'])['tasks'][0]
    assert current['owner'] == second['agent_id'] and len(current['attempts']) == 2
    adopted = studio.runs(current['run_id'])['runs'][0]
    assert adopted['workspace'] != run['workspace'] and adopted['fork_workspace']
    copied = studio.prepare_workspace(adopted)
    assert (copied / 'draft.txt').read_text() == 'Unique original evidence'
    assert not (copied / '.env').exists()
    context = studio.handoff_context(adopted)
    assert context['execution']['stopped'] is True
    assert context['execution']['error'] == 'Provider outcome unknown'
    assert context['public_output'][0]['text'] == 'Draft saved; conclusion still needed.'
    studio._threads.pop(current['run_id']).join()
    studio._state(current['run_id'], 'uncertain', exit_code=1, pid=124)
    studio.tasks.tick(); studio.tasks.tick()
    exhausted = studio.tasks.list(task['task_id'])['tasks'][0]
    assert exhausted['state'] == 'needs_help' and exhausted['takeover_issue']
    assert len(exhausted['attempts']) == 2
    assert set(exhausted['takeover_tried_agents']) == {first['agent_id'], second['agent_id']}


@pytest.mark.parametrize('case', ['missing_exit', 'process_live', 'cancelled', 'interrupted', 'manual'])
def test_no_automatic_takeover_without_intent_and_exit(service, monkeypatch, case):
    studio, task, run, first, second = setup_task(service, monkeypatch, auto=case != 'manual')
    state = case if case in {'cancelled', 'interrupted'} else 'uncertain'
    studio._state(run['run_id'], state, pid=123, **({} if case == 'missing_exit' else {'exit_code': 1}))
    if case == 'process_live':
        studio._processes[run['run_id']] = object()
    try:
        studio.tasks.tick(); studio.tasks.tick()
        assert len(studio.tasks.list(task['task_id'])['tasks'][0]['attempts']) == 1
        if case in {'missing_exit', 'process_live'}:
            with pytest.raises(ContractError, match='source_not_stopped'):
                studio.start({'request_id': 'blocked', 'agent_id': second['agent_id'],
                    'project_id': 'test-project', 'prompt': 'Continue', 'handoff_from_run_id': run['run_id']})
    finally:
        studio._processes.pop(run['run_id'], None)


def test_restart_reconciles_persisted_uncertain_and_manual_release(service, monkeypatch):
    studio, task, run, first, second = setup_task(service, monkeypatch)
    studio._state(run['run_id'], 'uncertain', pid=123, exit_code=1)
    studio._threads[run['run_id']] = object()
    studio.tasks.tick()
    studio._threads.pop(run['run_id'])
    cold = AgentStudio(service)
    monkeypatch.setattr(cold, '_execute', lambda *args: None)
    try:
        cold.tasks.tick()
        current = cold.tasks.list(task['task_id'])['tasks'][0]
        assert current['owner'] == second['agent_id'] and len(current['attempts']) == 2
        cold._threads.pop(current['run_id']).join()
        cold._state(current['run_id'], 'uncertain', pid=124, exit_code=1)
        cold.tasks.tick()
        current = cold.tasks.list(task['task_id'])['tasks'][0]
        released = cold.tasks.release({'request_id': 'manual-release', 'task_id': current['task_id'],
            'expected_version': current['version'], 'note': 'Continue after checking files'})['task']
        assert released['state'] == 'queued' and released['handoff_mode'] == 'copy'
        assert released['takeover_tried_agents'] == []
    finally:
        cold.shutdown()


def test_rework_prefers_last_author_after_cross_agent_takeover(service, monkeypatch):
    studio, task, run, first, second = setup_task(service, monkeypatch)
    studio._state(run['run_id'], 'changes_requested', exit_code=0)
    with studio.registry.transaction():
        c = studio.registry._connect()
        current = studio.tasks._read(c, task['task_id'])
        studio.tasks._write(c, {**current, 'state': 'queued', 'owner': None, 'rework_pending': True,
            'rework_agent_id': second['agent_id'], 'takeover_tried_agents': [],
            'handoff_run_id': run['run_id'], 'handoff_mode': 'copy'}, 'fixture', {})
    studio.tasks.tick()
    current = studio.tasks.list(task['task_id'])['tasks'][0]
    assert current['owner'] == second['agent_id'] and current['state'] == 'running'
    studio._threads.pop(current['run_id']).join()


def test_configured_retry_after_candidates_exhausted_is_bounded(service, monkeypatch):
    studio, task, run, first, second = setup_task(service, monkeypatch)
    with studio.registry.transaction():
        c = studio.registry._connect()
        current = studio.tasks._read(c, task['task_id'])
        studio.tasks._write(c, {**current, 'max_author_retries': 1}, 'fixture', {})
    studio._state(run['run_id'], 'uncertain', pid=123, exit_code=1)
    studio.tasks.tick()
    current = studio.tasks.list(task['task_id'])['tasks'][0]
    assert current['owner'] == second['agent_id'] and current['author_retry_count'] == 0
    studio._threads.pop(current['run_id']).join()
    studio._state(current['run_id'], 'uncertain', pid=124, exit_code=1)
    studio.tasks.tick(); studio.tasks.tick()
    recovered = studio.tasks.list(task['task_id'])['tasks'][0]
    assert recovered['owner'] == second['agent_id'] and recovered['author_retry_count'] == 1
    assert len(recovered['attempts']) == 3
    studio._threads.pop(recovered['run_id']).join()
    studio._state(recovered['run_id'], 'uncertain', pid=125, exit_code=1)
    studio.tasks.tick(); studio.tasks.tick()
    exhausted = studio.tasks.list(task['task_id'])['tasks'][0]
    assert exhausted['state'] == 'needs_help' and len(exhausted['attempts']) == 3


def test_archived_backup_cannot_leave_failed_author_queued_forever(service, monkeypatch):
    studio, task, run, first, second = setup_task(service, monkeypatch)
    studio.archive({'agent_id': second['agent_id'], 'expected_revision': second['revision']})
    studio._state(run['run_id'], 'uncertain', pid=123, exit_code=1)
    studio.tasks.tick()
    current = studio.tasks.list(task['task_id'])['tasks'][0]
    assert current['state'] == 'needs_help' and current['takeover_issue']
