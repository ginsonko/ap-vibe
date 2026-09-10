from __future__ import annotations

from pathlib import Path

import pytest

from ap_mind.contracts import ContractError
from ap_mind.gateway import GatewayTransportError
from ap_mind.knowledge_correction import (
    KnowledgeCorrectionConflict,
    KnowledgeCorrectionRequest,
    OpenAICompatibleKnowledgeCorrectionInterpreter,
)
from ap_mind.studio_server import StudioEpisodeService, StudioRequestConflict


PROJECT = "project-local"


def _stage_base(service: StudioEpisodeService):
    activity, _ = service.run_project_activity(
        "d4-base-activity-request",
        {
            "activity_id": "d4-base-activity",
            "project_id": PROJECT,
            "kind": "project_checkpoint",
            "summary": "D4 之前的已审项目基线",
            "source_ref": "session://d4/base",
            "completeness": "complete",
            "observed_completed": ["D3 已完成"],
            "observed_remaining": ["完成 D4 知识纠正"],
            "observed_redlines": ["不得读取 APV3 受保护数据库"],
            "observed_next_action": "实现 D4",
        },
    )
    proposal = activity["knowledge_candidates"][0]
    review, _ = service.run_project_knowledge_review(
        "d4-base-review-request",
        {
            "decision_id": "d4-base-decision",
            "project_id": PROJECT,
            "proposal_id": proposal["proposal_id"],
            "source_ref": "user://d4/base-review",
            "expected_parent_revision": None,
        },
    )
    return review["knowledge_revision"]


def _request(parent_id: str, *, changes=()):
    return {
        "project_id": PROJECT,
        "instruction": "保留旧记录，但把下一步改成完成 D4 页面验收。",
        "expected_parent_revision_id": parent_id,
        "explicit_changes": list(changes),
        "source_refs": ["user://d4/correction"],
        "evidence_refs": ["session://d4/base"],
    }


def _replace_next_action():
    return {
        "section": "work",
        "path": "/next_action",
        "operation": "replace_exact",
        "before": "实现 D4",
        "after": "完成 D4 页面验收",
        "rationale": "用户更新了下一原子动作，旧 revision 仍保留",
        "applicability": {"scope": "AP-Vibe D4"},
        "counterexamples": ["不适用于已经完成页面验收后的版本"],
        "source_refs": ["user://d4/correction"],
        "evidence_refs": ["session://d4/base"],
        "confidence": 1.0,
        "uncertainty": 0.0,
        "completeness": "complete",
    }


def test_provider_off_preview_is_honest_and_does_not_write_revision(tmp_path: Path) -> None:
    service = StudioEpisodeService(tmp_path / "service", codex_project_id=PROJECT)
    parent = _stage_base(service)
    before = service.local_recovery()

    draft, replayed = service.preview_knowledge_correction(
        "d4-prose-only",
        _request(parent["revision_id"]),
    )

    assert replayed is False
    assert draft["instruction"].startswith("保留旧记录")
    assert draft["interpretation_source"] == "provider_off"
    assert draft["proposal_incomplete"] is True
    assert draft["changes"] == []
    assert draft["write_state"] == "preview_only"
    after = service.local_recovery()
    assert after["revision_count"] == before["revision_count"] == 1
    assert after["milestone"]["content_hash"] == before["milestone"]["content_hash"]


