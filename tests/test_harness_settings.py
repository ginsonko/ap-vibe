import json
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from ap_mind.external_sessions import ExternalSessions
from ap_mind.harness_settings import read, save
from ap_mind.contracts import ContractError


def test_partial_settings_preserve_other_apps_and_default_reset(tmp_path):
    data=tmp_path/'data';data.mkdir()
    initial={'future_field':{'keep':True},'sources':{'opencode':{'enabled':False,'future':1}}}
    (data/'harness-monitor.json').write_text(json.dumps(initial))
    external=ExternalSessions(data,roots={'dsh':[str(tmp_path/'default')],'opencode':[]})
    first=read(external);assert first['revision']==0
    request={'request_id':'one','expected_revision':0,'sources':{'dsh':{'roots':[str(tmp_path/'portable')],'enabled':True}}}
    saved=save(external,request);assert saved['revision']==1
    assert save(external,request)['revision']==1
    with pytest.raises(ContractError,match='harness_settings_request_conflict'):
        save(external,{**request,'sources':{'dsh':{'enabled':False}}})
    actual=json.loads((data/'harness-monitor.json').read_text())
    assert actual['future_field']==initial['future_field'] and actual['sources']['opencode']==initial['sources']['opencode']
    assert list((data/'harness-monitor-backups').glob('*.json'))
    with pytest.raises(ContractError):save(external,{**request,'request_id':'conflict'})
    reset=save(external,{'request_id':'two','expected_revision':1,'sources':{'dsh':{'roots':None}}})
    source=next(x for x in reset['sources'] if x['harness']=='dsh')
    assert source['roots']==[str(tmp_path/'default')] and source['custom_roots'] is None


def test_invalid_path_or_type_keeps_original_configuration(tmp_path):
    external=ExternalSessions(tmp_path,roots={})
    for sources in [{'dsh':{'roots':['relative']}},{'not-a-client':{}},{'dsh':{'enabled':'yes'}},{'claude':{}},{}]:
        with pytest.raises(ContractError):save(external,{'expected_revision':0,'sources':sources})
        assert not external.config_path.exists()
