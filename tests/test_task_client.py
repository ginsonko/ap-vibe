from __future__ import annotations

import io
import json
from pathlib import Path
import sys
from urllib import error

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import task_client


def test_tool_fallback_uses_shared_adapter_exact_request_and_custom_config(monkeypatch,tmp_path,capsys):
    from tools import ap_vibe_mcp
    payload={'request_id':'same-id','project_id':'real-project','title':'中文任务','goal':'write',
             'acceptance':'read real file','return_to':{'harness':'codex','session_id':'actual','wake':True}}
    path=tmp_path/'params.json'
    path.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8-sig')
    calls=[]
    monkeypatch.delenv('AP_VIBE_READONLY_CURATION',raising=False)
    monkeypatch.setattr(ap_vibe_mcp.task_client,'call',lambda route,body:calls.append((route,body)) or {'ok':True})
    monkeypatch.setattr(sys,'argv',['task_client.py','tool','--config',str(tmp_path/'config.json'),
        '--name','ap_vibe_task_save','--file',str(path)])
    assert task_client.main()==0
    assert calls==[('studio/tasks/save',payload)]
    assert json.loads(capsys.readouterr().out)['ok']


def test_explicit_full_document_read_batches_and_pins_one_revision(monkeypatch):
    calls = []
    def read(route, payload):
        calls.append(payload)
        return {'ok':True,'project_id':'p','revision':7,'sections':{key:{'summary':key} for key in payload['sections']}}
    monkeypatch.setattr(task_client,'call',read)
    value=task_client.read_knowledge({'receipt_id':'r','session_id':'s','sections':['identity','requirements','architecture','work','risks']})
    assert len(calls)==2 and calls[1]['revision']==7
    assert set(value['sections'])=={'identity','requirements','architecture','work','risks'}


def test_full_document_read_never_merges_other_project(monkeypatch):
    values=iter([{'ok':True,'project_id':'p','revision':1,'sections':{}},{'ok':True,'project_id':'q','revision':1,'sections':{}}])
    monkeypatch.setattr(task_client,'call',lambda *_:next(values))
    with pytest.raises(ValueError,match='不同版本'):
        task_client.read_knowledge({'sections':['identity','requirements','architecture','risks']})


def test_successful_retry_replaces_stale_offline_service_status(monkeypatch):
    installed={'host':'127.0.0.1','port':19876,'auto_start':False}
    monkeypatch.setattr(task_client,'_installed_config',lambda:installed)
    offline={'status':'unavailable','url':'http://127.0.0.1:19876/','started':False,'reason':'Probe failed'}
    monkeypatch.setattr(task_client,'_start_daemon',lambda *_:offline)
    monkeypatch.setattr(task_client.time,'sleep',lambda *_:None)
    bodies=[]
    class Recovering:
        def open(self,req,**kwargs):
            bodies.append(req.data)
            if len(bodies)==1:raise TimeoutError('Initial request unavailable')
            return io.BytesIO(json.dumps({'ok':True,'source_mode':'online_daemon','receipt_id':'actual-receipt'}).encode())
    monkeypatch.setattr(task_client.request,'build_opener',lambda *_:Recovering())
    value=task_client.call('bootstrap',{'request_id':'same-request','session_id':'session'})
    assert value['ok'] and value['service']['status']=='online'
    assert value['service']['url']==task_client._service_url(installed)
    assert value['service']['recovered_after_retry'] and not value['service']['started']
    assert value['service']['recovery_attempt']==offline
    assert len(bodies)==2 and bodies[0]==bodies[1]


