from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time
from urllib import error as urllib_error
from urllib import request as urllib_request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ap_mind.contracts import ContractError
from ap_mind.product import CodexActivityMonitor
from ap_mind.studio_server import StudioEpisodeService, create_server


def _json_request(url: str, method: str = "GET", payload: dict | None = None):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib_request.Request(
        url,
        data=raw,
        method=method,
        headers={"Content-Type": "application/json"} if raw is not None else {},
    )
    with urllib_request.urlopen(request, timeout=15) as response:
        return response.status, json.loads(response.read().decode("utf-8")), dict(response.headers)


def _error_request(url: str, method: str, payload: dict) -> tuple[int, dict]:
    with pytest.raises(urllib_error.HTTPError) as info:
        _json_request(url, method, payload)
    response = info.value
    return response.code, json.loads(response.read().decode("utf-8"))


def _server(tmp_path: Path):
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    target = tmp_path / "portable-target"
    sessions = tmp_path / "sessions"
    for path in (project_a, project_b, target, sessions):
        path.mkdir()
    server = create_server(
        port=0,
        data_dir=tmp_path / "data",
        project_root=project_a,
        logic_root=project_a,
        codex_project_id="project-a",
        codex_sessions_root=sessions,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}", project_b, target


def test_g1_projects_are_explicitly_isolated_and_memory_disposition_is_reversible(tmp_path: Path) -> None:
    server, thread, base, project_b, _ = _server(tmp_path)
    try:
        _, registered, _ = _json_request(
            base + "/v1/ap-vibe/projects",
            "POST",
            {
                "project_id": "project-b",
                "display_name": "第二个项目",
                "root_path": str(project_b),
                "auto_monitor_enabled": False,
            },
        )
        assert registered["project"]["project_id"] == "project-b"

        activity_a = {
            "request_id": "g1-activity-a",
            "activity": {
                "activity_id": "activity-a",
                "project_id": "project-a",
                "kind": "progress",
                "summary": "项目 A 的进度",
                "source_ref": "test://g1/a",
                "completeness": "complete",
            },
        }
        activity_b = {
            "request_id": "g1-activity-b",
            "activity": {
                "activity_id": "activity-b",
                "project_id": "project-b",
                "kind": "progress",
                "summary": "项目 B 的进度",
                "source_ref": "test://g1/b",
                "completeness": "complete",
            },
        }
        _json_request(base + "/v1/ap-vibe/activities", "POST", activity_a)
        _json_request(base + "/v1/ap-vibe/activities", "POST", activity_b)

        _, state_a, _ = _json_request(base + "/v1/ap-vibe/state?project_id=project-a")
        _, state_b, _ = _json_request(base + "/v1/ap-vibe/state?project_id=project-b")
        assert {item["project_id"] for item in state_a["episodes"]} == {"project-a"}
        assert {item["project_id"] for item in state_b["episodes"]} == {"project-b"}
        assert all("项目 A" not in json.dumps(item, ensure_ascii=False) for item in state_b["episodes"])

        disposition = {
            "request_id": "g1-disposition-archive",
            "project_id": "project-b",
            "activity_id": "activity-b",
            "state": "archived",
            "reason": "这条记录只是临时试验，不作为项目记忆",
        }
        _, archived, _ = _json_request(
            base + "/v1/ap-vibe/memories/disposition", "POST", disposition
        )
        assert archived["disposition"]["state"] == "archived"
        _, data_b, _ = _json_request(base + "/v1/ap-vibe/data?project_id=project-b")
        memory_b = next(item for item in data_b["memories"] if item["activity_id"] == "activity-b")
        assert memory_b["disposition"]["state"] == "archived"

        disposition_restore = {**disposition, "request_id": "g1-disposition-restore", "state": "active"}
        _, restored, _ = _json_request(
            base + "/v1/ap-vibe/memories/disposition", "POST", disposition_restore
        )
        assert restored["disposition"]["state"] == "active"

        # Discovery and an empty poll are real no-op control operations: no
        # source means no episode and no provider invocation.
        _, discovery, _ = _json_request(base + "/v1/ap-vibe/codex/discover", "POST", {})
        assert discovery["configured"] is True
        before = server.service.health().to_dict()["ap_vibe"]["request_count"]
        code, poll = _error_request(
            base + "/v1/ap-vibe/codex/sync", "POST", {"project_id": "project-b"}
        )
        # A missing source is a truthful, bounded failure rather than an empty
        # success that could be mistaken for a processed message.
        assert code == 400
        assert poll["error"]["code"] == "codex_source_not_configured"
        after = server.service.health().to_dict()["ap_vibe"]["request_count"]
        assert after == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_g1_memory_disposition_changes_future_b_recall_without_deleting_history(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    service = StudioEpisodeService(
        tmp_path / "data",
        project_root=project,
        codex_project_id="project",
    )
    try:
        def activity(activity_id: str, summary: str) -> dict:
            return {
                "activity_id": activity_id,
                "project_id": "project",
                "kind": "progress",
                "summary": summary,
                "detail": summary,
                "source_ref": f"test://{activity_id}",
                "actor": "probe",
                "status": "observed",
                "completeness": "complete",
                "privacy_scope": "project",
            }

        def recall_refs(request_id: str, payload: dict) -> set[str]:
            view, _ = service.run_project_activity(request_id, payload)
            return {
                str(item["event_ref"])
                for tick in view["ticks"]
                for item in tick.get("recall", ())
                if isinstance(item, dict) and item.get("event_ref")
            }

        topic = "认证回调端口配置与浏览器真实回读验证"
        service.run_project_activity("g1-memory-anchor", activity("anchor", topic))
        before = recall_refs("g1-memory-before", activity("before", topic))
        anchor_event = service.learning_ledger.memory_events("project")[0].event_id
        assert anchor_event in before

        archived = service.set_memory_disposition(
            {
                "request_id": "g1-memory-archive",
                "project_id": "project",
                "activity_id": "anchor",
                "state": "archived",
                "reason": "聚焦反事实",
                "source_ref": "test://g1-memory-disposition",
            }
        )
        assert archived["disposition"]["state"] == "archived"
        after_archive = recall_refs("g1-memory-after-archive", activity("after-archive", topic))
        assert anchor_event not in after_archive

        restored = service.set_memory_disposition(
            {
                "request_id": "g1-memory-restore",
                "project_id": "project",
                "activity_id": "anchor",
                "state": "active",
                "reason": "聚焦反事实恢复",
                "source_ref": "test://g1-memory-disposition",
            }
        )
        assert restored["disposition"]["state"] == "active"
        after_restore = recall_refs("g1-memory-after-restore", activity("after-restore", topic))
        assert anchor_event in after_restore

        observations = service.learning_ledger.memory_observations("project")
        assert any(item["activity_id"] == "anchor" for item in observations)
        states = service.product_registry.memory_states("project")
        assert states["anchor"]["state"] == "active"
    finally:
        service.close()


def test_g1_memory_disposition_http_route_changes_next_episode_recall(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    server = create_server(
        port=0,
        data_dir=tmp_path / "data",
        project_root=project,
        codex_project_id="project",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        def activity(activity_id: str, summary: str) -> dict:
            return {
                "activity_id": activity_id,
                "project_id": "project",
                "kind": "progress",
                "summary": summary,
                "detail": summary,
                "source_ref": f"test://{activity_id}",
                "actor": "probe",
                "status": "observed",
                "completeness": "complete",
                "privacy_scope": "project",
            }

        def run(request_id: str, payload: dict) -> set[str]:
            _, view, _ = _json_request(
                base + "/v1/ap-vibe/activities",
                "POST",
                {"request_id": request_id, "activity": payload},
            )
            # The HTTP activity route wraps the AP episode under ``episode``;
            # keep the helper tolerant of the service-level view used by the
            # neighboring direct-service probe while asserting the real route.
            view = view.get("episode", view)
            return {
                str(item["event_ref"])
                for tick in view["ticks"]
                for item in tick.get("recall", ())
                if isinstance(item, dict) and item.get("event_ref")
            }

        topic = "认证回调端口配置与浏览器真实回读验证"
        run("http-memory-anchor", activity("anchor", topic))
        before = run("http-memory-before", activity("before", topic))
        _, data, _ = _json_request(base + "/v1/ap-vibe/data?project_id=project")
        anchor_event = next(item["event_id"] for item in data["memories"] if item["activity_id"] == "anchor")
        assert anchor_event in before

        _, archived, _ = _json_request(
            base + "/v1/ap-vibe/memories/disposition",
            "POST",
            {
                "request_id": "http-memory-archive",
                "project_id": "project",
                "activity_id": "anchor",
                "state": "archived",
                "reason": "HTTP 聚焦反事实",
            },
        )
        assert archived["disposition"]["state"] == "archived"
        after_archive = run("http-memory-after-archive", activity("after-archive", topic))
        assert anchor_event not in after_archive

        _, restored, _ = _json_request(
            base + "/v1/ap-vibe/memories/disposition",
            "POST",
            {
                "request_id": "http-memory-restore",
                "project_id": "project",
                "activity_id": "anchor",
                "state": "active",
                "reason": "HTTP 聚焦反事实恢复",
            },
        )
        assert restored["disposition"]["state"] == "active"
        after_restore = run("http-memory-after-restore", activity("after-restore", topic))
        assert anchor_event in after_restore

        _, data_after, _ = _json_request(base + "/v1/ap-vibe/data?project_id=project")
        retained = next(item for item in data_after["memories"] if item["activity_id"] == "anchor")
        assert retained["disposition"]["state"] == "active"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_g1_health_projects_automatic_discovery_as_configured_without_polling(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    sessions = tmp_path / "sessions"
    project.mkdir()
    sessions.mkdir()
    service = StudioEpisodeService(
        tmp_path / "data",
        project_root=project,
        logic_root=project,
        codex_project_id="project",
        codex_sessions_root=sessions,
    )
    try:
        health = service.health().to_dict()["ap_vibe"]["codex_sampler"]
        assert health["configured"] is True
        assert health["auto_discovery_configured"] is True
        assert health["mode"] == "auto_discovery_ready"
        assert health["source_count"] == 0
        assert service.recent_project_state("project")["episodes"] == []
    finally:
        service.close()


def test_g1_portable_preview_confirm_replay_and_reversible_rollback(tmp_path: Path) -> None:
    server, thread, base, _, target = _server(tmp_path)
    try:
        _json_request(
            base + "/v1/ap-vibe/activities",
            "POST",
            {
                "request_id": "g1-portable-activity",
                "activity": {
                    "activity_id": "portable-activity",
                    "project_id": "project-a",
                    "kind": "milestone",
                    "summary": "可迁移的项目事实",
                    "source_ref": "test://g1/portable",
                    "completeness": "complete",
                },
            },
        )
        _, exported, _ = _json_request(base + "/v1/ap-vibe/portable/export?project_id=project-a")
        bundle = dict(exported["bundle"])
        bundle.update(
            {
                "target_project_id": "project-imported",
                "target_display_name": "导入后的项目",
                "target_root": str(target),
            }
        )

        _, before_projects, _ = _json_request(base + "/v1/ap-vibe/projects")
        _, preview, headers = _json_request(
            base + "/v1/ap-vibe/portable/import/preview",
            "POST",
            {"request_id": "g1-portable-preview", "bundle": bundle},
        )
        assert preview["draft"]["write_state"] == "preview_only"
        assert headers["X-AP-Mind-Replayed"] == "false"
        _, after_preview_projects, _ = _json_request(base + "/v1/ap-vibe/projects")
        assert after_preview_projects["project_count"] == before_projects["project_count"]

        draft = preview["draft"]
        confirmation = {
            "request_id": "g1-portable-confirm",
            "confirmation": {"draft_id": draft["draft_id"], "bundle_hash": draft["content_hash"]},
        }
        _, confirmed, headers = _json_request(
            base + "/v1/ap-vibe/portable/import/confirm", "POST", confirmation
        )
        assert confirmed["project_id"] == "project-imported"
        assert confirmed["draft"]["status"] == "confirmed"
        assert headers["X-AP-Mind-Replayed"] == "false"

        _, replay, headers = _json_request(
            base + "/v1/ap-vibe/portable/import/confirm", "POST", confirmation
        )
        assert replay["project_id"] == "project-imported"
        assert replay["draft"]["status"] == "confirmed"
        assert headers["X-AP-Mind-Replayed"] == "true"
        _, projects_after_confirm, _ = _json_request(base + "/v1/ap-vibe/projects")
        assert [item["project_id"] for item in projects_after_confirm["projects"]].count("project-imported") == 1

        _, rolled_back, headers = _json_request(
            base + "/v1/ap-vibe/portable/import/rollback",
            "POST",
            {"project_id": "project-imported"},
        )
        assert rolled_back["project"]["status"] == "archived"
        assert rolled_back["data_deleted"] is False
        assert headers["X-AP-Mind-Replayed"] == "false"

        _, rollback_replay, headers = _json_request(
            base + "/v1/ap-vibe/portable/import/rollback",
            "POST",
            {"project_id": "project-imported"},
        )
        assert rollback_replay["project"]["status"] == "archived"
        assert rollback_replay["data_deleted"] is False
        assert headers["X-AP-Mind-Replayed"] == "true"

        code, error = _error_request(
            base + "/v1/ap-vibe/portable/import/rollback",
            "POST",
            {"project_id": "project-a"},
        )
        assert code == 409
        assert error["error"]["code"] == "portable_import_rollback_only"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_g1_portable_draft_survives_service_cold_restart_before_confirm(tmp_path: Path) -> None:
    """A preview draft is durable across service instances and remains idempotent.

    The two service instances intentionally share only the product data
    directory.  The second instance must recover the exact draft identity,
    continue the same confirmation, and never create a second imported
    project when the confirmation is replayed.
    """

    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    data_dir = tmp_path / "data"
    activity = {
        "activity_id": "cold-portable-activity",
        "project_id": "source-project",
        "kind": "milestone",
        "summary": "冷重启 portable 导入事实",
        "detail": "冷重启 portable 导入事实",
        "source_ref": "test://g1/portable-cold-restart",
        "actor": "probe",
        "status": "observed",
        "completeness": "complete",
        "privacy_scope": "project",
    }

    first = StudioEpisodeService(
        data_dir,
        project_root=source_root,
        codex_project_id="source-project",
    )
    try:
        first.run_project_activity("cold-source-activity", activity)
        exported = first.export_project("source-project")
        bundle = dict(exported["bundle"])
        bundle.update(
            {
                "target_project_id": "cold-imported",
                "target_display_name": "冷重启导入",
                "target_root": str(target_root),
            }
        )
        preview = first.preview_project_import("cold-preview", bundle)
        draft = preview["draft"]
        assert draft["write_state"] == "preview_only"
    finally:
        first.close()

    second = StudioEpisodeService(
        data_dir,
        project_root=source_root,
        codex_project_id="source-project",
    )
    try:
        recovered = second.product_registry.import_draft(draft["draft_id"])
        assert recovered["draft_id"] == draft["draft_id"]
        assert recovered["bundle_hash"] == draft["content_hash"]
        assert recovered["status"] == "preview_only"
        assert recovered["completed_stage"] == "preview_only"

        confirmed = second.confirm_project_import(
            "cold-confirm",
            {"draft_id": draft["draft_id"], "bundle_hash": draft["content_hash"]},
        )
        assert confirmed["project_id"] == "cold-imported"
        assert confirmed["draft"]["status"] == "confirmed"
        assert confirmed["draft"]["completed_stage"] == "confirmed"
        assert confirmed["replayed"] is False

        replay = second.confirm_project_import(
            "cold-confirm",
            {"draft_id": draft["draft_id"], "bundle_hash": draft["content_hash"]},
        )
        assert replay["project_id"] == "cold-imported"
        assert replay["draft"]["status"] == "confirmed"
        assert replay["replayed"] is True

        projects = second.projects(include_archived=True)
        assert projects["project_count"] == 2
        assert [item["project_id"] for item in projects["projects"]].count("cold-imported") == 1

        rollback = second.rollback_project_import("cold-imported")
        assert rollback["project"]["status"] == "archived"
        assert rollback["data_deleted"] is False
    finally:
        second.close()


def test_g1_monitor_reuses_one_discovery_per_poll_and_keeps_incremental_cursors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the real discovery -> AP episode path without double scanning."""

    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    sessions = tmp_path / "sessions"
    for path in (project_a, project_b, sessions):
        path.mkdir()

    def event(timestamp: str, outer: str, payload: dict) -> str:
        return json.dumps(
            {"timestamp": timestamp, "type": outer, "payload": payload},
            ensure_ascii=False,
        ) + "\n"

    source_a = sessions / "session-a.jsonl"
    source_b = sessions / "session-b.jsonl"
    source_a.write_text(
        "".join(
            (
                event("2026-09-04T02:00:00Z", "session_meta", {"id": "sa", "cwd": str(project_a)}),
                event(
                    "2026-09-04T02:00:01Z",
                    "response_item",
                    {
                        "type": "message",
                        "id": "a-visible",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "A initial"}],
                    },
                ),
            )
        ),
        encoding="utf-8",
    )
    source_b.write_text(
        "".join(
            (
                event("2026-09-04T02:00:00Z", "session_meta", {"id": "sb", "cwd": str(project_b)}),
                event(
                    "2026-09-04T02:00:01Z",
                    "response_item",
                    {
                        "type": "message",
                        "id": "b-visible",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "B initial"}],
                    },
                ),
            )
        ),
        encoding="utf-8",
    )

    service = StudioEpisodeService(
        tmp_path / "data",
        project_root=project_a,
        logic_root=project_a,
        codex_project_id="project-a",
        codex_sessions_root=sessions,
    )
    try:
        service.register_project(
            {
                "project_id": "project-b",
                "display_name": "Project B",
                "root_path": str(project_b),
                "auto_monitor_enabled": True,
            }
        )
        original_discovery = service.discover_codex_sources
        discovery_calls: list[int] = []

        def counted_discovery() -> dict:
            discovery_calls.append(1)
            return original_discovery()

        monkeypatch.setattr(service, "discover_codex_sources", counted_discovery)

        first = service.poll_codex_sources()
        assert len(discovery_calls) == 1
        assert first["processed_count"] == 2
        assert {item["project_id"] for item in first["projects"]} == {"project-a", "project-b"}
        assert all(
            source["source"]["cursor"] == source["source"]["source_size"]
            for project in first["projects"]
            for source in project["sources"]
        )

        before_counts = {
            project_id: len(service.recent_project_state(project_id)["episodes"])
            for project_id in ("project-a", "project-b")
        }
        empty = service.poll_codex_sources()
        assert len(discovery_calls) == 2
        assert empty["processed_count"] == 0
        assert empty["empty_poll"] is True
        assert {
            project_id: len(service.recent_project_state(project_id)["episodes"])
            for project_id in ("project-a", "project-b")
        } == before_counts

        with source_b.open("a", encoding="utf-8") as handle:
            handle.write(
                event(
                    "2026-09-04T02:00:02Z",
                    "response_item",
                    {
                        "type": "message",
                        "id": "b-appended",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "B appended"}],
                    },
                )
            )
        appended = service.poll_codex_sources()
        assert len(discovery_calls) == 3
        assert appended["processed_count"] == 1
        assert appended["projects"][0]["project_id"] in {"project-a", "project-b"}
        assert len(service.recent_project_state("project-a")["episodes"]) == before_counts["project-a"]
        assert len(service.recent_project_state("project-b")["episodes"]) == before_counts["project-b"] + 1

        # A direct public sync still performs its own discovery when called
        # outside the monitor path; the optimization is intentionally local.
        service.sync_codex_activity("project-a")
        assert len(discovery_calls) == 4
    finally:
        service.close()


def test_g1_partial_source_is_reported_without_blocking_other_project(
    tmp_path: Path,
) -> None:
    """A malformed/unassigned source stays visible while good work proceeds."""

    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    outside = tmp_path / "outside"
    sessions = tmp_path / "sessions"
    for path in (project_a, project_b, outside, sessions):
        path.mkdir()

    def event(timestamp: str, outer: str, payload: dict) -> str:
        return json.dumps(
            {"timestamp": timestamp, "type": outer, "payload": payload},
            ensure_ascii=False,
        ) + "\n"

    (sessions / "good.jsonl").write_text(
        event("2026-09-04T03:00:00Z", "session_meta", {"id": "good", "cwd": str(project_b)})
        + event(
            "2026-09-04T03:00:01Z",
            "response_item",
            {
                "type": "message",
                "id": "good-message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "good project progress"}],
            },
        ),
        encoding="utf-8",
    )
    # This source is assigned to project-a but contains one malformed line;
    # the later visible message must still be processed and the source kept
    # partial rather than aborting the whole monitor cycle.
    (sessions / "partial.jsonl").write_text(
        event("2026-09-04T03:00:00Z", "session_meta", {"id": "partial", "cwd": str(project_a)})
        + "{not-json}\n"
        + event(
            "2026-09-04T03:00:01Z",
            "response_item",
            {
                "type": "message",
                "id": "partial-message",
                "role": "user",
                "content": [{"type": "input_text", "text": "partial project progress"}],
            },
        ),
        encoding="utf-8",
    )
    (sessions / "unassigned.jsonl").write_text(
        event("2026-09-04T03:00:00Z", "session_meta", {"id": "unassigned", "cwd": str(outside)})
        + event(
            "2026-09-04T03:00:01Z",
            "response_item",
            {
                "type": "message",
                "id": "unassigned-message",
                "role": "user",
                "content": [{"type": "input_text", "text": "unassigned"}],
            },
        ),
        encoding="utf-8",
    )

    service = StudioEpisodeService(
        tmp_path / "data",
        project_root=project_a,
        logic_root=project_a,
        codex_project_id="project-a",
        codex_sessions_root=sessions,
    )
    try:
        service.register_project(
            {
                "project_id": "project-b",
                "display_name": "Project B",
                "root_path": str(project_b),
                "auto_monitor_enabled": True,
            }
        )
        result = service.poll_codex_sources()
        assert result["status"] == "partial"
        assert result["processed_count"] == 2
        assert result["discovery"]["unassigned"]
        assert result["discovery"]["unassigned"][0]["reason"] == "session_cwd_not_registered"
        by_project = {item["project_id"]: item for item in result["projects"]}
        assert by_project["project-a"]["status"] == "partial"
        assert by_project["project-b"]["status"] == "success"
        assert len(service.recent_project_state("project-a")["episodes"]) == 1
        assert len(service.recent_project_state("project-b")["episodes"]) == 1
        partial_sources = service.codex_sampler_state("project-a")["sources"]
        assert partial_sources[0]["status"] == "partial"
        assert partial_sources[0]["last_batch"]["parse_errors"]
    finally:
        service.close()


def test_g1_monitor_lifecycle_start_wake_stop_is_bounded_and_consistent() -> None:
    """The control-plane monitor has one thread, one poll fence and a clean stop."""

    calls: list[int] = []
    first_poll = threading.Event()

    def poll() -> dict[str, int | str]:
        calls.append(1)
        first_poll.set()
        return {"status": "success", "processed_count": 0}

    monitor = CodexActivityMonitor(poll, interval_seconds=1.0)
    assert monitor.state()["status"] == "stopped"
    assert monitor.start() is True
    running = monitor.state()
    assert running["running"] is True
    assert running["status"] == "running"
    assert monitor.start() is False
    assert first_poll.wait(2.0)

    # wake is an explicit bounded prompt, not a second worker or a new AP tick
    # source.  It may cause one additional empty poll, but never overlaps it.
    before_wake = len(calls)
    monitor.wake()
    deadline = time.monotonic() + 2.0
    while len(calls) <= before_wake and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) >= before_wake

    assert monitor.stop(timeout=2.0) is True
    stopped = monitor.state()
    assert stopped["running"] is False
    assert stopped["status"] == "stopped"
    after_stop = len(calls)
    monitor.wake()
    time.sleep(0.1)
    assert len(calls) == after_stop
    assert monitor.stop(timeout=0.1) is False


def test_g1_monitor_preserves_success_cache_and_surfaces_partial_poll() -> None:
    """A partial poll degrades the projection without erasing last success."""

    results = iter(
        (
            {"status": "success", "processed_count": 1, "marker": "good"},
            {
                "status": "partial",
                "processed_count": 0,
                "original_code": "codex_source_partial",
                "retryable": True,
                "next_action": "retry_source",
            },
        )
    )
    monitor = CodexActivityMonitor(lambda: next(results), interval_seconds=1.0)

    first = monitor.poll_once()
    assert first["status"] == "success"
    success = monitor.state()
    assert success["status"] == "stopped"
    assert success["last_success_at"]
    assert success["last_result"]["marker"] == "good"

    second = monitor.poll_once()
    degraded = monitor.state()
    assert second["status"] == "partial"
    assert degraded["status"] == "degraded"
    assert degraded["last_error"]["original_code"] == "codex_source_partial"
    assert degraded["last_error"]["retryable"] is True
    assert degraded["last_error"]["next_action"] == "retry_source"
    assert degraded["last_success_at"] == success["last_success_at"]
    assert degraded["last_result"]["status"] == "partial"


def test_g1_monitor_cold_restart_keeps_cursor_and_missing_source_receipt(
    tmp_path: Path,
) -> None:
    """A service rebuild resumes the same source instead of replaying history."""

    project = tmp_path / "project"
    sessions = tmp_path / "sessions"
    project.mkdir()
    sessions.mkdir()

    def event(timestamp: str, outer: str, payload: dict) -> str:
        return json.dumps(
            {"timestamp": timestamp, "type": outer, "payload": payload},
            ensure_ascii=False,
        ) + "\n"

    source = sessions / "session.jsonl"
    source.write_text(
        event("2026-09-04T04:00:00Z", "session_meta", {"id": "cold", "cwd": str(project)})
        + event(
            "2026-09-04T04:00:01Z",
            "response_item",
            {
                "type": "message",
                "id": "first",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "第一条可见进度"}],
            },
        ),
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"

    first = StudioEpisodeService(
        data_dir,
        project_root=project,
        logic_root=project,
        codex_project_id="project",
        codex_sessions_root=sessions,
    )
    try:
        initial = first.poll_codex_sources()
        assert initial["processed_count"] == 1
        initial_cursor = first.codex_sampler_state("project")["cursor"]
        assert initial_cursor == source.stat().st_size
        initial_episode_count = len(first.recent_project_state("project")["episodes"])
    finally:
        first.close()

    second = StudioEpisodeService(
        data_dir,
        project_root=project,
        logic_root=project,
        codex_project_id="project",
        codex_sessions_root=sessions,
    )
    try:
        resumed = second.poll_codex_sources()
        assert resumed["processed_count"] == 0
        assert resumed["empty_poll"] is True
        assert second.codex_sampler_state("project")["cursor"] == initial_cursor
        assert len(second.recent_project_state("project")["episodes"]) == initial_episode_count

        with source.open("a", encoding="utf-8") as handle:
            handle.write(
                event(
                    "2026-09-04T04:00:02Z",
                    "response_item",
                    {
                        "type": "message",
                        "id": "second",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "追加的一条进度"}],
                    },
                )
            )
        appended = second.poll_codex_sources()
        assert appended["processed_count"] == 1
        assert len(second.recent_project_state("project")["episodes"]) == initial_episode_count + 1

        source.unlink()
        missing = second.sync_codex_activity("project")
        assert missing["status"] == "partial"
        assert missing["sources"][0]["status"] == "failed"
        sampler = second.codex_sampler_state("project")["sources"][0]
        assert sampler["status"] == "unavailable"
        assert sampler["last_error"]["original_code"] == "codex_source_file_not_found"
        assert sampler["last_error"]["retryable"] is True
        assert sampler["last_error"]["next_action"]
    finally:
        second.close()


def test_g1_same_path_session_rotation_resets_cursor_before_new_prefix(
    tmp_path: Path,
) -> None:
    """A larger replacement session at the same path cannot skip its prefix."""

    project = tmp_path / "project"
    sessions = tmp_path / "sessions"
    project.mkdir()
    sessions.mkdir()

    def event(timestamp: str, outer: str, payload: dict) -> str:
        return json.dumps(
            {"timestamp": timestamp, "type": outer, "payload": payload},
            ensure_ascii=False,
        ) + "\n"

    source = sessions / "rotating.jsonl"
    source.write_text(
        event("2026-09-05T05:00:00Z", "session_meta", {"id": "old", "cwd": str(project)})
        + event(
            "2026-09-05T05:00:01Z",
            "response_item",
            {
                "type": "message",
                "id": "old-message",
                "role": "user",
                "content": [{"type": "input_text", "text": "旧会话"}],
            },
        ),
        encoding="utf-8",
    )
    service = StudioEpisodeService(
        tmp_path / "data",
        project_root=project,
        logic_root=project,
        codex_project_id="project",
        codex_sessions_root=sessions,
    )
    try:
        first = service.poll_codex_sources()
        assert first["processed_count"] == 1
        old_cursor = service.codex_sampler_state("project")["cursor"]
        assert old_cursor == source.stat().st_size

        source.write_text(
            event("2026-09-05T05:01:00Z", "session_meta", {"id": "new", "cwd": str(project)})
            + event(
                "2026-09-05T05:01:01Z",
                "response_item",
                {
                    "type": "message",
                    "id": "new-message-1",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "新会话前缀"}],
                },
            )
            + event(
                "2026-09-05T05:01:02Z",
                "response_item",
                {
                    "type": "message",
                    "id": "new-message-2",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "新会话尾部"}],
                },
            ),
            encoding="utf-8",
        )
        rotated = service.poll_codex_sources()
        assert rotated["processed_count"] == 2
        processed = rotated["projects"][0]["processed"]
        assert [item["summary"] for item in processed] == ["新会话前缀", "新会话尾部"]
        assert service.codex_sampler_state("project")["cursor"] == source.stat().st_size
        assert len(service.recent_project_state("project")["episodes"]) == 3
    finally:
        service.close()


def test_g1_explicit_cross_workspace_binding_is_identity_bound_and_disable_is_sticky(
    tmp_path: Path,
) -> None:
    """A selected session may be bound across cwd roots, but only by identity."""

    project = tmp_path / "project"
    session_cwd = tmp_path / "separate-worktree"
    sessions = tmp_path / "sessions"
    project.mkdir()
    session_cwd.mkdir()
    sessions.mkdir()
    source = sessions / "selected.jsonl"

    def event(outer: str, payload: dict) -> str:
        return json.dumps({"timestamp": "2026-09-05T06:00:00Z", "type": outer, "payload": payload}) + "\n"

    source.write_text(
        event("session_meta", {"id": "selected", "cwd": str(session_cwd)})
        + event("response_item", {"type": "message", "id": "selected-1", "role": "user", "content": [{"type": "input_text", "text": "selected"}]}),
        encoding="utf-8",
    )
    service = StudioEpisodeService(
        tmp_path / "data",
        project_root=project,
        codex_project_id="project",
        codex_sessions_root=sessions,
    )
    try:
        discovered = service.discover_codex_sources()
        session = discovered["report"]["sessions"][0]
        bound = service.bind_codex_source({
            "project_id": "project",
            "source_key": session["source_key"],
            "session_id": "selected",
            "enabled": True,
        })
        assert bound["source"]["binding_kind"] == "explicit_session"
        assert service.sync_codex_activity("project")["processed_count"] == 1

        with pytest.raises(ContractError, match="codex_binding_identity_changed"):
            service.bind_codex_source({
                "project_id": "project",
                "source_key": session["source_key"],
                "session_id": "wrong-session",
                "enabled": False,
            })

        disabled = service.bind_codex_source({
            "project_id": "project",
            "source_key": session["source_key"],
            "session_id": "selected",
            "enabled": False,
        })
        assert disabled["source"]["status"] == "disabled"
        result = service.poll_codex_sources()
        assert result["processed_count"] == 0
        assert service.product_registry.source(session["source_key"]).status == "disabled"
    finally:
        service.close()


def test_g1_codex_overview_resolves_legacy_redacted_source_prefix(tmp_path: Path, monkeypatch) -> None:
    """Historical 16-character refs still map to one source without guessing."""

    from ap_mind.codex_activity import CodexJsonlReceptor
    from dataclasses import replace
    original = CodexJsonlReceptor._visible
    def legacy(self, *args, **kwargs):
        item = original(self, *args, **kwargs)
        if item is None:
            return None
        prefix, key = item.source_ref.split("?source=")
        return replace(item, source_ref=prefix + "?source=" + key[:16] + "#" + key.split("#", 1)[1])
    monkeypatch.setattr(CodexJsonlReceptor, "_visible", legacy)

    project = tmp_path / "project"
    sessions = tmp_path / "sessions"
    project.mkdir()
    sessions.mkdir()
    source = sessions / "overview.jsonl"

    def event(timestamp: str, outer: str, payload: dict) -> str:
        return json.dumps(
            {"timestamp": timestamp, "type": outer, "payload": payload},
            ensure_ascii=False,
        ) + "\n"

    source.write_text(
        event("2026-09-05T06:00:00Z", "session_meta", {"id": "overview", "cwd": str(project)})
        + event(
            "2026-09-05T06:00:01Z",
            "response_item",
            {
                "type": "message",
                "id": "overview-user",
                "role": "user",
                "content": [{"type": "input_text", "text": "检查首页会话标题和历史上下文"}],
            },
        )
        + event(
            "2026-09-05T06:00:02Z",
            "response_item",
            {
                "type": "message",
                "id": "overview-assistant",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "已记录标题来源并保留可见历史。"}],
            },
        ),
        encoding="utf-8",
    )
    service = StudioEpisodeService(
        tmp_path / "data",
        project_root=project,
        codex_project_id="project",
        codex_sessions_root=sessions,
    )
    try:
        service.discover_codex_sources()
        assert service.sync_codex_activity("project")["processed_count"] == 2
        overview = service.codex_overview("project")
        session = overview["sessions"][0]
        assert session["message_count"] == 2
        assert session["title"] == "检查首页会话标题和历史上下文"
        assert session["title_source"] == "首条可见用户消息"
        assert [item["role"] for item in session["messages"]] == ["user", "assistant"]
        index = sessions.parent / "session_index.jsonl"
        index.write_text(json.dumps({"id": "overview", "thread_name": "支付回调修复"}) + "\n", encoding="utf-8")
        titled = service.codex_overview("project")["sessions"][0]
        assert titled["title"] == "支付回调修复"
        assert titled["title_source"] == "Codex 会话标题"
        with index.open("a", encoding="utf-8") as handle:
            handle.write('invalid json\n' + json.dumps({"id": "overview", "thread_name": "支付回调修复已完成"}) + "\n")
        assert service.codex_overview("project")["sessions"][0]["title"] == "支付回调修复已完成"
        key = "a" * 64
        assert service._resolve_codex_source_key(key[:16], {key: None}) == key
        assert service._resolve_codex_source_key(key[:16], {key: None, "a" * 63 + "b": None}) is None
        assert service._resolve_codex_source_key("a" * 8, {key: None}) is None

    finally:
        service.close()
