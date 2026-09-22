"""No paid providers: command contracts and a real loopback gateway exchange."""
import io
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_agent_studio import studio, profile
from ap_mind import agent_studio, organization_execution
from ap_mind.claude_model import claude_cli_model
from ap_mind.contracts import ContractError


def test_cli_identity_is_configurable_preserved_and_separate_from_upstream(studio):
    first = profile(studio, model='provider/new-model', cli_model=' opus ')
    assert first['model'] == 'provider/new-model' and first['cli_model'] == 'opus'
    saved = profile(studio, model=first['model'], agent_id=first['agent_id'], expected_revision=1)
    assert saved['cli_model'] == 'opus' and saved['capability_revision'] == 1
    copied = profile(studio, api_key='', copy_from_agent_id=saved['agent_id'], copy_from_revision=2)
    assert copied['cli_model'] == 'opus'
    cleared = profile(studio, agent_id=first['agent_id'], model=first['model'], expected_revision=2, cli_model='')
    assert claude_cli_model(cleared) == 'sonnet' and cleared['capability_revision'] == 3
    for invalid in (None, 42, 'bad\nmodel', 'x' * 201):
        with pytest.raises(ContractError, match='agent_cli_model_invalid'):
            profile(studio, cli_model=invalid)
    for upstream in ('grok-4.6', 'gemini-3.8-flash-high', 'new-vendor/unknown', 'claude-opus-future'):
        assert claude_cli_model({'model': upstream}) == 'sonnet'
    assert claude_cli_model({'cli_model': 'custom-client-alias'}) == 'custom-client-alias'


@pytest.fixture
def upstream():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append({'path': self.path, 'body': body})
            if self.path == '/v1/messages':
                value = {'id':'fixture','type':'message','role':'assistant','model':body['model'],
                         'content':[{'type':'text','text':'OK'}],'stop_reason':'end_turn',
                         'usage':{'input_tokens':1,'output_tokens':1}}
            else:
                value = {'choices':[{'message':{'content':'OK'},'finish_reason':'stop'}],
                         'usage':{'prompt_tokens':1,'completion_tokens':1}}
            data = json.dumps(value).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield f'http://127.0.0.1:{server.server_port}/v1', requests
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def gateway_exchange(env):
    body = {'model': env['ANTHROPIC_MODEL'], 'messages':[{'role':'user','content':'test'}], 'max_tokens':16, 'stream':False}
    req = urllib.request.Request(env['ANTHROPIC_BASE_URL'] + '/v1/messages', data=json.dumps(body).encode(),
                                 headers={'x-api-key':env['ANTHROPIC_API_KEY'], 'content-type':'application/json'})
    with urllib.request.urlopen(req, timeout=5) as response:
        assert json.load(response)['content'][0]['text'] == 'OK'


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('kind', ['studio', 'curation'])
@pytest.mark.parametrize('cli_model', ['', 'opus'])
def test_both_execution_paths_keep_actual_gateway_model(studio, upstream, monkeypatch, tmp_path, protocol, kind, cli_model):
    base, requests = upstream
    a = profile(studio, base_url=base, model='arbitrary-provider-model', protocol=protocol, cli_model=cli_model, max_request_retries=0)
    expected = cli_model or 'sonnet'
    monkeypatch.setattr(agent_studio, 'claude_executable', lambda: '/fixture/claude')
    if kind == 'curation':
        with organization_execution.claude_launch(studio.service.organization, 'fixture-task',
                {'id':'agent:'+a['agent_id'],'agent_id':a['agent_id'],'configuration_revision':1}, tmp_path) as (args, env):
            assert args[args.index('--model')+1] == expected
            assert env['ANTHROPIC_DEFAULT_OPUS_MODEL'] == expected
            assert env['ANTHROPIC_DEFAULT_HAIKU_MODEL'] == expected
            gateway_exchange(env)
    else:
        seen = {}
        class Process:
            pid = 123456
            def __init__(self, args, **kwargs):
                seen.update(args=args, env=kwargs['env'])
                self.stdin = io.BytesIO()
                class Output(io.BytesIO):
                    exchanged = False
                    def readline(self, *args):
                        if not self.exchanged:
                            self.exchanged = True
                            gateway_exchange(kwargs['env'])
                        return super().readline(*args)
                self.stdout = Output((json.dumps({'type':'result','subtype':'success','is_error':False})+'\n').encode())
                self.stderr = io.BytesIO()
            def wait(self): return 0
            def poll(self): return 0
        monkeypatch.setattr(agent_studio.subprocess, 'Popen', Process)
        started = studio.start({'request_id':'model-separation','agent_id':a['agent_id'],'project_id':'test-project','prompt':'fixture'})
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            run = studio.runs(started['run_id'])['runs'][0]
            if run['state'] not in ('starting','running'): break
            time.sleep(.02)
        assert run['state'] == 'awaiting_review', run.get('error')
        assert seen['args'][seen['args'].index('--model')+1] == expected
        assert seen['env']['ANTHROPIC_MODEL'] == expected
        assert run['model'] == a['model'] and run['cli_model'] == expected
    assert len(requests) == 1
    assert requests[0]['body']['model'] == a['model']
    assert requests[0]['path'] == ('/v1/messages' if protocol=='anthropic' else '/v1/chat/completions')
