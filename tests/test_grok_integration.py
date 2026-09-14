import json
from pathlib import Path
import tomllib

import pytest

from ap_mind.external_sessions import ExternalSessions
from ap_mind.grok_sessions import GrokFiles
from ap_mind.grok_runner import GrokStream, command, settings
from ap_mind.native_toml import dumps
from tools.install_grok import install_home, merge


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


def desktop(tmp_path):
    root = tmp_path / 'desktop'
    write(root/'sessions_index.json', [{'id':'ui-id','agentSessionId':'native-id','title':'Client, not model',
        'modelId':'custom-model','updatedAt':'2026-09-14T00:00:00Z'}])
    write(root/'sessions/ui-id/messages.json', [
        {'id':'a','role':'user','content':'Continue this project'},
        {'id':'b','role':'assistant','content':'public result','thought':'HIDDEN_THOUGHT'},
        {'id':'c','role':'tool','content':'HIDDEN_TOOL'},
        {'id':'d','role':'assistant','content':'failed visibly','isError':True}])
    write(root/'agent-home/sessions/encoded/native-id/summary.json', {'info':{'id':'native-id','cwd':str(tmp_path/'project')}})
    return root


def test_desktop_separate_identity_model_privacy_corrupt_replacement_and_cursors(tmp_path):
    root = desktop(tmp_path)
    source = ExternalSessions(tmp_path/'vibe', roots={'grok':[str(root)]}, cache_seconds=0)
    found = source.discover()['sources']
    assert len(found) == 1
    item = found[0]
    assert item['session_id']=='ui-id' and item['agent_session_id']=='native-id'
    assert item['harness']=='grok' and item['model']=='custom-model'
    last = source.read(item['source_id'], limit=1)
    assert last['events'][0]['error'] and last['has_older']
    first = source.read(item['source_id'], before=last['history_before'])
    assert [e['id'] for e in first['events']]==['a','b']
    assert 'HIDDEN' not in json.dumps(first)
    path = root/'sessions/ui-id/messages.json'
    path.write_text('[',encoding='utf8')
    assert source.read(item['source_id'])['stale']
    write(path,[{'role':'assistant','content':'replacement'}])
    changed=source.read(item['source_id'],after=last['cursor'],expected_generation=last['generation'])
    assert changed['reset'] and changed['events'][0]['text']=='replacement'
    with pytest.raises(Exception): source.read('grok-forged')


def test_native_chunks_partial_line_and_tool_activity(tmp_path):
    p=tmp_path/'sessions/encoded/native/updates.jsonl'
    p.parent.mkdir(parents=True)
    write(p.parent/'summary.json',{'info':{'id':'native','cwd':str(tmp_path)},'current_model_id':'test-model'})
    def row(kind,**kw):return {'timestamp':1789344000,'params':{'update':{'sessionUpdate':kind,**kw}}}
    rows=[row('user_message_chunk',content={'type':'text','text':'hello'}),
          row('agent_thought_chunk',content={'type':'text','text':'HIDDEN'}),
          row('agent_message_chunk',content={'type':'text','text':'good '}),
          row('agent_message_chunk',content={'type':'text','text':'answer'}),
          row('tool_call',_meta={'x.ai/tool':{'name':'read_file'}},rawInput={'path':'SECRET'}),
          row('turn_completed',stop_reason='end_turn')]
    p.write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf8')
    reader=GrokFiles()
    value=reader.snapshot(p)
    assert value['partial'] and [e['text'] for e in value['events']]==['hello','good answer']
    assert reader.observation(p)['state']=='running'
    with p.open('a',encoding='utf8') as f:f.write('\n')
    assert reader.observation(p)['state']=='idle' and reader.observation(p)['activity']=='read'
    assert 'HIDDEN' not in json.dumps(value) and 'SECRET' not in json.dumps(value)


def test_paths_cannot_escape_desktop_root(tmp_path):
    root=desktop(tmp_path)
    write(root/'sessions_index.json',[{'id':'../../outside'}])
    assert GrokFiles().paths(root)==[]


def test_desktop_default_model_uses_exact_native_session_model_id(tmp_path):
    root=desktop(tmp_path)
    write(root/'sessions_index.json',[{'id':'ui-id','agentSessionId':'native-id','modelId':None}])
    write(root/'agent-home/sessions/encoded/native-id/summary.json',
        {'info':{'id':'native-id','cwd':str(tmp_path)},'current_model_id':'relay-alias'})
    source=ExternalSessions(tmp_path/'vibe',roots={'grok':[str(root)]},cache_seconds=0)
    row=source.discover()['sources'][0]
    assert row['model']=='relay-alias' and row['model_source']=='native_session_model_id'
    assert source.read(row['source_id'])['model']=='relay-alias'


