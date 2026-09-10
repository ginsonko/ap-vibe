"""Replaceable Hybrid cognition gateway for the first vertical slice.

The gateway is intentionally proposal-shaped.  It may be backed by an LLM in
Wave 1B, a local model, or no model at all.  It never owns persistence,
physical evidence, or a second action arena; the runtime folds its response
into the current frame and the single ``DecisionSlot``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import re
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib import error as urllib_error
from urllib import request as urllib_request

from .contracts import DelegationDecision, utc_now


_SECRET_PATTERNS = (
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s,;\"']{8,}"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;\"']{8,}"),
)


_TEACHER_CAPABILITIES = frozenset(
    {
        "project.action_selection",
        "project.recall",
        "project.prediction",
        "project.appraisal",
        "project.thought",
        "project.paradigm",
        "project.attention",
        "project.expression",
        "project.parameter_tuning",
    }
)
_PARADIGM_KINDS = frozenset({"relation_frame", "sequence_frame", "expression_frame"})
_PARADIGM_INVARIANTS = frozenset(
    {
        "source_completeness",
        "has_open_items",
        "has_unknowns",
        "has_conflicts",
        "has_next_action",
    }
)
_PARADIGM_SLOT_SOURCES = frozenset(
    {
        "activity.summary",
        "activity.observed_next_action",
        "activity.observed_remaining",
        "activity.observed_unknown",
        "proposition.content",
        "recall.summary",
    }
)
_PARADIGM_RELATIONS = frozenset(
    {"precedes", "corresponds_to", "constrains", "supports", "contrasts_with", "fills"}
)
_ATTENTION_MODES = frozenset({"maintain_attention", "shift_attention", "diversify_attention"})
_EXPRESSION_TONES = frozenset({"neutral", "warm", "concise", "careful"})
_PARAMETER_BOUNDS: Mapping[str, tuple[float, float]] = {
    "attention.novelty_weight": (0.05, 0.45),
    "attention.mismatch_weight": (0.05, 0.45),
    "attention.recall_weight": (0.05, 0.45),
    "attention.open_goal_weight": (0.05, 0.45),
    "attention.paradigm_weight": (0.05, 0.45),
    "attention.fatigue_inhibition_weight": (0.02, 0.30),
}
_PARAMETER_SINGLE_DELTA_LIMIT = 0.05
_PARAMETER_DIRECTIONS = frozenset({"increase", "decrease"})

# E3's first expression curriculum deliberately learns only pragmatic surface
# organisation around one frozen claim.  These are grammar permissions, not
# answer templates: none of the affixes may introduce a fact, promise or
# assertion of external action.
_PRAGMATIC_PREFIXES = frozenset(
    {
        "",
        "我目前能确认的是：",
        "按现有信息，",
        "就目前看到的情况，",
        "我先说明能确认的部分：",
    }
)
_PRAGMATIC_SUFFIXES = frozenset({"", "。"})


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bounded_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", text)
    return text if len(text) <= limit else text[:limit] + "…"


def _bounded_mapping(value: Any, *, depth: int = 0, max_depth: int = 3, max_items: int = 64) -> Any:
    """Copy a provider input without allowing an unbounded prompt surface."""

    if depth >= max_depth:
        return _bounded_text(value, 1024)
    if isinstance(value, Mapping):
        return {
            _bounded_text(key, 96): _bounded_mapping(item, depth=depth + 1, max_depth=max_depth, max_items=max_items)
            for key, item in list(value.items())[:max_items]
        }
    if isinstance(value, (list, tuple)):
        return [
            _bounded_mapping(item, depth=depth + 1, max_depth=max_depth, max_items=max_items)
            for item in list(value)[:max_items]
        ]
    if isinstance(value, str):
        return _bounded_text(value, 8192)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _bounded_text(value, 1024)


def _extract_json_object(text: str) -> Mapping[str, Any] | None:
    """Parse a JSON object, tolerating one markdown fence but no free prose."""

    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    try:
        parsed = json.loads(candidate)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _mapping_sequence(value: Any, *, limit: int) -> tuple[Mapping[str, Any], ...]:
    """Return a bounded tuple of mapping items from an untrusted value."""

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    return tuple(item for item in list(value)[:limit] if isinstance(item, Mapping))


def _text_sequence(value: Any, *, limit: int, text_limit: int) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    return tuple(
        _bounded_text(item, text_limit)
        for item in list(value)[:limit]
        if isinstance(item, str) and item.strip()
    )


def _number(value: Any, default: float, *, low: float, high: float) -> tuple[float, bool]:
    """Clamp one provider number and report whether its original value was valid."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(default), False
    numeric = float(value)
    return max(low, min(high, numeric)), low <= numeric <= high


def _reference_inventory(frame_view: Mapping[str, Any]) -> tuple[set[str], set[str], set[str]]:
    """Collect refs the model may cite without turning its prose into evidence."""

    allowed: set[str] = set()
    memory: set[str] = set()
    actions: set[str] = set()

    def add(value: Any, target: set[str] = allowed) -> None:
        if isinstance(value, str) and value:
            target.add(value)

    def add_many(value: Any, target: set[str] = allowed) -> None:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                add(item, target)

    sa = frame_view.get("sa")
    if isinstance(sa, Mapping):
        for key in ("event_ref", "occurrence_id"):
            add(sa.get(key))
        for key in ("evidence_refs", "lineage_refs"):
            add_many(sa.get(key))

    for item in _mapping_sequence(frame_view.get("b_recall"), limit=64):
        for key in ("ref", "event_ref"):
            add(item.get(key), memory)
            add(item.get(key))
        add_many(item.get("lineage_refs"))

    for item in _mapping_sequence(frame_view.get("c_prediction"), limit=32):
        add(item.get("prediction_id"))
        add_many(item.get("anchor_refs"))

    for item in _mapping_sequence(frame_view.get("feelings"), limit=32):
        add_many(item.get("source_refs"))

    for key in ("proposition", "thought", "expression_draft"):
        item = frame_view.get(key)
        if not isinstance(item, Mapping):
            continue
        for ref_key in (
            "proposition_id",
            "thought_id",
            "draft_id",
            "proposition_ref",
            "attention_ref",
        ):
            add(item.get(ref_key))
        for refs_key in (
            "source_refs",
            "evidence_refs",
            "lineage_refs",
            "predecessor_refs",
            "event_refs",
        ):
            add_many(item.get(refs_key))

    for item in _mapping_sequence(frame_view.get("action_candidates"), limit=128):
        add(item.get("candidate_id"), actions)

    return allowed, memory, actions


def _validated_refs(value: Any, *, allowed: set[str], limit: int = 32) -> tuple[tuple[str, ...], tuple[str, ...]]:
    proposed = _text_sequence(value, limit=limit, text_limit=160)
    accepted = tuple(dict.fromkeys(item for item in proposed if item in allowed))
    rejected = tuple(dict.fromkeys(item for item in proposed if item not in allowed))
    return accepted, rejected


def _requested_capability_inventory(
    frame_view: Mapping[str, Any],
) -> tuple[set[str], tuple[str, ...], bool]:
    """Return the capability subset requested for this shared teacher call.

    Older callers do not carry a sampling plan, so absence means the complete
    supported contract.  An explicitly empty set remains empty: a caller that
    nevertheless invokes the provider cannot accidentally re-enable every
    teaching surface.
    """

    raw = frame_view.get("requested_capabilities")
    explicit = raw is not None
    if raw is None:
        ordered = tuple(sorted(_TEACHER_CAPABILITIES))
        return set(ordered), ordered, False
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        return set(), (), True
    ordered = tuple(
        dict.fromkeys(
            _bounded_text(item, 160)
            for item in list(raw)[:32]
            if isinstance(item, str) and item in _TEACHER_CAPABILITIES
        )
    )
    return set(ordered), ordered, explicit


