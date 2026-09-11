"""Read-only native session adapters for additional harnesses.

No native database is modified or migrated. One broken application cannot hide
other sources. Private reasoning, tool arguments, attachments and credentials
never enter the public projection. Paths are discovered, not accepted from read
requests, preventing a forged source_id from reading arbitrary local files.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from .contracts import ContractError, utc_now
from .harness_registry import HARNESS, default_roots
from .product import redact_portable
from .session_window import generation, public_window, read_records
from .native_json_sessions import GenericAgentFiles
from . import hermes_sessions
from .dsh_sessions import DshFiles, log_paths as dsh_paths


def stamp(value):
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    return value if isinstance(value, str) else None


def public_text(content):
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = '\n'.join(b['text'] for b in content if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str))
    else:
        return ''
    return redact_portable(text[:64000], preserve_local_paths=True)


def pi_event(record, start, end):
    message = record.get('message')
    if record.get('type') != 'message' or not isinstance(message, dict) or message.get('role') not in {'user', 'assistant'}:
        return None
    text = public_text(message.get('content'))
    if not text.strip():
        return None
    return {'id': str(record.get('id') or start), 'role': message['role'], 'text': text,
            'timestamp': stamp(record.get('timestamp') or message.get('timestamp')),
            'model': str(message.get('model') or '')[:200] or None,
            'text_may_be_truncated': len(text) >= 64000}


def pi_desktop_event(record, start, end):
    if record.get('type') != 'message' or record.get('role') not in {'user', 'assistant'}:
        return None
    text = public_text(record.get('blocks'))
    if not text.strip():
        return None
    meta = record.get('meta') if isinstance(record.get('meta'), dict) else {}
    return {'id': str(record.get('id') or start), 'role': record['role'], 'text': text,
            'timestamp': stamp(record.get('createdAt')), 'model': native_model(meta),
            'text_may_be_truncated': len(text) >= 64000}


def native_model(data):
    model = data.get('modelID') or data.get('modelId') or data.get('model')
    if isinstance(model, dict):
        model = model.get('modelID') or model.get('modelId') or model.get('id')
    return redact_portable(model[:200]) if isinstance(model, str) else None


def readonly_db(path):
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


class ExternalSessions:
    def __init__(self, data_dir, *, roots=None, cache_seconds=3, max_sources=1000):
        self.config_path = Path(data_dir) / 'harness-monitor.json'
        self.roots_override = roots
        self.cache_seconds, self.max_sources = cache_seconds, max_sources
        self._lock = threading.RLock()
        self._files, self._cache, self._snapshot = {}, {}, None
        self._scanned = 0
        self._ga = GenericAgentFiles()
        self._dsh = DshFiles()

    def _settings(self):
        roots = default_roots() if self.roots_override is None else dict(self.roots_override)
        warnings = []
        if self.config_path.is_file():
            try:
                config = json.loads(self.config_path.read_text(encoding='utf-8-sig'))
                for kind, value in config.get('sources', {}).items():
                    if kind not in HARNESS or kind in {'codex', 'claude'}:
                        continue
                    if value.get('enabled') is False:
                        roots[kind] = []
                    elif 'roots' in value:
                        if not isinstance(value['roots'], list) or not all(isinstance(p, str) for p in value['roots']):
                            raise ValueError('roots must be path lists')
                        roots[kind] = value['roots']
            except (OSError, ValueError, TypeError, AttributeError):
                warnings.append('扩展应用监看配置暂不可读，仍使用可用的默认目录。')
        return roots, warnings

    def discover(self):
        with self._lock:
            if self._snapshot is not None and time.monotonic() - self._scanned < self.cache_seconds:
                return self._snapshot
            roots, warnings = self._settings()
            sources, files, coverage = [], {}, {}
            for kind, paths in roots.items():
                if kind not in HARNESS:
                    continue
                seen, found = set(), []
                existing = 0
                for raw in paths:
                    root = Path(raw).expanduser().resolve()
                    if not root.exists():
                        continue
                    existing += 1
                    try:
                        storage = HARNESS[kind]['storage']
                        pattern = '*.json' if storage == 'ga-json' else '*/sessions/*.jsonl' if kind == 'openclaw' else '*.jsonl' if storage == 'pi-desktop' else '*/*.jsonl'
                        candidates = ([root] if root.is_file() else list(root.glob('state.db')) + list(root.glob('profiles/*/state.db'))) if storage == 'hermes' else ([root] if root.is_file() else list(root.glob('*.db')) + list(root.glob('*.sqlite'))) if storage == 'opencode' else ([root] if root.is_file() else list(root.glob(pattern)))
                        if storage == 'dsh':
                            candidates = dsh_paths(root)
                        for path in candidates:
                            path = path.resolve()
                            if path in seen or (root.is_dir() and not path.is_relative_to(root)):
                                continue
                            seen.add(path)
                            try:
                                if storage == 'opencode':
                                    found.extend(self._opencode_sources(path, files, kind))
                                elif storage == 'hermes':
                                    found.extend(hermes_sessions.sources(path,files,self.max_sources))
                                elif storage in {'ga-json', 'dsh'}:
                                    record = (self._dsh if storage == 'dsh' else self._ga).snapshot(path)
                                    key = kind + '-' + hashlib.sha256((os.path.normcase(str(path)) + ':' + record['session_id']).encode()).hexdigest()[:32]
                                    item = {k: v for k, v in record.items() if k not in {'events', 'signature', 'generation'}}
                                    found.append({**item, 'source_id': key, 'harness': kind, 'available': True, 'model_source': 'native_message' if record.get('model') else None})
                                    files[key] = (kind, path, record['session_id'])
                                else:
                                    if storage == 'pi-desktop' and path.name.endswith('.revisions.jsonl'):
                                        continue
                                    item = self._pi_source(kind, path)
                                    if item:
                                        found.append(item)
                                        files[item['source_id']] = (kind, path, item['session_id'])
                            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
                                warnings.append(HARNESS[kind]['name'] + ' 的一条记录暂不可读，其它记录继续显示。')
                    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
                        warnings.append(HARNESS[kind]['name'] + ' 的一个会话目录暂不可读，其它来源继续显示。')
                found.sort(key=lambda s: s['modified_at'], reverse=True)
                coverage[kind] = {'available_root_count': existing, 'total_discovered': len(found),
                                  'has_more_sources': len(found) > self.max_sources}
                sources.extend(found[:self.max_sources])
            self._files = {s['source_id']: files[s['source_id']] for s in sources}
            self._snapshot = {'ok': True, 'sources': sources, 'coverage': coverage, 'warnings': warnings, 'read_only': True}
            self._scanned = time.monotonic()
            return self._snapshot

    def _pi_source(self, kind, path):
        stat = path.stat()
        sig = (stat.st_mtime_ns, stat.st_size, generation(path, stat))
        desktop = HARNESS[kind]['storage'] == 'pi-desktop'
        index = path.parent.parent / 'pi.sqlite'
        if desktop:
            sig += tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in [index, Path(str(index) + '-wal')])
        key = kind + '-' + hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()[:32]
        if key in self._cache and self._cache[key][0] == sig:
            return dict(self._cache[key][1])
        head, _, _ = read_records(path, 0, min(stat.st_size, 65536))
        tail, invalid, _ = read_records(path, max(0, stat.st_size - 512 * 1024), stat.st_size)
        meta = next((r for _, r in head if r.get('type') == 'session'), {})
        if desktop:
            meta = {**meta, 'id': meta.get('sessionId')}
        if not meta.get('id'):
            return None
        title, model = None, None
        models = []
        for offset, record in head + tail:
            event = (pi_desktop_event if desktop else pi_event)(record, offset, offset)
            if event and event['role'] == 'user' and not title:
                title = event['text'].replace('\n', ' ')[:100]
            candidate = event.get('model') if event else (record.get('modelId') if record.get('type') == 'model_change' else None)
            if isinstance(candidate, str) and candidate:
                model = redact_portable(candidate[:200])
                if model not in models:
                    models.append(model)
            if record.get('type') == 'session_info' and isinstance(record.get('name'), str):
                title = record['name'][:200]
        if desktop and index.is_file():
            try:
                with closing(readonly_db(index)) as conn:
                    row = conn.execute('SELECT s.title,p.path,s.model_id FROM sessions s LEFT JOIN projects p ON p.id=s.project_id WHERE s.id=? AND s.deleted_at IS NULL', (meta['id'],)).fetchone()
                    if row:
                        title = row['title'] or title
                        meta['cwd'] = row['path'] or ''
                        model = model or row['model_id']
            except sqlite3.Error:
                pass  # Public transcript remains readable while the index is unavailable.
        result = {'source_id': key, 'harness': kind, 'session_id': str(meta['id'])[:256],
                  'cwd': str(meta.get('cwd') or '')[:4096],
                  'title': redact_portable(title or HARNESS[kind]['name'] + ' · ' + str(meta['id'])[:8]),
                  'title_source': 'native_session', 'model': model, 'observed_models': models,
                  'model_source': 'native_message' if model else None,
                  'modified_at': stamp(stat.st_mtime), 'available': True, 'invalid_lines': invalid}
        self._cache[key] = (sig, result)
        return dict(result)

    def _opencode_sources(self, path, files, kind='opencode'):
        result = []
        with closing(readonly_db(path)) as conn:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(session)')}
            parent = 'parent_id' if 'parent_id' in columns else 'NULL AS parent_id'
            rows = conn.execute('SELECT id, title, directory, time_updated, ' + parent + ' FROM session ORDER BY time_updated DESC LIMIT ?', (self.max_sources + 1,)).fetchall()
            imports = {}
            if kind == 'mimocode':
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                # MiMo may automatically copy existing clients' history. Keep
                # the true lineage without guessing from titles or versions.
                if 'external_import' in tables:
                    imports.update({r['session_id']: {'harness': 'claude' if r['source'] == 'cc' else r['source'], 'session_id': r['source_key']}
                                    for r in conn.execute('SELECT session_id,source,source_key FROM external_import')})
                elif 'claude_import' in tables:
                    imports.update({r['session_id']: {'harness': 'claude', 'session_id': r['source_uuid']}
                                    for r in conn.execute('SELECT session_id,source_uuid FROM claude_import')})
            for row in rows:
                key = kind + '-' + hashlib.sha256((os.path.normcase(str(path)) + ':' + row['id']).encode()).hexdigest()[:32]
                model = None
                for msg in conn.execute('SELECT data FROM message WHERE session_id=? ORDER BY time_created DESC LIMIT 10', (row['id'],)):
                    try:
                        data = json.loads(msg[0])
                        if data.get('role') == 'assistant' and native_model(data):
                            model = native_model(data); break
                    except (ValueError, AttributeError):
                        continue
                result.append({'source_id': key, 'harness': kind, 'session_id': row['id'],
                    'title': redact_portable(str(row['title'])[:200]), 'title_source': 'native_session',
                    'cwd': row['directory'], 'model': redact_portable(model) if model else None,
                    'model_source': 'native_message' if model else None,
                    'modified_at': stamp(row['time_updated']), 'available': True,
                    'parent_session_id': row['parent_id'], 'imported_from': imports.get(row['id'])})
                files[key] = (kind, path, row['id'])
        return result

    def read(self, source_id, **options):
        with self._lock:
            snapshot = self.discover()
            source = next((s for s in snapshot['sources'] if s['source_id'] == source_id), None)
            if source is None:
                raise ContractError('session_source_not_found')
            kind, path, session = self._files[source_id]
            try:
                storage = HARNESS[kind]['storage']
                window = self._dsh.read(path, **options) if storage == 'dsh' else hermes_sessions.read(path,session,**options) if storage=='hermes' else self._ga.read(path, **options) if storage == 'ga-json' else self._opencode_read(path, session, **options) if storage == 'opencode' else public_window(path, pi_desktop_event if storage == 'pi-desktop' else pi_event, **options)
                return {**source, **window}
            except (OSError, sqlite3.Error):
                raise ContractError('session_source_unavailable_retry') from None

    def _opencode_read(self, path, session, *, after=None, before=None, expected_generation=None, limit=20):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ContractError('session_page_limit_invalid')
        if after is not None and before is not None:
            raise ContractError('session_cursor_conflict')
        if any(type(v) is not int or v < 0 for v in (after, before) if v is not None):
            raise ContractError('session_cursor_invalid')
        with closing(readonly_db(path)) as conn:
            conn.execute('BEGIN')
            changed = conn.execute('SELECT max(time_updated) FROM part WHERE session_id=?', (session,)).fetchone()[0]
            current = hashlib.sha256((generation(path, path.stat()) + str(changed) + session).encode()).hexdigest()[:24]
            reset = bool(expected_generation and expected_generation != current)
            if reset:
                after = before = None
            count = conn.execute('SELECT count(*) FROM message WHERE session_id=?', (session,)).fetchone()[0]
            end = min(count, before) if before is not None else count
            start = min(count, after) if after is not None else max(0, end - limit)
            take = min(limit, count - start) if after is not None else end - start
            rows = conn.execute('SELECT id,data,time_created FROM message WHERE session_id=? ORDER BY time_created,id LIMIT ? OFFSET ?', (session, take, start)).fetchall()
            events = []
            for i, row in enumerate(rows):
                try:
                    data = json.loads(row['data'])
                    if data.get('role') not in {'user', 'assistant'}:
                        continue
                    parts = []
                    for part in conn.execute('SELECT data FROM part WHERE message_id=? ORDER BY id', (row['id'],)):
                        try:
                            block = json.loads(part[0])
                            if isinstance(block, dict) and not block.get('synthetic'):
                                parts.append(block)
                        except ValueError:
                            continue
                    text = public_text(parts)
                    if text:
                        events.append({'id': row['id'], 'role': data['role'], 'text': text,
                                       'timestamp': stamp(row['time_created']), 'model': native_model(data),
                                       'offset': start + i, 'end_offset': start + i + 1})
                except (ValueError, AttributeError):
                    continue
        return {'ok': True, 'events': events, 'generation': current, 'reset': reset,
                'cursor': start + len(rows), 'history_before': start, 'has_older': start > 0,
                'has_more': start + len(rows) < count, 'source_size': count, 'read_at': utc_now(),
                'read_only': True, 'history_scope': 'native_sqlite_public_messages'}
