"""Editable routing priors mixed with same-class attributed experience.

Pure functions for economical partner ranking. Defaults are user division
preferences, not public model benchmarks or intelligence scores. Callers filter
observations to compatible configurations; this module does not migrate samples
across unmatched categories, treat unknown price as zero, or rank by speed.

Family inference uses model, executor_kind, connection_label, template_id and
identity name only. Role and capability notes never participate.
"""
from collections import defaultdict

from ap_mind.contracts import ContractError


COST_TIERS = frozenset({'economy', 'standard', 'premium', 'unknown'})
DEFAULT_CATEGORIES = (
    'routine_code', 'frontend', 'writing', 'research',
    'reasoning', 'planning', 'review', 'critical',
)
DEFAULT_PRIOR_STRENGTH = 6.0
DEFAULT_SOURCE = '用户初始分工偏好'
NEUTRAL = 0.5
INDEPENDENT_WEIGHT = 1.0
RECORDED_WEIGHT = 0.5
REWORK_PENALTY = 0.25
PREMIUM_ROUTINE_PENALTY = 0.20
MAX_SOURCE_LEN = 2048
MAX_CATEGORY_LEN = 100
UPGRADE_FLAGS = frozenset({'critical', 'complex', 'hard'})
COST_TIE_RANK = {'economy': 0, 'standard': 1, 'unknown': 2, 'premium': 3}
IDENTITY_KEYS = ('name', 'model', 'template_id', 'connection_label', 'executor_kind')
CHANNEL_KEYS = ('name', 'template_id', 'connection_label')
PASS_OUTCOMES = {
    'independent_accepted': INDEPENDENT_WEIGHT,
    'recorded_accepted': RECORDED_WEIGHT,
}
FAIL_OUTCOMES = {
    'independent_changes_requested': INDEPENDENT_WEIGHT,
    'recorded_changes_requested': RECORDED_WEIGHT,
}
NON_QUALITY_OUTCOMES = frozenset({
    'unreviewed', 'self_reported', 'review_applied', 'review_unverified',
})
TAG_ALIASES = {
    '前端': 'frontend',
    '文案': 'writing',
    '写作': 'writing',
    '规划': 'planning',
    '推理': 'reasoning',
    '数学': 'reasoning',
    '审查': 'review',
    '验收': 'review',
    '评审': 'review',
    '关键': 'critical',
    '杂活': 'routine_code',
    '例行代码': 'routine_code',
    '简单代码': 'routine_code',
    '研究': 'research',
    '困难': 'hard',
    '复杂': 'complex',
}
FAMILY_STRENGTHS = {
    'grok': {'cost_tier': 'economy', 'strengths': {
        'routine_code': 0.86, 'frontend': 0.55, 'writing': 0.5, 'research': 0.55,
        'reasoning': 0.5, 'planning': 0.48, 'review': 0.52, 'critical': 0.35,
    }},
    'gemini': {'cost_tier': 'economy', 'strengths': {
        'routine_code': 0.5, 'frontend': 0.55, 'writing': 0.86, 'research': 0.6,
        'reasoning': 0.5, 'planning': 0.5, 'review': 0.72, 'critical': 0.35,
    }},
    'deepseek': {'cost_tier': 'economy', 'strengths': {
        'routine_code': 0.6, 'frontend': 0.4, 'writing': 0.45, 'research': 0.7,
        'reasoning': 0.86, 'planning': 0.82, 'review': 0.5, 'critical': 0.55,
    }},
    'kimi': {'cost_tier': 'economy', 'strengths': {
        'routine_code': 0.5, 'frontend': 0.88, 'writing': 0.55, 'research': 0.45,
        'reasoning': 0.45, 'planning': 0.4, 'review': 0.5, 'critical': 0.35,
    }},
    'glm': {'cost_tier': 'economy', 'strengths': {
        'routine_code': 0.78, 'frontend': 0.45, 'writing': 0.45, 'research': 0.4,
        'reasoning': 0.4, 'planning': 0.4, 'review': 0.45, 'critical': 0.3,
    }},
    'codex': {'cost_tier': 'premium', 'strengths': {
        'routine_code': 0.45, 'frontend': 0.45, 'writing': 0.5, 'research': 0.65,
        'reasoning': 0.7, 'planning': 0.9, 'review': 0.86, 'critical': 0.78,
    }},
    'ccmax': {'cost_tier': 'premium', 'strengths': {
        'routine_code': 0.42, 'frontend': 0.5, 'writing': 0.45, 'research': 0.65,
        'reasoning': 0.82, 'planning': 0.78, 'review': 0.72, 'critical': 0.9,
    }},
    'kiro': {'cost_tier': 'economy', 'strengths': {
        'routine_code': 0.68, 'frontend': 0.72, 'writing': 0.55, 'research': 0.5,
        'reasoning': 0.5, 'planning': 0.48, 'review': 0.55, 'critical': 0.4,
    }},
    'claude': {'cost_tier': 'unknown', 'strengths': {
        'routine_code': 0.5, 'frontend': 0.5, 'writing': 0.5, 'research': 0.5,
        'reasoning': 0.5, 'planning': 0.5, 'review': 0.5, 'critical': 0.5,
    }},
    'unknown': {'cost_tier': 'unknown', 'strengths': {
        'routine_code': 0.5, 'frontend': 0.5, 'writing': 0.5, 'research': 0.5,
        'reasoning': 0.5, 'planning': 0.5, 'review': 0.5, 'critical': 0.5,
    }},
}
FAMILY_STRENGTHS['astra'] = FAMILY_STRENGTHS['codex']