def _activity_structure_profile(frame_view: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = frame_view.get("activity_profile")
    if not isinstance(raw, Mapping):
        return {}
    return {
        key: raw[key]
        for key in _PARADIGM_INVARIANTS
        if key in raw and isinstance(raw[key], (str, int, float, bool)) and raw[key] is not None
    }


def _slot_source_available(frame_view: Mapping[str, Any], source: str) -> bool:
    if source == "proposition.content":
        proposition = frame_view.get("proposition")
        return isinstance(proposition, Mapping) and isinstance(proposition.get("content"), str)
    if source == "recall.summary":
        return any(isinstance(item.get("summary"), str) for item in _mapping_sequence(frame_view.get("b_recall"), limit=64))
    if source.startswith("activity."):
        activity = frame_view.get("activity")
        field_name = source.split(".", 1)[1]
        return isinstance(activity, Mapping) and field_name in activity
    return False


def _normalise_paradigm_candidates(
    raw: Mapping[str, Any],
    *,
    frame_view: Mapping[str, Any],
    call_id: str,
    input_refs: tuple[str, ...],
    allowed_refs: set[str],
    requested: set[str],
) -> tuple[Mapping[str, Any], ...]:
    profile = _activity_structure_profile(frame_view)
    output: list[Mapping[str, Any]] = []
    for index, item in enumerate(_mapping_sequence(raw.get("paradigm_candidates"), limit=6)):
        reasons: list[str] = []
        if "project.paradigm" not in requested:
            reasons.append("capability_not_requested")
        pattern_kind = _bounded_text(item.get("pattern_kind"), 40)
        if pattern_kind not in _PARADIGM_KINDS:
            reasons.append("unsupported_pattern_kind")

        invariants: dict[str, Any] = {}
        raw_invariants = item.get("invariants")
        if not isinstance(raw_invariants, Mapping) or not raw_invariants:
            reasons.append("missing_structural_invariants")
        else:
            for raw_key, value in list(raw_invariants.items())[:8]:
                key = _bounded_text(raw_key, 80)
                if key not in _PARADIGM_INVARIANTS:
                    reasons.append(f"unsupported_invariant:{key}")
                    continue
                if key not in profile:
                    reasons.append(f"unobserved_invariant:{key}")
                    continue
                if _canonical_hash(value) != _canonical_hash(profile[key]):
                    reasons.append(f"invariant_value_not_observed:{key}")
                    continue
                invariants[key] = value

        slots: list[Mapping[str, Any]] = []
        seen_slots: set[str] = set()
        raw_slots = item.get("slots")
        for slot_index, slot in enumerate(_mapping_sequence(raw_slots, limit=8)):
            name = _bounded_text(slot.get("name"), 40)
            source = _bounded_text(slot.get("source"), 80)
            required = slot.get("required", True)
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name):
                reasons.append(f"invalid_slot_name:{slot_index}")
                continue
            if name in seen_slots:
                reasons.append(f"duplicate_slot_name:{name}")
                continue
            if source not in _PARADIGM_SLOT_SOURCES:
                reasons.append(f"unsupported_slot_source:{name}")
                continue
            if not _slot_source_available(frame_view, source):
                reasons.append(f"unobserved_slot_source:{name}")
                continue
            if not isinstance(required, bool):
                reasons.append(f"invalid_slot_required:{name}")
                continue
            seen_slots.add(name)
            slots.append({"name": name, "source": source, "required": required})
        if not slots:
            reasons.append("no_supported_slots")

        relations: list[Mapping[str, Any]] = []
        for relation_index, relation in enumerate(_mapping_sequence(item.get("relations"), limit=12)):
            source_slot = _bounded_text(relation.get("source_slot"), 40)
            target_slot = _bounded_text(relation.get("target_slot"), 40)
            relation_kind = _bounded_text(relation.get("relation"), 40)
            if source_slot not in seen_slots or target_slot not in seen_slots:
                reasons.append(f"relation_slot_unobserved:{relation_index}")
                continue
            if relation_kind not in _PARADIGM_RELATIONS:
                reasons.append(f"unsupported_relation:{relation_index}")
                continue
            relations.append(
                {"source_slot": source_slot, "target_slot": target_slot, "relation": relation_kind}
            )

        confidence, confidence_valid = _number(item.get("confidence", 0.0), 0.0, low=0.0, high=1.0)
        uncertainty, uncertainty_valid = _number(item.get("uncertainty", 1.0), 1.0, low=0.0, high=1.0)
        if not confidence_valid:
            reasons.append("invalid_confidence")
        if not uncertainty_valid:
            reasons.append("invalid_uncertainty")
        evidence_refs, rejected_refs = _validated_refs(item.get("evidence_refs"), allowed=allowed_refs)
        if rejected_refs:
            reasons.append("unobserved_evidence_ref")
        completeness = _bounded_text(item.get("completeness") or "unknown", 40)
        if completeness not in {"complete", "partial", "unknown", "search_incomplete"}:
            completeness = "unknown"
            reasons.append("invalid_completeness")
        output.append(
            _candidate_envelope(
                kind="paradigm",
                index=index,
                item=item,
                call_id=call_id,
                input_refs=(*input_refs, *evidence_refs),
                uncertainty=uncertainty,
                rejection_reasons=reasons,
                content={
                    "pattern_kind": pattern_kind,
                    "invariants": invariants,
                    "slots": slots,
                    "relations": relations,
                    "confidence": round(confidence, 6),
                    "uncertainty": round(uncertainty, 6),
                    "evidence_refs": list(evidence_refs),
                    "completeness": completeness,
                    "counterexamples": list(
                        _text_sequence(item.get("counterexamples"), limit=8, text_limit=480)
                    ),
                },
            )
        )
    return tuple(output)


def _normalise_attention_candidates(
    raw: Mapping[str, Any],
    *,
    call_id: str,
    input_refs: tuple[str, ...],
    allowed_refs: set[str],
    requested: set[str],
) -> tuple[Mapping[str, Any], ...]:
    output: list[Mapping[str, Any]] = []
    for index, item in enumerate(_mapping_sequence(raw.get("attention_candidates"), limit=6)):
        reasons: list[str] = []
        if "project.attention" not in requested:
            reasons.append("capability_not_requested")
        mode = _bounded_text(item.get("mode"), 40)
        if mode not in _ATTENTION_MODES:
            reasons.append("unsupported_attention_mode")
        target_ref = _bounded_text(item.get("target_ref"), 160)
        if not target_ref:
            reasons.append("missing_attention_target_ref")
        elif target_ref not in allowed_refs:
            reasons.append("unobserved_attention_target_ref")
        gain, gain_valid = _number(item.get("gain_delta", 0.0), 0.0, low=-1.0, high=1.0)
        if not gain_valid:
            reasons.append("attention_gain_out_of_bounds")
        uncertainty, uncertainty_valid = _number(item.get("uncertainty", 1.0), 1.0, low=0.0, high=1.0)
        if not uncertainty_valid:
            reasons.append("invalid_uncertainty")
        source_refs, rejected_refs = _validated_refs(item.get("source_refs"), allowed=allowed_refs)
        if rejected_refs:
            reasons.append("unobserved_source_ref")
        output.append(
            _candidate_envelope(
                kind="attention",
                index=index,
                item=item,
                call_id=call_id,
                input_refs=(*input_refs, *source_refs, *((target_ref,) if target_ref in allowed_refs else ())),
                uncertainty=uncertainty,
                rejection_reasons=reasons,
                content={
                    "mode": mode,
                    "target_ref": target_ref or None,
                    "gain_delta": round(gain, 6),
                    "rationale": _bounded_text(item.get("rationale"), 1200),
                    "source_refs": list(source_refs),
                    "uncertainty": round(uncertainty, 6),
                },
            )
        )
    return tuple(output)


