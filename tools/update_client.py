"""Release staging and background update checks for one installation."""
from contextlib import contextmanager
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib import request
import uuid
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
from tools.installation import default_config_path
from ap_mind.studio_processes import process_absent

REPO='ginsonko/ap-vibe'
CONTRACT='ap-vibe-additive-v1'


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
    os.replace(temporary,path)


def validate_manifest(manifest):
    if manifest.get('schema')!='ap-vibe.release.v1' or manifest.get('repository')!=REPO:
        raise ValueError('release_source_mismatch')
    if manifest.get('storage_contract')!=CONTRACT:raise ValueError('storage_compatibility_unknown')
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?',str(manifest.get('version',''))):
        raise ValueError('release_version_invalid')
    if not isinstance(manifest.get('files'),dict) or not manifest['files']:raise ValueError('release_manifest_empty')
    for name,sha in manifest['files'].items():
        p=PurePosixPath(name)
        if not name or '\\' in name or ':' in name or p.is_absolute() or '..' in p.parts or str(p)!=name:
            raise ValueError('release_path_invalid')
        if not re.fullmatch(r'[0-9a-f]{64}',str(sha)):raise ValueError('release_digest_invalid')


def verify(root,manifest):
    validate_manifest(manifest)
    root=Path(root).resolve()
    for name,sha in manifest['files'].items():
        p=root/name
        if p.is_symlink() or not p.resolve().is_relative_to(root) or not p.is_file():raise ValueError('release_file_missing:'+name)
        if hashlib.sha256(p.read_bytes()).hexdigest()!=sha:raise ValueError('release_file_changed:'+name)
    for name in ('scripts/ap-vibe.ps1','tools/task_client.py','src/ap_mind/studio_server.py','apps/studio/dist/client/index.html'):
        if name not in manifest['files']:raise ValueError('release_entry_missing:'+name)
    return manifest


def stage(config_path,archive,manifest):
    validate_manifest(manifest)
    if hashlib.sha256(Path(archive).read_bytes()).hexdigest()!=manifest.get('sha256'):
        raise ValueError('release_archive_digest_mismatch')
    versions=Path(config_path).parent/'versions';versions.mkdir(exist_ok=True)
    target=versions/(manifest['version']+'-'+manifest['sha256'][:12])
    if target.exists():verify(target,manifest);return target
    temporary=versions/('.staging-'+uuid.uuid4().hex);temporary.mkdir()
    try:
        with zipfile.ZipFile(archive) as z:
            infos=z.infolist()
            names=[i.filename for i in infos if not i.is_dir()]
            if len(names)!=len(set(names)) or set(names)!=set(manifest['files']):raise ValueError('release_archive_entries_mismatch')
            if sum(i.file_size for i in infos)>512*1024*1024:raise ValueError('release_archive_too_large')
            for i in infos:
                if i.is_dir():continue
                if (i.external_attr>>16)&0o170000==0o120000:raise ValueError('release_symlink_invalid')
                p=temporary/i.filename;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(z.read(i))
        verify(temporary,manifest)
        atomic(temporary/'release.json',manifest)
        os.replace(temporary,target)
    finally:
        if temporary.exists():shutil.rmtree(temporary)
    return target


def fetch(url,limit):
    req=request.Request(url,headers={'Accept':'application/vnd.github+json','User-Agent':'AP-Vibe-Updater'})
    with request.urlopen(req,timeout=30) as response:
        data=response.read(limit+1)
        if len(data)>limit:raise ValueError('release_download_too_large')
        return data


def dirty(root):
    if not (Path(root)/'.git').exists():return False
    run=subprocess.run(['git','status','--porcelain','--untracked-files=normal'],cwd=root,capture_output=True,timeout=15)
    return run.returncode!=0 or bool(run.stdout.strip())


def version_order(value):
    match=re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)(?:-([A-Za-z]+)\.(\d+))?',str(value or ''))
    if not match:return None
    return (*map(int,match.groups()[:3]),1 if not match[4] else 0,match[4] or '',int(match[5] or 0))


def checkpoint(config_path,root):
    """Called after the exact daemon stops; keep private rollback data locally."""
    config=read(config_path);candidate=verify(root,read(Path(root)/'release.json'))
    directory=Path(config_path).parent/'update-backups'/uuid.uuid4().hex;directory.mkdir(parents=True)
    shutil.copy2(config_path,directory/'config.json')
    entries=[]
    for p in Path(config['data_dir']).glob('*.sqlite'):
        dst=directory/p.name
        with sqlite3.connect(str(p)) as source,sqlite3.connect(str(dst)) as target:source.backup(target)
        entries.append({'file':p.name,'bytes':dst.stat().st_size,'sha256':hashlib.sha256(dst.read_bytes()).hexdigest()})
    atomic(directory/'backup.json',{'data_dir':config['data_dir'],'databases':entries,'version':candidate['version'],
        'meaning':'恢复锚点；代码回退不自动覆盖数据库中的新写入。'})
    return {'ok':True,'backup':str(directory),'version':candidate['version']}


