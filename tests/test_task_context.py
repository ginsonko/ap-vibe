from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ap_mind.contracts import ContractError
from ap_mind.product import DiscoveredCodexSession
from ap_mind.studio_server import StudioEpisodeService
from ap_mind.vibe_mind import ProjectActivity


def test_multisource_receipt_writes_only_while_target_remains_bound(tmp_path):
    root = tmp_path / 'a'
    other = tmp_path / 'b'
    root.mkdir()
    other.mkdir()
    service = StudioEpisodeService(tmp_path / 'data', project_root=root, codex_project_id='a')
    service.register_project({'project_id': 'b', 'display_name': 'B', 'root_path': str(other)})
    registry = service.product_registry
    try:
        for key, project, cwd in [('a', 'a', root), ('b', 'b', other)]:
            path = tmp_path / (key + '.jsonl')
            path.write_text('{}\n', encoding='utf-8')
            registry.register_source(DiscoveredCodexSession(
                source_key=key * 64, source_path=str(path), source_name=path.name,
                session_id='shared', cwd=str(cwd), source_size=3,
                modified_at='2026-09-08T00:00:00Z'), project)
        receipt = service.task_context.bootstrap({'cwd': str(root), 'session_id': 'shared',
                                                 'request_id': 'multi-read', 'goal': 'Maintain A'})
        assert receipt['project_id'] == 'a'
        patch = {'receipt_id': receipt['receipt_id'], 'session_id': 'shared', 'request_id': 'multi-write',
                 'expected_revision': 0, 'sections': {'work': {'completed': ['Preserved A result']}}}
        assert service.task_context.update_knowledge(patch)['revision'] == 1
        assert service.task_context.documents.latest('b') is None
        # Real reassignment must invalidate the old writer, without breaking reading.
        registry.assign_source(request_id='move', source_key='a' * 64, project_id='b', session_id='shared', confidence=None,
                               rationale='User moved the session', evidence_refs=['test://move'], actor='user')
        with pytest.raises(ContractError, match='membership_changed'):
            service.task_context.update_knowledge({**patch, 'request_id': 'stale-write', 'expected_revision': 1})
        assert service.task_context.knowledge({'receipt_id': receipt['receipt_id'], 'session_id': 'shared'})['revision'] == 1
    finally:
        service.close()


def test_delivery_is_project_scoped_attributed_and_durable(tmp_path: Path) -> None:
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    service = StudioEpisodeService(tmp_path / "data", project_root=root_a, codex_project_id="a")
    service.register_project({"project_id": "b", "display_name": "B", "root_path": str(root_b)})
    for project, detail in (("a", "Use the configured PowerShell 7 runtime for lifecycle commands."),
                            ("b", "Do not share this separate project observation.")):
        service.run_project_activity("observation-" + project, ProjectActivity(
            activity_id="memory-" + project, project_id=project, kind="user_observation",
            actor="user", summary=detail, detail=detail, source_ref="user://" + project,
        ).to_dict())
    request = {"request_id": "read-1", "cwd": str(root_a), "session_id": "task-a", "goal": "PowerShell runtime"}
    delivered = service.task_context.bootstrap(request)
    assert delivered["project_id"] == "a"
    assert [item["memory_id"] for item in delivered["memories"]] == ["memory-a"]
    assert delivered["memories"][0]["authority"] == "unreviewed_observation"
    assert delivered["benefit"] == "not_measured"
    assert service.task_context.bootstrap(request)["replayed"]
    with pytest.raises(ContractError, match="request_conflict"):
        service.task_context.bootstrap({**request, "cwd": str(root_b)})
    feedback = {"request_id": "outcome-1", "receipt_id": delivered["receipt_id"], "session_id": "task-a",
                "decision": "Use the configured runtime", "outcome": "The lifecycle command parsed successfully",
                "adopted_memory_ids": ["memory-a"], "evidence_refs": ["test://runtime-parser"]}
    with pytest.raises(ContractError, match="memory_not_delivered"):
        service.task_context.feedback({**feedback, "adopted_memory_ids": ["memory-b"]})
    with pytest.raises(ContractError, match="identity_mismatch"):
        service.task_context.feedback({**feedback, "session_id": "other-task"})
    result = service.task_context.feedback(feedback)
    assert result["episode_id"]
    assert result["authority"] == "agent_reported"
    assert service.task_context.feedback(feedback)["replayed"]
    with pytest.raises(ContractError, match="outcome_conflict"):
        service.task_context.feedback({**feedback, "decision": "a different decision"})
    service.close()
    cold = StudioEpisodeService(tmp_path / "data", project_root=root_a, codex_project_id="a")
    assert cold.task_context.bootstrap(request)["receipt_id"] == delivered["receipt_id"]
    assert cold.task_context.feedback(feedback)["replayed"]
    counts = cold.task_context.status()
    assert counts["served_count"] == counts["reported_outcome_count"] == 1
    assert counts["verified_benefit_count"] == 0
    cold.product_registry.set_auto_monitor("a", False)
    readable = cold.task_context.bootstrap({**request, "request_id": "read-while-paused"})
    assert readable["project_id"] == "a"
    assert readable["organization"]["classification_required"] is False
    assert cold.task_context.knowledge({"receipt_id": readable["receipt_id"], "session_id": "task-a"})["ok"] is True
    cold.close()


