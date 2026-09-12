"""Public projection of WorkBuddy bundled CodeBuddy CLI JSONL transcripts.

CLI layout (measured): ~/.codebuddy/projects/<encoded-cwd>/<sessionId>.jsonl
Each complete line is a JSON object. Public user/assistant speech uses
type=message with content blocks type=input_text or output_text. The actual
model id is providerData.model on assistant rows (not Claude's message.model).

This module never opens WorkBuddy GUI sessions.db. That store is a different
source and was not measured here; do not treat JSONL recovery as GUI support.

Private reasoning, tool arguments/results, usage blobs, file-history snapshots,
local flags and credentials stay native. Pagination reuses ap_mind.session_window.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import threading
import time

try:
    from .contracts import ContractError
    from .product import redact_portable
    from .session_window import generation, public_window, read_records
except ImportError:
    from ap_mind.contracts import ContractError
    from ap_mind.product import redact_portable
    from ap_mind.session_window import generation, public_window, read_records

HARNESS = 'workbuddy'
STORAGE = 'codebuddy_cli_jsonl'
HISTORY_SCOPE = 'codebuddy_cli_public_jsonl'
DEFAULT_ROOT = Path.home() / '.codebuddy' / 'projects'
# Only these block types are public speech in the measured CLI transcript.
# 'text' is a defensive fallback, not evidence of a Claude-shaped CodeBuddy file.
PUBLIC_BLOCK_TYPES = frozenset({'input_text', 'output_text', 'text'})
# Record types observed or reserved; none of these are user/assistant speech.
SKIP_RECORD_TYPES = frozenset({
    'file-history-snapshot', 'progress', 'system', 'tool', 'tool_result',
    'attachment', 'custom-title',
})


def stamp(value):
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    return value if isinstance(value, str) else None


def public_text(content):
    """Join measured public blocks. Unknown types, thinking and tool payloads are dropped."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get('type')
            if kind not in PUBLIC_BLOCK_TYPES:
                continue
            piece = block.get('text')
            if isinstance(piece, str) and piece.strip():
                parts.append(piece)
        text = '\n'.join(parts)
    else:
        return ''
    return redact_portable(text[:64000], preserve_local_paths=True)


def record_model(record):
    """Assistant providerData.model (string or nested id). Never Claude message.model."""
    if record.get('type') != 'message' or record.get('role') != 'assistant':
        return None
    data = record.get('providerData')
    if not isinstance(data, dict):
        return None
    model = data.get('model')
    if isinstance(model, dict):
        model = model.get('id') or model.get('modelId') or model.get('model') or model.get('name')
    if not isinstance(model, str) or not model.strip() or model.strip() in {'<synthetic>', 'auto', 'Auto'}:
        return None
    return redact_portable(model.strip()[:200])


def codebuddy_event(record, start, end):
    if record.get('isMeta') or record.get('type') in SKIP_RECORD_TYPES:
        return None
    if record.get('type') != 'message' or record.get('role') not in {'user', 'assistant'}:
        return None
    text = public_text(record.get('content'))
    if not text.strip():
        return None
    return {
        'id': str(record.get('id') or start),
        'role': record['role'],
        'text': text,
        'timestamp': stamp(record.get('timestamp')),
        'model': record_model(record),
        'session_id': record['sessionId'][:256] if isinstance(record.get('sessionId'), str) else None,
        'cwd': record['cwd'][:4096] if isinstance(record.get('cwd'), str) else None,
        'text_may_be_truncated': len(text) >= 64000,
    }


def default_roots():
    override = os.environ.get('CODEBUDDY_HOME') or os.environ.get('CODEBUDDY_CONFIG_DIR')
    if override:
        return [Path(override).expanduser() / 'projects']
    return [DEFAULT_ROOT]


def _jsonl_paths(root):
    root = Path(root).expanduser()
    try:
        root = root.resolve()
    except OSError:
        return []
    if root.is_file():
        return [root] if root.suffix.lower() == '.jsonl' else []
    if not root.is_dir():
        return []
    seen, found = set(), []
    for pattern in ('*/*.jsonl', '*.jsonl'):
        try:
            candidates = root.glob(pattern)
        except OSError:
            continue
        for path in candidates:
            try:
                path = path.resolve()
            except OSError:
                continue
            if path in seen or path.suffix.lower() != '.jsonl':
                continue
            if path.name.endswith('.revisions.jsonl'):
                continue
            try:
                if not path.is_relative_to(root):
                    continue
            except (OSError, ValueError):
                continue
            seen.add(path)
            found.append(path)
    return found


def _source_id(path):
    return HARNESS + '-' + hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()[:32]