def _finite_number(value):
    return type(value) in (int, float) and not isinstance(value, bool) and value == value and abs(value) != float('inf')


def _has_control(text):
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def _compact(text):
    text = str(text).lower()
    for token in (' ', '-', '_', '·', '.', '/'):
        text = text.replace(token, '')
    return text


def _joined(profile, keys):
    profile = profile or {}
    return _compact(' '.join(str(profile.get(key) or '') for key in keys))


def infer_family(profile):
    """Map identity/channel labels to an editable prior family. Unknown Claude is not CC Max.

    Role and capability notes are ignored so a Kiro duty that mentions CC Max
    cannot reclassify the economy channel.
    """
    channel = _joined(profile, CHANNEL_KEYS)
    if 'kiro' in channel:
        return 'kiro'
    if 'ccmax' in channel or 'claudeccmax' in channel:
        return 'ccmax'
    blob = _joined(profile, IDENTITY_KEYS)
    if 'astra' in blob:
        return 'astra'
    if (profile or {}).get('executor_kind') == 'codex' or 'codex' in blob:
        return 'codex'
    if 'grok' in blob:
        return 'grok'
    if 'gemini' in blob:
        return 'gemini'
    if 'deepseek' in blob:
        return 'deepseek'
    if 'kimi' in blob or 'moonshot' in blob:
        return 'kimi'
    if 'glm' in blob:
        return 'glm'
    if any(token in blob for token in ('claude', 'anthropic', 'opus', 'sonnet', 'haiku', 'fable')):
        return 'claude'
    return 'unknown'


def default_routing(profile):
    """Infer an editable resume from model/channel labels. Never a public ranking."""
    family = infer_family(profile)
    preset = FAMILY_STRENGTHS[family]
    strengths = {category: NEUTRAL for category in DEFAULT_CATEGORIES}
    strengths.update(preset['strengths'])
    return {
        'cost_tier': preset['cost_tier'],
        'strengths': strengths,
        'prior_strength': DEFAULT_PRIOR_STRENGTH,
        'source': DEFAULT_SOURCE,
    }


def _canonical_category(key):
    text = key.strip()
    return TAG_ALIASES.get(text, text)


