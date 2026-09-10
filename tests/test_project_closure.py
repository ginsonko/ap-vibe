import json
import threading
from pathlib import Path

import pytest

from test_organization import service, complete_sections
from ap_mind.contracts import ContractError
from ap_mind.project_documents import clean
from ap_mind.organization_runner import prepare_bundle
from ap_mind.studio_server import StudioEpisodeService


def proposal(task, **extra):
    return {'groups': [{'name': '支付系统', 'source_keys': [i['source_key'] for i in task['included']],
        'rationale': '任务中明确要求持续维护订单支付', 'evidence_refs': ['user://payment-contract'],
        'sections': complete_sections(), **extra}], 'skipped': []}


def test_incomplete_draft_never_creates_a_project_and_is_recoverable(service):
    task = service.organization.prepare({'request_id': 'empty-doc', 'scope': 'recent_unclassified'})['task']
    before = service.product_registry.list()
    result = proposal(task, sections={'identity': {'summary': '只有简介'}})
    with pytest.raises(ContractError, match='project_document_incomplete'):
        service.organization.apply_result(task['task_id'], result)
    assert service.product_registry.list() == before
    failed = service.organization.task(task['task_id'])
    assert failed['status'] == 'failed'
    assert failed['result']['draft'] == result


def test_all_groups_roll_back_when_last_document_write_fails(service, monkeypatch):
    task = service.organization.prepare({'request_id': 'atomic', 'scope': 'all_unclassified'})['task']
    result = proposal(task)
    result['groups'][0]['source_keys'] = [task['included'][0]['source_key']]
    result['groups'].append({**result['groups'][0], 'name': '第二个项目',
        'source_keys': [task['included'][1]['source_key']]})
    before_projects = service.product_registry.list()
    before_sources = {i['source_key']: service.product_registry.source(i['source_key']) for i in task['included']}
    update = service.task_context.documents.update
    writes = []
    def fail_second(*args, **kwargs):
        writes.append(args[0])
        if len(writes) == 2:
            raise RuntimeError('injected disk write failure')
        return update(*args, **kwargs)
    monkeypatch.setattr(service.task_context.documents, 'update', fail_second)
    with pytest.raises(RuntimeError, match='injected'):
        service.organization.apply_result(task['task_id'], result)
    assert service.product_registry.list() == before_projects
    assert all(service.product_registry.source(k) == v for k, v in before_sources.items())
    assert all(service.task_context.documents.latest(p) is None for p in writes)
    assert service.product_registry.assignment_history() == []
    assert service.organization.task(task['task_id'])['status'] == 'failed'


def test_success_replay_archive_restore_and_cold_restart_preserve_profile(service, tmp_path):
    task = service.organization.prepare({'request_id': 'persist', 'scope': 'all_unclassified'})['task']
    result = proposal(task)
    applied = service.organization.apply_result(task['task_id'], result)
    pid = applied['result']['completed_projects'][0]
    snapshot = service.task_context.documents.latest(pid)
    assert service.organization.apply_result(task['task_id'], result) == applied
    assert service.task_context.documents.latest(pid) == snapshot
    with pytest.raises(ContractError, match='request_conflict'):
        service.organization.apply_result(task['task_id'], proposal(task, name='改名重放'))
    assert service.organization.task(task['task_id'])['status'] == 'completed'
    service.organization.update_project({'request_id': 'archive', 'project_id': pid, 'status': 'archived', 'reason': '用户归档'})
    assert service.product_registry.source_count(pid) == 2
    assert service.task_context.documents.latest(pid, 1) == snapshot
    service.organization.update_project({'request_id': 'restore', 'project_id': pid, 'status': 'active', 'reason': '用户恢复'})
    assert service.product_registry.get(pid).auto_monitor_enabled
    service.close()
    recovered = StudioEpisodeService(tmp_path/'data', project_root=tmp_path/'project', codex_project_id='project',
        codex_sessions_root=tmp_path/'sessions', auto_onboard_workspaces=True)
    try:
        assert recovered.task_context.documents.latest(pid) == snapshot
        assert recovered.product_registry.source_count(pid) == 2
        profile = next(p for p in recovered.projects()['projects'] if p['project_id'] == pid)
        assert profile['registration_state'] == 'registered'
        assert profile['documentation_complete'] and profile['maintained_section_count'] == 11
        assert recovered.organization.task(task['task_id'])['status'] == 'completed'
    finally:
        recovered.close()


def test_existing_workspace_document_registers_without_requiring_a_new_container(service):
    item = service.organization.catalog()['items'][0]
    pid = item['project_id']
    service.task_context.documents.update(pid, 'task', 'delivery', {'request_id': 'legacy', 'expected_revision': 0,
        'sections': complete_sections()})
    profile = next(p for p in service.projects()['projects'] if p['project_id'] == pid)
    assert profile['source'] == 'machine_auto_workspace'
    assert profile['registration_state'] == 'registered'
    assert service.organization.catalog()['total'] == 0


