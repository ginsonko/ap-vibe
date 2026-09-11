"""Public projection of GenericAgent Admin's native JSON session records.

The native file is never changed. A partially written replacement retains the
last complete public snapshot, explicitly marked stale, until the writer ends.
"""
import hashlib
import json
from pathlib import Path

from .contracts import ContractError, utc_now
from .product import redact_portable


class GenericAgentFiles:
    def __init__(self):
        self.cache = {}

    def snapshot(self, path):
        from .external_sessions import native_model, public_text, stamp
        path = Path(path)
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = self.cache.get(path)
        if cached and cached['signature'] == signature:
            return cached
        try:
            raw = path.read_bytes()
            record = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(record, dict) or not isinstance(record.get('id'), str) or not isinstance(record.get('messages'), list):
                raise ValueError('Invalid native session')
        except (OSError, ValueError):
            if cached:
                return {**cached, 'stale': True}
            raise
        events = []
        for index, message in enumerate(record['messages']):
            if not isinstance(message, dict) or message.get('role') not in {'user', 'assistant'}:
                continue
            # Never include raw_history, tool payloads, or structured reasoning.
            content = message.get('content')
            text = public_text(content)
            if not text.strip():
                continue
            model = message.get('model_id')
            model = native_model({'model': model}) if model else None
            events.append({'id': str(message.get('id') or index), 'role': message['role'],
                'text': text, 'timestamp': stamp(message.get('created_at')), 'model': model,
                'error': bool(message.get('error')), 'text_may_be_truncated': len(text) >= 64000})
        models = list(dict.fromkeys(e['model'] for e in events if e.get('model')))
        workspace = record.get('workspace')
        if isinstance(workspace, dict):
            workspace = workspace.get('path') or workspace.get('cwd')
        result = {'signature': signature, 'generation': hashlib.sha256(raw).hexdigest()[:24],
            'stale': False, 'events': events, 'session_id': record['id'],
            'title': redact_portable(str(record.get('title') or 'GenericAgent · ' + record['id'][:8])[:200]),
            'cwd': workspace if isinstance(workspace, str) else '',
            'title_source': record.get('title_source') or 'native_session',
            'modified_at': stamp(record.get('updated_at') or stat.st_mtime),
            'model': models[-1] if models else None, 'observed_models': models}
        self.cache[path] = result
        return result

    def read(self, path, *, after=None, before=None, expected_generation=None, limit=20):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ContractError('session_page_limit_invalid')
        if after is not None and before is not None:
            raise ContractError('session_cursor_conflict')
        if any(type(v) is not int or v < 0 for v in (after, before) if v is not None):
            raise ContractError('session_cursor_invalid')
        record = self.snapshot(path)
        reset = bool(expected_generation and record['generation'] != expected_generation)
        if reset:
            after = before = None
        count = len(record['events'])
        end = min(count, before) if before is not None else count
        start = min(count, after) if after is not None else max(0, end - limit)
        end = min(count, start + limit) if after is not None else end
        events = [{**e, 'offset': i, 'end_offset': i + 1} for i, e in enumerate(record['events'][start:end], start)]
        return {'ok': True, 'events': events, 'generation': record['generation'], 'reset': reset,
            'cursor': end, 'history_before': start, 'has_older': start > 0, 'has_more': end < count,
            'source_size': count, 'stale': record['stale'], 'read_at': utc_now(), 'read_only': True,
            'history_scope': 'native_json_public_messages'}
