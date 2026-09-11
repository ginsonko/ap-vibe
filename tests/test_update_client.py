import hashlib
import json
from pathlib import Path
import zipfile
import pytest
from tools import update_client
from test_agent_studio import studio,profile
from test_studio_image_qa import batch,verdicts
from ap_mind.contracts import ContractError


def package(tmp_path,files=None):
    files=files or {n:b'content' for n in ('scripts/ap-vibe.ps1','tools/task_client.py','src/ap_mind/studio_server.py','apps/studio/dist/client/index.html')}
    archive=tmp_path/'candidate.zip'
    with zipfile.ZipFile(archive,'w') as z:
        for n,content in files.items():z.writestr(n,content)
    manifest={'schema':'ap-vibe.release.v1','repository':'ginsonko/ap-vibe','version':'v0.2.0-beta.1',
        'storage_contract':update_client.CONTRACT,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
        'files':{n:hashlib.sha256(v).hexdigest() for n,v in files.items()}}
    return archive,manifest


def test_staging_preserves_data_and_rejects_changed_packages(tmp_path):
    cfg=tmp_path/'config.json';cfg.write_text('{}')
    data=tmp_path/'data';data.mkdir();(data/'user.txt').write_text('keep')
    archive,m=package(tmp_path)
    target=update_client.stage(cfg,archive,m)
    assert update_client.stage(cfg,archive,m)==target
    assert (data/'user.txt').read_text()=='keep' and cfg.read_text()=='{}'
    (target/'tools/task_client.py').write_text('changed')
    with pytest.raises(ValueError,match='changed'):update_client.stage(cfg,archive,m)
    archive.write_bytes(b'html-error')
    with pytest.raises(ValueError,match='digest'):update_client.stage(cfg,archive,m)


def test_release_traversal_and_unknown_storage_are_not_installed(tmp_path):
    archive,m=package(tmp_path,{'../escape.txt':b'unsafe'})
    with pytest.raises(ValueError,match='path_invalid'):update_client.stage(tmp_path/'config.json',archive,m)
    assert not (tmp_path/'escape.txt').exists()
    m['storage_contract']='future-destructive-migration'
    with pytest.raises(ValueError,match='compatibility'):update_client.validate_manifest(m)


def test_idle_window_cannot_overtake_reserved_image_and_expires(studio,tmp_path,monkeypatch):
    created,_,_=batch(studio,tmp_path)
    work=studio.image_qa.reserve(created['batch_id'])
    assert studio.maintenance.change({'owner':'upgrade','action':'acquire'})['reason']=='busy'
    studio.image_qa.settle(work,verdicts(work),{})
    lease=studio.maintenance.change({'owner':'upgrade','action':'acquire'})
    assert lease['acquired']
    assert studio.maintenance.change({'owner':'other','action':'release'})['reason']=='another_update'
    a=profile(studio,name='next-agent')['agent_id']
    with pytest.raises(ContractError,match='service_updating'):
        studio.start({'agent_id':a,'request_id':'must-wait','project_id':'test-project','prompt':'next work'})
    assert studio.image_qa.reserve(created['batch_id']) is None
    monkeypatch.setattr('ap_mind.studio_maintenance.time.time',lambda:lease['expires']+1)
    assert not studio.maintenance.active()


def test_background_check_is_coalesced_and_never_downgrades(tmp_path,monkeypatch):
    cfg=tmp_path/'config.json'
    cfg.write_text(json.dumps({'product_root':str(tmp_path),'installed_version':'v0.2.0-beta.1'}))
    calls=[]
    def fetch(*a):
        calls.append(a)
        return json.dumps([{'tag_name':'v0.1.0-beta.1','assets':[]}]).encode()
    monkeypatch.setattr(update_client,'fetch',fetch)
    assert update_client.check(cfg)['state']=='current'
    assert update_client.check(cfg)['state']=='current' and len(calls)==1


def test_user_preferences_preserve_install_config_and_disable_worker(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from ap_mind.studio_updates import StudioUpdates
    data = tmp_path/'data';data.mkdir()
    cfg = tmp_path/'config.json'
    cfg.write_text(json.dumps({'product':'AP-Vibe','data_dir':str(data),'product_root':str(tmp_path),'custom':'preserve'}))
    original = cfg.read_bytes()
    monkeypatch.setenv('AP_VIBE_CONFIG_PATH',str(cfg))
    updates=StudioUpdates(SimpleNamespace(data_dir=data))
    assert updates.status()['mode']=='automatic'
    assert updates.action({'action':'configure','mode':'off'})['state']=='disabled'
    assert cfg.read_bytes()==original
    monkeypatch.setattr(update_client,'fetch',lambda *a:pytest.fail('disabled update made network call'))
    assert update_client.check(cfg,True)['state']=='disabled'
    assert updates.action({'action':'check'})['state']=='disabled'
    assert not StudioUpdates(SimpleNamespace(data_dir=tmp_path/'other')).status()['available']


def test_old_absolute_client_forwards_without_touching_config(tmp_path):
    import subprocess,sys
    old=tmp_path/'old'/'tools';new=tmp_path/'new'/'tools'
    old.mkdir(parents=True);new.mkdir(parents=True)
    import shutil
    shutil.copy2(Path(update_client.__file__).with_name('active_entry.py'),old/'active_entry.py')
    (old/'client.py').write_text('from active_entry import forward\nforward(__file__)\nprint("old")\n')
    (new/'client.py').write_text('import json,sys\nprint(json.dumps({"version":"new","args":sys.argv[1:]}))\n')
    cfg=tmp_path/'config.json';cfg.write_text(json.dumps({'product':'AP-Vibe','product_root':str(new.parent)}))
    original=cfg.read_bytes()
    output=subprocess.check_output([sys.executable,str(old/'client.py'),'--config',str(cfg),'remaining work'],text=True)
    assert json.loads(output)=={'version':'new','args':['--config',str(cfg),'remaining work']}
    assert cfg.read_bytes()==original
