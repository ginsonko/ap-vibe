"""Read-only, bounded projection of ordinary Claude Code session transcripts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time

from .contracts import ContractError
from .product import redact_portable
from .session_window import generation, read_records


def visible(record, offset, *, preserve_local_paths=False):
    """Never project the full record, thinking, attachments or tool arguments."""
    kind = record.get('type')
    message = record.get('message')
    # Claude stores injected Skill bodies as user/isMeta records. They are not
    # user speech; the public Skill tool event already explains the operation.
    if record.get('isMeta'):
        return []
    if kind not in {'user', 'assistant'} or not isinstance(message, dict):
        return []
    content = message.get('content', [])
    if isinstance(content, str):
        content = [{'type': 'text', 'text': content}]
    if not isinstance(content, list):
        return []
    events = []
    for i, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        category = block.get('type')
        if category == 'text':
            text = block.get('text')
            if not isinstance(text, str) or not text.strip():
                continue
            label = kind
        elif category == 'tool_use':
            text = '调用工具：' + str(block.get('name', '未知'))[:200]
            label = 'tool'
        elif category == 'tool_result':
            text = '工具执行失败' if block.get('is_error') else '工具执行完成'
            label = 'tool_result'
        else:
            continue
        events.append({'id': str(record.get('uuid') or offset) + ':' + str(i), 'offset': offset,
                       'kind': label, 'text': redact_portable(text[:64000], preserve_local_paths=preserve_local_paths),
                       'timestamp': record.get('timestamp'), 'sidechain': bool(record.get('isSidechain'))})
    return events


class ClaudeSessions:
    def __init__(self, roots=None, *, window_bytes=512*1024, max_sources=300, cache_seconds=3):
        default = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'projects'
        if isinstance(roots, (str, Path)):
            roots = [roots]
        self.roots = [Path(p).expanduser().resolve() for p in (roots if roots is not None else [default])]
        self.window_bytes = max(4096, int(window_bytes))
        self.max_sources = max(1, int(max_sources))
        self.cache_seconds = max(0, float(cache_seconds))
        self._lock = threading.RLock()
        self._files, self._cache = {}, {}
        self._snapshot = None
        self._scanned = 0
        self.config_warning = None

    generation = staticmethod(generation)
    _records = staticmethod(read_records)

    def discover(self):
        with self._lock:
            if self._snapshot is not None and time.monotonic()-self._scanned < self.cache_seconds:
                return self._snapshot
            candidates, errors = [], [self.config_warning] if self.config_warning else []
            for root in self.roots:
                if not root.is_dir():
                    continue
                try:
                    # Top-level sessions only; nested subagent logs are separate work.
                    for path in root.glob('*/*.jsonl'):
                        resolved = path.resolve()
                        if not resolved.is_relative_to(root):
                            continue
                        try:
                            candidates.append((resolved, resolved.stat()))
                        except OSError:
                            continue
                except OSError:
                    errors.append('一个Claude记录目录暂时无法读取。')
            candidates = list({path:(path,stat) for path,stat in candidates}.values())
            candidates.sort(key=lambda pair: pair[1].st_mtime_ns, reverse=True)
            self._files = {}
            sources = []
            for path, stat in candidates[:self.max_sources]:
                source_id = 'claude-' + hashlib.sha256(str(path).encode()).hexdigest()[:32]
                self._files[source_id] = path
                fingerprint = (stat.st_mtime_ns, stat.st_size, self.generation(path, stat))
                cached = self._cache.get(source_id)
                if cached and cached[0] == fingerprint:
                    # Activity ages even when the underlying file is unchanged.
                    sources.append({**cached[1], 'activity': 'recent_output' if time.time()-stat.st_mtime < 120 else 'historical'})
                    continue
                try:
                    head, _, _ = self._records(path, 0, min(stat.st_size, 65536))
                    tail, invalid, _ = self._records(path, max(0, stat.st_size-self.window_bytes), stat.st_size)
                except OSError:
                    errors.append('一条Claude会话暂时无法读取，稍后自动刷新。'); continue
                metadata = {'source_id': source_id, 'session_id': path.stem, 'title': '未命名 Claude 会话 · ' + path.stem[:8],
                            'title_source': 'session_id', 'cwd': '', 'updated_at': stat.st_mtime,
                            'generation': fingerprint[2], 'read_only': True, 'activity': 'recent_output' if time.time()-stat.st_mtime < 120 else 'historical',
                            'invalid_lines': invalid}
                first = None
                for offset, record in head + tail:
                    if isinstance(record.get('sessionId'), str):
                        metadata['session_id'] = record['sessionId'][:200]
                    if isinstance(record.get('cwd'), str):
                        metadata['cwd'] = record['cwd'][:2000]
                    if record.get('type') == 'custom-title' and isinstance(record.get('customTitle'), str):
                        metadata.update(title=redact_portable(record['customTitle'][:200]), title_source='claude_title')
                    if first is None and record.get('type') == 'user':
                        texts = [e['text'] for e in visible(record, offset) if e['kind']=='user']
                        if texts:
                            first = texts[0].replace('\n',' ')[:100]
                if metadata['title_source']=='session_id' and first:
                    metadata.update(title=first, title_source='first_visible_message')
                self._cache[source_id] = (fingerprint, metadata)
                sources.append(metadata)
            self._cache = {k:v for k,v in self._cache.items() if k in self._files}
            self._snapshot = {'ok': True, 'sources': sources, 'read_only': True, 'total_discovered': len(candidates),
                              'has_more_sources': len(candidates)>self.max_sources, 'warnings': errors,
                              'root_count': len(self.roots), 'available_root_count': sum(p.is_dir() for p in self.roots)}
            self._scanned = time.monotonic()
            return self._snapshot

    def read(self, source_id, *, after=None, before=None, generation=None):
        with self._lock:
            self.discover()
            path = self._files.get(source_id)
            if path is None:
                raise ContractError('claude_source_not_found')
            if after is not None and before is not None:
                raise ContractError('claude_cursor_conflict')
            try:
                stat = path.stat()
                current = self.generation(path, stat)
                reset = bool(generation and generation != current) or (after is not None and int(after)>stat.st_size)
                if reset:
                    after = before = None
                end = min(stat.st_size, max(0,int(before))) if before is not None else stat.st_size
                start = max(0,end-self.window_bytes)
                if after is not None:
                    start = max(0,int(after)); end = min(stat.st_size,start+self.window_bytes)
                records, invalid, cursor = self._records(path,start,end)
                events = [e for offset,record in records for e in visible(record,offset)]
                return {'ok':True,'source_id':source_id,'generation':current,'reset':reset,'events':events,
                        'cursor':cursor,'history_before':records[0][0] if records else start,'has_older':start>0,'has_more':cursor<stat.st_size and cursor>start,
                        'invalid_lines':invalid,'read_only':True,'bounded_window_bytes':self.window_bytes}
            except OSError as exc:
                raise ContractError('claude_source_unavailable') from exc