def _normalise_expression_candidates(
    raw: Mapping[str, Any],
    *,
    call_id: str,
    input_refs: tuple[str, ...],
    allowed_refs: set[str],
    requested: set[str],
) -> tuple[Mapping[str, Any], ...]:
    output: list[Mapping[str, Any]] = []
    for index, item in enumerate(_mapping_sequence(raw.get("expression_candidates"), limit=6)):
        reasons: list[str] = []
        if "project.expression" not in requested:
            reasons.append("capability_not_requested")
        template = _bounded_text(item.get("template"), 320)
        if template.count("{claim}") != 1:
            reasons.append("expression_requires_exactly_one_claim_slot")
            prefix = ""
            suffix = ""
        else:
            prefix, suffix = template.split("{claim}", 1)
            if "{" in prefix or "}" in prefix or "{" in suffix or "}" in suffix:
                reasons.append("expression_contains_unsupported_slot")
            if prefix not in _PRAGMATIC_PREFIXES:
                reasons.append("expression_prefix_not_pragmatic")
            if suffix not in _PRAGMATIC_SUFFIXES:
                reasons.append("expression_suffix_not_pragmatic")
        tone = _bounded_text(item.get("tone") or "neutral", 40)
        if tone not in _EXPRESSION_TONES:
            tone = "neutral"
            reasons.append("unsupported_expression_tone")
        uncertainty, uncertainty_valid = _number(item.get("uncertainty", 1.0), 1.0, low=0.0, high=1.0)
        if not uncertainty_valid:
            reasons.append("invalid_uncertainty")
        evidence_refs, rejected_refs = _validated_refs(item.get("evidence_refs"), allowed=allowed_refs)
        if rejected_refs:
            reasons.append("unobserved_evidence_ref")
        output.append(
            _candidate_envelope(
                kind="expression",
                index=index,
                item=item,
                call_id=call_id,
                input_refs=(*input_refs, *evidence_refs),
                uncertainty=uncertainty,
                rejection_reasons=reasons,
                content={
                    "template": template,
                    "prefix": prefix,
                    "suffix": suffix,
                    "tone": tone,
                    "evidence_refs": list(evidence_refs),
                    "uncertainty": round(uncertainty, 6),
                    "counterexamples": list(
                        _text_sequence(item.get("counterexamples"), limit=8, text_limit=480)
                    ),
                },
            )
        )
    return tuple(output)


def _normalise_parameter_candidates(
    raw: Mapping[str, Any],
    *,
    frame_view: Mapping[str, Any],
    call_id: str,
    input_refs: tuple[str, ...],
    allowed_refs: set[str],
    requested: set[str],
) -> tuple[Mapping[str, Any], ...]:
    """Validate bounded, source-tagged deltas for the attention gain ledger.

    Parameters are meta-modulation proposals, not executable configuration or
    action advice.  A valid item can become only an independently reviewed
    curriculum trial in a later episode.
    """

    output: list[Mapping[str, Any]] = []
    for index, item in enumerate(_mapping_sequence(raw.get("parameter_candidates"), limit=6)):
        reasons: list[str] = []
        if "project.parameter_tuning" not in requested:
            reasons.append("capability_not_requested")
        parameter = _bounded_text(item.get("parameter"), 120)
        bounds = _PARAMETER_BOUNDS.get(parameter)
        if bounds is None:
            reasons.append("unsupported_parameter")
        delta, delta_valid = _number(
            item.get("delta", 0.0),
            0.0,
            low=-_PARAMETER_SINGLE_DELTA_LIMIT,
            high=_PARAMETER_SINGLE_DELTA_LIMIT,
        )
        if not delta_valid:
            reasons.append("parameter_delta_out_of_bounds")
        if abs(delta) <= 1e-12:
            reasons.append("parameter_delta_zero")
        expected_direction = _bounded_text(item.get("expected_direction"), 40)
        if expected_direction not in _PARAMETER_DIRECTIONS:
            reasons.append("unsupported_expected_direction")
        elif (expected_direction == "increase" and delta <= 0.0) or (
            expected_direction == "decrease" and delta >= 0.0
        ):
            reasons.append("parameter_direction_delta_mismatch")
        source_refs, rejected_refs = _validated_refs(item.get("source_refs"), allowed=allowed_refs)
        if not source_refs:
            reasons.append("missing_observed_source_ref")
        if rejected_refs:
            reasons.append("unobserved_source_ref")
        uncertainty, uncertainty_valid = _number(item.get("uncertainty", 1.0), 1.0, low=0.0, high=1.0)
        if not uncertainty_valid:
            reasons.append("invalid_uncertainty")
        scope = item.get("scope")
        observed_profile = _activity_structure_profile(frame_view)
        # The provider may describe scope, but cannot introduce content/event
        # routes.  Runtime later replaces it with the observed evidence profile.
        if isinstance(scope, Mapping):
            unsupported_scope = tuple(str(key) for key in scope if str(key) not in _PARADIGM_INVARIANTS)
            if unsupported_scope:
                reasons.append("unsupported_parameter_scope")
            for key, value in scope.items():
                if key in _PARADIGM_INVARIANTS and (
                    key not in observed_profile or _canonical_hash(value) != _canonical_hash(observed_profile[key])
                ):
                    reasons.append(f"parameter_scope_not_observed:{key}")
        output.append(
            _candidate_envelope(
                kind="parameter",
                index=index,
                item=item,
                call_id=call_id,
                input_refs=(*input_refs, *source_refs),
                uncertainty=uncertainty,
                rejection_reasons=reasons,
                content={
                    "parameter": parameter,
                    "delta": round(delta, 6),
                    "rationale": _bounded_text(item.get("rationale"), 1200),
                    "source_refs": list(source_refs),
                    "uncertainty": round(uncertainty, 6),
                    "counterexamples": list(
                        _text_sequence(item.get("counterexamples"), limit=8, text_limit=480)
                    ),
                    "scope": dict(observed_profile),
                    "expected_direction": expected_direction,
                    "bounds": list(bounds) if bounds is not None else None,
                },
            )
        )
    return tuple(output)


def _candidate_identity(kind: str, index: int, item: Mapping[str, Any]) -> str:
    supplied = item.get("candidate_id")
    if isinstance(supplied, str) and supplied.strip():
        return _bounded_text(supplied, 160)
    digest = _canonical_hash({"kind": kind, "index": index, "candidate": _bounded_mapping(item)})[:20]
    return f"teacher_{kind}_{digest}"


def _candidate_envelope(
    *,
    kind: str,
    index: int,
    item: Mapping[str, Any],
    call_id: str,
    input_refs: Sequence[str],
    uncertainty: float,
    rejection_reasons: Sequence[str],
    content: Mapping[str, Any],
) -> dict[str, Any]:
    reasons = tuple(dict.fromkeys(_bounded_text(reason, 240) for reason in rejection_reasons if reason))
    known_keys = {"candidate_id", "ref", "memory_ref", "uncertainty", *content.keys()}
    return {
        "candidate_id": _candidate_identity(kind, index, item),
        "kind": kind,
        **dict(content),
        "source": "llm_model",
        "owner": "llm",
        "model_receipt_ref": call_id,
        "input_refs": list(dict.fromkeys(_bounded_text(ref, 160) for ref in input_refs if ref)),
        "uncertainty": round(_clamp(uncertainty), 6),
        "limitations": list(reasons),
        "validation": "rejected" if reasons else "accepted",
        "rejection_reasons": list(reasons),
        "extensions": _bounded_mapping(
            {str(key): value for key, value in item.items() if str(key) not in known_keys},
            max_depth=4,
        ),
    }


