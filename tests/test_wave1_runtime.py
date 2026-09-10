from __future__ import annotations

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from ap_mind import (
    CapabilityOwnership,
    ContractError,
    DelegationDecision,
    EventEnvelope,
    EventStore,
    GatewayProposal,
    LocalVibeEnvironment,
    MindRuntime,
    OpenAICompatibleGateway,
    VibeEnvironment,
    VibeProjectClient,
    VibeRuntimeDriver,
)
from ap_mind.governance import GovernanceCompatibilityRecord

SHA = "440EA59A70902B0DB2384C7EE066B7EB25387299C8175321127172AF6AD19A39"


def _event(runtime: MindRuntime, env: LocalVibeEnvironment, *, text: str, key: str, episode: str = "wave1") -> EventEnvelope:
    return EventEnvelope(
        runtime_id=runtime.runtime_id,
        organism_id=runtime.organism_id,
        environment_id=env.environment_id,
        episode_id=episode,
        payload_inline={"text": text},
        evidence_refs=(f"user:{key}",),
        idempotency_key=key,
    )


def test_fresh_transport_retry_with_same_key_reuses_canonical_frame(tmp_path: Path) -> None:
    env = LocalVibeEnvironment()
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(store, env)
        first = runtime.tick(_event(runtime, env, text="项目进度已更新", key="input-1"))
        counts = {name: store.count(name) for name in ("events", "frames", "dispatches", "results")}

        # A connector retry commonly creates a new event_id and timestamps.
        retry = _event(runtime, env, text="项目进度已更新", key="input-1")
        second = runtime.tick(retry)

        assert second.recovered is True
        assert second.frame.frame_id == first.frame.frame_id
        assert second.receipt is not None and first.receipt is not None
        assert second.receipt.receipt_id == first.receipt.receipt_id
        assert second.result is not None and first.result is not None
        assert second.result.result_id == first.result.result_id
        assert {name: store.count(name) for name in counts} == counts


def test_idempotency_key_with_changed_payload_is_a_real_conflict(tmp_path: Path) -> None:
    env = LocalVibeEnvironment()
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(store, env)
        runtime.tick(_event(runtime, env, text="原始进度", key="input-conflict"))
        with pytest.raises(ContractError, match="event_idempotency_conflict"):
            runtime.tick(_event(runtime, env, text="被篡改的进度", key="input-conflict"))
        assert store.count("frames") == 1
        assert store.count("dispatches") == 1


def test_readback_is_a_new_cognitive_frame_but_cannot_self_dispatch(tmp_path: Path) -> None:
    env = LocalVibeEnvironment()
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(store, env)
        original = runtime.tick(_event(runtime, env, text="记录一个观察", key="input-readback"))
        assert original.result_envelope is not None

        readback_frame = runtime.tick(original.result_envelope)
        assert readback_frame.frame.tick_index == original.frame.tick_index + 1
        assert readback_frame.frame.sa.source == "readback"
        assert readback_frame.receipt is None
        assert readback_frame.result is None
        assert readback_frame.frame.decision["status"] == "abstained"
        assert store.count("dispatches") == 1

        replay = runtime.tick(original.result_envelope)
        assert replay.recovered is True
        assert replay.frame.frame_id == readback_frame.frame.frame_id
        assert store.count("frames") == 2
        assert store.count("events") == 2  # input and one projected readback; no loop event


@pytest.mark.parametrize("failure", ["dispatch", "readback"])
def test_failure_keeps_result_unknown_and_frontier_open(tmp_path: Path, failure: str) -> None:
    env = LocalVibeEnvironment(fail_dispatch=failure == "dispatch", fail_readback=failure == "readback")
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(store, env)
        result = runtime.tick(_event(runtime, env, text="需要外部记录", key=f"input-{failure}"))
        assert result.receipt is not None
        assert result.result is not None
        assert result.result.status in {"unknown", "failed"}
        assert result.result.completeness in {"unknown", "partial"}
        assert result.frame.phase_completeness["readback"] in {"unknown", "partial"}
        assert result.frame.frontiers[0]["status"] == "open"
        assert result.frame.frontiers[0]["closure"] == "open"
        assert result.frame.decision["result_status"] == result.result.status
        assert result.frame.decision["result_completeness"] == result.result.completeness
        assert result.frame.completeness in {"unknown", "partial"}

        # The projected result can be observed without turning into another
        # physical record action.
        assert result.result_envelope is not None
        follow_up = runtime.tick(result.result_envelope)
        assert follow_up.receipt is None
        assert follow_up.frame.decision["status"] == "abstained"
        assert store.count("dispatches") == 1


