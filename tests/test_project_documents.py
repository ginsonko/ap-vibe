from pathlib import Path
import json
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ap_mind.contracts import ContractError
from ap_mind.studio_server import StudioEpisodeService
from ap_mind.project_documents import provisional_sections


@pytest.fixture
def context(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    service = StudioEpisodeService(tmp_path / "data", project_root=root, codex_project_id="one", auto_onboard_workspaces=True)
    receipt = service.task_context.bootstrap({"cwd": str(root), "session_id": "task-one", "request_id": "start-one", "goal": "Read project docs"})
    yield service, {"receipt_id": receipt["receipt_id"], "session_id": "task-one"}
    service.close()


def test_full_history_can_exceed_old_64kb_limit_and_respects_configured_capacity(context, monkeypatch):
    service, identity = context
    sections = {key: {"summary": "真实历史条目" * 1500} for key in ['architecture', 'decisions', 'work', 'evidence']}
    patch = {**identity, "request_id": "large-history", "expected_revision": 0, "sections": sections}
    saved = service.task_context.update_knowledge(patch)
    assert saved['revision'] == 1
    assert service.task_context.knowledge({**identity, 'sections':['decisions']})['sections']['decisions'] == sections['decisions']
    monkeypatch.setenv('AP_VIBE_DOCUMENT_PATCH_BYTES','64000')
    with pytest.raises(ContractError, match='document_patch_too_large:64000'):
        service.task_context.update_knowledge({**patch, 'request_id':'small-config', 'expected_revision':1})
    assert service.task_context.documents.latest('one')['revision'] == 1


def test_structured_update_selective_read_conflict_and_cold_history(context, tmp_path):
    service, identity = context
    patch = {**identity, "request_id": "checkpoint-1", "expected_revision": 0,
             "sections": {"identity": {"name": "示例产品", "summary": "服务家庭", "api_key": "do-not-store"},
                          "work": {"remaining": ["校验恢复"], "completed": []},
                          "decisions": {"items": [{"old_logic": "旧逻辑", "new_logic": "新逻辑", "reason": "实际故障"}]}}}
    first = service.task_context.update_knowledge(patch)
    assert first["revision"] == 1
    assert service.task_context.update_knowledge(patch)["replayed"]
    catalog = service.task_context.knowledge(identity)
    assert len(catalog["catalog"]) == 11 and catalog["sections"] == {}
    assert catalog["project"]["display_name"] == "示例产品"
    selected = service.task_context.knowledge({**identity, "sections": ["identity"]})
    assert set(selected["sections"]) == {"identity"}
    assert "do-not-store" not in str(selected)
    assert selected["authority"] == "agent_reported"
    assert service.local_recovery("one")["milestone"] is None
    with pytest.raises(ContractError, match="revision_conflict"):
        service.task_context.update_knowledge({**patch, "request_id": "checkpoint-2"})
    with pytest.raises(ContractError, match="request_conflict"):
        service.task_context.update_knowledge({**patch, "sections": {"work": {"remaining": ["changed"]}}})
    service.task_context.update_knowledge({**patch, "request_id": "checkpoint-2", "expected_revision": 1,
                                           "sections": {"work": {"remaining": [], "completed": ["恢复通过"]}}})
    assert service.task_context.knowledge({**identity, "sections": ["identity"]})["sections"]["identity"]["name"] == "示例产品"
    assert service.task_context.knowledge({**identity, "revision": 1, "sections": ["work"]})["sections"]["work"]["remaining"] == ["校验恢复"]
    service.close()
    cold = StudioEpisodeService(tmp_path / "data", project_root=tmp_path / "project", codex_project_id="one")
    assert cold.task_context.knowledge({**identity, "sections": ["work"]})["revision"] == 2
    assert cold.task_context.update_knowledge(patch)["replayed"]
    cold.close()


def test_growing_history_preserves_all_entries_revisions_and_retry_after_restart(context, tmp_path):
    service, identity = context
    completed = [{"id": f"done-{i}", "summary": f"已完成工作 {i}"} for i in range(100)]
    decisions = {f"decision-{i}": {"reason": f"原决定 {i}"} for i in range(120)}
    first_patch = {**identity, "request_id": "growing-history-1", "expected_revision": 0,
                   "sections": {"work": {"completed": completed, "remaining": ["保留旧待办"]},
                                "decisions": {"by_id": decisions}}}
    first = service.task_context.update_knowledge(first_patch)
    extended = completed + [{"id": "done-100", "summary": "新增工作", "api_key": "synthetic-secret"}]
    second_patch = {**identity, "request_id": "growing-history-2", "expected_revision": 1,
                    "sections": {"work": {"completed": extended, "remaining": ["保留旧待办"]}}}
    second = service.task_context.update_knowledge(second_patch)
    assert second["revision"] == 2
    assert service.task_context.update_knowledge(second_patch)["replayed"]
    service.close()
    cold = StudioEpisodeService(tmp_path / "data", project_root=tmp_path / "project", codex_project_id="one")
    try:
        saved = cold.task_context.knowledge({**identity, "sections": ["work", "decisions"]})
        assert saved["revision"] == 2
        assert saved["sections"]["work"]["completed"][:-1] == completed
        assert saved["sections"]["work"]["completed"][-1]["api_key"] == "[REDACTED]"
        assert saved["sections"]["work"]["remaining"] == ["保留旧待办"]
        assert saved["sections"]["decisions"]["by_id"] == decisions
        old = cold.task_context.knowledge({**identity, "revision": 1, "sections": ["work"]})
        assert old["sections"]["work"]["completed"] == completed
        assert cold.task_context.documents.latest("one")["content_hash"] == second["content_hash"]
        assert cold.task_context.update_knowledge(first_patch)["content_hash"] == first["content_hash"]
        assert cold.task_context.update_knowledge(second_patch)["replayed"]
    finally:
        cold.close()


def test_large_collections_still_obey_byte_budget_without_losing_prior_revision(context, monkeypatch):
    service, identity = context
    baseline = service.task_context.update_knowledge({**identity, "request_id": "budget-baseline", "expected_revision": 0,
                                                     "sections": {"work": {"remaining": ["旧事项"]}}})
    monkeypatch.setenv("AP_VIBE_DOCUMENT_PATCH_BYTES", "80000")
    patch = {**identity, "request_id": "over-budget", "expected_revision": 1,
             "sections": {"work": {"completed": ["历史内容" * 200 for _ in range(150)]}}}
    with pytest.raises(ContractError, match="document_patch_too_large:80000"):
        service.task_context.update_knowledge(patch)
    assert service.task_context.documents.latest("one")["content_hash"] == baseline["content_hash"]


def test_total_budget_applies_to_separate_growing_chapters(context, monkeypatch):
    service, identity = context
    monkeypatch.setenv("AP_VIBE_DOCUMENT_PATCH_BYTES", "200000")
    chapter = {"items": ["x" * 999 for _ in range(150)]}
    for revision, name in enumerate(["work", "decisions"]):
        service.task_context.update_knowledge({**identity, "request_id": "total-" + name, "expected_revision": revision,
                                              "sections": {name: chapter}})
    with pytest.raises(ContractError, match="document_total_too_large"):
        service.task_context.update_knowledge({**identity, "request_id": "total-evidence", "expected_revision": 2,
                                              "sections": {"evidence": chapter}})
    saved = service.task_context.documents.latest("one")
    assert saved["revision"] == 2 and "evidence" not in saved["sections"]


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), {1: "bad key"}, {"x" * 129: "bad key"}])
def test_validation_reaches_items_beyond_old_collection_limit(context, invalid):
    service, identity = context
    with pytest.raises(ContractError):
        service.task_context.update_knowledge({**identity, "request_id": "invalid-tail", "expected_revision": 0,
                                              "sections": {"work": {"completed": ["valid"] * 150 + [invalid]}}})
    assert service.task_context.documents.latest("one") is None