def test_install_preserves_all_other_settings_and_repeats(tmp_path):
    root=Path(__file__).resolve().parents[1]
    home=tmp_path/'client';home.mkdir()
    prior='# user notes\n[model.relay]\napi_key="private-test"\nmodel="mine"\n[mcp_servers.media]\ncommand="keep"\n'
    config=home/'config.toml';config.write_text(prior,encoding='utf8')
    installed=install_home(home,root,tmp_path/'vibe/config.json')
    assert installed['ok'] and installed['mcp_auto']
    after=config.read_bytes();parsed=tomllib.loads(after.decode())
    parsed['mcp_servers'].pop('ap-vibe')
    assert parsed==tomllib.loads(prior) and '# user notes' in after.decode()
    again=install_home(home,root,tmp_path/'vibe/config.json')
    assert again['mcp']['status']=='unchanged' and after==config.read_bytes()
    assert 'private-test' not in json.dumps(installed)
    user_skill=home/'skills/ap-vibe-native-context/SKILL.md'
    user_skill.write_text('User customization',encoding='utf8')
    assert install_home(home,root,tmp_path/'vibe/config.json')['skill']['skipped']
    assert user_skill.read_text()=='User customization'


def test_custom_hook_is_preserved_and_install_reports_partial(tmp_path):
    root=Path(__file__).resolve().parents[1]
    home=tmp_path/'client'
    first=install_home(home,root,tmp_path/'vibe/config.json')
    assert first['ok']
    hook=home/'hooks/ap-vibe.json'
    hook.write_text('{"user_customized":true}',encoding='utf8')
    result=install_home(home,root,tmp_path/'vibe/config.json')
    assert not result['ok'] and result['mcp_auto'] and not result['lifecycle']['ok']
    assert json.loads(hook.read_text())=={'user_customized':True}


def test_unmanaged_server_and_malformed_marker_never_overwrite(tmp_path):
    with pytest.raises(ValueError):merge('[mcp_servers.ap-vibe]\ncommand="user"\n',dumps({'mcp_servers':{'ap-vibe':{'command':'new'}}}))
    with pytest.raises(ValueError):merge('# AP-VIBE-GROK-MCP BEGIN\n',dumps({'mcp_servers':{'ap-vibe':{'command':'new'}}}))


def test_desktop_only_discovery_and_dual_home_upgrade_preserve_configuration(tmp_path, monkeypatch):
    from tools.install_harness import install
    import shutil
    root = Path(__file__).resolve().parents[1]
    cli = tmp_path / 'cli'
    desktop_data = tmp_path / 'desktop'
    desktop_agent = desktop_data / 'agent-home'
    desktop_agent.mkdir(parents=True)
    monkeypatch.setenv('GROK_HOME', str(cli))
    monkeypatch.setenv('GROK_DESKTOP_DATA_DIR', str(desktop_data))
    original = 'model = "my-model"\n[mcp_servers.media]\ncommand = "user-media"\n'
    (desktop_agent / 'config.toml').write_text(original, encoding='utf8')
    (desktop_agent / 'rules').mkdir()
    (desktop_agent / 'rules/user.md').write_text('Keep my preferences', encoding='utf8')
    config = tmp_path / 'vibe/config.json'
    first = install(['grok'], config_path=config, root=root)
    assert first['ok'] and not first['results'][0].get('skipped')
    assert not cli.exists()
    assert first['results'][0]['home'] == str(desktop_agent)
    cli.mkdir()
    (cli / 'config.toml').write_text(original, encoding='utf8')
    assert install(['grok'], config_path=config, root=root)['ok']
    new_root = tmp_path / 'version-next'
    shutil.copytree(root / 'skills', new_root / 'skills')
    # Model an update using a new version directory and the same installation.
    result = install(['grok'], config_path=config, root=new_root)
    assert result['ok'] and len(result['results'][0]['homes']) == 2
    for home in (cli, desktop_agent):
        data = tomllib.loads((home / 'config.toml').read_text(encoding='utf8'))
        assert data['model'] == 'my-model'
        assert data['mcp_servers']['media'] == {'command': 'user-media'}
        assert str(new_root).replace('\\', '/') in data['mcp_servers']['ap-vibe']['args'][0].replace('\\', '/')
        installed = json.loads((home / 'skills/ap-vibe-native-context/references/installation.json').read_text())
        assert installed['product_root'] == str(new_root.resolve())
        hook = (home / 'hooks/ap-vibe.json').read_text(encoding='utf8')
        assert new_root.as_posix() in hook
    assert (desktop_agent / 'rules/user.md').read_text() == 'Keep my preferences'


