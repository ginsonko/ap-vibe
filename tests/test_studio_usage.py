from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.studio_usage import aggregate, anthropic_wire_usage, normalize, numeric_fields, run_usage


def test_cache_translation_preserves_total_and_native_exclusive_semantics():
    raw = {'prompt_tokens': 1000, 'completion_tokens': 75,
           'prompt_tokens_details': {'cached_tokens': 400, 'cache_write_tokens': 100}}
    wire = anthropic_wire_usage(raw)
    assert wire == {'input_tokens': 500, 'output_tokens': 75,
                    'cache_read_input_tokens': 400, 'cache_creation_input_tokens': 100}
    assert normalize(wire, 'anthropic')['total_tokens'] == normalize(raw, 'openai')['total_tokens'] == 1075


def test_missing_and_invalid_usage_are_not_measured_zero():
    for raw in [None, {}, {'prompt_tokens': -1, 'completion_tokens': True}, {'input_tokens': float('nan')}]:
        value = normalize(raw, 'openai')
        assert value['input_tokens'] is None and not value['complete']
    assert normalize({'prompt_tokens': 2, 'completion_tokens': 3, 'prompt_tokens_details': {'cached_tokens': 4}}, 'openai')['inconsistent']
    assert run_usage({'result': {}})['source'] == 'unavailable'


def test_terminal_reconciliation_and_provider_precedence():
    first = {'request_id': 'a', 'usage': normalize({}, 'openai')}
    final = {'request_id': 'a', 'usage': normalize({'prompt_tokens': 10, 'completion_tokens': 2}, 'openai')}
    unknown = {'request_id': 'b', 'usage': normalize(None, 'openai')}
    value = aggregate([first, final, final, unknown])
    assert value['request_count'] == 2 and value['total_tokens'] == 12 and value['unknown_requests'] == 1
    measured = run_usage({'provider_usage': [final], 'result': {'usage': {'input_tokens': 999}}})
    assert measured['input_tokens'] == 10


def test_codex_cached_input_is_inclusive_and_raw_text_is_not_retained():
    value = normalize({'input_tokens': 250, 'output_tokens': 10, 'cached_input_tokens': 200}, 'codex')
    assert value['input_tokens'] == 250 and value['uncached_input_tokens'] == 50
    assert numeric_fields({'input_tokens': 4, 'key': 'secret', 'details': {'cache': 2, 'note': 'private'}}) == {'input_tokens': 4, 'details': {'cache': 2}}
