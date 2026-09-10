"""Run the Wave E3 joint-growth vertical in an isolated AP-Vibe data root.

Only the remote teacher response is replaced by a deterministic, no-network
fixture.  Curriculum adoption, later AP use, attention competition/readback,
feedback attribution, capability-local sampling, and cold recovery all travel
through the normal ``StudioEpisodeService`` product path.
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
from ap_mind.vibe_mind import CurriculumMaturityPolicy


E3_CAPABILITIES = (
    "project.paradigm",
    "project.attention",
    "project.expression",
)


class WaveE3TeacherFixture:
    """Return only the capability families explicitly requested this episode."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        del method, url
        frame = json.loads(kwargs["body"]["messages"][1]["content"])["frame_view"]
        requested = tuple(frame.get("requested_capabilities", ()))
        self.calls.append(requested)
        event_ref = frame["sa"]["event_ref"]
        proposition_ref = frame["proposition"]["proposition_id"]
        payload = {
            "status": "proposal",
            "recall_candidates": [],
            "prediction_candidates": [],
            "appraisal_candidates": [],
            "thought_candidates": [],
            "paradigm_candidates": [{
                "pattern_kind": "relation_frame",
                "invariants": {
                    "source_completeness": "partial",
                    "has_open_items": True,
                    "has_unknowns": True,
                    "has_next_action": True,
                },
                "slots": [
                    {"name": "claim", "source": "proposition.content", "required": True},
                    {
                        "name": "next_action",
                        "source": "activity.observed_next_action",
                        "required": True,
                    },
                ],
                "relations": [{
                    "source_slot": "claim",
                    "target_slot": "next_action",
                    "relation": "precedes",
                }],
                "confidence": 0.8,
                "uncertainty": 0.2,
                "evidence_refs": [event_ref],
                "completeness": "partial",
                "counterexamples": ["任务已经闭合时不套用"],
            }] if "project.paradigm" in requested else [],
            "attention_candidates": [{
                "mode": "maintain_attention",
                "target_ref": event_ref,
                "gain_delta": 0.18,
                "rationale": "当前未闭合输入仍有直接信息价值",
                "source_refs": [event_ref],
                "uncertainty": 0.2,
            }] if "project.attention" in requested else [],
            "expression_candidates": [{
                "template": "我目前能确认的是：{claim}。",
                "tone": "careful",
                "evidence_refs": [proposition_ref],
                "uncertainty": 0.15,
                "counterexamples": ["无需强调证据边界时保持原表达"],
            }] if "project.expression" in requested else [],
            "candidate_preferences": {},
            "selected_candidate_ref": None,
            "lesson_candidates": [],
            "uncertainty": 0.2,
            "limitations": [
                "fixture_teacher_has_no_external_truth",
                "courses_require_later_independent_activity",
            ],
        }
        return {
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


def _activity(run_id: str, suffix: str, summary: str, occurred_at: str) -> dict[str, Any]:
    activity_id = f"{run_id}-{suffix}"
    return {
        "activity_id": activity_id,
        "project_id": "ap-vibe-local",
        "kind": f"unseen-e3-{suffix}",
        "summary": summary,
        "detail": "这是一条来源明确、仍待页面现实回读的隔离工程活动。",
        "source_ref": f"fixture://ap-vibe/e3/{activity_id}",
        "occurred_at": occurred_at,
        "completeness": "partial",
        "observed_remaining": ["页面现实回读"],
        "observed_unknown": ["用户是否看到预期因果链"],
        "observed_next_action": "读取页面现实结果",
    }


def _attempt(view: Mapping[str, Any], capability: str) -> Mapping[str, Any]:
    return next(
        item
        for item in view.get("curriculum_attempts", ())
        if item.get("target_capability") == capability
    )


def _curriculum(view: Mapping[str, Any], capability: str) -> Mapping[str, Any]:
    return next(
        item["curriculum"]
        for item in view.get("curriculum_episodes", ())
        if item.get("curriculum", {}).get("target_capability") == capability
    )


def _maturity(view: Mapping[str, Any], capability: str) -> Mapping[str, Any]:
    return next(
        item
        for item in view.get("learning", {}).get("capability_maturity", ())
        if item.get("target_capability") == capability
    )


def _feedback(
    service: StudioEpisodeService,
    *,
    run_id: str,
    target: Mapping[str, Any],
    capability: str,
    signal: str,
    text: str,
) -> tuple[dict[str, Any], bool]:
    attempt = _attempt(target, capability)
    selected_kind = target.get("selected_action", {}).get("kind") or "observe_only"
    suffix = capability.rsplit(".", 1)[-1]
    return service.run_project_feedback(
        f"{run_id}-feedback-{suffix}-{signal}-request",
        {
            "feedback_id": f"{run_id}-feedback-{suffix}-{signal}",
            "project_id": "ap-vibe-local",
            "target_episode_id": target["episode_id"],
            "target_action": selected_kind,
            "desired_action": None,
            "signal": signal,
            "magnitude": 1.0,
            "natural_language": text,
            "source_ref": f"user://ap-vibe/e3/{run_id}/{suffix}/{signal}",
            "applicability": {
                "target_capability": capability,
                "effect_key": attempt["effect_key"],
            },
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--run-id", default="wave-e3-vertical-20260904-001")
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(args.run_id).strip()
    if not run_id:
        raise ValueError("run_id_required")

    policy = CurriculumMaturityPolicy(
        window_size=4,
        minimum_opportunities=1,
        audit_agreement=0.95,
        reteach_counterexamples=1,
        teaching_rate=1.0,
        trial_rate=0.5,
        audit_rate=0.1,
    )
    transport = WaveE3TeacherFixture()
    gateway = OpenAICompatibleGateway(
        "https://fixture.invalid/v1",
        "fixture-only-not-a-real-key",
        "local-wave-e3-teacher",
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
        reason="deterministic Wave E3 teacher fixture; no network or external truth",
    )

    teacher_service = StudioEpisodeService(
        data_dir,
        gateway=gateway,
        governance=governance,
        capability=capability,
        maturity_policy=policy,
        teacher_capabilities=E3_CAPABILITIES,
    )
    taught, taught_replayed = teacher_service.run_project_activity(
        f"{run_id}-teacher-request",
        _activity(
            run_id,
            "teacher-source",
            "教师示范如何把未闭合工程信息连成规律、注意与表达",
            "2026-09-04T04:00:00Z",
        ),
    )
    taught_courses = {
        capability_key: _curriculum(taught, capability_key)
        for capability_key in E3_CAPABILITIES
    }

    local_service = StudioEpisodeService(data_dir, maturity_policy=policy)
    influenced, influenced_replayed = local_service.run_project_activity(
        f"{run_id}-influenced-request",
        _activity(
            run_id,
            "later-opportunity",
            "AP 独立处理另一条尚未闭合的页面观察",
            "2026-09-04T04:01:00Z",
        ),
    )
    attempts = {key: _attempt(influenced, key) for key in E3_CAPABILITIES}

    positive_feedback: dict[str, dict[str, Any]] = {}
    positive_replayed: dict[str, bool] = {}
    for capability_key in E3_CAPABILITIES:
        feedback, replayed = _feedback(
            local_service,
            run_id=run_id,
            target=influenced,
            capability=capability_key,
            signal="reward",
            text=f"这次{capability_key.rsplit('.', 1)[-1]}的作用符合预期，保留这条局部课程。",
        )
        positive_feedback[capability_key] = feedback
        positive_replayed[capability_key] = replayed

    # This configured episode is ordinal 2 for each capability.  With the
    # injected 10% audit policy and one real successful outcome per capability,
    # all three are locally withheld and the fixture transport is not called.
    sampling_service = StudioEpisodeService(
        data_dir,
        gateway=gateway,
        governance=governance,
        capability=capability,
        maturity_policy=policy,
        teacher_capabilities=E3_CAPABILITIES,
    )
    sampled_local, sampled_replayed = sampling_service.run_project_activity(
        f"{run_id}-sampled-local-request",
        _activity(
            run_id,
            "sampled-local",
            "成熟度抽样让本轮由 AP 本地独立完成",
            "2026-09-04T04:02:00Z",
        ),
    )

    correction, correction_replayed = _feedback(
        local_service,
        run_id=run_id,
        target=sampled_local,
        capability="project.expression",
        signal="correction",
        text="这层表达外壳不适合，只重新教学表达；规律和注意继续保留。",
    )

    # Rebuild the service from disk.  No in-memory learner or frame is reused.
    reopened = StudioEpisodeService(data_dir, maturity_policy=policy)
    after_correction, after_correction_replayed = reopened.run_project_activity(
        f"{run_id}-after-correction-request",
        _activity(
            run_id,
            "after-correction",
            "冷重启后继续观察另一条未闭合活动",
            "2026-09-04T04:03:00Z",
        ),
    )

    reteach_service = StudioEpisodeService(
        data_dir,
        gateway=gateway,
        governance=governance,
        capability=capability,
        maturity_policy=policy,
        teacher_capabilities=E3_CAPABILITIES,
    )
    reteach, reteach_replayed = reteach_service.run_project_activity(
        f"{run_id}-reteach-request",
        _activity(
            run_id,
            "expression-reteach",
            "表达反例后只恢复表达教师，其他能力继续本地运行",
            "2026-09-04T04:04:00Z",
        ),
    )

    final_local = StudioEpisodeService(data_dir, maturity_policy=policy)
    resumed, resumed_replayed = final_local.run_project_activity(
        f"{run_id}-resumed-request",
        _activity(
            run_id,
            "resumed-expression",
            "重新教学后的表达在下一条独立活动中恢复试用",
            "2026-09-04T04:05:00Z",
        ),
    )

    first_tick = influenced["ticks"][0]
    attention_attempt = attempts["project.attention"]
    expression_attempt = attempts["project.expression"]
    after_tick = after_correction["ticks"][0]
    resumed_tick = resumed["ticks"][0]
    expected_calls = int(not taught_replayed) + int(not reteach_replayed)

    assert len(transport.calls) == expected_calls
    if not taught_replayed:
        assert transport.calls[0] == E3_CAPABILITIES
    if not reteach_replayed:
        assert transport.calls[-1] == ("project.expression",)
    assert set(taught_courses) == set(E3_CAPABILITIES)
    assert all(
        course["status"] == "active_trial" for course in taught_courses.values()
    )
    assert not taught["ticks"][0]["paradigms"]
    assert taught["ticks"][0]["expression_draft"]["renderer_source"] == "ap_native"
    assert first_tick["paradigms"][0]["status"] == "matched"
    assert attention_attempt["effective_result"]["won_and_readback"] is True
    assert expression_attempt["effective_result"]["semantic_preserved"] is True
    assert expression_attempt["local_before"]["surface"] != expression_attempt["effective_result"]["surface"]
    assert first_tick["expression_draft"]["public_allowed"] is False
    assert all(
        positive_feedback[key]["curriculum_outcome"]["outcome"] == "success"
        for key in E3_CAPABILITIES
    )
    assert sampled_local["teacher"]["mode"] == "locally_withheld_by_sampling"
    assert sampled_local["teacher"]["requested_capabilities"] == []
    assert sampled_local["teacher"]["sampling"]["all_withheld"] is True
    assert correction["curriculum_outcome"]["outcome"] == "counterexample"
    assert _maturity(after_correction, "project.expression")["state"] == "reteach"
    assert after_tick["paradigms"] and after_tick["paradigms"][0]["curriculum_refs"]
    assert any(item.get("curriculum_refs") for item in after_tick["attention"]["candidates"])
    assert after_tick["expression_draft"]["renderer_source"] == "ap_native"
    assert reteach["teacher"]["requested_capabilities"] == ["project.expression"]
    assert resumed_tick["expression_draft"]["renderer_source"] == "assisted_curriculum"
    assert resumed_tick["expression_draft"]["diff"]["semantic_change"] is False

    print(json.dumps({
        "ok": True,
        "run_id": run_id,
        "data_dir": str(data_dir),
        "projection_version": influenced["projection_version"],
        "fixture_calls_this_run": [list(item) for item in transport.calls],
        "replayed": {
            "teacher": taught_replayed,
            "influenced": influenced_replayed,
            "positive_feedback": positive_replayed,
            "sampled_local": sampled_replayed,
            "correction": correction_replayed,
            "after_correction": after_correction_replayed,
            "reteach": reteach_replayed,
            "resumed": resumed_replayed,
        },
        "teacher_to_growth": {
            "courses": sorted(taught_courses),
            "independent_curriculum_episodes": len(taught["curriculum_episodes"]),
            "later_attempts": sorted(attempts),
            "paradigm_status": first_tick["paradigms"][0]["status"],
            "attention_won_and_readback": attention_attempt["effective_result"]["won_and_readback"],
            "expression_semantic_preserved": expression_attempt["effective_result"]["semantic_preserved"],
            "expression_public_allowed": first_tick["expression_draft"]["public_allowed"],
        },
        "local_teacher_sampling": {
            "mode": sampled_local["teacher"]["mode"],
            "requested_capabilities": sampled_local["teacher"]["requested_capabilities"],
            "all_withheld": sampled_local["teacher"]["sampling"]["all_withheld"],
        },
        "scoped_reteach": {
            "correction_outcome": correction["curriculum_outcome"]["outcome"],
            "after_restart_expression_state": _maturity(after_correction, "project.expression")["state"],
            "after_restart_paradigm_present": bool(after_tick["paradigms"]),
            "after_restart_attention_assisted": any(
                item.get("curriculum_refs") for item in after_tick["attention"]["candidates"]
            ),
            "after_restart_expression_source": after_tick["expression_draft"]["renderer_source"],
            "teacher_requested_after_counterexample": reteach["teacher"]["requested_capabilities"],
            "next_independent_expression_source": resumed_tick["expression_draft"]["renderer_source"],
        },
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
