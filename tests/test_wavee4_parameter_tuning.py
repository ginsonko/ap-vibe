from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ap_mind.contracts import CapabilityOwnership
from ap_mind.gateway import OpenAICompatibleGateway
from ap_mind.governance import GovernanceCompatibilityRecord
from ap_mind.studio_server import HYBRID_WHITEPAPER_SHA256
from ap_mind.vibe_mind import (
    CURRICULUM_PARAMETER_CAPABILITY,
    CURRICULUM_SUPPORTED_CAPABILITIES,
    CURRICULUM_TARGET_CAPABILITY,
    CurriculumAttempt,
    CurriculumCandidate,
    CurriculumMaturityPolicy,
    CurriculumOutcome,
    ProjectActivity,
    ProjectFeedback,
    ProjectLearningLedger,
    run_curriculum_episode,
    run_feedback_episode,
    run_project_episode,
)


class ParameterTeacherTransport:
    """A network-free teacher that proposes one bounded attention parameter."""

    def __init__(self, *, emit_candidate: bool = True) -> None:
        self.emit_candidate = emit_candidate
        self.calls: list[dict] = []

    def request_json(self, method: str, url: str, **kwargs):
        del method, url
        frame = json.loads(kwargs["body"]["messages"][1]["content"])["frame_view"]
        self.calls.append(frame)
        event_ref = frame["sa"]["event_ref"]
        parameter_candidates = []
        if self.emit_candidate:
            parameter_candidates.append(
                {
                    "parameter": "attention.novelty_weight",
                    "delta": 0.04,
                    "rationale": "当前证据结构中的新信息需要略高注意增益",
                    "source_refs": [event_ref],
                    "uncertainty": 0.2,
                    "counterexamples": ["后续注意效果未改善时撤回"],
                    "expected_direction": "increase",
                }
            )
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "status": "proposal",
                                "recall_candidates": [],
                                "prediction_candidates": [],
                                "appraisal_candidates": [],
                                "thought_candidates": [],
                                "paradigm_candidates": [],
                                "attention_candidates": [],
                                "expression_candidates": [],
                                "parameter_candidates": parameter_candidates,
                                "candidate_preferences": {},
                                "selected_candidate_ref": None,
                                "lesson_candidates": [],
                                "uncertainty": 0.2,
                                "limitations": ["teacher_has_no_winner_or_truth_authority"],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


def _activity(activity_id: str, summary: str) -> ProjectActivity:
    return ProjectActivity(
        activity_id=activity_id,
        project_id="project-local",
        kind=f"unseen-{activity_id}",
        summary=summary,
        detail="仍需读取真实页面结果并保留未知边界。",
        source_ref=f"session://e4/{activity_id}",
        completeness="partial",
        observed_remaining=("页面现实回读",),
        observed_unknown=("用户是否看到预期效果",),
        observed_next_action="读取页面现实结果",
    )


def _gateway(transport: ParameterTeacherTransport) -> OpenAICompatibleGateway:
    return OpenAICompatibleGateway(
        "https://provider.invalid/v1",
        "fixture-secret-never-persist",
        "gpt-fixture",
        transport=transport,
        max_retries=0,
    )


def _governance() -> GovernanceCompatibilityRecord:
    return GovernanceCompatibilityRecord(
        record_id="wave-e4-test",
        project_id="project-local",
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
        reason="bounded E4 fixture",
    )


def _capability_rows(ledger: ProjectLearningLedger) -> dict[str, dict]:
    return {
        item["target_capability"]: item
        for item in ledger.snapshot("project-local")["capability_maturity"]
    }