def test_stream_public_completion_usage_deduplication_and_resume():
    events, usages=[],[]
    stream=GrokStream(lambda k,v:events.append((k,v)),lambda k,v:usages.append((k,v)))
    stream.feed({'sessionUpdate':'agent_thought_chunk','content':{'type':'text','text':'HIDDEN'}})
    stream.feed({'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'OK'}})
    assert not stream.complete
    complete={'sessionUpdate':'turn_completed','prompt_id':'turn-1','stop_reason':'end_turn',
        'usage':{'inputTokens':100,'outputTokens':10,'cachedReadTokens':20}}
    stream.feed(complete);stream.feed(complete)
    assert stream.complete and events==[('assistant',{'text':'OK'})] and len(usages)==1
    assert usages[0][1]['prompt_tokens_details']['cached_tokens']==20
    stream.feed({**complete,'stop_reason':'error'})
    assert not stream.complete and stream.error
    p={'model':'configured','base_url':'https://provider.test/v1'}
    cfg=settings(p,{'command':'python','args':['mcp.py'],'env':{}})
    assert tomllib.loads(dumps(cfg))==cfg and cfg['model']['configured']['env_key']=='AP_VIBE_NATIVE_KEY'
    value={'executor_command':['grok'],'session_id':'native','parent_run_id':'previous'}
    args=command(value,p)
    assert '--resume' in args and '--session-id' not in args and not any('max-turn' in x for x in args)


def test_real_headless_flattened_protocol_has_public_tools_and_final_marker():
    events,usages=[],[]
    stream=GrokStream(lambda k,v:events.append((k,v)),lambda k,v:usages.append((k,v)))
    stream.feed({'type':'thought','data':'HIDDEN'})
    stream.feed({'type':'text','data':'public'})
    stream.feed({'type':'tool_call','toolName':'read_file','rawInput':{'path':'PRIVATE'},'title':'PRIVATE'})
    stream.feed({'type':'usage','usage':{'input_tokens':10,'output_tokens':3,'cache_read_input_tokens':2,'private':'HIDDEN'}})
    stream.feed({'type':'end','stopReason':'end_turn','sessionId':'native','usage':{'input_tokens':10}})
    assert stream.complete and not stream.error and len(usages)==1
    assert 'HIDDEN' not in json.dumps(events) and 'PRIVATE' not in json.dumps(events)
    assert any(v.get('activity')=='read' for k,v in events)


def test_desktop_delivery_idempotency_and_unknown_never_replays(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from ap_mind import grok_messages as gm
    external=ExternalSessions(tmp_path/'vibe',roots={'grok':[str(desktop(tmp_path))]},cache_seconds=0)
    service=SimpleNamespace(data_dir=tmp_path/'vibe',external_sessions=external)
    sender=gm.GrokMessages(service)
    monkeypatch.setattr(gm.GrokMessages,'busy',lambda self,sid:False)
    monkeypatch.setattr(gm,'connection',lambda:('http://127.0.0.1:1234','secret'))
    calls=[]
    def send(s,m,r):
        calls.append(r);return {'ok':True,'status':'queued','sessionId':s,'idempotencyKey':r}
    monkeypatch.setattr(gm,'send_native',send)
    args={'session_id':'ui-id','message':'next task','request_id':'one'}
    assert sender.enqueue(args)['delivery']['status']=='submitted'
    assert sender.enqueue(args)['replayed'] and calls==['one']
    with pytest.raises(Exception):sender.enqueue({**args,'message':'different'})
    def lost(*args):calls.append('lost');raise TimeoutError()
    monkeypatch.setattr(gm,'send_native',lost)
    assert sender.enqueue({**args,'request_id':'two'})['delivery']['status']=='uncertain'
    recovered=gm.GrokMessages(service)
    assert recovered.enqueue({**args,'request_id':'two'})['replayed'] and calls.count('lost')==1


def test_desktop_token_only_goes_to_exact_loopback(tmp_path):
    from ap_mind.grok_messages import connection
    for url in ('https://example.com','http://127.0.0.1:80@evil.test','http://localhost:1234','http://127.0.0.1:1234/path'):
        write(tmp_path/'session-api.json',{'url':url,'token':'secret'})
        with pytest.raises(Exception):connection(tmp_path)


def test_desktop_message_http_route_reaches_native_once_and_reads_receipt(tmp_path,monkeypatch):
    import threading
    from urllib.request import Request,urlopen
    from ap_mind.studio_server import create_server
    from ap_mind import grok_messages as gm
    root=tmp_path/'project';root.mkdir();(tmp_path/'empty').mkdir()
    server=create_server(host='127.0.0.1',port=0,data_dir=tmp_path/'data',
        project_root=root,codex_project_id='grok-http-test',codex_sessions_root=tmp_path/'empty')
    monkeypatch.setattr(server.service.grok_messages,'source',lambda sid:{'session_id':sid})
    monkeypatch.setattr(server.service.grok_messages,'busy',lambda sid:False)
    monkeypatch.setattr(gm,'connection',lambda:('http://127.0.0.1:1234','fixture-only'))
    calls=[]
    def send(s,m,r):
        calls.append(r);return {'ok':True,'status':'turn_started','sessionId':s,'idempotencyKey':r}
    monkeypatch.setattr(gm,'send_native',send)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    url=f'http://127.0.0.1:{server.server_port}/v1/ap-vibe/grok/messages'
    body={'session_id':'desktop','request_id':'http-message-1','message':'test'}
    try:
        def post():
            request=Request(url,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
            with urlopen(request,timeout=5) as response:return json.load(response)
        assert post()['delivery']['status']=='submitted'
        assert post()['replayed'] and calls==['http-message-1']
        with urlopen(url+'?session_id=desktop',timeout=5) as response:readback=json.load(response)
        assert readback['deliveries'][0]['request_id']==body['request_id']
    finally:
        server.shutdown();worker.join(timeout=5);server.server_close()


def test_busy_desktop_queues_locally_survives_restart_and_dispatches_once(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from ap_mind import grok_messages as gm
    service=SimpleNamespace(data_dir=tmp_path)
    busy=[True];calls=[]
    monkeypatch.setattr(gm.GrokMessages,'busy',lambda self,sid:busy[0])
    monkeypatch.setattr(gm.GrokMessages,'_ensure_worker',lambda self:None)
    monkeypatch.setattr(gm,'connection',lambda:('http://127.0.0.1:1234','fixture'))
    def send(s,m,r):
        calls.append(r);return {'ok':True,'status':'turn_started','sessionId':s,'idempotencyKey':r}
    monkeypatch.setattr(gm,'send_native',send)
    body={'session_id':'desktop','request_id':'busy-1','message':'next'}
    sender=gm.GrokMessages(service)
    assert sender.enqueue(body)['delivery']['status']=='queued' and not calls
    sender=gm.GrokMessages(service);sender._drain_once()
    assert not calls and sender.enqueue(body)['replayed']
    busy[0]=False;sender._drain_once();sender._drain_once()
    assert calls==['busy-1'] and sender.read_delivery('desktop','busy-1')['status']=='submitted'
    with pytest.raises(Exception):sender.cancel(body)
    busy[0]=True
    second={**body,'request_id':'busy-2'}
    sender.enqueue(second);assert sender.cancel(second)['delivery']['status']=='cancelled'
    busy[0]=False;sender._drain_once()
    assert calls==['busy-1'] and sender.enqueue(second)['delivery']['status']=='cancelled'


def test_grok_transport_normalizes_null_chunks_preserves_tools_and_counts_once():
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from urllib.request import Request,urlopen
    import threading
    from ap_mind.grok_transport import GrokTransport
    calls=[];usages=[]
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.headers['Authorization'],body))
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            for frame in [{'choices':None,'usage':None},
                          {'choices':[{'delta':{'tool_calls':[{'function':{'name':'read_file','arguments':'{}'}}]}}]},
                          {'choices':None,'usage':{'prompt_tokens':8,'completion_tokens':2}}]:
                self.wfile.write(('data: '+json.dumps(frame)+'\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
    upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    proxy=GrokTransport({'model':'m','base_url':f'http://127.0.0.1:{upstream.server_port}/v1'},'upstream-secret',lambda k,u:usages.append(u)).start()
    try:
        req=Request(proxy.url+'/chat/completions',data=b'{"model":"m","stream":true}',headers={'Authorization':'Bearer '+proxy.token,'Content-Type':'application/json'})
        data=urlopen(req,timeout=4).read().decode()
        frames=[json.loads(line[6:]) for line in data.splitlines() if line.startswith('data: ') and '[DONE]' not in line]
        assert frames[0]['choices']==[] and frames[-1]['choices']==[]
        assert frames[1]['choices'][0]['delta']['tool_calls'][0]['function']['name']=='read_file'
        assert len(calls)==1 and calls[0][0]=='Bearer upstream-secret'
        assert usages==[{'prompt_tokens':8,'completion_tokens':2}]
        assert proxy.normalized_chunks==2 and '[DONE]' in data
    finally:proxy.close();upstream.shutdown();upstream.server_close()
