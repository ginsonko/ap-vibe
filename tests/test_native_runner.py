import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.native_runner import OpenClawTail, command, settings


def test_native_completion_does_not_turn_progress_into_failure_or_hide_errors():
    from ap_mind.native_runner import completion_error
    assert completion_error(0, True, [], ['session_id: actual-session']) is None
    assert completion_error(0, True, ['upstream rejected request'], ['progress']) == 'upstream rejected request'
    assert completion_error(1, True, [], ['authentication failed']) == 'authentication failed'
    assert completion_error(0, False, [], [])
    assert '7' in completion_error(7, False, [], [])


def test_hermes_tail_native_history_does_not_replay_or_expose_private_fields(tmp_path):
    import sqlite3
    from ap_mind.hermes_runner import HermesTail
    path=tmp_path/'state.db'
    db=sqlite3.connect(path)
    db.executescript('CREATE TABLE sessions(id,source,started_at,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens);'
        'CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id,role,content,timestamp,active,_compressed_summary,display_kind,tool_name,reasoning);')
    db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?)',('native','cli',1,10,3,2,0))
    db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)',(1,'native','assistant','old',1,1,0,None,None,None));db.commit()
    events,identities=[],[]
    tail=HermesTail(tmp_path,lambda k,v:events.append((k,v)),identities.append)
    db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)',(2,'native','assistant','\x00json:'+json.dumps([
        {'type':'thinking','thinking':'PRIVATE'},{'type':'text','text':'new result'}]),2,1,0,None,None,'PRIVATE'))
    db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)',(3,'native','tool','PRIVATE_ARGUMENT',3,1,0,None,'ap_vibe_sessions',None))
    db.execute('UPDATE sessions SET input_tokens=30,output_tokens=8,cache_read_tokens=7')
    db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?)',('helper','subagent',4,0,0,0,0));db.commit()
    tail.flush();tail.flush()
    assert identities==['native','native']
    assert events==[('assistant',{'text':'new result'}),('tool',{'tool':'ap_vibe_sessions','text':'调用工具：ap_vibe_sessions'})]
    assert tail.completed_text and 'PRIVATE' not in json.dumps(events)
    assert tail.final_usage()=={'input_tokens':20,'output_tokens':5,'cache_read_tokens':5,'cache_write_tokens':0}
    db.close()


def test_hermes_native_configuration_and_same_session_resume(tmp_path):
    p={'name':'worker','model':'grok','base_url':'https://api.example/v1'}
    mcp={'command':'python','args':['mcp.py'],'env':{'AP_VIBE_CLIENT_KIND':'hermes'}}
    config=settings('hermes',p,tmp_path,tmp_path,mcp)
    assert config['providers']['apvibe']['key_env']=='AP_VIBE_NATIVE_KEY'
    assert config['terminal']['cwd']==str(tmp_path)
    assert config['agent']['max_turns']>1000000
    value={'executor_kind':'hermes','executor_command':['hermes'],'parent_run_id':'old','session_id':'native'}
    argv=command(value,p)
    assert argv[argv.index('--resume')+1]=='native'
    assert argv[argv.index('--query-file')+1]=='AP-VIBE-TASK.md'


