"""Stable, read-only projections for the first Mind Studio vertical slice.

The UI consumes this module's JSON-compatible view; it never reads SQLite or
runtime-private fields directly.  Projection cannot select actions, change a
frame or manufacture missing evidence.  Unavailable information stays null or
is listed under ``unknowns``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .runtime_types import TickResult


STUDIO_PROJECTION_VERSION = "0.1.0"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _texts(value: Any, *, limit: int = 128) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    return tuple(str(item)[:512] for item in list(value)[:limit] if isinstance(item, str))


def _selected_action(frame: Any) -> Mapping[str, Any] | None:
    selected_ref = _mapping(frame.decision).get("selected_candidate_ref")
    if not isinstance(selected_ref, str):
        return None
    return next(
        (
            item
            for item in frame.actions
            if isinstance(item, Mapping) and item.get("candidate_id") == selected_ref
        ),
        None,
    )


def _output_projection(frame: Any) -> dict[str, Any] | None:
    output = _mapping(frame.decision).get("output")
    if not isinstance(output, Mapping):
        return None
    cursor = _mapping(output.get("cursor"))
    draft = _mapping(output.get("draft"))
    result = _mapping(output.get("result"))
    receipt = _mapping(output.get("receipt"))
    return {
        "unit": output.get("unit") if isinstance(output.get("unit"), str) else None,
        "unit_index": output.get("unit_index") if isinstance(output.get("unit_index"), int) else None,
        "advanced": bool(output.get("advanced", False)),
        "recovered": bool(output.get("recovered", False)),
        "cursor": {
            "draft_id": cursor.get("draft_id"),
            "next_index": cursor.get("next_index"),
            "status": cursor.get("status"),
        },
        "draft": {
            "draft_id": draft.get("draft_id"),
            "units": list(_texts(draft.get("units"), limit=512)),
            "proposition_units": list(_texts(draft.get("proposition_units"), limit=512)),
            "renderer_source": draft.get("renderer_source"),
            "status": draft.get("status"),
            "diff": dict(_mapping(draft.get("diff"))),
        }
        if draft
        else None,
        "receipt": {
            "receipt_id": receipt.get("receipt_id"),
            "status": receipt.get("status"),
            "connector_ref": receipt.get("connector_ref"),
        }
        if receipt
        else None,
        "readback": {
            "result_id": result.get("result_id"),
            "status": result.get("status"),
            "completeness": result.get("completeness"),
            "control_kind": _mapping(result.get("payload_inline")).get("control_kind"),
        }
        if result
        else None,
    }


def _tick_projection(result: TickResult) -> dict[str, Any]:
    frame = result.frame
    selected = _selected_action(frame)
    output = _output_projection(frame)
    decision = _mapping(frame.decision)
    revision_review = decision.get("expression_revision_review")
    expression = frame.expression_draft.to_dict() if frame.expression_draft is not None else None
    proposition = frame.proposition.to_dict() if frame.proposition is not None else None
    output_unit = output.get("unit") if output else None
    control_kind = (
        _mapping(_mapping(output).get("readback")).get("control_kind") if output else None
    )
    timeline_kind = "cognition"
    if isinstance(output_unit, str):
        timeline_kind = "output_unit"
    elif isinstance(control_kind, str):
        timeline_kind = "control_readback"
    elif frame.sa.source == "readback":
        timeline_kind = "readback"

    scores = {
        str(item.get("candidate_ref")): item.get("score")
        for item in decision.get("scores", ())
        if isinstance(item, Mapping) and isinstance(item.get("candidate_ref"), str)
    }
    actions = []
    for action in frame.actions:
        if not isinstance(action, Mapping):
            continue
        components = _mapping(action.get("components"))
        actions.append(
            {
                "candidate_id": action.get("candidate_id"),
                "kind": action.get("kind"),
                "target": action.get("target"),
                "proposition": action.get("proposition"),
                "source": action.get("source"),
                "owner": action.get("owner"),
                "score": scores.get(str(action.get("candidate_id")), action.get("score")),
                "components": dict(components),
                "selected": bool(
                    action.get("candidate_id") == decision.get("selected_candidate_ref")
                ),
            }
        )

    return {
        "tick_index": frame.tick_index,
        "frame_id": frame.frame_id,
        "timeline_kind": timeline_kind,
        "trigger_refs": list(frame.trigger_refs),
        "sa": {
            "occurrence_id": frame.sa.occurrence_id,
            "text": frame.sa.text,
            "modality": frame.sa.modality,
            "source": frame.sa.source,
            "novelty": frame.sa.novelty,
            "evidence_refs": list(frame.sa.evidence_refs),
            "lineage_refs": list(frame.sa.lineage_refs),
        },
        "current_field": dict(frame.current_field),
        "recall": [item.to_dict() for item in frame.b_recall],
        "prediction": [item.to_dict() for item in frame.c_prediction],
        "feelings": [item.to_dict() for item in frame.feelings],
        "slow_affect": dict(frame.slow_affect),
        "attention": dict(frame.attention),
        "thoughts": [item.to_dict() for item in frame.thoughts],
        "paradigms": [item.to_dict() for item in frame.paradigms],
        "thought_stream": frame.thought_stream.to_dict() if frame.thought_stream else None,
        "proposition": proposition,
        "expression_draft": expression,
        "actions": actions,
        "decision": {
            "status": decision.get("status"),
            "selected_candidate_ref": decision.get("selected_candidate_ref"),
            "selected_kind": selected.get("kind") if selected else None,
            "owner": decision.get("decision_owner"),
            "reason": decision.get("reason"),
            "closure": decision.get("closure"),
            "result_status": decision.get("result_status"),
            "result_completeness": decision.get("result_completeness"),
            "revision_review": dict(revision_review)
            if isinstance(revision_review, Mapping)
            else None,
        },
        "output": output,
        "counters": dict(frame.counters),
        "phase_completeness": dict(frame.phase_completeness),
        "frontiers": [dict(item) for item in frame.frontiers],
        "cognitive_trial_observations": {
            str(capability): [dict(item) for item in observations]
            for capability, observations in frame.cognitive_trial_observations.items()
        },
        "research": {
            "state_pool": dict(frame.state_pool),
            "gateway": dict(frame.gateway),
            "process_refs": list(frame.process_refs),
            "result_ref": frame.result_ref,
            "completeness": frame.completeness,
        },
    }


def project_tick(result: TickResult) -> dict[str, Any]:
    """Return the stable read-only projection for one persisted AP tick.

    Product-specific views may reuse this function instead of parsing runtime
    internals or copying the projection logic.  It cannot select an action or
    mutate the frame.
    """

    if not isinstance(result, TickResult):
        raise TypeError("tick_result_required")
    return _tick_projection(result)


def _causal_summary(ticks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not ticks:
        return []
    first = ticks[0]
    revision = next(
        (
            item
            for item in ticks
            if _mapping(_mapping(item.get("decision")).get("revision_review")).get("eligible")
        ),
        None,
    )
    last = ticks[-1]
    nodes: list[dict[str, Any]] = [
        {
            "key": "event",
            "label": "外部事件",
            "detail": _mapping(first.get("sa")).get("text"),
            "state": "complete",
        },
        {
            "key": "proposition",
            "label": "AP 核心意思",
            "detail": _mapping(first.get("proposition")).get("content"),
            "state": "complete" if first.get("proposition") else "unknown",
        },
        {
            "key": "surface",
            "label": "表达草稿",
            "detail": "".join(_texts(_mapping(first.get("expression_draft")).get("units"), limit=512)),
            "state": "complete" if first.get("expression_draft") else "unknown",
        },
    ]
    if revision is not None:
        feelings = [
            str(item.get("name"))
            for item in revision.get("feelings", ())
            if isinstance(item, Mapping)
        ]
        nodes.extend(
            (
                {
                    "key": "mismatch",
                    "label": "readback 错配",
                    "detail": "、".join(feelings) or "检测到未发后缀与核心意思不一致",
                    "state": "attention",
                },
                {
                    "key": "arena",
                    "label": "行动竞争",
                    "detail": _mapping(revision.get("decision")).get("selected_kind"),
                    "state": "complete",
                },
                {
                    "key": "revision",
                    "label": "局部修订",
                    "detail": "修订未发后缀，已发现实不回写",
                    "state": "complete",
                },
            )
        )
    nodes.append(
        {
            "key": "readback",
            "label": "物理输出与回读",
            "detail": _mapping(last.get("decision")).get("closure"),
            "state": "complete" if _mapping(last.get("decision")).get("status") == "abstained" else "partial",
        }
    )
    return nodes[:7]


@dataclass(frozen=True)
class EpisodeView:
    request_id: str
    episode_id: str
    status: str
    proposition_text: str
    requested_surface_text: str | None
    physical_output: str
    ticks: tuple[Mapping[str, Any], ...]
    counters: Mapping[str, int]
    causal_summary: tuple[Mapping[str, Any], ...]
    product_effect: str = "local_runtime_episode"
    ownership: Mapping[str, str] = field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    projection_version: str = STUDIO_PROJECTION_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_version": self.projection_version,
            "request_id": self.request_id,
            "episode_id": self.episode_id,
            "status": self.status,
            "input": {
                "proposition_text": self.proposition_text,
                "requested_surface_text": self.requested_surface_text,
            },
            "physical_output": self.physical_output,
            "counters": dict(self.counters),
            "causal_summary": [dict(item) for item in self.causal_summary],
            "ticks": [dict(item) for item in self.ticks],
            "product_effect": self.product_effect,
            "ownership": dict(self.ownership),
            "limitations": list(self.limitations),
            "unknowns": list(self.unknowns),
        }


def project_episode(
    *,
    request_id: str,
    episode_id: str,
    proposition_text: str,
    requested_surface_text: str | None,
    results: Sequence[TickResult],
    status: str,
    limitations: Sequence[str] = (),
    unknowns: Sequence[str] = (),
) -> EpisodeView:
    """Project an already executed episode without altering its state."""

    ticks = tuple(_tick_projection(item) for item in results)
    physical_output = "".join(
        str(_mapping(item.get("output")).get("unit"))
        for item in ticks
        if isinstance(_mapping(item.get("output")).get("unit"), str)
        and bool(_mapping(item.get("output")).get("advanced"))
    )
    counters = dict(_mapping(ticks[-1].get("counters"))) if ticks else {}
    return EpisodeView(
        request_id=request_id,
        episode_id=episode_id,
        status=status,
        proposition_text=proposition_text,
        requested_surface_text=requested_surface_text,
        physical_output=physical_output,
        ticks=ticks,
        counters={str(key): int(value) for key, value in counters.items() if isinstance(value, int)},
        causal_summary=tuple(_causal_summary(ticks)),
        ownership={
            "tick_and_action_arena": "native",
            "proposition": "native",
            "surface_renderer": "assisted_fixture" if requested_surface_text is not None else "native",
            "readback_annotation": "assisted_fixture",
            "physical_output_and_readback": "native",
            "open_language_quality": "absent",
        },
        limitations=tuple(dict.fromkeys(str(item) for item in limitations if str(item))),
        unknowns=tuple(dict.fromkeys(str(item) for item in unknowns if str(item))),
    )


__all__ = [
    "STUDIO_PROJECTION_VERSION",
    "EpisodeView",
    "project_tick",
    "project_episode",
]
