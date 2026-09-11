import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from tools.release_bundle import build, collect_files
from tools.update_client import stage, verify


def source(tmp_path):
    root=tmp_path/'source';root.mkdir()
    subprocess.run(['git','init',str(root)],capture_output=True,check=True)
    policy={'schema':'ap-vibe.distribution.v1','include':['src/*','tools/*','scripts/*','apps/*','LICENSE','release-files.json'],
            'documents':['docs/guide.md'],'exclude':['*.db'],'bundle_exclude':[]}
    (root/'release-files.json').write_text(json.dumps(policy))
    names=['src/ap_mind/studio_server.py','tools/task_client.py','scripts/ap-vibe.ps1','scripts/process-identity.ps1',
           'apps/studio/dist/client/index.html','LICENSE','docs/guide.md']
    for name in names:
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('safe public fixture')
    return root


def test_distribution_omits_unreviewed_local_files_and_keeps_required_guides(tmp_path):
    root=source(tmp_path)
    for name in ['docs/private-notes.md','config.json','.local-acceptance/snapshot.ps1','src/user.db']:
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('private fixture')
    files=collect_files(root)
    assert 'docs/guide.md' in files
    assert not any('private' in n or '.local-' in n or n in {'config.json','src/user.db'} for n in files)


def test_deterministic_bundle_is_accepted_by_the_real_updater(tmp_path):
    root=source(tmp_path)
    one=build(root,tmp_path/'one','v0.2.0-beta.1')
    two=build(root,tmp_path/'two','v0.2.0-beta.1')
    assert one['sha256']==two['sha256']
    manifest=json.loads((tmp_path/'one/ap-vibe-manifest.json').read_text())
    cfg=tmp_path/'configuration/config.json';cfg.parent.mkdir();cfg.write_text('{}')
    target=stage(cfg,Path(one['archive']),manifest)
    assert verify(target,manifest)['version']=='v0.2.0-beta.1'
    assert (tmp_path/'one/SHA256SUMS').read_text().count('\n')==2
    with zipfile.ZipFile(one['archive']) as archive:
        assert all(entry.date_time==(1980,1,1,0,0,0) for entry in archive.infolist())


def test_secret_in_an_allowed_document_stops_publication(tmp_path):
    root=source(tmp_path)
    (root/'docs/guide.md').write_text('sk-'+'sensitivefixture'*3)
    with pytest.raises(ValueError,match='credential_pattern_in_release:docs/guide.md'):
        collect_files(root)
