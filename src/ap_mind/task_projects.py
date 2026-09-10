"""Explicit runtime/session membership over the existing project document store."""
from contextlib import closing
import hashlib
import json

from .contracts import ContractError, utc_now
from .project_documents import clean


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class TaskProjects:
    def __init__(self, context):
        self.context = context
        self.registry = context.registry
        with closing(self.registry._connect()) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS task_project_memberships (
                    client_kind TEXT NOT NULL, session_id TEXT NOT NULL,
                    project_id TEXT NOT NULL, version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(client_kind, session_id));
                CREATE TABLE IF NOT EXISTS task_project_changes (
                    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL, result_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_project_keys (
                    project_key TEXT PRIMARY KEY, project_id TEXT NOT NULL);
            ''')
            conn.commit()

    def membership(self, kind, session):
        with closing(self.registry._connect()) as conn:
            row = conn.execute('SELECT * FROM task_project_memberships WHERE client_kind=? AND session_id=?',
                               (kind, session)).fetchone()
        return dict(row) if row else None

    def catalog(self, raw):
        # Public local read: neither a session binding nor a write receipt is needed.
        if raw.get('project_id'):
            return self.context.documents.read(raw['project_id'], raw.get('sections'), raw.get('revision'))
        limit, after = raw.get('limit', 20), raw.get('after', '')
        if type(limit) is not int or not 1 <= limit <= 50 or not isinstance(after, str):
            raise ContractError('task_projects_page_invalid')
        projects = sorted(self.registry.list(), key=lambda p: p.project_id)
        values = []
        for project in projects:
            if project.project_id <= after or project.status == 'archived':
                continue
            profile = self.context.documents.profile(project)
            if profile['registration_state'] != 'registered':
                continue
            latest = self.context.documents.latest(project.project_id) or {}
            identity = latest.get('sections', {}).get('identity', {})
            values.append({'project_id': project.project_id, 'display_name': profile['display_name'],
                           'summary': str(identity.get('summary', ''))[:600], 'root_path': project.root_path,
                           'project_key': project.extra.get('project_key'),
                           'revision': latest.get('revision', 0), 'documentation_complete': profile['documentation_complete']})
            if len(values) > limit:
                break
        return {'ok': True, 'projects': values[:limit],
                'next_after': values[limit - 1]['project_id'] if len(values) > limit else None,
                'instructions': '按真实来源判断项目；用project_id及sections读取候选章节，不凭名称合并。'}

    def annotate_claude_sources(self, catalog):
        with closing(self.registry._connect()) as conn:
            rows = conn.execute('''SELECT m.session_id, m.project_id, p.display_name, p.status
                FROM task_project_memberships m JOIN projects p ON p.project_id=m.project_id
                WHERE m.client_kind='claude' ''').fetchall()
        memberships = {row['session_id']: dict(row) for row in rows}
        # The source cache remains read-only; membership may change between polls.
        return {**catalog, 'sources': [{**source, 'project_membership': memberships.get(source.get('session_id'))}
                                       for source in catalog.get('sources', [])]}

    def classify(self, raw):
        allowed = {'receipt_id', 'session_id', 'request_id', 'expected_membership_version',
                   'project_id', 'project_key', 'display_name', 'sections', 'rationale', 'evidence_refs'}
        if set(raw) - allowed:
            raise ContractError('task_classification_fields_invalid')
        for field in ('receipt_id', 'session_id', 'request_id', 'rationale'):
            if not isinstance(raw.get(field), str) or not raw[field].strip() or len(raw[field]) > 2000:
                raise ContractError('task_classification_' + field + '_invalid')
        refs = raw.get('evidence_refs')
        if not isinstance(refs, list) or not 1 <= len(refs) <= 16 or any(
                not isinstance(v, str) or not v.strip() or len(v) > 4096 for v in refs):
            raise ContractError('task_classification_evidence_required')
        expected = raw.get('expected_membership_version')
        if type(expected) is not int or expected < 0:
            raise ContractError('task_classification_version_required')
        body = clean(dict(raw))
        fingerprint = hashlib.sha256(encoded(body).encode()).hexdigest()
        with self.registry.transaction(), closing(self.registry._connect()) as conn:
            receipt = conn.execute('SELECT * FROM task_context_receipts WHERE receipt_id=?', (raw['receipt_id'],)).fetchone()
            if not receipt or receipt['session_id'] != raw['session_id']:
                raise ContractError('task_context_receipt_identity_mismatch')
            saved = json.loads(receipt['payload_json'])
            if saved.get('readonly_identity'):
                raise ContractError('task_context_write_identity_required')
            kind = saved.get('client_kind', 'codex')
            prior = conn.execute('SELECT * FROM task_project_changes WHERE request_id=?', (raw['request_id'],)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ContractError('task_classification_request_conflict')
                return {**json.loads(prior['result_json']), 'replayed': True}
            if saved.get('selected_project_id'):
                raise ContractError('task_classification_managed_project_already_selected')
            current = self.membership(kind, raw['session_id'])
            if expected != (current['version'] if current else 0):
                raise ContractError('task_classification_membership_changed_bootstrap_required')
            created = False
            reused = False
            if raw.get('project_id'):
                if 'sections' in raw or 'display_name' in raw or 'project_key' in raw:
                    raise ContractError('task_classification_attach_does_not_replace_documents')
                project = self.registry.get(raw['project_id'], include_archived=False)
                if self.context.documents.profile(project)['registration_state'] != 'registered':
                    raise ContractError('task_classification_target_needs_full_dossier')
                if not project.auto_monitor_enabled:
                    raise ContractError('task_context_project_disabled')
            else:
                name = raw.get('display_name')
                if not isinstance(name, str) or not name.strip() or len(name) > 160:
                    raise ContractError('project_display_name_required')
                sections = self.context.service.organization._validate_refresh_sections(body.get('sections'))
                project_key = body.get('project_key')
                if not isinstance(project_key, str) or not project_key.strip() or len(project_key) > 2048:
                    raise ContractError('task_classification_project_key_required_for_create')
                project_key = project_key.strip()
                existing = conn.execute('SELECT project_id FROM task_project_keys WHERE project_key=?', (project_key,)).fetchone()
                project = self.registry.get(existing[0], include_archived=True) if existing else None
                if project is not None and project.status != 'archived':
                    if not project.auto_monitor_enabled:
                        raise ContractError('task_context_project_disabled')
                    reused = True
                else:
                    identifier = 'curated-' + hashlib.sha256(raw['request_id'].encode()).hexdigest()[:24]
                    folder = self.context.service.data_dir / 'project-containers' / identifier
                    folder.mkdir(parents=True, exist_ok=True)
                    project, _ = self.registry.register(project_id=identifier, display_name=name, root_path=folder,
                        source='curated_project', extra={'logical_container': True, 'project_key': project_key})
                    self.context.documents.update(project.project_id, raw['session_id'], raw['receipt_id'],
                        {'request_id': 'classification-document-' + hashlib.sha256(raw['request_id'].encode()).hexdigest(),
                         'expected_revision': 0, 'sections': sections})
                    conn.execute('INSERT OR REPLACE INTO task_project_keys VALUES (?, ?)', (project_key, project.project_id))
                    created = True
            version = expected + 1
            conn.execute('INSERT OR REPLACE INTO task_project_memberships VALUES (?, ?, ?, ?, ?)',
                         (kind, raw['session_id'], project.project_id, version, utc_now()))
            if kind == 'codex':
                # The same explicit task identity drives both future context
                # and source monitoring. Keep cursors and assignment ancestry.
                for source in self.registry.sources_for_session(raw['session_id']):
                    self.registry.assign_source(
                        request_id='task-classify-source-' + hashlib.sha256((raw['request_id'] + source.source_key).encode()).hexdigest(),
                        source_key=source.source_key, session_id=source.session_id, project_id=project.project_id,
                        confidence=None, rationale=raw['rationale'], evidence_refs=raw['evidence_refs'], actor='codex')
            result = {'ok': True, 'project_id': project.project_id, 'client_kind': kind,
                      'session_id': raw['session_id'], 'membership_version': version, 'replayed': False,
                      'created': created, 'existing_project_reused': reused,
                      'previous_project_id': current['project_id'] if current else None,
                      'revision': (self.context.documents.latest(project.project_id) or {}).get('revision', 0),
                      'next_action': '重新ap_vibe_context获取新归属收据并回读档案；existing_project_reused=true表示已复用相同入口的项目，本次提案未覆盖它，需按真实增量合并。旧收据仍可读。'}
            conn.execute('INSERT INTO task_project_changes VALUES (?, ?, ?, ?)',
                         (raw['request_id'], fingerprint, encoded(body), encoded(result)))
            return result
