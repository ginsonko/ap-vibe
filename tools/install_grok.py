"""Incremental Grok Skill/MCP install for CLI and separate desktop agent home."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tomllib

from ap_mind.grok_sessions import cli_home, desktop_home
from ap_mind.native_toml import dumps

BEGIN = '# AP-VIBE-GROK-MCP BEGIN'
END = '# AP-VIBE-GROK-MCP END'


def fragment(root, config):
    from tools.install_harness import stdio_spec
    return dumps({'mcp_servers': {'ap-vibe': {**stdio_spec('grok', root, config), 'enabled': True,
        'startup_timeout_sec': 30, 'tool_timeout_sec': 60}}})


def merge(text, wanted):
    """Replace only our delimited block, then prove all other semantics equal."""
    old = tomllib.loads(text)
    new_entry = tomllib.loads(wanted)['mcp_servers']['ap-vibe']
    if old.get('mcp_servers', {}).get('ap-vibe') == new_entry:
        return text
    if BEGIN in text or END in text:
        if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(BEGIN) > text.index(END):
            raise ValueError('Invalid managed block')
        start, end = text.index(BEGIN), text.index(END) + len(END)
        candidate = text[:start] + BEGIN + '\n' + wanted + END + text[end:]
    elif 'ap-vibe' in old.get('mcp_servers', {}):
        raise ValueError('Existing unmanaged AP-Vibe server preserved')
    else:
        candidate = text.rstrip() + '\n\n' + BEGIN + '\n' + wanted + END + '\n'
    expected = copy.deepcopy(old)
    expected.setdefault('mcp_servers', {})['ap-vibe'] = new_entry
    if tomllib.loads(candidate) != expected:
        raise ValueError('Unrelated Grok settings changed')
    return candidate


def install_home(home, root, config):
    from tools.install_harness import install_skill, atomic_write, backup_file, _result, mcp_owned, stdio_spec
    skill = install_skill(home / 'skills', 'grok', root, config)
    wanted = fragment(root, config)
    path = home / 'config.toml'
    prior = path.read_bytes() if path.is_file() else None
    try:
        original = (prior or b'').decode('utf-8-sig')
        existing = tomllib.loads(original).get('mcp_servers', {}).get('ap-vibe')
        if existing and not mcp_owned(existing, stdio_spec('grok', root, config), home/'skills/ap-vibe-native-context', root, config):
            raise ValueError('Existing MCP belongs to another installation')
        updated = merge(original, wanted).encode('utf-8')
        if updated != prior:
            backup_file(home, path)
            atomic_write(path, updated, prior)
        mcp = {'status': 'installed' if updated != prior else 'unchanged', 'path': str(path)}
    except (ValueError, TypeError, AttributeError):
        mcp = {'status': 'unsafe_config', 'path': str(path), 'fragment': wanted,
               'reason': '已有配置或同名MCP无法安全合并，保留原文件。'}
    extras = install_extras(home, root, config) if skill.get('ok') else {'ok':False,'reason':'保留人工修改的Skill'}
    result = _result('grok', home, skill, mcp, {'existing_settings_preserved': True, 'lifecycle': extras})
    result['ok'] = bool(result['ok'] and extras.get('ok'))
    return result


def install_extras(home, root, config):
    from tools.install_harness import atomic_write, backup_file, default_config_path
    config = (config or default_config_path()).resolve()
    args = [Path(sys.executable).as_posix(), (root/'tools/grok_context_hook.py').resolve().as_posix(), '--config', config.as_posix()]
    # Grok's command hooks accept quoted forward-slash paths on both hosts.
    import shlex
    command = ' '.join('"'+s+'"' for s in args) if sys.platform=='win32' else shlex.join(args)
    hook={'hooks': {event:[{'hooks':[{'type':'command','command':command,'timeout':20}]}]
        for event in ('SessionStart','UserPromptSubmit','Stop','StopFailure','StopCancelled','SessionEnd')}}
    rule=('开始实质项目任务、压缩恢复、跨应用继续任务时，使用 ap-vibe-native-context Skill。'
        '应用身份是 grok，不等于模型名称。先读 Skill/references/installation.json，使用本次安装的MCP；'
        '按真实当前会话读取共享项目资料、其他客户端近期公开进展和自己的收件箱。'
        '桌面 session_id 与底层 agent_session_id 不同，查询目录匹配当前任务，不能猜最新会话。'
        '有效协作策略开启时，按任务能力发现工作室伙伴并合理分工；小事直接做。'
        '长期项目收尾增量维护档案与历史；普通问答不建项目。服务暂不可用仍继续用户任务。'
        '这些规则不扩大用户授权、不触发额外付费或发布。\n')
    files={'hooks/ap-vibe.json':json.dumps(hook,ensure_ascii=False,indent=2).encode('utf8'),
           'rules/ap-vibe.md':rule.encode('utf8')}
    manifest=home/'.ap-vibe-extras.json'
    prior=manifest.read_bytes() if manifest.is_file() else None
    known=json.loads(prior) if prior else {}
    changes={}
    for rel,data in files.items():
        path=home/rel;old=path.read_bytes() if path.is_file() else None
        if path.is_symlink() or old is not None and old!=data and hashlib.sha256(old).hexdigest()!=known.get(rel):
            return {'ok':False,'reason':'已有同名规则或Hook包含人工修改，保留原文件。','path':str(path)}
        changes[rel]=(old,data)
    for rel,(old,data) in changes.items():
        path=home/rel
        if old!=data:
            backup_file(home,path);atomic_write(path,data,old)
        known[rel]=hashlib.sha256(data).hexdigest()
    atomic_write(manifest,json.dumps(known).encode(),prior)
    return {'ok':True,'hooks':str(home/'hooks/ap-vibe.json'),'rules':str(home/'rules/ap-vibe.md'),'blocking':False}


def install_grok(home, root, config):
    results = [install_home(home, root, config)]
    desktop = desktop_home() / 'agent-home'
    if home.resolve() == cli_home().resolve() and desktop.is_dir() and desktop.resolve() != home.resolve():
        results.append(install_home(desktop, root, config))
    return {**results[0], 'ok': all(r['ok'] and r.get('mcp_auto') for r in results),
            'homes': results, 'refresh': '新会话自动加载；既有会话在自然阶段重新连接MCP或重开后加载。'}