def test_parameter_trial_changes_one_later_component_and_feedback_is_local(tmp_path: Path) -> None:
    source = _activity("teacher-source", "页面链路已实现但真实结果仍未闭合")
    later = _activity("later-opportunity", "新的页面观察仍包含未闭合现实回读")
    baseline_ledger = ProjectLearningLedger(tmp_path / "baseline-learning.sqlite")
    baseline_ledger.remember_activity(source)
    learning_path = tmp_path / "trial-learning.sqlite"
    ledger = ProjectLearningLedger(learning_path)
    transport = ParameterTeacherTransport()

    taught = run_project_episode(
        tmp_path / "teacher.sqlite",
        source,
        learning_ledger=ledger,
        gateway=_gateway(transport),
        governance=_governance(),
        capability=_capability(),
        teacher_capabilities=(CURRICULUM_PARAMETER_CAPABILITY,),
    )

    assert len(transport.calls) == 1
    assert transport.calls[0]["requested_capabilities"] == [CURRICULUM_PARAMETER_CAPABILITY]
    assert len(taught.curriculum_runs) == 1
    course_run = taught.curriculum_runs[0]
    assert course_run.curriculum.target_capability == CURRICULUM_PARAMETER_CAPABILITY
    assert course_run.curriculum.status == "active_trial"
    assert course_run.outcome is not None
    assert course_run.outcome.resulting_status == "active_trial"
    assert course_run.episode_id != taught.episode_id
    # The teaching source cannot retroactively tune the episode that created it.
    assert taught.results[0].frame.attention["policy"]["parameter_trials"] == []

    baseline = run_project_episode(
        tmp_path / "baseline.sqlite",
        later,
        learning_ledger=baseline_ledger,
    )
    influenced = run_project_episode(
        tmp_path / "influenced.sqlite",
        later,
        learning_ledger=ledger,
    )
    baseline_frame = baseline.results[0].frame
    frame = influenced.results[0].frame
    policy = frame.attention["policy"]
    assert policy["parameter_version"] == "attention-policy.v1+trial"
    assert policy["novelty_weight"] == 0.28
    assert policy["mismatch_weight"] == baseline_frame.attention["policy"]["mismatch_weight"]
    assert policy["recall_weight"] == baseline_frame.attention["policy"]["recall_weight"]
    assert policy["open_goal_weight"] == baseline_frame.attention["policy"]["open_goal_weight"]
    assert policy["paradigm_weight"] == baseline_frame.attention["policy"]["paradigm_weight"]
    assert policy["fatigue_inhibition_weight"] == baseline_frame.attention["policy"]["fatigue_inhibition_weight"]

    baseline_attention = {
        (item["mode"], item["target_ref"]): item
        for item in baseline_frame.attention["candidates"]
    }
    influenced_attention = {
        (item["mode"], item["target_ref"]): item
        for item in frame.attention["candidates"]
    }
    common = set(baseline_attention).intersection(influenced_attention)
    assert common
    changed_novelty_components = 0
    for key in common:
        before = baseline_attention[key]["gain_ledger"]
        after = influenced_attention[key]["gain_ledger"]
        for component in before:
            if component == "novelty":
                if after[component] > before[component]:
                    changed_novelty_components += 1
            else:
                assert after[component] == before[component]
    assert changed_novelty_components >= 1

    attempts = [
        item
        for item in influenced.curriculum_attempts
        if item.target_capability == CURRICULUM_PARAMETER_CAPABILITY
    ]
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.status == "attempted"
    assert attempt.target_action is None
    assert attempt.effect_key == "parameter:attention.novelty_weight"
    assert attempt.local_before["before"] == 0.24
    assert attempt.effective_result["delta"] == 0.04
    assert attempt.effective_result["effective"] == 0.28
    assert attempt.effective_result["component"] == "novelty"
    assert attempt.effective_result["component_delta"] > 0.0
    # Merely entering the ledger (or even winning) is not a success outcome.
    before_feedback = _capability_rows(ledger)
    assert before_feedback[CURRICULUM_PARAMETER_CAPABILITY]["success"] == 0
    assert before_feedback[CURRICULUM_PARAMETER_CAPABILITY]["unknown"] >= 1

    positive = ProjectFeedback(
        feedback_id="parameter-positive",
        project_id="project-local",
        target_episode_id=influenced.episode_id,
        target_action=frame.decision.get("selected_kind") or "observe_only",
        signal="reward",
        magnitude=1.0,
        natural_language="这次对新信息的注意方向更合适，保留这项试用。",
        source_ref="user://e4/parameter-positive",
        applicability={
            "target_capability": CURRICULUM_PARAMETER_CAPABILITY,
            "effect_key": attempt.effect_key,
        },
    )
    rewarded = run_feedback_episode(
        tmp_path / "positive-feedback.sqlite",
        positive,
        later,
        learning_ledger=ledger,
    )
    assert rewarded.curriculum_outcome is not None
    assert rewarded.curriculum_outcome.outcome == "success"
    assert ledger.get_curriculum(course_run.curriculum.curriculum_id).status == "active_trial"

    restarted = ProjectLearningLedger(learning_path)
    post_reward_activity = _activity("post-reward", "冷重启后继续观察另一项未闭合页面结果")
    post_reward = run_project_episode(
        tmp_path / "post-reward.sqlite",
        post_reward_activity,
        learning_ledger=restarted,
    )
    post_attempt = next(
        item
        for item in post_reward.curriculum_attempts
        if item.target_capability == CURRICULUM_PARAMETER_CAPABILITY
    )
    assert post_reward.results[0].frame.attention["policy"]["novelty_weight"] == 0.28
    other_capabilities_before = {
        key: value
        for key, value in _capability_rows(restarted).items()
        if key != CURRICULUM_PARAMETER_CAPABILITY
    }

    correction = ProjectFeedback(
        feedback_id="parameter-counterexample",
        project_id="project-local",
        target_episode_id=post_reward.episode_id,
        target_action=post_reward.results[0].frame.decision.get("selected_kind") or "observe_only",
        signal="correction",
        magnitude=1.0,
        natural_language="这次新信息权重偏高，只撤回并重教参数调节。",
        source_ref="user://e4/parameter-counterexample",
        applicability={
            "target_capability": CURRICULUM_PARAMETER_CAPABILITY,
            "effect_key": post_attempt.effect_key,
        },
    )
    corrected = run_feedback_episode(
        tmp_path / "counterexample-feedback.sqlite",
        correction,
        post_reward_activity,
        learning_ledger=restarted,
    )
    assert corrected.curriculum_outcome is not None
    assert corrected.curriculum_outcome.outcome == "counterexample"
    assert restarted.get_curriculum(course_run.curriculum.curriculum_id).status == "reteach"
    other_capabilities_after = {
        key: value
        for key, value in _capability_rows(restarted).items()
        if key != CURRICULUM_PARAMETER_CAPABILITY
    }
    assert other_capabilities_after == other_capabilities_before

    rolled_back = run_project_episode(
        tmp_path / "rolled-back.sqlite",
        _activity("rolled-back", "反例后的新活动应恢复默认注意参数"),
        learning_ledger=ProjectLearningLedger(learning_path),
    )
    rolled_policy = rolled_back.results[0].frame.attention["policy"]
    assert rolled_policy["parameter_version"] == "attention-policy.v1"
    assert rolled_policy["novelty_weight"] == 0.24
    assert rolled_policy["parameter_trials"] == []
    assert not any(
        item.target_capability == CURRICULUM_PARAMETER_CAPABILITY
        for item in rolled_back.curriculum_attempts
    )


