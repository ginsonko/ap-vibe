"""Read-only routing evidence shared by managers, fallback and public tools.

No model calls, no rewriting historical reviews, no hard model allowlist.
"""
from contextlib import closing
import json
from statistics import median

from .studio_metrics import observation, WINDOW
from .studio_routing import rank

POLICY = ('普通明确工作优先合适的经济伙伴；Astra/Codex主要负责短而关键的规划和最终验收，'
          'CC Max用于困难攻坚或关键可靠性。高价是软偏好，不是禁用。'
          '任务tags采用routine_code/frontend/writing/research/reasoning/planning/review/critical等可复用类别；'
          '关键或复杂任务标critical/complex/hard，避免把普通审查自动升级。'
          '简历是可编辑初始印象，排序按同类真实裁决平滑学习，不是模型智力排名。'
          '尊重用户指定和候选范围；若偏离排序，请简要说明任务依据。')


def compatible(sample, profile):
    if not profile:
        return False
    current = profile.get('capability_revision', profile.get('revision'))
    if sample.get('capability_revision') is not None:
        return sample['capability_revision'] == current
    if sample.get('profile_revision') in {profile.get('revision'), current} - {None}:
        return True
    legacy = profile.get('capability_legacy_revision', profile.get('revision'))
    return legacy is not None and sample.get('profile_revision') == legacy


class Router:
    def __init__(self, studio, profiles=None):
        self.settings = studio.guidance.settings() if hasattr(studio, 'guidance') else {}
        self.policy = studio.guidance.prompt() if hasattr(studio, 'guidance') else POLICY
        self.profiles = profiles if profiles is not None else studio.profiles()['agents']
        by_id = {p['agent_id']: p for p in self.profiles}
        with closing(studio.registry._connect()) as c:
            rows = c.execute('SELECT run_id,state,payload_json FROM studio_runs ORDER BY rowid DESC LIMIT ?', (WINDOW,)).fetchall()
            count = c.execute('SELECT COUNT(*) FROM studio_runs').fetchone()[0]
            tasks = {r['task_id']: json.loads(r['payload_json']) for r in c.execute('''SELECT task_id,payload_json FROM studio_tasks
                WHERE task_id IN (SELECT json_extract(payload_json,'$.logical_task_id') FROM studio_runs ORDER BY rowid DESC LIMIT ?)''', (WINDOW,))}
        self.samples = []
        invalid = 0
        for row in rows:
            try:
                run = {**json.loads(row['payload_json']), 'run_id': row['run_id'], 'state': row['state']}
                item = observation(run, tasks.get(run.get('logical_task_id')))
                if compatible(item, by_id.get(item['agent_id'], {})):
                    self.samples.append(item)
            except (ValueError, KeyError, AttributeError, TypeError):
                invalid += 1
        self.coverage = {'scanned_runs': len(rows), 'matched_runs': count, 'truncated': count > len(rows), 'invalid_records': invalid}

    def recommend(self, task, candidates=None, exclude=()):
        selected = [p for p in self.profiles if not p.get('archived') and not p.get('management_reserved')
                    and p['agent_id'] not in exclude and (candidates is None or p['agent_id'] in candidates)]
        if candidates is not None:
            order = {aid:i for i,aid in enumerate(candidates)}
            selected.sort(key=lambda p: order.get(p['agent_id'], len(order)))
        result = rank(selected, task, self.samples, self.settings)
        tags = set(task.get('tags') or [])
        for item in result:
            samples = [s for s in self.samples if s['agent_id'] == item['agent_id'] and
                       tags.intersection(s.get('tags') or []) and s.get('outcome') in
                       {'recorded_accepted', 'independent_accepted', 'recorded_changes_requested', 'independent_changes_requested'}]
            durations = [s['duration_ms'] for s in samples if s.get('duration_ms') is not None]
            costs = [s['estimated_cost_usd'] for s in samples if s.get('estimated_cost_usd') is not None]
            item['experience_evidence'] = {'run_ids': [s['run_id'] for s in samples[:8]],
                'reviewed_attempts': len(samples), 'median_duration_ms': median(durations) if durations else None,
                'median_reported_cost_usd': median(costs) if costs else None,
                'boundary': '同类尝试的描述性趋势，任务规模可能不同；不把一次更快、更便宜当质量更高。'}
            item['guidance_revision'] = self.settings.get('revision', 0)
        return result


def recommendations(studio, task):
    router = Router(studio)
    return {'ok': True, 'recommendations': router.recommend(task, task.get('eligible_agents')),
            'coverage': router.coverage, 'policy': router.policy, 'guidance': router.settings,
            'boundary': '配置简历与同类裁决形成的软排序；不是能力保证。未激活伙伴仍可供查看，实际派发检查连接、忙碌和用户预算。'}
