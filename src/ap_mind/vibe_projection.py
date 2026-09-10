"""Read-only AP-Vibe projections for the user workbench.

The projection layer translates persisted runtime facts into stable product
language.  It never reads SQLite directly, computes a winner, applies a
lesson, or promotes a knowledge proposal.  Missing information remains an
explicit limitation or unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .project_knowledge import KnowledgeReviewRun
from .studio_projection import project_tick
from .vibe_mind import CurriculumEpisodeRun, FeedbackEpisodeRun, ProjectEpisodeRun


AP_VIBE_PROJECTION_VERSION = "0.9.0"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _selected(tick: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return next(
        (
            item
            for item in tick.get("actions", ())
            if isinstance(item, Mapping) and item.get("selected") is True
        ),
        None,
    )


def _provider_view(tick: Mapping[str, Any] | None, provider_mode: str) -> dict[str, Any]:
    gateway = _mapping(_mapping(tick).get("research")).get("gateway")
    raw = _mapping(gateway)
    adoption = dict(_mapping(raw.get("adoption")))
    families = _mapping(adoption.get("families"))
    candidate_fields = {
        "recall": "recall_candidates",
        "prediction": "prediction_candidates",
        "appraisal": "appraisal_candidates",
        "thought": "thought_candidates",
        "paradigm": "paradigm_candidates",
        "attention": "attention_candidates",
        "expression": "expression_candidates",
        "parameter": "parameter_candidates",
        "lesson": "lesson_candidates",
    }
    candidates = {
        kind: list(raw.get(field, ()))[:16]
        if isinstance(raw.get(field), Sequence) and not isinstance(raw.get(field), (str, bytes))
        else []
        for kind, field in candidate_fields.items()
    }
    action_preferences = (
        [
            {"candidate_ref": str(key), "delta": float(value)}
            for key, value in raw.get("candidate_preferences", {}).items()
            if isinstance(key, str)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ]
        if isinstance(raw.get("candidate_preferences"), Mapping)
        else []
    )
    candidates["action"] = action_preferences
    call_receipt = _mapping(raw.get("call_receipt"))
    return {
        "mode": provider_mode,
        "status": raw.get("status", "unavailable"),
        "source": raw.get("source"),
        "owner": raw.get("owner"),
        "proposition": raw.get("proposition"),
        "feeling_hints": list(raw.get("feeling_hints", ()))[:16]
        if isinstance(raw.get("feeling_hints"), Sequence)
        and not isinstance(raw.get("feeling_hints"), (str, bytes))
        else [],
        "reusable_features": list(raw.get("reusable_features", ()))[:32]
        if isinstance(raw.get("reusable_features"), Sequence)
        and not isinstance(raw.get("reusable_features"), (str, bytes))
        else [],
        "uncertainty": raw.get("uncertainty"),
        "limitations": list(raw.get("limitations", ()))[:32]
        if isinstance(raw.get("limitations"), Sequence)
        and not isinstance(raw.get("limitations"), (str, bytes))
        else [],
        "model": raw.get("model"),
        "provider": raw.get("provider"),
        "advisor_role": raw.get("advisor_role"),
        "requested_capabilities": list(raw.get("requested_capabilities", ()))[:32]
        if isinstance(raw.get("requested_capabilities"), Sequence)
        and not isinstance(raw.get("requested_capabilities"), (str, bytes))
        else [],
        "sampling": dict(_mapping(raw.get("sampling"))),
        "candidates": candidates,
        "candidate_counts": {kind: len(items) for kind, items in candidates.items()},
        "adoption": {
            "families": {str(key): dict(value) for key, value in families.items() if isinstance(value, Mapping)},
            "summary": dict(_mapping(adoption.get("summary"))),
        },
        "validation_issues": list(raw.get("validation_issues", ()))[:64]
        if isinstance(raw.get("validation_issues"), Sequence)
        and not isinstance(raw.get("validation_issues"), (str, bytes))
        else [],
        "call_receipt": {
            "call_id": call_receipt.get("call_id"),
            "request_key": call_receipt.get("request_key"),
            "status": call_receipt.get("status"),
            "attempt": call_receipt.get("attempt"),
            "latency_ms": call_receipt.get("latency_ms"),
            "usage": dict(_mapping(call_receipt.get("usage"))),
            "input_snapshot_hash": call_receipt.get("input_snapshot_hash"),
            "prompt_template_version": call_receipt.get("prompt_template_version"),
            "output_hash": call_receipt.get("output_hash"),
            "created_at": call_receipt.get("created_at"),
        },
    }


def _counters(ticks: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    if not ticks:
        return {}
    raw = _mapping(ticks[-1].get("counters"))
    return {
        str(key): int(value)
        for key, value in raw.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def _readback_detail(tick: Mapping[str, Any]) -> tuple[str, str]:
    """Return the actual dispatch/readback result without inventing absence."""

    decision = _mapping(tick.get("decision"))
    status = decision.get("result_status")
    completeness = decision.get("result_completeness")
    if isinstance(status, str) and status:
        suffix = f" · {completeness}" if isinstance(completeness, str) and completeness else ""
        return f"{status}{suffix}", "complete" if status == "success" else "unknown"
    return "没有可确认的执行回读", "unknown"


def _activity_causal_summary(
    ticks: Sequence[Mapping[str, Any]],
    *,
    effect: str,
) -> list[dict[str, Any]]:
    if not ticks:
        return []
    first = ticks[0]
    selected = _selected(first)
    feelings = [
        str(item.get("name"))
        for item in first.get("feelings", ())
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    ]
    recall = first.get("recall") if isinstance(first.get("recall"), Sequence) else ()
    predictions = first.get("prediction") if isinstance(first.get("prediction"), Sequence) else ()
    last = ticks[-1]
    readback_detail, readback_state = _readback_detail(first)
    first_memory = next((item for item in recall if isinstance(item, Mapping)), None)
    memory_detail = (
        f"记起：{str(first_memory.get('summary') or '来源化历史经历')[:180]}"
        if first_memory is not None
        else "暂未召回直接相关经历"
    )
    if first_memory is not None and isinstance(first_memory.get("curriculum_gain"), (int, float)):
        memory_detail += (
            f" · AP {float(first_memory.get('base_score', first_memory.get('score', 0.0))):.3f}"
            f" · 课程 {float(first_memory.get('curriculum_gain', 0.0)):+.3f}"
            f" · 最终 {float(first_memory.get('score', 0.0)):.3f}"
        )
    return [
        {
            "key": "activity",
            "label": "真实工程活动",
            "detail": _mapping(first.get("sa")).get("text"),
            "state": "complete",
        },
        {
            "key": "memory",
            "label": "召回与预测",
            "detail": f"{memory_detail} · 共召回 {len(recall)} 项 / 预测 {len(predictions)} 项",
            "state": "complete" if recall or predictions else "unknown",
        },
        {
            "key": "appraisal",
            "label": "认知感受与注意",
            "detail": "、".join(feelings) or "当前没有显著感受",
            "state": "attention" if feelings else "unknown",
        },
        {
            "key": "arena",
            "label": "同一行动竞技场",
            "detail": f"{len(first.get('actions', ()))} 个候选 · 胜出 {selected.get('kind') if selected else '未决'}",
            "state": "complete" if selected else "unknown",
        },
        {
            "key": "readback",
            "label": "执行与现实回读",
            "detail": readback_detail,
            "state": readback_state,
        },
        {
            "key": "effect",
            "label": "本轮产品结果",
            "detail": effect,
            "state": "complete",
        },
    ]


def _feedback_causal_summary(
    ticks: Sequence[Mapping[str, Any]],
    *,
    lesson_status: str | None,
    feedback_text: str,
) -> list[dict[str, Any]]:
    if not ticks:
        return []
    first = ticks[0]
    selected = _selected(first)
    return [
        {
            "key": "feedback",
            "label": "用户纠正",
            "detail": feedback_text,
            "state": "complete",
        },
        {
            "key": "cognition",
            "label": "反馈进入 AP 主流程",
            "detail": "经过 SA、B/C、感受、注意和想法",
            "state": "complete",
        },
        {
            "key": "arena",
            "label": "是否记录课程的竞争",
            "detail": selected.get("kind") if selected else "未形成可执行候选",
            "state": "complete" if selected else "unknown",
        },
        {
            "key": "readback",
            "label": "课程记录回读",
            "detail": _mapping(first.get("decision")).get("result_status") or "没有结果回读",
            "state": "complete" if _mapping(first.get("decision")).get("result_status") == "success" else "unknown",
        },
        {
            "key": "learning",
            "label": "局部学习",
            "detail": lesson_status or "尚未应用",
            "state": "complete" if lesson_status == "applied" else "unknown",
        },
    ]


@dataclass(frozen=True)
class ProjectEpisodeView:
    request_id: str
    episode_kind: str
    episode_id: str
    project_id: str
    status: str
    input: Mapping[str, Any]
    ticks: tuple[Mapping[str, Any], ...]
    counters: Mapping[str, int]
    selected_action: Mapping[str, Any] | None
    knowledge_candidates: tuple[Mapping[str, Any], ...] = ()
    pending_questions: tuple[Mapping[str, Any], ...] = ()
    teacher: Mapping[str, Any] = field(default_factory=dict)
    learning: Mapping[str, Any] = field(default_factory=dict)
    lesson: Mapping[str, Any] | None = None
    curriculum_episodes: tuple[Mapping[str, Any], ...] = ()
    curriculum_attempt: Mapping[str, Any] | None = None
    curriculum_attempts: tuple[Mapping[str, Any], ...] = ()
    curriculum_outcome: Mapping[str, Any] | None = None
    knowledge_revision: Mapping[str, Any] | None = None
    causal_summary: tuple[Mapping[str, Any], ...] = ()
    product_effect: str = "local_project_cognition_episode"
    growth_effect: str = "none_observed"
    ownership: Mapping[str, str] = field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    projection_version: str = AP_VIBE_PROJECTION_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_version": self.projection_version,
            "request_id": self.request_id,
            "episode_kind": self.episode_kind,
            "episode_id": self.episode_id,
            "project_id": self.project_id,
            "status": self.status,
            "input": dict(self.input),
            "ticks": [dict(item) for item in self.ticks],
            "counters": dict(self.counters),
            "selected_action": dict(self.selected_action) if self.selected_action else None,
            "knowledge_candidates": [dict(item) for item in self.knowledge_candidates],
            "pending_questions": [dict(item) for item in self.pending_questions],
            "teacher": dict(self.teacher),
            "learning": dict(self.learning),
            "lesson": dict(self.lesson) if self.lesson else None,
            "curriculum_episodes": [dict(item) for item in self.curriculum_episodes],
            "curriculum_attempt": dict(self.curriculum_attempt) if self.curriculum_attempt else None,
            "curriculum_attempts": [dict(item) for item in self.curriculum_attempts],
            "curriculum_outcome": dict(self.curriculum_outcome) if self.curriculum_outcome else None,
            "knowledge_revision": dict(self.knowledge_revision) if self.knowledge_revision else None,
            "causal_summary": [dict(item) for item in self.causal_summary],
            "product_effect": self.product_effect,
            "growth_effect": self.growth_effect,
            "ownership": dict(self.ownership),
            "limitations": list(self.limitations),
            "unknowns": list(self.unknowns),
        }


def _curriculum_episode_projection(run: CurriculumEpisodeRun) -> dict[str, Any]:
    ticks = tuple(project_tick(item) for item in run.results)
    selected = _selected(ticks[0]) if ticks else None
    return {
        "episode_id": run.episode_id,
        "curriculum": run.curriculum.to_dict(),
        "ticks": [dict(item) for item in ticks],
        "counters": _counters(ticks),
        "selected_action": dict(selected) if selected else None,
        "outcome": run.outcome.to_dict() if run.outcome is not None else None,
        "provider_mode": run.provider_mode,
        "gateway_consulted": False,
        "product_effect": "curriculum_processed_through_independent_ap_episode",
        "growth_effect": (
            "reversible_active_trial_recorded_after_result_back"
            if run.outcome is not None and run.outcome.resulting_status == "active_trial"
            else "curriculum_not_activated"
        ),
        "ownership": {
            "curriculum_source": "assisted" if run.curriculum.source_kind == "llm_teacher" else "user",
            "curriculum_decision": "native",
            "dispatch_and_readback": "native",
            "trial_registration": "native",
            "maturity": "absent",
        },
    }


def project_activity_run(request_id: str, run: ProjectEpisodeRun) -> ProjectEpisodeView:
    """Project one activity run without altering its runtime or learning state."""

    ticks = tuple(project_tick(item) for item in run.results)
    selected = _selected(ticks[0]) if ticks else None
    candidates = tuple(item.to_dict() for item in run.staged_proposals)
    questions = tuple(dict(item) for item in run.pending_questions)
    if candidates:
        effect = "已形成待审知识候选，正式项目知识没有被改写"
    elif questions:
        effect = "已形成需要用户回答的最小问题，项目知识保持不变"
    else:
        kind = selected.get("kind") if selected else None
        effect = "保留观察，等待更多证据" if kind in {"defer", "observe_only"} else "没有形成外部项目改变"
    attempted_capabilities = {item.target_capability for item in run.curriculum_attempts}
    cognitive_attempts = {
        "project.recall",
        "project.appraisal",
        "project.prediction",
        "project.thought",
        "project.paradigm",
        "project.attention",
        "project.expression",
        "project.parameter_tuning",
    }.intersection(attempted_capabilities)
    if cognitive_attempts:
        growth = "active_curriculum_trials_influenced_later_cognition"
    elif run.curriculum_attempt is not None:
        growth = "active_curriculum_trial_influenced_later_action_competition"
    elif run.curriculum_runs:
        growth = "teacher_curriculum_processed_as_reversible_trial"
    elif run.learned_preferences:
        growth = "learned_preferences_influenced_action_competition"
    else:
        growth = "none_observed"
    status = "success" if selected is not None else "partial"
    return ProjectEpisodeView(
        request_id=request_id,
        episode_kind="project_activity",
        episode_id=run.episode_id,
        project_id=run.activity.project_id,
        status=status,
        input={"activity": run.activity.to_dict()},
        ticks=ticks,
        counters=_counters(ticks),
        selected_action=dict(selected) if selected else None,
        knowledge_candidates=candidates,
        pending_questions=questions,
        teacher=_provider_view(ticks[0] if ticks else None, run.provider_mode),
        learning={
            **dict(run.learning_snapshot),
            "applied_preferences": dict(run.learned_preferences),
            "applied_curriculum_preferences": dict(run.curriculum_preferences),
            "applicable_curriculum_trials": [dict(item) for item in run.curriculum_trials],
            "applicable_cognitive_trials": {
                str(key): [dict(item) for item in value]
                for key, value in run.cognitive_curriculum_trials.items()
            },
        },
        curriculum_episodes=tuple(_curriculum_episode_projection(item) for item in run.curriculum_runs),
        curriculum_attempt=run.curriculum_attempt.to_dict() if run.curriculum_attempt is not None else None,
        curriculum_attempts=tuple(item.to_dict() for item in run.curriculum_attempts),
        causal_summary=tuple(_activity_causal_summary(ticks, effect=effect)),
        product_effect="local_project_cognition_episode",
        growth_effect=growth,
        ownership={
            "event_and_evidence": "environment",
            "codec_state": "native",
            "recall": "assisted" if "project.recall" in attempted_capabilities else "native_shallow",
            "prediction": "assisted" if "project.prediction" in attempted_capabilities else "native_shallow",
            "appraisal": "assisted" if "project.appraisal" in attempted_capabilities else "native_shallow",
            "thought": "assisted" if "project.thought" in attempted_capabilities else "native_shallow",
            "paradigm": "assisted" if "project.paradigm" in attempted_capabilities else "absent",
            "attention": "assisted" if "project.attention" in attempted_capabilities else "native_shallow",
            "expression": "assisted" if "project.expression" in attempted_capabilities else "native_shallow",
            "parameter_tuning": "assisted" if "project.parameter_tuning" in attempted_capabilities else "native_shallow",
            "teacher": "absent" if run.provider_mode in {"provider_off", "locally_withheld_by_sampling"} else "assisted",
            "knowledge_candidate": "native",
            "action_selection": str(_mapping(_mapping(ticks[0]).get("decision")).get("owner") or "unknown") if ticks else "unknown",
            "dispatch_and_readback": "native",
            "formal_knowledge": "absent",
            "growth_attribution": "native",
        },
        limitations=(
            "provider_off",
            "local_environment_only",
            "semantic_generalisation_for_prediction_and_thought_unmeasured",
            "formal_project_knowledge_write_disabled",
            "live_codex_sampling_not_connected",
        ) if run.provider_mode == "provider_off" else (
            "local_environment_only",
            "semantic_generalisation_for_prediction_and_thought_unmeasured",
            "formal_project_knowledge_write_disabled",
            "live_codex_sampling_not_connected",
        ),
        unknowns=(
            "real_llm_teacher_quality_unmeasured",
            "live_vibe_round_trip_unavailable",
            "long_term_growth_unmeasured",
        ),
    )


def project_feedback_run(request_id: str, run: FeedbackEpisodeRun) -> ProjectEpisodeView:
    """Project one feedback/learning run without applying any new lesson."""

    ticks = tuple(project_tick(item) for item in run.results)
    selected = _selected(ticks[0]) if ticks else None
    lesson = run.lesson.to_dict() if run.lesson is not None else None
    applied = bool(lesson and lesson.get("status") == "applied")
    return ProjectEpisodeView(
        request_id=request_id,
        episode_kind="project_feedback",
        episode_id=run.episode_id,
        project_id=run.feedback.project_id,
        status="success" if applied else "partial",
        input={"feedback": run.feedback.to_dict()},
        ticks=ticks,
        counters=_counters(ticks),
        selected_action=dict(selected) if selected else None,
        teacher=_provider_view(ticks[0] if ticks else None, run.provider_mode),
        learning=dict(run.learning_snapshot),
        lesson=lesson,
        curriculum_outcome=(
            run.curriculum_outcome.to_dict()
            if run.curriculum_outcome is not None
            else None
        ),
        causal_summary=tuple(
            _feedback_causal_summary(
                ticks,
                lesson_status=str(lesson.get("status")) if lesson else None,
                feedback_text=run.feedback.natural_language,
            )
        ),
        product_effect="user_feedback_processed_through_ap_flow",
        growth_effect=(
            "capability_curriculum_counterexample_or_success_attributed_after_readback"
            if run.curriculum_outcome is not None
            else "lesson_applied_after_action_readback_and_next_tick"
            if applied
            else "lesson_not_applied"
        ),
        ownership={
            "feedback_evidence": "user",
            "codec_state_recall_prediction": "native",
            "feelings_attention_thought": "native_shallow",
            "teacher": "absent" if run.provider_mode == "provider_off" else "assisted",
            "lesson_recording_action": "native",
            "learning_attribution": "native",
            "formal_project_knowledge": "absent",
        },
        limitations=(
            "natural_language_feedback_interpreter_absent",
            "user_selected_structured_target_and_desired_action",
            "single_lesson_effect_only",
            "provider_off" if run.provider_mode == "provider_off" else "provider_assisted",
        ),
        unknowns=(
            "long_term_preference_stability_unmeasured",
            "teacher_generated_lesson_quality_unmeasured",
            "distribution_shift_detection_absent",
        ),
    )


def project_knowledge_review_run(
    request_id: str,
    run: KnowledgeReviewRun,
) -> ProjectEpisodeView:
    """Project one local recovery review without granting Vibe authority."""

    ticks = tuple(project_tick(item) for item in run.results)
    selected = _selected(ticks[0]) if ticks else None
    revision = run.revision.to_dict() if run.revision is not None else None
    selected_kind = selected.get("kind") if selected else None
    effect = (
        "已追加本地恢复基线；外部 Vibe 正式知识仍未写入"
        if revision is not None
        else "审阅事件已保留，本地恢复基线没有改变"
    )
    readback_detail, readback_state = _readback_detail(ticks[0]) if ticks else ("没有结果回读", "unknown")
    causal = (
        {
            "key": "review",
            "label": "用户审阅",
            "detail": run.review.rationale,
            "state": "complete",
        },
        {
            "key": "cognition",
            "label": "审阅进入 AP 主流程",
            "detail": "经过 SA、StatePool、CurrentField、B/C、感受、注意和想法",
            "state": "complete" if ticks else "unknown",
        },
        {
            "key": "arena",
            "label": "本地恢复行动竞争",
            "detail": f"{len(ticks[0].get('actions', ())) if ticks else 0} 个候选 · 胜出 {selected_kind or '未决'}",
            "state": "complete" if selected else "unknown",
        },
        {
            "key": "readback",
            "label": "提交准备与现实回读",
            "detail": readback_detail,
            "state": readback_state,
        },
        {
            "key": "revision",
            "label": "版本化恢复结果",
            "detail": effect,
            "state": "complete" if revision is not None else "unknown",
        },
    )
    return ProjectEpisodeView(
        request_id=request_id,
        episode_kind="project_knowledge_review",
        episode_id=run.episode_id,
        project_id=run.review.project_id,
        status="success" if revision is not None else "partial",
        input={
            "knowledge_review": run.review.to_dict(),
            "proposal_ref": run.proposal.proposal_id,
        },
        ticks=ticks,
        counters=_counters(ticks),
        selected_action=dict(selected) if selected else None,
        knowledge_revision=revision,
        causal_summary=causal,
        product_effect=(
            "local_reviewed_recovery_milestone_created"
            if revision is not None
            else "knowledge_review_deferred_or_observed"
        ),
        growth_effect="recovery_revision_available_to_later_consumer" if revision is not None else "none_observed",
        ownership={
            "review_authority": "user",
            "codec_state_recall_prediction": "native",
            "feelings_attention_thought": "native_shallow",
            "action_selection": str(_mapping(_mapping(ticks[0]).get("decision")).get("owner") or "unknown") if ticks else "unknown",
            "dispatch_and_readback": "native",
            "local_recovery_revision": "native",
            "vibe_formal_knowledge": "absent",
            "teacher": "absent",
        },
        limitations=(
            "local_reviewed_recovery_only",
            "vibe_formal_write_disabled",
            "codex_skill_consumption_not_yet_connected",
        ),
        unknowns=(
            "live_vibe_round_trip_unavailable",
            "independent_codex_session_consumption_unmeasured",
        ),
    )


__all__ = [
    "AP_VIBE_PROJECTION_VERSION",
    "ProjectEpisodeView",
    "project_activity_run",
    "project_feedback_run",
    "project_knowledge_review_run",
]
