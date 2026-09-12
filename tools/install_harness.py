"""Install AP-Vibe Skill and native MCP for Hermes, OpenCode, MiMo Code, and ZCode.

Discovers existing client homes only. Never downloads a client, never reads
secret files, and never replaces unrelated model/key/permission/MCP settings.
ROOT defaults to this file's product root so the main task can copy it into tools/.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from ap_mind.platform_paths import config_dir
OWNED = 'ap-vibe-harness-v1'
SKILL_NAME = 'ap-vibe-native-context'
MCP_NAME = 'ap-vibe'
HARNESSES = ('hermes', 'opencode', 'mimocode', 'zcode')


def default_config_path() -> Path:
    return config_dir() / 'config.json'


def custom_config_path(config_path: Path | None) -> Path | None:
    if config_path is None:
        return None
    resolved = config_path.expanduser().resolve()
    return None if resolved == default_config_path().resolve() else resolved


def xdg_config_home() -> Path:
    override = os.environ.get('XDG_CONFIG_HOME', '').strip()
    if override:
        return Path(override)
    return Path.home() / '.config'


def mcp_script(root: Path) -> Path:
    return root / 'tools/ap_vibe_mcp.py'


def mcp_env(harness: str, config_path: Path | None) -> dict[str, str]:
    return {'AP_VIBE_CLIENT_KIND': harness,
            'AP_VIBE_CONFIG_PATH': str((config_path or default_config_path()).resolve()),
            'PYTHONIOENCODING': 'utf-8'}


def stdio_spec(harness: str, root: Path, config_path: Path | None) -> dict:
    return {
        'command': sys.executable,
        'args': [str(mcp_script(root))],
        'env': mcp_env(harness, config_path),
    }


def installation_payload(harness: str | None, root: Path, config_path: Path | None) -> dict:
    resolved = (config_path or default_config_path()).resolve()
    return {
        'config_path': str(resolved),
        'product_root': str(root.resolve()),
        'python': sys.executable,
        'harness': harness,
        'reference_root': str((root / 'skills/ap-vibe-task-context/references').resolve()),
    }


def backup_file(home: Path, path: Path) -> Path | None:
    if not path.is_file():
        return None
    folder = home / '.ap-vibe-backups'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f'{uuid.uuid4().hex}-{path.name}'
    shutil.copy2(path, target)
    return target


def atomic_write(path: Path, data: bytes, prior: bytes | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.read_bytes() if path.exists() else None) != prior:
        raise ValueError(f'{path} 同时被修改，已保留现场，请重新运行安装器合并。')
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load_json_object(path: Path, raw: bytes | None = None) -> dict | None:
    """Return a dict for strict JSON. None means missing. Raise ValueError if unsafe."""
    if not path.is_file():
        return None
    raw = path.read_bytes() if raw is None else raw
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('配置中存在重复 JSON 键；保留原文件。')
            result[key]=value
        return result
    try:
        data = json.loads(raw.decode('utf-8-sig'),object_pairs_hook=unique)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'无法安全解析 {path}；保留原文件并提供配置片段。') from None
    if not isinstance(data, dict):
        raise ValueError(f'{path} 根节点必须是对象')
    return data


def dump_json(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def skill_source(root: Path) -> Path:
    path = root / 'skills/ap-vibe-native-context/SKILL.md'
    if not path.is_file():
        raise ValueError(f'缺少通用 Skill：{path}')
    return path


def install_skill(home_skills: Path, harness: str | None, root: Path, config_path: Path | None) -> dict:
    source = skill_source(root)
    target = home_skills / SKILL_NAME
    owned = target / '.ap-vibe-owned'
    manifest=target/'.ap-vibe-file-hashes.json'
    previous=load_json_object(manifest) or {'files': {}}
    info=installation_payload(harness,root,config_path)
    descriptor=target/'references/installation.json'
    if descriptor.is_file():
        old_info=load_json_object(descriptor) or {}
        if old_info.get('config_path') and Path(old_info['config_path']).resolve()!=Path(info['config_path']).resolve():
            return {'ok':False,'skipped':True,'reason':'同名Skill关联另一套工作台配置，保留原接入。','path':str(target)}
        info['previous_product_roots']=list(dict.fromkeys([
            *old_info.get('previous_product_roots',[]),old_info.get('product_root',str(root))]))
    else:
        info['previous_product_roots']=[str(root.resolve())]
    files={target/'SKILL.md':source.read_bytes(),
           descriptor:dump_json(info),owned:OWNED.encode()}
    references = root / 'skills/ap-vibe-task-context/references'
    if references.is_dir():
        for item in references.rglob('*.md'):
            dest = target / 'references' / item.relative_to(references)
            files[dest]=item.read_bytes()
    snapshots={p:p.read_bytes() if p.is_file() else None for p in files}
    for path,data in files.items():
        current=snapshots[path]
        known=previous['files'].get(str(path.relative_to(target)),[])
        if path.is_symlink() or current is not None and current!=data and hashlib.sha256(current).hexdigest() not in known:
            return {'ok':False,'skipped':True,'reason':'同名Skill不是本安装器管理，或已有人工修改；已保留原文件。','path':str(path)}
    old_manifest=manifest.read_bytes() if manifest.exists() else None
    for path,data in files.items():
        key=str(path.relative_to(target))
        previous['files'][key]=list(dict.fromkeys(previous['files'].get(key,[])+[hashlib.sha256(data).hexdigest()]))
    atomic_write(manifest,dump_json(previous),old_manifest)
    changed=[]
    for path,data in files.items():
        current=snapshots[path]
        if current==data:continue
        if current is not None:backup_file(home_skills.parent,path)
        atomic_write(path,data,current);changed.append(str(path))
    return {'ok': True, 'path': str(target), 'name': SKILL_NAME, 'harness': harness, 'changed':changed}


def _points_at_adapter(value, roots: list[Path]) -> bool:
    try:
        resolved = Path(str(value)).resolve()
    except OSError:
        return False
    return any(resolved == mcp_script(root).resolve() for root in roots)


def previous_roots(skill_dir: Path, root: Path) -> list[Path]:
    roots = [root.resolve()]
    descriptor = skill_dir / 'references/installation.json'
    if descriptor.is_file():
        try:
            previous = json.loads(descriptor.read_text(encoding='utf-8-sig'))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            previous = {}
        product = previous.get('product_root')
        if product:
            roots.append(Path(str(product)).resolve())
        roots.extend(Path(p).resolve() for p in previous.get('previous_product_roots',[]))
    return roots


def mcp_owned(existing: dict, wanted: dict, skill_dir: Path, root: Path, config_path: Path | None) -> bool:
    """True only when the existing ap-vibe entry already points at this product adapter."""
    if not isinstance(existing, dict):
        return False
    roots = previous_roots(skill_dir, root)
    command = existing.get('command')
    args = existing.get('args') or []
    script_ok = False
    if isinstance(command, list):
        script_ok = any(_points_at_adapter(value, roots) for value in command[1:])
    if isinstance(args, list):
        script_ok = script_ok or any(_points_at_adapter(value, roots) for value in args)
    if not script_ok:
        return False
    env = existing.get('env') or existing.get('environment') or {}
    if not isinstance(env, dict):
        return False
    resolved = (config_path or default_config_path()).resolve()
    previous = env.get('AP_VIBE_CONFIG_PATH')
    if previous and Path(str(previous)).resolve() != resolved:
        return False
    wanted_path = (wanted.get('env') or {}).get('AP_VIBE_CONFIG_PATH')
    if wanted_path and previous and Path(str(previous)).resolve() != Path(wanted_path).resolve():
        return False
    return True


def merge_env(existing: dict, wanted: dict) -> dict:
    extra = existing.get('env') or existing.get('environment') or {}
    merged = dict(extra) if isinstance(extra, dict) else {}
    merged.update(wanted.get('env') or {})
    return merged


def fragment_for(harness: str, root: Path, config_path: Path | None) -> dict:
    spec = stdio_spec(harness, root, config_path)
    python, script, env = spec['command'], spec['args'][0], spec['env']
    if harness == 'hermes':
        body = {
            'mcp_servers': {
                MCP_NAME: {'command': python, 'args': [script], 'env': env},
            }
        }
        text = (
            'mcp_servers:\n'
            f'  {MCP_NAME}:\n'
            f'    command: {json.dumps(python)}\n'
            f'    args: [{json.dumps(script)}]\n'
            '    env:\n'
            + ''.join(f'      {k}: {json.dumps(v)}\n' for k, v in env.items())
        )
        return {'format': 'yaml', 'path': 'config.yaml', 'key': 'mcp_servers', 'body': body, 'text': text}
    if harness == 'zcode':
        body = {'mcp': {'servers': {MCP_NAME: {'command': python, 'args': [script], 'env': env}}}}
        return {'format': 'json', 'path': 'cli/config.json', 'key': 'mcp.servers', 'body': body, 'text': json.dumps(body, ensure_ascii=False, indent=2)}
    body = {
        'mcp': {
            MCP_NAME: {
                'type': 'local',
                'command': [python, script],
                'environment': env,
                'enabled': True,
            }
        }
    }
    filename = 'opencode.jsonc' if harness == 'opencode' else 'mimocode.jsonc'
    return {'format': 'json', 'path': filename, 'key': 'mcp', 'body': body, 'text': json.dumps(body, ensure_ascii=False, indent=2)}


def _result(harness: str, home: Path, skill: dict, mcp: dict, extra: dict | None = None) -> dict:
    payload = {
        'ok': bool(skill.get('ok')) and mcp.get('status') in {'installed', 'unchanged', 'skipped_missing'},
        'harness': harness,
        'home': str(home),
        'skill': skill,
        'mcp': mcp,
        'downloaded': False,
        'secrets_disclosed': False,
        'live_client_tested': False,
    }
    if extra:
        payload.update(extra)
    if mcp.get('status') in {'unsafe_config', 'name_conflict'}:
        payload['ok'] = skill.get('ok', False)
        payload['mcp_auto'] = False
    else:
        payload['mcp_auto'] = mcp.get('status') in {'installed', 'unchanged'}
    return payload


# --- Hermes: parse nodes, preserve unrelated original YAML text -----------

def merge_hermes_yaml(text: str, spec: dict, skill_dir: Path, root: Path, config_path: Path | None) -> tuple[str, str]:
    try:
        import yaml
        from yaml.nodes import MappingNode
        from yaml.tokens import AliasToken, AnchorToken
    except ImportError:
        raise ValueError('Hermes 配置自动合并需要 PyYAML；Skill 可用，MCP 可按返回片段配置。') from None
    try:
        if any(isinstance(t,(AliasToken,AnchorToken)) for t in yaml.scan(text)):
            raise ValueError('复杂 YAML 锚点配置保留原文件，请按片段合并 MCP。')
        node=yaml.compose(text)
        def unique(n):
            if isinstance(n,MappingNode):
                names=[key.value for key,_ in n.value]
                if len(names)!=len(set(names)):raise ValueError('YAML 重复键，保留原文件。')
                for _,value in n.value:unique(value)
            elif hasattr(n,'value') and isinstance(n.value,list):
                for value in n.value:unique(value)
        if node is not None and not isinstance(node,MappingNode):
            raise ValueError('YAML 根节点须为对象，保留原文件。')
        if node:unique(node)
        data=yaml.safe_load(text) or {}
    except yaml.YAMLError:
        # YAML exceptions can include secret-bearing lines of the user's file.
        raise ValueError('无法安全解析 YAML；保留原文件并提供 MCP 片段。') from None
    original=copy.deepcopy(data)
    old=data.get('mcp_servers')
    if old is not None and not isinstance(old,dict):
        raise ValueError('mcp_servers 须为对象，保留原配置。')
    bucket=dict(old or {})
    existing=bucket.get(MCP_NAME)
    if existing is not None:
        if not mcp_owned(existing,spec,skill_dir,root,config_path):raise ValueError('name_conflict')
        wanted={**existing,**spec,'env':merge_env(existing,spec)}
        if wanted==existing:return text,'unchanged'
    else:wanted=spec
    bucket[MCP_NAME]=wanted
    expected={**original,'mcp_servers':bucket}
    newline='\r\n' if '\r\n' in text else '\n'
    block=yaml.safe_dump({MCP_NAME:wanted},allow_unicode=True,sort_keys=False,default_flow_style=False)
    block=''.join('  '+line+newline for line in block.splitlines())
    entry=next(((k,v) for k,v in node.value if k.value=='mcp_servers'),None) if node else None
    def line_start(index):return text.rfind('\n',0,index)+1
    if entry is None:
        # Insert before an explicit YAML document end, otherwise append.
        end=next((t.start_mark.index for t in yaml.scan(text) if isinstance(t,yaml.tokens.DocumentEndToken)),len(text))
        before=text[:end]
        updated=before+('' if not before or before.endswith('\n') else newline)+'mcp_servers:'+newline+block+text[end:]
    else:
        key,mapping=entry
        if isinstance(mapping,MappingNode) and mapping.flow_style and mapping.value:
            raise ValueError('内联 mcp_servers 保留原文；请按片段合并。')
        if old is None or old=={}:
            start=line_start(key.start_mark.index)
            end=text.find('\n',mapping.end_mark.index)
            # Block null nodes end at the key line; flow {} does too.
            if mapping.end_mark.column==0 and mapping.end_mark.index>key.start_mark.index:
                end=mapping.end_mark.index
            else:end=len(text) if end<0 else end+1
            updated=text[:start]+'mcp_servers:'+newline+block+text[end:]
        elif existing is None:
            end=line_start(mapping.end_mark.index) if mapping.end_mark.column==0 else mapping.end_mark.index
            prefix=text[:end]
            updated=prefix+('' if prefix.endswith('\n') else newline)+block+text[end:]
        else:
            pairs=mapping.value
            index=next(i for i,(k,_) in enumerate(pairs) if k.value==MCP_NAME)
            start=line_start(pairs[index][0].start_mark.index)
            end=line_start(pairs[index+1][0].start_mark.index) if index+1<len(pairs) else mapping.end_mark.index
            updated=text[:start]+block+text[end:]
    try:actual=yaml.safe_load(updated)
    except yaml.YAMLError:raise ValueError('无法保持 YAML 结构；保留原文件并提供片段。') from None
    if actual!=expected:raise ValueError('YAML 合并校验不一致；保留原文件。')
    return updated,'installed'


def install_hermes(home: Path, root: Path, config_path: Path | None) -> dict:
    home = home.resolve()
    skill = install_skill(home / 'skills', 'hermes', root, config_path)
    spec = stdio_spec('hermes', root, config_path)
    config = home / 'config.yaml'
    fragment = fragment_for('hermes', root, config_path)
    if not skill.get('ok') and skill.get('skipped'):
        mcp = {'status': 'skill_conflict', 'fragment': fragment}
        return _result('hermes', home, skill, mcp)
    prior = config.read_bytes() if config.is_file() else None
    text = prior.decode('utf-8-sig') if prior else ''
    try:
        updated, status = merge_hermes_yaml(text, spec, home / 'skills' / SKILL_NAME, root, config_path)
    except ValueError as exc:
        if str(exc) == 'name_conflict':
            mcp = {'status': 'name_conflict', 'reason': 'AP-Vibe MCP名称已被其它配置使用；未覆盖。', 'fragment': fragment}
            return _result('hermes', home, skill, mcp)
        mcp = {'status': 'unsafe_config', 'reason': str(exc), 'fragment': fragment}
        return _result('hermes', home, skill, mcp)
    if status != 'unchanged':
        backup_file(home, config)
        atomic_write(config, updated.encode('utf-8'), prior)
    mcp = {'status': status, 'path': str(config), 'format': 'yaml', 'key': 'mcp_servers'}
    return _result('hermes', home, skill, mcp, {'existing_settings_preserved': True})


# --- OpenCode / MiMo JSON objects ------------------------------------------

def _local_mcp_entry(spec: dict) -> dict:
    return {
        'type': 'local',
        'command': [spec['command'], *spec['args']],
        'environment': spec['env'],
        'enabled': True,
    }


def _entry_as_spec(entry: dict) -> dict:
    command = entry.get('command')
    if isinstance(command, list) and command:
        return {'command': command[0], 'args': command[1:], 'env': entry.get('environment') or entry.get('env') or {}}
    return {'command': command, 'args': entry.get('args') or [], 'env': entry.get('env') or entry.get('environment') or {}}


def merge_mcp_map(data: dict, spec: dict, skill_dir: Path, root: Path, config_path: Path | None, container: str) -> tuple[dict, str]:
    if container == 'mcp':
        bucket = data.setdefault('mcp', {})
        if not isinstance(bucket, dict):
            raise ValueError('mcp 必须是对象')
        existing = bucket.get(MCP_NAME)
    else:
        mcp = data.setdefault('mcp', {})
        if not isinstance(mcp, dict):
            raise ValueError('mcp 必须是对象')
        bucket = mcp.setdefault('servers', {})
        if not isinstance(bucket, dict):
            raise ValueError('mcp.servers 必须是对象')
        existing = bucket.get(MCP_NAME)
    wanted_entry = _local_mcp_entry(spec) if container == 'mcp' else {
        'command': spec['command'], 'args': spec['args'], 'env': spec['env'],
    }
    if existing is None:
        bucket[MCP_NAME] = wanted_entry
        return data, 'installed'
    if not isinstance(existing, dict):
        raise ValueError('name_conflict')
    current = _entry_as_spec(existing)
    if not mcp_owned(current, spec, skill_dir, root, config_path):
        same = current.get('command') == spec['command'] and current.get('args') == spec['args']
        env = current.get('env') or {}
        if not (same and isinstance(env, dict) and all(env.get(k) == v for k, v in spec['env'].items())):
            raise ValueError('name_conflict')
    merged_env = merge_env(existing, spec)
    if container == 'mcp':
        updated = dict(existing)
        updated.update(wanted_entry)
        if 'enabled' in existing:updated['enabled']=existing['enabled']
        updated['environment'] = merged_env
        updated.pop('env', None)
        if updated == existing:
            return data, 'unchanged'
        bucket[MCP_NAME] = updated
    else:
        updated = dict(existing)
        updated['command'] = spec['command']
        updated['args'] = spec['args']
        updated['env'] = merged_env
        if updated == existing:
            return data, 'unchanged'
        bucket[MCP_NAME] = updated
    return data, 'installed'


def install_json_client(
    harness: str,
    home: Path,
    root: Path,
    config_path: Path | None,
    skills_dir: Path,
    config_candidates: list[Path],
    create_name: str | Path,
    container: str,
) -> dict:
    home = home.resolve()
    skill = install_skill(skills_dir, harness, root, config_path)
    spec = stdio_spec(harness, root, config_path)
    fragment = fragment_for(harness, root, config_path)
    existing_file = next((path for path in config_candidates if path.is_file()), None)
    created = Path(create_name)
    target = existing_file or (created if created.is_absolute() else home.joinpath(*created.parts))
    if not skill.get('ok') and skill.get('skipped'):
        return _result(harness, home, skill, {'status': 'skill_conflict', 'fragment': fragment})
    try:
        prior = target.read_bytes() if target.is_file() else None
        data = load_json_object(target,prior) if prior is not None else {}
    except ValueError as exc:
        mcp = {'status': 'unsafe_config', 'reason': str(exc), 'path': str(target), 'fragment': fragment}
        return _result(harness, home, skill, mcp, {'existing_settings_preserved': True})
    assert data is not None
    preserved_keys = set(data)
    try:
        data, status = merge_mcp_map(data, spec, skills_dir / SKILL_NAME, root, config_path, container)
    except ValueError as exc:
        code = str(exc)
        if code == 'name_conflict':
            mcp = {'status': 'name_conflict', 'reason': 'AP-Vibe MCP名称已被其它配置使用；未覆盖。', 'path': str(target), 'fragment': fragment}
        else:
            mcp = {'status': 'unsafe_config', 'reason': code, 'path': str(target), 'fragment': fragment}
        return _result(harness, home, skill, mcp, {'existing_settings_preserved': True})
    if status != 'unchanged':
        backup_file(home, target)
        atomic_write(target, dump_json(data), prior)
    mcp = {
        'status': status,
        'path': str(target),
        'format': 'json',
        'key': 'mcp' if container == 'mcp' else 'mcp.servers',
        'preserved_keys': sorted(preserved_keys),
    }
    return _result(harness, home, skill, mcp, {'existing_settings_preserved': True})


def install_opencode(home: Path, root: Path, config_path: Path | None) -> dict:
    home = home.resolve()
    candidates = [home / name for name in ('opencode.jsonc', 'opencode.json', 'config.json')]
    return install_json_client('opencode', home, root, config_path, home / 'skills', candidates, 'opencode.jsonc', 'mcp')


def install_mimocode(home: Path, root: Path, config_path: Path | None) -> dict:
    home = home.resolve()
    config_home = home if (home / 'mimocode.jsonc').exists() or (home / 'mimocode.json').exists() or home.name == 'config' else home / 'config'
    if home.name != 'config' and not (home / 'mimocode.jsonc').is_file() and not (home / 'mimocode.json').is_file():
        if (home / 'config').is_dir() or os.environ.get('MIMOCODE_HOME'):
            config_home = home / 'config'
        else:
            config_home = home
    candidates = [config_home / name for name in ('mimocode.jsonc', 'mimocode.json')]
    skills_dir = config_home / 'skills'
    return install_json_client('mimocode', config_home, root, config_path, skills_dir, candidates, 'mimocode.jsonc', 'mcp')


def install_zcode(home: Path, root: Path, config_path: Path | None) -> dict:
    home = home.resolve()
    config = home / 'cli' / 'config.json'
    # ZCode's native MCP map shadows its generic .agents fallback. Creating a
    # native map must not make the user's unrelated fallback servers disappear.
    fallback=home.parent/'.agents/mcp.json'
    if fallback.is_file() and not config.is_file():
        skill=install_skill(home/'skills','zcode',root,config_path)
        return _result('zcode',home,skill,{'status':'unsafe_config',
           'reason':'已有 .agents/mcp.json；为保留其服务，本次不创建会遮蔽它的原生MCP映射。',
           'fragment':fragment_for('zcode',root,config_path)})
    return install_json_client('zcode', home, root, config_path, home / 'skills', [config], Path('cli') / 'config.json', 'servers')


# --- discovery -------------------------------------------------------------

def hermes_home_default() -> Path:
    env = os.environ.get('HERMES_HOME', '').strip()
    if env:
        return Path(env)
    if os.name != 'nt':
        return Path.home() / '.hermes'
    local = os.environ.get('LOCALAPPDATA', '').strip()
    base = Path(local) if local else Path.home() / 'AppData' / 'Local'
    return base / 'hermes'


def opencode_home_default() -> Path:
    env = os.environ.get('OPENCODE_CONFIG_DIR', '').strip()
    if env:
        return Path(env)
    return xdg_config_home() / 'opencode'


def mimocode_home_default() -> Path:
    env = os.environ.get('MIMOCODE_HOME', '').strip()
    if env:
        return Path(env)
    extra = os.environ.get('MIMOCODE_CONFIG_DIR', '').strip()
    if extra:
        return Path(extra)
    return xdg_config_home() / 'mimocode'


def zcode_home_default() -> Path:
    return Path.home() / '.zcode'


def is_discovered(harness: str, home: Path) -> bool:
    if harness in {'opencode','mimocode'} and home.resolve()==DEFAULT_HOMES[harness]().resolve():
        binary='mimo' if harness=='mimocode' else harness
        bundled=Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local/share')/'AP-Vibe/runtimes'/harness/(binary+('.exe' if os.name=='nt' else ''))
        if bundled.is_file() or shutil.which(binary):return True
    if not home.exists() or not home.is_dir():
        return False
    if harness == 'hermes':
        return any((home / name).exists() for name in ('config.yaml', 'skills', 'state.db', 'hermes-agent', '.env'))
    if harness == 'opencode':
        return any((home / name).exists() for name in ('opencode.jsonc', 'opencode.json', 'config.json', 'skills'))
    if harness == 'mimocode':
        return any(
            (home / name).exists()
            for name in ('mimocode.jsonc', 'mimocode.json', 'config', 'skills', 'data')
        ) or (home / 'config' / 'mimocode.jsonc').exists() or (home / 'config' / 'mimocode.json').exists()
    if harness == 'zcode':
        return True
    return False


INSTALLERS = {
    'hermes': install_hermes,
    'opencode': install_opencode,
    'mimocode': install_mimocode,
    'zcode': install_zcode,
}

DEFAULT_HOMES = {
    'hermes': hermes_home_default,
    'opencode': opencode_home_default,
    'mimocode': mimocode_home_default,
    'zcode': zcode_home_default,
}


def install(
    harnesses: list[str] | None = None,
    home: Path | None = None,
    config_path: Path | None = None,
    root: Path | None = None,
    require_discovered: bool = True,
) -> dict:
    root = (root or ROOT).resolve()
    selected = list(harnesses or HARNESSES)
    unknown = [name for name in selected if name not in INSTALLERS]
    if unknown:
        raise ValueError('未知客户端：' + ', '.join(unknown))
    if home is not None and len(selected) != 1:
        raise ValueError('--home 只能与单个 --harness 一起使用')
    results = []
    for name in selected:
        target = (home or DEFAULT_HOMES[name]()).expanduser()
        if require_discovered and not is_discovered(name, target):
            results.append({
                'ok': True,
                'skipped': True,
                'harness': name,
                'home': str(target),
                'reason': '未发现已存在的客户端目录；安装器不会下载或创建客户端。',
                'downloaded': False,
                'mcp': {'status': 'skipped_missing', 'fragment': fragment_for(name, root, config_path)},
            })
            continue
        try:
            results.append(INSTALLERS[name](target, root, config_path))
        except (OSError,ValueError,KeyError):
            results.append({'ok':False,'harness':name,'home':str(target),'reason':'接入未完成，已保留配置；检查目录权限、并发修改或原文件格式。'})
    ok = all(item.get('ok') for item in results)
    return {
        'ok': ok,
        'root': str(root),
        'config_path': str((config_path or default_config_path()).resolve()),
        'downloaded': False,
        'secrets_disclosed': False,
        'live_client_tested': False,
        'results': results,
    }


def main() -> None:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='为已发现的 Hermes/OpenCode/MiMo/ZCode 安装 AP-Vibe Skill 与已核实的 MCP。')
    parser.add_argument('--harness', action='append', choices=list(HARNESSES), help='可重复；默认处理全部已发现客户端')
    parser.add_argument('--home', type=Path, help='单个客户端的自定义目录，需同时指定一个 --harness')
    parser.add_argument('--config', type=Path, help='AP-Vibe 工作台 config.json；默认 %LOCALAPPDATA%/AP-Vibe/config.json')
    parser.add_argument('--if-available', action='store_true', help='未发现客户端时跳过而不是失败（默认即此行为）')
    args = parser.parse_args()
    payload = install(args.harness, args.home, args.config, ROOT, require_discovered=True)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if payload['ok'] else 1)


if __name__ == '__main__':
    main()
