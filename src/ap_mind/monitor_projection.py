"""Read-only task cards over collected sources; never change project membership."""
from copy import deepcopy
from datetime import datetime


def _instant(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0


def group_task_sources(sources: list[dict], message_limit: int) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for source in sources:
        identity = ('session', source['session_id']) if source.get('session_id') else ('source', source['project_id'], source['source_key'])
        groups.setdefault(identity, []).append(source)
    tasks = []
    for members in groups.values():
        # Prefer a confirmed project. Stable ties prevent normal polls from
        # changing a selected card just because another source produced text.
        members = sorted(members, key=lambda s: (s.get('classification') != 'confirmed', s['project_id'], s['source_key']))
        task = deepcopy(members[0])
        task['source_keys'] = sorted({s['source_key'] for s in members})
        task['source_projects'] = [{key: s.get(key) for key in ('project_id', 'project_name', 'source_key', 'classification')} for s in members]
        task['project_ids'] = sorted({s['project_id'] for s in members})
        task['project_names'] = list(dict.fromkeys(s['project_name'] for s in members if s.get('project_name')))
        task['source_errors'] = [{ 'source_key':s['source_key'], 'project_id':s['project_id'], 'error':deepcopy(s['last_error'])} for s in members if s.get('last_error')]
        messages = {(m['role'], m['timestamp'], m['text']): deepcopy(m) for s in members for m in s['messages']}
        merged = sorted(messages.values(), key=lambda m: (_instant(m['timestamp']), m.get('message_id', '')))
        task['message_count'] = len(merged)
        task['messages'] = merged[-message_limit:]
        task['active'] = any(s['active'] for s in members)
        latest = max(members, key=lambda s: _instant(s.get('last_activity_at')))
        task.update({key: latest.get(key) for key in ('last_activity_at', 'last_activity_text', 'last_activity_role')})
        tasks.append(task)
    return tasks
