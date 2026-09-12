"""CodeBuddy CLI JSONL public projection: real transcript + boundary cases.

The allowed WorkBuddy probe JSONL is source evidence. Expected public user/
assistant text and model are extracted from that file at test time, not copied
in as frozen literals. Synthetic fixtures cover empty/partial/rewrite/privacy
paths that a three-line live file cannot.
"""
import json
from pathlib import Path
import sys

import pytest

from ap_mind.contracts import ContractError
from ap_mind.codebuddy_sessions import (
    CodeBuddySessions, codebuddy_event, public_text, read, record_model,
    snapshot, sources,
)
import os
ALLOWED_TRANSCRIPT = Path(os.environ.get('AP_VIBE_WORKBUDDY_TEST_TRANSCRIPT', '/missing-workbuddy-transcript'))

PRIVATE_MARKERS = (
    'PRIVATE_REASONING', 'TOOL_ARGS', 'TOOL_RESULT', 'SNAPSHOT_BODY',
    'rawUsage', 'completion_thinking_tokens', 'trackedFileBackups',
    '__codebuddyLocal', 'traceId', 'sk-testsecretvalue',
)


def write_jsonl(path, rows, extra=b''):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = b''.join((json.dumps(row, ensure_ascii=False) + '\n').encode() for row in rows)
    path.write_bytes(body + extra)


def extract_public(path):
    """Independent public-field extractor used as the real-file oracle."""
    users, assistants, models, session_ids, cwds = [], [], [], [], []
    skipped = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get('type') != 'message':
            skipped.append(record.get('type'))
            continue
        texts = []
        content = record.get('content')
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get('type') in {'input_text', 'output_text', 'text'}:
                    if isinstance(block.get('text'), str) and block['text'].strip():
                        texts.append(block['text'])
        text = '\n'.join(texts)
        if record.get('role') == 'user' and text:
            users.append(text)
        elif record.get('role') == 'assistant' and text:
            assistants.append(text)
        if isinstance(record.get('sessionId'), str):
            session_ids.append(record['sessionId'])
        if isinstance(record.get('cwd'), str):
            cwds.append(record['cwd'])
        data = record.get('providerData') if record.get('role') == 'assistant' else None
        if isinstance(data, dict) and isinstance(data.get('model'), str) and data['model'].strip():
            models.append(data['model'])
    return {
        'users': users, 'assistants': assistants, 'models': models,
        'session_id': session_ids[-1] if session_ids else path.stem,
        'cwd': cwds[-1] if cwds else '', 'skipped_types': skipped,
    }


def message(role, text, **extra):
    block_type = 'input_text' if role == 'user' else 'output_text'
    row = {
        'id': extra.pop('id', role + '-1'),
        'timestamp': extra.pop('timestamp', 1789189026367),
        'type': 'message',
        'role': role,
        'content': [{'type': block_type, 'text': text}],
        'sessionId': extra.pop('sessionId', 'sess-native'),
        'cwd': extra.pop('cwd', r'C:\work\project'),
    }
    row.update(extra)
    return row


@pytest.mark.skipif(not ALLOWED_TRANSCRIPT.is_file(), reason='allowed CodeBuddy CLI transcript missing')
@pytest.mark.skipif(not ALLOWED_TRANSCRIPT.is_file(), reason='optional real local transcript')
def test_real_cli_transcript_public_user_assistant_and_model():
    oracle = extract_public(ALLOWED_TRANSCRIPT)
    assert oracle['users'] and oracle['assistants'] and oracle['models']
    page = read(ALLOWED_TRANSCRIPT)
    texts = [(e['role'], e['text']) for e in page['events']]
    assert texts == (
        [('user', t) for t in oracle['users']] +
        [('assistant', t) for t in oracle['assistants']]
    )
    assert page['session_id'] == oracle['session_id'] == ALLOWED_TRANSCRIPT.stem
    assert page['cwd'] == oracle['cwd']
    assert page['model'] == oracle['models'][-1]
    assert page['events'][-1]['model'] == oracle['models'][-1]
    assert page['model_source'] == 'providerData.model'
    assert 'file-history-snapshot' in oracle['skipped_types']
    dumped = json.dumps(page)
    for hidden in ('rawUsage', 'completion_thinking_tokens', 'trackedFileBackups',
                   '__codebuddyLocal', 'traceId', 'conversationRequestId'):
        assert hidden not in dumped
    assert all(e['role'] in {'user', 'assistant'} for e in page['events'])
    meta = snapshot(ALLOWED_TRANSCRIPT)
    assert meta['gui_sessions_db'] is False and meta['storage'] == 'codebuddy_cli_jsonl'


