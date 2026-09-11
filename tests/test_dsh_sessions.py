import hashlib
import json
from pathlib import Path
import sys

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.dsh_sessions import DshFiles, log_paths
from ap_mind.external_sessions import ExternalSessions


def rows():
    return [
        {'type':'session','version':3,'id':'native','cwd':'C:/work','createdAt':1000},
        {'type':'model/selection','seq':1,'data':{'model':'actual-model'}},
        {'type':'user/message','seq':2,'time':2000,'data':{'id':'u','role':'user','source':{'kind':'user'},'content':[{'type':'text','text':'real request'}]}},
        {'type':'user/message','seq':3,'data':{'source':{'kind':'plugin'},'content':[{'type':'text','text':'PRIVATE_SYSTEM'}]}},
        {'type':'assistant/message','seq':4,'time':3000,'surfaceOp':'append','data':{'message':{'id':'a','role':'assistant','source':{'kind':'model','model':'actual-model','replayState':{'secret':'PRIVATE_REPLAY'}},'content':[{'type':'reasoning','text':'PRIVATE_REASONING'},{'type':'text','text':'public answer'},{'type':'tool-call','arguments':'PRIVATE_TOOL'}]},'stream':[{'text':'PRIVATE_STREAM'}]}},
        {'type':'session/title','seq':5,'data':{'title':'real title'}},
    ]


def write_rows(path, items, compressed=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks=[(json.dumps(r)+'\n').encode() for r in items]
    if compressed:
        z=pytest.importorskip('zstandard')
        blocks=[z.ZstdCompressor().compress(b) for b in blocks]
    path.write_bytes(b''.join(blocks))


@pytest.mark.parametrize('compressed', [False, True])
def test_dsh_native_frames_public_only_pagination_and_growth(tmp_path, compressed):
    path=tmp_path/'sessions/project/native'/('session.v3.jsonl'+('.zstd' if compressed else ''))
    items=rows();write_rows(path,items,compressed)
    original=hashlib.sha256(path.read_bytes()).hexdigest()
    d=ExternalSessions(tmp_path,roots={'dsh':[str(tmp_path/'sessions')]},cache_seconds=0)
    source=d.discover()['sources'][0]
    assert source['session_id']=='native' and source['title']=='real title' and source['model']=='actual-model'
    result=d.read(source['source_id'],limit=1)
    assert result['events'][0]['text']=='public answer' and result['has_older']
    previous=d.read(source['source_id'],before=result['history_before'],expected_generation=result['generation'])
    assert [e['text'] for e in previous['events']]==['real request']
    assert 'PRIVATE' not in json.dumps([source,result,previous])
    assert hashlib.sha256(path.read_bytes()).hexdigest()==original
    items.append({'type':'user/message','seq':6,'data':{'source':{'kind':'user'},'content':[{'type':'text','text':'followup'}]}})
    write_rows(path,items,compressed)
    updated=d.read(source['source_id'],after=result['cursor'],expected_generation=result['generation'])
    assert updated['reset'] and updated['events'][-1]['text']=='followup'


def test_dsh_bad_file_retains_public_snapshot_and_other_sources(tmp_path):
    path=tmp_path/'p/s/session.v3.jsonl';write_rows(path,rows())
    reader=DshFiles();first=reader.snapshot(path)
    path.write_text('broken')
    cached=reader.snapshot(path)
    assert cached['stale'] and cached['events']==first['events']
    other=tmp_path/'p/other/session.v3.jsonl';write_rows(other,rows())
    d=ExternalSessions(tmp_path,roots={'dsh':[str(tmp_path)]},cache_seconds=0)
    assert len(d.discover()['sources'])==1 and d.discover()['warnings']


def test_dsh_bounded_decompression_and_future_format(tmp_path):
    path=tmp_path/'p/s/session.v3.jsonl';write_rows(path,rows())
    with pytest.raises(ValueError):DshFiles(max_bytes=100).snapshot(path)
    data=rows();data[0]['version']=9;write_rows(path,data)
    with pytest.raises(ValueError):DshFiles().snapshot(path)
    future=path.parent/'session.v10.jsonl';future.write_text('future')
    assert log_paths(tmp_path)==[future]
    other=tmp_path/'p/other/session.v3.jsonl';write_rows(other,rows())
    discovery=ExternalSessions(tmp_path/'data',roots={'dsh':[str(tmp_path)]}).discover()
    assert len(discovery['sources'])==1 and discovery['warnings']
