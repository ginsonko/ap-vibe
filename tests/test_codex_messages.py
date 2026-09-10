from pathlib import Path
import json
import sys
import threading
from types import SimpleNamespace
import uuid
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ap_mind.codex_messages import CodexMessages, source_state
from ap_mind.contracts import ContractError


def fixture(tmp_path):
    session_id = str(uuid.uuid4())
    path = tmp_path / "rollout.jsonl"
    meta = {"type": "session_meta", "payload": {"id": session_id, "cwd": str(tmp_path)}}
    path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    source = SimpleNamespace(source_path=str(path), session_id=session_id, session_cwd=str(tmp_path))
    service = SimpleNamespace(data_dir=tmp_path, product_registry=SimpleNamespace(sources_for_session=lambda sid: [source] if sid == session_id else []))
    return CodexMessages(service), source


def marker(source, kind):
    with Path(source.source_path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "event_msg", "payload": {"type": kind}}) + "\n")


def test_public_lifecycle_and_changed_identity(tmp_path):
    queue, source = fixture(tmp_path)
    assert source_state(source)[0] == "unknown"
    marker(source, "task_started")
    assert source_state(source)[0] == "busy"
    marker(source, "task_complete")
    assert source_state(source)[0] == "idle"
    source.session_id = str(uuid.uuid4())
    with pytest.raises(ContractError, match="identity_changed"):
        source_state(source)


def test_enqueue_idempotency_cancel_and_restart_uncertain(tmp_path, monkeypatch):
    queue, source = fixture(tmp_path)
    monkeypatch.setattr(queue, "_start", lambda _: None)
    raw = dict(session_id=source.session_id, request_id="request-1", message="继续只读查询")
    first = queue.enqueue(raw)
    assert first["delivery"]["status"] == "queued"
    assert queue.enqueue(raw)["replayed"]
    with pytest.raises(ContractError, match="request_id_conflict"):
        queue.enqueue({**raw, "message": "不同消息"})
    assert queue.cancel(raw)["delivery"]["status"] == "cancelled"
    record = first["delivery"]
    queue._save(record, status="running")
    restored = CodexMessages(queue.service)
    assert restored.list(source.session_id)["deliveries"][0]["status"] == "uncertain"


def test_busy_message_waits_and_cancel_prevents_execution(tmp_path, monkeypatch):
    monkeypatch.setattr('ap_mind.codex_messages.supports_queue', lambda _:False)
    queue, source = fixture(tmp_path)
    marker(source, "task_started")
    called = threading.Event()
    monkeypatch.setattr(queue, "_run", lambda *args: called.set())
    raw = dict(session_id=source.session_id, request_id="request-busy", message="继续")
    queue.enqueue(raw)
    assert not called.wait(.25), 'a busy task must not be resumed before cancellation'
    queue.cancel(raw)
    marker(source, "task_complete")
    queue.shutdown()
    assert not called.wait(.1)
    assert queue.list(source.session_id)["deliveries"][0]["status"] == "cancelled"


def test_idle_dispatch_is_single_and_preserves_shell_characters(tmp_path, monkeypatch):
    queue, source = fixture(tmp_path)
    marker(source, "task_complete")
    done = threading.Event()
    calls = []
    def run(record, cwd):
        calls.append(record["message"])
        queue._save(record, status="completed")
        done.set()
    monkeypatch.setattr(queue, "_run", run)
    raw = dict(session_id=source.session_id, request_id="request-idle", message='测试 `cmd` $(x) "hello"\n中文')
    queue.enqueue(raw)
    queue.enqueue(raw)
    assert done.wait(3)
    assert calls == [raw["message"]]
    queue.shutdown()


