"""Passive Grok lifecycle bridge. It never blocks a prompt or a completed turn."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'src')]
from tools import task_client
from ap_mind.grok_sessions import desktop_home, cli_home, load


def identity(native_id):
    home = cli_home().resolve()
    desktop = desktop_home()
    if home == (desktop/'agent-home').resolve():
        for row in load(desktop/'sessions_index.json', []) or []:
            if isinstance(row, dict) and row.get('agentSessionId') == native_id:
                return row.get('id')
        return None  # The UI may still be registering its new agent session.
    return native_id


def handle(event):
    name = event.get('hook_event_name')
    if not name:
        name = {'session_start':'SessionStart','user_prompt_submit':'UserPromptSubmit',
            'stop':'Stop','stop_failure':'StopFailure','stop_cancelled':'StopCancelled','session_end':'SessionEnd'}.get(event.get('hookEventName'))
    cwd, native = event.get('cwd'), event.get('sessionId') or event.get('session_id')
    if not isinstance(cwd,str) or not isinstance(native,str) or not native:
        return {}
    if name == 'SessionEnd':
        return {}  # Preserve StopFailure/StopCancelled rather than overwriting them.
    session = identity(native)
    if not session:
        return {}
    lifecycle = {'StopFailure':'failure','StopCancelled':'cancelled'}.get(name, name)
    task_client.record_lifecycle({'hook_event_name':lifecycle,'timestamp':event.get('timestamp'),
        'turn_id':event.get('promptId')}, cwd, session, 'grok')
    if name not in {'SessionStart','UserPromptSubmit'}:
        return {}
    # Grok discards allowing UserPromptSubmit stdout. Save a matching receipt;
    # the installed global rule/Skill remains the model-facing entry point.
    result = task_client.call('bootstrap', {'client_kind':'grok','cwd':cwd,'session_id':session,
        'goal':str(event.get('prompt') or 'Grok 当前任务共享项目上下文')[:1000],
        'lifecycle_event':name,'request_id':'grok-hook-'+uuid.uuid4().hex})
    if result.get('ok'):
        task_client.save_receipt(cwd,session,result)
    return {}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True)
    args=parser.parse_args()
    os.environ.update(AP_VIBE_CONFIG_PATH=str(Path(args.config).resolve()), AP_VIBE_CLIENT_KIND='grok')
    try:
        raw=sys.stdin.buffer.read(1024*1024+1)
        if len(raw)>1024*1024:raise ValueError('hook too large')
        event=json.loads(raw)
        if isinstance(event,dict):handle(event)
    except Exception:
        pass  # Optional local context failure never stops the user's Grok task.
    print('{}')


if __name__ == '__main__':
    main()
