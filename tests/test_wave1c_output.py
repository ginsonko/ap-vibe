from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ap_mind import (
    ActionCandidate,
    CallableExpressionReadbackAnnotator,
    CallableExpressionRenderer,
    ContractError,
    DispatchReceipt,
    EventEnvelope,
    EventStore,
    ExpressionDraft,
    ExpressionRevisionProposal,
    LocalTextUnitActuator,
    MindRuntime,
    ResultEvent,
    RendererProposal,
    dispatch_next_unit,
    output_control_result,
    prepare_output,
    restore_output,
    apply_output_revision,
)


class TextOnlyEnvironment:
    environment_id = "text-only-environment"

    def describe(self):
        return {"environment_id": self.environment_id, "actions": []}

    def candidates(self, event, frame_view):
        return ()

    def dispatch(self, candidate: ActionCandidate, idempotency_key: str) -> DispatchReceipt:
        raise AssertionError("text output must use the selected text actuator, not the environment adapter")

    def readback(self, receipt: DispatchReceipt) -> ResultEvent:
        raise AssertionError("text output must use the selected text actuator, not the environment adapter")


def _speech_event(runtime: MindRuntime, *, text: str, key: str) -> EventEnvelope:
    return EventEnvelope(
        runtime_id=runtime.runtime_id,
        organism_id=runtime.organism_id,
        environment_id="text-only-environment",
        episode_id="wave1c-expression",
        payload_inline={"text": text},
        evidence_refs=(f"user:{key}",),
        idempotency_key=key,
        extra={"text_reply_affordance": True, "communicative_relevance": 1.0},
    )


def _draft(*, draft_id: str = "draft-output") -> ExpressionDraft:
    return ExpressionDraft(
        draft_id=draft_id,
        proposition_ref="prop-output",
        units=("黄色", "苹蕉"),
        proposition_units=("黄色", "苹蕉"),
        unit_granularity="word",
        public_allowed=False,
        status="native_committed",
    )


def _surface_renderer(units: tuple[str, ...]) -> CallableExpressionRenderer:
    def propose(proposition, draft):
        return RendererProposal(
            proposition_refs=(proposition.proposition_id,),
            preserved_claim_refs=(proposition.proposition_id,),
            units=units,
            source="renderer-fixture",
            model_receipt_ref="renderer-local-fixture",
            status="complete",
        )

    return CallableExpressionRenderer(propose)


def _first_suffix_mismatch_annotator(
    *,
    confidence: float = 0.96,
    mismatch: float = 0.94,
) -> CallableExpressionReadbackAnnotator:
    """Provider-off observer for structure, not an example answer table."""

    def propose_revision(*, result, draft, cursor, dispatched_unit_index, next_index):
        del cursor, dispatched_unit_index
        for index in range(next_index, min(len(draft.units), len(draft.proposition_units))):
            if draft.units[index] == draft.proposition_units[index]:
                continue
            expected = (draft.proposition_units[index],)
            return ExpressionRevisionProposal(
                proposal_id=f"suffix-mismatch-{result.result_id}",
                target_draft_id=draft.draft_id,
                start_index=index,
                delete_count=1,
                replacement_units=expected,
                preserves_proposition=True,
                proposition_expected_units=expected,
                mismatch=mismatch,
                confidence=confidence,
                source="readback_codec_fixture",
                evidence_refs=(result.result_id, f"observed-unit:{next_index - 1}"),
                lineage_refs=(result.receipt_ref, draft.draft_id),
                reason="undispatched surface suffix diverges from the selected proposition units",
            )
        return None

    return CallableExpressionReadbackAnnotator(propose_revision)


def _selected_kind(result) -> str | None:
    selected_ref = result.frame.decision.get("selected_candidate_ref")
    selected = next(
        (item for item in result.frame.actions if item["candidate_id"] == selected_ref),
        None,
    )
    return selected["kind"] if selected else None