def test_replay_after_cold_restart_does_not_dispatch_again(tmp_path: Path) -> None:
    db = tmp_path / "mind.sqlite"
    env = LocalVibeEnvironment()
    event: EventEnvelope
    with EventStore(db) as store:
        runtime = MindRuntime(store, env)
        event = _event(runtime, env, text="冷重启前的观察", key="input-restart")
        first = runtime.tick(event)
        counts = {name: store.count(name) for name in ("events", "frames", "dispatches", "results")}
        assert first.receipt is not None

    with EventStore(db) as store:
        restored = MindRuntime(store, env)
        replay = restored.tick(event)
        assert replay.recovered is True
        assert replay.frame.frame_id == first.frame.frame_id
        assert restored.tick_index == 1
        assert {name: store.count(name) for name in counts} == counts


class FakeModelTransport:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict] = []

    def request_json(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


class FakeVibeTransport:
    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict | None]] = []

    def request_json(self, method: str, path: str, *, body=None, headers=None, **kwargs):
        self.calls.append((method, path, body))
        for marker, response in self.responses.items():
            if marker in path:
                return response
        return {}


def test_openai_gateway_is_structured_bounded_and_replayable() -> None:
    transport = FakeModelTransport(
        {
            "choices": [{"message": {"content": '{"status":"proposal","proposition":"保留当前观察","uncertainty":0.2}'}}],
            "usage": {"total_tokens": 7},
        }
    )
    gateway = OpenAICompatibleGateway(
        "https://provider.invalid/v1",
        "secret-do-not-leak",
        "gpt-test",
        transport=transport,
        max_retries=0,
    )
    frame_view = {"sa": {"event_ref": "evt-1", "text": "项目进度"}, "feelings": []}
    first = gateway.propose(frame_view, None)
    second = gateway.propose(frame_view, None)
    assert isinstance(first, GatewayProposal)
    assert first.status == "proposal"
    assert first.call_id and first.request_key and first.input_snapshot_hash
    assert second.call_id == first.call_id
    assert "in_process_cache" in second.limitations
    assert len(transport.calls) == 1
    assert "secret-do-not-leak" not in json.dumps(first.to_dict(), ensure_ascii=False)


def test_openai_gateway_keeps_bad_provider_output_incomplete() -> None:
    transport = FakeModelTransport({"choices": [{"message": {"content": "not-json"}}]})
    gateway = OpenAICompatibleGateway("https://provider.invalid/v1", "secret", "gpt-test", transport=transport, max_retries=0)
    proposal = gateway.propose({"sa": {"event_ref": "evt-2", "text": "x"}}, None)
    assert proposal.status == "partial"
    assert "proposal_incomplete" in proposal.limitations[0]
    assert proposal.uncertainty == 1.0


def test_vibe_client_is_read_only_until_explicit_remote_capability() -> None:
    transport = FakeVibeTransport(
        {
            "bootstrap": {"project": {"id": "p"}, "writeCapability": {"allowed": False}},
            "knowledge": {"status": {"summary": "baseline"}},
            "activities": {"activities": [{"activityId": "a1", "summary": "progress"}], "nextCursor": "c1"},
        }
    )
    client = VibeProjectClient("p", "conv", transport=transport, allow_writes=True)
    client.bootstrap()
    assert client.writes_available is False
    events, cursor, partial = client.activity_events()
    assert len(events) == 1 and cursor == "c1" and partial is False
    assert events[0].source == "external"
    receipt = client.write_progress({"summary": "do not write"}, idempotency_key="k", action_ref="a")
    assert receipt.status == "unknown"
    assert receipt.error_code == "vibe_write_capability_unconfirmed"
    assert not any(method == "POST" for method, _, _ in transport.calls)


