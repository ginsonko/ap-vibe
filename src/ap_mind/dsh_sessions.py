"""Read DSH's released v3 logs without replaying or migrating native state.

The durable v3 message contains its own compact stream. Only the finalized
public text is projected; stream chunks, reasoning and plugin injections stay
native. Concatenated Zstandard frames are normal, including during a live turn.
"""
from collections import OrderedDict
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import re

from .native_json_sessions import GenericAgentFiles
from .product import redact_portable


def log_paths(root):
    root = Path(root)
    if root.is_file():
        return [root]
    # A session's migrated generations coexist. Select the newest generation
    # even when unsupported, so discovery reports it rather than showing an
    # obsolete v3 copy as if it were the current conversation.
    paths = list(root.glob('*/*/session.v*.jsonl*'))
    selected = {}
    for p in paths:
        match = re.fullmatch(r'session\.v(\d+)\.jsonl(?:\.zstd)?', p.name)
        if match:
            rank = (int(match[1]), p.stat().st_mtime_ns)
            if p.parent not in selected or rank > selected[p.parent][0]:
                selected[p.parent] = (rank, p)
    return [item[1] for item in selected.values()]


def public_message(row):
    from .external_sessions import public_text, stamp, native_model
    kind, data = row.get('type'), row.get('data')
    if not isinstance(data, dict) or kind not in {'user/message', 'assistant/message'}:
        return None
    message = data if kind == 'user/message' else data.get('message')
    if not isinstance(message, dict):
        return None
    source = message.get('source') or {}
    if not isinstance(source, dict):
        return None
    # DSH also puts the system prompt and Skill catalog in user/message rows.
    if kind == 'user/message' and source.get('kind') != 'user':
        return None
    if kind == 'assistant/message' and source.get('kind') not in {None, 'model'}:
        return None
    text = public_text(message.get('content'))
    if not text.strip():
        return None
    return {'id': str(message.get('id') or row.get('seq')), 'role': kind.split('/')[0],
            'text': text, 'timestamp': stamp(row.get('time')),
            'model': native_model(source), 'native_seq': row.get('seq'),
            'surface_op': row.get('surfaceOp'), 'text_may_be_truncated': len(text) >= 64000}


class DshFiles(GenericAgentFiles):
    def __init__(self, max_bytes=256 * 1024 * 1024):
        self.cache = OrderedDict()
        self.max_bytes = max_bytes

    def snapshot(self, path):
        from .external_sessions import native_model, stamp
        path = Path(path)
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = self.cache.get(path)
        if cached and cached['signature'] == signature:
            self.cache.move_to_end(path)
            return cached
        header, events, title, model, models = None, [], None, None, []
        invalid, consumed, partial = 0, 0, False
        digest = hashlib.sha256()
        try:
            with ExitStack() as stack:
                raw = stack.enter_context(path.open('rb'))
                if path.suffix == '.zstd':
                    try:
                        import zstandard
                    except ImportError:
                        raise ValueError('DSH 压缩会话需要 zstandard；请运行 AP-Vibe 安装器补齐读取依赖。') from None
                    stream = stack.enter_context(zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True))
                else:
                    stream = raw
                reader = stack.enter_context(io.BufferedReader(stream))
                while True:
                    line = reader.readline(min(8 * 1024 * 1024, self.max_bytes - consumed + 1))
                    if not line:
                        break
                    consumed += len(line)
                    if consumed > self.max_bytes:
                        raise ValueError('DSH 会话超过本次读取资源范围，请分段导出公开会话。')
                    if not line.endswith(b'\n'):
                        partial = True
                        break
                    digest.update(line)
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        invalid += 1
                        continue
                    if not isinstance(row, dict):
                        invalid += 1
                        continue
                    if header is None:
                        if row.get('type') != 'session' or row.get('version') != 3 or not row.get('id'):
                            raise ValueError('DSH 会话格式暂不支持；原记录未修改。')
                        header = row
                        continue
                    data = row.get('data') or {}
                    if not isinstance(data, dict):
                        continue
                    if row.get('type') == 'session/title' and isinstance(data.get('title'), str):
                        title = redact_portable(data['title'][:200])
                    if row.get('type') in {'model/selection', 'request/context'}:
                        model = native_model(data) or model
                        if model and model not in models:
                            models.append(model)
                    event = public_message(row)
                    if event:
                        if not title and event['role'] == 'user':
                            title = event['text'].replace('\n', ' ')[:100]
                        event['model'] = event['model'] or model
                        events.append(event)
            if not header:
                raise ValueError('DSH 会话尚未写入完整头部。')
            if (path.stat().st_mtime_ns, path.stat().st_size) != signature:
                partial = True
        except Exception as exc:
            # Retain the last verified public projection while native writes
            # are incomplete. Do not dump raw corrupt/provider payloads.
            if cached:
                return {**cached, 'stale': True}
            if isinstance(exc, (OSError, ValueError)):
                raise
            raise ValueError('DSH 压缩会话暂不可读，原记录已保留。') from None
        result = {'signature': signature, 'generation': digest.hexdigest()[:24],
            'stale': partial, 'events': events, 'session_id': str(header['id']),
            'title': title or 'DSH · ' + str(header['id'])[:12],
            'title_source': 'native_session',
            'cwd': os.path.normpath(header['cwd']) if isinstance(header.get('cwd'), str) else '',
            'modified_at': stamp(stat.st_mtime), 'model': model, 'observed_models': models,
            'parent_session_id': header.get('parentSession'), 'invalid_lines': invalid}
        if not partial:
            self.cache[path] = result
            self.cache.move_to_end(path)
            while len(self.cache) > 32:
                self.cache.popitem(last=False)
        return result

    def read(self, path, **kwargs):
        return {**super().read(path, **kwargs), 'history_scope': 'dsh_v3_public_messages'}
