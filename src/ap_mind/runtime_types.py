"""Small runtime-side projections used by the first vertical AP episode.

These objects are deliberately projections, not a second contract authority.
Wire identity, source and readback semantics stay in ``contracts.core``;
runtime projections only make the tick trace inspectable and serializable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .contracts import EventEnvelope


def _json(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value, ensure_ascii=False))


@dataclass(frozen=True)
class SAOccurrence:
    occurrence_id: str
    event_ref: str
    modality: str
    text: str
    features: Mapping[str, Any]
    tokens: tuple[str, ...]
    source: str
    evidence_refs: tuple[str, ...] = ()
    lineage_refs: tuple[str, ...] = ()
    activation: float = 0.0
    novelty: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "occurrence_id": self.occurrence_id,
            "event_ref": self.event_ref,
            "modality": self.modality,
            "text": self.text,
            "features": _json(dict(self.features)),
            "tokens": list(self.tokens),
            "source": self.source,
            "evidence_refs": list(self.evidence_refs),
            "lineage_refs": list(self.lineage_refs),
            "activation": self.activation,
            "novelty": self.novelty,
        }


@dataclass(frozen=True)
class RecallCandidate:
    ref: str
    event_ref: str
    score: float
    reason: str
    source: str
    summary: str = ""
    lineage_refs: tuple[str, ...] = ()
    base_score: float | None = None
    curriculum_gain: float = 0.0
    curriculum_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "event_ref": self.event_ref,
            "score": self.score,
            "reason": self.reason,
            "source": self.source,
            "summary": self.summary,
            "lineage_refs": list(self.lineage_refs),
            "base_score": self.score if self.base_score is None else self.base_score,
            "curriculum_gain": self.curriculum_gain,
            "curriculum_refs": list(self.curriculum_refs),
        }


@dataclass(frozen=True)
class Prediction:
    prediction_id: str
    anchor_refs: tuple[str, ...]
    expected_features: Mapping[str, Any]
    observed_features: Mapping[str, Any]
    confidence: float
    mismatch: float
    direction: str
    source: str = "ap_native"
    completeness: str = "complete"
    hypothesis: str = ""
    mode: str = "transition"
    uncertainty: float = 0.0
    base_confidence: float | None = None
    curriculum_delta: float = 0.0
    curriculum_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "prediction_id": self.prediction_id,
            "anchor_refs": list(self.anchor_refs),
            "expected_features": _json(dict(self.expected_features)),
            "observed_features": _json(dict(self.observed_features)),
            "confidence": self.confidence,
            "mismatch": self.mismatch,
            "direction": self.direction,
            "source": self.source,
            "completeness": self.completeness,
            "hypothesis": self.hypothesis,
            "mode": self.mode,
            "uncertainty": self.uncertainty,
            "base_confidence": self.confidence if self.base_confidence is None else self.base_confidence,
            "curriculum_delta": self.curriculum_delta,
            "curriculum_refs": list(self.curriculum_refs),
        }


@dataclass(frozen=True)
class Feeling:
    name: str
    intensity: float
    valence: float
    rationale: str
    source_refs: tuple[str, ...] = ()
    source: str = "ap_native"
    base_intensity: float | None = None
    curriculum_delta: float = 0.0
    curriculum_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "intensity": self.intensity,
            "valence": self.valence,
            "rationale": self.rationale,
            "source_refs": list(self.source_refs),
            "source": self.source,
            "base_intensity": self.intensity if self.base_intensity is None else self.base_intensity,
            "curriculum_delta": self.curriculum_delta,
            "curriculum_refs": list(self.curriculum_refs),
        }


@dataclass(frozen=True)
class ThoughtFrame:
    thought_id: str
    status: str
    proposition: str
    predecessor_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    unresolved: tuple[str, ...]
    owner: str = "ap_native"
    source: str = "ap_native"
    base_proposition: str = ""
    curriculum_additions: tuple[str, ...] = ()
    curriculum_refs: tuple[str, ...] = ()
    uncertainty: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "thought_id": self.thought_id,
            "status": self.status,
            "proposition": self.proposition,
            "predecessor_refs": list(self.predecessor_refs),
            "evidence_refs": list(self.evidence_refs),
            "unresolved": list(self.unresolved),
            "owner": self.owner,
            "source": self.source,
            "base_proposition": self.proposition if not self.base_proposition else self.base_proposition,
            "curriculum_additions": list(self.curriculum_additions),
            "curriculum_refs": list(self.curriculum_refs),
            "uncertainty": self.uncertainty,
        }


@dataclass(frozen=True)
class ParadigmOccurrence:
    """One bounded later-episode instantiation of a learned pattern."""

    occurrence_id: str
    pattern_kind: str
    invariants: Mapping[str, Any]
    bindings: Mapping[str, Any]
    missing_slots: tuple[str, ...] = ()
    relations: tuple[Mapping[str, Any], ...] = ()
    base_match: float = 0.0
    curriculum_delta: float = 0.0
    final_match: float = 0.0
    source: str = "assisted_trial"
    curriculum_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    lineage_refs: tuple[str, ...] = ()
    status: str = "withheld"
    completeness: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "occurrence_id": self.occurrence_id,
            "pattern_kind": self.pattern_kind,
            "invariants": _json(dict(self.invariants)),
            "bindings": _json(dict(self.bindings)),
            "missing_slots": list(self.missing_slots),
            "relations": [_json(dict(item)) for item in self.relations],
            "base_match": self.base_match,
            "curriculum_delta": self.curriculum_delta,
            "final_match": self.final_match,
            "source": self.source,
            "curriculum_refs": list(self.curriculum_refs),
            "evidence_refs": list(self.evidence_refs),
            "lineage_refs": list(self.lineage_refs),
            "status": self.status,
            "completeness": self.completeness,
        }


@dataclass(frozen=True)
class Proposition:
    """A bounded semantic commitment formed before any renderer is called."""

    proposition_id: str
    content: str
    claim_kind: str = "observation"
    subject_scope: str = "private"
    source_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    confidence: float = 0.0
    uncertainty: float = 1.0
    expected_outcome: Mapping[str, Any] = field(default_factory=dict)
    frozen: bool = False
    owner: str = "ap_native"

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposition_id": self.proposition_id,
            "content": self.content,
            "claim_kind": self.claim_kind,
            "subject_scope": self.subject_scope,
            "source_refs": list(self.source_refs),
            "evidence_refs": list(self.evidence_refs),
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "expected_outcome": _json(dict(self.expected_outcome)),
            "frozen": self.frozen,
            "owner": self.owner,
        }


@dataclass(frozen=True)
class ExpressionDraft:
    """A public-facing unit sequence constrained by one proposition."""

    draft_id: str
    proposition_ref: str
    units: tuple[str, ...] = ()
    proposition_units: tuple[str, ...] = ()
    unit_granularity: str = "chunk"
    tone: str = "neutral"
    public_allowed: bool = False
    renderer_source: str = "ap_native"
    diff: Mapping[str, Any] = field(default_factory=dict)
    status: str = "draft"

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "proposition_ref": self.proposition_ref,
            "units": list(self.units),
            "proposition_units": list(self.proposition_units),
            "unit_granularity": self.unit_granularity,
            "tone": self.tone,
            "public_allowed": self.public_allowed,
            "renderer_source": self.renderer_source,
            "diff": _json(dict(self.diff)),
            "status": self.status,
        }


@dataclass(frozen=True)
class ExpressionRevisionProposal:
    """Source-tagged edit proposal for the not-yet-dispatched suffix."""

    proposal_id: str
    target_draft_id: str
    start_index: int
    delete_count: int
    replacement_units: tuple[str, ...] = ()
    preserves_proposition: bool = False
    proposition_expected_units: tuple[str, ...] = ()
    mismatch: float = 0.0
    confidence: float = 0.0
    source: str = "teacher"
    evidence_refs: tuple[str, ...] = ()
    lineage_refs: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "target_draft_id": self.target_draft_id,
            "start_index": self.start_index,
            "delete_count": self.delete_count,
            "replacement_units": list(self.replacement_units),
            "preserves_proposition": self.preserves_proposition,
            "proposition_expected_units": list(self.proposition_expected_units),
            "mismatch": self.mismatch,
            "confidence": self.confidence,
            "source": self.source,
            "evidence_refs": list(self.evidence_refs),
            "lineage_refs": list(self.lineage_refs),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ThoughtStreamFrame:
    """Lineage-aware thought projection; it is not a hidden second planner."""

    frame_id: str
    predecessor_refs: tuple[str, ...] = ()
    event_refs: tuple[str, ...] = ()
    status: str = "open"
    proposition_ref: str | None = None
    expression_draft_ref: str | None = None
    residuals: tuple[str, ...] = ()
    attention_ref: str | None = None
    source: str = "ap_native"
    owner: str = "ap_native"
    completeness: str = "complete"

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "predecessor_refs": list(self.predecessor_refs),
            "event_refs": list(self.event_refs),
            "status": self.status,
            "proposition_ref": self.proposition_ref,
            "expression_draft_ref": self.expression_draft_ref,
            "residuals": list(self.residuals),
            "attention_ref": self.attention_ref,
            "source": self.source,
            "owner": self.owner,
            "completeness": self.completeness,
        }


@dataclass
class CognitiveCounters:
    """Separate wall/provider/output counters from actual cognitive ticks."""

    cognitive_ticks: int = 0
    provider_waits: int = 0
    output_units: int = 0
    tool_progress: int = 0
    readbacks: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "cognitive_ticks": int(self.cognitive_ticks),
            "provider_waits": int(self.provider_waits),
            "output_units": int(self.output_units),
            "tool_progress": int(self.tool_progress),
            "readbacks": int(self.readbacks),
        }


@dataclass(frozen=True)
class ActionCandidate:
    candidate_id: str
    kind: str
    target: str
    proposition: str
    components: Mapping[str, float]
    expected_outcome: Mapping[str, Any]
    source: str = "environment"
    owner: str = "ap_native"
    idempotency_key: str = ""
    completeness: str = "complete"
    eligible: bool = True

    def score(self, weights: Mapping[str, float]) -> float:
        return sum(float(weights.get(key, 0.0)) * float(value) for key, value in self.components.items())

    def to_dict(self, *, score: float | None = None) -> dict[str, Any]:
        payload = {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "target": self.target,
            "proposition": self.proposition,
            "components": _json(dict(self.components)),
            "expected_outcome": _json(dict(self.expected_outcome)),
            "source": self.source,
            "owner": self.owner,
            "idempotency_key": self.idempotency_key,
            "completeness": self.completeness,
            "eligible": self.eligible,
        }
        if score is not None:
            payload["score"] = score
        return payload


@dataclass(frozen=True)
class ActionProcess:
    process_id: str
    candidate_ref: str
    status: str
    started_at: str
    last_update: str
    progress: float = 0.0
    result_ref: str | None = None
    lineage_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_id": self.process_id,
            "candidate_ref": self.candidate_ref,
            "status": self.status,
            "started_at": self.started_at,
            "last_update": self.last_update,
            "progress": self.progress,
            "result_ref": self.result_ref,
            "lineage_refs": list(self.lineage_refs),
        }


@dataclass(frozen=True)
class CognitionFrame:
    frame_id: str
    episode_id: str
    tick_index: int
    trigger_refs: tuple[str, ...]
    sa: SAOccurrence
    state_pool: Mapping[str, Any]
    current_field: Mapping[str, Any]
    b_recall: tuple[RecallCandidate, ...]
    c_prediction: tuple[Prediction, ...]
    feelings: tuple[Feeling, ...]
    slow_affect: Mapping[str, float]
    attention: Mapping[str, Any]
    thoughts: tuple[ThoughtFrame, ...]
    actions: tuple[Mapping[str, Any], ...]
    decision: Mapping[str, Any]
    gateway: Mapping[str, Any]
    frontiers: tuple[Mapping[str, Any], ...] = ()
    phase_completeness: Mapping[str, str] = field(default_factory=dict)
    process_refs: tuple[str, ...] = ()
    result_ref: str | None = None
    completeness: str = "complete"
    proposition: Proposition | None = None
    expression_draft: ExpressionDraft | None = None
    thought_stream: ThoughtStreamFrame | None = None
    counters: Mapping[str, int] = field(default_factory=dict)
    cognitive_trial_observations: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)
    paradigms: tuple[ParadigmOccurrence, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "episode_id": self.episode_id,
            "tick_index": self.tick_index,
            "trigger_refs": list(self.trigger_refs),
            "sa": self.sa.to_dict(),
            "state_pool": _json(dict(self.state_pool)),
            "current_field": _json(dict(self.current_field)),
            "b_recall": [item.to_dict() for item in self.b_recall],
            "c_prediction": [item.to_dict() for item in self.c_prediction],
            "feelings": [item.to_dict() for item in self.feelings],
            "slow_affect": _json(dict(self.slow_affect)),
            "attention": _json(dict(self.attention)),
            "thoughts": [item.to_dict() for item in self.thoughts],
            "actions": [_json(dict(item)) for item in self.actions],
            "decision": _json(dict(self.decision)),
            "gateway": _json(dict(self.gateway)),
            "frontiers": [_json(dict(item)) for item in self.frontiers],
            "phase_completeness": _json(dict(self.phase_completeness)),
            "process_refs": list(self.process_refs),
            "result_ref": self.result_ref,
            "completeness": self.completeness,
            "proposition": self.proposition.to_dict() if self.proposition else None,
            "expression_draft": self.expression_draft.to_dict() if self.expression_draft else None,
            "thought_stream": self.thought_stream.to_dict() if self.thought_stream else None,
            "counters": _json(dict(self.counters)),
            "cognitive_trial_observations": {
                str(capability): [_json(dict(item)) for item in observations]
                for capability, observations in self.cognitive_trial_observations.items()
            },
            "paradigms": [item.to_dict() for item in self.paradigms],
        }


@dataclass(frozen=True)
class TickResult:
    frame: CognitionFrame
    receipt: Any | None = None
    result: Any | None = None
    result_envelope: EventEnvelope | None = None
    recovered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame": self.frame.to_dict(),
            "receipt": self.receipt.to_dict() if self.receipt is not None else None,
            "result": self.result.to_dict() if self.result is not None else None,
            "result_envelope": self.result_envelope.to_dict() if self.result_envelope else None,
            "recovered": self.recovered,
        }


__all__ = [
    "SAOccurrence",
    "RecallCandidate",
    "Prediction",
    "Feeling",
    "ThoughtFrame",
    "ParadigmOccurrence",
    "Proposition",
    "ExpressionDraft",
    "ExpressionRevisionProposal",
    "ThoughtStreamFrame",
    "CognitiveCounters",
    "ActionCandidate",
    "ActionProcess",
    "CognitionFrame",
    "TickResult",
]