def _normalise_teacher_candidates(
    raw: Mapping[str, Any],
    *,
    frame_view: Mapping[str, Any],
    call_id: str,
    input_refs: tuple[str, ...],
) -> tuple[
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
    dict[str, float],
    str | None,
    tuple[Mapping[str, Any], ...],
]:
    """Normalize all teacher surfaces without granting semantic authority."""

    allowed_refs, memory_refs, action_ids = _reference_inventory(frame_view)
    requested, _, _ = _requested_capability_inventory(frame_view)
    issues: list[Mapping[str, Any]] = []

    recalls: list[Mapping[str, Any]] = []
    for index, item in enumerate(_mapping_sequence(raw.get("recall_candidates"), limit=8)):
        reasons: list[str] = []
        if "project.recall" not in requested:
            reasons.append("capability_not_requested")
        memory_ref = item.get("memory_ref", item.get("ref"))
        memory_ref = _bounded_text(memory_ref, 160) if isinstance(memory_ref, str) else ""
        if not memory_ref:
            reasons.append("missing_memory_ref")
        elif memory_ref not in memory_refs:
            reasons.append("unobserved_memory_ref")
        relevance, relevance_valid = _number(item.get("relevance", 0.0), 0.0, low=0.0, high=1.0)
        if not relevance_valid:
            reasons.append("invalid_relevance")
        recalls.append(
            _candidate_envelope(
                kind="recall",
                index=index,
                item=item,
                call_id=call_id,
                input_refs=(*input_refs, *((memory_ref,) if memory_ref in memory_refs else ())),
                uncertainty=1.0 - relevance,
                rejection_reasons=reasons,
                content={
                    "memory_ref": memory_ref or None,
                    "summary": _bounded_text(item.get("summary"), 1200),
                    "relevance": round(relevance, 6),
                    "rationale": _bounded_text(item.get("rationale"), 1200),
                },
            )
        )

    predictions: list[Mapping[str, Any]] = []
    for index, item in enumerate(_mapping_sequence(raw.get("prediction_candidates"), limit=8)):
        reasons: list[str] = []
        if "project.prediction" not in requested:
            reasons.append("capability_not_requested")
        content_text = _bounded_text(item.get("content"), 1600)
        if not content_text.strip():
            reasons.append("missing_prediction_content")
        confidence, confidence_valid = _number(item.get("confidence", 0.0), 0.0, low=0.0, high=1.0)
        uncertainty, uncertainty_valid = _number(item.get("uncertainty", 1.0), 1.0, low=0.0, high=1.0)
        if not confidence_valid:
            reasons.append("invalid_confidence")
        if not uncertainty_valid:
            reasons.append("invalid_uncertainty")
        evidence_refs, rejected_refs = _validated_refs(item.get("evidence_refs"), allowed=allowed_refs)
        if rejected_refs:
            reasons.append("unobserved_evidence_ref")
        completeness = _bounded_text(item.get("completeness") or "unknown", 40)
        if completeness not in {"complete", "partial", "unknown", "search_incomplete"}:
            completeness = "unknown"
            reasons.append("invalid_completeness")
        mode = _bounded_text(item.get("mode") or "forecast", 40)
        if mode not in {"forecast", "attribution", "relationship"}:
            mode = "forecast"
            reasons.append("invalid_prediction_mode")
        predictions.append(
            _candidate_envelope(
                kind="prediction",
                index=index,
                item=item,
                call_id=call_id,
                input_refs=(*input_refs, *evidence_refs),
                uncertainty=uncertainty,
                rejection_reasons=reasons,
                content={
                    "content": content_text,
                    "mode": mode,
                    "confidence": round(confidence, 6),
                    "uncertainty": round(uncertainty, 6),
                    "evidence_refs": list(evidence_refs),
                    "completeness": completeness,
                },
            )
        )

    appraisals: list[Mapping[str, Any]] = []
    appraisal_source = raw.get("appraisal_candidates")
    if appraisal_source is None:
        appraisal_source = raw.get("feeling_hints")
    for index, item in enumerate(_mapping_sequence(appraisal_source, limit=12)):
        reasons: list[str] = []
        if "project.appraisal" not in requested:
            reasons.append("capability_not_requested")
        name = _bounded_text(item.get("name"), 120)
        if not name.strip():
            reasons.append("missing_appraisal_name")
        intensity, intensity_valid = _number(item.get("intensity", 0.0), 0.0, low=0.0, high=1.0)
        valence, valence_valid = _number(item.get("valence", 0.0), 0.0, low=-1.0, high=1.0)
        if not intensity_valid:
            reasons.append("invalid_intensity")
        if not valence_valid:
            reasons.append("invalid_valence")
        source_refs, rejected_refs = _validated_refs(item.get("source_refs"), allowed=allowed_refs)
        if rejected_refs:
            reasons.append("unobserved_source_ref")
        appraisals.append(
            _candidate_envelope(
                kind="appraisal",
                index=index,
                item=item,
                call_id=call_id,
                input_refs=(*input_refs, *source_refs),
                uncertainty=1.0 - intensity,
                rejection_reasons=reasons,
                content={
                    "name": name,
                    "intensity": round(intensity, 6),
                    "valence": round(valence, 6),
                    "rationale": _bounded_text(item.get("rationale"), 1200),
                    "subject_scope": _bounded_text(item.get("subject_scope") or "private", 80),
                    "source_refs": list(source_refs),
                },
            )
        )

    thoughts: list[Mapping[str, Any]] = []
    for index, item in enumerate(_mapping_sequence(raw.get("thought_candidates"), limit=8)):
        reasons: list[str] = []
        if "project.thought" not in requested:
            reasons.append("capability_not_requested")
        content_text = _bounded_text(item.get("content"), 2000)
        if not content_text.strip():
            reasons.append("missing_thought_content")
        uncertainty, uncertainty_valid = _number(item.get("uncertainty", 1.0), 1.0, low=0.0, high=1.0)
        if not uncertainty_valid:
            reasons.append("invalid_uncertainty")
        evidence_refs, rejected_refs = _validated_refs(item.get("evidence_refs"), allowed=allowed_refs)
        if rejected_refs:
            reasons.append("unobserved_evidence_ref")
        thoughts.append(
            _candidate_envelope(
                kind="thought",
                index=index,
                item=item,
                call_id=call_id,
                input_refs=(*input_refs, *evidence_refs),
                uncertainty=uncertainty,
                rejection_reasons=reasons,
                content={
                    "content": content_text,
                    "evidence_refs": list(evidence_refs),
                    "unresolved": list(_text_sequence(item.get("unresolved"), limit=16, text_limit=240)),
                    "uncertainty": round(uncertainty, 6),
                },
            )
        )

    paradigms = _normalise_paradigm_candidates(
        raw,
        frame_view=frame_view,
        call_id=call_id,
        input_refs=input_refs,
        allowed_refs=allowed_refs,
        requested=requested,
    )
    attention = _normalise_attention_candidates(
        raw,
        call_id=call_id,
        input_refs=input_refs,
        allowed_refs=allowed_refs,
        requested=requested,
    )
    expressions = _normalise_expression_candidates(
        raw,
        call_id=call_id,
        input_refs=input_refs,
        allowed_refs=allowed_refs,
        requested=requested,
    )
    parameters = _normalise_parameter_candidates(
        raw,
        frame_view=frame_view,
        call_id=call_id,
        input_refs=input_refs,
        allowed_refs=allowed_refs,
        requested=requested,
    )

    lessons: list[Mapping[str, Any]] = []
    for index, item in enumerate(_mapping_sequence(raw.get("lesson_candidates"), limit=8)):
        reasons: list[str] = []
        capability = _bounded_text(item.get("capability"), 160)
        if not capability.strip():
            reasons.append("missing_lesson_capability")
        elif capability in _TEACHER_CAPABILITIES and capability not in requested:
            reasons.append("capability_not_requested")
        confidence, confidence_valid = _number(item.get("confidence", 0.0), 0.0, low=0.0, high=1.0)
        if not confidence_valid:
            reasons.append("invalid_confidence")
        lessons.append(
            _candidate_envelope(
                kind="lesson",
                index=index,
                item=item,
                call_id=call_id,
                input_refs=input_refs,
                uncertainty=1.0 - confidence,
                rejection_reasons=reasons,
                content={
                    "capability": capability,
                    "trigger_features": _bounded_mapping(item.get("trigger_features"))
                    if isinstance(item.get("trigger_features"), Mapping)
                    else {},
                    "suggested_adjustment": _bounded_mapping(item.get("suggested_adjustment"))
                    if isinstance(item.get("suggested_adjustment"), Mapping)
                    else {"description": _bounded_text(item.get("suggested_adjustment"), 1200)},
                    "counterexamples": list(_text_sequence(item.get("counterexamples"), limit=12, text_limit=480)),
                    "confidence": round(confidence, 6),
                },
            )
        )

    preferences: dict[str, float] = {}
    raw_preferences = raw.get("candidate_preferences")
    if isinstance(raw_preferences, Mapping):
        for key, value in list(raw_preferences.items())[:64]:
            candidate_ref = _bounded_text(key, 160)
            if "project.action_selection" not in requested:
                issues.append({"kind": "action_preference", "ref": candidate_ref, "reason": "capability_not_requested"})
                continue
            if candidate_ref not in action_ids:
                issues.append({"kind": "action_preference", "ref": candidate_ref, "reason": "unknown_action_candidate_ref"})
                continue
            delta, valid = _number(value, 0.0, low=-1.0, high=1.0)
            if not valid:
                issues.append({"kind": "action_preference", "ref": candidate_ref, "reason": "invalid_preference_delta"})
                continue
            preferences[candidate_ref] = round(delta, 6)

    selected = raw.get("selected_candidate_ref")
    selected_ref = _bounded_text(selected, 160) if isinstance(selected, str) and selected.strip() else None
    if selected_ref is not None and "project.action_selection" not in requested:
        issues.append({"kind": "action_selection", "ref": selected_ref, "reason": "capability_not_requested"})
        selected_ref = None
    if selected_ref is not None and selected_ref not in action_ids:
        issues.append({"kind": "action_selection", "ref": selected_ref, "reason": "unknown_action_candidate_ref"})
        selected_ref = None

    for candidate in (
        *recalls,
        *predictions,
        *appraisals,
        *thoughts,
        *lessons,
        *paradigms,
        *attention,
        *expressions,
        *parameters,
    ):
        if candidate.get("validation") == "rejected":
            issues.append(
                {
                    "kind": candidate.get("kind"),
                    "ref": candidate.get("candidate_id"),
                    "reason": ",".join(str(item) for item in candidate.get("rejection_reasons", ())),
                }
            )
    return (
        tuple(recalls),
        tuple(predictions),
        tuple(appraisals),
        tuple(thoughts),
        tuple(lessons),
        paradigms,
        attention,
        expressions,
        parameters,
        preferences,
        selected_ref,
        tuple(issues[:64]),
    )