def test_output_requires_outward_commitment_and_advances_one_unit(tmp_path: Path) -> None:
    draft = _draft()
    actuator = LocalTextUnitActuator()
    with EventStore(tmp_path / "mind.sqlite") as store:
        with pytest.raises(ContractError, match="output_requires_outward_commitment"):
            prepare_output(draft, outward_commitment=False, store=store)
        cursor = prepare_output(draft, outward_commitment=True, store=store)
        one = dispatch_next_unit(store, actuator, draft, cursor)
        assert one.advanced is True
        assert one.cursor.next_index == 1
        assert one.cursor.status == "ready"
        assert actuator.text() == "黄色"
        assert store.count("output_outbox") == 1
        restored = restore_output(store, draft.draft_id)
        assert restored is not None and restored[1] == one.cursor


def test_unknown_readback_never_advances_or_blindly_redispatches(tmp_path: Path) -> None:
    draft = _draft(draft_id="draft-unknown")
    actuator = LocalTextUnitActuator(fail_readback=True)
    with EventStore(tmp_path / "mind.sqlite") as store:
        cursor = prepare_output(draft, outward_commitment=True, store=store)
        first = dispatch_next_unit(store, actuator, draft, cursor)
        assert first.advanced is False
        assert first.cursor.next_index == 0
        assert first.cursor.status == "action_deferred"
        assert actuator.text() == "黄色"
        second = dispatch_next_unit(store, actuator, draft, cursor)
        assert second.recovered is True
        assert second.cursor.next_index == 0
        assert actuator.text() == "黄色"
        assert store.count("dispatches") == 1
        assert store.count("results") == 1


def test_cold_restart_recovers_cursor_and_does_not_repeat_completed_unit(tmp_path: Path) -> None:
    db = tmp_path / "mind.sqlite"
    draft = _draft(draft_id="draft-restart")
    actuator = LocalTextUnitActuator()
    with EventStore(db) as store:
        cursor = prepare_output(draft, outward_commitment=True, store=store)
        first = dispatch_next_unit(store, actuator, draft, cursor)
        assert first.cursor.next_index == 1
        counts = {name: store.count(name) for name in ("dispatches", "results", "output_outbox")}

    with EventStore(db) as store:
        restored = restore_output(store, draft.draft_id)
        assert restored is not None
        second = dispatch_next_unit(store, actuator, draft, restored[1])
        assert second.unit == "苹蕉"
        assert second.cursor.status == "completed"
        assert actuator.text() == "黄色苹蕉"
        assert store.count("dispatches") == counts["dispatches"] + 1
        assert store.count("results") == counts["results"] + 1
        assert store.count("output_outbox") == counts["output_outbox"]


def test_unreceipted_dispatch_state_requires_reconciliation(tmp_path: Path) -> None:
    draft = _draft(draft_id="draft-crash-window")
    actuator = LocalTextUnitActuator()
    with EventStore(tmp_path / "mind.sqlite") as store:
        cursor = prepare_output(draft, outward_commitment=True, store=store)
        store.save_output_outbox(
            draft_id=draft.draft_id,
            proposition_ref=draft.proposition_ref,
            next_index=0,
            status="dispatching",
            payload={"draft": draft.to_dict(), "cursor": replace(cursor, status="dispatching").to_dict()},
            updated_at="2026-09-02T00:00:00Z",
        )
        result = dispatch_next_unit(store, actuator, draft, cursor)
        assert result.advanced is False
        assert result.cursor.status == "action_deferred"
        assert result.cursor.last_error == "unreceipted_dispatch_requires_reconciliation"
        assert actuator.text() == ""
        assert store.count("dispatches") == 0


def test_output_outbox_cannot_rewind_or_replace_draft(tmp_path: Path) -> None:
    draft = _draft(draft_id="draft-fence")
    with EventStore(tmp_path / "mind.sqlite") as store:
        cursor = prepare_output(draft, outward_commitment=True, store=store)
        actuator = LocalTextUnitActuator()
        advanced = dispatch_next_unit(store, actuator, draft, cursor).cursor
        with pytest.raises(ContractError, match="cursor_cannot_rewind"):
            store.save_output_outbox(
                draft_id=draft.draft_id,
                proposition_ref=draft.proposition_ref,
                next_index=0,
                status="ready",
                payload={"draft": draft.to_dict(), "cursor": cursor.to_dict()},
                updated_at="2026-09-02T00:00:01Z",
            )
        changed = replace(draft, units=("替换",))
        with pytest.raises(ContractError, match="draft_snapshot_conflict"):
            prepare_output(changed, outward_commitment=True, store=store)
        assert advanced.next_index == 1


