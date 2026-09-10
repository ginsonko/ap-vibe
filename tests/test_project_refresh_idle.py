from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import ap_mind.organization_runner as runner


def test_idle_timeout_has_safe_bounded_default(monkeypatch):
    monkeypatch.delenv('AP_VIBE_CURATION_IDLE_TIMEOUT_SECONDS', raising=False)
    assert runner._idle_timeout_seconds() == 600
    monkeypatch.setenv('AP_VIBE_CURATION_IDLE_TIMEOUT_SECONDS', '1')
    assert runner._idle_timeout_seconds() == 30
    monkeypatch.setenv('AP_VIBE_CURATION_IDLE_TIMEOUT_SECONDS', '99999')
    assert runner._idle_timeout_seconds() == 1800


def test_windows_timeout_terminates_codex_process_tree(monkeypatch):
    calls = []

    class Process:
        pid = 1234

        @staticmethod
        def poll():
            return None

        @staticmethod
        def kill():
            calls.append("direct-kill")

    monkeypatch.setattr(runner.os, "name", "nt")
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )
    runner._terminate_process_tree(Process())

    assert calls and calls[0][0] == ["taskkill", "/PID", "1234", "/T", "/F"]
    assert "direct-kill" not in calls


def test_decode_final_repairs_only_missing_trailing_object_delimiters(tmp_path):
    output = tmp_path / "result.json"
    output.write_text('{"project":{"project_id":"p","sections":{}}}', encoding="utf-8")
    assert runner._decode_final(output, "") == {"project": {"project_id": "p", "sections": {}}}

    output.write_text('{"project":{"project_id":"p","sections":{}}', encoding="utf-8")
    assert runner._decode_final(output, "") == {"project": {"project_id": "p", "sections": {}}}

    output.write_text('{"project":{"project_id":"p","sections":{"identity":"unterminated}', encoding="utf-8")
    try:
        runner._decode_final(output, "")
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("incomplete string must not be repaired")
