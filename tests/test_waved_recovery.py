from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from ap_mind.project_knowledge import KnowledgeReviewRequest
from ap_mind.studio_server import StudioEpisodeService


def _activity(
    activity_id: str,
    summary: str,
    *,
    completed=(),
    remaining=(),
    unknown=(),
    redlines=(),
    resolved_remaining=(),
    resolved_unknown=(),
    next_action: str | None = None,
):
    return {
        "activity_id": activity_id,
        "project_id": "project-local",
        "kind": f"unseen-{activity_id}",
        "summary": summary,
        "source_ref": f"session://recovery/{activity_id}",
        "completeness": "complete",
        "observed_completed": list(completed),
        "observed_remaining": list(remaining),
        "observed_unknown": list(unknown),
        "observed_resolved_remaining": list(resolved_remaining),
        "observed_resolved_unknown": list(resolved_unknown),
        "observed_redlines": list(redlines),
        "observed_next_action": next_action,
    }


def _stage(service: StudioEpisodeService, activity_id: str, summary: str, **kwargs):
    view, replayed = service.run_project_activity(
        f"request-{activity_id}",
        _activity(activity_id, summary, **kwargs),
    )
    assert replayed is False
    assert view["selected_action"]["kind"] == "stage_knowledge_candidate"
    return view, view["knowledge_candidates"][0]


def _review(proposal: dict, *, decision_id: str, parent: str | None):
    return {
        "decision_id": decision_id,
        "project_id": "project-local",
        "proposal_id": proposal["proposal_id"],
        "source_ref": f"user://knowledge-review/{decision_id}",
        "rationale": "把这条已核对进度设为本地恢复基线",
        "expected_parent_revision": parent,
    }