def test_scores_need_evidence_and_unknown_is_not_zero(context):
    service, identity = context
    for bad in [{"score": 80}, {"score": 80, "reason": "fine"}, {"score": True}, {"score": 101}, {"score": float("nan")}]:
        with pytest.raises(ContractError):
            service.task_context.update_knowledge({**identity, "request_id": "bad", "expected_revision": 0,
                                                   "sections": {"risks": {"assessment": [{"key": "security", **bad}]}}})
    service.task_context.update_knowledge({**identity, "request_id": "scores", "expected_revision": 0, "sections": {
        "risks": {"assessment": [{"key": "security", "score": 62, "reason": "本地越权路径已核验，外部尚未知", "evidence_refs": ["test://isolation"]}]}}})
    scores = service.task_context.knowledge(identity)["assessment"]
    assert len(scores) == 10
    assert [item["score"] for item in scores if item["key"] == "security"] == [62]
    assert sum(item["score"] is None for item in scores) == 9


def test_cross_task_and_disabled_project_cannot_write(context, tmp_path):
    service, identity = context
    patch = {**identity, "request_id": "update", "expected_revision": 0, "sections": {"identity": {"name": "One"}}}
    with pytest.raises(ContractError, match="identity_mismatch"):
        service.task_context.update_knowledge({**patch, "session_id": "other"})
    with pytest.raises(ContractError, match="fields_invalid"):
        service.task_context.update_knowledge({**patch, "project_id": "other"})
    other = tmp_path / "other"
    other.mkdir()
    second = service.task_context.bootstrap({"cwd": str(other), "session_id": "task-two", "request_id": "start-two", "goal": "Separate project"})
    assert second["project_id"] != "one"
    service.task_context.update_knowledge(patch)
    assert service.task_context.knowledge({"receipt_id": second["receipt_id"], "session_id": "task-two"})["revision"] == 0
    service.product_registry.set_auto_monitor("one", False)
    with pytest.raises(ContractError, match="project_disabled"):
        service.task_context.update_knowledge(patch)


