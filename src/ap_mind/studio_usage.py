"""Provider usage with explicit accounting semantics; no price assumptions."""
import math


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def numeric_fields(value, depth=0):
    """Keep usage counters, never text, credentials or arbitrary response bodies."""
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {str(k): result for k, v in value.items()
                if (result := numeric_fields(v, depth + 1)) is not None}
    return number(value)


def normalize(raw, protocol):
    raw = raw if isinstance(raw, dict) else {}
    details = raw.get('prompt_tokens_details') or raw.get('input_tokens_details') or {}
    details = details if isinstance(details, dict) else {}
    if protocol == 'anthropic':
        uncached = number(raw.get('input_tokens'))
        cached = number(raw.get('cache_read_input_tokens'))
        write = number(raw.get('cache_creation_input_tokens'))
        # Missing optional cache counters do not invalidate a native input count.
        total = uncached + (cached or 0) + (write or 0) if uncached is not None else None
        output = number(raw.get('output_tokens'))
        semantics = 'input_excludes_cache_read_and_write'
    else:
        total = number(raw.get('prompt_tokens', raw.get('input_tokens')))
        cached = number(details.get('cached_tokens', raw.get('cached_input_tokens')))
        write = number(details.get('cache_write_tokens', raw.get('cache_write_tokens')))
        output = number(raw.get('completion_tokens', raw.get('output_tokens')))
        # OpenAI input is inclusive. Do not count cached tokens twice.
        uncached = max(0, total - cached - (write or 0)) if total is not None and cached is not None else None
        semantics = 'input_includes_cache'
    invalid = bool(total is not None and ((cached or 0) + (write or 0) > total))
    return {'input_tokens': total, 'output_tokens': output, 'uncached_input_tokens': uncached,
            'cache_read_input_tokens': cached, 'cache_creation_input_tokens': write,
            'total_tokens': total + output if total is not None and output is not None else None,
            'protocol': protocol, 'input_semantics': semantics,
            'complete': total is not None and output is not None and not invalid,
            'inconsistent': invalid, 'raw_usage': numeric_fields(raw)}


def aggregate(records):
    # One terminal record per request. The latest record reconciles an earlier
    # partial snapshot, it is not another charge.
    unique = {item['request_id']: item for item in records if item.get('request_id')}
    values = [item['usage'] for item in unique.values()]
    fields = ('input_tokens', 'output_tokens', 'uncached_input_tokens',
              'cache_read_input_tokens', 'cache_creation_input_tokens', 'total_tokens')
    totals = {field: sum(v[field] for v in values if v.get(field) is not None)
              if any(v.get(field) is not None for v in values) else None for field in fields}
    return {**totals, 'request_count': len(values), 'complete_requests': sum(v.get('complete', False) for v in values),
            'unknown_requests': sum(not v.get('complete', False) for v in values),
            'source': 'provider_requests', 'records': list(unique.values())}


def run_usage(run):
    records = run.get('provider_usage')
    if records:
        return aggregate(records)
    raw = (run.get('result') or {}).get('usage')
    value = normalize(raw, 'codex' if run.get('executor_kind') == 'codex' else 'anthropic')
    return {**value, 'source': 'executor_report' if raw else 'unavailable', 'request_count': None}


def anthropic_wire_usage(raw):
    value = normalize(raw, 'openai')
    # Transport compatibility requires numeric counters. These placeholders are
    # not evidence of measured zero usage; accounting uses raw provider records.
    result = {'input_tokens': value['input_tokens'] or 0, 'output_tokens': value['output_tokens'] or 0}
    if value['cache_read_input_tokens'] is not None and not value['inconsistent']:
        result['cache_read_input_tokens'] = value['cache_read_input_tokens']
        result['input_tokens'] -= value['cache_read_input_tokens']
    if value['cache_creation_input_tokens'] is not None and not value['inconsistent']:
        result['cache_creation_input_tokens'] = value['cache_creation_input_tokens']
        result['input_tokens'] -= value['cache_creation_input_tokens']
    return result
