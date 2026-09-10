from __future__ import annotations

import json
from pathlib import Path

from ap_mind.contracts import CapabilityOwnership
from ap_mind.gateway import OpenAICompatibleGateway
from ap_mind.governance import GovernanceCompatibilityRecord
from ap_mind.studio_server import HYBRID_WHITEPAPER_SHA256
from ap_mind.vibe_mind import (
    ProjectActivity,
    ProjectFeedback,
    ProjectLearningLedger,
    run_feedback_episode,
    run_project_episode,
)


class CurriculumTeacherTransport:
    def __init__(self, *, capability: str = "project.action_selection", adjustment=None) -> None:
        self.calls = 0
        self.capability = capability
        self.adjustment = adjustment if adjustment is not None else {"prefer": "ask_user"}

    def request_json(self, method: str, url: str, **kwargs):
        del method, url
        self.calls += 1
        context = json.loads(kwargs["body"]["messages"][1]["content"])["frame_view"]
        event_ref = context["sa"]["event_ref"]
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "status": "proposal",
                                "lesson_candidates": [
                                    {
                                        "capability": self.capability,
                                        "trigger_features": {"source_shape": "partial_project_evidence"},
                                        "suggested_adjustment": self.adjustment,
                                        "counterexamples": ["已有完整来源和 readback 时不需要追问"],
                                        "confidence": 0.9,
                                    }
                                ],
                                "uncertainty": 0.1,
                                "limitations": ["teacher_curriculum_requires_later_outcome"],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 20, "total_tokens": 40},
            "fixture_event_ref": event_ref,
        }


def _governance() -> GovernanceCompatibilityRecord:
    return GovernanceCompatibilityRecord(
        record_id="wave-c1-test",
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
        reason="bounded curriculum fixture",
    )


def _selected_kind(run) -> str | None:
    first = run.results[0].frame
    selected = first.decision.get("selected_candidate_ref")
    return next(
        (item.get("kind") for item in first.actions if item.get("candidate_id") == selected),
        None,
    )


def _partial(activity_id: str, summary: str, *, project_id: str = "project-local") -> ProjectActivity:
    return ProjectActivity(
        activity_id=activity_id,
        project_id=project_id,
        kind=f"unseen-kind-{activity_id}",
        summary=summary,
        source_ref=f"session://c1/{activity_id}",
        completeness="partial",
    )


def _teach_once(tmp_path: Path, ledger: ProjectLearningLedger):
    transport = CurriculumTeacherTransport()
    gateway = OpenAICompatibleGateway(
        "https://provider.invalid/v1",
        "fixture-secret",
        "gpt-fixture",
        transport=transport,
        max_retries=0,
    )
    activity = _partial("teacher-source", "这是一条证据尚不完整的首次工程活动")
    run = run_project_episode(
        tmp_path / "teacher.sqlite",
        activity,
        learning_ledger=ledger,
        gateway=gateway,
        governance=_governance(),
        capability=_capability(),
    )
    return transport, activity, run


def test_teacher_curriculum_uses_an_independent_ap_episode_before_active_trial(tmp_path: Path) -> None:
    ledger = ProjectLearningLedger(tmp_path / "learning.sqlite")
    transport, activity, run = _teach_once(tmp_path, ledger)

    assert transport.calls == 1
    assert run.curriculum_preferences == {}  # same-episode teaching is never retroactive
    assert len(run.curriculum_runs) == 1
    curriculum_run = run.curriculum_runs[0]
    assert len(curriculum_run.results) == 2
    assert _selected_kind(curriculum_run) == "adopt_curriculum"
    assert curriculum_run.results[0].frame.gateway["limitations"] == [
        "gateway_not_consulted_for_control_event"
    ]
    assert curriculum_run.outcome is not None
    assert curriculum_run.outcome.outcome == "active_trial"
    assert curriculum_run.curriculum.status == "active_trial"
    snapshot = ledger.snapshot(activity.project_id)
    assert snapshot["active_trial_count"] == 1
    assert snapshot["curricula"][0]["trial_adjustments"] == {"ask_user": 0.18}


