"""Ordinary session presence and opt-in collaboration over the shared ledger.

Lifecycle events establish activity. File timestamps only establish recency.
The same store is used by Hooks, the workbench and MCP; reads have no gate.
"""
from contextlib import closing
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone

from .contracts import ContractError, utc_now
from .harness_registry import valid_kind


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def actor_id(harness, session_id):
    return 'session-' + hashlib.sha256((harness + ':' + session_id).encode()).hexdigest()[:32]


def timestamp(value):
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError,TypeError,AttributeError,OverflowError):
        return None


class StudioSessions:
    def __init__(self, studio):
        self.studio, self.registry = studio, studio.registry
        self._cache = None
        self._cache_at = 0
        self._cache_lock = threading.Lock()
        with closing(self.registry._connect()) as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS studio_participation(
                    scope TEXT NOT NULL, target TEXT NOT NULL, revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY(scope,target));
                CREATE TABLE IF NOT EXISTS studio_session_actors(
                    actor_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS studio_session_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    actor_id TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS studio_session_event_actor ON studio_session_events(actor_id,seq);
                CREATE TABLE IF NOT EXISTS studio_participation_requests(
                    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL);
            ''')
            # A service restart cannot tell whether a desktop/CLI process survived.
            c.execute("UPDATE studio_session_actors SET payload_json=json_set(payload_json,'$.state','unknown','$.status_basis','service_restart') WHERE json_extract(payload_json,'$.state')='running'")
            c.commit()

    def settings(self, scope='global', target='', c=None):
        if c is None:
            with closing(self.registry._connect()) as connection:
                return self.settings(scope, target, connection)
        row = c.execute('SELECT * FROM studio_participation WHERE scope=? AND target=?',(scope,target)).fetchone()
        return {**(json.loads(row['payload_json']) if row else {'enabled':False if scope=='global' else None,'paused':False}),
                'scope':scope,'target':target,'revision':row['revision'] if row else 0}

    def effective(self, harness, session_id, project_id=None):
        with closing(self.registry._connect()) as c:
            global_ = self.settings(c=c)
            project = self.settings('project',project_id,c) if project_id else None
            session = self.settings('session',actor_id(harness,session_id),c)
        selected = session if session['enabled'] is not None else project if project and project['enabled'] is not None else global_
        return {'enabled':bool(selected['enabled']) and not global_['paused'],
                'basis':'global_pause' if global_['paused'] else selected['scope'],
                'revision':selected['revision'],'global_revision':global_['revision'],
                'read_allowed':True,'explicit_delegation_allowed':True}

    def configure(self, raw):
        scope, target, request_id=raw.get('scope','global'),raw.get('target',''),raw.get('request_id')
        if scope not in {'global','project','session'} or not isinstance(target,str) or (scope!='global' and not target) or (scope=='global' and target):
            raise ContractError('studio_participation_scope_invalid')
        if not isinstance(request_id,str) or not 1 <= len(request_id) <= 200:
            raise ContractError('studio_participation_request_required')
        enabled=raw.get('enabled')
        if type(enabled) is not bool and not (enabled is None and scope!='global'):
            raise ContractError('studio_participation_enabled_invalid')
        if type(raw.get('paused',False)) is not bool:
            raise ContractError('studio_participation_paused_invalid')
        fingerprint=hashlib.sha256(encoded(raw).encode()).hexdigest()
        with self.registry.transaction():
            c=self.registry._connect()
            prior=c.execute('SELECT * FROM studio_participation_requests WHERE request_id=?',(request_id,)).fetchone()
            if prior:
                if prior['fingerprint']!=fingerprint:raise ContractError('studio_participation_request_conflict')
                return {**json.loads(prior['result_json']),'replayed':True}
            current=self.settings(scope,target,c)
            if current['revision']!=raw.get('expected_revision'):raise ContractError('studio_participation_revision_conflict')
            if scope=='project':self.registry.get(target,include_archived=False)
            if scope=='session' and not c.execute('SELECT 1 FROM studio_session_actors WHERE actor_id=?',(target,)).fetchone():
                raise ContractError('studio_session_not_found')
            value={'enabled':enabled,'paused':raw.get('paused',False) if scope=='global' else False,'updated_at':utc_now()}
            c.execute('INSERT OR REPLACE INTO studio_participation VALUES (?,?,?,?)',(scope,target,current['revision']+1,encoded(value)))
            if hasattr(self.studio, 'native_recovery'):
                self.studio.native_recovery.record_policy(c, scope, target, current['revision'] + 1, value)
            result={'ok':True,'settings':self.settings(scope,target,c),'existing_runs':'continue'}
            c.execute('INSERT INTO studio_participation_requests VALUES (?,?,?)',(request_id,fingerprint,encoded(result)))
        return result

    def observe(self, raw):
        harness, session = raw.get('harness','codex'),raw.get('session_id')
        if not valid_kind(harness) or not isinstance(session,str) or not 1<=len(session)<=256 or session.startswith('readonly-'):
            return None
        identity=actor_id(harness,session)
        event_id=raw.get('event_id')
        if not isinstance(event_id,str) or not 1<=len(event_id)<=200:
            raise ContractError('studio_session_event_id_required')
        kind=raw.get('kind','context')
        stamp=raw.get('occurred_at') or utc_now()
        try:
            parsed=datetime.fromisoformat(stamp.replace('Z','+00:00'))
            if parsed.tzinfo is None:raise ValueError('timezone required')
            stamp=parsed.astimezone(timezone.utc).isoformat()
        except (ValueError,TypeError,AttributeError):
            raise ContractError('studio_session_timestamp_invalid')
        states={'SessionStart':'unknown','UserPromptSubmit':'running','SubagentStart':'running',
                'Stop':'idle','SessionEnd':'idle','failure':'failed','cancelled':'cancelled','unknown':'unknown'}
        # Copy only known scalar metadata, never raw Hook inputs/tool arguments.
        fields={k:str(raw[k])[:maximum] for k,maximum in [('cwd',4096),('project_id',200),('title',200),('summary',600),('source_id',256),('turn_id',256)] if raw.get(k)}
        with self.registry.transaction():
            c=self.registry._connect()
            if c.execute('SELECT 1 FROM studio_session_events WHERE event_id=?',(event_id,)).fetchone():return identity
            row=c.execute('SELECT payload_json FROM studio_session_actors WHERE actor_id=?',(identity,)).fetchone()
            prior=json.loads(row[0]) if row else {}
            if kind == 'failure' and (timestamp(prior.get('updated_at')) or 0) <= parsed.timestamp():
                fields = {**{k: prior[k] for k in ('cwd', 'project_id', 'title', 'summary', 'source_id') if prior.get(k)}, **fields}
            value={**prior,**fields,'actor_id':identity,'harness':harness,'session_id':session,
                   'updated_at':stamp,'state':states.get(kind,prior.get('state','unknown')),
                   'status_basis':'lifecycle_'+kind if kind in states else prior.get('status_basis','context_only')}
            if (timestamp(prior.get('updated_at')) or 0) > parsed.timestamp():
                value=prior
            c.execute('INSERT OR REPLACE INTO studio_session_actors VALUES (?,?)',(identity,encoded(value)))
            c.execute('INSERT INTO studio_session_events(event_id,actor_id,kind,created_at,payload_json) VALUES (?,?,?,?,?)',
                      (event_id,identity,kind,stamp,encoded(fields)))
        return identity

    def drain_hooks(self):
        directory=self.studio.root/'incoming'
        if not directory.is_dir():return
        for path in sorted(directory.glob('*.json'))[:80]:
            try:
                if path.stat().st_size>16000:continue
                raw=json.loads(path.read_text(encoding='utf8'))
                self.observe(raw)
                path.unlink()
            except (OSError,ValueError,ContractError):
                continue

    def snapshot(self, project_id=None, harness='codex', session_id=None):
        self.drain_hooks()
        # One shared scan for all page and MCP readers, not one per actor/frame.
        with self._cache_lock:
            if self._cache is None or time.monotonic()-self._cache_at>15:
                # One metadata pass across registered sources. Public directory
                # pages stay bounded; a long-lived active task must not vanish
                # simply because 50 newer files were touched.
                self._cache=self.studio.service.session_directory.catalog(limit=50,include_all=True)
                self._cache_at=time.monotonic()
            catalog=self._cache
        with closing(self.registry._connect()) as c:
            stored=[json.loads(r[0]) for r in c.execute('SELECT payload_json FROM studio_session_actors')]
            managed={(r['harness'],r['session_id']) for r in c.execute("SELECT json_extract(payload_json,'$.executor_kind') AS harness,json_extract(payload_json,'$.session_id') AS session_id FROM studio_runs")}
        items={s['actor_id']:s for s in stored}
        for session in catalog['sessions']:
            if not session.get('session_id'):continue
            identity=actor_id(session['harness'],session['session_id'])
            if (session['harness'],session['session_id']) in managed:continue
            try:
                observed=self.studio.service.session_directory.observation(session['source_id'])
            except (OSError,ValueError,ContractError):
                observed={}
            if observed.get('auxiliary'):
                items.pop(identity,None)
                continue
            if identity not in items:
                self.observe({'harness':session['harness'],'session_id':session['session_id'],
                    'event_id':'discovered:'+identity,'kind':'discovered','source_id':session['source_id'],
                    'occurred_at':session.get('modified_at'),
                    'cwd':session.get('cwd'),'project_id':session.get('project_id'),'title':session['title']})
            items[identity]={**session,**items.get(identity,{}),'actor_id':identity,
                'title':session['title'],'source_id':session['source_id'],'read_url':session['read_url'],
                'project_id':session['project_id'] if not session.get('membership_conflict') else None}
            if session.get('title_source')=='session_id' and observed.get('first_message'):
                items[identity]['title']=observed['first_message']
            prior_at=items[identity].get('updated_at')
            observed_at=observed.get('event_at')
            if observed.get('state')!='unknown' and observed.get('state') and (not prior_at or
                items[identity].get('status_basis') in {None,'context_only','service_restart'} or
                timestamp(observed_at) is not None and timestamp(observed_at) >= (timestamp(prior_at) or 0)):
                items[identity].update(state=observed['state'],status_basis=observed['status_basis'],lifecycle_at=observed_at)
            items[identity]['last_tool']=observed.get('tool')
        actors=[]
        for identity,item in items.items():
            if (item['harness'],item['session_id']) in managed:continue
            if project_id and item.get('project_id')!=project_id:continue
            state=item.get('state','unknown')
            try:
                stale_seconds=max(60,float(os.environ.get('AP_VIBE_SESSION_STALE_SECONDS','1800')))
            except ValueError:
                stale_seconds=1800
            last_seen=max(timestamp(item.get(k)) or 0 for k in ('updated_at','modified_at','lifecycle_at'))
            if state=='running' and time.time()-last_seen>stale_seconds:
                item={**item,'last_known_state':'running','status_basis':'old_lifecycle_without_recent_activity'}
                state='unknown'
            from .studio_presence import location
            room,label,animation=location({'state':state},{'kind':'tool','tool':item.get('last_tool')})
            if state=='idle':room,label,animation='rest','本轮已结束','rest'
            if state=='unknown':room,label,animation='waiting','已发现 · 状态待确认','idle'
            actors.append({**item,'state':state,'active':state=='running',
                'name':item.get('title') or (item['harness']+' 会话 · '+item['session_id'][:8]),
                'executor_kind':item['harness'],'appearance_id':item.get('appearance_id') or ('gpt-v3' if item['harness']=='codex' else 'claude-v3'),
                'collaboration':self.effective(item['harness'],item['session_id'],item.get('project_id')),
                'room':room,'animation':animation,'activity_label':label,
                'capabilities':{'read':True,'inbox':True,'stop':False,'direct_steer':False}})
        actors.sort(key=lambda a:(a['active'],a.get('updated_at') or a.get('modified_at') or ''),reverse=True)
        result = {'ok':True,'actors':actors,'settings':self.settings(),'catalog_total':catalog['total'],
                  'more_history_tool':'ap_vibe_sessions','observed_at':utc_now()}
        if session_id:
            # Display filters must not change the caller's actual project policy.
            caller = items.get(actor_id(harness, session_id), {})
            result['current_session'] = self.context(harness, session_id, caller.get('project_id'))
        else:
            result['current_session'] = None
            result['identity_hint'] = '填写当前真实 harness/session_id 读取本会话有效策略；settings 只是全局默认值。'
        return result

    def context(self,harness,session_id,project_id=None):
        mailbox=self.inbox(harness,session_id)
        return {'actor_id':actor_id(harness,session_id),'harness':harness,'session_id':session_id,
                'policy':self.effective(harness,session_id,project_id),
                'message_count':len(mailbox.get('messages',[])),
                'recent_messages':[{'message_id':m['message_id'],'summary':m.get('body','')[:220]} for m in mailbox.get('messages',[])[-3:]],
                'directory_tool':'ap_vibe_studio_context','inbox_tool':'ap_vibe_session_inbox',
                'reference':'references/agent-collaboration.md'}

    def check_message_policy(self, raw, recipients):
        """Explicit messages work regardless of policy; automatic advice is opt-in."""
        if not raw.get('automatic'):return
        if type(raw['automatic']) is not bool:raise ContractError('studio_automatic_flag_invalid')
        origin=raw.get('collaboration_origin')
        if not isinstance(origin,dict) or not origin.get('session_id') or not valid_kind(origin.get('harness')):
            raise ContractError('studio_collaboration_origin_invalid')
        with closing(self.registry._connect()) as c:
            source=c.execute('SELECT payload_json FROM studio_session_actors WHERE actor_id=?',
                (actor_id(origin['harness'],origin['session_id']),)).fetchone()
            project=json.loads(source[0]).get('project_id') if source else None
            if not self.effective(origin['harness'],origin['session_id'],project)['enabled']:
                raise ContractError('automatic_collaboration_paused')
            for recipient in recipients:
                target=c.execute('SELECT payload_json FROM studio_session_actors WHERE actor_id=?',(recipient,)).fetchone()
                if target:
                    item=json.loads(target[0])
                    if not self.effective(item['harness'],item['session_id'],item.get('project_id'))['enabled']:
                        raise ContractError('recipient_automatic_collaboration_paused')

    def inbox(self,harness,session_id,after=0):
        if not valid_kind(harness) or not isinstance(session_id,str) or not session_id:
            raise ContractError('studio_session_identity_required')
        identity=actor_id(harness,session_id)
        return {**self.studio.service.collaboration.inbox(identity,identity,after=after,limit=20),
                'actor_id':identity,'read_only':True}
