import json
from pathlib import Path
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import urllib.request
import urllib.error

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.claude_gateway import ClaudeGateway, translate_request


@pytest.mark.parametrize('stream', [False, True])
def test_empty_success_is_error_without_automatic_resubmission(stream):
    calls=[]
    class Empty(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(200);self.end_headers()
            if stream:
                self.wfile.write(b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
            else:
                self.wfile.write(b'{"choices":[{"message":{"content":null},"finish_reason":"stop"}]}')
    server=ThreadingHTTPServer(('127.0.0.1',0),Empty)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    events=[]
    gateway=ClaudeGateway(f'http://127.0.0.1:{server.server_port}/v1','fixture','test',events.append).start()
    try:
        req=urllib.request.Request(gateway.url+'/v1/messages',data=json.dumps({'stream':stream,'messages':[]}).encode(),headers={'x-api-key':gateway.token})
        if stream:
            with urllib.request.urlopen(req,timeout=3) as response:output=response.read().decode()
            assert 'event: error' in output and 'event: message_stop' not in output
        else:
            with pytest.raises(urllib.error.HTTPError):urllib.request.urlopen(req,timeout=3)
        assert 'no text or tool calls' in gateway.failure['error']
        with pytest.raises(urllib.error.HTTPError):urllib.request.urlopen(req,timeout=3)
        assert len(calls)==1 and not any(e['phase']=='completed' for e in events)
    finally:
        gateway.close();server.shutdown();server.server_close();thread.join(timeout=2)


def test_tool_pairing_images_and_model_are_preserved():
    raw = {'model': 'ignored-cli-alias', 'system': [{'type': 'text', 'text': 'Policy'}], 'messages': [
        {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'private'},
                                        {'type': 'tool_use', 'id': 'x', 'name': 'Read', 'input': {'path': 'a'}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': 'content'},
                                   {'type': 'text', 'text': 'Continue'}]}]}
    converted = translate_request(raw, 'user-selected-model')
    assert converted['model'] == 'user-selected-model'
    assert converted['messages'][1]['tool_calls'][0]['id'] == 'x'
    assert converted['messages'][2]['tool_call_id'] == 'x'
    assert 'private' not in str(converted)
    assert converted['messages'][3]['content'] == 'Continue'


def test_tool_errors_remain_actionable_text():
    converted = translate_request({'messages': [{'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 't', 'is_error': True,
         'content': [{'type': 'text', 'text': 'Missing required field: goal'}]}]}]}, 'custom')
    message = converted['messages'][0]
    assert message['role'] == 'tool' and message['tool_call_id'] == 't'
    assert isinstance(message['content'], str) and 'goal' in message['content']
    assert 'failed' in message['content']


def test_multiple_image_tool_results_keep_strict_tool_reply_order():
    raw={'messages':[{'role':'user','content':[
        {'type':'tool_result','tool_use_id':'image-a','content':[{'type':'image','source':{'type':'base64','media_type':'image/png','data':'aGVsbG8='}}]},
        {'type':'tool_result','tool_use_id':'text-b','content':'File found'},
        {'type':'text','text':'Compare the image'}]}]}
    messages=translate_request(raw,'selected')['messages']
    assert [m['role'] for m in messages]==['tool','tool','user','user']
    assert all(isinstance(m['content'],str) for m in messages if m['role']=='tool')
    assert 'image-a' in messages[2]['content'][0]['text']
    assert messages[2]['content'][1]['image_url']['url']=='data:image/png;base64,aGVsbG8='


def test_null_content_blocks_are_treated_as_empty_without_gateway_crash():
    converted = translate_request({'messages': [
        {'role': 'assistant', 'content': None},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'continue'}]},
    ]}, 'custom')
    assert converted['messages'][-1]['content'] == 'continue'


