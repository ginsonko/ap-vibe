"""Read-only observations from durable attempts and attributed reviews."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from statistics import median

from .contracts import ContractError, utc_now

WINDOW = 5000


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else None
    except (AttributeError, ValueError):
        return None


def observation(run, task):
    meta = run.get('task_snapshot') or task or {}
    result = run.get('result') or {}
    review = run.get('review') or {}
    verdict = review.get('verdict') or {}
    is_review = bool(meta.get('review_of_task_id'))
    verdict_matches = (verdict.get('source_run_id') == run['run_id'] and
                       verdict.get('reviewer_agent_id') != run['agent_id'] and
                       bool(verdict.get('reviewer_agent_id')))
    checks = verdict.get('checks') or []
    evidence = verdict.get('source_files') or []
    hashed_evidence = bool(evidence) and all(item.get('sha256') for item in evidence) and bool(verdict.get('report', {}).get('sha256'))
    independent = (not is_review and verdict_matches and hashed_evidence and checks and
                   ((verdict.get('outcome') == 'accepted' and all(item.get('status') == 'passed' for item in checks)) or
                    (verdict.get('outcome') == 'changes_requested' and any(item.get('status') == 'failed' for item in checks))))
    if is_review:
        outcome = 'review_applied' if verdict else 'review_unverified'
    elif independent:
        outcome = 'independent_accepted' if verdict['outcome'] == 'accepted' else 'independent_changes_requested'
    elif review.get('reviewer') in {run['agent_id'], run.get('name')}:
        outcome = 'self_reported'
    elif type(review.get('accepted')) is bool:
        outcome = 'recorded_accepted' if review['accepted'] else 'recorded_changes_requested'
    else:
        outcome = 'unreviewed'
    duration = number(result.get('duration_ms'))
    duration_source = 'executor' if duration is not None else None
    start, end = timestamp(run.get('execution_started_at')), timestamp(run.get('execution_finished_at'))
    if duration is None and start and end and end >= start:
        duration = (end - start).total_seconds() * 1000
        duration_source = 'process_wall_clock'
    usage = result.get('usage') or {}
    return {
        'run_id': run['run_id'], 'agent_id': run['agent_id'], 'profile_revision': run.get('profile_revision'),
        'model': run.get('model', ''), 'executor_kind': run.get('executor_kind', 'claude'),
        'task_id': run.get('logical_task_id'), 'title': meta.get('title') or run.get('prompt', '')[:160],
        'tags': meta.get('tags') or [], 'metadata_source': 'run_snapshot' if run.get('task_snapshot') else 'current_task' if task else 'run',
        'project_id': run.get('project_id'), 'kind': 'review' if is_review else 'delivery',
        'state': run['state'], 'outcome': outcome, 'created_at': run.get('created_at'),
        'duration_ms': duration, 'duration_source': duration_source,
        'estimated_cost_usd': number(result.get('total_cost_usd')),
        'usage': {key: number(usage.get(key)) for key in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')},
        'changes_requested_records': sum(1 for r in run.get('review_history', []) if r.get('accepted') is False),
        'reviewer': review.get('reviewer'), 'review_note': review.get('note', '')[:1600],
        'checks': checks[:100] if independent else [],
        'report': verdict.get('report') if verdict_matches else None,
        'source_files': evidence if independent else [],
        'run_url': '/v1/ap-vibe/agents/runs?run_id=' + run['run_id'],
    }


def summarize(samples):
    outcomes = Counter(item['outcome'] for item in samples)
    accepted = outcomes['independent_accepted']
    changes = outcomes['independent_changes_requested']
    costs = [item['estimated_cost_usd'] for item in samples if item['estimated_cost_usd'] is not None]
    durations = [item['duration_ms'] for item in samples if item['duration_ms'] is not None]
    return {
        'attempts': len(samples), 'logical_tasks': len({item['task_id'] for item in samples if item['task_id']}),
        'independent_accepted': accepted, 'independent_changes_requested': changes,
        'independent_pass_rate': accepted / (accepted + changes) if accepted + changes else None,
        'recorded_accepted': outcomes['recorded_accepted'], 'recorded_changes_requested': outcomes['recorded_changes_requested'],
        'review_attempts': sum(item['kind'] == 'review' for item in samples),
        'unreviewed': outcomes['unreviewed'], 'self_reported': outcomes['self_reported'],
        'changes_requested_records': sum(item['changes_requested_records'] for item in samples),
        'states': dict(Counter(item['state'] for item in samples)),
        'estimated_cost_usd': round(sum(costs), 6) if costs else None,
        'cost_known_samples': len(costs), 'cost_unknown_samples': len(samples) - len(costs),
        'median_duration_ms': median(durations) if durations else None, 'duration_known_samples': len(durations),
        'score': None,
    }


def snapshot(studio, *, agent_id=None, project_id=None, days=0, tag=None, configuration='all', offset=0, limit=20):
    if type(days) is not int or days < 0 or configuration not in {'all', 'current'}:
        raise ContractError('agent_metrics_filter_invalid')
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise ContractError('agent_metrics_page_invalid')
    filters, values = [], []
    if agent_id:
        filters.append('agent_id=?'); values.append(agent_id)
    if project_id:
        filters.append("json_extract(payload_json,'$.project_id')=?"); values.append(project_id)
    if days:
        try:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        except OverflowError:
            raise ContractError('agent_metrics_filter_invalid') from None
        filters.append("julianday(json_extract(payload_json,'$.created_at'))>=julianday(?)"); values.append(since)
    where = 'WHERE ' + ' AND '.join(filters) if filters else ''
    with closing(studio.registry._connect()) as c:
        c.execute('BEGIN')
        profiles = [json.loads(row[0]) for row in c.execute('SELECT public_json FROM studio_agents').fetchall()]
        count = c.execute('SELECT COUNT(*) FROM studio_runs ' + where, values).fetchone()[0]
        rows = c.execute('SELECT run_id,state,payload_json FROM studio_runs ' + where + ' ORDER BY rowid DESC LIMIT ?', [*values, WINDOW]).fetchall()
        tasks = {row['task_id']: json.loads(row['payload_json']) for row in c.execute('''SELECT task_id,payload_json FROM studio_tasks
            WHERE task_id IN (SELECT json_extract(payload_json,'$.logical_task_id') FROM studio_runs ''' + where + ' ORDER BY rowid DESC LIMIT ?)', [*values, WINDOW]).fetchall()}
    revisions = {profile['agent_id']: profile['revision'] for profile in profiles}
    samples, invalid = [], 0
    for row in rows:
        try:
            run = {**json.loads(row['payload_json']), 'run_id': row['run_id'], 'state': row['state']}
            item = observation(run, tasks.get(run.get('logical_task_id')))
            item['current_configuration'] = item['profile_revision'] == revisions.get(item['agent_id'])
            samples.append(item)
        except (ValueError, TypeError, AttributeError, KeyError):
            invalid += 1
    tags = sorted({value for item in samples for value in item['tags']})
    selected = [item for item in samples if (not tag or tag in item['tags']) and (configuration == 'all' or item['current_configuration'])]
    agents = []
    for profile in profiles:
        if agent_id and profile['agent_id'] != agent_id:
            continue
        own = [item for item in selected if item['agent_id'] == profile['agent_id']]
        groups = []
        for revision in sorted({item['profile_revision'] for item in own}, key=lambda value: str(value)):
            group = [item for item in own if item['profile_revision'] == revision]
            groups.append({'profile_revision': revision, 'model': group[0]['model'], 'executor_kind': group[0]['executor_kind'], 'metrics': summarize(group)})
        agents.append({**{key: profile.get(key) for key in ('agent_id', 'name', 'model', 'executor_kind', 'role', 'revision', 'archived', 'appearance_id')},
                       'metrics': summarize(own), 'configurations': groups})
    return {'ok': True, 'observed_at': utc_now(), 'agents': agents, 'summary': summarize(selected),
            'filters': {'agent_id': agent_id, 'project_id': project_id, 'days': days, 'tag': tag, 'configuration': configuration},
            'tags': tags, 'samples': selected[offset:offset + limit], 'total': len(selected),
            'next_offset': offset + limit if offset + limit < len(selected) else None,
            'coverage': {'matched_runs': count, 'scanned_runs': len(rows), 'limit': WINDOW, 'truncated': count > len(rows), 'invalid_records': invalid},
            'boundary': '当前窗口内的运行观察，不是模型智力排名。独立裁决有检查项和文件哈希；报告应用不证明评审能力。CLI费用为估算，缺失不按零计；同类任务与版本才适合比较。'}
