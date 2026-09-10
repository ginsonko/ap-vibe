"""Bounded, read-only Codex JSONL receptor for the AP-Vibe environment.

The receptor only turns user-visible conversation messages into source-grounded
``ProjectActivity`` values.  It never executes file content, reads hidden
reasoning/tool arguments, decides cognition, or advances its cursor before the
caller confirms that downstream processing succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from .contracts import ContractError, utc_now
from .vibe_mind import ProjectActivity


MAX_SAMPLE_BYTES = 2 * 1024 * 1024
MAX_SAMPLE_EVENTS = 32
MAX_VISIBLE_TEXT = 12_000
MAX_LINE_BYTES = 512 * 1024
SKIPPED_RESPONSE_TYPES = frozenset(
    {
        "reasoning",
        "custom_tool_call",
        "custom_tool_call_output",
        "function_call",
        "function_call_output",
        "web_search_call",
    }
)
SKIPPED_EVENT_TYPES = frozenset(
    {
        "agent_reasoning",
        "token_count",
        "mcp_tool_call_begin",
        "mcp_tool_call_end",
        "patch_apply_begin",
        "patch_apply_end",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s,;\"']{8,}"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;\"']{8,}"),
)
_CONTEXT_PREFIXES = (
    "<environment_context>",
    "<recommended_plugins>",
    "# AGENTS.md instructions",
    "<codex_delegation>",
)


def _clean_text(value: str) -> str:
    text = value.strip()
    text = re.sub(r"<(thinking|analysis)>[\s\S]*?(?:</\1>|$)", "", text, flags=re.IGNORECASE)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", text)
    return text[:MAX_VISIBLE_TEXT]


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def _content_text(content: Any, role: str) -> str | None:
    if isinstance(content, str):
        return _clean_text(content) or None
    if isinstance(content, (str, bytes)) or not isinstance(content, Sequence):
        return None
    allowed = "input_text" if role == "user" else "output_text"
    parts: list[str] = []
    for item in list(content)[:32]:
        if not isinstance(item, Mapping) or item.get("type") != allowed:
            continue
        raw = item.get("text")
        if not isinstance(raw, str):
            continue
        clean = _clean_text(raw)
        if clean and not clean.startswith(_CONTEXT_PREFIXES):
            parts.append(clean)
    joined = "\n".join(parts).strip()
    return joined[:MAX_VISIBLE_TEXT] or None


def _summary(text: str) -> str:
    compact = " ".join(text.split())
    return compact[:220] + ("…" if len(compact) > 220 else "")


@dataclass(frozen=True)
class CodexVisibleOccurrence:
    occurrence_id: str
    role: str
    text: str
    timestamp: str
    source_ref: str
    event_kind: str
    start_offset: int
    end_offset: int
    completeness: str = "complete"
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "occurrence_id": self.occurrence_id,
            "role": self.role,
            "text": self.text,
            "timestamp": self.timestamp,
            "source_ref": self.source_ref,
            "event_kind": self.event_kind,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "completeness": self.completeness,
            **dict(self.extra),
        }

    def as_activity(self, project_id: str) -> ProjectActivity:
        actor = "user" if self.role == "user" else "codex"
        return ProjectActivity(
            activity_id=_stable_id("codex_activity", self.occurrence_id, project_id),
            project_id=project_id,
            conversation_id=self.extra.get("conversation_id") if isinstance(self.extra.get("conversation_id"), str) else None,
            kind=f"codex_visible_{self.role}_message",
            summary=_summary(self.text),
            detail=self.text,
            actor=actor,
            status="observed_message",
            occurred_at=self.timestamp,
            source_ref=self.source_ref,
            evidence_refs=(self.source_ref,),
            completeness=self.completeness,
            privacy_scope="project",
            observed_unknown=("消息中的工程声明尚未由独立运行收据核验",) if self.role == "assistant" else (),
            observed_next_action="把这条可见消息送入项目认知流程",
            extra={
                "codex_event_kind": self.event_kind,
                "source_byte_range": [self.start_offset, self.end_offset],
                "source_role": self.role,
                "source_is_untrusted_observation": True,
            },
        )


@dataclass(frozen=True)
class CodexSampleBatch:
    source_path: str
    source_size: int
    start_offset: int
    commit_offset: int
    bytes_read: int
    occurrences: tuple[CodexVisibleOccurrence, ...]
    skipped_counts: Mapping[str, int]
    parse_errors: tuple[Mapping[str, Any], ...]
    completeness: str
    warnings: tuple[str, ...] = ()

    @property
    def batch_id(self) -> str:
        identities = "|".join(item.occurrence_id for item in self.occurrences)
        return _stable_id("codex_batch", self.source_path, str(self.start_offset), str(self.commit_offset), identities)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "source_path": self.source_path,
            "source_size": self.source_size,
            "start_offset": self.start_offset,
            "commit_offset": self.commit_offset,
            "bytes_read": self.bytes_read,
            "occurrences": [item.to_dict() for item in self.occurrences],
            "skipped_counts": dict(self.skipped_counts),
            "parse_errors": [dict(item) for item in self.parse_errors],
            "completeness": self.completeness,
            "warnings": list(self.warnings),
        }


class CodexJsonlReceptor:
    """Read a fixed JSONL source in finite batches without mutating it."""

    def __init__(
        self,
        source_path: str | Path,
        *,
        max_bytes: int = MAX_SAMPLE_BYTES,
        max_events: int = MAX_SAMPLE_EVENTS,
    ) -> None:
        self.source_path = Path(source_path).resolve()
        if not self.source_path.is_file():
            raise ContractError("codex_source_file_not_found")
        if not 1024 <= int(max_bytes) <= MAX_SAMPLE_BYTES:
            raise ContractError("codex_sample_byte_budget_out_of_bounds")
        if not 1 <= int(max_events) <= MAX_SAMPLE_EVENTS:
            raise ContractError("codex_sample_event_budget_out_of_bounds")
        self.max_bytes = int(max_bytes)
        self.max_events = int(max_events)

    def _visible(self, raw: Mapping[str, Any], *, start: int, end: int) -> CodexVisibleOccurrence | None:
        timestamp = raw.get("timestamp") if isinstance(raw.get("timestamp"), str) else utc_now()
        outer = raw.get("type")
        payload = raw.get("payload")
        if not isinstance(payload, Mapping):
            return None
        payload_type = payload.get("type")
        if outer == "response_item":
            if payload_type in SKIPPED_RESPONSE_TYPES or payload_type != "message":
                return None
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                return None
            if role == "assistant" and payload.get("channel") not in {None, "final", "commentary"}:
                return None
            text = _content_text(payload.get("content"), str(role))
            if text is None:
                return None
            message_id = payload.get("id") if isinstance(payload.get("id"), str) else f"bytes:{start}-{end}"
            # The daemon owns the absolute source path.  Episodes and browser
            # projections receive only a readable basename plus an opaque,
            # mechanical source identity so local directory names cannot leak.
            # Keep the same opaque identity as the product registry.  Older
            # receipts may contain the historical 16-character prefix; the
            # read-only overview resolves those prefixes only when unique.
            source_key = hashlib.sha256(str(self.source_path).encode("utf-8")).hexdigest()
            source_name = quote(self.source_path.name, safe="._-")
            source_ref = f"codex-jsonl://{source_name}?source={source_key}#bytes={start}-{end}"
            turn_id = None
            metadata = payload.get("internal_chat_message_metadata_passthrough")
            if isinstance(metadata, Mapping) and isinstance(metadata.get("turn_id"), str):
                turn_id = metadata["turn_id"]
            return CodexVisibleOccurrence(
                occurrence_id=_stable_id("codex_occurrence", str(self.source_path), str(message_id), str(start), str(end)),
                role=str(role),
                text=text,
                timestamp=timestamp,
                source_ref=source_ref,
                event_kind="response_item/message",
                start_offset=start,
                end_offset=end,
                extra={"message_id": message_id, "conversation_id": turn_id} if turn_id else {"message_id": message_id},
            )
        if outer == "event_msg" and payload_type in SKIPPED_EVENT_TYPES:
            return None
        return None

    def sample(self, cursor: int | None = None) -> CodexSampleBatch:
        source_size = self.source_path.stat().st_size
        warnings: list[str] = []
        skip_leading_fragment = False
        if cursor is None:
            requested_start = max(0, source_size - self.max_bytes)
            if requested_start:
                warnings.append("initial_tail_window_only")
                skip_leading_fragment = True
        else:
            if isinstance(cursor, bool) or int(cursor) < 0:
                raise ContractError("codex_cursor_invalid")
            requested_start = int(cursor)
        if requested_start > source_size:
            warnings.append("source_truncated_or_rotated")
            requested_start = max(0, source_size - self.max_bytes)
            skip_leading_fragment = bool(requested_start)

        occurrences: list[CodexVisibleOccurrence] = []
        skipped: dict[str, int] = {}
        errors: list[dict[str, Any]] = []
        with self.source_path.open("rb") as handle:
            handle.seek(requested_start)
            if skip_leading_fragment:
                # A tail window can begin mid-line; skip only that incomplete
                # fragment. A committed cursor is always a newline boundary.
                handle.readline(MAX_LINE_BYTES + 1)
            start_offset = handle.tell()
            read_limit = min(source_size, start_offset + self.max_bytes)
            commit_offset = start_offset
            while handle.tell() < read_limit and len(occurrences) < self.max_events:
                line_start = handle.tell()
                line = handle.readline(MAX_LINE_BYTES + 1)
                line_end = handle.tell()
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    skipped["line_too_large"] = skipped.get("line_too_large", 0) + 1
                    errors.append({"start_offset": line_start, "end_offset": line_end, "error": "line_too_large"})
                    commit_offset = line_end
                    continue
                if not line.endswith(b"\n") and line_end >= source_size:
                    warnings.append("trailing_partial_line")
                    break
                try:
                    parsed = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    errors.append({"start_offset": line_start, "end_offset": line_end, "error": "invalid_utf8_json"})
                    commit_offset = line_end
                    continue
                if not isinstance(parsed, Mapping):
                    skipped["non_object"] = skipped.get("non_object", 0) + 1
                    commit_offset = line_end
                    continue
                visible = self._visible(parsed, start=line_start, end=line_end)
                if visible is not None:
                    occurrences.append(visible)
                else:
                    key = f"{parsed.get('type', 'unknown')}/{(parsed.get('payload') or {}).get('type', 'unknown') if isinstance(parsed.get('payload'), Mapping) else 'unknown'}"
                    skipped[key] = skipped.get(key, 0) + 1
                commit_offset = line_end

        budget_incomplete = commit_offset < source_size and (
            commit_offset >= read_limit or len(occurrences) >= self.max_events
        )
        if budget_incomplete:
            warnings.append("search_incomplete")
        completeness = "search_incomplete" if warnings or errors else "complete"
        return CodexSampleBatch(
            source_path=str(self.source_path),
            source_size=source_size,
            start_offset=start_offset,
            commit_offset=commit_offset,
            bytes_read=max(0, commit_offset - start_offset),
            occurrences=tuple(occurrences),
            skipped_counts=skipped,
            parse_errors=tuple(errors[:64]),
            completeness=completeness,
            warnings=tuple(dict.fromkeys(warnings)),
        )


__all__ = [
    "MAX_SAMPLE_BYTES",
    "MAX_SAMPLE_EVENTS",
    "CodexVisibleOccurrence",
    "CodexSampleBatch",
    "CodexJsonlReceptor",
]