def test_openclaw_final_flush_preserves_public_tools_and_usage_without_replaying_history(tmp_path):
    path = tmp_path / 'agents/main/sessions/s.jsonl'
    path.parent.mkdir(parents=True)
    old = {'type': 'message', 'id': 'old', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'old'}]}}
    path.write_text(json.dumps(old) + '\n', encoding='utf-8')
    events, usages = [], []
    tail = OpenClawTail(tmp_path, lambda kind, data: events.append((kind, data)), lambda identity, usage: usages.append((identity, usage)))
    message = {'type': 'message', 'id': 'new', 'message': {'role': 'assistant', 'content': [
        {'type': 'thinking', 'thinking': 'private reasoning'}, {'type': 'text', 'text': 'public result'},
        {'type': 'toolCall', 'name': 'read', 'arguments': {'secret': 'private argument'}}],
        'usage': {'input': 10, 'output': 3, 'cacheRead': 20, 'cacheWrite': 5, 'cost': {'total': 0}}}}
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(message))
    tail.flush()
    assert events == []  # An in-progress native record is not advanced past.
    with path.open('a', encoding='utf-8') as stream:
        stream.write('\n')
    tail.flush()  # Represents the flush after the child has exited.
    tail.flush()
    assert events == [('assistant', {'text': 'public result'}), ('tool', {'tool': 'read', 'text': '调用工具：read'})]
    assert len(usages) == 1
    assert usages[0][1] == {'input_tokens': 10, 'output_tokens': 3, 'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 5}
    assert 'private' not in json.dumps([events, usages])


def test_native_resume_uses_same_session_and_keeps_keys_out_of_config(tmp_path):
    profile = {'name': 'worker', 'model': 'configured-model', 'base_url': 'https://api.example/v1'}
    value = {'executor_kind': 'opencode', 'executor_command': ['opencode.exe'], 'parent_run_id': 'previous', 'session_id': 'native-id'}
    argv = command(value, profile)
    assert argv[argv.index('--session') + 1] == 'native-id'
    assert not any('max-turn' in x for x in argv)
    mcp = {'command': 'python', 'args': ['mcp.py'], 'env': {}}
    config = settings('opencode', profile, tmp_path, tmp_path, mcp)
    assert config['provider']['apvibe']['options']['apiKey'] == '{env:AP_VIBE_NATIVE_KEY}'
    assert config['mcp']['ap-vibe']['command'] == ['python', 'mcp.py']


def test_mimo_resume_uses_official_binary_and_own_protocol(tmp_path, monkeypatch):
    from ap_mind.harness_registry import executable
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr('ap_mind.harness_registry.shutil.which', lambda _: None)
    binary = tmp_path / ('AP-Vibe/runtimes/mimocode/mimo' + ('.exe' if os.name == 'nt' else ''))
    binary.parent.mkdir(parents=True)
    binary.touch()
    assert executable('mimocode') == [str(binary)]
    profile = {'name': 'MiMo worker', 'model': 'configured-model', 'base_url': 'https://api.example/v1'}
    argv = command({'executor_kind': 'mimocode', 'executor_command': [str(binary)], 'parent_run_id': 'old', 'session_id': 'ses-origin'}, profile)
    assert '--pure' in argv and argv[argv.index('--session') + 1] == 'ses-origin'
    config = settings('mimocode', profile, tmp_path, tmp_path, {'command': 'python', 'args': ['mcp.py'], 'env': {}})
    assert config['model'] == 'apvibe/configured-model'
    assert config['provider']['apvibe']['options']['apiKey'] == '{env:AP_VIBE_NATIVE_KEY}'
    assert config['mcp']['ap-vibe']['enabled'] is True


def test_native_state_has_stable_installation_namespace_and_preserves_existing_history(tmp_path, monkeypatch):
    from ap_mind.native_runner import native_state_path
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.delenv('AP_VIBE_NATIVE_STATE_HOME', raising=False)
    value = {'run_id': 'run-one'}
    directory = tmp_path / ('long-name-' * 12) / 'agent-studio/run-one'
    first = native_state_path(value, directory)
    assert first == native_state_path(value, directory)
    if os.name == 'nt':
        assert first.is_relative_to(tmp_path / 'AP-Vibe/native')
        assert first != native_state_path(value, tmp_path / 'another-install/agent-studio/run-one')
    first.mkdir(parents=True)
    (first / 'native-history').write_text('preserved')
    resumed = native_state_path({'run_id': 'run-two', 'native_state_dir': str(first)}, directory)
    assert (resumed / 'native-history').read_text() == 'preserved'
