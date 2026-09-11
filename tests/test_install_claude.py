import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import install_claude


@pytest.mark.parametrize('explicit', [False, True])
def test_real_default_and_explicit_config_locations_preserve_settings(tmp_path, monkeypatch, explicit):
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    monkeypatch.delenv('CLAUDE_CONFIG_DIR',raising=False)
    directory=tmp_path/'.claude'
    directory.mkdir()
    if explicit:monkeypatch.setenv('CLAUDE_CONFIG_DIR',str(directory))
    settings={'env':{'ANTHROPIC_BASE_URL':'https://unchanged.invalid'},'permissions':{'allow':['Read']}}
    (directory/'settings.json').write_text(json.dumps(settings),encoding='utf-8')
    native=tmp_path/'.claude.json'; custom=directory/'.claude.json'
    for config in (native,custom):
        config.write_text(json.dumps({'mcpServers':{'other':{'command':'keep-me'}},'theme':'unchanged'}),encoding='utf-8')
    target=custom if explicit else native
    untouched=native if explicit else custom
    previous=untouched.read_bytes()
    monkeypatch.setattr(install_claude,'claude_executable',lambda:'claude')
    def cli(args,**kwargs):
        env=kwargs['env']
        chosen=Path(env['CLAUDE_CONFIG_DIR'])/'.claude.json' if env.get('CLAUDE_CONFIG_DIR') else tmp_path/'.claude.json'
        assert chosen==target
        data=json.loads(chosen.read_text())
        if 'ap-vibe' in data['mcpServers']:return SimpleNamespace(returncode=1)
        data['mcpServers']['ap-vibe']=json.loads(args[-1])
        chosen.write_text(json.dumps(data),encoding='utf-8')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(install_claude.subprocess,'run',cli)
    first=install_claude.install(directory)
    second=install_claude.install(directory)
    assert first['mcp_config']==str(target) and second['mcp_readback']
    assert untouched.read_bytes()==previous
    current=json.loads(target.read_text())
    assert current['theme']=='unchanged' and current['mcpServers']['other']['command']=='keep-me'
    current=json.loads((directory/'settings.json').read_text())
    assert current['env']==settings['env'] and 'Read' in current['permissions']['allow']
    assert len(current['hooks']['SessionStart'])==1
    assert (directory/'skills/ap-vibe-project-context/references/session-continuation.md').is_file()


@pytest.mark.parametrize('owned',[True,False])
def test_version_upgrade_replaces_only_owned_adapter_and_hook(tmp_path,monkeypatch,owned):
    import sys
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path))
    directory=tmp_path/'claude';directory.mkdir()
    monkeypatch.setenv('CLAUDE_CONFIG_DIR',str(directory))
    monkeypatch.setattr(install_claude,'claude_executable',lambda:'fixture-cli')
    monkeypatch.setattr(install_claude.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=1))
    old=tmp_path/'old-version';config=tmp_path/'AP-Vibe/config.json';config.parent.mkdir()
    config.write_text(json.dumps({'product':'AP-Vibe','product_root':str(install_claude.ROOT),'previous_product_root':str(old)}))
    skill=directory/'skills/ap-vibe-project-context';skill.mkdir(parents=True)
    if owned:(skill/'.ap-vibe-owned').write_text('ap-vibe-claude-v1')
    command=f'"{Path(sys.executable).as_posix()}" "{(old/"tools/claude_context_hook.py").as_posix()}"'
    groups=[{'hooks':[{'type':'command','command':command}]},{'hooks':[{'type':'command','command':'user-hook'}]}]
    (directory/'settings.json').write_text(json.dumps({'hooks':{n:groups for n in ['SessionStart','UserPromptSubmit','Stop']}}))
    mcp=directory/'.claude.json'
    original={'mcpServers':{'ap-vibe':{'command':sys.executable,'args':[str(old/'tools/ap_vibe_mcp.py')],'env':{'AP_VIBE_CLIENT_KIND':'claude','EXTRA':'keep'}},'other':{'command':'keep-other'}}}
    mcp.write_text(json.dumps(original))
    if not owned:
        with pytest.raises(ValueError):install_claude.install(directory,config)
        assert json.loads(mcp.read_text())==original
        return
    assert install_claude.install(directory,config)['mcp_readback']
    current=json.loads(mcp.read_text())
    assert current['mcpServers']['ap-vibe']['args']==[str(install_claude.ROOT/'tools/ap_vibe_mcp.py')]
    assert current['mcpServers']['ap-vibe']['env']['EXTRA']=='keep'
    assert current['mcpServers']['other']==original['mcpServers']['other']
    hooks=json.loads((directory/'settings.json').read_text())['hooks']
    for groups in hooks.values():
        commands=[h['command'] for g in groups for h in g['hooks']]
        assert len(commands)==2 and 'user-hook' in commands and command not in commands
