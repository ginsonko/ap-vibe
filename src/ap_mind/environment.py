"""Environment adapter SPI and a local Vibe-shaped fixture.

The fixture is deliberately small: it records an observation and returns a
readback payload.  It is not the live Vibe service and is reported as such by
the runtime probe.  A real connector can replace this class without changing
the AP event/tick contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any, Mapping, Protocol, Sequence

from .contracts import DispatchReceipt, EventEnvelope, ResultEvent, utc_now
from .runtime_types import ActionCandidate


class EnvironmentAdapter(Protocol):
    environment_id: str

    def describe(self) -> Mapping[str, Any]:
        """Return a manifest-like, read-only capability description."""

    def candidates(
        self,
        event: EventEnvelope,
        frame_view: Mapping[str, Any],
    ) -> Sequence[ActionCandidate]:
        """Return bounded action candidates; never select a winner."""

    def dispatch(self, candidate: ActionCandidate, idempotency_key: str) -> DispatchReceipt:
        """Attempt one action and return mechanical dispatch evidence."""

    def readback(self, receipt: DispatchReceipt) -> ResultEvent:
        """Read the physical/environment result, including unknown."""


def stable_idempotency_key(event_id: str, candidate_kind: str) -> str:
    """Create a stable mechanical key; it carries no semantic meaning."""

    digest = hashlib.sha256(f"{event_id}\x00{candidate_kind}".encode("utf-8")).hexdigest()
    return f"idem_{digest}"


@dataclass
class LocalVibeEnvironment:
    """Offline environment used for the first end-to-end AP episode."""

    environment_id: str = "vibe-local-fixture"
    records: list[dict[str, Any]] = field(default_factory=list)
    fail_dispatch: bool = False
    fail_readback: bool = False

    def describe(self) -> Mapping[str, Any]:
        return {
            "environment_id": self.environment_id,
            "name": "Vibe local fixture",
            "live": False,
            "observations": ["text", "state", "readback"],
            "actions": ["record_observation", "defer"],
            "permissions": ["local_fixture_memory_only"],
            "readback": True,
        }

    def candidates(
        self,
        event: EventEnvelope,
        frame_view: Mapping[str, Any],
    ) -> Sequence[ActionCandidate]:
        # A readback is a new reality for cognition, not a request to repeat
        # the physical action that produced it.  It still enters the AP flow
        # (SA/B/C/feelings/thought), but this fixture has no follow-up action
        # affordance for a result.  Real environments may expose a distinct
        # follow-up candidate with its own causal/idempotency key.
        if event.source in {"readback", "internal"} or event.role == "result":
            return ()
        sa = frame_view.get("sa", {})
        if not isinstance(sa, Mapping):
            sa = {}
        completeness = event.completeness
        evidence_fit = 1.0 if completeness == "complete" else 0.45 if completeness == "partial" else 0.15
        novelty = float(sa.get("novelty", 0.0) or 0.0)
        uncertainty = float(frame_view.get("uncertainty", 0.0) or 0.0)
        pressure = float(frame_view.get("pressure", 0.0) or 0.0)
        common = {
            "event_ref": event.event_id,
            "environment_id": self.environment_id,
        }
        return (
            ActionCandidate(
                candidate_id=f"action_{event.event_id}_record",
                kind="record_observation",
                target="vibe.project_memory",
                proposition="保留这次环境事件，供后续认知 tick 召回",
                components={
                    "goal_fit": 0.62,
                    "evidence_fit": evidence_fit,
                    "novelty": novelty,
                    "closure_gain": 0.35,
                    "uncertainty_cost": 1.0 - uncertainty,
                    "pressure_relief": 0.25 + pressure * 0.2,
                    "risk_cost": 0.02,
                },
                expected_outcome={"recorded": True, **common},
                source="environment",
                owner="ap_native",
                idempotency_key=stable_idempotency_key(event.event_id, "record_observation"),
            ),
            ActionCandidate(
                candidate_id=f"action_{event.event_id}_defer",
                kind="defer",
                target="vibe.project_memory",
                proposition="暂不做外部改变，保留未闭合状态",
                components={
                    "goal_fit": 0.24,
                    "evidence_fit": 1.0 - evidence_fit,
                    "novelty": 0.15 + novelty * 0.35,
                    "closure_gain": 0.05,
                    "uncertainty_cost": uncertainty,
                    "pressure_relief": 0.1,
                    "risk_cost": 0.0,
                },
                expected_outcome={"deferred": True, **common},
                source="environment",
                owner="ap_native",
                idempotency_key=stable_idempotency_key(event.event_id, "defer"),
            ),
        )

    def dispatch(self, candidate: ActionCandidate, idempotency_key: str) -> DispatchReceipt:
        now = utc_now()
        if self.fail_dispatch:
            return DispatchReceipt(
                action_ref=candidate.candidate_id,
                environment_id=self.environment_id,
                idempotency_key=idempotency_key,
                status="unknown",
                accepted_at=now,
                connector_ref="vibe-local-fixture",
                error_code="fixture_dispatch_unavailable",
                retryable=True,
            )
        if candidate.kind == "record_observation":
            self.records.append(
                {
                    "action_ref": candidate.candidate_id,
                    "target": candidate.target,
                    "proposition": candidate.proposition,
                    "recorded_at": now,
                }
            )
        return DispatchReceipt(
            action_ref=candidate.candidate_id,
            environment_id=self.environment_id,
            idempotency_key=idempotency_key,
            status="accepted",
            accepted_at=now,
            connector_ref="vibe-local-fixture",
        )

    def readback(self, receipt: DispatchReceipt) -> ResultEvent:
        if self.fail_readback:
            return ResultEvent(
                receipt_ref=receipt.receipt_id,
                action_ref=receipt.action_ref,
                environment_id=self.environment_id,
                status="unknown",
                observed_at=utc_now(),
                completeness="unknown",
                error_code="fixture_readback_unavailable",
                retryable=True,
            )
        if receipt.status not in {"accepted", "attempted"}:
            return ResultEvent(
                receipt_ref=receipt.receipt_id,
                action_ref=receipt.action_ref,
                environment_id=self.environment_id,
                status="unknown",
                observed_at=utc_now(),
                completeness="unknown",
                error_code=receipt.error_code or "dispatch_not_accepted",
                retryable=receipt.retryable,
            )
        return ResultEvent(
            receipt_ref=receipt.receipt_id,
            action_ref=receipt.action_ref,
            environment_id=self.environment_id,
            status="success",
            observed_at=utc_now(),
            payload_inline={
                "action_ref": receipt.action_ref,
                "record_count": len(self.records),
                "fixture": True,
            },
            evidence_refs=(f"readback:{receipt.receipt_id}",),
            completeness="complete",
            lineage_refs=(receipt.receipt_id, receipt.action_ref),
        )


__all__ = ["EnvironmentAdapter", "LocalVibeEnvironment", "stable_idempotency_key"]
