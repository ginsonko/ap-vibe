"""Run the Wave E2 prediction/thought growth vertical in an isolated data root.

Only the remote teacher response is replaced by a deterministic fixture.  The
teacher source activity, both curriculum adoption episodes, later provider-off
use, scoped user correction, and cold service recovery all use the normal
AP-Vibe product path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from ap_mind.contracts import CapabilityOwnership
from ap_mind.gateway import OpenAICompatibleGateway
from ap_mind.governance import GovernanceCompatibilityRecord
from ap_mind.studio_server import HYBRID_WHITEPAPER_SHA256, StudioEpisodeService


class WaveE2TeacherFixture:
    """One bounded prediction and one private thought scaffold, without I/O."""

    def __init__(self) -> None:
        self.calls = 0

    def request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        del method, url
        self.calls += 1
        frame_view = json.loads(kwargs["body"]["messages"][1]["content"])["frame_view"]
        event_ref = frame_view["sa"]["event_ref"]
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "status": "proposal",
                                "recall_candidates": [],
                                "prediction_candidates": [
                                    {
                                        "content": "下一步很可能需要读取浏览器中的真实结果，当前结论仍是假设。",
                                        "mode": "forecast",
                                        "confidence": 0.8,
                                        "uncertainty": 0.2,
                                        "evidence_refs": [event_ref],
                                        "completeness": "unknown",
                                    }
                                ],
                                "appraisal_candidates": [],
                                "thought_candidates": [
                                    {
                                        "content": "先区分已经完成的本地链路与尚未观察的浏览器结果。",
                                        "uncertainty": 0.15,
                                        "evidence_refs": [event_ref],
                                        "unresolved": ["浏览器结果尚未观察"],
                                    }
                                ],
                                "candidate_preferences": {},
                                "selected_candidate_ref": None,
                                "lesson_candidates": [],
                                "uncertainty": 0.2,
                                "limitations": [
                                    "fixture_teacher_has_no_external_truth",
                                    "courses_require_later_independent_activity",
                                ],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


def _activity(
    run_id: str,
    suffix: str,
    summary: str,
    *,
    detail: str,
    occurred_at: str,
) -> dict[str, Any]:
    activity_id = f"{run_id}-{suffix}"
    return {
        "activity_id": activity_id,
        "project_id": "ap-vibe-local",
        "kind": f"unseen-e2-{suffix}",
        "summary": summary,
        "detail": detail,
        "source_ref": f"fixture://ap-vibe/e2/{activity_id}",
        "occurred_at": occurred_at,
        "completeness": "partial",
        "observed_remaining": ["浏览器现实回读"],
        "observed_unknown": ["用户是否看到预期效果尚未确认"],
        "observed_next_action": "读取浏览器现实结果",
    }


def _attempt(view: Mapping[str, Any], capability: str) -> Mapping[str, Any]:
    return next(
        item
        for item in view.get("curriculum_attempts", [])
        if item.get("target_capability") == capability
    )


def _maturity(view: Mapping[str, Any], capability: str) -> Mapping[str, Any]:
    return next(
        item
        for item in view.get("learning", {}).get("capability_maturity", [])
        if item.get("target_capability") == capability
    )


def _curriculum(view: Mapping[str, Any], capability: str) -> Mapping[str, Any]:
    return next(
        item["curriculum"]
        for item in view.get("curriculum_episodes", [])
        if item.get("curriculum", {}).get("target_capability") == capability
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--run-id", default="wave-e2-vertical-20260903-001")
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(args.run_id).strip()
    if not run_id:
        raise ValueError("run_id_required")

    transport = WaveE2TeacherFixture()
    gateway = OpenAICompatibleGateway(
        "https://fixture.invalid/v1",
        "fixture-only-not-a-real-key",
        "local-wave-e2-teacher",
        provider="local-fixture",
        transport=transport,
        max_retries=0,
    )
    governance = GovernanceCompatibilityRecord(
        record_id=f"{run_id}-governance",
        project_id="ap-vibe-local",
        whitepaper_sha256=HYBRID_WHITEPAPER_SHA256,
        compatibility="compatible",
        llm_delegation_enabled=True,
        authority_sources={"whitepaper": HYBRID_WHITEPAPER_SHA256},
    )
    capability = CapabilityOwnership(
        capability_key="hybrid.cognition",
        stage="assisted",
        decision_owner="ap_native",
        content_owner="mixed",
        evidence_owner="environment",
        execution_owner="environment",
        llm_allowed=True,
        reason="deterministic Wave E2 teacher fixture; no network or external truth",
    )
    teacher_service = StudioEpisodeService(
        data_dir,
        gateway=gateway,
        governance=governance,
        capability=capability,
    )
    teacher, teacher_replayed = teacher_service.run_project_activity(
        f"{run_id}-teacher-request",
        _activity(
            run_id,
            "teacher-source",
            "恢复工作台仍等待现实页面回读",
            detail="教师只提出待检验预测和私有思考支架；本轮不能追溯改变自己。",
            occurred_at="2026-09-03T14:30:00Z",
        ),
    )
    prediction_course = _curriculum(teacher, "project.prediction")
    thought_course = _curriculum(teacher, "project.thought")

    # A fresh provider-off service consumes the adopted curricula during a
    # differently worded, differently identified external activity.
    local_service = StudioEpisodeService(data_dir)
    influenced, influenced_replayed = local_service.run_project_activity(
        f"{run_id}-influenced-request",
        _activity(
            run_id,
            "later-opportunity",
            "继续核对页面并读取未闭合的现实结果",
            detail="当前活动与教学源使用不同措辞、ID 和 kind，且不再调用教师。",
            occurred_at="2026-09-03T14:31:00Z",
        ),
    )
    first_tick = influenced["ticks"][0]
    prediction = next(item for item in first_tick["prediction"] if item.get("curriculum_refs"))
    thought = next(item for item in first_tick["thoughts"] if item.get("curriculum_refs"))
    prediction_attempt = _attempt(influenced, "project.prediction")
    thought_attempt = _attempt(influenced, "project.thought")
    proposition_content = (first_tick.get("proposition") or {}).get("content", "")
    draft_text = "".join((first_tick.get("expression_draft") or {}).get("units", []))
    readback_ticks = first_tick.get("tick_index") is not None and influenced["ticks"][1:]
    readback_reapplied = any(
        item.get("curriculum_refs")
        for tick in readback_ticks
        for item in tick.get("prediction", [])
    ) or any(
        item.get("curriculum_refs")
        for tick in readback_ticks
        for item in tick.get("thoughts", [])
    )

    selected_kind = influenced.get("selected_action", {}).get("kind") or "observe_only"
    feedback, feedback_replayed = local_service.run_project_feedback(
        f"{run_id}-feedback-request",
        {
            "feedback_id": f"{run_id}-prediction-counterexample",
            "project_id": "ap-vibe-local",
            "target_episode_id": influenced["episode_id"],
            "target_action": selected_kind,
            "desired_action": None,
            "signal": "correction",
            "magnitude": 1.0,
            "natural_language": "这条预测不合适，只重教预测；想法支架继续保留。",
            "source_ref": f"user://ap-vibe/e2/{run_id}/prediction-counterexample",
            "applicability": {
                "target_capability": "project.prediction",
                "effect_key": prediction_attempt["effect_key"],
            },
        },
    )

    # Reconstruct the whole product service from disk before the final
    # provider-off activity; no in-memory ledger is reused here.
    reopened_service = StudioEpisodeService(data_dir)
    after_feedback, after_replayed = reopened_service.run_project_activity(
        f"{run_id}-after-feedback-request",
        _activity(
            run_id,
            "after-feedback",
            "再次读取页面并处理尚未闭合的现实结果",
            detail="冷重开后验证 prediction 局部撤回，而 thought 课程仍继续。",
            occurred_at="2026-09-03T14:32:00Z",
        ),
    )
    after_tick = after_feedback["ticks"][0]
    prediction_maturity = _maturity(after_feedback, "project.prediction")
    thought_maturity = _maturity(after_feedback, "project.thought")

    curriculum_capabilities = {
        item.get("curriculum", {}).get("target_capability")
        for item in teacher.get("curriculum_episodes", [])
    }
    assert transport.calls == 1
    assert curriculum_capabilities == {"project.prediction", "project.thought"}
    assert all(
        item.get("curriculum", {}).get("status") == "active_trial"
        for item in teacher.get("curriculum_episodes", [])
    )
    assert not any(item.get("curriculum_refs") for item in teacher["ticks"][0]["prediction"])
    assert not teacher["ticks"][0]["thoughts"][0].get("curriculum_refs")
    assert prediction["observed_features"] == {}
    assert prediction["mismatch"] == 0.0
    assert prediction["base_confidence"] == 0.0
    assert prediction["curriculum_delta"] == 0.144
    assert prediction["confidence"] == 0.144
    assert thought["base_proposition"]
    assert thought["curriculum_additions"]
    assert thought["proposition"] != thought["base_proposition"]
    assert thought["curriculum_additions"][0] not in proposition_content
    assert thought["curriculum_additions"][0] not in draft_text
    assert prediction_attempt["effect_key"] == f"prediction_hypothesis:{prediction_course['curriculum_id']}"
    assert thought_attempt["effect_key"] == f"thought_scaffold:{thought_course['curriculum_id']}"
    assert prediction_attempt["contribution"] == 0.144
    assert 0.0 < thought_attempt["contribution"] <= 0.18
    assert not readback_reapplied
    assert feedback.get("curriculum_outcome", {}).get("outcome") == "counterexample"
    assert prediction_maturity["state"] == "reteach"
    assert thought_maturity["state"] != "reteach"
    assert not any(item.get("curriculum_refs") for item in after_tick["prediction"])
    assert after_tick["thoughts"][0].get("curriculum_refs")
    assert influenced["teacher"]["mode"] == "provider_off"
    assert after_feedback["teacher"]["mode"] == "provider_off"

    print(
        json.dumps(
            {
                "ok": True,
                "run_id": run_id,
                "data_dir": str(data_dir),
                "teacher_fixture_calls": transport.calls,
                "replayed": {
                    "teacher": teacher_replayed,
                    "influenced": influenced_replayed,
                    "feedback": feedback_replayed,
                    "after_feedback": after_replayed,
                },
                "curriculum": {
                    "capabilities": sorted(curriculum_capabilities),
                    "prediction_id": prediction_course["curriculum_id"],
                    "thought_id": thought_course["curriculum_id"],
                    "independent_episodes": len(teacher.get("curriculum_episodes", [])),
                    "all_active_after_readback": True,
                },
                "activity_b": {
                    "provider": influenced["teacher"]["mode"],
                    "prediction": {
                        "hypothesis": prediction["hypothesis"],
                        "base": prediction["base_confidence"],
                        "curriculum": prediction["curriculum_delta"],
                        "final": prediction["confidence"],
                        "observed_features": prediction["observed_features"],
                        "mismatch": prediction["mismatch"],
                    },
                    "thought": {
                        "base": thought["base_proposition"],
                        "addition": thought["curriculum_additions"][0],
                        "final": thought["proposition"],
                    },
                    "public_boundary": {
                        "scaffold_in_proposition": thought["curriculum_additions"][0] in proposition_content,
                        "scaffold_in_expression_draft": thought["curriculum_additions"][0] in draft_text,
                    },
                    "readback_reapplied_curriculum": bool(readback_reapplied),
                },
                "scoped_correction": {
                    "outcome": feedback["curriculum_outcome"]["outcome"],
                    "prediction_state": prediction_maturity["state"],
                    "thought_state": thought_maturity["state"],
                    "activity_c_prediction_refs": [
                        ref for item in after_tick["prediction"] for ref in item.get("curriculum_refs", [])
                    ],
                    "activity_c_thought_refs": after_tick["thoughts"][0].get("curriculum_refs", []),
                },
                "provider_after_teaching": {
                    "activity_b": influenced["teacher"]["mode"],
                    "feedback": feedback["teacher"]["mode"],
                    "activity_c": after_feedback["teacher"]["mode"],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
