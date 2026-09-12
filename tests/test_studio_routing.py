"""Boundary tests for editable routing priors and smoothed experience."""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / 'src'
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SRC))

from ap_mind.contracts import ContractError
from ap_mind.studio_routing import (
    DEFAULT_CATEGORIES,
    DEFAULT_PRIOR_STRENGTH,
    DEFAULT_SOURCE,
    MAX_CATEGORY_LEN,
    MAX_SOURCE_LEN,
    PREMIUM_ROUTINE_PENALTY,
    REWORK_PENALTY,
    default_routing,
    infer_family,
    normalize_routing,
    rank,
)


def profile(agent_id, **extra):
    return {'agent_id': agent_id, **extra}


def grok(**extra):
    return profile('grok', name='Grok · 快速执行员', model='grok-4.6', **extra)


def gemini(**extra):
    return profile('gemini', name='Gemini 文案', model='gemini-3.8-flash', **extra)


def deepseek(**extra):
    return profile('deepseek', name='DeepSeek 推理', model='DeepSeek-V4-Pro-0813', **extra)


def kimi(**extra):
    return profile('kimi', name='Kimi 前端', model='kimi-k2', **extra)


def glm(**extra):
    return profile('glm', name='GLM 简单代码', model='glm-4.5', **extra)


def astra(**extra):
    return profile('astra', name='Astra 规划验收', model='astra', **extra)


def codex(**extra):
    return profile('codex', name='Codex · 统筹验收', executor_kind='codex', model='', **extra)


def ccmax(**extra):
    return profile('ccmax', name='Claude · CC Max', model='claude-opus-5',
                   template_id='claude-ccmax', connection_label='CC Max · 专用渠道', **extra)


def kiro(**extra):
    return profile('kiro', name='Claude · Kiro', model='claude-opus-5',
                   connection_label='Kiro · 经济渠道', **extra)


def unknown_claude(**extra):
    return profile('claude-unknown', name='Claude · 备用设计师', model='claude-opus-4-8', **extra)


def unknown_model(**extra):
    return profile('mystery', name='Mystery Partner', model='totally-unknown-7b', **extra)


def sample(agent_id, task_id, tags, outcome, **extra):
    item = {
        'run_id': extra.pop('run_id', f'{agent_id}-{task_id}-{outcome}'),
        'agent_id': agent_id,
        'task_id': task_id,
        'tags': tags,
        'kind': extra.pop('kind', 'delivery'),
        'outcome': outcome,
        'state': extra.pop('state', 'completed'),
        'changes_requested_records': extra.pop('changes_requested_records', 0),
        'duration_ms': extra.pop('duration_ms', None),
        'estimated_cost_usd': extra.pop('estimated_cost_usd', None),
    }
    item.update(extra)
    return item


def by_id(rows):
    return {row['agent_id']: row for row in rows}


def test_default_family_priors_are_user_preferences_not_benchmarks():
    mapping = [
        (grok(), 'grok', 'economy', 'routine_code'),
        (gemini(), 'gemini', 'economy', 'writing'),
        (deepseek(), 'deepseek', 'economy', 'reasoning'),
        (kimi(), 'kimi', 'economy', 'frontend'),
        (glm(), 'glm', 'economy', 'routine_code'),
        (codex(), 'codex', 'premium', 'planning'),
        (astra(), 'astra', 'premium', 'planning'),
        (ccmax(), 'ccmax', 'premium', 'critical'),
        (kiro(), 'kiro', 'economy', 'frontend'),
        (unknown_claude(), 'claude', 'unknown', None),
        (unknown_model(), 'unknown', 'unknown', None),
    ]
    for item, family, tier, specialty in mapping:
        inferred = infer_family(item)
        routing = default_routing(item)
        assert inferred == family
        assert routing['cost_tier'] == tier
        assert routing['prior_strength'] == DEFAULT_PRIOR_STRENGTH
        assert routing['source'] == DEFAULT_SOURCE
        assert routing['source'] != '公开测评'
        assert set(DEFAULT_CATEGORIES) <= set(routing['strengths'])
        if specialty:
            assert routing['strengths'][specialty] == max(routing['strengths'][key] for key in DEFAULT_CATEGORIES)
    claude = default_routing(unknown_claude())
    maxed = default_routing(ccmax())
    assert claude['cost_tier'] != 'premium'
    assert infer_family(unknown_claude()) != 'ccmax'
    assert claude['strengths']['critical'] < maxed['strengths']['critical']
    assert default_routing(deepseek())['strengths']['planning'] > default_routing(grok())['strengths']['planning']


