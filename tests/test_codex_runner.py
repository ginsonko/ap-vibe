"""Executor contract checks; real model acceptance is recorded separately."""
import io
import json
import sys
import time
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind import agent_studio, codex_runner
from ap_mind.contracts import ContractError
from ap_mind.studio_server import StudioEpisodeService


@pytest.fixture
def studio(tmp_path, monkeypatch):
    root = tmp_path / 'project'; root.mkdir()
    service = StudioEpisodeService(tmp_path / 'data', project_root=root, codex_project_id='test-project')
    monkeypatch.setattr(agent_studio, 'codex_command', lambda: ['/fixture/codex'])
    yield service.agent_studio
    service.close()


def profile(studio, **extra):
    return studio.save({'name': 'Codex伙伴', 'executor_kind': 'codex', 'auth_mode': 'local_login',
                        'model': '', **extra})['agent']


def test_native_profile_does_not_require_or_store_key(studio):
    agent = profile(studio, api_key='discard-this-secret')
    assert agent['base_url'] == '' and not agent['key_saved']
    with studio.registry._connect() as connection:
        assert connection.execute('SELECT secret FROM studio_agents').fetchone()[0] == b''
    with pytest.raises(ContractError, match='protocol_not_implemented'):
        profile(studio, auth_mode='api_key', api_key='key', model='any-model',
                protocol='openai', base_url='https://example.invalid/v1')


def test_responses_command_keeps_credentials_in_environment():
    config = {'auth_mode': 'api_key', 'model': 'user-defined-model',
              'base_url': 'https://example.invalid/v1', 'request_timeout_seconds': 123}
    args = codex_runner.command({'executor_command': ['codex']}, config, {'command': 'python'}, 'instructions')
    values = [args[i+1] for i, item in enumerate(args) if item == '-c']
    assert '--ignore-user-config' not in args
    assert 'model_provider="ap_vibe"' in values
    assert 'sandbox_mode="danger-full-access"' in values
    assert '--approve-for-me' in args
    provider = next(x for x in values if x.startswith('model_providers='))
    assert 'AP_VIBE_CODEX_API_KEY' in provider and 'responses' in provider
    assert 'request_max_retries" = 0' in provider


def test_local_connection_imports_only_provider_and_moves_secrets(tmp_path):
    (tmp_path / 'config.toml').write_text('''model = "configured-model"
model_provider = "custom"
[model_providers.custom]
name = "custom"
base_url = "https://example.invalid/v1"
wire_api = "responses"
experimental_bearer_token = "private-bearer"
http_headers = { "x-api-key" = "private-header" }
[mcp_servers.unrelated]
url = "https://unrelated.invalid"
''', encoding='utf-8')
    connection, env = codex_runner.local_connection(tmp_path)
    assert connection['model'] == 'configured-model'
    assert 'mcp_servers' not in connection
    assert not any(x in json.dumps(connection) for x in ['private-bearer', 'private-header'])
    assert env['AP_VIBE_CODEX_LOCAL_KEY'] == 'private-bearer'
    assert env['AP_VIBE_CODEX_HEADER_0'] == 'private-header'


def test_codex_public_events_resume_and_no_replay(studio, monkeypatch):
    agent = profile(studio)
    seen = []
    class Process:
        pid = 5555
        def __init__(self, args, **kwargs):
            seen.append((args, kwargs))
            self.stdin = io.BytesIO()
            self.stderr = io.BytesIO()
            events = [
                {'type': 'thread.started', 'thread_id': '11111111-2222-4333-8444-555555555555'},
                {'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'never-public'}},
                {'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'private-command', 'aggregated_output': 'private-output', 'status': 'completed'}},
                {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Actual public answer'}},
                {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 20}},
            ]
            self.stdout = io.BytesIO(('\n'.join(json.dumps(e) for e in events) + '\n').encode())
        def poll(self): return 0
        def wait(self): return 0
    monkeypatch.setattr(codex_runner.subprocess, 'Popen', Process)
    payload = {'request_id': 'codex-one', 'agent_id': agent['agent_id'], 'project_id': 'test-project', 'prompt': 'read project'}
    first = studio.start(payload)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        run = studio.runs(first['run_id'])['runs'][0]
        if run['state'] not in {'starting', 'running'}: break
        time.sleep(.01)
    assert run['state'] == 'awaiting_review', run
    assert run['session_id'] == '11111111-2222-4333-8444-555555555555'
    public = json.dumps(studio.runs(first['run_id']))
    assert 'Actual public answer' in public
    assert not any(x in public for x in ['never-public', 'private-command', 'private-output'])
    assert studio.start(payload)['replayed'] and len(seen) == 1
    second = studio.start({**payload, 'request_id': 'codex-two', 'continue_run_id': first['run_id']})
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and len(seen) < 2: time.sleep(.01)
    assert 'resume' in seen[1][0] and run['session_id'] in seen[1][0]
    assert seen[0][1]['cwd'] == seen[1][1]['cwd']
    assert studio.runs(second['run_id'])['runs'][0]['codex_home'] == run['codex_home']


def test_missing_receipt_is_uncertain_and_not_auto_replayed(studio, monkeypatch):
    agent = profile(studio)
    class Failed:
        pid = 5556
        def __init__(self, *args, **kwargs):
            self.stdin = io.BytesIO(); self.stdout = io.BytesIO(); self.stderr = io.BytesIO(b'connection lost\n')
        def poll(self): return 1
        def wait(self): return 1
    monkeypatch.setattr(codex_runner.subprocess, 'Popen', Failed)
    req = {'request_id': 'codex-uncertain', 'agent_id': agent['agent_id'], 'project_id': 'test-project', 'prompt': 'work'}
    started = studio.start(req)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        run = studio.runs(started['run_id'])['runs'][0]
        if run['state'] not in {'starting', 'running'}: break
        time.sleep(.01)
    assert run['state'] == 'uncertain'
    assert studio.start(req)['replayed']
