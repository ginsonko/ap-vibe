import json
import os
from pathlib import Path
import stat
import sys
import pytest
from ap_mind import platform_paths
from ap_mind.contracts import ContractError


@pytest.mark.parametrize('system,tail',[('darwin','Library/Application Support/AP-Vibe'),('linux','.config/ap-vibe')])
def test_native_user_paths_and_legacy_preservation(tmp_path,monkeypatch,system,tail):
    monkeypatch.setattr(platform_paths.sys,'platform',system)
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    monkeypatch.delenv('XDG_CONFIG_HOME',raising=False)
    assert platform_paths.config_dir()==tmp_path/tail
    old=tmp_path/'AppData/Local/AP-Vibe';old.mkdir(parents=True);(old/'config.json').write_text('{}')
    assert platform_paths.config_dir()==old
    modern=tmp_path/tail;modern.mkdir(parents=True);(modern/'config.json').write_text('{}')
    assert platform_paths.config_dir()==modern


def test_xdg_and_explicit_private_location(tmp_path,monkeypatch):
    monkeypatch.setattr(platform_paths.sys,'platform','linux')
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'settings'))
    monkeypatch.setenv('XDG_DATA_HOME',str(tmp_path/'files'))
    monkeypatch.setenv('AP_VIBE_CONFIG_PATH',str(tmp_path/'custom/config.json'))
    assert platform_paths.config_dir()==tmp_path/'settings/ap-vibe'
    assert platform_paths.data_dir()==tmp_path/'files/ap-vibe'
    assert platform_paths.private_dir()==tmp_path/'custom'


@pytest.mark.skipif(os.name=='nt',reason='POSIX file ownership and permissions require a POSIX runner')
def test_posix_credentials_authenticated_restart_and_permissions(tmp_path,monkeypatch):
    pytest.importorskip('cryptography')
    from ap_mind import posix_secrets
    monkeypatch.setenv('AP_VIBE_CONFIG_PATH',str(tmp_path/'config.json'))
    raw=b'fixture-only-secret'
    encrypted=posix_secrets.protect(raw)
    assert raw not in encrypted and posix_secrets.protect(encrypted,True)==raw
    key=tmp_path/'credentials/master.key'
    assert stat.S_IMODE(key.stat().st_mode)==0o600
    assert stat.S_IMODE(key.parent.stat().st_mode)==0o700
    with pytest.raises(ContractError,match='decryption_failed'):
        posix_secrets.protect(encrypted[:-1]+bytes([encrypted[-1]^1]),True)
    key.unlink()
    with pytest.raises(ContractError,match='master_unavailable'):
        posix_secrets.protect(encrypted,True)
    assert not key.exists()