def normalize_routing(raw):
    """Canonicalize a resume. Invalid values raise ContractError."""
    if raw is None:
        raw = {}
    if type(raw) is not dict:
        raise ContractError('routing_profile_invalid')
    cost_tier = raw.get('cost_tier', 'unknown')
    if cost_tier not in COST_TIERS:
        raise ContractError('routing_cost_tier_invalid')
    strengths_raw = raw.get('strengths', {})
    if strengths_raw is None:
        strengths_raw = {}
    if type(strengths_raw) is not dict:
        raise ContractError('routing_strengths_invalid')
    strengths = {}
    for key, value in strengths_raw.items():
        if type(key) is not str or not key.strip():
            raise ContractError('routing_strengths_invalid')
        name = _canonical_category(key)
        if len(name) > MAX_CATEGORY_LEN or _has_control(name):
            raise ContractError('routing_strengths_invalid')
        if not _finite_number(value) or value < 0 or value > 1:
            raise ContractError('routing_strengths_invalid')
        strengths[name] = float(value)
    prior = raw.get('prior_strength', DEFAULT_PRIOR_STRENGTH)
    if prior is None:
        prior = DEFAULT_PRIOR_STRENGTH
    if not _finite_number(prior) or prior <= 0:
        raise ContractError('routing_prior_strength_invalid')
    source = raw.get('source', '')
    if source is None:
        source = ''
    if type(source) is not str or len(source) > MAX_SOURCE_LEN or _has_control(source):
        raise ContractError('routing_source_invalid')
    return {
        'cost_tier': cost_tier,
        'strengths': strengths,
        'prior_strength': float(prior),
        'source': source,
    }


def _routing_for(profile):
    raw = (profile or {}).get('routing_profile')
    if raw in (None, '', {}):
        return default_routing(profile)
    return normalize_routing(raw)


def _unique_keep_order(values):
    seen, ordered = set(), []
    for value in values:
        if type(value) is not str:
            value = str(value)
        text = value.strip()
        if text and text not in seen:
            seen.add(text)
            ordered.append(text)
    return ordered


def _task_tags(task):
    task = task or {}
    tags = list(task.get('tags') or [])
    tags.extend(task.get('categories') or [])
    return _unique_keep_order(tags)


def _recognized_work_categories(profiles):
    recognized = set(DEFAULT_CATEGORIES)
    for profile in profiles or []:
        raw = (profile or {}).get('routing_profile')
        if type(raw) is not dict:
            continue
        strengths = raw.get('strengths')
        if type(strengths) is not dict:
            continue
        for key in strengths:
            if type(key) is not str or not key.strip():
                continue
            name = _canonical_category(key)
            if name and name not in UPGRADE_FLAGS and len(name) <= MAX_CATEGORY_LEN and not _has_control(name):
                recognized.add(name)
    return recognized


def _scoring_categories(tags, recognized):
    ordered, seen = [], set()
    for tag in tags:
        category = _canonical_category(tag)
        if category in UPGRADE_FLAGS - {'critical'}:
            continue
        if category not in recognized or category in seen:
            continue
        seen.add(category)
        ordered.append(category)
    return ordered


def _upgrade_task(tags):
    return bool(UPGRADE_FLAGS.intersection(_canonical_category(tag) for tag in tags))


def _strength_lookup(strengths, category):
    if category in strengths:
        return strengths[category]
    for alias, canonical in TAG_ALIASES.items():
        if category == canonical and alias in strengths:
            return strengths[alias]
        if category == alias and canonical in strengths:
            return strengths[canonical]
    return NEUTRAL


def _prior_score(strengths, categories):
    if not categories:
        return NEUTRAL
    return sum(_strength_lookup(strengths, category) for category in categories) / len(categories)


def _quality_weight(outcome):
    if outcome in PASS_OUTCOMES:
        return PASS_OUTCOMES[outcome]
    if outcome in FAIL_OUTCOMES:
        return FAIL_OUTCOMES[outcome]
    return None


def _is_quality_observation(item):
    if type(item) is not dict:
        return False
    if item.get('kind') == 'review':
        return False
    outcome = item.get('outcome')
    if outcome in NON_QUALITY_OUTCOMES or _quality_weight(outcome) is None:
        return False
    return True


def _observation_tags(item):
    tags = set()
    for tag in item.get('tags') or []:
        text = str(tag).strip() if type(tag) is not str else tag.strip()
        if text:
            tags.add(_canonical_category(text))
    return tags


def _matches_task(item, categories):
    if not categories:
        return False
    return bool(_observation_tags(item).intersection(categories))


def _created_at_sort_key(item):
    value = item.get('created_at')
    if type(value) is not str:
        return ''
    return value


