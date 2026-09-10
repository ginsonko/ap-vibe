from __future__ import annotations

from pathlib import Path
import sys
import json
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ap_mind.contracts import ContractError
from ap_mind.project_documents import DIMENSIONS
from ap_mind.studio_server import StudioEpisodeService
from ap_mind import organization_runner as runner


@pytest.fixture
def service(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    instance = StudioEpisodeService(
        tmp_path / "data",
        project_root=root,
        codex_project_id="ap-vibe-local",
    )
    yield instance
    instance.close()


def test_audience_list_is_valid_project_content_not_an_unverified_identity(service):
    sections = refresh_sections()
    sections['identity']['audience'] = ['个人创作者', '项目维护者']
    sections['identity']['purpose'] = {'summary':'保存长期媒体项目和来源'}
    result = service.organization._validate_refresh_sections(sections)
    assert result['identity']['audience'] == ['个人创作者', '项目维护者']
    sections['identity']['audience'] = []
    with pytest.raises(ContractError, match='identity_unverified'):
        service.organization._validate_refresh_sections(sections)


def test_explained_unknown_scores_do_not_need_magic_words_or_fake_evidence(service):
    from ap_mind.project_documents import assessment_quality
    sections = refresh_sections()
    for item in sections['risks']['assessment']:
        item.update(score=None, evidence_refs=[], reason='项目只有设计文档，没有可执行程序及生产观测。')
    result = service.organization._validate_refresh_sections(sections)
    quality = assessment_quality(result['risks']['assessment'])
    assert quality['documented'] == 10 and quality['count'] == 0 and quality['pending'] == 10
    assert quality['issues'] == []
    result['risks']['assessment'][0]['risk'] = ''
    assert 'assessment_risk_missing:intent' in assessment_quality(result['risks']['assessment'])['issues']


def refresh_sections(name: str = "已核对项目") -> dict:
    assessment = [
        {
            "key": key,
            "score": 50,
            "reason": f"隔离合同测试覆盖了 {key} 维度的基本路径，真实运行仍需单独核验。",
            "risk": "测试范围有限，不能把这次结果当成长期项目成熟度。",
            "improvement": "补充可定位的运行记录和回读结果后重新评分。",
            "evidence_refs": [f"test://project-refresh/{key}"],
        }
        for key, _label in DIMENSIONS
    ]
    return {
        "identity": {
            "name": name,
            "summary": "一个有明确维护目标、并按证据持续更新的项目。",
            "purpose": "验证项目档案刷新合同。",
            "audience": "项目维护者。",
        },
        "requirements": {
            "goals": ["档案必须能被下一次任务继续使用。"],
            "acceptance": ["每次刷新都保留 11 章和十维评估。"],
            "redlines": ["不读取凭据值，不把未知写成事实。"],
        },
        "architecture": {
            "summary": "任务冻结项目身份，校验档案后以新 revision 写回。",
            "flow": ["准备范围", "读取来源", "校验完整档案", "事务写回"],
            "modules": ["organization", "project_documents"],
            "intentional_design": ["旧版本可回读，失败不覆盖旧档案。"],
        },
        "sources": {
            "documents": [
                {"title": "测试入口", "path": "test://project-refresh", "purpose": "合同证据"}
            ],
            "credential_location": "未配置；测试不读取凭据。",
        },
        "decisions": {
            "items": [
                {
                    "id": "refresh-contract",
                    "status": "active",
                    "old_logic": "章节数量完整就视为合格。",
                    "new_logic": "逐章校验内容和十维评估，未知保留证据边界。",
                    "reason": "避免自动识别内容冒充已核对档案。",
                    "evidence_refs": ["test://project-refresh/decision"],
                }
            ]
        },
        "work": {
            "completed": ["实现项目刷新边界校验。"],
            "remaining": ["补充真实运行证据。"],
            "not_implemented": ["长期收益测量。"],
            "blocked": [],
            "next_action": "核对真实项目入口并更新证据。",
        },
        "risks": {"assessment": assessment, "unknown": ["长期运行结果尚未测量。"]},
        "evidence": {
            "checks": ["项目刷新单元测试通过。"],
            "discoveries": ["失败写回保留 draft。"],
            "incidents": [],
        },
        "dependencies": {
            "items": [
                {"name": "本地 SQLite", "purpose": "保存版本和幂等记录", "status": "测试中", "entry": "test://sqlite"}
            ]
        },
        "status": {"summary": "合同测试已通过，真实项目仍需核验。", "phase": "合同验收", "unknown": ["真实运行性能"]},
        "recovery": {
            "summary": "先读项目档案，再查看最近证据。",
            "next_action": "从项目刷新任务的 draft 和当前 revision 继续。",
            "entry_points": ["test://project-refresh"],
            "do_not_repeat": ["不要重跑已完成的历史整理。"],
        },
    }


def prepare_refresh(instance: StudioEpisodeService, project_id: str, request_id: str):
    return instance.organization.prepare(
        {"request_id": request_id, "scope": "project_refresh", "project_ids": [project_id]}
    )["task"]


def test_scored_template_explanations_need_repair_but_unknowns_remain_readable(service):
    sections = refresh_sections()
    first, second = sections["risks"]["assessment"][:2]
    second["reason"] = first["reason"]
    with pytest.raises(ContractError, match="organization_assessment_repeated_explanation"):
        service.organization._validate_refresh_sections(sections)
    for item in sections["risks"]["assessment"]:
        item["score"] = None
        item["reason"] = "证据不足：当前只有设计目标，尚未运行真实场景。"
    assert service.organization._validate_refresh_sections(sections)["risks"]["assessment"][0]["score"] is None


def test_refresh_scope_is_registered_and_idempotent(service: StudioEpisodeService):
    project = service.organization.create_project({"request_id": "create-refresh-scope", "display_name": "刷新范围项目"})["project"]
    payload = {"request_id": "prepare-refresh-scope", "scope": "project_refresh", "project_ids": [project["project_id"]]}

    first = service.organization.prepare(payload)
    task = first["task"]
    assert task["scope"] == "project_refresh"
    assert task["result"]["project_ids"] == [project["project_id"]]
    assert task["result"]["frozen_projects"][0]["expected_revision"] == 0
    assert task["total"] == 1
    assert service.organization.prepare(payload)["replayed"] is True

    with pytest.raises(ContractError, match="organization_task_request_conflict"):
        service.organization.prepare({**payload, "project_ids": ["ap-vibe-local"]})
    with pytest.raises(ContractError, match="organization_project_not_registered"):
        service.organization.prepare({"request_id": "prepare-refresh-unknown", "scope": "project_refresh", "project_ids": ["missing"]})


def test_runner_preserves_completed_projects_and_resumes_only_failures(service, monkeypatch):
    projects = [service.organization.create_project({"request_id": f"shard-{i}", "display_name": f"项目 {i}"})["project"] for i in range(2)]
    task = service.organization.prepare({"request_id": "shards", "scope": "project_refresh", "project_ids": [p["project_id"] for p in projects]})["task"]
    calls = []
    fail = True

    def execute(_organization, _task_id, cwd, _prompt, _output, _timeout):
        project_id = json.loads((cwd / "index.json").read_text(encoding="utf-8"))["projects"][0]["project_id"]
        calls.append(project_id)
        sections = refresh_sections(project_id)
        if fail and project_id == projects[1]["project_id"]:
            sections["risks"]["assessment"] = []
        return {"project": {"project_id": project_id, "expected_revision": 0, "sections": sections}}, {"session_id": "test-shard"}

    monkeypatch.setattr(runner, "_run_once", execute)
    monkeypatch.setattr(runner, "_max_retries", lambda: 0)
    runner.run(service.organization, task["task_id"], "http://test")
    failed = service.organization.task(task["task_id"])
    assert failed["status"] == "failed"
    assert failed["result"]["completed_projects"] == [projects[0]["project_id"]]
    assert failed["result"]["draft_projects"] == []
    assert service.task_context.documents.latest(projects[0]["project_id"])["revision"] == 1
    assert service.task_context.documents.latest(projects[1]["project_id"]) is None

    # Recreate the job manager to demonstrate that in-memory state isn't used.
    from ap_mind.organization import Organization
    service.organization = Organization(service)
    fail = False
    runner.run(service.organization, task["task_id"], "http://test")
    complete = service.organization.task(task["task_id"])
    assert complete["status"] == "completed"
    assert calls.count(projects[0]["project_id"]) == 1
    assert calls.count(projects[1]["project_id"]) == 2
    assert len(complete["result"]["project_receipts"]) == 2
    assert all(service.task_context.documents.latest(p["project_id"])["revision"] == 1 for p in projects)
    runner.run(service.organization, task["task_id"], "http://test")
    assert len(calls) == 3
    assert all(service.task_context.documents.latest(p["project_id"])["revision"] == 1 for p in projects)


def test_refresh_transaction_rolls_back_every_project(service, monkeypatch):
    projects = [service.organization.create_project({"request_id": f"atomic-{i}", "display_name": f"项目 {i}"})["project"] for i in range(2)]
    task = service.organization.prepare({"request_id": "atomic", "scope": "project_refresh", "project_ids": [p["project_id"] for p in projects]})["task"]
    update = service.task_context.documents.update
    def fail_second(project_id, *args, **kwargs):
        if project_id == projects[1]["project_id"]:
            raise OSError("simulated disk write failure")
        return update(project_id, *args, **kwargs)
    monkeypatch.setattr(service.task_context.documents, "update", fail_second)
    with pytest.raises(OSError):
        service.organization.apply_result(task["task_id"], {"projects": [{"project_id": p["project_id"], "expected_revision": 0, "sections": refresh_sections()} for p in projects], "skipped": []})
    assert all(service.task_context.documents.latest(p["project_id"]) is None for p in projects)


def test_runner_receipt_is_atomic_and_does_not_replay_over_newer_edits(service, monkeypatch):
    project_id = service.organization.create_project({"request_id": "durable", "display_name": "持久项目"})["project"]["project_id"]
    task = prepare_refresh(service, project_id, "durable-refresh")
    candidate = {"project_id": project_id, "expected_revision": 0, "sections": refresh_sections()}
    original = service.organization.job_state
    def fail_receipt(*args, **kwargs):
        raise OSError("receipt disk failure")
    monkeypatch.setattr(service.organization, "job_state", fail_receipt)
    with pytest.raises(OSError):
        service.organization.commit_refresh_project(task["task_id"], candidate)
    assert service.task_context.documents.latest(project_id) is None
    monkeypatch.setattr(service.organization, "job_state", original)
    receipt = service.organization.commit_refresh_project(task["task_id"], candidate)
    assert receipt["revision"] == 1
    service.task_context.documents.update(project_id, "user", "user", {
        "request_id": "later-user-edit", "expected_revision": 1,
        "sections": {"status": {"summary": "用户后续修正"}}}, authority="user_edited")
    assert service.organization.commit_refresh_project(task["task_id"], candidate) == receipt
    assert service.task_context.documents.latest(project_id)["revision"] == 2
    assert service.task_context.documents.latest(project_id)["sections"]["status"]["summary"] == "用户后续修正"


def test_runner_commit_checks_revision_and_preserves_human_chapter(service):
    project_id = service.organization.create_project({"request_id": "human", "display_name": "人工项目"})["project"]["project_id"]
    task = prepare_refresh(service, project_id, "human-refresh")
    service.task_context.documents.update(project_id, "user", "user", {
        "request_id": "human-status", "expected_revision": 0,
        "sections": {"status": {"summary": "用户指定保留"}}}, authority="user_edited")
    candidate = {"project_id": project_id, "expected_revision": 0, "sections": refresh_sections()}
    with pytest.raises(ContractError, match="document_revision_conflict"):
        service.organization.commit_refresh_project(task["task_id"], candidate)
    assert not service.organization.task(task["task_id"])["result"].get("completed_projects")
    candidate["expected_revision"] = 1
    service.organization.commit_refresh_project(task["task_id"], candidate)
    assert service.task_context.documents.latest(project_id)["sections"]["status"]["summary"] == "用户指定保留"


def test_bootstrap_and_dossier_write_do_not_wait_for_teacher(service):
    done = threading.Event()
    outcomes = []
    def maintain():
        try:
            receipt = service.task_context.bootstrap({"request_id": "responsive-context", "cwd": service.default_project.root_path, "session_id": "responsive-session", "goal": "继续更新项目"})
            outcomes.append(service.task_context.update_knowledge({"request_id": "responsive-dossier", "receipt_id": receipt["receipt_id"], "session_id": "responsive-session", "expected_revision": 0, "sections": refresh_sections()}))
        except Exception as error:
            outcomes.append(error)
        finally:
            done.set()
    with service._lock:
        worker = threading.Thread(target=maintain)
        worker.start()
        responsive = done.wait(3)
    worker.join(5)
    assert responsive
    assert len(outcomes) == 1 and isinstance(outcomes[0], dict), outcomes
    assert outcomes[0]["ok"] is True


def test_refresh_rejects_incomplete_result_and_keeps_old_document(service: StudioEpisodeService):
    project = service.organization.create_project({"request_id": "create-refresh-invalid", "display_name": "不完整档案项目"})["project"]
    task = prepare_refresh(service, project["project_id"], "prepare-refresh-invalid")
    bad = refresh_sections()
    bad["risks"] = {"assessment": []}

    with pytest.raises(ContractError, match="organization_project_assessment_incomplete"):
        service.organization.apply_result(
            task["task_id"],
            {"projects": [{"project_id": project["project_id"], "expected_revision": 0, "sections": bad}], "skipped": []},
        )

    failed = service.organization.task(task["task_id"])
    assert failed["status"] == "failed"
    assert failed["result"]["draft"]["projects"][0]["project_id"] == project["project_id"]
    assert service.task_context.documents.latest(project["project_id"]) is None


def test_refresh_writes_all_sections_and_exact_replay_is_safe(service: StudioEpisodeService):
    project = service.organization.create_project({"request_id": "create-refresh-valid", "display_name": "完整刷新项目"})["project"]
    task = prepare_refresh(service, project["project_id"], "prepare-refresh-valid")
    result = {"projects": [{"project_id": project["project_id"], "expected_revision": 0, "sections": refresh_sections()}], "skipped": []}

    completed = service.organization.apply_result(task["task_id"], result)
    assert completed["status"] == "completed"
    profile = service.task_context.documents.profile(service.product_registry.get(project["project_id"]))
    assert profile["documentation_quality"] == "ready"
    assert profile["maintained_section_count"] == 11
    assert profile["assessment_count"] == 10
    assert service.task_context.documents.latest(project["project_id"])["revision"] == 1

    replayed = service.organization.apply_result(task["task_id"], result)
    assert replayed["status"] == "completed"
    assert replayed["result"] == completed["result"]
    assert service.task_context.documents.latest(project["project_id"])["revision"] == 1


def test_refresh_revision_conflict_does_not_overwrite_newer_document(service: StudioEpisodeService):
    project = service.organization.create_project({"request_id": "create-refresh-conflict", "display_name": "冲突项目"})["project"]
    task = prepare_refresh(service, project["project_id"], "prepare-refresh-conflict")
    service.task_context.documents.update(
        project["project_id"],
        "user-session",
        "user-receipt",
        {"request_id": "newer-document", "expected_revision": 0, "sections": {"identity": {"name": "人工更新", "summary": "人工先更新的简介"}}},
        authority="user_edited",
    )

    with pytest.raises(ContractError, match="document_revision_conflict"):
        service.organization.apply_result(
            task["task_id"],
            {"projects": [{"project_id": project["project_id"], "expected_revision": 0, "sections": refresh_sections("旧版本") }], "skipped": []},
        )

    latest = service.task_context.documents.latest(project["project_id"])
    assert latest["revision"] == 1
    assert latest["sections"]["identity"]["name"] == "人工更新"
    assert service.organization.task(task["task_id"])["status"] == "failed"