class GatewayTransportError(RuntimeError):
    """A bounded provider transport failure with no credential echo."""


class ModelTransport(Protocol):
    def request_json(
        self,
        method: str,
        url: str,
        *,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout: float,
        max_response_bytes: int,
    ) -> Mapping[str, Any]:
        ...


class UrllibModelTransport:
    """Small OpenAI-compatible transport; credentials never enter the body."""

    def request_json(
        self,
        method: str,
        url: str,
        *,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout: float,
        max_response_bytes: int,
    ) -> Mapping[str, Any]:
        request = urllib_request.Request(
            url,
            data=json.dumps(dict(body), ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", **dict(headers)},
            method=method.upper(),
        )
        try:
            class NoRedirect(urllib_request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None
            with urllib_request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                raw = response.read(max_response_bytes + 1)
                if len(raw) > max_response_bytes:
                    raise GatewayTransportError("provider_response_exceeds_bound")
                parsed = json.loads(raw.decode("utf-8"))
        except urllib_error.HTTPError as exc:
            # Do not include the response body or URL, which can contain
            # tenant/query data.  The status is enough for a bounded receipt.
            raise GatewayTransportError(f"provider_http_{exc.code}") from exc
        except (urllib_error.URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayTransportError(f"provider_transport_{type(exc).__name__}") from exc
        if not isinstance(parsed, Mapping):
            raise GatewayTransportError("provider_response_not_object")
        return parsed


@dataclass(frozen=True)
class GatewayProposal:
    """A source-tagged, bounded contribution to one cognition frame."""

    capability_key: str
    status: str
    proposition: str | None = None
    feeling_hints: tuple[Mapping[str, Any], ...] = ()
    recall_candidates: tuple[Mapping[str, Any], ...] = ()
    prediction_candidates: tuple[Mapping[str, Any], ...] = ()
    appraisal_candidates: tuple[Mapping[str, Any], ...] = ()
    thought_candidates: tuple[Mapping[str, Any], ...] = ()
    paradigm_candidates: tuple[Mapping[str, Any], ...] = ()
    attention_candidates: tuple[Mapping[str, Any], ...] = ()
    expression_candidates: tuple[Mapping[str, Any], ...] = ()
    parameter_candidates: tuple[Mapping[str, Any], ...] = ()
    requested_capabilities: tuple[str, ...] = ()
    candidate_preferences: Mapping[str, float] = field(default_factory=dict)
    selected_candidate_ref: str | None = None
    reusable_features: tuple[Mapping[str, Any], ...] = ()
    lesson_candidates: tuple[Mapping[str, Any], ...] = ()
    validation_issues: tuple[Mapping[str, Any], ...] = ()
    provider_extensions: Mapping[str, Any] = field(default_factory=dict)
    uncertainty: float = 1.0
    limitations: tuple[str, ...] = ()
    source: str = "llm"
    owner: str = "llm"
    model_receipt_ref: str | None = None
    call_id: str | None = None
    request_key: str | None = None
    advisor_role: str = "cognition"
    provider: str | None = None
    model: str | None = None
    input_refs: tuple[str, ...] = ()
    input_snapshot_hash: str | None = None
    prompt_template_version: str = "apv4.gateway.v4"
    output_hash: str | None = None
    failure: Mapping[str, Any] | None = None
    attempt: int = 1
    latency_ms: float | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_key": self.capability_key,
            "status": self.status,
            "proposition": self.proposition,
            "feeling_hints": [dict(item) for item in self.feeling_hints],
            "recall_candidates": [dict(item) for item in self.recall_candidates],
            "prediction_candidates": [dict(item) for item in self.prediction_candidates],
            "appraisal_candidates": [dict(item) for item in self.appraisal_candidates],
            "thought_candidates": [dict(item) for item in self.thought_candidates],
            "paradigm_candidates": [dict(item) for item in self.paradigm_candidates],
            "attention_candidates": [dict(item) for item in self.attention_candidates],
            "expression_candidates": [dict(item) for item in self.expression_candidates],
            "parameter_candidates": [dict(item) for item in self.parameter_candidates],
            "requested_capabilities": list(self.requested_capabilities),
            "candidate_preferences": dict(self.candidate_preferences),
            "selected_candidate_ref": self.selected_candidate_ref,
            "reusable_features": [dict(item) for item in self.reusable_features],
            "lesson_candidates": [dict(item) for item in self.lesson_candidates],
            "validation_issues": [dict(item) for item in self.validation_issues],
            "provider_extensions": dict(self.provider_extensions),
            "uncertainty": self.uncertainty,
            "limitations": list(self.limitations),
            "source": self.source,
            "owner": self.owner,
            "model_receipt_ref": self.model_receipt_ref,
            "call_id": self.call_id,
            "request_key": self.request_key,
            "advisor_role": self.advisor_role,
            "provider": self.provider,
            "model": self.model,
            "input_refs": list(self.input_refs),
            "input_snapshot_hash": self.input_snapshot_hash,
            "prompt_template_version": self.prompt_template_version,
            "output_hash": self.output_hash,
            "failure": dict(self.failure) if isinstance(self.failure, Mapping) else self.failure,
            "attempt": self.attempt,
            "latency_ms": self.latency_ms,
            "usage": dict(self.usage),
        }

    def without_action_authority(self, limitation: str) -> "GatewayProposal":
        """Strip winner hints when governance has not granted delegation."""

        return replace(
            self,
            candidate_preferences={},
            selected_candidate_ref=None,
            limitations=tuple(dict.fromkeys((*self.limitations, limitation))),
        )


@dataclass(frozen=True)
class GatewayCallReceipt:
    """Durable, credential-free evidence for one bounded advisor call."""

    call_id: str
    request_key: str
    capability_key: str
    advisor_role: str
    provider: str
    model: str
    input_refs: tuple[str, ...]
    input_snapshot_hash: str
    prompt_template_version: str
    status: str
    output_hash: str | None = None
    proposal: Mapping[str, Any] = field(default_factory=dict)
    failure: Mapping[str, Any] | None = None
    attempt: int = 1
    latency_ms: float | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "request_key": self.request_key,
            "capability_key": self.capability_key,
            "advisor_role": self.advisor_role,
            "provider": self.provider,
            "model": self.model,
            "input_refs": list(self.input_refs),
            "input_snapshot_hash": self.input_snapshot_hash,
            "prompt_template_version": self.prompt_template_version,
            "status": self.status,
            "output_hash": self.output_hash,
            "proposal": dict(self.proposal),
            "failure": dict(self.failure) if isinstance(self.failure, Mapping) else self.failure,
            "attempt": self.attempt,
            "latency_ms": self.latency_ms,
            "usage": dict(self.usage),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GatewayCallReceipt":
        if not isinstance(raw, Mapping):
            raise ValueError("gateway_call_receipt_must_be_an_object")
        refs = raw.get("input_refs", ())
        if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence):
            refs = ()
        return cls(
            call_id=_bounded_text(raw.get("call_id"), 160),
            request_key=_bounded_text(raw.get("request_key"), 160),
            capability_key=_bounded_text(raw.get("capability_key"), 160),
            advisor_role=_bounded_text(raw.get("advisor_role") or "cognition", 80),
            provider=_bounded_text(raw.get("provider") or "unknown", 120),
            model=_bounded_text(raw.get("model") or "unknown", 160),
            input_refs=tuple(_bounded_text(item, 160) for item in list(refs)[:128] if isinstance(item, str)),
            input_snapshot_hash=_bounded_text(raw.get("input_snapshot_hash"), 128),
            prompt_template_version=_bounded_text(raw.get("prompt_template_version") or "unknown", 120),
            status=_bounded_text(raw.get("status") or "partial", 40),
            output_hash=_bounded_text(raw.get("output_hash"), 128) if raw.get("output_hash") else None,
            proposal=_bounded_mapping(raw.get("proposal"), max_depth=6)
            if isinstance(raw.get("proposal"), Mapping)
            else {},
            failure=_bounded_mapping(raw.get("failure")) if isinstance(raw.get("failure"), Mapping) else None,
            attempt=int(raw.get("attempt") or 1),
            latency_ms=float(raw.get("latency_ms")) if isinstance(raw.get("latency_ms"), (int, float)) else None,
            usage=_bounded_mapping(raw.get("usage")) if isinstance(raw.get("usage"), Mapping) else {},
            created_at=_bounded_text(raw.get("created_at") or utc_now(), 80),
        )

    def to_proposal(self) -> "GatewayProposal | None":
        raw = self.proposal
        if not isinstance(raw, Mapping) or not raw:
            return None
        try:
            return GatewayProposal(
                capability_key=_bounded_text(raw.get("capability_key") or self.capability_key, 160),
                status=_bounded_text(raw.get("status") or self.status, 40),
                proposition=_bounded_text(raw.get("proposition"), 4096) if raw.get("proposition") is not None else None,
                feeling_hints=tuple(item for item in raw.get("feeling_hints", ()) if isinstance(item, Mapping))[:16],
                recall_candidates=tuple(item for item in raw.get("recall_candidates", ()) if isinstance(item, Mapping))[:8],
                prediction_candidates=tuple(item for item in raw.get("prediction_candidates", ()) if isinstance(item, Mapping))[:8],
                appraisal_candidates=tuple(item for item in raw.get("appraisal_candidates", ()) if isinstance(item, Mapping))[:12],
                thought_candidates=tuple(item for item in raw.get("thought_candidates", ()) if isinstance(item, Mapping))[:8],
                paradigm_candidates=tuple(item for item in raw.get("paradigm_candidates", ()) if isinstance(item, Mapping))[:6],
                attention_candidates=tuple(item for item in raw.get("attention_candidates", ()) if isinstance(item, Mapping))[:6],
                expression_candidates=tuple(item for item in raw.get("expression_candidates", ()) if isinstance(item, Mapping))[:6],
                parameter_candidates=tuple(item for item in raw.get("parameter_candidates", ()) if isinstance(item, Mapping))[:6],
                requested_capabilities=tuple(
                    item for item in raw.get("requested_capabilities", ())
                    if isinstance(item, str) and item in _TEACHER_CAPABILITIES
                )[:32],
                candidate_preferences={
                    str(key): max(-1.0, min(1.0, float(value)))
                    for key, value in (raw.get("candidate_preferences", {}) or {}).items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                } if isinstance(raw.get("candidate_preferences"), Mapping) else {},
                selected_candidate_ref=raw.get("selected_candidate_ref") if isinstance(raw.get("selected_candidate_ref"), str) else None,
                reusable_features=tuple(item for item in raw.get("reusable_features", ()) if isinstance(item, Mapping))[:32],
                lesson_candidates=tuple(item for item in raw.get("lesson_candidates", ()) if isinstance(item, Mapping))[:8],
                validation_issues=tuple(item for item in raw.get("validation_issues", ()) if isinstance(item, Mapping))[:64],
                provider_extensions=_bounded_mapping(raw.get("provider_extensions"), max_depth=4)
                if isinstance(raw.get("provider_extensions"), Mapping)
                else {},
                uncertainty=float(raw.get("uncertainty", 1.0)),
                limitations=tuple(item for item in raw.get("limitations", ()) if isinstance(item, str))[:32],
                source=_bounded_text(raw.get("source") or "llm_model", 80),
                owner=_bounded_text(raw.get("owner") or "llm", 80),
                model_receipt_ref=self.call_id,
                call_id=self.call_id,
                request_key=self.request_key,
                advisor_role=self.advisor_role,
                provider=self.provider,
                model=self.model,
                input_refs=self.input_refs,
                input_snapshot_hash=self.input_snapshot_hash,
                prompt_template_version=self.prompt_template_version,
                output_hash=self.output_hash,
                failure=self.failure,
                attempt=self.attempt,
                latency_ms=self.latency_ms,
                usage=self.usage,
            )
        except (TypeError, ValueError):
            return None

    @classmethod
    def from_proposal(
        cls,
        proposal: GatewayProposal,
        *,
        capability_key: str,
        input_refs: Sequence[str],
    ) -> "GatewayCallReceipt | None":
        if not proposal.call_id or not proposal.request_key:
            return None
        return cls(
            call_id=proposal.call_id,
            request_key=proposal.request_key,
            capability_key=capability_key,
            advisor_role=proposal.advisor_role,
            provider=proposal.provider or "unknown",
            model=proposal.model or "unknown",
            input_refs=tuple(dict.fromkeys((*input_refs, *proposal.input_refs))),
            input_snapshot_hash=proposal.input_snapshot_hash or "",
            prompt_template_version=proposal.prompt_template_version,
            status=proposal.status,
            output_hash=proposal.output_hash,
            proposal=proposal.to_dict(),
            failure=proposal.failure,
            attempt=proposal.attempt,
            latency_ms=proposal.latency_ms,
            usage=proposal.usage,
        )


class OpenAICompatibleGateway:
    """Bounded JSON proposal adapter for any OpenAI-compatible endpoint.

    The endpoint is a replaceable organ, not a second mind.  The adapter
    returns only a ``GatewayProposal``; the runtime still owns persistence,
    candidate eligibility, the single decision slot, dispatch and readback.
    """

    PROMPT_TEMPLATE_VERSION = "apv4.gateway.v4"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        provider: str = "openai-compatible",
        advisor_role: str = "cognition",
        timeout: float = 20.0,
        max_response_bytes: int = 2_000_000,
        max_retries: int = 1,
        max_prompt_chars: int = 48_000,
        max_output_tokens: int = 1_200,
        temperature: float = 0.2,
        transport: ModelTransport | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("gateway_base_url_required")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("gateway_api_key_required")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("gateway_model_required")
        if timeout <= 0 or timeout > 120:
            raise ValueError("gateway_timeout_out_of_bounds")
        if max_response_bytes < 1024 or max_response_bytes > 16_000_000:
            raise ValueError("gateway_response_bound_out_of_bounds")
        if isinstance(max_retries, bool) or max_retries < 0 or max_retries > 3:
            raise ValueError("gateway_retry_bound_out_of_bounds")
        if max_prompt_chars < 1024 or max_prompt_chars > 200_000:
            raise ValueError("gateway_prompt_bound_out_of_bounds")
        if isinstance(max_output_tokens, bool) or not 128 <= int(max_output_tokens) <= 8_192:
            raise ValueError("gateway_output_token_bound_out_of_bounds")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.provider = provider
        self.advisor_role = advisor_role
        self.timeout = float(timeout)
        self.max_response_bytes = int(max_response_bytes)
        self.max_retries = int(max_retries)
        self.max_prompt_chars = int(max_prompt_chars)
        self.max_output_tokens = int(max_output_tokens)
        self.temperature = max(0.0, min(2.0, float(temperature)))
        self.transport = transport or UrllibModelTransport()
        self._cache: dict[str, GatewayProposal] = {}

    def request_identity(self, frame_view: Mapping[str, Any], delegation: DelegationDecision | None) -> tuple[str, str, tuple[str, ...]]:
        bounded = _bounded_mapping(frame_view, max_depth=6)
        refs: list[str] = []
        for key in ("event_ref", "event_id", "trigger_ref"):
            value = bounded.get(key) if isinstance(bounded, Mapping) else None
            if isinstance(value, str) and value:
                refs.append(value)
        sa = bounded.get("sa") if isinstance(bounded, Mapping) else None
        if isinstance(sa, Mapping) and isinstance(sa.get("event_ref"), str):
            refs.append(sa["event_ref"])
        if delegation is not None:
            refs.extend(delegation.input_refs)
        snapshot_hash = _canonical_hash({
            "frame_view": bounded,
            "delegation": delegation.to_dict() if delegation is not None else None,
            "template": self.PROMPT_TEMPLATE_VERSION,
            "model": self.model,
        })
        request_key = _canonical_hash({"provider": self.provider, "model": self.model, "snapshot": snapshot_hash})
        return request_key, snapshot_hash, tuple(dict.fromkeys(refs))

    def _endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    def _build_messages(self, frame_view: Mapping[str, Any], delegation: DelegationDecision | None) -> list[dict[str, str]]:
        bounded = _bounded_mapping(frame_view, max_depth=6)
        _, requested_capabilities, requested_explicit = _requested_capability_inventory(frame_view)
        instruction = (
            "你是 APV4 Hybrid 联合心智中的结构化教师。只返回一个 JSON 对象，不要 markdown 或解释。"
            "你可以帮助冷启动 AP 提出来源化候选，但不能声称未观察到的事实、readback、权限或执行成功。"
            "必须使用这些顶层字段：status, recall_candidates, prediction_candidates, appraisal_candidates, "
            "thought_candidates, paradigm_candidates, attention_candidates, expression_candidates, parameter_candidates, "
            "candidate_preferences, selected_candidate_ref, lesson_candidates, uncertainty, limitations。"
            "recall_candidates 每项字段 memory_ref,summary,relevance,rationale，memory_ref 只能引用输入 b_recall；"
            "prediction_candidates 每项字段 content,confidence,uncertainty,evidence_refs,completeness；"
            "appraisal_candidates 每项字段 name,intensity,valence,rationale,subject_scope,source_refs；"
            "thought_candidates 每项字段 content,evidence_refs,uncertainty,unresolved；"
            "paradigm_candidates 每项只能使用 pattern_kind,invariants,slots,relations,confidence,uncertainty,evidence_refs,completeness,counterexamples；"
            "invariants 只能复述输入 activity_profile 已有结构轴；slot source 只能引用输入 activity/proposition/recall 的明确字段；"
            "attention_candidates 每项使用 mode,target_ref,gain_delta,rationale,source_refs,uncertainty，target_ref 必须来自输入；"
            "注意建议只是有界 gain，不能指定 winner 或直接安装焦点；"
            "expression_candidates 每项使用 template,tone,evidence_refs,uncertainty,counterexamples；"
            "template 必须且只能有一个 {claim}，其余文字只能是纯语用前缀或标点，不得新增事实、承诺或执行声明；"
            "parameter_candidates 每项只能使用 parameter,delta,rationale,source_refs,uncertainty,counterexamples,scope,expected_direction；"
            "parameter 只能是输入协议允许的六个 attention 权重，delta 必须在 -0.05 到 0.05 且非零，scope 只能复述 activity_profile，"
            "expected_direction 只能是 increase/decrease 且与 delta 同向；参数建议不能指定焦点、winner、答案、任务/语言/event 路由；"
            "candidate_preferences 只能使用输入 action_candidates 中已有 candidate_id，delta 范围 -1 到 1；"
            "lesson_candidates 每项字段 capability,trigger_features,suggested_adjustment,counterexamples,confidence。"
            f"本次只请求这些能力：{json.dumps(list(requested_capabilities), ensure_ascii=False)}。"
            + ("未请求能力必须返回空数组/空对象，不得自行扩权。" if requested_explicit else "") +
            "所有 evidence/source 引用只能来自输入，课程只是 shadow 提议，同轮不代表学会。"
            "不确定时使用 status=partial、completeness=unknown/search_incomplete 或 limitations。"
            "为兼容旧客户端可选返回 proposition,feeling_hints,reusable_features，但它们不替代六类字段。"
        )
        context = json.dumps(
            {"frame_view": bounded, "delegation": delegation.to_dict() if delegation is not None else None},
            ensure_ascii=False,
            sort_keys=True,
        )
        if len(context) > self.max_prompt_chars:
            context = context[: self.max_prompt_chars] + "…"
        return [
            {"role": "system", "content": instruction},
            {"role": "user", "content": context},
        ]

    @staticmethod
    def _content(response: Mapping[str, Any]) -> str | None:
        choices = response.get("choices")
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, Mapping):
            return None
        message = first.get("message")
        if not isinstance(message, Mapping):
            return None
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            parts = []
            for item in content:
                if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts) if parts else None
        return None

    def _proposal_from_object(
        self,
        raw: Mapping[str, Any],
        *,
        call_id: str,
        request_key: str,
        snapshot_hash: str,
        refs: tuple[str, ...],
        frame_view: Mapping[str, Any],
        latency_ms: float,
        attempt: int,
        usage: Mapping[str, Any],
    ) -> GatewayProposal:
        (
            recall_candidates,
            prediction_candidates,
            appraisal_candidates,
            thought_candidates,
            lesson_candidates,
            paradigm_candidates,
            attention_candidates,
            expression_candidates,
            parameter_candidates,
            preferences,
            selected_ref,
            validation_issues,
        ) = _normalise_teacher_candidates(
            raw,
            frame_view=frame_view,
            call_id=call_id,
            input_refs=refs,
        )
        hints = raw.get("feeling_hints")
        feeling_hints = tuple(
            _bounded_mapping(item)
            for item in (hints if isinstance(hints, Sequence) and not isinstance(hints, (str, bytes)) else ())
            if isinstance(item, Mapping)
        )[:16]
        features = raw.get("reusable_features")
        reusable = tuple(
            _bounded_mapping(item)
            for item in (features if isinstance(features, Sequence) and not isinstance(features, (str, bytes)) else ())
            if isinstance(item, Mapping)
        )[:32]
        limitations = tuple(
            _bounded_text(item, 240)
            for item in (raw.get("limitations") if isinstance(raw.get("limitations"), Sequence) and not isinstance(raw.get("limitations"), (str, bytes)) else ())
            if isinstance(item, str)
        )[:32]
        status = _bounded_text(raw.get("status") or "proposal", 40)
        if status not in {"proposal", "partial", "unavailable"}:
            status = "partial"
            limitations = (*limitations, "unsupported_provider_status")
        uncertainty = raw.get("uncertainty", 0.8)
        if not isinstance(uncertainty, (int, float)) or isinstance(uncertainty, bool):
            uncertainty = 1.0
            limitations = (*limitations, "invalid_uncertainty")
        raw_failure = raw.get("failure")
        failure = _bounded_mapping(raw_failure) if isinstance(raw_failure, Mapping) else None
        return GatewayProposal(
            capability_key="hybrid.cognition",
            status=status,
            proposition=_bounded_text(raw.get("proposition"), 4096) if raw.get("proposition") is not None else None,
            feeling_hints=feeling_hints,
            recall_candidates=recall_candidates,
            prediction_candidates=prediction_candidates,
            appraisal_candidates=appraisal_candidates,
            thought_candidates=thought_candidates,
            paradigm_candidates=paradigm_candidates,
            attention_candidates=attention_candidates,
            expression_candidates=expression_candidates,
            parameter_candidates=parameter_candidates,
            requested_capabilities=_requested_capability_inventory(frame_view)[1],
            candidate_preferences=preferences,
            selected_candidate_ref=selected_ref,
            reusable_features=reusable,
            lesson_candidates=lesson_candidates,
            validation_issues=validation_issues,
            provider_extensions=_bounded_mapping(
                {
                    str(key): value
                    for key, value in raw.items()
                    if str(key)
                    not in {
                        "status",
                        "proposition",
                        "feeling_hints",
                        "recall_candidates",
                        "prediction_candidates",
                        "appraisal_candidates",
                        "thought_candidates",
                        "paradigm_candidates",
                        "attention_candidates",
                        "expression_candidates",
                        "parameter_candidates",
                        "requested_capabilities",
                        "candidate_preferences",
                        "selected_candidate_ref",
                        "reusable_features",
                        "lesson_candidates",
                        "uncertainty",
                        "limitations",
                        "failure",
                    }
                },
                max_depth=4,
            ),
            uncertainty=round(_clamp(float(uncertainty)), 6),
            limitations=limitations,
            source="llm_model",
            owner="llm",
            model_receipt_ref=call_id,
            call_id=call_id,
            request_key=request_key,
            advisor_role=self.advisor_role,
            provider=self.provider,
            model=self.model,
            input_refs=refs,
            input_snapshot_hash=snapshot_hash,
            prompt_template_version=self.PROMPT_TEMPLATE_VERSION,
            output_hash=_canonical_hash(raw),
            failure=failure,
            attempt=attempt,
            latency_ms=round(latency_ms, 3),
            usage=_bounded_mapping(usage),
        )

    def propose(
        self,
        frame_view: Mapping[str, Any],
        delegation: DelegationDecision | None,
    ) -> GatewayProposal:
        request_key, snapshot_hash, refs = self.request_identity(frame_view, delegation)
        cached = self._cache.get(request_key)
        if cached is not None:
            return replace(cached, limitations=tuple(dict.fromkeys((*cached.limitations, "in_process_cache"))))
        call_id = f"llmcall_{request_key[:32]}"
        messages = self._build_messages(frame_view, delegation)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Idempotency-Key": call_id,
            "X-APV4-Call-Id": call_id,
        }
        last_error: str | None = None
        started = time.monotonic()
        for attempt in range(1, self.max_retries + 2):
            try:
                response = self.transport.request_json(
                    "POST",
                    self._endpoint(),
                    body=body,
                    headers=headers,
                    timeout=self.timeout,
                    max_response_bytes=self.max_response_bytes,
                )
                elapsed = (time.monotonic() - started) * 1000.0
                content = self._content(response)
                usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
                raw = _extract_json_object(content or "")
                if raw is None:
                    proposal = GatewayProposal(
                        capability_key="hybrid.cognition",
                        status="partial",
                        uncertainty=1.0,
                        limitations=("proposal_incomplete:response_not_json_object",),
                        source="llm_model",
                        owner="llm",
                        model_receipt_ref=call_id,
                        call_id=call_id,
                        request_key=request_key,
                        advisor_role=self.advisor_role,
                        provider=self.provider,
                        model=self.model,
                        input_refs=refs,
                        input_snapshot_hash=snapshot_hash,
                        prompt_template_version=self.PROMPT_TEMPLATE_VERSION,
                        failure={"code": "proposal_incomplete"},
                        attempt=attempt,
                        latency_ms=round(elapsed, 3),
                        usage=usage,
                    )
                else:
                    proposal = self._proposal_from_object(
                        raw,
                        call_id=call_id,
                        request_key=request_key,
                        snapshot_hash=snapshot_hash,
                        refs=refs,
                        frame_view=frame_view,
                        latency_ms=elapsed,
                        attempt=attempt,
                        usage=usage,
                    )
                self._cache[request_key] = proposal
                return proposal
            except GatewayTransportError as exc:
                last_error = str(exc)
                if attempt > self.max_retries:
                    break
        elapsed = (time.monotonic() - started) * 1000.0
        return GatewayProposal(
            capability_key="hybrid.cognition",
            status="unavailable",
            uncertainty=1.0,
            limitations=("provider_unavailable",),
            source="llm_model",
            owner="llm",
            model_receipt_ref=call_id,
            call_id=call_id,
            request_key=request_key,
            advisor_role=self.advisor_role,
            provider=self.provider,
            model=self.model,
            input_refs=refs,
            input_snapshot_hash=snapshot_hash,
            prompt_template_version=self.PROMPT_TEMPLATE_VERSION,
            failure={"code": last_error or "provider_unavailable", "retryable": True},
            attempt=self.max_retries + 1,
            latency_ms=round(elapsed, 3),
        )