def test_explicit_preview_then_confirm_uses_ap_readback_and_is_idempotent(tmp_path: Path) -> None:
    data_dir = tmp_path / "service"
    service = StudioEpisodeService(data_dir, codex_project_id=PROJECT)
    parent = _stage_base(service)
    request = _request(parent["revision_id"], changes=(_replace_next_action(),))

    draft, replayed = service.preview_knowledge_correction("d4-preview", request)
    assert replayed is False
    assert draft["proposal_incomplete"] is False
    assert draft["preview_sections"]["work"]["next_action"] == "完成 D4 页面验收"
    assert service.local_recovery()["revision_count"] == 1

    replay, replayed = service.preview_knowledge_correction("d4-preview", request)
    assert replayed is True
    assert replay == draft
    with pytest.raises(KnowledgeCorrectionConflict):
        service.preview_knowledge_correction(
            "d4-preview",
            {**request, "instruction": "同一个 request_id 被用于另一条纠正"},
        )

    confirmation = {
        "decision_id": "d4-confirm-decision",
        "project_id": PROJECT,
        "draft_id": draft["draft_id"],
        "source_ref": "user://d4/confirm",
        "rationale": "用户确认应用预览中的精确变更",
        "expected_parent_revision_id": parent["revision_id"],
    }
    confirmed, replayed = service.confirm_knowledge_correction("d4-confirm", confirmation)
    assert replayed is False
    assert confirmed["episode_kind"] == "knowledge_correction_confirm"
    assert confirmed["selected_action"]["kind"] == "commit_local_milestone"
    assert len(confirmed["ticks"]) == 2
    assert confirmed["ticks"][0]["decision"]["result_status"] == "success"
    assert confirmed["knowledge_revision"]["revision_number"] == 2
    assert confirmed["knowledge_revision"]["sections"]["work"]["next_action"] == "完成 D4 页面验收"

    again, replayed = service.confirm_knowledge_correction("d4-confirm", confirmation)
    assert replayed is True
    assert again == confirmed
    assert service.local_recovery()["revision_count"] == 2

    cold = StudioEpisodeService(data_dir, codex_project_id=PROJECT)
    recovered = cold.local_recovery()
    assert recovered["valid"] is True
    assert recovered["revision_count"] == 2
    assert recovered["milestone"]["sections"]["work"]["completed"] == ["D3 已完成"]
    assert recovered["milestone"]["sections"]["work"]["next_action"] == "完成 D4 页面验收"


def test_stale_or_inexact_correction_never_overwrites_current_revision(tmp_path: Path) -> None:
    service = StudioEpisodeService(tmp_path / "service", codex_project_id=PROJECT)
    parent = _stage_base(service)
    wrong = _replace_next_action()
    wrong["before"] = "看起来相似但并不完全相同"

    with pytest.raises(KnowledgeCorrectionConflict, match="knowledge_change_before_mismatch"):
        service.preview_knowledge_correction(
            "d4-inexact-preview",
            _request(parent["revision_id"], changes=(wrong,)),
        )
    assert service.local_recovery()["revision_count"] == 1

    draft, _ = service.preview_knowledge_correction(
        "d4-stale-preview",
        _request(parent["revision_id"], changes=(_replace_next_action(),)),
    )
    other, _ = service.run_project_activity(
        "d4-other-activity-request",
        {
            "activity_id": "d4-other-activity",
            "project_id": PROJECT,
            "kind": "project_checkpoint",
            "summary": "另一条真实进度先成为新基线",
            "source_ref": "session://d4/other",
            "completeness": "complete",
        },
    )
    other_proposal = other["knowledge_candidates"][0]
    service.run_project_knowledge_review(
        "d4-other-review-request",
        {
            "decision_id": "d4-other-decision",
            "project_id": PROJECT,
            "proposal_id": other_proposal["proposal_id"],
            "source_ref": "user://d4/other-review",
            "expected_parent_revision": parent["revision_id"],
        },
    )
    latest_before = service.local_recovery()["milestone"]

    with pytest.raises(KnowledgeCorrectionConflict, match="knowledge_correction_parent_revision_changed"):
        service.confirm_knowledge_correction(
            "d4-stale-confirm",
            {
                "decision_id": "d4-stale-decision",
                "project_id": PROJECT,
                "draft_id": draft["draft_id"],
                "source_ref": "user://d4/stale-confirm",
                "expected_parent_revision_id": parent["revision_id"],
            },
        )
    latest_after = service.local_recovery()["milestone"]
    assert latest_after["revision_id"] == latest_before["revision_id"]
    assert latest_after["content_hash"] == latest_before["content_hash"]


