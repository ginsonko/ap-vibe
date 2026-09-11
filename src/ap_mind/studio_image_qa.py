"""Durable per-image review with bounded workers and explicit uncertain requests."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import threading
import uuid

from .contracts import ContractError, utc_now
from .studio_budget import encoded
from .studio_vision import content, execute, file_record, parse_results
from .teacher_settings import protect


class StudioImageQA:
    def __init__(self,studio):
        self.studio,self.registry=studio,studio.registry
        self.lock=threading.RLock();self.workers={}
        self.executor=execute
        with closing(self.registry._connect()) as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS studio_image_batches(
                    batch_id TEXT PRIMARY KEY,state TEXT NOT NULL,revision INTEGER NOT NULL,payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS studio_image_items(
                    batch_id TEXT NOT NULL,item_id TEXT NOT NULL,state TEXT NOT NULL,payload_json TEXT NOT NULL,
                    PRIMARY KEY(batch_id,item_id));
                CREATE INDEX IF NOT EXISTS studio_image_queue ON studio_image_items(batch_id,state);
                CREATE TABLE IF NOT EXISTS studio_image_requests(
                    request_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,state TEXT NOT NULL,payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS studio_image_receipts(
                    request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result_json TEXT NOT NULL);
            ''')
            # Only submitted requests may have reached the provider. Occupied
            # but unsubmitted work is safe to return to its original phase.
            c.execute("""UPDATE studio_image_items SET state=CASE WHEN json_extract(payload_json,'$.phase')='review'
                THEN 'review_pending' ELSE 'pending' END WHERE state='running' AND
                json_extract(payload_json,'$.active_request') IN (SELECT request_id FROM studio_image_requests WHERE state='reserved')""")
            c.execute("UPDATE studio_image_requests SET state='not_submitted' WHERE state='reserved'")
            c.execute("UPDATE studio_image_requests SET state='uncertain' WHERE state='submitted'")
            c.execute("UPDATE studio_image_items SET state='uncertain' WHERE state='running'")
            c.commit()

    def _batch(self,c,batch_id):
        row=c.execute('SELECT * FROM studio_image_batches WHERE batch_id=?',(batch_id,)).fetchone()
        if not row:raise ContractError('image_batch_not_found')
        return {**json.loads(row['payload_json']),'batch_id':batch_id,'state':row['state'],'revision':row['revision']}

    def _profile(self,c,agent_id):
        row=c.execute('SELECT * FROM studio_agents WHERE agent_id=?',(agent_id,)).fetchone()
        if not row:raise ContractError('agent_not_found')
        value=json.loads(row['public_json'])
        if value.get('archived') or value.get('auth_mode')=='local_login' or not row['secret']:
            raise ContractError('image_review_api_profile_required')
        return value,row['secret']

    def _receipt(self,c,raw):
        request_id=raw.get('request_id')
        if not isinstance(request_id,str) or not 1<=len(request_id)<=200:raise ContractError('image_request_id_required')
        fingerprint=hashlib.sha256(encoded(raw).encode()).hexdigest()
        row=c.execute('SELECT * FROM studio_image_receipts WHERE request_id=?',(request_id,)).fetchone()
        if row and row['fingerprint']!=fingerprint:raise ContractError('image_request_conflict')
        return fingerprint,({**json.loads(row['result_json']),'replayed':True} if row else None)

    def create(self,raw):
        # Validate and hash first; the complete manifest is committed atomically.
        values=raw.get('items')
        if values is None and raw.get('directory'):
            root=Path(raw['directory']).expanduser().resolve()
            if not root.is_dir():raise ContractError('image_directory_not_found')
            values=[{'id':str(p.relative_to(root)),'product_id':raw.get('product_id'),
                'requirements':raw.get('requirements'),'path':str(p),'reference_path':raw.get('reference_path')}
                for p in sorted(root.rglob('*')) if p.is_file() and p.suffix.lower() in {'.png','.jpg','.jpeg','.webp'} and p.resolve().is_relative_to(root)]
        if not isinstance(values,list) or not values:raise ContractError('image_items_required')
        if not isinstance(raw.get('title'),str) or not raw['title'].strip():raise ContractError('image_title_required')
        if raw.get('project_id'):self.registry.get(raw['project_id'],include_archived=False)
        with closing(self.registry._connect()) as c:
            _,prior=self._receipt(c,raw)
            if prior:return prior
            self._profile(c,raw.get('agent_id'))
            if raw.get('reviewer_agent_id'):self._profile(c,raw['reviewer_agent_id'])
        for field,default in [('batch_size',4),('concurrency',2)]:
            if type(raw.get(field,default)) is not int or raw.get(field,default)<1:raise ContractError('image_'+field+'_invalid')
        sample=raw.get('sample_percent',10)
        if type(sample) not in (float,int) or not 0<=sample<=100:raise ContractError('image_sample_invalid')
        items=[];seen=set();file_cache={}
        def describe(path):
            if path not in file_cache:file_cache[path]=file_record(path)
            return file_cache[path]
        for value in values:
            if not isinstance(value,dict) or not isinstance(value.get('id'),str) or not 1<=len(value['id'])<=200 or value['id'] in seen:
                raise ContractError('image_duplicate_or_invalid_item_id')
            seen.add(value['id'])
            for key in ('product_id','requirements'):
                if not isinstance(value.get(key),str) or not value[key].strip():raise ContractError('image_'+key+'_required')
            item={k:value[k] for k in ('id','product_id','requirements')} | {'history':[],'phase':'initial'}
            try:
                item.update(image=describe(value['path']),reference=describe(value['reference_path']) if value.get('reference_path') else None)
            except (OSError,ContractError,TypeError,KeyError) as exc:
                item.update(image={'path':str(value.get('path',''))},reference=None,error=str(exc),registration_error=True)
            items.append(item)
        with self.lock,self.registry.transaction():
            c=self.registry._connect();fingerprint,prior=self._receipt(c,raw)
            if prior:return prior
            batch_id='image-batch-'+uuid.uuid4().hex
            value={k:raw.get(k) for k in ('title','project_id','agent_id','reviewer_agent_id')}
            value.update(batch_size=raw.get('batch_size',4),concurrency=raw.get('concurrency',2),sample_percent=sample,created_at=utc_now())
            c.execute('INSERT INTO studio_image_batches VALUES (?,?,?,?)',(batch_id,'draft',1,encoded(value)))
            c.executemany('INSERT INTO studio_image_items VALUES (?,?,?,?)',[(batch_id,v['id'],'error' if v.get('registration_error') else 'pending',encoded(v)) for v in items])
            result={'ok':True,'batch_id':batch_id,'state':'draft','revision':1,'item_count':len(items),'paid_request':False}
            c.execute('INSERT INTO studio_image_receipts VALUES (?,?,?)',(raw['request_id'],fingerprint,encoded(result)))
            return result

    def action(self,raw):
        action=raw.get('action')
        if action not in {'start','pause','resume','cancel','retry_unresolved'}:raise ContractError('image_action_invalid')
        with self.lock,self.registry.transaction():
            c=self.registry._connect();fingerprint,prior=self._receipt(c,raw)
            if prior:return prior
            batch=self._batch(c,raw.get('batch_id'))
            if raw.get('expected_revision')!=batch['revision']:raise ContractError('image_batch_revision_conflict')
            if batch['state']=='cancelled':raise ContractError('image_batch_cancelled')
            if action=='start' and batch['state']!='draft':raise ContractError('image_batch_already_started')
            if action=='retry_unresolved':
                if not raw.get('note'):raise ContractError('image_retry_note_required')
                # This is an explicit new paid attempt, never automatic replay.
                c.execute("UPDATE studio_image_items SET state=CASE WHEN json_extract(payload_json,'$.phase')='review' THEN 'review_pending' ELSE 'pending' END WHERE batch_id=? AND state IN ('error','uncertain') AND COALESCE(json_extract(payload_json,'$.registration_error'),0)=0",(batch['batch_id'],))
            state={'pause':'paused','cancel':'cancelled'}.get(action,'running')
            c.execute('UPDATE studio_image_batches SET state=?,revision=revision+1 WHERE batch_id=?',(state,batch['batch_id']))
            result={'ok':True,'batch_id':batch['batch_id'],'state':state,'revision':batch['revision']+1,
                    'inflight':'在途请求保留，可能仍会计费；已有结果不丢失。'}
            c.execute('INSERT INTO studio_image_receipts VALUES (?,?,?)',(raw['request_id'],fingerprint,encoded(result)))
            return result

    def list(self,batch_id=None,offset=0,limit=50):
        if type(offset) is not int or type(limit) is not int or offset<0 or not 1<=limit<=200:raise ContractError('image_page_invalid')
        with closing(self.registry._connect()) as c:
            ids=[batch_id] if batch_id else [r[0] for r in c.execute('SELECT batch_id FROM studio_image_batches ORDER BY rowid DESC LIMIT 100')]
            batches=[]
            for identity in ids:
                batch=self._batch(c,identity)
                batch['counts']={r[0]:r[1] for r in c.execute('SELECT state,COUNT(*) FROM studio_image_items WHERE batch_id=? GROUP BY state',(identity,))}
                batch['total']=sum(batch['counts'].values())
                batch['budget']=self.studio.budget.status(batch['agent_id'],c)
                batch['usage']=self.usage(identity,c)
                batches.append(batch)
            items=[];requests=[];more=False
            if batch_id:
                rows=c.execute('SELECT * FROM studio_image_items WHERE batch_id=? ORDER BY rowid LIMIT ? OFFSET ?',(batch_id,limit+1,offset)).fetchall()
                more=len(rows)>limit
                items=[{**json.loads(r['payload_json']),'state':r['state'],
                    'version':hashlib.sha256((r['state']+r['payload_json']).encode()).hexdigest()} for r in rows[:limit]]
                requests=[{**json.loads(r['payload_json']),'request_id':r['request_id'],'state':r['state']} for r in c.execute('SELECT * FROM studio_image_requests WHERE batch_id=? ORDER BY rowid DESC LIMIT 40',(batch_id,))]
        return {'ok':True,'batches':batches,'items':items,'requests':requests,'next_offset':offset+limit if more else None}

    def usage(self,batch_id,c):
        totals=c.execute("""SELECT COUNT(*) AS request_count,
            COALESCE(SUM(json_extract(payload_json,'$.usage.total_tokens')),0) AS tokens,
            COALESCE(SUM(CASE WHEN json_extract(payload_json,'$.usage.complete')=1 THEN 0 ELSE 1 END),0) AS unknown,
            COALESCE(SUM(CASE WHEN json_extract(payload_json,'$.estimate_state')='estimated' THEN 0 ELSE 1 END),0) AS unestimated
            FROM studio_usage_ledger WHERE run_id=?""",(batch_id,)).fetchone()
        amounts={r[0]:r[1] for r in c.execute("""SELECT json_extract(payload_json,'$.currency'),
            SUM(json_extract(payload_json,'$.amount')) FROM studio_usage_ledger WHERE run_id=? GROUP BY 1""",(batch_id,))}
        return {'request_count':totals['request_count'],'known_tokens':totals['tokens'],
            'unknown_usage_requests':totals['unknown'],'unestimated_requests':totals['unestimated'],
            'amounts_by_currency':amounts}

    def image(self,batch_id,item_id,reference=False):
        with closing(self.registry._connect()) as c:
            row=c.execute('SELECT payload_json FROM studio_image_items WHERE batch_id=? AND item_id=?',(batch_id,item_id)).fetchone()
            if not row:raise ContractError('image_item_not_found')
            record=json.loads(row[0]).get('reference' if reference else 'image')
        if not record or not record.get('sha256'):raise ContractError('image_preview_unavailable')
        data=Path(record['path']).read_bytes()
        if hashlib.sha256(data).hexdigest()!=record['sha256']:raise ContractError('image_changed_after_registration')
        return data,record['mime']

    def resolve(self,raw):
        if raw.get('verdict') not in {'passed','rejected'} or not isinstance(raw.get('reason'),str) or not raw['reason'].strip():
            raise ContractError('image_manual_reason_and_verdict_required')
        with self.lock,self.registry.transaction():
            c=self.registry._connect();fingerprint,prior=self._receipt(c,raw)
            if prior:return prior
            batch=self._batch(c,raw.get('batch_id'))
            row=c.execute('SELECT * FROM studio_image_items WHERE batch_id=? AND item_id=?',(batch['batch_id'],raw.get('item_id'))).fetchone()
            if not row:raise ContractError('image_item_not_found')
            if row['state'] in {'running','pending','review_pending'}:raise ContractError('image_item_still_active')
            version=hashlib.sha256((row['state']+row['payload_json']).encode()).hexdigest()
            if raw.get('expected_version')!=version:raise ContractError('image_item_changed_refresh_required')
            item=json.loads(row['payload_json'])
            item['history']=[*item.get('history',[]),{'phase':'manual','verdict':raw['verdict'],
                'reason':raw['reason'].strip(),'request_id':raw['request_id'],'actor':'工作台人工复核','at':utc_now()}]
            item.pop('error',None)
            c.execute('UPDATE studio_image_items SET state=?,payload_json=? WHERE batch_id=? AND item_id=?',
                (raw['verdict'],encoded(item),batch['batch_id'],item['id']))
            c.execute('UPDATE studio_image_batches SET revision=revision+1 WHERE batch_id=?',(batch['batch_id'],))
            if batch['state']=='needs_attention':
                unresolved=c.execute("SELECT 1 FROM studio_image_items WHERE batch_id=? AND state NOT IN ('passed','rejected') LIMIT 1",(batch['batch_id'],)).fetchone()
                if not unresolved:c.execute("UPDATE studio_image_batches SET state='completed' WHERE batch_id=?",(batch['batch_id'],))
            result={'ok':True,'batch_id':batch['batch_id'],'item_id':item['id'],'state':raw['verdict'],'paid_request':False}
            c.execute('INSERT INTO studio_image_receipts VALUES (?,?,?)',(raw['request_id'],fingerprint,encoded(result)))
            return result

    def tick(self):
        if self.studio._closing:return
        with self.lock:
            self.workers={k:v for k,v in self.workers.items() if v.is_alive()}
            with closing(self.registry._connect()) as c:
                batches=[self._batch(c,r[0]) for r in c.execute("SELECT batch_id FROM studio_image_batches WHERE state='running'")]
            for batch in batches:
                while sum(key.startswith(batch['batch_id']+':') for key in self.workers)<batch['concurrency']:
                    work=self.reserve(batch['batch_id'])
                    if not work:break
                    thread=threading.Thread(target=self._run,args=(work,),daemon=True,name='ap-vibe-image-review')
                    self.workers[work['request_id']]=thread;thread.start()

    def reserve(self,batch_id):
        with self.lock,self.registry.transaction():
            c=self.registry._connect();batch=self._batch(c,batch_id)
            if self.studio.maintenance.active(c):return None
            if batch['state']!='running':return None
            row=c.execute("SELECT * FROM studio_image_items WHERE batch_id=? AND state IN ('pending','review_pending') ORDER BY rowid LIMIT 1",(batch_id,)).fetchone()
            if not row:
                states={r[0] for r in c.execute('SELECT DISTINCT state FROM studio_image_items WHERE batch_id=?',(batch_id,))}
                if 'running' not in states:
                    state='needs_attention' if states&{'uncertain','error','needs_review'} else 'completed'
                    c.execute('UPDATE studio_image_batches SET state=?,revision=revision+1 WHERE batch_id=?',(state,batch_id))
                return None
            phase='review' if row['state']=='review_pending' else 'initial'
            agent_id=batch['reviewer_agent_id'] if phase=='review' else batch['agent_id']
            if self.studio.budget.check(agent_id,c):return None
            first=json.loads(row['payload_json'])
            rows=c.execute("SELECT * FROM studio_image_items WHERE batch_id=? AND state=? AND json_extract(payload_json,'$.product_id')=? ORDER BY rowid LIMIT ?",(batch_id,row['state'],first['product_id'],batch['batch_size'])).fetchall()
            items=[json.loads(r['payload_json']) for r in rows]
            profile,secret=self._profile(c,agent_id)
            request_id=batch_id+':'+uuid.uuid4().hex
            payload={'agent_id':agent_id,'model':profile['model'],'profile_revision':profile['revision'],
                'phase':phase,'item_ids':[v['id'] for v in items],'created_at':utc_now()}
            c.execute('INSERT INTO studio_image_requests VALUES (?,?,?,?)',(request_id,batch_id,'reserved',encoded(payload)))
            for item in items:
                item.update(active_request=request_id,phase=phase)
                c.execute("UPDATE studio_image_items SET state='running',payload_json=? WHERE batch_id=? AND item_id=?",(encoded(item),batch_id,item['id']))
            return {'request_id':request_id,'batch':batch,'profile':profile,'secret':secret,'items':items,'phase':phase}

    def _run(self,work):
        request_id=work['request_id'];agent_id=work['profile']['agent_id'];batch_id=work['batch']['batch_id']
        submitted=False;response=None
        try:
            input_errors={}
            parts=content(work['items'],input_errors)
            if input_errors:
                from .agent_studio import redact
                for item in work['items']:
                    if item['id'] in input_errors:
                        self.fail({**work,'items':[item]},'error',redact(input_errors[item['id']])[:600],finish_request=False)
                work={**work,'items':[item for item in work['items'] if item['id'] not in input_errors]}
                with self.lock,self.registry.transaction():
                    c=self.registry._connect()
                    row=c.execute('SELECT payload_json FROM studio_image_requests WHERE request_id=?',(request_id,)).fetchone()
                    payload=json.loads(row[0]);payload['reserved_item_ids']=payload['item_ids']
                    payload['item_ids']=[item['id'] for item in work['items']]
                    payload['skipped_before_submission']=list(input_errors)
                    c.execute('UPDATE studio_image_requests SET payload_json=? WHERE request_id=?',(encoded(payload),request_id))
                    if not work['items']:
                        self._finish_request(c,request_id,'not_submitted',None)
                        return
            with self.lock,self.registry.transaction():
                c=self.registry._connect()
                if self._batch(c,batch_id)['state']!='running' or self.studio.budget.check(agent_id,c):
                    self._unreserve(c,work)
                    return
                c.execute("UPDATE studio_image_requests SET state='submitted' WHERE request_id=?",(request_id,))
                self.studio.budget.observe(agent_id,batch_id,request_id)
            submitted=True
            response=self.executor(work['profile'],protect(work['secret'],decrypt=True).decode(),parts,request_id)
            self.studio.budget.observe(agent_id,batch_id,request_id,response.get('usage'),response.get('protocol','openai'))
            results=parse_results(response['text'],[v['id'] for v in work['items']])
            self.settle(work,results,response)
        except Exception as exc:
            from .agent_studio import redact
            reason=redact(str(exc))[:600]
            state='error' if not submitted or response is not None else 'uncertain'
            self.fail(work,state,reason,response)

    def _unreserve(self,c,work):
        for item in work['items']:
            c.execute("""UPDATE studio_image_items SET state=? WHERE batch_id=? AND item_id=?
                AND state='running' AND json_extract(payload_json,'$.active_request')=?""",
                ('review_pending' if work['phase']=='review' else 'pending',work['batch']['batch_id'],item['id'],work['request_id']))
        self._finish_request(c,work['request_id'],'not_submitted',None)

    def settle(self,work,results,response):
        result_map={v['id']:v for v in results};batch=work['batch'];request_id=work['request_id']
        with self.lock,self.registry.transaction():
            c=self.registry._connect()
            current=c.execute('SELECT state FROM studio_image_requests WHERE request_id=?',(request_id,)).fetchone()
            if current and current[0]=='completed':return
            for item in work['items']:
                row=c.execute('SELECT payload_json FROM studio_image_items WHERE batch_id=? AND item_id=?',(batch['batch_id'],item['id'])).fetchone()
                if json.loads(row[0]).get('active_request')!=request_id:continue
                result=result_map[item['id']]
                history=[*item.get('history',[]),{**result,'phase':work['phase'],'request_id':request_id,'agent_id':work['profile']['agent_id'],'model':work['profile']['model'],'at':utc_now()}]
                if work['phase']=='review':
                    first=next((r for r in reversed(history[:-1]) if r['phase']=='initial'),None)
                    state=result['verdict'] if first and first.get('verdict')==result['verdict'] and result['verdict']!='uncertain' else 'needs_review'
                else:
                    sampled=int(hashlib.sha256(item['id'].encode()).hexdigest()[:8],16)%10000 < batch['sample_percent']*100
                    needs=result['verdict']=='uncertain' or result.get('high_risk') or (result['verdict']=='passed' and sampled)
                    state=('review_pending' if batch.get('reviewer_agent_id') else 'needs_review') if needs else result['verdict']
                item.pop('error',None)
                c.execute('UPDATE studio_image_items SET state=?,payload_json=? WHERE batch_id=? AND item_id=?',
                    (state,encoded({**item,'history':history}),batch['batch_id'],item['id']))
            self._finish_request(c,request_id,'completed',response)

    def _finish_request(self,c,request_id,state,response):
        row=c.execute('SELECT payload_json FROM studio_image_requests WHERE request_id=?',(request_id,)).fetchone()
        payload=json.loads(row[0]);payload['finished_at']=utc_now()
        if response:
            payload.update({k:response.get(k) for k in ('provider_request_id','elapsed_seconds')})
            # Store bounded visible response for repair; never store reasoning.
            from .agent_studio import redact
            payload['response_text']=redact(str(response.get('text','')))[:24000]
        c.execute('UPDATE studio_image_requests SET state=?,payload_json=? WHERE request_id=?',(state,encoded(payload),request_id))

    def fail(self,work,state,reason,response=None,finish_request=True):
        with self.lock,self.registry.transaction():
            c=self.registry._connect()
            for item in work['items']:
                row=c.execute('SELECT payload_json FROM studio_image_items WHERE batch_id=? AND item_id=?',(work['batch']['batch_id'],item['id'])).fetchone()
                if json.loads(row[0]).get('active_request')!=work['request_id']:continue
                c.execute('UPDATE studio_image_items SET state=?,payload_json=? WHERE batch_id=? AND item_id=?',
                    (state,encoded({**item,'error':reason,'history':[*item.get('history',[]),{'phase':work['phase'],'request_id':work['request_id'],'agent_id':work['profile']['agent_id'],'model':work['profile']['model'],'error':reason,'state':state,'at':utc_now()}]}),work['batch']['batch_id'],item['id']))
            if finish_request:self._finish_request(c,work['request_id'],state,response)
