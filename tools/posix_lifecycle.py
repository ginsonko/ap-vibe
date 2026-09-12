"""macOS/Linux lifecycle; Windows keeps its existing PowerShell implementation."""
from contextlib import contextmanager
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib import request
import uuid
import venv

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'src')]
from tools.installation import default_config_path
from ap_mind.platform_paths import data_dir


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def atomic(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd,'w',encoding='utf8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(config):
    import fcntl
    Path(config).parent.mkdir(parents=True,exist_ok=True)
    with (Path(config).parent/'lifecycle.lock').open('a+b') as stream:
        deadline=time.monotonic()+30
        while True:
            try:
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise RuntimeError('另一安装或启动正在处理，请稍后重试。')
                time.sleep(.1)
        try:yield
        finally:fcntl.flock(stream,fcntl.LOCK_UN)


def url(cfg):
    if cfg.get('host') not in ('127.0.0.1','localhost'):
        raise ValueError('本机生命周期只支持127.0.0.1或localhost')
    return 'http://'+cfg['host']+':'+str(int(cfg['port']))+'/'


def http(cfg, path, body=None, timeout=2):
    base=url(cfg).rstrip('/')
    req=request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type':'application/json','Origin':base})
    with request.build_opener(request.ProxyHandler({})).open(req,timeout=timeout) as response:
        return json.load(response)


def healthy(cfg):
    try:return http(cfg,'/v1/health').get('status')=='ok'
    except (OSError,ValueError):return False


def process(pid):
    if type(pid) is not int or pid<=0:return None
    try:
        birth=subprocess.check_output(['ps','-p',str(pid),'-o','lstart='],stderr=subprocess.DEVNULL,timeout=3).decode().strip()
        if sys.platform.startswith('linux'):
            raw=Path('/proc',str(pid),'stat').read_text()
            birth+=':'+raw.rsplit(')',1)[1].split()[19]
            argv=[v.decode('utf8','surrogateescape') for v in Path('/proc',str(pid),'cmdline').read_bytes().split(b'\0') if v]
        elif sys.platform=='darwin':
            # ps output loses argument boundaries (notably paths with spaces).
            # KERN_PROCARGS2 supplies argc and NUL-separated argv; never retain env.
            import ctypes
            libc=ctypes.CDLL(None,use_errno=True);mib=(ctypes.c_int*3)(1,49,pid)
            length=ctypes.c_size_t()
            if libc.sysctl(mib,3,None,ctypes.byref(length),None,0)!=0:return None
            buffer=ctypes.create_string_buffer(length.value)
            if libc.sysctl(mib,3,buffer,ctypes.byref(length),None,0)!=0:return None
            raw=buffer.raw[:length.value];argc=int.from_bytes(raw[:4],sys.byteorder)
            start=raw.index(b'\0',4)+1
            while start<len(raw) and raw[start]==0:start+=1
            argv=[v.decode('utf8','surrogateescape') for v in raw[start:].split(b'\0')[:argc]]
        else:return None
        args=shlex.join(argv)
        return {'pid':pid,'args':args,'argv':argv,'fingerprint':hashlib.sha256((birth+'\n'+args).encode()).hexdigest()}
    except (OSError,subprocess.SubprocessError,IndexError,ValueError):return None


def belongs(value,cfg):
    if not value:return False
    try:
        words=value.get('argv') or shlex.split(value['args'])
        def flag(name):return words[words.index(name)+1]
        return flag('-m')=='ap_mind.studio_server' and Path(flag('--data-dir')).resolve()==Path(cfg['data_dir']).resolve()
    except (ValueError,IndexError):return False


def receipt_path(config):return Path(config).parent/'posix-receipt.json'


def receipt(config):
    try:return read(receipt_path(config))
    except (OSError,ValueError):return {}


def owned(config,cfg):
    saved=receipt(config); live=process(saved.get('pid'))
    if live and belongs(live,cfg) and saved.get('fingerprint')==live['fingerprint']:
        return live
    return None


def discover(cfg):
    """Recover a lost receipt from exact data-directory ownership, never a port alone."""
    found=[]
    # First one bounded process-table query; fingerprint only matching commands.
    try:rows=subprocess.check_output(['ps','-axww','-o','pid=','-o','command='],timeout=3).decode().splitlines()
    except (OSError,subprocess.SubprocessError):return []
    for row in rows:
        pieces=row.strip().split(None,1)
        if len(pieces)==2 and 'ap_mind.studio_server' in pieces[1]:
            item={'pid':int(pieces[0]),'args':pieces[1]}
            live=process(item['pid'])
            if belongs(live,cfg):found.append(live)
    return found


def canonical_config(config):
    """One data store retains its original installation and encryption-key directory."""
    config=Path(config).expanduser().resolve()
    cfg=read(config)
    anchor=Path(cfg['data_dir']).resolve()/'.posix-installation.json'
    if not anchor.exists():return config
    try:
        original=Path(read(anchor)['config_path']).resolve()
        previous=read(original)
    except FileNotFoundError as exc:
        raise ValueError('原安装配置已丢失，请连同credentials目录恢复备份；数据保持原位。') from exc
    if Path(previous['data_dir']).resolve()!=Path(cfg['data_dir']).resolve():
        raise ValueError('数据目录的安装记录与原配置不一致，请恢复原配置；不会新建或覆盖数据。')
    return original


def status(config):
    config=canonical_config(config)
    cfg=read(config); live=owned(config,cfg)
    return {'ok':True,'status':'running' if live and healthy(cfg) else 'starting' if live else 'stopped',
            'url':url(cfg),'pid':live['pid'] if live else None,'config_path':str(config)}


def _start(config):
    cfg=read(config); live=owned(config,cfg)
    previous_owner=receipt(config).get('maintenance_owner')
    def ready(replayed):
        if previous_owner:
            http(cfg,'/v1/ap-vibe/studio/maintenance',{'action':'release','owner':previous_owner},timeout=8)
        return {**status(config),'replayed':replayed}
    if not live:
        candidates=discover(cfg)
        if len(candidates)>1:return {'ok':False,'status':'identity_conflict','message':'发现同数据目录多实例，保留现场，请核对。'}
        if candidates:
            live=candidates[0]
            words=live['argv'];cfg['port']=int(words[words.index('--port')+1])
            atomic(config,cfg);atomic(receipt_path(config),live)
    if live:
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if healthy(cfg):return ready(True)
            if not process(live['pid']):break
            time.sleep(.15)
        return {'ok':False,'status':'starting','pid':live['pid'],'url':url(cfg),'message':'原实例尚未恢复健康，保留它，不启动重复实例。'}
    with socket.socket() as sock:
        try:sock.bind(('127.0.0.1',int(cfg['port'])))
        except OSError:sock.bind(('127.0.0.1',0))
        cfg['port']=sock.getsockname()[1]
    atomic(config,cfg)
    root=Path(cfg['product_root']); logs=Path(config).parent/'logs';logs.mkdir(exist_ok=True)
    command=[cfg['python'],'-m','ap_mind.studio_server','--host',cfg['host'],'--port',str(cfg['port']),
        '--data-dir',cfg['data_dir'],'--studio-dir',cfg['studio_dir'],'--project-root',cfg['project_root'],
        '--logic-root',cfg.get('logic_root',cfg['project_root']),'--codex-project-id',cfg.get('project_id','ap-vibe-local')]
    for setting,flag in (('auto_monitor','--auto-monitor'),('auto_onboard_workspaces','--auto-onboard-workspaces')):
        if cfg.get(setting):command.append(flag)
    env={**os.environ,'PYTHONPATH':str(root/'src'),'AP_VIBE_CONFIG_PATH':str(Path(config).resolve()),'PYTHONIOENCODING':'utf-8'}
    with (logs/'daemon.stdout.log').open('ab') as out,(logs/'daemon.stderr.log').open('ab') as err:
        child=subprocess.Popen(command,cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=err,close_fds=True,start_new_session=True)
    deadline=time.monotonic()+20
    saved=False
    while time.monotonic()<deadline:
        live=process(child.pid)
        if belongs(live,cfg) and not saved:
            atomic(receipt_path(config),live);saved=True
        if child.poll() is not None:break
        if saved and healthy(cfg):
            # macOS framework Python may exec its real interpreter during
            # startup. Capture the final argv only after the service is ready,
            # while this Popen child is still known to be alive.
            final=process(child.pid)
            if child.poll() is None and belongs(final,cfg):
                atomic(receipt_path(config),final)
                return ready(False)
        time.sleep(.15)
    return {'ok':False,'status':'start_failed','url':url(cfg),'pid':child.pid,'message':'服务未恢复健康，请查看本安装logs目录；已有进程和数据保留。'}


def start(config):
    with locked(config):
        cfg=read(config)
        # Different config files must serialize on the same resolved data store.
        data=Path(cfg['data_dir']).resolve()
        with locked(data/'lifecycle-owner.json'):
            original=canonical_config(config)
            atomic(data/'.posix-installation.json',{'config_path':str(original)})
            return _start(original)


def stop(config):
    config=canonical_config(config)
    with locked(config):
        cfg=read(config);live=owned(config,cfg)
        if not live:
            return {'ok':True,'status':'stopped','stopped':False,'message':'没有可核对的自有PID，未向任何进程发送停止信号。'}
        owner='posix-lifecycle:'+uuid.uuid4().hex
        try:lease=http(cfg,'/v1/ap-vibe/studio/maintenance',{'action':'acquire','owner':owner},timeout=8)
        except (OSError,ValueError):return {'ok':False,'status':'unreachable','message':'无法核对活动任务，保留进程，先查看日志。'}
        if not lease.get('acquired'):return {'ok':False,'status':'busy','message':'工作室仍有活动任务，空闲后再停止。'}
        atomic(receipt_path(config),{**live,'maintenance_owner':owner})
        try:
            current=owned(config,cfg)
            if not current or current['fingerprint']!=live['fingerprint']:
                return {'ok':False,'status':'identity_changed','message':'进程归属变化，未停止。'}
            os.kill(live['pid'],signal.SIGINT)
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                if not owned(config,cfg):
                    atomic(receipt_path(config),{**live,'state':'stopped','maintenance_owner':owner})
                    return {'ok':True,'status':'stopped','stopped':True}
                time.sleep(.15)
            return {'ok':False,'status':'stop_timeout','message':'退出仍未确认，没有强杀。'}
        finally:
            try:http(cfg,'/v1/ap-vibe/studio/maintenance',{'action':'release','owner':owner},timeout=2)
            except (OSError,ValueError):pass


def desktop_available():
    return sys.platform=='darwin' or bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


def open_frontend(result,skip=False):
    if result.get('ok') and result.get('url') and not skip:
        if desktop_available():
            import webbrowser
            result['browser_opened']=bool(webbrowser.open(result['url']))
        else:result['browser_opened']=False
    return result


def install(config, *, data=None, project_root=None, port=8765, python=None, skip_integrations=False, skip_dependencies=False):
    config=Path(config).expanduser().resolve()
    with locked(config):
        if config.exists():
            cfg=read(config)
            if Path(cfg['product_root']).resolve()!=ROOT:
                return {'ok':False,'status':'existing_installation','message':'已有另一安装，请使用原安装入口或明确升级；数据保持原位。','product_root':cfg['product_root']}
        else:
            if not python:
                environment=config.parent/'runtime'
                venv.EnvBuilder(with_pip=True).create(environment)
                python=str(environment/'bin/python')
            cfg={'product':'AP-Vibe','schema':'ap-vibe.lifecycle.v1','product_root':str(ROOT),'python':str(python),
                'data_dir':str(Path(data).resolve() if data else data_dir()),'project_root':str(Path(project_root or ROOT).resolve()),
                'project_id':'ap-vibe-local','studio_dir':str(ROOT/'apps/studio/dist/client'),'host':'127.0.0.1','port':int(port),
                'auto_start':True,'auto_monitor':True,'auto_onboard_workspaces':True,'updates':{'enabled':True,'auto_install':False}}
            atomic(config,cfg)
        if not Path(cfg['studio_dir'],'index.html').is_file():
            return {'ok':False,'status':'frontend_missing','message':'源码需先在apps/studio运行npm ci和npm run build，或使用带前端的发布包。'}
        issues=[]
        if not skip_dependencies:
            result=subprocess.run([cfg['python'],str(ROOT/'tools/install_dependencies.py')],capture_output=True,timeout=150)
            if result.returncode:issues.append('部分依赖未安装，请运行tools/install_dependencies.py修复；核心工作台仍可使用。')
        if not skip_integrations:
            commands=[]
            if (Path.home()/'.codex').exists() or shutil.which('codex'):
                commands.append([str(ROOT/'tools/install_task_context.py'),'--config',str(config)])
                if shutil.which('codex'):commands.append([str(ROOT/'tools/install_mcp.py'),'--config',str(config)])
            if shutil.which('claude') or (Path.home()/'.local/bin/claude').is_file():
                commands.append([str(ROOT/'tools/install_claude.py'),'--config',str(config)])
            commands.extend([[str(ROOT/'tools/install_harness.py'),'--config',str(config)],
                             [str(ROOT/'tools/install_native_extras.py'),'--config',str(config)]])
            for args in commands:
                try:
                    result=subprocess.run([cfg['python'],*args],capture_output=True,timeout=90)
                    if result.returncode:issues.append(Path(args[0]).name+'未完成，保留原应用配置。')
                except subprocess.TimeoutExpired:issues.append(Path(args[0]).name+'仍需检查，未重复安装。')
        data_path=Path(cfg['data_dir']).resolve()
        with locked(data_path/'lifecycle-owner.json'):
            original=canonical_config(config)
            atomic(data_path/'.posix-installation.json',{'config_path':str(original)})
            result=_start(original)
        return {**result,'integration_issues':issues,'capability_scope':'POSIX基础工作台与已安装CLI；Windows快捷方式不可用，自动更新先准备候选再人工切换。'}


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['install','start','status','stop','open'],nargs='?',default='install')
    parser.add_argument('--config',type=Path,default=default_config_path())
    parser.add_argument('--data-dir',type=Path);parser.add_argument('--project-root',type=Path)
    parser.add_argument('--port',type=int,default=8765);parser.add_argument('--python')
    parser.add_argument('--skip-open',action='store_true');parser.add_argument('--skip-integrations',action='store_true');parser.add_argument('--skip-dependencies',action='store_true')
    args=parser.parse_args(argv)
    if os.name=='nt':parser.error('Windows请使用scripts/ap-vibe.ps1；该入口面向macOS/Linux。')
    try:
        if args.action=='install':result=install(args.config,data=args.data_dir,project_root=args.project_root,port=args.port,python=args.python,skip_integrations=args.skip_integrations,skip_dependencies=args.skip_dependencies)
        elif args.action in {'start','open'}:result=start(args.config)
        elif args.action=='stop':result=stop(args.config)
        else:result=status(args.config)
        if args.action in {'install','start','open'}:result=open_frontend(result,args.skip_open)
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        result={'ok':False,'status':'failed','message':str(exc)}
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result.get('ok') else 1


if __name__=='__main__':raise SystemExit(main())
