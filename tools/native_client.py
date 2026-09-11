"""Application-neutral shell fallback for the shared AP-Vibe tool API."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def command(installation, arguments, harness=None):
    # argparse.REMAINDER retains the conventional option separator. It belongs
    # to this wrapper, not the downstream task client's subcommand arguments.
    arguments = list(arguments)
    if arguments[:1] == ['--']:
        arguments = arguments[1:]
    config=json.loads(Path(installation).read_text(encoding='utf-8-sig'))
    kind=harness or config.get('harness')
    if not isinstance(kind,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',kind):
        raise ValueError('请指定当前真实应用的 --harness；例如 hermes、opencode、dsh。')
    env={k:v for k,v in os.environ.items() if not k.startswith(('CODEX_','AP_VIBE_'))}
    # Keep an explicit read-only task boundary. Managed run attribution is only
    # reusable when it belongs to this exact installation and harness.
    if os.environ.get('AP_VIBE_READONLY_CURATION'):
        env['AP_VIBE_READONLY_CURATION']=os.environ['AP_VIBE_READONLY_CURATION']
    if (os.environ.get('AP_VIBE_CONFIG_PATH') and
        Path(os.environ['AP_VIBE_CONFIG_PATH']).resolve()==Path(config['config_path']).resolve() and
        os.environ.get('AP_VIBE_CLIENT_KIND')==kind):
        for field in ('AP_VIBE_RUN_ID','AP_VIBE_AGENT_ID','AP_VIBE_SELECTED_PROJECT_ID','AP_VIBE_SESSION_ID'):
            if os.environ.get(field):env[field]=os.environ[field]
    env.update(AP_VIBE_CONFIG_PATH=str(Path(config['config_path']).resolve()),
               AP_VIBE_CLIENT_KIND=kind,PYTHONIOENCODING='utf-8')
    script=Path(config['product_root'])/'tools/task_client.py'
    return [config.get('python') or sys.executable,'-X','utf8',str(script),*arguments],env


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--installation',required=True,type=Path)
    parser.add_argument('--harness')
    parser.add_argument('arguments',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    try:
        argv,env=command(args.installation,args.arguments,args.harness)
        raise SystemExit(subprocess.call(argv,env=env))
    except (OSError,ValueError,KeyError) as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False))
        raise SystemExit(1)
