import base64
import json
import struct
import zlib
import pytest
from test_agent_studio import studio
from ap_mind.contracts import ContractError
from ap_mind.studio_appearances import normalize, StudioAppearances


def chunk(name,body):
    return struct.pack('>I',len(body))+name+body+struct.pack('>I',zlib.crc32(name+body)&0xffffffff)


def image(width=4,height=2):
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,6,0,0,0))+chunk(b'tEXt',b'private-note\x00remove this')+chunk(b'IDAT',zlib.compress((b'\x00'+b'\x21\x81\x64\xff'*width)*height))+chunk(b'IEND',b'')


def raw(**changes):
    return {'display_name':'Fixture','png_base64':base64.b64encode(image()).decode(),**changes}


def test_asset_is_idempotent_persistent_and_history_survives_archive(studio):
    spec={'frame_width':2,'frame_height':2,'animations':{'idle':{'frames':[0]},'walk':{'frames':[0,1],'fps':6}}}
    a=studio.appearances.save(raw(manifest=spec))['appearances'][0]
    b=studio.appearances.save(raw(manifest=spec))['appearances'][0]
    assert a==b and len(studio.appearances.list()['appearances'])==1
    png=studio.appearances.image(a['appearance_id'])
    assert b'remove this' not in png
    restored=StudioAppearances(studio.registry)
    assert restored.image(a['appearance_id'])==png
    restored.archive({'appearance_id':a['appearance_id']})
    assert restored.list()['appearances']==[]
    assert restored.list(a['appearance_id'])['appearances'][0]['archived']
    assert restored.image(a['appearance_id'])==png
    renamed=restored.save(raw(display_name='Changed',manifest=spec))['appearances'][0]
    assert renamed['appearance_id']!=a['appearance_id']
    assert restored.save(raw(manifest=spec))['appearances'][0]['appearance_id']==a['appearance_id']


@pytest.mark.parametrize('patch',[
    {'png_base64':'https://example.invalid/asset.png'},
    {'png_base64':base64.b64encode(b'<svg onload="x"/>').decode()},
    {'png_base64':base64.b64encode(image()[:-2]).decode()},
    {'manifest':{'frame_width':3}},
    {'manifest':{'frame_width':2,'animations':{'idle':{'frames':[2]}}}},
    {'manifest':{'animations':{'idle':{'frames':[True]}}}},
    {'manifest':{'animations':{'idle':{'frames':[0,0],'fps':0}}}},
    {'manifest':{'anchor':{'x':300,'y':1}}},
    {'manifest':{'schema_version':99}},
    {'display_name':'\x00bad'},
])
def test_invalid_assets_do_not_write(studio,patch):
    with pytest.raises(ContractError):studio.appearances.save(raw(**patch))
    assert studio.appearances.list()['appearances']==[]


def test_decompression_limit_and_checksum():
    compressed=zlib.compress(b'X'*100000)
    bomb=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,6,0,0,0))+chunk(b'IDAT',compressed)+chunk(b'IEND',b'')
    with pytest.raises(ContractError):normalize(raw(png_base64=base64.b64encode(bomb).decode()))
    changed=bytearray(image());changed[40]^=1
    with pytest.raises(ContractError):normalize(raw(png_base64=base64.b64encode(changed).decode()))
