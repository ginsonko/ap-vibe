"""Cross-client curation contracts with real native files and local HTTP/MCP."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys

import pytest
from test_session_directory import server, register, codex, write
from test_dsh_sessions import rows, write_rows
from test_project_refresh import refresh_sections
from ap_mind import organization_execution as execution, organization_runner as runner
from ap_mind.contracts import ContractError
from ap_mind.external_sessions import ExternalSessions
from tools import ap_vibe_mcp


def native_sources(service, root, tmp_path):
    dsh = tmp_path / "dsh/sessions/project/one/session.v3.jsonl"
    records = rows()
    records[0].update(id="one", cwd=str(root))
    write_rows(dsh, records)
    service.external_sessions = ExternalSessions(tmp_path / "data", roots={"dsh":[str(tmp_path / "dsh/sessions")]}, cache_seconds=0)
    cc = service.claude_sessions.roots[0] / "project/one.jsonl"
    write(cc, [{"type":"user", "uuid":"u1", "sessionId":"one", "cwd":str(root),
                "message":{"role":"user","content":"请补充同一个支付项目的退款设计。"}}])
    return dsh, cc


def no_codex(monkeypatch):
    def missing():
        raise ContractError("codex_cli_missing")
    monkeypatch.setattr(runner, "codex_command", missing)
    monkeypatch.setattr("ap_mind.agent_studio.claude_executable", lambda:None)


def test_single_wrapped_json_is_recovered_without_calling_model(tmp_path):
    assert runner._decode_final(tmp_path/"missing", '方案如下\n```json\n{"groups": [], "skipped": []}\n```\n请核验') == {"groups":[],"skipped":[]}
    with pytest.raises(ValueError):
        runner._decode_final(tmp_path/"missing", '```json\n{"a":1}\n```\n```json\n{"a":2}\n```')


def test_no_codex_native_mcp_curation_and_refresh_keep_history(server, tmp_path, monkeypatch):
    service, root = server
    paths = native_sources(service, root, tmp_path)
    before = [p.read_bytes() for p in paths]
    no_codex(monkeypatch)
    directory = ap_vibe_mcp.invoke("ap_vibe_organization_list", {"scope":"all_unclassified"})
    assert {x["harness"] for x in directory["items"]} == {"dsh", "claude"}
    assert directory["executors"]["default"] == "external"
    prepared = ap_vibe_mcp.invoke("ap_vibe_organization_prepare", {"request_id":"native-all",
        "scope":"all_unclassified", "source_keys":[x["source_key"] for x in directory["items"]]})
    task = prepared["task"]
    assert task["status"] == "awaiting_client"
    bundle = ap_vibe_mcp.invoke("ap_vibe_organization_read", {"task_id":task["task_id"]})
    assert {x["harness"] for x in bundle["index"]["sources"]} == {"dsh", "claude"}
    texts = "".join((Path(bundle["folder"]) / x["context_file"]).read_text("utf-8") for x in bundle["index"]["sources"])
    assert "public answer" in texts and "退款设计" in texts and "PRIVATE" not in texts
    proposal = {"groups":[{"name":"跨客户端支付项目", "source_keys":[x["source_key"] for x in directory["items"]],
        "rationale":"两份实际来源描述同一目标", "evidence_refs":[x["source_key"] for x in directory["items"]],
        "sections":refresh_sections()}], "skipped":[]}
    file = tmp_path / "proposal.json"
    file.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
    args = {"task_id":task["task_id"], "handoff_id":bundle["handoff_id"], "file_path":str(file)}
    saved = ap_vibe_mcp.invoke("ap_vibe_organization_submit", args)
    assert saved["status"] == "completed"
    assert ap_vibe_mcp.invoke("ap_vibe_organization_submit", args)["status"] == "completed"
    pid = saved["result"]["completed_projects"][0]
    assert service.task_context.projects.membership("claude","one")["project_id"] == pid
    assert service.task_context.projects.membership("dsh","one")["project_id"] == pid
    assert service.organization.catalog("all_unclassified")["total"] == 0
    # User edits win over a model's stale/overenthusiastic replacement.
    service.organization.update_document({"request_id":"human-decision", "project_id":pid,
        "expected_revision":1, "sections":{"decisions":{"items":["用户决定保留：退款要对账"], "notes":["人工备注"]}}})
    refreshed = ap_vibe_mcp.invoke("ap_vibe_organization_prepare", {"request_id":"native-refresh",
        "scope":"project_refresh","project_ids":[pid]})["task"]
    bundle2 = ap_vibe_mcp.invoke("ap_vibe_organization_read", {"task_id":refreshed["task_id"]})
    assert {x["harness"] for x in bundle2["index"]["sources"]} == {"dsh","claude"}
    assert service.organization.task(refreshed["task_id"])["status"] == "awaiting_client"
    result = {"projects":[{"project_id":pid,"expected_revision":2,"evidence_refs":["test://native-refresh"],
                          "sections":refresh_sections()}], "skipped":[]}
    file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    done = ap_vibe_mcp.invoke("ap_vibe_organization_submit", {"task_id":refreshed["task_id"],
        "handoff_id":bundle2["handoff_id"],"file_path":str(file)})
    assert done["status"] == "completed"
    doc = service.task_context.documents.latest(pid)
    assert doc["revision"] == 3 and len(doc["sections"]) == 11
    assert "用户决定保留" in json.dumps(doc["sections"]["decisions"], ensure_ascii=False)
    assert len(doc["sections"]["risks"]["assessment"]) == 10
    assert [p.read_bytes() for p in paths] == before


def test_application_filter_keeps_same_id_in_different_clients(server, tmp_path):
    service, root = server
    native_sources(service, root, tmp_path)
    register(service, root, "codex-one", "one", [codex(0)])
    catalog = service.organization.catalog("rebuild_all")
    assert {x["harness"] for x in catalog["items"]} == {"codex","claude","dsh"}
    filtered = service.organization.catalog("rebuild_all", harness="dsh")
    assert filtered["total"] == 1 and filtered["items"][0]["harness"] == "dsh"
    assert set(filtered["applications"]) == {"codex","claude","dsh"}
    assert len({x["source_key"] for x in catalog["items"]}) == 3


def test_external_invalid_and_late_proposals_cannot_overwrite(server, tmp_path, monkeypatch):
    service, root = server
    native_sources(service, root, tmp_path)
    task = service.organization.prepare({"request_id":"late", "scope":"all_unclassified"})["task"]
    service.organization.dispatch({"task_id":task["task_id"],"executor":"external"},"http://localhost")
    bundle = service.organization.external_bundle(task["task_id"])
    with pytest.raises(ContractError):
        service.organization.submit_external({"task_id":task["task_id"], "handoff_id":bundle["handoff_id"],"result":{}})
    assert service.organization.task(task["task_id"])["status"] == "failed"
    assert service.task_context.projects.membership("dsh","one") is None
    monkeypatch.setattr(runner, "codex_command", lambda:["unused"])
    monkeypatch.setattr(runner, "run", lambda *a:None)
    service.organization.dispatch({"task_id":task["task_id"],"executor":"codex"},"http://localhost")
    with pytest.raises(ContractError, match="handoff_changed"):
        service.organization.submit_external({"task_id":task["task_id"],"handoff_id":bundle["handoff_id"],"result":{}})


def test_internal_shards_are_excluded_at_any_depth(server, tmp_path):
    service, root = server
    paths = native_sources(service, service.data_dir/"curation-jobs/task/shards/hash", tmp_path)
    assert service.organization.catalog("rebuild_all")["total"] == 0
    assert all("内部整理" in x["reason"] for x in service.organization.catalog("rebuild_all")["excluded"])


def test_claude_contract_has_only_read_tools_and_no_mcp_writes(server, monkeypatch):
    service, root = server
    monkeypatch.setattr("ap_mind.agent_studio.claude_executable", lambda:"claude-fixture")
    with execution.claude_launch(service.organization, "task", {"id":"claude"}, root) as (args, env):
        assert args[args.index("--tools")+1] == "Read,Glob,Grep"
        assert args[args.index("--permission-mode")+1] == "dontAsk"
        assert json.loads(args[args.index("--mcp-config")+1]) == {"mcpServers":{}}
        assert json.loads(args[args.index("--settings")+1])["disableAllHooks"]
        assert env["AP_VIBE_READONLY_CURATION"] == "1"


def test_structured_contract_uses_real_ids_and_existing_assessments():
    from ap_mind.organization_schema import output_schema
    from ap_mind.project_documents import DIMENSIONS, SECTION_INFO
    index = {"scope":"all_unclassified","sources":[{"source_key":"dsh-same"},{"source_key":"claude-same"}],
             "projects":[{"project_id":"existing"}]}
    schema = output_schema(index)
    group = schema["properties"]["groups"]["items"]["properties"]
    assert group["source_keys"]["items"]["enum"] == ["dsh-same","claude-same"]
    assert group["sections"]["required"] == list(SECTION_INFO)
    assessment = group["sections"]["properties"]["risks"]["properties"]["assessment"]["items"]
    assert assessment["properties"]["key"]["enum"] == [key for key,_ in DIMENSIONS]
    assert "null" in assessment["properties"]["score"]["type"]
    index["scope"]="project_refresh"
    assert output_schema(index)["properties"]["project"]["properties"]["project_id"]["enum"] == ["existing"]
    assert output_schema({"scope":"logic_analysis"}) is None


def test_claude_completion_requires_current_success_and_namespaced_resume(server, tmp_path, monkeypatch):
    from ap_mind import organization_claude
    service, root = server
    task = service.organization.prepare_logic({"request_id":"cc-stream","project_id":"fixture-project","question":"核对入口"})["task"]
    script = tmp_path/"fake_cli.py"
    script.write_text('import json,sys\nsys.stdin.read()\nprint(json.dumps({"type":"system","session_id":"cc-one"}))\nprint(json.dumps({"type":"result","is_error":False,"result":\'{"ok":true}\'}))\n')
    @contextmanager
    def launch(*args):
        yield [sys.executable,str(script)], {}
    monkeypatch.setattr(organization_claude,"claude_launch",launch)
    out = root/"result.json"
    result, meta = organization_claude.run_once(service.organization,task["task_id"],root,"read",out,30,
        {"id":"agent:first","kind":"claude","name":"test"})
    assert result == {"ok":True} and meta["harness"] == "claude"
    assert json.loads((root/"claude-runner-session.json").read_text())["executor"] == "agent:first:local"
    script.write_text('import json,sys\nsys.stdin.read()\nprint(json.dumps({"type":"result","is_error":True,"errors":["API 401"]}))\n')
    with pytest.raises(ContractError,match="API 401"):
        organization_claude.run_once(service.organization,task["task_id"],root,"read",out,30,
            {"id":"agent:second","kind":"claude","name":"test"})
    assert json.loads(out.read_text()) == {"ok":True}  # Old file retained, never returned as new success.
