"""The first small, complete AP tick orchestrator.

This module is intentionally a *shallow* vertical slice.  It keeps the
whitepaper's ordering visible—SA, StatePool, CurrentField, B/C, feelings,
attention, thought, one action slot, dispatch/readback, checkpoint—while
leaving sophisticated learners and real connectors replaceable.  Nothing here
is a second planner: the environment supplies candidates and the gateway
supplies bounded proposals; this runtime owns the one slot and the event flow.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .codec import decode_event
from .contracts import (
    CapabilityOwnership,
    ContractError,
    DecisionSlot,
    DelegationDecision,
    DispatchReceipt,
    EventEnvelope,
    ResultEvent,
    utc_now,
)
from .environment import EnvironmentAdapter
from .expression import ExpressionRenderer, RendererProposal, review_renderer_proposal
from .gateway import GatewayCallReceipt, GatewayProposal, HybridGateway, NullGateway
from .governance import GovernanceCompatibilityRecord
from .mind_state import ContinuityState
from .output import (
    ExpressionReadbackAnnotator,
    OutputCursor,
    OutputUnitResult,
    TextUnitActuator,
    apply_output_revision,
    dispatch_next_unit,
    output_control_result,
    output_revision_is_recoverable,
    output_revision_rejection_reasons,
    prepare_output,
    restore_output,
    set_output_status,
)
from .runtime_types import (
    ActionCandidate,
    CognitiveCounters,
    CognitionFrame,
    ExpressionDraft,
    ExpressionRevisionProposal,
    Feeling,
    ParadigmOccurrence,
    Proposition,
    Prediction,
    RecallCandidate,
    SAOccurrence,
    ThoughtStreamFrame,
    ThoughtFrame,
    TickResult,
)
from .storage import EventStore


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


@dataclass(frozen=True)
class AttentionPolicy:
    """Central parameters for a finite, inspectable attention gain ledger."""

    novelty_weight: float = 0.24
    mismatch_weight: float = 0.22
    recall_weight: float = 0.16
    open_goal_weight: float = 0.16
    paradigm_weight: float = 0.12
    fatigue_inhibition_weight: float = 0.10
    curriculum_limit: float = 0.18
    max_candidates: int = 3
    parameter_version: str = "attention-policy.v1"
    parameter_trials: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "novelty_weight": self.novelty_weight,
            "mismatch_weight": self.mismatch_weight,
            "recall_weight": self.recall_weight,
            "open_goal_weight": self.open_goal_weight,
            "paradigm_weight": self.paradigm_weight,
            "fatigue_inhibition_weight": self.fatigue_inhibition_weight,
            "curriculum_limit": self.curriculum_limit,
            "max_candidates": self.max_candidates,
            "parameter_version": self.parameter_version,
            "parameter_trials": [dict(item) for item in self.parameter_trials],
        }


class MindRuntime:
    """A bounded AP runtime that can be driven by any EnvironmentAdapter."""

    DEFAULT_WEIGHTS: Mapping[str, float] = {
        "goal_fit": 0.24,
        "evidence_fit": 0.18,
        "novelty": 0.12,
        "closure_gain": 0.18,
        "uncertainty_cost": 0.10,
        "pressure_relief": 0.10,
        "risk_cost": -0.08,
    }

    def __init__(
        self,
        store: EventStore,
        environment: EnvironmentAdapter,
        *,
        runtime_id: str = "runtime-local",
        organism_id: str = "organism-local",
        episode_id: str = "episode-local",
        gateway: HybridGateway | None = None,
        governance: GovernanceCompatibilityRecord | None = None,
        capability: CapabilityOwnership | None = None,
        action_weights: Mapping[str, float] | None = None,
        max_recall: int = 8,
        max_internal_ticks: int = 4,
        max_wake_attempts: int = 8,
        wake_threshold: float = 0.25,
        text_actuator: TextUnitActuator | None = None,
        text_readback_annotator: ExpressionReadbackAnnotator | None = None,
        expression_renderer: ExpressionRenderer | None = None,
        text_unit_granularity: str = "chunk",
        max_output_units: int = 256,
        revision_min_confidence: float = 0.65,
        revision_min_mismatch: float = 0.55,
        external_memory_events: Sequence[EventEnvelope] = (),
        cognitive_curriculum_trials: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        attention_policy: AttentionPolicy | None = None,
        requested_capabilities: Sequence[str] | None = None,
        teacher_sampling: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(store, EventStore):
            raise ContractError("runtime_requires_event_store")
        if not hasattr(environment, "candidates") or not hasattr(environment, "dispatch"):
            raise ContractError("runtime_requires_environment_adapter")
        if max_recall < 1 or max_recall > 64:
            raise ContractError("max_recall_out_of_bounds")
        if isinstance(max_internal_ticks, bool) or max_internal_ticks < 1 or max_internal_ticks > 32:
            raise ContractError("max_internal_ticks_out_of_bounds")
        if isinstance(max_wake_attempts, bool) or max_wake_attempts < 1 or max_wake_attempts > 128:
            raise ContractError("max_wake_attempts_out_of_bounds")
        if isinstance(wake_threshold, bool) or not 0.0 <= float(wake_threshold) <= 1.0:
            raise ContractError("wake_threshold_out_of_bounds")
        if text_unit_granularity not in {"character", "word", "chunk"}:
            raise ContractError("text_unit_granularity_unsupported")
        if isinstance(max_output_units, bool) or not isinstance(max_output_units, int) or not 1 <= max_output_units <= 2048:
            raise ContractError("max_output_units_out_of_bounds")
        if (
            isinstance(revision_min_confidence, bool)
            or not isinstance(revision_min_confidence, (int, float))
            or not 0.0 <= float(revision_min_confidence) <= 1.0
        ):
            raise ContractError("revision_min_confidence_out_of_bounds")
        if (
            isinstance(revision_min_mismatch, bool)
            or not isinstance(revision_min_mismatch, (int, float))
            or not 0.0 <= float(revision_min_mismatch) <= 1.0
        ):
            raise ContractError("revision_min_mismatch_out_of_bounds")
        self.store = store
        self.environment = environment
        self.runtime_id = runtime_id
        self.organism_id = organism_id
        self.episode_id = episode_id
        self.gateway = gateway or NullGateway()
        self.governance = governance
        self.capability = capability or CapabilityOwnership(
            capability_key="hybrid.cognition",
            stage="local_primary",
            decision_owner="ap_native",
            content_owner="ap_native",
            evidence_owner="environment",
            execution_owner="environment",
            llm_allowed=False,
            reason="provider-free first vertical slice",
        )
        self.action_weights = dict(action_weights or self.DEFAULT_WEIGHTS)
        self.max_recall = max_recall
        self.max_internal_ticks = int(max_internal_ticks)
        self.max_wake_attempts = int(max_wake_attempts)
        self.wake_threshold = float(wake_threshold)
        self.text_actuator = text_actuator
        self.text_readback_annotator = text_readback_annotator
        self.expression_renderer = expression_renderer
        self.text_unit_granularity = text_unit_granularity
        self.max_output_units = int(max_output_units)
        self.revision_min_confidence = float(revision_min_confidence)
        self.revision_min_mismatch = float(revision_min_mismatch)
        if isinstance(external_memory_events, (str, bytes)) or not isinstance(external_memory_events, Sequence):
            raise ContractError("external_memory_events_must_be_sequence")
        if len(external_memory_events) > 64 or not all(isinstance(item, EventEnvelope) for item in external_memory_events):
            raise ContractError("external_memory_events_invalid")
        self.external_memory_events = tuple(external_memory_events)
        raw_trials = cognitive_curriculum_trials or {}
        if not isinstance(raw_trials, Mapping):
            raise ContractError("cognitive_curriculum_trials_must_be_mapping")
        self.cognitive_curriculum_trials = {
            str(key): tuple(dict(item) for item in value if isinstance(item, Mapping))[:16]
            for key, value in raw_trials.items()
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes))
        }
        self.cognitive_trial_observations: dict[str, list[dict[str, Any]]] = {
            str(key): [] for key in self.cognitive_curriculum_trials
        }
        if attention_policy is not None and not isinstance(attention_policy, AttentionPolicy):
            raise ContractError("attention_policy_invalid")
        self.attention_policy = attention_policy or AttentionPolicy()
        if requested_capabilities is not None and (
            isinstance(requested_capabilities, (str, bytes))
            or not isinstance(requested_capabilities, Sequence)
        ):
            raise ContractError("requested_capabilities_must_be_sequence")
        self.requested_capabilities = (
            tuple(
                dict.fromkeys(
                    str(item)
                    for item in requested_capabilities
                    if isinstance(item, str) and item
                )
            )[:32]
            if requested_capabilities is not None
            else None
        )
        if teacher_sampling is not None and not isinstance(teacher_sampling, Mapping):
            raise ContractError("teacher_sampling_must_be_mapping")
        self.teacher_sampling = dict(teacher_sampling or {})
        self.tick_index = 0
        self.slow_affect: dict[str, float] = {
            "flow": 0.5,
            "pressure": 0.0,
            "fatigue": 0.0,
            "curiosity": 0.2,
        }
        self.state_pool: dict[str, Any] = {"event_count": 0, "token_counts": {}}
        self.open_thought: ThoughtFrame | None = None
        self.continuity = ContinuityState()
        self.last_sa: SAOccurrence | None = None
        self.last_frame_id: str | None = None
        self._restore()

    @property
    def last_proposition(self) -> Proposition | None:
        return self.continuity.proposition

    @last_proposition.setter
    def last_proposition(self, value: Proposition | None) -> None:
        self.continuity.proposition = value

    @property
    def last_expression_draft(self) -> ExpressionDraft | None:
        return self.continuity.expression_draft

    @last_expression_draft.setter
    def last_expression_draft(self, value: ExpressionDraft | None) -> None:
        self.continuity.expression_draft = value

    @property
    def last_thought_stream(self) -> ThoughtStreamFrame | None:
        return self.continuity.thought_stream

    @last_thought_stream.setter
    def last_thought_stream(self, value: ThoughtStreamFrame | None) -> None:
        self.continuity.thought_stream = value

    @property
    def counters(self) -> CognitiveCounters:
        return self.continuity.counters

    @counters.setter
    def counters(self, value: CognitiveCounters) -> None:
        self.continuity.counters = value

    @property
    def _last_frontiers(self) -> tuple[Mapping[str, Any], ...]:
        return self.continuity.frontiers

    @_last_frontiers.setter
    def _last_frontiers(self, value: tuple[Mapping[str, Any], ...]) -> None:
        self.continuity.frontiers = value

    @property
    def _wake_attempts(self) -> int:
        return self.continuity.wake_attempts

    @_wake_attempts.setter
    def _wake_attempts(self, value: int) -> None:
        self.continuity.wake_attempts = int(value)

    @property
    def _internal_sequence(self) -> int:
        return self.continuity.internal_sequence

    @_internal_sequence.setter
    def _internal_sequence(self, value: int) -> None:
        self.continuity.internal_sequence = int(value)

    @property
    def last_internal_status(self) -> str:
        return self.continuity.internal_status

    @last_internal_status.setter
    def last_internal_status(self, value: str) -> None:
        self.continuity.internal_status = str(value)

    def _restore(self) -> None:
        checkpoint = self.store.latest_checkpoint()
        if not checkpoint:
            return
        payload = checkpoint
        stored_episode = payload.get("episode_id")
        if isinstance(stored_episode, str) and stored_episode:
            self.episode_id = stored_episode
        tick = payload.get("tick_index")
        if isinstance(tick, int) and tick >= 0:
            self.tick_index = tick
        state = payload.get("state_pool")
        if isinstance(state, Mapping):
            self.state_pool = dict(state)
        affect = payload.get("slow_affect")
        if isinstance(affect, Mapping):
            self.slow_affect = {str(k): float(v) for k, v in affect.items()}
        thought = payload.get("open_thought")
        if isinstance(thought, Mapping):
            self.open_thought = ThoughtFrame(
                thought_id=str(thought.get("thought_id", "")),
                status=str(thought.get("status", "open")),
                proposition=str(thought.get("proposition", "")),
                predecessor_refs=tuple(str(x) for x in thought.get("predecessor_refs", ())),
                evidence_refs=tuple(str(x) for x in thought.get("evidence_refs", ())),
                unresolved=tuple(str(x) for x in thought.get("unresolved", ())),
                owner=str(thought.get("owner", "ap_native")),
                source=str(thought.get("source", "ap_native")),
                base_proposition=str(thought.get("base_proposition", thought.get("proposition", ""))),
                curriculum_additions=tuple(str(x) for x in thought.get("curriculum_additions", ())),
                curriculum_refs=tuple(str(x) for x in thought.get("curriculum_refs", ())),
                uncertainty=float(thought.get("uncertainty", 0.0) or 0.0),
            )
        last = payload.get("last_frame_id")
        if isinstance(last, str):
            self.last_frame_id = last
        self.continuity = ContinuityState.from_checkpoint(payload, tick_index=self.tick_index)

    @staticmethod
    def _now() -> str:
        return utc_now()

    def _prior_events(self, event: EventEnvelope) -> list[EventEnvelope]:
        local = [
            item
            for item in self.store.list_events(episode_id=event.episode_id, limit=self.max_recall + 1)
            if item.event_id != event.event_id
        ]
        combined: dict[str, EventEnvelope] = {}
        for item in (*self.external_memory_events, *local):
            if item.event_id != event.event_id:
                combined[item.event_id] = item
        return list(combined.values())[-max(self.max_recall * 4, self.max_recall) :]

    def _recall_curriculum_gain(self, event_ref: str) -> tuple[float, tuple[str, ...]]:
        gain = 0.0
        refs: list[str] = []
        for trial in self.cognitive_curriculum_trials.get("project.recall", ()):
            adjustments = trial.get("memory_gain_adjustments")
            if not isinstance(adjustments, Mapping):
                continue
            raw = adjustments.get(event_ref)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                continue
            gain += float(raw)
            curriculum_id = trial.get("curriculum_id")
            if isinstance(curriculum_id, str) and curriculum_id:
                refs.append(curriculum_id)
        return round(_clamp(gain, -0.36, 0.36), 6), tuple(dict.fromkeys(refs))

    def _recall(self, sa: SAOccurrence, prior_events: Sequence[EventEnvelope]) -> tuple[RecallCandidate, ...]:
        candidates: list[RecallCandidate] = []
        observations = self.cognitive_trial_observations.setdefault("project.recall", [])
        current = set(sa.tokens)
        for distance, prior in enumerate(reversed(prior_events), start=1):
            prior_sa = decode_event(prior)
            overlap = len(current.intersection(prior_sa.tokens)) / max(1, len(current.union(prior_sa.tokens)))
            recency = 1.0 / (1.0 + 0.35 * distance)
            base_score = round(_clamp(0.68 * overlap + 0.32 * recency), 6)
            curriculum_gain, curriculum_refs = self._recall_curriculum_gain(prior.event_id)
            score = round(_clamp(base_score + curriculum_gain), 6)
            if curriculum_refs:
                observations.append(
                    {
                        "curriculum_refs": list(curriculum_refs),
                        "effect_key": prior.event_id,
                        "base_score": base_score,
                        "curriculum_gain": curriculum_gain,
                        "final_score": score,
                        "status": "entered_b_recall" if score > 0.0 else "suppressed_below_zero",
                    }
                )
            if score <= 0.0:
                continue
            candidates.append(
                RecallCandidate(
                    ref=prior.event_id,
                    event_ref=prior.event_id,
                    score=score,
                    reason=f"token_overlap={overlap:.3f};recency={recency:.3f};curriculum_gain={curriculum_gain:.3f}",
                    source="memory_assisted_trial" if curriculum_refs else "memory",
                    summary=prior_sa.text[:320],
                    lineage_refs=tuple(dict.fromkeys((*prior.lineage_refs, prior.event_id))),
                    base_score=base_score,
                    curriculum_gain=curriculum_gain,
                    curriculum_refs=curriculum_refs,
                )
            )
        candidates.sort(key=lambda item: (-item.score, item.event_ref))
        return tuple(candidates[: self.max_recall])

    def _prediction_curriculum(self, event: EventEnvelope) -> tuple[Prediction, ...]:
        # Curriculum is for a later independent reality observation.  A
        # result-back or internal control tick still runs the complete AP
        # pipeline, but must not replay the same teacher scaffold as if new
        # evidence had arrived.
        if event.source != "external" or event.role != "observation":
            return ()
        observations = self.cognitive_trial_observations.setdefault("project.prediction", [])
        output: list[Prediction] = []
        for trial in self.cognitive_curriculum_trials.get("project.prediction", ())[:1]:
            hypotheses = trial.get("prediction_hypotheses")
            if isinstance(hypotheses, (str, bytes)) or not isinstance(hypotheses, Sequence):
                continue
            curriculum_id = trial.get("curriculum_id")
            for item in hypotheses[:1]:
                if not isinstance(item, Mapping):
                    continue
                content = item.get("content")
                confidence = item.get("confidence")
                uncertainty = item.get("uncertainty")
                mode = item.get("mode", "forecast")
                if (
                    not isinstance(content, str)
                    or not content.strip()
                    or isinstance(confidence, bool)
                    or not isinstance(confidence, (int, float))
                    or isinstance(uncertainty, bool)
                    or not isinstance(uncertainty, (int, float))
                    or mode not in {"forecast", "attribution", "relationship"}
                ):
                    continue
                refs = (
                    (str(curriculum_id),)
                    if isinstance(curriculum_id, str) and curriculum_id
                    else ()
                )
                source_refs = tuple(
                    str(ref)
                    for ref in item.get("evidence_refs", ())
                    if isinstance(ref, str) and ref
                )[:16]
                teacher_confidence = round(_clamp(float(confidence)), 6)
                bounded_uncertainty = round(_clamp(float(uncertainty)), 6)
                # The model's confidence is source metadata, not AP's current
                # belief.  A newly adopted curriculum can contribute only one
                # bounded local trial weight until later reality/feedback
                # supplies an outcome.
                curriculum_delta = round(
                    min(0.18, teacher_confidence * 0.18),
                    6,
                )
                stable_effect_key = (
                    f"prediction_hypothesis:{str(curriculum_id)}"
                    if isinstance(curriculum_id, str) and curriculum_id
                    else "prediction_hypothesis:unknown"
                )
                prediction = Prediction(
                    prediction_id=f"pred_{uuid4().hex}",
                    anchor_refs=source_refs,
                    expected_features={
                        "hypothesis": content.strip()[:1600],
                        "mode": str(mode),
                        "trial": True,
                    },
                    # A teacher hypothesis has not yet met reality.  It cannot
                    # manufacture an observation or a mismatch feeling.
                    observed_features={},
                    confidence=curriculum_delta,
                    mismatch=0.0,
                    direction=str(mode),
                    source="assisted_trial_hypothesis",
                    completeness=str(item.get("completeness", "unknown")),
                    hypothesis=content.strip()[:1600],
                    mode=str(mode),
                    uncertainty=bounded_uncertainty,
                    base_confidence=0.0,
                    curriculum_delta=curriculum_delta,
                    curriculum_refs=refs,
                )
                output.append(prediction)
                observations.append(
                    {
                        "curriculum_refs": list(refs),
                        "effect_key": stable_effect_key,
                        "base_confidence": 0.0,
                        "curriculum_delta": prediction.curriculum_delta,
                        "final_confidence": prediction.confidence,
                        "teacher_confidence": teacher_confidence,
                        "hypothesis": prediction.hypothesis,
                        "mode": prediction.mode,
                        "status": "entered_c_prediction",
                    }
                )
        return tuple(output)

    def _predict(
        self,
        sa: SAOccurrence,
        recalls: Sequence[RecallCandidate],
        prior_events: Sequence[EventEnvelope],
        event: EventEnvelope,
    ) -> tuple[Prediction, ...]:
        if not prior_events:
            local = (
                Prediction(
                    prediction_id=f"pred_{uuid4().hex}",
                    anchor_refs=(),
                    expected_features={},
                    observed_features={},
                    confidence=0.0,
                    mismatch=0.0,
                    direction="insufficient_history",
                    source="ap_native",
                    completeness="unknown",
                    mode="transition",
                    uncertainty=1.0,
                    base_confidence=0.0,
                ),
            )
        else:
            previous = decode_event(prior_events[-1])
            overlap = len(set(sa.tokens).intersection(previous.tokens)) / max(1, len(set(sa.tokens).union(previous.tokens)))
            expected = {"same_topic": round(overlap, 6), "next_event_present": True}
            observed = {"same_topic": round(overlap, 6), "next_event_present": True}
            mismatch = round(_clamp(1.0 - overlap), 6)
            confidence = round(_clamp(0.35 + 0.45 * overlap + 0.2 * min(1.0, len(recalls) / 4)), 6)
            local = (
                Prediction(
                    prediction_id=f"pred_{uuid4().hex}",
                    anchor_refs=(prior_events[-1].event_id,),
                    expected_features=expected,
                    observed_features=observed,
                    confidence=confidence,
                    mismatch=mismatch,
                    direction="continue_topic" if overlap >= 0.25 else "reorient",
                    source="ap_native",
                    completeness="complete",
                    mode="transition",
                    uncertainty=round(1.0 - confidence, 6),
                    base_confidence=confidence,
                ),
            )
        return tuple((*local, *self._prediction_curriculum(event)))[:4]

    def _feelings(self, sa: SAOccurrence, predictions: Sequence[Prediction], event: EventEnvelope) -> tuple[Feeling, ...]:
        mismatch = max((item.mismatch for item in predictions), default=0.0)
        feelings: list[Feeling] = []
        if sa.novelty >= 0.65:
            feelings.append(Feeling("surprise", round(_clamp(sa.novelty), 6), 0.05, "new or weakly overlapping input", (event.event_id,)))
        if mismatch >= 0.55:
            feelings.append(Feeling("incongruity", round(mismatch, 6), -0.1, "current input diverges from recent expectation", (event.event_id,)))
        if event.completeness != "complete":
            feelings.append(Feeling("uncertainty", round(1.0 - (0.55 if event.completeness == "partial" else 0.0), 6), -0.15, "source is incomplete", (event.event_id,)))
        revision_raw = event.payload_inline.get("expression_revision") if isinstance(event.payload_inline, Mapping) else None
        if isinstance(revision_raw, Mapping):
            revision_mismatch = revision_raw.get("mismatch", 0.0)
            revision_confidence = revision_raw.get("confidence", 0.0)
            if isinstance(revision_mismatch, (int, float)) and not isinstance(revision_mismatch, bool):
                mismatch = max(mismatch, _clamp(float(revision_mismatch)))
                if float(revision_mismatch) >= 0.55:
                    feelings.append(
                        Feeling(
                            "incongruity",
                            round(_clamp(float(revision_mismatch)), 6),
                            -0.1,
                            "a source-tagged expression revision conflicts with the undispatched suffix",
                            (event.event_id,),
                        )
                    )
            if isinstance(revision_confidence, (int, float)) and not isinstance(revision_confidence, bool) and float(revision_confidence) < 0.5:
                feelings.append(
                    Feeling(
                        "uncertainty",
                        round(_clamp(1.0 - float(revision_confidence)), 6),
                        -0.1,
                        "the expression revision proposal has weak support",
                        (event.event_id,),
                    )
                )
        if not feelings:
            feelings.append(Feeling("coherence", round(_clamp(0.45 + 0.45 * (1.0 - mismatch)), 6), 0.15, "current evidence is locally consistent", (event.event_id,)))
        signals: set[str] = set()
        payload = event.payload_inline if isinstance(event.payload_inline, Mapping) else {}
        if event.completeness != "complete":
            signals.add("source_incomplete")
        if mismatch >= 0.55:
            signals.add("prediction_mismatch_high")
        if sa.novelty >= 0.65:
            signals.add("novel_input")
        if payload.get("observed_remaining"):
            signals.add("open_items_present")
        if payload.get("observed_unknown"):
            signals.add("unknown_present")
        conflicts = payload.get("conflicts")
        if isinstance(conflicts, Sequence) and not isinstance(conflicts, (str, bytes)) and conflicts:
            signals.add("conflict_present")
        if payload.get("observed_completed"):
            signals.add("completion_observed")
        by_name = {item.name: item for item in feelings}
        observations = self.cognitive_trial_observations.setdefault("project.appraisal", [])
        for trial in self.cognitive_curriculum_trials.get("project.appraisal", ()):
            effects = trial.get("appraisal_effects")
            if isinstance(effects, (str, bytes)) or not isinstance(effects, Sequence):
                continue
            curriculum_id = trial.get("curriculum_id")
            for effect in effects[:8]:
                if not isinstance(effect, Mapping):
                    continue
                required = effect.get("required_signals")
                if isinstance(required, (str, bytes)) or not isinstance(required, Sequence):
                    continue
                required_set = {str(item) for item in required if isinstance(item, str)}
                if not required_set or not required_set.issubset(signals):
                    if isinstance(curriculum_id, str) and curriculum_id:
                        observations.append(
                            {
                                "curriculum_refs": [curriculum_id],
                                "effect_key": str(effect.get("name") or "unknown"),
                                "required_signals": sorted(required_set),
                                "observed_signals": sorted(signals),
                                "status": "withheld_signal_mismatch",
                            }
                        )
                    continue
                name = effect.get("name")
                delta = effect.get("intensity_delta")
                if not isinstance(name, str) or not name.strip() or isinstance(delta, bool) or not isinstance(delta, (int, float)):
                    continue
                delta_value = _clamp(float(delta), -0.36, 0.36)
                prior = by_name.get(name.strip())
                base = prior.intensity if prior is not None else 0.0
                final = round(_clamp(base + delta_value), 6)
                if final <= 0.0:
                    if isinstance(curriculum_id, str) and curriculum_id:
                        observations.append(
                            {
                                "curriculum_refs": [curriculum_id],
                                "effect_key": name.strip(),
                                "base_intensity": base,
                                "curriculum_delta": round(final - base, 6),
                                "final_intensity": final,
                                "required_signals": sorted(required_set),
                                "observed_signals": sorted(signals),
                                "status": "suppressed_below_zero",
                            }
                        )
                    continue
                valence_raw = effect.get("valence")
                valence = (
                    _clamp(float(valence_raw), -1.0, 1.0)
                    if isinstance(valence_raw, (int, float)) and not isinstance(valence_raw, bool)
                    else (prior.valence if prior is not None else 0.0)
                )
                prior_refs = prior.curriculum_refs if prior is not None else ()
                refs = tuple(
                    dict.fromkeys(
                        (*prior_refs, *((curriculum_id,) if isinstance(curriculum_id, str) and curriculum_id else ()))
                    )
                )
                prior_sources = prior.source_refs if prior is not None else (event.event_id,)
                by_name[name.strip()] = Feeling(
                    name=name.strip(),
                    intensity=final,
                    valence=round(valence, 6),
                    rationale=(
                        f"{prior.rationale + ';' if prior is not None else ''}"
                        f"bounded curriculum trial matched local signals:{','.join(sorted(required_set))}"
                    ),
                    source_refs=tuple(dict.fromkeys((*prior_sources, event.event_id))),
                    source="assisted_trial",
                    base_intensity=base,
                    curriculum_delta=round(final - base, 6),
                    curriculum_refs=refs,
                )
                if isinstance(curriculum_id, str) and curriculum_id:
                    observations.append(
                        {
                            "curriculum_refs": [curriculum_id],
                            "effect_key": name.strip(),
                            "base_intensity": base,
                            "curriculum_delta": round(final - base, 6),
                            "final_intensity": final,
                            "required_signals": sorted(required_set),
                            "observed_signals": sorted(signals),
                            "status": "entered_appraisal",
                        }
                    )
        ordered: list[Feeling] = []
        seen: set[str] = set()
        for item in (*feelings, *by_name.values()):
            effective = by_name.get(item.name, item)
            if effective.name in seen:
                continue
            seen.add(effective.name)
            ordered.append(effective)
        return tuple(ordered[:8])

    def _update_state(self, sa: SAOccurrence, event: EventEnvelope) -> None:
        self.state_pool["event_count"] = int(self.state_pool.get("event_count", 0)) + 1
        counts = dict(self.state_pool.get("token_counts", {}))
        for token in sa.tokens:
            counts[token] = int(counts.get(token, 0)) + 1
        # Keep the hot state bounded; this is a cache, not the authoritative
        # memory layer.  Eviction is by observed frequency, never answer text.
        if len(counts) > 512:
            keep = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:512]
            counts = dict(keep)
        self.state_pool["token_counts"] = counts
        self.state_pool["last_source"] = event.source
        self.state_pool["last_completeness"] = event.completeness

    @staticmethod
    def _event_structure_profile(event: EventEnvelope) -> dict[str, Any]:
        payload = event.payload_inline if isinstance(event.payload_inline, Mapping) else {}
        conflicts = payload.get("conflicts")
        return {
            "source_completeness": event.completeness,
            "has_open_items": bool(payload.get("observed_remaining")),
            "has_unknowns": bool(payload.get("observed_unknown")),
            "has_conflicts": bool(
                isinstance(conflicts, Sequence)
                and not isinstance(conflicts, (str, bytes))
                and conflicts
            ),
            "has_next_action": bool(payload.get("observed_next_action")),
        }

    @staticmethod
    def _paradigm_slot_value(
        source: str,
        *,
        event: EventEnvelope,
        proposition: Proposition,
        recalls: Sequence[RecallCandidate],
    ) -> tuple[Any, bool]:
        payload = event.payload_inline if isinstance(event.payload_inline, Mapping) else {}
        if source == "proposition.content":
            return proposition.content, bool(proposition.content)
        if source == "recall.summary":
            item = next((value for value in recalls if value.summary), None)
            return (item.summary, True) if item is not None else (None, False)
        if source.startswith("activity."):
            key = source.split(".", 1)[1]
            if key not in payload:
                return None, False
            value = payload.get(key)
            if value is None or value == "" or value == () or value == []:
                return value, False
            return value, True
        return None, False

    def _paradigms(
        self,
        event: EventEnvelope,
        proposition: Proposition,
        recalls: Sequence[RecallCandidate],
    ) -> tuple[ParadigmOccurrence, ...]:
        """Instantiate at most one source-tagged active pattern this tick."""

        if event.source != "external" or event.role != "observation":
            return ()
        trials = self.cognitive_curriculum_trials.get("project.paradigm", ())[:1]
        if not trials:
            return ()
        profile = self._event_structure_profile(event)
        observations = self.cognitive_trial_observations.setdefault("project.paradigm", [])
        trial = trials[0]
        patterns = trial.get("paradigm_patterns")
        if isinstance(patterns, (str, bytes)) or not isinstance(patterns, Sequence):
            return ()
        pattern = next((item for item in patterns[:1] if isinstance(item, Mapping)), None)
        if pattern is None:
            return ()
        raw_invariants = pattern.get("invariants")
        invariants = dict(raw_invariants) if isinstance(raw_invariants, Mapping) else {}
        matched = 0
        mismatched: list[str] = []
        for key, expected in invariants.items():
            if key in profile and profile[key] == expected:
                matched += 1
            else:
                mismatched.append(str(key))
        base_match = round(matched / max(1, len(invariants)), 6)
        bindings: dict[str, Any] = {}
        missing_required: list[str] = []
        missing_optional: list[str] = []
        raw_slots = pattern.get("slots")
        if isinstance(raw_slots, Sequence) and not isinstance(raw_slots, (str, bytes)):
            for slot in raw_slots[:8]:
                if not isinstance(slot, Mapping):
                    continue
                name = slot.get("name")
                source = slot.get("source")
                if not isinstance(name, str) or not isinstance(source, str):
                    continue
                value, available = self._paradigm_slot_value(
                    source,
                    event=event,
                    proposition=proposition,
                    recalls=recalls,
                )
                if available:
                    bindings[name] = value
                elif slot.get("required", True) is True:
                    missing_required.append(name)
                else:
                    missing_optional.append(name)
        confidence = trial.get("confidence", 1.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            confidence = 1.0
        curriculum_delta = round(min(0.18, _clamp(float(confidence)) * 0.18), 6)
        if mismatched or missing_required or not invariants:
            status = "withheld"
        elif missing_optional:
            status = "partial"
        else:
            status = "matched"
        final_match = round(_clamp(base_match + (curriculum_delta if status != "withheld" else 0.0)), 6)
        curriculum_id = trial.get("curriculum_id")
        curriculum_refs = (str(curriculum_id),) if isinstance(curriculum_id, str) and curriculum_id else ()
        evidence_refs = tuple(
            dict.fromkeys(
                (
                    event.event_id,
                    *(
                        str(ref)
                        for ref in pattern.get("evidence_refs", ())
                        if isinstance(ref, str) and ref
                    ),
                )
            )
        )
        occurrence = ParadigmOccurrence(
            # Mechanical identity is derived from the durable event/course
            # lineage only for replay.  It never participates in matching or
            # scoring.
            occurrence_id=f"paradigm_{event.event_id}_{str(curriculum_id or 'unknown')}",
            pattern_kind=str(pattern.get("pattern_kind", "relation_frame")),
            invariants=invariants,
            bindings=bindings,
            missing_slots=tuple((*missing_required, *missing_optional)),
            relations=tuple(
                dict(item)
                for item in pattern.get("relations", ())[:12]
                if isinstance(item, Mapping)
            ) if isinstance(pattern.get("relations"), Sequence) and not isinstance(pattern.get("relations"), (str, bytes)) else (),
            base_match=base_match,
            curriculum_delta=curriculum_delta,
            final_match=final_match,
            source="assisted_trial",
            curriculum_refs=curriculum_refs,
            evidence_refs=evidence_refs,
            lineage_refs=tuple(dict.fromkeys((*curriculum_refs, event.event_id))),
            status=status,
            completeness=str(pattern.get("completeness", "unknown")),
        )
        observations.append(
            {
                "curriculum_refs": list(curriculum_refs),
                "effect_key": f"paradigm_pattern:{str(curriculum_id or 'unknown')}",
                "base_match": base_match,
                "curriculum_delta": curriculum_delta,
                "final_match": final_match,
                "bindings": dict(bindings),
                "missing_slots": list(occurrence.missing_slots),
                "mismatched_invariants": mismatched,
                "occurrence_ref": occurrence.occurrence_id,
                "status": "entered_paradigm" if status in {"matched", "partial"} else "withheld_structure_mismatch",
            }
        )
        return (occurrence,)

    def _attention_curriculum_gain(self, mode: str) -> tuple[float, tuple[str, ...]]:
        gain = 0.0
        refs: list[str] = []
        for trial in self.cognitive_curriculum_trials.get("project.attention", ())[:1]:
            adjustments = trial.get("attention_adjustments")
            if isinstance(adjustments, (str, bytes)) or not isinstance(adjustments, Sequence):
                continue
            for item in adjustments[:4]:
                if not isinstance(item, Mapping) or item.get("mode") != mode:
                    continue
                raw = item.get("gain_delta")
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    continue
                gain += float(raw)
                curriculum_id = trial.get("curriculum_id")
                if isinstance(curriculum_id, str) and curriculum_id:
                    refs.append(curriculum_id)
        return round(_clamp(gain, -self.attention_policy.curriculum_limit, self.attention_policy.curriculum_limit), 6), tuple(dict.fromkeys(refs))

    def _observe_parameter_trial(
        self,
        *,
        ledger_inputs: Mapping[str, float],
        ledger: Mapping[str, float],
        target_ref: str,
        mode: str,
    ) -> tuple[str, ...]:
        """Record the actual component changed by the episode-local policy.

        At most one parameter trial is resolved upstream for an episode.  This
        observation does not score a candidate or declare success; it only
        makes the local contribution available for later, feedback-bound
        attribution.
        """

        refs: list[str] = []
        component_by_parameter = {
            "attention.novelty_weight": "novelty",
            "attention.mismatch_weight": "prediction_mismatch",
            "attention.recall_weight": "recall_relevance",
            "attention.open_goal_weight": "open_goal_pressure",
            "attention.paradigm_weight": "paradigm_information_gain",
            "attention.fatigue_inhibition_weight": "fatigue_return_inhibition",
        }
        input_by_parameter = {
            "attention.novelty_weight": "novelty",
            "attention.mismatch_weight": "prediction_mismatch",
            "attention.recall_weight": "recall_relevance",
            "attention.open_goal_weight": "open_goal_pressure",
            "attention.paradigm_weight": "paradigm_information_gain",
            "attention.fatigue_inhibition_weight": "fatigue_return_inhibition",
        }
        for trial in self.attention_policy.parameter_trials[:1]:
            parameter = trial.get("parameter")
            curriculum_id = trial.get("curriculum_id")
            if not isinstance(parameter, str) or not isinstance(curriculum_id, str) or not curriculum_id:
                continue
            component = component_by_parameter.get(parameter)
            input_key = input_by_parameter.get(parameter)
            if component is None or input_key is None:
                continue
            raw_input = ledger_inputs.get(input_key, 0.0)
            default_value = trial.get("default")
            effective_value = trial.get("effective")
            delta = trial.get("delta")
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (default_value, effective_value, delta)):
                continue
            sign = -1.0 if parameter.endswith("fatigue_inhibition_weight") else 1.0
            base_component = round(sign * float(default_value) * _clamp(raw_input), 6)
            effective_component = round(float(ledger.get(component, 0.0)), 6)
            observation = {
                "curriculum_refs": [curriculum_id],
                "effect_key": f"parameter:{parameter}",
                "parameter": parameter,
                "mode": mode,
                "target_ref": target_ref,
                "component": component,
                "input_value": round(float(raw_input), 6),
                "default": round(float(default_value), 6),
                "before": round(float(default_value), 6),
                "delta": round(float(delta), 6),
                "effective": round(float(effective_value), 6),
                "bounds": list(trial.get("bounds", ())),
                "base_component": base_component,
                "effective_component": effective_component,
                "component_delta": round(effective_component - base_component, 6),
                "policy_version": self.attention_policy.parameter_version,
                "status": "entered_attention_gain_ledger",
            }
            self.cognitive_trial_observations.setdefault("project.parameter_tuning", []).append(observation)
            refs.append(curriculum_id)
        return tuple(dict.fromkeys(refs))

    def _attention(
        self,
        sa: SAOccurrence,
        recalls: Sequence[RecallCandidate],
        predictions: Sequence[Prediction],
        paradigms: Sequence[ParadigmOccurrence],
        event: EventEnvelope,
    ) -> dict[str, Any]:
        mismatch = max((item.mismatch for item in predictions), default=0.0)
        uncertainty = max((item.uncertainty for item in predictions), default=0.0)
        recall = max(recalls, key=lambda item: item.score, default=None)
        paradigm = max(
            (item for item in paradigms if item.status in {"matched", "partial"}),
            key=lambda item: item.final_match,
            default=None,
        )
        payload = event.payload_inline if isinstance(event.payload_inline, Mapping) else {}
        open_pressure = _clamp(
            0.45 * float(bool(payload.get("observed_remaining")))
            + 0.35 * float(bool(payload.get("observed_unknown")))
            + 0.20 * self.slow_affect.get("pressure", 0.0)
        )
        prior = dict(self.continuity.prior_attention)
        candidate_rows: list[dict[str, Any]] = []

        def add(
            mode: str,
            target_ref: str,
            *,
            recall_relevance: float = 0.0,
            paradigm_information: float = 0.0,
            novelty: float = 0.0,
            mismatch_value: float = 0.0,
            pressure_value: float = 0.0,
            risk: float = 0.0,
        ) -> None:
            curriculum_delta, curriculum_refs = self._attention_curriculum_gain(mode)
            fatigue_penalty = (
                -self.attention_policy.fatigue_inhibition_weight * self.slow_affect.get("fatigue", 0.0)
                if prior.get("target_ref") == target_ref
                else 0.0
            )
            ledger = {
                "novelty": round(self.attention_policy.novelty_weight * _clamp(novelty), 6),
                "prediction_mismatch": round(self.attention_policy.mismatch_weight * _clamp(mismatch_value), 6),
                "recall_relevance": round(self.attention_policy.recall_weight * _clamp(recall_relevance), 6),
                "open_goal_pressure": round(self.attention_policy.open_goal_weight * _clamp(pressure_value), 6),
                "paradigm_information_gain": round(self.attention_policy.paradigm_weight * _clamp(paradigm_information), 6),
                "fatigue_return_inhibition": round(fatigue_penalty, 6),
                "curriculum_delta": curriculum_delta,
                "risk_cost": round(-0.08 * _clamp(risk), 6),
            }
            parameter_refs = self._observe_parameter_trial(
                ledger_inputs={
                    "novelty": novelty,
                    "prediction_mismatch": mismatch_value,
                    "recall_relevance": recall_relevance,
                    "open_goal_pressure": pressure_value,
                    "paradigm_information_gain": paradigm_information,
                    "fatigue_return_inhibition": self.slow_affect.get("fatigue", 0.0)
                    if prior.get("target_ref") == target_ref
                    else 0.0,
                },
                ledger=ledger,
                target_ref=target_ref,
                mode=mode,
            )
            total = round(_clamp(sum(ledger.values())), 6)
            candidate_rows.append(
                {
                    "candidate_id": f"action_{event.event_id}_{mode}",
                    "mode": mode,
                    "target_ref": target_ref,
                    "gain": total,
                    "gain_ledger": ledger,
                    "curriculum_refs": list(dict.fromkeys((*curriculum_refs, *parameter_refs))),
                    "parameter_curriculum_refs": list(parameter_refs),
                    "source": "assisted_trial" if curriculum_refs or parameter_refs else "ap_native",
                    "risk": round(_clamp(risk), 6),
                }
            )
            if curriculum_refs:
                self.cognitive_trial_observations.setdefault("project.attention", []).append(
                    {
                        "curriculum_refs": list(curriculum_refs),
                        "effect_key": f"attention_mode:{mode}",
                        "mode": mode,
                        "target_ref": target_ref,
                        "base_gain": round(_clamp(total - curriculum_delta), 6),
                        "curriculum_delta": curriculum_delta,
                        "final_gain": total,
                        "status": "entered_attention_competition",
                    }
                )

        add(
            "maintain_attention",
            event.event_id,
            novelty=sa.novelty,
            mismatch_value=mismatch,
            pressure_value=open_pressure,
            paradigm_information=paradigm.final_match if paradigm is not None else 0.0,
            risk=0.05,
        )
        if recall is not None or paradigm is not None:
            if paradigm is not None and (recall is None or paradigm.final_match >= recall.score):
                target_ref = paradigm.occurrence_id
                relevance = 0.0
                information = paradigm.final_match
            else:
                target_ref = recall.ref if recall is not None else sa.occurrence_id
                relevance = recall.score if recall is not None else 0.0
                information = 0.0
            add(
                "shift_attention",
                target_ref,
                recall_relevance=relevance,
                paradigm_information=information,
                mismatch_value=mismatch,
                pressure_value=open_pressure,
                risk=0.12,
            )
        has_unbound = any(item.missing_slots for item in paradigms)
        if open_pressure > 0.0 or mismatch > 0.0 or uncertainty > 0.5 or has_unbound:
            add(
                "diversify_attention",
                f"frontier_{event.event_id}",
                novelty=max(sa.novelty, uncertainty),
                mismatch_value=mismatch,
                pressure_value=open_pressure,
                paradigm_information=0.7 if has_unbound else 0.0,
                risk=0.18,
            )
        candidate_rows.sort(key=lambda item: (-float(item["gain"]), str(item["mode"])))
        return {
            "mode": "prior_readback" if prior else "current_field_entry_only",
            "entry_ref": sa.occurrence_id,
            "focus_ref": prior.get("target_ref"),
            "selected_target": prior.get("target_ref"),
            "prior_readback": prior or None,
            "candidates": candidate_rows[: self.attention_policy.max_candidates],
            "recall_count": len(recalls),
            "budget": {"current_field": 32, "b_candidates": self.max_recall, "c_candidates": 4, "attention_candidates": self.attention_policy.max_candidates},
            "policy": self.attention_policy.to_dict(),
            "source": "assisted_parameter_trial" if self.attention_policy.parameter_trials else "ap_native",
        }

    def _thought(self, sa: SAOccurrence, recalls: Sequence[RecallCandidate], feelings: Sequence[Feeling], event: EventEnvelope) -> ThoughtFrame:
        prior_refs = (self.open_thought.thought_id,) if self.open_thought and self.open_thought.status == "open" else ()
        recall_note = f"；召回 {len(recalls)} 个相关经历" if recalls else "；暂未召回直接相关经历"
        feeling_note = "、".join(item.name for item in feelings)
        if event.source == "internal":
            # Internal wake is a new occurrence, not a replay of the prior
            # sentence.  Its bounded payload names the residual being
            # revisited; the ordinary codec/B/C path supplies the rest.
            proposition = f"我继续检查未闭合前沿：{sa.text[:240]}{recall_note}；当前感受为{feeling_note}"
            status = "revise" if prior_refs else "extend"
        else:
            proposition = f"我收到了：{sa.text[:240]}{recall_note}；当前感受为{feeling_note}"
            status = "open"
        unresolved: list[str] = []
        if event.completeness != "complete":
            unresolved.append("输入证据尚不完整")
        # An internal pass alone is not new external evidence.  Keep an
        # inherited residual open until a later observation/readback supports
        # closure; bounded wake budgets stop infinite self-review honestly.
        if self.open_thought is not None and self.open_thought.unresolved:
            unresolved.append("上一认知前沿仍未闭合")
        base_proposition = proposition
        additions: list[str] = []
        curriculum_refs: list[str] = []
        scaffold_uncertainty = 0.0
        observations = self.cognitive_trial_observations.setdefault("project.thought", [])
        eligible_for_scaffold = event.source == "external" and event.role == "observation"
        for trial in self.cognitive_curriculum_trials.get("project.thought", ())[:1] if eligible_for_scaffold else ():
            scaffolds = trial.get("thought_scaffolds")
            if isinstance(scaffolds, (str, bytes)) or not isinstance(scaffolds, Sequence):
                continue
            curriculum_id = trial.get("curriculum_id")
            item = next((value for value in scaffolds[:1] if isinstance(value, Mapping)), None)
            if item is None:
                continue
            content = item.get("content")
            uncertainty = item.get("uncertainty", 1.0)
            if (
                not isinstance(content, str)
                or not content.strip()
                or isinstance(uncertainty, bool)
                or not isinstance(uncertainty, (int, float))
            ):
                continue
            addition = content.strip()[:1600]
            additions.append(addition)
            scaffold_uncertainty = round(_clamp(float(uncertainty)), 6)
            if isinstance(curriculum_id, str) and curriculum_id:
                curriculum_refs.append(curriculum_id)
            for residual in item.get("unresolved", ()):
                if isinstance(residual, str) and residual.strip():
                    unresolved.append(residual.strip()[:240])
            observations.append(
                {
                    "curriculum_refs": list(curriculum_refs),
                    "effect_key": f"thought_scaffold:{str(curriculum_id or 'unknown')}",
                    "base_proposition": base_proposition,
                    "curriculum_additions": [addition],
                    "final_proposition": f"{base_proposition}；可进一步检查：{addition}",
                    "uncertainty": scaffold_uncertainty,
                    "curriculum_delta": round(
                        (1.0 - scaffold_uncertainty) * 0.18,
                        6,
                    ),
                    "status": "entered_thought_stream",
                }
            )
            break
        if additions:
            proposition = f"{base_proposition}；可进一步检查：{additions[0]}"
        return ThoughtFrame(
            thought_id=f"thought_{uuid4().hex}",
            status=status,
            proposition=proposition,
            predecessor_refs=prior_refs,
            evidence_refs=tuple(dict.fromkeys((*event.evidence_refs, event.event_id))),
            unresolved=tuple(dict.fromkeys(unresolved)),
            owner="mixed" if additions else "ap_native",
            source="assisted_trial" if additions else "ap_native",
            base_proposition=base_proposition,
            curriculum_additions=tuple(additions),
            curriculum_refs=tuple(dict.fromkeys(curriculum_refs)),
            uncertainty=scaffold_uncertainty,
        )

    def _make_proposition(
        self,
        sa: SAOccurrence,
        feelings: Sequence[Feeling],
        event: EventEnvelope,
        predictions: Sequence[Prediction],
    ) -> Proposition:
        """Form the AP-native semantic commitment before renderer use.

        This is deliberately modest: it records what was actually received
        and the current evidence boundary.  It never promotes an advisor's
        prose or an unobserved outcome to reality.
        """

        confidence = 1.0 if event.completeness == "complete" else 0.45 if event.completeness == "partial" else 0.1
        # Prediction confidence says how well AP expects the event's context;
        # it does not weaken direct evidence that the event itself occurred.
        # Keep the axes separate so a surprising first observation remains a
        # well-grounded observation rather than becoming an uncertain fact.
        uncertainty = round(_clamp(1.0 - confidence), 6)
        content = sa.text[:512] if sa.text else "当前事件尚无可读内容"
        return Proposition(
            # One canonical event has one proposition identity.  The event id
            # is used only as a recovery lineage key; it never contributes to
            # scores, truth, recall rank or semantic content.
            proposition_id=f"prop_{event.event_id}",
            content=content,
            claim_kind="internal_observation" if event.source == "internal" else "observation",
            subject_scope=event.subject_scope,
            source_refs=(event.event_id,),
            evidence_refs=tuple(dict.fromkeys((*event.evidence_refs, *sa.evidence_refs))),
            confidence=round(_clamp(confidence), 6),
            uncertainty=uncertainty,
            expected_outcome={"feelings": [item.name for item in feelings]},
            frozen=False,
            owner="ap_native",
        )

    def _make_expression_draft(self, proposition: Proposition, event: EventEnvelope) -> ExpressionDraft:
        """Create a bounded, non-public draft tied to one proposition.

        A draft is an intention surface, not an automatic reply.  It remains
        private until a communicative candidate wins the same ActionArena.
        """

        base_text = proposition.content[:512]
        text = base_text
        curriculum_refs: tuple[str, ...] = ()
        pattern_diff: Mapping[str, Any] | None = None
        pattern_tone = "neutral"
        eligible_for_pattern = event.source == "external" and event.role == "observation"
        for trial in self.cognitive_curriculum_trials.get("project.expression", ())[:1] if eligible_for_pattern else ():
            patterns = trial.get("expression_patterns")
            if isinstance(patterns, (str, bytes)) or not isinstance(patterns, Sequence):
                continue
            pattern = next((item for item in patterns[:1] if isinstance(item, Mapping)), None)
            if pattern is None:
                continue
            template = pattern.get("template")
            prefix = pattern.get("prefix")
            suffix = pattern.get("suffix")
            if (
                not isinstance(template, str)
                or template.count("{claim}") != 1
                or not isinstance(prefix, str)
                or not isinstance(suffix, str)
                or template != f"{prefix}{{claim}}{suffix}"
            ):
                continue
            text = f"{prefix}{base_text}{suffix}"[:1024]
            raw_tone = pattern.get("tone", "neutral")
            pattern_tone = str(raw_tone)[:80] if isinstance(raw_tone, str) else "neutral"
            curriculum_id = trial.get("curriculum_id")
            curriculum_refs = (
                (str(curriculum_id),)
                if isinstance(curriculum_id, str) and curriculum_id
                else ()
            )
            pattern_diff = {
                "template": template,
                "slot_bindings": {"claim": base_text},
                "surface_before": base_text,
                "surface_after": text,
                "proposition_content": base_text,
                "curriculum_refs": list(curriculum_refs),
                "semantic_change": False,
            }
            self.cognitive_trial_observations.setdefault("project.expression", []).append(
                {
                    "curriculum_refs": list(curriculum_refs),
                    "effect_key": f"expression_pattern:{str(curriculum_id or 'unknown')}",
                    "base_surface": base_text,
                    "final_surface": text,
                    "proposition_ref": proposition.proposition_id,
                    "curriculum_delta": 0.18,
                    "status": "entered_expression_draft",
                }
            )
            break
        if self.text_unit_granularity == "character":
            units = tuple(text[: self.max_output_units])
        elif self.text_unit_granularity == "word":
            # The generic codec intentionally does not claim a language-aware
            # segmenter.  Preserve whitespace-delimited words where present;
            # otherwise fall back to characters so every unit is inspectable.
            split = tuple(part for part in text.split(" ") if part)
            units = (split if len(split) > 1 else tuple(text))[: self.max_output_units]
        else:
            units = tuple(text[i : i + 32] for i in range(0, len(text), 32))[: self.max_output_units]
        if self.text_unit_granularity == "character":
            base_units = tuple(base_text[: self.max_output_units])
        elif self.text_unit_granularity == "word":
            base_split = tuple(part for part in base_text.split(" ") if part)
            base_units = (base_split if len(base_split) > 1 else tuple(base_text))[: self.max_output_units]
        else:
            base_units = tuple(base_text[i : i + 32] for i in range(0, len(base_text), 32))[: self.max_output_units]
        return ExpressionDraft(
            draft_id=f"draft_{event.event_id}",
            proposition_ref=proposition.proposition_id,
            units=units,
            proposition_units=base_units,
            unit_granularity=self.text_unit_granularity,
            tone=pattern_tone,
            public_allowed=False,
            renderer_source="assisted_curriculum" if curriculum_refs else "ap_native",
            diff=(
                {
                    **dict(pattern_diff or {}),
                    "added": [],
                    "removed": [],
                    "retained_claim_units": list(base_units),
                }
                if curriculum_refs
                else {"added": [], "removed": [], "retained": list(units)}
            ),
            status="assisted_draft" if curriculum_refs else "draft",
        )

    @staticmethod
    def _expression_revision_from_event(event: EventEnvelope) -> ExpressionRevisionProposal | None:
        if not isinstance(event.payload_inline, Mapping):
            return None
        raw = event.payload_inline.get("expression_revision")
        if not isinstance(raw, Mapping):
            return None
        target = raw.get("target_draft_id")
        start = raw.get("start_index")
        delete = raw.get("delete_count")
        replacement = raw.get("replacement_units")
        expected = raw.get("proposition_expected_units")
        if not isinstance(target, str) or not target.strip():
            return None
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            return None
        if isinstance(delete, bool) or not isinstance(delete, int) or delete < 0:
            return None
        if isinstance(replacement, (str, bytes)) or not isinstance(replacement, (list, tuple)):
            return None
        units = tuple(str(item) for item in replacement[:256] if isinstance(item, str) and item)
        if isinstance(expected, (str, bytes)) or not isinstance(expected, (list, tuple)):
            return None
        expected_units = tuple(str(item) for item in expected[:256] if isinstance(item, str) and item)
        if not units and delete == 0:
            return None
        mismatch = raw.get("mismatch", 0.0)
        confidence = raw.get("confidence", 0.0)
        if isinstance(mismatch, bool) or not isinstance(mismatch, (int, float)):
            mismatch = 0.0
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            confidence = 0.0
        proposal_id = raw.get("proposal_id")
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            proposal_id = f"revision_{event.event_id}"
        raw_source = raw.get("source")
        source = str(raw_source)[:80] if isinstance(raw_source, str) and raw_source.strip() else event.source
        raw_evidence = raw.get("evidence_refs", ())
        raw_lineage = raw.get("lineage_refs", ())
        evidence = tuple(
            str(item)[:256]
            for item in raw_evidence
            if isinstance(item, str) and item
        ) if isinstance(raw_evidence, (list, tuple)) else ()
        lineage = tuple(
            str(item)[:256]
            for item in raw_lineage
            if isinstance(item, str) and item
        ) if isinstance(raw_lineage, (list, tuple)) else ()
        return ExpressionRevisionProposal(
            proposal_id=proposal_id,
            target_draft_id=target,
            start_index=start,
            delete_count=delete,
            replacement_units=units,
            preserves_proposition=bool(raw.get("preserves_proposition", False)),
            proposition_expected_units=expected_units,
            mismatch=_clamp(float(mismatch)),
            confidence=_clamp(float(confidence)),
            source=source,
            evidence_refs=tuple(dict.fromkeys((*event.evidence_refs, *evidence, event.event_id))),
            lineage_refs=tuple(dict.fromkeys((*event.lineage_refs, *lineage, event.event_id))),
            reason=str(raw.get("reason", ""))[:512],
        )

    def _text_output_affordances(
        self,
        event: EventEnvelope,
        proposition: Proposition,
        draft: ExpressionDraft,
        frame_view: Mapping[str, Any],
    ) -> tuple[ActionCandidate, ...]:
        """Expose expression actions without selecting or dispatching them.

        Beginning an expression is allowed only when the environment explicitly
        marks this event as communicative and a text actuator exists.  Follow-up
        units are exposed only by the prior unit's physical readback.  The
        payload does not get to install a winner; every option still enters the
        ordinary score ledger and ``DecisionSlot``.
        """

        if self.text_actuator is None:
            return ()
        pressure = _clamp(float(frame_view.get("pressure", 0.0) or 0.0))
        uncertainty = _clamp(float(frame_view.get("uncertainty", 0.0) or 0.0))
        revision = self._expression_revision_from_event(event)
        social_relevance = 0.0
        if isinstance(event.extra.get("communicative_relevance"), (int, float)) and not isinstance(
            event.extra.get("communicative_relevance"), bool
        ):
            social_relevance = _clamp(float(event.extra.get("communicative_relevance")))

        if event.source not in {"readback", "internal"} and event.role != "result":
            if not bool(event.extra.get("text_reply_affordance", False)):
                return ()
            return (
                ActionCandidate(
                    candidate_id=f"action_{event.event_id}_begin_expression",
                    kind="begin_expression",
                    target="text.output",
                    proposition="提交当前 AP 命题并输出第一个表达单元",
                    components={
                        "goal_fit": 0.34 + 0.38 * social_relevance,
                        "evidence_fit": proposition.confidence,
                        "novelty": 0.08,
                        "closure_gain": 0.28 + 0.24 * social_relevance,
                        "uncertainty_cost": 1.0 - uncertainty,
                        "pressure_relief": pressure * 0.2,
                        "risk_cost": 0.02 + 0.08 * uncertainty,
                    },
                    expected_outcome={
                        "draft_id": draft.draft_id,
                        "proposition_ref": proposition.proposition_id,
                        "unit_index": 0,
                    },
                    source="ap_native",
                    owner="ap_native",
                    idempotency_key=f"expression-begin:{draft.draft_id}",
                ),
            )

        if event.source != "readback" or event.role != "result" or not isinstance(event.payload_inline, Mapping):
            return ()
        draft_id = event.payload_inline.get("draft_id")
        unit_index = event.payload_inline.get("unit_index")
        if not isinstance(draft_id, str) or isinstance(unit_index, bool) or not isinstance(unit_index, int):
            return ()
        restored = restore_output(self.store, draft_id)
        if restored is None:
            return ()
        stored_draft, cursor = restored
        if cursor.status in {"completed", "withdrawn"} or cursor.next_index >= len(stored_draft.units):
            return ()
        control_kind = event.payload_inline.get("control_kind")
        expected_cursor_index = unit_index if control_kind == "revise_expression" else unit_index + 1
        if cursor.next_index != expected_cursor_index:
            return ()
        common = {
            "draft_id": draft_id,
            "proposition_ref": stored_draft.proposition_ref,
            "unit_index": cursor.next_index,
        }
        remaining_ratio = (len(stored_draft.units) - cursor.next_index) / max(1, len(stored_draft.units))
        candidates: list[ActionCandidate] = []
        revision_rejections = (
            self._expression_revision_rejections(stored_draft, cursor, revision)
            if revision is not None
            else ("revision_absent",)
        )
        valid_revision = revision is not None and not revision_rejections
        if valid_revision and revision is not None:
            candidates.append(
                ActionCandidate(
                    candidate_id=f"action_{event.event_id}_revise_expression",
                    kind="revise_expression",
                    target="text.output",
                    proposition="根据来源明确的错配 proposal 修订尚未输出的表达后缀",
                    components={
                        "goal_fit": 0.34 + 0.34 * revision.confidence,
                        "evidence_fit": revision.confidence,
                        "novelty": 0.08,
                        "closure_gain": 0.28 + 0.48 * revision.mismatch,
                        "uncertainty_cost": revision.confidence,
                        "pressure_relief": pressure * revision.mismatch,
                        "risk_cost": 0.02 + 0.08 * (1.0 - revision.confidence),
                    },
                    expected_outcome={**common, "revision": revision.to_dict()},
                    source=revision.source,
                    owner="ap_native",
                    idempotency_key=f"expression-revise:{draft_id}:{revision.proposal_id}",
                )
            )
        if cursor.status != "superseded":
            candidates.extend((
            ActionCandidate(
                candidate_id=f"action_{event.event_id}_continue_expression",
                kind="continue_expression",
                target="text.output",
                proposition="根据最新 readback 继续输出下一个表达单元",
                components={
                    "goal_fit": 0.58,
                    "evidence_fit": 0.86,
                    "novelty": 0.02,
                    "closure_gain": 0.38 + 0.2 * remaining_ratio - (0.34 * revision.mismatch if valid_revision and revision else 0.0),
                    "uncertainty_cost": 0.82,
                    "pressure_relief": pressure * 0.18,
                    "risk_cost": 0.02,
                },
                expected_outcome=common,
                source="ap_native",
                owner="ap_native",
                idempotency_key=f"expression-continue:{draft_id}:{cursor.next_index}",
            ),
            ActionCandidate(
                candidate_id=f"action_{event.event_id}_pause_expression",
                kind="pause_expression",
                target="text.output",
                proposition="暂停当前表达，保留游标等待更多认知信息",
                components={
                    "goal_fit": 0.18,
                    "evidence_fit": uncertainty,
                    "novelty": 0.04,
                    "closure_gain": 0.08,
                    "uncertainty_cost": uncertainty,
                    "pressure_relief": pressure * 0.12,
                    "risk_cost": 0.0,
                },
                expected_outcome=common,
                source="ap_native",
                owner="ap_native",
                idempotency_key=f"expression-pause:{draft_id}:{cursor.next_index}",
            ),
            ActionCandidate(
                candidate_id=f"action_{event.event_id}_withdraw_expression",
                kind="withdraw_expression",
                target="text.output",
                proposition="撤回尚未输出的表达单元并保留已发生的 readback",
                components={
                    "goal_fit": 0.08,
                    "evidence_fit": uncertainty,
                    "novelty": 0.02,
                    "closure_gain": 0.04,
                    "uncertainty_cost": uncertainty * 1.1,
                    "pressure_relief": pressure * 0.08,
                    "risk_cost": 0.0,
                },
                expected_outcome=common,
                source="ap_native",
                owner="ap_native",
                idempotency_key=f"expression-withdraw:{draft_id}:{cursor.next_index}",
            ),
            ))
        return tuple(candidates)

    def _expression_revision_rejections(
        self,
        draft: ExpressionDraft,
        cursor: OutputCursor,
        revision: ExpressionRevisionProposal,
    ) -> tuple[str, ...]:
        reasons = list(
            output_revision_rejection_reasons(
                draft,
                cursor,
                revision,
                min_confidence=self.revision_min_confidence,
                min_mismatch=self.revision_min_mismatch,
            )
        )
        if (
            cursor.status == "superseded"
            and "revision_target_terminal" in reasons
            and output_revision_is_recoverable(self.store, revision)
        ):
            reasons.remove("revision_target_terminal")
        return tuple(reasons)

    def _expression_revision_review(self, event: EventEnvelope) -> dict[str, Any] | None:
        """Expose proposal eligibility without granting it action authority."""

        revision = self._expression_revision_from_event(event)
        if revision is None:
            return None
        reasons: tuple[str, ...]
        target = restore_output(self.store, revision.target_draft_id)
        if event.source != "readback" or event.role != "result":
            reasons = ("revision_requires_physical_readback_tick",)
        elif target is None:
            reasons = ("revision_target_missing",)
        else:
            target_draft, target_cursor = target
            reasons = self._expression_revision_rejections(target_draft, target_cursor, revision)
        return {
            "proposal_id": revision.proposal_id,
            "target_draft_id": revision.target_draft_id,
            "source": revision.source,
            "evidence_refs": list(revision.evidence_refs),
            "confidence": revision.confidence,
            "mismatch": revision.mismatch,
            "eligible": not reasons,
            "reasons": list(reasons),
        }

    def _execute_text_output(
        self,
        candidate: ActionCandidate,
        current_draft: ExpressionDraft,
    ) -> OutputUnitResult:
        if self.text_actuator is None:
            raise ContractError("text_output_actuator_unavailable")
        draft_id = str(candidate.expected_outcome.get("draft_id", ""))
        if candidate.kind == "begin_expression":
            if current_draft.draft_id != draft_id:
                raise ContractError("expression_candidate_draft_conflict")
            committed_draft = replace(current_draft, public_allowed=True, status="native_committed")
            cursor = prepare_output(committed_draft, outward_commitment=True, store=self.store)
            return dispatch_next_unit(
                self.store,
                self.text_actuator,
                committed_draft,
                cursor,
                expected_index=int(candidate.expected_outcome.get("unit_index", 0)),
                readback_annotator=self.text_readback_annotator,
            )
        restored = restore_output(self.store, draft_id)
        if restored is None:
            raise ContractError("expression_outbox_missing")
        draft, cursor = restored
        if candidate.kind == "continue_expression":
            if cursor.status == "paused":
                cursor = set_output_status(self.store, draft, cursor, status="ready")
            return dispatch_next_unit(
                self.store,
                self.text_actuator,
                draft,
                cursor,
                expected_index=int(candidate.expected_outcome.get("unit_index", cursor.next_index)),
                readback_annotator=self.text_readback_annotator,
            )
        if candidate.kind == "pause_expression":
            cursor = set_output_status(self.store, draft, cursor, status="paused")
            return output_control_result(
                self.store,
                draft,
                cursor,
                action_ref=candidate.candidate_id,
                action_kind=candidate.kind,
                idempotency_key=candidate.idempotency_key,
                environment_id=str(getattr(self.text_actuator, "environment_id", "text-output")),
            )
        if candidate.kind == "withdraw_expression":
            cursor = set_output_status(self.store, draft, cursor, status="withdrawn")
            return output_control_result(
                self.store,
                draft,
                cursor,
                action_ref=candidate.candidate_id,
                action_kind=candidate.kind,
                idempotency_key=candidate.idempotency_key,
                environment_id=str(getattr(self.text_actuator, "environment_id", "text-output")),
            )
        if candidate.kind == "revise_expression":
            raw = candidate.expected_outcome.get("revision")
            if not isinstance(raw, Mapping):
                raise ContractError("expression_revision_payload_missing")
            replacement = raw.get("replacement_units", ())
            expected = raw.get("proposition_expected_units", ())
            proposal = ExpressionRevisionProposal(
                proposal_id=str(raw.get("proposal_id", "")),
                target_draft_id=str(raw.get("target_draft_id", "")),
                start_index=int(raw.get("start_index", -1)),
                delete_count=int(raw.get("delete_count", -1)),
                replacement_units=tuple(str(item) for item in replacement) if isinstance(replacement, (list, tuple)) else (),
                preserves_proposition=bool(raw.get("preserves_proposition", False)),
                proposition_expected_units=tuple(str(item) for item in expected) if isinstance(expected, (list, tuple)) else (),
                mismatch=_clamp(float(raw.get("mismatch", 0.0) or 0.0)),
                confidence=_clamp(float(raw.get("confidence", 0.0) or 0.0)),
                source=str(raw.get("source", "unknown")),
                evidence_refs=tuple(str(item) for item in raw.get("evidence_refs", ()) if isinstance(item, str)),
                lineage_refs=tuple(str(item) for item in raw.get("lineage_refs", ()) if isinstance(item, str)),
                reason=str(raw.get("reason", "")),
            )
            revised_draft, revised_cursor = apply_output_revision(self.store, proposal)
            return output_control_result(
                self.store,
                revised_draft,
                revised_cursor,
                action_ref=candidate.candidate_id,
                action_kind=candidate.kind,
                idempotency_key=candidate.idempotency_key,
                environment_id=str(getattr(self.text_actuator, "environment_id", "text-output")),
            )
        raise ContractError("expression_action_kind_unsupported")

    def _make_thought_stream(
        self,
        thought: ThoughtFrame,
        proposition: Proposition,
        draft: ExpressionDraft,
        attention: Mapping[str, Any],
        event: EventEnvelope,
    ) -> ThoughtStreamFrame:
        prior = (self.last_thought_stream.frame_id,) if self.last_thought_stream is not None else ()
        status = "extend" if event.source == "internal" and prior else "open"
        if thought.status in {"revise", "negate", "merge", "defer", "close", "abandon"}:
            status = thought.status
        return ThoughtStreamFrame(
            frame_id=f"thought_stream_{uuid4().hex}",
            predecessor_refs=prior,
            event_refs=(event.event_id,),
            status=status,
            proposition_ref=proposition.proposition_id,
            expression_draft_ref=draft.draft_id,
            residuals=thought.unresolved,
            attention_ref=str(attention.get("focus_ref")) if attention.get("focus_ref") is not None else None,
            source="ap_native",
            owner="ap_native",
            completeness="complete" if event.completeness == "complete" else event.completeness,
        )

    def _attention_candidates(
        self,
        event: EventEnvelope,
        attention: Mapping[str, Any],
    ) -> tuple[ActionCandidate, ...]:
        if event.source != "external" or event.role != "observation":
            return ()
        raw = attention.get("candidates")
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            return ()
        output: list[ActionCandidate] = []
        for item in raw[: self.attention_policy.max_candidates]:
            if not isinstance(item, Mapping):
                continue
            mode = item.get("mode")
            target_ref = item.get("target_ref")
            gain = item.get("gain")
            if (
                mode not in {"maintain_attention", "shift_attention", "diversify_attention"}
                or not isinstance(target_ref, str)
                or isinstance(gain, bool)
                or not isinstance(gain, (int, float))
            ):
                continue
            output.append(
                ActionCandidate(
                    candidate_id=str(item.get("candidate_id") or f"action_{event.event_id}_{mode}"),
                    kind=str(mode),
                    target=target_ref,
                    proposition={
                        "maintain_attention": "继续保持对当前输入的有限注意",
                        "shift_attention": "将持续注意转向当前最相关的记忆或范式实例",
                        "diversify_attention": "把有限注意用于检查尚未绑定或未闭合的信息",
                    }[str(mode)],
                    components={
                        "goal_fit": _clamp(float(gain)),
                        "evidence_fit": _clamp(float(gain)),
                        "novelty": _clamp(float(item.get("gain_ledger", {}).get("novelty", 0.0)) * 4.0)
                        if isinstance(item.get("gain_ledger"), Mapping)
                        else 0.0,
                        "closure_gain": _clamp(float(item.get("gain_ledger", {}).get("open_goal_pressure", 0.0)) * 4.0)
                        if isinstance(item.get("gain_ledger"), Mapping)
                        else 0.0,
                        "uncertainty_cost": _clamp(1.0 - float(item.get("risk", 0.0))),
                        "pressure_relief": _clamp(float(gain)),
                        "risk_cost": _clamp(float(item.get("risk", 0.0))),
                    },
                    expected_outcome={
                        "internal": True,
                        "attention_transition": True,
                        "attention_target_ref": target_ref,
                        "attention_mode": mode,
                        "gain_ledger": dict(item.get("gain_ledger", {}))
                        if isinstance(item.get("gain_ledger"), Mapping)
                        else {},
                        "curriculum_refs": list(item.get("curriculum_refs", ()))
                        if isinstance(item.get("curriculum_refs"), Sequence)
                        and not isinstance(item.get("curriculum_refs"), (str, bytes))
                        else [],
                    },
                    source="ap_native",
                    owner="ap_native",
                    idempotency_key=f"internal-attention:{event.event_id}:{mode}:{target_ref}",
                )
            )
        return tuple(output)

    def _internal_candidates(self, event: EventEnvelope, frame_view: Mapping[str, Any]) -> tuple[ActionCandidate, ...]:
        """Expose bounded internal actions to the same arena as all actions."""

        if event.source != "internal":
            return ()
        uncertainty = _clamp(float(frame_view.get("uncertainty", 0.0) or 0.0))
        pressure = _clamp(float(frame_view.get("pressure", 0.0) or 0.0))
        base = f"internal:{event.event_id}"
        return (
            ActionCandidate(
                candidate_id=f"action_{event.event_id}_continue",
                kind="continue_thought",
                target="self.thought_stream",
                proposition="继续检查并修订当前未闭合认知前沿",
                components={
                    "goal_fit": 0.52,
                    "evidence_fit": 1.0 - uncertainty,
                    "novelty": 0.18,
                    "closure_gain": 0.48,
                    "uncertainty_cost": 1.0 - uncertainty,
                    "pressure_relief": pressure,
                    "risk_cost": 0.0,
                },
                expected_outcome={"internal": True, "event_ref": event.event_id},
                source="ap_native",
                owner="ap_native",
                idempotency_key=f"{base}:continue",
            ),
            ActionCandidate(
                candidate_id=f"action_{event.event_id}_wait",
                kind="wait",
                target="self.thought_stream",
                proposition="暂时等待更多证据，不强行闭合解释",
                components={
                    "goal_fit": 0.22,
                    "evidence_fit": uncertainty,
                    "novelty": 0.05,
                    "closure_gain": 0.08,
                    "uncertainty_cost": uncertainty,
                    "pressure_relief": pressure * 0.2,
                    "risk_cost": -0.01,
                },
                expected_outcome={"internal": True, "event_ref": event.event_id},
                source="ap_native",
                owner="ap_native",
                idempotency_key=f"{base}:wait",
            ),
        )

    @staticmethod
    def _internal_receipt(candidate: ActionCandidate, environment_id: str, idempotency_key: str) -> DispatchReceipt:
        """Create a mechanical receipt for an internal cognition action.

        Internal continuation is still an action in the shared arena, but it
        must never be mistaken for a platform side effect.  The explicit
        connector marker makes that boundary inspectable and replayable.
        """

        return DispatchReceipt(
            action_ref=candidate.candidate_id,
            environment_id=environment_id,
            idempotency_key=idempotency_key,
            status="accepted",
            connector_ref="ap-internal",
            evidence_refs=(f"internal-action:{candidate.candidate_id}",),
            extra={"internal": True, "kind": candidate.kind},
        )

    @staticmethod
    def _internal_result(receipt: DispatchReceipt, candidate: ActionCandidate) -> ResultEvent:
        payload = {
            "internal": True,
            "kind": candidate.kind,
            "action_ref": candidate.candidate_id,
        }
        if candidate.expected_outcome.get("attention_transition") is True:
            payload.update(
                {
                    "attention_transition": True,
                    "attention_target_ref": candidate.expected_outcome.get("attention_target_ref"),
                    "attention_mode": candidate.expected_outcome.get("attention_mode"),
                    "gain_ledger": dict(candidate.expected_outcome.get("gain_ledger", {}))
                    if isinstance(candidate.expected_outcome.get("gain_ledger"), Mapping)
                    else {},
                    "curriculum_refs": list(candidate.expected_outcome.get("curriculum_refs", ()))
                    if isinstance(candidate.expected_outcome.get("curriculum_refs"), Sequence)
                    and not isinstance(candidate.expected_outcome.get("curriculum_refs"), (str, bytes))
                    else [],
                }
            )
        return ResultEvent(
            receipt_ref=receipt.receipt_id,
            action_ref=receipt.action_ref,
            environment_id=receipt.environment_id,
            status="success",
            payload_inline=payload,
            evidence_refs=receipt.evidence_refs,
            completeness="complete",
            lineage_refs=(receipt.receipt_id, receipt.action_ref),
            extra={"internal": True},
        )

    def _frontier_eligibility(self) -> dict[str, Any]:
        """Return a finite, explainable wake decision without timer chatter."""

        open_frontiers = [item for item in self._last_frontiers if str(item.get("status")) == "open"]
        residual = sum(len(item.get("unresolved", ())) for item in open_frontiers if isinstance(item, Mapping))
        open_processes = len(self.store.list_open_processes())
        uncertainty = _clamp(1.0 - float(self.last_proposition.confidence)) if self.last_proposition else 0.0
        pressure = _clamp(self.slow_affect.get("pressure", 0.0))
        score = _clamp(0.48 * min(1.0, residual / 2.0) + 0.22 * min(1.0, open_processes / 2.0) + 0.18 * uncertainty + 0.12 * pressure)
        eligible = bool(open_frontiers or open_processes) and score >= self.wake_threshold and self._wake_attempts < self.max_wake_attempts
        reasons: list[str] = []
        if open_frontiers:
            reasons.append("open_frontier")
        if residual:
            reasons.append("unresolved_residual")
        if open_processes:
            reasons.append("open_process")
        if uncertainty >= 0.5:
            reasons.append("uncertainty")
        if self._wake_attempts >= self.max_wake_attempts:
            reasons.append("wake_budget_exhausted")
        return {
            "eligible": eligible,
            "score": round(score, 6),
            "reasons": reasons,
            "open_frontiers": len(open_frontiers),
            "wake_attempts": self._wake_attempts,
            "remaining_budget": max(0, self.max_wake_attempts - self._wake_attempts),
        }

    def continue_internal(self) -> TickResult | None:
        """Run at most one bounded internal wake through the ordinary tick."""

        eligibility = self._frontier_eligibility()
        if not eligibility["eligible"]:
            self.last_internal_status = "idle" if eligibility["remaining_budget"] > 0 else "search_incomplete"
            return None
        predecessor = self.last_thought_stream.frame_id if self.last_thought_stream else self.last_frame_id
        if not predecessor:
            self.last_internal_status = "idle"
            return None
        self._wake_attempts += 1
        self._internal_sequence += 1
        residuals = []
        for frontier in self._last_frontiers:
            if isinstance(frontier, Mapping):
                residuals.extend(str(item) for item in frontier.get("unresolved", ())[:8])
        text = "；".join(dict.fromkeys(residuals)) or "重新检查当前未闭合前沿"
        event = EventEnvelope(
            runtime_id=self.runtime_id,
            organism_id=self.organism_id,
            environment_id=getattr(self.environment, "environment_id", "internal"),
            episode_id=self.episode_id,
            source="internal",
            role="observation",
            modality="thought",
            payload_inline={
                "text": text,
                "predecessor_ref": predecessor,
                "sequence": self._internal_sequence,
                "eligibility": eligibility,
            },
            evidence_refs=(f"internal:{predecessor}",),
            lineage_refs=(predecessor,),
            privacy_scope="private",
            completeness="complete",
            idempotency_key=f"internal-wake:{predecessor}:{self._wake_attempts}",
        )
        self.last_internal_status = "running"
        return self.tick(event)

    def run_internal_budget(self, budget: int | None = None) -> list[TickResult]:
        """Run a finite series of internal ticks; never loops indefinitely."""

        limit = self.max_internal_ticks if budget is None else int(budget)
        if isinstance(budget, bool) or limit < 0 or limit > self.max_internal_ticks:
            raise ContractError("internal_budget_out_of_bounds")
        results: list[TickResult] = []
        for _ in range(limit):
            result = self.continue_internal()
            if result is None:
                break
            results.append(result)
        remaining = self._frontier_eligibility()
        frontier_still_open = bool(remaining["open_frontiers"])
        if len(results) >= limit and frontier_still_open:
            self.last_internal_status = "search_incomplete"
        elif results and not remaining["eligible"]:
            self.last_internal_status = "idle"
        elif not results and self.last_internal_status == "running":
            self.last_internal_status = "idle"
        return results

    def _delegation(self, event: EventEnvelope) -> tuple[DelegationDecision | None, str]:
        if self.governance is None:
            return None, "no_governance_record"
        # Governance compatibility is necessary but not sufficient.  A
        # capability snapshot at ``local_primary``/``audit_only``/``reteach``
        # may intentionally keep the provider off (or only auditing) even
        # while the project itself is compatible.  Never silently elevate such
        # a snapshot into a delegated winner.
        if not self.capability.llm_allowed:
            return None, "capability_llm_disabled"
        if self.capability.stage == "audit_only":
            return None, "capability_audit_only"
        if not self.governance.llm_delegation_enabled or self.governance.compatibility != "compatible":
            return None, "governance_pending"
        return (
            DelegationDecision(
                capability_key="hybrid.cognition",
                mode="assisted" if self.capability.stage == "assisted" else "substituted",
                enabled=True,
                risk_level="low",
                scope=(f"environment:{event.environment_id}",),
                input_refs=(event.event_id,),
                allowed_decisions=("select_semantic_candidate",),
                budget={"tokens": 1200, "milliseconds": 5000},
                expires_at_tick=self.tick_index + 1,
                fallback="local_or_defer",
                requested_by="runtime",
            ),
            "delegation_enabled",
        )

    def _select(
        self,
        slot: DecisionSlot,
        candidates: Sequence[ActionCandidate],
        proposal: GatewayProposal | None,
        *,
        delegated: bool,
    ) -> tuple[DecisionSlot, dict[str, Any]]:
        scored: list[tuple[float, ActionCandidate, float, float]] = []
        for candidate in candidates:
            local_score = candidate.score(self.action_weights)
            teacher_delta = 0.0
            if delegated and proposal is not None:
                # A gateway may only bias candidates that the environment
                # actually offered, and the contribution is bounded.  This is
                # a score contribution, not a hidden second winner.
                preference = proposal.candidate_preferences.get(candidate.candidate_id, 0.0)
                if isinstance(preference, (int, float)) and not isinstance(preference, bool):
                    teacher_delta = max(-1.0, min(1.0, float(preference)))
            score = local_score + teacher_delta
            scored.append((round(score, 8), candidate, round(local_score, 8), round(teacher_delta, 8)))
        scored.sort(key=lambda pair: (-pair[0], pair[1].kind, pair[1].candidate_id))
        if not scored:
            return (
                DecisionSlot(
                    capability_key=slot.capability_key,
                    episode_id=slot.episode_id,
                    tick_index=slot.tick_index,
                    candidate_refs=(),
                    status="abstained",
                    delegation_ref=slot.delegation_ref,
                    reason="no_candidates",
                ),
                {"reason": "no_candidates", "scores": [], "selected_candidate_ref": None},
            )
        best_score = scored[0][0]
        ties = [candidate for score, candidate, _, _ in scored if math.isclose(score, best_score, rel_tol=0.0, abs_tol=1e-8)]
        if len(ties) != 1:
            return (
                DecisionSlot(
                    capability_key=slot.capability_key,
                    episode_id=slot.episode_id,
                    tick_index=slot.tick_index,
                    candidate_refs=tuple(item.candidate_id for item in candidates),
                    status="abstained",
                    delegation_ref=slot.delegation_ref,
                    reason="ambiguous_top_score",
                ),
                {
                    "reason": "ambiguous_top_score",
                    "scores": [
                        {
                            "candidate_ref": item.candidate_id,
                            "score": score,
                            "local_score": local_score,
                            "teacher_delta": teacher_delta,
                        }
                        for score, item, local_score, teacher_delta in scored
                    ],
                    "selected_candidate_ref": None,
                },
            )
        selected = ties[0]
        reason = f"bounded_action_score={best_score:.8f}"
        selected_score = next(item for item in scored if item[1].candidate_id == selected.candidate_id)
        if selected_score[3] != 0.0:
            reason += f";teacher_score_delta={selected_score[3]:.8f}"
        if delegated and proposal is not None and proposal.selected_candidate_ref == selected.candidate_id:
            # The teacher preference is explanatory support. The AP's single
            # score ledger still installs the winner, so ownership remains AP.
            reason += ";teacher_preferred_existing_candidate"
        return slot.select(selected.candidate_id, owner="ap_native", reason=reason), {
            "reason": reason,
            "scores": [
                {
                    "candidate_ref": item.candidate_id,
                    "score": score,
                    "local_score": local_score,
                    "teacher_delta": teacher_delta,
                }
                for score, item, local_score, teacher_delta in scored
            ],
            "selected_candidate_ref": selected.candidate_id,
        }

    @staticmethod
    def _teacher_adoption(
        *,
        proposal: GatewayProposal,
        effective_proposal: GatewayProposal,
        recalls: Sequence[RecallCandidate],
        predictions: Sequence[Prediction],
        feelings: Sequence[Feeling],
        thought: ThoughtFrame,
        candidates: Sequence[ActionCandidate],
        decision_meta: Mapping[str, Any],
        delegation_status: str,
        delegated: bool,
    ) -> dict[str, Any]:
        """Project what the teacher proposed versus what this tick adopted.

        The projection is evidence, not another decision layer. Five semantic
        families remain shadow-only in B3; only a bounded preference for an
        already eligible action may enter the ordinary score ledger.
        """

        def candidate_rows(value: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
            return [dict(item) for item in value if isinstance(item, Mapping)]

        semantic = {
            "recall": (
                [item.to_dict() for item in recalls],
                candidate_rows(proposal.recall_candidates),
            ),
            "prediction": (
                [item.to_dict() for item in predictions],
                candidate_rows(proposal.prediction_candidates),
            ),
            "appraisal": (
                [item.to_dict() for item in feelings],
                candidate_rows(proposal.appraisal_candidates),
            ),
            "thought": (
                [thought.to_dict()],
                candidate_rows(proposal.thought_candidates),
            ),
            "paradigm": (
                [],
                candidate_rows(proposal.paradigm_candidates),
            ),
            "attention": (
                [],
                candidate_rows(proposal.attention_candidates),
            ),
            "expression": (
                [],
                candidate_rows(proposal.expression_candidates),
            ),
            "parameter": (
                [],
                candidate_rows(proposal.parameter_candidates),
            ),
            "lesson": (
                [],
                candidate_rows(proposal.lesson_candidates),
            ),
        }
        projection: dict[str, Any] = {}
        for kind, (local_before, teacher_proposed) in semantic.items():
            rejected = []
            for item in teacher_proposed:
                reasons = item.get("rejection_reasons", ())
                reason = (
                    ",".join(str(value) for value in reasons)
                    if isinstance(reasons, Sequence) and not isinstance(reasons, (str, bytes)) and reasons
                    else "shadow_only_current_wave"
                )
                rejected.append({"candidate_id": item.get("candidate_id"), "reason": reason})
            projection[kind] = {
                "mode": "shadow_only",
                "local_before": local_before,
                "teacher_proposed": teacher_proposed,
                "adopted": [],
                "rejected": rejected,
                "reason": "shadow_only_current_wave;requires_later_event_readback_or_user_feedback",
            }

        candidate_ids = {item.candidate_id for item in candidates}
        score_rows = {
            str(item.get("candidate_ref")): dict(item)
            for item in decision_meta.get("scores", ())
            if isinstance(item, Mapping) and isinstance(item.get("candidate_ref"), str)
        }
        teacher_proposed_actions = [
            {"candidate_ref": key, "delta": float(value)}
            for key, value in proposal.candidate_preferences.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        adopted_actions = [
            {
                "candidate_ref": key,
                "delta": float(value),
                "local_score": score_rows.get(key, {}).get("local_score"),
                "final_score": score_rows.get(key, {}).get("score"),
            }
            for key, value in effective_proposal.candidate_preferences.items()
            if delegated and key in candidate_ids and float(value) != 0.0
        ]
        rejected_actions: list[dict[str, Any]] = [
            dict(item)
            for item in proposal.validation_issues
            if isinstance(item, Mapping) and str(item.get("kind", "")).startswith("action")
        ]
        if not delegated:
            rejected_actions.extend(
                {
                    "candidate_ref": item["candidate_ref"],
                    "reason": delegation_status,
                }
                for item in teacher_proposed_actions
            )
        projection["action"] = {
            "mode": "bounded_score_assist" if delegated else "shadow_only",
            "local_before": [
                {
                    "candidate_ref": item.candidate_id,
                    "kind": item.kind,
                    "score": score_rows.get(item.candidate_id, {}).get("local_score"),
                }
                for item in candidates
            ],
            "teacher_proposed": teacher_proposed_actions,
            "teacher_preferred_candidate_ref": proposal.selected_candidate_ref,
            "adopted": adopted_actions,
            "rejected": rejected_actions,
            "reason": "same_action_arena_bounded_score_component" if delegated else delegation_status,
        }
        return {
            "families": projection,
            "summary": {
                "teacher_proposed": sum(
                    len(value.get("teacher_proposed", ()))
                    for value in projection.values()
                ),
                "adopted": sum(len(value.get("adopted", ())) for value in projection.values()),
                "rejected": sum(len(value.get("rejected", ())) for value in projection.values()),
                "action_decision_owner": "ap_native",
                "delegation_status": delegation_status,
            },
        }

    def _save_checkpoint(self, event: EventEnvelope, frame: CognitionFrame) -> None:
        payload = {
            "episode_id": self.episode_id,
            "tick_index": self.tick_index,
            "last_frame_id": frame.frame_id,
            "state_pool": self.state_pool,
            "slow_affect": self.slow_affect,
            "open_thought": frame.thoughts[-1].to_dict() if frame.thoughts else None,
            "outbox": self.store.list_open_output_outbox(limit=64),
            "open_processes": self.store.list_open_processes(),
            "last_outward_commitment": frame.decision.get("selected_candidate_ref"),
            "event_cursor": event.event_id,
            **self.continuity.to_checkpoint(),
        }
        self.store.save_checkpoint(
            checkpoint_id=f"checkpoint_{uuid4().hex}",
            event_cursor=event.event_id,
            tick_index=self.tick_index,
            payload=payload,
            created_at=self._now(),
        )

    def tick(self, event: EventEnvelope) -> TickResult:
        """Process one event through all shallow AP phases exactly once."""

        if not isinstance(event, EventEnvelope):
            raise ContractError("runtime_tick_requires_event_envelope")
        if not event.runtime_id:
            event = EventEnvelope.from_dict({**event.to_dict(), "runtime_id": self.runtime_id})
        if not event.organism_id:
            event = EventEnvelope.from_dict({**event.to_dict(), "organism_id": self.organism_id})
        if not event.episode_id:
            event = EventEnvelope.from_dict({**event.to_dict(), "episode_id": self.episode_id})
        # The runtime's active episode follows the canonical event.  Internal
        # wakes must therefore inherit the same episode as the external event
        # that opened the frontier; otherwise B/C would silently search a
        # different history partition.
        self.episode_id = event.episode_id
        existing = self.store.get_frame_for_event(event.event_id)
        if existing is not None:
            # Idempotent re-ingest returns the persisted frame projection; it
            # does not dispatch a second physical action.
            return self._tick_result_from_persisted_frame(existing)

        # ``append_event`` may canonicalize a retry to an older event with the
        # same idempotency key.  Re-check the frame *after* that operation;
        # otherwise a retry with a fresh transport event_id would run the full
        # cognitive/action path a second time.
        event = self.store.append_event(event)
        existing = self.store.get_frame_for_event(event.event_id)
        if existing is not None:
            return self._tick_result_from_persisted_frame(existing)
        if event.source not in {"internal", "readback"} and event.role != "result":
            # Reset only for a genuinely new external reality event. A
            # duplicate transport retry returns above and cannot silently
            # refill the wake budget.
            self._wake_attempts = 0
            self._internal_sequence = 0
            self.last_internal_status = "idle"
        # Trial observations describe this tick only. The active curricula
        # persist, but an earlier event's withheld/used receipt must not leak
        # into a later frame within the same recovered runtime.
        self.cognitive_trial_observations = {
            str(key): [] for key in self.cognitive_curriculum_trials
        }
        prior_events = self._prior_events(event)
        prior_tokens = set().union(*(decode_event(item).tokens for item in prior_events)) if prior_events else set()
        sa = decode_event(event, prior_tokens=prior_tokens)
        self._update_state(sa, event)
        recalls = self._recall(sa, prior_events)
        predictions = self._predict(sa, recalls, prior_events, event)
        feelings = self._feelings(sa, predictions, event)
        mismatch = max((item.mismatch for item in predictions), default=0.0)
        self.slow_affect["pressure"] = round(_clamp(0.78 * self.slow_affect["pressure"] + 0.22 * mismatch), 6)
        self.slow_affect["flow"] = round(_clamp(0.82 * self.slow_affect["flow"] + 0.18 * (1.0 - mismatch)), 6)
        self.slow_affect["curiosity"] = round(_clamp(0.8 * self.slow_affect["curiosity"] + 0.2 * sa.novelty), 6)
        self.slow_affect["fatigue"] = round(_clamp(0.96 * self.slow_affect["fatigue"] + 0.01), 6)
        # Semantic intent is materialized before the gateway is consulted.
        # This ordering is the key distinction between an AP draft and a
        # renderer-produced role-play answer.
        proposition_native = self._make_proposition(sa, feelings, event, predictions)
        paradigms = self._paradigms(event, proposition_native, recalls)
        attention = self._attention(sa, recalls, predictions, paradigms, event)
        thought = self._thought(sa, recalls, feelings, event)
        expression_draft = self._make_expression_draft(proposition_native, event)
        should_render_expression = (
            self.expression_renderer is not None
            and self.text_actuator is not None
            and event.source not in {"readback", "internal"}
            and event.role != "result"
            and bool(event.extra.get("text_reply_affordance", False))
        )
        if should_render_expression and self.expression_renderer is not None:
            try:
                renderer_proposal = self.expression_renderer.propose(proposition_native, expression_draft)
            except Exception as exc:
                renderer_proposal = RendererProposal(
                    proposition_refs=(proposition_native.proposition_id,),
                    preserved_claim_refs=(proposition_native.proposition_id,),
                    units=(),
                    source="renderer_error",
                    status="partial",
                    limitations=(f"renderer_error:{type(exc).__name__}",),
                )
            if renderer_proposal is not None and not isinstance(renderer_proposal, RendererProposal):
                renderer_proposal = RendererProposal(
                    proposition_refs=(proposition_native.proposition_id,),
                    preserved_claim_refs=(proposition_native.proposition_id,),
                    units=(),
                    source="renderer_invalid_contract",
                    status="partial",
                    limitations=("renderer_returned_invalid_proposal",),
                )
            if renderer_proposal is not None:
                renderer_decision = review_renderer_proposal(
                    proposition_native,
                    expression_draft,
                    renderer_proposal,
                )
                # Accepted rendering changes only the surface units. A rejected
                # proposal retains AP's original units and remains private; the
                # later ActionArena still decides whether to speak.
                proposition_native = renderer_decision.proposition
                expression_draft = renderer_decision.draft
        thought_stream = self._make_thought_stream(
            thought, proposition_native, expression_draft, attention, event
        )
        gateway_event_eligible = (
            event.source not in {"readback", "internal"}
            and event.role != "result"
            and event.extra.get("gateway_consultation_allowed", True) is not False
        )
        delegation, delegation_status = self._delegation(event)
        if not gateway_event_eligible:
            delegation = None
            delegation_status = "gateway_not_consulted_for_control_event"
        frame_view = {
            "sa": sa.to_dict(),
            "b_recall": [item.to_dict() for item in recalls],
            "c_prediction": [item.to_dict() for item in predictions],
            "feelings": [item.to_dict() for item in feelings],
            "attention": attention,
            "thought": thought.to_dict(),
            "slow_affect": dict(self.slow_affect),
            "pressure": self.slow_affect["pressure"],
            "uncertainty": max((1.0 - item.confidence for item in predictions), default=0.0),
            # Minimal expression boundary: the gateway sees the AP-native
            # proposition and private draft, not the full StatePool or hidden
            # checkpoint. A renderer still cannot publish from this view.
            "proposition": proposition_native.to_dict(),
            "expression_draft": expression_draft.to_dict(),
            "paradigms": [item.to_dict() for item in paradigms],
            "activity": dict(event.payload_inline) if isinstance(event.payload_inline, Mapping) else {},
            "activity_profile": self._event_structure_profile(event),
        }
        if self.requested_capabilities is not None:
            frame_view["requested_capabilities"] = list(self.requested_capabilities)
        if self.teacher_sampling:
            frame_view["teacher_sampling"] = dict(self.teacher_sampling)
        # Build the environment's bounded affordances before asking an LLM
        # advisor for preferences.  Otherwise the advisor would receive no
        # candidate references and could not participate in the *same*
        # ActionArena.  It still cannot add a physical candidate by itself;
        # only the environment may expose executable affordances.
        candidates = tuple(self.environment.candidates(event, frame_view))
        candidates = tuple(
            (
                *candidates,
                *self._internal_candidates(event, frame_view),
                *self._attention_candidates(event, attention),
                *self._text_output_affordances(event, proposition_native, expression_draft, frame_view),
            )
        )
        # Review against the pre-competition outbox snapshot. A winning
        # revision atomically supersedes that target later in this tick; a
        # post-action recomputation would incorrectly describe the proposal as
        # terminal after the fact.
        revision_review = self._expression_revision_review(event)
        frame_view["action_candidates"] = [item.to_dict() for item in candidates]

        # If this gateway exposes a stable request identity and a prior call
        # receipt is durable, reuse the canonical proposal after a process
        # restart.  This protects paid/side-effecting provider calls without
        # making the receipt itself a semantic authority.
        proposal = None
        gateway_receipt = None
        if not gateway_event_eligible:
            proposal = GatewayProposal(
                capability_key="hybrid.cognition",
                status="unavailable",
                uncertainty=1.0,
                limitations=(delegation_status,),
                source="llm",
                owner="llm",
            )
        else:
            request_identity = getattr(self.gateway, "request_identity", None)
            if callable(request_identity):
                try:
                    request_key, _, _ = request_identity(frame_view, delegation)
                    stored_receipt = self.store.get_gateway_call(request_key)
                except Exception:
                    stored_receipt = None
                if stored_receipt is not None:
                    gateway_receipt = stored_receipt
                    proposal = stored_receipt.to_proposal()
            if proposal is None:
                proposal = self.gateway.propose(frame_view, delegation)
        if not isinstance(proposal, GatewayProposal):
            proposal = GatewayProposal(capability_key="hybrid.cognition", status="partial", limitations=("invalid_gateway_proposal",))
        if gateway_event_eligible and gateway_receipt is None:
            gateway_receipt = GatewayCallReceipt.from_proposal(
                proposal,
                capability_key="hybrid.cognition",
                input_refs=(event.event_id,),
            )
        if gateway_receipt is not None:
            gateway_receipt = self.store.append_gateway_call(gateway_receipt)
            canonical_proposal = gateway_receipt.to_proposal()
            if canonical_proposal is not None:
                proposal = canonical_proposal
        teacher_proposal = proposal
        if delegation is None:
            # A provider may still explain or teach in cold-start, but cannot
            # silently choose this slot while governance is pending.
            proposal = proposal.without_action_authority(delegation_status)
        candidate_refs = tuple(item.candidate_id for item in candidates)
        slot = DecisionSlot(
            capability_key="environment.action",
            episode_id=event.episode_id,
            tick_index=self.tick_index,
            candidate_refs=candidate_refs,
            delegation_ref=delegation.delegation_id if delegation else None,
        )
        slot, decision_meta = self._select(
            slot,
            candidates,
            proposal,
            delegated=delegation is not None,
        )
        receipt: DispatchReceipt | None = None
        result: ResultEvent | None = None
        result_envelope: EventEnvelope | None = None
        output_result: OutputUnitResult | None = None
        selected = next((item for item in candidates if item.candidate_id == slot.selected_candidate_ref), None)
        if selected is not None:
            if selected.kind in {"begin_expression", "continue_expression", "pause_expression", "withdraw_expression", "revise_expression"}:
                output_result = self._execute_text_output(selected, expression_draft)
                receipt = output_result.receipt
                result = output_result.result
            else:
                # A crash can occur after dispatch but before frame persistence.
                # Consult the durable fence before touching the environment again.
                receipt = self.store.get_dispatch_by_idempotency(selected.idempotency_key)
                if receipt is None:
                    if selected.source == "ap_native" and selected.kind in {
                        "continue_thought", "wait", "maintain_attention", "shift_attention", "diversify_attention"
                    }:
                        proposed_receipt = self._internal_receipt(
                            selected,
                            str(getattr(self.environment, "environment_id", "internal")),
                            selected.idempotency_key,
                        )
                    else:
                        proposed_receipt = self.environment.dispatch(selected, selected.idempotency_key)
                    receipt = self.store.append_dispatch(proposed_receipt)
                result = self.store.get_result_for_receipt(receipt.receipt_id)
                if result is None:
                    if selected.source == "ap_native" and selected.kind in {
                        "continue_thought", "wait", "maintain_attention", "shift_attention", "diversify_attention"
                    }:
                        result = self._internal_result(receipt, selected)
                    else:
                        result = self.environment.readback(receipt)
                    result = self.store.append_result(result)
            if result is not None:
                result_envelope = result.as_event_envelope(
                    runtime_id=self.runtime_id,
                    organism_id=self.organism_id,
                    episode_id=event.episode_id,
                )
                # Result-back is the next input, not an immediate semantic rewrite.
                # The projected result is itself idempotent.  Keep the canonical
                # envelope returned by the store so repeated readback projection
                # cannot create a second event identity.
                result_envelope = self.store.append_event(result_envelope)
                if (
                    selected is not None
                    and selected.expected_outcome.get("attention_transition") is True
                    and result.status == "success"
                    and result.completeness == "complete"
                ):
                    self.continuity.prior_attention = {
                        "target_ref": selected.expected_outcome.get("attention_target_ref"),
                        "mode": selected.expected_outcome.get("attention_mode"),
                        "action_ref": selected.candidate_id,
                        "receipt_ref": receipt.receipt_id if receipt is not None else None,
                        "result_ref": result.result_id,
                        "gain_ledger": dict(selected.expected_outcome.get("gain_ledger", {}))
                        if isinstance(selected.expected_outcome.get("gain_ledger"), Mapping)
                        else {},
                        "curriculum_refs": list(selected.expected_outcome.get("curriculum_refs", ()))
                        if isinstance(selected.expected_outcome.get("curriculum_refs"), Sequence)
                        and not isinstance(selected.expected_outcome.get("curriculum_refs"), (str, bytes))
                        else [],
                    }
        if output_result is not None:
            if output_result.result is None or output_result.result.status != "success":
                closure = "open"
            elif output_result.cursor.status in {"completed", "withdrawn"}:
                closure = "closed"
            else:
                closure = "open"
        elif thought.unresolved:
            closure = "open"
        elif result is None:
            # No physical attempt is not the same as a closed task.  A
            # readback observation (which has no action affordance in the
            # fixture) therefore remains explicitly unknown.
            closure = "open" if selected is not None else "unknown"
        elif result.status == "success" and result.completeness == "complete":
            closure = "closed"
        elif result.status in {"failed", "unknown", "deferred", "search_incomplete"}:
            closure = "open"
        else:
            closure = "unknown"
        # Count only after the action/readback boundary is known, and before
        # freezing the frame projection. Provider waiting is not cognition;
        # each persisted result is one readback observation.
        self.counters.cognitive_ticks += 1
        if result is not None:
            self.counters.readbacks += 1
        if (
            output_result is not None
            and output_result.unit is not None
            and output_result.unit_index is not None
            and output_result.result is not None
            and output_result.result.status == "success"
            and output_result.result.completeness == "complete"
        ):
            self.counters.output_units += 1
        gateway_view = teacher_proposal.to_dict()
        if self.teacher_sampling:
            gateway_view["sampling"] = dict(self.teacher_sampling)
        gateway_view["proposal_limitations"] = list(teacher_proposal.limitations)
        gateway_view["limitations"] = list(proposal.limitations)
        gateway_view["effective_candidate_preferences"] = dict(proposal.candidate_preferences)
        gateway_view["effective_selected_candidate_ref"] = proposal.selected_candidate_ref
        gateway_view["adoption"] = self._teacher_adoption(
            proposal=teacher_proposal,
            effective_proposal=proposal,
            recalls=recalls,
            predictions=predictions,
            feelings=feelings,
            thought=thought,
            candidates=candidates,
            decision_meta=decision_meta,
            delegation_status=delegation_status,
            delegated=delegation is not None,
        )
        if gateway_receipt is not None:
            gateway_view["call_receipt"] = gateway_receipt.to_dict()
        # Keep the AP-native thought and the gateway proposal separate.  The
        # UI may display them side by side, but a fluent helper sentence must
        # not silently become local authorship.
        decision_payload = {
            **slot.to_dict(),
            "scores": decision_meta.get("scores", []),
            "idempotency_key": selected.idempotency_key if selected else None,
            "dispatch_receipt_ref": receipt.receipt_id if receipt else None,
            "result_ref": result.result_id if result else None,
            "result_status": result.status if result else None,
            "result_completeness": result.completeness if result else None,
            "closure": closure,
            "output": output_result.to_dict() if output_result is not None else None,
        }
        if revision_review is not None:
            decision_payload["expression_revision_review"] = revision_review
        score_by_candidate = {
            item["candidate_ref"]: item["score"]
            for item in decision_meta.get("scores", [])
        }
        action_payloads = tuple(
            item.to_dict(score=score_by_candidate.get(item.candidate_id))
            for item in candidates
        )
        frame = CognitionFrame(
            frame_id=f"frame_{uuid4().hex}",
            episode_id=event.episode_id,
            tick_index=self.tick_index,
            trigger_refs=(event.event_id,),
            sa=sa,
            state_pool=dict(self.state_pool),
            current_field={"focus": sa.occurrence_id, "members": [sa.occurrence_id, *[item.ref for item in recalls[:4]]]},
            b_recall=tuple(recalls),
            c_prediction=tuple(predictions),
            feelings=tuple(feelings),
            slow_affect=dict(self.slow_affect),
            attention=attention,
            thoughts=(thought,),
            actions=action_payloads,
            decision=decision_payload,
            gateway=gateway_view,
            frontiers=(
                {
                    "frontier_id": f"frontier_{event.event_id}",
                    "status": "open" if thought.unresolved or closure == "open" else "observed",
                    "kind": "evidence_or_readback",
                    "closure": closure,
                    "result_status": result.status if result else None,
                    "result_completeness": result.completeness if result else None,
                    "unresolved": list(thought.unresolved),
                    "source_refs": [event.event_id, *([result.result_id] if result else [])],
                },
            ),
            phase_completeness={
                "ingest": "complete",
                "codec_sa": "complete",
                "state_pool": "complete",
                "current_field": "complete",
                "b_recall": "complete" if recalls else "empty",
                "c_prediction": "complete" if predictions and predictions[0].completeness == "complete" else "unknown",
                "feelings": "complete",
                "attention": "complete",
                "thought": "complete",
                "decision": "complete" if slot.status in {"selected", "abstained"} else "partial",
                "dispatch": "complete" if receipt is not None else "not_attempted",
                "readback": result.completeness if result is not None else "not_attempted",
            },
            result_ref=result.result_id if result else None,
            completeness="complete" if result is None or result.completeness == "complete" else result.completeness,
            proposition=proposition_native,
            expression_draft=expression_draft,
            thought_stream=thought_stream,
            counters=self.counters.to_dict(),
            cognitive_trial_observations={
                capability: tuple(dict(item) for item in observations)
                for capability, observations in self.cognitive_trial_observations.items()
            },
            paradigms=tuple(paradigms),
        )
        frame_payload = frame.to_dict()
        self.store.append_frame(frame.frame_id, frame.episode_id, frame.tick_index, event.event_id, frame_payload, self._now())
        self.last_frame_id = frame.frame_id
        self.open_thought = thought
        self.last_proposition = proposition_native
        self.last_expression_draft = expression_draft
        self.last_thought_stream = thought_stream
        self._last_frontiers = frame.frontiers
        # A completed internal action may leave the frontier observed.  Do not
        # infer a future wake from an old frontier snapshot after persistence.
        self.tick_index += 1
        self._save_checkpoint(event, frame)
        return TickResult(frame=frame, receipt=receipt, result=result, result_envelope=result_envelope)

    def _tick_result_from_persisted_frame(self, existing: Mapping[str, Any]) -> TickResult:
        """Rehydrate one frame and its mechanical result without side effects."""

        frame = self._frame_from_dict(existing)
        receipt_ref = frame.decision.get("dispatch_receipt_ref")
        receipt = self.store.get_dispatch(str(receipt_ref)) if receipt_ref else None
        key = frame.decision.get("idempotency_key")
        if receipt is None and key:
            receipt = self.store.get_dispatch_by_idempotency(str(key))
        result = self.store.get_result_for_receipt(receipt.receipt_id) if receipt else None
        envelope = None
        if result is not None:
            envelope = self.store.append_event(
                result.as_event_envelope(
                    runtime_id=self.runtime_id,
                    organism_id=self.organism_id,
                    episode_id=frame.episode_id,
                )
            )
        return TickResult(frame=frame, receipt=receipt, result=result, result_envelope=envelope, recovered=True)

    def counters_snapshot(self) -> dict[str, Any]:
        """Return inspectable counters without conflating waiting and thought."""

        return {
            **self.counters.to_dict(),
            "tick_index": self.tick_index,
            "wake_attempts": self._wake_attempts,
            "wake_status": self.last_internal_status,
        }

    def note_provider_wait(self, *, count: int = 1) -> dict[str, Any]:
        """Record provider wait/progress without creating a cognitive frame."""

        if isinstance(count, bool) or not isinstance(count, int) or count < 1 or count > 1000:
            raise ContractError("provider_wait_count_out_of_bounds")
        self.counters.provider_waits += count
        return self.counters_snapshot()

    def note_tool_progress(self, *, count: int = 1) -> dict[str, Any]:
        """Record actual tool progress notifications separately from thought."""

        if isinstance(count, bool) or not isinstance(count, int) or count < 1 or count > 1000:
            raise ContractError("tool_progress_count_out_of_bounds")
        self.counters.tool_progress += count
        return self.counters_snapshot()

    @staticmethod
    def _frame_from_dict(raw: Mapping[str, Any]) -> CognitionFrame:
        sa_raw = raw["sa"]
        sa = SAOccurrence(
            occurrence_id=sa_raw["occurrence_id"], event_ref=sa_raw["event_ref"], modality=sa_raw["modality"],
            text=sa_raw.get("text", ""), features=sa_raw.get("features", {}), tokens=tuple(sa_raw.get("tokens", ())),
            source=sa_raw.get("source", "external"), evidence_refs=tuple(sa_raw.get("evidence_refs", ())),
            lineage_refs=tuple(sa_raw.get("lineage_refs", ())), activation=float(sa_raw.get("activation", 0.0)), novelty=float(sa_raw.get("novelty", 0.0)),
        )
        recalls = tuple(RecallCandidate(ref=x["ref"], event_ref=x["event_ref"], score=float(x["score"]), reason=x["reason"], source=x["source"], summary=str(x.get("summary", ""))[:320], lineage_refs=tuple(x.get("lineage_refs", ())), base_score=float(x["base_score"]) if isinstance(x.get("base_score"), (int, float)) and not isinstance(x.get("base_score"), bool) else None, curriculum_gain=float(x.get("curriculum_gain", 0.0)), curriculum_refs=tuple(x.get("curriculum_refs", ()))) for x in raw.get("b_recall", ()))
        preds = tuple(Prediction(prediction_id=x["prediction_id"], anchor_refs=tuple(x.get("anchor_refs", ())), expected_features=x.get("expected_features", {}), observed_features=x.get("observed_features", {}), confidence=float(x["confidence"]), mismatch=float(x["mismatch"]), direction=x["direction"], source=x.get("source", "ap_native"), completeness=x.get("completeness", "complete"), hypothesis=str(x.get("hypothesis", "")), mode=str(x.get("mode", "transition")), uncertainty=float(x.get("uncertainty", max(0.0, 1.0 - float(x.get("confidence", 0.0))))), base_confidence=float(x["base_confidence"]) if isinstance(x.get("base_confidence"), (int, float)) and not isinstance(x.get("base_confidence"), bool) else None, curriculum_delta=float(x.get("curriculum_delta", 0.0)), curriculum_refs=tuple(x.get("curriculum_refs", ()))) for x in raw.get("c_prediction", ()))
        feelings = tuple(Feeling(name=x["name"], intensity=float(x["intensity"]), valence=float(x["valence"]), rationale=x["rationale"], source_refs=tuple(x.get("source_refs", ())), source=x.get("source", "ap_native"), base_intensity=float(x["base_intensity"]) if isinstance(x.get("base_intensity"), (int, float)) and not isinstance(x.get("base_intensity"), bool) else None, curriculum_delta=float(x.get("curriculum_delta", 0.0)), curriculum_refs=tuple(x.get("curriculum_refs", ()))) for x in raw.get("feelings", ()))
        thoughts = tuple(ThoughtFrame(thought_id=x["thought_id"], status=x["status"], proposition=x["proposition"], predecessor_refs=tuple(x.get("predecessor_refs", ())), evidence_refs=tuple(x.get("evidence_refs", ())), unresolved=tuple(x.get("unresolved", ())), owner=x.get("owner", "ap_native"), source=x.get("source", "ap_native"), base_proposition=str(x.get("base_proposition", x.get("proposition", ""))), curriculum_additions=tuple(x.get("curriculum_additions", ())), curriculum_refs=tuple(x.get("curriculum_refs", ())), uncertainty=float(x.get("uncertainty", 0.0) or 0.0)) for x in raw.get("thoughts", ()))
        proposition_raw = raw.get("proposition")
        proposition = None
        if isinstance(proposition_raw, Mapping):
            proposition = Proposition(
                proposition_id=str(proposition_raw.get("proposition_id", "")),
                content=str(proposition_raw.get("content", "")),
                claim_kind=str(proposition_raw.get("claim_kind", "observation")),
                subject_scope=str(proposition_raw.get("subject_scope", "private")),
                source_refs=tuple(str(x) for x in proposition_raw.get("source_refs", ())),
                evidence_refs=tuple(str(x) for x in proposition_raw.get("evidence_refs", ())),
                confidence=float(proposition_raw.get("confidence", 0.0) or 0.0),
                uncertainty=float(proposition_raw.get("uncertainty", 1.0)),
                expected_outcome=proposition_raw.get("expected_outcome", {}) if isinstance(proposition_raw.get("expected_outcome", {}), Mapping) else {},
                frozen=bool(proposition_raw.get("frozen", False)),
                owner=str(proposition_raw.get("owner", "ap_native")),
            )
        draft_raw = raw.get("expression_draft")
        draft = None
        if isinstance(draft_raw, Mapping):
            draft = ExpressionDraft(
                draft_id=str(draft_raw.get("draft_id", "")),
                proposition_ref=str(draft_raw.get("proposition_ref", "")),
                units=tuple(str(x) for x in draft_raw.get("units", ())),
                proposition_units=tuple(str(x) for x in draft_raw.get("proposition_units", ())),
                unit_granularity=str(draft_raw.get("unit_granularity", "chunk")),
                tone=str(draft_raw.get("tone", "neutral")),
                public_allowed=bool(draft_raw.get("public_allowed", False)),
                renderer_source=str(draft_raw.get("renderer_source", "ap_native")),
                diff=draft_raw.get("diff", {}) if isinstance(draft_raw.get("diff", {}), Mapping) else {},
                status=str(draft_raw.get("status", "draft")),
            )
        stream_raw = raw.get("thought_stream")
        stream = None
        if isinstance(stream_raw, Mapping):
            stream = ThoughtStreamFrame(
                frame_id=str(stream_raw.get("frame_id", "")),
                predecessor_refs=tuple(str(x) for x in stream_raw.get("predecessor_refs", ())),
                event_refs=tuple(str(x) for x in stream_raw.get("event_refs", ())),
                status=str(stream_raw.get("status", "open")),
                proposition_ref=str(stream_raw.get("proposition_ref")) if stream_raw.get("proposition_ref") is not None else None,
                expression_draft_ref=str(stream_raw.get("expression_draft_ref")) if stream_raw.get("expression_draft_ref") is not None else None,
                residuals=tuple(str(x) for x in stream_raw.get("residuals", ())),
                attention_ref=str(stream_raw.get("attention_ref")) if stream_raw.get("attention_ref") is not None else None,
                source=str(stream_raw.get("source", "ap_native")),
                owner=str(stream_raw.get("owner", "ap_native")),
                completeness=str(stream_raw.get("completeness", "complete")),
            )
        trial_observations_raw = raw.get("cognitive_trial_observations", {})
        trial_observations = {
            str(capability): tuple(dict(item) for item in observations if isinstance(item, Mapping))
            for capability, observations in trial_observations_raw.items()
            if isinstance(capability, str)
            and isinstance(observations, Sequence)
            and not isinstance(observations, (str, bytes))
        } if isinstance(trial_observations_raw, Mapping) else {}
        paradigms = tuple(
            ParadigmOccurrence(
                occurrence_id=str(x.get("occurrence_id", "")),
                pattern_kind=str(x.get("pattern_kind", "relation_frame")),
                invariants=x.get("invariants", {}) if isinstance(x.get("invariants"), Mapping) else {},
                bindings=x.get("bindings", {}) if isinstance(x.get("bindings"), Mapping) else {},
                missing_slots=tuple(str(item) for item in x.get("missing_slots", ())),
                relations=tuple(dict(item) for item in x.get("relations", ()) if isinstance(item, Mapping)),
                base_match=float(x.get("base_match", 0.0) or 0.0),
                curriculum_delta=float(x.get("curriculum_delta", 0.0) or 0.0),
                final_match=float(x.get("final_match", 0.0) or 0.0),
                source=str(x.get("source", "assisted_trial")),
                curriculum_refs=tuple(str(item) for item in x.get("curriculum_refs", ())),
                evidence_refs=tuple(str(item) for item in x.get("evidence_refs", ())),
                lineage_refs=tuple(str(item) for item in x.get("lineage_refs", ())),
                status=str(x.get("status", "withheld")),
                completeness=str(x.get("completeness", "unknown")),
            )
            for x in raw.get("paradigms", ())
            if isinstance(x, Mapping)
        )
        return CognitionFrame(frame_id=raw["frame_id"], episode_id=raw["episode_id"], tick_index=int(raw["tick_index"]), trigger_refs=tuple(raw.get("trigger_refs", ())), sa=sa, state_pool=raw.get("state_pool", {}), current_field=raw.get("current_field", {}), b_recall=recalls, c_prediction=preds, feelings=feelings, slow_affect=raw.get("slow_affect", {}), attention=raw.get("attention", {}), thoughts=thoughts, actions=tuple(raw.get("actions", ())), decision=raw.get("decision", {}), gateway=raw.get("gateway", {}), frontiers=tuple(raw.get("frontiers", ())), phase_completeness=raw.get("phase_completeness", {}), process_refs=tuple(raw.get("process_refs", ())), result_ref=raw.get("result_ref"), completeness=raw.get("completeness", "complete"), proposition=proposition, expression_draft=draft, thought_stream=stream, counters=raw.get("counters", {}), cognitive_trial_observations=trial_observations, paradigms=paradigms)


__all__ = ["MindRuntime"]