def _install_sampling_history(
    ledger: ProjectLearningLedger,
    *,
    capability: str,
    index: int,
) -> CurriculumCandidate:
    course = CurriculumCandidate(
        curriculum_id=f"mature-course-{index}",
        project_id="project-local",
        target_capability=capability,
        source_kind="llm_teacher",
        source_ref=f"teacher-{index}",
        source_episode_ref="sampling-teacher-episode",
        model_receipt_ref=f"sampling-receipt-{index}",
        trigger_features={"evidence_profile_keys": ["evidence_profile:fixture"]},
        suggested_adjustment={"action_adjustments": {"observe_only": 0.1}},
        counterexamples=("fixture counterexample",),
        confidence=0.8,
        uncertainty=0.2,
        status="active_trial",
    )
    ledger.stage_curriculum(course)
    for ordinal in range(4):
        attempt = ledger.record_curriculum_attempt(
            CurriculumAttempt(
                attempt_id=f"mature-attempt-{index}-{ordinal}",
                curriculum_id=course.curriculum_id,
                project_id="project-local",
                target_capability=capability,
                activity_episode_ref=f"mature-opportunity-{index}-{ordinal}",
                activity_ref=f"mature-activity-{index}-{ordinal}",
                feature_key="evidence_profile:fixture",
                target_action="observe_only" if capability == CURRICULUM_TARGET_CAPABILITY else None,
                observed_winner="observe_only",
                contribution=0.01,
                status="attempted",
                effect_key=None if capability == CURRICULUM_TARGET_CAPABILITY else f"effect-{index}",
            )
        )
        ledger.record_curriculum_outcome(
            CurriculumOutcome(
                outcome_id=f"mature-success-{index}-{ordinal}",
                curriculum_id=course.curriculum_id,
                project_id="project-local",
                outcome="success",
                resulting_status="active_trial",
                source_ref=f"feedback-{index}-{ordinal}",
                episode_ref=f"feedback-episode-{index}-{ordinal}",
                attempt_ref=attempt.attempt_id,
            )
        )
    return course