@contextmanager
def worker_lock(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    try:
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:
        try:
            owner=int(path.read_text())
            if process_absent(owner) is True:
                path.unlink();fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
            else:yield False;return
        except (OSError,ValueError):yield False;return
    os.write(fd,str(os.getpid()).encode());os.close(fd)
    try:yield True
    finally:
        try:path.unlink()
        except FileNotFoundError:pass


def check(config_path,force=False):
    config_path=Path(config_path).resolve();config=read(config_path)
    status_path=config_path.parent/'update-status.json'
    with worker_lock(config_path.parent/'update-worker.lock') as acquired:
        if not acquired:return {'ok':True,'state':'checking_elsewhere'}
        options=dict(config.get('updates') or {})
        preference_path=config_path.parent/'update-options.json'
        if preference_path.exists():options.update(read(preference_path))
        if options.get('enabled') is False:return {'ok':True,'state':'disabled'}
        previous=read(status_path) if status_path.exists() else {}
        if not force and previous.get('next_check_at',0)>time.time():return previous
        state={'ok':True,'state':'checking','checked_at':time.time(),'next_check_at':time.time()+3600,
               'repository':REPO,'current_version':config.get('installed_version')}
        atomic(status_path,state)
        try:
            releases=json.loads(fetch('https://api.github.com/repos/'+REPO+'/releases?per_page=10',2*1024*1024))
            release=next((r for r in releases if not r.get('draft') and (options.get('channel','beta')=='beta' or not r.get('prerelease'))),None)
            if not release:state.update(state='no_release');return state
            state['available_version']=release['tag_name']
            installed_order,available_order=version_order(config.get('installed_version')),version_order(release['tag_name'])
            if release['tag_name']==config.get('installed_version') or installed_order and available_order and available_order<=installed_order:
                state.update(state='current');return state
            assets={a['name']:a for a in release.get('assets',[])}
            if not {'ap-vibe-manifest.json','ap-vibe-app.zip'} <= set(assets):
                state.update(state='release_not_packaged',message='该发行版尚无可自动安装的应用包，当前服务保持运行。');return state
            def asset_url(name):
                url=assets[name]['browser_download_url']
                if not url.startswith('https://github.com/'+REPO+'/releases/download/'+release['tag_name']+'/'):
                    raise ValueError('release_asset_source_mismatch')
                return url
            manifest=json.loads(fetch(asset_url('ap-vibe-manifest.json'),4*1024*1024))
            validate_manifest(manifest)
            if manifest['version']!=release['tag_name']:raise ValueError('release_tag_mismatch')
            archive=config_path.parent/('download-'+manifest['version']+'.zip')
            try:
                archive.write_bytes(fetch(asset_url('ap-vibe-app.zip'),256*1024*1024))
                target=stage(config_path,archive,manifest)
            finally:archive.unlink(missing_ok=True)
            state.update(state='ready',candidate_root=str(target))
            if dirty(config['product_root']):
                state.update(state='development_changes',message='开发目录有本地改动；新版已独立准备，保留当前调试环境。');return state
            if options.get('auto_install',True) is False:return state
            shell=shutil.which('pwsh.exe') or shutil.which('powershell.exe')
            if not shell:state.update(state='ready',message='当前平台暂不支持自动切换。');return state
            result=subprocess.run([shell,'-NoProfile','-ExecutionPolicy','Bypass','-File',str(Path(config['product_root'])/'scripts/ap-vibe.ps1'),
                '-Action','apply-update','-ConfigDir',str(config_path.parent),'-CandidateRoot',str(target),'-SkipOpen'],
                capture_output=True,text=True,encoding='utf8',timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            applied=json.loads(result.stdout.lstrip('\ufeff'))
            state.update(state='installed' if applied.get('updated') else applied.get('status','pending'),result=applied)
            if state['state']=='busy':state['next_check_at']=time.time()+60
        except Exception as exc:
            state.update(ok=False,state='check_failed',message=str(exc)[:250])
        finally:atomic(status_path,state)
        return state


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['check','stage','verify','checkpoint']);p.add_argument('--config',type=Path,default=default_config_path());p.add_argument('--root',type=Path);p.add_argument('--force',action='store_true')
    p.add_argument('--archive',type=Path);p.add_argument('--manifest',type=Path)
    args=p.parse_args()
    if args.action=='check':result=check(args.config,args.force)
    elif args.action=='stage':
        if not args.archive or not args.manifest:p.error('stage requires --archive and --manifest')
        target=stage(args.config,args.archive,read(args.manifest))
        result={'ok':True,'candidate_root':str(target),'version':read(target/'release.json')['version']}
    elif args.action=='verify':
        value=verify(args.root,read(args.root/'release.json'));result={'ok':True,'version':value['version'],'storage_contract':value['storage_contract']}
    else:result=checkpoint(args.config,args.root)
    print(json.dumps(result,ensure_ascii=False))
