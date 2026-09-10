from pathlib import Path
import json

from tools.install_task_context import install


def test_install_copies_all_skill_references_and_preserves_hooks(tmp_path: Path) -> None:
    product_root = Path(__file__).resolve().parents[1]
    codex_root = tmp_path / ".codex"
    codex_root.mkdir()
    hooks = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "other-tool"}]}]}}
    (codex_root / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
    (codex_root / "AGENTS.md").write_text("用户自己的全局指令\n", encoding="utf-8")

    result = install(product_root, codex_root, "C:/Python/python.exe")

    assert {item["name"] for item in result["skills"]} == {"ap-vibe-task-context", "ap-vibe-project-recovery"}
    assert (codex_root / "skills/ap-vibe-task-context/references/organization.md").is_file()
    assert (codex_root / "skills/ap-vibe-project-recovery/references/portable-recovery.json").is_file()
    saved_hooks = json.loads((codex_root / "hooks.json").read_text(encoding="utf-8"))
    assert any(item.get("command") == "other-tool" for group in saved_hooks["hooks"]["SessionStart"] for item in group["hooks"])
    install(product_root, codex_root, "C:/Python/python.exe")
    agents = (codex_root / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.startswith("用户自己的全局指令\n")
    assert agents.count("<!-- AP-VIBE-TASK-CONTEXT-V1 BEGIN -->") == 1
    assert (Path(result['backup']) / 'AGENTS.md').read_text(encoding='utf8') == "用户自己的全局指令\n"


def test_custom_install_and_upgrade_keep_instance_and_user_snapshot(tmp_path: Path) -> None:
    product_root = Path(__file__).resolve().parents[1]
    codex_root = tmp_path / '用户目录'
    config = tmp_path / "工作台 '个人'" / 'config.json'
    install(product_root, codex_root, 'C:/Python/python.exe', config)
    snapshot = codex_root / 'skills/ap-vibe-project-recovery/references/portable-recovery.json'
    original = b'{"local_user_data":"keep this snapshot"}'
    snapshot.write_bytes(original)
    install(product_root, codex_root, 'C:/Python/python.exe', config)
    saved = json.loads((codex_root / 'hooks.json').read_text(encoding='utf-8'))
    for groups in saved['hooks'].values():
        own = [h for g in groups for h in g['hooks'] if 'task_client.py' in h.get('command', '')]
        assert len(own) == 1
        assert '--config "' + config.as_posix() + '"' in own[0]['command']
        assert config.as_posix().replace("'", "''") in own[0]['commandWindows']
    for skill in ['ap-vibe-task-context', 'ap-vibe-project-recovery']:
        descriptor = json.loads((codex_root / 'skills' / skill / 'references/installation.json').read_text(encoding='utf-8'))
        assert Path(descriptor['config_path']) == config
    assert snapshot.read_bytes() == original
