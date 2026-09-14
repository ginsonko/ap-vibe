"""Grok ACP public execution events and isolated native configuration."""
from .grok_sessions import update
from .studio_activity import tool_activity


def settings(profile, mcp):
    return {'models': {'default': profile['model'], 'max_retries': profile.get('max_request_retries', 5)},
        'endpoints': {'models_base_url': profile['base_url']},
        'cli': {'use_leader': False},
        'model': {profile['model']: {'model': profile['model'], 'base_url': profile['base_url'],
            'env_key': 'AP_VIBE_NATIVE_KEY', 'api_backend': {'openai': 'chat_completions', 'anthropic': 'messages', 'responses': 'responses'}[profile.get('protocol', 'openai')]}},
        'compat': {vendor: {item: False for item in ('mcps', 'skills', 'hooks', 'rules', 'agents')}
                   for vendor in ('claude', 'cursor', 'codex')},
        'mcp_servers': {'ap-vibe': {**mcp, 'enabled': True}}}


def command(value, profile):
    args = [*value['executor_command'], '--model', profile['model'], '--prompt-file', 'AP-VIBE-TASK.md',
        '--output-format', 'streaming-json', '--always-approve', '--no-subagents']
    args += ['--resume' if value.get('parent_run_id') else '--session-id', value['session_id']]
    return args


class GrokStream:
    def __init__(self, emit, observe):
        self.emit, self.observe = emit, observe
        self.complete = False
        self.error = None
        self.buffer = ''
        self.turns = set()
        self.usage_events = 0

    def flush(self):
        if self.buffer:
            self.emit('assistant', {'text': self.buffer})
            self.buffer = ''

    def feed(self, row):
        # Grok 1.0.30 headless flattens ACP into type/text/tool/usage/end;
        # persisted updates.jsonl keeps the full session/update envelope.
        if isinstance(row, dict) and row.get('type'):
            kind = row['type']
            if kind == 'text':
                value = row.get('data') or row.get('text')
                if isinstance(value, str):
                    self.buffer += value
                    if len(self.buffer) >= 2000:
                        self.flush()
            elif kind in {'tool_call', 'tool_call_update'} and row.get('toolName'):
                self.flush()
                tool = str(row['toolName'])[:200]
                self.emit('tool', {'tool': tool, 'text': '调用工具：' + tool,
                    'activity': tool_activity(tool, row.get('rawInput'))})
            elif kind == 'usage' and isinstance(row.get('usage'), dict):
                self.usage_events += 1
                raw = {k:v for k,v in row['usage'].items() if k in {'input_tokens','output_tokens',
                    'cache_read_input_tokens','cache_creation_input_tokens','reasoning_tokens'} and type(v) in {int,float} and v>=0}
                self.emit('usage', {'native_usage': raw, 'source': 'grok_headless'})
                self.observe('usage-' + str(self.usage_events), {'prompt_tokens': raw.get('input_tokens',0),
                    'completion_tokens': raw.get('output_tokens',0),
                    'prompt_tokens_details': {'cached_tokens': raw.get('cache_read_input_tokens',0)}})
            elif kind == 'end':
                self.flush()
                reason = row.get('stopReason') or row.get('stop_reason')
                self.complete = reason in {'EndTurn', 'end_turn'}
                if not self.complete:
                    self.error = 'Grok 本轮停止：' + str(reason or 'unknown')[:100]
            elif kind == 'error':
                self.flush()
                self.error = str(row.get('message') or 'Grok native error')[:1500]
            return
        u = row if isinstance(row, dict) and row.get('sessionUpdate') else update(row)
        kind = u.get('sessionUpdate')
        if kind == 'agent_message_chunk':
            content = u.get('content', {})
            if isinstance(content, dict) and content.get('type') == 'text' and isinstance(content.get('text'), str):
                self.buffer += content['text']
                if len(self.buffer) >= 2000:
                    self.flush()
        elif kind in {'tool_call', 'tool_call_update'}:
            meta = u.get('_meta') or {}
            tool = (meta.get('x.ai/tool') or {}).get('name')
            if tool:
                self.flush()
                self.emit('tool', {'tool': str(tool)[:200], 'text': '调用工具：' + str(tool)[:200],
                    'activity': tool_activity(tool, u.get('rawInput'))})
        elif kind == 'retry_state':
            self.flush()
            self.emit('status', {'text': 'Grok 正在处理原生请求重试', 'attempt': u.get('attempt'),
                                'max_retries': u.get('max_retries')})
        elif kind == 'turn_completed':
            self.flush()
            self.complete = u.get('stop_reason') in {'end_turn', 'EndTurn'}
            if not self.complete:
                self.error = 'Grok 本轮停止：' + str(u.get('stop_reason') or 'unknown')[:100]
            usage = u.get('usage') or {}
            identity = u.get('prompt_id')
            if identity and identity not in self.turns and isinstance(usage, dict):
                self.turns.add(identity)
                def n(key):
                    v = usage.get(key)
                    return v if type(v) in {int, float} and v >= 0 else 0
                if usage:
                    self.observe(str(identity), {'prompt_tokens': n('inputTokens'),
                        'completion_tokens': n('outputTokens'),
                        'prompt_tokens_details': {'cached_tokens': n('cachedReadTokens')}})
