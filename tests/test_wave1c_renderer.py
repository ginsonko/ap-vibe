from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ap_mind import ExpressionDraft, Proposition, RendererProposal, review_renderer_proposal


def _objects() -> tuple[Proposition, ExpressionDraft]:
    proposition = Proposition(
        proposition_id="prop-1",
        content="项目仍有一个未闭合步骤",
        source_refs=("event-1",),
        evidence_refs=("readback-1",),
        confidence=0.8,
        uncertainty=0.2,
        frozen=False,
    )
    draft = ExpressionDraft(
        draft_id="draft-1",
        proposition_ref=proposition.proposition_id,
        units=("项目仍有一个", "未闭合步骤"),
        public_allowed=False,
    )
    return proposition, draft


def test_renderer_can_change_surface_but_not_speaking_time() -> None:
    proposition, draft = _objects()
    proposal = RendererProposal(
        proposition_refs=(proposition.proposition_id,),
        preserved_claim_refs=(proposition.proposition_id,),
        units=("还有一步", "没有做完。"),
        tone="gentle",
        model_receipt_ref="call-1",
    )
    decision = review_renderer_proposal(proposition, draft, proposal)
    assert decision.accepted is True
    assert decision.proposition.frozen is True
    assert decision.draft.units == proposal.units
    assert decision.draft.public_allowed is False
    assert decision.draft.status == "rendered_private"


def test_renderer_added_fact_or_action_isolated_and_native_draft_preserved() -> None:
    proposition, draft = _objects()
    proposal = RendererProposal(
        proposition_refs=(proposition.proposition_id,),
        preserved_claim_refs=(proposition.proposition_id,),
        units=("我已经替你做完了。",),
        added_claims=("步骤已经完成",),
        new_commitments=("我保证今天发布",),
        external_action_claims=("已发布",),
        model_receipt_ref="call-evil",
    )
    decision = review_renderer_proposal(proposition, draft, proposal)
    assert decision.accepted is False
    assert decision.proposition.frozen is False
    assert decision.draft.units == draft.units
    assert decision.draft.public_allowed is False
    assert decision.draft.status == "renderer_rejected"
    assert "new_claims_declared" in decision.reasons
    assert decision.isolated["external_action_claims"] == ["已发布"]


def test_renderer_must_preserve_the_selected_proposition_reference() -> None:
    proposition, draft = _objects()
    proposal = RendererProposal(
        proposition_refs=("other-prop",),
        preserved_claim_refs=("other-prop",),
        units=("看起来都完成了",),
    )
    decision = review_renderer_proposal(proposition, draft, proposal)
    assert decision.accepted is False
    assert "proposition_ref_missing" in decision.reasons
    assert "frozen_claim_not_preserved" in decision.reasons

