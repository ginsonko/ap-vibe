import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ap_mind.contracts import ContractError
from ap_mind.studio_server import StudioEpisodeService
from ap_mind.teacher_settings import protect, BudgetTransport
from ap_mind.gateway import GatewayTransportError, NullGateway, OpenAICompatibleGateway


def source(root, work, name, stamp=None):
    stamp = stamp or datetime.now(timezone.utc)
    path = root / (name + '.jsonl')
    events = [('session_meta', {'id': name, 'cwd': str(work)}), ('response_item', {
        'type': 'message', 'role': 'user', 'id': name+'-message',
        'content': [{'type': 'input_text', 'text': '请修复这个项目的支付流程，并验证幂等处理。'}]})]
    path.write_text(''.join(json.dumps({'timestamp': stamp.isoformat(), 'type': t, 'payload': p})+'\n' for t,p in events), encoding='utf-8')
    os.utime(path, (stamp.timestamp(),stamp.timestamp()))
    return path


@pytest.fixture
def service(tmp_path):
    project=tmp_path/'project'; project.mkdir()
    sessions=tmp_path/'sessions'; sessions.mkdir()
    other=tmp_path/'other'; other.mkdir()
    source(sessions,other,'recent')
    source(sessions,other,'older',datetime.now(timezone.utc)-timedelta(days=9))
    instance=StudioEpisodeService(tmp_path/'data', project_root=project, codex_project_id='project',
                                  codex_sessions_root=sessions, auto_onboard_workspaces=True)
    instance.poll_codex_sources()
    yield instance
    instance.close()


def complete_sections(name='支付系统'):
    return {
        'identity': {'name': name, 'summary': '持续维护的支付系统，保证订单只扣费一次。'},
        'requirements': {'goals': ['重复请求只产生一笔扣款'], 'redlines': ['不读取真实凭据']},
        'architecture': {'summary': '订单入口校验幂等键，经事务保存交易再返回结果。'},
        'sources': {'documents': [{'path': 'user://payment-contract', 'purpose': '测试中的需求依据'}]},
        'decisions': {'items': [{'new_logic': '按请求键查询已有结果', 'reason': '防止重复扣款'}]},
        'work': {'remaining': ['验证真实支付网关'], 'next_action': '先核对沙箱接口'},
        'risks': {'unknown': ['网关故障后的对账路径未验证']},
        'evidence': {'checks': ['来源是隔离测试会话，不是线上支付证明']},
        'dependencies': {'items': ['支付网关，尚未连接']},
        'status': {'summary': '设计归档，待验证真实网关'},
        'recovery': {'next_action': '先查看幂等要求，再核验订单入口'},
    }


def test_catalog_filters_old_sources_and_freezes_task_scope(service):
    recent=service.organization.catalog()
    assert [s['session_id'] for s in recent['items']]==['recent']
    all_items=service.organization.catalog('all_unclassified')
    assert len(all_items['items'])==2
    payload={'request_id':'prepare-one','scope':'recent_unclassified'}
    prepared=service.organization.prepare(payload)
    assert prepared['task']['total']==1
    assert service.organization.prepare(payload)['replayed']
    with pytest.raises(ContractError,match='request_conflict'):
        service.organization.prepare({**payload,'scope':'all_unclassified'})


def test_multi_session_membership_preserves_history_and_future_routing(service):
    group=service.organization.create_project({'request_id':'group-a','display_name':'支付项目'})['project']['project_id']
    sessions=service.organization.catalog('all_unclassified')['items']
    for item in sessions:
        args={'request_id':'assign-'+item['session_id'],'source_key':item['source_key'],'session_id':item['session_id'],
              'project_id':group,'rationale':'用户核对为同一项目','evidence_refs':['user://test/confirmed'],'actor':'user'}
        assert service.organization.assign(args)['ok']
        assert service.organization.assign(args)['replayed']
    assert service.product_registry.source_count(group)==2
    overview=service.codex_overview(group)
    assert len(overview['sessions'])==2
    assert all(s['message_count'] for s in overview['sessions'])
    assert service.poll_codex_sources()['processed_count']==0
    assert len(service.organization.catalog('all_unclassified')['items'])==0


def test_archive_retains_sources_and_manual_document_authority(service):
    group=service.organization.create_project({'request_id':'group-b','display_name':'档案'})['project']['project_id']
    patch={'project_id':group,'request_id':'user-doc','expected_revision':0,'sections':{'identity':{'name':'档案','summary':'用户填写的用途'}}}
    assert service.organization.update_document(patch)['authority']=='user_edited'
    doc=service.task_context.documents.read(group,['identity'])
    assert doc['catalog'][0]['state']=='user_edited'
    assert doc['project']['documentation_state']=='maintained'
    service.task_context.documents.update(group,'test-agent','receipt',{'request_id':'agent-doc','expected_revision':1,'sections':{'work':{'remaining':['继续验证']}}})
    assert service.task_context.documents.read(group,['identity'])['catalog'][0]['state']=='user_edited'
    archived=service.organization.update_project({'request_id':'archive-one','project_id':group,'status':'archived','reason':'用户移除'})
    assert archived['raw_sessions_retained']
    assert service.task_context.documents.latest(group)['revision']==2
    with pytest.raises(ContractError,match='default_project'):
        service.organization.update_project({'request_id':'bad','project_id':'project','status':'archived','reason':'测试'})


