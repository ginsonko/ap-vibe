"""Metric provenance and recovery; synthetic records are not model evidence."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.contracts import ContractError, utc_now
from ap_mind.studio_server import StudioEpisodeService
from ap_mind.studio_metrics import observation, snapshot, summarize


@pytest.fixture
def studio(tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    service = StudioEpisodeService(tmp_path / 'data', project_root=root, codex_project_id='test-project')
    yield service.agent_studio
    service.close()


def profile(studio):
    return studio.save({'name': 'Worker', 'model': 'arbitrary', 'api_key': 'fixture-secret',
                        'base_url': 'https://example.invalid/v1'})['agent']


def run(**extra):
    return {'run_id': 'r1', 'agent_id': 'author', 'name': 'Author', 'profile_revision': 1, 'state': 'awaiting_review',
            'created_at': utc_now(), 'prompt': 'A task', **extra}


def verdict(source='r1', outcome='accepted'):
    return {'source_run_id': source, 'reviewer_agent_id': 'reviewer', 'outcome': outcome,
            'report': {'sha256': 'report-sha', 'name': 'check.md'},
            'source_files': [{'sha256': 'artifact-sha', 'name': 'result.json'}],
            'checks': [{'criterion': 'Actual output', 'status': 'passed' if outcome == 'accepted' else 'failed', 'evidence': 'result.json'}]}


def store(studio, value):
    with studio.registry.transaction():
        studio.registry._connect().execute('INSERT INTO studio_runs VALUES (?,?,?,?,?,?)',
            (value['run_id'], value['run_id'], 'fixture', value['agent_id'], value['state'], json.dumps(value)))


def test_independent_verdict_and_report_application_are_not_confused():
    review = {'accepted': True, 'reviewer': 'reviewer', 'note': 'Checked', 'verdict': verdict()}
    author = observation(run(review=review), {})
    reviewer = observation(run(run_id='review-run', agent_id='reviewer', review=review), {'review_of_task_id': 'task'})
    assert author['outcome'] == 'independent_accepted'
    assert reviewer['outcome'] == 'review_applied'
    stats = summarize([author, reviewer])
    assert stats['independent_accepted'] == 1 and stats['review_attempts'] == 1
    bad = observation(run(review={**review, 'verdict': verdict(source='another')}), {})
    assert bad['outcome'] == 'recorded_accepted' and not bad['source_files']
    self_review = observation(run(review={'accepted': True, 'reviewer': 'author'}), {})
    assert self_review['outcome'] == 'self_reported'


def test_unknown_cost_and_latency_do_not_turn_into_zero_or_include_review_delay():
    records = [observation(run(run_id=str(i), result={'total_cost_usd': value}), {}) for i, value in enumerate([None, -1, float('nan'), True, 0, 2.5])]
    stats = summarize(records)
    assert stats['estimated_cost_usd'] == 2.5 and stats['cost_known_samples'] == 2 and stats['cost_unknown_samples'] == 4
    assert stats['independent_pass_rate'] is None and stats['median_duration_ms'] is None
    value = run(execution_started_at='2026-09-01T00:00:00Z', execution_finished_at='2026-09-01T00:00:02Z', updated_at='2026-09-09T00:00:00Z')
    assert observation(value, {})['duration_ms'] == 2000
    assert observation(run(updated_at='2026-09-09T00:00:00Z'), {})['duration_ms'] is None


def test_rework_history_is_retained_without_counting_multiple_successes():
    review = {'accepted': True, 'reviewer': 'reviewer', 'verdict': verdict()}
    item = observation(run(logical_task_id='task', review=review, review_history=[{'accepted': False}, review]), {})
    assert summarize([item])['independent_accepted'] == 1
    assert summarize([item])['changes_requested_records'] == 1
    item2 = observation(run(run_id='r2', logical_task_id='task'), {})
    assert summarize([item, item2])['logical_tasks'] == 1


def test_filters_paging_versions_archives_and_no_secret(studio):
    agent = profile(studio)
    for i in range(85):
        store(studio, run(run_id=f'r{i}', agent_id=agent['agent_id'], project_id='test-project',
                         task_snapshot={'title': f'Task{i}', 'tags': ['code']}, result={'total_cost_usd': .1}))
    current = snapshot(studio, configuration='current', limit=50)
    assert current['total'] == 85 and current['next_offset'] == 50
    assert current['samples'][0]['title'] == 'Task84'
    assert len(snapshot(studio, offset=50, limit=50)['samples']) == 35
    saved = studio.save({**agent, 'expected_revision': 1, 'name': 'Renamed', 'api_key': ''})['agent']
    assert snapshot(studio, configuration='current')['total'] == 85
    assert snapshot(studio)['summary']['attempts'] == 85
    studio.archive({'agent_id': agent['agent_id'], 'expected_revision': saved['revision']})
    data = snapshot(studio, tag='code', days=7)
    assert data['summary']['cost_known_samples'] == 85 and data['agents'][0]['archived']
    assert 'fixture-secret' not in json.dumps(data) and 'example.invalid' not in json.dumps(data)
    assert snapshot(studio, tag='missing')['total'] == 0
    assert snapshot(studio, project_id='other')['total'] == 0
    assert snapshot(studio, agent_id='missing')['agents'] == []
    with pytest.raises(ContractError, match='filter_invalid'):
        snapshot(studio, days=-1)


def test_window_coverage_and_frozen_metadata(studio, monkeypatch):
    from ap_mind import studio_metrics
    monkeypatch.setattr(studio_metrics, 'WINDOW', 2)
    agent = profile(studio)
    for i in range(3):
        store(studio, run(run_id=str(i), agent_id=agent['agent_id']))
    data = snapshot(studio)
    assert data['coverage']['truncated'] and data['coverage']['matched_runs'] == 3
    assert data['summary']['attempts'] == 2
    observed = observation(run(task_snapshot={'title': 'Original', 'tags': ['old']}), {'title': 'Changed', 'tags': ['new']})
    assert observed['title'] == 'Original' and observed['tags'] == ['old']


def test_persisted_metrics_survive_service_reopen(tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    service = StudioEpisodeService(tmp_path / 'data', project_root=root, codex_project_id='test-project')
    agent = profile(service.agent_studio)
    store(service.agent_studio, run(agent_id=agent['agent_id'], result={'total_cost_usd': 1}))
    before = snapshot(service.agent_studio)
    service.close()
    service = StudioEpisodeService(tmp_path / 'data', project_root=root, codex_project_id='test-project')
    try:
        after = snapshot(service.agent_studio)
        assert before['summary'] == after['summary'] and before['samples'] == after['samples']
    finally:
        service.close()
