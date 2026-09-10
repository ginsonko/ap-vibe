"""Run the smallest honest Wave 0 contract receipt.

The script is intentionally local and provider-free.  It demonstrates wire
round-trip, one delegated semantic decision, physical result separation, and
duplicate fencing.  It does not claim a running AP organism.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ap_mind import (  # noqa: E402
    CapabilityOwnership,
    ContractError,
    DecisionSlot,
    DelegationDecision,
    DispatchReceipt,
    EventEnvelope,
    IdempotencyLedger,
    ResultEvent,
    dumps,
    loads,
)
from ap_mind.governance import GovernanceCompatibilityRecord  # noqa: E402


WHITEPAPER_SHA = "440EA59A70902B0DB2384C7EE066B7EB25387299C8175321127172AF6AD19A39"


def main() -> int:
    event = EventEnvelope(
        runtime_id="runtime-wave0",
        organism_id="organism-demo",
        environment_id="env-local",
        episode_id="episode-roundtrip",
        payload_inline={"text": "请记录这个项目的最新进度"},
        evidence_refs=("evidence-user-1",),
        extra={"future_codec_hint": {"modality": "text"}},
    )
    event_back = loads(EventEnvelope, dumps(event))
    assert event_back == event
    assert event_back.extra["future_codec_hint"]["modality"] == "text"

    ownership = CapabilityOwnership(
        capability_key="vibe.progress_summary",
        stage="llm_substituted",
        decision_owner="llm",
        content_owner="llm",
        evidence_owner="environment",
        execution_owner="environment",
        scope=("project:demo",),
        reason="cold-start semantic summarization",
    )
    delegation = DelegationDecision(
        capability_key=ownership.capability_key,
        mode="substituted",
        enabled=True,
        risk_level="low",
        scope=("project:demo",),
        input_refs=(event.event_id,),
        candidate_refs=("cand-summary-1", "cand-silence-1"),
        evidence_refs=event.evidence_refs,
        allowed_decisions=("select_semantic_candidate",),
        budget={"tokens": 800, "milliseconds": 5000},
        expires_at_tick=1,
        fallback="defer",
        rationale="semantic choice only; environment supplies reality",
    )
    slot = DecisionSlot(
        capability_key=ownership.capability_key,
        episode_id=event.episode_id,
        tick_index=0,
        candidate_refs=delegation.candidate_refs,
        delegation_ref=delegation.delegation_id,
    ).select("cand-summary-1", owner="llm", reason="delegated low-risk semantic choice")

    receipt = DispatchReceipt(
        action_ref=slot.selected_candidate_ref or "",
        environment_id=event.environment_id,
        idempotency_key="idem-demo-1",
        status="accepted",
        connector_ref="mock-vibe",
    )
    ledger = IdempotencyLedger(max_entries=8)
    accepted = ledger.register(receipt)
    duplicate = ledger.register(
        DispatchReceipt(
            action_ref=receipt.action_ref,
            environment_id=receipt.environment_id,
            idempotency_key=receipt.idempotency_key,
            status="attempted",
            connector_ref="mock-vibe",
        )
    )
    assert accepted.status == "accepted"
    assert duplicate.status == "duplicate"
    assert duplicate.duplicate_of == accepted.receipt_id

    result = ResultEvent(
        receipt_ref=accepted.receipt_id,
        action_ref=accepted.action_ref,
        environment_id=accepted.environment_id,
        status="success",
        payload_ref="blob://mock-vibe/progress-update-1",
        evidence_refs=("readback-vibe-1",),
        completeness="complete",
        lineage_refs=(event.event_id, slot.slot_id),
    )
    result_event = result.as_event_envelope(
        runtime_id=event.runtime_id,
        organism_id=event.organism_id,
        episode_id=event.episode_id,
    )
    assert result_event.source == "readback"
    assert result_event.role == "result"
    assert result_event.evidence_refs == result.evidence_refs

    governance = GovernanceCompatibilityRecord(
        record_id="gov-wave0",
        project_id="p-cand-local-251b4ac2d8120c",
        whitepaper_sha256=WHITEPAPER_SHA,
        compatibility="pending",
        llm_delegation_enabled=False,
        authority_sources={"whitepaper": WHITEPAPER_SHA, "skill": "unresolved-mapping"},
        incompatibilities=("current conversation is not strongly mapped in Vibe",),
    )
    try:
        governance.assert_delegation_ready()
    except ContractError:
        governance_guard = "blocked_until_vibe_mapping"
    else:  # pragma: no cover - pending is intentionally not ready
        raise AssertionError("pending governance unexpectedly enabled delegation")

    payload = {
        "round_trip_ok": True,
        "unknown_field_preserved": True,
        "single_decision_slot": slot.slot_id,
        "decision_owner": slot.decision_owner,
        "dispatch_status": accepted.status,
        "duplicate_status": duplicate.status,
        "result_status": result.status,
        "result_event_source": result_event.source,
        "governance_guard": governance_guard,
        "product_effect": "none_wave0_contract_only",
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