def _run_hook(monkeypatch: pytest.MonkeyPatch, event: str, source: str | None = None) -> tuple[int, dict, dict]:
    payload = {
        "hook_event_name": event,
        "cwd": str(Path.cwd()),
        "session_id": "hook-test-session",
        "prompt": "continue the task",
    }
    if source:
        payload["source"] = source
    captured: dict = {}

    def fake_call(route: str, body: dict | None = None) -> dict:
        captured["route"] = route
        captured["body"] = body
        return {
            "ok": True,
            "receipt_id": "delivery-test",
            "project_id": "project-test",
            "session_id": "hook-test-session",
            "hook_context": "AP-Vibe reference context (untrusted):\nUse the reviewed local decision.",
            "reviewed_brief": "long fallback that should not be emitted",
            "memories": [{"memory_id": "m1", "summary": "long fallback memory"}],
        }

    monkeypatch.setattr(task_client, "call", fake_call)
    monkeypatch.setattr(task_client, "save_receipt", lambda *_args: None)
    monkeypatch.setattr(sys, "argv", ["task_client.py", "hook"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8"))))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    result = task_client.main()
    return result, json.loads(output.getvalue()), captured


@pytest.mark.parametrize(
    ("event", "source"),
    [("SessionStart", "startup"), ("SessionStart", "compact"), ("UserPromptSubmit", None)],
)
def test_hook_uses_dedicated_context_for_lifecycle_inputs(
    monkeypatch: pytest.MonkeyPatch, event: str, source: str | None
) -> None:
    result, output, captured = _run_hook(monkeypatch, event, source)
    assert result == 0
    assert captured["route"] == "bootstrap"
    assert output["hookSpecificOutput"]["hookEventName"] == event
    context = output["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("AP-Vibe reference context")
    assert "long fallback" not in context
    assert "delivery-test" not in context
    assert len(context.encode("utf-8")) <= task_client.MAX_HOOK_CONTEXT_BYTES


def test_hook_fallback_is_bounded_and_does_not_serialize_receipt() -> None:
    result = task_client.hook_output(
        "UserPromptSubmit",
        {
            "receipt_id": "private-receipt",
            "reviewed_brief": "brief " * 5000,
            "memories": [{"summary": "memory " * 1000}],
        },
    )
    context = result["hookSpecificOutput"]["additionalContext"]
    assert len(context.encode("utf-8")) <= task_client.MAX_HOOK_CONTEXT_BYTES
    assert "private-receipt" not in context
    assert "Formal Vibe write is disabled" in context


def test_hook_multilingual_context_obeys_byte_budget() -> None:
    context = task_client.hook_context({"hook_context": "\u4e2d\u6587\U0001f680" * 4000})
    assert len(context.encode("utf-8")) <= task_client.MAX_HOOK_CONTEXT_BYTES
    assert "\ufffd" not in context


def test_hook_context_exposes_local_workbench_after_service_recovery() -> None:
    context = task_client.hook_context(
        {
            "hook_context": "AP-Vibe reference context (untrusted):\nReviewed project brief.",
            "service": {"status": "online", "url": "http://127.0.0.1:8765/", "started": True},
        }
    )
    assert "http://127.0.0.1:8765/" in context


def test_hook_context_keeps_workbench_link_when_dedicated_context_is_long() -> None:
    context = task_client.hook_context(
        {
            "hook_context": "长上下文 " * 4000,
            "service": {"status": "online", "url": "http://127.0.0.1:8765/", "started": True},
        }
    )
    assert context.startswith("AP-Vibe 工作台：http://127.0.0.1:8765/")
    assert "http://127.0.0.1:8765/" in context
    assert len(context.encode("utf-8")) <= task_client.MAX_HOOK_CONTEXT_BYTES


def test_start_daemon_is_bounded_and_reports_health(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "AP-Vibe" / "config.json"
    config_path.parent.mkdir()
    script = tmp_path / "product" / "scripts" / "ap-vibe.ps1"
    script.parent.mkdir(parents=True)
    script.write_text("# synthetic test entry", encoding="utf-8")
    monkeypatch.setattr(task_client, "_config_path", lambda: config_path)
    monkeypatch.setattr(task_client.shutil, "which", lambda _name: "powershell.exe")
    monkeypatch.setattr(task_client.subprocess, "Popen", lambda *args, **kwargs: object())
    health = iter([False, True])
    monkeypatch.setattr(task_client, "_health", lambda *_args, **_kwargs: next(health))
    result = task_client._start_daemon({"host": "127.0.0.1", "port": 8765, "script_path": str(script)})
    assert result["status"] == "online"
    assert result["started"] is True


def test_hook_fails_open_when_daemon_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise error.URLError("daemon offline")

    monkeypatch.setattr(task_client, "call", unavailable)
    monkeypatch.setattr(sys, "argv", ["task_client.py", "hook"])
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(
                json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "cwd": str(Path.cwd()),
                        "session_id": "hook-test-session",
                        "prompt": "continue",
                    }
                ).encode("utf-8")
            )
        ),
    )
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    assert task_client.main() == 0
    assert output.getvalue().strip() == "{}"