class HybridGateway(Protocol):
    """Minimal capability-level gateway contract."""

    def propose(
        self,
        frame_view: Mapping[str, Any],
        delegation: DelegationDecision | None,
    ) -> GatewayProposal:
        """Return one bounded proposal; errors are represented as status."""


class NullGateway:
    """Provider-free gateway used when no model is configured or available."""

    def __init__(
        self,
        *,
        limitation: str = "no_provider_configured",
        source: str = "llm",
    ) -> None:
        self.limitation = _bounded_text(limitation, 160)
        self.source = _bounded_text(source, 80)

    def propose(
        self,
        frame_view: Mapping[str, Any],
        delegation: DelegationDecision | None,
    ) -> GatewayProposal:
        return GatewayProposal(
            capability_key="hybrid.cognition",
            status="unavailable",
            uncertainty=1.0,
            limitations=(self.limitation,),
            source=self.source,
            owner="llm",
        )


class CallableGateway:
    """Adapter for a caller-supplied structured proposal function.

    This is useful for an offline fixture and for a future provider adapter.
    The callable receives only the bounded frame view supplied by the runtime;
    it cannot reach the store or environment through this interface.
    """

    def __init__(
        self,
        callback: Callable[[Mapping[str, Any], DelegationDecision | None], GatewayProposal],
        *,
        name: str = "callable",
    ) -> None:
        self.callback = callback
        self.name = name

    def propose(
        self,
        frame_view: Mapping[str, Any],
        delegation: DelegationDecision | None,
    ) -> GatewayProposal:
        try:
            proposal = self.callback(frame_view, delegation)
        except Exception as exc:  # provider faults remain bounded and visible
            return GatewayProposal(
                capability_key="hybrid.cognition",
                status="partial",
                uncertainty=1.0,
                limitations=(f"gateway_error:{type(exc).__name__}",),
                source="llm",
                owner="llm",
            )
        if not isinstance(proposal, GatewayProposal):
            return GatewayProposal(
                capability_key="hybrid.cognition",
                status="partial",
                uncertainty=1.0,
                limitations=("gateway_returned_invalid_proposal",),
                source="llm",
                owner="llm",
            )
        return proposal


