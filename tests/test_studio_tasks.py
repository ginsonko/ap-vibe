"""State-machine checks for logical tasks (runs remain executor attempts)."""
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.studio_server import StudioEpisodeService
from ap_mind.contracts import ContractError


@pytest.fixture
def service(tmp_path):
    root=tmp_path/'project'; root.mkdir()
    s=StudioEpisodeService(tmp_path/'data', project_root=root, codex_project_id='test-project')
    yield s
    s.close()


def profile(s, name='worker'):
    return s.agent_studio.save({'name':name,'base_url':'https://example.invalid/v1','api_key':'fixture-key','model':'m'})['agent']


def test_claim_is_atomic_and_resources_conflict(service, monkeypatch):
    a=profile(service); b=profile(service,'second')
    t=service.agent_studio.tasks.save({'request_id':'task-save','project_id':'test-project','title':'接口任务','goal':'完成接口','acceptance':'读取结果并检查','resources':['api'],'eligible_agents':[a['agent_id'],b['agent_id']]})['task']
    claimed=service.agent_studio.tasks.claim({'request_id':'claim-a','task_id':t['task_id'],'agent_id':a['agent_id'],'expected_version':t['version']})['task']
    with pytest.raises(ContractError,match='already_owned'):
        service.agent_studio.tasks.claim({'request_id':'claim-b','task_id':t['task_id'],'agent_id':b['agent_id'],'expected_version':claimed['version']})
    assert service.agent_studio.tasks.list(t['task_id'])['tasks'][0]['assignment_epoch']==1


def test_cycle_and_cross_project_edges_rejected(service):
    t=service.agent_studio.tasks.save({'request_id':'first','project_id':'test-project','title':'一','goal':'一','acceptance':'一'})['task']
    with pytest.raises(ContractError,match='dependency_cycle'):
        service.agent_studio.tasks.save({'request_id':'cycle','task_id':t['task_id'],'expected_version':t['version'],'project_id':'test-project','title':'一','goal':'一','acceptance':'一','dependencies':[t['task_id']]})


def test_auto_run_tick_dispatches_and_maps_review(service, monkeypatch):
    a=profile(service)
    started=[]
    def fake_start(payload):
        started.append(payload)
        return {'ok':True,'run_id':'run-logical-1','state':'starting'}
    monkeypatch.setattr(service.agent_studio,'start',fake_start)
    t=service.agent_studio.tasks.save({'request_id':'auto-save','project_id':'test-project','title':'自动任务','goal':'自动完成','acceptance':'结果可读','eligible_agents':[a['agent_id']],'auto_run':True})['task']
    service.agent_studio.tasks.tick()
    current=service.agent_studio.tasks.list(t['task_id'])['tasks'][0]
    assert started and current['state']=='running' and current['run_id']=='run-logical-1'
    assert started[0]['logical_task_id']==t['task_id'] and started[0]['assignment_epoch']==1


def test_release_preserves_original_run_pointer(service, monkeypatch):
    a=profile(service)
    t=service.agent_studio.tasks.save({'request_id':'release-save','project_id':'test-project','title':'接手','goal':'接手','acceptance':'检查','eligible_agents':[a['agent_id']]})['task']
    claimed=service.agent_studio.tasks.claim({'request_id':'release-claim','task_id':t['task_id'],'agent_id':a['agent_id'],'expected_version':t['version']})['task']
    with service.agent_studio.registry.transaction():
        c=service.agent_studio.registry._connect(); current=service.agent_studio.tasks._read(c,t['task_id']); current.update(state='needs_help',run_id='run-old'); service.agent_studio.tasks._write(c,current,'fixture',{})
    monkeypatch.setattr(service.agent_studio,'runs',lambda run_id:{'runs':[{'state':'failed'}]})
    released=service.agent_studio.tasks.release({'request_id':'release-now','task_id':t['task_id'],'expected_version':claimed['version']+1,'note':'核对旧成果后换人'})['task']
    assert released['state']=='queued' and released['handoff_run_id']=='run-old' and released['owner'] is None

def test_failed_task_auto_handoffs_to_next_candidate(service, monkeypatch):
    first=profile(service,'first'); second=profile(service,'second')
    t=service.agent_studio.tasks.save({'request_id':'handoff-save','project_id':'test-project','title':'失败换人','goal':'保留现场后换人','acceptance':'第二位继续','eligible_agents':[first['agent_id'],second['agent_id']],'auto_run':True})['task']
    claimed=service.agent_studio.tasks.claim({'request_id':'handoff-claim','task_id':t['task_id'],'agent_id':first['agent_id'],'expected_version':t['version']})['task']
    with service.agent_studio.registry.transaction():
        c=service.agent_studio.registry._connect(); current=service.agent_studio.tasks._read(c,t['task_id']); current.update(state='running',run_id='run-failed',attempts=[{'run_id':'run-failed','agent_id':first['agent_id'],'epoch':1}]); service.agent_studio.tasks._write(c,current,'fixture',{})
    monkeypatch.setattr(service.agent_studio,'runs',lambda run_id:{'runs':[{'state':'failed','run_id':run_id,'agent_id':first['agent_id']}]})
    launched=[]
    monkeypatch.setattr(service.agent_studio,'start',lambda payload: launched.append(payload) or {'ok':True,'run_id':'run-handoff-new','state':'starting'})
    service.agent_studio.tasks.tick()
    current=service.agent_studio.tasks.list(t['task_id'])['tasks'][0]
    assert current['owner']==second['agent_id'] and current['run_id']=='run-handoff-new'
    assert launched and launched[0]['handoff_from_run_id']=='run-failed'

