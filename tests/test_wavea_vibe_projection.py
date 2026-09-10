from pathlib import Path

from ap_mind.vibe_mind import (
    FeedbackLesson,
    ProjectActivity,
    ProjectCognitionEnvironment,
    ProjectFeedback,
    ProjectLearningLedger,
    run_feedback_episode,
    run_project_episode,
)
from ap_mind.vibe_projection import project_activity_run, project_feedback_run


def test_activity_projection_is_runtime_derived_and_keeps_formal_knowledge_closed(
    tmp_path: Path,
) -> None:
    activity = ProjectActivity(
        activity_id="projection-activity",
        project_id="project-local",
        kind="progress",
        summary="AP-Vibe 投影进入前端合同",
        source_ref="session://projection/activity",
        observed_completed=("只读投影",),
        observed_remaining=("本地 API",),
        observed_next_action="接入幂等服务",
    )
    run = run_project_episode(tmp_path / "activity.sqlite", activity)
    view = project_activity_run("request-activity", run).to_dict()

    assert view["status"] == "success"
    assert view["ticks"]
    assert view["ticks"][0]["sa"]["text"] == activity.summary
    assert view["selected_action"]["kind"] == "stage_knowledge_candidate"
    assert view["knowledge_candidates"][0]["formal_knowledge"] is False
    assert view["teacher"]["mode"] == "provider_off"
    assert view["ownership"]["action_selection"] == "ap_native"
    assert view["ownership"]["formal_knowledge"] == "absent"


def test_feedback_projection_shows_readback_then_scoped_learning(tmp_path: Path) -> None:
    ledger = ProjectLearningLedger(tmp_path / "learning.sqlite")
    activity = ProjectActivity(
        activity_id="projection-target",
        project_id="project-local",
        kind="progress",
        summary="证据仍不完整",
        source_ref="session://projection/target",
        completeness="partial",
    )
    target = run_project_episode(
        tmp_path / "target.sqlite",
        activity,
        learning_ledger=ledger,
    )
    feedback = ProjectFeedback(
        feedback_id="projection-feedback",
        project_id="project-local",
        target_episode_id=target.episode_id,
        target_action="stage_knowledge_candidate",
        desired_action="ask_user",
        signal="correction",
        magnitude=1.0,
        natural_language="证据不完整时先问我。",
        source_ref="user://projection/feedback",
    )
    run = run_feedback_episode(
        tmp_path / "feedback.sqlite",
        feedback,
        activity,
        learning_ledger=ledger,
    )
    view = project_feedback_run("request-feedback", run).to_dict()

    assert view["status"] == "success"
    assert len(view["ticks"]) == 2
    assert view["lesson"]["status"] == "applied"
    assert view["causal_summary"][0]["detail"] == "证据不完整时先问我。"
    assert view["growth_effect"] == "lesson_applied_after_action_readback_and_next_tick"
    assert view["learning"]["lesson_count"] == 1
    assert view["ownership"]["learning_attribution"] == "native"


def test_activity_projection_reports_real_readback_status(tmp_path: Path) -> None:
    activity = ProjectActivity(
        activity_id="projection-readback",
        project_id="project-local",
        kind="progress",
        summary="记录一次有结果回读的活动",
        source_ref="session://projection/readback",
    )
    view = project_activity_run(
        "request-readback",
        run_project_episode(tmp_path / "readback.sqlite", activity),
    ).to_dict()

    readback = next(item for item in view["causal_summary"] if item["key"] == "readback")
    assert readback["detail"].startswith("success")
    assert readback["state"] == "complete"


def test_feedback_lesson_preserves_future_fields_and_environment_declares_action() -> None:
    lesson = FeedbackLesson.from_dict(
        {
            "lesson_id": "lesson-future",
            "project_id": "project-local",
            "feedback_ref": "feedback-future",
            "target_capability": "action_selection",
            "target_refs": ["episode-future"],
            "signal": "correction",
            "magnitude": 0.5,
            "natural_language": "未来字段也要保留。",
            "structured_delta": {},
            "applicability": {},
            "future_teacher_note": {"schema": 2},
        }
    )

    assert lesson.to_dict()["future_teacher_note"] == {"schema": 2}
    assert "record_feedback_lesson" in ProjectCognitionEnvironment(
        "project-local"
    ).describe()["actions"]