def test_logic_roots_are_project_scoped_and_bundle_has_only_selected_project(service, tmp_path):
    selected = service.organization.catalog()['items'][0]['project_id']
    root = service.product_registry.get(selected).root_path
    status = service.configure_project_logic({'project_id': selected, 'root_path': root})
    assert status['available'] and status['root'] == root
    with pytest.raises(ContractError, match='outside_project'):
        service.configure_project_logic({'project_id': selected, 'root_path': str(tmp_path/'project')})
    task = service.organization.prepare_logic({'request_id': 'logic-bundle', 'project_id': selected,
        'question': '核对入口与保存的关系'})['task']
    folder = prepare_bundle(service.organization, task['task_id'])
    index = json.loads((folder/'index.json').read_text(encoding='utf-8'))
    assert [p['project_id'] for p in index['projects']] == [selected]
    assert service.resolve_codex_workspace(str(folder)).project_id == 'project'
    assert not service.organization.tasks()['tasks']


def test_logic_analysis_updates_right_project_and_preserves_concurrent_edits(service):
    selected = service.organization.catalog()['items'][0]['project_id']
    docs = service.task_context.documents
    docs.update(selected, 'task', 'receipt', {'request_id': 'initial', 'expected_revision': 0, 'sections': complete_sections()})
    task = service.organization.prepare_logic({'request_id': 'analysis', 'project_id': selected, 'question': '核对保存入口'})['task']
    docs.update(selected, 'other', 'receipt', {'request_id': 'concurrent', 'expected_revision': 1, 'sections': {'work': {'next_action': '另一任务新计划'}}})
    result = {'summary': '静态检查保存入口', 'findings': [], 'evidence_refs': ['src/payment.py:10'],
        'sections': {'architecture': {'summary': '经过幂等校验再写交易'}}}
    applied = service.organization.apply_result(task['task_id'], result)
    assert applied['status'] == 'completed' and applied['result']['document_revision'] == 3
    assert service.organization.apply_result(task['task_id'], result) == applied
    assert docs.latest(selected)['sections']['work']['next_action'] == '另一任务新计划'
    assert docs.latest('project') is None
    conflict = service.organization.prepare_logic({'request_id': 'conflict', 'project_id': selected, 'question': '再次核对'})['task']
    docs.update(selected, 'other', 'receipt', {'request_id': 'changed-same-chapter', 'expected_revision': 3,
        'sections': {'architecture': {'summary': '当前真实的新架构'}}})
    with pytest.raises(ContractError, match='revision_conflict'):
        service.organization.apply_result(conflict['task_id'], {**result, 'sections': {'architecture': {'summary': '基于旧版本的新结论'}}})
    assert docs.latest(selected)['revision'] == 4
    assert service.organization.task(conflict['task_id'])['status'] == 'failed'


def test_local_credential_locations_are_kept_but_values_are_redacted():
    synthetic_token = "s" + "k-syntheticcredential0123456789"
    value = clean({'credential_location': r'C:\private\payment.env', 'api_key_path': r'C:\private\api-key.txt',
        'api_key': 'synthetic-key-value', 'notes': 'token ' + synthetic_token})
    assert value['credential_location'] == r'C:\private\payment.env'
    assert value['api_key_path'] == r'C:\private\api-key.txt'
    assert value['api_key'] == '[REDACTED]'
    assert 'syntheticcredential' not in value['notes']


def test_dispatch_and_progress_do_not_wait_for_cognition_network_lock(service, monkeypatch):
    task = service.organization.prepare_logic({'request_id': 'responsive', 'project_id': 'project', 'question': '查询入口'})['task']
    monkeypatch.setattr('ap_mind.organization_runner.codex_command', lambda: ['unused'])
    launched = threading.Event()
    monkeypatch.setattr('ap_mind.organization_runner.run', lambda *a: launched.set())
    done = threading.Event()
    outcome = []
    def schedule():
        outcome.append(service.organization.dispatch({'task_id': task['task_id']}, 'http://localhost'))
        service.organization.job_state(task['task_id'], 'running', {'phase': '进度仍然及时可见'})
        done.set()
    with service._lock:
        worker = threading.Thread(target=schedule)
        worker.start()
        responsive = done.wait(2)
    worker.join(5)
    assert responsive and launched.is_set()
    assert outcome[0]['task']['status'] == 'running'
    assert service.organization.task(task['task_id'])['result']['phase'] == '进度仍然及时可见'
