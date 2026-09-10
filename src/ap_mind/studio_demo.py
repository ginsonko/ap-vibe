"""Provider-off composition root for the first interactive Mind Studio.

The demo exercises the real ``MindRuntime`` and durable output/readback path.
Its renderer and annotator are explicit local fixtures; they are not presented
as an LLM, visual model or learned language capability.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from .contracts import ContractError, EventEnvelope
from .expression import CallableExpressionRenderer, RendererProposal
from .output import (
    CallableExpressionReadbackAnnotator,
    ExpressionRevisionProposal,
    LocalTextUnitActuator,
)
from .runtime import MindRuntime
from .runtime_types import ActionCandidate, TickResult
from .storage import EventStore
from .studio_projection import EpisodeView, project_episode


MAX_STUDIO_TEXT = 512
MAX_STUDIO_TICKS = 64


class StudioTextEnvironment:
    """No-side-effect environment; text output uses the selected actuator."""

    environment_id = "mind-studio-local"

    def describe(self) -> dict[str, Any]:
        return {"environment_id": self.environment_id, "actions": [], "mode": "provider_off"}

    def candidates(self, event: EventEnvelope, frame_view: dict[str, Any]):
        del event, frame_view
        return ()

    def dispatch(self, candidate: ActionCandidate, idempotency_key: str):
        del candidate, idempotency_key
        raise ContractError("studio_environment_has_no_direct_actions")

    def readback(self, receipt):
        del receipt
        raise ContractError("studio_text_readback_belongs_to_text_actuator")


@dataclass(frozen=True)
class StudioEpisodeRequest:
    request_id: str
    proposition_text: str
    surface_text: str | None = None
    renderer_enabled: bool = True
    annotation_enabled: bool = True
    max_ticks: int = MAX_STUDIO_TICKS

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ContractError("studio_request_id_required")
        if len(self.request_id) > 160:
            raise ContractError("studio_request_id_too_long")
        if not isinstance(self.proposition_text, str) or not self.proposition_text.strip():
            raise ContractError("studio_proposition_text_required")
        if len(self.proposition_text) > MAX_STUDIO_TEXT:
            raise ContractError("studio_proposition_text_too_long")
        if self.surface_text is not None:
            if not isinstance(self.surface_text, str) or not self.surface_text:
                raise ContractError("studio_surface_text_invalid")
            if len(self.surface_text) > MAX_STUDIO_TEXT:
                raise ContractError("studio_surface_text_too_long")
        if isinstance(self.max_ticks, bool) or not 1 <= int(self.max_ticks) <= MAX_STUDIO_TICKS:
            raise ContractError("studio_max_ticks_out_of_bounds")

    def fingerprint(self) -> str:
        payload = {
            "proposition_text": self.proposition_text,
            "surface_text": self.surface_text,
            "renderer_enabled": self.renderer_enabled,
            "annotation_enabled": self.annotation_enabled,
            "max_ticks": int(self.max_ticks),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def episode_id(self) -> str:
        identity = hashlib.sha256(self.request_id.encode("utf-8")).hexdigest()[:20]
        return f"studio-{identity}"


def _renderer(surface_text: str) -> CallableExpressionRenderer:
    units = tuple(surface_text)

    def propose(proposition, draft):
        del draft
        return RendererProposal(
            proposition_refs=(proposition.proposition_id,),
            preserved_claim_refs=(proposition.proposition_id,),
            units=units,
            tone="neutral",
            source="studio_renderer_fixture",
            model_receipt_ref="provider-off-studio-renderer",
            status="complete",
            limitations=("local_fixture_not_language_model",),
        )

    return CallableExpressionRenderer(propose)


def _next_unit_mismatch_annotator() -> CallableExpressionReadbackAnnotator:
    """Compare the next planned surface unit with AP's canonical unit.

    This is deliberately generic and position-preserving.  It contains no
    task, word, language or expected-answer route.
    """

    def propose_revision(*, result, draft, cursor, dispatched_unit_index, next_index):
        del cursor, dispatched_unit_index
        if next_index >= len(draft.units) or next_index >= len(draft.proposition_units):
            return None
        surface = draft.units[next_index]
        expected = draft.proposition_units[next_index]
        if surface == expected:
            return None
        return ExpressionRevisionProposal(
            proposal_id=f"studio-mismatch-{result.result_id}-{next_index}",
            target_draft_id=draft.draft_id,
            start_index=next_index,
            delete_count=1,
            replacement_units=(expected,),
            preserves_proposition=True,
            proposition_expected_units=(expected,),
            mismatch=0.94,
            confidence=0.96,
            source="studio_readback_codec_fixture",
            evidence_refs=(result.result_id, f"draft-readback:{draft.draft_id}:{next_index}"),
            lineage_refs=(result.receipt_ref, draft.draft_id),
            reason="next surface unit differs from the selected proposition unit",
        )

    return CallableExpressionReadbackAnnotator(propose_revision)


def run_studio_episode(
    database_path: str | Path,
    request: StudioEpisodeRequest,
) -> EpisodeView:
    """Run or replay one bounded, provider-off episode on the real runtime."""

    if not isinstance(request, StudioEpisodeRequest):
        raise ContractError("studio_episode_request_required")
    database = Path(database_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    environment = StudioTextEnvironment()
    actuator = LocalTextUnitActuator(environment_id="mind-studio-text-actuator")
    renderer = (
        _renderer(request.surface_text)
        if request.renderer_enabled and request.surface_text is not None
        else None
    )
    annotator = _next_unit_mismatch_annotator() if request.annotation_enabled else None
    episode_id = request.episode_id()
    results: list[TickResult] = []
    limitations = ["provider_off", "local_fixture_only"]
    unknowns = [
        "real_llm_language_quality_unmeasured",
        "real_vlm_readback_unmeasured",
        "user_visible_cross_session_continuity_unmeasured",
    ]

    with EventStore(database) as store:
        runtime = MindRuntime(
            store,
            environment,
            runtime_id="mind-studio-runtime",
            organism_id="mind-studio-organism",
            episode_id=episode_id,
            text_actuator=actuator,
            text_readback_annotator=annotator,
            expression_renderer=renderer,
            text_unit_granularity="character",
            max_output_units=MAX_STUDIO_TEXT,
        )
        event = EventEnvelope(
            runtime_id=runtime.runtime_id,
            organism_id=runtime.organism_id,
            environment_id=environment.environment_id,
            episode_id=episode_id,
            payload_inline={"text": request.proposition_text},
            evidence_refs=(f"studio-user:{request.request_id}",),
            idempotency_key=f"studio-input:{request.request_id}:{request.fingerprint()}",
            extra={
                "text_reply_affordance": True,
                "communicative_relevance": 1.0,
                "studio_request_id": request.request_id,
            },
        )
        current = runtime.tick(event)
        results.append(current)
        while current.result_envelope is not None and len(results) < int(request.max_ticks):
            current = runtime.tick(current.result_envelope)
            results.append(current)

    budget_exhausted = bool(results and results[-1].result_envelope is not None)
    if budget_exhausted:
        limitations.append("episode_tick_budget_exhausted")
        unknowns.append("episode_completion_unknown")

    preview = project_episode(
        request_id=request.request_id,
        episode_id=episode_id,
        proposition_text=request.proposition_text,
        requested_surface_text=request.surface_text if request.renderer_enabled else None,
        results=results,
        status="partial" if budget_exhausted else "success",
        limitations=limitations,
        unknowns=unknowns,
    )
    view_limitations = list(preview.limitations)
    status = preview.status
    expected_surface = request.surface_text if request.renderer_enabled and request.surface_text is not None else request.proposition_text
    if not request.annotation_enabled and expected_surface != request.proposition_text:
        view_limitations.append("readback_annotation_disabled_surface_not_corrected")
        status = "partial"
    if len(expected_surface) != len(request.proposition_text):
        view_limitations.append("position_preserving_demo_cannot_reconcile_length_change")
        status = "partial"
    if preview.physical_output != request.proposition_text:
        view_limitations.append("physical_output_differs_from_canonical_proposition")
        status = "partial"
    return EpisodeView(
        request_id=preview.request_id,
        episode_id=preview.episode_id,
        status=status,
        proposition_text=preview.proposition_text,
        requested_surface_text=preview.requested_surface_text,
        physical_output=preview.physical_output,
        ticks=preview.ticks,
        counters=preview.counters,
        causal_summary=preview.causal_summary,
        product_effect=preview.product_effect,
        ownership={
            **dict(preview.ownership),
            "readback_annotation": "assisted_fixture" if request.annotation_enabled else "absent",
            "surface_renderer": "assisted_fixture" if renderer is not None else "native",
        },
        limitations=tuple(dict.fromkeys(view_limitations)),
        unknowns=preview.unknowns,
    )


__all__ = [
    "MAX_STUDIO_TEXT",
    "MAX_STUDIO_TICKS",
    "StudioEpisodeRequest",
    "StudioTextEnvironment",
    "run_studio_episode",
]