def test_runtime_recompetes_after_every_text_unit_readback(tmp_path: Path) -> None:
    actuator = LocalTextUnitActuator()
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(
            store,
            TextOnlyEnvironment(),
            text_actuator=actuator,
            text_unit_granularity="character",
        )
        first = runtime.tick(_speech_event(runtime, text="好呀", key="say-two-units"))
        assert first.frame.decision["status"] == "selected"
        assert next(item for item in first.frame.actions if item["candidate_id"] == first.frame.decision["selected_candidate_ref"])["kind"] == "begin_expression"
        assert first.result_envelope is not None
        assert first.frame.counters["cognitive_ticks"] == 1
        assert first.frame.counters["output_units"] == 1
        assert actuator.text() == "好"

        second = runtime.tick(first.result_envelope)
        selected = next(item for item in second.frame.actions if item["candidate_id"] == second.frame.decision["selected_candidate_ref"])
        assert selected["kind"] == "continue_expression"
        assert second.frame.sa.source == "readback"
        assert second.result_envelope is not None
        assert second.frame.counters["cognitive_ticks"] == 2
        assert second.frame.counters["output_units"] == 2
        assert actuator.text() == "好呀"

        final_readback = runtime.tick(second.result_envelope)
        assert final_readback.frame.sa.source == "readback"
        assert final_readback.frame.decision["status"] == "abstained"
        assert final_readback.receipt is None
        assert final_readback.frame.counters["cognitive_ticks"] == 3
        assert final_readback.frame.counters["output_units"] == 2
        assert store.count("frames") == 3
        assert store.count("dispatches") == 2
        assert store.count("results") == 2


def test_runtime_crash_after_first_unit_does_not_advance_before_readback_tick(tmp_path: Path) -> None:
    db = tmp_path / "mind.sqlite"
    actuator = LocalTextUnitActuator()
    environment = TextOnlyEnvironment()
    event: EventEnvelope
    with EventStore(db) as store:
        runtime = MindRuntime(
            store,
            environment,
            text_actuator=actuator,
            text_unit_granularity="character",
        )
        event = _speech_event(runtime, text="好呀", key="crash-before-frame")
        # Reproduce the exact durable boundary after physical readback but
        # before the cognition frame/checkpoint was saved.
        draft = ExpressionDraft(
            draft_id=f"draft_{event.event_id}",
            proposition_ref=f"prop_{event.event_id}",
            units=("好", "呀"),
            proposition_units=("好", "呀"),
            unit_granularity="character",
            public_allowed=True,
            renderer_source="ap_native",
            diff={"added": [], "removed": [], "retained": ["好", "呀"]},
            status="native_committed",
        )
        cursor = prepare_output(draft, outward_commitment=True, store=store)
        dispatched = dispatch_next_unit(store, actuator, draft, cursor, expected_index=0)
        assert dispatched.cursor.next_index == 1
        assert actuator.text() == "好"
        assert store.count("frames") == 0

    with EventStore(db) as store:
        restored = MindRuntime(
            store,
            environment,
            text_actuator=actuator,
            text_unit_granularity="character",
        )
        recovered = restored.tick(event)
        assert recovered.frame.decision["status"] == "selected"
        assert recovered.frame.decision["output"]["recovered"] is True
        assert recovered.frame.decision["output"]["unit_index"] == 0
        assert actuator.text() == "好"
        assert store.count("dispatches") == 1
        assert store.count("results") == 1
        assert recovered.result_envelope is not None

        next_tick = restored.tick(recovered.result_envelope)
        assert actuator.text() == "好呀"
        assert next_tick.frame.counters["output_units"] == 2


