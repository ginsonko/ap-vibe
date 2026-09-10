"""Small, versioned contracts shared by the AP Mind runtime.

Wave 0 deliberately keeps this module dependency-free.  The contracts are
boring on purpose: they carry provenance and boundaries, but they do not try
to be a runtime, a planner, or a hidden answer table.  Future runtime layers
may replace the storage and transport while retaining these wire semantics.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
import json
import re
from typing import Any, ClassVar, Mapping, TypeVar
from uuid import uuid4


SCHEMA_VERSION = "0.1.0"

SOURCE_VALUES = frozenset(
    {
        "external",
        "body",
        "memory",
        "prediction",
        "imagination",
        "llm",
        "action",
        "readback",
        "user_feedback",
        "system",
        # Internal cognition is still an ordinary, source-tagged occurrence.
        # It is not an external fact and must retain its own lineage when it
        # re-enters the single AP event stream.
        "internal",
    }
)
ROLE_VALUES = frozenset({"observation", "proposal", "command", "result", "teaching"})
COMPLETENESS_VALUES = frozenset(
    {"complete", "partial", "unknown", "search_incomplete"}
)
OWNER_VALUES = frozenset({"ap_native", "llm", "user", "environment", "mixed", "unknown"})
MATURITY_VALUES = frozenset(
    {"llm_substituted", "assisted", "local_primary", "audit_only", "reteach"}
)
DELEGATION_MODE_VALUES = frozenset({"none", "substituted", "assisted", "audit_only"})
RISK_VALUES = frozenset({"low", "medium", "high"})
SLOT_STATUS_VALUES = frozenset({"open", "selected", "deferred", "abstained", "expired"})
RECEIPT_STATUS_VALUES = frozenset(
    {"attempted", "accepted", "rejected", "duplicate", "unknown"}
)
RESULT_STATUS_VALUES = frozenset(
    {"success", "partial", "failed", "unknown", "search_incomplete", "deferred"}
)

# These are reality/permission boundaries, not semantic quality gates.  A
# delegated model may select a low-risk semantic candidate, but it cannot turn
# an unobserved result into a fact or acquire an execution capability.
IMMUTABLE_LLM_BOUNDARIES = (
    "physical_readback",
    "independent_external_evidence",
    "permission",
    "root_identity",
)
FORBIDDEN_LLM_AUTHORITIES = frozenset(
    {
        "physical_readback",
        "independent_external_evidence",
        "permission",
        "root_identity",
        "direct_execution",
        "truth_write",
    }
)


class ContractError(ValueError):
    """Raised when a wire contract would be ambiguous or unsafe to consume."""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> str:
    """Return a portable UTC timestamp for a new contract."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _copy_json(value: Any) -> Any:
    """Copy and validate JSON-compatible values without retaining caller state."""

    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"value_is_not_json_compatible: {exc}") from exc