def _mature_sampling_ledger(path: Path) -> tuple[ProjectLearningLedger, dict[str, CurriculumCandidate]]:
    policy = CurriculumMaturityPolicy(
        window_size=4,
        minimum_opportunities=1,
        audit_agreement=0.95,
        reteach_counterexamples=1,
        teaching_rate=1.0,
        trial_rate=0.5,
        audit_rate=0.25,
    )
    ledger = ProjectLearningLedger(path, maturity_policy=policy)
    courses = {
        capability: _install_sampling_history(ledger, capability=capability, index=index)
        for index, capability in enumerate(sorted(CURRICULUM_SUPPORTED_CAPABILITIES))
    }
    return ledger, courses


def test_sampling_requests_only_reteach_and_all_withheld_makes_zero_calls(tmp_path: Path) -> None:
    all_capabilities = tuple(sorted(CURRICULUM_SUPPORTED_CAPABILITIES))
    ledger, courses = _mature_sampling_ledger(tmp_path / "sampling-learning.sqlite")
    # Consume the first 25% audit slot (ordinal 5) for every mature capability.
    primer = ledger.teacher_sampling_plan(
        "project-local",
        "sampling-primer",
        capabilities=all_capabilities,
    )
    assert set(primer["requested_capabilities"]) == set(all_capabilities)
    parameter_course = courses[CURRICULUM_PARAMETER_CAPABILITY]
    ledger.record_curriculum_outcome(
        CurriculumOutcome(
            outcome_id="parameter-local-counterexample",
            curriculum_id=parameter_course.curriculum_id,
            project_id="project-local",
            outcome="counterexample",
            resulting_status="reteach",
            source_ref="user-parameter-counterexample",
            episode_ref="feedback-parameter-counterexample",
        )
    )
    request_transport = ParameterTeacherTransport(emit_candidate=False)
    requested = run_project_episode(
        tmp_path / "parameter-reteach.sqlite",
        _activity("parameter-reteach", "参数反例后的下一次教师抽查"),
        learning_ledger=ledger,
        gateway=_gateway(request_transport),
        governance=_governance(),
        capability=_capability(),
        teacher_capabilities=all_capabilities,
    )
    assert len(request_transport.calls) == 1
    assert requested.teacher_sampling["requested_capabilities"] == [CURRICULUM_PARAMETER_CAPABILITY]
    assert request_transport.calls[0]["requested_capabilities"] == [CURRICULUM_PARAMETER_CAPABILITY]

    withheld_ledger, _ = _mature_sampling_ledger(tmp_path / "withheld-learning.sqlite")
    withheld_ledger.teacher_sampling_plan(
        "project-local",
        "withheld-primer",
        capabilities=all_capabilities,
    )
    withheld_transport = ParameterTeacherTransport(emit_candidate=False)
    activity = _activity("all-withheld", "成熟能力在非抽查机会由本地处理")
    first = run_project_episode(
        tmp_path / "all-withheld.sqlite",
        activity,
        learning_ledger=withheld_ledger,
        gateway=_gateway(withheld_transport),
        governance=_governance(),
        capability=_capability(),
        teacher_capabilities=all_capabilities,
    )
    replay = run_project_episode(
        tmp_path / "all-withheld.sqlite",
        activity,
        learning_ledger=withheld_ledger,
        gateway=_gateway(withheld_transport),
        governance=_governance(),
        capability=_capability(),
        teacher_capabilities=all_capabilities,
    )
    assert first.provider_mode == "locally_withheld_by_sampling"
    assert first.teacher_sampling["all_withheld"] is True
    assert first.teacher_sampling["requested_capabilities"] == []
    assert len(withheld_transport.calls) == 0
    assert replay.results[0].recovered is True
    assert replay.results[0].frame.frame_id == first.results[0].frame.frame_id
    maturity = _capability_rows(withheld_ledger)
    assert set(maturity) == set(all_capabilities)
    assert all(item["actual_teacher_withheld"] >= 1 for item in maturity.values())
    assert all(item["long_term_gate"]["met"] is False for item in maturity.values())