def test_review_requires_ap_action_and_readback_then_cold_restart_is_idempotent(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "service"
    service = StudioEpisodeService(data_dir, codex_project_id="project-local")
    _, proposal = _stage(
        service,
        "milestone-source",
        "恢复底座已经完成第一步",
        completed=("完成事件与课程底座",),
        remaining=("接入下一会话恢复",),
        redlines=("不读取 APV3 受保护数据库",),
        next_action="让 Agent Brief 被下一会话消费",
    )
    raw_review = _review(proposal, decision_id="review-one", parent=None)

    first, replayed = service.run_project_knowledge_review("review-request-one", raw_review)
    assert replayed is False
    assert len(first["ticks"]) == 2
    assert first["selected_action"]["kind"] == "commit_local_milestone"
    assert first["ticks"][0]["decision"]["result_status"] == "success"
    assert first["knowledge_revision"]["revision_number"] == 1
    assert first["knowledge_revision"]["vibe_formal_write"] is False

    replay, replayed = service.run_project_knowledge_review("review-request-one", raw_review)
    assert replayed is True
    assert replay == first

    cold = StudioEpisodeService(data_dir, codex_project_id="project-local")
    recovery = cold.local_recovery()
    assert recovery["valid"] is True
    assert recovery["revision_count"] == 1
    assert recovery["milestone"]["content_hash"] == first["knowledge_revision"]["content_hash"]
    assert recovery["milestone"]["sections"]["work"]["completed"] == ["完成事件与课程底座"]
    assert recovery["write_capability"]["vibe_formal_write"] is False


def test_unreviewed_activity_is_latest_delta_and_brief_preserves_milestone(
    tmp_path: Path,
) -> None:
    service = StudioEpisodeService(tmp_path / "service", codex_project_id="project-local")
    _, proposal = _stage(
        service,
        "base",
        "已确认的恢复基线",
        completed=("底座已完成",),
        remaining=("页面接线",),
        redlines=("不能让 delta 覆盖 milestone",),
        next_action="完成页面接线",
    )
    service.run_project_knowledge_review(
        "review-base",
        _review(proposal, decision_id="decision-base", parent=None),
    )

    _stage(
        service,
        "delta",
        "页面接线出现了新的阶段性进展",
        completed=("页面第一块已接入",),
        remaining=("浏览器验收",),
        next_action="完成浏览器验收",
    )
    recovery = service.local_recovery()
    assert recovery["milestone"]["sections"]["status"]["summary"] == "已确认的恢复基线"
    assert recovery["milestone"]["sections"]["requirements"]["redlines"] == [
        "不能让 delta 覆盖 milestone"
    ]
    assert recovery["latest_delta"]["summary"] == "页面接线出现了新的阶段性进展"
    assert recovery["latest_delta"]["authority"] == "unreviewed_delta"

    brief = service.agent_brief(
        {
            "project_id": "project-local",
            "goal": "从当前冷保存点继续",
            "max_chars": 2_000,
        }
    )
    assert brief["characters"] <= 2_000
    assert "底座已完成" in brief["text"]
    assert "不能让 delta 覆盖 milestone" in brief["text"]
    assert "页面接线出现了新的阶段性进展" in brief["text"]
    assert "完成页面接线" in brief["text"]
    assert "C:\\" not in brief["text"]
    assert brief["vibe_formal_write"] is False


def test_stale_parent_review_defers_without_overwriting_current_revision(
    tmp_path: Path,
) -> None:
    service = StudioEpisodeService(tmp_path / "service", codex_project_id="project-local")
    _, first_proposal = _stage(service, "first", "第一条已确认进度")
    first, _ = service.run_project_knowledge_review(
        "review-first",
        _review(first_proposal, decision_id="decision-first", parent=None),
    )
    assert first["knowledge_revision"]["revision_number"] == 1

    _, second_proposal = _stage(service, "second", "第二条候选基于过时父版本")
    stale, _ = service.run_project_knowledge_review(
        "review-stale",
        _review(second_proposal, decision_id="decision-stale", parent=None),
    )
    assert stale["selected_action"]["kind"] == "defer"
    assert stale["knowledge_revision"] is None
    assert service.local_recovery()["revision_count"] == 1


def test_brief_budget_keeps_redline_and_next_action_ahead_of_long_lists(
    tmp_path: Path,
) -> None:
    service = StudioEpisodeService(tmp_path / "service", codex_project_id="project-local")
    long_completed = tuple(
        f"普通完成项 {index}：" + "已核对内容" * 45
        for index in range(24)
    )
    _, proposal = _stage(
        service,
        "budget-priority",
        "很多普通完成记录不能挤掉关键恢复信息",
        completed=long_completed,
        remaining=("继续完成恢复界面",),
        redlines=tuple(f"绝不能越过关键红线 {index}：" + "边界" * 40 for index in range(16)),
        next_action="执行唯一的下一原子动作",
    )
    service.run_project_knowledge_review(
        "review-budget-priority",
        _review(proposal, decision_id="decision-budget-priority", parent=None),
    )

    brief = service.agent_brief(
        {"project_id": "project-local", "goal": "有界恢复", "max_chars": 2_000}
    )
    assert brief["characters"] <= 2_000
    assert brief["brief_incomplete"] is True
    assert "恢复有效：是" in brief["text"]
    assert "绝不能越过关键红线 0" in brief["text"]
    assert "绝不能越过关键红线 15" in brief["text"]
    assert "执行唯一的下一原子动作" in brief["text"]


def test_recovery_excludes_unreviewed_proposals_older_than_latest_milestone(
    tmp_path: Path,
) -> None:
    service = StudioEpisodeService(tmp_path / "service", codex_project_id="project-local")
    _, old = _stage(service, "old-pending", "这条旧候选没有被纳入随后建立的基线")
    _, current = _stage(service, "current", "随后建立的当前基线")
    reviewed, _ = service.run_project_knowledge_review(
        "review-current",
        _review(current, decision_id="decision-current", parent=None),
    )
    assert reviewed["knowledge_revision"]["revision_number"] == 1

    latest_revision = service.knowledge_store.latest("project-local")
    assert latest_revision is not None
    after = (
        datetime.fromisoformat(latest_revision.created_at.replace("Z", "+00:00"))
        + timedelta(seconds=1)
    ).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    proposals = service._knowledge_proposals("project-local")
    old_proposal = next(item for item in proposals if item.proposal_id == old["proposal_id"])
    synthetic_after = replace(old_proposal, proposal_id="after-milestone", created_at=after, summary="真正位于 milestone 之后的增量")

    no_after = service.knowledge_store.recovery("project-local", proposals)
    assert no_after["latest_delta"] is None
    with_after = service.knowledge_store.recovery("project-local", (*proposals, synthetic_after))
    assert with_after["latest_delta"]["summary"] == "真正位于 milestone 之后的增量"


def test_reviewed_resolution_closes_only_exact_parent_items_with_lineage(tmp_path: Path) -> None:
    service = StudioEpisodeService(tmp_path / "service", codex_project_id="project-local")
    _, base = _stage(
        service,
        "resolution-base",
        "跨任务恢复尚待完成",
        remaining=("全局 Skill 尚未安装", "自动上下文压缩 hook 尚未接入"),
        unknown=("独立任务能否发现 Skill", "独立任务能否自动发现 Skill（相似但不同）"),
        next_action="安装全局 Skill",
    )
    base_run, _ = service.run_project_knowledge_review(
        "review-resolution-base",
        _review(base, decision_id="decision-resolution-base", parent=None),
    )
    parent_id = base_run["knowledge_revision"]["revision_id"]

    _, closure = _stage(
        service,
        "resolution-closure",
        "独立任务已从全局 Skill 恢复",
        completed=("全局 Skill 已安装并被独立任务消费",),
        resolved_remaining=("全局 Skill 尚未安装", "自动上下文压缩 hook 尚未接入（相似但不同）"),
        resolved_unknown=("独立任务能否发现 Skill",),
        next_action="继续接入自动上下文压缩 hook",
    )
    final_run, _ = service.run_project_knowledge_review(
        "review-resolution-closure",
        _review(closure, decision_id="decision-resolution-closure", parent=parent_id),
    )
    assert final_run["knowledge_revision"]["revision_number"] == 2
    recovery = service.local_recovery()
    work = recovery["milestone"]["sections"]["work"]
    status = recovery["milestone"]["sections"]["status"]
    assert work["remaining"] == ["自动上下文压缩 hook 尚未接入"]
    assert work["unknown"] == ["独立任务能否自动发现 Skill（相似但不同）"]
    assert status["resolved_remaining"] == ["全局 Skill 尚未安装"]
    assert status["resolved_unknown"] == ["独立任务能否发现 Skill"]
    assert recovery["valid"] is True
