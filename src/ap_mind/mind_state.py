"""Recoverable continuity state for one AP Mind runtime.

Keeping this state in a dedicated object prevents the orchestrator from
growing a second set of loosely coupled counters and restore branches.  It is
still a projection owned by ``MindRuntime``; it does not select actions or
create events on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .runtime_types import CognitiveCounters, ExpressionDraft, Proposition, ThoughtStreamFrame


def _texts(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


@dataclass
class ContinuityState:
    proposition: Proposition | None = None
    expression_draft: ExpressionDraft | None = None
    thought_stream: ThoughtStreamFrame | None = None
    counters: CognitiveCounters = field(default_factory=CognitiveCounters)
    frontiers: tuple[Mapping[str, Any], ...] = ()
    wake_attempts: int = 0
    internal_sequence: int = 0
    internal_status: str = "idle"
    prior_attention: Mapping[str, Any] = field(default_factory=dict)

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "counters": self.counters.to_dict(),
            "frontiers": [dict(item) for item in self.frontiers],
            "proposition": self.proposition.to_dict() if self.proposition else None,
            "expression_draft": self.expression_draft.to_dict() if self.expression_draft else None,
            "thought_stream": self.thought_stream.to_dict() if self.thought_stream else None,
            "wake_attempts": self.wake_attempts,
            "internal_sequence": self.internal_sequence,
            "last_internal_status": self.internal_status,
            "prior_attention": dict(self.prior_attention),
        }

    @classmethod
    def from_checkpoint(cls, payload: Mapping[str, Any], *, tick_index: int = 0) -> "ContinuityState":
        counters_raw = payload.get("counters")
        counters = CognitiveCounters(cognitive_ticks=max(0, int(tick_index)))
        if isinstance(counters_raw, Mapping):
            counters = CognitiveCounters(
                cognitive_ticks=max(0, int(counters_raw.get("cognitive_ticks", tick_index) or 0)),
                provider_waits=max(0, int(counters_raw.get("provider_waits", 0) or 0)),
                output_units=max(0, int(counters_raw.get("output_units", 0) or 0)),
                tool_progress=max(0, int(counters_raw.get("tool_progress", 0) or 0)),
                readbacks=max(0, int(counters_raw.get("readbacks", 0) or 0)),
            )

        proposition = None
        raw = payload.get("proposition")
        if isinstance(raw, Mapping):
            proposition = Proposition(
                proposition_id=str(raw.get("proposition_id", "")),
                content=str(raw.get("content", "")),
                claim_kind=str(raw.get("claim_kind", "observation")),
                subject_scope=str(raw.get("subject_scope", "private")),
                source_refs=_texts(raw.get("source_refs")),
                evidence_refs=_texts(raw.get("evidence_refs")),
                confidence=float(raw.get("confidence", 0.0) or 0.0),
                uncertainty=float(raw.get("uncertainty", 1.0)),
                expected_outcome=dict(raw.get("expected_outcome", {})) if isinstance(raw.get("expected_outcome"), Mapping) else {},
                frozen=bool(raw.get("frozen", False)),
                owner=str(raw.get("owner", "ap_native")),
            )

        draft = None
        raw = payload.get("expression_draft")
        if isinstance(raw, Mapping):
            draft = ExpressionDraft(
                draft_id=str(raw.get("draft_id", "")),
                proposition_ref=str(raw.get("proposition_ref", "")),
                units=_texts(raw.get("units")),
                proposition_units=_texts(raw.get("proposition_units")),
                unit_granularity=str(raw.get("unit_granularity", "chunk")),
                tone=str(raw.get("tone", "neutral")),
                public_allowed=bool(raw.get("public_allowed", False)),
                renderer_source=str(raw.get("renderer_source", "ap_native")),
                diff=dict(raw.get("diff", {})) if isinstance(raw.get("diff"), Mapping) else {},
                status=str(raw.get("status", "draft")),
            )

        stream = None
        raw = payload.get("thought_stream")
        if isinstance(raw, Mapping):
            stream = ThoughtStreamFrame(
                frame_id=str(raw.get("frame_id", "")),
                predecessor_refs=_texts(raw.get("predecessor_refs")),
                event_refs=_texts(raw.get("event_refs")),
                status=str(raw.get("status", "open")),
                proposition_ref=str(raw.get("proposition_ref")) if raw.get("proposition_ref") is not None else None,
                expression_draft_ref=str(raw.get("expression_draft_ref")) if raw.get("expression_draft_ref") is not None else None,
                residuals=_texts(raw.get("residuals")),
                attention_ref=str(raw.get("attention_ref")) if raw.get("attention_ref") is not None else None,
                source=str(raw.get("source", "ap_native")),
                owner=str(raw.get("owner", "ap_native")),
                completeness=str(raw.get("completeness", "complete")),
            )

        return cls(
            proposition=proposition,
            expression_draft=draft,
            thought_stream=stream,
            counters=counters,
            frontiers=tuple(item for item in payload.get("frontiers", ()) if isinstance(item, Mapping)),
            wake_attempts=max(0, int(payload.get("wake_attempts", 0) or 0)),
            internal_sequence=max(0, int(payload.get("internal_sequence", 0) or 0)),
            internal_status=str(payload.get("last_internal_status", "idle")),
            prior_attention=(
                dict(payload.get("prior_attention", {}))
                if isinstance(payload.get("prior_attention"), Mapping)
                else {}
            ),
        )


__all__ = ["ContinuityState"]