def test_kiro_channel_is_not_ccmax_even_when_role_mentions_it():
    mislabeled = kiro(
        role='困难任务交 CC Max，本渠道只做经济杂活',
        capability_notes='遇到 CC Max 模板再升级，不要自己当 CC Max',
    )
    assert infer_family(mislabeled) == 'kiro'
    assert default_routing(mislabeled)['cost_tier'] == 'economy'
    named_only = profile(
        'named-kiro',
        name='Kiro 经济',
        model='claude-opus-5',
        role='把关键工作交给 CC Max',
    )
    assert infer_family(named_only) == 'kiro'
    notes_only = profile(
        'notes-claude',
        name='备用 Claude',
        model='claude-opus-5',
        capability_notes='CC Max 渠道专用于关键难题',
        role='困难任务交 CC Max',
    )
    assert infer_family(notes_only) == 'claude'
    assert default_routing(notes_only)['cost_tier'] == 'unknown'


def test_normalize_rejects_invalid_and_keeps_custom_categories():
    normalized = normalize_routing({
        'cost_tier': 'standard',
        'strengths': {'frontend': 0.2, 'css-animation': 1, 'routine_code': 0, '前端': 0.4},
        'prior_strength': 8,
        'source': '用户修改',
        'ignored': True,
    })
    assert normalized == {
        'cost_tier': 'standard',
        'strengths': {'frontend': 0.4, 'css-animation': 1.0, 'routine_code': 0.0},
        'prior_strength': 8.0,
        'source': '用户修改',
    }
    assert normalize_routing({})['cost_tier'] == 'unknown'
    assert normalize_routing({})['prior_strength'] == DEFAULT_PRIOR_STRENGTH
    assert normalize_routing(default_routing(grok()))['cost_tier'] == 'economy'
    with pytest.raises(ContractError, match='cost_tier'):
        normalize_routing({'cost_tier': 'cheap'})
    with pytest.raises(ContractError, match='strengths'):
        normalize_routing({'strengths': {'frontend': 1.5}})
    with pytest.raises(ContractError, match='strengths'):
        normalize_routing({'strengths': {'frontend': True}})
    with pytest.raises(ContractError, match='strengths'):
        normalize_routing({'strengths': {'frontend': float('inf')}})
    with pytest.raises(ContractError, match='strengths'):
        normalize_routing({'strengths': {'x' * (MAX_CATEGORY_LEN + 1): 0.5}})
    with pytest.raises(ContractError, match='strengths'):
        normalize_routing({'strengths': {'bad\nname': 0.5}})
    with pytest.raises(ContractError, match='prior_strength'):
        normalize_routing({'prior_strength': 0})
    with pytest.raises(ContractError, match='prior_strength'):
        normalize_routing({'prior_strength': float('nan')})
    with pytest.raises(ContractError, match='source'):
        normalize_routing({'source': 12})
    with pytest.raises(ContractError, match='source'):
        normalize_routing({'source': 'x' * (MAX_SOURCE_LEN + 1)})
    with pytest.raises(ContractError, match='source'):
        normalize_routing({'source': 'bad\tsource'})
    with pytest.raises(ContractError, match='profile'):
        normalize_routing(['economy'])


