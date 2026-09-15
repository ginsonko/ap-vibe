import json
from pathlib import Path
import sys
import pytest
import yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.dsh_mcp_install import patch_document, install_profiles
from tools.install_native_extras import install, atomic

PLUGIN={"id":"mcp-ap-vibe","name":"@deepseek-ai/dsh-mcp-client",
        "config":{"serverName":"ap-vibe","transport":"stdio","command":"python","args":["mcp.py"],"env":{}}}


@pytest.mark.parametrize("original",[
    "# comment\r\n[]\r\n",
    "- id: custom\n  config: !!js process.env.MY_VALUE\n# retain\n",
    '[{"id":"custom","config":{"env":"keep"}},]\n',
    "---\n- id: user\n  config: !!js (() => 5)()\n...\n",
])
def test_insert_patch_preserves_custom_yaml_and_repeats(original):
    updated, sha, owner=patch_document(original,PLUGIN)
    assert owner=="managed" and ("insert:" in updated or '"insert"' in updated)
    assert "custom" not in original or "custom" in updated
    assert "!!js" not in original or "!!js" in updated
    second, _, _=patch_document(updated,PLUGIN,[sha])
    assert second==updated
    parsed=yaml.compose(updated)
    assert isinstance(parsed,yaml.SequenceNode)


def test_user_registration_and_modified_managed_block_are_preserved():
    original=yaml.safe_dump([{"insert":[PLUGIN]}])
    assert patch_document(original,PLUGIN)==(original,None,"existing_user_registration")
    managed,sha,_=patch_document("[]",PLUGIN)
    with pytest.raises(ValueError,match="user_modified"):
        patch_document(managed.replace("mcp.py","user.py"),PLUGIN,[sha])
    with pytest.raises(ValueError,match="sequence"):
        patch_document("plugins: []",PLUGIN)


def setup(tmp_path):
    home=tmp_path/"dsh"
    profile=home/"profiles/desktop"
    profile.mkdir(parents=True)
    (profile/"package.json").write_text('{"dsh":{"profile":{"bundles":[]}}}')
    patch=profile/"cordis.patch.yml"
    patch.write_text("# original profile\n[]\n")
    settings=home/"settings.yaml";settings.write_text("model: retain\ncustom: keep\n")
    config=tmp_path/"vibe/config.json";config.parent.mkdir()
    config.write_text(json.dumps({"python":sys.executable}))
    return home,patch,settings,config


def test_native_install_writes_real_profile_keeps_settings_and_backup(tmp_path):
    home,patch,settings,config=setup(tmp_path)
    old=patch.read_bytes()
    outcome=install("dsh",home,config)
    assert outcome["mcp"]=="native_config_written" and outcome["status"]=="installed"
    assert yaml.safe_load(patch.read_text())[0]["insert"][0]["config"]["serverName"]=="ap-vibe"
    assert settings.read_text()=="model: retain\ncustom: keep\n"
    assert not install("dsh",home,config)["changed"]
    assert old in [p.read_bytes() for p in (config.parent/"integrations/backups").iterdir()]
    altered=patch.read_text().replace("mcp-ap-vibe","mcp-personal")
    patch.write_text(altered)
    assert install("dsh",home,config)["status"]=="configuration_partial"
    assert patch.read_text()==altered


def test_concurrent_edit_after_backup_is_not_overwritten(tmp_path):
    home,patch,settings,config=setup(tmp_path)
    def write(path,data):
        atomic(path,data)
        if path.name.endswith("-cordis.patch.yml"):
            patch.write_text("- id: user-concurrent-change\n")
    result=install_profiles(home,config,PLUGIN,write)
    assert result["issues"] and not result["changed"]
    assert patch.read_text()=="- id: user-concurrent-change\n"