def test_runtime_observes_mismatch_revises_suffix_then_recompetes_before_continuing(tmp_path: Path) -> None:
    actuator = LocalTextUnitActuator()
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(
            store,
            TextOnlyEnvironment(),
            text_actuator=actuator,
            text_unit_granularity="character",
            expression_renderer=_surface_renderer(tuple("黄色苹蕉")),
            text_readback_annotator=_first_suffix_mismatch_annotator(),
        )
        first = runtime.tick(_speech_event(runtime, text="黄色苹果", key="surface-mismatch"))
        assert _selected_kind(first) == "begin_expression"
        assert first.frame.proposition is not None
        assert first.frame.proposition.content == "黄色苹果"
        assert first.frame.expression_draft is not None
        assert first.frame.expression_draft.units == tuple("黄色苹蕉")
        assert first.frame.expression_draft.proposition_units == tuple("黄色苹果")
        assert first.result_envelope is not None
        assert actuator.text() == "黄"

        revision = runtime.tick(first.result_envelope)
        assert {item["kind"] for item in revision.frame.actions} >= {
            "revise_expression",
            "continue_expression",
            "pause_expression",
            "withdraw_expression",
        }
        assert _selected_kind(revision) == "revise_expression"
        assert any(item.name == "incongruity" for item in revision.frame.feelings)
        assert revision.frame.decision["expression_revision_review"]["eligible"] is True
        assert revision.frame.decision["expression_revision_review"]["reasons"] == []
        assert revision.frame.decision["output"]["unit"] is None
        assert revision.frame.decision["output"]["draft"]["units"] == list("黄色苹果")
        assert revision.result_envelope is not None
        assert revision.result_envelope.payload_inline["control_kind"] == "revise_expression"
        assert actuator.text() == "黄"
        assert revision.frame.counters["output_units"] == 1

        revised_id = revision.frame.decision["output"]["cursor"]["draft_id"]
        old_id = first.frame.expression_draft.draft_id
        assert revised_id != old_id
        old_outbox = restore_output(store, old_id)
        revised_outbox = restore_output(store, revised_id)
        assert old_outbox is not None and old_outbox[1].status == "superseded"
        assert revised_outbox is not None
        assert revised_outbox[0].units == tuple("黄色苹果")
        assert revised_outbox[1].next_index == 1

        current = runtime.tick(revision.result_envelope)
        assert _selected_kind(current) == "continue_expression"
        assert actuator.text() == "黄色"
        while current.result_envelope is not None:
            current = runtime.tick(current.result_envelope)
        assert actuator.text() == "黄色苹果"
        assert current.frame.decision["status"] == "abstained"
        assert current.frame.counters["cognitive_ticks"] == 6
        assert current.frame.counters["output_units"] == 4
        assert current.frame.counters["readbacks"] == 5
        assert store.count("dispatches") == 5
        assert store.count("results") == 5


