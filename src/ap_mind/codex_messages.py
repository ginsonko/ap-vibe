"""Durable, explicit workbench messages to existing local Codex tasks.

Queued messages are handed to the user's existing Codex task through the
official local queue command. A process interruption may have already performed
work, so an uncertain dispatch is never replayed silently.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import uuid

from .contracts import ContractError, utc_now
from .organization_runner import codex_command, _write_json, _read_json, _terminate_process_tree
from .project_documents import clean
from .codex_cli import supports_queue


def source_state(source):
    """Read public lifecycle markers, never expose reasoning or tool payloads."""
    path = Path(source.source_path)
    try:
        with path.open("rb") as stream:
            meta = json.loads(stream.readline(65537))
            payload = meta.get("payload", {})
            if meta.get("type") != "session_meta" or payload.get("id") != source.session_id:
                raise ContractError("message_source_identity_changed")
            cwd = Path(payload.get("cwd", "")).resolve()
            if cwd != Path(source.session_cwd).resolve() or not cwd.is_dir():
                raise ContractError("message_source_directory_changed")
            size = path.stat().st_size
            stream.seek(max(0, size - 512 * 1024))
            if stream.tell():
                stream.readline()
            lines = stream.readlines()
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") != "event_msg":
                continue
            kind = event.get("payload", {}).get("type")
            if kind in {"task_complete", "turn_aborted"}:
                return "idle", cwd
            if kind == "task_started":
                return "busy", cwd
        return "unknown", cwd
    except ContractError:
        raise
    except (OSError, ValueError, TypeError):
        raise ContractError("message_source_unavailable")


class CodexMessages:
    @staticmethod
    def _public(record):
        # File offsets are internal resume state, not part of the delivery
        # contract. Windows paths can exceed the document-key limit.
        public = {k:v for k,v in record.items() if k != 'reconcile_offsets'}
        if len(public.get('message','')) > 12000:
            public['message'] = public['message'][:12000]
            public['message_truncated'] = True
        return clean(public)

    def __init__(self, service):
        self.service = service
        self.folder = service.data_dir / "codex-messages"
        self.folder.mkdir(exist_ok=True, parents=True)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.workers = {}
        self.processes = {}
        for path in self.folder.glob("*.json"):
            record = _read_json(path)
            if record and record.get("status") == "running":
                record.update(status="uncertain", detail="服务中断，消息是否已排入 Codex 未确定。请先查看任务结果，不会自动重复发送。", updated_at=utc_now())
                _write_json(path, record)

    def _path(self, request_id):
        return self.folder / (hashlib.sha256(request_id.encode()).hexdigest() + ".json")

    def _records(self, session_id):
        records = [r for p in self.folder.glob("*.json") if (r := _read_json(p)) and r.get("session_id") == session_id]
        return sorted(records, key=lambda r: r["created_at"])

    @staticmethod
    def _item_text(item):
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, list):
            parts = [entry.get("text", "") for entry in content if isinstance(entry, dict) and isinstance(entry.get("text"), str)]
            return "\n".join(parts).strip()
        value = item.get("text") if isinstance(item, dict) else None
        return value.strip() if isinstance(value, str) else ""

    def _reconcile_submitted(self, record, source):
        """Promote an accepted queue message after the source task completes.

        Only public ``event_msg`` lifecycle data is inspected.  The queue
        command is intentionally acknowledged before the task runs, so this
        readback keeps the UI honest without claiming that acceptance is a
        completed response.
        """
        if record.get("status") != "submitted":
            return
        cutoff = record.get("dispatch_started_at") or record.get("created_at", "")
        path = Path(source.source_path)
        offsets = dict(record.get('reconcile_offsets') or {})
        source_key = str(path.resolve())
        try:
            size = path.stat().st_size
            offset = int(offsets.get(source_key, 0))
            if offset > size:offset = 0
            if offset == size:return
            with path.open("rb") as stream:
                stream.seek(offset)
                lines = []
                while stream.tell() - offset < 512 * 1024:
                    start = stream.tell()
                    line = stream.readline(2 * 1024 * 1024 + 1)
                    if not line:break
                    if not line.endswith(b'\n'):
                        # An unfinished line must be revisited after the
                        # writer appends its end. Oversized non-public data
                        # is skipped without loading it into memory.
                        if len(line) > 2 * 1024 * 1024:
                            while line and not line.endswith(b'\n'):
                                line = stream.readline(65536)
                            if not line:stream.seek(start);break
                            continue
                        stream.seek(start);break
                    lines.append(line)
                offsets[source_key] = stream.tell()
                if record.get('matched_turn_id') and stream.tell() < size:
                    if size - 512 * 1024 > stream.tell():
                        stream.seek(size - 512 * 1024)
                        stream.readline()
                    lines.extend(stream.readlines())
        except OSError:
            return
        turns = {record["matched_turn_id"]} if record.get("matched_turn_id") else set()
        claimed = {r.get("matched_message_id") for r in self._records(record["session_id"])
                   if r.get("request_id") != record.get("request_id") and r.get("matched_message_id")}
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if event.get("type") != "event_msg":
                continue
            payload = event.get("payload") or {}
            if str(event.get("timestamp", "")) < cutoff:
                continue
            kind = payload.get("type")
            if kind == "item_completed":
                item = payload.get("item") or {}
                item_kind = str(item.get("type", "")).lower()
                if item_kind == "usermessage" and self._item_text(item) == record.get("message"):
                    turn_id = payload.get("turn_id") or item.get("turn_id")
                    message_id = item.get("id") or str(turn_id) + ":" + record["message"]
                    if turn_id and message_id not in claimed and not turns:
                        turns.add(turn_id)
                        self._save(record, matched_turn_id=turn_id, matched_message_id=message_id)
            elif kind == "task_complete" and payload.get("turn_id") in turns:
                response = clean(str(payload.get("last_agent_message") or "")[:12000])
                self._save(record, status="completed", detail="Codex 已完成回复，结果已回到工作台。", response=response)
                return
        self._save(record, reconcile_offsets=offsets)

    def _source(self, session_id):
        sources = self.service.product_registry.sources_for_session(session_id)
        sources = [s for s in sources if Path(s.source_path).is_file()]
        if not sources:
            raise ContractError("message_session_not_found")
        # A resumed session can have several rollout files. The newest file
        # carries the current lifecycle, while the session ID remains stable.
        return max(sources, key=lambda s: Path(s.source_path).stat().st_mtime_ns)

    def _save(self, record, **changes):
        with self.lock:
            record.update(changes, updated_at=utc_now())
            _write_json(self._path(record["request_id"]), record)

    def enqueue(self, raw):
        session_id, request_id, message = raw.get("session_id"), raw.get("request_id"), raw.get("message")
        try:
            uuid.UUID(session_id)
        except (ValueError, TypeError, AttributeError):
            raise ContractError("message_session_id_invalid")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise ContractError("message_request_id_required")
        if not isinstance(message, str) or not message.strip() or len(message.encode()) > 64000:
            raise ContractError("message_text_invalid")
        message = message.strip()
        with self.lock:
            prior = _read_json(self._path(request_id))
            if prior:
                if (prior["session_id"], prior["message"]) != (session_id, message):
                    raise ContractError("request_id_conflict")
                return {"ok": True, "replayed": True, "delivery": self._public(prior)}
            source_state(self._source(session_id))
            record = dict(request_id=request_id, session_id=session_id, message=message,
                          status="queued", detail="已接收，等待当前任务空闲后发送。", created_at=utc_now(), updated_at=utc_now())
            _write_json(self._path(request_id), record)
            self._start(session_id)
            return {"ok": True, "replayed": False, "delivery": self._public(record)}

    def _start(self, session_id):
        with self.lock:
            if self.stop.is_set() or self.workers.get(session_id, None) and self.workers[session_id].is_alive():
                return
            worker = threading.Thread(target=self._work, args=(session_id,), daemon=True)
            self.workers[session_id] = worker
            worker.start()

    def read_delivery(self, session_id, request_id):
        """Reconcile one known message without starting or replaying a sender."""
        with self.lock:
            record = _read_json(self._path(request_id))
            if not record or record.get('session_id') != session_id:
                return None
            if record.get('status') == 'submitted':
                try:
                    sources = self.service.product_registry.sources_for_session(session_id)
                    for source in sources:
                        if record.get('status') != 'submitted':break
                        self._reconcile_submitted(record, source)
                except ContractError:
                    pass
            return self._public(record)

    def resume_queued_delivery(self, session_id, request_id):
        """Background recovery of unsent work, separate from read-only GETs."""
        with self.lock:
            record = _read_json(self._path(request_id))
            if not record or record.get('session_id') != session_id or record.get('status') != 'queued':
                return False
            self._start(session_id)
            return True

    def list(self, session_id):
        with self.lock:
            records = self._records(session_id)
            try:
                source = self._source(session_id)
                for record in records:
                    self._reconcile_submitted(record, source)
                records = self._records(session_id)
            except ContractError:
                pass
            if any(r["status"] == "queued" for r in records):
                self._start(session_id)
            try:
                transport = "desktop_queue" if supports_queue(tuple(codex_command())) else "cli_resume"
            except ContractError:
                transport = "unavailable"
            return {"ok": True, "session_id": session_id, "transport": transport, "deliveries": [self._public(r) for r in records[-20:]]}

    def cancel(self, raw):
        with self.lock:
            record = _read_json(self._path(str(raw.get("request_id", ""))))
            if not record or record["session_id"] != raw.get("session_id"):
                raise ContractError("message_not_found")
            if record["status"] == "queued":
                self._save(record, status="cancelled", detail="已撤回，未发送给 Codex。")
            return {"ok": True, "delivery": self._public(record)}

    def _work(self, session_id):
        while not self.stop.is_set():
            with self.lock:
                records = self._records(session_id)
                record = next((r for r in records if r["status"] == "queued"), None)
                if not record:
                    self.workers.pop(session_id, None)
                    return
            try:
                status, cwd = source_state(self._source(session_id))
                if status in {"unknown", "busy"} and not supports_queue(tuple(codex_command())):
                    detail = "任务仍在执行，消息已排队；可以撤回尚未发送的消息。" if status == "busy" else "正在等待可确认的任务状态；可以打开原任务查看或撤回消息。"
                    if record["detail"] != detail:
                        with self.lock:
                            current = _read_json(self._path(record["request_id"]))
                            if current and current["status"] == "queued":
                                self._save(current, detail=detail)
                    self.stop.wait(3)
                    continue
                with self.lock:
                    latest = _read_json(self._path(record["request_id"]))
                    if not latest or latest["status"] != "queued":
                        continue
                    self._save(record, status="running", detail="正在连接 Codex 续接当前任务。")
                self._run(record, cwd)
            except Exception as exc:
                self._save(record, status="failed", detail=clean(str(exc)[:900]))

    def _run(self, record, cwd):
        env = dict(os.environ)
        env.pop("CODEX_THREAD_ID", None)
        env.pop("AP_VIBE_READONLY_CURATION", None)
        # CLI resume can continue released local tasks. Desktop-owned tasks
        # may retain a writer even while idle; never steal that ownership.
        command = codex_command()
        native_queue = supports_queue(tuple(command))
        self._save(record, transport="desktop_queue" if native_queue else "cli_resume", dispatch_started_at=utc_now())
        args = command + (["queue", "--thread", record["session_id"], "--message", record["message"]] if native_queue
                          else ["exec", "resume", "--json", "--skip-git-repo-check", record["session_id"], "-"])
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=str(cwd), env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.processes[record["request_id"]] = process
        timeout = max(30, float(os.environ.get("AP_VIBE_MESSAGE_TIMEOUT_SECONDS", "120")))
        timer = threading.Timer(timeout, lambda: _terminate_process_tree(process))
        timer.start()
        try:
            stdout, stderr = process.communicate(input=None if native_queue else record["message"].encode("utf-8"), timeout=timeout + 5)
            code = process.returncode
            output = clean((stdout or b"").decode("utf-8", errors="replace")[-12000:])
            error = clean((stderr or b"").decode("utf-8", errors="replace")[-12000:])
            started = False
            completed = False
            reply = ""
            for line in (stdout or b"").splitlines():
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                started |= event.get("type") == "turn.started"
                completed |= event.get("type") == "turn.completed"
                item = event.get("item") or {}
                if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                    reply = clean(str(item.get("text") or "")[:12000])
            if code == 0:
                if completed:
                    self._save(record, status="completed", submitted_at=utc_now(), detail="Codex 已完成本次回复。", response=reply)
                else:
                    self._save(record, status="submitted", submitted_at=utc_now(), detail="Codex 已接受续接，等待完成回读。", queue_receipt=output[-500:])
            else:
                diagnostic = error or output or f"Codex 退出码 {code}。"
                if not started and "active writer" in diagnostic:
                    self._save(record, status="desktop_required", detail="此任务仍由 Codex 桌面管理，网页没有发送。可复制这条消息，到原任务继续。", diagnostic=diagnostic[:900])
                elif started:
                    self._save(record, status="uncertain", detail="Codex 已开始执行，但没有取得完成回执；请查看原任务，不要重复发送。", diagnostic=diagnostic[:900])
                else:
                    self._save(record, status="failed", detail="Codex 未能启动本次续接，消息仍保留。请查看技术详情。", diagnostic=diagnostic[:900])
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            self._save(record, status="uncertain", detail="排队请求超时，消息是否进入 Codex 未确定；请先查看原任务，不会自动重复发送。")
        except Exception as exc:
            self._save(record, status="uncertain", detail=clean(str(exc)[:900]))
        finally:
            timer.cancel()
            _terminate_process_tree(process)
            self.processes.pop(record["request_id"], None)
            if getattr(self.service, "codex_monitor", None):
                self.service.codex_monitor.wake()

    def shutdown(self):
        self.stop.set()
        for process in list(self.processes.values()):
            _terminate_process_tree(process)
