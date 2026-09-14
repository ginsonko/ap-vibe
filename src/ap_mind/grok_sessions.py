"""Grok Desktop snapshots and CLI ACP logs, projected without private payloads.

Desktop IDs identify windows; agent_session_id identifies the underlying CLI.
Only public message content is collected. Native reasoning, raw tool inputs,
chat_history and desktop secrets are deliberately outside this adapter.
"""
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import sys

from .native_json_sessions import GenericAgentFiles
from .product import redact_portable


def desktop_home():
    override = os.environ.get('GROK_DESKTOP_DATA_DIR')
    if override:
        return Path(override).expanduser()
    home = Path.home()
    base = (Path(os.environ.get('APPDATA') or home / 'AppData/Roaming') if os.name == 'nt'
            else home / 'Library/Application Support' if sys.platform == 'darwin'
            else Path(os.environ.get('XDG_CONFIG_HOME') or home / '.config'))
    return base / 'grokapp/grok-app/data'


def cli_home():
    return Path(os.environ.get('GROK_HOME') or Path.home() / '.grok').expanduser()


def load(path, fallback=None):
    try:
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('Grok snapshot exceeds read resource budget')
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return fallback


def child(root, *parts):
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Grok native path escapes source')
    return path


def update(row):
    if not isinstance(row, dict):
        return {}
    params = row.get('params', {})
    return params.get('update', {}) if isinstance(params, dict) else {}


