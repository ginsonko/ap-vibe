"""Read-only, evidence-bearing spatial projection of managed runs."""
from contextlib import closing
import json

from .contracts import utc_now


ACTIVE = ('waiting', 'starting', 'running', 'cancelling')
READ_TOOLS = {'Read', 'Glob', 'Grep', 'WebFetch', 'WebSearch', 'web_search',
              'ap_vibe_context', 'ap_vibe_read', 'ap_vibe_projects'}
WRITE_TOOLS = {'Write', 'Edit', 'MultiEdit', 'file_change', 'apply_patch',
               'ap_vibe_update', 'ap_vibe_update_file', 'ap_vibe_classify'}


def location(run, event, review=False):
    state = run['state']
    if state in {'failed', 'uncertain', 'interrupted', 'changes_requested', 'cancelling'}:
        return 'waiting', '需要留意', 'wait'
    if state == 'waiting':
        return 'waiting', '等待上游', 'wait'
    if state == 'awaiting_review':
        return 'review', '成果待验收', 'wait'
    if state in {'completed', 'cancelled'}:
        return 'rest', '已验收' if state == 'completed' else '已停止', 'rest'
    if review:
        return 'review', '独立检查中', 'work'
    if state == 'starting':
        return 'planning', '正在启动', 'wait'
    if state != 'running':
        return 'waiting', '状态待确认', 'wait'
    if event and event['kind'] == 'tool':
        tool = (event.get('tool') or '').rsplit('__', 1)[-1]
        if tool in READ_TOOLS:
            return 'library', '正在查阅', 'read'
        if tool in WRITE_TOOLS:
            return 'engineering', '正在写入', 'work'
    return 'planning', '执行中', 'work'


def snapshot(studio):
    with closing(studio.registry._connect()) as c:
        # History limits must never hide an older still-active run.
        rows = c.execute('''SELECT r.*, e.seq, e.kind, e.created_at AS event_at,
                           e.payload_json AS event_json
            FROM studio_runs r LEFT JOIN studio_events e ON e.seq=(
                SELECT MAX(seq) FROM studio_events WHERE run_id=r.run_id)
            WHERE r.state IN ('waiting','starting','running','cancelling')
               OR r.rowid IN (SELECT source_row FROM (
                   SELECT rowid AS source_row, ROW_NUMBER() OVER (
                       PARTITION BY agent_id ORDER BY
                       COALESCE(json_extract(payload_json,'$.updated_at'),
                                json_extract(payload_json,'$.created_at'),'') DESC, rowid DESC
                   ) AS recent FROM studio_runs) WHERE recent=1)
            ORDER BY r.rowid DESC''').fetchall()
        task_ids = list({json.loads(row['payload_json']).get('logical_task_id') for row in rows} - {None})
        task_rows = c.execute('''SELECT task_id,payload_json FROM studio_tasks
            WHERE task_id IN (SELECT value FROM json_each(?))''', (json.dumps(task_ids),)).fetchall()
    tasks = {r['task_id']: json.loads(r['payload_json']) for r in task_rows}
    runs = []
    for row in rows:
        value = json.loads(row['payload_json'])
        run = {key: value.get(key) for key in ('agent_id', 'name', 'appearance_id', 'model',
               'executor_kind', 'project_id', 'created_at', 'updated_at', 'logical_task_id', 'depends_on')}
        run.update(run_id=row['run_id'], state=row['state'], prompt=value.get('prompt', '')[:500])
        event = None
        if row['event_json']:
            payload = json.loads(row['event_json'])
            event = {'seq': row['seq'], 'kind': row['kind'], 'created_at': row['event_at'],
                     'text': str(payload.get('text', ''))[:500], 'tool': payload.get('tool')}
        task = tasks.get(run.get('logical_task_id'), {})
        run['room'], run['activity_label'], run['animation'] = location(run, event, bool(task.get('review_of_task_id')))
        run['last_event'] = event
        run['active'] = row['state'] in ACTIVE
        runs.append(run)
    collaboration = getattr(studio.service, 'collaboration', None)
    messages = collaboration.list()['messages'][:80] if collaboration else []
    messages = [{key: m.get(key) for key in ('message_id', 'sender', 'recipient', 'created_at', 'task_id')}
                | {'body': m['body'][:300], 'truncated': len(m['body']) > 300} for m in messages]
    return {'ok': True, 'observed_at': utc_now(), 'runs': runs, 'messages': messages,
            'active_count': sum(r['active'] for r in runs), 'source': 'persisted_managed_runs'}
