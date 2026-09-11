"""Open local discovery and on-demand reading of ordinary harness sessions."""
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
from urllib.parse import urlencode

from .claude_sessions import assistant_model, visible
from .codex_activity import CodexJsonlReceptor
from .contracts import ContractError, utc_now
from .product import redact_portable
from .session_window import public_window
from .harness_registry import catalog as harness_catalog, valid_kind


def canonical(value):
    return os.path.normcase(str(Path(value).expanduser().resolve())) if value else ''


class SessionDirectory:
    def __init__(self, service):
        self.service = service
        self._state_cache = {}

    def observation(self, source_id):
        if not source_id.startswith(('codex-', 'claude-')):
            window = self.service.external_sessions.read(source_id, limit=1)
            events = window.get('events', [])
            return {'state': 'unknown', 'status_basis': 'public_transcript_only',
                    'event_at': events[-1].get('timestamp') if events else None,
                    'tool': None, 'first_message': None, 'auxiliary': False}
        if source_id.startswith('codex-'):
            source = self.service.product_registry.source(source_id.removeprefix('codex-'))
            path, harness, session = Path(source.source_path), 'codex', source.session_id
        else:
            self.service.claude_sessions.discover()
            path = self.service.claude_sessions._files.get(source_id)
            if path is None:
                raise ContractError('session_source_not_found')
            harness, session = 'claude', path.stem
        stat = path.stat()
        signature = (str(path), stat.st_size, stat.st_mtime_ns)
        prior = self._state_cache.get(source_id)
        if prior and prior[0] == signature:
            return prior[1]
        from .session_observation import inspect
        value = inspect(path, harness, session)
        if len(self._state_cache) > 512:
            self._state_cache.clear()
        self._state_cache[source_id] = (signature, value)
        return value

    def catalog(self, *, harness=None, cwd=None, project_id=None, session_id=None, query=None, offset=0, limit=20, include_all=False):
        if harness and not valid_kind(harness):
            raise ContractError('session_harness_invalid')
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ContractError('session_page_invalid')
        sources, warnings = [], []
        coverage = {}
        if not harness or harness == 'codex':
            registry = self.service.product_registry
            titles = self.service._codex_titles()
            with closing(registry._connect()) as connection:
                rows = connection.execute('SELECT * FROM codex_sources ORDER BY modified_at DESC, source_key').fetchall()
            coverage['codex'] = {'scope': 'registered_sources', 'source_count': len(rows)}
            for row in rows:
                source = registry._source_row(row)
                stamp, available = source.modified_at, True
                try:
                    stamp = datetime.fromtimestamp(Path(source.source_path).stat().st_mtime, timezone.utc).isoformat()
                except OSError:
                    available = False
                sources.append({'source_id': 'codex-' + source.source_key, 'harness': 'codex',
                    'session_id': source.session_id, 'cwd': source.session_cwd,
                    'title': titles.get(source.session_id) or '未命名 Codex 会话 · ' + str(source.session_id or source.source_key)[:8],
                    'title_source': 'codex_title' if source.session_id in titles else 'session_id',
                    'project_id': source.project_id, 'membership_basis': source.binding_kind,
                    'modified_at': stamp, 'available': available})
        if not harness or harness == 'claude':
            snapshot = self.service.task_context.projects.annotate_claude_sources(self.service.claude_sessions.discover())
            coverage['claude'] = {key: snapshot[key] for key in ('total_discovered', 'has_more_sources', 'available_root_count')}
            warnings.extend(snapshot.get('warnings', []))
            for source in snapshot['sources']:
                membership = source.get('project_membership') or {}
                sources.append({'source_id': source['source_id'], 'harness': 'claude',
                    'session_id': source['session_id'], 'cwd': source['cwd'], 'title': source['title'],
                    'model': source.get('model'), 'model_source': source.get('model_source'),
                    'observed_models': source.get('observed_models', []),
                    'title_source': source['title_source'], 'project_id': membership.get('project_id'),
                    'membership_basis': 'explicit_session' if membership else 'unclassified',
                    'modified_at': datetime.fromtimestamp(source['updated_at'], timezone.utc).isoformat(), 'available': True})
        external = getattr(self.service, 'external_sessions', None)
        if external:
            snapshot = self.service.task_context.projects.annotate_sources(external.discover())
            warnings.extend(snapshot.get('warnings', []))
            coverage.update(snapshot.get('coverage', {}))
            for source in snapshot['sources']:
                if harness and source['harness'] != harness:
                    continue
                membership = source.get('project_membership') or {}
                sources.append({**source, 'project_id': membership.get('project_id'),
                                'membership_basis': 'explicit_session' if membership else 'unclassified'})
        from . import managed_sessions
        managed = managed_sessions.catalog(self.service.product_registry)
        sources.extend(item for item in managed if not harness or item['harness'] == harness)
        coverage['studio'] = {'scope': 'all_saved_managed_runs', 'source_count': len(managed)}
        grouped = {}
        for item in sources:
            if cwd and canonical(cwd) != canonical(item['cwd']):
                continue
            if project_id and project_id != item['project_id']:
                continue
            if session_id and session_id != item['session_id']:
                continue
            if query and query.casefold() not in '\n'.join(str(item.get(k) or '') for k in ('title', 'cwd', 'session_id', 'model')).casefold():
                continue
            item['read_url'] = '/v1/ap-vibe/sessions/read?' + urlencode({'source_id': item['source_id']})
            key = (item['harness'], item['session_id'] or item['source_id'])
            grouped.setdefault(key, []).append(item)
        sessions = []
        for items in grouped.values():
            items.sort(key=lambda item: (item['modified_at'], item['source_id']), reverse=True)
            latest = next((item for item in items if item.get('managed')), items[0])
            project_ids = sorted({item['project_id'] for item in items if item['project_id']})
            sessions.append({**latest, 'source_project_ids': project_ids,
                'membership_conflict': len(project_ids) > 1,
                'sources': [{k: item.get(k) for k in ('source_id', 'modified_at', 'available', 'read_url', 'project_id', 'model', 'model_source', 'observed_models', 'managed', 'state', 'run_id', 'task_id')} for item in items]})
        sessions.sort(key=lambda item: (item['modified_at'], item['source_id']), reverse=True)
        return {'ok': True, 'sessions': sessions if include_all else sessions[offset:offset + limit], 'total': len(sessions),
                'next_offset': offset + limit if not include_all and offset + limit < len(sessions) else None,
                'offset': offset, 'limit': limit, 'read_at': utc_now(), 'coverage': coverage,
                'warnings': warnings, 'read_only': True, 'harnesses': harness_catalog(),
                'instructions': '按真实会话ID和工作内容选择，标题不决定项目归属。modified_at 是文件活动，不证明仍在执行。sources 包含同会话不同记录，最新来源优先；正文按 source_id 读取。cwd 仅为精确目录筛选，无结果可不带 cwd 全局查找。'}

    def read(self, source_id, **options):
        if not isinstance(source_id, str):
            raise ContractError('session_source_id_required')
        if source_id.startswith('studio-'):
            from .managed_sessions import read
            return read(self.service.product_registry, source_id, **options)
        if source_id.startswith('codex-'):
            source = self.service.product_registry.source(source_id.removeprefix('codex-'))
            path = Path(source.source_path)
            parser = CodexJsonlReceptor(path)
            def project(record, start, end):
                event = parser._visible(record, start=start, end=end)
                if event:
                    return {'id': event.occurrence_id, 'role': event.role, 'text': redact_portable(event.text, preserve_local_paths=True),
                            'timestamp': event.timestamp, 'source_ref': event.source_ref,
                            'text_may_be_truncated': len(event.text) >= 12000}
            identity = {'harness': 'codex', 'session_id': source.session_id, 'cwd': source.session_cwd}
        elif source_id.startswith('claude-'):
            monitor = self.service.claude_sessions
            snapshot = monitor.discover()
            source = next((item for item in snapshot['sources'] if item['source_id'] == source_id), None)
            if not source:
                raise ContractError('claude_source_not_found')
            path = monitor._files[source_id]
            def project(record, start, end):
                events = visible(record, start, preserve_local_paths=True)
                if not events:
                    return None
                text = '\n'.join(item['text'] for item in events)
                role = 'tool' if all(item['kind'] in {'tool', 'tool_result'} for item in events) else record['type']
                return {'id': str(record.get('uuid') or start), 'role': role, 'text': text[:64000],
                        'timestamp': record.get('timestamp'), 'text_may_be_truncated': len(text) >= 64000,
                        'model': assistant_model(record)}
            identity = {'harness': 'claude', 'session_id': source['session_id'], 'cwd': source['cwd'],
                        'title': source['title'], 'title_source': source['title_source'],
                        'model': source.get('model'), 'model_source': source.get('model_source'),
                        'observed_models': source.get('observed_models', [])}
        elif getattr(self.service, 'external_sessions', None):
            return self.service.external_sessions.read(source_id, **options)
        else:
            raise ContractError('session_source_not_found')
        try:
            return {**public_window(path, project, **options), **identity, 'source_id': source_id}
        except OSError as exc:
            raise ContractError('session_source_unavailable_retry') from exc
