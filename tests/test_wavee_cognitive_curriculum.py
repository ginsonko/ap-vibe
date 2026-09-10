from __future__ import annotations

import json
from pathlib import Path

from ap_mind.contracts import CapabilityOwnership
from ap_mind.gateway import OpenAICompatibleGateway
from ap_mind.governance import GovernanceCompatibilityRecord
from ap_mind.runtime import MindRuntime
from ap_mind.storage import EventStore
from ap_mind.studio_server import HYBRID_WHITEPAPER_SHA256, StudioEpisodeService
from ap_mind.vibe_mind import (
    CurriculumCandidate,
    ProjectActivity,
    ProjectCognitionEnvironment,
    ProjectFeedback,
    ProjectLearningLedger,
    run_feedback_episode,
    run_curriculum_episode,
    run_project_episode,
)


def _governance() -> GovernanceCompatibilityRecord:
    return GovernanceCompatibilityRecord(
        record_id="wave-e1-test",
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
        reason="bounded E1 fixture",
    )


class CognitiveTeacherTransport:
    def __init__(self, memory_ref: str) -> None:
        self.memory_ref = memory_ref
        self.calls = 0

    def request_json(self, method: str, url: str, **kwargs):
        del method, url
        self.calls += 1
        context = json.loads(kwargs["body"]["messages"][1]["content"])["frame_view"]
        event_ref = context["sa"]["event_ref"]
        return {
            "choices": [{"message": {"content": json.dumps({
                "status": "proposal",
                "recall_candidates": [{
                    "memory_ref": self.memory_ref,
                    "summary": "先前同主题的真实项目经历",
                    "relevance": 0.9,
                    "rationale": "与当前未闭合证据相关",
                }],
                "appraisal_candidates": [{
                    "name": "task_open",
                    "intensity": 0.8,
                    "valence": -0.2,
                    "rationale": "本地结构显示仍有未闭合项",
                    "subject_scope": "private",
                    "source_refs": [event_ref],
                }],
                "prediction_candidates": [],
                "thought_candidates": [],
                "candidate_preferences": {},
                "selected_candidate_ref": None,
                "lesson_candidates": [],
                "uncertainty": 0.2,
                "limitations": ["teacher_effect_requires_later_independent_episode"],
            }, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 30, "total_tokens": 60},
        }


def _activity(activity_id: str, summary: str, *, project_id: str = "project-local") -> ProjectActivity:
    return ProjectActivity(
        activity_id=activity_id,
        project_id=project_id,
        kind=f"unseen-{activity_id}",
        summary=summary,
        detail="真实页面仍需要回读",
        source_ref=f"session://e1/{activity_id}",
        completeness="partial",
        observed_remaining=("浏览器现实回读",),
        observed_unknown=("真实结果尚未确认",),
    )


def test_shared_project_memory_enters_the_existing_b_recall_across_episodes(tmp_path: Path) -> None:
    ledger = ProjectLearningLedger(tmp_path / "learning.sqlite")
    first = _activity("history", "恢复页面的浏览器回读仍未闭合")
    run_project_episode(tmp_path / "first.sqlite", first, learning_ledger=ledger)

    second = _activity("later", "需要继续核对恢复页面的浏览器结果")
    run = run_project_episode(tmp_path / "second.sqlite", second, learning_ledger=ledger)

    recalled = next(item for item in run.results[0].frame.b_recall if item.event_ref == first.as_event(
        runtime_id="x", organism_id="y", episode_id="z"
    ).event_id)
    assert recalled.source == "memory"
    assert recalled.summary == first.summary
    assert recalled.base_score == recalled.score
    assert recalled.curriculum_gain == 0.0
    assert recalled.event_ref in run.results[0].frame.current_field["members"]

    reopened = ProjectLearningLedger(tmp_path / "learning.sqlite")
    assert reopened.memory_events("project-local")[0].event_id == recalled.event_ref
    assert reopened.memory_events("another-project") == ()


def test_recall_and_appraisal_courses_are_adopted_then_affect_a_later_episode(tmp_path: Path) -> None:
    ledger = ProjectLearningLedger(tmp_path / "learning.sqlite")
    history = _activity("history", "恢复页面的浏览器回读仍未闭合")
    run_project_episode(tmp_path / "history.sqlite", history, learning_ledger=ledger)
    memory_ref = ledger.memory_events("project-local")[0].event_id

    transport = CognitiveTeacherTransport(memory_ref)
    gateway = OpenAICompatibleGateway(
        "https://provider.invalid/v1", "fixture-secret", "gpt-fixture",
        transport=transport, max_retries=0,
    )
    source = _activity("teacher-source", "恢复页面仍缺少浏览器现实回读")
    taught = run_project_episode(
        tmp_path / "teacher.sqlite", source,
        learning_ledger=ledger, gateway=gateway,
        governance=_governance(), capability=_capability(),
    )
    assert transport.calls == 1
    assert {item.curriculum.target_capability for item in taught.curriculum_runs} == {
        "project.recall", "project.appraisal"
    }
    assert all(item.curriculum.status == "active_trial" for item in taught.curriculum_runs)
    assert not any(item.curriculum_refs for item in taught.results[0].frame.b_recall)
    assert not any(item.curriculum_refs for item in taught.results[0].frame.feelings)

    later = _activity("later-opportunity", "核对恢复页面并确认浏览器的真实结果")
    influenced = run_project_episode(tmp_path / "later.sqlite", later, learning_ledger=ledger)
    recall = next(item for item in influenced.results[0].frame.b_recall if item.event_ref == memory_ref)
    feeling = next(item for item in influenced.results[0].frame.feelings if item.name == "task_open")
    assert recall.curriculum_gain > 0
    assert recall.score > recall.base_score
    assert recall.source == "memory_assisted_trial"
    assert feeling.source == "assisted_trial"
    assert feeling.base_intensity == 0.0
    assert feeling.curriculum_delta > 0
    assert {item.target_capability for item in influenced.curriculum_attempts} == {
        "project.recall", "project.appraisal"
    }
    snapshot = influenced.learning_snapshot
    maturity = {item["target_capability"]: item["maturity"] for item in snapshot["curricula"]}
    assert maturity["project.recall"]["opportunities"] == 1
    assert maturity["project.appraisal"]["opportunities"] == 1
    assert maturity["project.recall"]["state"] == "insufficient_evidence"
    assert maturity["project.recall"]["teacher_intervention_rate"] == 1.0
    aggregated = {item["target_capability"]: item for item in snapshot["capability_maturity"]}
    assert set(aggregated) == {
        "project.action_selection",
        "project.recall",
        "project.appraisal",
        "project.prediction",
        "project.thought",
        "project.paradigm",
        "project.attention",
        "project.expression",
        "project.parameter_tuning",
    }
    assert aggregated["project.prediction"]["maturity_level"] == "L0"
    assert aggregated["project.prediction"]["state"] == "no_local_course"
    assert aggregated["project.recall"]["opportunities"] == 1
    assert aggregated["project.appraisal"]["attempted"] == 1

    feedback = ProjectFeedback(
        feedback_id="recall-counterexample",
        project_id="project-local",
        target_episode_id=influenced.episode_id,
        target_action="stage_knowledge_candidate",
        desired_action="stage_knowledge_candidate",
        signal="correction",
        magnitude=1.0,
        natural_language="这次被增强的历史记忆并不相关，请只重教召回。",
        source_ref="user://e1/recall-counterexample",
        applicability={"target_capability": "project.recall", "effect_key": memory_ref},
    )
    corrected = run_feedback_episode(
        tmp_path / "feedback.sqlite", feedback, later, learning_ledger=ledger
    )
    assert corrected.curriculum_outcome is not None
    assert corrected.curriculum_outcome.outcome == "counterexample"
    recall_curriculum = next(
        item for item in taught.curriculum_runs
        if item.curriculum.target_capability == "project.recall"
    ).curriculum
    appraisal_curriculum = next(
        item for item in taught.curriculum_runs
        if item.curriculum.target_capability == "project.appraisal"
    ).curriculum
    assert ledger.get_curriculum(recall_curriculum.curriculum_id).status == "reteach"
    assert ledger.get_curriculum(appraisal_curriculum.curriculum_id).status == "active_trial"
    assert corrected.lesson is not None
    assert corrected.lesson.target_capability == "project.recall"
    assert ledger.preferences(later) == {}


def test_cognitive_trial_observations_survive_a_cold_frame_replay(tmp_path: Path) -> None:
    ledger = ProjectLearningLedger(tmp_path / "learning.sqlite")
    history = _activity("cold-history", "恢复页面的浏览器回读仍未闭合")
    run_project_episode(tmp_path / "history.sqlite", history, learning_ledger=ledger)
    memory_ref = ledger.memory_events("project-local")[0].event_id

    gateway = OpenAICompatibleGateway(
        "https://provider.invalid/v1",
        "fixture-secret",
        "gpt-fixture",
        transport=CognitiveTeacherTransport(memory_ref),
        max_retries=0,
    )
    source = _activity("cold-teacher-source", "恢复页面仍缺少浏览器现实回读")
    run_project_episode(
        tmp_path / "teacher.sqlite",
        source,
        learning_ledger=ledger,
        gateway=gateway,
        governance=_governance(),
        capability=_capability(),
    )

    later = _activity("cold-later", "核对恢复页面并确认浏览器的真实结果")
    episode_path = tmp_path / "later.sqlite"
    first_run = run_project_episode(episode_path, later, learning_ledger=ledger)
    first_observations = first_run.results[0].frame.cognitive_trial_observations
    assert first_observations["project.recall"][0]["status"] == "entered_b_recall"
    assert first_observations["project.appraisal"][0]["status"] == "entered_appraisal"

    runtime_id = f"ap-vibe-runtime:{later.project_id}"
    organism_id = f"ap-vibe-organism:{later.project_id}"
    event = later.as_event(
        runtime_id=runtime_id,
        organism_id=organism_id,
        episode_id=first_run.episode_id,
    )
    with EventStore(episode_path) as reopened_store:
        replay_runtime = MindRuntime(
            reopened_store,
            ProjectCognitionEnvironment(later.project_id),
            runtime_id=runtime_id,
            organism_id=organism_id,
            episode_id=first_run.episode_id,
        )
        replayed = replay_runtime.tick(event)

    assert replayed.recovered is True
    assert replayed.frame.cognitive_trial_observations == first_observations
    assert replayed.frame.b_recall[0].summary
    assert replayed.frame.b_recall[0].base_score is not None
    assert replayed.frame.b_recall[0].curriculum_gain > 0
    assert any(item.curriculum_delta > 0 for item in replayed.frame.feelings)


def test_invalid_memory_ref_and_unmatched_appraisal_signal_never_enter_cognition(tmp_path: Path) -> None:
    ledger = ProjectLearningLedger(tmp_path / "learning.sqlite")
    source = _activity("source", "一条部分证据")
    profile = "evidence_profile:completeness=partial|unknown=1|conflicts=0|remaining=1|next=0"
    invalid = CurriculumCandidate(
        curriculum_id="invalid-recall",
        project_id="project-local",
        target_capability="project.recall",
        source_kind="llm_teacher",
        source_ref="teacher-invalid",
        source_episode_ref="episode-source",
        model_receipt_ref="receipt-invalid",
        trigger_features={"evidence_profile_keys": [profile], "source_memory_refs": ["real-ref"]},
        suggested_adjustment={"memory_gain_adjustments": {"invented-ref": 1.0}},
        confidence=0.9,
        uncertainty=0.1,
    )
    invalid_run = run_curriculum_episode(tmp_path / "invalid.sqlite", invalid, learning_ledger=ledger)
    assert invalid_run.curriculum.status in {"deferred", "rejected"}

    appraisal = CurriculumCandidate(
        curriculum_id="appraisal-course",
        project_id="project-local",
        target_capability="project.appraisal",
        source_kind="llm_teacher",
        source_ref="teacher-appraisal",
        source_episode_ref="episode-source",
        model_receipt_ref="receipt-appraisal",
        trigger_features={"evidence_profile_keys": [profile]},
        suggested_adjustment={"appraisal_effects": [{
            "name": "task_open", "intensity_delta": 1.0,
            "required_signals": ["conflict_present"],
        }]},
        confidence=0.9,
        uncertainty=0.1,
    )
    adopted = run_curriculum_episode(tmp_path / "appraisal.sqlite", appraisal, learning_ledger=ledger)
    assert adopted.curriculum.status == "active_trial"
    run = run_project_episode(tmp_path / "later.sqlite", source, learning_ledger=ledger)
    assert not any(item.name == "task_open" for item in run.results[0].frame.feelings)
    appraisal_attempt = next(item for item in run.curriculum_attempts if item.target_capability == "project.appraisal")
    assert appraisal_attempt.status == "withheld"
    assert appraisal_attempt.extra["usage_status"] == "withheld_signal_mismatch"


def test_negative_non_action_effect_keeps_user_feedback_meaning(tmp_path: Path) -> None:
    ledger = ProjectLearningLedger(tmp_path / "learning.sqlite")
    history = _activity("history-negative", "恢复页面的浏览器回读仍未闭合")
    history_run = run_project_episode(tmp_path / "history.sqlite", history, learning_ledger=ledger)
    memory_ref = ledger.memory_events("project-local")[0].event_id
    profile = "evidence_profile:completeness=partial|unknown=1|conflicts=0|remaining=1|next=0"
    curriculum = CurriculumCandidate(
        curriculum_id="negative-recall-course",
        project_id="project-local",
        target_capability="project.recall",
        source_kind="llm_teacher",
        source_ref="teacher-negative-recall",
        source_episode_ref=history_run.episode_id,
        model_receipt_ref="receipt-negative-recall",
        trigger_features={"evidence_profile_keys": [profile], "source_memory_refs": [memory_ref]},
        suggested_adjustment={"memory_gain_adjustments": {memory_ref: -1.0}},
        confidence=0.9,
        uncertainty=0.1,
        extra={"source_activity_ref": history.activity_id},
    )
    assert run_curriculum_episode(tmp_path / "curriculum.sqlite", curriculum, learning_ledger=ledger).curriculum.status == "active_trial"

    later = _activity("later-negative", "恢复页面的浏览器回读仍未闭合")
    influenced = run_project_episode(tmp_path / "later.sqlite", later, learning_ledger=ledger)
    attempt = next(item for item in influenced.curriculum_attempts if item.target_capability == "project.recall")
    assert attempt.contribution < 0

    correction = ProjectFeedback(
        feedback_id="negative-effect-correction",
        project_id="project-local",
        target_episode_id=influenced.episode_id,
        target_action="stage_knowledge_candidate",
        signal="correction",
        magnitude=1.0,
        natural_language="降低这条记忆的相关性是不对的。",
        source_ref="user://negative-effect-correction",
        applicability={"target_capability": "project.recall", "effect_key": memory_ref},
    )
    corrected = run_feedback_episode(tmp_path / "feedback.sqlite", correction, later, learning_ledger=ledger)
    assert corrected.curriculum_outcome is not None
    assert corrected.curriculum_outcome.outcome == "counterexample"
    assert ledger.get_curriculum(curriculum.curriculum_id).status == "reteach"


def test_service_projection_exposes_effects_and_maturity_without_provider(tmp_path: Path) -> None:
    service = StudioEpisodeService(tmp_path / "service")
    first = _activity("service-first", "项目恢复页面等待现实回读", project_id="ap-vibe-local")
    second = _activity("service-second", "继续核对项目恢复页面", project_id="ap-vibe-local")
    service.run_project_activity("request-first", first.to_dict())
    view, replayed = service.run_project_activity("request-second", second.to_dict())
    assert replayed is False
    assert view["teacher"]["mode"] == "provider_off"
    assert view["ticks"][0]["recall"]
    assert view["ticks"][0]["recall"][0]["summary"] == first.summary
    assert view["learning"]["applicable_cognitive_trials"] == {
        "project.recall": [],
        "project.appraisal": [],
        "project.prediction": [],
        "project.thought": [],
    }
    assert view["ownership"]["recall"] == "native_shallow"
