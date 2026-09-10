"""Public verdict protocol with real files/transactions and no provider calls."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from test_studio_review import rig
from ap_mind.contracts import ContractError


def reviewer_ready(rig):
    service, parent, worker, reviewer, *_ = rig
    studio = service.agent_studio
    studio._state(parent['run_id'], 'awaiting_review')
    studio.tasks.tick()
    parent = studio.tasks.list(parent['task_id'])['tasks'][0]
    child = studio.tasks.list(parent['review_task_id'])['tasks'][0]
    workspace = Path(studio.runs(child['run_id'])['runs'][0]['workspace'])
    workspace.mkdir(parents=True)
    (workspace / 'acceptance.md').write_text('# Report\nActual file checked. Missing example.', encoding='utf-8')
    raw = {'request_id': 'verdict-one', 'run_id': child['run_id'], 'agent_id': reviewer['agent_id'],
           'outcome': 'changes_requested', 'note': 'Add a concrete example.', 'report_path': 'acceptance.md',
           'source_files': ['guide.md'], 'checks': [{'criterion': 'Modes and example', 'status': 'failed',
                'evidence': 'guide.md has modes, but no example.', 'required_change': 'Add an example.'}]}
    return studio, parent, child, raw, workspace


def test_real_reject_rework_accept_preserves_files_and_unblocks_downstream(rig):
    studio, parent, child, raw, _ = reviewer_ready(rig)
    dependent = studio.tasks.save({'request_id': 'dependent', 'project_id': parent['project_id'],
        'title': 'Publish summary', 'goal': 'Read accepted result', 'acceptance': 'Use the latest accepted version',
        'dependencies': [parent['task_id']], 'eligible_agents': [parent['owner']], 'auto_run': True})['task']
    studio.tasks.verdicts.submit(raw)
    studio.tasks.tick()
    assert studio.tasks.list(parent['task_id'])['tasks'][0]['state'] == 'waiting_review'
    assert studio.tasks.list(dependent['task_id'])['tasks'][0]['run_id'] is None
    studio._state(child['run_id'], 'awaiting_review')
    studio.tasks.tick()
    rework = studio.tasks.list(parent['task_id'])['tasks'][0]
    assert rework['owner'] == parent['owner'] and rework['rework_round'] == 1
    assert len(rework['attempts']) == 2 and rework['state'] == 'running'
    new_run = studio.runs(rework['run_id'])['runs'][0]
    original = Path(studio.runs(parent['run_id'])['runs'][0]['workspace'])
    (original / '.env').write_text('secret', encoding='utf-8')
    fresh = studio.prepare_workspace(new_run)
    assert fresh != original and not (fresh / '.env').exists()
    assert (fresh / 'guide.md').read_text(encoding='utf-8') == (original / 'guide.md').read_text(encoding='utf-8')
    (fresh / 'guide.md').write_text('Modes plus a working example.', encoding='utf-8')
    assert 'example' not in (original / 'guide.md').read_text(encoding='utf-8')
    studio._state(rework['run_id'], 'awaiting_review'); studio.tasks.tick()
    parent_now = studio.tasks.list(parent['task_id'])['tasks'][0]
    review2 = studio.tasks.list(parent_now['review_task_id'])['tasks'][0]
    assert review2['task_id'] != child['task_id'] and review2['review_source_epoch'] == 2
    report2 = Path(studio.runs(review2['run_id'])['runs'][0]['workspace'])
    report2.mkdir(parents=True); (report2 / 'acceptance.md').write_text('Modes and example verified.', encoding='utf-8')
    accepted = {**raw, 'request_id': 'verdict-two', 'run_id': review2['run_id'], 'outcome': 'accepted',
                'checks': [{'criterion': 'Modes and example', 'status': 'passed', 'evidence': 'guide.md contains both.'}]}
    studio.tasks.verdicts.submit(accepted)
    studio._state(review2['run_id'], 'awaiting_review'); studio.tasks.tick(); studio.tasks.tick()
    final = studio.tasks.list(parent['task_id'])['tasks'][0]
    assert final['state'] == 'completed' and len(final['verdict_history']) == 2
    assert len(final['attempts']) == 2 and len(final['review_task_history']) == 2
    assert studio.tasks.list(dependent['task_id'])['tasks'][0]['state'] == 'running'
    assert studio.tasks.verdicts.submit(accepted)['replayed']


def test_duplicate_and_concurrent_submission_cannot_schedule_twice(rig):
    studio, parent, child, raw, _ = reviewer_ready(rig)
    with ThreadPoolExecutor(3) as pool:
        result = list(pool.map(lambda _: studio.tasks.verdicts.submit(raw), range(3)))
    assert sum(not r['replayed'] for r in result) == 1
    with pytest.raises(ContractError, match='request_conflict'):
        studio.tasks.verdicts.submit({**raw, 'note': 'Changed intent'})
    studio._state(child['run_id'], 'awaiting_review')
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(lambda _: studio.tasks.tick(), range(3)))
    assert len(studio.tasks.list(parent['task_id'])['tasks'][0]['attempts']) == 2


@pytest.mark.parametrize('defect', ['identity', 'unknown_pass', 'missing_change', 'missing_report', 'escape', 'old_epoch'])
def test_invalid_verdict_never_accepts_or_requeues(rig, defect):
    studio, parent, child, raw, _ = reviewer_ready(rig)
    if defect == 'identity': raw['agent_id'] = parent['owner']
    elif defect == 'unknown_pass': raw.update(outcome='accepted', checks=[{'criterion': 'unknown', 'status': 'unknown', 'evidence': 'not tested'}])
    elif defect == 'missing_change': raw['checks'][0].pop('required_change')
    elif defect == 'missing_report': raw['report_path'] = 'absent.md'
    elif defect == 'escape': raw['source_files'] = ['../private.txt']
    else:
        with studio.registry.transaction():
            c = studio.registry._connect()
            studio.tasks._write(c, {**parent, 'assignment_epoch': 99}, 'fixture', {})
    with pytest.raises(ContractError): studio.tasks.verdicts.submit(raw)
    assert studio.runs(parent['run_id'])['runs'][0]['state'] == 'awaiting_review'
    assert not studio.tasks.list(child['task_id'])['tasks'][0].get('verdict')


@pytest.mark.parametrize('defect', ['changed_report', 'changed_source', 'failed_reviewer', 'manual_decision', 'archived_parent'])
def test_post_submission_changes_preserve_result_without_blind_execution(rig, defect):
    studio, parent, child, raw, workspace = reviewer_ready(rig)
    studio.tasks.verdicts.submit(raw)
    studio._state(child['run_id'], 'awaiting_review')
    if defect == 'changed_report': (workspace / 'acceptance.md').write_text('Different report.', encoding='utf-8')
    elif defect == 'changed_source':
        (Path(studio.runs(parent['run_id'])['runs'][0]['workspace']) / 'guide.md').write_text('Different source.', encoding='utf-8')
    elif defect == 'failed_reviewer': studio._state(child['run_id'], 'failed', exit_code=None)
    elif defect == 'manual_decision':
        studio.review({'request_id': 'human', 'run_id': parent['run_id'], 'reviewer': 'user', 'accepted': True, 'note': 'Manual decision'})
    else: studio.tasks.archive({'task_id': parent['task_id'], 'expected_version': parent['version']})
    studio.tasks.tick(); studio.tasks.tick()
    assert len(studio.tasks.list(parent['task_id'])['tasks'][0]['attempts']) == 1
    assert studio.tasks.list(child['task_id'])['tasks'][0]['review_issue']


def test_atomic_apply_rolls_back_then_recovers_and_limit_is_configurable(rig, monkeypatch):
    studio, parent, child, raw, _ = reviewer_ready(rig)
    with studio.registry.transaction():
        c = studio.registry._connect()
        studio.tasks._write(c, {**parent, 'max_rework_rounds': 0}, 'fixture', {})
    studio.tasks.verdicts.submit(raw); studio._state(child['run_id'], 'awaiting_review')
    original = studio.tasks._write
    def fail(c, task, kind, detail):
        if kind == 'review_conclusion': raise OSError('Simulated power loss')
        return original(c, task, kind, detail)
    monkeypatch.setattr(studio.tasks, '_write', fail)
    with pytest.raises(OSError): studio.tasks.tick()
    assert studio.runs(parent['run_id'])['runs'][0]['state'] == 'awaiting_review'
    assert studio.tasks.list(child['task_id'])['tasks'][0]['verdict']['status'] == 'pending'
    monkeypatch.setattr(studio.tasks, '_write', original)
    studio.tasks.tick()
    final = studio.tasks.list(parent['task_id'])['tasks'][0]
    assert final['state'] == 'changes_requested' and len(final['attempts']) == 1
    assert final['review_issue']


def test_inconclusive_never_becomes_fake_pass_or_retry(rig):
    studio, parent, child, raw, _ = reviewer_ready(rig)
    raw.update(outcome='inconclusive', source_files=[], checks=[{'criterion': 'External service', 'status': 'unknown', 'evidence': 'Unavailable'}])
    studio.tasks.verdicts.submit(raw); studio._state(child['run_id'], 'awaiting_review')
    studio.tasks.tick(); studio.tasks.tick()
    current = studio.tasks.list(parent['task_id'])['tasks'][0]
    assert current['state'] == 'waiting_review' and current['review_issue']
    assert len(current['attempts']) == 1


def test_mcp_uses_managed_identity(monkeypatch):
    from tools import ap_vibe_mcp
    seen = []
    monkeypatch.setenv('AP_VIBE_AGENT_ID', 'actual-agent')
    monkeypatch.setenv('AP_VIBE_RUN_ID', 'actual-run')
    monkeypatch.setattr(ap_vibe_mcp.task_client, 'call', lambda route, body: seen.append((route, body)) or {'ok': True})
    ap_vibe_mcp.invoke('ap_vibe_review_submit', {'request_id': 'same', 'outcome': 'inconclusive', 'note': 'Unknown',
        'report_path': 'acceptance.md', 'source_files': [], 'checks': []})
    assert seen[0][0] == 'studio/tasks/verdict'
    assert seen[0][1]['agent_id'] == 'actual-agent' and seen[0][1]['run_id'] == 'actual-run'


def test_stopped_reviewer_recovers_once_without_replaying_author_or_old_request(rig):
    studio, parent, child, raw, _ = reviewer_ready(rig)
    old = studio.runs(child['run_id'])['runs'][0]
    studio._state(child['run_id'], 'uncertain', exit_code=1, error='Upstream disconnected')
    studio._threads.pop(child['run_id'], None)
    studio.tasks.tick()
    recovered = studio.tasks.list(child['task_id'])['tasks'][0]
    assert recovered['state'] == 'running' and recovered['review_retry_count'] == 1
    assert recovered['run_id'] != child['run_id'] and recovered['review_source_run_id'] == parent['run_id']
    new = studio.runs(recovered['run_id'])['runs'][0]
    assert new['workspace'] != old['workspace'] and recovered['dispatch_request_id'] != child['dispatch_request_id']
    assert len(studio.tasks.list(parent['task_id'])['tasks'][0]['attempts']) == 1
    studio._state(recovered['run_id'], 'uncertain', exit_code=1, error='Again')
    studio._threads.pop(recovered['run_id'], None)
    studio.tasks.tick(); studio.tasks.tick()
    final = studio.tasks.list(child['task_id'])['tasks'][0]
    assert final['state'] == 'needs_help' and len(final['attempts']) == 2 and final['review_issue']


@pytest.mark.parametrize('case', ['still_owned', 'no_exit_proof', 'disabled', 'submitted_verdict'])
def test_review_recovery_requires_ended_process_and_respects_config(rig, case):
    studio, parent, child, raw, _ = reviewer_ready(rig)
    if case == 'submitted_verdict': studio.tasks.verdicts.submit(raw)
    if case == 'disabled':
        with studio.registry.transaction():
            c = studio.registry._connect()
            studio.tasks._write(c, {**parent, 'max_review_retries': 0}, 'fixture', {})
    studio._state(child['run_id'], 'uncertain', **({} if case == 'no_exit_proof' else {'exit_code': 1}))
    if case != 'still_owned': studio._threads.pop(child['run_id'], None)
    studio.tasks.tick(); studio.tasks.tick()
    assert len(studio.tasks.list(child['task_id'])['tasks'][0]['attempts']) == 1


@pytest.mark.parametrize('outcome', ['accepted', 'changes_requested', 'inconclusive'])
def test_saved_verdict_recovers_after_final_disconnect_without_repeating_review(rig, outcome):
    studio, parent, child, raw, _ = reviewer_ready(rig)
    raw['outcome'] = outcome
    if outcome != 'changes_requested':
        raw['checks'][0]['status'] = 'passed' if outcome == 'accepted' else 'unknown'
    studio.tasks.verdicts.submit(raw)
    studio._state(child['run_id'], 'uncertain', exit_code=1, error='Final reply lost')
    studio._threads.pop(child['run_id'], None)
    studio.tasks.tick(); studio.tasks.tick()
    final = studio.tasks.list(parent['task_id'])['tasks'][0]
    review = studio.tasks.list(child['task_id'])['tasks'][0]
    assert final['state'] == {'accepted': 'completed', 'changes_requested': 'running', 'inconclusive': 'waiting_review'}[outcome]
    assert len(final['verdict_history']) == 1
    assert review['state'] == 'completed' and len(review['attempts']) == 1
    recovery = review['verdict']['recovered_after_executor_exit']
    assert recovery['state'] == 'uncertain' and recovery['exit_code'] == 1
    assert recovery['provider_request_replayed'] is False
    assert studio.runs(child['run_id'])['runs'][0]['error'] == 'Final reply lost'
    assert studio.tasks.verdicts.submit(raw)['replayed']


@pytest.mark.parametrize('defect', ['live_thread', 'live_process', 'no_exit', 'cancelled', 'interrupted',
    'report_changed', 'source_changed', 'manual_decision', 'new_owner', 'new_epoch'])
def test_saved_verdict_recovery_preserves_execution_and_version_boundaries(rig, defect):
    studio, parent, child, raw, workspace = reviewer_ready(rig)
    studio.tasks.verdicts.submit(raw)
    studio._state(child['run_id'], 'uncertain', **({} if defect == 'no_exit' else {'exit_code': 1}))
    studio._threads.pop(child['run_id'], None)
    if defect == 'live_thread': studio._threads[child['run_id']] = object()
    if defect == 'live_process': studio._processes[child['run_id']] = object()
    if defect in {'cancelled', 'interrupted'}: studio._state(child['run_id'], defect)
    if defect == 'report_changed': (workspace / 'acceptance.md').write_text('changed', encoding='utf-8')
    if defect == 'source_changed':
        (Path(studio.runs(parent['run_id'])['runs'][0]['workspace']) / 'guide.md').write_text('changed', encoding='utf-8')
    if defect == 'manual_decision':
        studio.review({'request_id': 'human-after-submit', 'run_id': parent['run_id'], 'reviewer': 'user',
                       'accepted': True, 'note': 'Manual decision wins'})
    if defect in {'new_owner', 'new_epoch'}:
        with studio.registry.transaction():
            c = studio.registry._connect()
            changed = {'owner': parent['owner']} if defect == 'new_owner' else {'assignment_epoch': 99}
            studio.tasks._write(c, {**studio.tasks._read(c, child['task_id']), **changed}, 'fixture', {})
    try:
        studio.tasks.tick(); studio.tasks.tick()
        review = studio.tasks.list(child['task_id'])['tasks'][0]
        assert review['verdict']['status'] == 'pending' and review['review_issue']
        assert len(review['attempts']) == 1
        assert len(studio.tasks.list(parent['task_id'])['tasks'][0]['attempts']) == 1
    finally:
        studio._threads.pop(child['run_id'], None)
        studio._processes.pop(child['run_id'], None)
