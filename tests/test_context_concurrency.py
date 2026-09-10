"""Overlapping hook retries must recover one committed context receipt."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.contracts import ContractError
from ap_mind.studio_server import StudioEpisodeService


@pytest.mark.parametrize('conflicting', [False, True])
def test_overlapping_bootstrap_retries_keep_one_attributed_receipt(tmp_path, monkeypatch, conflicting):
    root = tmp_path / 'project'; root.mkdir()
    service = StudioEpisodeService(tmp_path / 'data', project_root=root, codex_project_id='project')
    barrier = threading.Barrier(2)
    original = service.agent_brief
    def slow_brief(raw):
        barrier.wait(timeout=15)
        return original(raw)
    monkeypatch.setattr(service, 'agent_brief', slow_brief)
    request = {'request_id': 'retry-same-request', 'cwd': str(root), 'session_id': 'same-session', 'goal': 'read project'}
    second = {**request, 'goal': 'different input'} if conflicting else dict(request)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(service.task_context.bootstrap, raw) for raw in (request, second)]
            results, errors = [], []
            for job in jobs:
                try: results.append(job.result(timeout=25))
                except ContractError as error: errors.append(str(error))
        if conflicting:
            assert errors == ['task_context_request_conflict']
            assert len(results) == 1
        else:
            assert not errors and len(results) == 2
            assert sorted(r['replayed'] for r in results) == [False, True]
            assert {k:v for k,v in results[0].items() if k != 'replayed'} == {k:v for k,v in results[1].items() if k != 'replayed'}
        winner = results[0]
        assert winner['project_id'] == 'project' and winner['session_id'] == 'same-session'
        assert service.task_context.knowledge({'receipt_id': winner['receipt_id'], 'session_id': 'same-session'})['ok']
        monkeypatch.setattr(service, 'agent_brief', original)
        original_request = request if not conflicting or not jobs[0].exception() else second
        assert service.task_context.bootstrap(original_request)['replayed']
    finally:
        service.close()
