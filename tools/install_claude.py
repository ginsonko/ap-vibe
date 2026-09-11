"""Install AP-Vibe in a Claude user directory without replacing other settings."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from ap_mind.agent_studio import claude_executable
from ap_mind.mcp_catalog import allowed_tools


def install(directory:Path,config_path:Path|None=None):
    default_config=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'AP-Vibe/config.json'
    if config_path and config_path.resolve()==default_config.resolve():
        config_path=None
    directory=directory.resolve(); directory.mkdir(parents=True,exist_ok=True)
    settings_path=directory/'settings.json'
    prior=settings_path.read_bytes() if settings_path.exists() else None
    settings=json.loads(prior.decode('utf-8-sig')) if prior else {}
    if not isinstance(settings,dict):raise ValueError('Claude settings must be an object')
    skill=directory/'skills/ap-vibe-project-context'
    source=(ROOT/'skills/ap-vibe-claude/SKILL.md').read_text(encoding='utf-8').replace('name: project-context','name: ap-vibe-project-context',1)
    owned=skill/'.ap-vibe-owned'
    if skill.exists() and not owned.exists():raise ValueError('同名Skill已存在且不是本安装器管理，已保留原文件。')
    command=f'"{Path(sys.executable).as_posix()}" "{(ROOT/"tools/claude_context_hook.py").as_posix()}"'
    if config_path:
        # Windows and POSIX hook launchers both accept quoted forward-slash paths.
        command+=f' --config "{config_path.resolve().as_posix()}"'
    hooks=settings.setdefault('hooks',{})
    for name in ['SessionStart','UserPromptSubmit','Stop']:
        groups=hooks.setdefault(name,[])
        if not any(any(h.get('command')==command for h in group.get('hooks',[])) for group in groups):
            groups.append({'hooks':[{'type':'command','command':command,'timeout':20}]})
    allow=settings.setdefault('permissions',{}).setdefault('allow',[])
    for rule in allowed_tools():
        if rule not in allow:allow.append(rule)
    rule='Skill(ap-vibe-project-context)'
    if rule not in allow:allow.append(rule)
    exe=claude_executable()
    if not exe:raise ValueError('未找到Claude Code执行器')
    mcp={'command':sys.executable,'args':[str(ROOT/'tools/ap_vibe_mcp.py')], 'env':{'AP_VIBE_CLIENT_KIND':'claude'}}
    if config_path:mcp['env']['AP_VIBE_CONFIG_PATH']=str(config_path.resolve())
    env=dict(os.environ)
    native_default = directory == (Path.home()/'.claude').resolve() and not env.get('CLAUDE_CONFIG_DIR')
    if not native_default:
        env['CLAUDE_CONFIG_DIR']=str(directory)
    # Native Claude reads ~/.claude.json. Explicit CLAUDE_CONFIG_DIR changes
    # this to <directory>/.claude.json, even when directory is ~/.claude.
    config_file=Path.home()/'.claude.json' if native_default else directory/'.claude.json'
    added=subprocess.run([exe,'mcp','add-json','--scope','user','ap-vibe',json.dumps(mcp)],env=env,capture_output=True,timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if added.returncode:
        # CLI refuses duplicate names. Read its config as data without printing
        # credentials; reuse only the exact adapter command/config destination.
        actual=json.loads(config_file.read_text(encoding='utf-8')).get('mcpServers',{}).get('ap-vibe') if config_file.exists() else None
        if not actual or actual.get('command')!=mcp['command'] or actual.get('args')!=mcp['args']:
            raise ValueError('AP-Vibe MCP名称已被其它配置使用或注册失败；未覆盖现有连接。')
        old_env=actual.get('env',{})
        if old_env.get('AP_VIBE_CONFIG_PATH')!=mcp['env'].get('AP_VIBE_CONFIG_PATH'):
            raise ValueError('现有MCP连接另一个工作台；请保留原配置或指定相同config。')
        if old_env.get('AP_VIBE_CLIENT_KIND')!='claude':
            if not owned.exists():raise ValueError('现有MCP未被本安装器管理，未改动。')
            original=config_file.read_bytes();config=json.loads(original.decode('utf-8'))
            config['mcpServers']['ap-vibe']={**actual,'env':{**old_env,**mcp['env']}}
            backup=directory/'.ap-vibe-backups';backup.mkdir(exist_ok=True)
            (backup/(uuid.uuid4().hex+'-mcp.json')).write_bytes(original)
            temp=config_file.with_suffix('.'+uuid.uuid4().hex+'.tmp')
            temp.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
            if config_file.read_bytes()!=original:raise ValueError('MCP配置同时变化，保留现场后重试。')
            os.replace(temp,config_file)
    actual=json.loads(config_file.read_text(encoding='utf-8')).get('mcpServers',{}).get('ap-vibe') if config_file.exists() else None
    if actual != mcp:
        # Extra user env is compatible; the adapter and required identity must match.
        if not actual or actual.get('command')!=mcp['command'] or actual.get('args')!=mcp['args'] or any(
                actual.get('env',{}).get(k)!=v for k,v in mcp['env'].items()):
            raise ValueError('Claude MCP注册未在实际用户配置中回读成功；保留现场以便修复。')
    if (settings_path.read_bytes() if settings_path.exists() else None)!=prior:
        raise ValueError('Claude设置同时被修改，已保留现场，请重新运行安装器合并。')
    if prior:
        backup=directory/'.ap-vibe-backups';backup.mkdir(exist_ok=True)
        (backup/(uuid.uuid4().hex+'-settings.json')).write_bytes(prior)
    skill.mkdir(parents=True,exist_ok=True);(skill/'references').mkdir(exist_ok=True)
    (skill/'SKILL.md').write_text(source,encoding='utf-8');owned.write_text('ap-vibe-claude-v1',encoding='utf-8')
    references=ROOT/'skills/ap-vibe-task-context/references'
    for reference in references.rglob('*.md'):
        target=skill/'references'/reference.relative_to(references)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(reference,target)
    temp=settings_path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(settings,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temp,settings_path)
    return {'ok':True,'claude_dir':str(directory),'mcp_config':str(config_file),'mcp_readback':True,'skill':'ap-vibe-project-context','mcp':'ap-vibe','hooks':['SessionStart','UserPromptSubmit','Stop'],'existing_settings_preserved':True,'next':'新开Claude任务或恢复任务时使用；已在运行的回合不强行重启。'}


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser();parser.add_argument('--claude-dir',type=Path,default=Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home()/'.claude'));parser.add_argument('--config',type=Path);parser.add_argument('--if-available',action='store_true')
    args=parser.parse_args()
    if args.if_available and not claude_executable():
        print(json.dumps({'ok':True,'skipped':True,'reason':'尚未安装Claude Code；Codex接入正常继续。'},ensure_ascii=False))
    else:
        print(json.dumps(install(args.claude_dir,args.config),ensure_ascii=False))
