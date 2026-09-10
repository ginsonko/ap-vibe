from pathlib import Path

from ap_mind import EventStore
from ap_mind.vibe_mind import (
    ProjectActivity,
    ProjectCognitionEnvironment,
    ProjectFeedback,
    ProjectLearningLedger,
    run_feedback_episode,
    run_project_episode,
)


def _selected_kind(run) -> str | None:
    first = run.results[0].frame
    selected = first.decision.get("selected_candidate_ref")
    return next(
        (item.get("kind") for item in first.actions if item.get("candidate_id") == selected),
        None,
    )


def test_complete_open_world_activity_stages_candidate_without_formal_write(tmp_path: Path) -> None:
    activity = ProjectActivity.from_dict(
        {
            "activity_id": "activity-complete",
            "project_id": "project-local",
            "kind": "future_tool_result_kind",
            "summary": "完成 AP-Vibe 第一条项目认知纵切",
            "source_ref": "session://current/turn/1",
            "observed_completed": ["项目活动合同", "待审知识合同"],
            "observed_remaining": ["工作台投影"],
            "observed_next_action": "接入本地只读 API",
            "future_extension": {"new_field": True},
        }
    )

    assert activity.extra["future_extension"] == {"new_field": True}
    run = run_project_episode(tmp_path / "episode.sqlite", activity)

    assert _selected_kind(run) == "stage_knowledge_candidate"
    assert len(run.staged_proposals) == 1
    proposal = run.staged_proposals[0].to_dict()
    assert proposal["summary"] == activity.summary
    assert proposal["status"] == "staged"
    assert proposal["formal_knowledge"] is False
    assert proposal["teacher_basis"] == {}
    assert run.results[0].result.payload_inline["formal_knowledge_changed"] is False
    assert run.provider_mode == "provider_off"


def test_unknown_conflicted_activity_asks_user_via_same_action_arena(tmp_path: Path) -> None:
    activity = ProjectActivity(
        activity_id="activity-unknown",
        project_id="project-local",
        kind="another_future_kind",
        summary="界面声称完成，但没有运行回读",
        source_ref="session://current/turn/2",
        completeness="unknown",
        observed_unknown=("是否真的运行", "是否已冷保存"),
        extra={"conflicts": ["完成声明与缺少 readback 冲突"]},
    )

    run = run_project_episode(tmp_path / "episode.sqlite", activity)

    assert _selected_kind(run) == "ask_user"
    assert not run.staged_proposals
    assert len(run.pending_questions) == 1
    assert "是否真的运行" in run.pending_questions[0]["questions"]
    kinds = {item["kind"] for item in run.results[0].frame.actions}
    # E3 adds AP-native attention transitions to the same ActionArena.  The
    # unresolved evidence contract still selects ``ask_user``; the additional
    # focus candidates are valid observations of the shared arena and must not
    # be mistaken for a second decision path.
    assert kinds == {
        "stage_knowledge_candidate",
        "ask_user",
        "defer",
        "observe_only",
        "maintain_attention",
        "diversify_attention",
    }


def test_cold_replay_rebuilds_effect_from_result_without_second_dispatch(tmp_path: Path) -> None:
    database = tmp_path / "episode.sqlite"
    activity = ProjectActivity(
        activity_id="activity-replay",
        project_id="project-local",
        kind="progress",
        summary="保存当前阶段进度",
        source_ref="session://current/turn/3",
        observed_completed=("A1",),
        observed_remaining=("A2",),
    )
    first = run_project_episode(database, activity)
    with EventStore(database) as store:
        before = {name: store.count(name) for name in ("events", "frames", "dispatches", "results")}

    replay = run_project_episode(database, activity)
    with EventStore(database) as store:
        after = {name: store.count(name) for name in ("events", "frames", "dispatches", "results")}

    assert after == before
    assert replay.staged_proposals == first.staged_proposals
    assert replay.results[0].recovered is True


