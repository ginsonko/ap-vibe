from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ap_mind import EventEnvelope, EventStore, LocalVibeEnvironment, MindRuntime


def _event(runtime: MindRuntime, env: LocalVibeEnvironment, *, key: str, completeness: str = "complete") -> EventEnvelope:
    return EventEnvelope(
        runtime_id=runtime.runtime_id,
        organism_id=runtime.organism_id,
        environment_id=env.environment_id,
        episode_id="wave1c",
        payload_inline={"text": "需要继续检查的开放问题"},
        evidence_refs=(f"user:{key}",),
        completeness=completeness,
        idempotency_key=key,
    )


def test_partial_event_forms_native_proposition_then_bounded_inherited_thoughts(tmp_path: Path) -> None:
    env = LocalVibeEnvironment()
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(store, env, max_internal_ticks=3)
        first = runtime.tick(_event(runtime, env, key="partial", completeness="partial"))
        assert first.frame.proposition is not None
        assert first.frame.proposition.confidence == 0.45
        assert first.frame.proposition.uncertainty == 0.55
        assert first.frame.expression_draft is not None
        assert first.frame.expression_draft.proposition_ref == first.frame.proposition.proposition_id
        assert first.frame.expression_draft.public_allowed is False
        assert first.frame.thought_stream is not None

        continued = runtime.run_internal_budget(2)
        assert len(continued) == 2
        assert continued[0].frame.sa.source == "internal"
        assert continued[0].frame.thought_stream is not None
        assert continued[0].frame.thought_stream.predecessor_refs == (first.frame.thought_stream.frame_id,)
        assert continued[1].frame.thought_stream.predecessor_refs == (continued[0].frame.thought_stream.frame_id,)
        assert continued[0].frame.thoughts[0].proposition != first.frame.thoughts[0].proposition
        assert continued[0].frame.b_recall
        assert continued[0].frame.c_prediction
        assert continued[0].frame.feelings
        assert continued[0].frame.decision["status"] in {"selected", "abstained"}
        assert runtime.last_internal_status == "search_incomplete"
        assert runtime.counters_snapshot()["cognitive_ticks"] == 3


def test_no_open_frontier_does_not_create_timer_thought(tmp_path: Path) -> None:
    env = LocalVibeEnvironment()
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(store, env)
        runtime.tick(_event(runtime, env, key="complete"))
        before = {name: store.count(name) for name in ("events", "frames", "dispatches", "results")}
        assert runtime.continue_internal() is None
        assert {name: store.count(name) for name in before} == before
        assert runtime.last_internal_status == "idle"


def test_counters_and_thought_lineage_survive_restart_without_redispatch(tmp_path: Path) -> None:
    db = tmp_path / "mind.sqlite"
    env = LocalVibeEnvironment()
    with EventStore(db) as store:
        runtime = MindRuntime(store, env)
        runtime.tick(_event(runtime, env, key="restart", completeness="partial"))
        one = runtime.continue_internal()
        assert one is not None and one.frame.thought_stream is not None
        counters = runtime.counters_snapshot()
        counts = {name: store.count(name) for name in ("events", "frames", "dispatches", "results")}
        stream_id = one.frame.thought_stream.frame_id

    with EventStore(db) as store:
        restored = MindRuntime(store, env)
        assert restored.counters_snapshot()["cognitive_ticks"] == counters["cognitive_ticks"]
        assert restored.counters_snapshot()["readbacks"] == counters["readbacks"]
        assert restored.last_thought_stream is not None
        assert restored.last_thought_stream.frame_id == stream_id
        assert {name: store.count(name) for name in counts} == counts


def test_new_external_event_resets_wake_budget_but_keeps_lineage(tmp_path: Path) -> None:
    env = LocalVibeEnvironment()
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(store, env, max_internal_ticks=1, max_wake_attempts=1)
        runtime.tick(_event(runtime, env, key="first", completeness="partial"))
        assert len(runtime.run_internal_budget(1)) == 1
        assert runtime.last_internal_status == "search_incomplete"
        prior_stream = runtime.last_thought_stream.frame_id if runtime.last_thought_stream else None

        runtime.tick(_event(runtime, env, key="second", completeness="partial"))
        assert runtime.counters_snapshot()["wake_attempts"] == 0
        assert runtime.last_thought_stream is not None
        assert runtime.last_thought_stream.predecessor_refs == (prior_stream,)
        assert len(runtime.run_internal_budget(1)) == 1


def test_duplicate_external_retry_does_not_refill_wake_budget(tmp_path: Path) -> None:
    env = LocalVibeEnvironment()
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(store, env, max_internal_ticks=1, max_wake_attempts=1)
        original = _event(runtime, env, key="retry", completeness="partial")
        runtime.tick(original)
        assert len(runtime.run_internal_budget(1)) == 1
        assert runtime.counters_snapshot()["wake_attempts"] == 1
        retry = runtime.tick(_event(runtime, env, key="retry", completeness="partial"))
        assert retry.recovered is True
        assert runtime.counters_snapshot()["wake_attempts"] == 1
        assert runtime.continue_internal() is None
        assert runtime.last_internal_status == "search_incomplete"


def test_provider_wait_and_tool_progress_do_not_create_cognitive_ticks(tmp_path: Path) -> None:
    env = LocalVibeEnvironment()
    with EventStore(tmp_path / "mind.sqlite") as store:
        runtime = MindRuntime(store, env)
        runtime.tick(_event(runtime, env, key="counter"))
        before = runtime.counters_snapshot()
        runtime.note_provider_wait(count=3)
        runtime.note_tool_progress(count=2)
        after = runtime.counters_snapshot()
        assert after["cognitive_ticks"] == before["cognitive_ticks"]
        assert after["provider_waits"] == before["provider_waits"] + 3
        assert after["tool_progress"] == before["tool_progress"] + 2
        assert store.count("frames") == 1
