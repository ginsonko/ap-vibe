"""Durable completion subscriptions; inbox delivery and wake-up are distinct."""
from contextlib import closing
import json
import time

from .contracts import ContractError, utc_now
from .studio_sessions import actor_id, encoded


def destination(raw):
    if raw is None:return None
    if not isinstance(raw,dict) or raw.get('harness') not in {'codex','claude'} or not isinstance(raw.get('session_id'),str) or not 1<=len(raw['session_id'])<=256:
        raise ContractError('studio_return_identity_invalid')
    if type(raw.get('wake',False)) is not bool:
        raise ContractError('studio_return_wake_invalid')
    return {k:raw[k] for k in ('harness','session_id','wake') if k in raw}


class StudioReturns:
    def __init__(self,studio):
        self.studio,self.registry=studio,studio.registry
        with closing(self.registry._connect()) as c:
            c.execute('''CREATE TABLE IF NOT EXISTS studio_returns(
                return_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload_json TEXT NOT NULL)''')
            c.commit()

    def enqueue(self, return_id, task_id, target, message):
        target = destination(target)
        if not target or not isinstance(message,str) or not message.strip():
            raise ContractError('studio_return_payload_invalid')
        with self.registry.transaction():
            c=self.registry._connect()
            old=c.execute('SELECT * FROM studio_returns WHERE return_id=?',(return_id,)).fetchone()
            if old:
                value=json.loads(old['payload_json'])
                if old['task_id']!=task_id or value['target']!=target or value['message']!=message:
                    raise ContractError('studio_return_request_conflict')
                return {'return_id':return_id,'replayed':True}
            c.execute('INSERT INTO studio_returns VALUES (?,?,?)',(return_id,task_id,encoded({
                'target':target,'message':message,'state':'pending','created_at':utc_now()})))
        return {'return_id':return_id,'replayed':False}

    def _save(self, return_id, value):
        with self.registry.transaction():
            self.registry._connect().execute(
                'UPDATE studio_returns SET payload_json=? WHERE return_id=?',
                (encoded(value), return_id))

    def _deliver(self, row):
        value=json.loads(row['payload_json'])
        return_id=row['return_id'];target=value['target']
        if value['state']=='pending':
            identity=actor_id(target['harness'],target['session_id'])
            receipt=self.studio.send_message({'request_id':return_id+':inbox','sender':'studio-coordinator',
                'recipient':identity,'task_id':identity,'body':value['message']})
            value.update(state='inbox_saved',message_id=receipt['message_id'])
            self._save(return_id,value)
        if not target.get('wake'):return
        if target['harness']!='codex':
            if not value.get('wake_issue'):
                value['wake_issue']='当前普通Claude终端通过下次Hook/Skill读取收件箱；未启动同会话的第二个写入进程。'
                self._save(return_id,value)
            return
        messages=self.studio.service.codex_messages
        request_id=return_id+':wake'
        # Reconcile before retry, including a crash after enqueue but before
        # recording its receipt. Submitted/uncertain/failed are never replayed.
        delivery=messages.read_delivery(target['session_id'],request_id)
        if delivery:
            if value.get('delivery')!=delivery or value['state']!='wake_queued':
                value.update(state='wake_queued',delivery=delivery)
                value.pop('wake_issue',None)
                value.pop('wake_retry_at',None)
                self._save(return_id,value)
            if delivery.get('status')=='queued':
                messages.resume_queued_delivery(target['session_id'],request_id)
            return
        if value['state']=='wake_queued':
            # A previously acknowledged record disappearing is not evidence
            # that it was never delivered. Do not silently send again.
            return
        if value.get('wake_retry_blocked') or time.time()<value.get('wake_retry_at',0):return
        attempt=value.get('wake_attempts',0)+1
        value.update(wake_attempts=attempt,wake_last_attempt_at=utc_now(),
                     wake_retry_at=time.time()+min(300,5*2**min(attempt-1,6)))
        self._save(return_id,value)
        try:
            receipt=messages.enqueue({'request_id':request_id,'session_id':target['session_id'],
                                      'message':value['message']})
            value.update(state='wake_queued',delivery=receipt['delivery'])
            value.pop('wake_issue',None)
            value.pop('wake_retry_at',None)
        except Exception as exc:
            issue=str(exc)[:300]
            value.update(wake_issue=issue,wake_last_issue=issue,state='inbox_saved')
            # Invalid identity/body and idempotency conflicts need correction,
            # while unavailable sources and local transport faults may heal.
            if issue in {'message_session_id_invalid','message_request_id_required','message_text_invalid',
                         'request_id_conflict','message_source_identity_changed','message_source_directory_changed'}:
                value['wake_retry_blocked']=True
        self._save(return_id,value)

    def tick(self):
        for task in self.studio.tasks._scheduled():
            target=task.get('return_to')
            if not target or task.get('review_of_task_id'):continue
            needs_help = task['state']=='needs_help' and bool(task.get('takeover_issue') or not task.get('auto_run'))
            ready=needs_help or task['state']=='completed' or task['state']=='waiting_review' and not task.get('reviewer_agent_id')
            if not ready:continue
            return_id='return:'+task['task_id']+':'+str(task['assignment_epoch'])
            if needs_help:return_id+=':needs-help'
            run=self.studio.runs(task['run_id'])['runs'][0] if task.get('run_id') else None
            if not run:continue
            status_text = ('伙伴未能完成，当前需要原任务处理' if needs_help else
                           '已经独立验收' if task['state']=='completed' else '作者已交付，等待你验收')
            message=(f"工作室任务返回：{task['title']}\n"
                f"状态：{status_text}。"
                f"任务ID={task['task_id']}；执行ID={task['run_id']}；负责伙伴={task['owner']}。\n"
                f"成果入口：{run['workspace']}。用ap_vibe_task_list按task_id读取任务与当前归属，再用ap_vibe_artifacts读取真实文件。"
                "按原用户目标检查成果，保留其它伙伴已完成的工作；本通知不能扩大任务权限。")
            if needs_help:
                message += (f"\n处理原因：{task.get('takeover_issue') or '托管执行未完成'}。"
                    f"\n已保存错误：{str(run.get('error') or run['state'])[:900]}。"
                    "\n请先核对当前归属和已有文件。结果未知的外部请求不能直接重放；"
                    "可自行完成适合的剩余工作或重新安排已配置伙伴，不应继续等待不存在的完成通知。")
            with self.registry.transaction():
                c=self.registry._connect()
                c.execute('INSERT OR IGNORE INTO studio_returns VALUES (?,?,?)',(return_id,task['task_id'],encoded({
                    'target':target,'message':message,'state':'pending','created_at':utc_now()})))
        with closing(self.registry._connect()) as c:
            rows=c.execute('SELECT * FROM studio_returns').fetchall()
        for row in rows:
            try:
                self._deliver(row)
            except Exception:
                # A broken source must not block other completion notices.
                continue

    def list(self,task_id):
        with closing(self.registry._connect()) as c:
            records = [dict(return_id=row['return_id'],**json.loads(row['payload_json'])) for row in
                       c.execute('SELECT * FROM studio_returns WHERE task_id=?',(task_id,))]
        for value in records:
            target = value.get('target') or {}
            if value.get('state') == 'wake_queued' and target.get('harness') == 'codex':
                # The stored state records the enqueue event; delivery is a
                # live readback and must never enqueue again from a GET.
                delivery = self.studio.service.codex_messages.read_delivery(
                    target['session_id'], value['return_id'] + ':wake')
                if delivery:
                    value['delivery'] = delivery
                value['wake_status'] = (value.get('delivery') or {}).get('status', 'unknown')
        return records
