"""Observed Yinzi sample ratios, not provider tariff or cache-miss prices."""
from urllib.parse import urlsplit

# Screenshot column is per 100 MILLION input tokens and already includes net
# output/cache billing. Displayed token counts are rounded. Keep the sample
# ratio and approximate blended total-token rate as separate fields.
SAMPLES = [
    ('regular-opus5', 'claude-opus-5', 'regular', 31.1763, 41120000, 91000, 75.8139, .915),
    ('regular-fable', 'claude-fable-5-1', 'regular', 9.3095, 1760000, 21000, 530.3601, .0),
    ('regular-gemini-high', 'gemini-3.8-flash-high', 'regular', 2.3948, 77160000, 1030000, 3.1036, .827),
    ('regular-grok', 'grok-4.6', 'regular', 1.5303, 47860000, 810000, 3.1976, .759),
    ('regular-opus48', 'claude-opus-4-8', 'regular', 1.2857, 49900000, 83000, 2.5768, .497),
    ('ccmax-opus5', 'claude-opus-5', 'ccmax', 99.5434, 29840000, 150000, 333.5594, .790),
]


def catalog():
    values = []
    for key, model, channel, cost, incoming, outgoing, ratio, cached in SAMPLES:
        blended = round(cost * 1000000 / (incoming + outgoing), 8)
        source = (f'银子API用户授权用量截图，2026-09-11至09-12；{channel}/{model}；'
                  f'每亿输入综合成本{ratio}元，真实缓存率{cached:.1%}；'
                  '按净消费÷截图输入加输出得到近似混合总Token参考价，截图Token有取整。'
                  '四类同值，已包含缓存收益，不再乘缓存折扣；不是输入/输出原始单价，后续渠道或命中率变化会有偏差。')
        values.append({'sample_id': key, 'model': model, 'channel': channel, 'currency': 'CNY',
                       'observed_at': '2026-09-12', 'net_cost_cny': cost,
                       'displayed_input_tokens': incoming, 'displayed_output_tokens': outgoing,
                       'effective_per_100m_input_cny': ratio, 'effective_per_million_input_cny': ratio / 100,
                       'cache_rate': cached, 'blended_per_million_total_cny': blended,
                       'price_basis': 'blended', 'price_source': source,
                       'prices': dict.fromkeys(('input', 'output', 'cache_read', 'cache_write'), blended)})
    return values


def reference_for(profile):
    try:
        url = urlsplit(profile.get('base_url', ''))
    except ValueError:
        return None
    if url.scheme != 'https' or url.hostname != 'api.yinziapi.top' or url.path.rstrip('/') != '/v1':
        return None
    from .studio_routing import infer_family
    family = infer_family(profile)
    channel = 'ccmax' if family == 'ccmax' else 'regular'
    # An unlabeled Opus5 channel cannot safely inherit Kiro pricing.
    if profile.get('model') == 'claude-opus-5' and family not in ('ccmax', 'kiro'):
        return None
    return next((x for x in catalog() if x['model'] == profile.get('model') and x['channel'] == channel), None)


def budget_defaults(profile):
    reference = reference_for(profile)
    return ({k: reference[k] for k in ('prices', 'price_basis', 'price_source', 'currency')} if reference else {})
