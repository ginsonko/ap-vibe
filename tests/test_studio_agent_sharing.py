"""Cross-installation sharing, credential isolation and transactional recovery."""
import base64
import copy
import json
from pathlib import Path

import pytest
from test_agent_studio import studio, profile
from test_studio_appearances import raw, image
from ap_mind.contracts import ContractError
from ap_mind.studio_server import StudioEpisodeService


@pytest.fixture
def destination(tmp_path):
    service = StudioEpisodeService(tmp_path / 'destination', project_root=tmp_path, codex_project_id='destination')
    yield service.agent_studio
    service.close()


def bundle(studio):
    appearance = studio.appearances.save(raw(manifest={'frame_width':2,'frame_height':2,
        'animations':{'idle':{'frames':[0]},'walk':{'frames':[0,1],'fps':6}}}))['appearances'][0]
    a = profile(studio, name='Designer', appearance_id=appearance['appearance_id'],
                persona='Patient designer', role='Frontend', protocol='openai')
    b = profile(studio, name='Reviewer', appearance_id='gpt-v3')
    studio.budget.save({'request_id':'source-budget','agent_id':a['agent_id'],'expected_revision':0,
        'token_limit':120000,'amount_limit':20,'price_basis':'blended','prices':{'input':3,'output':3},'price_source':'Sample'})
    studio.budget.observe(a['agent_id'],'run-fixture','paid-fixture',{'input_tokens':999})
    return studio.sharing.export({'agent_ids':[a['agent_id'],b['agent_id']]})['bundle']


def test_two_partners_roundtrip_no_credentials_usage_or_history(studio, destination):
    pack = bundle(studio)
    forbidden = {'api_key','secret','key_saved','password','authorization','agent_id','revision','tokens_used','run_id'}
    def check(value):
        if isinstance(value,dict):
            assert not forbidden.intersection(value)
            for item in value.values(): check(item)
        elif isinstance(value,list):
            for item in value: check(item)
    check(pack)
    assert 'fixture-secret-only' not in json.dumps(pack)
    preview = destination.sharing.preview({'bundle':pack})
    assert len(preview['agents']) == 2 and not any(a['already_imported'] for a in preview['agents'])
    request = {'request_id':'roundtrip','bundle':pack}
    result = destination.sharing.import_bundle(request)
    assert len(result['agents']) == 2 and not result['paid_request']
    agents = destination.profiles()['agents']
    assert all(not a['key_saved'] and not a['activated'] for a in agents)
    assert not {a['agent_id'] for a in agents}.intersection(a['source_id'] for a in pack['agents'])
    designer = next(a for a in agents if a['name']=='Designer')
    assert designer['persona']=='Patient designer' and designer['budget']['token_limit']==120000
    assert designer['budget']['tokens_used']==0 and designer['budget']['request_count']==0
    assert destination.sharing.import_bundle(request)['replayed']
    repeat = destination.sharing.import_bundle({**request,'request_id':'again'})
    assert not repeat['agents'] and len(repeat['skipped'])==2
    exported = destination.sharing.export({'agent_ids':[designer['agent_id']]})['bundle']
    assert exported['appearances'][0]['manifest']['animations']['walk']['frames']==[0,1]
    assert b'private-note' not in base64.b64decode(exported['appearances'][0]['png_base64'])


def test_partial_import_and_invalid_selection(studio, destination):
    pack = bundle(studio)
    for bad in ('x', 1, {}, [123], ['missing']):
        with pytest.raises(ContractError): destination.sharing.import_bundle({'request_id':'bad','bundle':pack,'selected_ids':bad})
    assert destination.profiles()['agents']==[]
    result = destination.sharing.import_bundle({'request_id':'first','bundle':pack,'selected_ids':[pack['agents'][0]['source_id']]})
    assert len(result['agents'])==1
    result = destination.sharing.import_bundle({'request_id':'second','bundle':pack})
    assert len(result['agents'])==1 and len(result['skipped'])==1