def test_vibe_activity_repoll_with_new_cursor_is_canonical_and_idempotent(tmp_path: Path) -> None:
    class PollingTransport:
        def __init__(self) -> None:
            self.polls = 0

        def request_json(self, method: str, path: str, *, body=None, headers=None, **kwargs):
            if "/activities" not in path:
                return {}
            self.polls += 1
            return {
                "activities": [{"activityId": "same-activity", "summary": "同一条活动"}],
                "nextCursor": f"cursor-{self.polls}",
            }

    transport = PollingTransport()
    client = VibeProjectClient("p", transport=transport)
    first, cursor1, _ = client.activity_events()
    second, cursor2, _ = client.activity_events(cursor=cursor1)
    assert cursor1 != cursor2
    assert first[0].event_id == second[0].event_id
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(store, LocalVibeEnvironment())
        one = runtime.tick(first[0])
        two = runtime.tick(second[0])
        assert two.recovered is True
        assert two.frame.frame_id == one.frame.frame_id
        assert store.count("events") == 2  # input + one readback
        assert store.count("frames") == 1


def test_vibe_environment_progress_requires_command_approval_and_readback() -> None:
    transport = FakeVibeTransport(
        {
            "bootstrap": {"project": {"id": "p"}, "writeCapability": {"allowed": True}},
            "progress": {"ok": True, "status": "accepted", "revision": "r1"},
        }
    )
    client = VibeProjectClient("p", "conv", transport=transport, allow_writes=True)
    client.bootstrap()
    env = VibeEnvironment(client)
    event = EventEnvelope(
        runtime_id="r",
        organism_id="o",
        environment_id=env.environment_id,
        episode_id="e",
        source="external",
        role="command",
        payload_inline={"summary": "approved progress"},
        extra={"vibe_write_approved": True},
        idempotency_key="input-progress",
    )
    candidates = env.candidates(event, {"sa": {"novelty": 0.1}, "uncertainty": 0.0})
    progress = next(item for item in candidates if item.kind == "publish_progress")
    receipt = env.dispatch(progress, progress.idempotency_key)
    assert receipt.status == "accepted"
    result = env.readback(receipt)
    assert result.status == "success" and result.source == "readback"


def test_gateway_sees_environment_candidates_and_competes_in_same_slot(tmp_path: Path) -> None:
    class PreferenceGateway:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def propose(self, frame_view, delegation):
            self.seen = [item["candidate_id"] for item in frame_view.get("action_candidates", [])]
            return GatewayProposal(
                capability_key="hybrid.cognition",
                status="proposal",
                candidate_preferences={self.seen[-1]: 0.35} if self.seen else {},
                selected_candidate_ref=self.seen[-1] if self.seen else None,
                source="llm_fixture",
                owner="llm",
            )

    env = LocalVibeEnvironment()
    gateway = PreferenceGateway()
    governance = GovernanceCompatibilityRecord(
        record_id="g",
        project_id="p",
        whitepaper_sha256=SHA,
        compatibility="compatible",
        llm_delegation_enabled=True,
    )
    capability = CapabilityOwnership(
        capability_key="hybrid.cognition",
        stage="assisted",
        decision_owner="ap_native",
        content_owner="llm",
        evidence_owner="environment",
        execution_owner="environment",
        llm_allowed=True,
        reason="fixture",
    )
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(store, env, gateway=gateway, governance=governance, capability=capability)
        result = runtime.tick(_event(runtime, env, text="候选应先提供给顾问", key="input-candidates"))
        # E3 attention transitions are ordinary internal affordances in the
        # same slot.  Preserve both environment candidates while allowing the
        # bounded attention candidates to compete beside them.
        assert len(gateway.seen) >= 2
        assert any(ref.endswith("_record") for ref in gateway.seen)
        assert any(ref.endswith("_defer") for ref in gateway.seen)
        assert any("attention" in ref for ref in gateway.seen)
        assert set(gateway.seen) == {item["candidate_id"] for item in result.frame.actions}
        assert result.frame.decision["status"] in {"selected", "abstained"}


def test_gateway_call_receipt_is_reused_after_runtime_restart(tmp_path: Path) -> None:
    class CountingGateway(OpenAICompatibleGateway):
        pass

    transport = FakeModelTransport(
        {"choices": [{"message": {"content": '{"status":"proposal","proposition":"一次调用"}'}}]}
    )
    gateway = CountingGateway("https://provider.invalid/v1", "secret", "gpt-test", transport=transport, max_retries=0)
    db = tmp_path / "mind.sqlite"
    env = LocalVibeEnvironment()
    event = None
    with EventStore(db) as store:
        runtime = MindRuntime(store, env, gateway=gateway)
        event = _event(runtime, env, text="调用收据应可恢复", key="input-call-replay")
        first = runtime.tick(event)
        assert first.frame.gateway["call_receipt"]["status"] == "proposal"
        assert store.count("gateway_calls") == 1
    with EventStore(db) as store:
        restored = MindRuntime(store, env, gateway=gateway)
        # Replaying the original event uses the stored frame and does not call
        # the model again.
        second = restored.tick(event)
        assert second.recovered is True
        assert len(transport.calls) == 1
        assert store.count("gateway_calls") == 1


