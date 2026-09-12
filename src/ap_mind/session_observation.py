"""Bounded public lifecycle projection. Never return arguments or thinking."""
import json
from pathlib import Path

from .contracts import ContractError
from .studio_activity import tool_activity


def inspect(path, harness, session_id):
    path = Path(path)
    result = {'state': 'unknown', 'status_basis': 'no_lifecycle_marker',
              'event_at': None, 'tool': None, 'first_message': None, 'auxiliary': False}
    with path.open('rb') as stream:
        head = stream.read(65536).splitlines()
        stream.seek(max(0, path.stat().st_size - 524288))
        if stream.tell():
            stream.readline()
        tail = stream.readlines()
    if harness == 'codex':
        try:
            meta = json.loads(head[0])
            if meta.get('type') != 'session_meta' or meta.get('payload', {}).get('id') != session_id:
                raise ContractError('message_source_identity_changed')
            origin = meta['payload'].get('source', {})
            origin = origin.get('subagent') if isinstance(origin, dict) else None
            result['auxiliary'] = origin in ('review', 'approval', 'memory_consolidation') if isinstance(origin, str) else False
        except (ValueError, IndexError):
            raise ContractError('message_source_identity_changed')
    # A missing saved title can fall back to actual public user speech.
    for line in head:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get('payload') or {}
        payload = payload if isinstance(payload, dict) else {}
        if harness == 'codex' and record.get('type') == 'event_msg' and payload.get('type') == 'user_message':
            message = payload.get('message')
            if isinstance(message, str) and message.strip():
                from .product import redact_portable
                result['first_message'] = redact_portable(message.strip().replace('\n', ' ')[:110])
                break
    # Select by lifecycle time, not append order: delayed failures must not
    # replace newer activity. At equal times cancellation/activity wins; other markers keep append order.
    from .studio_sessions import timestamp
    candidates = []
    for index, line in enumerate(tail):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get('payload') or {}
        payload = payload if isinstance(payload, dict) else {}
        kind = record.get('type')
        state, basis, explicit = None, None, False
        if harness == 'codex':
            if kind == 'response_item' and payload.get('type') in ('function_call', 'custom_tool_call'):
                result['tool'] = str(payload.get('name') or '')[:200]
                activity = tool_activity(result['tool'], payload.get('arguments') or payload.get('input'))
                if activity: result['activity'] = activity
                state, basis = 'running', 'tool_call'
            states = {'task_complete': 'idle', 'turn_aborted': 'cancelled',
                      'task_started': 'running', 'task_failed': 'failed', 'turn_failed': 'failed'}
            if kind == 'event_msg' and payload.get('type') in states:
                state, basis = states[payload['type']], payload['type']
                explicit = state == 'failed'
        else:
            if record.get('sessionId') and record['sessionId'] != session_id:
                continue
            message = record.get('message') or {}
            if not isinstance(message, dict):
                continue
            content = message.get('content') or []
            if isinstance(content, list):
                tool = next((str(b.get('name', ''))[:200] for b in reversed(content)
                             if isinstance(b, dict) and b.get('type') == 'tool_use'), None)
                if tool:
                    result['tool'] = tool
                    block = next(b for b in reversed(content) if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') == tool)
                    activity = tool_activity(tool, block.get('input'))
                    if activity: result['activity'] = activity
                    state, basis = 'running', 'claude_tool_use'
            if kind == 'assistant' and message.get('stop_reason') == 'end_turn':
                state, basis = 'idle', 'claude_end_turn'
            if kind == 'assistant' and record.get('isApiErrorMessage') is True:
                state, basis, explicit = 'failed', 'claude_api_error', True
            if kind == 'user' and not record.get('isMeta') and (isinstance(content, str) or
                    (isinstance(content, list) and any(isinstance(b, dict) and b.get('type')=='text' for b in content))):
                state, basis = 'running', 'claude_user_message'
            if kind == 'result':
                subtype = record.get('subtype')
                cancelled = (record.get('cancelled') is True or record.get('is_cancelled') is True or
                             subtype in {'cancelled', 'canceled', 'user_cancelled', 'interrupted'})
                if cancelled:
                    state, basis = 'cancelled', 'claude_cancelled'
                elif subtype in {'error_max_turns', 'error_max_budget_usd', 'error_max_structured_output_retries'}:
                    state, basis = 'unknown', 'claude_execution_limit'
                elif record.get('is_error') is True:
                    state, basis, explicit = 'failed', 'claude_error_during_execution', True
                elif record.get('is_error') is False or subtype == 'success':
                    state, basis = 'idle', 'claude_result'
                else:
                    state, basis = 'unknown', 'claude_result_unclassified'
        if state:
            stamp = record.get('timestamp')
            # Missing times can inform display but never qualify for recovery.
            at = timestamp(stamp)
            priority = {'failed': 0, 'running': 1, 'idle': 0, 'unknown': 2, 'cancelled': 3}[state]
            candidates.append(((at if at is not None else float('-inf'), priority, index),
                               {'state': state, 'status_basis': 'public_' + str(basis),
                                'event_at': stamp, 'explicit_failure': explicit,
                                'turn_id': payload.get('turn_id') or record.get('turn_id')}))
    if candidates:
        result.update(max(candidates, key=lambda pair: pair[0])[1])
    return result