def test_project_identity_mismatch_never_stages_candidate(tmp_path: Path) -> None:
    activity = ProjectActivity(
        activity_id="activity-wrong-project",
        project_id="project-other",
        kind="progress",
        summary="不属于当前项目",
        source_ref="session://other/turn/1",
    )
    event = activity.as_event(
        runtime_id="runtime",
        organism_id="organism",
        episode_id="episode",
    )
    environment = ProjectCognitionEnvironment("project-local")
    candidates = environment.candidates(
        event,
        {"sa": {"novelty": 1.0}, "uncertainty": 0.0, "pressure": 0.0},
    )

    assert [item.kind for item in candidates] == ["observe_only"]
    assert environment.staged_proposals == []


def test_feedback_readback_changes_later_unseen_activity_without_fixed_winner(tmp_path: Path) -> None:
    ledger = ProjectLearningLedger(tmp_path / "learning.sqlite")
    first_activity = ProjectActivity(
        activity_id="partial-before",
        project_id="project-local",
        kind="unseen_kind_before",
        summary="第一份只有部分证据的进度",
        source_ref="session://before",
        completeness="partial",
    )
    first = run_project_episode(
        tmp_path / "before.sqlite",
        first_activity,
        learning_ledger=ledger,
    )
    assert _selected_kind(first) == "stage_knowledge_candidate"

    feedback = ProjectFeedback(
        feedback_id="feedback-ask-first",
        project_id="project-local",
        target_episode_id=first.episode_id,
        target_action="stage_knowledge_candidate",
        desired_action="ask_user",
        signal="correction",
        magnitude=1.0,
        natural_language="这类证据不完整时，应该先问我，不要急着暂存。",
        source_ref="user://feedback/ask-first",
    )
    teaching = run_feedback_episode(
        tmp_path / "feedback.sqlite",
        feedback,
        first_activity,
        learning_ledger=ledger,
    )
    assert len(teaching.results) == 2
    assert teaching.lesson is not None
    assert teaching.lesson.status == "applied"

    unseen_activity = ProjectActivity(
        activity_id="partial-after",
        project_id="project-local",
        kind="different_unseen_kind",
        summary="文字与类别都不同的另一份部分进度",
        source_ref="session://after",
        completeness="partial",
    )
    after = run_project_episode(
        tmp_path / "after.sqlite",
        unseen_activity,
        learning_ledger=ledger,
    )
    assert _selected_kind(after) == "ask_user"
    assert after.learned_preferences == {
        "ask_user": 0.18,
        "stage_knowledge_candidate": -0.18,
    }

    # The same lesson must not install a global script.  A complete activity
    # has a different evidence profile and still follows its own competition.
    complete_activity = ProjectActivity(
        activity_id="complete-after",
        project_id="project-local",
        kind="third_unseen_kind",
        summary="有完整来源的独立活动",
        source_ref="session://complete-after",
        completeness="complete",
    )
    complete = run_project_episode(
        tmp_path / "complete.sqlite",
        complete_activity,
        learning_ledger=ledger,
    )
    assert _selected_kind(complete) == "stage_knowledge_candidate"
    assert complete.learned_preferences == {}


def test_feedback_retry_is_idempotent_across_cold_restart(tmp_path: Path) -> None:
    ledger = ProjectLearningLedger(tmp_path / "learning.sqlite")
    activity = ProjectActivity(
        activity_id="target",
        project_id="project-local",
        kind="progress",
        summary="部分进度",
        source_ref="session://target",
        completeness="partial",
    )
    target = run_project_episode(tmp_path / "target.sqlite", activity, learning_ledger=ledger)
    feedback = ProjectFeedback(
        feedback_id="feedback-idempotent",
        project_id="project-local",
        target_episode_id=target.episode_id,
        target_action="stage_knowledge_candidate",
        desired_action="ask_user",
        signal="correction",
        magnitude=1.0,
        natural_language="先问再暂存",
        source_ref="user://feedback/idempotent",
    )
    first = run_feedback_episode(
        tmp_path / "feedback.sqlite",
        feedback,
        activity,
        learning_ledger=ledger,
    )
    replay = run_feedback_episode(
        tmp_path / "feedback.sqlite",
        feedback,
        activity,
        learning_ledger=ProjectLearningLedger(tmp_path / "learning.sqlite"),
    )

    assert replay.lesson == first.lesson
    assert replay.learning_snapshot["lesson_count"] == 1
    assert all(item["sample_count"] == 1 for item in replay.learning_snapshot["preferences"])