def test_sdk_retry_does_not_resubmit_upstream():
    received = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            received.append(1)
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(503); self.end_headers()
            self.wfile.write(b'provider unavailable')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    gateway = ClaudeGateway(f'http://127.0.0.1:{server.server_port}/v1', 'fixture', 'any').start()
    try:
        request = urllib.request.Request(gateway.url + '/v1/messages',
            data=json.dumps({'messages': [{'role': 'user', 'content': 'hello'}]}).encode(),
            headers={'x-api-key': gateway.token})
        for expected in (503, 400, 400):
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=3)
            assert caught.value.code == expected
        assert len(received) == 1 and gateway.failure['http_status'] == 503
    finally:
        gateway.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_real_loopback_stream_preserves_split_tools_and_usage():
    received = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
            chunks = [
                {'choices': [{'index': 0, 'delta': {'content': 'Checking the artifact.'}}]},
                {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'call-x', 'function': {'name': 'Read', 'arguments': '{"pa'}}]}}]},
                {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': 'th":"a"}'}}]}, 'finish_reason': 'tool_calls'}]},
                {'choices': [], 'usage': {'prompt_tokens': 123, 'completion_tokens': 9}}]
            for x in chunks: self.wfile.write(b'data: '+json.dumps(x).encode()+b'\n\n')
            self.wfile.write(b'data: [DONE]\n\n')
    server = ThreadingHTTPServer(('127.0.0.1',0), Upstream)
    t = threading.Thread(target=server.serve_forever,daemon=True); t.start()
    events=[]
    gateway=ClaudeGateway('http://127.0.0.1:'+str(server.server_port)+'/v1','fixture-upstream-key','test-model',events.append).start()
    try:
        body={'stream':True,'max_tokens':30,'messages':[{'role':'user','content':'read a'}]}
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(urllib.request.Request(gateway.url+'/v1/messages',data=json.dumps(body).encode()),timeout=5)
        assert caught.value.code==401
        req=urllib.request.Request(gateway.url+'/v1/messages',data=json.dumps(body).encode(),headers={'x-api-key':gateway.token})
        with urllib.request.urlopen(req,timeout=5) as response: lines=response.read().splitlines()
        output=[json.loads(line[5:]) for line in lines if line.startswith(b'data:')]
        tool=next(x for x in output if x['type']=='content_block_start' and x['content_block']['type']=='tool_use')['content_block']
        assert tool['id']=='call-x' and tool['name']=='Read'
        delta=next(x for x in output if x['type']=='content_block_delta' and x['delta']['type']=='input_json_delta')['delta']
        assert json.loads(delta['partial_json'])=={'path':'a'}
        assert output[-2]['delta']['stop_reason']=='tool_use'
        assert output[-2]['usage']=={'input_tokens':123,'output_tokens':9}
        assert received[0]['model']=='test-model'
        assert events[-1]['usage']['prompt_tokens']==123
        active=None
        for event in output:
            if event['type']=='content_block_start':
                assert active is None, 'Claude requires sequential content blocks'
                active=event['index']
            elif event['type']=='content_block_stop':
                assert event['index']==active
                active=None
        assert active is None
    finally:
        gateway.close();server.shutdown();server.server_close();t.join(timeout=2)