@pytest.mark.parametrize(
    ("proposal_changes", "expected_reason"),
    (
        ({"confidence": 0.2}, "revision_confidence_below_threshold"),
        ({"mismatch": 0.1}, "revision_mismatch_below_threshold"),
        ({"start_index": 0}, "revision_changes_dispatched_prefix"),
        ({"start_index": 99}, "revision_range_out_of_bounds"),
        ({"preserves_proposition": False}, "revision_changes_proposition"),
        ({"delete_count": 0}, "revision_span_changes_alignment"),
    ),
)
def test_invalid_revision_proposal_cannot_install_winner(
    tmp_path: Path,
    proposal_changes: dict[str, object],
    expected_reason: str,
) -> None:
    actuator = LocalTextUnitActuator()

    def annotate(*, result, draft, cursor, dispatched_unit_index, next_index):
        del cursor, dispatched_unit_index
        values = {
            "proposal_id": f"invalid-{result.result_id}",
            "target_draft_id": draft.draft_id,
            "start_index": next_index,
            "delete_count": 1,
            "replacement_units": (draft.proposition_units[next_index],),
            "preserves_proposition": True,
            "proposition_expected_units": (draft.proposition_units[next_index],),
            "mismatch": 0.94,
            "confidence": 0.96,
            "source": "teacher_fixture",
            "evidence_refs": (result.result_id,),
            "lineage_refs": (result.receipt_ref,),
            "reason": "adversarial proposal",
        }
        values.update(proposal_changes)
        return ExpressionRevisionProposal(**values)

    with EventStore(tmp_path / f"mind-{expected_reason}.sqlite") as store:
        runtime = MindRuntime(
            store,
            TextOnlyEnvironment(),
            text_actuator=actuator,
            text_unit_granularity="character",
            expression_renderer=_surface_renderer(tuple("甲乙")),
            text_readback_annotator=CallableExpressionReadbackAnnotator(annotate),
        )
        first = runtime.tick(_speech_event(runtime, text="甲丙", key=expected_reason))
        assert first.result_envelope is not None
        follow = runtime.tick(first.result_envelope)
        assert "revise_expression" not in {item["kind"] for item in follow.frame.actions}
        assert _selected_kind(follow) == "continue_expression"
        assert actuator.text() == "甲乙"
        review = follow.frame.decision["expression_revision_review"]
        assert review["eligible"] is False
        assert expected_reason in review["reasons"]
        # The proposal remains visible as data; it did not write the outbox or
        # install a DecisionSlot winner merely because teacher/UI supplied it.
        assert first.result_envelope.payload_inline["expression_annotation"]["status"] == "proposed"
        assert expected_reason in {
            "revision_confidence_below_threshold",
            "revision_mismatch_below_threshold",
            "revision_changes_dispatched_prefix",
            "revision_range_out_of_bounds",
            "revision_changes_proposition",
            "revision_span_changes_alignment",
        }


def test_revision_cold_restart_has_one_continuable_successor_and_no_duplicate_unit(tmp_path: Path) -> None:
    db = tmp_path / "mind-revision-restart.sqlite"
    actuator = LocalTextUnitActuator()
    environment = TextOnlyEnvironment()
    revision_readback: EventEnvelope
    revised_id: str
    old_id: str
    with EventStore(db) as store:
        runtime = MindRuntime(
            store,
            environment,
            text_actuator=actuator,
            text_unit_granularity="character",
            expression_renderer=_surface_renderer(tuple("甲乙")),
            text_readback_annotator=_first_suffix_mismatch_annotator(),
        )
        first = runtime.tick(_speech_event(runtime, text="甲丙", key="revision-restart"))
        assert first.result_envelope is not None and first.frame.expression_draft is not None
        old_id = first.frame.expression_draft.draft_id
        revision = runtime.tick(first.result_envelope)
        assert _selected_kind(revision) == "revise_expression"
        assert revision.result_envelope is not None
        revision_readback = revision.result_envelope
        revised_id = revision.frame.decision["output"]["cursor"]["draft_id"]
        assert actuator.text() == "甲"
        assert [item["draft_id"] for item in store.list_open_output_outbox()] == [revised_id]

    with EventStore(db) as store:
        restored = MindRuntime(
            store,
            environment,
            text_actuator=actuator,
            text_unit_granularity="character",
            text_readback_annotator=_first_suffix_mismatch_annotator(),
        )
        assert restore_output(store, old_id)[1].status == "superseded"
        assert [item["draft_id"] for item in store.list_open_output_outbox()] == [revised_id]
        continued = restored.tick(revision_readback)
        assert _selected_kind(continued) == "continue_expression"
        assert actuator.text() == "甲丙"
        assert store.count("output_outbox") == 2
        assert store.count("dispatches") == 3
        assert store.count("results") == 3


def test_annotation_failure_keeps_physical_readback_and_normal_expression_path(tmp_path: Path) -> None:
    actuator = LocalTextUnitActuator()

    def broken_annotation(**kwargs):
        del kwargs
        raise RuntimeError("fixture failure")

    with EventStore(tmp_path / "mind-annotation-failure.sqlite") as store:
        runtime = MindRuntime(
            store,
            TextOnlyEnvironment(),
            text_actuator=actuator,
            text_unit_granularity="character",
            text_readback_annotator=CallableExpressionReadbackAnnotator(broken_annotation),
        )
        first = runtime.tick(_speech_event(runtime, text="甲乙", key="annotation-failure"))
        assert first.result is not None and first.result.status == "success"
        assert first.result_envelope is not None
        assert first.result_envelope.payload_inline["expression_annotation"] == {
            "status": "proposal_incomplete",
            "error_type": "RuntimeError",
        }
        follow = runtime.tick(first.result_envelope)
        assert _selected_kind(follow) == "continue_expression"
        assert actuator.text() == "甲乙"


