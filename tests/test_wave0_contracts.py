from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from ap_mind import (
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
from ap_mind.governance import GovernanceCompatibilityRecord


SHA = "440EA59A70902B0DB2384C7EE066B7EB25387299C8175321127172AF6AD19A39"


def test_event_round_trip_retains_unknown_fields() -> None:
    event = EventEnvelope(
        runtime_id="r",
        organism_id="o",
        environment_id="e",
        episode_id="p",
        payload_inline={"text": "hello"},
        extra={"future": {"value": 3}},
    )
    raw = event.to_dict()
    raw["future_wire_field"] = ["kept", 1]
    parsed = EventEnvelope.from_dict(raw)
    assert parsed.extra["future_wire_field"] == ["kept", 1]
    assert loads(EventEnvelope, dumps(parsed)) == parsed


def test_delegation_cannot_claim_physical_or_truth_authority() -> None:
    with pytest.raises(ContractError, match="forbidden_authority"):
        DelegationDecision(
            capability_key="x",
            mode="substituted",
            enabled=True,
            allowed_decisions=("truth_write",),
        )

    with pytest.raises(ContractError, match="llm_cannot_own_execution"):
        CapabilityOwnership(
            capability_key="x",
            execution_owner="llm",
        )


def test_one_decision_slot_has_one_selection() -> None:
    slot = DecisionSlot(
        capability_key="x",
        episode_id="p",
        candidate_refs=("a", "b"),
    ).select("a", owner="llm", reason="delegated")
    assert slot.selected_candidate_ref == "a"
    with pytest.raises(ContractError, match="different_selection"):
        slot.select("b", owner="ap_native", reason="conflict")
    with pytest.raises(ContractError, match="unknown_candidate"):
        DecisionSlot(capability_key="x", episode_id="p", candidate_refs=("a",)).select(
            "z", owner="ap_native", reason="bad ref"
        )


def test_receipt_and_result_are_separate_and_unknown_is_honest() -> None:
    receipt = DispatchReceipt(
        action_ref="a",
        environment_id="e",
        idempotency_key="i",
        connector_ref="mock",
        status="accepted",
    )
    unknown = ResultEvent(
        receipt_ref=receipt.receipt_id,
        action_ref=receipt.action_ref,
        environment_id=receipt.environment_id,
        status="unknown",
        completeness="unknown",
    )
    assert receipt.status == "accepted"
    assert unknown.status == "unknown"
    assert unknown.as_event_envelope(runtime_id="r", organism_id="o", episode_id="p").source == "readback"

    with pytest.raises(ContractError, match="successful_result_requires"):
        ResultEvent(
            receipt_ref="r",
            action_ref="a",
            environment_id="e",
            status="success",
            completeness="complete",
        )


def test_idempotency_duplicate_does_not_create_second_success() -> None:
    ledger = IdempotencyLedger(max_entries=2)
    first = DispatchReceipt(
        action_ref="a", environment_id="e", idempotency_key="same", connector_ref="mock"
    )
    saved = ledger.register(first)
    duplicate = ledger.register(
        DispatchReceipt(
            action_ref="a", environment_id="e", idempotency_key="same", connector_ref="mock"
        )
    )
    assert len(ledger) == 1
    assert duplicate.status == "duplicate"
    assert duplicate.duplicate_of == saved.receipt_id
    with pytest.raises(ContractError, match="reused_for_different_action"):
        ledger.register(
            DispatchReceipt(
                action_ref="other", environment_id="e", idempotency_key="same", connector_ref="mock"
            )
        )


def test_pending_governance_does_not_enable_delegated_winner() -> None:
    record = GovernanceCompatibilityRecord(
        record_id="g",
        project_id="p",
        whitepaper_sha256=SHA,
        compatibility="pending",
        llm_delegation_enabled=False,
    )
    with pytest.raises(ContractError, match="delegated_winner_not_enabled"):
        record.assert_delegation_ready()

    ready = GovernanceCompatibilityRecord(
        record_id="g2",
        project_id="p",
        whitepaper_sha256=SHA,
        compatibility="compatible",
        llm_delegation_enabled=True,
    )
    ready.assert_delegation_ready()