def _merge_task_sample(items):
    usable = [item for item in items if _is_quality_observation(item)]
    if not usable:
        return None
    usable = sorted(usable, key=_created_at_sort_key)
    final = usable[-1]
    outcome = final.get('outcome')
    weight = _quality_weight(outcome)
    passed = outcome in PASS_OUTCOMES
    earlier_fail = any(item.get('outcome') in FAIL_OUTCOMES for item in usable[:-1])
    rework_records = any((item.get('changes_requested_records') or 0) > 0 for item in usable)
    if not passed:
        quality = 0.0
    elif earlier_fail or rework_records:
        quality = 1.0 - REWORK_PENALTY
    else:
        quality = 1.0
    return quality, weight


def _experience(agent_id, observations, categories):
    grouped = defaultdict(list)
    for item in observations or []:
        if type(item) is not dict or item.get('agent_id') != agent_id:
            continue
        if not _matches_task(item, categories):
            continue
        task_id = item.get('task_id') or item.get('run_id')
        if not task_id:
            continue
        grouped[task_id].append(item)
    qualities, weights = [], []
    for bucket in grouped.values():
        merged = _merge_task_sample(bucket)
        if merged is None:
            continue
        quality, weight = merged
        qualities.append(quality)
        weights.append(weight)
    if not weights:
        return 0.0, None
    total = sum(weights)
    observed = sum(quality * weight for quality, weight in zip(qualities, weights)) / total
    return total, observed


def _reason(routing, *, n, weight, upgrade, categories):
    bits = [f"简历来源：{routing['source'] or DEFAULT_SOURCE}"]
    if not categories:
        bits.append('任务无类别，使用中性先验，不因未知价格排除')
    if n <= 0:
        bits.append('无同类有效战绩，使用可编辑先验')
    else:
        bits.append(
            f"同类有效样本 {n:g}，经验权重 {weight:.3f}=n/(n+{routing['prior_strength']:g})，平滑混合"
        )
    if routing['cost_tier'] == 'premium' and upgrade:
        bits.append('关键/困难任务允许高价升级，无硬禁止')
    elif routing['cost_tier'] == 'premium':
        bits.append('高价对常规任务有软惩罚')
    if routing['cost_tier'] == 'unknown':
        bits.append('未知价格不视为免费或零分')
    return '；'.join(bits)


def _score_profile(profile, task, observations, recognized):
    profile = profile or {}
    agent_id = profile.get('agent_id')
    if type(agent_id) is not str or not agent_id.strip():
        raise ContractError('routing_agent_id_invalid')
    routing = _routing_for(profile)
    tags = _task_tags(task)
    categories = _scoring_categories(tags, recognized)
    upgrade = _upgrade_task(tags)
    prior = _prior_score(routing['strengths'], categories)
    n, observed = _experience(agent_id, observations, categories)
    weight = n / (n + routing['prior_strength']) if n else 0.0
    blended = prior if observed is None else (1.0 - weight) * prior + weight * observed
    score = blended
    if routing['cost_tier'] == 'premium' and not upgrade:
        score = blended - PREMIUM_ROUTINE_PENALTY
    return {
        'agent_id': agent_id,
        'score': score,
        'cost_tier': routing['cost_tier'],
        'prior_score': prior,
        'experience_weight': weight,
        'effective_samples': n,
        'observed_score': observed,
        'categories': list(categories),
        'reason': _reason(routing, n=n, weight=weight, upgrade=upgrade, categories=categories),
    }


def rank(profiles, task, observations):
    """Rank partners by smoothed same-class score. Explicit assignment stays with the caller.

    Equal scores keep input order after preferring economy over unknown. This
    function does not limit task turns.
    """
    if profiles is None:
        profiles = []
    if type(profiles) is not list:
        raise ContractError('routing_profiles_invalid')
    if task is None:
        task = {}
    if type(task) is not dict:
        raise ContractError('routing_task_invalid')
    if observations is None:
        observations = []
    if type(observations) is not list:
        raise ContractError('routing_observations_invalid')
    recognized = _recognized_work_categories(profiles)
    ranked = []
    for index, profile in enumerate(profiles):
        ranked.append((_score_profile(profile, task, observations, recognized), index))
    ranked.sort(key=lambda item: (
        -item[0]['score'],
        COST_TIE_RANK.get(item[0]['cost_tier'], 9),
        item[1],
    ))
    return [row for row, _index in ranked]
