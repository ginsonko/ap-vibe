import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.install_native_extras import install
from tools.native_client import command


def test_native_desktop_bridge_idempotency_ownership_and_models(tmp_path):
    config=tmp_path/'ap-vibe/config.json';config.parent.mkdir()
    config.write_text(json.dumps({'python':sys.executable}))
    home=tmp_path/'dsh';home.mkdir();settings=home/'settings.yaml'
    settings.write_text('model: user-selected\ncustom: preserve\n')
    first=install('dsh',home,config)
    assert first['status']=='installed' and first['mcp']=='plugin_overlay_available'
    assert not install('dsh',home,config)['changed']
    skill=Path(first['skill'])/'SKILL.md';skill.write_text('User-owned modifications')
    assert install('dsh',home,config)['status']=='user_file_preserved'
    assert skill.read_text()=='User-owned modifications'
    assert settings.read_text()=='model: user-selected\ncustom: preserve\n'


def test_pi_shared_skill_uses_real_caller_and_native_mcp_layout(tmp_path,monkeypatch):
    config=tmp_path/'config.json';config.write_text(json.dumps({'python':sys.executable}))
    home=tmp_path/'pi';home.mkdir();agents=tmp_path/'shared-agents'
    result=install('pi-desktop',home,config,shared_agents=agents)
    assert result['status']=='installed'
    mcp=json.loads((agents/'servers/ap-vibe.json').read_text())
    assert mcp['env']['AP_VIBE_CLIENT_KIND']=='pi-desktop' and mcp['transport']=='stdio'
    info=Path(result['skill'])/'references/installation.json'
    assert json.loads(info.read_text())['harness'] is None
    monkeypatch.setenv('CODEX_THREAD_ID','wrong-parent')
    monkeypatch.setenv('AP_VIBE_CONFIG_PATH','wrong-installation')
    argv,env=command(info,['sessions'],'dsh')
    assert env['AP_VIBE_CONFIG_PATH']==str(config) and env['AP_VIBE_CLIENT_KIND']=='dsh'
    assert 'CODEX_THREAD_ID' not in env and argv[-1]=='sessions'


def test_absent_client_is_not_installed(tmp_path):
    config=tmp_path/'config.json';config.write_text('{}')
    missing=tmp_path/'no-client'
    assert install('dsh',missing,config)['status']=='not_installed'
    assert not missing.exists()


def test_shell_bridge_accepts_separator_without_changing_downstream_values(tmp_path):
    config=tmp_path/'config.json';config.write_text(json.dumps({'python':sys.executable}))
    home=tmp_path/'dsh';home.mkdir()
    result=install('dsh',home,config)
    info=Path(result['skill'])/'references/installation.json'
    arguments=['bootstrap','--goal','--']
    direct,_=command(info,arguments)
    separated,_=command(info,['--',*arguments])
    assert separated==direct and separated[-1]=='--'
    import subprocess
    script=Path(__file__).resolve().parents[1]/'tools/native_client.py'
    process=subprocess.run([sys.executable,str(script),'--installation',str(info),
                            '--','sessions','--help'],capture_output=True,text=True)
    assert process.returncode==0,process.stderr
    assert '--harness' in process.stdout
