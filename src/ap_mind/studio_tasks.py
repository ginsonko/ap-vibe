"""Durable logical tasks over managed runs, with atomic ownership and dispatch.

Runs are attempts; tasks retain goals, history and handoff context. Dispatch IDs
are saved before launch and reused during reconciliation, never reinvented.
"""
from contextlib import closing
from datetime import datetime
import hashlib
import json
import threading
import uuid

from .contracts import ContractError, utc_now


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def required(raw, name, length=16000):
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > length:
        raise ContractError('studio_task_' + name + '_required')
    return value.strip()


class StudioTasks:
    def __init__(self, studio):
        self.studio = studio
        self.registry = studio.registry
        self.lock = threading.RLock()
        from .studio_verdicts import StudioVerdicts
        self.verdicts = StudioVerdicts(self)
        with closing(self.registry._connect()) as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS studio_tasks (
                    task_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                    state TEXT NOT NULL, payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS studio_task_requests (
                    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS studio_task_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    kind TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL);
            ''')

    def _read(self, c, task_id):
        row = c.execute('SELECT * FROM studio_tasks WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            raise ContractError('studio_task_not_found')
        return {**json.loads(row['payload_json']), 'task_id': task_id, 'version': row['version'], 'state': row['state']}

    def _write(self, c, task, kind, detail):
        task = {**task, 'version': task['version'] + 1, 'updated_at': utc_now()}
        c.execute('INSERT OR REPLACE INTO studio_tasks VALUES (?,?,?,?)',
                  (task['task_id'], task['version'], task['state'], encoded(task)))
        c.execute('INSERT INTO studio_task_events(task_id,kind,created_at,payload_json) VALUES (?,?,?,?)',
                  (task['task_id'], kind, utc_now(), encoded(detail)))
        return task

    def _request(self, c, request_id, raw):
        fingerprint = hashlib.sha256(encoded(raw).encode()).hexdigest()
        old = c.execute('SELECT * FROM studio_task_requests WHERE request_id=?', (request_id,)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint:
                raise ContractError('studio_task_request_conflict')
            return fingerprint, {**json.loads(old['result_json']), 'replayed': True}
        return fingerprint, None

    def _receipt(self, c, request_id, fingerprint, task):
        result = {'ok': True, 'task': task, 'replayed': False}
        c.execute('INSERT INTO studio_task_requests VALUES (?,?,?)', (request_id, fingerprint, encoded(result)))
        return result

    def list(self, task_id=None, *, project_id=None, state=None, offset=0, limit=200, compact=False):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ContractError('studio_task_page_invalid')
        filters, params = [], []
        for name, value in (('task_id', task_id), ("json_extract(payload_json,'$.project_id')", project_id), ('state', state)):
            if value:
                filters.append(name + '=?'); params.append(value)
        where = 'WHERE ' + ' AND '.join(filters) if filters else ''
        with closing(self.registry._connect()) as c:
            rows = c.execute('SELECT task_id FROM studio_tasks ' + where +
                             ' ORDER BY rowid DESC LIMIT ? OFFSET ?', (*params, limit + 1, offset)).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            tasks = [self._read(c, row[0]) for row in rows]
            events = c.execute('SELECT * FROM studio_task_events WHERE task_id=? ORDER BY seq DESC LIMIT 100',
                               (task_id,)).fetchall() if task_id else []
        if compact and not task_id:
            tasks = [{key: t.get(key) for key in ('task_id','version','state','project_id','title','owner','assignment_epoch',
                'run_id','dependencies','eligible_agents','reviewer_agent_id','review_task_id','auto_run','updated_at')}
                | {'goal': t['goal'][:240], 'read_tool':'ap_vibe_task_list'} for t in tasks]
        return {'ok': True, 'tasks': tasks, 'events': [dict(row) | {'payload': json.loads(row['payload_json'])} for row in reversed(events)],
                'next_offset': offset + limit if has_more else None}

    def _scheduled(self):
        # UI pagination must never prevent older work from progressing.
        with closing(self.registry._connect()) as c:
            rows = c.execute("SELECT task_id FROM studio_tasks WHERE state != 'archived' ORDER BY rowid").fetchall()
            return [self._read(c, row[0]) for row in rows]

    def _ready(self, c, task):
        review_parent = task.get('review_of_task_id')
        for dep in task['dependencies']:
            parent = self._read(c, dep)
            if parent['project_id'] != task['project_id']:
                return False
            if review_parent == dep:
                run = c.execute('SELECT state FROM studio_runs WHERE run_id=?', (task.get('review_source_run_id'),)).fetchone()
                if (parent.get('review_task_id') != task['task_id'] or
                        parent.get('run_id') != task.get('review_source_run_id') or
                        parent['assignment_epoch'] != task.get('review_source_epoch') or
                        parent['state'] not in {'waiting_review', 'completed'} or
                        not run or run['state'] not in {'awaiting_review', 'completed'}):
                    return False
            elif parent['state'] != 'completed':
                return False
        return True

    def _dependencies(self, c, task_id, dependencies, project_id):
        pending = list(dependencies)
        visited = set()
        for parent in dependencies:
            if self._read(c, parent)['project_id'] != project_id:
                raise ContractError('studio_task_dependency_project_mismatch')
        while pending:
            current = pending.pop()
            if current == task_id:
                raise ContractError('studio_task_dependency_cycle')
            if current in visited:
                continue
            visited.add(current)
            pending.extend(self._read(c, current).get('dependencies', []))

    def save(self, raw):
        if any(raw.get(key) is not None for key in ('review_of_task_id', 'review_source_run_id', 'review_source_epoch', 'review_task_id')):
            raise ContractError('studio_task_review_link_server_managed')
        request_id = required(raw, 'request_id', 200)
        project_id = required(raw, 'project_id', 200)
        self.registry.get(project_id, include_archived=False)
        fields = {name: required(raw, name) for name in ('title', 'goal', 'acceptance')}
        if 'extensions' in raw:
            from .studio_extensions import extension_ids
            fields['extensions'] = extension_ids(raw)
        for name in ('dependencies', 'eligible_agents', 'resources', 'tags'):
            values = raw.get(name, [])
            if not isinstance(values, list) or any(not isinstance(x, str) or not x.strip() or len(x) > 1000 for x in values):
                raise ContractError('studio_task_' + name + '_invalid')
            fields[name] = list(dict.fromkeys(values))
        reviewer_agent_id = raw.get('reviewer_agent_id')
        if reviewer_agent_id is not None:
            if not isinstance(reviewer_agent_id, str) or not reviewer_agent_id.strip() or len(reviewer_agent_id) > 100:
                raise ContractError('studio_task_reviewer_agent_invalid')
            reviewer_agent_id = reviewer_agent_id.strip()
            with closing(self.registry._connect()) as check:
                reviewer = check.execute('SELECT public_json FROM studio_agents WHERE agent_id=?', (reviewer_agent_id,)).fetchone()
            if reviewer is None or json.loads(reviewer[0]).get('archived'):
                raise ContractError('studio_task_reviewer_agent_not_found')
        fields['reviewer_agent_id'] = reviewer_agent_id
        rounds = raw.get('max_rework_rounds', 2)
        if type(rounds) is not int or rounds < 0:
            raise ContractError('studio_task_rework_rounds_invalid')
        fields['max_rework_rounds'] = rounds
        review_retries = raw.get('max_review_retries', 1)
        if type(review_retries) is not int or review_retries < 0:
            raise ContractError('studio_task_review_retries_invalid')
        fields['max_review_retries'] = review_retries
        author_retries = raw.get('max_author_retries', 1)
        if type(author_retries) is not int or author_retries < 0:
            raise ContractError('studio_task_author_retries_invalid')
        fields['max_author_retries'] = author_retries
        max_turns = raw.get('max_turns')
        if max_turns is not None and (type(max_turns) is not int or max_turns < 1):
            raise ContractError('agent_max_turns_invalid')
        fields['max_turns'] = max_turns
        if reviewer_agent_id and fields['eligible_agents'] == [reviewer_agent_id]:
            raise ContractError('studio_task_reviewer_must_differ')
        auto_run = raw.get('auto_run', False)
        if type(auto_run) is not bool:
            raise ContractError('studio_task_auto_run_invalid')
        if auto_run and not fields['eligible_agents']:
            raise ContractError('studio_task_eligible_agents_required')
        with self.lock, self.registry.transaction():
            c = self.registry._connect()
            fingerprint, old = self._request(c, request_id, raw)
            if old: return old
            for agent_id in fields['eligible_agents']:
                candidate = c.execute('SELECT public_json FROM studio_agents WHERE agent_id=?', (agent_id,)).fetchone()
                if not candidate or json.loads(candidate[0]).get('archived'):
                    raise ContractError('studio_task_candidate_not_found')
            task_id = raw.get('task_id') or 'task-' + uuid.uuid4().hex
            task = self._read(c, task_id) if raw.get('task_id') else {
                'task_id': task_id, 'version': 0, 'state': 'queued', 'project_id': project_id,
                'owner': None, 'assignment_epoch': 0, 'run_id': None, 'attempts': [], 'created_at': utc_now()}
            if task['project_id'] != project_id:
                raise ContractError('studio_task_project_immutable')
            if task['version'] != raw.get('expected_version', 0):
                raise ContractError('studio_task_version_conflict')
            if task.get('review_of_task_id'):
                raise ContractError('studio_task_review_link_server_managed')
            if task['state'] in {'dispatching', 'running', 'waiting_review'}:
                raise ContractError('studio_task_active_edit_conflict')
            self._dependencies(c, task_id, fields['dependencies'], project_id)
            task.update(fields, auto_run=auto_run)
            task = self._write(c, task, 'saved', {'title': task['title'], 'requested_by':raw.get('requested_by')})
            return self._receipt(c, request_id, fingerprint, task)

    def claim(self, raw):
        request_id = required(raw, 'request_id', 200)
        agent_id = required(raw, 'agent_id', 100)
        task_id = required(raw, 'task_id', 100)
        with self.lock, self.registry.transaction():
            c = self.registry._connect()
            fingerprint, old = self._request(c, request_id, raw)
            if old: return old
            task = self._read(c, task_id)
            if task['version'] != raw.get('expected_version'):
                raise ContractError('studio_task_version_conflict')
            if task['state'] != 'queued' or task.get('owner'):
                raise ContractError('studio_task_already_owned')
            if not self._ready(c, task):
                raise ContractError('studio_task_dependencies_pending')
            if agent_id == task.get('reviewer_agent_id'):
                raise ContractError('studio_task_reviewer_must_differ')
            if task.get('review_of_task_id') and agent_id not in task['eligible_agents']:
                raise ContractError('studio_task_review_agent_mismatch')
            agent = c.execute('SELECT public_json FROM studio_agents WHERE agent_id=?', (agent_id,)).fetchone()
            if agent is None or json.loads(agent[0]).get('archived'):
                raise ContractError('agent_not_found')
            owners = c.execute("SELECT payload_json FROM studio_tasks WHERE state IN ('dispatching','running')").fetchall()
            for row in owners:
                other = json.loads(row[0])
                if other.get('owner') == agent_id:
                    raise ContractError('studio_task_agent_busy')
                if set(task['resources']) & set(other.get('resources', [])):
                    raise ContractError('studio_task_resource_busy')
            active_run = c.execute("SELECT 1 FROM studio_runs WHERE agent_id=? AND state IN ('starting','running','cancelling')", (agent_id,)).fetchone()
            if active_run:
                raise ContractError('studio_task_agent_busy')
            task.update(owner=agent_id, assignment_epoch=task['assignment_epoch'] + 1, state='dispatching', run_id=None)
            task['dispatch_request_id'] = f"{task_id}:epoch:{task['assignment_epoch']}"
            task = self._write(c, task, 'claimed', {'agent_id': agent_id, 'epoch': task['assignment_epoch'], 'requested_by':raw.get('requested_by')})
            return self._receipt(c, request_id, fingerprint, task)

    def check_owner(self, c, task_id, epoch, agent_id):
        task = self._read(c, task_id)
        if task['assignment_epoch'] != epoch or task['owner'] != agent_id or task['state'] != 'dispatching':
            raise ContractError('studio_task_owner_changed')
        if not self._ready(c, task):
            raise ContractError('studio_task_dependencies_pending')
        return task

    def dispatch(self, task_id):
        with self.lock:
            task = self.list(task_id)['tasks'][0]
            if task['state'] != 'dispatching': return None
            prompt = (f"任务：{task['title']}\n目标：{task['goal']}\n验收条件：{task['acceptance']}\n"
                      f"这是逻辑任务 {task_id} 的第 {task['assignment_epoch']} 次分工。"
                      '按真实文件交付，报告执行检查与未完成项，不编造验收。')
            request = {'request_id': task['dispatch_request_id'], 'agent_id': task['owner'],
                       'project_id': task['project_id'], 'prompt': prompt,
                       'logical_task_id': task_id, 'assignment_epoch': task['assignment_epoch']}
            if task.get('max_turns'):
                request['max_turns'] = task['max_turns']
            if 'extensions' in task:
                request['extensions'] = task['extensions']
            dependencies = ([task['review_source_run_id']] if task.get('review_of_task_id') else
                            [self.list(dep)['tasks'][0].get('run_id') for dep in task['dependencies']])
            if dependencies:
                request['depends_on'] = [item for item in dependencies if item]
            if task.get('handoff_run_id'):
                source = self.studio.runs(task['handoff_run_id'])['runs'][0]
                if not self.studio.execution_stopped({**source, 'run_id': task['handoff_run_id']}):
                    return None
                request['handoff_from_run_id'] = task['handoff_run_id']
                if task.get('handoff_mode') == 'copy':
                    request['fork_workspace'] = True
                request['prompt'] += '\n接手说明：' + task.get('handoff_note', '')
            started = self.studio.start(request)
            with self.registry.transaction():
                c = self.registry._connect()
                current = self._read(c, task_id)
                self.check_owner(c, task_id, task['assignment_epoch'], task['owner'])
                attempt = {'run_id': started['run_id'], 'agent_id': task['owner'], 'epoch': task['assignment_epoch']}
                current.update(state='running', run_id=started['run_id'], attempts=[*current['attempts'], attempt])
                return self._write(c, current, 'dispatched', attempt)

    def release(self, raw):
        """Return verified stopped work to the queue with its original artifact entry."""
        request_id = required(raw, 'request_id', 200)
        note = required(raw, 'note', 4000)
        task_id = required(raw, 'task_id', 100)
        with self.lock, self.registry.transaction():
            c = self.registry._connect()
            fingerprint, old = self._request(c, request_id, raw)
            if old: return old
            task = self._read(c, task_id)
            if task['version'] != raw.get('expected_version'):
                raise ContractError('studio_task_version_conflict')
            if task['state'] not in {'needs_help', 'changes_requested', 'paused'}:
                raise ContractError('studio_task_release_requires_stopped')
            run = self.studio.runs(task['run_id'])['runs'][0] if task.get('run_id') else None
            if run and not self.studio.execution_stopped({**run, 'run_id': task['run_id']}):
                raise ContractError('studio_task_process_reconciliation_required')
            task.update(owner=None, state='queued', handoff_run_id=task.get('run_id'), handoff_note=note,
                        handoff_mode='copy', rework_pending=False, takeover_tried_agents=[], takeover_issue=None,
                        author_retry_count=0)
            task = self._write(c, task, 'released', {'note': note, 'from_run_id': task.get('run_id'), 'requested_by':raw.get('requested_by')})
            return self._receipt(c, request_id, fingerprint, task)

    def archive(self, raw):
        task_id = required(raw, 'task_id', 100)
        request_id = required(raw, 'request_id', 200) if 'request_id' in raw else None
        with self.lock, self.registry.transaction():
            c = self.registry._connect()
            if request_id:
                fingerprint, old = self._request(c, request_id, raw)
                if old: return old
            task = self._read(c, task_id)
            if task['version'] != raw.get('expected_version'):
                raise ContractError('studio_task_version_conflict')
            if task['state'] in {'dispatching', 'running'}:
                raise ContractError('studio_task_release_requires_stopped')
            if task.get('run_id'):
                runs = self.studio.runs(task['run_id'])['runs']
                if runs and not self.studio.execution_stopped(runs[0]):
                    raise ContractError('studio_task_process_reconciliation_required')
            task = self._write(c, {**task, 'state': 'archived', 'auto_run': False}, 'archived',
                               {'history_retained': True, 'requested_by':raw.get('requested_by')})
            if request_id:
                return self._receipt(c, request_id, fingerprint, task)
            return {'ok': True, 'task': task}

    def tick(self):
        """Local reconciliation and dispatch; no model calls for polling."""
        with self.lock:
            for task in self._scheduled():
                if task['state'] == 'running' and task.get('run_id'):
                    runs = self.studio.runs(task['run_id'])['runs']
                    if not runs: continue
                    state = runs[0]['state']
                    mapped = {'awaiting_review': 'waiting_review', 'completed': 'completed',
                              'changes_requested': 'changes_requested', 'failed': 'needs_help',
                              'uncertain': 'needs_help', 'interrupted': 'needs_help', 'cancelled': 'paused'}.get(state)
                elif task['state'] in {'waiting_review', 'changes_requested', 'completed'} and task.get('run_id'):
                    runs = self.studio.runs(task['run_id'])['runs']
                    if not runs: continue
                    run = runs[0]
                    mapped = {'completed': 'completed', 'changes_requested': 'changes_requested'}.get(run['state'])
                else: mapped = None
                if mapped and mapped != task['state']:
                    with self.registry.transaction():
                        c = self.registry._connect()
                        current = self._read(c, task['task_id'])
                        if current['version'] == task['version']:
                            self._write(c, {**current, 'state': mapped}, 'run_state', {'state': mapped, 'run_id': task['run_id']})
            for task in self._scheduled():
                self.verdicts.reconcile(task)
                self.verdicts.recover_check(task)
                self._recover_author(task)
            # Reconcile review registration independently from the state
            # transition. A crash between those writes must not lose the
            # follow-up task; save() makes this pass idempotent.
            for task in self._scheduled():
                if task['state'] in {'waiting_review', 'completed'} and task.get('reviewer_agent_id'):
                    self._ensure_review_task(task)
            for task in self._scheduled():
                if task['state'] == 'queued' and (task.get('auto_run') or task.get('rework_pending')):
                    candidates = task['eligible_agents']
                    if task.get('rework_pending') and task.get('rework_agent_id') in candidates:
                        candidates = [task['rework_agent_id'], *[a for a in candidates if a != task['rework_agent_id']]]
                    for agent_id in candidates:
                        if agent_id in task.get('takeover_tried_agents', []):
                            continue
                        try:
                            self.claim({'request_id': f"auto:{task['task_id']}:{task['version']}:{agent_id}",
                                        'task_id': task['task_id'], 'agent_id': agent_id, 'expected_version': task['version']})
                            break
                        except ContractError:
                            continue
                current = self.list(task['task_id'])['tasks'][0]
                if current['state'] == 'dispatching':
                    try:
                        self.dispatch(current['task_id'])
                    except Exception as exc:
                        from .agent_studio import redact
                        with self.registry.transaction():
                            c = self.registry._connect(); latest = self._read(c, current['task_id'])
                            if latest['state'] == 'dispatching':
                                self._write(c, {**latest, 'state': 'needs_help'}, 'dispatch_failed', {'reason': redact(str(exc))[:1000]})
            return {'ok': True}

    def _recover_author(self, task):
        if task['state'] != 'needs_help' or not task.get('auto_run') or task.get('review_of_task_id'):
            return
        with self.registry.transaction():
            c = self.registry._connect()
            current = self._read(c, task['task_id'])
            if current['state'] != 'needs_help' or not current.get('run_id'):
                return
            run = self.studio.runs(current['run_id'])['runs'][0]
            if run['state'] not in {'failed', 'uncertain'} or not self.studio.execution_stopped(run):
                return
            tried = list(dict.fromkeys([*current.get('takeover_tried_agents', []), current['owner']]))
            available = {row['agent_id'] for row in c.execute('SELECT agent_id,public_json FROM studio_agents')
                         if not json.loads(row['public_json']).get('archived')}
            alternatives = [a for a in current.get('eligible_agents', [])
                            if a not in tried and a in available and a != current.get('reviewer_agent_id')]
            retries = current.get('author_retry_count', 0)
            retrying = False
            if (not alternatives and retries < current.get('max_author_retries', 0) and
                    current['owner'] in current.get('eligible_agents', []) and
                    current['owner'] != current.get('reviewer_agent_id')):
                agent = c.execute('SELECT public_json FROM studio_agents WHERE agent_id=?', (current['owner'],)).fetchone()
                if agent and not json.loads(agent[0]).get('archived'):
                    alternatives = [current['owner']]
                    tried = [a for a in tried if a != current['owner']]
                    retries += 1
                    retrying = True
            if not alternatives:
                issue = '本轮可用候选均已尝试，已有成果和失败原因已保留；可编辑候选或手动继续。'
                if current.get('takeover_issue') != issue:
                    self._write(c, {**current, 'takeover_issue': issue, 'takeover_tried_agents': tried},
                                'takeover_exhausted', {'reason': issue})
                return
            note = '原执行进程已退出。先读取交接文件、已有成果和公开进展，在新目录完成剩余工作；先查询未知外部操作的结果，不直接重放。'
            self._write(c, {**current, 'state': 'queued', 'owner': None, 'rework_pending': False,
                'handoff_mode': 'copy', 'handoff_run_id': current['run_id'], 'handoff_note': note,
                'takeover_tried_agents': tried, 'takeover_issue': None,
                'author_retry_count': retries}, 'author_recovery' if retrying else 'auto_handoff',
                {'from_agent': current['owner'], 'run_id': current['run_id'], 'candidates': alternatives,
                 'exit_code': run.get('exit_code'), 'original_state': run['state'], 'note': note, 'retry': retries})

    def _ensure_review_task(self, task):
        """Atomically link one reviewer to the exact returned attempt.

        Reconciliation repeats after restart; it neither needs a human to
        pre-accept the result nor marks the author's work accepted itself.
        """
        with self.registry.transaction():
            c = self.registry._connect()
            task = self._read(c, task['task_id'])
            reviewer = task.get('reviewer_agent_id')
            if not reviewer or task.get('review_of_task_id') or task['state'] not in {'waiting_review', 'completed'}:
                return None
            if task.get('review_task_epoch') == task['assignment_epoch'] and task.get('review_task_id'):
                return self._read(c, task['review_task_id'])
            agent = c.execute('SELECT public_json FROM studio_agents WHERE agent_id=?', (reviewer,)).fetchone()
            reason = ('验收伙伴已归档或不存在，请在伙伴配置中恢复。' if not agent or json.loads(agent[0]).get('archived') else
                      '验收伙伴与原作者相同，请配置另一位伙伴。' if reviewer == task.get('owner') else None)
            if reason:
                if task.get('review_issue') != reason:
                    self._write(c, {**task, 'review_issue': reason}, 'review_unavailable', {'reason': reason})
                return None
            source = c.execute('SELECT state FROM studio_runs WHERE run_id=?', (task.get('run_id'),)).fetchone()
            if not source or source['state'] not in {'awaiting_review', 'completed'}:
                return None
            request_id = f"review:{task['task_id']}:epoch:{task['assignment_epoch']}"
            review_id = 'task-review-' + hashlib.sha256(request_id.encode()).hexdigest()[:32]
            review_task = {
                'task_id': review_id, 'version': 0, 'state': 'queued',
                'project_id': task['project_id'], 'title': f"验收：{task['title']}",
                'goal': ('读取 ap-vibe-dependency.json，再读取其中的真实成果文件。'
                         '对照以下原任务逐条独立检查，将通过项、问题、引用证据和未完成项写入 acceptance.md 并回读。'
                         '仅在本次成果目录写验收报告，保留原作者文件。无需修改长期项目档案。'
                         '最后必须调用 ap_vibe_review_submit 提交结构化裁决：outcome 为 accepted、changes_requested 或 inconclusive；'
                         'report_path=acceptance.md；source_files 是实际检查过的原成果相对路径；checks 逐项写 criterion、status(passed/failed/unknown)、evidence，'
                         '失败项必须写 required_change。无法判断就如实 unknown/inconclusive，不能虚假通过。request_id 固定且唯一，提交成功后结束本回合，勿再修改报告和原成果。\n'
                         f"原任务：{task['title']}\n目标：{task['goal']}\n完成标准：{task['acceptance']}"),
                'acceptance': '实际读取上游文件；acceptance.md 有逐项结论与证据，明确未知；不只依据执行器自述。',
                'dependencies': [task['task_id']], 'eligible_agents': [reviewer],
                'resources': task.get('resources', []), 'tags': list(dict.fromkeys([*task.get('tags', []), 'review'])),
                'review_of_task_id': task['task_id'], 'review_source_run_id': task['run_id'],
                'review_source_epoch': task['assignment_epoch'], 'reviewer_agent_id': None,
                'auto_run': True, 'owner': None, 'assignment_epoch': 0, 'run_id': None,
                'attempts': [], 'created_at': utc_now(),
            }
            created = self._write(c, review_task, 'review_created', {'parent_task_id': task['task_id'], 'source_run_id': task['run_id']})
            history = [*task.get('review_task_history', []), {'task_id': review_id, 'source_run_id': task['run_id'], 'epoch': task['assignment_epoch']}]
            self._write(c, {**task, 'review_task_id': review_id, 'review_task_epoch': task['assignment_epoch'],
                            'review_task_history': history, 'review_issue': None}, 'review_linked', {'review_task_id': review_id})
            self._receipt(c, request_id, hashlib.sha256(encoded(review_task).encode()).hexdigest(), created)
            return created
