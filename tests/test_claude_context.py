import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from ap_mind.claude_context import prepare_plugin, observe_document_result
from tools import task_client
from tools import claude_context_hook


def test_plugin_has_complete_shared_contract_without_source_path_dependency(tmp_path):
    root=Path(__file__).resolve().parents[1]
    plugin=prepare_plugin(tmp_path,root)
    skill=plugin/'skills/project-context'
    assert json.loads((plugin/'.claude-plugin/plugin.json').read_text())['name']=='ap-vibe'
    assert (skill/'references/project-documents.md').read_bytes()==(root/'skills/ap-vibe-task-context/references/project-documents.md').read_bytes()
    assert 'references/project-documents.md' in (skill/'SKILL.md').read_text(encoding='utf-8')
    assert (skill/'references/agent-collaboration.md').read_bytes()==(root/'skills/ap-vibe-task-context/references/agent-collaboration.md').read_bytes()
    assert 'references/agent-collaboration.md' in (skill/'SKILL.md').read_text(encoding='utf-8')


def test_explicit_instance_never_falls_back_to_default_or_starts_another_service(tmp_path,monkeypatch):
    config=tmp_path/'isolated.json'
    config.write_text(json.dumps({'host':'127.0.0.1','port':12345,'auto_start':False}),encoding='utf-8')
    monkeypatch.setenv('AP_VIBE_CONFIG_PATH',str(config))
    monkeypatch.setattr(task_client,'_health',lambda *_:False)
    monkeypatch.setattr(task_client.subprocess,'Popen',lambda *_a,**_k: (_ for _ in ()).throw(AssertionError('must not launch')))
    value=task_client._start_daemon(task_client._installed_config())
    assert value['url']=='http://127.0.0.1:12345/' and not value['started']
    config.unlink()
    try:task_client._installed_config()
    except FileNotFoundError:pass
    else:raise AssertionError('missing explicit config must not load production config')


def test_cache_separates_runtime_and_explicit_instance(tmp_path,monkeypatch):
    monkeypatch.delenv('AP_VIBE_CONFIG_PATH',raising=False)
    codex=task_client.cache_path(str(tmp_path),'same','codex')
    claude=task_client.cache_path(str(tmp_path),'same','claude')
    monkeypatch.setenv('AP_VIBE_CONFIG_PATH',str(tmp_path/'isolated.json'))
    isolated=task_client.cache_path(str(tmp_path),'same','claude')
    assert len({codex,claude,isolated})==3


def test_native_hook_passes_real_identity_and_small_catalog(monkeypatch):
    calls=[]
    def call(route,payload):
        calls.append((route,payload))
        return {'ok':True,'receipt_id':'r','project_id':'p','session_id':payload['session_id'],'created_at':'now',
                'knowledge_manifest':{'revision':3,'catalog':[{'key':'identity'},{'key':'risks'}]},
                'service':{'url':'http://127.0.0.1:9876/','started':True},'hook_context':'DO_NOT_FORWARD_CODEX_SPECIFIC_PROMPT'}
    monkeypatch.setattr(task_client,'call',call)
    monkeypatch.setattr(task_client,'save_receipt',lambda *args:None)
    result=claude_context_hook.handle({'hook_event_name':'UserPromptSubmit','session_id':'real-claude','cwd':'real-directory','prompt':'actual goal'})
    assert calls[0][1]['session_id']=='real-claude' and calls[0][1]['cwd']=='real-directory'
    context=result['hookSpecificOutput']['additionalContext']
    assert result['hookSpecificOutput']['hookEventName']=='UserPromptSubmit'
    assert '9876' in context and 'identity,risks' in context
    assert 'DO_NOT_FORWARD_CODEX_SPECIFIC_PROMPT' not in context
    assert claude_context_hook.handle({'hook_event_name':'SessionStart'})=={}


def test_native_stop_never_blocks_simple_question_or_repeats(tmp_path,monkeypatch):
    transcript=tmp_path/'session.jsonl'
    record={'type':'user','message':{'content':'one-off question'}}
    transcript.write_text(json.dumps(record)+'\n',encoding='utf-8')
    event={'hook_event_name':'Stop','session_id':'s','cwd':str(tmp_path),'transcript_path':str(transcript)}
    assert claude_context_hook.handle(event)=={}

    with transcript.open('a',encoding='utf-8') as stream:stream.write(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Write'}]}})+'\n')
    target=tmp_path/'receipt.json';target.write_text(json.dumps({'receipt_id':'r'}),encoding='utf-8')
    monkeypatch.setattr(task_client,'cache_path',lambda *args:target)
    assert claude_context_hook.handle(event)['decision']=='block'
    assert claude_context_hook.handle({**event,'stop_hook_active':True})=={}
    target.with_suffix('.maintained.json').write_text(json.dumps({'receipt_id':'r'}),encoding='utf-8')
    assert claude_context_hook.handle(event)=={}


def test_dossier_readback_requires_actual_matching_revision_and_all_changed_chapters():
    state={}
    def event(name,**value):
        return observe_document_result('mcp__ap-vibe__ap_vibe_'+name,{'content':[{'type':'text','text':json.dumps({'ok':True,'project_id':'p',**value})}]},state,'p')
    assert event('update',revision=7,updated_sections=['work','evidence'])
    assert not event('read',revision=6,sections={'work':{},'evidence':{}})
    assert not event('read',revision=7,sections={'work':{}})
    assert event('read',revision=7,sections={'evidence':{}})
    assert state['readback_complete']
    assert not event('read',revision=7,sections={'evidence':{}})
    assert event('update',revision=8,updated_sections=['work'])
    assert not state['readback_complete']
