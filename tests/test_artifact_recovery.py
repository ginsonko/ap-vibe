"""Review existing output after a stopped executor fails, without rerunning it."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.studio_server import StudioEpisodeService
from ap_mind.contracts import ContractError


@pytest.fixture
def studio(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    service = StudioEpisodeService(tmp_path / 'data', project_root=root, codex_project_id='test-project')
    yield service.agent_studio
    service.close()


def failed_run(studio, tmp_path, state='uncertain', **changes):
    root = tmp_path / 'artifacts'
    root.mkdir(exist_ok=True)
    (root / 'result.md').write_text('真实文件内容\n', encoding='utf-8')
    payload = dict(run_id='run-recovery', workspace=str(root), agent_id='agent-fixture',
                   state=state, exit_code=1, error='final response timeout',
                   result={'usage': {'input_tokens': 112}, 'billing_status': 'unknown'}, **changes)
    with studio.registry._connect() as c:
        c.execute('INSERT INTO studio_runs VALUES (?,?,?,?,?,?)',
                  (payload['run_id'], 'once', 'hash', payload['agent_id'], state, json.dumps(payload)))
        c.commit()
    return payload, root


def verdict(**changes):
    return dict(run_id='run-recovery', request_id='review-output-once', accepted=True,
                reviewer='independent-test', note='Read actual output and checked the contract.',
                evidence_refs=['result.md'], expected_revision=0) | changes


@pytest.mark.parametrize('state', ['failed', 'uncertain', 'interrupted'])
def test_stopped_output_is_reviewable_and_original_failure_persists(studio, tmp_path, state):
    run, root = failed_run(studio, tmp_path, state)
    assert studio.runs(run['run_id'])['runs'][0]['artifact_recovery_available']
    accepted = studio.review(verdict())
    assert accepted['state'] == 'completed'
    record = accepted['review']
    assert record['artifact_recovery'] and record['source_execution_state'] == state
    assert record['artifacts'][0]['sha256'] == hashlib.sha256((root / 'result.md').read_bytes()).hexdigest()
    saved = studio.runs(run['run_id'])['runs'][0]
    assert saved['error'] == run['error'] and saved['result'] == run['result']
    assert saved['execution_outcome_before_review'] == state
    assert studio.review(verdict())['replayed']
    assert len(studio.runs(run['run_id'])['runs'][0]['review_history']) == 1
    with pytest.raises(ContractError, match='request_conflict'):
        studio.review(verdict(accepted=False))


@pytest.mark.parametrize('refs', [[], ['missing.md'], ['../outside.md'], ['.env'], ['.ssh/key.txt']])
def test_recovery_requires_real_public_output(studio, tmp_path, refs):
    _, root = failed_run(studio, tmp_path)
    (tmp_path / 'outside.md').write_text('unrelated')
    (root / '.env').write_text('private')
    (root / '.ssh').mkdir()
    (root / '.ssh/key.txt').write_text('private')
    with pytest.raises(ContractError, match='recovery_artifact_required'):
        studio.review(verdict(evidence_refs=refs))
    assert studio.runs('run-recovery')['runs'][0]['state'] == 'uncertain'


@pytest.mark.parametrize('state', ['running', 'cancelled', 'budget_paused'])
def test_live_cancelled_or_budget_paused_run_is_not_revived(studio, tmp_path, state):
    failed_run(studio, tmp_path, state)
    with pytest.raises(ContractError, match='requires_result'):
        studio.review(verdict())


def test_process_exit_is_required_even_with_artifacts(studio, tmp_path):
    run, _ = failed_run(studio, tmp_path)
    studio._threads[run['run_id']] = object()
    try:
        assert not studio.runs(run['run_id'])['runs'][0]['artifact_recovery_available']
        with pytest.raises(ContractError, match='execution_not_stopped'):
            studio.review(verdict())
    finally:
        studio._threads.clear()


@pytest.mark.parametrize('successor', [False, True])
def test_task_changes_only_when_reviewed_attempt_is_still_current(studio, tmp_path, successor):
    task = studio.tasks.save({'request_id':'save-task','project_id':'test-project',
                             'title':'output recovery','goal':'write result','acceptance':'actual file'})['task']
    failed_run(studio, tmp_path, logical_task_id=task['task_id'])
    with studio.registry.transaction():
        c = studio.registry._connect()
        studio.tasks._write(c, task | {'state':'needs_help','run_id':'run-new' if successor else 'run-recovery',
                                      'owner':'new-owner' if successor else 'old-owner'}, 'fixture', {})
    studio.review(verdict())
    after = studio.tasks.list(task['task_id'])['tasks'][0]
    assert after['state'] == ('needs_help' if successor else 'completed')
    assert after['owner'] == ('new-owner' if successor else 'old-owner')


def test_incomplete_existing_output_can_be_sent_for_revision(studio, tmp_path):
    failed_run(studio, tmp_path)
    assert studio.review(verdict(accepted=False))['state'] == 'changes_requested'


def test_capability_discovery_matches_supported_modes(studio):
    features = studio.profiles()['features']
    # Capability discovery is additive; unrelated supported features may grow.
    assert features['buffered_upstream'] is True
    assert features['artifact_recovery_review'] is True