def test_submitted_queue_message_reconciles_from_public_task_complete(tmp_path):
    queue, source = fixture(tmp_path)
    message = "只回复固定文本"
    record = {
        "request_id": "request-submitted",
        "session_id": source.session_id,
        "message": message,
        "status": "submitted",
        "detail": "已排入",
        "created_at": "2026-09-07T00:00:00.000000Z",
        "submitted_at": "2026-09-07T00:00:01.000000Z",
    }
    (queue.folder / ("request-submitted".encode().hex() + ".json")).write_text(json.dumps(record), encoding="utf-8")
    events = [
        {"timestamp": "2026-09-07T00:00:02.000000Z", "type": "event_msg", "payload": {"type": "item_completed", "turn_id": "turn-1", "item": {"type": "UserMessage", "content": [{"type": "text", "text": message}]}}},
        {"timestamp": "2026-09-07T00:00:03.000000Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "固定回复"}},
    ]
    with Path(source.source_path).open("a", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(event) + "\n")
    # Use the real hashed path helper so the test does not depend on a path
    # naming detail.
    (queue.folder / ("request-submitted".encode().hex() + ".json")).unlink()
    queue._save(record)
    value = queue.list(source.session_id)
    assert value["deliveries"][0]["status"] == "completed"
    assert value["deliveries"][0]["response"] == "固定回复"
    queue.shutdown()


@pytest.mark.parametrize(('code','events','stderr','expected'), [
    (1, [], b'thread-store conflict: already has an active writer', 'desktop_required'),
    (1, [{'type':'turn.started'}], b'network disconnected', 'uncertain'),
    (0, [{'type':'turn.started'},{'type':'item.completed','item':{'type':'agent_message','text':'真实回复'}},{'type':'turn.completed'}], b'', 'completed'),
])
def test_resume_outcome_distinguishes_ownership_uncertainty_and_real_completion(tmp_path, monkeypatch, code, events, stderr, expected):
    from ap_mind import codex_messages
    monkeypatch.setattr(codex_messages,'supports_queue',lambda _:False)
    queue, source = fixture(tmp_path)
    record={'request_id':'resume-outcome','session_id':source.session_id,'message':'读取状态','status':'running','created_at':'now'}
    class Process:
        returncode=code
        def communicate(self, **_): return b'\n'.join(json.dumps(e,ensure_ascii=False).encode('utf8') for e in events), stderr
        def poll(self): return code
    monkeypatch.setattr(codex_messages.subprocess,'Popen',lambda *a,**k:Process())
    monkeypatch.setattr(codex_messages,'codex_command',lambda:['codex'])
    queue._run(record,tmp_path)
    result=queue._records(source.session_id)[0]
    assert result['status']==expected
    if expected=='completed': assert result['response']=='真实回复'
    if expected=='desktop_required': assert result['message']=='读取状态' and 'active writer' in result['diagnostic']


def test_completion_survives_large_trace_after_user_message(tmp_path, monkeypatch):
    queue, source=fixture(tmp_path)
    monkeypatch.setattr('ap_mind.codex_messages.supports_queue',lambda _:False)
    record={'request_id':'long-turn','session_id':source.session_id,'message':'保持历史','status':'submitted','created_at':'2026-09-07T00:00:00Z','submitted_at':'2026-09-07T00:00:05Z'}
    queue._save(record)
    with Path(source.source_path).open('a',encoding='utf8') as stream:
        stream.write(json.dumps({'timestamp':'2026-09-07T00:00:02Z','type':'event_msg','payload':{'type':'item_completed','turn_id':'long-turn-id','item':{'type':'UserMessage','content':[{'type':'text','text':'保持历史'}]}}})+'\n')
    assert queue.list(source.session_id)['deliveries'][0]['matched_turn_id']=='long-turn-id'
    with Path(source.source_path).open('a',encoding='utf8') as stream:
        stream.write(json.dumps({'type':'ignored-fixture','padding':'x'*600000})+'\n')
        stream.write(json.dumps({'timestamp':'2026-09-07T00:01:00Z','type':'event_msg','payload':{'type':'task_complete','turn_id':'long-turn-id','last_agent_message':'完成'}})+'\n')
    assert queue.list(source.session_id)['deliveries'][0]['status']=='completed'
