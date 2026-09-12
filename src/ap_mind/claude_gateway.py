"""Per-run Anthropic Messages gateway for OpenAI Chat Completions.

Only the configured model/provider is used. Authorized request recovery keeps
the same CLI turn and delivers tool calls once. The loopback token is distinct
from the API key; private reasoning is not published to the event ledger.
"""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler
from .local_http import LocalThreadingHTTPServer as ThreadingHTTPServer
import json
import math
import secrets
import select
import socket
import tempfile
import threading
import urllib.error
import urllib.request
import uuid

from .studio_usage import anthropic_wire_usage


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _text(blocks):
    if isinstance(blocks, str):
        return blocks
    return '\n'.join(b.get('text', '') for b in blocks if b.get('type') == 'text')


def _content(blocks):
    if not blocks:
        return ''
    if isinstance(blocks, str):
        return blocks
    result = []
    for b in blocks:
        kind = b.get('type')
        if kind == 'text':
            result.append({'type': 'text', 'text': b.get('text', '')})
        elif kind == 'image':
            source = b.get('source', {})
            if source.get('type') == 'base64':
                url = 'data:' + source['media_type'] + ';base64,' + source['data']
            elif source.get('type') == 'url':
                url = source['url']
            else:
                raise ValueError('Unsupported image source')
            result.append({'type': 'image_url', 'image_url': {'url': url}})
        elif kind not in {'tool_use', 'tool_result', 'thinking', 'redacted_thinking'}:
            raise ValueError('Unsupported content block: ' + str(kind))
    if result and all(item['type'] == 'text' for item in result):
        return '\n'.join(item['text'] for item in result)
    return result or None


def translate_request(raw, model):
    messages = []
    if raw.get('system'):
        messages.append({'role': 'system', 'content': _text(raw['system'])})
    for message in raw.get('messages', []):
        blocks = message.get('content') or []
        role = message['role']
        if isinstance(blocks, str):
            messages.append({'role': role, 'content': blocks})
            continue
        if role == 'assistant':
            calls = [{'id': b['id'], 'type': 'function', 'function': {'name': b['name'], 'arguments': json.dumps(b.get('input', {}), ensure_ascii=False)}}
                     for b in blocks if b.get('type') == 'tool_use']
            item = {'role': role, 'content': _content(blocks)}
            if calls:
                item['tool_calls'] = calls
            messages.append(item)
        else:
            # Tool replies must precede subsequent user content for strict providers.
            image_replies=[]
            for b in blocks:
                if b.get('type') == 'tool_result':
                    blocks_result = b.get('content', '')
                    value = (_text(blocks_result) if isinstance(blocks_result, str) or
                             all(x.get('type') == 'text' for x in blocks_result) else _content(blocks_result))
                    if b.get('is_error') and isinstance(value, str):
                        value = 'Tool execution failed. Correct the call before continuing.\n' + value
                    if isinstance(value,list) and any(part.get('type')=='image_url' for part in value):
                        # OpenAI chat-completions tool messages accept text, not
                        # image parts. Pair the acknowledgement with a labelled
                        # user image message after ALL tool-call replies.
                        image_replies.extend([{'type':'text','text':'Image returned by tool_call_id='+b['tool_use_id']+
                            (' (tool reported an error; inspect with caution)' if b.get('is_error') else '')},*value])
                        value='Image output is attached in the following user message, labelled with tool_call_id='+b['tool_use_id']
                    messages.append({'role': 'tool', 'tool_call_id': b['tool_use_id'],
                                     'content': value if value is not None else ''})
            if image_replies:
                messages.append({'role':'user','content':image_replies})
            content = _content(blocks)
            if content:
                messages.append({'role': role, 'content': content})
    value = {'model': model, 'messages': messages, 'max_tokens': raw.get('max_tokens', 4096),
             'stream': bool(raw.get('stream'))}
    if value['stream']:
        value['stream_options'] = {'include_usage': True}
    for key in ('temperature', 'top_p'):
        if key in raw:
            value[key] = raw[key]
    if raw.get('stop_sequences'):
        value['stop'] = raw['stop_sequences']
    tools = []
    for tool in raw.get('tools', []):
        if not tool.get('input_schema'):
            raise ValueError('Server-side tools are not supported by this adapter')
        tools.append({'type': 'function', 'function': {'name': tool['name'], 'description': tool.get('description', ''),
                                                     'parameters': tool['input_schema']}})
    if tools:
        value['tools'] = tools
        choice = raw.get('tool_choice', {'type': 'auto'})
        value['tool_choice'] = ({'type': 'function', 'function': {'name': choice['name']}} if choice.get('type') == 'tool'
                                else {'any': 'required', 'none': 'none', 'auto': 'auto'}.get(choice.get('type'), 'auto'))
    return value


