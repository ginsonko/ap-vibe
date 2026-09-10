"""Run the provider-free Wave E1 product vertical in an isolated data root.

The fixture replaces only the remote teacher response.  Every curriculum still
has to pass through the normal AP episode, ActionArena, dispatch/readback and a
later independent project activity before it can affect cognition.
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


class WaveETeacherFixture:
    """One bounded, source-grounded teacher response with no network access."""

    def __init__(self, memory_ref: str) -> None:
        self.memory_ref = memory_ref
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
                                "recall_candidates": [
                                    {
                                        "memory_ref": self.memory_ref,
                                        "summary": "先前有一条同主题且可回查的项目活动",
                                        "relevance": 0.9,
                                        "rationale": "它与当前未闭合的浏览器回读具有相近证据结构",
                                    }
                                ],
                                "prediction_candidates": [],
                                "appraisal_candidates": [
                                    {
                                        "name": "task_open",
                                        "intensity": 0.8,
                                        "valence": -0.2,
                                        "rationale": "本地结构显示任务和未知仍未闭合",
                                        "subject_scope": "private",
                                        "source_refs": [event_ref],
                                    }
                                ],
                                "thought_candidates": [],
                                "candidate_preferences": {},
                                "selected_candidate_ref": None,
                                "lesson_candidates": [],
                                "uncertainty": 0.2,
                                "limitations": [
                                    "fixture_teacher_has_no_external_evidence",
                                    "teacher_effect_requires_a_later_independent_episode",
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
        "kind": f"wave-e1-{suffix}",
        "summary": summary,
        "detail": detail,
        "source_ref": f"fixture://ap-vibe/e1/{activity_id}",
        "occurred_at": occurred_at,
        "completeness": "partial",
        "observed_remaining": ["浏览器现实回读"],
        "observed_unknown": ["最终用户效果尚未确认"],
        "observed_next_action": "读取页面现实结果并只纠正发生偏差的认知能力",
    }


def _maturity(view: Mapping[str, Any], capability: str) -> Mapping[str, Any]:
    items = view.get("learning", {}).get("capability_maturity", [])
    return next(item for item in items if item.get("target_capability") == capability)


def _attempt(view: Mapping[str, Any], capability: str) -> Mapping[str, Any]:
    return next(
        item
        for item in view.get("curriculum_attempts", [])
        if item.get("target_capability") == capability
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--run-id", default="wave-e1-vertical-20260903-001")
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(args.run_id).strip()
    if not run_id:
        raise ValueError("run_id_required")

    local_service = StudioEpisodeService(data_dir)
    history, history_replayed = local_service.run_project_activity(
        f"{run_id}-history-request",
        _activity(
            run_id,
            "history",
            "恢复工作台的浏览器结果仍未闭合",
            detail="这是来源明确的历史项目活动；稍后只能从该真实记录中召回。",
            occurred_at="2026-09-03T12:50:00Z",
        ),
    )
    memory = local_service.learning_ledger.memory_events("ap-vibe-local", limit=8)
    if not memory:
        raise AssertionError("history_did_not_enter_project_memory")
    memory_ref = memory[0].event_id

    transport = WaveETeacherFixture(memory_ref)
    gateway = OpenAICompatibleGateway(
        "https://fixture.invalid/v1",
        "fixture-only-not-a-real-key",
        "local-wave-e-teacher",
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
        reason="deterministic Wave E teacher fixture; no network or external truth",
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
            "恢复工作台还缺少浏览器的现实回读",
            detail="教师只能引用本轮 B 阶段已经看见的真实历史，并提出可撤销课程。",
            occurred_at="2026-09-03T12:51:00Z",
        ),
    )

    local_service = StudioEpisodeService(data_dir)
    influenced, influenced_replayed = local_service.run_project_activity(
        f"{run_id}-influenced-request",
        _activity(
            run_id,
            "later-opportunity",
            "继续核对工作台并读取浏览器中的实际呈现",
            detail="措辞、活动类型和 ID 都与教学源不同，用来观察课程能否迁移。",
            occurred_at="2026-09-03T12:52:00Z",
        ),
    )
    first_tick = influenced["ticks"][0]
    recall = next(item for item in first_tick["recall"] if item["event_ref"] == memory_ref)
    feeling = next(item for item in first_tick["feelings"] if item["name"] == "task_open")
    recall_attempt = _attempt(influenced, "project.recall")
    appraisal_attempt = _attempt(influenced, "project.appraisal")

    selected_kind = influenced.get("selected_action", {}).get("kind") or "observe_only"
    feedback, feedback_replayed = local_service.run_project_feedback(
        f"{run_id}-feedback-request",
        {
            "feedback_id": f"{run_id}-recall-counterexample",
            "project_id": "ap-vibe-local",
            "target_episode_id": influenced["episode_id"],
            "target_action": selected_kind,
            "desired_action": None,
            "signal": "correction",
            "magnitude": 1.0,
            "natural_language": "这次增强的历史记忆不相关，只重教 B 召回；认知感受保持原样。",
            "source_ref": f"user://ap-vibe/e1/{run_id}/recall-counterexample",
            "applicability": {
                "target_capability": "project.recall",
                "effect_key": recall_attempt["effect_key"],
            },
        },
    )

    local_service = StudioEpisodeService(data_dir)
    after_feedback, after_replayed = local_service.run_project_activity(
        f"{run_id}-after-feedback-request",
        _activity(
            run_id,
            "after-feedback",
            "检查现实页面并处理仍未闭合的可见结果",
            detail="冷重开本地 service 后验证召回课程局部撤回而感受课程继续工作。",
            occurred_at="2026-09-03T12:53:00Z",
        ),
    )
    after_tick = after_feedback["ticks"][0]
    after_memory = next(item for item in after_tick["recall"] if item["event_ref"] == memory_ref)
    after_feeling = next(item for item in after_tick["feelings"] if item["name"] == "task_open")
    recall_maturity = _maturity(after_feedback, "project.recall")
    appraisal_maturity = _maturity(after_feedback, "project.appraisal")

    curriculum_capabilities = {
        item.get("curriculum", {}).get("target_capability")
        for item in teacher.get("curriculum_episodes", [])
    }
    assert curriculum_capabilities == {"project.recall", "project.appraisal"}
    assert all(
        item.get("curriculum", {}).get("status") == "active_trial"
        for item in teacher.get("curriculum_episodes", [])
    )
    assert recall["summary"] == history["input"]["activity"]["summary"]
    assert recall["source"] == "memory_assisted_trial"
    assert recall["curriculum_gain"] > 0
    assert recall["score"] > recall["base_score"]
    assert feeling["source"] == "assisted_trial"
    assert feeling["curriculum_delta"] > 0
    assert recall_attempt["status"] == "attempted"
    assert appraisal_attempt["status"] == "attempted"
    assert feedback.get("curriculum_outcome", {}).get("outcome") == "counterexample"
    assert recall_maturity["state"] == "reteach"
    assert appraisal_maturity["state"] != "reteach"
    assert after_memory["curriculum_gain"] == 0
    assert not after_memory["curriculum_refs"]
    assert after_feeling["curriculum_delta"] > 0
    assert after_feeling["curriculum_refs"]
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
                    "history": history_replayed,
                    "teacher": teacher_replayed,
                    "influenced": influenced_replayed,
                    "feedback": feedback_replayed,
                    "after_feedback": after_replayed,
                },
                "curriculum": {
                    "capabilities": sorted(curriculum_capabilities),
                    "independent_episodes": len(teacher.get("curriculum_episodes", [])),
                    "all_active_after_readback": True,
                },
                "product_effect": {
                    "remembered_summary": recall["summary"],
                    "recall": {
                        "base": recall["base_score"],
                        "teacher_gain": recall["curriculum_gain"],
                        "final": recall["score"],
                        "source": recall["source"],
                    },
                    "appraisal": {
                        "name": feeling["name"],
                        "base": feeling["base_intensity"],
                        "teacher_delta": feeling["curriculum_delta"],
                        "final": feeling["intensity"],
                        "source": feeling["source"],
                    },
                },
                "scoped_correction": {
                    "outcome": feedback["curriculum_outcome"]["outcome"],
                    "recall_state": recall_maturity["state"],
                    "recall_gain_after": after_memory["curriculum_gain"],
                    "appraisal_state": appraisal_maturity["state"],
                    "appraisal_delta_after": after_feeling["curriculum_delta"],
                },
                "provider_after_teaching": {
                    "influenced": influenced["teacher"]["mode"],
                    "after_feedback": after_feedback["teacher"]["mode"],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
