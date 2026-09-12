"""Build a reviewable application release from the current source, not local data."""
import argparse
from fnmatch import fnmatchcase
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile


def collect_files(root, *, bundle=True):
    """Use the reviewed distribution policy, never every untracked workspace file."""
    root = Path(root).resolve()
    policy = json.loads((root/'release-files.json').read_text(encoding='utf-8-sig'))
    if policy.get('schema') != 'ap-vibe.distribution.v1':
        raise ValueError('release_policy_invalid')
    paths=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=root).decode().split('\0')
    selected={}
    for name in sorted(set(paths)):
        p=root/name
        if not name or not p.is_file() or p.is_symlink():continue
        if not p.resolve().is_relative_to(root):continue
        if not (name in policy['documents'] or any(fnmatchcase(name,pattern) for pattern in policy['include'])):continue
        if any(fnmatchcase(name,pattern) for pattern in policy['exclude']):continue
        if bundle and any(fnmatchcase(name,pattern) for pattern in policy.get('bundle_exclude',[])):continue
        if name.startswith(('.local-', '.git/', '.pytest_cache/', 'node_modules/', '.venv/')):continue
        if any(part in {'__pycache__','node_modules','.venv'} for part in p.relative_to(root).parts):continue
        if p.suffix.lower() in {'.sqlite','.sqlite3','.db','.jsonl','.log','.pyc'}:continue
        data=p.read_bytes()
        if p.suffix.lower() in {'.md','.json','.py','.ps1','.toml','.txt','.js','.jsx','.yml','.yaml'}:
            if re.search(rb'\bsk-[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',data):
                raise ValueError('credential_pattern_in_release:'+name)
        selected[name]=(data,hashlib.sha256(data).hexdigest())
    return selected


def build(root, output, version):
    root,output=Path(root),Path(output)
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?',version):
        raise ValueError('invalid_release_version')
    selected=collect_files(root)
    version_data=json.dumps({'product':'AP-Vibe','version':version},ensure_ascii=False,indent=2).encode('utf8')
    selected['ap-vibe-version.json']=(version_data,hashlib.sha256(version_data).hexdigest())
    for required in ('src/ap_mind/studio_server.py','tools/task_client.py','scripts/ap-vibe.ps1','scripts/process-identity.ps1','apps/studio/dist/client/index.html','LICENSE'):
        if required not in selected:raise ValueError('release_file_missing:'+required)
    output.mkdir(parents=True,exist_ok=True)
    archive=output/'ap-vibe-app.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name,(data,_) in sorted(selected.items()):
            entry=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0))
            entry.compress_type=zipfile.ZIP_DEFLATED
            entry.create_system=3
            entry.external_attr=(0o100755 if name.endswith('.sh') else 0o100644) << 16
            z.writestr(entry,data,compresslevel=6)
    manifest={'schema':'ap-vibe.release.v1','version':version,'repository':'ginsonko/ap-vibe',
        'storage_contract':'ap-vibe-additive-v1','archive':'ap-vibe-app.zip',
        'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
        'files':{name:sha for name,(_,sha) in selected.items()}}
    (output/'ap-vibe-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    sums=[]
    for path in (archive,output/'ap-vibe-manifest.json'):
        sums.append(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.name)
    (output/'SHA256SUMS').write_text('\n'.join(sums)+'\n',encoding='ascii')
    return {'ok':True,'version':version,'file_count':len(selected),'archive':str(archive),'sha256':manifest['sha256']}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--version',required=True)
    args=p.parse_args()
    print(json.dumps(build(Path(__file__).resolve().parents[1],args.output,args.version)))
