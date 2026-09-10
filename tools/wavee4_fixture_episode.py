"""Run the Wave E4 parameter-growth vertical through the AP-Vibe product path.

The only replaced boundary is the remote teacher transport. Curriculum review,
independent result-back, a later episode-local attention policy, ActionArena,
feedback attribution, scoped rollback, cold recovery, and UI projections all
use ``StudioEpisodeService`` and the production runtime. No network or real key
is used, and the short fixture never claims long-term maturity.
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
from ap_mind.vibe_mind import (
    CURRICULUM_PARAMETER_CAPABILITY,
    CURRICULUM_SUPPORTED_CAPABILITIES,
    CurriculumMaturityPolicy,
)


class WaveE4TeacherFixture:
    """Return one source-bound delta only when parameter teaching is requested."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        del method, url
        frame = json.loads(kwargs["body"]["messages"][1]["content"])["frame_view"]
        requested = tuple(frame.get("requested_capabilities", ()))
        self.calls.append(requested)
        event_ref = frame["sa"]["event_ref"]
        payload = {
            "status": "proposal",
            "recall_candidates": [],
            "prediction_candidates": [],
            "appraisal_candidates": [],
            "thought_candidates": [],
            "paradigm_candidates": [],
            "attention_candidates": [],
            "expression_candidates": [],
            "parameter_candidates": [
                {
                    "parameter": "attention.novelty_weight",
                    "delta": 0.04,
                    "rationale": "当前证据结构中的新信息需要略高注意增益",
                    "source_refs": [event_ref],
                    "uncertainty": 0.2,
                    "counterexamples": ["后续注意效果没有改善时撤回并重新教学"],
                    "scope": {
                        "source_completeness": "partial",
                        "has_open_items": True,
                        "has_unknowns": True,
                        "has_conflicts": False,
                        "has_next_action": True,
                    },
                    "expected_direction": "increase",
                }
            ]
            if CURRICULUM_PARAMETER_CAPABILITY in requested
            else [],
            "candidate_preferences": {},
            "selected_candidate_ref": None,
            "lesson_candidates": [],
            "uncertainty": 0.2,
            "limitations": [
                "fixture_teacher_has_no_truth_or_winner_authority",
                "parameter_requires_independent_curriculum_and_later_episode",
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
        "kind": f"unseen-e4-{suffix}",
        "summary": summary,
        "detail": "这是一条来源明确、仍待现实回读的隔离工程活动。",
        "source_ref": f"fixture://ap-vibe/e4/{activity_id}",
        "occurred_at": occurred_at,
        "completeness": "partial",
        "observed_remaining": ["页面现实回读"],
        "observed_unknown": ["用户是否看到预期因果链"],
        "observed_next_action": "读取页面现实结果",
    }


def _attempt(view: Mapping[str, Any]) -> Mapping[str, Any]:
    return next(
        item
        for item in view.get("curriculum_attempts", ())
        if item.get("target_capability") == CURRICULUM_PARAMETER_CAPABILITY
    )


def _curriculum(view: Mapping[str, Any]) -> Mapping[str, Any]:
    return next(
        item["curriculum"]
        for item in view.get("curriculum_episodes", ())
        if item.get("curriculum", {}).get("target_capability")
        == CURRICULUM_PARAMETER_CAPABILITY
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
    suffix: str,
    target: Mapping[str, Any],
    signal: str,
    text: str,
) -> tuple[dict[str, Any], bool]:
    attempt = _attempt(target)
    selected_kind = target.get("selected_action", {}).get("kind") or "observe_only"
    return service.run_project_feedback(
        f"{run_id}-feedback-{suffix}-request",
        {
            "feedback_id": f"{run_id}-feedback-{suffix}",
            "project_id": "ap-vibe-local",
            "target_episode_id": target["episode_id"],
            "target_action": selected_kind,
            "desired_action": None,
            "signal": signal,
            "magnitude": 1.0,
            "natural_language": text,
            "source_ref": f"user://ap-vibe/e4/{run_id}/{suffix}",
            "applicability": {
                "target_capability": CURRICULUM_PARAMETER_CAPABILITY,
                "effect_key": attempt["effect_key"],
            },
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--run-id", default="wave-e4-vertical-20260904-001")
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
        audit_rate=0.25,
    )
    transport = WaveE4TeacherFixture()
    gateway = OpenAICompatibleGateway(
        "https://fixture.invalid/v1",
        "fixture-only-not-a-real-key",
        "local-wave-e4-teacher",
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
        reason="deterministic Wave E4 fixture; no network or external truth",
    )

    teacher_service = StudioEpisodeService(
        data_dir,
        gateway=gateway,
        governance=governance,
        capability=capability,
        maturity_policy=policy,
        teacher_capabilities=(CURRICULUM_PARAMETER_CAPABILITY,),
    )
    taught, taught_replayed = teacher_service.run_project_activity(
        f"{run_id}-teacher-request",
        _activity(
            run_id,
            "teacher-source",
            "教师提出一项有来源、可撤销的注意参数试用",
            "2026-09-04T05:00:00Z",
        ),
    )
    course = _curriculum(taught)

    local_service = StudioEpisodeService(data_dir, maturity_policy=policy)
    influenced, influenced_replayed = local_service.run_project_activity(
        f"{run_id}-influenced-request",
        _activity(
            run_id,
            "later-opportunity",
            "AP 在另一条独立活动中试用新的注意参数",
            "2026-09-04T05:01:00Z",
        ),
    )
    influenced_attempt = _attempt(influenced)
    positive, positive_replayed = _feedback(
        local_service,
        run_id=run_id,
        suffix="parameter-reward",
        target=influenced,
        signal="reward",
        text="这次对新信息的注意方向更合适，保留这项参数试用。",
    )

    # Reconstruct the service from disk before observing whether the trial
    # remains active. No in-memory learner or runtime state is reused.
    reopened = StudioEpisodeService(data_dir, maturity_policy=policy)
    retained, retained_replayed = reopened.run_project_activity(
        f"{run_id}-retained-request",
        _activity(
            run_id,
            "retained-after-restart",
            "冷重启后参数试用仍只影响对应注意增益分量",
            "2026-09-04T05:02:00Z",
        ),
    )
    retained_attempt = _attempt(retained)
    correction, correction_replayed = _feedback(
        reopened,
        run_id=run_id,
        suffix="parameter-counterexample",
        target=retained,
        signal="correction",
        text="这次新信息权重偏高，只撤回并重新教学参数调节。",
    )

    rolled_service = StudioEpisodeService(data_dir, maturity_policy=policy)
    rolled_back, rolled_back_replayed = rolled_service.run_project_activity(
        f"{run_id}-rolled-back-request",
        _activity(
            run_id,
            "rolled-back",
            "反例后的新活动恢复默认注意参数",
            "2026-09-04T05:03:00Z",
        ),
    )

    reteach_service = StudioEpisodeService(
        data_dir,
        gateway=gateway,
        governance=governance,
        capability=capability,
        maturity_policy=policy,
        teacher_capabilities=(CURRICULUM_PARAMETER_CAPABILITY,),
    )
    reteach, reteach_replayed = reteach_service.run_project_activity(
        f"{run_id}-reteach-request",
        _activity(
            run_id,
            "parameter-reteach",
            "参数反例后只恢复参数教师",
            "2026-09-04T05:04:00Z",
        ),
    )

    final_local = StudioEpisodeService(data_dir, maturity_policy=policy)
    resumed, resumed_replayed = final_local.run_project_activity(
        f"{run_id}-resumed-request",
        _activity(
            run_id,
            "resumed-parameter",
            "重新教学后的参数在下一条独立活动中恢复试用",
            "2026-09-04T05:05:00Z",
        ),
    )
    resumed_attempt = _attempt(resumed)

    # A new teacher-enabled episode after the replacement must use the current
    # generation's trial/audit cadence.  With the bounded fixture policy this
    # second post-replacement opportunity is withheld locally, proving the old
    # counterexample no longer forces a model call forever.
    post_reteach_sampling_service = StudioEpisodeService(
        data_dir,
        gateway=gateway,
        governance=governance,
        capability=capability,
        maturity_policy=policy,
        teacher_capabilities=(CURRICULUM_PARAMETER_CAPABILITY,),
    )
    post_reteach_sampling, post_reteach_sampling_replayed = (
        post_reteach_sampling_service.run_project_activity(
            f"{run_id}-post-reteach-sampling-request",
            _activity(
                run_id,
                "post-reteach-sampling",
                "新训练世代按试用频率由 AP 本地处理，而非被旧反例永久锁定",
                "2026-09-04T05:06:00Z",
            ),
        )
    )

    influenced_policy = influenced["ticks"][0]["attention"]["policy"]
    retained_policy = retained["ticks"][0]["attention"]["policy"]
    rolled_policy = rolled_back["ticks"][0]["attention"]["policy"]
    resumed_policy = resumed["ticks"][0]["attention"]["policy"]
    maturity_rows = resumed.get("learning", {}).get("capability_maturity", ())
    maturity_keys = {item.get("target_capability") for item in maturity_rows}
    expected_calls = int(not taught_replayed) + int(not reteach_replayed)

    assert len(transport.calls) == expected_calls
    if not taught_replayed:
        assert transport.calls[0] == (CURRICULUM_PARAMETER_CAPABILITY,)
    if not reteach_replayed:
        assert transport.calls[-1] == (CURRICULUM_PARAMETER_CAPABILITY,)
    assert course["status"] == "active_trial"
    assert not taught["ticks"][0]["attention"]["policy"]["parameter_trials"]
    assert influenced_attempt["status"] == "attempted"
    assert influenced_attempt["target_action"] is None
    assert influenced_attempt["local_before"]["before"] == 0.24
    assert influenced_attempt["effective_result"]["effective"] == 0.28
    assert influenced_attempt["effective_result"]["component"] == "novelty"
    assert influenced_attempt["effective_result"]["component_delta"] > 0.0
    assert _maturity(influenced, CURRICULUM_PARAMETER_CAPABILITY)["success"] == 0
    assert positive["curriculum_outcome"]["outcome"] == "success"
    assert retained_policy["parameter_version"] == "attention-policy.v1+trial"
    assert retained_policy["novelty_weight"] == 0.28
    assert retained_attempt["effective_result"]["component_delta"] > 0.0
    assert correction["curriculum_outcome"]["outcome"] == "counterexample"
    assert _maturity(rolled_back, CURRICULUM_PARAMETER_CAPABILITY)["state"] == "reteach"
    assert rolled_policy["parameter_version"] == "attention-policy.v1"
    assert rolled_policy["novelty_weight"] == 0.24
    assert rolled_policy["parameter_trials"] == []
    assert reteach["teacher"]["requested_capabilities"] == [CURRICULUM_PARAMETER_CAPABILITY]
    assert resumed_policy["parameter_version"] == "attention-policy.v1+trial"
    assert resumed_attempt["effective_result"]["effective"] == 0.28
    assert _maturity(resumed, CURRICULUM_PARAMETER_CAPABILITY)["state"] == "trial"
    assert post_reteach_sampling["teacher"]["mode"] == "locally_withheld_by_sampling"
    assert post_reteach_sampling["teacher"]["requested_capabilities"] == []
    assert post_reteach_sampling["teacher"]["sampling"]["all_withheld"] is True
    old_course = next(
        item
        for item in resumed.get("learning", {}).get("curricula", ())
        if item.get("curriculum_id") == course["curriculum_id"]
    )
    new_course = _curriculum(reteach)
    assert old_course["status"] == "retracted"
    assert old_course["outcome_counts"]["counterexample"] == 1
    assert old_course["outcome_counts"]["replaced"] == 1
    assert old_course["replacement"]["replaced_by_curriculum_id"] == new_course["curriculum_id"]
    assert maturity_keys == set(CURRICULUM_SUPPORTED_CAPABILITIES)
    assert all(item["long_term_gate"]["met"] is False for item in maturity_rows)

    print(
        json.dumps(
            {
                "ok": True,
                "run_id": run_id,
                "data_dir": str(data_dir),
                "projection_version": influenced["projection_version"],
                "fixture_calls_this_run": [list(item) for item in transport.calls],
                "replayed": {
                    "teacher": taught_replayed,
                    "influenced": influenced_replayed,
                    "positive_feedback": positive_replayed,
                    "retained": retained_replayed,
                    "counterexample": correction_replayed,
                    "rolled_back": rolled_back_replayed,
                    "reteach": reteach_replayed,
                    "resumed": resumed_replayed,
                    "post_reteach_sampling": post_reteach_sampling_replayed,
                },
                "parameter_growth": {
                    "curriculum_episode_count": len(taught["curriculum_episodes"]),
                    "teaching_source_parameter_trials": taught["ticks"][0]["attention"]["policy"]["parameter_trials"],
                    "default": influenced_attempt["local_before"]["before"],
                    "delta": influenced_attempt["effective_result"]["delta"],
                    "effective": influenced_attempt["effective_result"]["effective"],
                    "component": influenced_attempt["effective_result"]["component"],
                    "component_delta": influenced_attempt["effective_result"]["component_delta"],
                    "observed_winner": influenced_attempt["observed_winner"],
                    "success_before_feedback": _maturity(
                        influenced, CURRICULUM_PARAMETER_CAPABILITY
                    )["success"],
                    "reward_outcome": positive["curriculum_outcome"]["outcome"],
                },
                "scoped_rollback": {
                    "retained_after_restart": retained_policy["novelty_weight"],
                    "counterexample_outcome": correction["curriculum_outcome"]["outcome"],
                    "state_after_counterexample": _maturity(
                        rolled_back, CURRICULUM_PARAMETER_CAPABILITY
                    )["state"],
                    "default_after_restart": rolled_policy["novelty_weight"],
                    "teacher_requested_after_counterexample": reteach["teacher"]["requested_capabilities"],
                    "resumed_effective": resumed_policy["novelty_weight"],
                    "replacement_curriculum_id": new_course["curriculum_id"],
                    "old_counterexample_preserved": old_course["outcome_counts"]["counterexample"],
                    "current_generation_state": _maturity(
                        resumed, CURRICULUM_PARAMETER_CAPABILITY
                    )["state"],
                    "next_sampling_requested": post_reteach_sampling["teacher"]["requested_capabilities"],
                    "next_sampling_provider_mode": post_reteach_sampling["teacher"]["mode"],
                },
                "joint_growth_ledger": {
                    "capability_count": len(maturity_rows),
                    "capabilities": sorted(maturity_keys),
                    "long_term_gate_met": any(
                        item["long_term_gate"]["met"] for item in maturity_rows
                    ),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
