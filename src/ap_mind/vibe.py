"""Replaceable Vibe project receptor and actuator.

The live Vibe service is intentionally *not* required by the AP core.  This
module contains a small HTTP SPI plus a project-scoped adapter that turns
activity/knowledge responses into source-tagged ``EventEnvelope`` objects and
turns explicitly approved progress commands into mechanical dispatch/readback
receipts.  A fake transport can exercise the same contract without network
access.

The adapter never writes StatePool, selects a winner, or treats a model
proposal as project truth.  It is an environment organ behind the one AP
ActionArena.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping, Protocol, Sequence
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from uuid import uuid4

from .contracts import DispatchReceipt, EventEnvelope, ResultEvent, utc_now
from .environment import EnvironmentAdapter
from .runtime_types import ActionCandidate, TickResult
from .storage import EventStore
from .runtime import MindRuntime


class VibeTransportError(RuntimeError):
    """A bounded connector failure with a safe, short error code."""


class VibeTransport(Protocol):
    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        max_response_bytes: int = 4_000_000,
    ) -> Mapping[str, Any]:
        """Perform one bounded JSON request and return an object response."""


class UrllibVibeTransport:
    """Minimal JSON transport for a configured Vibe base URL.

    Authentication is supplied as a session header by the caller.  Neither
    the header nor response body is included in exceptions or logs.
    """

    def __init__(self, base_url: str, *, session_token: str | None = None) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("vibe_base_url_required")
        self.base_url = base_url.rstrip("/")
        self._session_token = session_token

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        max_response_bytes: int = 4_000_000,
    ) -> Mapping[str, Any]:
        if not isinstance(path, str) or not path.startswith("/"):
            raise VibeTransportError("vibe_path_must_be_absolute")
        if timeout <= 0 or timeout > 120:
            raise VibeTransportError("vibe_timeout_out_of_bounds")
        if max_response_bytes < 1024 or max_response_bytes > 16_000_000:
            raise VibeTransportError("vibe_response_bound_out_of_bounds")
        request_headers = {"Accept": "application/json", **dict(headers or {})}
        if self._session_token:
            request_headers.setdefault("x-yv-session", self._session_token)
        data = None
        if body is not None:
            try:
                data = json.dumps(dict(body), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise VibeTransportError("vibe_request_not_json_serializable") from exc
            request_headers["Content-Type"] = "application/json"
        req = urllib_request.Request(
            self.base_url + path,
            data=data,
            headers=request_headers,
            method=method.upper(),
        )
        try:
            with urllib_request.urlopen(req, timeout=timeout) as response:  # nosec B310 - configured connector URL
                raw = response.read(max_response_bytes + 1)
                if len(raw) > max_response_bytes:
                    raise VibeTransportError("vibe_response_exceeds_bound")
                decoded = raw.decode("utf-8")
                parsed = json.loads(decoded) if decoded.strip() else {}
        except urllib_error.HTTPError as exc:
            raise VibeTransportError(f"vibe_http_{exc.code}") from exc
        except (urllib_error.URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VibeTransportError(f"vibe_transport_{type(exc).__name__}") from exc
        if not isinstance(parsed, Mapping):
            raise VibeTransportError("vibe_response_not_object")
        return parsed


def _copy_bounded(value: Any, *, depth: int = 0, max_depth: int = 4, max_items: int = 128) -> Any:
    if depth >= max_depth:
        return str(value)[:2048]
    if isinstance(value, Mapping):
        return {
            str(key)[:128]: _copy_bounded(item, depth=depth + 1, max_depth=max_depth, max_items=max_items)
            for key, item in list(value.items())[:max_items]
        }
    if isinstance(value, (list, tuple)):
        return [
            _copy_bounded(item, depth=depth + 1, max_depth=max_depth, max_items=max_items)
            for item in list(value)[:max_items]
        ]
    if isinstance(value, str):
        return value[:8192]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:2048]


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _first_text(record: Mapping[str, Any], names: Sequence[str]) -> str | None:
    for name in names:
        value = record.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return None


def _records_from_response(response: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], str | None, bool]:
    """Extract a bounded list without assigning meaning to list order."""

    if isinstance(response, Mapping):
        for key in ("items", "activities", "events", "data", "results"):
            value = response.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                records = [item for item in value if isinstance(item, Mapping)]
                cursor = _first_text(response, ("nextCursor", "next_cursor", "cursor", "revision"))
                partial = bool(response.get("partial") or response.get("truncated"))
                return records, cursor, partial
        # A single activity object is also accepted.  This keeps the adapter
        # compatible with small local transports and does not use key names as
        # semantic routing; it only detects an object-shaped response.
        if any(key in response for key in ("eventId", "event_id", "activityId", "activity_id", "summary", "message")):
            return [response], _first_text(response, ("nextCursor", "next_cursor", "cursor", "revision")), False
    return [], None, True


@dataclass(frozen=True)
class VibePage:
    records: tuple[Mapping[str, Any], ...]
    next_cursor: str | None = None
    partial: bool = False
    source_ref: str = ""


@dataclass
class VibeProjectClient:
    """Project-scoped Vibe API client with explicit read/write capability."""

    project_id: str
    conversation_id: str | None = None
    base_url: str = "http://127.0.0.1:4317"
    transport: VibeTransport | None = None
    allow_writes: bool = False
    runtime_id: str = "runtime-local"
    organism_id: str = "organism-local"
    max_records: int = 80
    max_response_bytes: int = 4_000_000
    paths: Mapping[str, str] = field(default_factory=dict)
    _remote_write_allowed: bool | None = field(default=None, init=False, repr=False)
    _last_bootstrap: Mapping[str, Any] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise ValueError("vibe_project_id_required")
        if self.conversation_id is not None and (not isinstance(self.conversation_id, str) or not self.conversation_id.strip()):
            raise ValueError("vibe_conversation_id_invalid")
        if isinstance(self.max_records, bool) or self.max_records < 1 or self.max_records > 200:
            raise ValueError("vibe_max_records_out_of_bounds")
        if self.max_response_bytes < 1024 or self.max_response_bytes > 16_000_000:
            raise ValueError("vibe_response_bound_out_of_bounds")
        if self.transport is None:
            self.transport = UrllibVibeTransport(self.base_url)

    @property
    def environment_id(self) -> str:
        return f"vibe-project:{self.project_id}"

    @property
    def writes_available(self) -> bool:
        return bool(self.allow_writes and self._remote_write_allowed is True)

    def _path(self, key: str, default: str) -> str:
        template = self.paths.get(key, default)
        try:
            return template.format(
                project_id=urllib_parse.quote(self.project_id, safe=""),
                conversation_id=urllib_parse.quote(self.conversation_id or "", safe=""),
            )
        except (KeyError, ValueError) as exc:
            raise VibeTransportError(f"vibe_path_template_invalid:{key}") from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        assert self.transport is not None
        return self.transport.request_json(
            method,
            path,
            body=body,
            headers=headers,
            timeout=10.0,
            max_response_bytes=self.max_response_bytes,
        )

    def bootstrap(self) -> Mapping[str, Any]:
        if self.conversation_id:
            path = self._path("bootstrap", "/api/agent-sessions/{conversation_id}/bootstrap")
        else:
            path = self._path("bootstrap", "/api/projects/{project_id}/bootstrap")
        response = self._request("GET", path)
        self._last_bootstrap = _copy_bounded(response)
        capability = response.get("writeCapability") or response.get("write_capability")
        allowed = capability.get("allowed") if isinstance(capability, Mapping) else None
        self._remote_write_allowed = bool(allowed) if isinstance(allowed, bool) else False
        return response

    def knowledge(self, sections: Sequence[str] = ("status", "work", "recovery")) -> Mapping[str, Any]:
        clean = [str(item).strip() for item in sections if isinstance(item, str) and item.strip()][:16]
        path = self._path("knowledge", "/api/projects/{project_id}/knowledge")
        if clean:
            path += "?sections=" + urllib_parse.quote(",".join(clean), safe="")
        return self._request("GET", path)

    def activity(self, *, cursor: str | None = None, limit: int | None = None) -> VibePage:
        effective_limit = self.max_records if limit is None else int(limit)
        if effective_limit < 1 or effective_limit > self.max_records:
            raise ValueError("vibe_activity_limit_out_of_bounds")
        path = self._path("activity", "/api/activities")
        query = {"limit": str(effective_limit), "projectId": self.project_id}
        if cursor:
            query["cursor"] = cursor
        path += "?" + urllib_parse.urlencode(query)
        response = self._request("GET", path)
        records, next_cursor, partial = _records_from_response(response)
        return VibePage(tuple(records[:effective_limit]), next_cursor, partial, source_ref="activity")

    def _record_ref(self, record: Mapping[str, Any]) -> str:
        direct = _first_text(
            record,
            ("eventId", "event_id", "activityId", "activity_id", "receiptId", "receipt_id", "revision", "cursor"),
        )
        return direct or _hash(_copy_bounded(record))

    def activity_events(self, *, cursor: str | None = None, limit: int | None = None) -> tuple[tuple[EventEnvelope, ...], str | None, bool]:
        page = self.activity(cursor=cursor, limit=limit)
        events: list[EventEnvelope] = []
        for record in page.records:
            source_ref = self._record_ref(record)
            event_key = f"vibe:{self.project_id}:activity:{source_ref}"
            event_id = "evt_" + _hash(event_key)[:32]
            completeness = record.get("completeness")
            if completeness not in {"complete", "partial", "unknown", "search_incomplete"}:
                completeness = "partial" if page.partial else "complete"
            observed_at = _first_text(record, ("observedAt", "observed_at", "occurredAt", "occurred_at", "timestamp")) or utc_now()
            events.append(
                EventEnvelope(
                    event_id=event_id,
                    runtime_id=self.runtime_id,
                    organism_id=self.organism_id,
                    environment_id=self.environment_id,
                    episode_id=f"vibe-activity:{self.project_id}",
                    source="external",
                    role="observation",
                    modality="project_activity",
                    occurred_at=observed_at,
                    observed_at=observed_at,
                    payload_inline=_copy_bounded(record),
                    evidence_refs=(f"vibe:{self.project_id}:activity:{source_ref}",),
                    lineage_refs=(f"vibe:project:{self.project_id}", source_ref),
                    privacy_scope="project",
                    completeness=str(completeness),
                    idempotency_key=event_key,
                    # The page cursor is polling transport state, not part of
                    # the activity's meaning.  Keep it out of the canonical
                    # event so the same activity remains replayable when a
                    # later poll reports a different cursor.
                    extra={"vibe_source_ref": source_ref},
                )
            )
        return tuple(events), page.next_cursor, page.partial

    def knowledge_event(self, sections: Sequence[str] = ("status", "work", "recovery")) -> EventEnvelope:
        response = self.knowledge(sections)
        source_ref = "knowledge:" + _hash(response)[:32]
        return EventEnvelope(
            event_id="evt_" + _hash(f"vibe:{self.project_id}:{source_ref}")[:32],
            runtime_id=self.runtime_id,
            organism_id=self.organism_id,
            environment_id=self.environment_id,
            episode_id=f"vibe-knowledge:{self.project_id}",
            source="external",
            role="observation",
            modality="project_knowledge",
            payload_inline=_copy_bounded(response),
            evidence_refs=(f"vibe:{self.project_id}:{source_ref}",),
            lineage_refs=(f"vibe:project:{self.project_id}", source_ref),
            privacy_scope="project",
            completeness="complete",
            idempotency_key=f"vibe:{self.project_id}:{source_ref}",
            extra={"vibe_source_ref": source_ref},
        )

    def write_progress(self, payload: Mapping[str, Any], *, idempotency_key: str, action_ref: str) -> DispatchReceipt:
        if not self.allow_writes:
            return DispatchReceipt(
                action_ref=action_ref,
                environment_id=self.environment_id,
                idempotency_key=idempotency_key,
                status="rejected",
                connector_ref="vibe-project-client",
                error_code="vibe_writes_disabled",
                retryable=False,
            )
        if self._remote_write_allowed is not True:
            return DispatchReceipt(
                action_ref=action_ref,
                environment_id=self.environment_id,
                idempotency_key=idempotency_key,
                status="unknown",
                connector_ref="vibe-project-client",
                error_code="vibe_write_capability_unconfirmed",
                retryable=True,
            )
        if not self.conversation_id:
            return DispatchReceipt(
                action_ref=action_ref,
                environment_id=self.environment_id,
                idempotency_key=idempotency_key,
                status="rejected",
                connector_ref="vibe-project-client",
                error_code="vibe_conversation_required_for_progress",
                retryable=False,
            )
        path = self._path("progress", "/api/agent-sessions/{conversation_id}/progress")
        try:
            response = self._request(
                "POST",
                path,
                body=_copy_bounded(payload),
                headers={"Idempotency-Key": idempotency_key},
            )
        except VibeTransportError as exc:
            return DispatchReceipt(
                action_ref=action_ref,
                environment_id=self.environment_id,
                idempotency_key=idempotency_key,
                status="unknown",
                connector_ref="vibe-project-client",
                error_code=str(exc),
                retryable=True,
            )
        # HTTP acceptance is mechanical receipt only.  Explicit body `ok` or
        # success status is required before readback can close the frontier.
        body_status = response.get("status")
        accepted = response.get("ok") is True or body_status in {"accepted", "success", "completed"}
        return DispatchReceipt(
            action_ref=action_ref,
            environment_id=self.environment_id,
            idempotency_key=idempotency_key,
            status="accepted" if accepted else "unknown",
            connector_ref="vibe-project-client",
            error_code=None if accepted else "vibe_write_readback_unconfirmed",
            retryable=not accepted,
            evidence_refs=(f"vibe:{self.project_id}:progress:{idempotency_key}",),
            extra={"vibe_response": _copy_bounded(response)},
        )

    def read_progress(self, receipt: DispatchReceipt) -> ResultEvent:
        response = receipt.extra.get("vibe_response") if isinstance(receipt.extra, Mapping) else None
        if receipt.status != "accepted":
            return ResultEvent(
                receipt_ref=receipt.receipt_id,
                action_ref=receipt.action_ref,
                environment_id=self.environment_id,
                status="unknown",
                completeness="unknown",
                error_code=receipt.error_code or "vibe_dispatch_not_accepted",
                retryable=receipt.retryable,
            )
        return ResultEvent(
            receipt_ref=receipt.receipt_id,
            action_ref=receipt.action_ref,
            environment_id=self.environment_id,
            status="success" if isinstance(response, Mapping) else "unknown",
            payload_inline=_copy_bounded(response) if isinstance(response, Mapping) else None,
            evidence_refs=receipt.evidence_refs,
            completeness="complete" if isinstance(response, Mapping) else "unknown",
            lineage_refs=(receipt.receipt_id, receipt.action_ref),
            error_code=None if isinstance(response, Mapping) else "vibe_progress_response_missing",
            retryable=not isinstance(response, Mapping),
        )


@dataclass
class VibeEnvironment:
    """EnvironmentAdapter backed by a VibeProjectClient."""

    client: VibeProjectClient
    environment_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.client, VibeProjectClient):
            raise TypeError("vibe_environment_requires_project_client")
        if self.environment_id is None:
            self.environment_id = self.client.environment_id

    def describe(self) -> Mapping[str, Any]:
        return {
            "environment_id": self.environment_id,
            "name": "Vibe project",
            "project_id": self.client.project_id,
            "conversation_id": self.client.conversation_id,
            "live": True,
            "observations": ["project_activity", "project_knowledge", "readback"],
            "actions": ["observe_only", "defer", "publish_progress"],
            "write_enabled": self.client.writes_available,
            "write_requested": self.client.allow_writes,
            "readback": True,
        }

    def candidates(self, event: EventEnvelope, frame_view: Mapping[str, Any]) -> Sequence[ActionCandidate]:
        if event.source in {"readback", "internal"} or event.role == "result":
            return ()
        sa = frame_view.get("sa") if isinstance(frame_view, Mapping) else {}
        if not isinstance(sa, Mapping):
            sa = {}
        novelty = max(0.0, min(1.0, float(sa.get("novelty", 0.0) or 0.0)))
        uncertainty = max(0.0, min(1.0, float(frame_view.get("uncertainty", 0.0) or 0.0)))
        common = {"event_ref": event.event_id, "project_id": self.client.project_id}
        candidates: list[ActionCandidate] = [
            ActionCandidate(
                candidate_id=f"action_{event.event_id}_observe",
                kind="observe_only",
                target=f"vibe.project:{self.client.project_id}",
                proposition="保留并观察这次 Vibe 事件，不改变项目状态",
                components={
                    "goal_fit": 0.28,
                    "evidence_fit": 1.0 - uncertainty,
                    "novelty": novelty,
                    "closure_gain": 0.16,
                    "uncertainty_cost": 1.0 - uncertainty,
                    "pressure_relief": 0.18,
                    "risk_cost": 0.0,
                },
                expected_outcome={"observed": True, **common},
                source="environment",
                owner="ap_native",
                idempotency_key=f"vibe-observe:{self.client.project_id}:{event.event_id}",
            ),
            ActionCandidate(
                candidate_id=f"action_{event.event_id}_defer",
                kind="defer",
                target=f"vibe.project:{self.client.project_id}",
                proposition="暂不改变项目，保留未闭合状态",
                components={
                    "goal_fit": 0.20,
                    "evidence_fit": uncertainty,
                    "novelty": novelty * 0.25,
                    "closure_gain": 0.04,
                    "uncertainty_cost": uncertainty,
                    "pressure_relief": 0.08,
                    "risk_cost": 0.0,
                },
                expected_outcome={"deferred": True, **common},
                source="environment",
                owner="ap_native",
                idempotency_key=f"vibe-defer:{self.client.project_id}:{event.event_id}",
            ),
        ]
        write_approved = bool(event.role == "command" and event.extra.get("vibe_write_approved") is True)
        payload = event.payload_inline if isinstance(event.payload_inline, Mapping) else None
        if self.client.writes_available and write_approved and isinstance(payload, Mapping):
            candidates.append(
                ActionCandidate(
                    candidate_id=f"action_{event.event_id}_progress",
                    kind="publish_progress",
                    target=f"vibe.project:{self.client.project_id}",
                    proposition="发布用户明确批准的项目进度",
                    components={
                        "goal_fit": 0.82,
                        "evidence_fit": 1.0 - uncertainty,
                        "novelty": novelty * 0.4,
                        "closure_gain": 0.72,
                        "uncertainty_cost": 1.0 - uncertainty,
                        "pressure_relief": 0.58,
                        "risk_cost": 0.08,
                    },
                    expected_outcome={"progress_payload": _copy_bounded(payload), **common},
                    source="environment",
                    owner="ap_native",
                    idempotency_key=f"vibe-progress:{self.client.project_id}:{event.event_id}",
                )
            )
        return tuple(candidates)

    def dispatch(self, candidate: ActionCandidate, idempotency_key: str) -> DispatchReceipt:
        if candidate.kind == "publish_progress":
            payload = candidate.expected_outcome.get("progress_payload")
            if not isinstance(payload, Mapping):
                return DispatchReceipt(
                    action_ref=candidate.candidate_id,
                    environment_id=str(self.environment_id),
                    idempotency_key=idempotency_key,
                    status="rejected",
                    connector_ref="vibe-project-client",
                    error_code="vibe_progress_payload_missing",
                    retryable=False,
                )
            return self.client.write_progress(payload, idempotency_key=idempotency_key, action_ref=candidate.candidate_id)
        return DispatchReceipt(
            action_ref=candidate.candidate_id,
            environment_id=str(self.environment_id),
            idempotency_key=idempotency_key,
            status="accepted",
            connector_ref="vibe-project-client",
            evidence_refs=(f"vibe:{self.client.project_id}:local-observation:{candidate.candidate_id}",),
            extra={"local_only": True, "kind": candidate.kind},
        )

    def readback(self, receipt: DispatchReceipt) -> ResultEvent:
        if receipt.action_ref.endswith("_progress") or receipt.extra.get("kind") == "publish_progress":
            return self.client.read_progress(receipt)
        if receipt.status != "accepted":
            return ResultEvent(
                receipt_ref=receipt.receipt_id,
                action_ref=receipt.action_ref,
                environment_id=str(self.environment_id),
                status="unknown",
                completeness="unknown",
                error_code=receipt.error_code or "vibe_local_dispatch_not_accepted",
                retryable=receipt.retryable,
            )
        return ResultEvent(
            receipt_ref=receipt.receipt_id,
            action_ref=receipt.action_ref,
            environment_id=str(self.environment_id),
            status="success",
            payload_inline={"local_only": True, "action_ref": receipt.action_ref},
            evidence_refs=receipt.evidence_refs,
            completeness="complete",
            lineage_refs=(receipt.receipt_id, receipt.action_ref),
        )


@dataclass(frozen=True)
class VibeSyncReport:
    """Bounded, inspectable result of one activity synchronization pass."""

    receptor_key: str
    status: str
    pages: int
    events_seen: int
    frames_created: int
    cursor_before: str | None
    cursor_after: str | None
    partial: bool = False
    error_code: str | None = None
    next_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "receptor_key": self.receptor_key,
            "status": self.status,
            "pages": self.pages,
            "events_seen": self.events_seen,
            "frames_created": self.frames_created,
            "cursor_before": self.cursor_before,
            "cursor_after": self.cursor_after,
            "partial": self.partial,
            "error_code": self.error_code,
            "next_action": self.next_action,
        }


@dataclass
class VibeRuntimeDriver:
    """Drive Vibe observations through one ``MindRuntime`` with a cursor fence."""

    client: VibeProjectClient
    runtime: MindRuntime
    store: EventStore
    max_pages: int = 4
    max_events: int = 80

    def __post_init__(self) -> None:
        if not isinstance(self.client, VibeProjectClient):
            raise TypeError("vibe_driver_requires_project_client")
        # Accept a protocol-compatible runtime (including a test double or a
        # future hosted runtime) rather than coupling the connector to one
        # concrete class.  The only authority it needs is ``tick(event)``.
        if not callable(getattr(self.runtime, "tick", None)):
            raise TypeError("vibe_driver_requires_mind_runtime")
        if not isinstance(self.store, EventStore):
            raise TypeError("vibe_driver_requires_event_store")
        if isinstance(self.max_pages, bool) or self.max_pages < 1 or self.max_pages > 32:
            raise ValueError("vibe_driver_max_pages_out_of_bounds")
        if isinstance(self.max_events, bool) or self.max_events < 1 or self.max_events > 1000:
            raise ValueError("vibe_driver_max_events_out_of_bounds")

    @property
    def receptor_key(self) -> str:
        return f"vibe.activity:{self.client.project_id}:{self.client.conversation_id or '-'}"

    def sync_activity(self) -> VibeSyncReport:
        prior = self.store.get_receptor_cursor(self.receptor_key) or {}
        cursor_before = prior.get("cursor") if isinstance(prior.get("cursor"), str) else None
        cursor = cursor_before
        pages = 0
        events_seen = 0
        frames_created = 0
        partial = False
        for _ in range(self.max_pages):
            if events_seen >= self.max_events:
                partial = True
                return VibeSyncReport(
                    self.receptor_key, "search_incomplete", pages, events_seen, frames_created,
                    cursor_before, cursor, partial, "activity_event_budget_exhausted", "继续同步前提高预算或稍后重试",
                )
            try:
                events, next_cursor, page_partial = self.client.activity_events(cursor=cursor, limit=min(self.max_events - events_seen, self.client.max_records))
            except VibeTransportError as exc:
                return VibeSyncReport(
                    self.receptor_key, "unknown", pages, events_seen, frames_created,
                    cursor_before, cursor, partial, str(exc), "检查 Vibe 连接后重试；旧游标未推进",
                )
            pages += 1
            if not events:
                partial = partial or page_partial
                break
            for event in events:
                try:
                    before = self.store.get_frame_for_event(event.event_id)
                    self.runtime.tick(event)
                    after = self.store.get_frame_for_event(event.event_id)
                    if before is None and after is not None:
                        frames_created += 1
                    events_seen += 1
                except Exception as exc:
                    # Do not advance the cursor past an event whose AP frame
                    # could not be durably formed.
                    return VibeSyncReport(
                        self.receptor_key, "partial", pages, events_seen, frames_created,
                        cursor_before, cursor, True, f"runtime_event_failed:{type(exc).__name__}", "修复该事件后从旧游标安全重试",
                    )
            partial = partial or page_partial
            if next_cursor is None or next_cursor == cursor:
                break
            cursor = next_cursor
        else:
            partial = True
        status = "partial" if partial else "ok"
        try:
            self.store.save_receptor_cursor(
                self.receptor_key,
                cursor=cursor,
                revision=None,
                status=status,
                payload={"pages": pages, "events_seen": events_seen},
                updated_at=utc_now(),
            )
        except Exception as exc:
            return VibeSyncReport(
                self.receptor_key, "partial", pages, events_seen, frames_created,
                cursor_before, cursor, True, f"cursor_persist_failed:{type(exc).__name__}", "检查本地存储后重试；认知事件已保留",
            )
        return VibeSyncReport(
            self.receptor_key, status, pages, events_seen, frames_created,
            cursor_before, cursor, partial,
            None,
            "继续轮询" if partial else None,
        )

    def sync_knowledge(self, sections: Sequence[str] = ("status", "work", "recovery")) -> TickResult:
        """Ingest one knowledge snapshot through the same AP tick path."""

        return self.runtime.tick(self.client.knowledge_event(sections))


__all__ = [
    "VibeTransport",
    "VibeTransportError",
    "UrllibVibeTransport",
    "VibePage",
    "VibeProjectClient",
    "VibeEnvironment",
    "VibeSyncReport",
    "VibeRuntimeDriver",
]