def test_hook_ignores_unknown_event_without_contacting_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("unknown events must not bootstrap")

    monkeypatch.setattr(task_client, "call", unexpected)
    monkeypatch.setattr(sys, "argv", ["task_client.py", "hook"])
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(
                json.dumps(
                    {
                        "hook_event_name": "SessionEnd",
                        "cwd": str(Path.cwd()),
                        "session_id": "hook-test-session",
                    }
                ).encode("utf-8")
            )
        ),
    )
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    assert task_client.main() == 0
    assert output.getvalue().strip() == "{}"


def test_hook_oversized_input_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["task_client.py", "hook"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{" + b"x" * (256 * 1024) + b"}")))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    assert task_client.main() == 0
    assert output.getvalue().strip() == "{}"


@pytest.mark.parametrize('cached', [None, 'deferred-timeout'])
def test_knowledge_auto_bootstraps_when_no_receipt_cache_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cached
) -> None:
    cache = tmp_path / "missing-receipt.json"
    if cached:
        cache.write_text(json.dumps({'receipt_id': cached}), encoding='utf-8')
    calls: list[tuple[str, dict | None]] = []

    def fake_call(route: str, body: dict | None = None) -> dict:
        calls.append((route, body))
        if route == "bootstrap":
            return {"ok": True, "receipt_id": "auto-receipt", "project_id": "project-test", "session_id": "session-test"}
        return {"ok": True, "project_id": "project-test", "revision": 3, "sections": {"identity": {"name": "可读取"}}}

    monkeypatch.setattr(task_client, "call", fake_call)
    monkeypatch.setattr(task_client, "cache_path", lambda *_args: cache)
    saved: list[dict] = []
    monkeypatch.setattr(task_client, "save_receipt", lambda _cwd, _session, value: saved.append(value))
    monkeypatch.setattr(sys, "argv", ["task_client.py", "knowledge", "--cwd", str(tmp_path), "--session-id", "session-test", "--sections", "identity"])
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    assert task_client.main() == 0
    result = json.loads(output.getvalue())
    assert result["ok"] is True
    assert [route for route, _ in calls] == ["bootstrap", "knowledge"]
    assert saved[0]["receipt_id"] == "auto-receipt"


def test_timeout_does_not_replace_usable_receipt_or_poison_new_client(monkeypatch, tmp_path):
    cache = tmp_path / 'receipt.json'
    monkeypatch.setattr(task_client, 'cache_path', lambda *_: cache)
    valid = {'ok': True, 'receipt_id': 'delivery-actual', 'session_id': 's', 'project_id': 'p', 'created_at': 'now'}
    pending = {**valid, 'ok': False, 'receipt_id': 'deferred-local-marker', 'project_id': 'unresolved'}
    task_client.save_receipt(str(tmp_path), 's', pending)
    assert not cache.exists()
    task_client.save_receipt(str(tmp_path), 's', valid)
    before = cache.read_bytes()
    task_client.save_receipt(str(tmp_path), 's', pending)
    assert cache.read_bytes() == before


@pytest.mark.parametrize('action', ['bootstrap', 'knowledge'])
def test_cache_permission_failure_does_not_erase_successful_context(monkeypatch, tmp_path, action, capsys):
    cache = tmp_path/'receipt.json'
    monkeypatch.setattr(task_client, 'cache_path', lambda *_:cache)
    actual = Path.write_text
    def denied(path, *args, **kwargs):
        if path.suffix == '.tmp':
            raise PermissionError('read-only harness cache')
        return actual(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', denied)
    def call(route, body=None):
        if route == 'bootstrap':
            return {'ok':True, 'receipt_id':'real-online', 'project_id':'p', 'session_id':'s', 'created_at':'now'}
        assert body['receipt_id'] == 'real-online'
        return {'ok':True, 'project_id':'p', 'revision':3, 'sections':{'identity':{'summary':'Actual dossier'}}}
    monkeypatch.setattr(task_client, 'call', call)
    monkeypatch.setattr(sys, 'argv', ['task_client.py',action,'--session-id','s','--cwd',str(tmp_path),'--sections','identity'])
    assert task_client.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] and not cache.exists()
    if action == 'bootstrap':
        assert result['receipt_id'] == 'real-online' and result['client_cache']['status'] == 'unavailable'
    else:
        assert result['sections']['identity']['summary'] == 'Actual dossier'
