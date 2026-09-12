"""Real no-model lifecycle checks on Linux/macOS CI; no Windows simulation claims."""
import json
import os
from pathlib import Path
import socket
import sys
from urllib import request
import pytest

from tools import posix_lifecycle as life

pytestmark=pytest.mark.skipif(os.name=='nt',reason='Requires real POSIX process/lock behavior')


@pytest.fixture
def installation(tmp_path,monkeypatch):
    root=Path(__file__).resolve().parents[1]
    cfg=tmp_path/'config with 空格/config.json'
    frontend=tmp_path/'frontend';frontend.mkdir();(frontend/'index.html').write_text('<html>fixture</html>')
    data=tmp_path/'data with 空格';project=tmp_path/'project';project.mkdir()
    life.atomic(cfg,{'product':'AP-Vibe','product_root':str(root),'python':sys.executable,'project_root':str(project),
        'project_id':'ap-vibe-local','data_dir':str(data),'studio_dir':str(frontend),'host':'127.0.0.1','port':0,
        'auto_monitor':False,'auto_onboard_workspaces':False,'custom_user_field':{'preserved':True},'auto_start':True})
    monkeypatch.setenv('AP_VIBE_CONFIG_PATH',str(cfg))
    yield cfg
    live=life.owned(cfg,life.read(cfg))
    if live:
        result=life.stop(cfg)
        assert result.get('stopped') or not life.owned(cfg,life.read(cfg)),result


def test_live_reuse_port_fallback_secret_and_restart(installation):
    cfg=installation
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1',0));occupied.listen()
        before=life.read(cfg);before['port']=occupied.getsockname()[1];life.atomic(cfg,before)
        first=life.start(cfg)
        assert first['ok'] and first['status']=='running',first
        current=life.read(cfg)
        assert current['port']!=before['port'] and current['custom_user_field']==before['custom_user_field']
        second=life.start(cfg)
        assert second['pid']==first['pid'] and second['replayed']
        alias=cfg.parent.parent/'second config/config.json'
        life.atomic(alias,current)
        reused=life.start(alias)
        assert reused['pid']==first['pid'] and Path(reused['config_path'])==cfg
        # Lost receipt recovery must reuse the same process and data directory.
        life.receipt_path(cfg).unlink()
        recovered=life.start(cfg)
        assert recovered['pid']==first['pid'] and recovered['replayed']
        health=life.http(current,'/v1/health')
        assert health['platform_capabilities']['desktop_launcher'] is False
        agent=life.http(current,'/v1/ap-vibe/agents/save',{'name':'POSIX fixture','model':'grok-fixture',
            'api_key':'fixture-secret-never-sent','base_url':'https://example.invalid/v1','protocol':'openai'})['agent']
        assert agent['key_saved']
        assert 'fixture-secret-never-sent' not in json.dumps(agent)
        assert life.stop(cfg)['stopped']
        restarted=life.start(cfg)
        assert restarted['pid']!=first['pid'] and restarted['ok']
        saved=life.http(life.read(cfg),'/v1/ap-vibe/agents')['agents']
        assert next(a for a in saved if a['agent_id']==agent['agent_id'])['key_saved']
        # No paid task was created merely by installing or saving a profile.
        assert not life.http(life.read(cfg),'/v1/ap-vibe/agents/runs')['runs']


def test_busy_maintenance_never_signals_and_stale_pid_is_ignored(installation,monkeypatch):
    assert life.start(installation)['ok']
    real_http=life.http
    monkeypatch.setattr(life,'http',lambda cfg,path,body=None,**kw: {'acquired':False} if body and body.get('action')=='acquire' else real_http(cfg,path,body,**kw))
    assert life.stop(installation)['status']=='busy'
    assert life.status(installation)['status']=='running'
    monkeypatch.setattr(life,'http',real_http)
    assert life.stop(installation)['stopped']
    life.atomic(life.receipt_path(installation),{'pid':os.getpid(),'fingerprint':'wrong'})
    assert not life.stop(installation)['stopped']


def test_headless_open_and_installer_preserves_settings(installation,monkeypatch):
    monkeypatch.setattr(life,'desktop_available',lambda:False)
    monkeypatch.delenv('DISPLAY',raising=False);monkeypatch.delenv('WAYLAND_DISPLAY',raising=False)
    result=life.install(installation,python=sys.executable,skip_dependencies=True,skip_integrations=True)
    assert result['ok'] and not life.open_frontend(result)['browser_opened']
    assert life.read(installation)['custom_user_field']=={'preserved':True}
    from tools import task_client
    assert task_client._start_daemon(life.read(installation))['status']=='online'
