"""Provider-free event codec used by the first AP vertical slice.

The codec does not decide what an event *means*.  It exposes a bounded,
human-readable surface and a few generic features so the runtime can form SA,
recall and prediction candidates.  A multimodal/LLM codec can implement the
same function later and retain the original payload reference.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping
from uuid import uuid4

from .contracts import ContractError, EventEnvelope
from .runtime_types import SAOccurrence


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff]")
_TEXT_KEYS = ("text", "content", "message", "summary", "title", "value")


def _text_from_payload(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        for key in _TEXT_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        # A stable, bounded representation keeps structured state observable
        # without treating any key as a semantic answer route.
        try:
            return json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)[:4096]
        except (TypeError, ValueError):
            return ""
    try:
        return str(payload)[:4096]
    except Exception:
        return ""


def tokenize(text: str, *, max_tokens: int = 256) -> tuple[str, ...]:
    if not isinstance(text, str):
        raise ContractError("codec_text_must_be_text")
    return tuple(_TOKEN_RE.findall(text[:8192])[:max_tokens])


def decode_event(event: EventEnvelope, *, prior_tokens: set[str] | None = None) -> SAOccurrence:
    """Decode one event into a source-grounded SA occurrence."""

    if not isinstance(event, EventEnvelope):
        raise ContractError("codec_accepts_event_envelopes_only")
    text = _text_from_payload(event.payload_inline)
    if not text and event.payload_ref:
        text = f"[{event.modality} payload {event.payload_ref}]"
    tokens = tokenize(text)
    current = set(tokens)
    prior = prior_tokens or set()
    overlap = len(current.intersection(prior)) / max(1, len(current.union(prior)))
    novelty = round(1.0 - overlap, 6)
    features: dict[str, Any] = {
        "modality": event.modality,
        "token_count": len(tokens),
        "has_payload_ref": bool(event.payload_ref),
        "source": event.source,
        "role": event.role,
        "completeness": event.completeness,
    }
    if isinstance(event.payload_inline, Mapping):
        features["payload_fields"] = sorted(str(key) for key in event.payload_inline.keys())[:32]
        for key in ("x", "y", "z", "width", "height", "duration_ms"):
            value = event.payload_inline.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                features[key] = value
    return SAOccurrence(
        occurrence_id=f"sa_{uuid4().hex}",
        event_ref=event.event_id,
        modality=event.modality,
        text=text,
        features=features,
        tokens=tokens,
        source=event.source,
        evidence_refs=event.evidence_refs,
        lineage_refs=tuple(dict.fromkeys((*event.lineage_refs, event.event_id))),
        activation=1.0,
        novelty=novelty,
    )


__all__ = ["decode_event", "tokenize"]