def test_vibe_runtime_driver_persists_cursor_only_after_complete_page(tmp_path: Path) -> None:
    class PollingTransport:
        def __init__(self) -> None:
            self.calls = 0

        def request_json(self, method: str, path: str, *, body=None, headers=None, **kwargs):
            if "/activities" not in path:
                return {}
            self.calls += 1
            if self.calls == 1:
                return {"activities": [{"activityId": "a1", "summary": "第一条"}], "nextCursor": "c1"}
            return {"activities": [], "nextCursor": "c1"}

    transport = PollingTransport()
    client = VibeProjectClient("p", transport=transport)
    env = VibeEnvironment(client)
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(store, env)
        driver = VibeRuntimeDriver(client, runtime, store)
        report = driver.sync_activity()
        assert report.status == "ok"
        assert report.cursor_after == "c1"
        saved = store.get_receptor_cursor(driver.receptor_key)
        assert saved and saved["cursor"] == "c1"
        # A fresh driver resumes at c1; no new activity/frame is created.
        driver2 = VibeRuntimeDriver(client, MindRuntime(store, env), store)
        again = driver2.sync_activity()
        assert again.events_seen == 0
        assert store.count("frames") == 1


def test_vibe_runtime_driver_does_not_advance_cursor_when_runtime_fails(tmp_path: Path) -> None:
    class OneEventTransport:
        def __init__(self) -> None:
            self.calls = 0

        def request_json(self, method: str, path: str, *, body=None, headers=None, **kwargs):
            if "/activities" in path:
                self.calls += 1
                return {"activities": [{"activityId": "a1", "summary": "待处理"}], "nextCursor": "c1"}
            return {}

    transport = OneEventTransport()
    client = VibeProjectClient("p", transport=transport)
    env = VibeEnvironment(client)

    class BrokenRuntime:
        def tick(self, event):
            raise RuntimeError("synthetic")

    with EventStore(tmp_path / "mind.sqlite") as store:
        driver = VibeRuntimeDriver(client, BrokenRuntime(), store)  # type: ignore[arg-type]
        report = driver.sync_activity()
        assert report.status == "partial"
        assert report.cursor_after is None
        assert store.get_receptor_cursor(driver.receptor_key) is None
        assert transport.calls == 1


def test_readback_remains_recallable_but_has_no_repeat_action(tmp_path: Path) -> None:
    env = LocalVibeEnvironment()
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(store, env)
        original = runtime.tick(_event(runtime, env, text="记录一个观察", key="input-readback-recall"))
        follow = runtime.tick(original.result_envelope)
        assert follow.frame.b_recall
        assert follow.receipt is None
        assert follow.frame.decision["status"] == "abstained"
        # A later reality event can recall the readback occurrence; the
        # immediate readback frame itself must not manufacture another action.
        later = EventEnvelope(
            runtime_id=runtime.runtime_id,
            organism_id=runtime.organism_id,
            environment_id=env.environment_id,
            episode_id="wave1",
            payload_inline={"text": "继续观察刚才的记录"},
            idempotency_key="input-readback-recall-later",
        )
        later_result = runtime.tick(later)
        assert any(item.event_ref == original.result_envelope.event_id for item in later_result.frame.b_recall)


def test_local_primary_snapshot_does_not_silently_enable_llm(tmp_path: Path) -> None:
    env = LocalVibeEnvironment()
    governance = GovernanceCompatibilityRecord(
        record_id="g",
        project_id="p",
        whitepaper_sha256=SHA,
        compatibility="compatible",
        llm_delegation_enabled=True,
    )
    capability = CapabilityOwnership(
        capability_key="hybrid.cognition",
        stage="local_primary",
        decision_owner="ap_native",
        content_owner="ap_native",
        evidence_owner="environment",
        execution_owner="environment",
        llm_allowed=False,
        reason="provider off for mature local capability",
    )
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(store, env, governance=governance, capability=capability)
        result = runtime.tick(_event(runtime, env, text="本地能力优先", key="input-local-primary"))
        assert result.frame.gateway["selected_candidate_ref"] is None
        assert "capability_llm_disabled" in result.frame.gateway["limitations"]