def test_sources_and_class_read_synthetic_tree(tmp_path):
    root = tmp_path / 'projects' / 'encoded-cwd'
    write_jsonl(root / 'sess-native.jsonl', [
        message('user', 'Continue the product'),
        {'type': 'file-history-snapshot', 'snapshot': {'trackedFileBackups': 'SNAPSHOT_BODY'}, 'cwd': r'C:\work\project'},
        message('assistant', 'Public progress', providerData={'model': 'glm-5.3', 'rawUsage': {'completion_thinking_tokens': 3}}),
    ])
    files = {}
    found = sources(tmp_path / 'projects', files, 10)
    assert len(found) == 1
    source = found[0]
    assert source['session_id'] == 'sess-native' and source['model'] == 'glm-5.3'
    assert source['source_id'] in files
    monitor = CodeBuddySessions([tmp_path / 'projects'], cache_seconds=0)
    discovered = monitor.discover()
    sid = discovered['sources'][0]['source_id']
    page = monitor.read(sid)
    assert [e['text'] for e in page['events']] == ['Continue the product', 'Public progress']
    assert 'SNAPSHOT_BODY' not in json.dumps(page)
    with pytest.raises(ContractError):
        monitor.read('../../secrets')


def test_private_blocks_claude_shape_and_credentials_are_dropped(tmp_path):
    path = tmp_path / 's.jsonl'
    write_jsonl(path, [
        message('user', 'Visible prompt api_key=sk-testsecretvalue'),
        {
            'type': 'message', 'role': 'assistant', 'id': 'a',
            'timestamp': 1789189030008, 'sessionId': 'sess-native', 'cwd': r'C:\work',
            'content': [
                {'type': 'thinking', 'text': 'PRIVATE_REASONING'},
                {'type': 'tool_use', 'name': 'Read', 'input': {'secret': 'TOOL_ARGS'}},
                {'type': 'tool_result', 'content': 'TOOL_RESULT'},
                {'type': 'output_text', 'text': 'Visible answer'},
            ],
            'providerData': {
                'model': 'glm-5.3',
                'rawUsage': {'completion_thinking_tokens': 9},
                'traceId': 'TRACE',
            },
            'message': {'usage': {'input_tokens': 1}},
        },
        {'type': 'assistant', 'message': {'role': 'assistant', 'model': 'claude-spoof',
                                         'content': [{'type': 'text', 'text': 'CLAUDE_SHAPED'}]}},
        {'type': 'user', 'message': {'role': 'user', 'content': 'CLAUDE_USER'}},
    ])
    page = read(path)
    dumped = json.dumps(page)
    for hidden in PRIVATE_MARKERS + ('CLAUDE_SHAPED', 'CLAUDE_USER', 'TRACE'):
        assert hidden not in dumped
    assert [e['text'] for e in page['events']] == ['Visible prompt api_key=[REDACTED]', 'Visible answer']
    assert page['model'] == 'glm-5.3'
    assert public_text([{'type': 'thinking', 'text': 'PRIVATE_REASONING'}]) == ''
    assert record_model({'type': 'message', 'role': 'user', 'providerData': {'model': 'nope'}}) is None
    assert codebuddy_event({'type': 'file-history-snapshot', 'snapshot': {}}, 0, 1) is None


