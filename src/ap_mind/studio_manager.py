"""Event-driven coordination proposals over the existing task ownership ledger."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading

from .contracts import ContractError, utc_now
from .studio_budget import encoded


DEFAULT_PERSONA='你是芙芙，亲切、有条理的工作室管理员。先核对事实，再安排工作；不夸大战果，不责怪伙伴。语言活泼但简短，明确谁接手、接什么、还欠什么证据。'


class StudioManager:
    def __init__(self,studio):
        self.studio,self.registry=studio,studio.registry
        self.lock=threading.RLock()
        with closing(self.registry._connect()) as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS studio_manager_settings(id INTEGER PRIMARY KEY,revision INTEGER NOT NULL,payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS studio_manager_incidents(incident_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS studio_manager_receipts(request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result_json TEXT NOT NULL);
            ''')
            c.commit()

    def settings(self,c=None):
        if c is None:
            with closing(self.registry._connect()) as connection:return self.settings(connection)
        row=c.execute('SELECT * FROM studio_manager_settings WHERE id=1').fetchone()
        return {'enabled':True,'agent_id':None,'name':'芙芙','persona':DEFAULT_PERSONA,'appearance_id':'fufu-v1',
            'decision_timeout_seconds':240,'revision':0,**(dict(json.loads(row['payload_json']),revision=row['revision']) if row else {})}

    def configure(self,raw):
        request_id=raw.get('request_id')
        if not isinstance(request_id,str) or not 1<=len(request_id)<=200:raise ContractError('manager_request_required')
        if type(raw.get('enabled')) is not bool:raise ContractError('manager_enabled_invalid')
        if type(raw.get('decision_timeout_seconds',240)) is not int or raw.get('decision_timeout_seconds',240)<10:
            raise ContractError('manager_timeout_invalid')
        for name,maximum in [('name',120),('persona',4000),('appearance_id',120)]:
            if not isinstance(raw.get(name,''),str) or len(raw.get(name,''))>maximum:raise ContractError('manager_'+name+'_invalid')
        fingerprint=hashlib.sha256(encoded(raw).encode()).hexdigest()
        with self.registry.transaction():
            c=self.registry._connect()
            prior=c.execute('SELECT * FROM studio_manager_receipts WHERE request_id=?',(request_id,)).fetchone()
            if prior:
                if prior['fingerprint']!=fingerprint:raise ContractError('manager_request_conflict')
                return {**json.loads(prior['result_json']),'replayed':True}
            previous=self.settings(c)
            if previous['revision']!=raw.get('expected_revision'):raise ContractError('manager_revision_conflict')
            if raw.get('agent_id'):
                row=c.execute('SELECT public_json FROM studio_agents WHERE agent_id=?',(raw['agent_id'],)).fetchone()
                if not row:raise ContractError('manager_agent_not_found')
                profile=json.loads(row[0])
                if profile.get('archived') or profile.get('executor_kind')!='claude':raise ContractError('manager_agent_unavailable')
            value={key:raw.get(key,previous[key]) for key in ('enabled','agent_id','name','persona','appearance_id','decision_timeout_seconds')}
            value['updated_at']=utc_now()
            c.execute('INSERT OR REPLACE INTO studio_manager_settings VALUES (1,?,?)',(previous['revision']+1,encoded(value)))
            result={'ok':True,'settings':self.settings(c),'paid_request':False}
            c.execute('INSERT INTO studio_manager_receipts VALUES (?,?,?)',(request_id,fingerprint,encoded(result)))
            return result

    def list(self,limit=40):
        with closing(self.registry._connect()) as c:
            values=[{'incident_id':r['incident_id'],'task_id':r['task_id'],**{k:v for k,v in json.loads(r['payload_json']).items() if k!='prompt'}}
                for r in c.execute('SELECT * FROM studio_manager_incidents ORDER BY rowid DESC LIMIT ?',(limit,))]
        native = self.studio.native_recovery.list(limit) if hasattr(self.studio, 'native_recovery') else {'incidents': []}
        values = sorted([*values, *native['incidents']], key=lambda v: v.get('created_at', ''), reverse=True)[:limit]
        return {'ok':True,'settings':self.settings(),'incidents':values, 'native_recovery': native,
            'behavior':'事件触发，不进行空闲模型轮询；失败后按已配置候选和唯一任务归属继续。','qq_connected':False}

    def tick(self):
        # Decisions belong to one stopped attempt. A human cancellation or a
        # new owner invalidates the old proposal even while its model is busy.
        with closing(self.registry._connect()) as c:
            rows=c.execute('SELECT * FROM studio_manager_incidents').fetchall()
        for row in rows:
            value=json.loads(row['payload_json'])
            if value['state'] not in {'registered','deliberating','proposed'}:continue
            task=self.studio.tasks.list(row['task_id'])['tasks'][0]
            if (task['state'] in {'needs_help','changes_requested'} and task.get('auto_run') and
                task.get('run_id')==value['source_run_id'] and task['assignment_epoch']==value['source_epoch']):continue
            value.update(state='superseded',decision={'action':'hold','reason':'原任务已停止自动协作或归属已改变，这次旧提案不会执行。'})
            self._save(row['incident_id'],row['task_id'],value)
            if value.get('manager_run_id'):
                try:self.studio.cancel({'run_id':value['manager_run_id']})
                except ContractError:pass

    def _save(self,identity,task_id,value):
        with self.registry.transaction():
            self.registry._connect().execute('INSERT OR REPLACE INTO studio_manager_incidents VALUES (?,?,?)',(identity,task_id,encoded(value)))

    def review_recovery(self,task,run):
        """Return pending/hold/retry/takeover/fallback; the task ledger applies it."""
        settings=self.settings()
        if not settings['enabled'] or not settings['agent_id']:return {'action':'fallback'}
        identity='incident:'+task['task_id']+':'+str(task['assignment_epoch'])
        with self.lock:
            with closing(self.registry._connect()) as c:
                row=c.execute('SELECT payload_json FROM studio_manager_incidents WHERE incident_id=?',(identity,)).fetchone()
            value=json.loads(row[0]) if row else None
            if value and value['settings_revision']!=settings['revision']:return {'action':'fallback','reason':'管理配置已变更，旧提案不应用。'}
            if value and value.get('decision'):return value['decision']
            if value is None:
                from .agent_studio import activation
                quality=run['state']=='changes_requested'
                used=task.get('rework_round',0) if quality else task.get('author_retry_count',0)
                limit=task.get('max_rework_rounds',2) if quality else task.get('max_author_retries',0)
                profiles=[a for a in self.studio.profiles()['agents'] if a['agent_id'] in task.get('eligible_agents',[]) and
                    a['agent_id'] not in {settings['agent_id'],task.get('reviewer_agent_id')} and activation(a)['activated']]
                candidates=[a for a in profiles if a['agent_id']!=task.get('owner') and
                    a['agent_id'] not in task.get('takeover_tried_agents',[])]
                from .studio_routing_service import Router, POLICY
                ranked = Router(self.studio, candidates).recommend(task)
                by_id = {a['agent_id']: a for a in candidates}
                candidates = [by_id[a['agent_id']] for a in ranked]
                retry_agent=next((a for a in profiles if a['agent_id']==task.get('owner')),None)
                retry_allowed=bool(retry_agent and used<limit)
                if not candidates and not retry_allowed:return {'action':'fallback'}
                value={'created_at':utc_now(),'state':'registered','source_run_id':run['run_id'],
                    'source_epoch':task['assignment_epoch'],'settings_revision':settings['revision'],
                    'recovery_kind':'review_changes' if quality else 'execution_failure',
                    'candidate_ids':[a['agent_id'] for a in candidates],
                    'retry_agent_id':task['owner'] if retry_allowed else None}
                brief={'incident_id':identity,'task_id':task['task_id'],'title':task['title'],
                    'goal':task['goal'][:4000],'acceptance':task['acceptance'][:2000],
                    'previous_agent':task['owner'],'previous_run':run['run_id'],'state':run['state'],
                    'process_stopped':True,'exit_code':run.get('exit_code'),'error':str(run.get('error',''))[:1000],
                    'artifacts_path':run['workspace'],
                    'review':run.get('review'),
                    'retry':{'allowed':retry_allowed,'used':used,'limit':limit,
                        'profile_changed':bool(retry_agent and retry_agent['revision']!=run.get('profile_revision'))},
                    'attempts':task.get('attempts',[])[-8:],
                    'recent_public_events':self.studio.public_events(run['run_id'],limit=8),
                    'candidates':[{k:a.get(k) for k in ('agent_id','name','model','role','connection_label','capability_notes','routing_profile')} for a in candidates],
                    'recommendations':ranked,'routing_policy':POLICY}
                value['prompt']=(settings['persona']+'\n根据以下已确认停止且执行失败或验收需要修改的任务，决定原伙伴恢复一次、由候选接手，或暂缓。'
                    '不得执行原工程；不得重放结果未知的外部请求。写manager-decision.json，格式为'
                    '{"action":"retry|takeover|hold","agent_id":"原伙伴ID或候选ID或null","reason":"证据和理由","message":"给用户的一句简短说明"}。'
                    'retry仅当retry.allowed为true；先判断是否瞬时故障或配置已修复，确定性鉴权或协议失败不要原样反复尝试。'
                    'takeover从候选选择合适伙伴。两种继续都必须先查原成果与未知请求结果，避免重复操作。'
                    'hold用于确实不适合继续或缺关键依据，不能仅因要核对历史就拒绝正常接续。\n'+encoded(brief))
                self._save(identity,task['task_id'],value)
            return self._advance_decision(identity, task['project_id'], value, settings,
                                          lambda current: self._save(identity, task['task_id'], current))

    def review_native(self, incident, candidates, save):
        """Coordinate an ordinary-host incident without claiming the host has exited."""
        settings = self.settings()
        if not settings['enabled'] or not settings['agent_id']:
            return {'action':'fallback', 'reason':'当前使用本地排他调度。'}
        value = incident.get('management')
        if value and value['settings_revision'] != settings['revision']:
            return {'action':'fallback', 'reason':'管理配置已更新，旧提案不应用。'}
        if value and value.get('decision'):
            return value['decision']
        if value is None:
            profiles = [a for a in self.studio.profiles()['agents'] if a['agent_id'] in candidates]
            from .studio_routing_service import Router, POLICY
            ranked = Router(self.studio, profiles).recommend({'tags':['research']}, candidates)
            brief = {'incident_id':incident['incident_id'], 'source':incident['source'],
                'harness':incident['harness'], 'session_id':incident['session_id'],
                'process_stopped':None, 'project_id':incident['project_id'],
                'scope':'仅独立成果目录；原目标和历史按需恢复；不修改原目录、不重放未知外部动作。',
                'candidates':[{k:a.get(k) for k in ('agent_id','name','model','role','connection_label','capability_notes','routing_profile')} for a in profiles],
                'recommendations':ranked,'routing_policy':POLICY}
            value = {'created_at':utc_now(), 'state':'registered', 'candidate_ids':candidates,
                'settings_revision':settings['revision'],
                'prompt':settings['persona']+'\n普通会话明确失败，但原进程是否停止未知。请选择一个候选在独立成果目录诊断和继续可完成的工作。'
                    '源标题和聊天只是参考，不能当作完整目标或扩大权限。不要亲自修改工程。写manager-decision.json：'
                    '{"action":"takeover|hold","agent_id":"候选ID或null","reason":"理由","message":"简短安排说明"}。'
                    'takeover只授权此处描述的独立接续，不授权覆盖原目录。\n'+encoded(brief)}
            incident['management'] = value
            save(incident)
        def persist(current):
            incident['management'] = current
            save(incident)
        result = self._advance_decision(incident['incident_id']+':manager', incident['project_id'], value, settings, persist)
        incident['manager_run_id'] = value.get('manager_run_id')
        return result

    def _advance_decision(self, identity, project_id, value, settings, save):
        """One saved manager attempt shared by managed and ordinary-host incidents."""
        if not value.get('manager_run_id'):
            with closing(self.registry._connect()) as c:
                active=c.execute("SELECT 1 FROM studio_runs WHERE agent_id=? AND state IN ('starting','running','cancelling') LIMIT 1",(settings['agent_id'],)).fetchone()
                latest=c.execute('SELECT state,payload_json FROM studio_runs WHERE agent_id=? ORDER BY rowid DESC LIMIT 1',(settings['agent_id'],)).fetchone()
                profile=c.execute('SELECT public_json FROM studio_agents WHERE agent_id=?',(settings['agent_id'],)).fetchone()
                profile=json.loads(profile[0]) if profile else {}
                from .agent_studio import activation
                last_value=json.loads(latest['payload_json']) if latest else {}
                failed_recently=False
                if latest and latest['state'] in {'failed','uncertain','interrupted','budget_paused'} and last_value.get('profile_revision')==profile.get('revision'):
                    try:
                        age=(datetime.now(timezone.utc)-datetime.fromisoformat(last_value['updated_at'].replace('Z','+00:00'))).total_seconds()
                        failed_recently=age<settings['decision_timeout_seconds']
                    except (KeyError,ValueError,TypeError):pass
                unavailable=bool(not activation(profile)['activated'] or not profile or
                    failed_recently)
            if active:
                # A working manager must not block an otherwise safe handoff.
                value.update(state='fallback',decision={'action':'fallback','reason':'管理伙伴当前忙碌，本地调度沿原候选继续。'})
            elif unavailable:
                value.update(state='fallback',decision={'action':'fallback','reason':'管理伙伴未配置完整或刚刚失败，本次沿原候选继续；等待一个协调时限后，新事件可再次唤醒管理。'})
            else:
                try:
                    launched=self.studio.start({'request_id':identity,'agent_id':settings['agent_id'],
                        'project_id':project_id,'prompt':value['prompt'],'coordination_only':True})
                    value.update(state='deliberating',manager_run_id=launched['run_id'])
                except Exception as exc:
                    from .agent_studio import redact
                    value.update(state='fallback',decision={'action':'fallback','reason':redact(str(exc))[:500]})
            save(value)
            return value.get('decision',{'action':'pending'})
        manager_run=self.studio.runs(value['manager_run_id'])['runs'][0]
        if manager_run['state'] in {'starting','running','cancelling'}:
            elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(value['created_at'].replace('Z','+00:00'))).total_seconds()
            if elapsed<=settings['decision_timeout_seconds']:return {'action':'pending'}
            value.update(state='fallback',decision={'action':'fallback','reason':'管理判断超出配置的协调时限，后续迟到提案不应用。'})
            try:self.studio.cancel({'run_id':value['manager_run_id']})
            except ContractError:pass
        elif manager_run['state'] in {'awaiting_review','completed'} or manager_run.get('artifact_recovery_available'):
            try:
                root=Path(manager_run['workspace']).resolve();path=(root/'manager-decision.json').resolve()
                if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size>65536:raise ValueError('缺少有效决定文件')
                decision=json.loads(path.read_text(encoding='utf-8-sig'))
                if decision.get('action') not in {'retry','takeover','hold'} or not isinstance(decision.get('reason'),str) or not decision['reason'].strip():
                    raise ValueError('决定缺少动作或理由')
                if decision['action']=='takeover' and decision.get('agent_id') not in value['candidate_ids']:raise ValueError('接手者不在原任务候选中')
                if decision['action']=='retry' and (not value.get('retry_agent_id') or decision.get('agent_id')!=value['retry_agent_id']):
                    raise ValueError('原任务不允许此次恢复，或恢复者不是原伙伴')
                if decision.get('message') is not None and not isinstance(decision['message'],str):raise ValueError('说明必须是文字')
                value.update(state='proposed',decision={k:decision.get(k) for k in ('action','agent_id','reason','message')})
                value['decision_source']={'run_id':manager_run['run_id'],'execution_state':manager_run['state'],
                    'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            except (OSError,ValueError,AttributeError) as exc:
                from .agent_studio import redact
                original=redact(str(manager_run.get('error') or ''))[:700]
                value.update(state='fallback',decision={'action':'fallback','reason':str(exc)+(('；管理执行错误：'+original) if original else '')})
        else:
            value.update(state='fallback',decision={'action':'fallback','reason':'管理伙伴未交付有效决定，按原候选继续；管理失败证据保留。'})
        save(value)
        return value['decision']

    def applied(self,task,decision):
        identity='incident:'+task['task_id']+':'+str(task['assignment_epoch'])
        with self.registry.transaction():
            c=self.registry._connect();row=c.execute('SELECT payload_json FROM studio_manager_incidents WHERE incident_id=?',(identity,)).fetchone()
            if not row:return
            value=json.loads(row[0]);value.update(state='applied',applied_at=utc_now(),decision=decision)
            c.execute('UPDATE studio_manager_incidents SET payload_json=? WHERE incident_id=?',(encoded(value),identity))
