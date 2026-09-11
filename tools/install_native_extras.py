"""Install documented desktop/native bridges without modifying model settings.

DSH scans <DSH_HOME>/skills. PI Desktop scans ~/.agents/{skills,servers}.
OpenClaw scans <OPENCLAW_STATE_DIR>/skills. Unknown clients can use an exported
generic bridge folder; no fabricated native MCP location is written.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]


def atomic(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    pending=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        pending.write_bytes(data);os.replace(pending,path)
    finally:pending.unlink(missing_ok=True)


def install(kind, home, config_path, *, shared_agents=None, product_root=ROOT):
    home,config_path,product_root=Path(home).resolve(),Path(config_path).resolve(),Path(product_root).resolve()
    config=json.loads(config_path.read_text(encoding='utf-8-sig'))
    py=config.get('python') or sys.executable
    if not home.is_dir():return {'harness':kind,'status':'not_installed','changed':[]}
    root=Path(shared_agents).resolve() if kind=='pi-desktop' and shared_agents else home
    skill=root/'skills/ap-vibe-native-context'
    install_info={'owner':'AP-Vibe','config_path':str(config_path),'product_root':str(product_root),
      'python':py,'harness':None if kind=='pi-desktop' else kind,
      'reference_root':str(product_root/'skills/ap-vibe-task-context/references')}
    planned={skill/'SKILL.md':(product_root/'skills/ap-vibe-native-context/SKILL.md').read_bytes(),
             skill/'references/installation.json':json.dumps(install_info,ensure_ascii=False,indent=2).encode()}
    env={'AP_VIBE_CONFIG_PATH':str(config_path),'AP_VIBE_CLIENT_KIND':kind,'PYTHONIOENCODING':'utf-8'}
    command={'command':py,'args':['-X','utf8',str(product_root/'tools/ap_vibe_mcp.py')],'env':env}
    mcp_status='shell_fallback'
    if kind=='pi-desktop':
        # PI's activation-state key is "mcp"; its native files use "servers".
        planned[root/'servers/ap-vibe.json']=json.dumps({'id':'ap-vibe','label':'AP-Vibe',
          'description':'Shared local projects and public sessions','transport':'stdio',**command},indent=2).encode()
        mcp_status='native_config_written'
    elif kind=='dsh':
        # A plugin overlay is exact install material, not a guessed settings.yaml
        # mutation. DSH supports custom JS YAML tags, so do not reserialize it.
        overlay=[{'id':'mcp-ap-vibe','name':'@deepseek-ai/dsh-mcp-client','config':{
            'serverName':'ap-vibe','transport':'stdio',**command}}]
        planned[skill/'references/mcp-overlay.json']=json.dumps(overlay,indent=2).encode()
        mcp_status='plugin_overlay_available'
    owner_id=hashlib.sha256((kind+':'+str(root)).encode()).hexdigest()[:16]
    state_path=config_path.parent/'integrations'/('native-'+owner_id+'.json')
    state=json.loads(state_path.read_text()) if state_path.exists() else {'files':{}}
    expected={str(p):hashlib.sha256(b).hexdigest() for p,b in planned.items()}
    for p,data in planned.items():
        if p.is_symlink():return {'harness':kind,'status':'user_file_preserved','path':str(p),'changed':[]}
        if p.exists():
            current=hashlib.sha256(p.read_bytes()).hexdigest()
            if current!=expected[str(p)] and current not in state.get('files',{}).get(str(p),[]):
                return {'harness':kind,'status':'user_file_preserved','path':str(p),'changed':[]}
    # Persist both generations before replacing anything. An interrupted install
    # can resume exactly these files; modified user files remain protected.
    previous={str(p):p.read_bytes() if p.exists() else None for p in planned}
    for path,digest in expected.items():
        state['files'][path]=list(set(state['files'].get(path,[])+[digest]))
    atomic(state_path,json.dumps(state,indent=2).encode())
    changed=[]
    for p,data in planned.items():
        old=previous[str(p)]
        if old==data:continue
        if (p.read_bytes() if p.exists() else None)!=old:
            return {'harness':kind,'status':'concurrent_edit_preserved','path':str(p),'changed':changed}
        if old is not None:
            atomic(state_path.parent/'backups'/(uuid.uuid4().hex+'-'+p.name),old)
        atomic(p,data);changed.append(str(p))
    return {'harness':kind,'status':'installed','skill':str(skill),'mcp':mcp_status,'changed':changed}


def defaults():
    user=Path.home()
    return {'dsh':Path(os.environ.get('DSH_HOME') or user/'.dsh'),
      'openclaw':Path(os.environ.get('OPENCLAW_STATE_DIR') or user/'.openclaw'),
      'pi-desktop':Path(os.environ.get('PI_DESKTOP_DATA_DIR') or user/'.pi-desktop')}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,default=Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData/Local')/'AP-Vibe/config.json')
    parser.add_argument('--client',choices=['dsh','pi-desktop','openclaw'])
    parser.add_argument('--home',type=Path)
    parser.add_argument('--agents-home',type=Path)
    args=parser.parse_args()
    if args.home and not args.client:parser.error('--home requires --client')
    results=[]
    for kind,home in defaults().items():
        if args.client and kind!=args.client:continue
        try:
            results.append(install(kind,args.home or home,args.config,
                shared_agents=(args.agents_home or Path.home()/'.agents') if kind=='pi-desktop' else None))
        except (OSError,ValueError,KeyError):
            results.append({'harness':kind,'status':'configuration_unavailable','changed':[]})
    print(json.dumps({'ok':all(r['status'] in {'installed','not_installed'} for r in results),'results':results},ensure_ascii=False,indent=2))
    raise SystemExit(0 if all(r['status'] in {'installed','not_installed'} for r in results) else 1)