def test_sparse_uses_resume_and_does_not_exclude_unknown():
    rows = by_id(rank(
        [grok(), ccmax(), unknown_model(), unknown_claude()],
        {'tags': ['routine_code']},
        [],
    ))
    assert [row['agent_id'] for row in rank([grok(), ccmax()], {'tags': ['routine_code']}, [])] == ['grok', 'ccmax']
    assert rows['grok']['effective_samples'] == 0
    assert rows['grok']['experience_weight'] == 0
    assert rows['grok']['observed_score'] is None
    assert rows['grok']['prior_score'] == default_routing(grok())['strengths']['routine_code']
    assert rows['grok']['score'] == rows['grok']['prior_score']
    assert rows['mystery']['cost_tier'] == 'unknown'
    assert rows['claude-unknown']['cost_tier'] == 'unknown'
    assert '不因未知价格排除' not in rows['grok']['reason']
    assert rows['mystery']['score'] == rows['mystery']['prior_score'] == pytest.approx(0.5)
    untagged = by_id(rank([unknown_model(), grok()], {}, None))
    assert untagged['mystery']['categories'] == []
    assert untagged['mystery']['prior_score'] == pytest.approx(0.5)
    assert '中性先验' in untagged['mystery']['reason']


def test_single_sample_blends_instead_of_jumping():
    observations = [sample('grok', 't1', ['routine_code'], 'independent_accepted')]
    sparse = rank([grok()], {'tags': ['routine_code']}, [])[0]
    one = rank([grok()], {'tags': ['routine_code']}, observations)[0]
    assert one['effective_samples'] == pytest.approx(1)
    assert one['experience_weight'] == pytest.approx(1 / 7)
    expected = (6 / 7) * sparse['prior_score'] + (1 / 7) * 1.0
    assert one['score'] == pytest.approx(expected)
    assert abs(one['score'] - sparse['score']) < 0.15
    assert one['score'] < 0.95


def test_experience_weight_grows_smoothly_with_distinct_tasks():
    tags = ['routine_code']
    prior = default_routing(grok())['strengths']['routine_code']
    rows = []
    for count in (0, 1, 6, 18):
        observations = [sample('grok', f't{i}', tags, 'independent_accepted') for i in range(count)]
        rows.append(rank([grok()], {'tags': tags}, observations)[0])
    weights = [row['experience_weight'] for row in rows]
    assert weights[0] == 0
    assert weights[1] == pytest.approx(1 / 7)
    assert weights[2] == pytest.approx(0.5)
    assert weights[3] == pytest.approx(18 / 24)
    assert weights[0] < weights[1] < weights[2] < weights[3] < 1
    assert rows[0]['score'] < rows[1]['score'] < rows[2]['score'] < rows[3]['score']
    assert rows[3]['score'] == pytest.approx((1 - 18 / 24) * prior + (18 / 24) * 1.0)


def test_same_task_rework_is_one_vote_and_pass_cannot_wash_failure():
    tags = ['routine_code']
    duplicates = [
        sample('grok', 'same', tags, 'independent_accepted', run_id='a'),
        sample('grok', 'same', tags, 'independent_accepted', run_id='b'),
        sample('grok', 'same', tags, 'independent_accepted', run_id='c'),
    ]
    deduped = rank([grok()], {'tags': tags}, duplicates)[0]
    assert deduped['effective_samples'] == pytest.approx(1)
    assert deduped['observed_score'] == pytest.approx(1)
    rework = [
        sample('grok', 'same', tags, 'independent_changes_requested', run_id='fail',
               created_at='2026-09-12T01:00:00Z'),
        sample('grok', 'same', tags, 'independent_accepted', run_id='pass',
               changes_requested_records=1, created_at='2026-09-12T02:00:00Z'),
    ]
    merged = rank([grok()], {'tags': tags}, rework)[0]
    assert merged['effective_samples'] == pytest.approx(1)
    assert merged['observed_score'] == pytest.approx(1 - REWORK_PENALTY)
    assert merged['observed_score'] < 1
    only_fail = rank([grok()], {'tags': tags}, rework[:1])[0]
    assert only_fail['observed_score'] == pytest.approx(0)
    history_only = rank([grok()], {'tags': tags}, [
        sample('grok', 'same', tags, 'independent_accepted', changes_requested_records=2),
    ])[0]
    assert history_only['observed_score'] == pytest.approx(1 - REWORK_PENALTY)


