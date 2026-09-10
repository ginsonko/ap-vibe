"""Run the single bounded Wave B3 real-teacher episode.

Credentials are accepted only through the process environment by
``gateway_from_environment``.  This tool prints a compact, credential-free
receipt and uses a stable request id so an accidental rerun replays the local
result instead of paying for a second provider call.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ap_mind.studio_server import StudioEpisodeService, gateway_from_environment  # noqa: E402
from ap_mind.vibe_mind import ProjectActivity  # noqa: E402


REQUEST_ID = "ap-vibe-b3-real-teacher-20260903-001"
ACTIVITY_ID = "ap-vibe-b3-real-activity-20260903-001"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one idempotent AP-Vibe B3 teacher episode")
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args(argv)

    gateway, governance, capability, _ = gateway_from_environment()
    service = StudioEpisodeService(
        Path(args.data_dir),
        gateway=gateway,
        governance=governance,
        capability=capability,
    )
    activity = ProjectActivity(
        activity_id=ACTIVITY_ID,
        project_id="ap-vibe-local",
        kind="hybrid_teacher_acceptance",
        summary="六类教师合同已接入，当前需要核对 AP 与 LLM 分账是否在网页中真实可见",
        detail=(
            "本地 AP 已完成事件、SA、StatePool、CurrentField、B/C、认知感受、想法、"
            "行动竞争、dispatch 和 readback。LLM 只能对召回、预测、感受、想法、"
            "既有行动候选和课程提出结构化建议；正式知识写入仍关闭。"
        ),
        actor="codex",
        status="implementation_ready_for_live_acceptance",
        source_ref="codex://ap-vibe/b3/real-teacher-episode",
        evidence_refs=("plan://ap-vibe/b3",),
        completeness="partial",
        observed_completed=(
            "六类教师 proposal 协议",
            "同一 ActionArena 的有界行动偏好",
            "provider-off 回退与持久调用收据",
        ),
        observed_remaining=("浏览器核对 AP/LLM/adoption 分账",),
        observed_unknown=("长期教师质量与本地成长收益尚未测量",),
        observed_next_action="在工作台检查六类建议、采用状态、行动竞争和 readback",
    )
    view, replayed = service.run_project_activity(REQUEST_ID, activity.to_dict())
    teacher = view.get("teacher", {})
    receipt = teacher.get("call_receipt", {}) if isinstance(teacher, dict) else {}
    serialized = json.dumps(view, ensure_ascii=False, sort_keys=True)
    provider_state = service.provider_state()
    output = {
        "ok": view.get("status") in {"success", "partial"},
        "replayed": replayed,
        "request_id": view.get("request_id"),
        "episode_id": view.get("episode_id"),
        "tick_count": len(view.get("ticks", ())),
        "selected_action": (view.get("selected_action") or {}).get("kind"),
        "readback_status": (view.get("ticks") or [{}])[0].get("decision", {}).get("result_status"),
        "provider": {
            "configured": provider_state.get("configured"),
            "status": teacher.get("status") if isinstance(teacher, dict) else None,
            "provider": teacher.get("provider") if isinstance(teacher, dict) else None,
            "model": teacher.get("model") if isinstance(teacher, dict) else None,
            "candidate_counts": teacher.get("candidate_counts") if isinstance(teacher, dict) else None,
            "adoption_summary": (teacher.get("adoption") or {}).get("summary")
            if isinstance(teacher, dict)
            else None,
            "latency_ms": receipt.get("latency_ms") if isinstance(receipt, dict) else None,
            "usage": receipt.get("usage") if isinstance(receipt, dict) else None,
            "call_id": receipt.get("call_id") if isinstance(receipt, dict) else None,
            "request_key": receipt.get("request_key") if isinstance(receipt, dict) else None,
            "failure": (view.get("ticks") or [{}])[0].get("research", {}).get("gateway", {}).get("failure"),
        },
        "validation_issue_count": len(teacher.get("validation_issues", ()))
        if isinstance(teacher, dict)
        else 0,
        "secret_or_endpoint_in_projection": any(
            marker in serialized
            for marker in ("Authorization", "Bearer ", "api.yinziapi.top", "AP_VIBE_LLM_API_KEY")
        ),
        "formal_knowledge_write": False,
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0 if output["ok"] and not output["secret_or_endpoint_in_projection"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