class OfflineTeacherGateway:
    """A tiny deterministic teacher fixture, never presented as an LLM result.

    It mirrors the shape of a model response so the runtime can exercise
    source/ownership boundaries without network calls.  It only proposes a
    source-grounded proposition and reusable feature; the runtime still owns
    truth, selection, dispatch and learning.
    """

    def propose(
        self,
        frame_view: Mapping[str, Any],
        delegation: DelegationDecision | None,
    ) -> GatewayProposal:
        sa = frame_view.get("sa") if isinstance(frame_view, Mapping) else None
        text = ""
        if isinstance(sa, Mapping):
            raw = sa.get("text")
            if isinstance(raw, str):
                text = raw.strip()
        proposition = f"已收到一个可追溯事件：{text}" if text else "已收到一个待解释事件"
        status = "proposal"
        limitations = ("offline_fixture_not_external_evidence",)
        if delegation is not None and not delegation.enabled:
            limitations = (*limitations, "delegation_disabled")
        return GatewayProposal(
            capability_key="hybrid.cognition",
            status=status,
            proposition=proposition,
            reusable_features=({"feature": "source_grounded_event", "value": True},),
            uncertainty=0.45,
            limitations=limitations,
            source="llm_fixture",
            owner="llm",
            model_receipt_ref="offline-teacher-fixture",
        )


__all__ = [
    "GatewayProposal",
    "GatewayCallReceipt",
    "GatewayTransportError",
    "ModelTransport",
    "UrllibModelTransport",
    "OpenAICompatibleGateway",
    "HybridGateway",
    "NullGateway",
    "CallableGateway",
    "OfflineTeacherGateway",
]
