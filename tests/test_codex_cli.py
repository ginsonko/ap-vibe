from pathlib import Path
import os
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind import codex_cli


def test_explicit_cli_override_and_missing_file(tmp_path, monkeypatch):
    path=tmp_path/'custom codex.exe';path.write_bytes(b'fixture')
    monkeypatch.setenv('AP_VIBE_CODEX_EXECUTABLE',str(path))
    assert codex_cli.codex_command()==[str(path)]
    monkeypatch.setenv('AP_VIBE_CODEX_EXECUTABLE',str(tmp_path/'missing'))
    with pytest.raises(Exception,match='configured_codex_executable_not_found'):codex_cli.codex_command()


@pytest.mark.skipif(os.name!='nt',reason='Windows Desktop discovery')
def test_desktop_queue_capability_wins_over_older_path_cli(tmp_path, monkeypatch):
    monkeypatch.delenv('AP_VIBE_CODEX_EXECUTABLE',raising=False)
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path))
    path=tmp_path/'OpenAI/Codex/bin/version-a/codex.exe';path.parent.mkdir(parents=True);path.write_bytes(b'fixture')
    monkeypatch.setattr(codex_cli,'supports_queue',lambda cmd:cmd==(str(path),))
    monkeypatch.setattr(codex_cli.shutil,'which',lambda _: 'old/codex.exe')
    assert codex_cli.codex_command()==[str(path)]