def test_empty_partial_rewrite_and_cursors(tmp_path):
    empty = tmp_path / 'empty.jsonl'
    empty.write_bytes(b'')
    blank = read(empty)
    assert blank['events'] == [] and blank['session_id'] == 'empty'
    assert blank['ok'] and not blank['has_more']

    path = tmp_path / 'live.jsonl'
    first = message('user', 'first prompt', id='u1')
    write_jsonl(path, [first])
    page = read(path, limit=20)
    assert [e['text'] for e in page['events']] == ['first prompt']
    partial = json.dumps(message('assistant', 'second', id='a1', providerData={'model': 'glm-5.3'})).encode()
    with path.open('ab') as stream:
        stream.write(partial[:40])
    waiting = read(path, after=page['cursor'], expected_generation=page['generation'])
    assert waiting['events'] == [] and waiting['trailing_partial']
    assert waiting['cursor'] == page['cursor']
    with path.open('ab') as stream:
        stream.write(partial[40:] + b'\n')
    later = read(path, after=waiting['cursor'], expected_generation=waiting['generation'])
    assert [e['text'] for e in later['events']] == ['second']
    assert later['events'][0]['model'] == 'glm-5.3'

    write_jsonl(path, [message('user', 'rewritten', id='u2')])
    reset = read(path, after=later['cursor'], expected_generation=later['generation'])
    assert reset['reset'] and [e['text'] for e in reset['events']] == ['rewritten']


def test_pagination_invalid_lines_and_db_not_parsed(tmp_path):
    path = tmp_path / 'pages.jsonl'
    rows = [message('user' if i % 2 == 0 else 'assistant', f'line {i}', id=str(i),
                    timestamp=1789189026367 + i,
                    providerData={'model': 'glm-5.3'} if i % 2 else {'agent': 'cli'})
            for i in range(8)]
    write_jsonl(path, rows)
    with path.open('ab') as stream:
        stream.write(b'not-json\n')
    newest = read(path, limit=2)
    assert [e['text'] for e in newest['events']] == ['line 6', 'line 7']
    assert newest['has_older']
    older = read(path, before=newest['history_before'], expected_generation=newest['generation'], limit=2)
    assert older['events'][-1]['text'] == 'line 5'
    mixed = tmp_path / 'mixed'
    write_jsonl(mixed / 'ok.jsonl', [message('user', 'keep')])
    (mixed / 'sessions.db').write_bytes(b'sqlite')
    files = {}
    found = sources(mixed, files, 10)
    assert len(found) == 1 and found[0]['session_id'] == 'sess-native'
    monitor = CodeBuddySessions([mixed / 'sessions.db'], cache_seconds=0)
    discovered = monitor.discover()
    assert discovered['sources'] == []
    assert any('sessions.db' in w for w in discovered['warnings'])
    with pytest.raises(ContractError):
        read(path, after=0, before=1)


def test_external_directory_integration_and_missing_other_clients(tmp_path):
    from ap_mind.external_sessions import ExternalSessions
    from ap_mind.harness_registry import HARNESS
    native=tmp_path/'codebuddy'/'projects'/'cwd'/'one.jsonl'
    write_jsonl(native,[message('user','Continue project'),message('assistant','Actual answer', providerData={'model':'native-model'})])
    adapter=ExternalSessions(tmp_path/'vibe',roots={'workbuddy':[str(native.parent.parent)],'opencode':[str(tmp_path/'missing')]},cache_seconds=0)
    result=adapter.discover()
    source=result['sources'][0]
    assert source['harness']=='workbuddy' and source['model']=='native-model'
    assert HARNESS['workbuddy']['executor'] is None
    assert [e['text'] for e in adapter.read(source['source_id'])['events']]==['Continue project','Actual answer']
    assert result['coverage']['opencode']['total_discovered']==0
