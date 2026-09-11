"""A presentation journal. Reading/replaying never executes work or consumes mail."""
from contextlib import closing
import hashlib
import json
import threading
import time

from .contracts import ContractError,utc_now
from .studio_budget import encoded


class StudioReplay:
    def __init__(self,studio):
        self.studio,self.registry=studio,studio.registry
        self.lock=threading.RLock();self.last_tick=0
        with closing(self.registry._connect()) as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS studio_replay_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,kind TEXT NOT NULL,created_at TEXT NOT NULL,payload_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS studio_replay_time ON studio_replay_events(created_at,seq);
                CREATE TABLE IF NOT EXISTS studio_replay_actors(actor_id TEXT PRIMARY KEY,signature TEXT NOT NULL,payload_json TEXT NOT NULL);
            ''')
            c.commit()

    def tick(self):
        if self.studio._closing or time.monotonic()-self.last_tick<4:return
        self.last_tick=time.monotonic()
        from .studio_presence import snapshot
        self.capture(snapshot(self.studio))

    def capture(self,snapshot):
        actors=[]
        for run in snapshot.get('runs',[]):
            actors.append({'actor_id':'run:'+run['run_id'],**{k:run.get(k) for k in
                ('name','agent_id','model','appearance_id','project_id','room','state','animation','activity_label','active','run_id')},
                'summary':run.get('prompt','')[:300]})
        actors.extend({k:a.get(k) for k in ('actor_id','name','model','appearance_id','project_id','room','state','animation',
            'activity_label','active','harness','session_id','source_id','summary')} for a in snapshot.get('ordinary',{}).get('actors',[]))
        if snapshot.get('manager_actor'):actors.append(snapshot['manager_actor'])
        actors.extend(snapshot.get('batch_actors',[]))
        with self.lock,self.registry.transaction():
            c=self.registry._connect()
            for actor in actors:
                identity=actor['actor_id']
                signature=hashlib.sha256(encoded({k:actor.get(k) for k in ('name','room','state','animation','summary','appearance_id')}).encode()).hexdigest()
                prior=c.execute('SELECT * FROM studio_replay_actors WHERE actor_id=?',(identity,)).fetchone()
                if prior and prior['signature']==signature:continue
                previous=json.loads(prior['payload_json']) if prior else None
                c.execute('INSERT OR REPLACE INTO studio_replay_actors VALUES (?,?,?)',(identity,signature,encoded(actor)))
                payload={'actor':actor,'previous':previous,'timing_basis':'observed_transition'}
                c.execute('INSERT INTO studio_replay_events(event_id,kind,created_at,payload_json) VALUES (?,?,?,?)',
                    ('presence:'+identity+':'+utc_now(),'move' if previous and actor['room']!=previous['room'] else 'state',utc_now(),encoded(payload)))

    def message(self,raw,message_id):
        with self.lock,self.registry.transaction():
            c=self.registry._connect()
            value={k:raw.get(k) for k in ('sender','recipient','body','task_id')}
            value.update(message_id=message_id,timing_basis='message_saved')
            from .agent_studio import redact
            value['body']=redact(value['body'])[:16000]
            for side in ('sender','recipient'):
                # Task scope selects the recipient's run, never an unrelated
                # sender. One profile can have several simultaneously visible runs.
                row=c.execute("""SELECT payload_json FROM studio_replay_actors
                    WHERE actor_id=? OR json_extract(payload_json,'$.run_id')=?
                       OR json_extract(payload_json,'$.agent_id')=?
                    ORDER BY CASE WHEN actor_id=? THEN 0
                        WHEN json_extract(payload_json,'$.run_id')=? THEN 1 ELSE 2 END,
                        rowid DESC LIMIT 1""",(value[side],value[side],value[side],value[side],
                            value.get('task_id') if side=='recipient' else value[side])).fetchone()
                if row:value[side+'_actor']=json.loads(row[0])
            c.execute('INSERT OR IGNORE INTO studio_replay_events(event_id,kind,created_at,payload_json) VALUES (?,?,?,?)',
                ('message:'+message_id,'message',utc_now(),encoded(value)))

    def list(self,after=0,limit=100,since=None,interactions_only=False):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=200:raise ContractError('studio_replay_page_invalid')
        with closing(self.registry._connect()) as c:
            rows=c.execute("""SELECT * FROM studio_replay_events WHERE seq>? AND (? IS NULL OR created_at>=?)
                AND (?=0 OR kind IN ('message','move') OR
                    json_extract(payload_json,'$.previous.state') IS NOT json_extract(payload_json,'$.actor.state')
                    AND json_type(payload_json,'$.previous')='object') ORDER BY seq LIMIT ?""",
                (after,since,since,int(interactions_only),limit+1)).fetchall()
            more=len(rows)>limit;rows=rows[:limit]
            events=[{'seq':r['seq'],'event_id':r['event_id'],'kind':r['kind'],'created_at':r['created_at'],**json.loads(r['payload_json'])} for r in rows]
        return {'ok':True,'events':events,'next_cursor':events[-1]['seq'] if events else after,'has_more':more,
            'read_only':True,'meaning':'按已记录的公开工作事件演绎；记录开始前的走位未知。等待折叠，动作正常播放，后台任务不受回放影响。'}
