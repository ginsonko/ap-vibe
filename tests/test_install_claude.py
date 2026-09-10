import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import install_claude


@pytest.mark.parametrize('explicit', [False, True])
def test_real_default_and_explicit_config_locations_preserve_settings(tmp_path, monkeypatch, explicit):
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
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
