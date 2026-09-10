import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.studio_extensions import extension_ids, prepare_extensions
from ap_mind.contracts import ContractError
from ap_mind.codex_runner import command
from test_agent_studio import studio, profile
from ap_mind import agent_studio


def installed(tmp_path):
    source = tmp_path / 'installed plugin with spaces'
    for path in ['skills/codex-yinzi-universal-video/SKILL.md', 'scripts/runtime-state.mjs', 'mcp/server.mjs']:
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('fixture source', encoding='utf-8')
    (source / '.mcp.json').write_text('DO_NOT_COPY_USER_CONFIGURATION')
    registry = tmp_path / 'runtime.json'
    registry.write_text(json.dumps({'api_base': 'http://127.0.0.1:24681'}))
    return {'AP_VIBE_MEDIA_PLUGIN_ROOT': str(source), 'AP_VIBE_MEDIA_RUNTIME_PATH': str(registry), 'AP_VIBE_NODE': '/fixture/node'}


def test_explicit_extension_is_separate_from_default_and_preserves_runtime_binding(tmp_path):
    env = installed(tmp_path)
    target = tmp_path / 'run'
    assert prepare_extensions([], target, env)['mcp'] == {}
    assert not target.exists()
    value = prepare_extensions(['yinzi-media'], target, env)
    server = value['mcp']['yinzi-media']
    assert server['env'] == {'YINZI_WORKFLOW_URL': 'http://127.0.0.1:24681'}
    assert Path(server['args'][0]).is_file()
    assert server['args'][server['args'].index('--node')+1] == '/fixture/node'
    plugin = Path(value['plugins'][0])
    assert not (plugin / '.mcp.json').exists()
    assert (plugin / 'scripts/runtime-state.mjs').is_file()
    assert json.loads((plugin / '.claude-plugin/plugin.json').read_text())['name'] == 'yinzi-media'
    assert 'DO_NOT_COPY_USER_CONFIGURATION' not in json.dumps(value)


def test_missing_or_remote_registry_does_not_block_context_or_forward_remote_base(tmp_path):
    env = installed(tmp_path)
    Path(env['AP_VIBE_MEDIA_RUNTIME_PATH']).write_text(json.dumps({'api_base': 'https://example.invalid'}))
    assert prepare_extensions(['yinzi-media'], tmp_path/'run', env)['env'] == {}
    env['AP_VIBE_MEDIA_PLUGIN_ROOT'] = str(tmp_path/'missing')
    result = prepare_extensions(['yinzi-media'], tmp_path/'missing-run', env)
    assert result['mcp'] == {} and not result['status'][0]['available']
    assert result['instructions']
    with pytest.raises(ContractError):
        extension_ids({'extensions': 'yinzi-media'})


def test_run_freezes_extensions_in_idempotency_and_inherits_on_resume(studio, monkeypatch):
    monkeypatch.setattr(agent_studio, 'claude_executable', lambda: '/fixture/claude')
    monkeypatch.setattr(studio, '_execute', lambda *_: None)
    agent = profile(studio)
    raw = {'request_id': 'media-run', 'agent_id': agent['agent_id'], 'project_id': 'test-project',
           'prompt': 'Process local image', 'extensions': ['yinzi-media']}
    result = studio.start(raw)
    run_id = result['run_id']
    assert studio.start(raw)['run_id'] == run_id
    with pytest.raises(ContractError, match='request_conflict'):
        studio.start({**raw, 'extensions': []})
    studio._state(run_id, 'awaiting_review')
    resumed = studio.start({k: v for k, v in {**raw, 'request_id':'media-resume',
        'continue_run_id':run_id}.items() if k != 'extensions'})
    assert studio.runs(resumed['run_id'])['runs'][0]['extensions'] == ['yinzi-media']


def test_task_preserves_extension_when_old_client_omits_it(studio):
    raw = {'request_id': 'media-task', 'project_id':'test-project', 'title':'Image task',
           'goal':'Produce image', 'acceptance':'Inspect image', 'extensions':['yinzi-media']}
    task = studio.tasks.save(raw)['task']
    next_raw = {k:v for k,v in raw.items() if k != 'extensions'}
    task = studio.tasks.save({**next_raw, 'request_id':'media-task-next',
        'task_id':task['task_id'], 'expected_version':task['version']})['task']
    assert task['extensions'] == ['yinzi-media']


def test_codex_mcp_mount_keeps_media_and_project_context():
    args = command({'executor_command':['codex']}, {'auth_mode':'local_login'}, {'command':'python'},
        'instructions', extension_mcp={'yinzi-media':{'command':'node','args':['C:/space path/server.mjs']}})
    mounted = next(value for value in args if value.startswith('mcp_servers='))
    assert 'ap-vibe' in mounted and 'yinzi-media' in mounted and 'space path' in mounted


def test_large_media_history_is_preserved_but_not_forced_into_context(tmp_path):
    from tools.media_mcp_proxy import project_session
    source = {'session':{'id':'s','status':'running','plan_revision':9,'plan':{'private_detail':'original'}},
        'nodes':[{'node_key':'a','id':'one','status':'failed','version':3,'decision':{'prompt':'long reference'}},
                 {'node_key':'b','id':'two','status':'succeeded','output_refs':['actual.png']}],
        'events':[{'text':'old event'}]*500, 'artifacts':[{'id':'actual.png'}]}
    raw = {'content':[{'type':'text','text':json.dumps(source)}]}
    result = json.loads(project_session(raw,tmp_path)['content'][0]['text'])
    assert result['session']['status'] == 'running' and result['nodes'][0]['status'] == 'failed'
    assert 'decision' not in result['nodes'][0] and 'events' not in result
    assert json.loads(Path(result['full_record']['path']).read_text()) == source
    selected = json.loads(project_session(raw,tmp_path,['a'])['content'][0]['text'])
    assert selected['nodes'][0]['decision'] == source['nodes'][0]['decision']
    assert project_session({'isError':True},tmp_path) == {'isError':True}
