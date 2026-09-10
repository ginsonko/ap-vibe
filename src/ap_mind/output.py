"""One-unit text output substrate with durable receipt/readback fencing.

The substrate is intentionally connector-agnostic.  It advances exactly one
unit only after a complete successful readback and never decides whether AP
should speak.  ``MindRuntime`` or a future expression capability must first
select an outward commitment in the shared ActionArena.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Protocol

from .contracts import ContractError, DispatchReceipt, ResultEvent, utc_now
from .runtime_types import ExpressionDraft, ExpressionRevisionProposal
from .storage import EventStore


class TextUnitActuator(Protocol):
    environment_id: str

    def dispatch_unit(
        self,
        *,
        draft_id: str,
        proposition_ref: str,
        unit_index: int,
        unit: str,
        idempotency_key: str,
    ) -> DispatchReceipt:
        """Dispatch idempotently for the supplied key.

        A connector must return the same physical attempt when the same key is
        retried.  The local outbox deliberately refuses to guess whether an
        unreceipted ``dispatching`` attempt reached a non-idempotent connector.
        """
        ...

    def readback_unit(self, receipt: DispatchReceipt) -> ResultEvent:
        ...


class ExpressionReadbackAnnotator(Protocol):
    """Replaceable observer that may propose, but never apply, a suffix edit.

    The annotator runs only after a complete physical readback. Its proposal
    is embedded in that result and must re-enter ``MindRuntime.tick`` before it
    can become an ordinary ``revise_expression`` candidate. It therefore has
    no store, actuator, ActionArena or winner authority.
    """

    def propose_revision(
        self,
        *,
        result: ResultEvent,
        draft: ExpressionDraft,
        cursor: "OutputCursor",
        dispatched_unit_index: int,
        next_index: int,
    ) -> ExpressionRevisionProposal | None:
        ...


@dataclass(frozen=True)
class CallableExpressionReadbackAnnotator:
    """Small adapter for a local codec, VLM, teacher or test callback."""

    callback: Callable[..., ExpressionRevisionProposal | None]

    def propose_revision(
        self,
        *,
        result: ResultEvent,
        draft: ExpressionDraft,
        cursor: "OutputCursor",
        dispatched_unit_index: int,
        next_index: int,
    ) -> ExpressionRevisionProposal | None:
        return self.callback(
            result=result,
            draft=draft,
            cursor=cursor,
            dispatched_unit_index=dispatched_unit_index,
            next_index=next_index,
        )


@dataclass(frozen=True)
class OutputCursor:
    draft_id: str
    proposition_ref: str
    next_index: int = 0
    status: str = "ready"
    committed: bool = False
    receipt_refs: tuple[str, ...] = ()
    result_refs: tuple[str, ...] = ()
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "proposition_ref": self.proposition_ref,
            "next_index": self.next_index,
            "status": self.status,
            "committed": self.committed,
            "receipt_refs": list(self.receipt_refs),
            "result_refs": list(self.result_refs),
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OutputCursor":
        if not isinstance(raw, Mapping):
            raise ContractError("output_cursor_must_be_an_object")
        refs = raw.get("receipt_refs", ())
        results = raw.get("result_refs", ())
        if isinstance(refs, (str, bytes)) or not isinstance(refs, (list, tuple)):
            raise ContractError("output_cursor_receipt_refs_invalid")
        if isinstance(results, (str, bytes)) or not isinstance(results, (list, tuple)):
            raise ContractError("output_cursor_result_refs_invalid")
        next_index = raw.get("next_index", 0)
        if isinstance(next_index, bool) or not isinstance(next_index, int) or next_index < 0:
            raise ContractError("output_cursor_next_index_invalid")
        return cls(
            draft_id=str(raw.get("draft_id", "")),
            proposition_ref=str(raw.get("proposition_ref", "")),
            next_index=next_index,
            status=str(raw.get("status", "ready")),
            committed=bool(raw.get("committed", False)),
            receipt_refs=tuple(str(item) for item in refs),
            result_refs=tuple(str(item) for item in results),
            last_error=str(raw["last_error"]) if raw.get("last_error") is not None else None,
        )


@dataclass(frozen=True)
class OutputUnitResult:
    cursor: OutputCursor
    unit: str | None = None
    unit_index: int | None = None
    receipt: DispatchReceipt | None = None
    result: ResultEvent | None = None
    advanced: bool = False
    recovered: bool = False
    draft: ExpressionDraft | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor.to_dict(),
            "unit": self.unit,
            "unit_index": self.unit_index,
            "receipt": self.receipt.to_dict() if self.receipt else None,
            "result": self.result.to_dict() if self.result else None,
            "advanced": self.advanced,
            "recovered": self.recovered,
            "draft": self.draft.to_dict() if self.draft else None,
        }


def _readback_payload(result: ResultEvent) -> dict[str, Any]:
    if isinstance(result.payload_inline, Mapping):
        return dict(result.payload_inline)
    if result.payload_inline is None:
        return {}
    # Preserve a non-object physical payload instead of replacing it with the
    # annotation envelope.
    return {"physical_readback": result.payload_inline}


def annotate_output_readback(
    result: ResultEvent,
    draft: ExpressionDraft,
    cursor: OutputCursor,
    *,
    dispatched_unit_index: int,
    annotator: ExpressionReadbackAnnotator | None,
) -> ResultEvent:
    """Attach one source-tagged proposal without changing physical truth.

    Annotation failure is an internal advisory failure: the successful
    readback is retained and ordinary continue/pause/withdraw competition can
    still proceed. No annotation is attempted for incomplete physical
    readback because it cannot confirm the observed unit boundary.
    """

    if not isinstance(result, ResultEvent):
        raise ContractError("output_readback_must_be_result_event")
    if annotator is None or result.status != "success" or result.completeness != "complete":
        return result
    next_index = dispatched_unit_index + 1
    try:
        proposal = annotator.propose_revision(
            result=result,
            draft=draft,
            cursor=cursor,
            dispatched_unit_index=dispatched_unit_index,
            next_index=next_index,
        )
    except Exception as exc:  # advisor failure cannot erase physical readback
        payload = _readback_payload(result)
        payload.setdefault(
            "expression_annotation",
            {"status": "proposal_incomplete", "error_type": type(exc).__name__},
        )
        return replace(result, payload_inline=payload)
    if proposal is None:
        return result
    payload = _readback_payload(result)
    if "expression_revision" in payload:
        payload.setdefault("expression_annotation", {"status": "proposal_conflict"})
        return replace(result, payload_inline=payload)
    if not isinstance(proposal, ExpressionRevisionProposal):
        payload["expression_annotation"] = {
            "status": "proposal_incomplete",
            "error_type": "invalid_contract",
        }
        return replace(result, payload_inline=payload)
    payload["expression_revision"] = proposal.to_dict()
    payload["expression_annotation"] = {
        "status": "proposed",
        "proposal_id": proposal.proposal_id,
        "source": proposal.source,
    }
    return replace(result, payload_inline=payload)


def output_revision_rejection_reasons(
    draft: ExpressionDraft,
    cursor: OutputCursor,
    proposal: ExpressionRevisionProposal,
    *,
    min_confidence: float = 0.0,
    min_mismatch: float = 0.0,
) -> tuple[str, ...]:
    """Return bounded reasons why a proposal cannot enter ActionArena.

    This is the single eligibility review used by the runtime. It does not
    select or apply a valid proposal.
    """

    reasons: list[str] = []
    if not isinstance(draft, ExpressionDraft) or not isinstance(cursor, OutputCursor):
        return ("revision_target_projection_invalid",)
    if not isinstance(proposal, ExpressionRevisionProposal):
        return ("revision_proposal_invalid",)
    if not 0.0 <= float(min_confidence) <= 1.0 or not 0.0 <= float(min_mismatch) <= 1.0:
        raise ContractError("output_revision_threshold_out_of_bounds")
    if proposal.target_draft_id != draft.draft_id:
        reasons.append("revision_target_mismatch")
    if cursor.status in {"completed", "withdrawn", "superseded"}:
        reasons.append("revision_target_terminal")
    if isinstance(proposal.start_index, bool) or proposal.start_index < cursor.next_index:
        reasons.append("revision_changes_dispatched_prefix")
    if isinstance(proposal.delete_count, bool) or proposal.delete_count < 0:
        reasons.append("revision_delete_count_invalid")
    else:
        end = proposal.start_index + proposal.delete_count
        if proposal.start_index > len(draft.units) or end > len(draft.units):
            reasons.append("revision_range_out_of_bounds")
        elif not proposal.replacement_units and proposal.delete_count == 0:
            reasons.append("revision_has_no_change")
        elif draft.units[proposal.start_index:end] == tuple(proposal.replacement_units):
            reasons.append("revision_surface_unchanged")
    if not proposal.preserves_proposition:
        reasons.append("revision_changes_proposition")
    if (
        proposal.delete_count != len(proposal.replacement_units)
        or len(proposal.replacement_units) != len(proposal.proposition_expected_units)
    ):
        # This bounded repair surface is position-preserving. Insertions,
        # deletions and re-segmentation require a new expression/proposition
        # proposal because subsequent unit indices would otherwise acquire
        # unreviewed semantic meaning.
        reasons.append("revision_span_changes_alignment")
    if tuple(proposal.replacement_units) != tuple(proposal.proposition_expected_units):
        reasons.append("revision_not_supported_by_declared_proposition")
    expected_end = proposal.start_index + len(proposal.proposition_expected_units)
    if (
        not draft.proposition_units
        or proposal.start_index < 0
        or expected_end > len(draft.proposition_units)
        or draft.proposition_units[proposal.start_index:expected_end]
        != tuple(proposal.proposition_expected_units)
    ):
        reasons.append("revision_not_grounded_in_local_proposition")
    if proposal.confidence < float(min_confidence):
        reasons.append("revision_confidence_below_threshold")
    if proposal.mismatch < float(min_mismatch):
        reasons.append("revision_mismatch_below_threshold")
    if not isinstance(proposal.source, str) or not proposal.source.strip() or proposal.source == "unknown":
        reasons.append("revision_source_unknown")
    if not proposal.evidence_refs:
        reasons.append("revision_evidence_missing")
    return tuple(dict.fromkeys(reasons))


def _draft_from_dict(raw: Mapping[str, Any]) -> ExpressionDraft:
    units = raw.get("units", ())
    if isinstance(units, (str, bytes)) or not isinstance(units, (list, tuple)):
        raise ContractError("output_draft_units_invalid")
    diff = raw.get("diff", {})
    return ExpressionDraft(
        draft_id=str(raw.get("draft_id", "")),
        proposition_ref=str(raw.get("proposition_ref", "")),
        units=tuple(str(item) for item in units),
        proposition_units=tuple(
            str(item)
            for item in raw.get("proposition_units", ())
            if isinstance(item, str)
        ) if isinstance(raw.get("proposition_units", ()), (list, tuple)) else (),
        unit_granularity=str(raw.get("unit_granularity", "chunk")),
        tone=str(raw.get("tone", "neutral")),
        public_allowed=bool(raw.get("public_allowed", False)),
        renderer_source=str(raw.get("renderer_source", "ap_native")),
        diff=dict(diff) if isinstance(diff, Mapping) else {},
        status=str(raw.get("status", "draft")),
    )


def _persist_cursor(store: EventStore, draft: ExpressionDraft, cursor: OutputCursor) -> OutputCursor:
    saved = store.save_output_outbox(
        draft_id=draft.draft_id,
        proposition_ref=draft.proposition_ref,
        next_index=cursor.next_index,
        status=cursor.status,
        payload={"draft": draft.to_dict(), "cursor": cursor.to_dict()},
        updated_at=utc_now(),
    )
    cursor_raw = saved.get("cursor")
    if not isinstance(cursor_raw, Mapping):
        raise ContractError("output_outbox_cursor_missing")
    # The indexed columns are canonical even if an older payload did not yet
    # carry the latest cursor projection.
    return OutputCursor.from_dict(
        {
            **dict(cursor_raw),
            "draft_id": saved["draft_id"],
            "proposition_ref": saved["proposition_ref"],
            "next_index": saved["next_index"],
            "status": saved["status"],
        }
    )


def restore_output(store: EventStore, draft_id: str) -> tuple[ExpressionDraft, OutputCursor] | None:
    raw = store.get_output_outbox(draft_id)
    if raw is None:
        return None
    draft_raw = raw.get("draft")
    cursor_raw = raw.get("cursor")
    if not isinstance(draft_raw, Mapping) or not isinstance(cursor_raw, Mapping):
        raise ContractError("output_outbox_projection_incomplete")
    draft = _draft_from_dict(draft_raw)
    cursor = OutputCursor.from_dict(
        {
            **dict(cursor_raw),
            "draft_id": raw["draft_id"],
            "proposition_ref": raw["proposition_ref"],
            "next_index": raw["next_index"],
            "status": raw["status"],
        }
    )
    return draft, cursor


def set_output_status(
    store: EventStore,
    draft: ExpressionDraft,
    cursor: OutputCursor,
    *,
    status: str,
    last_error: str | None = None,
) -> OutputCursor:
    if status not in {"ready", "paused", "action_deferred", "failed", "completed", "withdrawn"}:
        raise ContractError("output_status_invalid")
    return _persist_cursor(store, draft, replace(cursor, status=status, last_error=last_error))


def _revision_diff(old_draft_id: str, proposal: ExpressionRevisionProposal) -> dict[str, Any]:
    return {
        "supersedes": old_draft_id,
        "proposal_id": proposal.proposal_id,
        "start_index": proposal.start_index,
        "delete_count": proposal.delete_count,
        "replacement_units": list(proposal.replacement_units),
        "preserves_proposition": proposal.preserves_proposition,
        "proposition_expected_units": list(proposal.proposition_expected_units),
        "mismatch": proposal.mismatch,
        "confidence": proposal.confidence,
        "source": proposal.source,
        "evidence_refs": list(proposal.evidence_refs),
        "lineage_refs": list(proposal.lineage_refs),
        "reason": proposal.reason,
    }


def output_revision_is_recoverable(
    store: EventStore,
    proposal: ExpressionRevisionProposal,
) -> bool:
    """Recognize only the exact successor of an interrupted revision action."""

    if not isinstance(store, EventStore) or not isinstance(proposal, ExpressionRevisionProposal):
        return False
    restored = restore_output(store, proposal.target_draft_id)
    if restored is None:
        return False
    old_draft, old_cursor = restored
    if old_cursor.status != "superseded":
        return False
    successor = restore_output(store, f"{old_draft.draft_id}~{proposal.proposal_id}")
    if successor is None:
        return False
    successor_draft, successor_cursor = successor
    return (
        successor_draft.proposition_ref == old_draft.proposition_ref
        and successor_draft.diff == _revision_diff(old_draft.draft_id, proposal)
        and successor_cursor.next_index == old_cursor.next_index
        and successor_cursor.status not in {"completed", "withdrawn", "superseded"}
    )


def apply_output_revision(
    store: EventStore,
    proposal: ExpressionRevisionProposal,
) -> tuple[ExpressionDraft, OutputCursor]:
    """Apply an accepted edit only to the undispatched suffix.

    This function does not select the proposal.  It is called only after a
    ``revise_expression`` candidate wins the shared ActionArena.
    """

    if not isinstance(proposal, ExpressionRevisionProposal):
        raise ContractError("output_revision_proposal_required")
    restored = restore_output(store, proposal.target_draft_id)
    if restored is None:
        raise ContractError("output_revision_target_missing")
    old_draft, old_cursor = restored
    new_draft_id = f"{old_draft.draft_id}~{proposal.proposal_id}"
    if old_cursor.status == "superseded":
        successor = restore_output(store, new_draft_id)
        if successor is None:
            raise ContractError("output_revision_successor_missing")
        successor_draft, successor_cursor = successor
        if successor_draft.diff != _revision_diff(old_draft.draft_id, proposal):
            raise ContractError("output_revision_successor_conflict")
        return successor_draft, successor_cursor
    if old_cursor.status in {"completed", "withdrawn"}:
        raise ContractError("output_revision_target_terminal")
    if isinstance(proposal.start_index, bool) or proposal.start_index < old_cursor.next_index:
        raise ContractError("output_revision_cannot_change_dispatched_prefix")
    if isinstance(proposal.delete_count, bool) or proposal.delete_count < 0:
        raise ContractError("output_revision_delete_count_invalid")
    end = proposal.start_index + proposal.delete_count
    if proposal.start_index > len(old_draft.units) or end > len(old_draft.units):
        raise ContractError("output_revision_range_out_of_bounds")
    if not proposal.replacement_units and proposal.delete_count == 0:
        raise ContractError("output_revision_has_no_change")
    if not proposal.preserves_proposition:
        raise ContractError("output_revision_cannot_change_proposition")
    if (
        proposal.delete_count != len(proposal.replacement_units)
        or len(proposal.replacement_units) != len(proposal.proposition_expected_units)
    ):
        raise ContractError("output_revision_span_changes_alignment")
    if tuple(proposal.replacement_units) != tuple(proposal.proposition_expected_units):
        raise ContractError("output_revision_not_supported_by_proposition")
    expected_end = proposal.start_index + len(proposal.proposition_expected_units)
    if (
        not old_draft.proposition_units
        or expected_end > len(old_draft.proposition_units)
        or old_draft.proposition_units[proposal.start_index:expected_end]
        != tuple(proposal.proposition_expected_units)
    ):
        raise ContractError("output_revision_not_grounded_in_local_proposition")
    new_units = (
        old_draft.units[: proposal.start_index]
        + tuple(proposal.replacement_units)
        + old_draft.units[end:]
    )
    if not new_units or old_cursor.next_index > len(new_units):
        raise ContractError("output_revision_invalid_result")
    new_draft = replace(
        old_draft,
        draft_id=new_draft_id,
        units=new_units,
        proposition_units=old_draft.proposition_units,
        public_allowed=True,
        diff=_revision_diff(old_draft.draft_id, proposal),
        status="native_committed",
    )
    new_cursor = replace(
        old_cursor,
        draft_id=new_draft_id,
        status="ready",
        last_error=None,
    )
    old_terminal = replace(old_cursor, status="superseded", last_error=None)
    _, saved = store.supersede_output_outbox(
        old_draft_id=old_draft.draft_id,
        new_draft_id=new_draft_id,
        proposition_ref=old_draft.proposition_ref,
        next_index=old_cursor.next_index,
        old_payload={"draft": old_draft.to_dict(), "cursor": old_terminal.to_dict()},
        new_payload={"draft": new_draft.to_dict(), "cursor": new_cursor.to_dict()},
        updated_at=utc_now(),
    )
    saved_draft = saved.get("draft")
    saved_cursor = saved.get("cursor")
    if not isinstance(saved_draft, Mapping) or not isinstance(saved_cursor, Mapping):
        raise ContractError("output_revision_projection_incomplete")
    return _draft_from_dict(saved_draft), OutputCursor.from_dict(
        {
            **dict(saved_cursor),
            "draft_id": saved["draft_id"],
            "proposition_ref": saved["proposition_ref"],
            "next_index": saved["next_index"],
            "status": saved["status"],
        }
    )


def output_control_result(
    store: EventStore,
    draft: ExpressionDraft,
    cursor: OutputCursor,
    *,
    action_ref: str,
    action_kind: str,
    idempotency_key: str,
    environment_id: str,
) -> OutputUnitResult:
    """Persist mechanical receipt/readback for pause, withdraw or revision."""

    receipt = store.get_dispatch_by_idempotency(idempotency_key)
    recovered = receipt is not None
    if receipt is None:
        receipt = store.append_dispatch(
            DispatchReceipt(
                action_ref=action_ref,
                environment_id=environment_id,
                idempotency_key=idempotency_key,
                status="accepted",
                connector_ref="ap-expression-control",
                evidence_refs=(f"expression-control:{action_kind}:{draft.draft_id}",),
                extra={
                    "draft_id": draft.draft_id,
                    "proposition_ref": draft.proposition_ref,
                    "unit_index": cursor.next_index,
                    "control_kind": action_kind,
                },
            )
        )
    result = store.get_result_for_receipt(receipt.receipt_id)
    if result is None:
        result = store.append_result(
            ResultEvent(
                receipt_ref=receipt.receipt_id,
                action_ref=receipt.action_ref,
                environment_id=environment_id,
                status="success",
                payload_inline={
                    "draft_id": draft.draft_id,
                    "proposition_ref": draft.proposition_ref,
                    "unit_index": cursor.next_index,
                    "control_kind": action_kind,
                    "cursor_status": cursor.status,
                },
                evidence_refs=receipt.evidence_refs,
                completeness="complete",
                lineage_refs=(receipt.receipt_id, receipt.action_ref, draft.draft_id),
            )
        )
    else:
        recovered = True
    return OutputUnitResult(cursor=cursor, receipt=receipt, result=result, recovered=recovered, draft=draft)


def prepare_output(
    draft: ExpressionDraft,
    *,
    outward_commitment: bool,
    store: EventStore | None = None,
) -> OutputCursor:
    if not isinstance(draft, ExpressionDraft):
        raise ContractError("output_requires_expression_draft")
    if not outward_commitment:
        raise ContractError("output_requires_outward_commitment")
    if draft.status not in {"rendered_private", "native_committed", "output_ready"}:
        raise ContractError("output_draft_not_committed")
    if not draft.units:
        raise ContractError("output_draft_has_no_units")
    cursor = OutputCursor(
        draft_id=draft.draft_id,
        proposition_ref=draft.proposition_ref,
        next_index=0,
        status="ready",
        committed=True,
    )
    if store is None:
        return cursor
    existing = restore_output(store, draft.draft_id)
    if existing is not None:
        existing_draft, existing_cursor = existing
        if existing_draft.to_dict() != draft.to_dict():
            raise ContractError("output_outbox_draft_snapshot_conflict")
        return existing_cursor
    return _persist_cursor(store, draft, cursor)


def dispatch_next_unit(
    store: EventStore,
    actuator: TextUnitActuator,
    draft: ExpressionDraft,
    cursor: OutputCursor,
    *,
    expected_index: int | None = None,
    readback_annotator: ExpressionReadbackAnnotator | None = None,
) -> OutputUnitResult:
    """Dispatch and read back one unit, advancing only on complete success."""

    if not isinstance(store, EventStore):
        raise ContractError("output_requires_event_store")
    if not isinstance(draft, ExpressionDraft) or not isinstance(cursor, OutputCursor):
        raise ContractError("output_requires_draft_and_cursor")
    restored = restore_output(store, draft.draft_id)
    if restored is not None:
        stored_draft, stored_cursor = restored
        if stored_draft.to_dict() != draft.to_dict():
            raise ContractError("output_outbox_draft_snapshot_conflict")
        cursor = stored_cursor
    else:
        cursor = _persist_cursor(store, draft, cursor)
    if draft.draft_id != cursor.draft_id or draft.proposition_ref != cursor.proposition_ref:
        raise ContractError("output_cursor_draft_conflict")
    if not cursor.committed:
        raise ContractError("output_cursor_not_committed")
    if expected_index is not None:
        if isinstance(expected_index, bool) or not isinstance(expected_index, int) or expected_index < 0:
            raise ContractError("output_expected_index_invalid")
        if expected_index >= len(draft.units):
            raise ContractError("output_expected_index_out_of_bounds")
        if cursor.next_index < expected_index:
            raise ContractError("output_cursor_has_not_reached_expected_index")
        if cursor.next_index > expected_index:
            key = f"text-unit:{draft.draft_id}:{expected_index}"
            receipt = store.get_dispatch_by_idempotency(key)
            result = store.get_result_for_receipt(receipt.receipt_id) if receipt is not None else None
            if receipt is None or result is None:
                raise ContractError("output_advanced_without_durable_receipt")
            return OutputUnitResult(
                cursor=cursor,
                unit=draft.units[expected_index],
                unit_index=expected_index,
                receipt=receipt,
                result=result,
                advanced=False,
                recovered=True,
                draft=draft,
            )
    if cursor.next_index >= len(draft.units):
        completed = replace(cursor, status="completed", last_error=None)
        completed = _persist_cursor(store, draft, completed)
        return OutputUnitResult(cursor=completed, draft=draft)

    index = cursor.next_index
    unit = draft.units[index]
    key = f"text-unit:{draft.draft_id}:{index}"
    receipt = store.get_dispatch_by_idempotency(key)
    recovered = receipt is not None
    if receipt is None and cursor.status == "dispatching":
        # The process may have died after touching a connector but before its
        # receipt was durable.  Without a connector receipt we cannot tell
        # whether the unit is already visible, so never blind-resend it.
        deferred = replace(
            cursor,
            status="action_deferred",
            last_error="unreceipted_dispatch_requires_reconciliation",
        )
        deferred = _persist_cursor(store, draft, deferred)
        return OutputUnitResult(deferred, unit, index, recovered=True, draft=draft)
    if receipt is None:
        cursor = _persist_cursor(store, draft, replace(cursor, status="dispatching", last_error=None))
        receipt = actuator.dispatch_unit(
            draft_id=draft.draft_id,
            proposition_ref=draft.proposition_ref,
            unit_index=index,
            unit=unit,
            idempotency_key=key,
        )
        receipt = store.append_dispatch(receipt)
    result = store.get_result_for_receipt(receipt.receipt_id)
    if result is None:
        raw_result = actuator.readback_unit(receipt)
        annotated_result = annotate_output_readback(
            raw_result,
            draft,
            cursor,
            dispatched_unit_index=index,
            annotator=readback_annotator,
        )
        result = store.append_result(annotated_result)
    else:
        recovered = True

    success = result.status == "success" and result.completeness == "complete"
    if success:
        next_index = index + 1
        status = "completed" if next_index >= len(draft.units) else "ready"
        updated = replace(
            cursor,
            next_index=next_index,
            status=status,
            receipt_refs=tuple(dict.fromkeys((*cursor.receipt_refs, receipt.receipt_id))),
            result_refs=tuple(dict.fromkeys((*cursor.result_refs, result.result_id))),
            last_error=None,
        )
    else:
        updated = replace(
            cursor,
            status="action_deferred" if result.retryable else "failed",
            receipt_refs=tuple(dict.fromkeys((*cursor.receipt_refs, receipt.receipt_id))),
            result_refs=tuple(dict.fromkeys((*cursor.result_refs, result.result_id))),
            last_error=result.error_code or result.status,
        )
    updated = _persist_cursor(store, draft, updated)
    return OutputUnitResult(updated, unit, index, receipt, result, success, recovered, draft)


@dataclass
class LocalTextUnitActuator:
    """A local physical-text fixture with exact unit readback."""

    environment_id: str = "text-local-fixture"
    units: list[str] = field(default_factory=list)
    fail_readback: bool = False
    receipts_by_key: dict[str, DispatchReceipt] = field(default_factory=dict)

    def dispatch_unit(
        self,
        *,
        draft_id: str,
        proposition_ref: str,
        unit_index: int,
        unit: str,
        idempotency_key: str,
    ) -> DispatchReceipt:
        prior = self.receipts_by_key.get(idempotency_key)
        if prior is not None:
            return prior
        if not isinstance(unit, str) or not unit:
            receipt = DispatchReceipt(
                action_ref=f"text:{draft_id}:{unit_index}",
                environment_id=self.environment_id,
                idempotency_key=idempotency_key,
                status="rejected",
                connector_ref="text-local-fixture",
                error_code="text_unit_empty",
            )
        else:
            self.units.append(unit)
            receipt = DispatchReceipt(
                action_ref=f"text:{draft_id}:{unit_index}",
                environment_id=self.environment_id,
                idempotency_key=idempotency_key,
                status="accepted",
                connector_ref="text-local-fixture",
                evidence_refs=(f"text-unit:{draft_id}:{unit_index}",),
                extra={
                    "draft_id": draft_id,
                    "proposition_ref": proposition_ref,
                    "unit_index": unit_index,
                    "unit": unit,
                    "fixture": True,
                },
            )
        self.receipts_by_key[idempotency_key] = receipt
        return receipt

    def readback_unit(self, receipt: DispatchReceipt) -> ResultEvent:
        if self.fail_readback:
            return ResultEvent(
                receipt_ref=receipt.receipt_id,
                action_ref=receipt.action_ref,
                environment_id=self.environment_id,
                status="unknown",
                completeness="unknown",
                error_code="text_readback_unavailable",
                retryable=True,
            )
        return ResultEvent(
            receipt_ref=receipt.receipt_id,
            action_ref=receipt.action_ref,
            environment_id=self.environment_id,
            status="success",
            payload_inline={
                "draft_id": receipt.extra.get("draft_id"),
                "proposition_ref": receipt.extra.get("proposition_ref"),
                "unit": receipt.extra.get("unit"),
                "unit_index": receipt.extra.get("unit_index"),
                "fixture": True,
            },
            evidence_refs=receipt.evidence_refs,
            completeness="complete",
            lineage_refs=(receipt.receipt_id, receipt.action_ref),
        )

    def text(self) -> str:
        return "".join(self.units)


__all__ = [
    "TextUnitActuator",
    "ExpressionReadbackAnnotator",
    "CallableExpressionReadbackAnnotator",
    "OutputCursor",
    "OutputUnitResult",
    "annotate_output_readback",
    "output_revision_rejection_reasons",
    "output_revision_is_recoverable",
    "prepare_output",
    "restore_output",
    "set_output_status",
    "apply_output_revision",
    "output_control_result",
    "dispatch_next_unit",
    "LocalTextUnitActuator",
]
