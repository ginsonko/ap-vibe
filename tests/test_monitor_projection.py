import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from ap_mind.product import CodexActivityMonitor


def test_monitor_overview_stays_small_but_diagnostics_keep_actual_failures():
    result = {'status': 'partial', 'processed_count': 3, 'pending_assignment_count': 2,
              'projects': [{'project_id': f'p-{i}', 'status': 'partial',
                            'sources': [{'source_key': f's-{i}', 'status': 'failed',
                                         'original_code': 'new_provider_error', 'retryable': True,
                                         'next_action': 'inspect original file',
                                         'batch': {'occurrences': ['large visible activity' * 500] * 10}}]}
                           for i in range(40)]}
    monitor = CodexActivityMonitor(lambda: result)
    monitor.poll_once()
    compact = monitor.state(compact=True)
    assert compact['status'] == 'degraded'
    error = compact['last_error']
    assert error['pending_assignment_count'] == 2
    assert error['source_error_count'] == 40
    assert error['source_errors_truncated']
    assert error['source_error_samples'][0]['original_code'] == 'new_provider_error'
    assert error['source_error_samples'][0]['next_action'] == 'inspect original file'
    assert len(json.dumps(compact)) < 15000
    full = monitor.state()
    assert full['last_result']['projects'][39]['sources'][0]['batch']
    compact['last_result']['source_error_samples'][0]['original_code'] = 'changed by caller'
    assert monitor.state(compact=True)['last_result']['source_error_samples'][0]['original_code'] == 'new_provider_error'


def test_monitor_summary_retains_last_success_and_clears_errors_only_after_recovery():
    results = iter([{'status': 'success', 'processed_count': 1},
                    {'status': 'partial', 'processed_count': 0, 'original_code': 'missing_source'},
                    {'status': 'success', 'processed_count': 0}])
    monitor = CodexActivityMonitor(lambda: next(results))
    monitor.poll_once()
    success_time = monitor.state(compact=True)['last_success_at']
    monitor.poll_once()
    assert monitor.state(compact=True)['last_success_at'] == success_time
    assert monitor.state(compact=True)['last_error']['original_code'] == 'missing_source'
    monitor.poll_once()
    assert monitor.state(compact=True)['last_error'] is None


def test_monitor_exception_and_nested_source_errors_remain_actionable():
    def fail():
        raise OSError('filesystem temporarily unavailable')
    monitor = CodexActivityMonitor(fail)
    monitor.poll_once()
    error = monitor.state(compact=True)['last_error']
    assert error['original_code'] == 'filesystem temporarily unavailable'
    assert error['retryable'] is True
    assert error['next_action']
    summary = monitor._result_summary({'status': 'partial', 'discovery': {'report': {'warnings': None}},
                                      'projects': [{'sources': [{'status': 'failed', 'error': {'code': 'unknown-new-code', 'retryable': False}}]}]})
    assert summary['source_error_samples'][0]['error']['code'] == 'unknown-new-code'
