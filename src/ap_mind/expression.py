"""Renderer boundary for AP-native propositions and expression drafts.

This module intentionally does not decide when to speak or dispatch text.  It
only checks whether a structured renderer proposal preserves the proposition
that AP already formed.  A rejected renderer response remains inspectable and
cannot become a public draft by accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from .contracts import ContractError
from .runtime_types import ExpressionDraft, Proposition


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value) if value is not None else ""
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass(frozen=True)
class RendererProposal:
    """One source-tagged rendering proposal with explicit semantic deltas."""

    proposal_id: str = field(default_factory=lambda: f"renderer_{uuid4().hex}")
    proposition_refs: tuple[str, ...] = ()
    preserved_claim_refs: tuple[str, ...] = ()
    units: tuple[str, ...] = ()
    added_claims: tuple[str, ...] = ()
    omitted_claim_refs: tuple[str, ...] = ()
    new_commitments: tuple[str, ...] = ()
    external_action_claims: tuple[str, ...] = ()
    tone: str = "neutral"
    source: str = "llm_renderer"
    model_receipt_ref: str | None = None
    status: str = "proposal"
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "proposition_refs": list(self.proposition_refs),
            "preserved_claim_refs": list(self.preserved_claim_refs),
            "units": list(self.units),
            "added_claims": list(self.added_claims),
            "omitted_claim_refs": list(self.omitted_claim_refs),
            "new_commitments": list(self.new_commitments),
            "external_action_claims": list(self.external_action_claims),
            "tone": self.tone,
            "source": self.source,
            "model_receipt_ref": self.model_receipt_ref,
            "status": self.status,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RendererProposal":
        if not isinstance(raw, Mapping):
            raise ContractError("renderer_proposal_must_be_an_object")

        def texts(name: str, *, limit: int = 128) -> tuple[str, ...]:
            value = raw.get(name, ())
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                return ()
            return tuple(_bounded_text(item, 512) for item in list(value)[:limit] if isinstance(item, str))

        return cls(
            proposal_id=_bounded_text(raw.get("proposal_id") or f"renderer_{uuid4().hex}", 160),
            proposition_refs=texts("proposition_refs"),
            preserved_claim_refs=texts("preserved_claim_refs"),
            units=texts("units", limit=256),
            added_claims=texts("added_claims"),
            omitted_claim_refs=texts("omitted_claim_refs"),
            new_commitments=texts("new_commitments"),
            external_action_claims=texts("external_action_claims"),
            tone=_bounded_text(raw.get("tone") or "neutral", 80),
            source=_bounded_text(raw.get("source") or "llm_renderer", 80),
            model_receipt_ref=_bounded_text(raw.get("model_receipt_ref"), 160) if raw.get("model_receipt_ref") else None,
            status=_bounded_text(raw.get("status") or "proposal", 40),
            limitations=texts("limitations", limit=32),
        )


@dataclass(frozen=True)
class RendererDecision:
    accepted: bool
    proposition: Proposition
    draft: ExpressionDraft
    reasons: tuple[str, ...] = ()
    isolated: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "proposition": self.proposition.to_dict(),
            "draft": self.draft.to_dict(),
            "reasons": list(self.reasons),
            "isolated": dict(self.isolated),
        }


class ExpressionRenderer(Protocol):
    """Replaceable surface renderer with no speaking or dispatch authority."""

    def propose(
        self,
        proposition: Proposition,
        draft: ExpressionDraft,
    ) -> RendererProposal | None:
        ...


@dataclass(frozen=True)
class CallableExpressionRenderer:
    """Adapter for a bounded local fixture or future provider-backed organ."""

    callback: Callable[[Proposition, ExpressionDraft], RendererProposal | None]

    def propose(
        self,
        proposition: Proposition,
        draft: ExpressionDraft,
    ) -> RendererProposal | None:
        return self.callback(proposition, draft)


def review_renderer_proposal(
    proposition: Proposition,
    draft: ExpressionDraft,
    proposal: RendererProposal,
) -> RendererDecision:
    """Accept surface rendering only when the frozen semantic refs survive.

    This is a claim/reference boundary, not a perfect semantic theorem prover.
    If the renderer declares a new fact, omits the proposition, adds a promise
    or asserts an external action, the proposal is isolated.  A future teacher
    may revise the AP proposition in a later tick; the renderer cannot do so.
    """

    if not isinstance(proposition, Proposition) or not isinstance(draft, ExpressionDraft):
        raise ContractError("renderer_review_requires_proposition_and_draft")
    if not isinstance(proposal, RendererProposal):
        raise ContractError("renderer_review_requires_renderer_proposal")
    reasons: list[str] = []
    if proposition.proposition_id not in proposal.proposition_refs:
        reasons.append("proposition_ref_missing")
    if proposition.proposition_id not in proposal.preserved_claim_refs:
        reasons.append("frozen_claim_not_preserved")
    if proposal.omitted_claim_refs:
        reasons.append("claim_omission_declared")
    if proposal.added_claims:
        reasons.append("new_claims_declared")
    if proposal.new_commitments:
        reasons.append("new_commitments_declared")
    if proposal.external_action_claims:
        reasons.append("unobserved_external_action_declared")
    units = tuple(item for item in proposal.units if item)
    if not units:
        reasons.append("renderer_units_empty")
    if proposal.status not in {"proposal", "complete"}:
        reasons.append("renderer_status_incomplete")

    isolated = {
        "added_claims": list(proposal.added_claims),
        "omitted_claim_refs": list(proposal.omitted_claim_refs),
        "new_commitments": list(proposal.new_commitments),
        "external_action_claims": list(proposal.external_action_claims),
    }
    if reasons:
        rejected = replace(
            draft,
            public_allowed=False,
            renderer_source=proposal.source,
            status="renderer_rejected",
            diff={
                "retained": list(draft.units),
                "removed": [],
                "added": [],
                "isolated": isolated,
                "reasons": reasons,
                "model_receipt_ref": proposal.model_receipt_ref,
            },
        )
        return RendererDecision(False, proposition, rejected, tuple(reasons), isolated)

    rendered = replace(
        draft,
        units=units,
        tone=proposal.tone,
        # Speaking time still belongs to a later ActionArena commitment.
        public_allowed=False,
        renderer_source=proposal.source,
        status="rendered_private",
        diff={
            "retained_claim_refs": list(proposal.preserved_claim_refs),
            "surface_before": list(draft.units),
            "surface_after": list(units),
            "added": [],
            "removed": [],
            "model_receipt_ref": proposal.model_receipt_ref,
        },
    )
    return RendererDecision(True, replace(proposition, frozen=True), rendered)


__all__ = [
    "RendererProposal",
    "RendererDecision",
    "ExpressionRenderer",
    "CallableExpressionRenderer",
    "review_renderer_proposal",
]
