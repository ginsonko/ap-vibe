from __future__ import annotations

import json
from pathlib import Path

from ap_mind.contracts import CapabilityOwnership
from ap_mind.gateway import OpenAICompatibleGateway
from ap_mind.governance import GovernanceCompatibilityRecord
from ap_mind.runtime import MindRuntime
from ap_mind.storage import EventStore
from ap_mind.studio_server import HYBRID_WHITEPAPER_SHA256
from ap_mind.vibe_mind import (
    ProjectActivity,
    ProjectCognitionEnvironment,
    ProjectFeedback,
    ProjectLearningLedger,
    run_feedback_episode,
    run_project_episode,
)


class PredictionThoughtTeacher:
    def __init__(self) -> None:
        self.calls = 0

    def request_json(self, method: str, url: str, **kwargs):
        del method, url
        self.calls += 1
        frame = json.loads(kwargs["body"]["messages"][1]["content"])["frame_view"]
        event_ref = frame["sa"]["event_ref"]
        return {
            "choices": [{"message": {"content": json.dumps({
                "status": "proposal",
                "recall_candidates": [],
                "prediction_candidates": [{
                    "content": "下一步很可能需要读取浏览器中的真实结果，当前结论仍是假设。",
                    "mode": "forecast",
                    "confidence": 0.8,
                    "uncertainty": 0.2,
                    "evidence_refs": [event_ref],
                    "completeness": "unknown",
                }],
                "appraisal_candidates": [],
                "thought_candidates": [{
                    "content": "先区分已经完成的本地链路与尚未观察的浏览器结果。",
                    "uncertainty": 0.15,
                    "evidence_refs": [event_ref],
                    "unresolved": ["浏览器结果尚未观察"],
                }],
                "candidate_preferences": {},
                "selected_candidate_ref": None,
                "lesson_candidates": [],
                "uncertainty": 0.2,
                "limitations": ["teacher_has_no_external_truth"],
            }, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


def _activity(activity_id: str, summary: str) -> ProjectActivity:
    return ProjectActivity(
        activity_id=activity_id,
        project_id="project-local",
        kind=f"unseen-{activity_id}",
        summary=summary,
        detail="仍需读取真实页面并保留未知边界。",
        source_ref=f"session://e2/{activity_id}",
        completeness="partial",
        observed_remaining=("浏览器现实回读",),
        observed_unknown=("用户是否看到预期效果尚未确认",),
        observed_next_action="读取浏览器现实结果",
    )


def _gateway(transport: PredictionThoughtTeacher) -> OpenAICompatibleGateway:
    return OpenAICompatibleGateway(
        "https://provider.invalid/v1",
        "fixture-secret",
        "gpt-fixture",
        transport=transport,
        max_retries=0,
    )


def _governance() -> GovernanceCompatibilityRecord:
    return GovernanceCompatibilityRecord(
        record_id="wave-e2-test",
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
        reason="bounded E2 fixture",
    )


def test_prediction_and_thought_courses_change_only_a_later_external_frame(tmp_path: Path) -> None:
    ledger = ProjectLearningLedger(tmp_path / "learning.sqlite")
    transport = PredictionThoughtTeacher()
    taught = run_project_episode(
        tmp_path / "teacher.sqlite",
        _activity("teacher-source", "恢复工作台仍等待现实页面回读"),
        learning_ledger=ledger,
        gateway=_gateway(transport),
        governance=_governance(),
        capability=_capability(),
    )
    assert transport.calls == 1
    assert {run.curriculum.target_capability for run in taught.curriculum_runs} == {
        "project.prediction", "project.thought"
    }
    assert all(run.curriculum.status == "active_trial" for run in taught.curriculum_runs)
    assert not any(item.curriculum_refs for item in taught.results[0].frame.c_prediction)
    assert not taught.results[0].frame.thoughts[0].curriculum_refs

    later = _activity("later-opportunity", "继续核对页面并读取未闭合的现实结果")
    influenced = run_project_episode(tmp_path / "later.sqlite", later, learning_ledger=ledger)
    first = influenced.results[0].frame
    assisted_prediction = next(item for item in first.c_prediction if item.curriculum_refs)
    assisted_thought = next(item for item in first.thoughts if item.curriculum_refs)
    assert assisted_prediction.observed_features == {}
    assert assisted_prediction.mismatch == 0.0
    assert assisted_prediction.confidence == assisted_prediction.curriculum_delta == 0.144
    assert assisted_prediction.base_confidence == 0.0
    assert assisted_prediction.hypothesis
    assert assisted_thought.base_proposition
    assert assisted_thought.curriculum_additions
    assert assisted_thought.proposition != assisted_thought.base_proposition
    assert first.proposition is not None
    assert first.expression_draft is not None
    assert assisted_thought.curriculum_additions[0] not in first.proposition.content
    assert assisted_thought.curriculum_additions[0] not in "".join(first.expression_draft.units)

    attempts = {item.target_capability: item for item in influenced.curriculum_attempts}
    assert set(attempts) == {"project.prediction", "project.thought"}
    assert attempts["project.prediction"].effect_key.startswith("prediction_hypothesis:curriculum_")
    assert attempts["project.prediction"].contribution == 0.144
    assert attempts["project.thought"].effect_key.startswith("thought_scaffold:curriculum_")
    assert 0.0 < attempts["project.thought"].contribution <= 0.18

    # The normal action creates a readback tick, but a single external
    # opportunity must produce only one curriculum observation per capability.
    assert len(influenced.results) >= 2
    assert not any(item.curriculum_refs for item in influenced.results[1].frame.c_prediction)
    assert not influenced.results[1].frame.thoughts[0].curriculum_refs
    assert influenced.results[1].frame.cognitive_trial_observations["project.prediction"] == ()
    assert influenced.results[1].frame.cognitive_trial_observations["project.thought"] == ()


def test_prediction_feedback_is_scoped_and_cold_replay_preserves_effect(tmp_path: Path) -> None:
    ledger = ProjectLearningLedger(tmp_path / "learning.sqlite")
    source = _activity("teacher-source", "恢复工作台仍等待现实页面回读")
    taught = run_project_episode(
        tmp_path / "teacher.sqlite", source, learning_ledger=ledger,
        gateway=_gateway(PredictionThoughtTeacher()),
        governance=_governance(), capability=_capability(),
    )
    later = _activity("later-opportunity", "继续核对页面并读取未闭合的现实结果")
    episode_path = tmp_path / "later.sqlite"
    influenced = run_project_episode(episode_path, later, learning_ledger=ledger)
    prediction_attempt = next(
        item for item in influenced.curriculum_attempts
        if item.target_capability == "project.prediction"
    )
    thought_course = next(
        run.curriculum for run in taught.curriculum_runs
        if run.curriculum.target_capability == "project.thought"
    )
    prediction_course = next(
        run.curriculum for run in taught.curriculum_runs
        if run.curriculum.target_capability == "project.prediction"
    )

    runtime_id = f"ap-vibe-runtime:{later.project_id}"
    organism_id = f"ap-vibe-organism:{later.project_id}"
    event = later.as_event(runtime_id=runtime_id, organism_id=organism_id, episode_id=influenced.episode_id)
    with EventStore(episode_path) as store:
        replay = MindRuntime(
            store,
            ProjectCognitionEnvironment(later.project_id),
            runtime_id=runtime_id,
            organism_id=organism_id,
            episode_id=influenced.episode_id,
        ).tick(event)
    assert replay.recovered is True
    assert replay.frame.cognitive_trial_observations == influenced.results[0].frame.cognitive_trial_observations
    assert any(item.curriculum_refs for item in replay.frame.c_prediction)
    assert replay.frame.thoughts[0].curriculum_additions

    correction = ProjectFeedback(
        feedback_id="prediction-only-correction",
        project_id="project-local",
        target_episode_id=influenced.episode_id,
        target_action=influenced.results[0].frame.decision.get("selected_kind") or "observe_only",
        signal="correction",
        magnitude=1.0,
        natural_language="这条预测不合适，只重教预测；想法支架继续保留。",
        source_ref="user://e2/prediction-correction",
        applicability={
            "target_capability": "project.prediction",
            "effect_key": prediction_attempt.effect_key,
        },
    )
    corrected = run_feedback_episode(
        tmp_path / "feedback.sqlite", correction, later, learning_ledger=ledger
    )
    assert corrected.curriculum_outcome is not None
    assert corrected.curriculum_outcome.outcome == "counterexample"
    assert ledger.get_curriculum(prediction_course.curriculum_id).status == "reteach"
    assert ledger.get_curriculum(thought_course.curriculum_id).status == "active_trial"

    after = run_project_episode(
        tmp_path / "after.sqlite",
        _activity("after-feedback", "再次读取页面并处理还没有闭合的现实结果"),
        learning_ledger=ProjectLearningLedger(tmp_path / "learning.sqlite"),
    )
    assert not any(item.curriculum_refs for item in after.results[0].frame.c_prediction)
    assert after.results[0].frame.thoughts[0].curriculum_refs