def test_child_identity_title_and_internal_channel_filter(context, tmp_path):
    from ap_mind.codex_activity import CodexJsonlReceptor
    from types import SimpleNamespace
    service, _ = context
    source = tmp_path / "child.jsonl"
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": "child", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent", "agent_path": "/root/frontend", "agent_nickname": "Ada"}}}}}) + "\n", encoding="utf-8")
    assert service._codex_task_identity(SimpleNamespace(source_path=source, session_id="child"))["parent_session_id"] == "parent"
    assert service._codex_task_identity(SimpleNamespace(source_path=source, session_id="different")) == {}
    assert service._codex_fallback_title('# Response annotations:\nboilerplate\n## My request:\n修复页脚') == "修复页脚"
    receptor = CodexJsonlReceptor(source)
    raw = {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "analysis", "content": [{"type": "output_text", "text": "private"}]}}
    assert receptor._visible(raw, start=0, end=10) is None
    raw["payload"]["channel"] = "final"
    raw["payload"]["content"][0]["text"] = "<thinking>private</thinking>公开结论"
    assert receptor._visible(raw, start=0, end=10).text == "公开结论"


def test_auto_project_profile_reads_bounded_fixed_entries_without_revision(tmp_path):
    root = tmp_path / "documented"
    root.mkdir()
    (root / "README.md").write_text("# 线索项目\n\n这是 README 中的项目简介。\n\n## 其他内容\n不要读取这一段。\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'manifest-name'\ndescription = '来自 Python 元数据的简介'\n", encoding="utf-8"
    )
    (root / "package.json").write_text('{"name":"frontend-name","description":"前端简介"}', encoding="utf-8")
    project = SimpleNamespace(root_path=root, display_name="目录名不代表用途")

    provisional = provisional_sections(project)
    assert provisional["identity"]["name"] == "manifest-name"
    assert provisional["identity"]["summary"] == "来自 Python 元数据的简介"
    assert provisional["identity"]["provenance"] == "auto_detected"
    assert provisional["identity"]["confidence"] == "unverified"
    assert {item["path"] for item in provisional["sources"]["documents"]} == {"README.md", "pyproject.toml", "package.json"}
    assert provisional["architecture"]["provenance"] == "auto_detected"
    assert provisional["recovery"]["entry_points"] == ["README.md", "pyproject.toml", "package.json"]

    service = StudioEpisodeService(tmp_path / "data", project_root=root, codex_project_id="one", auto_onboard_workspaces=True)
    receipt = service.task_context.bootstrap({"cwd": str(root), "session_id": "task-one", "request_id": "start-one", "goal": "检查自动资料"})
    before = service.task_context.knowledge({"receipt_id": receipt["receipt_id"], "session_id": "task-one"})
    assert before["revision"] == 0
    assert before["project"]["documentation_state"] == "auto_detected"
    assert before["project"]["display_name"] == "manifest-name"
    assert before["provisional"]["state"] == "auto_detected"
    assert sum(item["score"] is None for item in before["assessment"]) == 10
    service.close()