def usage(value):
    return anthropic_wire_usage(value)


def reason(value):
    return {'tool_calls': 'tool_use', 'length': 'max_tokens', 'stop': 'end_turn'}.get(value, 'end_turn')


class UpstreamResponseError(ValueError):
    """Preserve structured provider errors, including errors inside HTTP 200."""
    def __init__(self, error):
        super().__init__(json.dumps(error, ensure_ascii=False))
        details = error if isinstance(error, dict) else {}
        self.retryable = not any(str(details.get(field, '')).lower() in {
            'authentication_error', 'permission_error', 'invalid_request_error',
            'invalid_api_key', 'invalid_parameter', 'model_not_found',
            '401', '403', '404', '400', '422',
        } for field in ('type', 'code', 'status', 'status_code'))


class RequestBudgetPaused(Exception):
    """A local spending policy is not a provider transport error."""


class ClaudeGateway:
    def __init__(self, base_url, key, model, observe=lambda event: None, timeout=300, protocol='openai', heartbeat_interval=15, before_request=None, upstream_mode='stream', max_request_retries=0, retry_backoff_seconds=1):
        if type(max_request_retries) is not int or max_request_retries<0:
            raise ValueError('Request retries must be a nonnegative integer')
        if not math.isfinite(retry_backoff_seconds) or retry_backoff_seconds<0:
            raise ValueError('Retry backoff must be nonnegative and finite')
        self.max_request_retries=max_request_retries
        self.retry_backoff_seconds=retry_backoff_seconds
        self.closed=threading.Event()
        if upstream_mode not in {'stream', 'buffered'}:
            raise ValueError('Invalid upstream response mode')
        self.upstream_mode = upstream_mode
        if not math.isfinite(heartbeat_interval) or heartbeat_interval <= 0:
            raise ValueError('Heartbeat interval must be positive and finite')
        self.token = secrets.token_urlsafe(32)
        self.key = key
        self.model = model
        base = base_url.rstrip('/')
        self.protocol = protocol
        route = '/messages' if protocol == 'anthropic' else '/chat/completions'
        self.endpoint = base + (route if base.endswith('/v1') else '/v1' + route)
        self.observe = observe
        self.timeout = timeout
        self.heartbeat_interval = heartbeat_interval
        self.failure = None
        self.paused = None
        self.before_request = before_request
        self.request_lock = threading.Lock()
        self.inflight_id = None
        self.delivering = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, status, value):
                if getattr(self, 'holds_request', False):
                    owner.delivering = True
                self.io_stage = 'client_write'
                data = _json(value)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def send_event(self, kind, data):
                if self.heartbeat_error:
                    self.io_stage = 'client_write'
                    raise self.heartbeat_error
                self.send_stream_frame(b'event: ' + kind.encode() + b'\ndata: ' + _json(data) + b'\n\n', kind in {'message_stop', 'error'})

            def send_stream_frame(self, frame, terminal=False):
                if self.capture_frames:
                    previous_stage = self.io_stage
                    self.io_stage = 'capture_write'
                    self.capture.write(frame)
                    self.io_stage = previous_stage
                    return
                self.io_stage = 'client_write'
                if terminal and self.holds_request:
                    owner.delivering = True
                with self.stream_lock:
                    if terminal:
                        self.heartbeat_stop.set()
                    self.wfile.write(frame)
                    self.wfile.flush()
                self.io_stage = 'upstream_read'

            def start_stream(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.flush()

                def heartbeat():
                    while not self.heartbeat_stop.wait(owner.heartbeat_interval):
                        try:
                            with self.stream_lock:
                                if self.heartbeat_stop.is_set():
                                    return
                                self.wfile.write(b'event: ping\ndata: {"type":"ping"}\n\n')
                                self.wfile.flush()
                        except OSError as exc:
                            self.heartbeat_error = exc
                            self.heartbeat_stop.set()
                            return
                self.heartbeat_thread = threading.Thread(target=heartbeat, daemon=True, name='ap-vibe-stream-ping')
                self.heartbeat_thread.start()

            def upstream_lines(self, upstream):
                self.io_stage = 'upstream_read'
                for line in upstream:
                    yield line
                    self.io_stage = 'upstream_read'

            def client_closed(self):
                # Also handle non-stream clients and disconnects between pings.
                try:
                    readable, _, _ = select.select([self.connection], [], [], 0)
                    return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b''
                except OSError:
                    return True

            def release_request(self):
                if self.capture_frames:return
                if self.holds_request:
                    self.holds_request = False
                    owner.inflight_id = None
                    owner.delivering = False
                    owner.request_lock.release()

            def exchange(self, req, body, client_stream, request_id):
                self.io_stage = 'upstream_connect'
                with urllib.request.urlopen(req, timeout=owner.timeout) as upstream:
                    if body.get('stream'):
                        if owner.protocol == 'anthropic':
                            self.native_stream(upstream, request_id)
                        else:
                            self.stream(upstream, request_id)
                    else:
                        data = json.load(upstream)
                        self.attempt_usage = data.get('usage') or {}
                        if data.get('error') or data.get('type') == 'error':
                            raise UpstreamResponseError(data.get('error', data))
                        if owner.protocol == 'anthropic':
                            if data.get('type') != 'message' or not isinstance(data.get('content'), list) or not data['content']:
                                raise ValueError('Upstream returned an invalid Messages response')
                            owner.observe({'phase': 'completed', 'request_id': request_id, 'usage': data.get('usage')})
                            self.complete_message(data, client_stream)
                            return
                        choice = data['choices'][0]
                        message = choice['message']
                        content = []
                        if message.get('content'):
                            content.append({'type': 'text', 'text': message['content']})
                        for call in message.get('tool_calls', []):
                            content.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'],
                                            'input': json.loads(call['function']['arguments'])})
                        measured = data.get('usage', {})
                        if not content:
                            raise ValueError('Upstream returned no text or tool calls; finish_reason=' + str(choice.get('finish_reason')))
                        response = {'id': data.get('id', request_id), 'type': 'message', 'role': 'assistant',
                                           'model': owner.model, 'content': content, 'stop_reason': reason(choice.get('finish_reason')),
                                           'stop_sequence': None, 'usage': usage(measured)}
                        owner.observe({'phase': 'completed', 'request_id': request_id, 'usage': measured or None})
                        self.complete_message(response, client_stream)

            def do_POST(self):
                token = self.headers.get('x-api-key') or self.headers.get('Authorization', '').removeprefix('Bearer ')
                if not hmac.compare_digest(token, owner.token):
                    return self.send_json(401, {'type': 'error', 'error': {'type': 'authentication_error', 'message': 'Local gateway token mismatch'}})
                streaming = False
                submitted = False
                self.io_stage = 'client_read'
                self.holds_request = False
                self.capture_frames = False
                self.capture = None
                self.stream_lock = threading.Lock()
                self.heartbeat_stop = threading.Event()
                self.heartbeat_thread = None
                self.heartbeat_error = None
                self.attempt_usage = {}
                self.attempt_count = 0
                request_id = 'apvibe-gateway-' + uuid.uuid4().hex
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if size < 1 or size > 16*1024*1024:
                        raise ValueError('Request size invalid')
                    raw = json.loads(self.rfile.read(size))
                    if self.path.split('?')[0] == '/v1/messages/count_tokens':
                        # Scheduling hint only. Actual billed usage comes from the provider.
                        estimated = math.ceil(len(_json(raw)) / 3)
                        owner.observe({'phase': 'token_estimate', 'input_tokens_estimate': estimated})
                        return self.send_json(200, {'input_tokens': estimated})
                    if self.path.split('?')[0] != '/v1/messages':
                        return self.send_json(404, {'type': 'error', 'error': {'type': 'not_found_error', 'message': 'Unsupported gateway route'}})
                    # A CLI may time out before the provider socket does. Reject
                    # overlapping retries while the original is still in flight.
                    self.holds_request = owner.request_lock.acquire(blocking=False)
                    if not self.holds_request and owner.delivering:
                        # CC can submit its next turn immediately after receiving
                        # message_stop, before this handler finishes cleanup.
                        # Wait only at that boundary; no second provider call.
                        self.holds_request = owner.request_lock.acquire(timeout=5)
                    if not self.holds_request:
                        message = 'Previous model request is still in flight; overlapping resubmission blocked. Original request: ' + str(owner.inflight_id)
                        owner.observe({'phase': 'inflight_duplicate_blocked', 'request_id': request_id,
                                       'original_request_id': owner.inflight_id, 'outcome': 'uncertain'})
                        return self.send_json(400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': message}})
                    # This gateway owns the retry budget. An SDK retry after
                    # exhaustion must not start a second retry series.
                    if owner.failure:
                        return self.send_json(400, {'type': 'error', 'error': {'type': 'invalid_request_error',
                            'message': f'Current model request stopped after {owner.failure.get("attempts",1)} attempts. ' + owner.failure['error']}})
                    pause = owner.paused or (owner.before_request() if owner.before_request else None)
                    if pause:
                        owner.paused = pause
                        owner.observe({'phase': 'budget_paused', 'request_id': request_id, 'submitted': False})
                        return self.send_json(400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': pause}})
                    body = ({**raw, 'model': owner.model} if owner.protocol == 'anthropic'
                            else translate_request(raw, owner.model))
                    client_stream = bool(raw.get('stream'))
                    if owner.upstream_mode == 'buffered':
                        body['stream'] = False
                        body.pop('stream_options', None)
                    owner.inflight_id = request_id
                    headers = {'Content-Type': 'application/json', 'X-Request-ID': request_id}
                    if owner.protocol == 'anthropic':
                        headers.update({'x-api-key': owner.key, 'anthropic-version': self.headers.get('anthropic-version', '2023-06-01')})
                        if self.headers.get('anthropic-beta'):
                            headers['anthropic-beta'] = self.headers['anthropic-beta']
                    else:
                        headers['Authorization'] = 'Bearer ' + owner.key
                    req = urllib.request.Request(owner.endpoint, data=_json(body), headers=headers)
                    if client_stream:
                        self.start_stream()
                        streaming = True
                    logical_request_id=request_id
                    for retry in range(owner.max_request_retries+1):
                        if owner.closed.is_set() or self.heartbeat_error or self.client_closed():
                            self.io_stage='client_write'
                            raise BrokenPipeError('Client or gateway closed during request recovery')
                        if retry:
                            pause=owner.before_request() if owner.before_request else None
                            if pause:
                                owner.paused=pause
                                owner.observe({'phase':'budget_paused','request_id':request_id,'submitted':False,
                                               'attempts':self.attempt_count})
                                raise RequestBudgetPaused(pause)
                            request_id=logical_request_id+':retry:'+str(retry)
                            headers['X-Request-ID']=request_id
                            req=urllib.request.Request(owner.endpoint,data=_json(body),headers=headers)
                            owner.inflight_id=request_id
                        owner.observe({'phase':'submitted','request_id':request_id,'logical_request_id':logical_request_id,
                                       'retry':retry,'model':owner.model})
                        submitted = True
                        self.attempt_count += 1
                        self.capture_frames=streaming and owner.max_request_retries>0
                        self.capture=tempfile.SpooledTemporaryFile(max_size=2*1024*1024) if self.capture_frames else None
                        self.attempt_usage = {}
                        try:
                            self.exchange(req,body,client_stream,request_id)
                            self.capture_frames=False
                            if self.capture:
                                # Buffered chunks can split an SSE JSON frame.
                                # Stop pings before flushing so a heartbeat can
                                # never be inserted inside that frame's JSON.
                                self.heartbeat_stop.set()
                                capture_size = self.capture.seek(0, 2)
                                self.capture.seek(0)
                                # Keep ownership through delivery and cleanup.
                                owner.delivering = True
                                while chunk:=self.capture.read(65536):
                                    if self.capture.tell() == capture_size:
                                        self.send_stream_frame(chunk, terminal=True)
                                    else:
                                        self.send_stream_frame(chunk)
                            if retry:owner.observe({'phase':'retry_recovered','request_id':request_id,
                                'logical_request_id':logical_request_id,'retries_used':retry})
                            break
                        except Exception as exc:
                            status=exc.code if isinstance(exc,urllib.error.HTTPError) else None
                            error_text=(exc.read(8000).decode('utf8',errors='replace') if isinstance(exc,urllib.error.HTTPError) else str(exc))
                            error_text=error_text.replace(owner.key,'[redacted]').replace(owner.token,'[redacted]')[:2500]
                            exc._apvibe_error_text=error_text
                            retryable=(status in {408,425,429} or status is not None and status>=500 or
                                       status is None and isinstance(exc,(OSError,ValueError,KeyError,IndexError)))
                            if isinstance(exc, UpstreamResponseError):
                                retryable = exc.retryable
                            # No client-visible partial tool stream is replayed.
                            safe=self.io_stage.startswith('upstream') and (not streaming or self.capture_frames)
                            if retry>=owner.max_request_retries or not retryable or not safe or owner.closed.is_set() or self.heartbeat_error:
                                owner.failure={'request_id':request_id,'logical_request_id':logical_request_id,
                                               'http_status':status,'error':error_text,'attempts':retry+1}
                                raise
                            owner.observe({'phase':'attempt_error','request_id':request_id,'logical_request_id':logical_request_id,
                                           'http_status':status,'error':error_text,'attempt':retry+1,'outcome':'unknown',
                                           'usage':self.attempt_usage or None})
                            delay=min(30,owner.retry_backoff_seconds*2**min(retry,5))
                            owner.observe({'phase':'retry_wait','request_id':request_id,'logical_request_id':logical_request_id,
                                           'retry':retry+1,'max_retries':owner.max_request_retries,'wait_seconds':delay})
                            if owner.closed.wait(delay):
                                self.io_stage='client_write'
                                raise BrokenPipeError('Gateway closed during retry wait')
                        finally:
                            self.capture_frames=False
                            if self.capture:self.capture.close();self.capture=None
                except RequestBudgetPaused as exc:
                    error = {'type':'error','error':{'type':'invalid_request_error','message':str(exc)}}
                    try:
                        if streaming:
                            self.send_event('error', error)
                        else:
                            self.send_json(400, error)
                    except OSError:
                        pass
                except (BrokenPipeError, ConnectionResetError) as exc:
                    side = 'upstream' if self.io_stage.startswith('upstream') else 'client'
                    if submitted:
                        owner.failure = {**(owner.failure or {}), 'request_id': request_id, 'http_status': None,
                                         'attempts': self.attempt_count,
                                         'error': f'{side} connection interrupted at {self.io_stage}; outcome is unknown. {type(exc).__name__} ({getattr(exc, "winerror", None) or exc.errno})'}
                    owner.observe({'phase': side + '_disconnected', 'request_id': request_id,
                                   'io_stage': self.io_stage, 'outcome': 'uncertain', 'usage':self.attempt_usage or None})
                except Exception as exc:
                    status = 502
                    if isinstance(exc, urllib.error.HTTPError):
                        status = exc.code
                        text = getattr(exc,'_apvibe_error_text',None) or exc.read(8000).decode('utf-8', errors='replace')
                    else:
                        text = str(exc)
                    text = text.replace(owner.key, '[redacted]').replace(owner.token, '[redacted]')
                    owner.observe({'phase': 'error', 'request_id': request_id, 'http_status': status,
                                   'error': text[:2500], 'usage':self.attempt_usage or None})
                    owner.failure = {**(owner.failure or {}),'request_id': request_id, 'http_status': status,
                                     'attempts':self.attempt_count, 'error': text[:2500]}
                    error = {'type': 'error', 'error': {'type': 'api_error', 'message': text[:2500]}}
                    try:
                        if streaming:
                            self.send_event('error', error)
                        else:
                            self.send_json(status, error)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                finally:
                    self.capture_frames=False
                    if self.capture:self.capture.close()
                    self.heartbeat_stop.set()
                    if self.heartbeat_thread:
                        self.heartbeat_thread.join(timeout=1)
                    self.release_request()

            def complete_message(self, value, client_stream):
                if not client_stream:
                    self.send_json(200, value)
                    return
                self.send_event('message_start', {'type': 'message_start', 'message': {
                    **value, 'content': [], 'stop_reason': None, 'stop_sequence': None,
                    'usage': {**value.get('usage', {}), 'output_tokens': 0}}})
                for index, block in enumerate(value['content']):
                    kind = block.get('type')
                    start = dict(block)
                    deltas = []
                    if kind == 'text':
                        start['text'] = ''
                        deltas.append({'type': 'text_delta', 'text': block.get('text', '')})
                    elif kind == 'tool_use':
                        start['input'] = {}
                        deltas.append({'type': 'input_json_delta', 'partial_json': json.dumps(block.get('input', {}), ensure_ascii=False)})
                    elif kind == 'thinking':
                        start.update(thinking='', signature='')
                        deltas.append({'type': 'thinking_delta', 'thinking': block.get('thinking', '')})
                        if block.get('signature'):
                            deltas.append({'type': 'signature_delta', 'signature': block['signature']})
                    self.send_event('content_block_start', {'type': 'content_block_start', 'index': index, 'content_block': start})
                    for delta in deltas:
                        self.send_event('content_block_delta', {'type': 'content_block_delta', 'index': index, 'delta': delta})
                    self.send_event('content_block_stop', {'type': 'content_block_stop', 'index': index})
                self.send_event('message_delta', {'type': 'message_delta', 'delta': {
                    'stop_reason': value.get('stop_reason'), 'stop_sequence': value.get('stop_sequence')},
                    'usage': value.get('usage', {})})
                self.send_event('message_stop', {'type': 'message_stop'})

            def native_stream(self, upstream, request_id):
                measured = {}
                stopped = False
                frame = bytearray()
                for line in self.upstream_lines(upstream):
                    frame.extend(line)
                    if line.startswith(b'data:'):
                        event = json.loads(line[5:])
                        measured.update(event.get('message', {}).get('usage') or {})
                        measured.update(event.get('usage') or {})
                        self.attempt_usage = measured
                        if event.get('type') == 'error':
                            raise UpstreamResponseError(event.get('error', {}))
                        stopped = stopped or event.get('type') == 'message_stop'
                    if line.strip() == b'' or stopped:
                        self.send_stream_frame(bytes(frame).rstrip(b'\r\n') + b'\n\n', stopped)
                        frame.clear()
                    if stopped:
                        break
                if not stopped:
                    raise ValueError('Upstream Messages stream closed without message_stop')
                owner.observe({'phase': 'completed', 'request_id': request_id, 'usage': measured or None})

            def stream(self, upstream, request_id):
                self.send_event('message_start', {'type': 'message_start', 'message': {
                    'id': request_id, 'type': 'message', 'role': 'assistant', 'model': owner.model,
                    'content': [], 'stop_reason': None, 'stop_sequence': None,
                    'usage': {'input_tokens': 0, 'output_tokens': 0}}})
                blocks = {}
                pending = {}
                measured = {}
                finish = None
                saw_done = False

                def open_block(key, value):
                    if key not in blocks:
                        blocks[key] = len(blocks)
                        self.send_event('content_block_start', {'type': 'content_block_start', 'index': blocks[key], 'content_block': value})
                    return blocks[key]

                for line in self.upstream_lines(upstream):
                    if not line.startswith(b'data:'):
                        continue
                    payload = line[5:].strip()
                    if payload == b'[DONE]':
                        saw_done = True
                        break
                    if not payload:
                        continue
                    chunk = json.loads(payload)
                    if chunk.get('usage'):
                        measured = chunk['usage']
                        self.attempt_usage = measured
                    if chunk.get('error'):
                        raise UpstreamResponseError(chunk['error'])
                    for choice in chunk.get('choices', []):
                        if choice.get('index', 0) != 0:
                            continue
                        delta = choice.get('delta', {})
                        text = delta.get('content')
                        if text:
                            index = open_block('text', {'type': 'text', 'text': ''})
                            self.send_event('content_block_delta', {'type': 'content_block_delta', 'index': index,
                                                                   'delta': {'type': 'text_delta', 'text': text}})
                        for tool in delta.get('tool_calls', []):
                            key = 'tool-' + str(tool['index'])
                            item = pending.setdefault(key, {'id': '', 'name': '', 'arguments': ''})
                            item['id'] += tool.get('id') or ''
                            function = tool.get('function', {})
                            item['name'] += function.get('name') or ''
                            item['arguments'] += function.get('arguments') or ''
                            # Start a tool after a complete name is normally available;
                            # final JSON is verified before sending the tool-use result.
                        if choice.get('finish_reason'):
                            finish = choice['finish_reason']
                if not saw_done and not finish:
                    raise ValueError('Upstream stream closed without completion')
                if not blocks and not pending:
                    raise ValueError('Upstream returned no text or tool calls; finish_reason=' + str(finish))
                # Anthropic content blocks are sequential. Close text before
                # opening a tool block, and close each tool before the next.
                for index in blocks.values():
                    self.send_event('content_block_stop', {'type': 'content_block_stop', 'index': index})
                for key, item in pending.items():
                    if not item['id'] or not item['name']:
                        raise ValueError('Incomplete upstream tool identity')
                    json.loads(item['arguments'] or '{}')
                    index = open_block(key, {'type': 'tool_use', 'id': item['id'], 'name': item['name'], 'input': {}})
                    self.send_event('content_block_delta', {'type': 'content_block_delta', 'index': index,
                                                           'delta': {'type': 'input_json_delta', 'partial_json': item['arguments'] or '{}'}})
                    self.send_event('content_block_stop', {'type': 'content_block_stop', 'index': index})
                self.send_event('message_delta', {'type': 'message_delta', 'delta': {
                    'stop_reason': 'tool_use' if pending else reason(finish), 'stop_sequence': None}, 'usage': usage(measured)})
                self.send_event('message_stop', {'type': 'message_stop'})
                owner.observe({'phase': 'completed', 'request_id': request_id, 'usage': measured or None})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.closed.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