def test_delivery_honors_archived_memory_and_redacts_secrets(tmp_path: Path) -> None:
    service = StudioEpisodeService(tmp_path / "data", project_root=tmp_path, codex_project_id="a")
    synthetic_token = "s" + "k-FAKE_TEST_SECRET_1234567890"
    service.run_project_activity("secret-observation", ProjectActivity(
        activity_id="secret", project_id="a", kind="observation", summary="configuration",
        detail="key " + synthetic_token, source_ref="test://configuration",
    ).to_dict())
    request = {"request_id": "read", "cwd": str(tmp_path), "session_id": "task", "goal": "configuration"}
    receipt = service.task_context.bootstrap(request)
    assert "FAKE_TEST_SECRET" not in str(receipt)
    service.product_registry.set_memory_disposition(request_id="archive", project_id="a", activity_id="secret",
                                                    state="archived", reason="incorrect", source_ref="test://archive")
    fresh = service.task_context.bootstrap({**request, "request_id": "read-new"})
    assert fresh["memories"] == []
    service.close()


def test_current_catalog_avoids_stale_history_and_unrelated_observations(tmp_path, monkeypatch):
    service = StudioEpisodeService(tmp_path / 'data', project_root=tmp_path, codex_project_id='a')
    try:
        service.task_context.documents.update('a', 'task', 'test-receipt', {
            'request_id': 'current-document', 'expected_revision': 0,
            'sections': {'status': {'summary': 'Shipping the new reader; previous installation is complete.'}}})
        for key, summary in [('match', 'PowerShell lifecycle'), ('other', 'Garden watering calendar')]:
            service.run_project_activity('observe-' + key, ProjectActivity(
                activity_id=key, project_id='a', kind='observation', summary=summary,
                detail='detail-' + key, source_ref='test://' + key).to_dict())
        calls = []
        def historical(raw):
            calls.append(raw)
            return {'text': 'STALE_NEXT_STEP: install the old manager', 'brief': {'recovery_valid': True}}
        monkeypatch.setattr(service, 'agent_brief', historical)
        request = {'request_id': 'current-entry', 'cwd': str(tmp_path), 'session_id': 'task', 'goal': 'PowerShell'}
        value = service.task_context.bootstrap(request)
        assert not calls and not value['reviewed_brief']
        assert value['knowledge_manifest']['revision'] == 1
        assert [item['memory_id'] for item in value['memories']] == ['match']
        assert 'detail' not in value['memories'][0]
        assert 'STALE_NEXT_STEP' not in value['hook_context'] and 'Garden' not in str(value)
        assert 'previous installation is complete' in value['hook_context']
        assert '8765' not in value['hook_context']
        with pytest.raises(ContractError, match='request_conflict'):
            service.task_context.bootstrap({**request, 'include_history': True})
        expanded = service.task_context.bootstrap({**request, 'request_id': 'history', 'include_history': True})
        assert len(calls) == 1 and 'STALE_NEXT_STEP' in expanded['reviewed_brief']
        assert expanded['memories'][0]['detail'] == 'detail-match'
        assert 'STALE_NEXT_STEP' not in expanded['hook_context']
        unrelated = service.task_context.bootstrap({**request, 'request_id': 'no-match', 'goal': 'Zebras'})
        assert unrelated['memories'] == []
        assert service.task_context.bootstrap(request)['memories'] == value['memories']
    finally:
        service.close()


