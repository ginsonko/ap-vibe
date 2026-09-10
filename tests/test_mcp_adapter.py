import json
from pathlib import Path
import subprocess
import sys

from tools import ap_vibe_mcp
from tools.trust_hooks import own_hooks


def test_stdio_handshake_discovery_and_invalid_requests():
    messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26'}},
              {'jsonrpc':'2.0','method':'notifications/initialized'},
              {'jsonrpc':'2.0','id':2,'method':'tools/list'},
              {'jsonrpc':'2.0','id':3,'method':'unknown'},
              {'jsonrpc':'2.0','id':4,'method':'tools/call','params':[]}]
    run=subprocess.run([sys.executable,str(Path(ap_vibe_mcp.__file__))],input='\n'.join(json.dumps(m) for m in messages).encode(),capture_output=True,timeout=10)
    assert run.returncode==0
    results=[json.loads(l) for l in run.stdout.splitlines()]
    assert len(results)==4 and results[0]['result']['protocolVersion']=='2025-03-26'
    names = {tool['name'] for tool in results[1]['result']['tools']}
    assert {'ap_vibe_context', 'ap_vibe_update', 'ap_vibe_review_submit', 'ap_vibe_collaboration_send'} <= names
    assert len(names) == len(results[1]['result']['tools'])
    assert results[2]['error']['code']==-32601


def test_update_forwards_exact_request_identity_without_creating_new_state(monkeypatch):
    captured=[]
    monkeypatch.setattr(ap_vibe_mcp.task_client,'call',lambda r,p:captured.append((r,p)) or {'ok':True,'revision':8})
    args={'receipt_id':'r','session_id':'s','request_id':'stable','expected_revision':7,'sections':{'work':{'completed':['真实变化']}}}
    result=ap_vibe_mcp.invoke('ap_vibe_update',args)
    assert result['revision']==8 and captured==[('update',args)]

def test_update_file_is_scoped_and_forwards_saved_patch(tmp_path, monkeypatch):
    patch=tmp_path/'patch.json'; patch.write_text(json.dumps({'request_id':'stable','expected_revision':7,'sections':{'work':{'summary':'x'}}}),encoding='utf8')
    captured=[]; monkeypatch.setattr(ap_vibe_mcp.task_client,'call',lambda r,p:captured.append((r,p)) or {'ok':True,'revision':8})
    result=ap_vibe_mcp.invoke('ap_vibe_update_file',{'receipt_id':'r','session_id':'s','cwd':str(tmp_path),'file_path':str(patch)})
    assert result['revision']==8 and captured[0][0]=='update' and captured[0][1]['request_id']=='stable'
    outside=tmp_path.parent/'outside.json'; outside.write_text(patch.read_text(),encoding='utf8')
    try: ap_vibe_mcp.invoke('ap_vibe_update_file',{'receipt_id':'r','session_id':'s','cwd':str(tmp_path),'file_path':str(outside)})
    except ValueError as exc: assert 'cwd' in str(exc)
    else: assert False


def test_missing_goal_names_the_fix_without_guessing_context(monkeypatch):
    captured = []
    monkeypatch.setattr(ap_vibe_mcp.task_client, 'call', lambda *args: captured.append(args))
    result = ap_vibe_mcp.handle({'id': 1, 'method': 'tools/call', 'params': {
        'name': 'ap_vibe_context', 'arguments': {'cwd': 'example', 'session_id': 'session'}}})
    assert result['result']['isError']
    assert 'goal' in result['result']['content'][0]['text']
    assert not captured


def test_native_runtime_and_executor_selection_are_not_model_arguments(monkeypatch):
    calls=[]
    monkeypatch.setenv('AP_VIBE_CLIENT_KIND','claude')
    monkeypatch.setenv('AP_VIBE_SELECTED_PROJECT_ID','selected-by-executor')
    monkeypatch.setattr(ap_vibe_mcp.task_client,'call',lambda route,body:calls.append((route,body)) or {'ok':False})
    ap_vibe_mcp.invoke('ap_vibe_context',{'cwd':'real-root','session_id':'actual','goal':'Task'})
    assert calls[0][1]['client_kind']=='claude'
    assert calls[0][1]['selected_project_id']=='selected-by-executor'
    ap_vibe_mcp.invoke('ap_vibe_projects',{})
    assert calls[1]==('projects',{})


def test_trust_filter_cannot_adopt_neighboring_or_modified_commands(tmp_path):
    source=tmp_path/'hooks.json'
    expected='python exact-ap-vibe-task-client hook'
    base={'handlerType':'command','sourcePath':str(source),'command':expected}
    groups=[{'hooks':[base,{**base,'sourcePath':str(tmp_path/'other.json')},{**base,'command':'evil; '+expected}]}]
    assert own_hooks(groups,source,{expected})==[base]


def test_collaboration_tools_reach_real_http_service(tmp_path, monkeypatch):
    import threading
    from ap_mind.studio_server import create_server
    root=tmp_path/'project';root.mkdir()
    server=create_server(port=0,data_dir=tmp_path/'data',project_root=root)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setattr(ap_vibe_mcp.task_client,'_installed_config',lambda:{'host':'127.0.0.1','port':server.server_address[1],'auto_start':False})
    try:
        body={'request_id':'message-real-route','sender':'one','recipient':'two','body':'x'*300,'task_id':'run-fixture'}
        sent=ap_vibe_mcp.invoke('ap_vibe_collaboration_send',body)
        assert sent['ok'] and ap_vibe_mcp.invoke('ap_vibe_collaboration_send',body)['replayed']
        result=ap_vibe_mcp.invoke('ap_vibe_collaboration_list',{})
        assert result['messages'][0]['message_id']==sent['message_id']
        assert 'state_help' in result and 'tasks' in result
        assert ap_vibe_mcp.invoke('ap_vibe_collaboration_list',{'run_id':'missing'})['task']['runs']==[]
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def test_appearance_files_import_and_read_back_over_real_http(tmp_path, monkeypatch):
    import threading
    from ap_mind.studio_server import create_server
    root=tmp_path/'project';root.mkdir()
    server=create_server(port=0,data_dir=tmp_path/'data',project_root=root)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setattr(ap_vibe_mcp.task_client,'_installed_config',lambda:{'host':'127.0.0.1','port':server.server_address[1],'auto_start':False})
    try:
        source=Path(__file__).resolve().parents[1]/'assets/characters/deepseek-v3'
        args={'png_path':str(source/'deepseek-atlas.png'),'manifest_path':str(source/'manifest.json')}
        first=ap_vibe_mcp.invoke('ap_vibe_appearance_import',args)['appearances'][0]
        second=ap_vibe_mcp.invoke('ap_vibe_appearance_import',args)['appearances'][0]
        assert first['appearance_id']==second['appearance_id']
        read=ap_vibe_mcp.invoke('ap_vibe_appearances',{'appearance_id':first['appearance_id']})
        assert read['appearances'][0]['sha256']==first['sha256']
        assert 'png_base64' not in json.dumps(read)
        assert len(ap_vibe_mcp.invoke('ap_vibe_appearances',{})['appearances'])==1
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