def test_later_failure_is_the_final_verdict_not_washed_by_earlier_pass():
    tags = ['routine_code']
    later_fail = [
        sample('grok', 'same', tags, 'independent_accepted', run_id='early-pass',
               created_at='2026-09-12T01:00:00Z'),
        sample('grok', 'same', tags, 'independent_changes_requested', run_id='late-fail',
               created_at='2026-09-12T03:00:00Z'),
    ]
    result = rank([grok()], {'tags': tags}, later_fail)[0]
    assert result['effective_samples'] == pytest.approx(1)
    assert result['observed_score'] == pytest.approx(0)
    unordered = list(reversed(later_fail))
    assert rank([grok()], {'tags': tags}, unordered)[0]['observed_score'] == pytest.approx(0)


def test_network_failures_and_unreviewed_are_quality_neutral():
    tags = ['routine_code']
    baseline = rank([grok()], {'tags': tags}, [])[0]
    noise = [
        sample('grok', 'n1', tags, 'unreviewed', state='failed', run_id='net1'),
        sample('grok', 'n2', tags, 'unreviewed', state='timed_out', run_id='net2'),
        sample('grok', 'n3', tags, 'unreviewed', state='completed', run_id='open'),
    ]
    assert rank([grok()], {'tags': tags}, noise)[0]['score'] == pytest.approx(baseline['score'])
    assert rank([grok()], {'tags': tags}, noise)[0]['effective_samples'] == 0


def test_self_reported_and_review_kind_are_not_quality():
    tags = ['routine_code']
    baseline = rank([grok()], {'tags': tags}, [])[0]
    ignored = [
        sample('grok', 's1', tags, 'self_reported'),
        sample('grok', 'r1', tags, 'review_applied', kind='review'),
        sample('grok', 'r2', ['review'], 'independent_accepted', kind='review'),
    ]
    result = rank([grok()], {'tags': tags}, ignored)[0]
    assert result['effective_samples'] == 0
    assert result['score'] == pytest.approx(baseline['score'])
    recorded = rank([grok()], {'tags': tags}, [
        sample('grok', 'rec', tags, 'recorded_accepted'),
    ])[0]
    independent = rank([grok()], {'tags': tags}, [
        sample('grok', 'ind', tags, 'independent_accepted'),
    ])[0]
    assert recorded['effective_samples'] == pytest.approx(0.5)
    assert independent['effective_samples'] == pytest.approx(1)
    assert recorded['experience_weight'] < independent['experience_weight']


def test_unmatched_tags_do_not_migrate_and_free_tags_are_kept():
    writing_history = [sample('grok', f't{i}', ['writing'], 'independent_accepted') for i in range(6)]
    frontend_task = rank([grok()], {'tags': ['frontend']}, writing_history)[0]
    writing_task = rank([grok()], {'tags': ['writing']}, writing_history)[0]
    assert frontend_task['effective_samples'] == 0
    assert writing_task['effective_samples'] == pytest.approx(6)
    custom = profile('custom', routing_profile={
        'cost_tier': 'economy',
        'strengths': {'css-animation': 0.95, 'routine_code': 0.1},
        'prior_strength': 6,
        'source': '用户修改',
    })
    rows = by_id(rank([custom, grok()], {'tags': ['css-animation']}, []))
    assert rows['custom']['categories'] == ['css-animation']
    assert rows['custom']['prior_score'] == pytest.approx(0.95)
    assert rows['custom']['score'] > rows['grok']['score']


