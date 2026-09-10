from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RESTORE_PATH = ROOT / "skills" / "ap-vibe-project-recovery" / "scripts" / "restore.py"
SPEC = importlib.util.spec_from_file_location("ap_vibe_recovery_restore", RESTORE_PATH)
assert SPEC is not None and SPEC.loader is not None
restore = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore)


def test_missing_explicit_installation_never_queries_default_workbench(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('AP_VIBE_CONFIG_PATH', str(tmp_path / 'missing-config.json'))
    def forbidden(*args, **kwargs):
        raise AssertionError('Must not fall back to the unrelated default endpoint')
    monkeypatch.setattr(restore, '_online_recovery', forbidden)
    assert restore.main(['restore']) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['context_available'] is False
    assert 'installation_config_unavailable' in json.dumps(result)


def _brief(**overrides):
    value = {
        "protocol_version": "ap-vibe.agent-brief.v1",
        "project_id": "ap-vibe-local",
        "goal": "继续当前任务",
        "recovery_valid": True,
        "authority": restore.EXPECTED_AUTHORITY,
        "revision_id": "knowledge_revision_1",
        "content_hash": "A" * 64,
        "summary": "已审恢复基线",
        "completed": ["读取链路已接通"],
        "remaining": ["继续观察真实任务"],
        "unknown": ["长期收益"],
        "redlines": ["不要伪造正式 Vibe 写回"],
        "next_action": "继续读取按需章节",
        "latest_delta": None,
        "source_pointers": ["runtime://127.0.0.1:8765/"],
        "stop_conditions": ["未知保持未知"],
    }
    value.update(overrides)
    return value


def test_clean_brief_keeps_verified_baseline_compatible() -> None:
    cleaned = restore._clean_brief(_brief(), "ap-vibe-local")

    assert cleaned["recovery_valid"] is True
    assert cleaned["degraded"] is False
    assert cleaned["revision_id"] == "knowledge_revision_1"
    assert cleaned["content_hash"] == "A" * 64


def test_clean_brief_accepts_unreviewed_delta_without_revision_gate() -> None:
    cleaned = restore._clean_brief(
        _brief(
            recovery_valid=False,
            authority="temporary_delta_only",
            revision_id=None,
            content_hash=None,
            summary=None,
            latest_delta={
                "authority": "unreviewed_delta",
                "summary": "页面刚刚完成局部接线",
                "completed": [],
                "remaining": ["等待真实验收"],
                "unknown": [],
                "redlines": [],
                "next_action": "读取真实运行证据",
                "conflicts": [],
                "completeness": "partial",
                "proposal_ref": "proposal-1",
                "source_refs": ["session://example"],
                "pending_count": 1,
            },
        ),
        "ap-vibe-local",
    )

    assert cleaned["recovery_valid"] is False
    assert cleaned["degraded"] is True
    assert cleaned["authority"] == "temporary_delta_only"
    assert cleaned["revision_id"] is None
    assert cleaned["latest_delta"]["summary"] == "页面刚刚完成局部接线"


def test_online_recovery_returns_degraded_read_when_chain_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        {
            "project_id": "ap-vibe-local",
            "valid": False,
            "reason": "content_hash_mismatch",
            "milestone": None,
            "write_capability": {"vibe_formal_write": False},
        },
        {
            "vibe_formal_write": False,
            "brief": _brief(
                recovery_valid=False,
                authority="temporary_delta_only",
                revision_id=None,
                content_hash=None,
                summary=None,
            ),
            "text": "",
            "characters": 0,
            "brief_incomplete": True,
        },
    ]
    monkeypatch.setattr(restore, "_read_json", lambda request, timeout: responses.pop(0))

    snapshot = restore._online_recovery(
        "http://127.0.0.1:8765",
        "ap-vibe-local",
        "继续当前任务",
        8_000,
        2.0,
    )

    assert snapshot["recovery_valid"] is False
    assert snapshot["degraded"] is True
    assert snapshot["authority"] == "temporary_delta_only"
    assert snapshot["brief_text"].startswith("# AP-Vibe Agent Brief")
    assert "reviewed_recovery_unavailable:content_hash_mismatch" in snapshot["limitations"]


def test_portable_degraded_snapshot_is_readable_but_marked_stale(tmp_path: Path) -> None:
    path = tmp_path / "portable-recovery.json"
    snapshot = {
        "protocol_version": restore.SNAPSHOT_PROTOCOL,
        "project_id": "ap-vibe-local",
        "exported_at": "2026-09-07T00:00:00Z",
        "source_mode": "online_daemon",
        "recovery_valid": False,
        "reported_recovery_valid": False,
        "degraded": True,
        "revision_id": None,
        "revision_number": 0,
        "content_hash": None,
        "authority": "temporary_delta_only",
        "vibe_formal_write": False,
        "brief": _brief(
            recovery_valid=False,
            authority="temporary_delta_only",
            revision_id=None,
            content_hash=None,
            summary=None,
        ),
        "brief_text": "降级上下文",
        "brief_incomplete": True,
        "limitations": ["reviewed_recovery_unavailable"],
    }
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    loaded = restore._portable(path, "ap-vibe-local")
    receipt = restore._receipt(loaded, source_mode="portable_snapshot", online_error="daemon_unavailable")

    assert receipt["ok"] is True
    assert receipt["context_available"] is True
    assert receipt["degraded"] is True
    assert receipt["recovery_valid"] is False
    assert receipt["stale"] is True
    assert receipt["revision_id"] is None


def test_no_source_returns_handled_empty_receipt() -> None:
    receipt = restore._failure(
        restore.RecoveryError("daemon_unavailable", "启动本地 AP-Vibe daemon。"),
        restore.RecoveryError("portable_snapshot_missing", "在线时生成快照。"),
        "ap-vibe-local",
    )

    assert receipt["ok"] is True
    assert receipt["context_available"] is False
    assert receipt["degraded"] is True
    assert receipt["recovery_valid"] is False
    assert receipt["authority"] == "unavailable"
