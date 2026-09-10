"""Bounded, reviewable project-knowledge corrections for AP-Vibe.

This module deliberately owns interpretation, exact patch validation and draft
persistence, but never owns the final knowledge write.  A draft becomes a
``ProjectKnowledgeProposal`` only after an explicit confirm request; the
existing knowledge-review AP episode remains the sole commit path.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping, Protocol, Sequence

from .contracts import ContractError, utc_now
from .gateway import GatewayTransportError, ModelTransport, UrllibModelTransport
from .project_knowledge import KNOWLEDGE_SECTIONS, LocalKnowledgeRevision
from .vibe_mind import ProjectKnowledgeProposal


MAX_CORRECTION_CHANGES = 24
MAX_CORRECTION_TEXT = 12_000
MAX_CORRECTION_VALUE_CHARS = 24_000
MAX_EXISTING_KNOWLEDGE_CHARS = 2_000_000
MAX_PATH_DEPTH = 12
CHANGE_OPERATIONS = frozenset({"set", "append", "replace_exact", "remove_exact"})
MISSING = object()


class KnowledgeCorrectionNotFound(ContractError):
    """A confirm request references no durable correction draft."""


class KnowledgeCorrectionConflict(ContractError):
    """A correction is stale or does not exactly match its parent."""


def _text(value: Any, name: str, *, limit: int = MAX_CORRECTION_TEXT, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{name}_must_be_text")
    if not allow_empty and not value.strip():
        raise ContractError(f"{name}_must_not_be_empty")
    if len(value) > limit:
        raise ContractError(f"{name}_exceeds_bound")
    return value


def _optional_text(value: Any, name: str, *, limit: int = MAX_CORRECTION_TEXT) -> str | None:
    if value is None:
        return None
    return _text(value, name, limit=limit, allow_empty=True)


def _texts(value: Any, name: str, *, max_items: int = 64, limit: int = 2_048) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractError(f"{name}_must_be_a_sequence")
    if len(value) > max_items:
        raise ContractError(f"{name}_exceeds_bound")
    return tuple(_text(item, f"{name}_item", limit=limit) for item in value)


def _json_copy(value: Any, name: str, *, limit: int = MAX_CORRECTION_VALUE_CHARS) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        copied = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_must_be_json") from exc
    if len(encoded) > limit:
        raise ContractError(f"{name}_exceeds_bound")
    return copied


def _mapping(value: Any, name: str, *, limit: int = MAX_CORRECTION_VALUE_CHARS) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContractError(f"{name}_must_be_an_object")
    copied = _json_copy(dict(value), name, limit=limit)
    if not isinstance(copied, dict):
        raise ContractError(f"{name}_must_be_an_object")
    return copied


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def _pointer_tokens(path: Any) -> tuple[str, ...]:
    text = _text(path, "knowledge_change_path", limit=1_024)
    if not text.startswith("/") or text == "/":
        raise ContractError("knowledge_change_path_must_be_non_root_json_pointer")
    raw = text.split("/")[1:]
    if len(raw) > MAX_PATH_DEPTH:
        raise ContractError("knowledge_change_path_exceeds_depth")
    output: list[str] = []
    for token in raw:
        if not token or token in {"-", "*", ".."}:
            raise ContractError("knowledge_change_path_token_unsupported")
        decoded = token.replace("~1", "/").replace("~0", "~")
        if "~" in token.replace("~0", "").replace("~1", ""):
            raise ContractError("knowledge_change_path_escape_invalid")
        if decoded in {"-", "*", ".."}:
            raise ContractError("knowledge_change_path_token_unsupported")
        output.append(decoded)
    return tuple(output)


def _resolve_parent(root: Any, tokens: Sequence[str]) -> tuple[Any, str]:
    if not tokens:
        raise KnowledgeCorrectionConflict("knowledge_change_root_write_forbidden")
    current = root
    for token in tokens[:-1]:
        if isinstance(current, dict):
            if token not in current:
                raise KnowledgeCorrectionConflict("knowledge_change_path_not_found")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit():
                raise KnowledgeCorrectionConflict("knowledge_change_list_index_invalid")
            index = int(token)
            if index < 0 or index >= len(current):
                raise KnowledgeCorrectionConflict("knowledge_change_list_index_out_of_bounds")
            current = current[index]
        else:
            raise KnowledgeCorrectionConflict("knowledge_change_path_not_container")
    return current, tokens[-1]


def _read_at(parent: Any, token: str) -> Any:
    if isinstance(parent, dict):
        return parent.get(token, MISSING)
    if isinstance(parent, list):
        if not token.isdigit():
            raise KnowledgeCorrectionConflict("knowledge_change_list_index_invalid")
        index = int(token)
        if index < 0 or index >= len(parent):
            return MISSING
        return parent[index]
    raise KnowledgeCorrectionConflict("knowledge_change_path_not_container")


def _write_at(parent: Any, token: str, value: Any) -> None:
    if isinstance(parent, dict):
        parent[token] = value
        return
    if isinstance(parent, list):
        if not token.isdigit():
            raise KnowledgeCorrectionConflict("knowledge_change_list_index_invalid")
        index = int(token)
        if index < 0 or index >= len(parent):
            raise KnowledgeCorrectionConflict("knowledge_change_list_index_out_of_bounds")
        parent[index] = value
        return
    raise KnowledgeCorrectionConflict("knowledge_change_path_not_container")


def _remove_at(parent: Any, token: str) -> None:
    if isinstance(parent, dict):
        if token not in parent:
            raise KnowledgeCorrectionConflict("knowledge_change_path_not_found")
        del parent[token]
        return
    if isinstance(parent, list):
        if not token.isdigit():
            raise KnowledgeCorrectionConflict("knowledge_change_list_index_invalid")
        index = int(token)
        if index < 0 or index >= len(parent):
            raise KnowledgeCorrectionConflict("knowledge_change_list_index_out_of_bounds")
        parent.pop(index)
        return
    raise KnowledgeCorrectionConflict("knowledge_change_path_not_container")


@dataclass(frozen=True)
class KnowledgeSectionChange:
    section: str
    path: str
    operation: str
    before: Any = None
    after: Any = None
    before_present: bool = True
    rationale: str = "用户明确提出局部知识纠正"
    applicability: Mapping[str, Any] = field(default_factory=dict)
    counterexamples: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    confidence: float = 1.0
    uncertainty: float = 0.0
    completeness: str = "complete"

    def __post_init__(self) -> None:
        if self.section not in KNOWLEDGE_SECTIONS:
            raise ContractError("knowledge_change_section_unsupported")
        _pointer_tokens(self.path)
        if self.operation not in CHANGE_OPERATIONS:
            raise ContractError("knowledge_change_operation_unsupported")
        if not isinstance(self.before_present, bool):
            raise ContractError("knowledge_change_before_present_must_be_boolean")
        _json_copy(self.before, "knowledge_change_before")
        _json_copy(self.after, "knowledge_change_after")
        _text(self.rationale, "knowledge_change_rationale", limit=4_096)
        _mapping(self.applicability, "knowledge_change_applicability")
        _texts(self.counterexamples, "knowledge_change_counterexamples", max_items=12)
        _texts(self.source_refs, "knowledge_change_source_refs")
        _texts(self.evidence_refs, "knowledge_change_evidence_refs")
        for name in ("confidence", "uncertainty"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                raise ContractError(f"knowledge_change_{name}_out_of_bounds")
        if self.completeness not in {"complete", "partial", "search_incomplete", "unknown"}:
            raise ContractError("knowledge_change_completeness_unsupported")
        if self.operation in {"replace_exact", "remove_exact"} and not self.before_present:
            raise ContractError("knowledge_change_exact_operation_requires_before")
        if self.operation == "append" and self.before_present:
            raise ContractError("knowledge_change_append_must_not_claim_before")

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "path": self.path,
            "operation": self.operation,
            "before": _json_copy(self.before, "knowledge_change_before"),
            "after": _json_copy(self.after, "knowledge_change_after"),
            "before_present": self.before_present,
            "rationale": self.rationale,
            "applicability": _mapping(self.applicability, "knowledge_change_applicability"),
            "counterexamples": list(self.counterexamples),
            "source_refs": list(self.source_refs),
            "evidence_refs": list(self.evidence_refs),
            "confidence": round(float(self.confidence), 6),
            "uncertainty": round(float(self.uncertainty), 6),
            "completeness": self.completeness,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "KnowledgeSectionChange":
        if not isinstance(raw, Mapping):
            raise ContractError("knowledge_section_change_must_be_an_object")
        allowed = {
            "section", "path", "operation", "before", "after", "before_present",
            "rationale", "applicability", "counterexamples", "source_refs",
            "evidence_refs", "confidence", "uncertainty", "completeness",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ContractError("knowledge_section_change_unknown_fields")
        values = dict(raw)
        values["before_present"] = bool(raw.get("before_present", "before" in raw))
        for name in ("counterexamples", "source_refs", "evidence_refs"):
            if name in values:
                values[name] = _texts(values[name], f"knowledge_change_{name}", max_items=12 if name == "counterexamples" else 64)
        return cls(**values)


def apply_section_changes(
    raw_sections: Mapping[str, Any],
    raw_changes: Sequence[Mapping[str, Any] | KnowledgeSectionChange],
) -> dict[str, Any]:
    """Apply a bounded sequence atomically to a JSON copy of all sections."""

    sections = _mapping(raw_sections, "knowledge_sections", limit=MAX_EXISTING_KNOWLEDGE_CHARS)
    if isinstance(raw_changes, (str, bytes)) or not isinstance(raw_changes, Sequence):
        raise ContractError("knowledge_section_changes_must_be_a_sequence")
    if len(raw_changes) > MAX_CORRECTION_CHANGES:
        raise ContractError("knowledge_section_changes_exceeds_bound")
    for raw in raw_changes:
        change = raw if isinstance(raw, KnowledgeSectionChange) else KnowledgeSectionChange.from_dict(raw)
        if change.section == "identity" and change.path in {
            "/project_id", "/authority", "/vibe_formal_write"
        }:
            raise KnowledgeCorrectionConflict("knowledge_change_protected_identity_field")
        section_root = sections.get(change.section)
        if not isinstance(section_root, (dict, list)):
            raise KnowledgeCorrectionConflict("knowledge_change_section_not_container")
        parent, token = _resolve_parent(section_root, _pointer_tokens(change.path))
        current = _read_at(parent, token)
        if change.operation == "set":
            if change.before_present:
                if current is MISSING or current != change.before:
                    raise KnowledgeCorrectionConflict("knowledge_change_before_mismatch")
            elif current is not MISSING:
                raise KnowledgeCorrectionConflict("knowledge_change_expected_missing_but_exists")
            _write_at(parent, token, _json_copy(change.after, "knowledge_change_after"))
        elif change.operation == "append":
            if current is MISSING:
                raise KnowledgeCorrectionConflict("knowledge_change_path_not_found")
            if not isinstance(current, list):
                raise KnowledgeCorrectionConflict("knowledge_change_append_requires_list")
            value = _json_copy(change.after, "knowledge_change_after")
            if value not in current:
                current.append(value)
        elif change.operation == "replace_exact":
            if current is MISSING or current != change.before:
                raise KnowledgeCorrectionConflict("knowledge_change_before_mismatch")
            _write_at(parent, token, _json_copy(change.after, "knowledge_change_after"))
        else:
            if current is MISSING or current != change.before:
                raise KnowledgeCorrectionConflict("knowledge_change_before_mismatch")
            _remove_at(parent, token)
    return sections


@dataclass(frozen=True)
class KnowledgeCorrectionRequest:
    project_id: str
    instruction: str
    expected_parent_revision_id: str | None
    explicit_changes: tuple[KnowledgeSectionChange, ...] = ()
    source_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _text(self.project_id, "knowledge_correction_project_id", limit=512)
        _text(self.instruction, "knowledge_correction_instruction")
        _optional_text(self.expected_parent_revision_id, "knowledge_correction_parent", limit=512)
        if len(self.explicit_changes) > MAX_CORRECTION_CHANGES:
            raise ContractError("knowledge_correction_changes_exceeds_bound")
        if any(not isinstance(item, KnowledgeSectionChange) for item in self.explicit_changes):
            raise ContractError("knowledge_correction_change_invalid")
        _texts(self.source_refs, "knowledge_correction_source_refs")
        _texts(self.evidence_refs, "knowledge_correction_evidence_refs")
        _text(self.created_at, "knowledge_correction_created_at", limit=128)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "instruction": self.instruction,
            "expected_parent_revision_id": self.expected_parent_revision_id,
            "explicit_changes": [item.to_dict() for item in self.explicit_changes],
            "source_refs": list(self.source_refs),
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "KnowledgeCorrectionRequest":
        if not isinstance(raw, Mapping):
            raise ContractError("knowledge_correction_request_must_be_an_object")
        allowed = {
            "project_id", "instruction", "expected_parent_revision_id", "explicit_changes",
            "source_refs", "evidence_refs", "created_at",
        }
        if set(raw) - allowed:
            raise ContractError("knowledge_correction_request_unknown_fields")
        changes = raw.get("explicit_changes", ())
        if isinstance(changes, (str, bytes)) or not isinstance(changes, Sequence):
            raise ContractError("knowledge_correction_changes_must_be_a_sequence")
        return cls(
            project_id=raw.get("project_id"),
            instruction=raw.get("instruction"),
            expected_parent_revision_id=raw.get("expected_parent_revision_id"),
            explicit_changes=tuple(KnowledgeSectionChange.from_dict(item) for item in changes),
            source_refs=_texts(raw.get("source_refs"), "knowledge_correction_source_refs"),
            evidence_refs=_texts(raw.get("evidence_refs"), "knowledge_correction_evidence_refs"),
            created_at=raw.get("created_at", utc_now()),
        )


@dataclass(frozen=True)
class CorrectionInterpretation:
    changes: tuple[KnowledgeSectionChange, ...]
    source: str
    proposal_incomplete: bool
    limitations: tuple[str, ...] = ()
    model_receipt: Mapping[str, Any] | None = None


class KnowledgeCorrectionInterpreter(Protocol):
    def interpret(
        self,
        request: KnowledgeCorrectionRequest,
        parent: LocalKnowledgeRevision | None,
    ) -> CorrectionInterpretation:
        ...


class NullKnowledgeCorrectionInterpreter:
    """Provider-off behavior: never infer executable semantics from prose."""

    def interpret(
        self,
        request: KnowledgeCorrectionRequest,
        parent: LocalKnowledgeRevision | None,
    ) -> CorrectionInterpretation:
        del parent
        if request.explicit_changes:
            return CorrectionInterpretation(
                changes=request.explicit_changes,
                source="user_explicit",
                proposal_incomplete=False,
                limitations=("provider_off_explicit_structure_used",),
            )
        return CorrectionInterpretation(
            changes=(),
            source="provider_off",
            proposal_incomplete=True,
            limitations=(
                "proposal_incomplete:no_provider_configured",
                "select_section_operation_path_and_values_to_continue",
            ),
        )


class OpenAICompatibleKnowledgeCorrectionInterpreter:
    """One bounded teacher call that can only propose exact local patches."""

    PROMPT_TEMPLATE_VERSION = "ap-vibe.knowledge-correction.v1"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        provider: str = "openai-compatible",
        timeout: float = 30.0,
        max_response_bytes: int = 1_000_000,
        max_prompt_chars: int = 64_000,
        max_output_tokens: int = 2_400,
        transport: ModelTransport | None = None,
    ) -> None:
        self.base_url = _text(base_url, "correction_provider_base_url", limit=2_048).rstrip("/")
        self._api_key = _text(api_key, "correction_provider_api_key", limit=4_096)
        self.model = _text(model, "correction_provider_model", limit=256)
        self.provider = _text(provider, "correction_provider_name", limit=256)
        if not 0 < float(timeout) <= 120:
            raise ContractError("correction_provider_timeout_out_of_bounds")
        if not 1_024 <= int(max_response_bytes) <= 8_000_000:
            raise ContractError("correction_provider_response_bound_out_of_bounds")
        if not 4_000 <= int(max_prompt_chars) <= 200_000:
            raise ContractError("correction_provider_prompt_bound_out_of_bounds")
        if not 256 <= int(max_output_tokens) <= 8_192:
            raise ContractError("correction_provider_output_bound_out_of_bounds")
        self.timeout = float(timeout)
        self.max_response_bytes = int(max_response_bytes)
        self.max_prompt_chars = int(max_prompt_chars)
        self.max_output_tokens = int(max_output_tokens)
        self.transport = transport or UrllibModelTransport()

    def _endpoint(self) -> str:
        return self.base_url if self.base_url.endswith("/chat/completions") else self.base_url + "/chat/completions"

    @staticmethod
    def _content(response: Mapping[str, Any]) -> str | None:
        choices = response.get("choices")
        if isinstance(choices, (str, bytes)) or not isinstance(choices, Sequence) or not choices:
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
            return "".join(
                str(item.get("text"))
                for item in content
                if isinstance(item, Mapping) and isinstance(item.get("text"), str)
            ) or None
        return None

    @staticmethod
    def _object(content: str | None) -> Mapping[str, Any] | None:
        if not content:
            return None
        text = content.strip()
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, Mapping) else None

    def interpret(
        self,
        request: KnowledgeCorrectionRequest,
        parent: LocalKnowledgeRevision | None,
    ) -> CorrectionInterpretation:
        if request.explicit_changes:
            return CorrectionInterpretation(
                changes=request.explicit_changes,
                source="user_explicit",
                proposal_incomplete=False,
                limitations=("explicit_user_structure_used_without_teacher_call",),
            )
        if parent is None:
            return CorrectionInterpretation(
                changes=(), source="llm_model", proposal_incomplete=True,
                limitations=("proposal_incomplete:no_reviewed_parent",),
            )

        parent_view = {
            "revision_id": parent.revision_id,
            "content_hash": parent.content_hash,
            "sections": parent.sections,
            "source_refs": list(parent.source_refs),
            "evidence_refs": list(parent.evidence_refs),
        }
        user_view = {
            "instruction": request.instruction,
            "source_refs": list(request.source_refs),
            "evidence_refs": list(request.evidence_refs),
            "expected_parent_revision_id": request.expected_parent_revision_id,
        }
        context = _canonical({"current_knowledge": parent_view, "user_request": user_view})
        if len(context) > self.max_prompt_chars:
            return CorrectionInterpretation(
                changes=(), source="llm_model", proposal_incomplete=True,
                limitations=("proposal_incomplete:knowledge_context_exceeds_bound",),
            )
        system = (
            "你是 AP-Vibe 联合心智中的项目知识纠正教师。只返回一个 JSON 对象，不要 markdown。"
            "顶层只能使用 status, changes, limitations, uncertainty。changes 最多 24 项。"
            "每项必须含 section,path,operation,before,after,before_present,rationale,applicability,"
            "counterexamples,confidence,uncertainty,completeness。section 只能是 identity,status,work,"
            "architecture,requirements,decisions,dependencies,evidence,risks,sources,recovery；"
            "operation 只能是 set,append,replace_exact,remove_exact。path 是相对章节根的 JSON Pointer，"
            "不能使用通配符或模糊匹配。replace/remove 的 before 必须逐字逐结构复制当前知识中的真实值；"
            "append 的 path 必须指向现有列表且 before_present=false；set 已有值时必须给精确 before。"
            "不得修改 identity 的 project_id,authority,vibe_formal_write；不得把用户主张当外部证据；"
            "不得生成执行结果、行动 winner 或 revision。含糊时返回 status=partial、空 changes 和限制，"
            "不要猜测。若用户要求保留旧记录，应通过追加带适用范围/新解释的条目实现，而不是抹掉来源。"
        )
        request_key = hashlib.sha256(
            _canonical({"template": self.PROMPT_TEMPLATE_VERSION, "model": self.model, "context": context}).encode("utf-8")
        ).hexdigest()
        call_id = f"llmcall_{request_key[:32]}"
        started = time.monotonic()
        try:
            response = self.transport.request_json(
                "POST",
                self._endpoint(),
                body={
                    "model": self.model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": context}],
                    "temperature": 0.1,
                    "max_tokens": self.max_output_tokens,
                    "response_format": {"type": "json_object"},
                },
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Idempotency-Key": call_id,
                    "X-APV4-Call-Id": call_id,
                },
                timeout=self.timeout,
                max_response_bytes=self.max_response_bytes,
            )
        except GatewayTransportError as exc:
            return CorrectionInterpretation(
                changes=(), source="llm_model", proposal_incomplete=True,
                limitations=(f"proposal_incomplete:{exc}",),
                model_receipt={
                    "call_id": call_id, "request_key": request_key, "provider": self.provider,
                    "model": self.model, "prompt_template_version": self.PROMPT_TEMPLATE_VERSION,
                    "status": "failed", "latency_ms": round((time.monotonic() - started) * 1000.0, 3),
                },
            )
        elapsed = round((time.monotonic() - started) * 1000.0, 3)
        content = self._content(response)
        raw = self._object(content)
        usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
        receipt = {
            "call_id": call_id, "request_key": request_key, "provider": self.provider,
            "model": self.model, "prompt_template_version": self.PROMPT_TEMPLATE_VERSION,
            "status": "received", "latency_ms": elapsed,
            "usage": _mapping(usage, "correction_provider_usage"),
            "output_hash": hashlib.sha256((content or "").encode("utf-8")).hexdigest(),
        }
        if raw is None:
            return CorrectionInterpretation(
                changes=(), source="llm_model", proposal_incomplete=True,
                limitations=("proposal_incomplete:response_not_json_object",), model_receipt=receipt,
            )
        raw_changes = raw.get("changes", ())
        if isinstance(raw_changes, (str, bytes)) or not isinstance(raw_changes, Sequence):
            raw_changes = ()
        limitations = [
            _text(item, "correction_provider_limitation", limit=512)
            for item in (raw.get("limitations") if isinstance(raw.get("limitations"), Sequence) and not isinstance(raw.get("limitations"), (str, bytes)) else ())[:24]
            if isinstance(item, str)
        ]
        accepted: list[KnowledgeSectionChange] = []
        for index, item in enumerate(raw_changes[:MAX_CORRECTION_CHANGES]):
            if not isinstance(item, Mapping):
                limitations.append(f"change_{index}:not_object")
                continue
            candidate_raw = {
                key: value for key, value in item.items()
                if key not in {"source_refs", "evidence_refs"}
            }
            candidate_raw["source_refs"] = [*request.source_refs, f"llm:{call_id}"]
            candidate_raw["evidence_refs"] = list(request.evidence_refs)
            try:
                candidate = KnowledgeSectionChange.from_dict(candidate_raw)
                apply_section_changes(parent.sections, (candidate,))
            except (ContractError, KnowledgeCorrectionConflict) as exc:
                limitations.append(f"change_{index}:{exc}")
                continue
            accepted.append(candidate)
        status = raw.get("status")
        incomplete = status not in {"proposal", "complete"} or not accepted or len(accepted) != len(raw_changes)
        if len(raw_changes) > MAX_CORRECTION_CHANGES:
            incomplete = True
            limitations.append("changes_exceed_bound")
        if incomplete and not any(item.startswith("proposal_incomplete") for item in limitations):
            limitations.append("proposal_incomplete:teacher_output_requires_user_review")
        return CorrectionInterpretation(
            changes=tuple(accepted),
            source="llm_model",
            proposal_incomplete=incomplete,
            limitations=tuple(dict.fromkeys(limitations[:64])),
            model_receipt=receipt,
        )


@dataclass(frozen=True)
class KnowledgeCorrectionDraft:
    draft_id: str
    request_id: str
    project_id: str
    instruction: str
    parent_revision_id: str | None
    parent_content_hash: str | None
    changes: tuple[KnowledgeSectionChange, ...]
    interpretation_source: str
    proposal_incomplete: bool
    limitations: tuple[str, ...]
    source_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    preview_sections: Mapping[str, Any] | None
    created_at: str
    model_receipt: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "request_id": self.request_id,
            "project_id": self.project_id,
            "instruction": self.instruction,
            "parent_revision_id": self.parent_revision_id,
            "parent_content_hash": self.parent_content_hash,
            "changes": [item.to_dict() for item in self.changes],
            "interpretation_source": self.interpretation_source,
            "proposal_incomplete": self.proposal_incomplete,
            "limitations": list(self.limitations),
            "source_refs": list(self.source_refs),
            "evidence_refs": list(self.evidence_refs),
            "preview_sections": _mapping(self.preview_sections, "preview_sections", limit=MAX_EXISTING_KNOWLEDGE_CHARS) if self.preview_sections is not None else None,
            "created_at": self.created_at,
            "model_receipt": _mapping(self.model_receipt, "model_receipt") if self.model_receipt is not None else None,
            "write_state": "preview_only",
            "formal_knowledge": False,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "KnowledgeCorrectionDraft":
        if not isinstance(raw, Mapping):
            raise ContractError("knowledge_correction_draft_must_be_an_object")
        changes = raw.get("changes", ())
        return cls(
            draft_id=raw["draft_id"],
            request_id=raw["request_id"],
            project_id=raw["project_id"],
            instruction=raw["instruction"],
            parent_revision_id=raw.get("parent_revision_id"),
            parent_content_hash=raw.get("parent_content_hash"),
            changes=tuple(KnowledgeSectionChange.from_dict(item) for item in changes),
            interpretation_source=raw["interpretation_source"],
            proposal_incomplete=bool(raw.get("proposal_incomplete", False)),
            limitations=_texts(raw.get("limitations"), "knowledge_correction_limitations"),
            source_refs=_texts(raw.get("source_refs"), "knowledge_correction_source_refs"),
            evidence_refs=_texts(raw.get("evidence_refs"), "knowledge_correction_evidence_refs"),
            preview_sections=_mapping(raw.get("preview_sections"), "preview_sections", limit=MAX_EXISTING_KNOWLEDGE_CHARS) if raw.get("preview_sections") is not None else None,
            created_at=raw["created_at"],
            model_receipt=_mapping(raw.get("model_receipt"), "model_receipt") if raw.get("model_receipt") is not None else None,
        )

    def as_proposal(self) -> ProjectKnowledgeProposal:
        if self.proposal_incomplete or not self.changes:
            raise ContractError("knowledge_correction_draft_incomplete")
        confidence = min(item.confidence for item in self.changes)
        uncertainty = max(item.uncertainty for item in self.changes)
        return ProjectKnowledgeProposal(
            proposal_id=_stable_id("knowledge_correction_proposal", self.project_id, self.draft_id),
            project_id=self.project_id,
            target_sections=tuple(dict.fromkeys(item.section for item in self.changes)),
            summary=self.instruction,
            source_refs=tuple(dict.fromkeys((*self.source_refs, f"draft:{self.draft_id}"))),
            evidence_refs=tuple(dict.fromkeys(self.evidence_refs)),
            section_changes=tuple(item.to_dict() for item in self.changes),
            ap_basis={"correction_draft_ref": self.draft_id},
            teacher_basis=_mapping(self.model_receipt, "model_receipt") if self.model_receipt is not None else {},
            confidence=confidence,
            uncertainty=uncertainty,
            conflicts=(),
            status="staged",
            created_at=self.created_at,
            extra={
                "source_completeness": "complete",
                "proposal_kind": "knowledge_correction",
                "interpretation_source": self.interpretation_source,
                "draft_id": self.draft_id,
            },
        )


class KnowledgeCorrectionStore:
    """Append-only durable draft registry; final revision remains elsewhere."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS knowledge_correction_drafts (
                    draft_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    draft_json TEXT NOT NULL,
                    confirmed_revision_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_correction_project_recent
                   ON knowledge_correction_drafts(project_id, created_at DESC)"""
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    @staticmethod
    def fingerprint(request: KnowledgeCorrectionRequest) -> str:
        semantic = request.to_dict()
        # Generated time is lineage metadata, not part of the user's intent.
        semantic.pop("created_at", None)
        return hashlib.sha256(_canonical(semantic).encode("utf-8")).hexdigest()

    def create_or_replay(
        self,
        request_id: str,
        request: KnowledgeCorrectionRequest,
        parent: LocalKnowledgeRevision | None,
        interpreter: KnowledgeCorrectionInterpreter,
    ) -> tuple[KnowledgeCorrectionDraft, bool]:
        request_id = _text(request_id, "knowledge_correction_request_id", limit=256)
        fingerprint = self.fingerprint(request)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_correction_drafts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is not None:
                if str(row["fingerprint"]) != fingerprint:
                    raise KnowledgeCorrectionConflict("knowledge_correction_request_id_conflict")
                return KnowledgeCorrectionDraft.from_dict(json.loads(str(row["draft_json"]))), True

        current_parent = parent.revision_id if parent is not None else None
        if request.expected_parent_revision_id != current_parent:
            raise KnowledgeCorrectionConflict("knowledge_correction_parent_revision_changed")
        interpretation = interpreter.interpret(request, parent)
        preview_sections = None
        if interpretation.changes:
            base = parent.sections if parent is not None else {key: {} for key in KNOWLEDGE_SECTIONS}
            preview_sections = apply_section_changes(base, interpretation.changes)
        created_at = utc_now()
        draft_id = _stable_id("knowledge_correction_draft", request.project_id, request_id, fingerprint)
        draft = KnowledgeCorrectionDraft(
            draft_id=draft_id,
            request_id=request_id,
            project_id=request.project_id,
            instruction=request.instruction,
            parent_revision_id=current_parent,
            parent_content_hash=parent.content_hash if parent is not None else None,
            changes=interpretation.changes,
            interpretation_source=interpretation.source,
            proposal_incomplete=interpretation.proposal_incomplete,
            limitations=interpretation.limitations,
            source_refs=request.source_refs,
            evidence_refs=request.evidence_refs,
            preview_sections=preview_sections,
            created_at=created_at,
            model_receipt=interpretation.model_receipt,
        )
        with closing(self._connect()) as connection:
            try:
                connection.execute(
                    """INSERT INTO knowledge_correction_drafts
                       (draft_id, request_id, fingerprint, project_id, draft_json,
                        confirmed_revision_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, NULL, ?, ?)""",
                    (
                        draft.draft_id, request_id, fingerprint, request.project_id,
                        _canonical(draft.to_dict()), created_at, created_at,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise KnowledgeCorrectionConflict("knowledge_correction_draft_identity_conflict") from exc
        return draft, False

    def get(self, project_id: str, draft_id: str) -> KnowledgeCorrectionDraft:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT draft_json FROM knowledge_correction_drafts WHERE project_id = ? AND draft_id = ?",
                (project_id, draft_id),
            ).fetchone()
        if row is None:
            raise KnowledgeCorrectionNotFound("knowledge_correction_draft_not_found")
        return KnowledgeCorrectionDraft.from_dict(json.loads(str(row["draft_json"])))

    def mark_confirmed(self, draft_id: str, revision_id: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE knowledge_correction_drafts
                   SET confirmed_revision_id = COALESCE(confirmed_revision_id, ?), updated_at = ?
                   WHERE draft_id = ?""",
                (revision_id, utc_now(), draft_id),
            )
            connection.commit()

    def recent(self, project_id: str, *, limit: int = 8) -> list[dict[str, Any]]:
        bounded = max(1, min(24, int(limit)))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT draft_json, confirmed_revision_id FROM knowledge_correction_drafts
                   WHERE project_id = ? ORDER BY created_at DESC LIMIT ?""",
                (project_id, bounded),
            ).fetchall()
        output = []
        for row in rows:
            item = KnowledgeCorrectionDraft.from_dict(json.loads(str(row["draft_json"]))).to_dict()
            item["confirmed_revision_id"] = str(row["confirmed_revision_id"]) if row["confirmed_revision_id"] else None
            item["write_state"] = "confirmed" if row["confirmed_revision_id"] else "preview_only"
            output.append(item)
        return output


__all__ = [
    "CHANGE_OPERATIONS",
    "MAX_CORRECTION_CHANGES",
    "KnowledgeCorrectionConflict",
    "KnowledgeCorrectionNotFound",
    "KnowledgeSectionChange",
    "KnowledgeCorrectionRequest",
    "CorrectionInterpretation",
    "KnowledgeCorrectionInterpreter",
    "NullKnowledgeCorrectionInterpreter",
    "KnowledgeCorrectionDraft",
    "KnowledgeCorrectionStore",
    "apply_section_changes",
]
