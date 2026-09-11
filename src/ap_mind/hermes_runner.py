"""Tail an isolated Hermes session without importing its optional runtime."""
from contextlib import closing
import sqlite3
import threading

from .external_sessions import readonly_db
from .hermes_sessions import message_text, public_filter


class HermesTail:
    def __init__(self,state,emit,session):
        self.path=state/'state.db';self.emit=emit;self.session=session
        self.lock=threading.Lock();self.cursor=0;self.completed_text=False
        self.baseline=self.usage()
        if self.path.is_file():
            with closing(readonly_db(self.path)) as conn:
                self.cursor=conn.execute('SELECT COALESCE(MAX(id),0) FROM messages').fetchone()[0]

    def usage(self):
        if not self.path.is_file():return {}
        try:
            with closing(readonly_db(self.path)) as conn:
                row=conn.execute('SELECT SUM(input_tokens),SUM(output_tokens),SUM(cache_read_tokens),SUM(cache_write_tokens) FROM sessions').fetchone()
                return {k:(v or 0) for k,v in zip(('input_tokens','output_tokens','cache_read_tokens','cache_write_tokens'),row)}
        except sqlite3.Error:return {}

    def flush(self):
        if not self.path.is_file():return
        with self.lock:
            try:
                with closing(readonly_db(self.path)) as conn:
                    conn.execute('BEGIN')
                    newest=conn.execute("SELECT id FROM sessions WHERE source='cli' ORDER BY started_at DESC LIMIT 1").fetchone()
                    if newest:self.session(newest[0])
                    where,_=public_filter(conn)
                    rows=conn.execute("SELECT id,role,CASE WHEN role='assistant' THEN substr(CAST(content AS BLOB),1,262144) ELSE NULL END AS content,tool_name "
                        "FROM messages WHERE id>? ORDER BY id LIMIT 200",(self.cursor,)).fetchall()
                    for row in rows:
                        if row['role']=='assistant':
                            visible=conn.execute('SELECT 1 FROM messages WHERE '+where+' AND id=?',(newest[0],row['id'])).fetchone() if newest else None
                            text=message_text(row['content']) if visible else ''
                            if text:self.emit('assistant',{'text':text});self.completed_text=True
                        elif row['role']=='tool' and row['tool_name']:
                            name=str(row['tool_name'])[:200]
                            self.emit('tool',{'tool':name,'text':'调用工具：'+name})
                        self.cursor=row['id']
            except (OSError,sqlite3.Error):return

    def final_usage(self):
        current=self.usage()
        return {k:max(0,v-self.baseline.get(k,0)) for k,v in current.items()}