def _parameter_course(
    curriculum_id: str,
    source_activity: str,
    *,
    evidence_profile: str = (
        "evidence_profile:completeness=partial|unknown=1|conflicts=0|remaining=1|next=1"
    ),
) -> CurriculumCandidate:
    source_ref = f"session://e4/{source_activity}"
    return CurriculumCandidate(
        curriculum_id=curriculum_id,
        project_id="project-local",
        target_capability=CURRICULUM_PARAMETER_CAPABILITY,
        source_kind="llm_teacher",
        source_ref=f"teacher://{curriculum_id}",
        source_episode_ref=f"teacher-episode://{curriculum_id}",
        model_receipt_ref=f"teacher-receipt://{curriculum_id}",
        trigger_features={
            "evidence_profile_keys": [evidence_profile],
            "source_input_refs": [source_ref, source_activity],
        },
        suggested_adjustment={
            "parameter_adjustments": [
                {
                    "parameter": "attention.novelty_weight",
                    "delta": 0.04,
                    "expected_direction": "increase",
                    "source_refs": [source_ref],
                    "rationale": "同一证据画像下提高新颖性注意的可撤销试用",
                }
            ]
        },
        counterexamples=("后续注意效果未改善时撤回",),
        confidence=0.8,
        uncertainty=0.2,
        status="staged",
        extra={"source_activity_ref": source_activity},
    )