def test_renderer_rejection_preserves_native_units_and_cannot_create_outward_claim(tmp_path: Path) -> None:
    actuator = LocalTextUnitActuator()

    def malicious_renderer(proposition, draft):
        del draft
        return RendererProposal(
            proposition_refs=(proposition.proposition_id,),
            preserved_claim_refs=(proposition.proposition_id,),
            units=("我已经执行成功",),
            added_claims=("执行已经成功",),
            external_action_claims=("已经执行",),
            source="renderer-adversarial-fixture",
            status="complete",
        )

    with EventStore(tmp_path / "mind-renderer-rejected.sqlite") as store:
        runtime = MindRuntime(
            store,
            TextOnlyEnvironment(),
            text_actuator=actuator,
            text_unit_granularity="character",
            expression_renderer=CallableExpressionRenderer(malicious_renderer),
        )
        first = runtime.tick(_speech_event(runtime, text="仅表达观察", key="renderer-rejected-runtime"))
        assert first.frame.expression_draft is not None
        assert first.frame.expression_draft.status == "renderer_rejected"
        assert first.frame.expression_draft.units == tuple("仅表达观察")
        assert actuator.text() == "仅"
        assert "我已经执行成功" not in actuator.text()


def test_restart_after_atomic_supersede_recovers_revision_control_readback(tmp_path: Path) -> None:
    db = tmp_path / "mind-revision-control-crash.sqlite"
    actuator = LocalTextUnitActuator()
    environment = TextOnlyEnvironment()
    first_readback: EventEnvelope
    proposal: ExpressionRevisionProposal
    revised_id: str
    with EventStore(db) as store:
        runtime = MindRuntime(
            store,
            environment,
            text_actuator=actuator,
            text_unit_granularity="character",
            expression_renderer=_surface_renderer(tuple("甲乙")),
            text_readback_annotator=_first_suffix_mismatch_annotator(),
        )
        first = runtime.tick(_speech_event(runtime, text="甲丙", key="revision-control-crash"))
        assert first.result_envelope is not None
        first_readback = first.result_envelope
        # Use the same event-boundary normalization as the real action path;
        # it appends the readback event/evidence lineage before persistence.
        proposal = runtime._expression_revision_from_event(first_readback)
        assert proposal is not None
        revised, cursor = apply_output_revision(store, proposal)
        revised_id = revised.draft_id
        assert cursor.next_index == 1
        # Simulated crash: supersede transaction is durable but the control
        # receipt/result and cognition frame have not been written.
        assert store.get_dispatch_by_idempotency(
            f"expression-revise:{proposal.target_draft_id}:{proposal.proposal_id}"
        ) is None

    with EventStore(db) as store:
        restored = MindRuntime(
            store,
            environment,
            text_actuator=actuator,
            text_unit_granularity="character",
        )
        recovered = restored.tick(first_readback)
        assert _selected_kind(recovered) == "revise_expression"
        assert recovered.frame.decision["expression_revision_review"]["eligible"] is True
        assert recovered.frame.decision["output"]["cursor"]["draft_id"] == revised_id
        assert recovered.frame.decision["output"]["recovered"] is False
        assert recovered.result_envelope is not None
        assert recovered.result_envelope.payload_inline["control_kind"] == "revise_expression"
        assert actuator.text() == "甲"
        continued = restored.tick(recovered.result_envelope)
        assert _selected_kind(continued) == "continue_expression"
        assert actuator.text() == "甲丙"
        assert store.count("output_outbox") == 2
        assert store.count("dispatches") == 3
        assert store.count("results") == 3
