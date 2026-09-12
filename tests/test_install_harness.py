"""Isolated tests for install_harness.py. Redirects all homes to tmp_path."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tools import install_harness as harness


def product_tree(tmp_path: Path) -> Path:
    root = tmp_path / 'ap-vibe-public'
    skill = root / 'skills/ap-vibe-native-context'
    refs = root / 'skills/ap-vibe-task-context/references'
    skill.mkdir(parents=True)
    refs.mkdir(parents=True)
    (skill / 'SKILL.md').write_text(
        '---\nname: ap-vibe-native-context\ndescription: Restore AP-Vibe context.\n---\n\n# AP-Vibe\n',
        encoding='utf-8',
    )
    (refs / 'organization.md').write_text('# org\n', encoding='utf-8')
    (refs / 'project-documents.md').write_text('# docs\n', encoding='utf-8')
    (refs / 'agent-collaboration.md').write_text('# collab\n', encoding='utf-8')
    (root / 'tools').mkdir()
    (root / 'tools/ap_vibe_mcp.py').write_text('# mcp adapter placeholder\n', encoding='utf-8')
    return root


def isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, 'home', lambda: tmp_path / 'home')
    (tmp_path / 'home').mkdir()
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg-config'))
    monkeypatch.delenv('HERMES_HOME', raising=False)
    monkeypatch.delenv('OPENCODE_CONFIG_DIR', raising=False)
    monkeypatch.delenv('MIMOCODE_HOME', raising=False)
    monkeypatch.delenv('MIMOCODE_CONFIG_DIR', raising=False)
    monkeypatch.setattr(harness, 'ROOT', product_tree(tmp_path))


def skill_dir(home: Path) -> Path:
    return home / 'skills/ap-vibe-native-context'


def assert_skill(home: Path, expected_harness: str, root: Path, config: Path | None = None) -> None:
    skill = skill_dir(home)
    assert (skill / 'SKILL.md').is_file()
    assert (skill / '.ap-vibe-owned').read_text(encoding='utf-8') == harness.OWNED
    assert (skill / 'references/organization.md').is_file()
    data = json.loads((skill / 'references/installation.json').read_text(encoding='utf-8'))
    assert data['product_root'] == str(root.resolve())
    assert data['python'] == sys.executable
    assert data['harness'] == expected_harness
    assert data['reference_root'] == str((root / 'skills/ap-vibe-task-context/references').resolve())
    expected_config = (config or harness.default_config_path()).resolve()
    assert Path(data['config_path']).resolve() == expected_config
    text = (skill / 'SKILL.md').read_text(encoding='utf-8') + json.dumps(data)
    assert 'sk-live' not in text.lower()
    assert 'api_key' not in text.lower()
    assert 'user-secret' not in text.lower()


def test_missing_clients_are_skipped_without_download(tmp_path, monkeypatch):
    isolate_env(tmp_path, monkeypatch)
    result = harness.install(root=harness.ROOT)
    assert result['ok'] and result['downloaded'] is False and result['secrets_disclosed'] is False
    names = {item['harness']: item for item in result['results']}
    for name in harness.HARNESSES:
        assert names[name]['skipped'] is True
        assert '不会下载' in names[name]['reason']
        assert names[name]['mcp']['fragment']['text']
    assert not (tmp_path / 'local/hermes').exists()
    assert not (tmp_path / 'xdg-config/opencode').exists()


def test_hermes_preserves_yaml_and_is_idempotent(tmp_path, monkeypatch):
    isolate_env(tmp_path, monkeypatch)
    home = tmp_path / 'hermes-home'
    home.mkdir()
    original = (
        'model:\n'
        '  default: "keep-me"\n'
        '  provider: "custom"\n'
        'mcp_servers:\n'
        '  filesystem:\n'
        '    command: npx\n'
        '    args:\n'
        '      - -y\n'
        '      - "@modelcontextprotocol/server-filesystem"\n'
        '    env:\n'
        '      TOKEN: "user-secret-not-copied"\n'
        'permissions:\n'
        '  allow: ["Read"]\n'
    )
    (home / 'config.yaml').write_text(original, encoding='utf-8')
    (home / 'skills').mkdir()
    first = harness.install(['hermes'], home=home, root=harness.ROOT, require_discovered=True)
    second = harness.install(['hermes'], home=home, root=harness.ROOT)
    assert first['ok'] and second['ok']
    text = (home / 'config.yaml').read_text(encoding='utf-8')
    assert 'default: "keep-me"' in text
    assert 'provider: "custom"' in text
    assert 'filesystem:' in text
    assert 'TOKEN: "user-secret-not-copied"' in text
    assert 'allow: ["Read"]' in text
    assert text.count('ap-vibe:') == 1
    assert sys.executable in text
    assert 'ap_vibe_mcp.py' in text
    assert 'AP_VIBE_CLIENT_KIND: hermes' in text
    assert_skill(home, 'hermes', harness.ROOT)
    assert first['results'][0]['mcp']['status'] == 'installed'
    assert second['results'][0]['mcp']['status'] == 'unchanged'
    generated = (skill_dir(home) / 'references/installation.json').read_text(encoding='utf-8')
    assert 'user-secret-not-copied' not in generated
    assert (home / '.ap-vibe-backups').is_dir()


def test_hermes_does_not_overwrite_foreign_ap_vibe_name(tmp_path, monkeypatch):
    isolate_env(tmp_path, monkeypatch)
    home = tmp_path / 'hermes-home'
    home.mkdir()
    (home / 'config.yaml').write_text(
        'mcp_servers:\n  ap-vibe:\n    command: other-tool\n    args:\n      - keep\n',
        encoding='utf-8',
    )
    (home / 'skills').mkdir()
    result = harness.install(['hermes'], home=home, root=harness.ROOT)
    assert result['results'][0]['skill']['ok']
    assert result['results'][0]['mcp']['status'] == 'name_conflict'
    text = (home / 'config.yaml').read_text(encoding='utf-8')
    assert 'command: other-tool' in text
    assert 'ap_vibe_mcp.py' not in text
    assert result['results'][0]['mcp']['fragment']['text'].startswith('mcp_servers:')


def test_opencode_jsonc_roundtrip_and_foreign_skill(tmp_path, monkeypatch):
    isolate_env(tmp_path, monkeypatch)
    home = tmp_path / 'xdg-config' / 'opencode'
    home.mkdir(parents=True)
    payload = {
        'model': 'grok-4.6',
        'provider': {'keep': True},
        'mcp': {
            'other': {'type': 'local', 'command': ['npx', '-y', 'keep-me'], 'environment': {'KEY': 'do-not-copy'}},
        },
        'permission': {'allow': ['read']},
    }
    (home / 'opencode.jsonc').write_text(json.dumps(payload), encoding='utf-8')
    foreign = home / 'skills/ap-vibe-native-context'
    foreign.mkdir(parents=True)
    (foreign / 'SKILL.md').write_text('foreign skill', encoding='utf-8')
    result = harness.install(['opencode'], home=home, root=harness.ROOT)
    item = result['results'][0]
    assert item['skill']['skipped'] is True
    assert '不是本安装器管理' in item['skill']['reason']
    assert (foreign / 'SKILL.md').read_text(encoding='utf-8') == 'foreign skill'
    data = json.loads((home / 'opencode.jsonc').read_text(encoding='utf-8'))
    assert data['model'] == 'grok-4.6'
    assert 'ap-vibe' not in data['mcp']
    assert data['mcp']['other']['environment']['KEY'] == 'do-not-copy'


def test_opencode_mcp_merge_idempotent_and_explicit_config(tmp_path, monkeypatch):
    isolate_env(tmp_path, monkeypatch)
    home = tmp_path / 'xdg-config' / 'opencode'
    home.mkdir(parents=True)
    (home / 'opencode.jsonc').write_text(
        json.dumps({'theme': 'dark', 'mcp': {'docs': {'type': 'remote', 'url': 'https://example.invalid'}}}),
        encoding='utf-8',
    )
    custom = tmp_path / 'workbench' / 'config.json'
    custom.parent.mkdir()
    custom.write_text(json.dumps({'product': 'AP-Vibe'}), encoding='utf-8')
    first = harness.install(['opencode'], home=home, config_path=custom, root=harness.ROOT)
    second = harness.install(['opencode'], home=home, config_path=custom, root=harness.ROOT)
    assert first['ok'] and second['ok']
    data = json.loads((home / 'opencode.jsonc').read_text(encoding='utf-8'))
    assert data['theme'] == 'dark'
    assert data['mcp']['docs']['url'] == 'https://example.invalid'
    entry = data['mcp']['ap-vibe']
    assert entry['type'] == 'local'
    assert entry['command'][0] == sys.executable
    assert entry['command'][1].endswith('ap_vibe_mcp.py')
    assert entry['environment']['AP_VIBE_CLIENT_KIND'] == 'opencode'
    assert entry['environment']['AP_VIBE_CONFIG_PATH'] == str(custom.resolve())
    assert_skill(home, 'opencode', harness.ROOT, custom)
    assert first['results'][0]['mcp']['status'] == 'installed'
    assert second['results'][0]['mcp']['status'] == 'unchanged'


def test_mimocode_home_layout_and_unsafe_jsonc(tmp_path, monkeypatch):
    isolate_env(tmp_path, monkeypatch)
    mimo = tmp_path / 'mimo-home'
    (mimo / 'config').mkdir(parents=True)
    (mimo / 'data').mkdir()
    monkeypatch.setenv('MIMOCODE_HOME', str(mimo))
    config = mimo / 'config' / 'mimocode.jsonc'
    config.write_text(
        json.dumps({
            'model': 'xiaomi/mimo',
            'mcp': {'keep': {'type': 'local', 'command': ['echo'], 'environment': {'SECRET': 'nope'}}},
        }),
        encoding='utf-8',
    )
    first = harness.install(['mimocode'], home=mimo, root=harness.ROOT)
    assert first['ok']
    data = json.loads(config.read_text(encoding='utf-8'))
    assert data['model'] == 'xiaomi/mimo'
    assert data['mcp']['keep']['environment']['SECRET'] == 'nope'
    assert data['mcp']['ap-vibe']['environment']['AP_VIBE_CLIENT_KIND'] == 'mimocode'
    assert_skill(mimo / 'config', 'mimocode', harness.ROOT)
    config.write_text('{ /* comment */ "model": "x",\n', encoding='utf-8')
    second = harness.install(['mimocode'], home=mimo, root=harness.ROOT)
    item = second['results'][0]
    assert item['skill']['ok']
    assert item['mcp']['status'] == 'unsafe_config'
    assert 'fragment' in item['mcp']
    assert config.read_text(encoding='utf-8').startswith('{ /* comment */')
    assert 'nope' not in json.dumps(item['mcp']['fragment'])


def test_zcode_cli_config_servers_and_default_config_path(tmp_path, monkeypatch):
    isolate_env(tmp_path, monkeypatch)
    home = tmp_path / 'home' / '.zcode'
    (home / 'cli').mkdir(parents=True)
    (home / 'skills').mkdir()
    (home / 'cli' / 'config.json').write_text(
        json.dumps({'model': 'glm', 'mcp': {'servers': {'memory': {'command': 'npx', 'args': ['keep']}}}}),
        encoding='utf-8',
    )
    result = harness.install(['zcode'], home=home, root=harness.ROOT)
    assert result['ok']
    data = json.loads((home / 'cli' / 'config.json').read_text(encoding='utf-8'))
    assert data['model'] == 'glm'
    assert data['mcp']['servers']['memory']['args'] == ['keep']
    server = data['mcp']['servers']['ap-vibe']
    assert server['command'] == sys.executable
    assert server['args'][0].endswith('ap_vibe_mcp.py')
    assert server['env']['AP_VIBE_CLIENT_KIND'] == 'zcode'
    assert server['env']['AP_VIBE_CONFIG_PATH'] == str(harness.default_config_path().resolve())
    assert_skill(home, 'zcode', harness.ROOT)
    assert Path(json.loads((skill_dir(home) / 'references/installation.json').read_text())['config_path']) == harness.default_config_path()


def test_generated_files_never_contain_user_secrets(tmp_path, monkeypatch):
    isolate_env(tmp_path, monkeypatch)
    home = tmp_path / 'xdg-config' / 'opencode'
    home.mkdir(parents=True)
    # Synthetic key-shaped value; never a usable credential in the source tree.
    secret = 'sk-' + 'synthetic-fixture-never-a-credential'
    (home / 'opencode.json').write_text(
        json.dumps({'provider': {'apiKey': secret}, 'mcp': {}}),
        encoding='utf-8',
    )
    harness.install(['opencode'], home=home, root=harness.ROOT)
    generated = (skill_dir(home) / 'references/installation.json').read_text(encoding='utf-8')
    skill = (skill_dir(home) / 'SKILL.md').read_text(encoding='utf-8')
    assert secret not in generated and secret not in skill
    data = json.loads((home / 'opencode.json').read_text(encoding='utf-8'))
    assert data['provider']['apiKey'] == secret
    assert data['mcp']['ap-vibe']['environment'].get('apiKey') is None


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))


@pytest.mark.parametrize('original',[
    'mcp_servers:\nmodel: keep\n',
    'mcp_servers: {}\nmodel: keep\n',
    'model: keep\n...\n',
    'model: keep\r\nmcp_servers: {}\r\npermissions:\r\n  keep: true\r\n',
])
def test_yaml_empty_nodes_and_document_end_preserve_siblings(tmp_path,monkeypatch,original):
    import yaml
    isolate_env(tmp_path,monkeypatch)
    home=tmp_path/'hermes';home.mkdir();path=home/'config.yaml';path.write_bytes(original.encode())
    result=harness.install(['hermes'],home=home,root=harness.ROOT)
    assert result['results'][0]['mcp']['status']=='installed',result
    value=yaml.safe_load(path.read_bytes());entry=value.pop('mcp_servers')['ap-vibe']
    old=yaml.safe_load(original);old.pop('mcp_servers',None)
    assert value==old and entry['env']['AP_VIBE_CLIENT_KIND']=='hermes'


@pytest.mark.parametrize('original',[
    'mcp_servers: &shared {}\nother: *shared\n',
    'model: old\nmodel: new\n',
    'mcp_servers: {remote: {url: https://example.invalid}}\n',
    '---\nmodel: first\n---\nmodel: second\n',
    'private: "unterminated-sensitive-fixture\n',
])
def test_yaml_complex_or_invalid_is_preserved_without_content_disclosure(tmp_path,monkeypatch,original):
    isolate_env(tmp_path,monkeypatch)
    home=tmp_path/'hermes';home.mkdir();path=home/'config.yaml';path.write_text(original,encoding='utf-8')
    result=harness.install(['hermes'],home=home,root=harness.ROOT)
    assert result['results'][0]['mcp']['status']=='unsafe_config'
    assert path.read_text(encoding='utf-8')==original
    assert 'unterminated-sensitive-fixture' not in json.dumps(result)


def test_skill_user_edit_and_disabled_mcp_survive_update(tmp_path,monkeypatch):
    isolate_env(tmp_path,monkeypatch)
    home=tmp_path/'oc';home.mkdir();config=home/'opencode.json'
    config.write_text('{}')
    assert harness.install(['opencode'],home=home,root=harness.ROOT)['ok']
    value=json.loads(config.read_text());value['mcp']['ap-vibe']['enabled']=False
    value['mcp']['ap-vibe']['environment']['CUSTOM']='keep'
    config.write_text(json.dumps(value))
    second=harness.install(['opencode'],home=home,root=harness.ROOT)
    assert second['ok'] and second['results'][0]['skill']['changed']==[]
    assert json.loads(config.read_text())==value
    skill=skill_dir(home)/'SKILL.md';skill.write_text('My project guidance',encoding='utf-8')
    third=harness.install(['opencode'],home=home,root=harness.ROOT)
    assert not third['results'][0]['skill']['ok']
    assert skill.read_text()=='My project guidance' and json.loads(config.read_text())==value


def test_atomic_new_file_race_and_duplicate_json_preserved(tmp_path):
    path=tmp_path/'config.json';path.write_text('{"model":"first","model":"second"}')
    with pytest.raises(ValueError):harness.load_json_object(path)
    with pytest.raises(ValueError):harness.atomic_write(path,b'{}',None)
    assert path.read_text()=='{"model":"first","model":"second"}'


def test_different_workbench_does_not_split_skill_and_mcp_identity(tmp_path,monkeypatch):
    isolate_env(tmp_path,monkeypatch)
    home=tmp_path/'oc';home.mkdir();config=home/'opencode.json';config.write_text('{}')
    first=tmp_path/'first/config.json';second=tmp_path/'second/config.json'
    assert harness.install(['opencode'],home=home,config_path=first,root=harness.ROOT)['ok']
    files={str(p):p.read_bytes() for p in home.rglob('*') if p.is_file()}
    result=harness.install(['opencode'],home=home,config_path=second,root=harness.ROOT)
    assert not result['ok']
    assert {str(p):p.read_bytes() for p in home.rglob('*') if p.is_file()}==files


def test_workbuddy_skill_bridge_preserves_native_login_and_settings(tmp_path, monkeypatch):
    isolate_env(tmp_path,monkeypatch)
    home=tmp_path/'workbuddy-cli'; home.mkdir()
    settings=home/'settings.json'; settings.write_text('{"permissions":{"allow":["Read"]},"model":"auto"}')
    before=settings.read_bytes()
    result=harness.install(['workbuddy'],home,root=harness.ROOT)
    assert result['ok'] and result['results'][0]['tool_fallback']
    assert settings.read_bytes()==before
    assert_skill(home,'workbuddy',harness.ROOT)
    again=harness.install(['workbuddy'],home,root=harness.ROOT)
    assert again['results'][0]['skill']['changed']==[]


def test_hermes_executor_respects_same_custom_home_as_monitor(tmp_path, monkeypatch):
    from ap_mind import harness_registry
    home=tmp_path/'custom-hermes'
    python=home/'hermes-agent/venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    python.parent.mkdir(parents=True);python.write_bytes(b'fixture')
    monkeypatch.setenv('HERMES_HOME',str(home))
    monkeypatch.delenv('AP_VIBE_HERMES_EXE',raising=False)
    assert harness_registry.default_roots()['hermes']==[str(home)]
    assert harness_registry.executable('hermes')[:3]==[str(python),'-X','utf8']