def _text(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{name}_must_be_text")
    if not allow_empty and not value.strip():
        raise ContractError(f"{name}_must_not_be_empty")
    return value


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _enum(value: Any, name: str, allowed: frozenset[str]) -> str:
    value = _text(value, name)
    if value not in allowed:
        allowed_text = ",".join(sorted(allowed))
        raise ContractError(f"{name}_unsupported:{value};allowed={allowed_text}")
    return value


def _tuple_texts(value: Any, name: str, *, max_items: int = 256) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ContractError(f"{name}_must_be_a_sequence")
    if len(value) > max_items:
        raise ContractError(f"{name}_exceeds_bound:{max_items}")
    return tuple(_text(item, f"{name}_item") for item in value)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContractError(f"{name}_must_be_an_object")
    copied = _copy_json(dict(value))
    if not isinstance(copied, dict):  # pragma: no cover - guarded by JSON
        raise ContractError(f"{name}_must_be_an_object")
    return copied


def _nonnegative_int(value: Any, name: str, *, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{name}_must_be_a_nonnegative_integer")
    return value


def _nonnegative_number(value: Any, name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ContractError(f"{name}_must_be_nonnegative")
    return value


def _split_input(raw: Mapping[str, Any], known: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split known wire fields from forward-compatible unknown fields."""

    values = {key: raw[key] for key in known if key in raw}
    unknown = {key: _copy_json(value) for key, value in raw.items() if key not in known}
    explicit_extra = raw.get("extra")
    if explicit_extra is not None:
        if not isinstance(explicit_extra, Mapping):
            raise ContractError("extra_must_be_an_object")
        for key, value in explicit_extra.items():
            unknown.setdefault(str(key), _copy_json(value))
    return values, unknown


def _merge_output(values: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    output = {key: _copy_json(value) for key, value in values.items()}
    for key, value in _mapping(extra, "extra").items():
        # A future producer cannot shadow a known field.  This prevents an
        # unknown extension from silently changing the meaning of a contract.
        if key not in output:
            output[key] = _copy_json(value)
    return output


def _json_dump(contract: Any) -> str:
    if not hasattr(contract, "to_dict"):
        raise ContractError("object_is_not_a_contract")
    return json.dumps(contract.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


T = TypeVar("T")


def _known_fields(cls: type[Any]) -> set[str]:
    return {item.name for item in fields(cls) if item.name != "extra"}


@dataclass(frozen=True)
class EventEnvelope:
    """One source-tagged occurrence entering the single AP event stream."""

    schema_version: str = SCHEMA_VERSION
    event_id: str = field(default_factory=lambda: _new_id("evt"))
    runtime_id: str = ""
    organism_id: str = ""
    environment_id: str = ""
    subject_scope: str = "private"
    episode_id: str = ""
    source: str = "external"
    role: str = "observation"
    modality: str = "text"
    occurred_at: str = field(default_factory=utc_now)
    observed_at: str = field(default_factory=utc_now)
    payload_ref: str | None = None
    payload_inline: Any | None = None
    evidence_refs: tuple[str, ...] = ()
    lineage_refs: tuple[str, ...] = ()
    privacy_scope: str = "private"
    completeness: str = "complete"
    idempotency_key: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict, compare=True)

    def __post_init__(self) -> None:
        _text(self.schema_version, "schema_version")
        for name in ("event_id", "runtime_id", "organism_id", "environment_id", "episode_id"):
            _text(getattr(self, name), name)
        _enum(self.source, "source", SOURCE_VALUES)
        _enum(self.role, "role", ROLE_VALUES)
        _text(self.modality, "modality")
        _text(self.subject_scope, "subject_scope")
        _text(self.privacy_scope, "privacy_scope")
        _enum(self.completeness, "completeness", COMPLETENESS_VALUES)
        _text(self.occurred_at, "occurred_at")
        _text(self.observed_at, "observed_at")
        _optional_text(self.payload_ref, "payload_ref")
        if self.payload_ref is None and self.payload_inline is None:
            raise ContractError("event_requires_payload_ref_or_payload_inline")
        if self.payload_inline is not None:
            _copy_json(self.payload_inline)
        _tuple_texts(self.evidence_refs, "evidence_refs")
        _tuple_texts(self.lineage_refs, "lineage_refs")
        _optional_text(self.idempotency_key, "idempotency_key")
        _mapping(self.extra, "extra")

    def to_dict(self) -> dict[str, Any]:
        return _merge_output(
            {
                "schema_version": self.schema_version,
                "event_id": self.event_id,
                "runtime_id": self.runtime_id,
                "organism_id": self.organism_id,
                "environment_id": self.environment_id,
                "subject_scope": self.subject_scope,
                "episode_id": self.episode_id,
                "source": self.source,
                "role": self.role,
                "modality": self.modality,
                "occurred_at": self.occurred_at,
                "observed_at": self.observed_at,
                "payload_ref": self.payload_ref,
                "payload_inline": self.payload_inline,
                "evidence_refs": list(self.evidence_refs),
                "lineage_refs": list(self.lineage_refs),
                "privacy_scope": self.privacy_scope,
                "completeness": self.completeness,
                "idempotency_key": self.idempotency_key,
            },
            self.extra,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EventEnvelope":
        if not isinstance(raw, Mapping):
            raise ContractError("event_must_be_an_object")
        known, extra = _split_input(raw, _known_fields(cls))
        for name in ("evidence_refs", "lineage_refs"):
            if name in known:
                known[name] = _tuple_texts(known[name], name)
        known["extra"] = extra
        return cls(**known)

    def json(self) -> str:
        return _json_dump(self)


@dataclass(frozen=True)
class CapabilityOwnership:
    """Per-capability ownership snapshot used by the hybrid gateway."""

    schema_version: str = SCHEMA_VERSION
    capability_key: str = ""
    stage: str = "llm_substituted"
    decision_owner: str = "llm"
    content_owner: str = "llm"
    evidence_owner: str = "environment"
    execution_owner: str = "environment"
    scope: tuple[str, ...] = ()
    llm_allowed: bool = True
    reason: str = ""
    version: str = "0.1.0"
    extra: Mapping[str, Any] = field(default_factory=dict, compare=True)

    def __post_init__(self) -> None:
        _text(self.schema_version, "schema_version")
        _text(self.capability_key, "capability_key")
        _enum(self.stage, "stage", MATURITY_VALUES)
        for name in ("decision_owner", "content_owner", "evidence_owner", "execution_owner"):
            owner = _enum(getattr(self, name), name, OWNER_VALUES)
            if name == "execution_owner" and owner == "llm":
                raise ContractError("llm_cannot_own_execution")
            if name == "evidence_owner" and owner == "llm":
                raise ContractError("llm_cannot_own_independent_evidence")
        _tuple_texts(self.scope, "scope")
        if self.stage in {"llm_substituted", "assisted"} and not self.llm_allowed:
            raise ContractError("llm_stage_requires_llm_allowed")
        _text(self.reason, "reason", allow_empty=True)
        _text(self.version, "version")
        _mapping(self.extra, "extra")

    def to_dict(self) -> dict[str, Any]:
        return _merge_output(
            {
                "schema_version": self.schema_version,
                "capability_key": self.capability_key,
                "stage": self.stage,
                "decision_owner": self.decision_owner,
                "content_owner": self.content_owner,
                "evidence_owner": self.evidence_owner,
                "execution_owner": self.execution_owner,
                "scope": list(self.scope),
                "llm_allowed": self.llm_allowed,
                "reason": self.reason,
                "version": self.version,
            },
            self.extra,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapabilityOwnership":
        if not isinstance(raw, Mapping):
            raise ContractError("capability_ownership_must_be_an_object")
        known, extra = _split_input(raw, _known_fields(cls))
        if "scope" in known:
            known["scope"] = _tuple_texts(known["scope"], "scope")
        known["extra"] = extra
        return cls(**known)

    def json(self) -> str:
        return _json_dump(self)


@dataclass(frozen=True)
class DelegationDecision:
    """A bounded, explicit decision to let an LLM act for one capability."""

    schema_version: str = SCHEMA_VERSION
    delegation_id: str = field(default_factory=lambda: _new_id("dlg"))
    capability_key: str = ""
    mode: str = "none"
    enabled: bool = False
    risk_level: str = "low"
    scope: tuple[str, ...] = ()
    input_refs: tuple[str, ...] = ()
    candidate_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    allowed_decisions: tuple[str, ...] = ()
    immutable_boundaries: tuple[str, ...] = IMMUTABLE_LLM_BOUNDARIES
    budget: Mapping[str, int | float] = field(default_factory=dict)
    expires_at_tick: int | None = None
    fallback: str = "defer"
    revocable_by: tuple[str, ...] = ("user", "policy")
    requested_by: str = "runtime"
    rationale: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict, compare=True)

    def __post_init__(self) -> None:
        _text(self.schema_version, "schema_version")
        _text(self.delegation_id, "delegation_id")
        _text(self.capability_key, "capability_key")
        _enum(self.mode, "mode", DELEGATION_MODE_VALUES)
        _enum(self.risk_level, "risk_level", RISK_VALUES)
        for name in (
            "scope",
            "input_refs",
            "candidate_refs",
            "evidence_refs",
            "revocable_by",
        ):
            _tuple_texts(getattr(self, name), name)
        decisions = _tuple_texts(self.allowed_decisions, "allowed_decisions")
        forbidden = FORBIDDEN_LLM_AUTHORITIES.intersection(decisions)
        if forbidden:
            raise ContractError(
                "delegation_contains_forbidden_authority:" + ",".join(sorted(forbidden))
            )
        boundaries = _tuple_texts(self.immutable_boundaries, "immutable_boundaries")
        missing = set(IMMUTABLE_LLM_BOUNDARIES).difference(boundaries)
        if missing:
            raise ContractError(
                "delegation_must_preserve_boundaries:" + ",".join(sorted(missing))
            )
        if self.enabled and self.mode == "none":
            raise ContractError("enabled_delegation_requires_non_none_mode")
        if not self.enabled and self.mode != "none":
            raise ContractError("disabled_delegation_requires_none_mode")
        _mapping(self.budget, "budget")
        for key, value in self.budget.items():
            _text(key, "budget_key")
            _nonnegative_number(value, f"budget.{key}")
        _nonnegative_int(self.expires_at_tick, "expires_at_tick", allow_none=True)
        _text(self.fallback, "fallback")
        _text(self.requested_by, "requested_by")
        _text(self.rationale, "rationale", allow_empty=True)
        _mapping(self.extra, "extra")

    def to_dict(self) -> dict[str, Any]:
        return _merge_output(
            {
                "schema_version": self.schema_version,
                "delegation_id": self.delegation_id,
                "capability_key": self.capability_key,
                "mode": self.mode,
                "enabled": self.enabled,
                "risk_level": self.risk_level,
                "scope": list(self.scope),
                "input_refs": list(self.input_refs),
                "candidate_refs": list(self.candidate_refs),
                "evidence_refs": list(self.evidence_refs),
                "allowed_decisions": list(self.allowed_decisions),
                "immutable_boundaries": list(self.immutable_boundaries),
                "budget": dict(self.budget),
                "expires_at_tick": self.expires_at_tick,
                "fallback": self.fallback,
                "revocable_by": list(self.revocable_by),
                "requested_by": self.requested_by,
                "rationale": self.rationale,
            },
            self.extra,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DelegationDecision":
        if not isinstance(raw, Mapping):
            raise ContractError("delegation_decision_must_be_an_object")
        known, extra = _split_input(raw, _known_fields(cls))
        for name in (
            "scope",
            "input_refs",
            "candidate_refs",
            "evidence_refs",
            "allowed_decisions",
            "immutable_boundaries",
            "revocable_by",
        ):
            if name in known:
                known[name] = _tuple_texts(known[name], name)
        if "budget" in known:
            known["budget"] = _mapping(known["budget"], "budget")
        known["extra"] = extra
        return cls(**known)

    def json(self) -> str:
        return _json_dump(self)


@dataclass(frozen=True)
class DecisionSlot:
    """The one decision record shared by local and delegated candidates."""

    schema_version: str = SCHEMA_VERSION
    slot_id: str = field(default_factory=lambda: _new_id("slot"))
    capability_key: str = ""
    episode_id: str = ""
    tick_index: int = 0
    candidate_refs: tuple[str, ...] = ()
    status: str = "open"
    selected_candidate_ref: str | None = None
    decision_owner: str = "ap_native"
    delegation_ref: str | None = None
    commitment_ref: str | None = None
    reason: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict, compare=True)

    def __post_init__(self) -> None:
        _text(self.schema_version, "schema_version")
        _text(self.slot_id, "slot_id")
        _text(self.capability_key, "capability_key")
        _text(self.episode_id, "episode_id")
        _nonnegative_int(self.tick_index, "tick_index")
        refs = _tuple_texts(self.candidate_refs, "candidate_refs")
        if len(set(refs)) != len(refs):
            raise ContractError("candidate_refs_must_be_unique")
        _enum(self.status, "status", SLOT_STATUS_VALUES)
        _enum(self.decision_owner, "decision_owner", OWNER_VALUES)
        _optional_text(self.selected_candidate_ref, "selected_candidate_ref")
        if self.selected_candidate_ref is not None and self.selected_candidate_ref not in refs:
            raise ContractError("selected_candidate_must_be_one_of_candidate_refs")
        if self.status == "selected" and self.selected_candidate_ref is None:
            raise ContractError("selected_slot_requires_selected_candidate")
        if self.status != "selected" and self.selected_candidate_ref is not None:
            raise ContractError("unselected_slot_cannot_have_selected_candidate")
        _optional_text(self.delegation_ref, "delegation_ref")
        _optional_text(self.commitment_ref, "commitment_ref")
        _text(self.reason, "reason", allow_empty=True)
        _mapping(self.extra, "extra")

    def to_dict(self) -> dict[str, Any]:
        return _merge_output(
            {
                "schema_version": self.schema_version,
                "slot_id": self.slot_id,
                "capability_key": self.capability_key,
                "episode_id": self.episode_id,
                "tick_index": self.tick_index,
                "candidate_refs": list(self.candidate_refs),
                "status": self.status,
                "selected_candidate_ref": self.selected_candidate_ref,
                "decision_owner": self.decision_owner,
                "delegation_ref": self.delegation_ref,
                "commitment_ref": self.commitment_ref,
                "reason": self.reason,
            },
            self.extra,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DecisionSlot":
        if not isinstance(raw, Mapping):
            raise ContractError("decision_slot_must_be_an_object")
        known, extra = _split_input(raw, _known_fields(cls))
        if "candidate_refs" in known:
            known["candidate_refs"] = _tuple_texts(known["candidate_refs"], "candidate_refs")
        known["extra"] = extra
        return cls(**known)

    def select(self, candidate_ref: str, *, owner: str, reason: str) -> "DecisionSlot":
        """Return a selected copy; a second winner cannot be represented."""

        candidate_ref = _text(candidate_ref, "candidate_ref")
        owner = _enum(owner, "decision_owner", OWNER_VALUES)
        reason = _text(reason, "reason")
        if candidate_ref not in self.candidate_refs:
            raise ContractError("cannot_select_unknown_candidate")
        if self.status == "selected" and self.selected_candidate_ref != candidate_ref:
            raise ContractError("decision_slot_already_has_a_different_selection")
        return replace(
            self,
            status="selected",
            selected_candidate_ref=candidate_ref,
            decision_owner=owner,
            reason=reason,
        )

    def json(self) -> str:
        return _json_dump(self)


@dataclass(frozen=True)
class DispatchReceipt:
    """Mechanical dispatch evidence; it never claims that the action worked."""

    schema_version: str = SCHEMA_VERSION
    receipt_id: str = field(default_factory=lambda: _new_id("rcpt"))
    action_ref: str = ""
    environment_id: str = ""
    idempotency_key: str = ""
    status: str = "attempted"
    accepted_at: str = field(default_factory=utc_now)
    connector_ref: str = ""
    attempt: int = 1
    evidence_refs: tuple[str, ...] = ()
    retryable: bool = False
    error_code: str | None = None
    duplicate_of: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict, compare=True)

    def __post_init__(self) -> None:
        _text(self.schema_version, "schema_version")
        for name in ("receipt_id", "action_ref", "environment_id", "idempotency_key", "connector_ref"):
            _text(getattr(self, name), name)
        _enum(self.status, "status", RECEIPT_STATUS_VALUES)
        _text(self.accepted_at, "accepted_at")
        _nonnegative_int(self.attempt, "attempt")
        if self.attempt == 0:
            raise ContractError("attempt_must_start_at_one")
        _tuple_texts(self.evidence_refs, "evidence_refs")
        _optional_text(self.error_code, "error_code")
        _optional_text(self.duplicate_of, "duplicate_of")
        if self.status == "duplicate" and self.duplicate_of is None:
            raise ContractError("duplicate_receipt_requires_duplicate_of")
        _mapping(self.extra, "extra")

    def to_dict(self) -> dict[str, Any]:
        return _merge_output(
            {
                "schema_version": self.schema_version,
                "receipt_id": self.receipt_id,
                "action_ref": self.action_ref,
                "environment_id": self.environment_id,
                "idempotency_key": self.idempotency_key,
                "status": self.status,
                "accepted_at": self.accepted_at,
                "connector_ref": self.connector_ref,
                "attempt": self.attempt,
                "evidence_refs": list(self.evidence_refs),
                "retryable": self.retryable,
                "error_code": self.error_code,
                "duplicate_of": self.duplicate_of,
            },
            self.extra,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DispatchReceipt":
        if not isinstance(raw, Mapping):
            raise ContractError("dispatch_receipt_must_be_an_object")
        known, extra = _split_input(raw, _known_fields(cls))
        if "evidence_refs" in known:
            known["evidence_refs"] = _tuple_texts(known["evidence_refs"], "evidence_refs")
        known["extra"] = extra
        return cls(**known)

    def json(self) -> str:
        return _json_dump(self)


@dataclass(frozen=True)
class ResultEvent:
    """Readback/result-back, kept separate from the dispatch receipt."""

    schema_version: str = SCHEMA_VERSION
    result_id: str = field(default_factory=lambda: _new_id("res"))
    receipt_ref: str = ""
    action_ref: str = ""
    environment_id: str = ""
    status: str = "unknown"
    observed_at: str = field(default_factory=utc_now)
    payload_ref: str | None = None
    payload_inline: Any | None = None
    evidence_refs: tuple[str, ...] = ()
    completeness: str = "unknown"
    source: str = "readback"
    lineage_refs: tuple[str, ...] = ()
    error_code: str | None = None
    retryable: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict, compare=True)

    def __post_init__(self) -> None:
        _text(self.schema_version, "schema_version")
        for name in ("result_id", "receipt_ref", "action_ref", "environment_id"):
            _text(getattr(self, name), name)
        _enum(self.status, "status", RESULT_STATUS_VALUES)
        _text(self.observed_at, "observed_at")
        _optional_text(self.payload_ref, "payload_ref")
        if self.payload_inline is not None:
            _copy_json(self.payload_inline)
        evidence = _tuple_texts(self.evidence_refs, "evidence_refs")
        _enum(self.completeness, "completeness", COMPLETENESS_VALUES)
        if self.source != "readback":
            raise ContractError("result_source_must_be_readback")
        _tuple_texts(self.lineage_refs, "lineage_refs")
        _optional_text(self.error_code, "error_code")
        if self.status in {"success", "partial"} and self.payload_ref is None and self.payload_inline is None and not evidence:
            raise ContractError("successful_result_requires_payload_or_evidence")
        if self.status == "success" and self.completeness == "unknown":
            raise ContractError("successful_result_cannot_have_unknown_completeness")
        _mapping(self.extra, "extra")

    def to_dict(self) -> dict[str, Any]:
        return _merge_output(
            {
                "schema_version": self.schema_version,
                "result_id": self.result_id,
                "receipt_ref": self.receipt_ref,
                "action_ref": self.action_ref,
                "environment_id": self.environment_id,
                "status": self.status,
                "observed_at": self.observed_at,
                "payload_ref": self.payload_ref,
                "payload_inline": self.payload_inline,
                "evidence_refs": list(self.evidence_refs),
                "completeness": self.completeness,
                "source": self.source,
                "lineage_refs": list(self.lineage_refs),
                "error_code": self.error_code,
                "retryable": self.retryable,
            },
            self.extra,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResultEvent":
        if not isinstance(raw, Mapping):
            raise ContractError("result_event_must_be_an_object")
        known, extra = _split_input(raw, _known_fields(cls))
        for name in ("evidence_refs", "lineage_refs"):
            if name in known:
                known[name] = _tuple_texts(known[name], name)
        known["extra"] = extra
        return cls(**known)

    def as_event_envelope(self, *, runtime_id: str, organism_id: str, episode_id: str) -> EventEnvelope:
        """Project readback into the next AP tick without changing its evidence."""

        # Even an unknown/empty readback is an occurrence: the next tick needs
        # to feel that the attempt was unresolved.  The status marker is not a
        # claimed physical payload and remains explicitly incomplete.
        inline_status = None
        if self.payload_ref is None:
            inline_status = {
                "result_status": self.status,
                "completeness": self.completeness,
                "error_code": self.error_code,
            }
        return EventEnvelope(
            # A result has one durable identity even when an adapter projects
            # it more than once.  This makes replay safe before the store's
            # idempotency-key fallback is consulted.
            event_id=f"evt_readback_{self.result_id}",
            runtime_id=runtime_id,
            organism_id=organism_id,
            environment_id=self.environment_id,
            episode_id=episode_id,
            source="readback",
            role="result",
            modality="tool",
            observed_at=self.observed_at,
            occurred_at=self.observed_at,
            payload_ref=self.payload_ref,
            payload_inline=self.payload_inline if self.payload_inline is not None else inline_status,
            evidence_refs=self.evidence_refs,
            lineage_refs=tuple(dict.fromkeys((*self.lineage_refs, self.result_id, self.receipt_ref))),
            privacy_scope="private",
            completeness=self.completeness,
            idempotency_key=f"readback:{self.result_id}",
            extra={"result_status": self.status, "error_code": self.error_code},
        )

    def json(self) -> str:
        return _json_dump(self)


class IdempotencyLedger:
    """A bounded in-memory duplicate fence for dispatch receipts.

    This is intentionally a mechanical helper, not an action selector.  A
    persistent runtime will back it with its event store and restore the same
    key/action mapping before retrying an irreversible operation.
    """

    def __init__(self, *, max_entries: int = 1024) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ContractError("max_entries_must_be_positive")
        self.max_entries = max_entries
        self._receipts: OrderedDict[str, DispatchReceipt] = OrderedDict()

    def register(self, receipt: DispatchReceipt) -> DispatchReceipt:
        if not isinstance(receipt, DispatchReceipt):
            raise ContractError("ledger_accepts_dispatch_receipts_only")
        prior = self._receipts.get(receipt.idempotency_key)
        if prior is not None:
            if prior.action_ref != receipt.action_ref or prior.environment_id != receipt.environment_id:
                raise ContractError("idempotency_key_reused_for_different_action")
            return replace(
                receipt,
                receipt_id=_new_id("rcpt"),
                status="duplicate",
                duplicate_of=prior.receipt_id,
                accepted_at=prior.accepted_at,
                attempt=prior.attempt,
            )
        self._receipts[receipt.idempotency_key] = receipt
        self._receipts.move_to_end(receipt.idempotency_key)
        while len(self._receipts) > self.max_entries:
            self._receipts.popitem(last=False)
        return receipt

    def get(self, idempotency_key: str) -> DispatchReceipt | None:
        return self._receipts.get(_text(idempotency_key, "idempotency_key"))

    def __len__(self) -> int:
        return len(self._receipts)


def dumps(contract: Any) -> str:
    """Canonical JSON serialization for any Wave 0 contract."""

    return _json_dump(contract)


def loads(contract_type: type[T], payload: str) -> T:
    """Parse canonical JSON through the contract's explicit ``from_dict``."""

    if not isinstance(payload, str):
        raise ContractError("payload_must_be_text")
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid_json:{exc.msg}") from exc
    parser = getattr(contract_type, "from_dict", None)
    if parser is None:
        raise ContractError("contract_type_has_no_from_dict")
    return parser(raw)


__all__ = [
    "SCHEMA_VERSION",
    "IMMUTABLE_LLM_BOUNDARIES",
    "ContractError",
    "EventEnvelope",
    "CapabilityOwnership",
    "DelegationDecision",
    "DecisionSlot",
    "DispatchReceipt",
    "ResultEvent",
    "IdempotencyLedger",
    "dumps",
    "loads",
    "utc_now",
]
