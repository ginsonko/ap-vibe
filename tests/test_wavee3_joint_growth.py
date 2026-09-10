from __future__ import annotations

import json
from pathlib import Path

from ap_mind.contracts import CapabilityOwnership
from ap_mind.gateway import OpenAICompatibleGateway
from ap_mind.governance import GovernanceCompatibilityRecord
from ap_mind.studio_server import HYBRID_WHITEPAPER_SHA256
from ap_mind.vibe_mind import (
    CurriculumCandidate,
    CurriculumMaturityPolicy,
    CurriculumOutcome,
    ProjectActivity,
    ProjectFeedback,
    ProjectLearningLedger,
    run_feedback_episode,
    run_project_episode,
)
from ap_mind.vibe_projection import project_activity_run


E3_CAPABILITIES = (
    "project.paradigm",
    "project.attention",
    "project.expression",
)


class JointTeacherTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request_json(self, method: str, url: str, **kwargs):
        frame = json.loads(kwargs["body"]["messages"][1]["content"])["frame_view"]
        self.calls.append(frame)
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
                    {"name": "next_action", "source": "activity.observed_next_action", "required": True},
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
                "counterexamples": ["任务已闭合时不套用"],
            }],
            "attention_candidates": [{
                "mode": "maintain_attention",
                "target_ref": event_ref,
                "gain_delta": 0.18,
                "rationale": "当前未闭合输入有直接信息价值",
                "source_refs": [event_ref],
                "uncertainty": 0.2,
            }],
            "expression_candidates": [{
                "template": "我目前能确认的是：{claim}。",
                "tone": "careful",
                "evidence_refs": [proposition_ref],
                "uncertainty": 0.15,
                "counterexamples": ["无需强调证据边界时保持原表达"],
            }],
            "candidate_preferences": {},
            "selected_candidate_ref": None,
            "lesson_candidates": [],
            "uncertainty": 0.2,
            "limitations": ["teacher_has_no_external_truth"],
        }
        return {
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


def _activity(activity_id: str, summary: str) -> ProjectActivity:
    return ProjectActivity(
        activity_id=activity_id,
        project_id="project-local",
        kind=f"unseen-{activity_id}",
        summary=summary,
        detail="真实页面效果仍需要观察。",
        source_ref=f"session://e3/{activity_id}",
        completeness="partial",
        observed_remaining=("页面现实回读",),
        observed_unknown=("用户是否看到预期效果",),
        observed_next_action="读取页面现实结果",
    )


def _gateway(transport: JointTeacherTransport) -> OpenAICompatibleGateway:
    return OpenAICompatibleGateway(
        "https://provider.invalid/v1",
        "fixture-secret",
        "gpt-fixture",
        transport=transport,
        max_retries=0,
    )


def _governance() -> GovernanceCompatibilityRecord:
    return GovernanceCompatibilityRecord(
        record_id="wave-e3-test",
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
        reason="bounded E3 fixture",
    )


def test_joint_courses_change_later_frame_and_expression_feedback_is_local(tmp_path: Path) -> None:
    ledger_path = tmp_path / "learning.sqlite"
    ledger = ProjectLearningLedger(ledger_path)
    transport = JointTeacherTransport()
    taught = run_project_episode(
        tmp_path / "teacher.sqlite",
        _activity("teacher-source", "实现已结束但页面现实结果尚未闭合"),
        learning_ledger=ledger,
        gateway=_gateway(transport),
        governance=_governance(),
        capability=_capability(),
        teacher_capabilities=E3_CAPABILITIES,
    )
    assert len(transport.calls) == 1
    assert transport.calls[0]["requested_capabilities"] == list(E3_CAPABILITIES)
    assert {item.curriculum.target_capability for item in taught.curriculum_runs} == set(E3_CAPABILITIES)
    assert all(item.curriculum.status == "active_trial" for item in taught.curriculum_runs)
    assert not taught.results[0].frame.paradigms
    assert taught.results[0].frame.expression_draft.renderer_source == "ap_native"
    taught_view = project_activity_run("teacher-view", taught).to_dict()
    assert taught_view["projection_version"] == "0.9.0"
    assert taught_view["teacher"]["requested_capabilities"] == list(E3_CAPABILITIES)
    assert [
        item["target_capability"]
        for item in taught_view["teacher"]["sampling"]["capabilities"]
    ] == list(E3_CAPABILITIES)
    assert len(taught_view["teacher"]["candidates"]["paradigm"]) == 1
    assert len(taught_view["teacher"]["candidates"]["attention"]) == 1
    assert len(taught_view["teacher"]["candidates"]["expression"]) == 1

    later = _activity("later-opportunity", "继续处理另一项尚未闭合的页面观察")
    influenced = run_project_episode(tmp_path / "later.sqlite", later, learning_ledger=ledger)
    frame = influenced.results[0].frame
    attempts = {item.target_capability: item for item in influenced.curriculum_attempts}
    assert set(attempts) == set(E3_CAPABILITIES)

    paradigm = frame.paradigms[0]
    assert paradigm.status == "matched"
    assert paradigm.bindings["claim"] == frame.proposition.content
    assert paradigm.bindings["next_action"] == later.observed_next_action
    assert attempts["project.paradigm"].contribution > 0.0
    assert attempts["project.paradigm"].effective_result["match"] >= attempts["project.paradigm"].local_before["match"]
    assert attempts["project.paradigm"].effective_result["match"] <= 1.0

    attention = attempts["project.attention"]
    assert attention.local_before["gain"] < attention.effective_result["gain"]
    assert attention.effective_result["won_and_readback"] is True
    assert frame.decision["selected_candidate_ref"] == attention.effective_result["candidate_ref"]

    expression = attempts["project.expression"]
    assert expression.effective_result["semantic_preserved"] is True
    assert expression.local_before["proposition"] == expression.effective_result["proposition"]
    assert expression.local_before["surface"] != expression.effective_result["surface"]
    assert frame.expression_draft.public_allowed is False
    influenced_view = project_activity_run("influenced-view", influenced).to_dict()
    assert influenced_view["ticks"][0]["paradigms"][0]["status"] == "matched"
    assert any(
        item.get("curriculum_refs")
        for item in influenced_view["ticks"][0]["attention"]["candidates"]
    )
    assert influenced_view["ticks"][0]["expression_draft"]["renderer_source"] == "assisted_curriculum"
    assert influenced_view["ownership"]["paradigm"] == "assisted"
    assert influenced_view["ownership"]["attention"] == "assisted"
    assert influenced_view["ownership"]["expression"] == "assisted"

    expression_course = next(
        item.curriculum for item in taught.curriculum_runs
        if item.curriculum.target_capability == "project.expression"
    )
    correction = ProjectFeedback(
        feedback_id="expression-only-correction",
        project_id="project-local",
        target_episode_id=influenced.episode_id,
        target_action=frame.decision.get("selected_kind") or "observe_only",
        signal="correction",
        magnitude=1.0,
        natural_language="这一层表达外壳不适合，只重教表达。",
        source_ref="user://e3/expression-correction",
        applicability={
            "target_capability": "project.expression",
            "effect_key": expression.effect_key,
        },
    )
    corrected = run_feedback_episode(
        tmp_path / "feedback.sqlite", correction, later, learning_ledger=ledger
    )
    assert corrected.curriculum_outcome is not None
    assert corrected.curriculum_outcome.outcome == "counterexample"
    assert ledger.get_curriculum(expression_course.curriculum_id).status == "reteach"
    assert all(
        ledger.get_curriculum(item.curriculum.curriculum_id).status == "active_trial"
        for item in taught.curriculum_runs
        if item.curriculum.target_capability != "project.expression"
    )

    after = run_project_episode(
        tmp_path / "after.sqlite",
        _activity("after-restart", "冷重启后继续处理新的未闭合页面观察"),
        learning_ledger=ProjectLearningLedger(ledger_path),
    )
    after_frame = after.results[0].frame
    assert after_frame.paradigms and after_frame.paradigms[0].curriculum_refs
    assert any(
        item.get("curriculum_refs")
        for item in after_frame.attention.get("candidates", ())
    )
    assert after_frame.expression_draft.renderer_source == "ap_native"


def _install_mature_course(
    ledger: ProjectLearningLedger,
    *,
    capability: str,
    index: int,
) -> None:
    curriculum = CurriculumCandidate(
        curriculum_id=f"course-{capability}-{index}",
        project_id="project-local",
        target_capability=capability,
        source_kind="llm_teacher",
        source_ref=f"teacher-{capability}",
        source_episode_ref="episode-teacher",
        model_receipt_ref="receipt-teacher",
        trigger_features={"evidence_profile_keys": ["fixture"]},
        suggested_adjustment={
            "paradigm_patterns": [{
                "pattern_kind": "relation_frame",
                "invariants": {"has_open_items": True},
                "slots": [{"name": "claim", "source": "proposition.content", "required": True}],
                "relations": [],
                "completeness": "partial",
            }]
        },
        counterexamples=("fixture counterexample",),
        confidence=0.8,
        uncertainty=0.2,
        status="active_trial",
    )
    ledger.stage_curriculum(curriculum)
    return None


def test_sampling_changes_real_requested_set_and_counterexample_restores_one_capability(tmp_path: Path) -> None:
    policy = CurriculumMaturityPolicy(
        window_size=4,
        minimum_opportunities=1,
        audit_agreement=0.95,
        reteach_counterexamples=1,
        teaching_rate=1.0,
        trial_rate=0.5,
        audit_rate=0.25,
    )
    ledger = ProjectLearningLedger(tmp_path / "learning.sqlite", maturity_policy=policy)
    for index, capability in enumerate(E3_CAPABILITIES):
        _install_mature_course(ledger, capability=capability, index=index)
        # Four independent, explicitly attributed successes put the next
        # ordinal on the first 25% audit slot. Production still retains the
        # default 20/100/95% policy.
        from ap_mind.vibe_mind import CurriculumAttempt
        for opportunity in range(4):
            attempt = ledger.record_curriculum_attempt(CurriculumAttempt(
                attempt_id=f"attempt-{capability}-{opportunity}",
                curriculum_id=f"course-{capability}-{index}",
                project_id="project-local",
                target_capability=capability,
                activity_episode_ref=f"opportunity-{capability}-{opportunity}",
                activity_ref=f"activity-{capability}-{opportunity}",
                feature_key="fixture",
                target_action=None,
                observed_winner="observe_only",
                contribution=0.1,
                status="attempted",
                effect_key=f"effect-{capability}",
            ))
            ledger.record_curriculum_outcome(CurriculumOutcome(
                outcome_id=f"success-{capability}-{opportunity}",
                curriculum_id=f"course-{capability}-{index}",
                project_id="project-local",
                outcome="success",
                resulting_status="active_trial",
                source_ref=f"feedback-{capability}-{opportunity}",
                episode_ref=f"feedback-episode-{capability}-{opportunity}",
                attempt_ref=attempt.attempt_id,
            ))

    transport = JointTeacherTransport()
    first = run_project_episode(
        tmp_path / "sample-first.sqlite",
        _activity("sample-first", "成熟能力进入本地低频抽查的第一机会"),
        learning_ledger=ledger,
        gateway=_gateway(transport),
        governance=_governance(),
        capability=_capability(),
        teacher_capabilities=E3_CAPABILITIES,
    )
    assert len(transport.calls) == 1
    assert first.teacher_sampling["requested_capabilities"] == list(E3_CAPABILITIES)

    second = run_project_episode(
        tmp_path / "sample-second.sqlite",
        _activity("sample-second", "成熟能力进入本地低频抽查的第二机会"),
        learning_ledger=ledger,
        gateway=_gateway(transport),
        governance=_governance(),
        capability=_capability(),
        teacher_capabilities=E3_CAPABILITIES,
    )
    assert len(transport.calls) == 1
    assert second.provider_mode == "locally_withheld_by_sampling"
    assert second.teacher_sampling["requested_capabilities"] == []
    assert "locally_withheld_by_sampling" in second.results[0].frame.gateway["limitations"]
    second_view = project_activity_run("sampling-withheld-view", second).to_dict()
    assert second_view["teacher"]["mode"] == "locally_withheld_by_sampling"
    assert second_view["teacher"]["requested_capabilities"] == []
    assert second_view["teacher"]["sampling"]["all_withheld"] is True
    assert second_view["ownership"]["teacher"] == "absent"
    e3_curricula = [
        item
        for item in second_view["learning"]["curricula"]
        if item["curriculum_id"].startswith("course-project.")
    ]
    assert len(e3_curricula) == 3
    assert all(
        item["maturity"]["teacher_intervention_rate"] == policy.audit_rate
        for item in e3_curricula
    )
    assert all(
        item["maturity"]["state"] == "low_frequency_audit"
        for item in e3_curricula
    )

    expression = next(
        item for item in ledger.curricula("project-local")
        if item.target_capability == "project.expression"
    )
    ledger.record_curriculum_outcome(CurriculumOutcome(
        outcome_id="expression-counterexample",
        curriculum_id=expression.curriculum_id,
        project_id="project-local",
        outcome="counterexample",
        resulting_status="reteach",
        source_ref="user-expression-counterexample",
        episode_ref="feedback-expression-counterexample",
    ))
    third = run_project_episode(
        tmp_path / "sample-third.sqlite",
        _activity("sample-third", "表达反例后只有表达恢复教学"),
        learning_ledger=ledger,
        gateway=_gateway(transport),
        governance=_governance(),
        capability=_capability(),
        teacher_capabilities=E3_CAPABILITIES,
    )
    assert len(transport.calls) == 2
    assert third.teacher_sampling["requested_capabilities"] == ["project.expression"]
    assert transport.calls[-1]["requested_capabilities"] == ["project.expression"]
