"""Project-cognition contracts for the AP-Vibe environment.

This module is the first Vibe-specific vertical slice on top of the generic
``MindRuntime``.  It turns a source-grounded engineering activity into the
ordinary AP event stream and exposes *affordances* for staging a knowledge
proposal, asking the user, deferring, or observing.  It does not maintain a
second planner and it never promotes a proposal to formal project knowledge.

The field lists are deliberately open-world.  Unknown activity kinds, status
values, and extension fields survive round-trips.  Only collection sizes and
physical authority boundaries are constrained.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Sequence

from .contracts import (
    CapabilityOwnership,
    ContractError,
    DispatchReceipt,
    EventEnvelope,
    ResultEvent,
    utc_now,
)
from .environment import EnvironmentAdapter
from .gateway import HybridGateway, NullGateway
from .governance import GovernanceCompatibilityRecord
from .runtime import AttentionPolicy, MindRuntime
from .runtime_types import ActionCandidate, TickResult
from .storage import EventStore


MAX_ACTIVITY_TEXT = 12_000
MAX_ACTIVITY_ITEMS = 64
PROPOSAL_STATUS = frozenset(
    {
        "proposed",
        "staged",
        "needs_user",
        "deferred",
        "superseded",
        "rejected",
        "accepted",
    }
)
PROJECT_ACTION_KINDS = frozenset(
    {
        "stage_knowledge_candidate",
        "ask_user",
        "defer",
        "observe_only",
        "record_feedback_lesson",
    }
)
CURRICULUM_ACTION_KINDS = frozenset({"adopt_curriculum", "defer", "reject_curriculum"})
LESSON_STATUS = frozenset({"proposed", "recorded", "applied", "retracted", "unknown"})
CURRICULUM_STATUS = frozenset(
    {
        "staged",
        "tentative_unsupported",
        "active_trial",
        "deferred",
        "rejected",
        "reteach",
        "retracted",
    }
)
CURRICULUM_ATTEMPT_STATUS = frozenset({"attempted", "withheld", "unknown"})
CURRICULUM_OUTCOME_STATUS = frozenset(
    {
        "active_trial",
        "deferred",
        "rejected",
        "success",
        "counterexample",
        "unknown",
        "replaced",
    }
)
CURRICULUM_RESULTING_STATUS = frozenset(
    {"active_trial", "deferred", "rejected", "reteach", "retracted"}
)
CURRICULUM_TARGET_CAPABILITY = "project.action_selection"
CURRICULUM_RECALL_CAPABILITY = "project.recall"
CURRICULUM_APPRAISAL_CAPABILITY = "project.appraisal"
CURRICULUM_PREDICTION_CAPABILITY = "project.prediction"
CURRICULUM_THOUGHT_CAPABILITY = "project.thought"
CURRICULUM_PARADIGM_CAPABILITY = "project.paradigm"
CURRICULUM_ATTENTION_CAPABILITY = "project.attention"
CURRICULUM_EXPRESSION_CAPABILITY = "project.expression"
CURRICULUM_PARAMETER_CAPABILITY = "project.parameter_tuning"
CURRICULUM_SUPPORTED_CAPABILITIES = frozenset(
    {
        CURRICULUM_TARGET_CAPABILITY,
        CURRICULUM_RECALL_CAPABILITY,
        CURRICULUM_APPRAISAL_CAPABILITY,
        CURRICULUM_PREDICTION_CAPABILITY,
        CURRICULUM_THOUGHT_CAPABILITY,
        CURRICULUM_PARADIGM_CAPABILITY,
        CURRICULUM_ATTENTION_CAPABILITY,
        CURRICULUM_EXPRESSION_CAPABILITY,
        CURRICULUM_PARAMETER_CAPABILITY,
    }
)
CURRICULUM_TARGET_ACTIONS = frozenset(
    {"stage_knowledge_candidate", "ask_user", "defer", "observe_only"}
)
MAX_CURRICULA_PER_ACTIVITY = 16
MAX_ACTIVE_CURRICULA_PER_EPISODE = 16
CURRICULUM_SINGLE_TRIAL_LIMIT = 0.18
CURRICULUM_CAPABILITY_LIMIT = 0.36
CURRICULUM_APPRAISAL_SIGNALS = frozenset(
    {
        "source_incomplete",
        "prediction_mismatch_high",
        "novel_input",
        "open_items_present",
        "unknown_present",
        "conflict_present",
        "completion_observed",
    }
)
CURRICULUM_MATURITY_POLICY: Mapping[str, float | int] = {
    "window_size": 100,
    "minimum_opportunities": 20,
    "audit_agreement": 0.95,
    "reteach_counterexamples": 1,
    "teaching_rate": 1.0,
    "trial_rate": 0.5,
    "audit_rate": 0.1,
}

PARADIGM_KINDS = frozenset({"relation_frame", "sequence_frame", "expression_frame"})
PARADIGM_INVARIANTS = frozenset(
    {"source_completeness", "has_open_items", "has_unknowns", "has_conflicts", "has_next_action"}
)
PARADIGM_SLOT_SOURCES = frozenset(
    {
        "activity.summary",
        "activity.observed_next_action",
        "activity.observed_remaining",
        "activity.observed_unknown",
        "proposition.content",
        "recall.summary",
    }
)
PARADIGM_RELATIONS = frozenset(
    {"precedes", "corresponds_to", "constrains", "supports", "contrasts_with", "fills"}
)
ATTENTION_MODES = frozenset({"maintain_attention", "shift_attention", "diversify_attention"})
EXPRESSION_TONES = frozenset({"neutral", "warm", "concise", "careful"})
EXPRESSION_PREFIXES = frozenset(
    {"", "我目前能确认的是：", "按现有信息，", "就目前看到的情况，", "我先说明能确认的部分："}
)
EXPRESSION_SUFFIXES = frozenset({"", "。"})
ATTENTION_PARAMETER_BOUNDS: Mapping[str, tuple[float, float]] = {
    "attention.novelty_weight": (0.05, 0.45),
    "attention.mismatch_weight": (0.05, 0.45),
    "attention.recall_weight": (0.05, 0.45),
    "attention.open_goal_weight": (0.05, 0.45),
    "attention.paradigm_weight": (0.05, 0.45),
    "attention.fatigue_inhibition_weight": (0.02, 0.30),
}
ATTENTION_PARAMETER_DEFAULTS: Mapping[str, float] = {
    "attention.novelty_weight": 0.24,
    "attention.mismatch_weight": 0.22,
    "attention.recall_weight": 0.16,
    "attention.open_goal_weight": 0.16,
    "attention.paradigm_weight": 0.12,
    "attention.fatigue_inhibition_weight": 0.10,
}
PARAMETER_SINGLE_TRIAL_LIMIT = 0.05
PARAMETER_AGGREGATE_LIMIT = 0.10


@dataclass(frozen=True)
class CurriculumMaturityPolicy:
    """Central, injectable policy for capability-local teacher sampling."""

    window_size: int = 100
    minimum_opportunities: int = 20
    audit_agreement: float = 0.95
    reteach_counterexamples: int = 1
    teaching_rate: float = 1.0
    trial_rate: float = 0.5
    audit_rate: float = 0.1

    def __post_init__(self) -> None:
        if isinstance(self.window_size, bool) or not 1 <= int(self.window_size) <= 1000:
            raise ContractError("maturity_window_size_out_of_bounds")
        if isinstance(self.minimum_opportunities, bool) or not 1 <= int(self.minimum_opportunities) <= int(self.window_size):
            raise ContractError("maturity_minimum_opportunities_out_of_bounds")
        if isinstance(self.reteach_counterexamples, bool) or not 1 <= int(self.reteach_counterexamples) <= int(self.window_size):
            raise ContractError("maturity_reteach_counterexamples_out_of_bounds")
        for name in ("audit_agreement", "teaching_rate", "trial_rate", "audit_rate"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 < float(value) <= 1.0:
                raise ContractError(f"maturity_{name}_out_of_bounds")

    def to_dict(self) -> dict[str, float | int]:
        return {
            "window_size": int(self.window_size),
            "minimum_opportunities": int(self.minimum_opportunities),
            "audit_agreement": float(self.audit_agreement),
            "reteach_counterexamples": int(self.reteach_counterexamples),
            "teaching_rate": float(self.teaching_rate),
            "trial_rate": float(self.trial_rate),
            "audit_rate": float(self.audit_rate),
        }


DEFAULT_CURRICULUM_MATURITY_POLICY = CurriculumMaturityPolicy(
    **dict(CURRICULUM_MATURITY_POLICY)
)


def _text(value: Any, name: str, *, limit: int = MAX_ACTIVITY_TEXT, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{name}_must_be_text")
    clean = value.strip()
    if not clean and not allow_empty:
        raise ContractError(f"{name}_must_not_be_empty")
    if len(clean) > limit:
        raise ContractError(f"{name}_exceeds_bound:{limit}")
    return clean


def _optional_text(value: Any, name: str, *, limit: int = MAX_ACTIVITY_TEXT) -> str | None:
    if value is None:
        return None
    clean = _text(value, name, limit=limit, allow_empty=True)
    return clean or None


def _texts(value: Any, name: str, *, max_items: int = MAX_ACTIVITY_ITEMS, limit: int = 2048) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractError(f"{name}_must_be_a_sequence")
    if len(value) > max_items:
        raise ContractError(f"{name}_exceeds_bound:{max_items}")
    return tuple(_text(item, f"{name}_item", limit=limit) for item in value)


def _mapping(value: Any, name: str, *, max_items: int = 128) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContractError(f"{name}_must_be_an_object")
    if len(value) > max_items:
        raise ContractError(f"{name}_exceeds_bound:{max_items}")
    try:
        copied = json.loads(json.dumps(dict(value), ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_must_be_json_compatible") from exc
    if not isinstance(copied, dict):  # pragma: no cover - protected by input check
        raise ContractError(f"{name}_must_be_an_object")
    return copied


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _stable_id(prefix: str, *parts: str) -> str:
    """Return a mechanical replay identity; the digest has no semantic role."""

    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


@dataclass(frozen=True)
class ProjectActivity:
    """One source-grounded engineering occurrence observed by AP-Vibe."""

    activity_id: str
    project_id: str
    kind: str
    summary: str
    source_ref: str
    detail: str = ""
    conversation_id: str | None = None
    actor: str = "unknown"
    status: str = "observed"
    occurred_at: str = field(default_factory=utc_now)
    evidence_refs: tuple[str, ...] = ()
    completeness: str = "complete"
    privacy_scope: str = "project"
    observed_completed: tuple[str, ...] = ()
    observed_remaining: tuple[str, ...] = ()
    observed_unknown: tuple[str, ...] = ()
    observed_resolved_remaining: tuple[str, ...] = ()
    observed_resolved_unknown: tuple[str, ...] = ()
    observed_redlines: tuple[str, ...] = ()
    observed_next_action: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("activity_id", "project_id", "kind", "summary", "source_ref"):
            _text(getattr(self, name), name, limit=MAX_ACTIVITY_TEXT if name == "summary" else 512)
        _text(self.detail, "detail", allow_empty=True)
        _optional_text(self.conversation_id, "conversation_id", limit=512)
        _text(self.actor, "actor", limit=512)
        _text(self.status, "status", limit=256)
        _text(self.occurred_at, "occurred_at", limit=128)
        _texts(self.evidence_refs, "evidence_refs", limit=2048)
        if self.completeness not in {"complete", "partial", "unknown", "search_incomplete"}:
            raise ContractError("activity_completeness_unsupported")
        _text(self.privacy_scope, "privacy_scope", limit=256)
        for name in (
            "observed_completed",
            "observed_remaining",
            "observed_unknown",
            "observed_resolved_remaining",
            "observed_resolved_unknown",
            "observed_redlines",
        ):
            _texts(getattr(self, name), name)
        if set(self.observed_remaining) & set(self.observed_resolved_remaining):
            raise ContractError("activity_remaining_cannot_be_resolved_and_open")
        if set(self.observed_unknown) & set(self.observed_resolved_unknown):
            raise ContractError("activity_unknown_cannot_be_resolved_and_open")
        _optional_text(self.observed_next_action, "observed_next_action", limit=2048)
        _mapping(self.extra, "extra")

    def to_dict(self) -> dict[str, Any]:
        base = {
            "activity_id": self.activity_id,
            "project_id": self.project_id,
            "conversation_id": self.conversation_id,
            "kind": self.kind,
            "summary": self.summary,
            "detail": self.detail,
            "actor": self.actor,
            "status": self.status,
            "occurred_at": self.occurred_at,
            "source_ref": self.source_ref,
            "evidence_refs": list(self.evidence_refs),
            "completeness": self.completeness,
            "privacy_scope": self.privacy_scope,
            "observed_completed": list(self.observed_completed),
            "observed_remaining": list(self.observed_remaining),
            "observed_unknown": list(self.observed_unknown),
            "observed_resolved_remaining": list(self.observed_resolved_remaining),
            "observed_resolved_unknown": list(self.observed_resolved_unknown),
            "observed_redlines": list(self.observed_redlines),
            "observed_next_action": self.observed_next_action,
        }
        for key, value in _mapping(self.extra, "extra").items():
            if key not in base:
                base[key] = value
        return base

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProjectActivity":
        if not isinstance(raw, Mapping):
            raise ContractError("project_activity_must_be_an_object")
        known_names = {
            "activity_id",
            "project_id",
            "conversation_id",
            "kind",
            "summary",
            "detail",
            "actor",
            "status",
            "occurred_at",
            "source_ref",
            "evidence_refs",
            "completeness",
            "privacy_scope",
            "observed_completed",
            "observed_remaining",
            "observed_unknown",
            "observed_resolved_remaining",
            "observed_resolved_unknown",
            "observed_redlines",
            "observed_next_action",
            "extra",
        }
        extra = _mapping(raw.get("extra"), "extra")
        for key, value in raw.items():
            if key not in known_names:
                extra.setdefault(str(key), value)
        values = {key: raw[key] for key in known_names if key in raw and key != "extra"}
        for name in (
            "evidence_refs",
            "observed_completed",
            "observed_remaining",
            "observed_unknown",
            "observed_resolved_remaining",
            "observed_resolved_unknown",
            "observed_redlines",
        ):
            if name in values:
                values[name] = _texts(values[name], name)
        values["extra"] = extra
        return cls(**values)

    def as_event(
        self,
        *,
        runtime_id: str,
        organism_id: str,
        episode_id: str,
    ) -> EventEnvelope:
        evidence = tuple(dict.fromkeys((self.source_ref, *self.evidence_refs)))
        return EventEnvelope(
            event_id=_stable_id("evt", self.project_id, self.activity_id, self.source_ref),
            runtime_id=runtime_id,
            organism_id=organism_id,
            environment_id=f"ap-vibe:{self.project_id}",
            subject_scope="project",
            episode_id=episode_id,
            source="external",
            role="observation",
            modality="project_activity",
            occurred_at=self.occurred_at,
            observed_at=utc_now(),
            payload_inline=self.to_dict(),
            evidence_refs=evidence,
            lineage_refs=(f"project:{self.project_id}", self.source_ref),
            privacy_scope=self.privacy_scope,
            completeness=self.completeness,
            idempotency_key=f"ap-vibe-activity:{self.project_id}:{self.activity_id}",
            extra={
                "activity_kind": self.kind,
                "activity_status": self.status,
                "conversation_id": self.conversation_id,
            },
        )


@dataclass(frozen=True)
class ProjectKnowledgeProposal:
    """A versionable candidate which is explicitly not formal knowledge."""

    proposal_id: str
    project_id: str
    target_sections: tuple[str, ...]
    summary: str
    source_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    completed: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    resolved_remaining: tuple[str, ...] = ()
    resolved_unknown: tuple[str, ...] = ()
    section_changes: tuple[Mapping[str, Any], ...] = ()
    redlines: tuple[str, ...] = ()
    next_action: str | None = None
    ap_basis: Mapping[str, Any] = field(default_factory=dict)
    teacher_basis: Mapping[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    uncertainty: float = 1.0
    conflicts: tuple[str, ...] = ()
    status: str = "proposed"
    created_at: str = field(default_factory=utc_now)
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.proposal_id, "proposal_id", limit=512)
        _text(self.project_id, "project_id", limit=512)
        _texts(self.target_sections, "target_sections", max_items=11, limit=128)
        _text(self.summary, "summary")
        for name in (
            "source_refs",
            "evidence_refs",
            "completed",
            "remaining",
            "unknown",
            "resolved_remaining",
            "resolved_unknown",
            "redlines",
            "conflicts",
        ):
            _texts(getattr(self, name), name)
        if set(self.remaining) & set(self.resolved_remaining):
            raise ContractError("proposal_remaining_cannot_be_resolved_and_open")
        if set(self.unknown) & set(self.resolved_unknown):
            raise ContractError("proposal_unknown_cannot_be_resolved_and_open")
        if isinstance(self.section_changes, (str, bytes)) or not isinstance(self.section_changes, Sequence):
            raise ContractError("proposal_section_changes_must_be_a_sequence")
        if len(self.section_changes) > 24:
            raise ContractError("proposal_section_changes_exceeds_bound")
        for item in self.section_changes:
            _mapping(item, "proposal_section_change", max_items=32)
        _optional_text(self.next_action, "next_action", limit=2048)
        _mapping(self.ap_basis, "ap_basis")
        _mapping(self.teacher_basis, "teacher_basis")
        if isinstance(self.confidence, bool) or not 0.0 <= float(self.confidence) <= 1.0:
            raise ContractError("proposal_confidence_out_of_bounds")
        if isinstance(self.uncertainty, bool) or not 0.0 <= float(self.uncertainty) <= 1.0:
            raise ContractError("proposal_uncertainty_out_of_bounds")
        if self.status not in PROPOSAL_STATUS:
            raise ContractError("proposal_status_unsupported")
        _text(self.created_at, "created_at", limit=128)
        _mapping(self.extra, "extra")

    def to_dict(self) -> dict[str, Any]:
        base = {
            "proposal_id": self.proposal_id,
            "project_id": self.project_id,
            "target_sections": list(self.target_sections),
            "summary": self.summary,
            "completed": list(self.completed),
            "remaining": list(self.remaining),
            "unknown": list(self.unknown),
            "resolved_remaining": list(self.resolved_remaining),
            "resolved_unknown": list(self.resolved_unknown),
            "section_changes": [
                _mapping(item, "proposal_section_change", max_items=32)
                for item in self.section_changes
            ],
            "redlines": list(self.redlines),
            "next_action": self.next_action,
            "source_refs": list(self.source_refs),
            "evidence_refs": list(self.evidence_refs),
            "ap_basis": _mapping(self.ap_basis, "ap_basis"),
            "teacher_basis": _mapping(self.teacher_basis, "teacher_basis"),
            "confidence": round(_clamp(self.confidence), 6),
            "uncertainty": round(_clamp(self.uncertainty), 6),
            "conflicts": list(self.conflicts),
            "status": self.status,
            "created_at": self.created_at,
            "formal_knowledge": False,
        }
        for key, value in _mapping(self.extra, "extra").items():
            if key not in base:
                base[key] = value
        return base

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProjectKnowledgeProposal":
        if not isinstance(raw, Mapping):
            raise ContractError("project_knowledge_proposal_must_be_an_object")
        names = {
            "proposal_id",
            "project_id",
            "target_sections",
            "summary",
            "completed",
            "remaining",
            "unknown",
            "resolved_remaining",
            "resolved_unknown",
            "section_changes",
            "redlines",
            "next_action",
            "source_refs",
            "evidence_refs",
            "ap_basis",
            "teacher_basis",
            "confidence",
            "uncertainty",
            "conflicts",
            "status",
            "created_at",
            "extra",
        }
        extra = _mapping(raw.get("extra"), "extra")
        for key, value in raw.items():
            if key not in names and key != "formal_knowledge":
                extra.setdefault(str(key), value)
        values = {key: raw[key] for key in names if key in raw and key != "extra"}
        for name in (
            "target_sections",
            "completed",
            "remaining",
            "unknown",
            "resolved_remaining",
            "resolved_unknown",
            "redlines",
            "source_refs",
            "evidence_refs",
            "conflicts",
        ):
            if name in values:
                values[name] = _texts(values[name], name)
        if "section_changes" in values:
            raw_changes = values["section_changes"]
            if isinstance(raw_changes, (str, bytes)) or not isinstance(raw_changes, Sequence):
                raise ContractError("proposal_section_changes_must_be_a_sequence")
            values["section_changes"] = tuple(
                _mapping(item, "proposal_section_change", max_items=32)
                for item in raw_changes
            )
        values["extra"] = extra
        return cls(**values)

    def with_status(self, status: str) -> "ProjectKnowledgeProposal":
        return replace(self, status=status)

    def with_teacher(self, teacher_basis: Mapping[str, Any]) -> "ProjectKnowledgeProposal":
        return replace(self, teacher_basis=_mapping(teacher_basis, "teacher_basis"))


@dataclass(frozen=True)
class ProjectFeedback:
    """One explicit user/environment evaluation of a prior project action."""

    feedback_id: str
    project_id: str
    target_episode_id: str
    target_action: str
    signal: str
    magnitude: float
    natural_language: str
    source_ref: str
    desired_action: str | None = None
    evidence_refs: tuple[str, ...] = ()
    applicability: Mapping[str, Any] = field(default_factory=dict)
    counterexamples: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("feedback_id", "project_id", "target_episode_id", "target_action", "signal", "source_ref"):
            _text(getattr(self, name), name, limit=512)
        if isinstance(self.magnitude, bool) or not isinstance(self.magnitude, (int, float)) or not 0.0 <= float(self.magnitude) <= 1.0:
            raise ContractError("feedback_magnitude_out_of_bounds")
        _text(self.natural_language, "natural_language", limit=4096)
        _optional_text(self.desired_action, "desired_action", limit=512)
        _texts(self.evidence_refs, "evidence_refs", limit=2048)
        _mapping(self.applicability, "applicability")
        _texts(self.counterexamples, "counterexamples", limit=2048)
        _text(self.created_at, "created_at", limit=128)
        _mapping(self.extra, "extra")

    def to_dict(self) -> dict[str, Any]:
        base = {
            "feedback_id": self.feedback_id,
            "project_id": self.project_id,
            "target_episode_id": self.target_episode_id,
            "target_action": self.target_action,
            "desired_action": self.desired_action,
            "signal": self.signal,
            "magnitude": float(self.magnitude),
            "natural_language": self.natural_language,
            "source_ref": self.source_ref,
            "evidence_refs": list(self.evidence_refs),
            "applicability": _mapping(self.applicability, "applicability"),
            "counterexamples": list(self.counterexamples),
            "created_at": self.created_at,
        }
        for key, value in _mapping(self.extra, "extra").items():
            if key not in base:
                base[key] = value
        return base

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProjectFeedback":
        if not isinstance(raw, Mapping):
            raise ContractError("project_feedback_must_be_an_object")
        names = {
            "feedback_id", "project_id", "target_episode_id", "target_action",
            "desired_action", "signal", "magnitude", "natural_language", "source_ref",
            "evidence_refs", "applicability", "counterexamples", "created_at", "extra",
        }
        extra = _mapping(raw.get("extra"), "extra")
        for key, value in raw.items():
            if key not in names:
                extra.setdefault(str(key), value)
        values = {key: raw[key] for key in names if key in raw and key != "extra"}
        for name in ("evidence_refs", "counterexamples"):
            if name in values:
                values[name] = _texts(values[name], name)
        values["extra"] = extra
        return cls(**values)

    def as_event(
        self,
        *,
        runtime_id: str,
        organism_id: str,
        episode_id: str,
        target_activity: ProjectActivity | None = None,
    ) -> EventEnvelope:
        extra: dict[str, Any] = {
            "feedback_signal": self.signal,
            "target_action": self.target_action,
        }
        if target_activity is not None:
            if target_activity.project_id != self.project_id:
                raise ContractError("feedback_target_project_mismatch")
            extra["target_activity"] = target_activity.to_dict()
        return EventEnvelope(
            event_id=_stable_id("evt", self.project_id, self.feedback_id, self.source_ref),
            runtime_id=runtime_id,
            organism_id=organism_id,
            environment_id=f"ap-vibe:{self.project_id}",
            subject_scope="project",
            episode_id=episode_id,
            source="user_feedback",
            role="teaching",
            modality="project_feedback",
            occurred_at=self.created_at,
            observed_at=utc_now(),
            payload_inline=self.to_dict(),
            evidence_refs=tuple(dict.fromkeys((self.source_ref, *self.evidence_refs))),
            lineage_refs=(self.target_episode_id, self.source_ref),
            privacy_scope="project",
            completeness="complete",
            idempotency_key=f"ap-vibe-feedback:{self.project_id}:{self.feedback_id}",
            extra=extra,
        )


@dataclass(frozen=True)
class FeedbackLesson:
    """A scoped learning proposal recorded only after action/readback."""

    lesson_id: str
    project_id: str
    feedback_ref: str
    target_capability: str
    target_refs: tuple[str, ...]
    signal: str
    magnitude: float
    natural_language: str
    structured_delta: Mapping[str, Any]
    applicability: Mapping[str, Any]
    counterexamples: tuple[str, ...] = ()
    source: str = "user"
    status: str = "proposed"
    created_at: str = field(default_factory=utc_now)
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("lesson_id", "project_id", "feedback_ref", "target_capability", "signal", "natural_language", "source"):
            _text(getattr(self, name), name, limit=4096 if name == "natural_language" else 512)
        _texts(self.target_refs, "target_refs", limit=2048)
        if isinstance(self.magnitude, bool) or not isinstance(self.magnitude, (int, float)) or not 0.0 <= float(self.magnitude) <= 1.0:
            raise ContractError("lesson_magnitude_out_of_bounds")
        _mapping(self.structured_delta, "structured_delta")
        _mapping(self.applicability, "applicability")
        _texts(self.counterexamples, "counterexamples", limit=2048)
        if self.status not in LESSON_STATUS:
            raise ContractError("lesson_status_unsupported")
        _text(self.created_at, "created_at", limit=128)
        _mapping(self.extra, "extra")

    def to_dict(self) -> dict[str, Any]:
        base = {
            "lesson_id": self.lesson_id,
            "project_id": self.project_id,
            "feedback_ref": self.feedback_ref,
            "target_capability": self.target_capability,
            "target_refs": list(self.target_refs),
            "signal": self.signal,
            "magnitude": float(self.magnitude),
            "natural_language": self.natural_language,
            "structured_delta": _mapping(self.structured_delta, "structured_delta"),
            "applicability": _mapping(self.applicability, "applicability"),
            "counterexamples": list(self.counterexamples),
            "source": self.source,
            "status": self.status,
            "created_at": self.created_at,
        }
        for key, value in _mapping(self.extra, "extra").items():
            if key not in base:
                base[key] = value
        return base

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FeedbackLesson":
        if not isinstance(raw, Mapping):
            raise ContractError("feedback_lesson_must_be_an_object")
        names = {
            "lesson_id", "project_id", "feedback_ref", "target_capability",
            "target_refs", "signal", "magnitude", "natural_language",
            "structured_delta", "applicability", "counterexamples", "source",
            "status", "created_at", "extra",
        }
        extra = _mapping(raw.get("extra"), "extra")
        for key, value in raw.items():
            if key not in names:
                extra.setdefault(str(key), value)
        values = {key: raw[key] for key in names if key in raw and key != "extra"}
        for name in ("target_refs", "counterexamples"):
            if name in values:
                values[name] = _texts(values[name], name)
        values["extra"] = extra
        return cls(**values)

    def with_status(self, status: str) -> "FeedbackLesson":
        return replace(self, status=status)


@dataclass(frozen=True)
class CurriculumCandidate:
    """One source-tagged, reversible teaching proposal.

    A candidate is curriculum, not truth or an installed rule.  Only a later
    AP-selected ``adopt_curriculum`` action followed by result-back may move it
    into ``active_trial``.  Digests below are recovery identities only and are
    never used as semantic features or score inputs.
    """

    curriculum_id: str
    project_id: str
    target_capability: str
    source_kind: str
    source_ref: str
    source_episode_ref: str
    trigger_features: Mapping[str, Any]
    suggested_adjustment: Mapping[str, Any]
    model_receipt_ref: str | None = None
    counterexamples: tuple[str, ...] = ()
    confidence: float = 0.0
    uncertainty: float = 1.0
    evidence_refs: tuple[str, ...] = ()
    status: str = "staged"
    created_at: str = field(default_factory=utc_now)
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "curriculum_id",
            "project_id",
            "target_capability",
            "source_kind",
            "source_ref",
            "source_episode_ref",
        ):
            _text(getattr(self, name), name, limit=512)
        _optional_text(self.model_receipt_ref, "model_receipt_ref", limit=512)
        _mapping(self.trigger_features, "trigger_features")
        _mapping(self.suggested_adjustment, "suggested_adjustment")
        _texts(self.counterexamples, "counterexamples", max_items=12, limit=2048)
        for name in ("confidence", "uncertainty"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                raise ContractError(f"curriculum_{name}_out_of_bounds")
        _texts(self.evidence_refs, "evidence_refs", limit=2048)
        if self.status not in CURRICULUM_STATUS:
            raise ContractError("curriculum_status_unsupported")
        _text(self.created_at, "created_at", limit=128)
        _mapping(self.extra, "extra")

    def to_dict(self) -> dict[str, Any]:
        base = {
            "curriculum_id": self.curriculum_id,
            "project_id": self.project_id,
            "target_capability": self.target_capability,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "source_episode_ref": self.source_episode_ref,
            "model_receipt_ref": self.model_receipt_ref,
            "trigger_features": _mapping(self.trigger_features, "trigger_features"),
            "suggested_adjustment": _mapping(self.suggested_adjustment, "suggested_adjustment"),
            "counterexamples": list(self.counterexamples),
            "confidence": round(float(self.confidence), 6),
            "uncertainty": round(float(self.uncertainty), 6),
            "evidence_refs": list(self.evidence_refs),
            "status": self.status,
            "created_at": self.created_at,
        }
        for key, value in _mapping(self.extra, "extra").items():
            if key not in base:
                base[key] = value
        return base

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CurriculumCandidate":
        if not isinstance(raw, Mapping):
            raise ContractError("curriculum_candidate_must_be_an_object")
        names = {
            "curriculum_id", "project_id", "target_capability", "source_kind",
            "source_ref", "source_episode_ref", "model_receipt_ref",
            "trigger_features", "suggested_adjustment", "counterexamples",
            "confidence", "uncertainty", "evidence_refs", "status",
            "created_at", "extra",
        }
        extra = _mapping(raw.get("extra"), "extra")
        for key, value in raw.items():
            if key not in names:
                extra.setdefault(str(key), value)
        values = {key: raw[key] for key in names if key in raw and key != "extra"}
        for name in ("counterexamples", "evidence_refs"):
            if name in values:
                values[name] = _texts(values[name], name, max_items=12 if name == "counterexamples" else 64)
        values["extra"] = extra
        return cls(**values)

    def with_status(self, status: str) -> "CurriculumCandidate":
        return replace(self, status=status)

    def as_event(self, *, runtime_id: str, organism_id: str, episode_id: str) -> EventEnvelope:
        return EventEnvelope(
            event_id=_stable_id("evt", self.project_id, self.curriculum_id, "teacher_curriculum"),
            runtime_id=runtime_id,
            organism_id=organism_id,
            environment_id=f"ap-vibe:{self.project_id}",
            subject_scope="project",
            episode_id=episode_id,
            source="llm" if self.source_kind == "llm_teacher" else self.source_kind,
            role="teaching",
            modality="teacher_curriculum",
            occurred_at=self.created_at,
            observed_at=utc_now(),
            payload_inline=self.to_dict(),
            evidence_refs=tuple(dict.fromkeys((self.source_ref, *self.evidence_refs))),
            lineage_refs=tuple(
                dict.fromkeys(
                    (
                        self.source_episode_ref,
                        self.source_ref,
                        *([self.model_receipt_ref] if self.model_receipt_ref else []),
                    )
                )
            ),
            privacy_scope="project",
            completeness="complete" if self.status == "staged" else "partial",
            idempotency_key=f"ap-vibe-curriculum:{self.project_id}:{self.curriculum_id}",
            # A curriculum event is a control/teaching event.  Runtime also
            # excludes it by source/role; the explicit flag makes that
            # permission visible to adapters and future runtimes.
            extra={"gateway_consultation_allowed": False, "curriculum_control_event": True},
        )


@dataclass(frozen=True)
class CurriculumAttempt:
    """One later, independent opportunity where an active trial contributed."""

    attempt_id: str
    curriculum_id: str
    project_id: str
    target_capability: str
    activity_episode_ref: str
    activity_ref: str
    feature_key: str
    target_action: str | None
    observed_winner: str | None
    contribution: float
    status: str
    effect_key: str | None = None
    local_before: Mapping[str, Any] = field(default_factory=dict)
    effective_result: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "attempt_id", "curriculum_id", "project_id", "target_capability",
            "activity_episode_ref", "activity_ref", "feature_key",
        ):
            _text(getattr(self, name), name, limit=1024)
        _optional_text(self.target_action, "target_action", limit=512)
        _optional_text(self.effect_key, "effect_key", limit=1024)
        _optional_text(self.observed_winner, "observed_winner", limit=512)
        if self.target_capability == CURRICULUM_TARGET_CAPABILITY and self.target_action not in CURRICULUM_TARGET_ACTIONS:
            raise ContractError("curriculum_attempt_action_unsupported")
        if self.target_capability != CURRICULUM_TARGET_CAPABILITY and not self.effect_key:
            raise ContractError("curriculum_attempt_effect_key_required")
        if isinstance(self.contribution, bool) or not isinstance(self.contribution, (int, float)):
            raise ContractError("curriculum_attempt_contribution_invalid")
        if abs(float(self.contribution)) > CURRICULUM_SINGLE_TRIAL_LIMIT + 1e-9:
            raise ContractError("curriculum_attempt_contribution_out_of_bounds")
        if self.status not in CURRICULUM_ATTEMPT_STATUS:
            raise ContractError("curriculum_attempt_status_unsupported")
        _text(self.created_at, "created_at", limit=128)
        _mapping(self.local_before, "local_before")
        _mapping(self.effective_result, "effective_result")
        _mapping(self.extra, "extra")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "curriculum_id": self.curriculum_id,
            "project_id": self.project_id,
            "target_capability": self.target_capability,
            "activity_episode_ref": self.activity_episode_ref,
            "activity_ref": self.activity_ref,
            "feature_key": self.feature_key,
            "target_action": self.target_action,
            "observed_winner": self.observed_winner,
            "contribution": round(float(self.contribution), 6),
            "status": self.status,
            "effect_key": self.effect_key,
            "local_before": _mapping(self.local_before, "local_before"),
            "effective_result": _mapping(self.effective_result, "effective_result"),
            "created_at": self.created_at,
            "extra": _mapping(self.extra, "extra"),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CurriculumAttempt":
        if not isinstance(raw, Mapping):
            raise ContractError("curriculum_attempt_must_be_an_object")
        return cls(
            attempt_id=raw.get("attempt_id"),
            curriculum_id=raw.get("curriculum_id"),
            project_id=raw.get("project_id"),
            target_capability=raw.get("target_capability"),
            activity_episode_ref=raw.get("activity_episode_ref"),
            activity_ref=raw.get("activity_ref"),
            feature_key=raw.get("feature_key"),
            target_action=raw.get("target_action"),
            observed_winner=raw.get("observed_winner"),
            contribution=raw.get("contribution"),
            status=raw.get("status"),
            effect_key=raw.get("effect_key"),
            local_before=_mapping(raw.get("local_before"), "local_before"),
            effective_result=_mapping(raw.get("effective_result"), "effective_result"),
            created_at=raw.get("created_at", utc_now()),
            extra=_mapping(raw.get("extra"), "extra"),
        )


@dataclass(frozen=True)
class CurriculumOutcome:
    """An append-only adoption or later feedback attribution for one trial."""

    outcome_id: str
    curriculum_id: str
    project_id: str
    outcome: str
    resulting_status: str
    source_ref: str
    episode_ref: str
    attempt_ref: str | None = None
    evidence_refs: tuple[str, ...] = ()
    rationale: str = ""
    created_at: str = field(default_factory=utc_now)
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("outcome_id", "curriculum_id", "project_id", "outcome", "resulting_status", "source_ref", "episode_ref"):
            _text(getattr(self, name), name, limit=1024)
        _optional_text(self.attempt_ref, "attempt_ref", limit=512)
        if self.outcome not in CURRICULUM_OUTCOME_STATUS:
            raise ContractError("curriculum_outcome_unsupported")
        if self.resulting_status not in CURRICULUM_RESULTING_STATUS:
            raise ContractError("curriculum_resulting_status_unsupported")
        _texts(self.evidence_refs, "evidence_refs", limit=2048)
        _text(self.rationale, "rationale", limit=4096, allow_empty=True)
        _text(self.created_at, "created_at", limit=128)
        _mapping(self.extra, "extra")

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "curriculum_id": self.curriculum_id,
            "project_id": self.project_id,
            "outcome": self.outcome,
            "resulting_status": self.resulting_status,
            "source_ref": self.source_ref,
            "episode_ref": self.episode_ref,
            "attempt_ref": self.attempt_ref,
            "evidence_refs": list(self.evidence_refs),
            "rationale": self.rationale,
            "created_at": self.created_at,
            "extra": _mapping(self.extra, "extra"),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CurriculumOutcome":
        if not isinstance(raw, Mapping):
            raise ContractError("curriculum_outcome_must_be_an_object")
        return cls(
            outcome_id=raw.get("outcome_id"),
            curriculum_id=raw.get("curriculum_id"),
            project_id=raw.get("project_id"),
            outcome=raw.get("outcome"),
            resulting_status=raw.get("resulting_status"),
            source_ref=raw.get("source_ref"),
            episode_ref=raw.get("episode_ref"),
            attempt_ref=raw.get("attempt_ref"),
            evidence_refs=_texts(raw.get("evidence_refs"), "evidence_refs"),
            rationale=raw.get("rationale", ""),
            created_at=raw.get("created_at", utc_now()),
            extra=_mapping(raw.get("extra"), "extra"),
        )


def _normalise_action_curriculum_adjustment(value: Mapping[str, Any]) -> tuple[dict[str, float], tuple[str, ...]]:
    """Accept only explicit existing action names and bounded numeric deltas."""

    raw = _mapping(value, "suggested_adjustment")
    reasons: list[str] = []
    adjustments: dict[str, float] = {}
    if "action_adjustments" in raw:
        nested = raw.get("action_adjustments")
        if not isinstance(nested, Mapping):
            reasons.append("action_adjustments_not_an_object")
        else:
            for action, delta in list(nested.items())[:16]:
                if action not in CURRICULUM_TARGET_ACTIONS:
                    reasons.append(f"unknown_action:{str(action)[:160]}")
                    continue
                if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not -1.0 <= float(delta) <= 1.0:
                    reasons.append(f"adjustment_out_of_bounds:{action}")
                    continue
                adjustments[str(action)] = round(float(delta) * CURRICULUM_SINGLE_TRIAL_LIMIT, 6)
    for key, direction in (("prefer", 1.0), ("avoid", -1.0)):
        if key not in raw:
            continue
        action = raw.get(key)
        if not isinstance(action, str) or action not in CURRICULUM_TARGET_ACTIONS:
            reasons.append(f"unknown_action:{str(action)[:160]}")
            continue
        prior = adjustments.get(action, 0.0)
        adjustments[action] = round(max(-CURRICULUM_SINGLE_TRIAL_LIMIT, min(CURRICULUM_SINGLE_TRIAL_LIMIT, prior + direction * CURRICULUM_SINGLE_TRIAL_LIMIT)), 6)
    if not adjustments:
        reasons.append("no_explicit_supported_action_adjustment")
    return adjustments, tuple(dict.fromkeys(reasons))


def _normalise_recall_curriculum_adjustment(value: Mapping[str, Any]) -> tuple[dict[str, float], tuple[str, ...]]:
    raw = _mapping(value, "suggested_adjustment")
    reasons: list[str] = []
    adjustments: dict[str, float] = {}
    nested = raw.get("memory_gain_adjustments")
    if isinstance(nested, Mapping):
        for memory_ref, delta in list(nested.items())[:16]:
            if not isinstance(memory_ref, str) or not memory_ref.strip():
                reasons.append("memory_ref_invalid")
                continue
            if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not -1.0 <= float(delta) <= 1.0:
                reasons.append(f"memory_gain_out_of_bounds:{str(memory_ref)[:160]}")
                continue
            adjustments[memory_ref.strip()] = round(float(delta) * CURRICULUM_SINGLE_TRIAL_LIMIT, 6)
    elif nested is not None:
        reasons.append("memory_gain_adjustments_not_an_object")
    preferred = raw.get("prefer_memory_ref")
    if preferred is not None:
        if not isinstance(preferred, str) or not preferred.strip():
            reasons.append("memory_ref_invalid")
        else:
            prior = adjustments.get(preferred.strip(), 0.0)
            adjustments[preferred.strip()] = round(
                max(-CURRICULUM_SINGLE_TRIAL_LIMIT, min(CURRICULUM_SINGLE_TRIAL_LIMIT, prior + CURRICULUM_SINGLE_TRIAL_LIMIT)),
                6,
            )
    if not adjustments:
        reasons.append("no_explicit_supported_memory_adjustment")
    return adjustments, tuple(dict.fromkeys(reasons))


def _normalise_appraisal_curriculum_adjustment(
    value: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    raw = _mapping(value, "suggested_adjustment")
    source = raw.get("appraisal_effects")
    reasons: list[str] = []
    effects: list[Mapping[str, Any]] = []
    if isinstance(source, (str, bytes)) or not isinstance(source, Sequence):
        reasons.append("appraisal_effects_not_a_sequence")
        return (), tuple(reasons)
    for index, item in enumerate(source[:8]):
        if not isinstance(item, Mapping):
            reasons.append(f"appraisal_effect_not_an_object:{index}")
            continue
        name = item.get("name")
        delta = item.get("intensity_delta")
        signals = item.get("required_signals")
        if not isinstance(name, str) or not name.strip():
            reasons.append(f"appraisal_name_invalid:{index}")
            continue
        if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not -1.0 <= float(delta) <= 1.0:
            reasons.append(f"appraisal_delta_out_of_bounds:{name[:120]}")
            continue
        if isinstance(signals, (str, bytes)) or not isinstance(signals, Sequence):
            reasons.append(f"appraisal_required_signals_missing:{name[:120]}")
            continue
        signal_values = tuple(dict.fromkeys(str(signal) for signal in signals if isinstance(signal, str) and signal))
        if not signal_values:
            reasons.append(f"appraisal_required_signals_missing:{name[:120]}")
            continue
        unsupported = tuple(signal for signal in signal_values if signal not in CURRICULUM_APPRAISAL_SIGNALS)
        if unsupported:
            reasons.extend(f"unsupported_appraisal_signal:{signal}" for signal in unsupported)
            continue
        valence = item.get("valence")
        if valence is not None and (
            isinstance(valence, bool) or not isinstance(valence, (int, float)) or not -1.0 <= float(valence) <= 1.0
        ):
            reasons.append(f"appraisal_valence_out_of_bounds:{name[:120]}")
            continue
        effect: dict[str, Any] = {
            "name": name.strip()[:120],
            "intensity_delta": round(float(delta) * CURRICULUM_SINGLE_TRIAL_LIMIT, 6),
            "required_signals": list(signal_values),
        }
        if valence is not None:
            effect["valence"] = round(float(valence), 6)
        effects.append(effect)
    if not effects:
        reasons.append("no_explicit_supported_appraisal_effect")
    return tuple(effects), tuple(dict.fromkeys(reasons))


def _normalise_prediction_curriculum_adjustment(
    value: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    """Normalise hypotheses without turning them into observed reality."""

    raw = _mapping(value, "suggested_adjustment")
    source = raw.get("prediction_hypotheses")
    reasons: list[str] = []
    hypotheses: list[Mapping[str, Any]] = []
    if isinstance(source, (str, bytes)) or not isinstance(source, Sequence):
        return (), ("prediction_hypotheses_not_a_sequence",)
    for index, item in enumerate(source[:4]):
        if not isinstance(item, Mapping):
            reasons.append(f"prediction_hypothesis_not_an_object:{index}")
            continue
        content = item.get("content")
        mode = item.get("mode", "forecast")
        confidence = item.get("confidence")
        uncertainty = item.get("uncertainty")
        if not isinstance(content, str) or not content.strip():
            reasons.append(f"prediction_content_invalid:{index}")
            continue
        if mode not in {"forecast", "attribution", "relationship"}:
            reasons.append(f"prediction_mode_invalid:{index}")
            continue
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
            reasons.append(f"prediction_confidence_invalid:{index}")
            continue
        if isinstance(uncertainty, bool) or not isinstance(uncertainty, (int, float)) or not 0.0 <= float(uncertainty) <= 1.0:
            reasons.append(f"prediction_uncertainty_invalid:{index}")
            continue
        completeness = item.get("completeness", "unknown")
        if completeness not in {"complete", "partial", "unknown", "search_incomplete"}:
            reasons.append(f"prediction_completeness_invalid:{index}")
            completeness = "unknown"
        evidence_refs = _texts(item.get("evidence_refs"), "prediction_evidence_refs", max_items=16, limit=1024)
        hypotheses.append(
            {
                "content": content.strip()[:1600],
                "mode": str(mode),
                "confidence": round(_clamp(float(confidence)), 6),
                "uncertainty": round(_clamp(float(uncertainty)), 6),
                "evidence_refs": list(evidence_refs),
                "completeness": str(completeness),
            }
        )
    if not hypotheses:
        reasons.append("no_explicit_supported_prediction_hypothesis")
    return tuple(hypotheses), tuple(dict.fromkeys(reasons))


def _normalise_thought_curriculum_adjustment(
    value: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    """Normalise private thought scaffolds; they do not author propositions."""

    raw = _mapping(value, "suggested_adjustment")
    source = raw.get("thought_scaffolds")
    reasons: list[str] = []
    scaffolds: list[Mapping[str, Any]] = []
    if isinstance(source, (str, bytes)) or not isinstance(source, Sequence):
        return (), ("thought_scaffolds_not_a_sequence",)
    for index, item in enumerate(source[:4]):
        if not isinstance(item, Mapping):
            reasons.append(f"thought_scaffold_not_an_object:{index}")
            continue
        content = item.get("content")
        uncertainty = item.get("uncertainty")
        if not isinstance(content, str) or not content.strip():
            reasons.append(f"thought_content_invalid:{index}")
            continue
        if isinstance(uncertainty, bool) or not isinstance(uncertainty, (int, float)) or not 0.0 <= float(uncertainty) <= 1.0:
            reasons.append(f"thought_uncertainty_invalid:{index}")
            continue
        scaffolds.append(
            {
                "content": content.strip()[:1600],
                "unresolved": list(_texts(item.get("unresolved"), "thought_unresolved", max_items=8, limit=240)),
                "evidence_refs": list(_texts(item.get("evidence_refs"), "thought_evidence_refs", max_items=16, limit=1024)),
                "uncertainty": round(_clamp(float(uncertainty)), 6),
            }
        )
    if not scaffolds:
        reasons.append("no_explicit_supported_thought_scaffold")
    return tuple(scaffolds), tuple(dict.fromkeys(reasons))


def _normalise_paradigm_curriculum_adjustment(
    value: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    """Keep only structural, field-bound patterns; never executable prose."""

    raw = _mapping(value, "suggested_adjustment")
    source = raw.get("paradigm_patterns")
    reasons: list[str] = []
    patterns: list[Mapping[str, Any]] = []
    if isinstance(source, (str, bytes)) or not isinstance(source, Sequence):
        return (), ("paradigm_patterns_not_a_sequence",)
    for index, item in enumerate(source[:4]):
        if not isinstance(item, Mapping):
            reasons.append(f"paradigm_pattern_not_an_object:{index}")
            continue
        pattern_kind = item.get("pattern_kind")
        if pattern_kind not in PARADIGM_KINDS:
            reasons.append(f"paradigm_kind_invalid:{index}")
            continue
        raw_invariants = item.get("invariants")
        invariants: dict[str, Any] = {}
        if not isinstance(raw_invariants, Mapping) or not raw_invariants:
            reasons.append(f"paradigm_invariants_missing:{index}")
            continue
        for key, invariant_value in list(raw_invariants.items())[:8]:
            if key not in PARADIGM_INVARIANTS:
                reasons.append(f"paradigm_invariant_unsupported:{str(key)[:80]}")
                continue
            if not isinstance(invariant_value, (str, int, float, bool)) or invariant_value is None:
                reasons.append(f"paradigm_invariant_value_invalid:{str(key)[:80]}")
                continue
            invariants[str(key)] = invariant_value
        raw_slots = item.get("slots")
        slots: list[Mapping[str, Any]] = []
        slot_names: set[str] = set()
        if isinstance(raw_slots, Sequence) and not isinstance(raw_slots, (str, bytes)):
            for slot_index, slot in enumerate(raw_slots[:8]):
                if not isinstance(slot, Mapping):
                    reasons.append(f"paradigm_slot_not_an_object:{index}:{slot_index}")
                    continue
                name = slot.get("name")
                field_source = slot.get("source")
                required = slot.get("required", True)
                if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) is None:
                    reasons.append(f"paradigm_slot_name_invalid:{index}:{slot_index}")
                    continue
                if name in slot_names:
                    reasons.append(f"paradigm_slot_duplicate:{name}")
                    continue
                if field_source not in PARADIGM_SLOT_SOURCES:
                    reasons.append(f"paradigm_slot_source_unsupported:{name}")
                    continue
                if not isinstance(required, bool):
                    reasons.append(f"paradigm_slot_required_invalid:{name}")
                    continue
                slot_names.add(name)
                slots.append({"name": name, "source": str(field_source), "required": required})
        if not invariants or not slots:
            reasons.append(f"paradigm_structure_incomplete:{index}")
            continue
        relations: list[Mapping[str, Any]] = []
        raw_relations = item.get("relations", ())
        if isinstance(raw_relations, Sequence) and not isinstance(raw_relations, (str, bytes)):
            for relation_index, relation in enumerate(raw_relations[:12]):
                if not isinstance(relation, Mapping):
                    reasons.append(f"paradigm_relation_not_an_object:{index}:{relation_index}")
                    continue
                source_slot = relation.get("source_slot")
                target_slot = relation.get("target_slot")
                relation_kind = relation.get("relation")
                if source_slot not in slot_names or target_slot not in slot_names:
                    reasons.append(f"paradigm_relation_slot_invalid:{index}:{relation_index}")
                    continue
                if relation_kind not in PARADIGM_RELATIONS:
                    reasons.append(f"paradigm_relation_kind_invalid:{index}:{relation_index}")
                    continue
                relations.append(
                    {"source_slot": source_slot, "target_slot": target_slot, "relation": relation_kind}
                )
        patterns.append(
            {
                "pattern_kind": pattern_kind,
                "invariants": invariants,
                "slots": slots,
                "relations": relations,
                "evidence_refs": list(
                    _texts(item.get("evidence_refs"), "paradigm_evidence_refs", max_items=16, limit=1024)
                ),
                "completeness": str(item.get("completeness", "unknown"))
                if item.get("completeness", "unknown") in {"complete", "partial", "unknown", "search_incomplete"}
                else "unknown",
            }
        )
    if not patterns:
        reasons.append("no_explicit_supported_paradigm_pattern")
    return tuple(patterns), tuple(dict.fromkeys(reasons))


def _normalise_attention_curriculum_adjustment(
    value: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    raw = _mapping(value, "suggested_adjustment")
    source = raw.get("attention_adjustments")
    reasons: list[str] = []
    adjustments: list[Mapping[str, Any]] = []
    if isinstance(source, (str, bytes)) or not isinstance(source, Sequence):
        return (), ("attention_adjustments_not_a_sequence",)
    for index, item in enumerate(source[:4]):
        if not isinstance(item, Mapping):
            reasons.append(f"attention_adjustment_not_an_object:{index}")
            continue
        mode = item.get("mode")
        selector = item.get("target_selector")
        delta = item.get("gain_delta")
        if mode not in ATTENTION_MODES:
            reasons.append(f"attention_mode_invalid:{index}")
            continue
        expected_selector = {
            "maintain_attention": "current_sa",
            "shift_attention": "best_recall_or_paradigm",
            "diversify_attention": "unresolved_frontier",
        }[str(mode)]
        if selector != expected_selector:
            reasons.append(f"attention_selector_invalid:{index}")
            continue
        if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not -1.0 <= float(delta) <= 1.0:
            reasons.append(f"attention_gain_out_of_bounds:{index}")
            continue
        adjustments.append(
            {
                "mode": str(mode),
                "target_selector": str(selector),
                "source_target_ref": str(item.get("source_target_ref"))[:512]
                if isinstance(item.get("source_target_ref"), str)
                else None,
                "gain_delta": round(float(delta) * CURRICULUM_SINGLE_TRIAL_LIMIT, 6),
                "source_refs": list(
                    _texts(item.get("source_refs"), "attention_source_refs", max_items=16, limit=1024)
                ),
            }
        )
    if not adjustments:
        reasons.append("no_explicit_supported_attention_adjustment")
    return tuple(adjustments), tuple(dict.fromkeys(reasons))


def _normalise_expression_curriculum_adjustment(
    value: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    raw = _mapping(value, "suggested_adjustment")
    source = raw.get("expression_patterns")
    reasons: list[str] = []
    patterns: list[Mapping[str, Any]] = []
    if isinstance(source, (str, bytes)) or not isinstance(source, Sequence):
        return (), ("expression_patterns_not_a_sequence",)
    for index, item in enumerate(source[:4]):
        if not isinstance(item, Mapping):
            reasons.append(f"expression_pattern_not_an_object:{index}")
            continue
        template = item.get("template")
        prefix = item.get("prefix")
        suffix = item.get("suffix")
        tone = item.get("tone", "neutral")
        if not isinstance(template, str) or template.count("{claim}") != 1:
            reasons.append(f"expression_claim_slot_invalid:{index}")
            continue
        if not isinstance(prefix, str) or not isinstance(suffix, str) or template != f"{prefix}{{claim}}{suffix}":
            reasons.append(f"expression_affix_mismatch:{index}")
            continue
        if prefix not in EXPRESSION_PREFIXES or suffix not in EXPRESSION_SUFFIXES:
            reasons.append(f"expression_affix_not_pragmatic:{index}")
            continue
        if tone not in EXPRESSION_TONES:
            reasons.append(f"expression_tone_invalid:{index}")
            continue
        patterns.append(
            {
                "template": template[:320],
                "prefix": prefix,
                "suffix": suffix,
                "tone": str(tone),
                "evidence_refs": list(
                    _texts(item.get("evidence_refs"), "expression_evidence_refs", max_items=16, limit=1024)
                ),
            }
        )
    if not patterns:
        reasons.append("no_explicit_supported_expression_pattern")
    return tuple(patterns), tuple(dict.fromkeys(reasons))


def _normalise_parameter_curriculum_adjustment(
    value: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    raw = _mapping(value, "suggested_adjustment")
    source = raw.get("parameter_adjustments")
    reasons: list[str] = []
    adjustments: list[Mapping[str, Any]] = []
    if isinstance(source, (str, bytes)) or not isinstance(source, Sequence):
        return (), ("parameter_adjustments_not_a_sequence",)
    for index, item in enumerate(source[:6]):
        if not isinstance(item, Mapping):
            reasons.append(f"parameter_adjustment_not_an_object:{index}")
            continue
        parameter = item.get("parameter")
        if not isinstance(parameter, str) or parameter not in ATTENTION_PARAMETER_BOUNDS:
            reasons.append(f"unsupported_parameter:{index}")
            continue
        delta = item.get("delta")
        if isinstance(delta, bool) or not isinstance(delta, (int, float)):
            reasons.append(f"parameter_delta_invalid:{index}")
            continue
        delta_value = float(delta)
        if abs(delta_value) <= 1e-12 or abs(delta_value) > PARAMETER_SINGLE_TRIAL_LIMIT + 1e-9:
            reasons.append(f"parameter_delta_out_of_bounds:{index}")
            continue
        expected_direction = item.get("expected_direction")
        if expected_direction not in {"increase", "decrease"}:
            reasons.append(f"parameter_direction_invalid:{index}")
            continue
        if (expected_direction == "increase" and delta_value <= 0.0) or (
            expected_direction == "decrease" and delta_value >= 0.0
        ):
            reasons.append(f"parameter_direction_delta_mismatch:{index}")
            continue
        source_refs = _texts(item.get("source_refs"), "parameter_source_refs", max_items=16, limit=1024)
        if not source_refs:
            reasons.append(f"parameter_source_refs_missing:{index}")
            continue
        adjustments.append(
            {
                "parameter": parameter,
                "delta": round(delta_value, 6),
                "expected_direction": str(expected_direction),
                "source_refs": list(source_refs),
                "rationale": _optional_text(item.get("rationale"), "parameter_rationale", limit=1200),
                "bounds": list(ATTENTION_PARAMETER_BOUNDS[parameter]),
            }
        )
    if not adjustments:
        reasons.append("no_explicit_supported_parameter_adjustment")
    return tuple(adjustments), tuple(dict.fromkeys(reasons))


def _normalise_curriculum_effect(
    capability: str,
    value: Mapping[str, Any],
) -> tuple[Mapping[str, Any], tuple[str, ...]]:
    if capability == CURRICULUM_TARGET_CAPABILITY:
        adjustments, reasons = _normalise_action_curriculum_adjustment(value)
        return {"action_adjustments": adjustments}, reasons
    if capability == CURRICULUM_RECALL_CAPABILITY:
        adjustments, reasons = _normalise_recall_curriculum_adjustment(value)
        return {"memory_gain_adjustments": adjustments}, reasons
    if capability == CURRICULUM_APPRAISAL_CAPABILITY:
        effects, reasons = _normalise_appraisal_curriculum_adjustment(value)
        return {"appraisal_effects": list(effects)}, reasons
    if capability == CURRICULUM_PREDICTION_CAPABILITY:
        hypotheses, reasons = _normalise_prediction_curriculum_adjustment(value)
        return {"prediction_hypotheses": list(hypotheses)}, reasons
    if capability == CURRICULUM_THOUGHT_CAPABILITY:
        scaffolds, reasons = _normalise_thought_curriculum_adjustment(value)
        return {"thought_scaffolds": list(scaffolds)}, reasons
    if capability == CURRICULUM_PARADIGM_CAPABILITY:
        patterns, reasons = _normalise_paradigm_curriculum_adjustment(value)
        return {"paradigm_patterns": list(patterns)}, reasons
    if capability == CURRICULUM_ATTENTION_CAPABILITY:
        adjustments, reasons = _normalise_attention_curriculum_adjustment(value)
        return {"attention_adjustments": list(adjustments)}, reasons
    if capability == CURRICULUM_EXPRESSION_CAPABILITY:
        patterns, reasons = _normalise_expression_curriculum_adjustment(value)
        return {"expression_patterns": list(patterns)}, reasons
    if capability == CURRICULUM_PARAMETER_CAPABILITY:
        adjustments, reasons = _normalise_parameter_curriculum_adjustment(value)
        return {"parameter_adjustments": list(adjustments)}, reasons
    return {}, ("target_capability_not_yet_supported",)


def _normalise_curriculum_adjustment(value: Mapping[str, Any]) -> tuple[dict[str, float], tuple[str, ...]]:
    """Backward-compatible action curriculum projection."""

    return _normalise_action_curriculum_adjustment(value)


def _curriculum_eligibility_reasons(
    curriculum: CurriculumCandidate,
) -> tuple[Mapping[str, Any], tuple[str, ...]]:
    effect, raw_reasons = _normalise_curriculum_effect(curriculum.target_capability, curriculum.suggested_adjustment)
    reasons = list(raw_reasons)
    if curriculum.target_capability not in CURRICULUM_SUPPORTED_CAPABILITIES:
        reasons.append("target_capability_not_yet_supported")
    if curriculum.source_kind not in {"llm_teacher", "user_feedback"}:
        reasons.append("curriculum_source_not_eligible_for_adoption")
    if curriculum.source_kind == "llm_teacher" and not curriculum.model_receipt_ref:
        reasons.append("teacher_model_receipt_missing")
    profile = curriculum.trigger_features.get("evidence_profile_keys")
    if isinstance(profile, (str, bytes)) or not isinstance(profile, Sequence) or not any(
        isinstance(item, str) and item.startswith("evidence_profile:") for item in profile
    ):
        reasons.append("evidence_profile_not_mappable")
    source_reasons = curriculum.extra.get("eligibility_reasons")
    if isinstance(source_reasons, Sequence) and not isinstance(source_reasons, (str, bytes)):
        reasons.extend(str(item) for item in source_reasons if isinstance(item, str) and item)
    if curriculum.target_capability == CURRICULUM_RECALL_CAPABILITY:
        source_memory_refs = curriculum.trigger_features.get("source_memory_refs")
        allowed_refs = {
            str(item) for item in source_memory_refs
            if isinstance(item, str)
        } if isinstance(source_memory_refs, Sequence) and not isinstance(source_memory_refs, (str, bytes)) else set()
        for memory_ref in effect.get("memory_gain_adjustments", {}):
            if memory_ref not in allowed_refs:
                reasons.append(f"memory_ref_not_in_source_recall:{memory_ref[:160]}")
    if curriculum.target_capability in {
        CURRICULUM_PARADIGM_CAPABILITY,
        CURRICULUM_ATTENTION_CAPABILITY,
        CURRICULUM_EXPRESSION_CAPABILITY,
        CURRICULUM_PARAMETER_CAPABILITY,
    }:
        raw_source_refs = curriculum.trigger_features.get("source_input_refs")
        source_refs = {
            str(item)
            for item in raw_source_refs
            if isinstance(item, str) and item
        } if isinstance(raw_source_refs, Sequence) and not isinstance(raw_source_refs, (str, bytes)) else set()
        effect_key = {
            CURRICULUM_PARADIGM_CAPABILITY: "paradigm_patterns",
            CURRICULUM_ATTENTION_CAPABILITY: "attention_adjustments",
            CURRICULUM_EXPRESSION_CAPABILITY: "expression_patterns",
            CURRICULUM_PARAMETER_CAPABILITY: "parameter_adjustments",
        }[curriculum.target_capability]
        for item in effect.get(effect_key, ()):
            if not isinstance(item, Mapping):
                continue
            for ref_name in ("evidence_refs", "source_refs"):
                for ref in item.get(ref_name, ()):
                    if isinstance(ref, str) and ref not in source_refs:
                        reasons.append(f"effect_ref_not_in_source_input:{ref[:160]}")
            source_target = item.get("source_target_ref")
            if isinstance(source_target, str) and source_target and source_target not in source_refs:
                reasons.append(f"effect_target_not_in_source_input:{source_target[:160]}")
    return dict(effect), tuple(dict.fromkeys(reasons))


def _activity_learning_features(activity: ProjectActivity) -> tuple[str, ...]:
    """Return one content-agnostic evidence profile for safe generalisation.

    Atomic booleans would make a lesson for one partial/clean activity leak to
    every unrelated activity which merely shares ``has_conflicts=False``.
    Grouping the generic axes keeps transfer across new wording and event kinds
    while requiring the same evidence shape.  Future learners may add broader
    features only after independent outcomes justify them.
    """

    conflicts = activity.extra.get("conflicts")
    has_conflicts = isinstance(conflicts, Sequence) and not isinstance(conflicts, (str, bytes)) and bool(conflicts)
    return (
        "evidence_profile:"
        f"completeness={activity.completeness}|"
        f"unknown={int(bool(activity.observed_unknown))}|"
        f"conflicts={int(has_conflicts)}|"
        f"remaining={int(bool(activity.observed_remaining))}|"
        f"next={int(bool(activity.observed_next_action))}",
    )


def _activity_structure_profile(activity: ProjectActivity) -> dict[str, Any]:
    conflicts = activity.extra.get("conflicts")
    return {
        "source_completeness": activity.completeness,
        "has_open_items": bool(activity.observed_remaining),
        "has_unknowns": bool(activity.observed_unknown),
        "has_conflicts": bool(
            isinstance(conflicts, Sequence)
            and not isinstance(conflicts, (str, bytes))
            and conflicts
        ),
        "has_next_action": bool(activity.observed_next_action),
    }


class ProjectLearningLedger:
    """A small, feature-conditioned and idempotent local learning ledger."""

    def __init__(
        self,
        path: str | Path,
        *,
        maturity_policy: CurriculumMaturityPolicy | None = None,
    ) -> None:
        self.path = Path(path)
        self.maturity_policy = maturity_policy or DEFAULT_CURRICULUM_MATURITY_POLICY
        # Compatibility aliases are opt-in and explicit.  They are only used
        # for read projections (writes always carry the canonical project id),
        # so a historical client label cannot create a second learning stream.
        self._project_aliases: dict[str, str] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def set_project_alias(self, alias: str, canonical_project_id: str) -> None:
        """Register a narrowly scoped legacy project label for projections."""

        alias_value = str(alias).strip()
        canonical_value = str(canonical_project_id).strip()
        if alias_value and canonical_value and alias_value != canonical_value:
            self._project_aliases[alias_value] = canonical_value

    def _canonical_project_id(self, project_id: str) -> str:
        value = str(project_id).strip()
        return self._project_aliases.get(value, value)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS feedback_lessons (
                    lesson_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    feedback_ref TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS action_preferences (
                    project_id TEXT NOT NULL,
                    feature_key TEXT NOT NULL,
                    action_kind TEXT NOT NULL,
                    value REAL NOT NULL,
                    sample_count INTEGER NOT NULL,
                    last_lesson_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, feature_key, action_kind)
                );
                CREATE TABLE IF NOT EXISTS curricula (
                    curriculum_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    target_capability TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_curricula_project_status
                    ON curricula(project_id, status, created_at);
                CREATE TABLE IF NOT EXISTS curriculum_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    curriculum_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    activity_episode_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, activity_episode_ref)
                );
                CREATE INDEX IF NOT EXISTS idx_curriculum_attempts_curriculum
                    ON curriculum_attempts(curriculum_id, created_at);
                CREATE TABLE IF NOT EXISTS curriculum_outcomes (
                    outcome_id TEXT PRIMARY KEY,
                    curriculum_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    episode_ref TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    resulting_status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_curriculum_outcomes_curriculum
                    ON curriculum_outcomes(curriculum_id, created_at);
                CREATE TABLE IF NOT EXISTS curriculum_attempts_v2 (
                    attempt_id TEXT PRIMARY KEY,
                    curriculum_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    target_capability TEXT NOT NULL,
                    activity_episode_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, activity_episode_ref, target_capability)
                );
                CREATE INDEX IF NOT EXISTS idx_curriculum_attempts_v2_curriculum
                    ON curriculum_attempts_v2(curriculum_id, created_at);
                CREATE TABLE IF NOT EXISTS project_memory_events (
                    event_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    activity_id TEXT NOT NULL,
                    privacy_scope TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, activity_id)
                );
                CREATE INDEX IF NOT EXISTS idx_project_memory_recent
                    ON project_memory_events(project_id, created_at);
                CREATE TABLE IF NOT EXISTS capability_opportunities (
                    opportunity_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    target_capability TEXT NOT NULL,
                    activity_episode_ref TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    teacher_requested INTEGER,
                    sampling_state TEXT,
                    sampling_rate REAL,
                    sampling_period INTEGER,
                    sampling_reason TEXT,
                    UNIQUE(project_id, target_capability, activity_episode_ref),
                    UNIQUE(project_id, target_capability, ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_capability_opportunities
                    ON capability_opportunities(project_id, target_capability, ordinal);
                """
            )
            opportunity_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(capability_opportunities)").fetchall()
            }
            for column, declaration in (
                ("teacher_requested", "INTEGER"),
                ("sampling_state", "TEXT"),
                ("sampling_rate", "REAL"),
                ("sampling_period", "INTEGER"),
                ("sampling_reason", "TEXT"),
            ):
                if column not in opportunity_columns:
                    connection.execute(
                        f"ALTER TABLE capability_opportunities ADD COLUMN {column} {declaration}"
                    )
            connection.commit()

    @staticmethod
    def _loads(raw: str) -> dict[str, Any]:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ContractError("learning_payload_must_be_object")
        return parsed

    def get_lesson(self, lesson_id: str) -> FeedbackLesson | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM feedback_lessons WHERE lesson_id = ? OR feedback_ref = ? LIMIT 1",
                (lesson_id, lesson_id),
            ).fetchone()
        return FeedbackLesson.from_dict(self._loads(str(row["payload_json"]))) if row else None

    def stage_curriculum(self, curriculum: CurriculumCandidate) -> CurriculumCandidate:
        """Persist one bounded proposal without installing it as a rule."""

        if not isinstance(curriculum, CurriculumCandidate):
            raise ContractError("learning_ledger_requires_curriculum_candidate")
        now = utc_now()
        payload = json.dumps(curriculum.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT project_id, target_capability, source_ref, status, payload_json FROM curricula WHERE curriculum_id = ?",
                    (curriculum.curriculum_id,),
                ).fetchone()
                if row is not None:
                    if (
                        str(row["project_id"]) != curriculum.project_id
                        or str(row["target_capability"]) != curriculum.target_capability
                        or str(row["source_ref"]) != curriculum.source_ref
                        or str(row["payload_json"]) != payload
                    ):
                        raise ContractError("curriculum_identity_reused_with_different_payload")
                    connection.commit()
                    return curriculum.with_status(str(row["status"]))
                connection.execute(
                    """INSERT INTO curricula
                    (curriculum_id, project_id, target_capability, source_ref, status,
                     payload_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        curriculum.curriculum_id,
                        curriculum.project_id,
                        curriculum.target_capability,
                        curriculum.source_ref,
                        curriculum.status,
                        payload,
                        curriculum.created_at,
                        now,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return curriculum

    def get_curriculum(self, curriculum_id: str) -> CurriculumCandidate | None:
        _text(curriculum_id, "curriculum_id", limit=512)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT status, payload_json FROM curricula WHERE curriculum_id = ? LIMIT 1",
                (curriculum_id,),
            ).fetchone()
        if row is None:
            return None
        return CurriculumCandidate.from_dict(self._loads(str(row["payload_json"]))).with_status(str(row["status"]))

    def remember_activity(self, activity: ProjectActivity) -> EventEnvelope:
        """Append one canonical source activity to the bounded shared project memory."""

        if not isinstance(activity, ProjectActivity):
            raise ContractError("project_memory_requires_activity")
        episode_id = _stable_id("project_memory_episode", activity.project_id, activity.activity_id)
        event = activity.as_event(
            runtime_id=f"ap-vibe-runtime:{activity.project_id}",
            organism_id=f"ap-vibe-organism:{activity.project_id}",
            episode_id=episode_id,
        )
        payload = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                prior = connection.execute(
                    "SELECT payload_json FROM project_memory_events WHERE project_id = ? AND activity_id = ?",
                    (activity.project_id, activity.activity_id),
                ).fetchone()
                if prior is not None:
                    existing = EventEnvelope.from_dict(self._loads(str(prior["payload_json"])))
                    if existing.to_dict() != event.to_dict():
                        # Observation time is transport-level; all other bytes
                        # must remain stable for a repeated activity identity.
                        left = existing.to_dict()
                        right = event.to_dict()
                        left.pop("observed_at", None)
                        right.pop("observed_at", None)
                        if left != right:
                            raise ContractError("project_memory_activity_identity_conflict")
                    connection.commit()
                    return existing
                connection.execute(
                    """INSERT INTO project_memory_events
                    (event_id, project_id, activity_id, privacy_scope, occurred_at,
                     payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_id,
                        activity.project_id,
                        activity.activity_id,
                        activity.privacy_scope,
                        activity.occurred_at,
                        payload,
                        utc_now(),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return event

    def memory_events(
        self,
        project_id: str,
        *,
        exclude_activity_id: str | None = None,
        excluded_activity_ids: Sequence[str] = (),
        privacy_scope: str = "project",
        limit: int = 32,
    ) -> tuple[EventEnvelope, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
            raise ContractError("project_memory_limit_out_of_bounds")
        if isinstance(excluded_activity_ids, (str, bytes)) or not isinstance(excluded_activity_ids, Sequence):
            raise ContractError("project_memory_excluded_activity_ids_must_be_sequence")
        excluded = {
            _text(item, "excluded_activity_id", limit=512)
            for item in excluded_activity_ids
        }
        if len(excluded) > 128:
            raise ContractError("project_memory_excluded_activity_ids_out_of_bounds")
        if exclude_activity_id is not None:
            excluded.add(_text(exclude_activity_id, "exclude_activity_id", limit=512))
        predicates = ["project_id = ?"]
        parameters: list[Any] = [project_id]
        if excluded:
            predicates.append(
                f"activity_id NOT IN ({','.join('?' for _ in excluded)})"
            )
            parameters.extend(sorted(excluded))
        parameters.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT activity_id, privacy_scope, payload_json FROM project_memory_events
                WHERE {' AND '.join(predicates)} ORDER BY created_at DESC LIMIT ?""",
                tuple(parameters),
            ).fetchall()
        output: list[EventEnvelope] = []
        for row in reversed(rows):
            stored_scope = str(row["privacy_scope"])
            if stored_scope != privacy_scope and stored_scope != "project":
                continue
            output.append(EventEnvelope.from_dict(self._loads(str(row["payload_json"]))))
        return tuple(output[-limit:])

    def memory_observations(self, project_id: str, *, limit: int = 128) -> tuple[dict[str, Any], ...]:
        """Return bounded source observations for product inspection/export.

        This projection does not change recall eligibility or scoring.  It is
        intentionally separate from ``memory_events`` so an administrative UI
        cannot silently acquire cognitive authority.
        """

        _text(project_id, "project_id", limit=512)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2_000:
            raise ContractError("project_memory_projection_limit_out_of_bounds")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT event_id, activity_id, privacy_scope, occurred_at,
                payload_json, created_at FROM project_memory_events
                WHERE project_id = ? ORDER BY created_at DESC LIMIT ?""",
                (project_id, limit),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            event = EventEnvelope.from_dict(self._loads(str(row["payload_json"])))
            activity = event.payload_inline if isinstance(event.payload_inline, Mapping) else {}
            output.append(
                {
                    "event_id": str(row["event_id"]),
                    "activity_id": str(row["activity_id"]),
                    "privacy_scope": str(row["privacy_scope"]),
                    "occurred_at": str(row["occurred_at"]),
                    "created_at": str(row["created_at"]),
                    "activity": dict(activity),
                    "source_ref": activity.get("source_ref") if isinstance(activity.get("source_ref"), str) else None,
                    "summary": activity.get("summary") if isinstance(activity.get("summary"), str) else "",
                    "completeness": activity.get("completeness") if isinstance(activity.get("completeness"), str) else event.completeness,
                }
            )
        return tuple(output)

    def has_memory_activity(self, project_id: str, activity_id: str) -> bool:
        _text(project_id, "project_id", limit=512)
        _text(activity_id, "activity_id", limit=512)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM project_memory_events WHERE project_id = ? AND activity_id = ? LIMIT 1",
                (project_id, activity_id),
            ).fetchone()
        return row is not None

    def record_curriculum_outcome(self, outcome: CurriculumOutcome) -> CurriculumOutcome:
        """Append one causal outcome and move only its curriculum projection."""

        if not isinstance(outcome, CurriculumOutcome):
            raise ContractError("learning_ledger_requires_curriculum_outcome")
        payload = json.dumps(outcome.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                candidate = connection.execute(
                    "SELECT project_id, status FROM curricula WHERE curriculum_id = ?",
                    (outcome.curriculum_id,),
                ).fetchone()
                if candidate is None:
                    raise ContractError("curriculum_outcome_target_not_found")
                if str(candidate["project_id"]) != outcome.project_id:
                    raise ContractError("curriculum_outcome_project_mismatch")
                prior = connection.execute(
                    "SELECT payload_json FROM curriculum_outcomes WHERE outcome_id = ?",
                    (outcome.outcome_id,),
                ).fetchone()
                if prior is not None:
                    prior_outcome = CurriculumOutcome.from_dict(self._loads(str(prior["payload_json"])))
                    if prior_outcome != outcome:
                        raise ContractError("curriculum_outcome_identity_reused")
                    connection.commit()
                    return prior_outcome
                current_status = str(candidate["status"])
                if current_status in {"reteach", "retracted"} and outcome.resulting_status not in {"reteach", "retracted"}:
                    outcome = replace(
                        outcome,
                        resulting_status=current_status,
                        extra={
                            **outcome.extra,
                            "transition_blocked_by_status": current_status,
                        },
                    )
                    payload = json.dumps(outcome.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                connection.execute(
                    """INSERT INTO curriculum_outcomes
                    (outcome_id, curriculum_id, project_id, episode_ref, outcome,
                     resulting_status, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        outcome.outcome_id,
                        outcome.curriculum_id,
                        outcome.project_id,
                        outcome.episode_ref,
                        outcome.outcome,
                        outcome.resulting_status,
                        payload,
                        outcome.created_at,
                    ),
                )
                connection.execute(
                    "UPDATE curricula SET status = ?, updated_at = ? WHERE curriculum_id = ?",
                    (outcome.resulting_status, outcome.created_at, outcome.curriculum_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return outcome

    @staticmethod
    def _curriculum_evidence_profile(curriculum: CurriculumCandidate) -> tuple[str, ...]:
        """Return the complete, content-agnostic scope of one course generation."""

        raw = curriculum.trigger_features.get("evidence_profile_keys", ())
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            return ()
        return tuple(
            sorted(
                dict.fromkeys(
                    str(item)
                    for item in raw
                    if isinstance(item, str) and item.startswith("evidence_profile:")
                )
            )
        )

    def activate_curriculum(
        self,
        activation: CurriculumOutcome,
    ) -> tuple[CurriculumOutcome, tuple[CurriculumOutcome, ...]]:
        """Activate one independently read-back course and close replaced reteach generations.

        Replacement is deliberately narrower than capability-wide supersession: project,
        capability, and the complete evidence profile must all match.  Historical
        attempts/counterexamples remain append-only; only their course projection becomes
        ``retracted`` after the replacement itself has physical result-back.
        """

        if not isinstance(activation, CurriculumOutcome):
            raise ContractError("learning_ledger_requires_curriculum_outcome")
        if activation.outcome != "active_trial" or activation.resulting_status != "active_trial":
            raise ContractError("curriculum_activation_requires_active_trial_outcome")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                replacement_row = connection.execute(
                    "SELECT project_id, target_capability, status, payload_json FROM curricula WHERE curriculum_id = ?",
                    (activation.curriculum_id,),
                ).fetchone()
                if replacement_row is None:
                    raise ContractError("curriculum_outcome_target_not_found")
                if str(replacement_row["project_id"]) != activation.project_id:
                    raise ContractError("curriculum_outcome_project_mismatch")
                replacement = CurriculumCandidate.from_dict(
                    self._loads(str(replacement_row["payload_json"]))
                ).with_status(str(replacement_row["status"]))
                profile = self._curriculum_evidence_profile(replacement)
                if not profile:
                    raise ContractError("curriculum_activation_evidence_profile_missing")

                prior_activation = connection.execute(
                    "SELECT payload_json FROM curriculum_outcomes WHERE outcome_id = ?",
                    (activation.outcome_id,),
                ).fetchone()
                if prior_activation is not None:
                    stored = CurriculumOutcome.from_dict(
                        self._loads(str(prior_activation["payload_json"]))
                    )
                    stored_input_extra = {
                        key: value
                        for key, value in stored.extra.items()
                        if key not in {"replacement_scope", "replaces_curriculum_ids"}
                    }
                    if replace(stored, extra=stored_input_extra) != activation:
                        raise ContractError("curriculum_outcome_identity_reused")
                    replacement_ids = tuple(
                        str(item)
                        for item in stored.extra.get("replaces_curriculum_ids", ())
                        if isinstance(item, str)
                    )
                    replacement_outcomes: list[CurriculumOutcome] = []
                    for curriculum_id in replacement_ids:
                        row = connection.execute(
                            """SELECT payload_json FROM curriculum_outcomes
                            WHERE outcome_id = ? LIMIT 1""",
                            (
                                _stable_id(
                                    "curriculum_outcome",
                                    curriculum_id,
                                    activation.curriculum_id,
                                    activation.outcome_id,
                                    "replaced",
                                ),
                            ),
                        ).fetchone()
                        if row is not None:
                            replacement_outcomes.append(
                                CurriculumOutcome.from_dict(self._loads(str(row["payload_json"])))
                            )
                    connection.commit()
                    return stored, tuple(replacement_outcomes)

                reteach_rows = connection.execute(
                    """SELECT curriculum_id, status, payload_json FROM curricula
                    WHERE project_id = ? AND target_capability = ? AND status = 'reteach'
                      AND curriculum_id <> ? ORDER BY created_at, curriculum_id""",
                    (activation.project_id, replacement.target_capability, activation.curriculum_id),
                ).fetchall()
                replaced: list[CurriculumCandidate] = []
                for row in reteach_rows:
                    candidate = CurriculumCandidate.from_dict(
                        self._loads(str(row["payload_json"]))
                    ).with_status(str(row["status"]))
                    if self._curriculum_evidence_profile(candidate) == profile:
                        replaced.append(candidate)

                activation = replace(
                    activation,
                    extra={
                        **activation.extra,
                        "replacement_scope": {
                            "project_id": activation.project_id,
                            "target_capability": replacement.target_capability,
                            "evidence_profile_keys": list(profile),
                        },
                        "replaces_curriculum_ids": [item.curriculum_id for item in replaced],
                    },
                )
                activation_payload = json.dumps(
                    activation.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                connection.execute(
                    """INSERT INTO curriculum_outcomes
                    (outcome_id, curriculum_id, project_id, episode_ref, outcome,
                     resulting_status, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        activation.outcome_id,
                        activation.curriculum_id,
                        activation.project_id,
                        activation.episode_ref,
                        activation.outcome,
                        activation.resulting_status,
                        activation_payload,
                        activation.created_at,
                    ),
                )
                connection.execute(
                    "UPDATE curricula SET status = 'active_trial', updated_at = ? WHERE curriculum_id = ?",
                    (activation.created_at, activation.curriculum_id),
                )

                replacement_outcomes: list[CurriculumOutcome] = []
                for prior in replaced:
                    replacement_outcome = CurriculumOutcome(
                        outcome_id=_stable_id(
                            "curriculum_outcome",
                            prior.curriculum_id,
                            activation.curriculum_id,
                            activation.outcome_id,
                            "replaced",
                        ),
                        curriculum_id=prior.curriculum_id,
                        project_id=activation.project_id,
                        outcome="replaced",
                        resulting_status="retracted",
                        source_ref=activation.source_ref,
                        episode_ref=activation.episode_ref,
                        evidence_refs=tuple(
                            dict.fromkeys(
                                (
                                    *activation.evidence_refs,
                                    activation.outcome_id,
                                    activation.curriculum_id,
                                )
                            )
                        ),
                        rationale="A later course for the same evidence profile completed AP adoption and readback",
                        created_at=activation.created_at,
                        extra={
                            "replacement_curriculum_id": activation.curriculum_id,
                            "replacement_activation_outcome_id": activation.outcome_id,
                            "replacement_scope": {
                                "project_id": activation.project_id,
                                "target_capability": replacement.target_capability,
                                "evidence_profile_keys": list(profile),
                            },
                            "preserved_counterexample_history": True,
                        },
                    )
                    payload = json.dumps(
                        replacement_outcome.to_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    connection.execute(
                        """INSERT INTO curriculum_outcomes
                        (outcome_id, curriculum_id, project_id, episode_ref, outcome,
                         resulting_status, payload_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            replacement_outcome.outcome_id,
                            replacement_outcome.curriculum_id,
                            replacement_outcome.project_id,
                            replacement_outcome.episode_ref,
                            replacement_outcome.outcome,
                            replacement_outcome.resulting_status,
                            payload,
                            replacement_outcome.created_at,
                        ),
                    )
                    connection.execute(
                        "UPDATE curricula SET status = 'retracted', updated_at = ? WHERE curriculum_id = ?",
                        (activation.created_at, prior.curriculum_id),
                    )
                    replacement_outcomes.append(replacement_outcome)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return activation, tuple(replacement_outcomes)

    def active_curriculum_trials(
        self,
        activity: ProjectActivity,
    ) -> tuple[dict[str, float], tuple[Mapping[str, Any], ...]]:
        """Return bounded trial deltas and lineage for one evidence profile."""

        features = _activity_learning_features(activity)
        feature_set = set(features)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT payload_json FROM curricula
                WHERE project_id = ? AND target_capability = ? AND status = 'active_trial'
                ORDER BY created_at DESC LIMIT ?""",
                (activity.project_id, CURRICULUM_TARGET_CAPABILITY, MAX_ACTIVE_CURRICULA_PER_EPISODE),
            ).fetchall()
        contributions: dict[str, float] = {}
        details: list[Mapping[str, Any]] = []
        for row in rows:
            candidate = CurriculumCandidate.from_dict(self._loads(str(row["payload_json"]))).with_status("active_trial")
            if candidate.extra.get("source_activity_ref") == activity.activity_id:
                # A replay of the teaching source is not a later opportunity.
                # This also prevents newly adopted curriculum from becoming
                # retroactive evidence for the episode that created it.
                continue
            profile_values = candidate.trigger_features.get("evidence_profile_keys", ())
            if isinstance(profile_values, (str, bytes)) or not isinstance(profile_values, Sequence):
                continue
            matching = tuple(str(item) for item in profile_values if isinstance(item, str) and item in feature_set)
            if not matching:
                continue
            adjustments, reasons = _normalise_curriculum_adjustment(candidate.suggested_adjustment)
            if reasons or not adjustments:
                continue
            for action, delta in adjustments.items():
                contributions[action] = round(
                    max(
                        -CURRICULUM_CAPABILITY_LIMIT,
                        min(CURRICULUM_CAPABILITY_LIMIT, contributions.get(action, 0.0) + delta),
                    ),
                    6,
                )
            details.append(
                {
                    "curriculum_id": candidate.curriculum_id,
                    "target_capability": candidate.target_capability,
                    "source_kind": candidate.source_kind,
                    "source_ref": candidate.source_ref,
                    "feature_keys": list(matching),
                    "action_adjustments": adjustments,
                    "confidence": candidate.confidence,
                    "status": candidate.status,
                }
            )
        return contributions, tuple(details)

    def active_cognitive_curriculum_trials(
        self,
        activity: ProjectActivity,
        memory_events: Sequence[EventEnvelope],
    ) -> Mapping[str, tuple[Mapping[str, Any], ...]]:
        """Resolve bounded cognitive trials against real local inputs."""

        features = set(_activity_learning_features(activity))
        memory_refs = {item.event_id for item in memory_events}
        cognitive_capabilities = (
            CURRICULUM_RECALL_CAPABILITY,
            CURRICULUM_APPRAISAL_CAPABILITY,
            CURRICULUM_PREDICTION_CAPABILITY,
            CURRICULUM_THOUGHT_CAPABILITY,
            CURRICULUM_PARADIGM_CAPABILITY,
            CURRICULUM_ATTENTION_CAPABILITY,
            CURRICULUM_EXPRESSION_CAPABILITY,
            CURRICULUM_PARAMETER_CAPABILITY,
        )
        placeholders = ",".join("?" for _ in cognitive_capabilities)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT payload_json FROM curricula WHERE project_id = ?
                AND target_capability IN ({placeholders}) AND status = 'active_trial'
                ORDER BY created_at DESC LIMIT ?""",
                (
                    activity.project_id,
                    *cognitive_capabilities,
                    MAX_ACTIVE_CURRICULA_PER_EPISODE,
                ),
            ).fetchall()
        grouped: dict[str, list[Mapping[str, Any]]] = {
            CURRICULUM_RECALL_CAPABILITY: [],
            CURRICULUM_APPRAISAL_CAPABILITY: [],
            CURRICULUM_PREDICTION_CAPABILITY: [],
            CURRICULUM_THOUGHT_CAPABILITY: [],
            CURRICULUM_PARADIGM_CAPABILITY: [],
            CURRICULUM_ATTENTION_CAPABILITY: [],
            CURRICULUM_EXPRESSION_CAPABILITY: [],
            CURRICULUM_PARAMETER_CAPABILITY: [],
        }
        for row in rows:
            candidate = CurriculumCandidate.from_dict(self._loads(str(row["payload_json"]))).with_status("active_trial")
            if candidate.extra.get("source_activity_ref") == activity.activity_id:
                continue
            profiles = candidate.trigger_features.get("evidence_profile_keys")
            if isinstance(profiles, (str, bytes)) or not isinstance(profiles, Sequence) or not features.intersection(
                str(item) for item in profiles if isinstance(item, str)
            ):
                continue
            effect, reasons = _curriculum_eligibility_reasons(candidate)
            if reasons:
                continue
            if candidate.target_capability == CURRICULUM_RECALL_CAPABILITY:
                raw_adjustments = effect.get("memory_gain_adjustments")
                if not isinstance(raw_adjustments, Mapping):
                    continue
                resolvable = {
                    str(ref): float(delta)
                    for ref, delta in raw_adjustments.items()
                    if ref in memory_refs and isinstance(delta, (int, float)) and not isinstance(delta, bool)
                }
                if not resolvable:
                    continue
                grouped[candidate.target_capability].append(
                    {
                        "curriculum_id": candidate.curriculum_id,
                        "target_capability": candidate.target_capability,
                        "source_ref": candidate.source_ref,
                        "feature_keys": list(features),
                        "memory_gain_adjustments": resolvable,
                    }
                )
            elif candidate.target_capability == CURRICULUM_APPRAISAL_CAPABILITY:
                effects = effect.get("appraisal_effects")
                if isinstance(effects, Sequence) and not isinstance(effects, (str, bytes)) and effects:
                    grouped[candidate.target_capability].append(
                        {
                            "curriculum_id": candidate.curriculum_id,
                            "target_capability": candidate.target_capability,
                            "source_ref": candidate.source_ref,
                            "feature_keys": list(features),
                            "appraisal_effects": [dict(item) for item in effects if isinstance(item, Mapping)],
                        }
                    )
            elif candidate.target_capability == CURRICULUM_PREDICTION_CAPABILITY:
                hypotheses = effect.get("prediction_hypotheses")
                if isinstance(hypotheses, Sequence) and not isinstance(hypotheses, (str, bytes)) and hypotheses:
                    grouped[candidate.target_capability].append(
                        {
                            "curriculum_id": candidate.curriculum_id,
                            "target_capability": candidate.target_capability,
                            "source_ref": candidate.source_ref,
                            "feature_keys": list(features),
                            "prediction_hypotheses": [dict(item) for item in hypotheses if isinstance(item, Mapping)],
                        }
                    )
            elif candidate.target_capability == CURRICULUM_THOUGHT_CAPABILITY:
                scaffolds = effect.get("thought_scaffolds")
                if isinstance(scaffolds, Sequence) and not isinstance(scaffolds, (str, bytes)) and scaffolds:
                    grouped[candidate.target_capability].append(
                        {
                            "curriculum_id": candidate.curriculum_id,
                            "target_capability": candidate.target_capability,
                            "source_ref": candidate.source_ref,
                            "feature_keys": list(features),
                            "thought_scaffolds": [dict(item) for item in scaffolds if isinstance(item, Mapping)],
                        }
                    )
            elif candidate.target_capability == CURRICULUM_PARADIGM_CAPABILITY:
                patterns = effect.get("paradigm_patterns")
                if isinstance(patterns, Sequence) and not isinstance(patterns, (str, bytes)) and patterns:
                    grouped[candidate.target_capability].append(
                        {
                            "curriculum_id": candidate.curriculum_id,
                            "target_capability": candidate.target_capability,
                            "source_ref": candidate.source_ref,
                            "feature_keys": list(features),
                            "confidence": candidate.confidence,
                            "uncertainty": candidate.uncertainty,
                            "paradigm_patterns": [dict(item) for item in patterns if isinstance(item, Mapping)],
                        }
                    )
            elif candidate.target_capability == CURRICULUM_ATTENTION_CAPABILITY:
                adjustments = effect.get("attention_adjustments")
                if isinstance(adjustments, Sequence) and not isinstance(adjustments, (str, bytes)) and adjustments:
                    grouped[candidate.target_capability].append(
                        {
                            "curriculum_id": candidate.curriculum_id,
                            "target_capability": candidate.target_capability,
                            "source_ref": candidate.source_ref,
                            "feature_keys": list(features),
                            "confidence": candidate.confidence,
                            "uncertainty": candidate.uncertainty,
                            "attention_adjustments": [dict(item) for item in adjustments if isinstance(item, Mapping)],
                        }
                    )
            elif candidate.target_capability == CURRICULUM_EXPRESSION_CAPABILITY:
                patterns = effect.get("expression_patterns")
                if isinstance(patterns, Sequence) and not isinstance(patterns, (str, bytes)) and patterns:
                    grouped[candidate.target_capability].append(
                        {
                            "curriculum_id": candidate.curriculum_id,
                            "target_capability": candidate.target_capability,
                            "source_ref": candidate.source_ref,
                            "feature_keys": list(features),
                            "confidence": candidate.confidence,
                            "uncertainty": candidate.uncertainty,
                            "expression_patterns": [dict(item) for item in patterns if isinstance(item, Mapping)],
                        }
                    )
            elif candidate.target_capability == CURRICULUM_PARAMETER_CAPABILITY:
                adjustments = effect.get("parameter_adjustments")
                if isinstance(adjustments, Sequence) and not isinstance(adjustments, (str, bytes)) and adjustments:
                    grouped[candidate.target_capability].append(
                        {
                            "curriculum_id": candidate.curriculum_id,
                            "target_capability": candidate.target_capability,
                            "source_ref": candidate.source_ref,
                            "feature_keys": list(features),
                            "confidence": candidate.confidence,
                            "uncertainty": candidate.uncertainty,
                            "parameter_adjustments": [dict(item) for item in adjustments if isinstance(item, Mapping)],
                        }
                    )
        legacy_keys = {
            CURRICULUM_RECALL_CAPABILITY,
            CURRICULUM_APPRAISAL_CAPABILITY,
            CURRICULUM_PREDICTION_CAPABILITY,
            CURRICULUM_THOUGHT_CAPABILITY,
        }
        # Preserve the original four empty projections for older consumers.
        # New E3 capabilities become visible only when a real active trial is
        # applicable; an empty key must not imply that a capability ran.
        return {
            key: tuple(value)
            for key, value in grouped.items()
            if key in legacy_keys or value
        }

    @staticmethod
    def attention_policy_from_trials(
        cognitive_trials: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> AttentionPolicy:
        """Build one immutable attention policy for one later episode.

        No class constant, file, environment value, or BirthProfile is
        changed.  The first applicable trial is sufficient for E4's bounded
        causal slice; later parameter aggregation remains capped for forward
        compatibility.
        """

        values = dict(ATTENTION_PARAMETER_DEFAULTS)
        resolved: list[dict[str, Any]] = []
        totals: dict[str, float] = {}
        raw_trials = cognitive_trials.get(CURRICULUM_PARAMETER_CAPABILITY, ())
        if isinstance(raw_trials, Sequence) and not isinstance(raw_trials, (str, bytes)):
            for trial in raw_trials[:MAX_ACTIVE_CURRICULA_PER_EPISODE]:
                if not isinstance(trial, Mapping):
                    continue
                adjustments = trial.get("parameter_adjustments")
                if isinstance(adjustments, (str, bytes)) or not isinstance(adjustments, Sequence):
                    continue
                for item in adjustments[:1]:
                    if not isinstance(item, Mapping):
                        continue
                    parameter = item.get("parameter")
                    delta = item.get("delta")
                    if (
                        not isinstance(parameter, str)
                        or parameter not in ATTENTION_PARAMETER_BOUNDS
                        or isinstance(delta, bool)
                        or not isinstance(delta, (int, float))
                    ):
                        continue
                    bounded_delta = max(
                        -PARAMETER_AGGREGATE_LIMIT,
                        min(PARAMETER_AGGREGATE_LIMIT, totals.get(parameter, 0.0) + float(delta)),
                    )
                    totals[parameter] = bounded_delta
                    low, high = ATTENTION_PARAMETER_BOUNDS[parameter]
                    effective = round(max(low, min(high, ATTENTION_PARAMETER_DEFAULTS[parameter] + bounded_delta)), 6)
                    values[parameter] = effective
                    curriculum_id = trial.get("curriculum_id")
                    resolved.append(
                        {
                            "curriculum_id": str(curriculum_id) if isinstance(curriculum_id, str) else "",
                            "parameter": parameter,
                            "default": ATTENTION_PARAMETER_DEFAULTS[parameter],
                            "before": ATTENTION_PARAMETER_DEFAULTS[parameter],
                            "delta": round(bounded_delta, 6),
                            "effective": effective,
                            "bounds": [low, high],
                            "feature_keys": list(trial.get("feature_keys", ())),
                            "source_ref": trial.get("source_ref"),
                        }
                    )
                    # E4 intentionally consumes at most one parameter trial.
                    break
                if resolved:
                    break
        return AttentionPolicy(
            novelty_weight=values["attention.novelty_weight"],
            mismatch_weight=values["attention.mismatch_weight"],
            recall_weight=values["attention.recall_weight"],
            open_goal_weight=values["attention.open_goal_weight"],
            paradigm_weight=values["attention.paradigm_weight"],
            fatigue_inhibition_weight=values["attention.fatigue_inhibition_weight"],
            parameter_version="attention-policy.v1+trial" if resolved else "attention-policy.v1",
            parameter_trials=tuple(resolved),
        )

    def record_curriculum_attempt(self, attempt: CurriculumAttempt) -> CurriculumAttempt:
        """Record at most one attributed opportunity per episode and capability."""

        if not isinstance(attempt, CurriculumAttempt):
            raise ContractError("learning_ledger_requires_curriculum_attempt")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                prior = connection.execute(
                    """SELECT payload_json FROM curriculum_attempts_v2
                    WHERE project_id = ? AND activity_episode_ref = ? AND target_capability = ?""",
                    (attempt.project_id, attempt.activity_episode_ref, attempt.target_capability),
                ).fetchone()
                if prior is not None:
                    connection.commit()
                    return CurriculumAttempt.from_dict(self._loads(str(prior["payload_json"])))
                opportunity = connection.execute(
                    """SELECT ordinal FROM capability_opportunities
                    WHERE project_id = ? AND target_capability = ?
                      AND activity_episode_ref = ? LIMIT 1""",
                    (attempt.project_id, attempt.target_capability, attempt.activity_episode_ref),
                ).fetchone()
                if opportunity is None:
                    maximum = connection.execute(
                        """SELECT MAX(ordinal) AS n FROM capability_opportunities
                        WHERE project_id = ? AND target_capability = ?""",
                        (attempt.project_id, attempt.target_capability),
                    ).fetchone()
                    opportunity_ordinal = int(maximum["n"] or 0) + 1
                    connection.execute(
                        """INSERT INTO capability_opportunities
                        (opportunity_id, project_id, target_capability,
                         activity_episode_ref, ordinal, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            _stable_id(
                                "capability_opportunity",
                                attempt.project_id,
                                attempt.target_capability,
                                attempt.activity_episode_ref,
                            ),
                            attempt.project_id,
                            attempt.target_capability,
                            attempt.activity_episode_ref,
                            opportunity_ordinal,
                            attempt.created_at,
                        ),
                    )
                else:
                    opportunity_ordinal = int(opportunity["ordinal"])
                attempt = replace(
                    attempt,
                    extra={**attempt.extra, "opportunity_ordinal": opportunity_ordinal},
                )
                payload = json.dumps(
                    attempt.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                connection.execute(
                    """INSERT INTO curriculum_attempts_v2
                    (attempt_id, curriculum_id, project_id, target_capability,
                     activity_episode_ref, status, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        attempt.attempt_id,
                        attempt.curriculum_id,
                        attempt.project_id,
                        attempt.target_capability,
                        attempt.activity_episode_ref,
                        attempt.status,
                        payload,
                        attempt.created_at,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return attempt

    def _recent_capability_counts(
        self,
        project_id: str,
        target_capability: str,
    ) -> Mapping[str, Any]:
        """Aggregate the current training generations over recent opportunities.

        Attempts and outcomes from a retracted generation remain queryable in
        the audit projection, but they must not keep its replacement in
        perpetual reteach.
        """

        limit = self.maturity_policy.window_size
        with closing(self._connect()) as connection:
            generation_rows = connection.execute(
                """SELECT curriculum_id FROM curricula
                WHERE project_id = ? AND target_capability = ?
                  AND status IN ('active_trial', 'reteach')""",
                (project_id, target_capability),
            ).fetchall()
            current_curriculum_ids = tuple(str(row["curriculum_id"]) for row in generation_rows)
            if not current_curriculum_ids:
                return {
                    "opportunities": 0,
                    "attempted": 0,
                    "withheld": 0,
                    "unknown_attempts": 0,
                    "success": 0,
                    "counterexample": 0,
                    "known_outcomes": 0,
                    "unknown": 0,
                    "latest_ordinal": 0,
                    "window_size": limit,
                    "current_curriculum_ids": [],
                }
            curriculum_placeholders = ",".join("?" for _ in current_curriculum_ids)
            attempt_rows = connection.execute(
                f"""SELECT a.payload_json, o.ordinal FROM curriculum_attempts_v2 a
                JOIN capability_opportunities o
                  ON o.project_id = a.project_id
                 AND o.target_capability = a.target_capability
                 AND o.activity_episode_ref = a.activity_episode_ref
                WHERE a.project_id = ? AND a.target_capability = ?
                  AND a.curriculum_id IN ({curriculum_placeholders})
                ORDER BY o.ordinal DESC LIMIT ?""",
                (project_id, target_capability, *current_curriculum_ids, limit),
            ).fetchall()
            attempts = [
                CurriculumAttempt.from_dict(self._loads(str(row["payload_json"])))
                for row in attempt_rows
            ]
            attempt_refs = tuple(item.attempt_id for item in attempts)
            outcome_rows: Sequence[sqlite3.Row] = ()
            if attempt_refs:
                placeholders = ",".join("?" for _ in attempt_refs)
                outcome_rows = connection.execute(
                    f"""SELECT outcome_id, created_at, payload_json FROM curriculum_outcomes
                    WHERE project_id = ? AND json_extract(payload_json, '$.attempt_ref')
                    IN ({placeholders}) ORDER BY created_at DESC, outcome_id DESC""",
                    (project_id, *attempt_refs),
                ).fetchall()
        latest_by_attempt: dict[str, CurriculumOutcome] = {}
        for row in outcome_rows:
            outcome = CurriculumOutcome.from_dict(self._loads(str(row["payload_json"])))
            if outcome.attempt_ref and outcome.attempt_ref not in latest_by_attempt:
                latest_by_attempt[outcome.attempt_ref] = outcome
        success = sum(1 for item in latest_by_attempt.values() if item.outcome == "success")
        counterexample = sum(1 for item in latest_by_attempt.values() if item.outcome == "counterexample")
        status_counts: dict[str, int] = {}
        for item in attempts:
            status_counts[item.status] = status_counts.get(item.status, 0) + 1
        return {
            "opportunities": len(attempts),
            "attempted": int(status_counts.get("attempted", 0)),
            "withheld": int(status_counts.get("withheld", 0)),
            "unknown_attempts": int(status_counts.get("unknown", 0)),
            "success": success,
            "counterexample": counterexample,
            "known_outcomes": success + counterexample,
            "unknown": max(0, len(attempts) - success - counterexample),
            "latest_ordinal": int(attempt_rows[0]["ordinal"]) if attempt_rows else 0,
            "window_size": limit,
            "current_curriculum_ids": list(current_curriculum_ids),
        }

    def attempt_for_episode(
        self,
        project_id: str,
        episode_ref: str,
        target_capability: str = CURRICULUM_TARGET_CAPABILITY,
    ) -> CurriculumAttempt | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT payload_json FROM curriculum_attempts_v2 WHERE project_id = ?
                AND activity_episode_ref = ? AND target_capability = ? LIMIT 1""",
                (project_id, episode_ref, target_capability),
            ).fetchone()
            if row is None and target_capability == CURRICULUM_TARGET_CAPABILITY:
                row = connection.execute(
                "SELECT payload_json FROM curriculum_attempts WHERE project_id = ? AND activity_episode_ref = ? LIMIT 1",
                (project_id, episode_ref),
                ).fetchone()
        return CurriculumAttempt.from_dict(self._loads(str(row["payload_json"]))) if row else None

    def attempts_for_episode(self, project_id: str, episode_ref: str) -> tuple[CurriculumAttempt, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT payload_json FROM curriculum_attempts_v2 WHERE project_id = ?
                AND activity_episode_ref = ? ORDER BY target_capability""",
                (project_id, episode_ref),
            ).fetchall()
            legacy = connection.execute(
                "SELECT payload_json FROM curriculum_attempts WHERE project_id = ? AND activity_episode_ref = ? LIMIT 1",
                (project_id, episode_ref),
            ).fetchone()
        attempts = [CurriculumAttempt.from_dict(self._loads(str(row["payload_json"]))) for row in rows]
        if legacy is not None:
            old = CurriculumAttempt.from_dict(self._loads(str(legacy["payload_json"])))
            if not any(item.target_capability == old.target_capability for item in attempts):
                attempts.append(old)
        return tuple(attempts)

    @staticmethod
    def _sampling_state(
        *,
        status_reteach: bool,
        opportunities: int,
        successes: int,
        counterexamples: int,
        policy: CurriculumMaturityPolicy,
    ) -> tuple[str, float, str, float | None]:
        known = successes + counterexamples
        agreement = round(successes / known, 6) if known else None
        if status_reteach or counterexamples >= policy.reteach_counterexamples:
            return "reteach", policy.teaching_rate, "related_counterexample_requires_reteach", agreement
        if opportunities < policy.minimum_opportunities:
            state = "insufficient_evidence" if opportunities else "teaching"
            return state, policy.teaching_rate, "minimum_independent_opportunities_not_met", agreement
        if agreement is not None and agreement >= policy.audit_agreement:
            return "low_frequency_audit", policy.audit_rate, "agreement_threshold_met_for_local_audit", agreement
        return "trial", policy.trial_rate, "local_trial_requires_periodic_teacher", agreement

    def teacher_sampling_plan(
        self,
        project_id: str,
        activity_episode_ref: str,
        *,
        capabilities: Sequence[str] = tuple(sorted(CURRICULUM_SUPPORTED_CAPABILITIES)),
    ) -> Mapping[str, Any]:
        """Persist one capability-local opportunity and return its replayable due set.

        The ordinal is a mechanical counter scoped to a capability.  It only
        schedules an advisory call; it never changes semantic matching, truth,
        action scores, or a curriculum result.  The episode fence makes retries
        return the same ordinal and therefore the same due decision.
        """

        _text(project_id, "project_id", limit=512)
        _text(activity_episode_ref, "activity_episode_ref", limit=1024)
        requested = tuple(
            dict.fromkeys(
                str(item)
                for item in capabilities
                if isinstance(item, str) and item in CURRICULUM_SUPPORTED_CAPABILITIES
            )
        )[:32]
        if not requested:
            return {
                "policy": self.maturity_policy.to_dict(),
                "capabilities": [],
                "requested_capabilities": [],
                "all_withheld": True,
            }
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                ordinals: dict[str, int] = {}
                for capability_key in requested:
                    prior = connection.execute(
                        """SELECT ordinal FROM capability_opportunities
                        WHERE project_id = ? AND target_capability = ?
                          AND activity_episode_ref = ? LIMIT 1""",
                        (project_id, capability_key, activity_episode_ref),
                    ).fetchone()
                    if prior is not None:
                        ordinals[capability_key] = int(prior["ordinal"])
                        continue
                    maximum = connection.execute(
                        """SELECT MAX(ordinal) AS n FROM capability_opportunities
                        WHERE project_id = ? AND target_capability = ?""",
                        (project_id, capability_key),
                    ).fetchone()
                    ordinal = int(maximum["n"] or 0) + 1
                    connection.execute(
                        """INSERT INTO capability_opportunities
                        (opportunity_id, project_id, target_capability,
                         activity_episode_ref, ordinal, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            _stable_id(
                                "capability_opportunity",
                                project_id,
                                capability_key,
                                activity_episode_ref,
                            ),
                            project_id,
                            capability_key,
                            activity_episode_ref,
                            ordinal,
                            utc_now(),
                        ),
                    )
                    ordinals[capability_key] = ordinal
                curriculum_rows = connection.execute(
                    """SELECT target_capability, status FROM curricula
                    WHERE project_id = ?""",
                    (project_id,),
                ).fetchall()
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        curriculum_status: dict[str, list[str]] = {}
        for row in curriculum_rows:
            curriculum_status.setdefault(str(row["target_capability"]), []).append(str(row["status"]))
        rows: list[dict[str, Any]] = []
        due_capabilities: list[str] = []
        policy = self.maturity_policy
        for capability_key in requested:
            counts = self._recent_capability_counts(project_id, capability_key)
            successes = int(counts["success"])
            counterexamples = int(counts["counterexample"])
            opportunities = int(counts["opportunities"])
            statuses = curriculum_status.get(capability_key, ())
            state, rate, reason, agreement = self._sampling_state(
                status_reteach="reteach" in statuses,
                opportunities=opportunities,
                successes=successes,
                counterexamples=counterexamples,
                policy=policy,
            )
            ordinal = ordinals[capability_key]
            if rate >= 1.0:
                due = True
                period = 1
            else:
                period = max(1, int(round(1.0 / rate)))
                due = ((ordinal - 1) % period) == 0
            if due:
                due_capabilities.append(capability_key)
            rows.append(
                {
                    "target_capability": capability_key,
                    "opportunity_ordinal": ordinal,
                    "state": state,
                    "rate": round(float(rate), 6),
                    "sampling_period": period,
                    "due": due,
                    "reason": reason if due else "locally_withheld_by_sampling",
                    "window": {
                        "size": policy.window_size,
                        "opportunities": opportunities,
                        "known_outcomes": successes + counterexamples,
                        "success": successes,
                        "counterexample": counterexamples,
                        "agreement": agreement,
                    },
                }
            )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for item in rows:
                    connection.execute(
                        """UPDATE capability_opportunities
                        SET teacher_requested = ?, sampling_state = ?, sampling_rate = ?,
                            sampling_period = ?, sampling_reason = ?
                        WHERE project_id = ? AND target_capability = ?
                          AND activity_episode_ref = ?""",
                        (
                            1 if item["due"] else 0,
                            item["state"],
                            item["rate"],
                            item["sampling_period"],
                            item["reason"],
                            project_id,
                            item["target_capability"],
                            activity_episode_ref,
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "policy": policy.to_dict(),
            "capabilities": rows,
            "requested_capabilities": due_capabilities,
            "all_withheld": not due_capabilities,
        }

    def curricula(self, project_id: str, *, limit: int = 64) -> tuple[CurriculumCandidate, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 256:
            raise ContractError("curriculum_list_limit_out_of_bounds")
        project_id = self._canonical_project_id(project_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT status, payload_json FROM curricula WHERE project_id = ?
                ORDER BY updated_at DESC LIMIT ?""",
                (project_id, limit),
            ).fetchall()
        return tuple(
            CurriculumCandidate.from_dict(self._loads(str(row["payload_json"]))).with_status(str(row["status"]))
            for row in rows
        )

    def preferences(self, activity: ProjectActivity) -> dict[str, float]:
        project_id = self._canonical_project_id(activity.project_id)
        features = _activity_learning_features(activity)
        if not features:
            return {}
        placeholders = ",".join("?" for _ in features)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT action_kind, value FROM action_preferences
                WHERE project_id = ? AND feature_key IN ({placeholders})""",
                (project_id, *features),
            ).fetchall()
        grouped: dict[str, list[float]] = {}
        for row in rows:
            grouped.setdefault(str(row["action_kind"]), []).append(float(row["value"]))
        return {
            action: round(max(-0.6, min(0.6, sum(values) / len(values))), 6)
            for action, values in grouped.items()
            if values
        }

    def apply(self, lesson: FeedbackLesson) -> FeedbackLesson:
        """Apply one lesson once, after its readback has entered cognition."""

        if not isinstance(lesson, FeedbackLesson):
            raise ContractError("learning_ledger_requires_feedback_lesson")
        prior = self.get_lesson(lesson.lesson_id)
        if prior is not None:
            return prior
        delta = _mapping(lesson.structured_delta, "structured_delta")
        raw_features = delta.get("feature_keys")
        features = _texts(raw_features, "feature_keys", max_items=16, limit=256) if raw_features is not None else ()
        raw_adjustments = delta.get("action_adjustments")
        adjustments = _mapping(raw_adjustments, "action_adjustments") if raw_adjustments is not None else {}
        applied = lesson.with_status("applied")
        now = utc_now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO feedback_lessons
                    (lesson_id, project_id, feedback_ref, status, payload_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        applied.lesson_id,
                        applied.project_id,
                        applied.feedback_ref,
                        applied.status,
                        json.dumps(applied.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        applied.created_at,
                        now,
                    ),
                )
                for feature in features:
                    for action, raw_delta in adjustments.items():
                        if action not in PROJECT_ACTION_KINDS:
                            continue
                        if isinstance(raw_delta, bool) or not isinstance(raw_delta, (int, float)):
                            continue
                        row = connection.execute(
                            """SELECT value, sample_count FROM action_preferences
                            WHERE project_id = ? AND feature_key = ? AND action_kind = ?""",
                            (applied.project_id, feature, action),
                        ).fetchone()
                        prior_value = float(row["value"]) if row else 0.0
                        count = int(row["sample_count"]) if row else 0
                        # A small bounded update lets repeated feedback matter
                        # without one click installing a fixed winner.
                        next_value = max(-0.6, min(0.6, prior_value + 0.18 * float(raw_delta)))
                        connection.execute(
                            """INSERT INTO action_preferences
                            (project_id, feature_key, action_kind, value, sample_count, last_lesson_id, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(project_id, feature_key, action_kind) DO UPDATE SET
                            value=excluded.value, sample_count=excluded.sample_count,
                            last_lesson_id=excluded.last_lesson_id, updated_at=excluded.updated_at""",
                            (applied.project_id, feature, action, next_value, count + 1, applied.lesson_id, now),
                        )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return applied

    def snapshot(self, project_id: str) -> dict[str, Any]:
        project_id = self._canonical_project_id(project_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT feature_key, action_kind, value, sample_count, last_lesson_id, updated_at
                FROM action_preferences WHERE project_id = ?
                ORDER BY feature_key, action_kind""",
                (project_id,),
            ).fetchall()
            lesson_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM feedback_lessons WHERE project_id = ?",
                    (project_id,),
                ).fetchone()["n"]
            )
            curriculum_rows = connection.execute(
                """SELECT curriculum_id, target_capability, source_ref, status,
                payload_json, updated_at FROM curricula WHERE project_id = ?
                ORDER BY updated_at DESC LIMIT 64""",
                (project_id,),
            ).fetchall()
            attempt_rows = connection.execute(
                """SELECT curriculum_id, status, COUNT(*) AS n
                FROM curriculum_attempts_v2 WHERE project_id = ?
                GROUP BY curriculum_id, status ORDER BY curriculum_id, status""",
                (project_id,),
            ).fetchall()
            legacy_attempt_rows = connection.execute(
                """SELECT curriculum_id, status, COUNT(*) AS n
                FROM curriculum_attempts WHERE project_id = ?
                GROUP BY curriculum_id, status ORDER BY curriculum_id, status""",
                (project_id,),
            ).fetchall()
            outcome_rows = connection.execute(
                """SELECT curriculum_id, outcome, COUNT(*) AS n
                FROM curriculum_outcomes WHERE project_id = ?
                GROUP BY curriculum_id, outcome ORDER BY curriculum_id, outcome""",
                (project_id,),
            ).fetchall()
            replacement_rows = connection.execute(
                """SELECT curriculum_id, payload_json FROM curriculum_outcomes
                WHERE project_id = ? AND outcome = 'replaced'
                ORDER BY created_at DESC, outcome_id DESC""",
                (project_id,),
            ).fetchall()
            sampling_rows = connection.execute(
                """SELECT target_capability,
                COUNT(*) AS opportunities,
                SUM(CASE WHEN teacher_requested = 1 THEN 1 ELSE 0 END) AS requested,
                SUM(CASE WHEN teacher_requested = 0 THEN 1 ELSE 0 END) AS withheld,
                MAX(ordinal) AS latest_ordinal
                FROM capability_opportunities WHERE project_id = ?
                GROUP BY target_capability""",
                (project_id,),
            ).fetchall()
            latest_sampling_rows = connection.execute(
                """SELECT o.target_capability, o.teacher_requested, o.sampling_state,
                o.sampling_rate, o.sampling_period, o.sampling_reason, o.ordinal
                FROM capability_opportunities o
                JOIN (
                    SELECT target_capability, MAX(ordinal) AS latest_ordinal
                    FROM capability_opportunities WHERE project_id = ?
                    GROUP BY target_capability
                ) latest ON latest.target_capability = o.target_capability
                    AND latest.latest_ordinal = o.ordinal
                WHERE o.project_id = ?""",
                (project_id, project_id),
            ).fetchall()
        attempt_counts: dict[str, dict[str, int]] = {}
        for row in (*attempt_rows, *legacy_attempt_rows):
            by_status = attempt_counts.setdefault(str(row["curriculum_id"]), {})
            by_status[str(row["status"])] = by_status.get(str(row["status"]), 0) + int(row["n"])
        outcome_counts: dict[str, dict[str, int]] = {}
        for row in outcome_rows:
            outcome_counts.setdefault(str(row["curriculum_id"]), {})[str(row["outcome"])] = int(row["n"])
        replacement_by_curriculum: dict[str, CurriculumOutcome] = {}
        for row in replacement_rows:
            curriculum_id = str(row["curriculum_id"])
            if curriculum_id not in replacement_by_curriculum:
                replacement_by_curriculum[curriculum_id] = CurriculumOutcome.from_dict(
                    self._loads(str(row["payload_json"]))
                )
        recent_counts_by_capability = {
            capability_key: self._recent_capability_counts(project_id, capability_key)
            for capability_key in CURRICULUM_SUPPORTED_CAPABILITIES
        }
        sampling_by_capability = {
            str(row["target_capability"]): {
                "opportunities": int(row["opportunities"] or 0),
                "requested": int(row["requested"] or 0),
                "withheld": int(row["withheld"] or 0),
                "latest_ordinal": int(row["latest_ordinal"] or 0),
            }
            for row in sampling_rows
        }
        latest_sampling_by_capability = {
            str(row["target_capability"]): {
                "requested": bool(row["teacher_requested"]) if row["teacher_requested"] is not None else None,
                "state": str(row["sampling_state"]) if row["sampling_state"] is not None else None,
                "rate": float(row["sampling_rate"]) if row["sampling_rate"] is not None else None,
                "period": int(row["sampling_period"]) if row["sampling_period"] is not None else None,
                "reason": str(row["sampling_reason"]) if row["sampling_reason"] is not None else None,
                "ordinal": int(row["ordinal"]),
            }
            for row in latest_sampling_rows
        }
        curriculum_projection: list[dict[str, Any]] = []
        for row in curriculum_rows:
            item = CurriculumCandidate.from_dict(self._loads(str(row["payload_json"]))).with_status(str(row["status"]))
            effect, reasons = _normalise_curriculum_effect(item.target_capability, item.suggested_adjustment)
            counts = attempt_counts.get(item.curriculum_id, {})
            outcomes = outcome_counts.get(item.curriculum_id, {})
            opportunities = sum(counts.values())
            successes = int(outcomes.get("success", 0))
            counterexamples = int(outcomes.get("counterexample", 0))
            known = successes + counterexamples
            agreement = round(successes / known, 6) if known else None
            enough = opportunities >= self.maturity_policy.minimum_opportunities
            replacement_outcome = replacement_by_curriculum.get(item.curriculum_id)
            if item.status == "retracted" and replacement_outcome is not None:
                maturity_state = "retracted"
                intervention_rate = 0.0
                maturity_reason = "旧课程与反例历史已保留；同范围新课程完成采纳和回读后接替当前训练世代"
            else:
                maturity_state, intervention_rate, maturity_reason_key, _ = self._sampling_state(
                    status_reteach=item.status == "reteach",
                    opportunities=opportunities,
                    successes=successes,
                    counterexamples=counterexamples,
                    policy=self.maturity_policy,
                )
                maturity_reason = {
                    "related_counterexample_requires_reteach": "出现相关反例，恢复该能力局部教学",
                    "minimum_independent_opportunities_not_met": f"独立机会 {opportunities}/{self.maturity_policy.minimum_opportunities}，尚不足以降低教师介入",
                    "agreement_threshold_met_for_local_audit": "独立结果已达到低频抽查建议；不是永久关闭教师",
                    "local_trial_requires_periodic_teacher": "仍在试用，需要更多独立结果校准",
                }[maturity_reason_key]
            curriculum_projection.append(
                {
                    **item.to_dict(),
                    "trial_effect": dict(effect),
                    "trial_adjustments": dict(effect.get("action_adjustments", {})),
                    "unsupported_reasons": list(reasons),
                    "attempt_counts": counts,
                    "outcome_counts": outcomes,
                    "updated_at": str(row["updated_at"]),
                    "replacement": {
                        "replaced_by_curriculum_id": (
                            str(replacement_outcome.extra.get("replacement_curriculum_id"))
                            if replacement_outcome is not None
                            and isinstance(
                                replacement_outcome.extra.get("replacement_curriculum_id"), str
                            )
                            else None
                        ),
                        "activation_outcome_ref": (
                            str(replacement_outcome.extra.get("replacement_activation_outcome_id"))
                            if replacement_outcome is not None
                            and isinstance(
                                replacement_outcome.extra.get("replacement_activation_outcome_id"), str
                            )
                            else None
                        ),
                        "historical_counterexamples_preserved": item.status == "retracted"
                        and counterexamples > 0,
                    },
                    "maturity": {
                        "state": maturity_state,
                        "opportunities": opportunities,
                        "known_outcomes": known,
                        "success": successes,
                        "counterexample": counterexamples,
                        "unknown": max(0, opportunities - known),
                        "recent_agreement": agreement,
                        "sample_sufficiency": enough,
                        "teacher_intervention_rate": round(float(intervention_rate), 6),
                        "reason": maturity_reason,
                    },
                    "teacher_still_needed_reason": maturity_reason,
                }
            )
        capability_projection: list[dict[str, Any]] = []
        capability_keys = tuple(sorted(CURRICULUM_SUPPORTED_CAPABILITIES))
        for capability_key in capability_keys:
            members = [item for item in curriculum_projection if item["target_capability"] == capability_key]
            recent = recent_counts_by_capability.get(capability_key, {})
            opportunity_count = int(recent.get("opportunities", 0))
            attempted_count = int(recent.get("attempted", 0))
            withheld_count = int(recent.get("withheld", 0))
            success_count = int(recent.get("success", 0))
            counterexample_count = int(recent.get("counterexample", 0))
            known_count = success_count + counterexample_count
            agreement = round(success_count / known_count, 6) if known_count else None
            enough = opportunity_count >= self.maturity_policy.minimum_opportunities
            active_trials = sum(1 for item in members if item["status"] == "active_trial")
            state, intervention_rate, state_reason, _ = self._sampling_state(
                status_reteach=any(item["status"] == "reteach" for item in members),
                opportunities=opportunity_count,
                successes=success_count,
                counterexamples=counterexample_count,
                policy=self.maturity_policy,
            )
            reason = {
                "related_counterexample_requires_reteach": "该能力出现相关反例，恢复局部教学；其他能力不受影响",
                "minimum_independent_opportunities_not_met": f"独立机会 {opportunity_count}/{self.maturity_policy.minimum_opportunities}，尚不足以降低教师介入",
                "agreement_threshold_met_for_local_audit": "该能力近期已达到低频抽查建议；遇到反例会重新教学",
                "local_trial_requires_periodic_teacher": "该能力仍在试用，需要更多独立结果校准",
            }[state_reason]
            if not members:
                state = "no_local_course"
                intervention_rate = self.maturity_policy.teaching_rate
                reason = "尚无本地课程；冷启动阶段由教师提供可审建议，AP 保留是否采纳权"
            if not members:
                maturity_level = "L0"
                ownership = "native_shallow"
                product_effect = "local_baseline_only"
                growth_effect = "no_local_course"
            elif opportunity_count == 0:
                maturity_level = "L1"
                ownership = "assisted"
                product_effect = "course_registered_not_yet_used"
                growth_effect = "reversible_course_available"
            elif attempted_count == 0:
                maturity_level = "L2"
                ownership = "assisted"
                product_effect = "trial_replayable_but_withheld"
                growth_effect = "bounded_trial_available"
            elif state == "low_frequency_audit":
                maturity_level = "L3"
                ownership = "assisted"
                product_effect = "local_trial_influenced_later_episode"
                growth_effect = "low_frequency_audit_mechanism_only"
            else:
                maturity_level = "L3"
                ownership = "assisted"
                product_effect = "local_trial_influenced_later_episode"
                growth_effect = "outcome_window_accumulating"
            sampling_counts = sampling_by_capability.get(capability_key, {})
            actual_sampling_opportunities = int(sampling_counts.get("opportunities", 0))
            actual_requests = int(sampling_counts.get("requested", 0))
            actual_withheld = int(sampling_counts.get("withheld", 0))
            latest_sampling = latest_sampling_by_capability.get(capability_key, {})
            capability_projection.append(
                {
                    "target_capability": capability_key,
                    "curriculum_count": len(members),
                    "teacher_proposed": len(members),
                    "active_trials": active_trials,
                    "opportunities": opportunity_count,
                    "attempted": attempted_count,
                    "withheld": withheld_count,
                    "success": success_count,
                    "counterexample": counterexample_count,
                    "unknown": max(0, opportunity_count - known_count),
                    "recent_agreement": agreement,
                    "sample_sufficiency": enough,
                    "state": state,
                    "teacher_intervention_rate": round(float(intervention_rate), 6),
                    "window_size": self.maturity_policy.window_size,
                    "reason": reason,
                    "maturity_level": maturity_level,
                    "ownership": ownership,
                    "product_effect": product_effect,
                    "growth_effect": growth_effect,
                    "actual_teacher_requests": actual_requests,
                    "actual_teacher_withheld": actual_withheld,
                    "actual_request_rate": round(actual_requests / actual_sampling_opportunities, 6)
                    if actual_sampling_opportunities
                    else None,
                    "latest_sampling": latest_sampling,
                    "long_term_gate": {
                        "met": False,
                        "required": "真实 100 次窗口、至少 95% 无需修订、未见输入、冷重启与跨模型证据",
                        "reason": "long_term_and_cross_model_evidence_unmeasured",
                    },
                }
            )
        return {
            "project_id": project_id,
            "lesson_count": lesson_count,
            "preferences": [
                {
                    "feature_key": str(row["feature_key"]),
                    "action_kind": str(row["action_kind"]),
                    "value": round(float(row["value"]), 6),
                    "sample_count": int(row["sample_count"]),
                    "last_lesson_id": str(row["last_lesson_id"]),
                    "updated_at": str(row["updated_at"]),
                }
                for row in rows
            ],
            "curriculum_count": len(curriculum_projection),
            "active_trial_count": sum(1 for item in curriculum_projection if item["status"] == "active_trial"),
            "capability_maturity": capability_projection,
            "curricula": curriculum_projection,
        }


def lesson_from_feedback(
    feedback: ProjectFeedback,
    target_activity: ProjectActivity,
) -> FeedbackLesson:
    """Translate explicit structured feedback into a conservative lesson.

    Natural-language interpretation by an LLM will be a replaceable producer
    of ``ProjectFeedback``.  This function does not parse sentiment or task
    words; it uses the explicit target/desired action and target occurrence's
    generic features, preserving the user's original sentence as evidence.
    """

    if feedback.project_id != target_activity.project_id:
        raise ContractError("feedback_target_project_mismatch")
    raw_capability = feedback.applicability.get("target_capability")
    target_capability = (
        str(raw_capability)
        if isinstance(raw_capability, str) and raw_capability in CURRICULUM_SUPPORTED_CAPABILITIES
        else CURRICULUM_TARGET_CAPABILITY
    )
    if target_capability == CURRICULUM_TARGET_CAPABILITY:
        if feedback.target_action not in PROJECT_ACTION_KINDS:
            raise ContractError("feedback_target_action_unsupported")
        if feedback.desired_action is not None and feedback.desired_action not in PROJECT_ACTION_KINDS:
            raise ContractError("feedback_desired_action_unsupported")
    if feedback.signal in {"reward", "positive", "approve"}:
        direction = 1.0
    elif feedback.signal in {"punishment", "negative", "reject", "correction"}:
        direction = -1.0
    else:
        direction = 0.0
    features = _activity_learning_features(target_activity)
    structured_delta: dict[str, Any] = {"feature_keys": list(features)}
    target_refs = [feedback.target_episode_id]
    if target_capability == CURRICULUM_TARGET_CAPABILITY:
        adjustments: dict[str, float] = {
            feedback.target_action: round(direction * float(feedback.magnitude), 6)
        }
        if feedback.desired_action is not None:
            adjustments[feedback.desired_action] = round(float(feedback.magnitude), 6)
        structured_delta["action_adjustments"] = adjustments
        target_refs.append(feedback.target_action)
    else:
        effect_key = feedback.applicability.get("effect_key")
        if isinstance(effect_key, str) and effect_key:
            target_refs.append(effect_key)
        structured_delta["curriculum_feedback"] = {
            "target_capability": target_capability,
            "effect_key": effect_key,
            "direction": direction,
        }
    return FeedbackLesson(
        lesson_id=_stable_id("lesson", feedback.project_id, feedback.feedback_id),
        project_id=feedback.project_id,
        feedback_ref=feedback.feedback_id,
        target_capability=target_capability,
        target_refs=tuple(target_refs),
        signal=feedback.signal,
        magnitude=float(feedback.magnitude),
        natural_language=feedback.natural_language,
        structured_delta=structured_delta,
        applicability={
            "project_id": feedback.project_id,
            "feature_keys": list(features),
            **_mapping(feedback.applicability, "applicability"),
        },
        counterexamples=feedback.counterexamples,
        source="user",
        status="proposed",
    )


def curricula_from_teacher_frame(
    activity: ProjectActivity,
    *,
    source_episode_ref: str,
    gateway_view: Mapping[str, Any],
) -> tuple[CurriculumCandidate, ...]:
    """Convert accepted B3 lesson envelopes into staged curriculum objects.

    This consumes the persisted/normalised gateway view, never a fresh model
    response.  The current activity's evidence profile is appended as a
    verifiable applicability boundary; model trigger features are preserved
    alongside it and are not interpreted as executable code.
    """

    lessons_raw = gateway_view.get("lesson_candidates")
    lessons: list[Mapping[str, Any]] = [
        item for item in lessons_raw[:MAX_CURRICULA_PER_ACTIVITY]
        if isinstance(item, Mapping)
    ] if isinstance(lessons_raw, Sequence) and not isinstance(lessons_raw, (str, bytes)) else []
    # Direct recall/appraisal teacher candidates are demonstrations. Convert
    # only accepted, source-grounded items to the same staged curriculum
    # contract; they still require an independent AP adoption episode.
    recall_candidates = gateway_view.get("recall_candidates")
    if isinstance(recall_candidates, Sequence) and not isinstance(recall_candidates, (str, bytes)):
        for item in recall_candidates[:4]:
            if not isinstance(item, Mapping) or item.get("validation") != "accepted":
                continue
            memory_ref = item.get("memory_ref")
            relevance = item.get("relevance")
            if not isinstance(memory_ref, str) or isinstance(relevance, bool) or not isinstance(relevance, (int, float)):
                continue
            lessons.append(
                {
                    **dict(item),
                    "candidate_id": f"{item.get('candidate_id')}:recall-course",
                    "capability": CURRICULUM_RECALL_CAPABILITY,
                    "trigger_features": {},
                    "suggested_adjustment": {"memory_gain_adjustments": {memory_ref: float(relevance)}},
                    "counterexamples": ["后续活动与该记忆无关或用户明确纠正召回时撤回"],
                    "confidence": float(relevance),
                }
            )
    appraisal_candidates = gateway_view.get("appraisal_candidates")
    if isinstance(appraisal_candidates, Sequence) and not isinstance(appraisal_candidates, (str, bytes)):
        for item in appraisal_candidates[:4]:
            if not isinstance(item, Mapping) or item.get("validation") != "accepted":
                continue
            name = item.get("name")
            intensity = item.get("intensity")
            if not isinstance(name, str) or isinstance(intensity, bool) or not isinstance(intensity, (int, float)):
                continue
            required_signals = []
            if activity.completeness != "complete":
                required_signals.append("source_incomplete")
            if activity.observed_remaining:
                required_signals.append("open_items_present")
            if activity.observed_unknown:
                required_signals.append("unknown_present")
            conflicts = activity.extra.get("conflicts")
            if isinstance(conflicts, Sequence) and not isinstance(conflicts, (str, bytes)) and conflicts:
                required_signals.append("conflict_present")
            if not required_signals:
                # Do not manufacture a textual trigger from the teacher's
                # rationale or appraisal name.
                continue
            effect: dict[str, Any] = {
                "name": name,
                "intensity_delta": float(intensity),
                "required_signals": required_signals,
            }
            valence = item.get("valence")
            if isinstance(valence, (int, float)) and not isinstance(valence, bool):
                effect["valence"] = float(valence)
            lessons.append(
                {
                    **dict(item),
                    "candidate_id": f"{item.get('candidate_id')}:appraisal-course",
                    "capability": CURRICULUM_APPRAISAL_CAPABILITY,
                    "trigger_features": {},
                    "suggested_adjustment": {"appraisal_effects": [effect]},
                    "counterexamples": ["相同结构信号未产生该感受或用户明确纠正时撤回"],
                    "confidence": float(intensity),
                }
            )
    prediction_candidates = gateway_view.get("prediction_candidates")
    if isinstance(prediction_candidates, Sequence) and not isinstance(prediction_candidates, (str, bytes)):
        for item in prediction_candidates[:4]:
            if not isinstance(item, Mapping) or item.get("validation") != "accepted":
                continue
            content = item.get("content")
            confidence = item.get("confidence")
            uncertainty = item.get("uncertainty")
            if (
                not isinstance(content, str)
                or not content.strip()
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or isinstance(uncertainty, bool)
                or not isinstance(uncertainty, (int, float))
            ):
                continue
            lessons.append(
                {
                    **dict(item),
                    "candidate_id": f"{item.get('candidate_id')}:prediction-course",
                    "capability": CURRICULUM_PREDICTION_CAPABILITY,
                    "trigger_features": {},
                    "suggested_adjustment": {
                        "prediction_hypotheses": [
                            {
                                "content": content,
                                "mode": item.get("mode", "forecast"),
                                "confidence": float(confidence),
                                "uncertainty": float(uncertainty),
                                "evidence_refs": list(item.get("evidence_refs", ())),
                                "completeness": item.get("completeness", "unknown"),
                            }
                        ]
                    },
                    "counterexamples": ["后续现实与该假设不符或用户明确纠正预测时撤回"],
                    "confidence": float(confidence),
                }
            )
    thought_candidates = gateway_view.get("thought_candidates")
    if isinstance(thought_candidates, Sequence) and not isinstance(thought_candidates, (str, bytes)):
        for item in thought_candidates[:4]:
            if not isinstance(item, Mapping) or item.get("validation") != "accepted":
                continue
            content = item.get("content")
            uncertainty = item.get("uncertainty")
            if (
                not isinstance(content, str)
                or not content.strip()
                or isinstance(uncertainty, bool)
                or not isinstance(uncertainty, (int, float))
            ):
                continue
            lessons.append(
                {
                    **dict(item),
                    "candidate_id": f"{item.get('candidate_id')}:thought-course",
                    "capability": CURRICULUM_THOUGHT_CAPABILITY,
                    "trigger_features": {},
                    "suggested_adjustment": {
                        "thought_scaffolds": [
                            {
                                "content": content,
                                "unresolved": list(item.get("unresolved", ())),
                                "evidence_refs": list(item.get("evidence_refs", ())),
                                "uncertainty": float(uncertainty),
                            }
                        ]
                    },
                    "counterexamples": ["后续活动不需要该思考支架或用户明确纠正想法时撤回"],
                    "confidence": round(_clamp(1.0 - float(uncertainty)), 6),
                    "uncertainty": float(uncertainty),
                }
            )
    paradigm_candidates = gateway_view.get("paradigm_candidates")
    if isinstance(paradigm_candidates, Sequence) and not isinstance(paradigm_candidates, (str, bytes)):
        for item in paradigm_candidates[:4]:
            if not isinstance(item, Mapping) or item.get("validation") != "accepted":
                continue
            pattern = {
                key: item.get(key)
                for key in (
                    "pattern_kind", "invariants", "slots", "relations", "evidence_refs", "completeness"
                )
            }
            lessons.append(
                {
                    **dict(item),
                    "candidate_id": f"{item.get('candidate_id')}:paradigm-course",
                    "capability": CURRICULUM_PARADIGM_CAPABILITY,
                    "trigger_features": {},
                    "suggested_adjustment": {"paradigm_patterns": [pattern]},
                    "counterexamples": list(item.get("counterexamples", ()))
                    or ["后续活动结构不相容或必需槽位无法绑定时不应套用"],
                    "confidence": float(item.get("confidence", 0.0)),
                    "uncertainty": float(item.get("uncertainty", 1.0)),
                }
            )
    attention_candidates = gateway_view.get("attention_candidates")
    if isinstance(attention_candidates, Sequence) and not isinstance(attention_candidates, (str, bytes)):
        for item in attention_candidates[:4]:
            if not isinstance(item, Mapping) or item.get("validation") != "accepted":
                continue
            mode = item.get("mode")
            selector = {
                "maintain_attention": "current_sa",
                "shift_attention": "best_recall_or_paradigm",
                "diversify_attention": "unresolved_frontier",
            }.get(str(mode))
            if selector is None:
                continue
            lessons.append(
                {
                    **dict(item),
                    "candidate_id": f"{item.get('candidate_id')}:attention-course",
                    "capability": CURRICULUM_ATTENTION_CAPABILITY,
                    "trigger_features": {},
                    "suggested_adjustment": {
                        "attention_adjustments": [
                            {
                                "mode": mode,
                                "target_selector": selector,
                                "source_target_ref": item.get("target_ref"),
                                "gain_delta": item.get("gain_delta", 0.0),
                                "source_refs": list(item.get("source_refs", ())),
                            }
                        ]
                    },
                    "counterexamples": ["后续信息价值、成本或疲劳方向与该注意建议不符时撤回"],
                    "confidence": round(_clamp(1.0 - float(item.get("uncertainty", 1.0))), 6),
                }
            )
    expression_candidates = gateway_view.get("expression_candidates")
    if isinstance(expression_candidates, Sequence) and not isinstance(expression_candidates, (str, bytes)):
        for item in expression_candidates[:4]:
            if not isinstance(item, Mapping) or item.get("validation") != "accepted":
                continue
            pattern = {
                key: item.get(key)
                for key in ("template", "prefix", "suffix", "tone", "evidence_refs")
            }
            lessons.append(
                {
                    **dict(item),
                    "candidate_id": f"{item.get('candidate_id')}:expression-course",
                    "capability": CURRICULUM_EXPRESSION_CAPABILITY,
                    "trigger_features": {},
                    "suggested_adjustment": {"expression_patterns": [pattern]},
                    "counterexamples": list(item.get("counterexamples", ()))
                    or ["表达外壳改变命题或不适合当前语境时撤回"],
                    "confidence": round(_clamp(1.0 - float(item.get("uncertainty", 1.0))), 6),
                }
            )
    parameter_candidates = gateway_view.get("parameter_candidates")
    if isinstance(parameter_candidates, Sequence) and not isinstance(parameter_candidates, (str, bytes)):
        for item in parameter_candidates[:4]:
            if not isinstance(item, Mapping) or item.get("validation") != "accepted":
                continue
            parameter = item.get("parameter")
            delta = item.get("delta")
            if (
                not isinstance(parameter, str)
                or parameter not in ATTENTION_PARAMETER_BOUNDS
                or isinstance(delta, bool)
                or not isinstance(delta, (int, float))
            ):
                continue
            lessons.append(
                {
                    **dict(item),
                    "candidate_id": f"{item.get('candidate_id')}:parameter-course",
                    "capability": CURRICULUM_PARAMETER_CAPABILITY,
                    "trigger_features": {},
                    "suggested_adjustment": {
                        "parameter_adjustments": [
                            {
                                "parameter": parameter,
                                "delta": float(delta),
                                "expected_direction": item.get("expected_direction"),
                                "source_refs": list(item.get("source_refs", ())),
                                "rationale": item.get("rationale"),
                                "bounds": list(ATTENTION_PARAMETER_BOUNDS[parameter]),
                            }
                        ]
                    },
                    "counterexamples": list(item.get("counterexamples", ()))
                    or ["该增益分量方向未改善后续注意效果时撤回"],
                    "confidence": round(_clamp(1.0 - float(item.get("uncertainty", 1.0))), 6),
                }
            )
    if not lessons:
        return ()
    output: list[CurriculumCandidate] = []
    profile = _activity_learning_features(activity)
    receipt = gateway_view.get("call_receipt")
    receipt_created_at = receipt.get("created_at") if isinstance(receipt, Mapping) else None
    curriculum_created_at = (
        receipt_created_at
        if isinstance(receipt_created_at, str) and receipt_created_at.strip()
        else activity.occurred_at
    )
    source_memory_refs = tuple(
        str(item.get("ref"))
        for item in gateway_view.get("adoption", {}).get("families", {}).get("recall", {}).get("local_before", ())
        if isinstance(item, Mapping) and isinstance(item.get("ref"), str)
    ) if isinstance(gateway_view.get("adoption"), Mapping) else ()
    for index, raw in enumerate(lessons[:MAX_CURRICULA_PER_ACTIVITY]):
        if not isinstance(raw, Mapping) or raw.get("validation") != "accepted":
            continue
        candidate_ref = raw.get("candidate_id")
        capability = raw.get("capability")
        source_ref = candidate_ref if isinstance(candidate_ref, str) and candidate_ref else f"teacher-lesson-{index}"
        target_capability = str(capability).strip() if isinstance(capability, str) else ""
        model_receipt_ref = raw.get("model_receipt_ref")
        trigger = _mapping(raw.get("trigger_features"), "trigger_features")
        trigger["evidence_profile_keys"] = list(profile)
        trigger["structure_profile"] = _activity_structure_profile(activity)
        trigger["source_input_refs"] = list(
            dict.fromkeys(
                (
                    activity.source_ref,
                    activity.activity_id,
                    *(
                        str(item)
                        for item in raw.get("input_refs", ())
                        if isinstance(item, str) and item
                    ),
                )
            )
        )
        if target_capability == CURRICULUM_RECALL_CAPABILITY:
            trigger["source_memory_refs"] = list(source_memory_refs)
        suggested = _mapping(raw.get("suggested_adjustment"), "suggested_adjustment")
        counterexamples = _texts(raw.get("counterexamples"), "counterexamples", max_items=12, limit=2048)
        confidence_raw = raw.get("confidence", 0.0)
        confidence = float(confidence_raw) if isinstance(confidence_raw, (int, float)) and not isinstance(confidence_raw, bool) else 0.0
        confidence = _clamp(confidence)
        uncertainty_raw = raw.get("uncertainty", 1.0 - confidence)
        uncertainty = float(uncertainty_raw) if isinstance(uncertainty_raw, (int, float)) and not isinstance(uncertainty_raw, bool) else 1.0 - confidence
        evidence_refs = tuple(
            dict.fromkeys(
                (
                    activity.source_ref,
                    activity.activity_id,
                    *(
                        str(item)
                        for item in raw.get("input_refs", ())
                        if isinstance(item, str) and item
                    ),
                )
            )
        )
        preliminary = CurriculumCandidate(
            curriculum_id=_stable_id(
                "curriculum",
                activity.project_id,
                source_episode_ref,
                str(model_receipt_ref or "missing-receipt"),
                source_ref,
            ),
            project_id=activity.project_id,
            target_capability=target_capability or "unknown",
            source_kind="llm_teacher",
            source_ref=source_ref,
            source_episode_ref=source_episode_ref,
            model_receipt_ref=str(model_receipt_ref) if isinstance(model_receipt_ref, str) and model_receipt_ref else None,
            trigger_features=trigger,
            suggested_adjustment=suggested,
            counterexamples=counterexamples,
            confidence=round(confidence, 6),
            uncertainty=round(_clamp(uncertainty), 6),
            evidence_refs=evidence_refs,
            status="staged",
            created_at=curriculum_created_at,
            extra={
                "teacher_validation": "accepted",
                "teacher_extensions": _mapping(raw.get("extensions"), "extensions"),
                "source_activity_ref": activity.activity_id,
            },
        )
        _, reasons = _curriculum_eligibility_reasons(preliminary)
        if reasons:
            preliminary = replace(
                preliminary,
                status="tentative_unsupported",
                extra={**preliminary.extra, "eligibility_reasons": list(reasons)},
            )
        output.append(preliminary)
    return tuple(output)


def proposal_from_activity(
    activity: ProjectActivity,
    frame_view: Mapping[str, Any],
) -> ProjectKnowledgeProposal:
    """Form a conservative AP-native project-knowledge candidate.

    The proposal mirrors only source fields.  It does not infer completion or
    invent a next action from prose.  More capable AP/teacher implementations
    can propose richer candidates through the same contract later.
    """

    confidence_by_completeness = {
        "complete": 0.88,
        "partial": 0.55,
        "search_incomplete": 0.34,
        "unknown": 0.20,
    }
    confidence = confidence_by_completeness.get(activity.completeness, 0.20)
    conflicts = _texts(activity.extra.get("conflicts"), "conflicts") if activity.extra.get("conflicts") is not None else ()
    if conflicts:
        confidence = max(0.05, confidence - min(0.35, 0.08 * len(conflicts)))
    sa = frame_view.get("sa") if isinstance(frame_view.get("sa"), Mapping) else {}
    predictions = frame_view.get("c_prediction") if isinstance(frame_view.get("c_prediction"), Sequence) else ()
    feelings = frame_view.get("feelings") if isinstance(frame_view.get("feelings"), Sequence) else ()
    ap_basis = {
        "sa_ref": sa.get("occurrence_id") if isinstance(sa, Mapping) else None,
        "novelty": sa.get("novelty") if isinstance(sa, Mapping) else None,
        "prediction_refs": [item.get("prediction_id") for item in predictions if isinstance(item, Mapping)][:8],
        "feelings": [item.get("name") for item in feelings if isinstance(item, Mapping)][:16],
        "attention": _mapping(frame_view.get("attention"), "attention") if isinstance(frame_view.get("attention"), Mapping) else {},
    }
    return ProjectKnowledgeProposal(
        proposal_id=_stable_id("knowledge", activity.project_id, activity.activity_id, activity.source_ref),
        project_id=activity.project_id,
        target_sections=("status", "work", "recovery"),
        summary=activity.summary,
        completed=activity.observed_completed,
        remaining=activity.observed_remaining,
        unknown=activity.observed_unknown,
        resolved_remaining=activity.observed_resolved_remaining,
        resolved_unknown=activity.observed_resolved_unknown,
        redlines=activity.observed_redlines,
        next_action=activity.observed_next_action,
        source_refs=(activity.source_ref, activity.activity_id),
        evidence_refs=tuple(dict.fromkeys((activity.source_ref, *activity.evidence_refs))),
        ap_basis=ap_basis,
        confidence=round(_clamp(confidence), 6),
        uncertainty=round(_clamp(1.0 - confidence), 6),
        conflicts=conflicts,
        status="proposed",
        extra={
            "activity_kind": activity.kind,
            "activity_status": activity.status,
            "source_completeness": activity.completeness,
        },
    )


@dataclass
class ProjectCognitionEnvironment(EnvironmentAdapter):
    """Vibe affordances for one project; it owns no cognitive state."""

    project_id: str
    environment_id: str | None = None
    staged_proposals: list[ProjectKnowledgeProposal] = field(default_factory=list)
    pending_questions: list[dict[str, Any]] = field(default_factory=list)
    learning_preferences: Mapping[str, float] = field(default_factory=dict)
    curriculum_preferences: Mapping[str, float] = field(default_factory=dict)
    active_curriculum_trials: tuple[Mapping[str, Any], ...] = ()
    pending_lessons: list[FeedbackLesson] = field(default_factory=list)
    pending_curricula: list[CurriculumCandidate] = field(default_factory=list)

    def __post_init__(self) -> None:
        _text(self.project_id, "project_id", limit=512)
        if self.environment_id is None:
            self.environment_id = f"ap-vibe:{self.project_id}"
        _text(self.environment_id, "environment_id", limit=1024)
        _mapping(self.learning_preferences, "learning_preferences")
        _mapping(self.curriculum_preferences, "curriculum_preferences")
        if isinstance(self.active_curriculum_trials, (str, bytes)) or not isinstance(self.active_curriculum_trials, Sequence):
            raise ContractError("active_curriculum_trials_must_be_sequence")
        if len(self.active_curriculum_trials) > MAX_ACTIVE_CURRICULA_PER_EPISODE:
            raise ContractError("active_curriculum_trials_exceeds_bound")

    def describe(self) -> Mapping[str, Any]:
        return {
            "environment_id": self.environment_id,
            "name": "AP-Vibe project cognition",
            "project_id": self.project_id,
            "live": False,
            "observations": ["project_activity", "user_feedback", "teacher_curriculum", "readback"],
            "actions": [
                "stage_knowledge_candidate",
                "ask_user",
                "defer",
                "observe_only",
                "record_feedback_lesson",
                "adopt_curriculum",
                "reject_curriculum",
            ],
            "formal_knowledge_write": False,
            "learning_preferences": {
                str(key): round(float(value), 6)
                for key, value in self.learning_preferences.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            },
            "curriculum_preferences": {
                str(key): round(float(value), 6)
                for key, value in self.curriculum_preferences.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            },
            "active_curriculum_trial_count": len(self.active_curriculum_trials),
            "readback": True,
        }

    @staticmethod
    def _activity(event: EventEnvelope) -> ProjectActivity | None:
        if not isinstance(event.payload_inline, Mapping):
            return None
        try:
            return ProjectActivity.from_dict(event.payload_inline)
        except (ContractError, TypeError):
            return None

    @staticmethod
    def _feedback(event: EventEnvelope) -> ProjectFeedback | None:
        if event.source != "user_feedback" or event.role != "teaching" or not isinstance(event.payload_inline, Mapping):
            return None
        try:
            return ProjectFeedback.from_dict(event.payload_inline)
        except (ContractError, TypeError):
            return None

    @staticmethod
    def _curriculum(event: EventEnvelope) -> CurriculumCandidate | None:
        if event.role != "teaching" or event.modality != "teacher_curriculum" or not isinstance(event.payload_inline, Mapping):
            return None
        try:
            return CurriculumCandidate.from_dict(event.payload_inline)
        except (ContractError, TypeError):
            return None

    def _learned(self, action_kind: str) -> float:
        raw = self.learning_preferences.get(action_kind, 0.0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return 0.0
        return round(max(-0.6, min(0.6, float(raw))), 6)

    def _curriculum_gain(self, action_kind: str) -> float:
        raw = self.curriculum_preferences.get(action_kind, 0.0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return 0.0
        return round(
            max(-CURRICULUM_CAPABILITY_LIMIT, min(CURRICULUM_CAPABILITY_LIMIT, float(raw))),
            6,
        )

    @staticmethod
    def _curriculum_reasons(curriculum: CurriculumCandidate) -> tuple[dict[str, float], tuple[str, ...]]:
        return _curriculum_eligibility_reasons(curriculum)

    def candidates(self, event: EventEnvelope, frame_view: Mapping[str, Any]) -> Sequence[ActionCandidate]:
        if event.source in {"readback", "internal"} or event.role == "result":
            return ()
        curriculum = self._curriculum(event)
        if curriculum is not None and curriculum.project_id == self.project_id:
            adjustments, reasons = self._curriculum_reasons(curriculum)
            confidence = _clamp(curriculum.confidence)
            uncertainty = _clamp(curriculum.uncertainty)
            pressure = _clamp(float(frame_view.get("pressure", 0.0) or 0.0))
            hard_invalid = any(
                reason.startswith(("unknown_action:", "adjustment_out_of_bounds:", "action_adjustments_not_", "no_explicit_"))
                or reason in {"curriculum_source_not_eligible_for_adoption", "teacher_model_receipt_missing"}
                for reason in reasons
            )
            soft_unsupported = any(
                reason in {"target_capability_not_yet_supported", "evidence_profile_not_mappable"}
                for reason in reasons
            )
            common = {
                "project_id": self.project_id,
                "curriculum": curriculum.to_dict(),
                "curriculum_ref": curriculum.curriculum_id,
                "validation_reasons": list(reasons),
                "trial_adjustments": adjustments,
            }
            offered: list[ActionCandidate] = []
            # Do not rely on ActionCandidate.eligible: the generic selector in
            # this runtime intentionally scores every offered affordance.  An
            # invalid or unsupported curriculum therefore has no adopt
            # affordance at all.
            if not reasons and curriculum.status == "staged":
                offered.append(
                    ActionCandidate(
                        candidate_id=f"action_{event.event_id}_adopt_curriculum",
                        kind="adopt_curriculum",
                        target=f"project:{self.project_id}:curriculum",
                        proposition="把这条有来源的课程作为可撤销试用，而不是正式规则",
                        components={
                            "goal_fit": _clamp(0.48 + 0.34 * confidence),
                            "evidence_fit": _clamp(0.45 + 0.45 * confidence),
                            "novelty": _clamp(0.35 + 0.25 * uncertainty),
                            "closure_gain": _clamp(0.52 + 0.20 * confidence),
                            "uncertainty_cost": 1.0 - uncertainty,
                            "pressure_relief": _clamp(0.18 + 0.22 * pressure),
                            "risk_cost": _clamp(0.08 + 0.35 * uncertainty + (0.0 if curriculum.counterexamples else 0.12)),
                        },
                        expected_outcome={"curriculum_transition": "active_trial", **common},
                        source="environment",
                        owner="mixed",
                        idempotency_key=f"ap-vibe-curriculum-adopt:{curriculum.curriculum_id}",
                        completeness="complete",
                    )
                )
            offered.extend(
                (
                    ActionCandidate(
                        candidate_id=f"action_{event.event_id}_defer_curriculum",
                        kind="defer",
                        target=f"project:{self.project_id}:curriculum",
                        proposition="保留课程及其未知，等待更多适用证据或未来器官支持",
                        components={
                            "goal_fit": _clamp(0.24 + 0.48 * uncertainty + (0.18 if soft_unsupported else 0.0)),
                            "evidence_fit": _clamp(0.22 + 0.50 * uncertainty),
                            "novelty": 0.18,
                            "closure_gain": _clamp(0.16 + 0.20 * uncertainty),
                            "uncertainty_cost": uncertainty,
                            "pressure_relief": _clamp(0.12 + 0.16 * pressure),
                            "risk_cost": 0.0,
                        },
                        expected_outcome={"curriculum_transition": "deferred", **common},
                        source="environment",
                        owner="ap_native",
                        idempotency_key=f"ap-vibe-curriculum-defer:{curriculum.curriculum_id}",
                        completeness="complete" if not reasons else "partial",
                    ),
                    ActionCandidate(
                        candidate_id=f"action_{event.event_id}_reject_curriculum",
                        kind="reject_curriculum",
                        target=f"project:{self.project_id}:curriculum",
                        proposition="拒绝当前不能安全试用的课程，同时保留原始建议和原因",
                        components={
                            "goal_fit": _clamp(0.18 + (0.58 if hard_invalid else 0.0)),
                            "evidence_fit": _clamp(0.18 + (0.70 if hard_invalid else 0.0)),
                            "novelty": 0.12,
                            "closure_gain": _clamp(0.18 + (0.55 if hard_invalid else 0.0)),
                            "uncertainty_cost": _clamp(0.25 + (0.50 if hard_invalid else 0.0)),
                            "pressure_relief": 0.10,
                            "risk_cost": 0.02,
                        },
                        expected_outcome={"curriculum_transition": "rejected", **common},
                        source="environment",
                        owner="ap_native",
                        idempotency_key=f"ap-vibe-curriculum-reject:{curriculum.curriculum_id}",
                        completeness="complete",
                    ),
                )
            )
            return tuple(offered)
        feedback = self._feedback(event)
        if feedback is not None and feedback.project_id == self.project_id:
            raw_target = event.extra.get("target_activity")
            if not isinstance(raw_target, Mapping):
                return (
                    ActionCandidate(
                        candidate_id=f"action_{event.event_id}_observe_feedback",
                        kind="observe_only",
                        target=f"project:{self.project_id}:feedback",
                        proposition="保留反馈，但目标经历不足，暂不形成学习",
                        components={
                            "goal_fit": 0.42,
                            "evidence_fit": 0.22,
                            "novelty": 0.35,
                            "closure_gain": 0.05,
                            "uncertainty_cost": 0.95,
                            "pressure_relief": 0.08,
                            "risk_cost": 0.0,
                        },
                        expected_outcome={"feedback_recorded": True, "learning_applied": False},
                        source="environment",
                        owner="ap_native",
                        idempotency_key=f"ap-vibe-feedback-observe:{self.project_id}:{feedback.feedback_id}",
                        completeness="partial",
                    ),
                )
            try:
                target_activity = ProjectActivity.from_dict(raw_target)
                lesson = lesson_from_feedback(feedback, target_activity)
            except ContractError:
                return ()
            return (
                ActionCandidate(
                    candidate_id=f"action_{event.event_id}_record_lesson",
                    kind="record_feedback_lesson",
                    target=f"project:{self.project_id}:learning",
                    proposition="记录有来源、有限适用域的反馈课程，等待 readback 后应用",
                    components={
                        "goal_fit": 0.84,
                        "evidence_fit": 0.92,
                        "novelty": 0.42,
                        "closure_gain": 0.62,
                        "uncertainty_cost": 0.88,
                        "pressure_relief": 0.48,
                        "risk_cost": 0.06,
                    },
                    expected_outcome={"feedback_lesson": lesson.to_dict(), "project_id": self.project_id},
                    source="environment",
                    owner="mixed",
                    idempotency_key=f"ap-vibe-lesson:{lesson.lesson_id}",
                    completeness="complete",
                ),
                ActionCandidate(
                    candidate_id=f"action_{event.event_id}_defer_feedback",
                    kind="defer",
                    target=f"project:{self.project_id}:learning",
                    proposition="暂不应用这次反馈，保留为待解释经历",
                    components={
                        "goal_fit": 0.18,
                        "evidence_fit": 0.28,
                        "novelty": 0.12,
                        "closure_gain": 0.06,
                        "uncertainty_cost": 0.40,
                        "pressure_relief": 0.08,
                        "risk_cost": 0.0,
                    },
                    expected_outcome={"feedback_deferred": True, "project_id": self.project_id},
                    source="environment",
                    owner="ap_native",
                    idempotency_key=f"ap-vibe-feedback-defer:{self.project_id}:{feedback.feedback_id}",
                    completeness="complete",
                ),
            )
        activity = self._activity(event)
        if activity is None or activity.project_id != self.project_id:
            return (
                ActionCandidate(
                    candidate_id=f"action_{event.event_id}_observe",
                    kind="observe_only",
                    target=f"project:{self.project_id}",
                    proposition="保留不能安全解释的项目事件，不改变项目知识",
                    components={
                        "goal_fit": 0.34,
                        "evidence_fit": 0.22,
                        "novelty": 0.35,
                        "closure_gain": 0.05,
                        "uncertainty_cost": 0.95,
                        "pressure_relief": 0.08,
                        "risk_cost": 0.0,
                    },
                    expected_outcome={"observed": True, "event_ref": event.event_id},
                    source="environment",
                    owner="ap_native",
                    idempotency_key=f"ap-vibe-observe:{self.project_id}:{event.event_id}",
                    completeness="partial",
                ),
            )

        proposal = proposal_from_activity(activity, frame_view)
        uncertainty = proposal.uncertainty
        sa = frame_view.get("sa") if isinstance(frame_view.get("sa"), Mapping) else {}
        novelty = _clamp(float(sa.get("novelty", 0.0) or 0.0)) if isinstance(sa, Mapping) else 0.0
        pressure = _clamp(float(frame_view.get("pressure", 0.0) or 0.0))
        unresolved_count = len(proposal.unknown) + len(proposal.conflicts)
        unresolved_ratio = _clamp(unresolved_count / 4.0)
        stage_risk = _clamp(0.04 + 0.12 * unresolved_ratio)
        common = {
            "project_id": self.project_id,
            "activity_ref": activity.activity_id,
            "event_ref": event.event_id,
        }
        questions = [*proposal.unknown, *proposal.conflicts]
        if not questions:
            questions = ["这条进度还缺少哪些会影响下一步的事实？"]
        return (
            ActionCandidate(
                candidate_id=f"action_{event.event_id}_stage",
                kind="stage_knowledge_candidate",
                target=f"project:{self.project_id}:knowledge-candidates",
                proposition="把有来源的工程活动暂存为待审项目知识，不覆盖正式知识",
                components={
                    "goal_fit": 0.78,
                    "evidence_fit": proposal.confidence,
                    "novelty": novelty,
                    "closure_gain": _clamp(0.60 - 0.32 * unresolved_ratio),
                    "uncertainty_cost": 1.0 - uncertainty,
                    "pressure_relief": _clamp(0.24 + pressure * 0.32),
                    "risk_cost": stage_risk,
                    "learned_preference": self._learned("stage_knowledge_candidate"),
                    "curriculum_trial": self._curriculum_gain("stage_knowledge_candidate"),
                },
                expected_outcome={"knowledge_proposal": proposal.to_dict(), **common},
                source="environment",
                owner="mixed",
                idempotency_key=f"ap-vibe-stage:{proposal.proposal_id}",
                completeness="complete" if activity.completeness == "complete" else "partial",
            ),
            ActionCandidate(
                candidate_id=f"action_{event.event_id}_ask",
                kind="ask_user",
                target=f"project:{self.project_id}:human-feedback",
                proposition="向用户询问能真正补齐当前未知或冲突的最小问题",
                components={
                    "goal_fit": _clamp(0.44 + 0.42 * uncertainty),
                    "evidence_fit": uncertainty,
                    "novelty": _clamp(0.18 + novelty * 0.30),
                    "closure_gain": _clamp(0.18 + 0.72 * max(uncertainty, unresolved_ratio)),
                    "uncertainty_cost": uncertainty,
                    "pressure_relief": _clamp(0.12 + pressure * 0.42),
                    "risk_cost": 0.02,
                    "learned_preference": self._learned("ask_user"),
                    "curriculum_trial": self._curriculum_gain("ask_user"),
                },
                expected_outcome={"questions": questions[:8], **common},
                source="environment",
                owner="mixed",
                idempotency_key=f"ap-vibe-ask:{self.project_id}:{event.event_id}",
                completeness="complete",
            ),
            ActionCandidate(
                candidate_id=f"action_{event.event_id}_defer",
                kind="defer",
                target=f"project:{self.project_id}:frontier",
                proposition="保留未闭合状态，等待更多环境证据",
                components={
                    "goal_fit": _clamp(0.20 + 0.18 * uncertainty),
                    "evidence_fit": uncertainty,
                    "novelty": novelty * 0.15,
                    "closure_gain": 0.08,
                    "uncertainty_cost": uncertainty,
                    "pressure_relief": pressure * 0.20,
                    "risk_cost": 0.0,
                    "learned_preference": self._learned("defer"),
                    "curriculum_trial": self._curriculum_gain("defer"),
                },
                expected_outcome={"deferred": True, **common},
                source="environment",
                owner="ap_native",
                idempotency_key=f"ap-vibe-defer:{self.project_id}:{event.event_id}",
                completeness="complete",
            ),
            ActionCandidate(
                candidate_id=f"action_{event.event_id}_observe",
                kind="observe_only",
                target=f"project:{self.project_id}:activity",
                proposition="仅保留这次活动和认知结果，不生成知识更新",
                components={
                    "goal_fit": 0.28,
                    "evidence_fit": _clamp(0.40 + 0.28 * proposal.confidence),
                    "novelty": novelty * 0.28,
                    "closure_gain": 0.12,
                    "uncertainty_cost": _clamp(0.55 + 0.35 * uncertainty),
                    "pressure_relief": 0.10,
                    "risk_cost": 0.0,
                    "learned_preference": self._learned("observe_only"),
                    "curriculum_trial": self._curriculum_gain("observe_only"),
                },
                expected_outcome={"observed": True, **common},
                source="environment",
                owner="ap_native",
                idempotency_key=f"ap-vibe-observe:{self.project_id}:{event.event_id}",
                completeness="complete",
            ),
        )

    def dispatch(self, candidate: ActionCandidate, idempotency_key: str) -> DispatchReceipt:
        now = utc_now()
        extra: dict[str, Any] = {"kind": candidate.kind}
        if candidate.kind == "stage_knowledge_candidate":
            raw = candidate.expected_outcome.get("knowledge_proposal")
            if not isinstance(raw, Mapping):
                return DispatchReceipt(
                    action_ref=candidate.candidate_id,
                    environment_id=str(self.environment_id),
                    idempotency_key=idempotency_key,
                    status="rejected",
                    accepted_at=now,
                    connector_ref="ap-vibe-local",
                    error_code="knowledge_proposal_missing",
                    retryable=False,
                )
            proposal = ProjectKnowledgeProposal.from_dict(raw).with_status("staged")
            if not any(item.proposal_id == proposal.proposal_id for item in self.staged_proposals):
                self.staged_proposals.append(proposal)
            extra["knowledge_proposal"] = proposal.to_dict()
        elif candidate.kind == "ask_user":
            questions = candidate.expected_outcome.get("questions", ())
            item = {
                "action_ref": candidate.candidate_id,
                "project_id": self.project_id,
                "questions": list(questions)[:8] if isinstance(questions, Sequence) and not isinstance(questions, (str, bytes)) else [],
            }
            if not any(entry.get("action_ref") == candidate.candidate_id for entry in self.pending_questions):
                self.pending_questions.append(item)
            extra["question_request"] = item
        elif candidate.kind == "record_feedback_lesson":
            raw = candidate.expected_outcome.get("feedback_lesson")
            if not isinstance(raw, Mapping):
                return DispatchReceipt(
                    action_ref=candidate.candidate_id,
                    environment_id=str(self.environment_id),
                    idempotency_key=idempotency_key,
                    status="rejected",
                    accepted_at=now,
                    connector_ref="ap-vibe-local",
                    error_code="feedback_lesson_missing",
                    retryable=False,
                )
            lesson = FeedbackLesson.from_dict(raw).with_status("recorded")
            if not any(item.lesson_id == lesson.lesson_id for item in self.pending_lessons):
                self.pending_lessons.append(lesson)
            extra["feedback_lesson"] = lesson.to_dict()
        elif candidate.kind in {"adopt_curriculum", "reject_curriculum"} or (
            candidate.kind == "defer" and isinstance(candidate.expected_outcome.get("curriculum"), Mapping)
        ):
            raw = candidate.expected_outcome.get("curriculum")
            transition = candidate.expected_outcome.get("curriculum_transition")
            if not isinstance(raw, Mapping) or transition not in {"active_trial", "deferred", "rejected"}:
                return DispatchReceipt(
                    action_ref=candidate.candidate_id,
                    environment_id=str(self.environment_id),
                    idempotency_key=idempotency_key,
                    status="rejected",
                    accepted_at=now,
                    connector_ref="ap-vibe-local",
                    error_code="curriculum_transition_missing",
                    retryable=False,
                )
            curriculum = CurriculumCandidate.from_dict(raw)
            if not any(item.curriculum_id == curriculum.curriculum_id for item in self.pending_curricula):
                self.pending_curricula.append(curriculum)
            extra["curriculum"] = curriculum.to_dict()
            extra["curriculum_transition"] = transition
            extra["validation_reasons"] = list(candidate.expected_outcome.get("validation_reasons", ()))[:32]
            extra["trial_adjustments"] = _mapping(candidate.expected_outcome.get("trial_adjustments"), "trial_adjustments")
        elif candidate.kind not in {"defer", "observe_only"}:
            return DispatchReceipt(
                action_ref=candidate.candidate_id,
                environment_id=str(self.environment_id),
                idempotency_key=idempotency_key,
                status="rejected",
                accepted_at=now,
                connector_ref="ap-vibe-local",
                error_code="project_action_not_supported",
                retryable=False,
            )
        return DispatchReceipt(
            action_ref=candidate.candidate_id,
            environment_id=str(self.environment_id),
            idempotency_key=idempotency_key,
            status="accepted",
            accepted_at=now,
            connector_ref="ap-vibe-local",
            evidence_refs=(f"ap-vibe-readback:{candidate.candidate_id}",),
            extra=extra,
        )

    def readback(self, receipt: DispatchReceipt) -> ResultEvent:
        if receipt.status != "accepted":
            return ResultEvent(
                receipt_ref=receipt.receipt_id,
                action_ref=receipt.action_ref,
                environment_id=str(self.environment_id),
                status="unknown",
                completeness="unknown",
                error_code=receipt.error_code or "project_dispatch_not_accepted",
                retryable=receipt.retryable,
            )
        payload: dict[str, Any] = {
            "action_kind": receipt.extra.get("kind"),
            "project_id": self.project_id,
            "formal_knowledge_changed": False,
        }
        if isinstance(receipt.extra.get("knowledge_proposal"), Mapping):
            payload["knowledge_proposal"] = receipt.extra["knowledge_proposal"]
            payload["proposal_staged"] = True
        if isinstance(receipt.extra.get("question_request"), Mapping):
            payload["question_request"] = receipt.extra["question_request"]
            payload["awaiting_user"] = True
        if isinstance(receipt.extra.get("feedback_lesson"), Mapping):
            payload["feedback_lesson"] = receipt.extra["feedback_lesson"]
            payload["lesson_recorded"] = True
            payload["learning_applied"] = False
        if isinstance(receipt.extra.get("curriculum"), Mapping):
            payload["curriculum"] = receipt.extra["curriculum"]
            payload["curriculum_transition"] = receipt.extra.get("curriculum_transition")
            payload["validation_reasons"] = list(receipt.extra.get("validation_reasons", ()))[:32]
            payload["trial_adjustments"] = _mapping(receipt.extra.get("trial_adjustments"), "trial_adjustments")
            payload["curriculum_recorded"] = True
            payload["learning_applied"] = False
        return ResultEvent(
            receipt_ref=receipt.receipt_id,
            action_ref=receipt.action_ref,
            environment_id=str(self.environment_id),
            status="success",
            observed_at=utc_now(),
            payload_inline=payload,
            evidence_refs=receipt.evidence_refs,
            completeness="complete",
            lineage_refs=(receipt.receipt_id, receipt.action_ref),
        )


@dataclass(frozen=True)
class ProjectEpisodeRun:
    """A small return object used by projections and focused episodes."""

    activity: ProjectActivity
    episode_id: str
    results: tuple[TickResult, ...]
    staged_proposals: tuple[ProjectKnowledgeProposal, ...]
    pending_questions: tuple[Mapping[str, Any], ...]
    provider_mode: str
    learned_preferences: Mapping[str, float] = field(default_factory=dict)
    curriculum_preferences: Mapping[str, float] = field(default_factory=dict)
    curriculum_trials: tuple[Mapping[str, Any], ...] = ()
    cognitive_curriculum_trials: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)
    curriculum_attempt: CurriculumAttempt | None = None
    curriculum_attempts: tuple[CurriculumAttempt, ...] = ()
    curriculum_runs: tuple[CurriculumEpisodeRun, ...] = ()
    learning_snapshot: Mapping[str, Any] = field(default_factory=dict)
    teacher_sampling: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FeedbackEpisodeRun:
    """One feedback teaching episode plus its post-readback learning effect."""

    feedback: ProjectFeedback
    episode_id: str
    results: tuple[TickResult, ...]
    lesson: FeedbackLesson | None
    curriculum_outcome: CurriculumOutcome | None
    learning_snapshot: Mapping[str, Any]
    provider_mode: str


@dataclass(frozen=True)
class CurriculumEpisodeRun:
    """One curriculum proposal processed through the complete AP flow."""

    curriculum: CurriculumCandidate
    episode_id: str
    results: tuple[TickResult, ...]
    outcome: CurriculumOutcome | None
    learning_snapshot: Mapping[str, Any]
    provider_mode: str


def _effects_from_results(
    results: Sequence[TickResult],
) -> tuple[tuple[ProjectKnowledgeProposal, ...], tuple[Mapping[str, Any], ...]]:
    """Rebuild environment effects from durable result-back payloads.

    The environment's Python lists are only a live convenience.  Receipts and
    result events are the recovery authority, so a cold replay must produce
    the same staged proposal/question projection without dispatching again.
    """

    proposals: dict[str, ProjectKnowledgeProposal] = {}
    questions: dict[str, dict[str, Any]] = {}
    for item in results:
        result = item.result
        payload = result.payload_inline if result is not None else None
        if not isinstance(payload, Mapping):
            continue
        raw_proposal = payload.get("knowledge_proposal")
        if isinstance(raw_proposal, Mapping):
            try:
                proposal = ProjectKnowledgeProposal.from_dict(raw_proposal)
            except ContractError:
                pass
            else:
                proposals.setdefault(proposal.proposal_id, proposal)
        raw_question = payload.get("question_request")
        if isinstance(raw_question, Mapping):
            action_ref = raw_question.get("action_ref")
            if isinstance(action_ref, str) and action_ref:
                questions.setdefault(action_ref, _mapping(raw_question, "question_request"))
    return tuple(proposals.values()), tuple(questions.values())


def _selected_action_kind(results: Sequence[TickResult]) -> str | None:
    if not results:
        return None
    frame = results[0].frame
    selected_ref = frame.decision.get("selected_candidate_ref")
    return next(
        (
            str(item.get("kind"))
            for item in frame.actions
            if item.get("candidate_id") == selected_ref and isinstance(item.get("kind"), str)
        ),
        None,
    )


def _provider_mode(results: Sequence[TickResult]) -> str:
    first_gateway = results[0].frame.gateway if results else {}
    if isinstance(first_gateway, Mapping):
        limitations = first_gateway.get("limitations")
        provider_off = (
            first_gateway.get("status") == "unavailable"
            and isinstance(limitations, Sequence)
            and not isinstance(limitations, (str, bytes))
            and (
                "no_provider_configured" in limitations
                or "gateway_not_consulted_for_control_event" in limitations
                or "locally_withheld_by_sampling" in limitations
            )
        )
        if provider_off and "locally_withheld_by_sampling" in limitations:
            return "locally_withheld_by_sampling"
        return "provider_off" if provider_off else str(first_gateway.get("source", "unknown"))
    return "unknown"


def run_curriculum_episode(
    database_path: str | Path,
    curriculum: CurriculumCandidate,
    *,
    learning_ledger: ProjectLearningLedger,
    max_result_ticks: int = 2,
) -> CurriculumEpisodeRun:
    """Stage then process one curriculum event; no gateway is consulted."""

    if not isinstance(curriculum, CurriculumCandidate):
        raise ContractError("curriculum_episode_requires_curriculum")
    if not isinstance(learning_ledger, ProjectLearningLedger):
        raise ContractError("curriculum_episode_requires_learning_ledger")
    if isinstance(max_result_ticks, bool) or not 1 <= int(max_result_ticks) <= 8:
        raise ContractError("curriculum_result_tick_budget_out_of_bounds")
    curriculum = learning_ledger.stage_curriculum(curriculum)
    database = Path(database_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    episode_id = _stable_id("curriculum_episode", curriculum.project_id, curriculum.curriculum_id)
    environment = ProjectCognitionEnvironment(curriculum.project_id)
    results: list[TickResult] = []
    with EventStore(database) as store:
        runtime = MindRuntime(
            store,
            environment,
            runtime_id=f"ap-vibe-runtime:{curriculum.project_id}",
            organism_id=f"ap-vibe-organism:{curriculum.project_id}",
            episode_id=episode_id,
            gateway=NullGateway(),
            action_weights={**MindRuntime.DEFAULT_WEIGHTS, "learned_preference": 1.0, "curriculum_trial": 1.0},
            max_internal_ticks=4,
            max_wake_attempts=8,
        )
        current = runtime.tick(
            curriculum.as_event(
                runtime_id=runtime.runtime_id,
                organism_id=runtime.organism_id,
                episode_id=episode_id,
            )
        )
        results.append(current)
        result_back_seen = False
        for _ in range(int(max_result_ticks)):
            if current.result_envelope is None:
                break
            current = runtime.tick(current.result_envelope)
            results.append(current)
            result_back_seen = True

    outcome: CurriculumOutcome | None = None
    first = results[0] if results else None
    result_payload = first.result.payload_inline if first is not None and first.result is not None else None
    transition = result_payload.get("curriculum_transition") if isinstance(result_payload, Mapping) else None
    selected_kind = _selected_action_kind(results)
    expected = {
        "adopt_curriculum": "active_trial",
        "defer": "deferred",
        "reject_curriculum": "rejected",
    }.get(selected_kind or "")
    # A dispatch result is only an observation.  Require its envelope to have
    # passed through the next AP tick before mutating the learning projection.
    if result_back_seen and isinstance(transition, str) and transition == expected:
        outcome = CurriculumOutcome(
            outcome_id=_stable_id("curriculum_outcome", curriculum.curriculum_id, episode_id, transition),
            curriculum_id=curriculum.curriculum_id,
            project_id=curriculum.project_id,
            outcome=transition,
            resulting_status=transition,
            source_ref=first.result.result_id if first is not None and first.result is not None else episode_id,
            episode_ref=episode_id,
            evidence_refs=(
                first.result.result_id if first is not None and first.result is not None else episode_id,
            ),
            rationale=f"AP selected {selected_kind}; physical readback re-entered cognition",
            created_at=(
                first.result.observed_at
                if first is not None and first.result is not None
                else curriculum.created_at
            ),
        )
        if transition == "active_trial":
            outcome, _ = learning_ledger.activate_curriculum(outcome)
        else:
            outcome = learning_ledger.record_curriculum_outcome(outcome)
        current_curriculum = learning_ledger.get_curriculum(curriculum.curriculum_id)
        curriculum = current_curriculum if current_curriculum is not None else curriculum.with_status(outcome.resulting_status)
    return CurriculumEpisodeRun(
        curriculum=curriculum,
        episode_id=episode_id,
        results=tuple(results),
        outcome=outcome,
        learning_snapshot=learning_ledger.snapshot(curriculum.project_id),
        provider_mode=_provider_mode(results),
    )


def run_project_episode(
    database_path: str | Path,
    activity: ProjectActivity,
    *,
    gateway: HybridGateway | None = None,
    governance: GovernanceCompatibilityRecord | None = None,
    capability: CapabilityOwnership | None = None,
    learning_ledger: ProjectLearningLedger | None = None,
    teacher_capabilities: Sequence[str] | None = None,
    excluded_memory_activity_ids: Sequence[str] = (),
    max_result_ticks: int = 2,
) -> ProjectEpisodeRun:
    """Run one project activity and bounded result-back through MindRuntime."""

    if not isinstance(activity, ProjectActivity):
        raise ContractError("project_episode_requires_activity")
    if isinstance(max_result_ticks, bool) or not 0 <= int(max_result_ticks) <= 8:
        raise ContractError("project_result_tick_budget_out_of_bounds")
    database = Path(database_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    episode_id = _stable_id("project_episode", activity.project_id, activity.activity_id)
    learned_preferences = learning_ledger.preferences(activity) if learning_ledger is not None else {}
    curriculum_preferences: Mapping[str, float] = {}
    curriculum_trials: tuple[Mapping[str, Any], ...] = ()
    memory_events: tuple[EventEnvelope, ...] = ()
    cognitive_trials: Mapping[str, tuple[Mapping[str, Any], ...]] = {}
    teacher_sampling: Mapping[str, Any] = {}
    if learning_ledger is not None:
        curriculum_preferences, curriculum_trials = learning_ledger.active_curriculum_trials(activity)
        memory_events = learning_ledger.memory_events(
            activity.project_id,
            exclude_activity_id=activity.activity_id,
            excluded_activity_ids=excluded_memory_activity_ids,
            privacy_scope=activity.privacy_scope,
            limit=32,
        )
        cognitive_trials = learning_ledger.active_cognitive_curriculum_trials(activity, memory_events)
    attention_policy = (
        learning_ledger.attention_policy_from_trials(cognitive_trials)
        if learning_ledger is not None
        else AttentionPolicy()
    )
    environment = ProjectCognitionEnvironment(
        activity.project_id,
        learning_preferences=learned_preferences,
        curriculum_preferences=curriculum_preferences,
        active_curriculum_trials=curriculum_trials,
    )
    provider = gateway or NullGateway()
    requested_capabilities: tuple[str, ...] | None = None
    if learning_ledger is not None and not isinstance(provider, NullGateway):
        requested_source = (
            tuple(sorted(CURRICULUM_SUPPORTED_CAPABILITIES))
            if teacher_capabilities is None
            else teacher_capabilities
        )
        teacher_sampling = learning_ledger.teacher_sampling_plan(
            activity.project_id,
            episode_id,
            capabilities=requested_source,
        )
        requested_capabilities = tuple(
            str(item)
            for item in teacher_sampling.get("requested_capabilities", ())
            if isinstance(item, str)
        )
        if not requested_capabilities:
            provider = NullGateway(
                limitation="locally_withheld_by_sampling",
                source="local_sampling",
            )
    results: list[TickResult] = []
    with EventStore(database) as store:
        runtime = MindRuntime(
            store,
            environment,
            runtime_id=f"ap-vibe-runtime:{activity.project_id}",
            organism_id=f"ap-vibe-organism:{activity.project_id}",
            episode_id=episode_id,
            gateway=provider,
            governance=governance,
            capability=capability,
            action_weights={
                **MindRuntime.DEFAULT_WEIGHTS,
                "learned_preference": 1.0,
                "curriculum_trial": 1.0,
            },
            max_internal_ticks=4,
            max_wake_attempts=8,
            external_memory_events=memory_events,
            cognitive_curriculum_trials=cognitive_trials,
            attention_policy=attention_policy,
            requested_capabilities=requested_capabilities,
            teacher_sampling=teacher_sampling,
        )
        current = runtime.tick(
            activity.as_event(
                runtime_id=runtime.runtime_id,
                organism_id=runtime.organism_id,
                episode_id=episode_id,
            )
        )
        results.append(current)
        for _ in range(int(max_result_ticks)):
            if current.result_envelope is None:
                break
            current = runtime.tick(current.result_envelope)
            results.append(current)
    durable_proposals, durable_questions = _effects_from_results(results)
    provider_mode = _provider_mode(results)
    curriculum_attempts: list[CurriculumAttempt] = []
    curriculum_attempt: CurriculumAttempt | None = None
    if learning_ledger is not None and curriculum_trials and results:
        selected_kind = _selected_action_kind(results)
        selected_score = next(
            (
                item
                for item in results[0].frame.actions
                if item.get("candidate_id") == results[0].frame.decision.get("selected_candidate_ref")
            ),
            None,
        )
        selected_trial = (
            float(selected_score.get("components", {}).get("curriculum_trial", 0.0))
            if isinstance(selected_score, Mapping) and isinstance(selected_score.get("components"), Mapping)
            else 0.0
        )
        trial = next(
            (
                item for item in curriculum_trials
                if isinstance(item, Mapping)
                and isinstance(item.get("action_adjustments"), Mapping)
                and selected_kind in item.get("action_adjustments", {})
            ),
            None,
        )
        if trial is None:
            # The trial still participated in the ordinary competition even if
            # another candidate won.  Attribute one bounded withheld attempt
            # to the most recent applicable curriculum, never all of them.
            trial = next((item for item in curriculum_trials if isinstance(item, Mapping)), None)
        if trial is not None:
            adjustments = trial.get("action_adjustments")
            if isinstance(adjustments, Mapping):
                target_action = next(
                    (
                        str(action)
                        for action, delta in adjustments.items()
                        if isinstance(action, str)
                        and action in CURRICULUM_TARGET_ACTIONS
                        and isinstance(delta, (int, float))
                        and not isinstance(delta, bool)
                        and float(delta) > 0.0
                    ),
                    next((str(action) for action in adjustments if action in CURRICULUM_TARGET_ACTIONS), "observe_only"),
                )
                contribution_raw = adjustments.get(target_action, 0.0)
                contribution = (
                    float(contribution_raw)
                    if isinstance(contribution_raw, (int, float)) and not isinstance(contribution_raw, bool)
                    else 0.0
                )
                curriculum_attempt = CurriculumAttempt(
                    attempt_id=_stable_id("curriculum_attempt", str(trial.get("curriculum_id")), episode_id),
                    curriculum_id=str(trial.get("curriculum_id")),
                    project_id=activity.project_id,
                    target_capability=str(trial.get("target_capability", CURRICULUM_TARGET_CAPABILITY)),
                    activity_episode_ref=episode_id,
                    activity_ref=activity.activity_id,
                    feature_key=str((trial.get("feature_keys") or ("unknown",))[0]),
                    target_action=target_action,
                    observed_winner=selected_kind,
                    contribution=round(max(-CURRICULUM_SINGLE_TRIAL_LIMIT, min(CURRICULUM_SINGLE_TRIAL_LIMIT, contribution)), 6),
                    status="attempted" if selected_kind == target_action and abs(selected_trial) > 0.0 else "withheld",
                    created_at=(
                        results[0].result.observed_at
                        if results[0].result is not None
                        else activity.occurred_at
                    ),
                    extra={"trial_applied_to_score": True},
                )
                curriculum_attempt = learning_ledger.record_curriculum_attempt(curriculum_attempt)
                curriculum_attempts.append(curriculum_attempt)

    if learning_ledger is not None and results:
        first_frame = results[0].frame
        for capability_key, trials in cognitive_trials.items():
            if not trials:
                continue
            if capability_key == CURRICULUM_RECALL_CAPABILITY:
                used = [item for item in first_frame.b_recall if item.curriculum_refs]
                if used:
                    winner = max(used, key=lambda item: abs(item.curriculum_gain))
                    curriculum_id = winner.curriculum_refs[0]
                    effect_key = winner.event_ref
                    contribution = winner.curriculum_gain
                    status = "attempted"
                    local_before = {"memory_ref": winner.event_ref, "score": winner.base_score}
                    effective_result = {"memory_ref": winner.event_ref, "score": winner.score}
                    usage_status = "entered_b_recall"
                    curriculum_refs = list(winner.curriculum_refs)
                else:
                    observations = first_frame.cognitive_trial_observations.get(capability_key, ())
                    withheld = next(
                        (
                            item for item in observations
                            if isinstance(item, Mapping)
                            and item.get("status") == "suppressed_below_zero"
                            and isinstance(item.get("curriculum_refs"), Sequence)
                            and not isinstance(item.get("curriculum_refs"), (str, bytes))
                            and item.get("curriculum_refs")
                        ),
                        None,
                    )
                    if withheld is None:
                        continue
                    curriculum_refs = [str(item) for item in withheld.get("curriculum_refs", ()) if isinstance(item, str)]
                    curriculum_id = curriculum_refs[0]
                    effect_key = str(withheld.get("effect_key") or "unknown")
                    contribution = float(withheld.get("curriculum_gain", 0.0))
                    status = "withheld"
                    local_before = {"memory_ref": effect_key, "score": withheld.get("base_score")}
                    effective_result = {"memory_ref": effect_key, "score": withheld.get("final_score")}
                    usage_status = "suppressed_below_zero"
                attempt = CurriculumAttempt(
                    attempt_id=_stable_id("curriculum_attempt", curriculum_id, episode_id, capability_key),
                    curriculum_id=curriculum_id,
                    project_id=activity.project_id,
                    target_capability=capability_key,
                    activity_episode_ref=episode_id,
                    activity_ref=activity.activity_id,
                    feature_key=str((trials[0].get("feature_keys") or ("unknown",))[0]),
                    target_action=None,
                    observed_winner=_selected_action_kind(results),
                    contribution=contribution,
                    status=status,
                    effect_key=effect_key,
                    local_before=local_before,
                    effective_result=effective_result,
                    created_at=activity.occurred_at,
                    extra={"usage_status": usage_status, "curriculum_refs": curriculum_refs},
                )
                curriculum_attempts.append(learning_ledger.record_curriculum_attempt(attempt))
            elif capability_key == CURRICULUM_APPRAISAL_CAPABILITY:
                used = [item for item in first_frame.feelings if item.curriculum_refs]
                if used:
                    winner = max(used, key=lambda item: abs(item.curriculum_delta))
                    curriculum_id = winner.curriculum_refs[0]
                    effect_key = winner.name
                    contribution = winner.curriculum_delta
                    status = "attempted"
                    local_before = {"feeling": winner.name, "intensity": winner.base_intensity}
                    effective_result = {"feeling": winner.name, "intensity": winner.intensity}
                    usage_status = "entered_appraisal"
                    curriculum_refs = list(winner.curriculum_refs)
                else:
                    observations = first_frame.cognitive_trial_observations.get(capability_key, ())
                    withheld = next(
                        (
                            item for item in observations
                            if isinstance(item, Mapping)
                            and str(item.get("status", "")).startswith(("withheld_", "suppressed_"))
                            and isinstance(item.get("curriculum_refs"), Sequence)
                            and not isinstance(item.get("curriculum_refs"), (str, bytes))
                            and item.get("curriculum_refs")
                        ),
                        None,
                    )
                    if withheld is None:
                        continue
                    curriculum_refs = [str(item) for item in withheld.get("curriculum_refs", ()) if isinstance(item, str)]
                    curriculum_id = curriculum_refs[0]
                    effect_key = str(withheld.get("effect_key") or "unknown")
                    contribution = float(withheld.get("curriculum_delta", 0.0))
                    status = "withheld"
                    local_before = {
                        "feeling": effect_key,
                        "intensity": withheld.get("base_intensity"),
                        "observed_signals": list(withheld.get("observed_signals", ())),
                    }
                    effective_result = {
                        "feeling": effect_key,
                        "intensity": withheld.get("final_intensity"),
                        "required_signals": list(withheld.get("required_signals", ())),
                    }
                    usage_status = str(withheld.get("status") or "withheld")
                attempt = CurriculumAttempt(
                    attempt_id=_stable_id("curriculum_attempt", curriculum_id, episode_id, capability_key),
                    curriculum_id=curriculum_id,
                    project_id=activity.project_id,
                    target_capability=capability_key,
                    activity_episode_ref=episode_id,
                    activity_ref=activity.activity_id,
                    feature_key=str((trials[0].get("feature_keys") or ("unknown",))[0]),
                    target_action=None,
                    observed_winner=_selected_action_kind(results),
                    contribution=contribution,
                    status=status,
                    effect_key=effect_key,
                    local_before=local_before,
                    effective_result=effective_result,
                    created_at=activity.occurred_at,
                    extra={"usage_status": usage_status, "curriculum_refs": curriculum_refs},
                )
                curriculum_attempts.append(learning_ledger.record_curriculum_attempt(attempt))
            elif capability_key in {
                CURRICULUM_PREDICTION_CAPABILITY,
                CURRICULUM_THOUGHT_CAPABILITY,
            }:
                observations = first_frame.cognitive_trial_observations.get(capability_key, ())
                entered = next(
                    (
                        item
                        for item in observations
                        if isinstance(item, Mapping)
                        and str(item.get("status", "")).startswith("entered_")
                        and isinstance(item.get("curriculum_refs"), Sequence)
                        and not isinstance(item.get("curriculum_refs"), (str, bytes))
                        and item.get("curriculum_refs")
                    ),
                    None,
                )
                if entered is None:
                    continue
                curriculum_refs = [
                    str(item)
                    for item in entered.get("curriculum_refs", ())
                    if isinstance(item, str) and item
                ]
                if not curriculum_refs:
                    continue
                curriculum_id = curriculum_refs[0]
                effect_key = str(entered.get("effect_key") or "unknown")
                contribution = float(entered.get("curriculum_delta", 0.0) or 0.0)
                if capability_key == CURRICULUM_PREDICTION_CAPABILITY:
                    local_before = {
                        "hypothesis": entered.get("hypothesis"),
                        "confidence": entered.get("base_confidence"),
                    }
                    effective_result = {
                        "hypothesis": entered.get("hypothesis"),
                        "confidence": entered.get("final_confidence"),
                        "mode": entered.get("mode"),
                    }
                    usage_status = "entered_c_prediction"
                else:
                    local_before = {
                        "proposition": entered.get("base_proposition"),
                    }
                    effective_result = {
                        "proposition": entered.get("final_proposition"),
                        "curriculum_additions": list(entered.get("curriculum_additions", ())),
                    }
                    usage_status = "entered_thought_stream"
                attempt = CurriculumAttempt(
                    attempt_id=_stable_id("curriculum_attempt", curriculum_id, episode_id, capability_key),
                    curriculum_id=curriculum_id,
                    project_id=activity.project_id,
                    target_capability=capability_key,
                    activity_episode_ref=episode_id,
                    activity_ref=activity.activity_id,
                    feature_key=str((trials[0].get("feature_keys") or ("unknown",))[0]),
                    target_action=None,
                    observed_winner=_selected_action_kind(results),
                    contribution=round(
                        max(-CURRICULUM_SINGLE_TRIAL_LIMIT, min(CURRICULUM_SINGLE_TRIAL_LIMIT, contribution)),
                        6,
                    ),
                    status="attempted",
                    effect_key=effect_key,
                    local_before=local_before,
                    effective_result=effective_result,
                    created_at=activity.occurred_at,
                    extra={"usage_status": usage_status, "curriculum_refs": curriculum_refs},
                )
                curriculum_attempts.append(learning_ledger.record_curriculum_attempt(attempt))
            elif capability_key in {
                CURRICULUM_PARADIGM_CAPABILITY,
                CURRICULUM_ATTENTION_CAPABILITY,
                CURRICULUM_EXPRESSION_CAPABILITY,
                CURRICULUM_PARAMETER_CAPABILITY,
            }:
                observations = first_frame.cognitive_trial_observations.get(capability_key, ())
                entered = next(
                    (
                        item
                        for item in observations
                        if isinstance(item, Mapping)
                        and isinstance(item.get("curriculum_refs"), Sequence)
                        and not isinstance(item.get("curriculum_refs"), (str, bytes))
                        and item.get("curriculum_refs")
                        and str(item.get("status", "")).startswith(("entered_", "withheld_"))
                    ),
                    None,
                )
                if entered is None:
                    continue
                curriculum_refs = [
                    str(item)
                    for item in entered.get("curriculum_refs", ())
                    if isinstance(item, str) and item
                ]
                if not curriculum_refs:
                    continue
                curriculum_id = curriculum_refs[0]
                effect_key = str(entered.get("effect_key") or "unknown")
                contribution = float(entered.get("curriculum_delta", 0.0) or 0.0)
                entered_status = str(entered.get("status") or "unknown")
                if capability_key == CURRICULUM_PARADIGM_CAPABILITY:
                    local_before = {
                        "match": entered.get("base_match"),
                        "bindings": dict(entered.get("bindings", {}))
                        if isinstance(entered.get("bindings"), Mapping)
                        else {},
                        "missing_slots": list(entered.get("missing_slots", ())),
                        "mismatched_invariants": list(entered.get("mismatched_invariants", ())),
                    }
                    effective_result = {
                        "match": entered.get("final_match"),
                        "occurrence_ref": entered.get("occurrence_ref"),
                        "status": entered_status,
                    }
                    usage_status = entered_status
                    status = "attempted" if entered_status == "entered_paradigm" else "withheld"
                elif capability_key == CURRICULUM_ATTENTION_CAPABILITY:
                    target_ref = entered.get("target_ref")
                    attention_action = next(
                        (
                            item
                            for item in first_frame.actions
                            if isinstance(item, Mapping)
                            and item.get("kind") == entered.get("mode")
                            and item.get("target") == target_ref
                        ),
                        None,
                    )
                    action_ref = attention_action.get("candidate_id") if isinstance(attention_action, Mapping) else None
                    won = bool(
                        isinstance(action_ref, str)
                        and action_ref == first_frame.decision.get("selected_candidate_ref")
                        and first_frame.decision.get("result_status") == "success"
                        and first_frame.decision.get("result_completeness") == "complete"
                    )
                    local_before = {
                        "mode": entered.get("mode"),
                        "target_ref": target_ref,
                        "gain": entered.get("base_gain"),
                    }
                    effective_result = {
                        "mode": entered.get("mode"),
                        "target_ref": target_ref,
                        "gain": entered.get("final_gain"),
                        "candidate_ref": action_ref,
                        "won_and_readback": won,
                    }
                    usage_status = "attention_winner_readback" if won else "entered_attention_competition"
                    status = "attempted" if won else "withheld"
                elif capability_key == CURRICULUM_EXPRESSION_CAPABILITY:
                    draft = first_frame.expression_draft
                    base_surface = str(entered.get("base_surface") or "")
                    final_surface = str(entered.get("final_surface") or "")
                    proposition_content = (
                        first_frame.proposition.content if first_frame.proposition is not None else None
                    )
                    semantic_preserved = bool(
                        draft is not None
                        and proposition_content is not None
                        and entered.get("proposition_ref") == draft.proposition_ref
                        and base_surface == proposition_content
                        and dict(draft.diff).get("semantic_change") is False
                    )
                    local_before = {
                        "proposition": proposition_content,
                        "surface": base_surface,
                    }
                    effective_result = {
                        "proposition": proposition_content,
                        "surface": final_surface,
                        "semantic_preserved": semantic_preserved,
                    }
                    usage_status = "entered_expression_draft" if semantic_preserved else "withheld_semantic_change"
                    status = "attempted" if semantic_preserved else "withheld"
                else:
                    parameter = entered.get("parameter")
                    component_delta = float(entered.get("component_delta", 0.0) or 0.0)
                    contribution = component_delta
                    local_before = {
                        "parameter": parameter,
                        "default": entered.get("default"),
                        "before": entered.get("before"),
                        "component": entered.get("component"),
                        "component_value": entered.get("base_component"),
                        "input_value": entered.get("input_value"),
                    }
                    effective_result = {
                        "parameter": parameter,
                        "delta": entered.get("delta"),
                        "effective": entered.get("effective"),
                        "bounds": list(entered.get("bounds", ())),
                        "component": entered.get("component"),
                        "component_value": entered.get("effective_component"),
                        "component_delta": component_delta,
                        "mode": entered.get("mode"),
                        "target_ref": entered.get("target_ref"),
                        "policy_version": entered.get("policy_version"),
                    }
                    usage_status = "entered_attention_gain_ledger"
                    # Entering the ledger is an attempt even if the resulting
                    # action loses. Outcome remains unknown until explicit
                    # feedback or reality is bound to this attempt.
                    status = "attempted"
                attempt = CurriculumAttempt(
                    attempt_id=_stable_id("curriculum_attempt", curriculum_id, episode_id, capability_key),
                    curriculum_id=curriculum_id,
                    project_id=activity.project_id,
                    target_capability=capability_key,
                    activity_episode_ref=episode_id,
                    activity_ref=activity.activity_id,
                    feature_key=str((trials[0].get("feature_keys") or ("unknown",))[0]),
                    target_action=None,
                    observed_winner=_selected_action_kind(results),
                    contribution=round(
                        max(-CURRICULUM_SINGLE_TRIAL_LIMIT, min(CURRICULUM_SINGLE_TRIAL_LIMIT, contribution)),
                        6,
                    ),
                    status=status,
                    effect_key=effect_key,
                    local_before=local_before,
                    effective_result=effective_result,
                    created_at=activity.occurred_at,
                    extra={"usage_status": usage_status, "curriculum_refs": curriculum_refs},
                )
                curriculum_attempts.append(learning_ledger.record_curriculum_attempt(attempt))

    curriculum_runs: list[CurriculumEpisodeRun] = []
    if learning_ledger is not None and results:
        for curriculum in curricula_from_teacher_frame(
            activity,
            source_episode_ref=episode_id,
            gateway_view=results[0].frame.gateway,
        ):
            curriculum_database = database.with_name(
                f"{database.stem}-curriculum-{curriculum.curriculum_id}{database.suffix or '.sqlite'}"
            )
            curriculum_runs.append(
                run_curriculum_episode(
                    curriculum_database,
                    curriculum,
                    learning_ledger=learning_ledger,
                    max_result_ticks=max_result_ticks,
                )
            )
    if learning_ledger is not None:
        # Remember only after this episode has completed its real AP path, so
        # the current activity cannot recall itself or teach retroactively.
        learning_ledger.remember_activity(activity)
    return ProjectEpisodeRun(
        activity=activity,
        episode_id=episode_id,
        results=tuple(results),
        staged_proposals=durable_proposals or tuple(environment.staged_proposals),
        pending_questions=durable_questions or tuple(dict(item) for item in environment.pending_questions),
        provider_mode=provider_mode,
        learned_preferences=learned_preferences,
        curriculum_preferences=curriculum_preferences,
        curriculum_trials=curriculum_trials,
        cognitive_curriculum_trials=cognitive_trials,
        curriculum_attempt=curriculum_attempt,
        curriculum_attempts=tuple(curriculum_attempts),
        curriculum_runs=tuple(curriculum_runs),
        learning_snapshot=(
            learning_ledger.snapshot(activity.project_id)
            if learning_ledger is not None
            else {"project_id": activity.project_id, "lesson_count": 0, "preferences": []}
        ),
        teacher_sampling=teacher_sampling,
    )


def run_feedback_episode(
    database_path: str | Path,
    feedback: ProjectFeedback,
    target_activity: ProjectActivity,
    *,
    learning_ledger: ProjectLearningLedger,
    gateway: HybridGateway | None = None,
    governance: GovernanceCompatibilityRecord | None = None,
    capability: CapabilityOwnership | None = None,
    max_result_ticks: int = 2,
) -> FeedbackEpisodeRun:
    """Run feedback through cognition/readback, then apply its scoped lesson."""

    if not isinstance(feedback, ProjectFeedback):
        raise ContractError("feedback_episode_requires_feedback")
    if not isinstance(target_activity, ProjectActivity):
        raise ContractError("feedback_episode_requires_target_activity")
    if not isinstance(learning_ledger, ProjectLearningLedger):
        raise ContractError("feedback_episode_requires_learning_ledger")
    if feedback.project_id != target_activity.project_id:
        raise ContractError("feedback_target_project_mismatch")
    if isinstance(max_result_ticks, bool) or not 1 <= int(max_result_ticks) <= 8:
        raise ContractError("feedback_result_tick_budget_out_of_bounds")
    database = Path(database_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    episode_id = _stable_id("feedback_episode", feedback.project_id, feedback.feedback_id)
    environment = ProjectCognitionEnvironment(feedback.project_id)
    provider = gateway or NullGateway()
    results: list[TickResult] = []
    with EventStore(database) as store:
        runtime = MindRuntime(
            store,
            environment,
            runtime_id=f"ap-vibe-runtime:{feedback.project_id}",
            organism_id=f"ap-vibe-organism:{feedback.project_id}",
            episode_id=episode_id,
            gateway=provider,
            governance=governance,
            capability=capability,
            action_weights={**MindRuntime.DEFAULT_WEIGHTS, "learned_preference": 1.0},
            max_internal_ticks=4,
            max_wake_attempts=8,
        )
        current = runtime.tick(
            feedback.as_event(
                runtime_id=runtime.runtime_id,
                organism_id=runtime.organism_id,
                episode_id=episode_id,
                target_activity=target_activity,
            )
        )
        results.append(current)
        result_back_seen = False
        for _ in range(int(max_result_ticks)):
            if current.result_envelope is None:
                break
            current = runtime.tick(current.result_envelope)
            results.append(current)
            result_back_seen = True

    lesson: FeedbackLesson | None = None
    for item in results:
        payload = item.result.payload_inline if item.result is not None else None
        raw_lesson = payload.get("feedback_lesson") if isinstance(payload, Mapping) else None
        if isinstance(raw_lesson, Mapping):
            lesson = FeedbackLesson.from_dict(raw_lesson)
            break
    # A successful action result is necessary but not enough: require that
    # its result envelope was actually processed as the next cognitive tick.
    if lesson is not None and result_back_seen:
        lesson = learning_ledger.apply(lesson)

    curriculum_outcome: CurriculumOutcome | None = None
    if lesson is not None and lesson.status == "applied" and result_back_seen:
        feedback_capability = feedback.applicability.get("target_capability")
        target_capability = (
            str(feedback_capability)
            if isinstance(feedback_capability, str) and feedback_capability in CURRICULUM_SUPPORTED_CAPABILITIES
            else CURRICULUM_TARGET_CAPABILITY
        )
        attempt = learning_ledger.attempt_for_episode(
            feedback.project_id,
            feedback.target_episode_id,
            target_capability,
        )
        if attempt is not None:
            if feedback.signal in {"reward", "positive", "approve"}:
                target_direction = 1.0
            elif feedback.signal in {"punishment", "negative", "reject", "correction"}:
                target_direction = -1.0
            else:
                target_direction = 0.0
            feedback_direction = 0.0
            if attempt.target_capability == CURRICULUM_TARGET_CAPABILITY:
                if feedback.target_action == attempt.target_action:
                    feedback_direction += target_direction * float(feedback.magnitude)
                if feedback.desired_action == attempt.target_action:
                    feedback_direction += float(feedback.magnitude)
            else:
                feedback_effect_key = feedback.applicability.get("effect_key")
                if feedback_effect_key is None or str(feedback_effect_key) == attempt.effect_key:
                    feedback_direction = target_direction * float(feedback.magnitude)
            # For a non-action capability the user evaluates the observed
            # effect itself.  A correction is therefore a counterexample and
            # an approval is a success regardless of whether the curriculum
            # raised or lowered a score.  Multiplying by a negative trial
            # contribution would invert that human meaning.
            alignment = (
                float(attempt.contribution) * feedback_direction
                if attempt.target_capability == CURRICULUM_TARGET_CAPABILITY
                else feedback_direction
            )
            if alignment > 1e-9:
                outcome_name = "success"
                resulting_status = "active_trial"
                rationale = "用户反馈方向支持这条课程在该能力上的试用贡献"
            elif alignment < -1e-9:
                outcome_name = "counterexample"
                resulting_status = "reteach"
                rationale = "用户纠正与这条课程在该能力上的试用方向相反"
            else:
                outcome_name = "unknown"
                resulting_status = "active_trial"
                rationale = "这次反馈没有明确指向课程实际调整过的能力效果"
            curriculum_outcome = CurriculumOutcome(
                outcome_id=_stable_id("curriculum_outcome", attempt.curriculum_id, feedback.feedback_id),
                curriculum_id=attempt.curriculum_id,
                project_id=feedback.project_id,
                outcome=outcome_name,
                resulting_status=resulting_status,
                source_ref=feedback.feedback_id,
                episode_ref=episode_id,
                attempt_ref=attempt.attempt_id,
                evidence_refs=tuple(dict.fromkeys((feedback.source_ref, feedback.feedback_id, attempt.attempt_id))),
                rationale=rationale,
                created_at=(results[0].result.observed_at if results and results[0].result is not None else feedback.created_at),
                extra={
                    "feedback_signal": feedback.signal,
                    "feedback_target_action": feedback.target_action,
                    "feedback_desired_action": feedback.desired_action,
                    "curriculum_target_action": attempt.target_action,
                    "curriculum_target_capability": attempt.target_capability,
                    "curriculum_effect_key": attempt.effect_key,
                    "curriculum_contribution": attempt.contribution,
                },
            )
            curriculum_outcome = learning_ledger.record_curriculum_outcome(curriculum_outcome)

    first_gateway = results[0].frame.gateway if results else {}
    if isinstance(first_gateway, Mapping):
        limitations = first_gateway.get("limitations")
        provider_off = (
            first_gateway.get("status") == "unavailable"
            and isinstance(limitations, Sequence)
            and not isinstance(limitations, (str, bytes))
            and "no_provider_configured" in limitations
        )
        provider_mode = "provider_off" if provider_off else str(first_gateway.get("source", "unknown"))
    else:
        provider_mode = "unknown"
    return FeedbackEpisodeRun(
        feedback=feedback,
        episode_id=episode_id,
        results=tuple(results),
        lesson=lesson,
        curriculum_outcome=curriculum_outcome,
        learning_snapshot=learning_ledger.snapshot(feedback.project_id),
        provider_mode=provider_mode,
    )


__all__ = [
    "MAX_ACTIVITY_TEXT",
    "MAX_ACTIVITY_ITEMS",
    "ProjectActivity",
    "ProjectFeedback",
    "FeedbackLesson",
    "CurriculumCandidate",
    "CurriculumAttempt",
    "CurriculumOutcome",
    "CURRICULUM_PARAMETER_CAPABILITY",
    "CURRICULUM_SUPPORTED_CAPABILITIES",
    "CurriculumEpisodeRun",
    "ProjectLearningLedger",
    "ProjectKnowledgeProposal",
    "ProjectCognitionEnvironment",
    "ProjectEpisodeRun",
    "FeedbackEpisodeRun",
    "proposal_from_activity",
    "lesson_from_feedback",
    "curricula_from_teacher_frame",
    "run_project_episode",
    "run_feedback_episode",
    "run_curriculum_episode",
]
