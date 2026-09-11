"""Real HTTP faults: retry only the current, undelivered model response."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import socketserver
import sys
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.claude_gateway import ClaudeGateway


@contextmanager
def recovering(actions, **options):
    calls, events = [], []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append({'id': self.headers['X-Request-ID'], 'body': body})
            action = actions[min(len(calls) - 1, len(actions) - 1)]
            if callable(action):
                action(self)
                return
            status, data, content_type = action
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.end_headers()
            self.wfile.write(data.encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway = ClaudeGateway(f'http://127.0.0.1:{server.server_port}/v1', 'fixture', 'fixture',
        events.append, **{'max_request_retries': 5, 'retry_backoff_seconds': 0,
                         'heartbeat_interval': .03, **options}).start()
    try:
        yield gateway, calls, events
    finally:
        gateway.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


BAD = (503, 'temporary overload', 'text/plain')
GOOD = (200, json.dumps({'choices': [{'message': {'content': 'recovered'}, 'finish_reason': 'stop'}],
                         'usage': {'prompt_tokens': 12, 'completion_tokens': 4}}), 'application/json')


def request(gateway, stream=True):
    req = urllib.request.Request(gateway.url + '/v1/messages', data=json.dumps({
        'messages': [{'role': 'user', 'content': 'continue'}], 'stream': stream}).encode(),
        headers={'x-api-key': gateway.token})
    with urllib.request.urlopen(req, timeout=5) as response:
        data = response.read().decode()
    return ([json.loads(line[6:]) for line in data.splitlines() if line.startswith('data: ')]
            if stream else json.loads(data))


@pytest.mark.parametrize('stream', [True, False])
def test_recovers_twice_and_resets_for_next_model_request(stream):
    with recovering([BAD, BAD, GOOD, BAD, GOOD], upstream_mode='buffered') as (gateway, calls, events):
        for expected in (3, 5):
            result = request(gateway, stream)
            assert 'recovered' in json.dumps(result)
            assert len(calls) == expected and gateway.failure is None
        assert len({call['id'] for call in calls}) == 5
        recoveries = [e for e in events if e['phase'] == 'retry_recovered']
        assert [e['retries_used'] for e in recoveries] == [2, 1]
        assert len({e['logical_request_id'] for e in recoveries}) == 2
        assert len([e for e in events if e['phase'] == 'submitted']) == 5
        assert all(call['body'] == calls[0]['body'] for call in calls)


@pytest.mark.parametrize('retries', [0, 5])
def test_exhaustion_is_bounded_even_if_sdk_submits_again(retries):
    with recovering([BAD], max_request_retries=retries, upstream_mode='buffered') as (gateway, calls, events):
        assert any(e['type'] == 'error' for e in request(gateway))
        assert len(calls) == retries + 1
        assert gateway.failure['attempts'] == retries + 1
        with pytest.raises(urllib.error.HTTPError) as error:
            request(gateway)
        assert error.value.code == 400 and len(calls) == retries + 1
        assert len([e for e in events if e['phase'] == 'retry_wait']) == retries


@pytest.mark.parametrize('status,body', [(401, 'unauthorized'), (403, 'forbidden'), (422, 'bad input'),
    (200, '{"error":{"type":"authentication_error","message":"bad key"}}'),
    (200, '{"error":{"code":"invalid_api_key"}}')])
def test_auth_and_parameters_do_not_retry(status, body):
    with recovering([(status, body, 'application/json')], upstream_mode='buffered') as (gateway, calls, events):
        assert any(e['type'] == 'error' for e in request(gateway))
        assert len(calls) == 1 and not any(e['phase'] == 'retry_wait' for e in events)


def native_frame(event):
    return 'event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n'


def test_failed_native_tool_stream_is_discarded_and_usage_retained():
    prefix = ''.join(native_frame(e) for e in [
        {'type': 'message_start', 'message': {'id': 'x', 'role': 'assistant', 'content': [],
             'usage': {'input_tokens': 8, 'output_tokens': 0}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {
             'type': 'tool_use', 'id': 'once', 'name': 'Write', 'input': {}}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {
             'type': 'input_json_delta', 'partial_json': '{"file_path":"result.txt","content":"OK"}'}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 4}},
    ])
    with recovering([(200, prefix, 'text/event-stream'),
                     (200, prefix + native_frame({'type': 'message_stop'}), 'text/event-stream')],
                    protocol='anthropic') as (gateway, calls, events):
        result = request(gateway)
        assert len(calls) == 2
        assert sum(e['type'] == 'message_start' for e in result) == 1
        assert sum(e['type'] == 'content_block_start' for e in result) == 1
        assert sum(e['type'] == 'message_stop' for e in result) == 1
        failed = next(e for e in events if e['phase'] == 'attempt_error')
        assert failed['usage'] == {'input_tokens': 8, 'output_tokens': 4}


def test_long_response_flush_preserves_all_bytes(monkeypatch):
    write = socketserver._SocketWriter.write
    def slow_chunk(writer, data):
        result = write(writer, data)
        if len(data) == 65536:
            time.sleep(.01)  # Allow pings to interleave if not stopped.
        return result
    monkeypatch.setattr(socketserver._SocketWriter, 'write', slow_chunk)
    text = 'Z' * (2 * 1024 * 1024 + 31)
    body = json.dumps({'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}]})
    with recovering([(200, body, 'application/json')], upstream_mode='buffered', heartbeat_interval=.002) as (gateway, calls, events):
        result = request(gateway)
        assert ''.join(e.get('delta', {}).get('text', '') for e in result) == text
        assert result[-1]['type'] == 'message_stop' and len(calls) == 1


@pytest.mark.parametrize('attempts', [1, 3])
def test_budget_stops_before_retry(attempts):
    checks = []
    def budget():
        checks.append(1)
        return 'budget exhausted' if len(checks) > attempts else None
    with recovering([BAD], before_request=budget, upstream_mode='buffered') as (gateway, calls, events):
        assert any(e['type'] == 'error' for e in request(gateway))
        assert len(calls) == attempts and gateway.paused == 'budget exhausted'
        assert gateway.failure is None
        assert not any(e['phase'] == 'error' for e in events)
        assert next(e for e in events if e['phase'] == 'budget_paused')['attempts'] == attempts


@pytest.mark.parametrize('stream', [False, True])
def test_client_disconnect_prevents_another_attempt(stream):
    entered, release = threading.Event(), threading.Event()
    def blocked(handler):
        entered.set()
        release.wait(3)
        handler.send_response(503)
        handler.end_headers()
    with recovering([blocked, GOOD], upstream_mode='buffered') as (gateway, calls, events):
        sock = socket.create_connection(('127.0.0.1', gateway.server.server_port))
        body = json.dumps({'messages': [], 'stream': stream}).encode()
        sock.sendall((f'POST /v1/messages HTTP/1.1\r\nHost: localhost\r\nx-api-key: {gateway.token}\r\n'
                      f'Content-Length: {len(body)}\r\n\r\n').encode() + body)
        assert entered.wait(2)
        sock.shutdown(socket.SHUT_RDWR)
        sock.close()
        release.set()
        deadline = time.monotonic() + 2
        while gateway.inflight_id and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(calls) == 1


@pytest.mark.parametrize('attempts', [1, 3])
def test_gateway_stop_prevents_retry(attempts):
    with recovering([BAD], retry_backoff_seconds=.2, upstream_mode='buffered') as (gateway, calls, events):
        result = []
        caller = threading.Thread(target=lambda: result.append(request(gateway)))
        caller.start()
        deadline = time.monotonic() + 2
        while not any(e['phase'] == 'retry_wait' and e['retry'] == attempts for e in events) and time.monotonic() < deadline:
            time.sleep(.01)
        gateway.closed.set()
        caller.join(timeout=2)
        assert not caller.is_alive() and len(calls) == attempts
        assert gateway.failure['attempts'] == attempts


def test_next_request_waits_for_terminal_delivery_cleanup():
    with recovering([BAD, GOOD, GOOD], upstream_mode='buffered') as (gateway, calls, events):
        def observe(event):
            events.append(event)
            if event['phase'] == 'retry_recovered':
                # Deliberately hold the first handler after message_stop.
                time.sleep(.25)
        gateway.observe = observe
        req = urllib.request.Request(gateway.url + '/v1/messages', data=b'{"messages":[],"stream":true}',
                                     headers={'x-api-key': gateway.token})
        with urllib.request.urlopen(req, timeout=3) as response:
            for line in response:
                if line.startswith(b'event: message_stop'):
                    break
            assert gateway.delivering
            assert 'recovered' in json.dumps(request(gateway))
        assert len(calls) == 3 and gateway.failure is None
