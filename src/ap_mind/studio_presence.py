"""Read-only, evidence-bearing spatial projection of managed runs."""
from contextlib import closing
import json

from .contracts import utc_now
from .studio_activity import event_activity


ACTIVE = ('waiting', 'starting', 'running', 'cancelling')



def location(run, event, review=False):
    state = run['state']
    if state == 'budget_paused':
        return 'rest', '饿昏了 · 等待投喂', 'rest'
    if state in {'failed', 'uncertain', 'interrupted', 'changes_requested', 'cancelling'}:
        return 'waiting', '需要留意', 'wait'
    if state == 'waiting':
        return 'waiting', '等待上游', 'wait'
    if state == 'awaiting_review':
        return 'review', '成果待验收', 'wait'
    if state in {'completed', 'cancelled'}:
        return 'rest', '已验收' if state == 'completed' else '已停止', 'rest'
    if state == 'starting':
        return 'planning', '正在启动', 'wait'
    if state != 'running':
        return 'waiting', '状态待确认', 'wait'
    activity = event_activity(event)
    if activity == 'read':
        return 'library', '正在查阅', 'read'
    if activity == 'write':
        return 'engineering', '正在编写', 'work'
    if activity == 'test':
        return 'review', '正在测试', 'work'
    if activity == 'discuss':
        return 'planning', '正在交流', 'work'
    if review:
        return 'review', '独立检查中', 'work'
    return 'planning', '执行中', 'work'


def snapshot(studio):
    with closing(studio.registry._connect()) as c:
        c.create_function('spatial_activity', 2, lambda kind, raw: event_activity({**json.loads(raw), 'kind': kind}))
        # History limits must never hide an older still-active run.
        rows = c.execute('''SELECT r.*, e.seq, e.kind, e.created_at AS event_at,
                           e.payload_json AS event_json, a.seq AS activity_seq,
                           a.created_at AS activity_at, a.payload_json AS activity_json, a.kind AS activity_kind
            FROM studio_runs r LEFT JOIN studio_events e ON e.seq=(
                SELECT MAX(seq) FROM studio_events WHERE run_id=r.run_id)
            LEFT JOIN studio_events a ON a.seq=(
                SELECT MAX(seq) FROM studio_events WHERE run_id=r.run_id
                AND kind IN ('tool','tool_result') AND spatial_activity(kind,payload_json) IS NOT NULL)
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
               'executor_kind', 'project_id', 'created_at', 'updated_at', 'logical_task_id', 'depends_on', 'coordination_only')}
        run.update(run_id=row['run_id'], state=row['state'], prompt=value.get('prompt', '')[:500])
        event = None
        if row['event_json']:
            payload = json.loads(row['event_json'])
            event = {'seq': row['seq'], 'kind': row['kind'], 'created_at': row['event_at'],
                     'text': str(payload.get('text', ''))[:500], 'tool': payload.get('tool')}
        task = tasks.get(run.get('logical_task_id'), {})
        run['title'] = task.get('title') or ('协调工作安排' if value.get('coordination_only') else '')
        activity = None
        if row['activity_json']:
            payload = json.loads(row['activity_json'])
            activity = {'kind': row['activity_kind'], 'tool': payload.get('tool'), 'activity': event_activity({**payload, 'kind': row['activity_kind']}),
                        'seq': row['activity_seq'], 'created_at': row['activity_at']}
        run['room'], run['activity_label'], run['animation'] = location(run, activity, bool(task.get('review_of_task_id')))
        run['last_activity'] = activity
        run['last_event'] = event
        run['active'] = row['state'] in ACTIVE
        runs.append(run)
    collaboration = getattr(studio.service, 'collaboration', None)
    messages = collaboration.list()['messages'][:80] if collaboration else []
    messages = [{key: m.get(key) for key in ('message_id', 'sender', 'recipient', 'created_at', 'task_id')}
                | {'body': m['body'][:300], 'truncated': len(m['body']) > 300} for m in messages]
    manager_actor=None
    if hasattr(studio,'manager'):
        manager=studio.manager.list(limit=10)
        settings=manager['settings']
        # Normal plan coordination has no failure incident. Project the actual
        # manager run, so both plan assignment and incident handling animate.
        busy=next((r for r in runs if r['agent_id']==settings['agent_id'] and r.get('coordination_only')
                   and r['state'] in ('starting','running','cancelling')),None)
        with closing(studio.registry._connect()) as c:
            row=c.execute('SELECT public_json FROM studio_agents WHERE agent_id=?',(settings['agent_id'],)).fetchone()
            manager_profile=json.loads(row[0]) if row else {}
        from .agent_studio import activation
        inactive=bool(settings['agent_id']) and not activation(manager_profile)['activated'] and not busy
        manager_actor={'actor_id':'studio-manager','agent_id':settings['agent_id'],'name':settings['name'],
            'model':manager_profile.get('model'),'executor_kind':manager_profile.get('executor_kind'),
            'appearance_id':settings['appearance_id'],'room':'rest' if inactive else 'management','animation':'read' if busy else 'rest' if inactive else 'wait',
            'state':'running' if busy else 'inactive' if inactive else 'idle','active':bool(busy),
            'activity_label':'正在协调' if busy else '未激活 · 等待配置' if inactive else '本地值守' if settings['agent_id'] and settings['enabled'] else '可配置管理伙伴',
            'summary':'事件触发的协调办公室；空闲时不调用模型。','manager':True}
    batch_actors=[]
    if hasattr(studio,'image_qa'):
        with closing(studio.registry._connect()) as c:
            for row in c.execute("SELECT * FROM studio_image_batches WHERE state IN ('running','paused') ORDER BY rowid"):
                batch=json.loads(row['payload_json']);agent=c.execute('SELECT public_json FROM studio_agents WHERE agent_id=?',(batch['agent_id'],)).fetchone()
                if not agent:continue
                profile=json.loads(agent[0])
                batch_actors.append({'actor_id':row['batch_id'],'agent_id':batch['agent_id'],'name':batch['title'],
                    'model':profile['model'],'appearance_id':profile.get('appearance_id'),'project_id':batch['project_id'],
                    'room':'review' if row['state']=='running' else 'waiting','state':row['state'],'active':row['state']=='running',
                    'animation':'read' if row['state']=='running' else 'wait','activity_label':'批量看图' if row['state']=='running' else '已暂停新增图片',
                    'summary':batch['title'],'batch_id':row['batch_id']})
    return {'ok': True, 'observed_at': utc_now(), 'runs': runs, 'messages': messages,
            'manager_actor':manager_actor,'batch_actors':batch_actors,
            'ordinary': studio.sessions.snapshot() if hasattr(studio,'sessions') else {'actors':[]},
            'active_count': sum(r['active'] for r in runs), 'source': 'persisted_managed_runs'}
