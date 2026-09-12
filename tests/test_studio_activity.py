import json
from ap_mind.studio_activity import command_activity, tool_activity
from ap_mind.studio_presence import location, snapshot
from test_agent_studio import studio, profile


def test_actions_use_tool_semantics_and_keep_unknowns():
    for name in ('Read', 'read_file', 'functions.read_file', 'mcp__server__web_search', '读取文件'):
        assert tool_activity(name) == 'read'
    for name in ('Write', 'apply_patch', 'file_change', '编辑文件'):
        assert tool_activity(name) == 'write'
    assert tool_activity('Bash', {'command': 'pytest -q tests'}) == 'test'
    assert tool_activity('exec_command', {'cmd': 'Get-Content notes.md'}) == 'read'
    assert tool_activity('functions.exec', 'const r = await tools.exec_command({cmd:"rg -n pattern src"});') == 'read'
    assert command_activity('echo "pytest Read Write"') is None
    assert tool_activity('Bash', {'command': 'python unknown_script.py'}) is None
    assert location({'state':'running'}, {'kind':'tool','tool':'Read'}, review=True)[0] == 'library'
    assert location({'state':'running'}, {'kind':'tool','tool':'apply_patch'}, review=True)[0] == 'engineering'


def test_real_action_survives_transport_and_unknown_tool_events(studio):
    a=profile(studio)
    with studio.registry.transaction():
        c=studio.registry._connect()
        c.execute('INSERT INTO studio_runs VALUES (?,?,?,?,?,?)', ('spatial-run','spatial-request','fixture',a['agent_id'],'running',json.dumps({'agent_id':a['agent_id']})))
    studio._event('spatial-run','tool',{'tool':'Read'})
    studio._event('spatial-run','tool_result',{'text':'finished'})
    studio._event('spatial-run','provider',{'text':'requesting'})
    r=snapshot(studio)['runs'][0]
    assert r['room']=='library' and r['last_event']['kind']=='provider'
    studio._event('spatial-run','tool',{'tool':'unknown_custom_tool'})
    assert snapshot(studio)['runs'][0]['room']=='library'
    studio._event('spatial-run','tool',{'tool':'Bash','activity':'write'})
    studio._event('spatial-run','provider',{'text':'completed'})
    assert snapshot(studio)['runs'][0]['room']=='engineering'
    studio._event('spatial-run','tool_result',{'tool':'web_search','activity':'read'})
    assert snapshot(studio)['runs'][0]['room']=='library'
    studio._state('spatial-run','failed')
    assert snapshot(studio)['runs'][0]['room']=='waiting'


def test_ordinary_wrapper_projects_only_action_not_arguments(tmp_path):
    from ap_mind.session_observation import inspect
    path=tmp_path/'session.jsonl'
    records=[{'type':'session_meta','payload':{'id':'one'}},
             {'type':'response_item','timestamp':'2026-09-12T00:00:00Z','payload':{'type':'function_call','name':'functions.exec_command','arguments':json.dumps({'cmd':'Set-Content file.txt PRIVATE_ARGUMENT'})}},
             {'type':'response_item','timestamp':'2026-09-12T00:00:01Z','payload':{'type':'function_call','name':'unknown','arguments':'private'}}]
    path.write_text('\n'.join(map(json.dumps,records))+'\n')
    value=inspect(path,'codex','one')
    assert value['activity']=='write'
    assert 'PRIVATE_ARGUMENT' not in json.dumps(value)