class GrokFiles(GenericAgentFiles):
    def __init__(self, max_cache=1000):
        self.cache = OrderedDict()
        self.metadata = {}
        self.logs = {}
        self.index_cache = {}
        self.max_cache = max_cache

    def paths(self, root):
        """A desktop root owns its native log copies, avoiding duplicate actors."""
        root = Path(root)
        if root.is_file():
            return [root] if root.name in {'messages.json', 'updates.jsonl'} else []
        index = root / 'sessions_index.json'
        if index.is_file():
            try:
                rows = load(index)
                if not isinstance(rows,list):raise ValueError('Invalid Grok index')
                self.index_cache[index] = rows
            except (OSError,ValueError):
                if index not in self.index_cache:raise
                rows = self.index_cache[index]
            if not isinstance(rows, list):
                raise ValueError('Invalid Grok session index')
            summaries = {}
            for p in (root / 'agent-home/sessions').glob('*/*/summary.json'):
                try:
                    summary = load(p, {}) or {}
                    info = summary.get('info', {})
                    if isinstance(info, dict) and info.get('id'):
                        summaries[info['id']] = {**info, 'log': p.parent / 'updates.jsonl',
                                                'model_id': summary.get('current_model_id')}
                except (ValueError, OSError, AttributeError):
                    continue
            paths = []
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                    continue
                try:
                    path = child(root, 'sessions', row['id'], 'messages.json')
                except ValueError:
                    continue
                if not path.is_file():
                    continue
                native = summaries.get(row.get('agentSessionId'), {})
                self.metadata[path] = {**row, 'cwd': native.get('cwd', ''),
                    'model': row.get('modelId') or native.get('model_id'),
                    'model_source': 'grok_desktop_config' if row.get('modelId') else 'native_session_model_id'}
                self.logs[path] = native.get('log')
                paths.append(path)
            return paths
        sessions = root / 'sessions' if (root / 'sessions').is_dir() else root
        return list(sessions.glob('*/*/updates.jsonl'))

    def observation(self, path):
        from .external_sessions import stamp
        from .studio_activity import tool_activity
        from .studio_sessions import timestamp
        path = Path(path)
        result = {'state': 'unknown', 'status_basis': 'grok_no_lifecycle_marker',
            'event_at': None, 'tool': None, 'activity': None, 'first_message': None, 'auxiliary': False}
        native = self.logs.get(path) if path.name == 'messages.json' else path
        if native and native.is_file():
            with native.open('rb') as stream:
                stream.seek(max(0, native.stat().st_size - 512 * 1024))
                if stream.tell():
                    stream.readline()
                for line in stream:
                    if not line.endswith(b'\n'):
                        continue
                    try:
                        row = json.loads(line)
                        u = update(row)
                        kind = u.get('sessionUpdate')
                    except (ValueError, AttributeError):
                        continue
                    state = None
                    if kind in {'user_message_chunk', 'agent_message_chunk', 'tool_call', 'tool_call_update', 'retry_state'}:
                        state = 'running'
                    elif kind == 'turn_completed':
                        reason = u.get('stop_reason')
                        state = 'idle' if reason in {'end_turn', 'EndTurn'} else 'cancelled' if reason == 'cancelled' else 'failed'
                    if kind in {'tool_call', 'tool_call_update'}:
                        tool = ((u.get('_meta') or {}).get('x.ai/tool') or {}).get('name')
                        if tool:
                            result['tool'] = str(tool)[:200]
                            result['activity'] = tool_activity(tool, u.get('rawInput')) or result['activity']
                    if state:
                        result.update(state=state, status_basis='grok_' + str(kind), event_at=stamp(row.get('timestamp')),
                            explicit_failure=kind=='turn_completed' and u.get('stop_reason')=='error', turn_id=u.get('prompt_id'))
        if path.name == 'messages.json':
            try:
                lease = load(path.parent / 'turn_lease.json', {}) or {}
                states = {'active': 'running', 'interrupted': 'interrupted', 'completed': 'idle',
                          'cancelled': 'cancelled', 'failed': 'failed'}
                at = lease.get('updatedAt')
                if lease.get('sessionId') == path.parent.name and lease.get('status') in states and (
                        (timestamp(at) or 0) >= (timestamp(result['event_at']) or 0)):
                    result.update(state=states[lease['status']], status_basis='grok_desktop_turn_lease', event_at=at)
                    result['explicit_failure'] = lease['status']=='failed'
                    result['turn_id'] = lease.get('turnId')
            except (OSError, ValueError, AttributeError):
                pass
        return result

    def snapshot(self, path):
        from .external_sessions import public_text, stamp, native_model
        path = Path(path)
        stat = path.stat()
        meta = self.metadata.get(path, {})
        summary_path = path.parent / 'summary.json'
        summary_stat = summary_path.stat().st_mtime_ns if summary_path.is_file() else 0
        signature = (stat.st_mtime_ns, stat.st_size, summary_stat, json.dumps(meta, sort_keys=True))
        cached = self.cache.get(path)
        if cached and cached['signature'] == signature:
            self.cache.move_to_end(path)
            return cached
        events, models, partial, invalid = [], [], False, 0
        model = native_model(meta)
        try:
            if path.name == 'messages.json':
                rows = load(path)
                if not isinstance(rows, list):
                    raise ValueError('Invalid Grok public message snapshot')
                session = meta.get('id') or path.parent.name
                agent_session = meta.get('agentSessionId')
                cwd = meta.get('cwd', '')
                title = meta.get('title')
                for i, row in enumerate(rows):
                    if not isinstance(row, dict) or row.get('role') not in {'user', 'assistant'}:
                        continue
                    text = public_text(row.get('content'))
                    if text.strip():
                        events.append({'id': str(row.get('id') or i), 'role': row['role'], 'text': text,
                            'timestamp': stamp(row.get('createdAt')), 'model': model,
                            'error': bool(row.get('isError')), 'text_may_be_truncated': len(text) >= 64000})
            else:
                summary = load(summary_path, {}) or {}
                info = summary.get('info', {}) or {}
                session = info.get('id') or path.parent.name
                agent_session = session
                cwd = info.get('cwd', '')
                title = summary.get('generated_title')
                model = native_model({'model': summary.get('current_model_id')})
                # Stream line-by-line: private payloads are never retained in cache.
                if stat.st_size > 256 * 1024 * 1024:
                    raise ValueError('Grok log exceeds read resource budget')
                join = False
                with path.open('rb') as stream:
                    while True:
                        offset = stream.tell()
                        line = stream.readline(4 * 1024 * 1024 + 1)
                        if not line:
                            break
                        if len(line) > 4 * 1024 * 1024:
                            while line and not line.endswith(b'\n'):
                                line = stream.readline(65536)
                            invalid += 1
                            join = False
                            continue
                        if not line.endswith(b'\n'):
                            partial = True
                            break
                        try:
                            row = json.loads(line)
                            u = update(row)
                            kind = u.get('sessionUpdate')
                        except (ValueError, AttributeError):
                            invalid += 1
                            continue
                        if kind not in {'user_message_chunk', 'agent_message_chunk'}:
                            if kind not in {'agent_thought_chunk'}:
                                join = False
                            continue
                        model = native_model(u.get('_meta') or {}) or model
                        content = u.get('content') or {}
                        text = public_text([content])
                        if not text:
                            continue
                        role = 'user' if kind == 'user_message_chunk' else 'assistant'
                        if join and events and events[-1]['role'] == role:
                            full = events[-1]['text'] + text
                            events[-1].update(text=full[:64000], text_may_be_truncated=len(full) >= 64000)
                        else:
                            events.append({'id': str(offset), 'role': role, 'text': text,
                                'timestamp': stamp(row.get('timestamp')), 'model': model,
                                'text_may_be_truncated': len(text) >= 64000})
                        join = True
                        if model and model not in models:
                            models.append(model)
            if model and model not in models:
                models.append(model)
            title = title if isinstance(title, str) else None
            title = title or next((e['text'].replace('\n', ' ')[:100] for e in events if e['role'] == 'user'), None)
            # Hash the public projection; a thought-only update cannot reset readers.
            digest = hashlib.sha256(json.dumps(events, sort_keys=True).encode()).hexdigest()[:24]
            result = {'signature': signature, 'generation': digest, 'events': events, 'stale': False,
                'session_id': str(session), 'agent_session_id': agent_session,
                'native_surface': 'desktop' if path.name == 'messages.json' else 'cli',
                'title': redact_portable(str(title or 'Grok · ' + str(session)[:8])[:200]),
                'title_source': 'native_session', 'cwd': str(cwd or ''), 'model': model,
                'model_source': meta.get('model_source') if path.name=='messages.json' else 'native_session_model_id',
                'observed_models': models, 'modified_at': stamp(meta.get('updatedAt') or stat.st_mtime),
                'archived': bool(meta.get('archived')), 'invalid_lines': invalid, 'partial': partial}
        except (ValueError, OSError, TypeError, AttributeError):
            if cached:
                return {**cached, 'stale': True}
            raise ValueError('Grok public history temporarily unavailable') from None
        self.cache[path] = result
        self.cache.move_to_end(path)
        while len(self.cache) > self.max_cache:
            self.cache.popitem(last=False)
        return result