def test_reteach_generation_closes_only_after_replacement_readback(tmp_path: Path) -> None:
    policy = CurriculumMaturityPolicy(
        window_size=4,
        minimum_opportunities=1,
        audit_agreement=0.95,
        reteach_counterexamples=1,
        teaching_rate=1.0,
        trial_rate=0.5,
        audit_rate=0.25,
    )
    learning_path = tmp_path / "replacement-learning.sqlite"
    ledger = ProjectLearningLedger(learning_path, maturity_policy=policy)
    old = _parameter_course("parameter-generation-old", "generation-old-source")
    old_run = run_curriculum_episode(
        tmp_path / "generation-old-curriculum.sqlite",
        old,
        learning_ledger=ledger,
    )
    assert old_run.curriculum.status == "active_trial"

    used = run_project_episode(
        tmp_path / "generation-old-use.sqlite",
        _activity("generation-old-use", "旧参数课程进入一个独立使用机会"),
        learning_ledger=ledger,
    )
    old_attempt = next(
        item
        for item in used.curriculum_attempts
        if item.target_capability == CURRICULUM_PARAMETER_CAPABILITY
    )
    correction = ProjectFeedback(
        feedback_id="generation-old-counterexample",
        project_id="project-local",
        target_episode_id=used.episode_id,
        target_action=used.results[0].frame.decision.get("selected_kind") or "observe_only",
        signal="correction",
        magnitude=1.0,
        natural_language="旧参数在这个证据画像下偏高，需要重新教学。",
        source_ref="user://e4/generation-old-counterexample",
        applicability={
            "target_capability": CURRICULUM_PARAMETER_CAPABILITY,
            "effect_key": old_attempt.effect_key,
        },
    )
    corrected = run_feedback_episode(
        tmp_path / "generation-old-feedback.sqlite",
        correction,
        _activity("generation-old-use", "旧参数课程进入一个独立使用机会"),
        learning_ledger=ledger,
    )
    assert corrected.curriculum_outcome is not None
    assert corrected.curriculum_outcome.outcome == "counterexample"
    assert ledger.get_curriculum(old.curriculum_id).status == "reteach"
    before_replacement = _capability_rows(ledger)
    other_capabilities_before = {
        key: {
            field: value[field]
            for field in (
                "curriculum_count",
                "active_trials",
                "opportunities",
                "attempted",
                "withheld",
                "success",
                "counterexample",
                "unknown",
                "state",
                "maturity_level",
                "ownership",
            )
        }
        for key, value in before_replacement.items()
        if key != CURRICULUM_PARAMETER_CAPABILITY
    }

    replacement = _parameter_course("parameter-generation-new", "generation-new-source")
    ledger.stage_curriculum(replacement)
    # A teacher proposal by itself has no authority to clear the old failure.
    assert ledger.get_curriculum(old.curriculum_id).status == "reteach"
    assert _capability_rows(ledger)[CURRICULUM_PARAMETER_CAPABILITY]["state"] == "reteach"

    replacement_run = run_curriculum_episode(
        tmp_path / "generation-new-curriculum.sqlite",
        replacement,
        learning_ledger=ledger,
    )
    assert replacement_run.curriculum.status == "active_trial"
    assert replacement_run.outcome is not None
    assert replacement_run.outcome.extra["replaces_curriculum_ids"] == [old.curriculum_id]
    assert ledger.get_curriculum(old.curriculum_id).status == "retracted"

    resumed = run_project_episode(
        tmp_path / "generation-new-use.sqlite",
        _activity("generation-new-use", "新课程在后续独立活动中恢复有界参数试用"),
        learning_ledger=ProjectLearningLedger(learning_path, maturity_policy=policy),
    )
    new_attempt = next(
        item
        for item in resumed.curriculum_attempts
        if item.target_capability == CURRICULUM_PARAMETER_CAPABILITY
    )
    assert new_attempt.curriculum_id == replacement.curriculum_id
    assert resumed.results[0].frame.attention["policy"]["novelty_weight"] == 0.28

    reopened = ProjectLearningLedger(learning_path, maturity_policy=policy)
    snapshot = reopened.snapshot("project-local")
    maturity = {
        item["target_capability"]: item for item in snapshot["capability_maturity"]
    }
    assert maturity[CURRICULUM_PARAMETER_CAPABILITY]["state"] == "trial"
    assert maturity[CURRICULUM_PARAMETER_CAPABILITY]["counterexample"] == 0
    assert {
        key: {
            field: value[field]
            for field in (
                "curriculum_count",
                "active_trials",
                "opportunities",
                "attempted",
                "withheld",
                "success",
                "counterexample",
                "unknown",
                "state",
                "maturity_level",
                "ownership",
            )
        }
        for key, value in maturity.items()
        if key != CURRICULUM_PARAMETER_CAPABILITY
    } == other_capabilities_before

    old_projection = next(
        item for item in snapshot["curricula"] if item["curriculum_id"] == old.curriculum_id
    )
    assert old_projection["status"] == "retracted"
    assert old_projection["outcome_counts"]["counterexample"] == 1
    assert old_projection["outcome_counts"]["replaced"] == 1
    assert old_projection["replacement"]["replaced_by_curriculum_id"] == replacement.curriculum_id
    assert old_projection["replacement"]["historical_counterexamples_preserved"] is True
    assert old_projection["maturity"]["state"] == "retracted"
    assert old_projection["maturity"]["teacher_intervention_rate"] == 0.0

    first_sampling = reopened.teacher_sampling_plan(
        "project-local",
        "replacement-sampling-first",
        capabilities=(CURRICULUM_PARAMETER_CAPABILITY,),
    )
    second_sampling = reopened.teacher_sampling_plan(
        "project-local",
        "replacement-sampling-second",
        capabilities=(CURRICULUM_PARAMETER_CAPABILITY,),
    )
    assert first_sampling["capabilities"][0]["state"] == "trial"
    assert first_sampling["capabilities"][0]["rate"] == 0.5
    assert second_sampling["requested_capabilities"] == []
    assert second_sampling["capabilities"][0]["reason"] == "locally_withheld_by_sampling"

    different_scope = _parameter_course(
        "parameter-generation-different-scope",
        "generation-different-scope-source",
        evidence_profile=(
            "evidence_profile:completeness=complete|unknown=0|conflicts=0|remaining=0|next=0"
        ),
    ).with_status("reteach")
    reopened.stage_curriculum(different_scope)
    another_same_scope = _parameter_course(
        "parameter-generation-another-same-scope",
        "generation-another-source",
    )
    run_curriculum_episode(
        tmp_path / "generation-another-curriculum.sqlite",
        another_same_scope,
        learning_ledger=reopened,
    )
    assert reopened.get_curriculum(different_scope.curriculum_id).status == "reteach"


def test_learning_ledger_migrates_pre_e3_opportunity_table(tmp_path: Path) -> None:
    path = tmp_path / "legacy-learning.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE capability_opportunities (
                opportunity_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                target_capability TEXT NOT NULL,
                activity_episode_ref TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(project_id, target_capability, activity_episode_ref),
                UNIQUE(project_id, target_capability, ordinal)
            );
            """
        )
    ProjectLearningLedger(path)
    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(capability_opportunities)")
        }
    assert {
        "teacher_requested",
        "sampling_state",
        "sampling_rate",
        "sampling_period",
        "sampling_reason",
    }.issubset(columns)
