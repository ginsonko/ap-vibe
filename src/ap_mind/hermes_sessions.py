"""Hermes Desktop/CLI public history; no native imports or database writes."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path

from .contracts import ContractError, utc_now


def message_text(value):
    from .external_sessions import public_text
    if isinstance(value,bytes):value=value.decode('utf-8',errors='replace')
    if isinstance(value,str) and value.startswith('\x00json:'):
        try:value=json.loads(value[6:])
        except ValueError:return ''  # Never expose a broken structured reasoning/attachment payload.
    return public_text(value)


def columns(conn,table):
    return {r[1] for r in conn.execute('PRAGMA table_info('+table+')')}


def public_filter(conn):
    cols=columns(conn,'messages')
    result="session_id=? AND role IN ('user','assistant') AND content IS NOT NULL"
    if '_compressed_summary' in cols: result+=' AND COALESCE(_compressed_summary,0)=0'
    if 'display_kind' in cols: result+=" AND COALESCE(display_kind,'') NOT IN ('hidden','reasoning','thinking')"
    return result,cols


def sources(path,files,limit):
    from .external_sessions import readonly_db,stamp,public_text
    result=[]
    with closing(readonly_db(path)) as conn:
        cols=columns(conn,'sessions');where,_=public_filter(conn)
        optional=lambda name,default:'s.'+name if name in cols else default+' AS '+name
        rows=conn.execute('SELECT s.id,s.model,s.started_at,'+','.join([
            optional('title',"''"),optional('cwd',"''"),optional('parent_session_id','NULL'),
            "COALESCE((SELECT MAX(timestamp) FROM messages WHERE session_id=s.id),s.started_at) AS modified"])+
            ' FROM sessions s ORDER BY modified DESC LIMIT ?',(limit+1,)).fetchall()
        for row in rows:
            key='hermes-'+hashlib.sha256((os.path.normcase(str(path))+':'+row['id']).encode()).hexdigest()[:32]
            preview=conn.execute('SELECT substr(CAST(content AS BLOB),1,262144) FROM messages WHERE '+where+
                " AND role='user' ORDER BY timestamp,id LIMIT 1",(row['id'],)).fetchone()
            model=public_text(row['model'] or '')[:200] or None
            result.append({'source_id':key,'harness':'hermes','session_id':row['id'],
                'title':(public_text(row['title']) if row['title'] else message_text(preview[0]) if preview else '')[:200] or 'Hermes · '+row['id'][:8],
                'title_source':'native_session','cwd':str(row['cwd'] or ''),
                'model':model,'model_source':'native_session' if model else None,
                'observed_models':[model] if model else [],'parent_session_id':row['parent_session_id'],
                'modified_at':stamp(row['modified']),'available':True})
            files[key]=('hermes',path,row['id'])
    return result


def read(path,session,*,after=None,before=None,expected_generation=None,limit=20):
    from .external_sessions import readonly_db,stamp,public_text
    if type(limit) is not int or not 1<=limit<=50:raise ContractError('session_page_limit_invalid')
    if after is not None and before is not None:raise ContractError('session_cursor_conflict')
    if any(type(v) is not int or v<0 for v in (after,before) if v is not None):raise ContractError('session_cursor_invalid')
    with closing(readonly_db(path)) as conn:
        conn.execute('BEGIN');where,cols=public_filter(conn)
        count=conn.execute('SELECT count(*) FROM messages WHERE '+where,(session,)).fetchone()[0]
        # Native messages can be updated in place. Include WAL identity so a
        # same-length streaming replacement cannot leave a stale public window.
        signatures=[]
        for item in (path,Path(str(path)+'-wal')):
            try:
                stat=item.stat();signatures.append((stat.st_ino,stat.st_mtime_ns,stat.st_size))
            except FileNotFoundError:signatures.append(None)
        current=hashlib.sha256(repr((session,count,signatures)).encode()).hexdigest()[:24]
        reset=bool(expected_generation and expected_generation!=current)
        if reset:after=before=None
        end=min(count,before) if before is not None else count
        start=min(count,after) if after is not None else max(0,end-limit)
        take=min(limit,count-start) if after is not None else end-start
        active='active' if 'active' in cols else '1 AS active'
        rows=conn.execute('SELECT id,role,substr(CAST(content AS BLOB),1,262144) AS content,timestamp,'+active+
            ' FROM messages WHERE '+where+' ORDER BY timestamp,id LIMIT ? OFFSET ?',(session,take,start)).fetchall()
        events=[]
        for index,row in enumerate(rows,start):
            text=message_text(row['content'])
            if text.strip():events.append({'id':str(row['id']),'role':row['role'],'text':text,
                'timestamp':stamp(row['timestamp']),'context_archived':not bool(row['active']),
                'text_may_be_truncated':len(row['content'])>64000,'offset':index,'end_offset':index+1})
    return {'ok':True,'events':events,'generation':current,'reset':reset,'cursor':start+len(rows),
        'history_before':start,'has_older':start>0,'has_more':start+len(rows)<count,'source_size':count,
        'read_at':utc_now(),'read_only':True,'history_scope':'native_sqlite_public_messages'}
