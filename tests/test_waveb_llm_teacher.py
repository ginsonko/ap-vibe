from __future__ import annotations

import json
from pathlib import Path

from ap_mind.contracts import CapabilityOwnership
from ap_mind.gateway import GatewayCallReceipt, OpenAICompatibleGateway
from ap_mind.governance import GovernanceCompatibilityRecord
from ap_mind.studio_server import HYBRID_WHITEPAPER_SHA256, StudioEpisodeService
from ap_mind.vibe_mind import ProjectActivity


class StructuredTeacherTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request_json(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        context = json.loads(kwargs["body"]["messages"][1]["content"])["frame_view"]
        event_ref = context["sa"]["event_ref"]
        recall_ref = (context.get("b_recall") or [{}])[0].get("ref", "not-observed")
        action_ref = context["action_candidates"][0]["candidate_id"]
        payload = {
            "status": "proposal",
            "recall_candidates": [
                {
                    "memory_ref": recall_ref,
                    "summary": "与当前工程活动相关的最近观察",
                    "relevance": 0.72,
                    "rationale": "同一来源链",
                }
            ],
            "prediction_candidates": [
                {
                    "content": "如果证据仍不完整，下一步可能需要追问",
                    "confidence": 0.68,
                    "uncertainty": 0.32,
                    "evidence_refs": [event_ref],
                    "completeness": "partial",
                },
                {
                    "content": "这条建议故意引用不存在的证据",
                    "confidence": 0.9,
                    "uncertainty": 0.1,
                    "evidence_refs": ["invented-evidence"],
                    "completeness": "complete",
                },
            ],
            "appraisal_candidates": [
                {
                    "name": "task_open",
                    "intensity": 0.7,
                    "valence": -0.1,
                    "rationale": "活动仍有未闭合项",
                    "subject_scope": "private",
                    "source_refs": [event_ref],
                }
            ],
            "thought_candidates": [
                {
                    "content": "先保留事实边界，再决定追问或暂存",
                    "evidence_refs": [event_ref],
                    "uncertainty": 0.25,
                    "unresolved": ["缺少独立 readback"],
                }
            ],
            "candidate_preferences": {action_ref: 0.2, "invented-action": 1.0},
            "selected_candidate_ref": action_ref,
            "lesson_candidates": [
                {
                    "capability": "project_evidence_triage",
                    "trigger_features": {"completeness": "partial"},
                    "suggested_adjustment": {"prefer": "ask_user"},
                    "counterexamples": ["完整 readback 已存在时无需追问"],
                    "confidence": 0.76,
                }
            ],
            "uncertainty": 0.28,
            "limitations": ["teacher_advice_is_not_external_evidence"],
            "future_provider_field": {"schema": 2},
        }
        return {
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
        }


def _governance() -> GovernanceCompatibilityRecord:
    return GovernanceCompatibilityRecord(
        record_id="b3-test",
        project_id="ap-vibe-local",
        whitepaper_sha256=HYBRID_WHITEPAPER_SHA256,
        compatibility="compatible",
        llm_delegation_enabled=True,
        authority_sources={"whitepaper": HYBRID_WHITEPAPER_SHA256},
    )


def _capability() -> CapabilityOwnership:
    return CapabilityOwnership(
        capability_key="hybrid.cognition",
        stage="assisted",
        decision_owner="ap_native",
        content_owner="mixed",
        evidence_owner="environment",
        execution_owner="environment",
        llm_allowed=True,
        reason="bounded B3 fixture",
    )


def test_six_teacher_families_are_bounded_persistable_and_source_tagged() -> None:
    transport = StructuredTeacherTransport()
    gateway = OpenAICompatibleGateway(
        "https://provider.invalid/v1",
        "secret-never-in-receipt",
        "gpt-test",
        transport=transport,
        max_retries=0,
    )
    frame = {
        "sa": {"event_ref": "evt-current", "occurrence_id": "sa-current"},
        "b_recall": [{"ref": "evt-memory", "event_ref": "evt-memory"}],
        "c_prediction": [],
        "feelings": [],
        "proposition": {"proposition_id": "prop-current", "source_refs": ["evt-current"]},
        "action_candidates": [{"candidate_id": "act-existing"}],
    }
    proposal = gateway.propose(frame, None)

    assert len(transport.calls) == 1
    assert len(proposal.recall_candidates) == 1
    assert len(proposal.prediction_candidates) == 2
    assert len(proposal.appraisal_candidates) == 1
    assert len(proposal.thought_candidates) == 1
    assert len(proposal.lesson_candidates) == 1
    assert proposal.candidate_preferences == {"act-existing": 0.2}
    assert proposal.selected_candidate_ref == "act-existing"
    assert proposal.recall_candidates[0]["source"] == "llm_model"
    assert proposal.lesson_candidates[0]["model_receipt_ref"] == proposal.call_id
    assert proposal.prediction_candidates[1]["validation"] == "rejected"
    assert "unobserved_evidence_ref" in proposal.prediction_candidates[1]["rejection_reasons"]
    assert any(item["reason"] == "unknown_action_candidate_ref" for item in proposal.validation_issues)
    assert proposal.provider_extensions["future_provider_field"] == {"schema": 2}
    assert transport.calls[0]["body"]["max_tokens"] == 1200

    receipt = GatewayCallReceipt.from_proposal(
        proposal,
        capability_key="hybrid.cognition",
        input_refs=("evt-current",),
    )
    assert receipt is not None
    restored = GatewayCallReceipt.from_dict(receipt.to_dict()).to_proposal()
    assert restored is not None
    assert restored.lesson_candidates == proposal.lesson_candidates
    assert "secret-never-in-receipt" not in json.dumps(receipt.to_dict(), ensure_ascii=False)


def test_gateway_redacts_visible_secret_shapes_before_provider_transport() -> None:
    transport = StructuredTeacherTransport()
    gateway = OpenAICompatibleGateway(
        "https://provider.invalid/v1",
        "transport-secret",
        "gpt-test",
        transport=transport,
        max_retries=0,
    )
    gateway.propose(
        {
            "sa": {
                "event_ref": "evt-redaction",
                "occurrence_id": "sa-redaction",
                "text": "用户误贴了 " + "s" + "k-abcdefghijklmnopqrstuvwx",
            },
            "b_recall": [],
            "c_prediction": [],
            "feelings": [],
            "proposition": {"proposition_id": "prop-redaction", "source_refs": ["evt-redaction"]},
            "action_candidates": [{"candidate_id": "act-redaction"}],
        },
        None,
    )
    outbound = json.dumps(transport.calls[0]["body"], ensure_ascii=False)
    assert "abcdefghijklmnopqrstuvwx" not in outbound
    assert "[REDACTED]" in outbound


def test_service_injects_one_teacher_call_and_projects_adoption(tmp_path: Path) -> None:
    transport = StructuredTeacherTransport()
    gateway = OpenAICompatibleGateway(
        "https://provider.invalid/v1",
        "secret-never-in-ui",
        "gpt-test",
        transport=transport,
        max_retries=0,
    )
    service = StudioEpisodeService(
        tmp_path / "service",
        gateway=gateway,
        governance=_governance(),
        capability=_capability(),
    )
    activity = ProjectActivity(
        activity_id="b3-service-activity",
        project_id="ap-vibe-local",
        kind="progress",
        summary="实现完成，但浏览器回读仍未闭合",
        source_ref="session://b3/current",
        completeness="partial",
        observed_remaining=("浏览器回读",),
    )

    view, replayed = service.run_project_activity("b3-service-request", activity.to_dict())
    replay, replayed_again = service.run_project_activity("b3-service-request", activity.to_dict())

    assert replayed is False and replayed_again is True
    assert replay == view
    assert len(transport.calls) == 1  # result-back and replay never call the paid teacher again
    assert view["teacher"]["candidate_counts"] == {
        "recall": 1,
        "prediction": 2,
        "appraisal": 1,
        "thought": 1,
        "lesson": 1,
        "action": 1,
        "paradigm": 0,
        "attention": 0,
        "expression": 0,
        "parameter": 0,
    }
    families = view["teacher"]["adoption"]["families"]
    assert families["prediction"]["mode"] == "shadow_only"
    assert families["prediction"]["adopted"] == []
    assert len(families["action"]["adopted"]) == 1
    assert families["action"]["adopted"][0]["candidate_ref"] in {
        item["candidate_id"] for item in view["ticks"][0]["actions"]
    }
    assert view["ticks"][0]["decision"]["owner"] == "ap_native"
    assert view["teacher"]["call_receipt"]["usage"]["total_tokens"] == 200
    assert "secret-never-in-ui" not in json.dumps(view, ensure_ascii=False)
    assert service.health().to_dict()["provider"] == {
        "configured": True,
        "status": "ready",
        "provider": "openai-compatible",
        "model": "gpt-test",
        "advisor_role": "cognition",
        "max_retries": 0,
        "max_output_tokens": 1200,
    }


def test_provider_off_keeps_the_same_project_episode_chain(tmp_path: Path) -> None:
    service = StudioEpisodeService(tmp_path / "provider-off")
    activity = ProjectActivity(
        activity_id="provider-off-activity",
        project_id="ap-vibe-local",
        kind="progress",
        summary="本地链路继续运行",
        source_ref="session://provider-off/current",
    )
    view, replayed = service.run_project_activity("provider-off-request", activity.to_dict())

    assert replayed is False
    assert view["ticks"]
    assert view["selected_action"] is not None
    assert view["teacher"]["mode"] == "provider_off"
    assert view["teacher"]["candidate_counts"] == {
        "recall": 0,
        "prediction": 0,
        "appraisal": 0,
        "thought": 0,
        "lesson": 0,
        "action": 0,
        "paradigm": 0,
        "attention": 0,
        "expression": 0,
        "parameter": 0,
    }
    assert service.health().to_dict()["mode"] == "local_provider_off"
