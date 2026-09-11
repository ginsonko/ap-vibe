"""Public Studio attempts in the same directory as ordinary CLI sessions.

Reads only the saved public event projection. No private CLI reasoning, API
credentials, executor prompts or raw tool arguments are exposed by this view.
"""
from contextlib import closing
import json

from .contracts import ContractError, utc_now
from .product import redact_portable


def entry(row):
    run = json.loads(row['payload_json'])
    return {'source_id': 'studio-' + row['run_id'], 'harness': run.get('executor_kind', 'claude'),
        'session_id': run.get('session_id') or row['run_id'], 'run_id': row['run_id'],
        'task_id': run.get('logical_task_id'), 'managed': True,
        'agent_id': run.get('agent_id'), 'agent_name': run.get('name'),
        'title': (run.get('task_snapshot') or {}).get('title') or run.get('name') or '工作室任务',
        'title_source': 'studio_task', 'model': run.get('model'), 'model_source': 'run_configuration',
        'cwd': run.get('workspace'), 'project_id': run.get('project_id'),
        'membership_basis': 'managed_task', 'state': row['state'],
        'modified_at': run.get('updated_at') or run.get('created_at') or '', 'available': True}


def catalog(registry):
    with closing(registry._connect()) as c:
        return [entry(row) for row in c.execute('SELECT run_id,state,payload_json FROM studio_runs')]


def read(registry, source_id, *, after=None, before=None, expected_generation=None, limit=20):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ContractError('session_page_limit_invalid')
    if after is not None and before is not None:
        raise ContractError('session_cursor_conflict')
    if any(type(v) is not int or v < 0 for v in (after, before) if v is not None):
        raise ContractError('session_cursor_invalid')
    run_id = source_id.removeprefix('studio-')
    generation = 'studio-events:' + run_id
    reset = bool(expected_generation and expected_generation != generation)
    if reset:
        after = before = None
    with closing(registry._connect()) as c:
        run = c.execute('SELECT run_id,state,payload_json FROM studio_runs WHERE run_id=?', (run_id,)).fetchone()
        if not run:
            raise ContractError('session_source_not_found')
        where, args = 'run_id=?', [run_id]
        if after is not None:
            where += ' AND seq>?'; args.append(after)
        if before is not None:
            where += ' AND seq<?'; args.append(before)
        order = 'ASC' if after is not None else 'DESC'
        rows = c.execute('SELECT seq,kind,created_at,payload_json FROM studio_events WHERE ' + where +
            ' ORDER BY seq ' + order + ' LIMIT ?', (*args, limit + 1)).fetchall()
        has_more = after is not None and len(rows) > limit
        rows = rows[:limit]
        if after is None:
            rows.reverse()
        oldest = rows[0]['seq'] if rows else (before or 0)
        has_older = bool(c.execute('SELECT 1 FROM studio_events WHERE run_id=? AND seq<? LIMIT 1', (run_id, oldest)).fetchone())
    events = []
    for row in rows:
        value = json.loads(row['payload_json'])
        text = str(value.get('text') or row['kind'])
        if value.get('error'):
            text += '\n' + str(value['error'])
        events.append({'id': str(row['seq']), 'offset': row['seq'], 'end_offset': row['seq'],
            'role': 'assistant' if row['kind'] == 'assistant' else 'tool',
            'text': redact_portable(text[:64000], preserve_local_paths=True),
            'timestamp': row['created_at'], 'text_may_be_truncated': len(text) > 64000})
    return {'ok': True, **entry(run), 'events': events, 'generation': generation, 'reset': reset,
        'cursor': rows[-1]['seq'] if rows else (after or 0), 'history_before': oldest,
        'has_older': has_older, 'has_more': has_more, 'invalid_lines': 0,
        'read_only': True, 'read_at': utc_now(), 'history_scope': 'saved_public_studio_events'}