def test_bad_assets_rejected_during_preview_without_writes(studio,destination):
    original = bundle(studio)
    for field,value in [('png_base64','https://example.invalid/image.png'),('png_base64',base64.b64encode(b'bad').decode()),
                        ('manifest',{'frame_width':99999})]:
        pack=copy.deepcopy(original);pack['appearances'][0][field]=value
        with pytest.raises(ContractError):destination.sharing.preview({'bundle':pack})
    pack=copy.deepcopy(original);pack['appearances']=[]
    with pytest.raises(ContractError,match='appearance_missing'):destination.sharing.preview({'bundle':pack})
    assert destination.profiles()['agents']==[] and destination.appearances.list()['appearances']==[]


def test_transaction_rolls_back_all_profiles_assets_and_receipts(studio,destination,monkeypatch):
    pack=bundle(studio); real=destination.sharing._save_agent; count=0
    def fail_second(*args):
        nonlocal count
        count+=1
        if count==2:raise ContractError('injected_failure')
        return real(*args)
    monkeypatch.setattr(destination.sharing,'_save_agent',fail_second)
    with pytest.raises(ContractError,match='injected_failure'):
        destination.sharing.import_bundle({'request_id':'atomic','bundle':pack})
    assert destination.profiles()['agents']==[] and destination.appearances.list()['appearances']==[]
    monkeypatch.setattr(destination.sharing,'_save_agent',real)
    assert len(destination.sharing.import_bundle({'request_id':'atomic','bundle':pack})['agents'])==2


def test_same_builtin_name_different_content_preserves_shared_asset(studio,destination):
    pack=bundle(studio)
    builtin=next(a for a in pack['appearances'] if a['source_id']=='gpt-v3')
    builtin['display_name']='My edited GPT'
    builtin['manifest']['anchor']['x']+=1
    result=destination.sharing.import_bundle({'request_id':'modified','bundle':pack})
    reviewer=next(a for a in result['agents'] if a['name']=='Reviewer')
    assert reviewer['appearance_id'].startswith('custom-')
    assert len(destination.sharing.import_bundle({'request_id':'modified-repeat','bundle':pack})['skipped'])==2


def test_untrusted_fields_do_not_create_credentials(studio,destination):
    pack=bundle(studio)
    pack['agents'][0]['profile'].update(api_key='bad-secret',secret='bad-secret',agent_id='overwrite')
    pack['agents'][0]['profile']['persona']='Bearer abcdefghijklmnop and sk-abcdefgh1234567890'
    result=destination.sharing.import_bundle({'request_id':'redacted','bundle':pack})
    assert result['warnings']
    assert all(not a['key_saved'] for a in destination.profiles()['agents'])
    assert 'sk-abcdefgh1234567890' not in json.dumps(destination.profiles())


def test_all_builtins_export_and_import(studio,destination):
    ids=[]
    for appearance_id in studio.sharing.builtin_catalog():
        ids.append(profile(studio,name=appearance_id,appearance_id=appearance_id)['agent_id'])
    pack=studio.sharing.export({'agent_ids':ids})['bundle']
    result=destination.sharing.import_bundle({'request_id':'all-appearances','bundle':pack})
    assert len(result['agents'])==len(ids)
    assert all(a['appearance_id']==a['name'] for a in result['agents'])


def test_minimal_profile_import_is_repeatable_and_credential_url_is_rejected(destination):
    pack={'schema':'ap-vibe.agents.v1','agents':[{'source_id':'a','profile':{'name':' Starter '}}],'appearances':[]}
    assert len(destination.sharing.import_bundle({'request_id':'minimal-1','bundle':pack})['agents'])==1
    assert len(destination.sharing.import_bundle({'request_id':'minimal-2','bundle':pack})['skipped'])==1
    pack['agents'][0]['profile']['base_url']='https://user:secret@example.invalid/v1'
    with pytest.raises(ContractError,match='agent_url_invalid'):destination.sharing.preview({'bundle':pack})