def snapshot(path):
    """Bounded public metadata from a CLI JSONL file. Incomplete rewrite keeps last cache via caller."""
    path = Path(path)
    stat = path.stat()
    head, _, _ = read_records(path, 0, min(stat.st_size, 65536))
    tail, invalid, _ = read_records(path, max(0, stat.st_size - 512 * 1024), stat.st_size)
    session_id, cwd, title, model, models = path.stem[:256], '', None, None, []
    for offset, record in head + tail:
        if not isinstance(record, dict):
            continue
        if isinstance(record.get('sessionId'), str) and record['sessionId']:
            session_id = record['sessionId'][:256]
        if isinstance(record.get('cwd'), str) and record['cwd']:
            cwd = record['cwd'][:4096]
        event = codebuddy_event(record, offset, offset)
        if event and event['role'] == 'user' and not title:
            title = event['text'].replace('\n', ' ')[:100]
        candidate = record_model(record) or (event.get('model') if event else None)
        if isinstance(candidate, str) and candidate:
            model = candidate
            if model not in models:
                models.append(model)
    return {
        'harness': HARNESS,
        'storage': STORAGE,
        'session_id': session_id,
        'cwd': cwd,
        'title': redact_portable(title or 'CodeBuddy · ' + session_id[:8]),
        'title_source': 'first_visible_message' if title else 'session_id',
        'model': model,
        'model_source': 'providerData.model' if model else None,
        'observed_models': models,
        'modified_at': stamp(stat.st_mtime),
        'available': True,
        'read_only': True,
        'invalid_lines': invalid,
        'generation': generation(path, stat),
        'source_size': stat.st_size,
        'gui_sessions_db': False,
        'gui_recovery': False,
    }


def sources(path, files, max_sources):
    """Discover CLI JSONL transcripts. `files[source_id] = (harness, path, session_id)`."""
    if type(max_sources) is not int or max_sources < 1:
        raise ContractError('session_page_limit_invalid')
    found = []
    for jsonl in _jsonl_paths(path):
        try:
            record = snapshot(jsonl)
        except (OSError, ValueError, TypeError):
            continue
        key = _source_id(jsonl)
        item = {**record, 'source_id': key}
        found.append(item)
        files[key] = (HARNESS, jsonl, record['session_id'])
    found.sort(key=lambda item: item.get('modified_at') or '', reverse=True)
    return found[:max_sources]


def read(path, session=None, **options):
    """Page public events with session_window cursors. `session` is optional identity check."""
    path = Path(path)
    window = public_window(path, codebuddy_event, **options)
    meta = snapshot(path)
    if session is not None and meta['session_id'] != session:
        raise ContractError('session_source_not_found')
    return {**meta, **window, 'history_scope': HISTORY_SCOPE, 'ok': True}


class CodeBuddySessions:
    """Directory adapter: sources() plus source_id read for main-task registration."""

    def __init__(self, roots=None, *, max_sources=1000, cache_seconds=3, window_bytes=2 * 1024 * 1024):
        if isinstance(roots, (str, Path)):
            roots = [roots]
        self.roots = [Path(p).expanduser() for p in (roots if roots is not None else default_roots())]
        self.max_sources = max(1, int(max_sources))
        self.cache_seconds = max(0, float(cache_seconds))
        self.window_bytes = max(4096, int(window_bytes))
        self._lock = threading.RLock()
        self._files, self._meta = {}, {}
        self._snapshot = None
        self._scanned = 0

    def discover(self):
        with self._lock:
            if self._snapshot is not None and time.monotonic() - self._scanned < self.cache_seconds:
                return self._snapshot
            files, items, warnings = {}, [], []
            existing = 0
            for root in self.roots:
                if root.is_file() and root.suffix.lower() in {'.db', '.sqlite'}:
                    warnings.append('WorkBuddy GUI sessions.db 未测，本模块只读 CodeBuddy CLI JSONL。')
                    continue
                if root.exists():
                    existing += 1
                try:
                    items.extend(sources(root, files, self.max_sources))
                except OSError:
                    warnings.append('一个 CodeBuddy CLI 目录暂时无法读取。')
            items.sort(key=lambda item: item.get('modified_at') or '', reverse=True)
            clipped = items[:self.max_sources]
            self._files = {item['source_id']: files[item['source_id']] for item in clipped if item['source_id'] in files}
            self._meta = {item['source_id']: item for item in clipped}
            self._snapshot = {
                'ok': True, 'sources': clipped, 'read_only': True,
                'total_discovered': len(items), 'has_more_sources': len(items) > self.max_sources,
                'warnings': warnings, 'root_count': len(self.roots), 'available_root_count': existing,
                'harness': HARNESS, 'storage': STORAGE, 'gui_sessions_db': False, 'gui_recovery': False,
            }
            self._scanned = time.monotonic()
            return self._snapshot

    def read(self, source_id, **options):
        with self._lock:
            self.discover()
            loc = self._files.get(source_id)
            if loc is None:
                raise ContractError('session_source_not_found')
            kind, path, session = loc
            try:
                window = public_window(
                    path, codebuddy_event, window_bytes=self.window_bytes, **options)
                return {**self._meta.get(source_id, {}), **window,
                        'source_id': source_id, 'history_scope': HISTORY_SCOPE}
            except OSError as exc:
                raise ContractError('session_source_unavailable_retry') from exc