def test_batch_tags_are_not_work_categories_and_do_not_leak_experience():
    batch = 'economical-routing-20260912'
    writing_history = [
        sample('grok', f't{i}', ['writing', batch], 'independent_accepted') for i in range(6)
    ]
    frontend_with_batch = rank(
        [grok(), kimi()],
        {'tags': ['frontend', batch]},
        writing_history,
    )
    rows = by_id(frontend_with_batch)
    assert rows['grok']['categories'] == ['frontend']
    assert rows['kimi']['categories'] == ['frontend']
    assert rows['grok']['effective_samples'] == 0
    assert rows['kimi']['score'] > rows['grok']['score']
    batch_only = rank([grok(), kimi()], {'tags': [batch]}, writing_history)
    assert batch_only[0]['categories'] == []
    assert batch_only[0]['prior_score'] == pytest.approx(0.5)
    assert batch_only[0]['effective_samples'] == 0
    chinese = rank([kimi(), grok()], {'tags': ['前端']}, [])
    assert chinese[0]['categories'] == ['frontend']
    assert chinese[0]['agent_id'] == 'kimi'


def test_premium_soft_penalty_on_routine_and_upgrade_on_critical():
    assert PREMIUM_ROUTINE_PENALTY == pytest.approx(0.20)
    routine = by_id(rank([grok(), ccmax(), astra()], {'tags': ['routine_code']}, []))
    assert routine['grok']['score'] > routine['ccmax']['score']
    assert routine['ccmax']['score'] == pytest.approx(
        routine['ccmax']['prior_score'] - PREMIUM_ROUTINE_PENALTY
    )
    assert '软惩罚' in routine['ccmax']['reason']
    review = by_id(rank([gemini(), codex(), astra()], {'tags': ['review']}, []))
    assert review['gemini']['score'] > review['codex']['score']
    assert review['codex']['score'] == pytest.approx(
        review['codex']['prior_score'] - PREMIUM_ROUTINE_PENALTY
    )
    assert review['gemini']['score'] > review['astra']['score']
    planning = by_id(rank([deepseek(), astra(), grok()], {'tags': ['planning']}, []))
    assert planning['deepseek']['score'] > planning['astra']['score']
    assert planning['astra']['score'] == pytest.approx(
        planning['astra']['prior_score'] - PREMIUM_ROUTINE_PENALTY
    )
    critical = by_id(rank([grok(), ccmax()], {'tags': ['critical']}, []))
    assert critical['ccmax']['score'] > critical['grok']['score']
    assert critical['ccmax']['score'] == pytest.approx(critical['ccmax']['prior_score'])
    assert '允许高价升级' in critical['ccmax']['reason']
    hard_writing = by_id(rank(
        [gemini(), astra()],
        {'tags': ['writing', 'hard']},
        [],
    ))
    assert hard_writing['astra']['score'] == pytest.approx(hard_writing['astra']['prior_score'])
    assert hard_writing['gemini']['score'] > hard_writing['astra']['score']
    complex_only = rank([ccmax()], {'tags': ['complex']}, [])[0]
    assert complex_only['categories'] == []
    assert complex_only['score'] == pytest.approx(0.5)


def test_user_edit_overrides_family_default():
    edited = grok(routing_profile={
        'cost_tier': 'premium',
        'strengths': {'writing': 0.99, 'routine_code': 0.1},
        'prior_strength': 4,
        'source': '用户修改分工',
    })
    assert default_routing(edited)['cost_tier'] == 'economy'
    ranked = rank([edited, gemini()], {'tags': ['writing']}, [])
    rows = by_id(ranked)
    assert rows['grok']['cost_tier'] == 'premium'
    assert rows['grok']['prior_score'] == pytest.approx(0.99)
    assert rows['grok']['score'] == pytest.approx(0.99 - PREMIUM_ROUTINE_PENALTY)
    assert rows['grok']['score'] < rows['gemini']['score']
    hard = by_id(rank([edited, gemini()], {'tags': ['writing', 'hard']}, []))
    assert hard['grok']['score'] == pytest.approx(0.99)
    assert hard['grok']['score'] > hard['gemini']['score']
    assert '用户修改分工' in rows['grok']['reason']
    empty_config = grok(routing_profile={})
    assert rank([empty_config], {'tags': ['routine_code']}, [])[0]['prior_score'] == pytest.approx(
        default_routing(grok())['strengths']['routine_code']
    )


