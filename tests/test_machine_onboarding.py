from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ap_mind.contracts import ContractError
from ap_mind.product import CodexSessionDiscovery, MACHINE_WORKSPACE_SOURCE
from ap_mind.studio_server import StudioEpisodeService


def _event(kind: str, payload: dict) -> str:
    return json.dumps({"timestamp": "2026-09-05T09:00:00Z", "type": kind, "payload": payload}) + "\n"


def _session(path: Path, cwd: Path, session_id: str, text: str = "visible") -> None:
    path.write_text(
        _event("session_meta", {"id": session_id, "cwd": str(cwd)})
        + _event("response_item", {
            "type": "message", "id": session_id + "-message", "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }),
        encoding="utf-8",
    )


def _service(tmp_path: Path, *, enabled: bool = True) -> StudioEpisodeService:
    default = tmp_path / "default"
    sessions = tmp_path / "sessions"
    default.mkdir(exist_ok=True)
    sessions.mkdir(exist_ok=True)
    return StudioEpisodeService(
        tmp_path / "data", project_root=default, codex_project_id="default",
        codex_sessions_root=sessions, auto_onboard_workspaces=enabled,
    )


def test_machine_workspaces_are_exact_stable_and_keep_manual_root_priority(tmp_path: Path) -> None:
    parent = tmp_path / "workspaces"
    child = parent / "child"
    nested = child / "nested"
    nested.mkdir(parents=True)
    service = _service(tmp_path)
    try:
        broad = service.resolve_codex_workspace(str(parent))
        narrow = service.resolve_codex_workspace(str(child))
        assert broad.project_id != narrow.project_id
        assert broad.source == narrow.source == MACHINE_WORKSPACE_SOURCE
        assert broad.extra["match_scope"] == "exact_cwd"
        assert broad.logic_root is narrow.logic_root is None
        assert service.product_registry.match_project(str(nested)) == (None, "session_cwd_not_registered")
        assert service.resolve_codex_workspace(str(child / ".." / "child")).project_id == narrow.project_id
        manual, _ = service.product_registry.register(
            root_path=nested, display_name="Manual", project_id="manual",
        )
        nested_child = nested / "inside"
        nested_child.mkdir()
        assert service.resolve_codex_workspace(str(nested_child)).project_id == manual.project_id
    finally:
        service.close()
    cold = _service(tmp_path)
    try:
        assert cold.resolve_codex_workspace(str(parent)).project_id == broad.project_id
        assert cold.resolve_codex_workspace(str(child)).project_id == narrow.project_id
    finally:
        cold.close()


def test_machine_discovery_is_opt_in_and_exclusions_are_sticky(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    service = _service(tmp_path, enabled=False)
    source = tmp_path / "sessions" / "first.jsonl"
    _session(source, workspace, "first")
    try:
        off = service.discover_codex_sources()
        assert off["unassigned"][0]["reason"] == "session_cwd_not_registered"
        assert len(service.product_registry.list()) == 1
        service.auto_onboard_workspaces = True
        result = service.poll_codex_sources()
        assert result["processed_count"] == 1
        record = service.product_registry.source(result["discovery"]["assigned"][0]["source_key"])
        project = service.product_registry.get(record.project_id)
        assert project.project_id != "default"
        assert service.resolve_codex_workspace(str(workspace), "first").project_id == project.project_id
        assert service.codex_sampler_state(project.project_id)["auto_onboard_workspaces"] is True
        with pytest.raises(ContractError, match="codex_binding_identity_changed"):
            service.resolve_codex_workspace(str(tmp_path), "first")
        service.product_registry.update_source(record.source_key, status="disabled")
        assert service.poll_codex_sources()["processed_count"] == 0
        with pytest.raises(ContractError, match="codex_source_disabled"):
            service.resolve_codex_workspace(str(workspace), "first")
        service.product_registry.set_status(project.project_id, "archived")
        _session(tmp_path / "sessions" / "second.jsonl", workspace, "second")
        archived = service.discover_codex_sources()
        assert archived["unassigned"][0]["reason"] == "codex_workspace_monitor_disabled"
        assert service.product_registry.source(record.source_key).status == "disabled"
        assert len(service.product_registry.list()) == 2
        with pytest.raises(ContractError, match="codex_workspace_monitor_disabled"):
            service.resolve_codex_workspace(str(workspace))
    finally:
        service.close()


def test_machine_preserves_explicit_binding_and_rejects_changed_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    service = _service(tmp_path, enabled=False)
    source = tmp_path / "sessions" / "explicit.jsonl"
    _session(source, workspace, "explicit")
    try:
        discovered = CodexSessionDiscovery(source.parent).discover().sessions[0]
        service.product_registry.register_source(discovered, "default", explicit=True)
        service.auto_onboard_workspaces = True
        assert service.poll_codex_sources()["processed_count"] == 1
        assert service.resolve_codex_workspace(str(workspace), "explicit").project_id == "default"
        assert len(service.product_registry.list()) == 1
        other = tmp_path / "other"
        other.mkdir()
        _session(source, other, "explicit")
        changed = service.poll_codex_sources()
        assert changed["processed_count"] == 0
        assert changed["status"] == "partial"
        assert service.product_registry.source(discovered.source_key).project_id == "default"
    finally:
        service.close()


def test_machine_poll_pages_sources_fairly_and_cold_restart_keeps_cursors(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    service = _service(tmp_path)
    for index in range(19):
        _session(tmp_path / "sessions" / f"session-{index:02d}.jsonl", workspace, f"session-{index:02d}")
    try:
        first = service.poll_codex_sources()
        assert first["processed_count"] == 16
        project = service.resolve_codex_workspace(str(workspace))
        assert first["projects"][0]["source_count_total"] == 19
        second = service.poll_codex_sources()
        assert second["processed_count"] == 3
        sources = service.product_registry.sources(project.project_id, limit=128)
        assert len(sources) == 19
        assert all(source.cursor == source.source_size and source.last_polled_at for source in sources)
        assert service.poll_codex_sources()["processed_count"] == 0
    finally:
        service.close()
    cold = _service(tmp_path)
    try:
        assert cold.poll_codex_sources()["processed_count"] == 0
    finally:
        cold.close()


def test_machine_discovery_reuses_unchanged_source_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A repeated discovery must not take a write transaction for unchanged sources."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    (tmp_path / "sessions").mkdir()
    source = tmp_path / "sessions" / "stable.jsonl"
    _session(source, workspace, "stable")
    service = _service(tmp_path)
    try:
        first = service.discover_codex_sources()
        assert len(first["assigned"]) == 1

        def unexpected_register(*_args, **_kwargs):
            raise AssertionError("unchanged source should use the read-through projection")

        monkeypatch.setattr(service.product_registry, "register_source", unexpected_register)
        second = service.discover_codex_sources()
        assert len(second["assigned"]) == 1
        assert second["assigned"][0]["replayed"] is True
        assert second["unassigned"] == []
    finally:
        service.close()


def test_machine_rejects_relative_missing_and_non_directory_workspaces(tmp_path: Path) -> None:
    service = _service(tmp_path)
    regular_file = tmp_path / "file"
    regular_file.write_text("file", encoding="utf-8")
    try:
        for cwd, code in (
            ("relative", "codex_workspace_cwd_must_be_absolute"),
            (str(tmp_path / "missing"), "codex_workspace_cwd_not_found"),
            (str(regular_file), "codex_workspace_cwd_not_directory"),
        ):
            with pytest.raises(ContractError, match=code):
                service.resolve_codex_workspace(cwd)
    finally:
        service.close()
