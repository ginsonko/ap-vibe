"""Local Wave 1A episode and cold-restart probe.

This probe is intentionally provider-free and uses only the new local fixture.
Its output is an engineering receipt for the causal chain, not a claim about
the live Vibe service.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ap_mind.contracts import EventEnvelope  # noqa: E402
from ap_mind.environment import LocalVibeEnvironment  # noqa: E402
from ap_mind.runtime import MindRuntime  # noqa: E402
from ap_mind.storage import EventStore  # noqa: E402


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="ap-mind-wave1-"))
    db = work / "mind.sqlite"
    env = LocalVibeEnvironment()
    try:
        with EventStore(db) as store:
            runtime = MindRuntime(store, env)
            events = (
                EventEnvelope(
                    runtime_id=runtime.runtime_id,
                    organism_id=runtime.organism_id,
                    environment_id=env.environment_id,
                    episode_id="wave1-episode",
                    payload_inline={"text": "项目需要更新进度"},
                    evidence_refs=("user:episode-1",),
                    idempotency_key="input-wave1-1",
                ),
                EventEnvelope(
                    runtime_id=runtime.runtime_id,
                    organism_id=runtime.organism_id,
                    environment_id=env.environment_id,
                    episode_id="wave1-episode",
                    payload_inline={"text": "项目进度已经更新"},
                    evidence_refs=("user:episode-2",),
                    idempotency_key="input-wave1-2",
                ),
            )
            first = runtime.tick(events[0])
            second = runtime.tick(events[1])
            repeat = runtime.tick(events[1])
            before = {
                "frame_ticks": [first.frame.tick_index, second.frame.tick_index],
                "frame_ids_distinct": first.frame.frame_id != second.frame.frame_id,
                "second_predecessor_thought": bool(second.frame.thoughts[0].predecessor_refs),
                "second_recall_count": len(second.frame.b_recall),
                "second_prediction_count": len(second.frame.c_prediction),
                "phase_completeness": dict(second.frame.phase_completeness),
                "decision_status": second.frame.decision.get("status"),
                "receipt_status": second.receipt.status if second.receipt else None,
                "result_status": second.result.status if second.result else None,
                "repeat_same_frame": repeat.frame.frame_id == second.frame.frame_id,
                "counts_before_restart": {name: store.count(name) for name in ("events", "frames", "dispatches", "results", "checkpoints")},
            }
        with EventStore(db) as store:
            restored = MindRuntime(store, env)
            after = {
                "restored_tick_index": restored.tick_index,
                "restored_open_thought": bool(restored.open_thought),
                "counts_after_restart": {name: store.count(name) for name in ("events", "frames", "dispatches", "results", "checkpoints")},
            }
        print(json.dumps({
            "episode_probe": "wave1a_local_fixture",
            "product_effect": "local_mock_only",
            "causal_chain_receipt": before,
            "cold_restart_receipt": after,
            "first_broken_link": None,
        }, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