def test_unknown_price_is_not_zero_and_speed_is_not_intelligence():
    cheap_fast = unknown_model()
    slow_known = grok()
    observations = [
        sample('mystery', 't1', ['routine_code'], 'independent_accepted',
               duration_ms=12, estimated_cost_usd=None),
        sample('grok', 't1', ['routine_code'], 'independent_accepted',
               duration_ms=90_000, estimated_cost_usd=0.02),
    ]
    rows = by_id(rank([cheap_fast, slow_known], {'tags': ['routine_code']}, observations))
    assert rows['mystery']['cost_tier'] == 'unknown'
    assert '不视为免费' in rows['mystery']['reason']
    assert rows['mystery']['observed_score'] == rows['grok']['observed_score'] == pytest.approx(1)
    assert rows['grok']['score'] > rows['mystery']['score']


def test_tie_keeps_input_order_and_prefers_economy_over_unknown():
    left = profile('z-last', routing_profile={
        'cost_tier': 'unknown',
        'strengths': {'writing': 0.5},
        'prior_strength': 6,
        'source': '用户修改',
    })
    right = profile('a-first', routing_profile={
        'cost_tier': 'unknown',
        'strengths': {'writing': 0.5},
        'prior_strength': 6,
        'source': '用户修改',
    })
    same_unknown = rank([left, right], {'tags': ['writing']}, [])
    assert [row['agent_id'] for row in same_unknown] == ['z-last', 'a-first']
    economy = profile('eco', routing_profile={
        'cost_tier': 'economy',
        'strengths': {'writing': 0.5},
        'prior_strength': 6,
        'source': '用户修改',
    })
    unknown = profile('unk', routing_profile={
        'cost_tier': 'unknown',
        'strengths': {'writing': 0.5},
        'prior_strength': 6,
        'source': '用户修改',
    })
    mixed = rank([unknown, economy], {'tags': ['writing']}, [])
    assert [row['agent_id'] for row in mixed] == ['eco', 'unk']
    assert mixed[0]['score'] == mixed[1]['score']


def test_rank_output_shape_sorted_and_does_not_mutate_inputs():
    profiles = [ccmax(), grok(), kimi()]
    snapshot = [dict(item) for item in profiles]
    task = {'tags': ['frontend', 'routine_code']}
    observations = [sample('kimi', 'ui', ['frontend'], 'independent_accepted')]
    rows = rank(profiles, task, observations)
    scores = [row['score'] for row in rows]
    assert scores == sorted(scores, reverse=True)
    assert rows[0]['agent_id'] == 'kimi'
    required = {
        'agent_id', 'score', 'cost_tier', 'prior_score', 'experience_weight',
        'effective_samples', 'observed_score', 'categories', 'reason',
    }
    for row in rows:
        assert required <= set(row)
        assert row['categories'] == ['frontend', 'routine_code']
        assert type(row['reason']) is str and row['reason']
    assert profiles == snapshot
    with pytest.raises(ContractError, match='profiles'):
        rank({'agent_id': 'x'}, {}, [])
    with pytest.raises(ContractError, match='task'):
        rank([], [], [])
    with pytest.raises(ContractError, match='observations'):
        rank([], {}, {})
    with pytest.raises(ContractError, match='agent_id'):
        rank([{'name': 'no-id'}], {}, [])
