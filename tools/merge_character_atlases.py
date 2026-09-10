"""Compose reviewed action atlases into a new immutable built-in appearance."""
import argparse
import hashlib
import json
from pathlib import Path
from PIL import Image


def merge(inputs, target, appearance_id, name, atlas_name=None):
    sources = [(p, json.loads((p/'manifest.json').read_text(encoding='utf-8'))['characters'][0]) for p in inputs]
    images = [Image.open(p/c['atlas']).convert('RGBA') for p,c in sources]
    assert all(c['frame_width']==96 and c['frame_height']==96 and im.width==384 for im,(_,c) in zip(images,sources))
    result = Image.new('RGBA',(384,sum(im.height for im in images)))
    animations, offset, y = {}, 0, 0
    for im,(_,c) in zip(images,sources):
        result.alpha_composite(im,(0,y))
        animations.update({k:{**v,'frames':[i+offset for i in v['frames']]} for k,v in c['animations'].items()})
        offset += (im.width//96)*(im.height//96)
        y += im.height
    target.mkdir(parents=True,exist_ok=True)
    atlas=atlas_name or (appearance_id + '-atlas.png')
    assert Path(atlas).name == atlas and atlas.lower().endswith('.png'), 'Atlas must be a PNG filename'
    result.save(target/atlas)
    record={**sources[-1][1],'appearance_id':appearance_id,'display_name':name,'version':3,'atlas':atlas,
            'atlas_width':result.width,'atlas_height':result.height,'animations':animations,'missing_animations':[],
            'production':{'method':'Two reviewed image2 sources, local extraction and atlas composition',
                          'sources':[{'generation_id':c['production']['generation_id'],'source_sha256':c['source_sha256'],
                                      'atlas_sha256':c['sha256']} for _,c in sources]},
            'sha256':hashlib.sha256((target/atlas).read_bytes()).hexdigest()}
    record.pop('source_sha256',None)
    record['missing_animations']=[k for k in ['idle','walk','work','read','talk','wait','rest'] if k not in animations]
    (target/'manifest.json').write_text(json.dumps({'schema_version':1,'characters':[record]},ensure_ascii=False,indent=2),encoding='utf-8')
    contact=Image.new('RGB',(768,result.height*2),'#e3eee7')
    contact.paste(result.resize(contact.size,Image.Resampling.NEAREST),(0,0),result.resize(contact.size,Image.Resampling.NEAREST))
    contact.save(target/'contact-sheet.png')
    print(json.dumps({'appearance_id':appearance_id,'frames':offset,'actions':list(animations),'sha256':record['sha256']}))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--inputs',type=Path,nargs='+',required=True)
    parser.add_argument('--target',type=Path,required=True)
    parser.add_argument('--appearance-id',required=True)
    parser.add_argument('--name',required=True)
    parser.add_argument('--atlas-name')
    args=parser.parse_args()
    merge(args.inputs,args.target,args.appearance_id,args.name,args.atlas_name)
