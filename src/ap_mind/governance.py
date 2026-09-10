"""Version compatibility record for the APV4 Hybrid governance boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping

from .contracts.core import (
    ContractError,
    IMMUTABLE_LLM_BOUNDARIES,
    SCHEMA_VERSION,
    _copy_json,
    _enum,
    _mapping,
    _merge_output,
    _optional_text,
    _split_input,
    _text,
    _tuple_texts,
    _known_fields,
    _json_dump,
    utc_now,
)


COMPATIBILITY_VALUES = frozenset({"pending", "compatible", "incompatible"})
SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")


@dataclass(frozen=True)
class GovernanceCompatibilityRecord:
    """A compact, inspectable statement of which authority rules are active.

    It is a record, not an approval workflow.  A ``pending`` record permits
    local contract work while preventing a caller from silently enabling a
    delegated winner before the surrounding Skill/project policy agrees.
    """

    schema_version: str = SCHEMA_VERSION
    record_id: str = ""
    project_id: str = ""
    runtime_contract_version: str = SCHEMA_VERSION
    whitepaper_sha256: str = ""
    compatibility: str = "pending"
    llm_delegation_enabled: bool = False
    authority_sources: Mapping[str, str] = field(default_factory=dict)
    immutable_boundaries: tuple[str, ...] = IMMUTABLE_LLM_BOUNDARIES
    incompatibilities: tuple[str, ...] = ()
    checked_at: str = field(default_factory=utc_now)
    next_action: str = "confirm the project/Skill mapping before enabling delegated winner"
    extra: Mapping[str, Any] = field(default_factory=dict, compare=True)

    def __post_init__(self) -> None:
        _text(self.schema_version, "schema_version")
        _text(self.record_id, "record_id")
        _text(self.project_id, "project_id")
        _text(self.runtime_contract_version, "runtime_contract_version")
        if not SHA256_RE.fullmatch(self.whitepaper_sha256):
            raise ContractError("whitepaper_sha256_must_be_64_hex_characters")
        _enum(self.compatibility, "compatibility", COMPATIBILITY_VALUES)
        if self.llm_delegation_enabled and self.compatibility != "compatible":
            raise ContractError("llm_delegation_requires_compatible_governance")
        _mapping(self.authority_sources, "authority_sources")
        for key, value in self.authority_sources.items():
            _text(key, "authority_source_key")
            _text(value, "authority_source_value")
        boundaries = _tuple_texts(self.immutable_boundaries, "immutable_boundaries")
        if set(IMMUTABLE_LLM_BOUNDARIES).difference(boundaries):
            raise ContractError("governance_record_dropped_immutable_boundary")
        _tuple_texts(self.incompatibilities, "incompatibilities")
        _text(self.checked_at, "checked_at")
        _text(self.next_action, "next_action")
        _mapping(self.extra, "extra")

    def to_dict(self) -> dict[str, Any]:
        return _merge_output(
            {
                "schema_version": self.schema_version,
                "record_id": self.record_id,
                "project_id": self.project_id,
                "runtime_contract_version": self.runtime_contract_version,
                "whitepaper_sha256": self.whitepaper_sha256,
                "compatibility": self.compatibility,
                "llm_delegation_enabled": self.llm_delegation_enabled,
                "authority_sources": dict(self.authority_sources),
                "immutable_boundaries": list(self.immutable_boundaries),
                "incompatibilities": list(self.incompatibilities),
                "checked_at": self.checked_at,
                "next_action": self.next_action,
            },
            self.extra,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GovernanceCompatibilityRecord":
        if not isinstance(raw, Mapping):
            raise ContractError("governance_record_must_be_an_object")
        known, extra = _split_input(raw, _known_fields(cls))
        for name in ("immutable_boundaries", "incompatibilities"):
            if name in known:
                known[name] = _tuple_texts(known[name], name)
        if "authority_sources" in known:
            known["authority_sources"] = _mapping(known["authority_sources"], "authority_sources")
        known["extra"] = extra
        return cls(**known)

    def assert_delegation_ready(self) -> None:
        if self.compatibility != "compatible" or not self.llm_delegation_enabled:
            raise ContractError(
                "delegated_winner_not_enabled:" + (self.next_action or "resolve governance compatibility")
            )

    def json(self) -> str:
        return _json_dump(self)


__all__ = ["GovernanceCompatibilityRecord"]
