SYNTHETIC_KEY = "sk-" + "test" * 10

import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from ap_mind.claude_sessions import ClaudeSessions
from ap_mind.contracts import ContractError

def entry(i,text='visible',kind='user'):
    return {'type':kind,'uuid':str(i),'sessionId':'session-real','cwd':'C:/work/project',
            'timestamp':'2026-09-08T12:00:00Z','message':{'role':kind,'content':text}}
def write(path, values):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(b''.join((json.dumps(v)+'\n').encode() for v in values))
def setup(tmp_path,values,**kwargs):
    path=tmp_path/'projects'/'encoded'/'session-real.jsonl';write(path,values)
    monitor=ClaudeSessions([tmp_path/'projects'],cache_seconds=0,**kwargs)
    sid=monitor.discover()['sources'][0]['source_id']
    return monitor,path,sid

def test_public_blocks_and_explicit_title_only(tmp_path):
    records=[entry(0,'first prompt'),{'type':'custom-title','customTitle':'Real title'},
             entry(1,[{'type':'thinking','thinking':'HIDDEN_REASONING'},
                      {'type':'tool_use','name':'Read','input':{'private':'TOOL_INPUT'}},
                      {'type':'text','text':'Visible '+SYNTHETIC_KEY}],'assistant'),
             entry(2,[{'type':'tool_result','content':'SECRET_TOOL_CONTENT','is_error':False}]),
             {'type':'attachment','attachment':{'content':'ATTACHMENT_BODY'}},
             {**entry(3,'INJECTED_SKILL_BODY'), 'isMeta':True}]
    monitor,path,sid=setup(tmp_path,records)
    assert monitor.discover()['sources'][0]['title']=='Real title'
    output=json.dumps(monitor.read(sid))
    for hidden in ('HIDDEN_REASONING','TOOL_INPUT','SECRET_TOOL_CONTENT','ATTACHMENT_BODY','INJECTED_SKILL_BODY',SYNTHETIC_KEY):
        assert hidden not in output
    assert 'Visible' in output and 'tool_result' in output
    with pytest.raises(ContractError):monitor.read('../../anything')

def test_append_partial_and_replacement(tmp_path):
    monitor,path,sid=setup(tmp_path,[entry(0)])
    first=monitor.read(sid)
    partial=json.dumps(entry(1,'second')).encode()
    with path.open('ab') as f:f.write(partial[:30])
    waiting=monitor.read(sid,after=first['cursor'],generation=first['generation'])
    assert waiting['cursor']==first['cursor'] and not waiting['events']
    with path.open('ab') as f:f.write(partial[30:]+b'\n')
    update=monitor.read(sid,after=waiting['cursor'],generation=waiting['generation'])
    assert [e['text'] for e in update['events']]==['second']
    write(path,[entry(2,'new')])
    reset=monitor.read(sid,after=update['cursor'],generation=update['generation'])
    assert reset['reset'] and [e['text'] for e in reset['events']]==['new']


def test_cached_source_activity_ages_without_new_output(tmp_path,monkeypatch):
    monitor,path,sid=setup(tmp_path,[entry(0)])
    updated=path.stat().st_mtime
    monkeypatch.setattr('ap_mind.claude_sessions.time.time',lambda:updated+10)
    assert monitor.discover()['sources'][0]['activity']=='recent_output'
    monkeypatch.setattr('ap_mind.claude_sessions.time.time',lambda:updated+130)
    assert monitor.discover()['sources'][0]['activity']=='historical'


def test_model_comes_from_real_assistant_response_and_refreshes(tmp_path):
    first = entry(1, 'first answer', 'assistant')
    first['message']['model'] = 'provider/first-model'
    spoofed = entry(2, 'user supplied identity')
    spoofed['message']['model'] = 'not-a-response-model'
    injected = {**entry(3, 'injected', 'assistant'), 'isMeta': True}
    injected['message']['model'] = 'injected-model'
    synthetic = entry(4, 'synthetic interruption notice', 'assistant')
    synthetic['message']['model'] = '<synthetic>'
    monitor, path, sid = setup(tmp_path, [first, spoofed, injected, synthetic])
    source = monitor.discover()['sources'][0]
    assert source['model'] == 'provider/first-model'
    assert source['model_source'] == 'assistant_message'
    assert source['observed_models'] == ['provider/first-model']

    next_reply = entry(5, 'changed model answer', 'assistant')
    next_reply['message']['model'] = 'custom-next-model'
    with path.open('ab') as stream:
        stream.write((json.dumps(next_reply)+'\n').encode())
    updated = monitor.discover()['sources'][0]
    assert updated['model'] == 'custom-next-model'
    assert updated['observed_models'] == ['provider/first-model', 'custom-next-model']


def test_missing_model_stays_unknown_and_malformed_metadata_is_ignored(tmp_path):
    invalid = entry(2, 'malformed', 'assistant')
    invalid['message']['model'] = {'model': 'never stringify a whole object'}
    monitor, _, _ = setup(tmp_path, [entry(1, 'ordinary prompt'), invalid])
    source = monitor.discover()['sources'][0]
    assert source['model'] is None and source['model_source'] is None
    assert source['observed_models'] == []


def test_history_windows_do_not_drop_crossing_record(tmp_path):
    monitor,path,sid=setup(tmp_path,[entry(i,'x'*330+str(i)) for i in range(35)],window_bytes=4096)
    page=monitor.read(sid);ids={e['id'] for e in page['events']}
    for _ in range(15):
        if not page['has_older']:break
        page=monitor.read(sid,before=page['history_before'],generation=page['generation'])
        ids.update(e['id'] for e in page['events'])
    assert ids=={str(i)+':0' for i in range(35)}

def test_long_records_invalid_lines_and_missing_root(tmp_path):
    assert not ClaudeSessions([tmp_path/'missing']).discover()['sources']
    monitor,path,sid=setup(tmp_path,[entry(0,'x'*12000),entry(1,'tail')],window_bytes=4096)
    page=monitor.read(sid,after=0)
    assert page['cursor']>0 and page['invalid_lines']
    for _ in range(5):
        page=monitor.read(sid,after=page['cursor'],generation=page['generation'])
        if page['events']:break
    assert page['events'][-1]['text']=='tail'
    with path.open('ab') as f:f.write(b'bad json\n'+(json.dumps(entry(2,'after bad'))+'\n').encode())
    page=monitor.read(sid,after=page['cursor'],generation=page['generation'])
    assert page['invalid_lines']==1 and page['events'][-1]['text']=='after bad'
