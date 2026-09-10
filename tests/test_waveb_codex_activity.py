from pathlib import Path
import json

from ap_mind.codex_activity import CodexJsonlReceptor
from ap_mind.studio_server import StudioEpisodeService


def _line(timestamp: str, outer: str, payload: dict) -> bytes:
    return (json.dumps({"timestamp": timestamp, "type": outer, "payload": payload}, ensure_ascii=False) + "\n").encode("utf-8")


def test_receptor_keeps_only_visible_messages_and_redacts_secrets(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    source.write_bytes(
        b"".join(
            (
                _line("2026-09-03T00:00:00Z", "response_item", {"type": "reasoning", "summary": [{"text": "hidden"}], "encrypted_content": "secret"}),
                _line("2026-09-03T00:00:01Z", "response_item", {"type": "custom_tool_call", "name": "exec", "input": '{"api_key":"' + "s" + "k-tool-secret-123456" + '"}'}),
                _line("2026-09-03T00:00:02Z", "response_item", {"type": "message", "id": "user-1", "role": "user", "content": [{"type": "input_text", "text": "请继续整理项目，key=" + "s" + "k-user-secret-123456789"}]}),
                _line("2026-09-03T00:00:03Z", "response_item", {"type": "message", "id": "assistant-1", "role": "assistant", "content": [{"type": "output_text", "text": "完成了一段可见进度，但仍缺运行收据。"}]}),
                _line("2026-09-03T00:00:04Z", "response_item", {"type": "custom_tool_call_output", "output": "s" + "k-output-secret-123456"}),
            )
        )
    )

    batch = CodexJsonlReceptor(source, max_bytes=16_384).sample(cursor=0)

    assert [item.role for item in batch.occurrences] == ["user", "assistant"]
    visible = "\n".join(item.text for item in batch.occurrences)
    assert "[REDACTED]" in visible
    assert "secret-123456" not in visible
    assert "hidden" not in visible
    assert batch.commit_offset == source.stat().st_size
    activity = batch.occurrences[1].as_activity("project-local")
    assert activity.actor == "codex"
    assert activity.observed_unknown
    assert activity.extra["source_is_untrusted_observation"] is True
    assert activity.source_ref.startswith(f"codex-jsonl://{source.name}?source=")
    assert str(source.parent) not in activity.source_ref
    assert activity.source_ref.startswith(f"codex-jsonl://{source.name}?source=")
    assert str(source.parent) not in activity.source_ref


def test_receptor_cursor_reads_only_append_and_preserves_partial_tail(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    first_line = _line("2026-09-03T00:00:00Z", "response_item", {"type": "message", "id": "a", "role": "assistant", "content": [{"type": "output_text", "text": "第一条"}]})
    source.write_bytes(first_line)
    receptor = CodexJsonlReceptor(source, max_bytes=16_384)
    first = receptor.sample(cursor=0)
    assert [item.text for item in first.occurrences] == ["第一条"]

    complete = _line("2026-09-03T00:00:01Z", "response_item", {"type": "message", "id": "b", "role": "assistant", "content": [{"type": "output_text", "text": "第二条"}]})
    partial = json.dumps({"timestamp": "2026-09-03T00:00:02Z", "type": "response_item", "payload": {"type": "message"}}, ensure_ascii=False).encode("utf-8")
    with source.open("ab") as handle:
        handle.write(complete)
        handle.write(partial)
    second = receptor.sample(cursor=first.commit_offset)
    assert [item.text for item in second.occurrences] == ["第二条"]
    assert "trailing_partial_line" in second.warnings
    assert second.commit_offset == len(first_line) + len(complete)

    with source.open("ab") as handle:
        handle.write(b"\n")
    third = receptor.sample(cursor=second.commit_offset)
    assert not third.occurrences
    assert third.commit_offset == source.stat().st_size


def test_receptor_initial_tail_and_event_budget_are_explicit(tmp_path: Path) -> None:
    source = tmp_path / "rollout.jsonl"
    source.write_bytes(
        b"".join(
            _line("2026-09-03T00:00:00Z", "response_item", {"type": "message", "id": str(index), "role": "assistant", "content": [{"type": "output_text", "text": f"消息 {index}"}]})
            for index in range(12)
        )
    )
    batch = CodexJsonlReceptor(source, max_bytes=1_024, max_events=2).sample()
    assert len(batch.occurrences) <= 2
    assert batch.completeness == "search_incomplete"
    assert "initial_tail_window_only" in batch.warnings
    assert "search_incomplete" in batch.warnings


def test_service_sync_commits_cursor_after_durable_project_episode(tmp_path: Path) -> None:
    source = tmp_path / "live-rollout.jsonl"
    source.write_bytes(
        _line(
            "2026-09-03T00:00:00Z",
            "response_item",
            {
                "type": "message",
                "id": "sync-a",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "完成了投影，但还需要浏览器回读。"}],
            },
        )
    )
    data_dir = tmp_path / "service"
    service = StudioEpisodeService(
        data_dir,
        codex_source=source,
        codex_project_id="project-local",
    )

    first = service.sync_codex_activity()
    assert first["processed_count"] == 1
    assert first["processed"][0]["role"] == "assistant"
    assert first["cursor_committed"] == source.stat().st_size
    assert service.codex_sampler_state()["cursor"] == source.stat().st_size
    state = service.recent_project_state()
    assert state["episodes"][0]["episode"]["input"]["activity"]["actor"] == "codex"
    assert state["codex_sampler"]["source_name"] == source.name
    assert str(source.parent) not in json.dumps(state, ensure_ascii=False)
    assert state["codex_sampler"]["source_name"] == source.name
    assert str(source.parent) not in json.dumps(state, ensure_ascii=False)

    replay = StudioEpisodeService(
        data_dir,
        codex_source=source,
        codex_project_id="project-local",
    ).sync_codex_activity()
    assert replay["processed_count"] == 0

    with source.open("ab") as handle:
        handle.write(
            _line(
                "2026-09-03T00:00:01Z",
                "response_item",
                {
                    "type": "message",
                    "id": "sync-b",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "请继续当前任务。"}],
                },
            )
        )
    appended = StudioEpisodeService(
        data_dir,
        codex_source=source,
        codex_project_id="project-local",
    ).sync_codex_activity()
    assert appended["processed_count"] == 1
    assert appended["processed"][0]["role"] == "user"
    assert len(StudioEpisodeService(data_dir).recent_project_state()["episodes"]) == 2