def test_result_scope_cannot_mutate_an_unselected_source(service):
    task=service.organization.prepare({'request_id':'scope','scope':'recent_unclassified'})['task']
    before=service.product_registry.list()
    with pytest.raises(ContractError,match='scope_conflict'):
        service.organization.apply_result(task['task_id'],{'groups':[{'name':'bad','source_keys':['not-selected'],
             'rationale':'bad','evidence_refs':['anything']}], 'skipped':[]})
    assert service.product_registry.list()==before


def test_result_merges_duplicate_existing_project_groups_before_document_writeback(service):
    group = service.organization.create_project({'request_id': 'merge-project', 'display_name': '合并项目'})['project']['project_id']
    task = service.organization.prepare({'request_id': 'merge-task', 'scope': 'all_unclassified'})['task']
    items = task['included']
    result = {
        'groups': [
            {'project_id': group, 'source_keys': [items[0]['source_key']], 'rationale': '同一项目的第一条证据',
             'evidence_refs': ['user://merge/one'], 'expected_revision': 0,
             'sections': complete_sections('合并项目')},
            {'project_id': group, 'source_keys': [items[1]['source_key']], 'rationale': '同一项目的第二条证据',
             'evidence_refs': ['user://merge/two'], 'expected_revision': 0,
             'sections': {'work': {'completed': ['已合并两条会话']}}},
        ],
        'skipped': [],
    }
    service.organization.apply_result(task['task_id'], result)
    assert service.product_registry.source_count(group) == 2
    assert service.task_context.documents.latest(group)['revision'] == 1


def test_prepare_reports_empty_scope_without_creating_a_task(service):
    items = service.organization.catalog('all_unclassified')['items']
    group = service.organization.create_project({'request_id': 'all-group', 'display_name': '全部归类'})['project']['project_id']
    for item in items:
        service.organization.assign({'request_id': 'all-' + item['session_id'], 'source_key': item['source_key'],
            'session_id': item['session_id'], 'project_id': group, 'rationale': '测试归类',
            'evidence_refs': ['user://empty'], 'actor': 'user'})
    assert service.organization.catalog('recent_unclassified')['total'] == 0
    with pytest.raises(ContractError, match='organization_no_candidates'):
        service.organization.prepare({'request_id': 'empty-prepare', 'scope': 'recent_unclassified'})


@pytest.mark.skipif(os.name!='nt',reason='Windows DPAPI contract')
def test_optional_teacher_is_protected_and_zero_calls_on_configuration(service, monkeypatch):
    transport=Mock()
    monkeypatch.setattr('ap_mind.gateway.UrllibModelTransport.request_json',transport)
    settings=service.teacher_settings
    assert not settings.state()['enabled']
    secret='synthetic-test-credential-do-not-use'
    saved=settings.update({'enabled':True,'base_url':'https://example.com/v1','model':'test-model', 'api_key':secret,'calls_per_hour':1})
    assert saved['enabled'] and saved['key_saved']
    assert secret not in json.dumps(saved)
    assert secret.encode() not in settings.path.read_bytes()
    assert json.loads(protect(settings.path.read_bytes(),True))['api_key']==secret
    assert isinstance(service.gateway,OpenAICompatibleGateway)
    assert transport.call_count==0
    settings.update({'enabled':False})
    assert isinstance(service.gateway,NullGateway)
    with pytest.raises(GatewayTransportError,match='disabled'):
        BudgetTransport(settings).request_json('POST','https://example.com')
    assert transport.call_count==0


def test_teacher_rejects_credentials_in_url_without_mutation(service):
    with pytest.raises(ContractError,match='teacher_url_invalid'):
        service.teacher_settings.update({'enabled':True,'base_url':'https://user:pass@example.com/v1?key=secret','model':'test','api_key':'synthetic'})
    assert not service.teacher_settings.state()['enabled']


def test_teacher_unlimited_budget_is_observed_without_fixed_cap(service, monkeypatch):
    transport = Mock(return_value={"ok": True})
    monkeypatch.setattr('ap_mind.gateway.UrllibModelTransport.request_json', transport)
    settings = service.teacher_settings
    settings.value.update({'enabled': True, 'calls_per_hour': None})
    assert BudgetTransport(settings).request_json('POST', 'https://example.com') == {"ok": True}
    assert transport.call_count == 1


def test_teacher_budget_accepts_unlimited_or_explicit_values_without_300_cap(service, monkeypatch):
    monkeypatch.setattr('ap_mind.teacher_settings.protect', lambda raw, decrypt=False: raw)
    settings = service.teacher_settings
    assert settings.update({'enabled': False, 'calls_per_hour': None})['calls_per_hour'] is None
    assert settings.update({'enabled': False, 'calls_per_hour': 1200})['calls_per_hour'] == 1200
