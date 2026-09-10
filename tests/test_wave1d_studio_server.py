from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ap_mind import StudioEpisodeRequest, StudioEpisodeService, StudioRequestConflict
from ap_mind.studio_server import create_server


def test_service_idempotency_and_conflict(tmp_path: Path) -> None:
    service = StudioEpisodeService(tmp_path / "service")
    request = StudioEpisodeRequest(
        request_id="service-idempotency",
        proposition_text="甲丙",
        surface_text="甲乙",
    )
    first, replayed_first = service.run(request)
    second, replayed_second = service.run(request)
    assert replayed_first is False
    assert replayed_second is True
    assert second == first
    assert first["physical_output"] == "甲丙"
    with pytest.raises(StudioRequestConflict):
        service.run(
            StudioEpisodeRequest(
                request_id="service-idempotency",
                proposition_text="不同输入",
            )
        )


def _json_request(url: str, method: str = "GET", payload: dict | None = None):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib_request.Request(
        url,
        data=raw,
        method=method,
        headers={"Content-Type": "application/json"} if raw is not None else {},
    )
    with urllib_request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8")), dict(response.headers)


def test_loopback_http_health_episode_replay_and_conflict(tmp_path: Path) -> None:
    server = create_server(port=0, data_dir=tmp_path / "http-data")
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, health, _ = _json_request(base + "/v1/health")
        assert status == 200
        assert health["status"] == "ok"
        assert health["mode"] == "local_provider_off"
        assert health["capability_boundary"]["absent"] == [
            "real_llm",
            "real_vlm",
            "vibe_live_control",
            "napcat",
        ]
        assert health["ap_vibe"]["formal_knowledge_write"] is False
        payload = {
            "request_id": "http-episode",
            "proposition_text": "黄色苹果",
            "surface_text": "黄色苹蕉",
            "renderer_enabled": True,
            "annotation_enabled": True,
        }
        status, first, headers = _json_request(base + "/v1/demo/episodes", "POST", payload)
        assert status == 200
        assert first["replayed"] is False
        assert first["episode"]["physical_output"] == "黄色苹果"
        assert headers["X-AP-Mind-Replayed"] == "false"
        _, second, headers = _json_request(base + "/v1/demo/episodes", "POST", payload)
        assert second["replayed"] is True
        assert second["episode"] == first["episode"]
        assert headers["X-AP-Mind-Replayed"] == "true"
        conflict = dict(payload, proposition_text="不同输入")
        with pytest.raises(urllib_error.HTTPError) as exc_info:
            _json_request(base + "/v1/demo/episodes", "POST", conflict)
        assert exc_info.value.code == 409
        body = json.loads(exc_info.value.read().decode("utf-8"))
        assert body["error"]["code"] == "request_id_conflict"
        assert "新的 request_id" in body["error"]["solution"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_ap_vibe_http_activity_feedback_learning_replay_and_state(tmp_path: Path) -> None:
    data_dir = tmp_path / "ap-vibe-http-data"
    server = create_server(port=0, data_dir=data_dir)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        activity_payload = {
            "request_id": "api-activity-before",
            "activity": {
                "activity_id": "api-target",
                "project_id": "project-local",
                "kind": "progress",
                "summary": "只有部分证据的进度",
                "source_ref": "session://api/target",
                "completeness": "partial",
            },
        }
        status, first, headers = _json_request(
            base + "/v1/ap-vibe/activities", "POST", activity_payload
        )
        assert status == 200
        assert first["replayed"] is False
        assert first["episode"]["selected_action"]["kind"] == "stage_knowledge_candidate"
        assert first["episode"]["knowledge_candidates"][0]["formal_knowledge"] is False
        assert headers["X-AP-Mind-Replayed"] == "false"

        _, replay, headers = _json_request(
            base + "/v1/ap-vibe/activities", "POST", activity_payload
        )
        assert replay["replayed"] is True
        assert replay["episode"] == first["episode"]
        assert headers["X-AP-Mind-Replayed"] == "true"

        feedback_payload = {
            "request_id": "api-feedback",
            "feedback": {
                "feedback_id": "api-feedback",
                "project_id": "project-local",
                "target_episode_id": first["episode"]["episode_id"],
                "target_action": "stage_knowledge_candidate",
                "desired_action": "ask_user",
                "signal": "correction",
                "magnitude": 1.0,
                "natural_language": "证据不完整时先问我。",
                "source_ref": "user://api/feedback",
            },
        }
        _, feedback, _ = _json_request(
            base + "/v1/ap-vibe/feedback", "POST", feedback_payload
        )
        assert feedback["episode"]["lesson"]["status"] == "applied"
        assert feedback["episode"]["learning"]["lesson_count"] == 1

        after_payload = {
            "request_id": "api-activity-after",
            "activity": {
                "activity_id": "api-after",
                "project_id": "project-local",
                "kind": "future_unseen_kind",
                "summary": "文本与类别都不同的另一份部分进度",
                "source_ref": "session://api/after",
                "completeness": "partial",
            },
        }
        _, after, _ = _json_request(
            base + "/v1/ap-vibe/activities", "POST", after_payload
        )
        assert after["episode"]["selected_action"]["kind"] == "ask_user"
        assert after["episode"]["learning"]["applied_preferences"] == {
            "ask_user": 0.18,
            "stage_knowledge_candidate": -0.18,
        }

        _, state, _ = _json_request(base + "/v1/ap-vibe/state")
        assert state["last_success"]["episode"]["request_id"] == "api-activity-after"
        assert state["formal_knowledge_write"] is False

        # A new service object must recover the completed request and the same
        # shared learner without redispatching or applying the lesson twice.
        cold = type(server.service)(data_dir)
        replayed_view, replayed = cold.run_project_feedback(
            feedback_payload["request_id"], feedback_payload["feedback"]
        )
        assert replayed is True
        assert replayed_view == feedback["episode"]
        assert cold.learning_ledger.snapshot("project-local")["lesson_count"] == 1

        conflict = json.loads(json.dumps(activity_payload, ensure_ascii=False))
        conflict["activity"]["summary"] = "同 ID 的不同输入"
        with pytest.raises(urllib_error.HTTPError) as exc_info:
            _json_request(base + "/v1/ap-vibe/activities", "POST", conflict)
        assert exc_info.value.code == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_ap_vibe_feedback_requires_a_known_local_target(tmp_path: Path) -> None:
    server = create_server(port=0, data_dir=tmp_path / "missing-target")
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        payload = {
            "request_id": "missing-feedback",
            "feedback": {
                "feedback_id": "missing-feedback",
                "project_id": "project-local",
                "target_episode_id": "not-present",
                "target_action": "stage_knowledge_candidate",
                "desired_action": "ask_user",
                "signal": "correction",
                "magnitude": 1.0,
                "natural_language": "先问我。",
                "source_ref": "user://missing",
            },
        }
        with pytest.raises(urllib_error.HTTPError) as exc_info:
            _json_request(base + "/v1/ap-vibe/feedback", "POST", payload)
        assert exc_info.value.code == 404
        body = json.loads(exc_info.value.read().decode("utf-8"))
        assert body["error"]["code"] == "feedback_target_activity_not_found"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_ap_vibe_http_local_recovery_review_and_brief_routes(tmp_path: Path) -> None:
    server = create_server(
        port=0,
        data_dir=tmp_path / "recovery-http",
        codex_project_id="project-local",
    )
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        activity_payload = {
            "request_id": "recovery-http-activity",
            "activity": {
                "activity_id": "recovery-http-activity",
                "project_id": "project-local",
                "kind": "unseen-recovery-event",
                "summary": "本地恢复接口已经形成候选",
                "source_ref": "session://recovery/http",
                "completeness": "complete",
                "observed_completed": ["完成本地恢复后端"],
                "observed_remaining": ["完成真实页面验收"],
                "observed_redlines": ["不得冒充 Vibe 正式写回"],
                "observed_next_action": "完成真实页面验收",
            },
        }
        _, activity, _ = _json_request(
            base + "/v1/ap-vibe/activities", "POST", activity_payload
        )
        proposal = activity["episode"]["knowledge_candidates"][0]
        review_payload = {
            "request_id": "recovery-http-review",
            "review": {
                "decision_id": "recovery-http-decision",
                "project_id": "project-local",
                "proposal_id": proposal["proposal_id"],
                "source_ref": "user://recovery/http",
                "expected_parent_revision": None,
            },
        }
        _, review, _ = _json_request(
            base + "/v1/ap-vibe/knowledge/commit", "POST", review_payload
        )
        assert review["episode"]["selected_action"]["kind"] == "commit_local_milestone"
        assert len(review["episode"]["ticks"]) == 2

        _, recovery, _ = _json_request(base + "/v1/ap-vibe/recovery")
        assert recovery["valid"] is True
        assert recovery["milestone"]["sections"]["work"]["next_action"] == "完成真实页面验收"
        assert recovery["write_capability"]["vibe_formal_write"] is False

        _, brief, _ = _json_request(
            base + "/v1/ap-vibe/brief",
            "POST",
            {"project_id": "project-local", "goal": "继续恢复验收", "max_chars": 2_000},
        )
        assert "不得冒充 Vibe 正式写回" in brief["text"]
        assert "完成真实页面验收" in brief["text"]
        assert brief["vibe_formal_write"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_ap_vibe_http_knowledge_correction_preview_and_confirm(tmp_path: Path) -> None:
    server = create_server(
        port=0,
        data_dir=tmp_path / "correction-http",
        codex_project_id="project-local",
    )
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        _, activity, _ = _json_request(
            base + "/v1/ap-vibe/activities",
            "POST",
            {
                "request_id": "correction-http-base-activity",
                "activity": {
                    "activity_id": "correction-http-base-activity",
                    "project_id": "project-local",
                    "kind": "project_checkpoint",
                    "summary": "知识纠正 HTTP 基线",
                    "source_ref": "session://correction/http/base",
                    "completeness": "complete",
                    "observed_next_action": "实现纠正接口",
                },
            },
        )
        proposal = activity["episode"]["knowledge_candidates"][0]
        _, base_review, _ = _json_request(
            base + "/v1/ap-vibe/knowledge/commit",
            "POST",
            {
                "request_id": "correction-http-base-review",
                "review": {
                    "decision_id": "correction-http-base-decision",
                    "project_id": "project-local",
                    "proposal_id": proposal["proposal_id"],
                    "source_ref": "user://correction/http/base",
                    "expected_parent_revision": None,
                },
            },
        )
        parent = base_review["episode"]["knowledge_revision"]
        preview_payload = {
            "request_id": "correction-http-preview",
            "correction": {
                "project_id": "project-local",
                "instruction": "把下一步精确改为完成真实页面验收",
                "expected_parent_revision_id": parent["revision_id"],
                "explicit_changes": [
                    {
                        "section": "work",
                        "path": "/next_action",
                        "operation": "replace_exact",
                        "before": "实现纠正接口",
                        "after": "完成真实页面验收",
                        "rationale": "用户核对了修改前后",
                        "before_present": True,
                    }
                ],
                "source_refs": ["user://correction/http/preview"],
            },
        }
        _, preview, headers = _json_request(
            base + "/v1/ap-vibe/knowledge/corrections/preview",
            "POST",
            preview_payload,
        )
        assert preview["status"] == "success"
        assert preview["draft"]["write_state"] == "preview_only"
        assert preview["draft"]["preview_sections"]["work"]["next_action"] == "完成真实页面验收"
        assert headers["X-AP-Mind-Replayed"] == "false"
        _, recovery_before, _ = _json_request(base + "/v1/ap-vibe/recovery")
        assert recovery_before["revision_count"] == 1

        _, confirmed, _ = _json_request(
            base + "/v1/ap-vibe/knowledge/corrections/confirm",
            "POST",
            {
                "request_id": "correction-http-confirm",
                "confirmation": {
                    "decision_id": "correction-http-confirm-decision",
                    "project_id": "project-local",
                    "draft_id": preview["draft"]["draft_id"],
                    "source_ref": "user://correction/http/confirm",
                    "expected_parent_revision_id": parent["revision_id"],
                },
            },
        )
        assert confirmed["episode"]["episode_kind"] == "knowledge_correction_confirm"
        assert len(confirmed["episode"]["ticks"]) == 2
        assert confirmed["episode"]["selected_action"]["kind"] == "commit_local_milestone"
        assert confirmed["episode"]["knowledge_revision"]["revision_number"] == 2

        _, state, _ = _json_request(base + "/v1/ap-vibe/state")
        assert state["knowledge_corrections"][0]["write_state"] == "confirmed"
        assert state["knowledge_reviews"][0]["kind"] == "knowledge_correction_confirm"
        assert state["recovery"]["milestone"]["sections"]["work"]["next_action"] == "完成真实页面验收"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
