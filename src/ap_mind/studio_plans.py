"""Durable task graphs; execution, review, ownership and returns stay in Studio.

Lock order is studio._lock -> tasks.lock -> registry.transaction. Model launch
and cancellation never wait inside a database transaction. A persisted launch
identity survives a lost acknowledgement without another paid attempt.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from .contracts import ContractError, utc_now
from .studio_tasks import encoded, required
from .studio_returns import destination


class StudioPlans:
    def __init__(self, studio):
        self.studio, self.registry = studio, studio.registry
        with closing(self.registry._connect()) as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS studio_plans(plan_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL, payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS studio_plan_receipts(request_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL, result_json TEXT NOT NULL);
            ''')
            c.commit()

    def _read(self, c, identity):
        row = c.execute('SELECT * FROM studio_plans WHERE plan_id=?', (identity,)).fetchone()
        if not row: raise ContractError('studio_plan_not_found')
        return {**json.loads(row['payload_json']), 'plan_id':identity, 'revision':row['revision']}

    def _write(self, c, plan):
        plan = {**plan, 'revision':plan.get('revision', 0)+1, 'updated_at':utc_now()}
        c.execute('INSERT OR REPLACE INTO studio_plans VALUES (?,?,?)',
                  (plan['plan_id'], plan['revision'], encoded(plan)))
        return plan

    def _receipt(self, c, raw, result=None):
        identity = required(raw, 'request_id', 200)
        fingerprint = hashlib.sha256(encoded(raw).encode()).hexdigest()
        prior = c.execute('SELECT * FROM studio_plan_receipts WHERE request_id=?', (identity,)).fetchone()
        if prior:
            if prior['fingerprint'] != fingerprint: raise ContractError('studio_plan_request_conflict')
            return {**json.loads(prior['result_json']), 'replayed':True}
        if result is not None:
            c.execute('INSERT INTO studio_plan_receipts VALUES (?,?,?)', (identity, fingerprint, encoded(result)))

    def submit(self, raw):
        required(raw, 'request_id', 200)
        project = required(raw, 'project_id', 200)
        self.registry.get(project, include_archived=False)
        target = destination(raw.get('return_to'))
        if not target: raise ContractError('studio_plan_return_required')
        origin = destination(raw.get('collaboration_origin'))
        fields = {key:required(raw, key) for key in ('title','goal')}
        nodes = raw.get('tasks')
        if not isinstance(nodes, list) or not nodes: raise ContractError('studio_plan_tasks_required')
        by_key = {}
        for node in nodes:
            if not isinstance(node, dict): raise ContractError('studio_plan_node_invalid')
            key = required(node, 'key', 100)
            if key in by_key: raise ContractError('studio_plan_duplicate_key')
            deps = node.get('dependencies', [])
            if not isinstance(deps, list) or any(not isinstance(dep,str) for dep in deps):
                raise ContractError('studio_plan_dependencies_invalid')
            by_key[key] = {**node, 'key':key, 'dependencies':list(dict.fromkeys(deps))}
        # Kahn ordering validates forward references before any task is inserted.
        ordered, remaining = [], dict(by_key)
        if any(dep not in by_key for node in nodes for dep in node.get('dependencies', [])):
            raise ContractError('studio_plan_dependency_missing')
        while remaining:
            ready = [key for key,node in remaining.items() if all(dep in ordered for dep in node['dependencies'])]
            if not ready: raise ContractError('studio_plan_dependency_cycle')
            for key in ready: ordered.append(key); remaining.pop(key)
        with self.studio._lock, self.studio.tasks.lock, self.registry.transaction():
            c = self.registry._connect()
            prior = self._receipt(c, raw)
            if prior: return prior
            identity = 'plan-' + uuid.uuid4().hex
            mapping, saved = {}, []
            allowed = ('title','goal','acceptance','eligible_agents','reviewer_agent_id','resources','tags',
                       'max_turns','max_author_retries','max_review_retries','max_rework_rounds','extensions')
            for key in ordered:
                node = by_key[key]
                request = {name:node[name] for name in allowed if name in node}
                request.update(request_id=identity+':node:'+key, project_id=project, auto_run=False,
                               dependencies=[mapping[dep] for dep in node['dependencies']])
                if origin: request['collaboration_origin'] = origin
                task = self.studio.tasks.save(request)['task']
                task = self.studio.tasks._write(c, {**task, 'plan_id':identity}, 'plan_registered', {'plan_id':identity})
                mapping[key] = task['task_id']
                saved.append({'key':key,'task_id':task['task_id'], 'dependencies':node['dependencies'],
                    'requested_agents':node.get('eligible_agents',[]), 'requested_reviewer':node.get('reviewer_agent_id')})
            plan = self._write(c, {'plan_id':identity, 'project_id':project, **fields,
                'state':'planning','created_at':utc_now(),'return_to':target,'collaboration_origin':origin,
                'nodes':saved,'manager_acknowledged':False,'assignment_source':None})
            result = {'ok':True,'plan':self._public(c, plan),'replayed':False}
            self._receipt(c, raw, result)
            return result

    def _public(self, c, plan):
        result = {k:v for k,v in plan.items() if k not in {'manager_request','candidate_snapshot'}}
        result['nodes'] = [{**node, **{key:task.get(key) for key in
            ('title','state','owner','run_id','version','assignment_epoch','reviewer_agent_id','takeover_issue','review_issue')}}
            for node in plan['nodes'] for task in [self.studio.tasks._read(c,node['task_id'])]]
        result['returns'] = self.studio.returns.list(plan['plan_id'])
        return result

    def list(self, plan_id=None, project_id=None, offset=0, limit=30):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise ContractError('studio_plan_page_invalid')
        with closing(self.registry._connect()) as c:
            if plan_id: plans = [self._read(c,plan_id)]; more = False
            else:
                where,params = ("WHERE json_extract(payload_json,'$.project_id')=?", [project_id]) if project_id else ('',[])
                rows = c.execute('SELECT plan_id FROM studio_plans '+where+' ORDER BY rowid DESC LIMIT ? OFFSET ?',
                                 (*params,limit+1,offset)).fetchall()
                more = len(rows)>limit; plans = [self._read(c,row[0]) for row in rows[:limit]]
            return {'ok':True,'plans':[self._public(c,p) for p in plans], 'next_offset':offset+limit if more else None}

    def guard(self, c, task):
        if task.get('plan_id') and self._read(c,task['plan_id'])['state']!='running':
            raise ContractError('studio_plan_not_released')

    def cancel(self, raw):
        with self.studio._lock, self.studio.tasks.lock, self.registry.transaction():
            c = self.registry._connect()
            prior = self._receipt(c, raw)
            if prior: return prior
            plan = self._read(c,required(raw,'plan_id',100))
            if plan['revision'] != raw.get('expected_revision'): raise ContractError('studio_plan_revision_conflict')
            plan = self._write(c, {**plan,'state':'cancelled'})
            # The stop intent is durable even if cancelling a process fails.
            self._disable(c,plan)
            result = {'ok':True,'plan':self._public(c,plan),'replayed':False}
            self._receipt(c,raw,result)
        self._stop_cancelled(plan)
        return result

    def _disable(self,c,plan):
        queue = [n['task_id'] for n in plan['nodes']]
        for identity in queue:
            task = self.studio.tasks._read(c,identity)
            if task.get('review_task_id') and task['review_task_id'] not in queue: queue.append(task['review_task_id'])
            if task.get('auto_run') or task.get('rework_pending'):
                self.studio.tasks._write(c,{**task,'auto_run':False,'rework_pending':False},'plan_stopped',{'plan_id':plan['plan_id']})

    def _stop_cancelled(self,plan):
        with closing(self.registry._connect()) as c:
            queue = [n['task_id'] for n in plan['nodes']]; runs = [plan.get('manager_run_id')]
            for identity in queue:
                task = self.studio.tasks._read(c,identity); runs.append(task.get('run_id'))
                if task.get('review_task_id') and task['review_task_id'] not in queue: queue.append(task['review_task_id'])
        for rid in set(runs)-{None}:
            run = self.studio.runs(rid)['runs'][0]
            if run['state'] in {'starting','running','cancelling','waiting'}:
                try: self.studio.cancel({'run_id':rid})
                except ContractError: pass

    def _profiles(self):
        from .agent_studio import activation
        return [p for p in self.studio.profiles()['agents'] if activation(p)['activated'] and not p.get('management_reserved')]

    def _assignments(self,plan,profiles,proposal=None):
        from .studio_routing_service import Router
        router = Router(self.studio, profiles)
        available = {p['agent_id']:p for p in profiles}
        needed = {dep for n in plan['nodes'] for dep in n['dependencies']}
        supplied = {}
        if proposal is not None:
            if not isinstance(proposal,dict) or not isinstance(proposal.get('assignments'),list):
                raise ContractError('studio_plan_proposal_invalid')
            for item in proposal['assignments']:
                if not isinstance(item,dict) or not isinstance(item.get('key'),str) or item['key'] in supplied:
                    raise ContractError('studio_plan_assignment_invalid')
                supplied[item['key']] = item
            if set(supplied)!={n['key'] for n in plan['nodes']}: raise ContractError('studio_plan_assignment_incomplete')
            if not isinstance(proposal.get('message'),str) or len(proposal['message'])>4000:
                raise ContractError('studio_plan_message_invalid')
        assignments=[]
        for node in plan['nodes']:
            candidates = [a for a in (node['requested_agents'] or list(available)) if a in available]
            task = self.studio.tasks.list(node['task_id'])['tasks'][0]
            ranked = router.recommend(task, candidates)
            candidates = [a['agent_id'] for a in ranked]
            fixed = node.get('requested_reviewer')
            if fixed and fixed not in available: raise ContractError('studio_plan_reviewer_unavailable')
            candidates = [a for a in candidates if a!=fixed]
            if not candidates: raise ContractError('studio_plan_no_candidate')
            item = supplied.get(node['key'])
            author = item.get('agent_id') if item else candidates[0]
            reviewer = item.get('reviewer_agent_id') if item else fixed
            if author not in candidates: raise ContractError('studio_plan_author_unavailable')
            if fixed and reviewer!=fixed: raise ContractError('studio_plan_reviewer_changed')
            if not item and node['key'] in needed and not reviewer:
                review_ranked = router.recommend({**task, 'tags': ['review', *task.get('tags', [])]}, exclude=[author])
                reviewer = next((a['agent_id'] for a in review_ranked),None)
            if (reviewer and (reviewer not in available or reviewer==author)) or (node['key'] in needed and not reviewer):
                raise ContractError('studio_plan_independent_review_required')
            reason = item.get('reason') if item else next(a['reason'] for a in ranked if a['agent_id']==author)
            if not isinstance(reason,str) or not reason.strip() or len(reason)>4000: raise ContractError('studio_plan_reason_invalid')
            assignments.append({'key':node['key'],'agent_id':author,'reviewer_agent_id':reviewer,'reason':reason,
                'candidates':[author,*[a for a in candidates if a not in {author,reviewer}]]})
        return assignments

    def _allowed(self,plan):
        origin = plan.get('collaboration_origin')
        return not origin or self.studio.sessions.effective(origin['harness'],origin['session_id'],plan['project_id'])['enabled']

    def _planning(self,plan):
        profiles = self._profiles()
        try: fallback = self._assignments(plan,profiles)
        except ContractError as exc:
            return self._issue(plan,str(exc),'needs_configuration')
        settings = self.studio.manager.settings()
        if not self._allowed(plan): return
        if settings['enabled'] and settings.get('agent_id') and not plan.get('manager_issue'):
            if not plan.get('manager_request'):
                catalog = [{k:p.get(k) for k in ('agent_id','name','model','role','connection_label','capability_notes','routing_profile')} for p in profiles]
                with closing(self.registry._connect()) as c:
                    briefs = [{**n, **{k:t[k] for k in ('title','goal','acceptance','tags')}}
                        for n in plan['nodes'] for t in [self.studio.tasks._read(c,n['task_id'])]]
                from .studio_routing_service import Router, POLICY
                router = Router(self.studio, profiles)
                recommendations = {t['key']: router.recommend(t, t['requested_agents'] or None) for t in briefs}
                prompt = (settings['persona']+'\n为已有工作计划选择执行伙伴和独立验收伙伴。不得改目标或依赖，不亲自执行工程。'
                    '优先参考候选职责及同类历史；未知能力不编造。每项有下游的工作必须指定不同的验收者。'
                    '写manager-plan.json：{"message":"简短安排","assignments":[{"key":"节点key",'
                    '"agent_id":"候选ID","reviewer_agent_id":"不同候选ID或null","reason":"原因"}]}。'
                    '必须覆盖全部节点；requested_agents非空时作者只能从中选择，requested_reviewer非空时须沿用。\n'
                    +POLICY+'\n'+encoded({'title':plan['title'],'goal':plan['goal'],'tasks':briefs,'partners':catalog,
                        'task_recommendations':recommendations,'history_coverage':router.coverage}))
                plan = self._persist({**plan,'state':'planning','manager_settings_revision':settings['revision'],
                    'manager_deadline_seconds':settings['decision_timeout_seconds'],
                    'manager_request':{'request_id':plan['plan_id']+':manager','agent_id':settings['agent_id'],
                        'project_id':plan['project_id'],'prompt':prompt,'coordination_only':True,'coordination_output':'manager-plan.json'}})
            if settings['revision']!=plan.get('manager_settings_revision'):
                plan = self._persist({**plan,'manager_issue':'管理配置已改变，原提案不应用。'})
            elif not plan.get('manager_run_id'):
                with closing(self.registry._connect()) as c:
                    existing=c.execute('SELECT run_id FROM studio_runs WHERE request_id=?',(plan['manager_request']['request_id'],)).fetchone()
                    busy = c.execute("SELECT 1 FROM studio_runs WHERE agent_id=? AND state IN ('starting','running','cancelling')",(settings['agent_id'],)).fetchone()
                if existing:
                    existing_run=self.studio.runs(existing[0])['runs'][0]
                    self._persist({**plan,'manager_run_id':existing[0],'manager_started_at':existing_run['created_at']})
                    return
                if busy: return
                try:
                    started = self.studio.start(plan['manager_request'])
                    self._persist({**plan,'manager_run_id':started['run_id'],'manager_started_at':utc_now()})
                    return
                except Exception as exc:
                    from .agent_studio import redact
                    # start may have persisted a real run before losing its
                    # acknowledgement. Recover it before choosing fallback.
                    with closing(self.registry._connect()) as c:
                        existing=c.execute('SELECT run_id FROM studio_runs WHERE request_id=?',
                                           (plan['manager_request']['request_id'],)).fetchone()
                    if existing:
                        existing_run=self.studio.runs(existing[0])['runs'][0]
                        self._persist({**plan,'manager_run_id':existing[0],'manager_started_at':existing_run['created_at'],
                                       'manager_launch_issue':redact(str(exc))[:800]})
                        return
                    plan = self._persist({**plan,'manager_issue':redact(str(exc))[:800]})
            else:
                run = self.studio.runs(plan['manager_run_id'])['runs'][0]
                if run['state'] in {'starting','running','cancelling'}:
                    age=(datetime.now(timezone.utc)-datetime.fromisoformat(plan['manager_started_at'].replace('Z','+00:00'))).total_seconds()
                    if age<=plan['manager_deadline_seconds']: return
                    cancel_issue=None
                    try:self.studio.cancel({'run_id':run['run_id']})
                    except (ContractError,OSError) as exc:
                        from .agent_studio import redact
                        cancel_issue=redact(str(exc))[:800]
                    plan = self._persist({**plan,'manager_issue':'管理决定超时，保留原请求并按候选继续。',
                                          'manager_cancel_issue':cancel_issue})
                elif self.studio.execution_stopped(run):
                    try:
                        path=Path(run['workspace'])/'manager-plan.json'
                        if path.is_symlink() or path.resolve().parent!=Path(run['workspace']).resolve() or path.stat().st_size>128000:
                            raise ValueError('invalid manager output path or size')
                        data=path.read_bytes();proposal=json.loads(data)
                        assignments=self._assignments(plan,profiles,proposal)
                        self._apply(plan,assignments,'manager',proposal['message'],hashlib.sha256(data).hexdigest())
                        return
                    except (OSError,ValueError,ContractError,TypeError) as exc:
                        from .agent_studio import redact
                        plan = self._persist({**plan,'manager_issue':redact(str(exc)+'; '+str(run.get('error') or ''))[:1200]})
                else: return
        self._apply(plan,fallback,'fallback','按已配置候选继续；没有将本地分配记作管理模型决定。')

    def _persist(self,plan):
        with self.registry.transaction(): return self._write(self.registry._connect(),plan)

    def _issue(self,plan,issue,state):
        if plan.get('issue')!=issue or plan['state']!=state:
            return self._persist({**plan,'issue':issue,'state':state})

    def _apply(self,plan,assignments,source,message,sha=None):
        with self.studio.tasks.lock, self.registry.transaction():
            c=self.registry._connect();current=self._read(c,plan['plan_id'])
            if current['state'] not in {'planning','needs_configuration'} or not self._allowed(current): return
            for node in current['nodes']:
                task=self.studio.tasks._read(c,node['task_id'])
                if task['state']!='queued' or task.get('owner'): raise ContractError('studio_plan_node_changed')
                item=next(a for a in assignments if a['key']==node['key'])
                self.studio.tasks._write(c,{**task,'auto_run':True,'eligible_agents':item['candidates'],
                    'reviewer_agent_id':item['reviewer_agent_id']},'plan_assigned',item)
            self._write(c,{**current,'state':'running','issue':None,'assignments':assignments,
                'assignment_source':source,'manager_acknowledged':source=='manager','manager_message':message,
                'manager_decision_sha256':sha})

    def _progress(self,plan):
        with closing(self.registry._connect()) as c:
            tasks=[self.studio.tasks._read(c,n['task_id']) for n in plan['nodes']]
            reviews={t['task_id']:self.studio.tasks._read(c,t['review_task_id']) for t in tasks if t.get('review_task_id')}
        upstream={dep for t in tasks for dep in t['dependencies']}
        terminal=lambda t:t['state']=='completed' or (t['state']=='waiting_review' and not t.get('reviewer_agent_id') and t['task_id'] not in upstream)
        ready=all(terminal(t) for t in tasks)
        failed=[t for t in tasks if t['state'] in {'paused','archived'} or
            t['state'] in {'needs_help','changes_requested'} and (t.get('takeover_issue') or not t.get('auto_run') or
            t['state']=='changes_requested' and t.get('rework_round',0)>=t.get('max_rework_rounds',2))]
        review_blocked={tid for tid,review in reviews.items() if
            review['state'] in {'paused','archived'} or review.get('review_issue') or
            (review.get('verdict') or {}).get('outcome')=='inconclusive'}
        # The reviewer may be unavailable before any child run is created.
        review_blocked.update(t['task_id'] for t in tasks if t['state']=='waiting_review' and t.get('review_issue'))
        available={p['agent_id'] for p in self._profiles()}
        unconfigured=lambda t:t['state']=='queued' and not any(a in available for a in t.get('eligible_agents',[]))
        review_blocked.update(tid for tid,review in reviews.items() if unconfigured(review))
        failed.extend(t for t in tasks if unconfigured(t))
        failed.extend(t for t in tasks if t['task_id'] in review_blocked)
        active=any(t['state'] in {'running','dispatching'} or (t['state']=='waiting_review' and t.get('reviewer_agent_id')
            and t['task_id'] not in review_blocked) for t in tasks)
        active=active or any(r['state'] in {'running','dispatching'} for r in reviews.values())
        if not ready and not (failed and not active): return
        state='completed' if all(t['state']=='completed' for t in tasks) else 'ready_for_review' if ready else 'needs_help'
        lines=[]
        for t in tasks:
            run=self.studio.runs(t['run_id'])['runs'][0] if t.get('run_id') else {}
            lines.append(f"{t['title']}：{t['state']}；task_id={t['task_id']}；run_id={t.get('run_id')}；成果={run.get('workspace','尚未执行')}")
        message=(f"工作室整批返回：{plan['title']}\nplan_id={plan['plan_id']}；状态={state}\n"+'\n'.join(lines)+
            '\n按原目标读取实际成果并总结。ready_for_review是等你独立验收，不能当已通过；保留失败和未完成项，不重放未知外部动作。')
        with self.registry.transaction():
            c=self.registry._connect();current=self._read(c,plan['plan_id'])
            if current['state']!='running': return
            if failed: self._disable(c,current)
            cycle=current.get('return_cycle',0)
            return_id='return:'+plan['plan_id']+(f':cycle:{cycle}' if cycle else '')
            self.studio.returns.enqueue(return_id,plan['plan_id'],plan['return_to'],message)
            self._write(c,{**current,'state':state,'returned_at':utc_now()})

    def _reconcile_returned(self,plan):
        """A batch return is a review handoff, not a permanent execution lock.

        Reviewers can reject an already-returned leaf. Keep its original task,
        assignments and recovery policy; only reopen the graph for the changed
        nodes. A new return cycle is persisted once so restart/polling cannot
        lose or duplicate the next batch notification.
        """
        with self.registry.transaction():
            c=self.registry._connect();current=self._read(c,plan['plan_id'])
            if current['state'] not in {'ready_for_review','completed'}: return
            tasks=[self.studio.tasks._read(c,n['task_id']) for n in current['nodes']]
            if all(t['state']=='completed' for t in tasks):
                if current['state']!='completed': self._write(c,{**current,'state':'completed'})
                return
            upstream={dep for t in tasks for dep in t['dependencies']}
            terminal=lambda t:t['state']=='completed' or (t['state']=='waiting_review' and
                not t.get('reviewer_agent_id') and t['task_id'] not in upstream)
            if all(terminal(t) for t in tasks): return
            self._write(c,{**current,'state':'running','issue':None,
                'return_cycle':current.get('return_cycle',0)+1,
                'previous_returned_at':current.get('returned_at'),'returned_at':None})

    def tick(self):
        with self.studio._lock, self.studio.tasks.lock:
            with closing(self.registry._connect()) as c:
                plans=[self._read(c,r[0]) for r in c.execute('SELECT plan_id FROM studio_plans')]
            for plan in plans:
                try:
                    if plan['state']=='cancelled': self._stop_cancelled(plan)
                    elif plan['state'] in {'planning','needs_configuration'}: self._planning(plan)
                    elif plan['state']=='running': self._progress(plan)
                    elif plan['state'] in {'ready_for_review','completed'}: self._reconcile_returned(plan)
                except (ContractError,OSError,ValueError) as exc:
                    # Preserve a recoverable diagnostic without blocking other plans.
                    self._issue(plan,str(exc),plan['state'])
