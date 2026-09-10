"""Evidence-bound reviewer results and atomic automatic rework transitions."""
import hashlib
import json

from .contracts import ContractError, utc_now
from .studio_artifacts import preview_text
from .studio_tasks import encoded, required


class StudioVerdicts:
    def __init__(self, tasks):
        self.tasks = tasks
        self.studio = tasks.studio

    def _run(self, c, run_id):
        row = c.execute('SELECT state,payload_json FROM studio_runs WHERE run_id=?', (run_id,)).fetchone()
        if not row:
            raise ContractError('agent_run_not_found')
        return {**json.loads(row['payload_json']), 'run_id': run_id, 'state': row['state']}

    def _file(self, run_id, name, report=False):
        artifacts = self.studio.artifacts
        if report:
            value = artifacts.read(run_id, name)
            if not value.get('sha256') or not (value.get('text') or '').strip():
                raise ContractError('studio_review_readable_report_required')
            return {'run_id': run_id, 'name': value['name'], 'sha256': value['sha256']}
        path, name = artifacts._resolve(artifacts._workspace(run_id), name)
        try:
            with path.open('rb') as stream:
                before = path.stat()
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ContractError('studio_review_evidence_changed')
        except OSError:
            raise ContractError('studio_review_source_file_unreadable') from None
        return {'run_id': run_id, 'name': name, 'sha256': digest}

    def submit(self, raw):
        request_id = required(raw, 'request_id', 200)
        run_id = required(raw, 'run_id', 100)
        agent_id = required(raw, 'agent_id', 100)
        outcome = raw.get('outcome')
        if outcome not in {'accepted', 'changes_requested', 'inconclusive'}:
            raise ContractError('studio_review_outcome_invalid')
        note = preview_text(required(raw, 'note', 4000))
        checks = raw.get('checks')
        if not isinstance(checks, list) or not checks or len(checks) > 100:
            raise ContractError('studio_review_checks_required')
        clean = []
        for check in checks:
            if not isinstance(check, dict) or check.get('status') not in {'passed', 'failed', 'unknown'}:
                raise ContractError('studio_review_check_status_invalid')
            item = {k: preview_text(required(check, k, 4000)) for k in ('criterion', 'evidence')}
            item['status'] = check['status']
            if check['status'] == 'failed':
                item['required_change'] = preview_text(required(check, 'required_change', 4000))
            clean.append(item)
        if outcome == 'accepted' and any(item['status'] != 'passed' for item in clean):
            raise ContractError('studio_review_accept_requires_passed_checks')
        if outcome == 'changes_requested' and not any(item['status'] == 'failed' for item in clean):
            raise ContractError('studio_review_changes_require_failed_check')
        names = raw.get('source_files', [])
        if (not isinstance(names, list) or len(names) > 100 or
                any(not isinstance(name, str) for name in names) or
                (outcome != 'inconclusive' and not names)):
            raise ContractError('studio_review_source_files_required')
        with self.tasks.lock, self.tasks.registry.transaction():
            c = self.tasks.registry._connect()
            fingerprint, old = self.tasks._request(c, request_id, raw)
            if old:
                return old
            run = self._run(c, run_id)
            if run['agent_id'] != agent_id or not run.get('logical_task_id'):
                raise ContractError('studio_review_assigned_reviewer_required')
            task = self.tasks._read(c, run['logical_task_id'])
            if (not task.get('review_of_task_id') or task.get('owner') != agent_id or
                    task.get('run_id') != run_id or task['assignment_epoch'] != run.get('assignment_epoch') or
                    task['state'] not in {'running', 'waiting_review'} or
                    run['state'] not in {'starting', 'running', 'awaiting_review'}):
                raise ContractError('studio_review_assigned_reviewer_required')
            parent = self.tasks._read(c, task['review_of_task_id'])
            self._current(parent, task)
            source = self._run(c, parent['run_id'])
            if source['state'] != 'awaiting_review':
                raise ContractError('studio_review_source_already_decided')
            if task.get('verdict'):
                raise ContractError('studio_review_verdict_already_submitted')
            verdict = {'request_id': request_id, 'outcome': outcome, 'note': note, 'checks': clean,
                       'reviewer_agent_id': agent_id, 'reviewer_run_id': run_id,
                       'source_run_id': parent['run_id'], 'source_epoch': parent['assignment_epoch'],
                       'source_review_revision': len(source.get('review_history', [])),
                       'report': self._file(run_id, required(raw, 'report_path', 2000), report=True),
                       'source_files': [self._file(parent['run_id'], name) for name in dict.fromkeys(names)],
                       'submitted_at': utc_now(), 'status': 'pending'}
            task = self.tasks._write(c, {**task, 'verdict': verdict}, 'verdict_submitted', verdict)
            return self.tasks._receipt(c, request_id, fingerprint, task)

    @staticmethod
    def _current(parent, task):
        if (parent.get('review_task_id') != task['task_id'] or
                parent.get('run_id') != task['review_source_run_id'] or
                parent['assignment_epoch'] != task['review_source_epoch'] or
                parent['state'] != 'waiting_review' or
                parent['project_id'] != task['project_id']):
            raise ContractError('studio_review_source_changed')

    def _write_run(self, c, run, state, record):
        history = run.get('review_history', [])
        record = {**record, 'revision': len(history) + 1, 'created_at': utc_now()}
        value = {k: v for k, v in run.items() if k not in {'run_id', 'state'}}
        value.update(review=record, review_history=[*history, record], updated_at=utc_now(),
                     verification='reviewer_accepted' if state == 'completed' else 'reviewer_changes_requested')
        c.execute('UPDATE studio_runs SET state=?,payload_json=? WHERE run_id=?', (state, encoded(value), run['run_id']))
        c.execute('INSERT INTO studio_events(run_id,created_at,kind,payload_json) VALUES (?,?,?,?)',
                  (run['run_id'], utc_now(), 'review', encoded({'text': record['note'], 'reviewer': record['reviewer']})))

    def reconcile(self, task):
        if task.get('verdict', {}).get('status') != 'pending':
            return
        try:
            with self.tasks.registry.transaction():
                c = self.tasks.registry._connect()
                task = self.tasks._read(c, task['task_id'])
                verdict = task['verdict']
                if verdict['status'] != 'pending':
                    return
                run = self._run(c, verdict['reviewer_run_id'])
                if run['state'] in {'starting', 'running', 'waiting', 'cancelling'}:
                    return
                # The committed verdict is the result. A lost final chat reply
                # must not replay review work once this exact process has exited.
                recovered = (run['state'] in {'failed', 'uncertain'} and
                             type(run.get('exit_code')) is int and
                             run['run_id'] not in self.studio._threads and
                             run['run_id'] not in self.studio._processes)
                if run['state'] != 'awaiting_review' and not recovered:
                    raise ContractError('studio_review_reviewer_did_not_finish_normally')
                allowed_states = {'running', 'waiting_review', 'needs_help'} if recovered else {'running', 'waiting_review'}
                if (task.get('run_id') != run['run_id'] or task['state'] not in allowed_states or
                        task.get('owner') != verdict['reviewer_agent_id'] or
                        task['assignment_epoch'] != run.get('assignment_epoch')):
                    raise ContractError('studio_review_reviewer_changed')
                parent = self.tasks._read(c, task['review_of_task_id'])
                self._current(parent, task)
                source = self._run(c, verdict['source_run_id'])
                if source['state'] != 'awaiting_review' or len(source.get('review_history', [])) != verdict['source_review_revision']:
                    raise ContractError('studio_review_source_already_decided')
                for ref in [verdict['report'], *verdict['source_files']]:
                    if self._file(ref['run_id'], ref['name'])['sha256'] != ref['sha256']:
                        raise ContractError('studio_review_evidence_changed')
                if recovered:
                    verdict = {**verdict, 'recovered_after_executor_exit': {
                        'state': run['state'], 'exit_code': run['exit_code'],
                        'error': preview_text(run.get('error') or '')[:1500],
                        'provider_request_replayed': False}}
                outcome = verdict['outcome']
                record = {'request_id': verdict['request_id'], 'accepted': outcome == 'accepted',
                          'reviewer': verdict['reviewer_agent_id'], 'note': verdict['note'],
                          'evidence_refs': [verdict['report']['name']], 'verdict': verdict}
                if outcome != 'inconclusive':
                    self._write_run(c, source, 'completed' if outcome == 'accepted' else 'changes_requested', record)
                self._write_run(c, run, 'completed', {**record, 'accepted': True,
                    'note': '结构化验收报告已应用；原成果结论：' + outcome})
                applied = {**verdict, 'status': 'applied', 'applied_at': utc_now()}
                self.tasks._write(c, {**task, 'state': 'completed', 'verdict': applied, 'review_issue': None}, 'verdict_applied', applied)
                parent.update(last_verdict=applied, verdict_history=[*parent.get('verdict_history', []), applied], review_issue=None)
                if outcome == 'accepted':
                    parent.update(state='completed', rework_pending=False)
                elif outcome == 'changes_requested':
                    rounds = parent.get('rework_round', 0)
                    parent.update(state='changes_requested', rework_pending=False)
                    if rounds < parent.get('max_rework_rounds', 2):
                        parent.update(state='queued', owner=None, rework_round=rounds + 1, rework_pending=True,
                                      takeover_tried_agents=[], takeover_issue=None, author_retry_count=0,
                                      rework_agent_id=source['agent_id'],
                                      handoff_run_id=source['run_id'], handoff_mode='copy',
                                      handoff_note='按验收意见在新目录修订，旧成果保留。先 Read 验收报告：' +
                                      str(self.studio.artifacts._workspace(run['run_id']) / verdict['report']['name']) +
                                      '\n修改摘要：' + verdict['note'])
                    else:
                        parent['review_issue'] = '已达到本任务自动返工次数，成果和问题清单已保留，可手动继续分配。'
                else:
                    parent['review_issue'] = '验收暂时无法判断：' + verdict['note']
                self.tasks._write(c, parent, 'review_conclusion', applied)
        except ContractError as exc:
            with self.tasks.registry.transaction():
                c = self.tasks.registry._connect()
                current = self.tasks._read(c, task['task_id'])
                issue = '验收结论尚未应用：' + str(exc)
                if current.get('review_issue') != issue:
                    self.tasks._write(c, {**current, 'review_issue': issue}, 'verdict_deferred', {'reason': issue})

    def recover_check(self, task):
        if not task.get('review_of_task_id') or task['state'] != 'needs_help' or task.get('verdict'):
            return
        with self.tasks.registry.transaction():
            c = self.tasks.registry._connect()
            current = self.tasks._read(c, task['task_id'])
            if current['state'] != 'needs_help' or current.get('verdict') or not current.get('run_id'):
                return
            parent = self.tasks._read(c, current['review_of_task_id'])
            try:
                self._current(parent, current)
            except ContractError:
                return
            run = self._run(c, current['run_id'])
            # exit_code is written only after wait() has reaped this exact
            # process. A crash label or elapsed time alone cannot establish it.
            if (run['state'] not in {'failed', 'uncertain'} or type(run.get('exit_code')) is not int or
                    current['run_id'] in self.studio._threads):
                return
            retries = current.get('review_retry_count', 0)
            if retries >= parent.get('max_review_retries', 1):
                issue = '检查连接异常且已达到自动恢复次数；原成果保留。' + preview_text(run.get('error') or '')[:800]
                if current.get('review_issue') != issue:
                    self.tasks._write(c, {**current, 'review_issue': issue}, 'review_recovery_exhausted', {'reason': issue})
                return
            self.tasks._write(c, {**current, 'state': 'queued', 'owner': None,
                'review_retry_count': retries + 1, 'review_issue': None}, 'review_recovery',
                {'from_run_id': current['run_id'], 'reason': '检查进程已退出，重新读取同一版作者成果；原请求费用仍未知。',
                 'retry': retries + 1})