def test_native_messages_preserves_stream_and_provider_identity():
    received = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append((self.path, self.headers.get('x-api-key'), body))
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            for event in [
                {'type': 'message_start', 'message': {'usage': {'input_tokens': 12}}},
                {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'native proof'}},
                {'type': 'content_block_stop', 'index': 0},
                {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 3}},
                {'type': 'message_stop'},
            ]:
                self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    events = []
    gateway = ClaudeGateway(f'http://127.0.0.1:{server.server_port}/v1', 'native-fixture-key',
                            'selected-native', events.append, protocol='anthropic').start()
    try:
        request = urllib.request.Request(gateway.url + '/v1/messages',
            data=json.dumps({'model': 'cli-alias', 'stream': True, 'messages': []}).encode(),
            headers={'x-api-key': gateway.token})
        with urllib.request.urlopen(request, timeout=3) as response:
            body = response.read().decode()
        assert 'native proof' in body and 'message_stop' in body
        assert received[0][0] == '/v1/messages' and received[0][1] == 'native-fixture-key'
        assert received[0][2]['model'] == 'selected-native'
        assert events[-1]['usage'] == {'input_tokens': 12, 'output_tokens': 3}
        with urllib.request.urlopen(request, timeout=3) as response:
            assert 'message_stop' in response.read().decode()
        assert len(received) == 2 and gateway.failure is None
    finally:
        gateway.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_native_http_200_error_is_not_success_or_replayed():
    received, events = [], []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"type":"error","error":{"message":"provider capacity unavailable"}}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    gateway = ClaudeGateway(f'http://127.0.0.1:{server.server_port}/v1', 'native-fixture-key',
                            'selected-native', events.append, protocol='anthropic').start()
    try:
        request = urllib.request.Request(gateway.url + '/v1/messages',
            data=json.dumps({'messages': []}).encode(), headers={'x-api-key': gateway.token})
        for expected in (502, 400):
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=3)
            assert caught.value.code == expected
        assert len(received) == 1
        assert not any(e['phase'] == 'completed' for e in events)
        assert 'capacity unavailable' in gateway.failure['error']
    finally:
        gateway.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_inflight_cli_retry_never_reaches_provider():
    entered, release = threading.Event(), threading.Event()
    received, events, first_result = [], [], []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])))
            entered.set(); release.wait(4)
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"original result"},"finish_reason":"stop"}]}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    gateway = ClaudeGateway(f'http://127.0.0.1:{server.server_port}/v1', 'fixture', 'any', events.append).start()
    def request(path='/v1/messages'):
        return urllib.request.Request(gateway.url + path, data=b'{"messages":[]}', headers={'x-api-key': gateway.token})
    def first():
        with urllib.request.urlopen(request(), timeout=5) as response:
            first_result.append(json.load(response))
    caller = threading.Thread(target=first)
    try:
        caller.start(); assert entered.wait(2)
        with urllib.request.urlopen(request('/v1/messages/count_tokens'), timeout=2) as response:
            assert 'input_tokens' in json.load(response)
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request(), timeout=2)
        assert caught.value.code == 400
        assert 'still in flight' in caught.value.read().decode()
        release.set(); caller.join(timeout=3)
        assert first_result[0]['content'][0]['text'] == 'original result'
        with urllib.request.urlopen(request(), timeout=2) as response:
            assert json.load(response)['content'][0]['text'] == 'original result'
        assert len(received) == 2 and gateway.failure is None
        assert any(e['phase'] == 'inflight_duplicate_blocked' for e in events)
        assert gateway.request_lock.acquire(timeout=2)
        gateway.request_lock.release()
    finally:
        release.set(); caller.join(timeout=3)
        gateway.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_stream_ping_survives_delayed_headers_and_partial_events(protocol):
    received, observed = [], []
    class Slow(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])))
            time.sleep(.25)
            self.send_response(200); self.end_headers()
            if protocol == 'anthropic':
                events = [
                    {'type':'message_start','message':{'usage':{'input_tokens':4}}},
                    {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
                    {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'native slow result'}},
                    {'type':'content_block_stop','index':0},
                    {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':3}},
                    {'type':'message_stop'}]
                for event in events:
                    self.wfile.write(('event: '+event['type']+'\n').encode());self.wfile.flush()
                    time.sleep(.06)
                    self.wfile.write(b'data: '+json.dumps(event).encode()+b'\n\n');self.wfile.flush()
            else:
                first={'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'id':'call-slow','function':{'name':'Read','arguments':'{"path": '}}]}}]}
                last={'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'function':{'arguments':'"file"}'}}]},'finish_reason':'tool_calls'}]}
                self.wfile.write(b'data: '+json.dumps(first).encode()+b'\n\n')
                self.wfile.flush();time.sleep(.25)
                self.wfile.write(b'data: '+json.dumps(last).encode()+b'\n\ndata: [DONE]\n\n')
    server=ThreadingHTTPServer(('127.0.0.1',0),Slow)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    gateway=ClaudeGateway(f'http://127.0.0.1:{server.server_port}/v1','fixture','selected',observed.append,
                          protocol=protocol,heartbeat_interval=.025).start()
    try:
        req=urllib.request.Request(gateway.url+'/v1/messages',data=b'{"stream":true,"messages":[]}',headers={'x-api-key':gateway.token})
        with urllib.request.urlopen(req,timeout=.15) as response:
            payload=response.read().decode()
        frames=[part for part in payload.split('\n\n') if part.strip()]
        parsed=[]
        for frame in frames:
            lines=frame.splitlines()
            kinds=[line[7:] for line in lines if line.startswith('event: ')]
            data=[json.loads(line[6:]) for line in lines if line.startswith('data: ')]
            assert len(kinds)==len(data)==1, 'Heartbeat must not split a native event'
            assert kinds[0]==data[0]['type']
            parsed.extend(data)
        assert sum(e['type']=='ping' for e in parsed)>=3
        assert parsed[-1]['type']=='message_stop' and len(received)==1
        assert gateway.failure is None
        assert any(e['phase']=='completed' for e in observed)
        if protocol=='openai':
            delta=next(e['delta'] for e in parsed if e['type']=='content_block_delta')
            assert json.loads(delta['partial_json'])=={'path':'file'}
        else:
            assert 'native slow result' in payload
    finally:
        gateway.close();server.shutdown();server.server_close();thread.join(timeout=2)


def test_delayed_stream_http_failure_emits_error_without_terminal_success_or_replay():
    received=[]
    class SlowError(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])))
            time.sleep(.25);self.send_response(503);self.end_headers();self.wfile.write(b'busy')
    server=ThreadingHTTPServer(('127.0.0.1',0),SlowError)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    gateway=ClaudeGateway(f'http://127.0.0.1:{server.server_port}/v1','fixture','selected',heartbeat_interval=.025).start()
    try:
        req=urllib.request.Request(gateway.url+'/v1/messages',data=b'{"stream":true,"messages":[]}',headers={'x-api-key':gateway.token})
        with urllib.request.urlopen(req,timeout=.15) as response:payload=response.read().decode()
        assert 'event: ping' in payload and 'event: error' in payload and 'event: message_stop' not in payload
        assert payload.rstrip().endswith('"busy"}}')
        with pytest.raises(urllib.error.HTTPError):urllib.request.urlopen(req,timeout=1)
        assert len(received)==1 and gateway.failure['http_status']==503
    finally:
        gateway.close();server.shutdown();server.server_close();thread.join(timeout=2)