def test_confirm_contract_rejects_browser_supplied_change_body(tmp_path: Path) -> None:
    service = StudioEpisodeService(tmp_path / "service", codex_project_id=PROJECT)
    parent = _stage_base(service)
    draft, _ = service.preview_knowledge_correction(
        "d4-tamper-preview",
        _request(parent["revision_id"], changes=(_replace_next_action(),)),
    )
    with pytest.raises(ContractError, match="knowledge_correction_confirmation_identity_only"):
        service.confirm_knowledge_correction(
            "d4-tamper-confirm",
            {
                "decision_id": "d4-tamper-decision",
                "project_id": PROJECT,
                "draft_id": draft["draft_id"],
                "source_ref": "user://d4/tamper",
                "expected_parent_revision_id": parent["revision_id"],
                "changes": [{"section": "identity", "path": "/authority", "operation": "set", "after": "browser"}],
            },
        )


class _TeacherTransport:
    def __init__(self, content=None, failure: str | None = None):
        self.content = content
        self.failure = failure
        self.calls = 0
        self.last_headers = None

    def request_json(self, method, url, *, body, headers, timeout, max_response_bytes):
        del method, url, body, timeout, max_response_bytes
        self.calls += 1
        self.last_headers = dict(headers)
        if self.failure:
            raise GatewayTransportError(self.failure)
        return {
            "choices": [{"message": {"content": self.content}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        }


def test_teacher_can_propose_but_invalid_before_is_rejected(tmp_path: Path) -> None:
    service = StudioEpisodeService(tmp_path / "base", codex_project_id=PROJECT)
    parent_raw = _stage_base(service)
    parent = service.knowledge_store.latest(PROJECT)
    assert parent is not None
    transport = _TeacherTransport(
        content='{"status":"proposal","changes":[{"section":"work","path":"/next_action",'
        '"operation":"replace_exact","before":"模型猜错的旧值","after":"完成页面验收",'
        '"before_present":true,"rationale":"教师候选","applicability":{},"counterexamples":[],'
        '"confidence":0.9,"uncertainty":0.1,"completeness":"complete"}],"limitations":[]}'
    )
    interpreter = OpenAICompatibleKnowledgeCorrectionInterpreter(
        "https://provider.invalid/v1",
        "secret-used-only-in-header",
        "teacher-model",
        transport=transport,
    )
    interpretation = interpreter.interpret(
        KnowledgeCorrectionRequest(
            project_id=PROJECT,
            instruction="请修正下一步",
            expected_parent_revision_id=parent_raw["revision_id"],
        ),
        parent,
    )
    assert transport.calls == 1
    assert transport.last_headers["Authorization"] == "Bearer secret-used-only-in-header"
    assert interpretation.changes == ()
    assert interpretation.proposal_incomplete is True
    assert any("knowledge_change_before_mismatch" in item for item in interpretation.limitations)
    assert interpretation.model_receipt["model"] == "teacher-model"


def test_teacher_failure_becomes_incomplete_without_blocking_explicit_fallback(tmp_path: Path) -> None:
    service = StudioEpisodeService(tmp_path / "base", codex_project_id=PROJECT)
    parent_raw = _stage_base(service)
    parent = service.knowledge_store.latest(PROJECT)
    transport = _TeacherTransport(failure="provider_http_503")
    interpreter = OpenAICompatibleKnowledgeCorrectionInterpreter(
        "https://provider.invalid/v1", "secret", "teacher-model", transport=transport
    )
    failed = interpreter.interpret(
        KnowledgeCorrectionRequest(
            project_id=PROJECT,
            instruction="请修正下一步",
            expected_parent_revision_id=parent_raw["revision_id"],
        ),
        parent,
    )
    assert failed.proposal_incomplete is True
    assert failed.changes == ()
    assert failed.model_receipt["status"] == "failed"

    explicit = interpreter.interpret(
        KnowledgeCorrectionRequest(
            project_id=PROJECT,
            instruction="我已经明确填写字段",
            expected_parent_revision_id=parent_raw["revision_id"],
            explicit_changes=(
                __import__("ap_mind").KnowledgeSectionChange.from_dict(_replace_next_action()),
            ),
        ),
        parent,
    )
    assert explicit.proposal_incomplete is False
    assert len(explicit.changes) == 1
    assert transport.calls == 1
