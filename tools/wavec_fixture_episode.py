"""Create one idempotent, provider-free C1 product episode for local QA."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ap_mind.contracts import CapabilityOwnership
from ap_mind.gateway import OpenAICompatibleGateway
from ap_mind.governance import GovernanceCompatibilityRecord
from ap_mind.studio_server import HYBRID_WHITEPAPER_SHA256, StudioEpisodeService


class FixtureTransport:
    def request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        del method, url, kwargs
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "status": "proposal",
                                "thought_candidates": [
                                    {
                                        "content": "证据还不完整，先保留未知，再决定是否追问。",
                                        "evidence_refs": [],
                                        "uncertainty": 0.2,
                                    }
                                ],
                                "lesson_candidates": [
                                    {
                                        "capability": "project.action_selection",
                                        "trigger_features": {"evidence_shape": "partial"},
                                        "suggested_adjustment": {"prefer": "ask_user"},
                                        "counterexamples": ["已有完整证据和 readback 时无需追问"],
                                        "confidence": 0.9,
                                    }
                                ],
                                "uncertainty": 0.1,
                                "limitations": ["fixture_teacher_no_external_evidence"],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    gateway = OpenAICompatibleGateway(
        "https://fixture.invalid/v1",
        "fixture-only-not-a-real-key",
        "local-structured-fixture",
        provider="local-fixture",
        transport=FixtureTransport(),
        max_retries=0,
    )
    governance = GovernanceCompatibilityRecord(
        record_id="ap-vibe-c1-fixture",
        project_id="ap-vibe-local",
        whitepaper_sha256=HYBRID_WHITEPAPER_SHA256,
        compatibility="compatible",
        llm_delegation_enabled=True,
        authority_sources={"whitepaper": HYBRID_WHITEPAPER_SHA256},
    )
    capability = CapabilityOwnership(
        capability_key="hybrid.cognition",
        stage="assisted",
        decision_owner="ap_native",
        content_owner="mixed",
        evidence_owner="environment",
        execution_owner="environment",
        llm_allowed=True,
        reason="local deterministic C1 product fixture",
    )
    service = StudioEpisodeService(
        data_dir,
        gateway=gateway,
        governance=governance,
        capability=capability,
    )
    request_id = "ap-vibe-c1-fixture-teacher-20260903"
    activity_id = "ap-vibe-c1-fixture-source-20260903"
    view, replayed = service.run_project_activity(
        request_id,
        {
            "activity_id": activity_id,
            "project_id": "ap-vibe-local",
            "kind": "c1_fixture_partial_progress",
            "summary": "教师建议怎样经过 AP 自己的课程行动竞争后再进入本地试用",
            "detail": "这是一条固定的本地结构化教师样本，不连接外部模型，也不产生费用。",
            "source_ref": f"fixture://ap-vibe/c1/{activity_id}",
            "completeness": "partial",
            "observed_remaining": ["用后续独立活动观察试用贡献"],
            "observed_unknown": ["长期成功率尚未测量"],
            "observed_next_action": "提交另一条证据画像相同但措辞不同的活动",
        },
    )
    print(
        json.dumps(
            {
                "request_id": request_id,
                "replayed": replayed,
                "projection_version": view.get("projection_version"),
                "teacher_model": view.get("teacher", {}).get("model"),
                "curriculum_episodes": len(view.get("curriculum_episodes", [])),
                "active_trial_count": view.get("learning", {}).get("active_trial_count"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