def test_empty_or_invalid_project_docs_stay_unverified_and_do_not_infer_identity(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project\ninvalid", encoding="utf-8")
    (root / "package.json").write_text("{invalid", encoding="utf-8")
    provisional = provisional_sections(SimpleNamespace(root_path=root, display_name="workspace-name"))
    assert provisional["identity"]["name"] == "workspace-name"
    assert provisional["identity"]["summary"] == "尚未从项目说明文件识别到简介。"
    assert provisional["identity"]["confidence"] == "unverified"
    assert provisional["identity"]["source_files"] == ["pyproject.toml", "package.json"]
    assert provisional["sources"]["documents"][0]["path"] == "pyproject.toml"
    assert provisional["status"]["phase"] == "自动建档"


def test_agent_update_overrides_auto_identity_without_losing_other_sections(tmp_path):
    root = tmp_path / "curate"
    root.mkdir()
    (root / "README.md").write_text("# 自动名称\n\n自动简介\n", encoding="utf-8")
    service = StudioEpisodeService(tmp_path / "data", project_root=root, codex_project_id="one", auto_onboard_workspaces=True)
    receipt = service.task_context.bootstrap({"cwd": str(root), "session_id": "task-one", "request_id": "start-one", "goal": "整理项目"})
    identity = {"receipt_id": receipt["receipt_id"], "session_id": "task-one"}
    service.task_context.update_knowledge({**identity, "request_id": "curate-1", "expected_revision": 0, "sections": {
        "identity": {"name": "核对后的名称", "summary": "核对后的简介", "audience": "开发者"},
        "work": {"remaining": ["继续核对"], "completed": []},
    }})
    selected = service.task_context.knowledge({**identity, "sections": ["identity", "work", "risks"]})
    assert selected["revision"] == 1
    assert selected["sections"]["identity"]["name"] == "核对后的名称"
    assert selected["sections"]["identity"]["summary"] == "核对后的简介"
    assert selected["sections"]["work"]["remaining"] == ["继续核对"]
    assert selected["sections"]["risks"].get("assessment", []) == []
    assert all(item["score"] is None for item in selected["assessment"])
    assert selected["catalog"][[item["key"] for item in selected["catalog"]].index("identity")]["state"] == "agent_reported"
    assert selected["catalog"][[item["key"] for item in selected["catalog"]].index("requirements")]["state"] == "auto_detected"
    service.close()


def test_document_read_keeps_dossier_available_when_review_chain_is_invalid(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    service = StudioEpisodeService(tmp_path / "data", project_root=root, codex_project_id="one")
    try:
        service.task_context.bootstrap({"cwd": str(root), "session_id": "reader", "request_id": "reader-start", "goal": "读取"})
        service.knowledge_store.validate = lambda _project_id: (False, "content_hash_mismatch", None)
        result = service.task_context.documents.read("one", ["identity"])
        assert result["ok"] is True
        assert result["reviewed_chain"]["valid"] is False
        assert result["reviewed_chain"]["status"] == "content_hash_mismatch"
    finally:
        service.close()