def test_readonly_bootstrap_keeps_context_after_identity_drift(tmp_path: Path) -> None:
    root = tmp_path / "root"
    moved = tmp_path / "moved"
    root.mkdir()
    moved.mkdir()
    source_path = tmp_path / "session.jsonl"
    source_path.write_text("{}\n", encoding="utf-8")
    service = StudioEpisodeService(tmp_path / "data", project_root=root, codex_project_id="a")
    service.product_registry.register_source(DiscoveredCodexSession(
        source_key="a" * 64, source_path=str(source_path), source_name=source_path.name,
        session_id="drifted-session", cwd=str(root), source_size=source_path.stat().st_size,
        modified_at="2026-09-06T00:00:00Z",
    ), "a")
    try:
        receipt = service.task_context.bootstrap({
            "request_id": "drift-read", "cwd": str(moved), "session_id": "drifted-session",
            "goal": "读取项目资料",
        })
        assert receipt["project_id"] == "a"
        assert receipt["identity_status"] in {"recovered", "ambiguous"}
        assert receipt["identity_issue"] == "codex_binding_identity_changed"
        readback = service.task_context.knowledge({"receipt_id": receipt["receipt_id"], "session_id": "drifted-session"})
        assert readback["ok"] is True
    finally:
        service.close()


def test_readonly_bootstrap_falls_back_for_unknown_workspace(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    service = StudioEpisodeService(tmp_path / "data", project_root=root, codex_project_id="a")
    try:
        receipt = service.task_context.bootstrap({
            "request_id": "unknown-read", "cwd": str(tmp_path / "does-not-exist"),
            "session_id": "unknown-session", "goal": "继续任务",
        })
        assert receipt["project_id"] == "a"
        assert receipt["identity_status"] == "fallback"
        assert receipt["classification"] == "unresolved"
        assert receipt['project_context_role'] == 'landing_reference'
        assert receipt['organization']['classification_advisory'] is True
        assert receipt['organization']['classification_required'] is False
        assert '不是当前任务归属' in receipt['hook_context']
    finally:
        service.close()


def test_readonly_bootstrap_accepts_missing_caller_metadata_without_opening_write_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    service = StudioEpisodeService(tmp_path / "data", project_root=root, codex_project_id="a")
    try:
        receipt = service.task_context.bootstrap({})
        assert receipt["ok"] is True
        assert receipt["readonly_identity"] is True
        assert receipt["session_id"].startswith("readonly-")
        assert receipt["project_id"] == "a"
        # The receipt remains useful even when a lightweight reader does not
        # echo the synthetic session id back to the knowledge endpoint.
        assert service.task_context.knowledge({"receipt_id": receipt["receipt_id"]})["ok"] is True
        with pytest.raises(ContractError, match="write_identity_required"):
            service.task_context.update_knowledge({
                "receipt_id": receipt["receipt_id"],
                "session_id": receipt["session_id"],
                "request_id": "anonymous-update",
                "expected_revision": 0,
                "sections": {"identity": {"name": "should not write"}},
            })
    finally:
        service.close()