def test_trial_transfers_to_unseen_same_profile_but_not_other_profile(tmp_path: Path) -> None:
    ledger = ProjectLearningLedger(tmp_path / "learning.sqlite")
    _teach_once(tmp_path, ledger)

    unseen = _partial("unseen-opportunity", "措辞、类别和标识都不同的另一条不完整观察")
    after = run_project_episode(tmp_path / "after.sqlite", unseen, learning_ledger=ledger)
    assert _selected_kind(after) == "ask_user"
    assert after.curriculum_preferences == {"ask_user": 0.18}
    assert after.curriculum_attempt is not None
    assert after.curriculum_attempt.status == "attempted"
    ask = next(item for item in after.results[0].frame.actions if item["kind"] == "ask_user")
    assert ask["components"]["curriculum_trial"] == 0.18

    complete = ProjectActivity(
        activity_id="different-profile",
        project_id="project-local",
        kind="unseen-complete-kind",
        summary="这条独立观察具有完整来源",
        source_ref="session://c1/different-profile",
        completeness="complete",
    )
    isolated = run_project_episode(tmp_path / "isolated.sqlite", complete, learning_ledger=ledger)
    assert isolated.curriculum_preferences == {}
    assert isolated.curriculum_attempt is None
    assert _selected_kind(isolated) == "stage_knowledge_candidate"


def test_counterexample_retracts_only_related_trial_and_cold_replay_is_idempotent(tmp_path: Path) -> None:
    ledger_path = tmp_path / "learning.sqlite"
    ledger = ProjectLearningLedger(ledger_path)
    transport, _, taught = _teach_once(tmp_path, ledger)
    opportunity = _partial("counterexample-target", "另一条部分证据活动先受课程影响")
    influenced = run_project_episode(tmp_path / "influenced.sqlite", opportunity, learning_ledger=ledger)
    assert _selected_kind(influenced) == "ask_user"
    curriculum_id = taught.curriculum_runs[0].curriculum.curriculum_id

    feedback = ProjectFeedback(
        feedback_id="counterexample-feedback",
        project_id="project-local",
        target_episode_id=influenced.episode_id,
        target_action="ask_user",
        desired_action="stage_knowledge_candidate",
        signal="correction",
        magnitude=1.0,
        natural_language="这一次已有足够依据，应该先形成待审候选，不要追问。",
        source_ref="user://c1/counterexample",
    )
    first = run_feedback_episode(
        tmp_path / "feedback.sqlite",
        feedback,
        opportunity,
        learning_ledger=ledger,
    )
    assert first.curriculum_outcome is not None
    assert first.curriculum_outcome.outcome == "counterexample"
    assert ledger.get_curriculum(curriculum_id).status == "reteach"

    replay = run_feedback_episode(
        tmp_path / "feedback.sqlite",
        feedback,
        opportunity,
        learning_ledger=ProjectLearningLedger(ledger_path),
    )
    snapshot = replay.learning_snapshot
    assert replay.curriculum_outcome == first.curriculum_outcome
    assert snapshot["curricula"][0]["outcome_counts"]["counterexample"] == 1
    assert transport.calls == 1

    later = run_project_episode(
        tmp_path / "after-counterexample.sqlite",
        _partial("post-reteach", "反例后的新活动不再获得教师 trial"),
        learning_ledger=ProjectLearningLedger(ledger_path),
    )
    assert later.curriculum_preferences == {}
    assert later.curriculum_attempt is None
    # The user's own feedback lesson remains present and independently favours
    # staging; retracting the teacher trial did not clear that local learning.
    assert later.learned_preferences == {
        "ask_user": -0.18,
        "stage_knowledge_candidate": 0.18,
    }
    assert _selected_kind(later) == "stage_knowledge_candidate"


def test_unsupported_or_invalid_teacher_curriculum_never_gets_adopt_affordance(tmp_path: Path) -> None:
    for index, (capability, adjustment) in enumerate(
        (
            ("project.recall", {"prefer": "ask_user"}),
            ("project.action_selection", {"prefer": "invented_action"}),
            ("project.action_selection", {"description": "可以考虑追问"}),
            ("project.action_selection", {"action_adjustments": {"ask_user": 4.0}}),
        )
    ):
        case = tmp_path / f"case-{index}"
        ledger = ProjectLearningLedger(case / "learning.sqlite")
        transport = CurriculumTeacherTransport(capability=capability, adjustment=adjustment)
        gateway = OpenAICompatibleGateway(
            "https://provider.invalid/v1",
            "fixture-secret",
            "gpt-fixture",
            transport=transport,
            max_retries=0,
        )
        run = run_project_episode(
            case / "episode.sqlite",
            _partial(f"invalid-{index}", f"无效课程样本 {index}"),
            learning_ledger=ledger,
            gateway=gateway,
            governance=_governance(),
            capability=_capability(),
        )
        curriculum_run = run.curriculum_runs[0]
        kinds = {item["kind"] for item in curriculum_run.results[0].frame.actions}
        assert "adopt_curriculum" not in kinds
        assert curriculum_run.curriculum.status in {"deferred", "rejected"}
        assert ledger.snapshot("project-local")["active_trial_count"] == 0
        assert transport.calls == 1
