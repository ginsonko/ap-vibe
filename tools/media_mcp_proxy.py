"""Project a media session directory without flooding a managed agent's context."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading


def project_session(result, directory, node_keys=()):
    if result.get('isError'):
        return result
    content = result.get('content', [])
    if len(content) != 1 or content[0].get('type') != 'text':
        return result
    try:
        source = json.loads(content[0]['text'])
    except (ValueError, TypeError):
        return result
    if not isinstance(source, dict) or not isinstance(source.get('session'), dict) or not isinstance(source.get('nodes'), list):
        return result
    raw = json.dumps(source, ensure_ascii=False, indent=2).encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    saved = directory / ('session-' + digest + '.json')
    if not saved.exists():
        saved.write_bytes(raw)
    session = source['session']
    value = {
        'projection': 'ap-vibe.media-session-directory.v1',
        'session': {k:session[k] for k in ('id','title','status','plan_revision','user_goal','updated_at') if k in session},
        'counts': source.get('counts'), 'creative_preferences': source.get('creative_preferences'),
        'nodes': [], 'artifacts': source.get('artifacts', []),
        'full_record': {'path': str(saved.resolve()), 'sha256': digest, 'bytes': len(raw)},
        'next_read': 'This is the complete node directory, not the full history. Read only the needed node using get_session(node_keys=[key]). The full_record is for targeted recovery; do not read all history just to confirm session identity.'}
    selected = set(node_keys)
    for node in source['nodes']:
        fields = ('id','node_key','status','version','attempt','active','depends_on','output_refs','error')
        value['nodes'].append(node if node.get('node_key') in selected else {k:node[k] for k in fields if k in node})
    return {**result, 'content': [{'type':'text','text':json.dumps(value,ensure_ascii=False)}]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--node', required=True)
    parser.add_argument('--server', required=True)
    parser.add_argument('--results-dir', type=Path, required=True)
    args = parser.parse_args()
    child = subprocess.Popen([args.node, args.server], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=sys.stderr, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    calls = {}
    lock = threading.Lock()

    def forward():
        try:
            for line in sys.stdin.buffer:
                message = json.loads(line)
                params = message.get('params', {})
                node_keys = []
                if message.get('method') == 'tools/call' and params.get('name') == 'get_session':
                    node_keys = params.get('arguments', {}).pop('node_keys', [])
                    if not isinstance(node_keys, list):
                        node_keys = []
                    node_keys = [key for key in node_keys if isinstance(key, str)]
                if 'id' in message:
                    with lock:
                        calls[message['id']] = (message.get('method'), params.get('name'), node_keys)
                child.stdin.write((json.dumps(message,ensure_ascii=False)+'\n').encode('utf-8'))
                child.stdin.flush()
        finally:
            child.stdin.close()

    threading.Thread(target=forward, daemon=True).start()
    try:
        for line in child.stdout:
            message = json.loads(line)
            with lock:
                method, name, selected = calls.pop(message.get('id'), (None,None,[]))
            result = message.get('result')
            if isinstance(result, dict):
                if method == 'tools/list':
                    for tool in result.get('tools', []):
                        if tool['name'] == 'get_session':
                            tool['description'] += ' AP-Vibe默认返回紧凑目录；不要通读原始历史。node_keys可只展开需要的节点；完整记录仍可按需定位读取。'
                            tool['inputSchema'].setdefault('properties', {})['node_keys'] = {
                                'type':'array','items':{'type':'string'},'description':'Only expand these nodes; omitted means compact directory.'}
                elif method == 'tools/call' and name == 'get_session':
                    try:
                        message['result'] = project_session(result, args.results_dir, selected)
                    except OSError:
                        pass  # Original upstream result remains usable if optional cache fails.
            sys.stdout.buffer.write((json.dumps(message,ensure_ascii=False)+'\n').encode('utf-8'))
            sys.stdout.buffer.flush()
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait()


if __name__ == '__main__':
    main()
