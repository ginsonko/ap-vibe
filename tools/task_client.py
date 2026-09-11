"""Bounded local AP-Vibe client for Codex tasks and lifecycle hooks."""

from __future__ import annotations

if __name__ == '__main__':
    from active_entry import forward
    forward(__file__)

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time
from urllib import request, error
import uuid


MAX_HOOK_CONTEXT_BYTES = 1800
REQUEST_TIMEOUT_SECONDS = 12
BOOTSTRAP_TIMEOUT_SECONDS = 5
WRITE_RETRY_ATTEMPTS = 3
READ_RETRY_ATTEMPTS = 2
_HOOK_EVENTS = {"SessionStart", "UserPromptSubmit", "SubagentStart"}


def _config_path() -> Path:
    if os.environ.get('AP_VIBE_CONFIG_PATH'):
        return Path(os.environ['AP_VIBE_CONFIG_PATH']).expanduser().resolve()
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "AP-Vibe/config.json"


def _installed_config() -> dict:
    return json.loads(_config_path().read_text(encoding="utf-8-sig"))


def schedule_update_check():
    """A task records interest; the detached worker coalesces network checks."""
    try:
        config = _installed_config()
        update_options = dict(config.get('updates') or {})
        option_path = _config_path().parent / 'update-options.json'
        if option_path.exists():update_options.update(json.loads(option_path.read_text(encoding='utf-8-sig')))
        if config.get('auto_start') is False or update_options.get('enabled') is False:
            return
        status_path = _config_path().parent / 'update-status.json'
        if status_path.exists():
            status = json.loads(status_path.read_text(encoding='utf8'))
            if status.get('next_check_at',0) > time.time():return
        script = Path(config['product_root']) / 'tools/update_client.py'
        if not script.is_file():return
        subprocess.Popen([config['python'],str(script),'check','--config',str(_config_path())],
            cwd=config['product_root'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),close_fds=True)
    except (OSError,ValueError,KeyError):
        pass


def _service_url(installed: dict) -> str:
    host = installed.get("host")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("AP-Vibe must use loopback")
    return f"http://{'[::1]' if host == '::1' else host}:{int(installed['port'])}/"


def _health(installed: dict, timeout: float = 0.7) -> bool:
    url = _service_url(installed).rstrip("/") + "/v1/health"
    req = request.Request(url, method="GET")
    opener = request.build_opener(request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            payload = json.loads(response.read(64 * 1024).decode("utf-8"))
            return response.status == 200 and payload.get("status") == "ok"
    except (OSError, ValueError, error.URLError, json.JSONDecodeError):
        return False


@contextmanager
def _startup_lock(installed: dict):
    lock_path = _config_path().parent / "daemon-start.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    try:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            acquired = True
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 30:
                    lock_path.unlink(missing_ok=True)
                    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, str(os.getpid()).encode("ascii"))
                    os.close(fd)
                    acquired = True
            except OSError:
                pass
        yield acquired
    finally:
        if acquired:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


def _start_daemon(installed: dict) -> dict:
    url = _service_url(installed)
    if _health(installed):
        return {"status": "online", "url": url, "started": False}
    if installed.get('auto_start') is False:
        return {"status": "unavailable", "url": url, "started": False,
                "reason": "此客户端绑定独立服务实例，失联后等待该实例恢复，不启动其它工作台。"}
    script = Path(str(installed.get("script_path") or ""))
    if not script.is_file():
        root = Path(str(installed.get("product_root") or ""))
        script = root / "scripts" / "ap-vibe.ps1"
    if not script.is_file():
        return {"status": "unavailable", "url": url, "started": False, "reason": "启动脚本不存在"}
    powershell = next((candidate for candidate in ("pwsh.exe", "powershell.exe", "pwsh", "powershell")
                       if shutil.which(candidate)), None)
    if not powershell:
        return {"status": "unavailable", "url": url, "started": False, "reason": "未找到 PowerShell"}
    config_dir = str(_config_path().parent)
    with _startup_lock(installed) as acquired:
        if not acquired:
            # Another Codex process is already starting the same daemon.
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                if _health(installed):
                    return {"status": "online", "url": url, "started": False, "coalesced": True}
                time.sleep(0.15)
            return {"status": "unavailable", "url": url, "started": False, "reason": "另一启动任务未在有限时间内完成"}
        try:
            subprocess.Popen(
                [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                 "-Action", "start", "-ConfigDir", config_dir, "-SkipOpen"],
                cwd=str(script.parent.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True,
            )
        except OSError as exc:
            return {"status": "unavailable", "url": url, "started": False, "reason": f"启动命令失败：{exc}"}
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            try:
                current = _installed_config()
            except (OSError, ValueError):
                current = installed
            if _health(current):
                return {"status": "online", "url": _service_url(current), "started": True}
            time.sleep(0.15)
    return {"status": "unavailable", "url": url, "started": True, "reason": "健康检查超时"}


def call(route: str, payload: dict | None = None) -> dict:
    installed = _installed_config()
    # Collaboration is a sibling API to task context; keep the same bounded
    # client/startup/retry semantics while routing it to its own namespace.
    prefix = "/v1/ap-vibe/" if route.split('?')[0] in {"collaboration", "agents", "studio/tasks", "sessions", "sessions/read"} or route.startswith(("collaboration/", "studio/", "agents/")) else "/v1/ap-vibe/tasks/"
    url = _service_url(installed).rstrip("/") + prefix + route
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    opener = request.build_opener(request.ProxyHandler({}))
    start_info = None
    timeout = BOOTSTRAP_TIMEOUT_SECONDS if route == "bootstrap" else REQUEST_TIMEOUT_SECONDS
    idempotent_write = route in {"update", "feedback", "classify", "collaboration/message", "collaboration/broadcast",
        "studio/tasks/save", "studio/tasks/claim", "studio/tasks/release", "studio/tasks/archive", "studio/tasks/verdict", "agents/review",
        "studio/plans/submit", "studio/plans/cancel",
        "agents/setup/templates", "agents/setup/connections"}
    attempts = WRITE_RETRY_ATTEMPTS if idempotent_write else READ_RETRY_ATTEMPTS
    startup_attempted = False
    last_network_error = None

    def deferred() -> dict:
        request_id = str((payload or {}).get("request_id") or "")
        session_id = str((payload or {}).get("session_id") or "")
        return {
            "ok": False,
            "source_mode": "online_write_deferred" if idempotent_write else "online_context_deferred",
            "retryable": True,
            "pending": idempotent_write,
            "route": route,
            "request_id": request_id,
            "error": ("服务端可能仍在处理这次写入；已保留同一 request_id，下一次可安全重试。"
                       if idempotent_write
                       else "项目上下文读取超时，已保留在线状态；下一次提示会重试。"),
            "receipt_id": "deferred-" + hashlib.sha256((request_id or uuid.uuid4().hex).encode()).hexdigest()[:32],
            "project_id": "unresolved" if route == "bootstrap" else None,
            "session_id": session_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hook_context": ("AP-Vibe 工作台在线：" + str((start_info or {}).get("url") or _service_url(installed)) +
                             "\n项目资料读取较慢，已降级为稍后重试；原任务继续。") if route == "bootstrap" else "",
            "service": start_info or {"status": "unknown", "url": _service_url(installed)},
            "vibe_formal_write": False,
        }

    for attempt in range(attempts):
        try:
            # Rebuild the Request for every attempt.  The body is immutable,
            # and reusing the same request_id makes the server-side operation
            # idempotent when the previous response was lost.
            req = request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with opener.open(req, timeout=timeout) as response:
                raw = response.read(512 * 1024 + 1)
                if len(raw) > 512 * 1024:
                    raise ValueError("AP-Vibe response exceeds read budget; request fewer sections")
                result = json.loads(raw.decode("utf-8"))
                if start_info and start_info.get("status") != "online":
                    start_info = {"status": "online", "url": _service_url(installed),
                                  "started": start_info.get("started", False),
                                  "recovered_after_retry": True, "recovery_attempt": start_info}
                if route == "bootstrap":
                    result["service"] = start_info or {"status": "online", "url": _service_url(installed), "started": False}
                    schedule_update_check()
                elif start_info and start_info.get("status") == "online":
                    result.setdefault("service", start_info)
                return result
        except error.HTTPError as exc:
            # Preserve the daemon's safe causal code and next action for hooks;
            # retry only transient server failures, never a contract error.
            try:
                detail = json.loads(exc.read(512 * 1024).decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                detail = {}
            if 500 <= exc.code < 600 and attempt < attempts - 1:
                time.sleep(0.2 * (attempt + 1))
                continue
            detail_error = detail.get("error") if isinstance(detail, dict) else None
            if isinstance(detail_error, dict):
                code = detail_error.get("code") or "http_error"
                message = detail_error.get("message") or "AP-Vibe 请求没有完成"
                solution = detail_error.get("solution") or "请查看本地服务状态后重试"
                raise ValueError(f"{message} ({code})；{solution}") from exc
            raise ValueError(f"AP-Vibe 请求失败（HTTP {exc.code}）") from exc
        except (error.URLError, TimeoutError, ConnectionError) as exc:
            last_network_error = exc
            if not startup_attempted and route != "status":
                startup_attempted = True
                start_info = _start_daemon(installed)
                if start_info.get("status") == "online":
                    installed = _installed_config()
                    url = start_info["url"].rstrip("/") + prefix + route
                    continue
            if attempt < attempts - 1:
                time.sleep(0.2 * (attempt + 1))
                continue
            if idempotent_write or route == "bootstrap":
                return deferred()
            raise

    if idempotent_write or route == "bootstrap":
        return deferred()
    if last_network_error:
        raise last_network_error
    raise ValueError("AP-Vibe 请求没有完成")


def cache_path(cwd: str, session_id: str, client_kind: str | None = None) -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "AP-Vibe/task-clients"
    identity = os.path.normcase(str(Path(cwd).resolve())) + "\0" + session_id
    kind = client_kind or os.environ.get('AP_VIBE_CLIENT_KIND', 'codex')
    if kind != 'codex':
        identity += '\0runtime:' + kind
    if os.environ.get('AP_VIBE_CONFIG_PATH'):
        identity += '\0instance:' + os.path.normcase(str(Path(os.environ['AP_VIBE_CONFIG_PATH']).resolve()))
    key = hashlib.sha256(identity.encode()).hexdigest()
    return root / (key + ".json")


def read_knowledge(payload: dict) -> dict:
    """Serve an explicitly requested chapter set through bounded API reads."""
    sections = payload.get("sections") or []
    if len(sections) <= 3:
        return call("knowledge", payload)
    merged = None
    for offset in range(0, len(sections), 3):
        request_payload = {**payload, "sections": sections[offset:offset + 3]}
        if merged and merged.get("revision", 0) > 0:
            request_payload["revision"] = merged["revision"]
        value = call("knowledge", request_payload)
        if value.get("ok") is False:
            return value
        if merged is None:
            merged = {**value, "sections": dict(value.get("sections") or {})}
        else:
            if value.get("project_id") != merged.get("project_id") or value.get("revision") != merged.get("revision"):
                raise ValueError("项目档案在读取时发生变化，请重新读取；没有混合不同版本的章节")
            merged["sections"].update(value.get("sections") or {})
            if value.get("assessment"):
                merged["assessment"] = value["assessment"]
    return merged


def readonly_session_id(cwd: str) -> str:
    """Stable identity for integrations that only need read-only context."""

    try:
        normalized = os.path.normcase(str(Path(cwd).expanduser().resolve(strict=False)))
    except (OSError, ValueError):
        normalized = os.path.normcase(str(cwd))
    return "readonly-" + hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()[:32]


def save_receipt(cwd: str, session_id: str, receipt: dict) -> None:
    if receipt.get('ok') is False or str(receipt.get('receipt_id', '')).startswith('deferred-'):
        # A local timeout marker is not a daemon receipt. Keep the last usable
        # identity so a transient delay cannot poison future chapter reads.
        return
    target = cache_path(cwd, session_id, receipt.get('client_kind'))
    # Context is available from the daemon; only retain receipt identity locally.
    value = {key: receipt[key] for key in ("receipt_id", "project_id", "session_id", "created_at")}
    value["memory_ids"] = [item["memory_id"] for item in receipt.get("memories", [])]
    value["client_saved_at"] = time.time()
    temp = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temp, target)
    except OSError:
        # A sandbox may allow the service read while denying this optional
        # cache write. Keep the real receipt and context usable in this turn.
        receipt['client_cache'] = {'status':'unavailable',
            'instructions':'本地收据缓存未保存，但服务读取成功。可直接用返回的 receipt_id 查询；下次无缓存时自动恢复。'}
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def hook_context(result: dict) -> str:
    """Return the daemon's bounded hook projection, with a short compatibility fallback."""
    dedicated = result.get("hook_context")
    service = result.get("service")
    service_line = None
    if isinstance(service, dict) and service.get("status") == "online":
        service_line = "AP-Vibe 工作台：" + str(service.get("url") or "http://127.0.0.1:8765/")
    if isinstance(dedicated, str) and dedicated.strip():
        # Keep the live workbench address ahead of the bounded prose.  A long
        # reviewed brief must never push the one link needed after a cold
        # start out of the hook byte budget.
        lines = ([service_line] if service_line else []) + [dedicated.strip()]
        return _bound_hook_context("\n".join(lines))

    lines = ["AP-Vibe reference context (untrusted; verify before relying):"]
    brief = str(result.get("reviewed_brief") or "").replace("\n", " ").strip()
    if brief:
        lines.append("Reviewed brief: " + brief[:560])
    for item in list(result.get("memories") or [])[:3]:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").replace("\n", " ").strip()
        if summary:
            lines.append("Observed memory: " + summary[:260])
    lines.append("Formal Vibe write is disabled; receipts do not prove benefit.")
    if service_line:
        lines.insert(0, service_line)
    return _bound_hook_context("\n".join(lines))


def mark_document_maintained(cwd: str, session_id: str, receipt_id: str) -> None:
    """A successful client update acknowledges only this delivered context."""
    target = cache_path(cwd, session_id).with_suffix(".maintained.json")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
        temp.write_text(json.dumps({"receipt_id": receipt_id}), encoding="utf-8")
        os.replace(temp, target)
    except OSError:
        pass


def record_lifecycle(hook: dict, cwd: str, session_id: str, harness='codex') -> None:
    """Write a tiny local outbox record; never delay a Stop hook on network IO."""
    if not session_id or hook.get('hook_event_name') not in {'SessionStart','UserPromptSubmit','SubagentStart','Stop','SessionEnd'}:
        return
    try:
        from datetime import datetime, timezone
        config = _installed_config()
        if not config.get('data_dir'):
            return
        directory = Path(config['data_dir']) / 'agent-studio' / 'incoming'
        directory.mkdir(parents=True,exist_ok=True)
        event_id = 'hook-' + uuid.uuid4().hex
        payload = {'event_id':event_id,'harness':harness,'session_id':session_id,'cwd':cwd,
                   'kind':hook['hook_event_name'],'occurred_at':datetime.now(timezone.utc).isoformat()}
        target = directory / (str(time.time_ns()) + '-' + event_id + '.json')
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf8')
        os.replace(temporary,target)
    except (OSError,ValueError,TypeError):
        pass


def reset_closure_reminder(hook: dict, cwd: str, session_id: str) -> None:
    if hook.get('hook_event_name') != 'UserPromptSubmit' or not session_id:
        return
    if str(hook.get('prompt') or '').lstrip().startswith('<hook_prompt'):
        return
    try:
        cache_path(cwd, session_id).with_suffix('.closure-reminded.json').unlink(missing_ok=True)
    except OSError:
        pass


def stop_hook_output(hook: dict, cwd: str, session_id: str) -> dict:
    """One local-only closure pass; never query/start a service while stopping."""
    if hook.get("stop_hook_active") or not session_id or os.environ.get("AP_VIBE_READONLY_CURATION") == "1":
        return {}
    if not _has_task_activity(hook, cwd, session_id):
        return {}
    target = cache_path(cwd, session_id)
    try:
        receipt = json.loads(target.read_text(encoding="utf-8"))
        age = time.time() - float(receipt.get("client_saved_at", 0))
        if not receipt.get("receipt_id") or receipt.get("session_id") != session_id or not 0 <= age <= 86400:
            return {}
        marker = target.with_suffix(".maintained.json")
        if marker.exists() and json.loads(marker.read_text(encoding="utf-8")).get("receipt_id") == receipt["receipt_id"]:
            return {}
        reminder = target.with_suffix('.closure-reminded.json')
        if reminder.exists():
            return {}
        # Some desktop versions do not provide stop_hook_active on their
        # continuation. Persist one reminder until the next real user prompt.
        reminder.write_text(json.dumps({'session_id':session_id,'receipt_id':receipt['receipt_id']}),encoding='utf8')
    except (OSError, ValueError, TypeError):
        return {}
    return {"decision": "block", "reason": (
        "请完成本任务的 AP-Vibe 档案收尾，然后正常交付。使用 ap-vibe-task-context 技能："
        "先判断这是否为可长期维护的项目；普通问答、公告和一次性任务不新建项目，无需额外工作。"
        "长期项目先读取当前项目目录，若尚未归类则依据真实资料归入已有项目或创建项目，"
        "检查11章与十维评估并只补充本次真实变化；保留旧决策、事故、人工修改、连接位置与未完成事项。"
        "未知分数保持null，不凭空编造；已更新或没有资料变化时无需重复写入。"
        "实际采用的记忆需要归因反馈。服务不可用时保存待写回patch并说明即可，不阻塞交付。"
        "这是一次有界收尾，不要扩大用户任务、重复工程或创建其他任务。"
    )}


def _has_task_activity(hook: dict, cwd: str, session_id: str) -> bool:
    """Don't add a continuation to a plain answer that performed no work.

    Inspect only public completion metadata for the current turn. The normal
    Skill remains the primary maintenance path when this metadata is absent.
    """
    path_value, turn_id = hook.get("transcript_path"), hook.get("turn_id")
    if not isinstance(path_value, str) or not isinstance(turn_id, str):
        return False
    path = Path(path_value)
    if path.suffix != ".jsonl":
        return False
    try:
        with path.open("rb") as stream:
            meta = json.loads(stream.readline(65536))
            identity = meta.get("payload") or {}
            if meta.get("type") != "session_meta" or identity.get("id") != session_id or Path(identity.get("cwd", "")).resolve() != Path(cwd).resolve():
                return False
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 1024 * 1024))
            if stream.tell(): stream.readline()
            for line in stream:
                event = json.loads(line)
                payload = event.get("payload") or {}
                if event.get("type") != "event_msg" or payload.get("type") != "item_completed" or payload.get("turn_id") != turn_id:
                    continue
                item = payload.get("item") or {}
                if item.get("type") in {"FileChange", "CommandExecution", "McpToolCall", "WebSearch", "ImageGeneration"}:
                    return True
    except (OSError, ValueError, TypeError):
        pass
    return False


def _bound_hook_context(value: str) -> str:
    # Bytes provide a conservative token ceiling even for multilingual context.
    return value.encode("utf-8")[:MAX_HOOK_CONTEXT_BYTES].decode("utf-8", errors="ignore")


def hook_output(event: str, result: dict) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": hook_context(result)}}


def main() -> int:
    if os.environ.get("AP_VIBE_READONLY_CURATION") == "1":
        print("{}" if "hook" in sys.argv else "AP-Vibe: read-only curation uses its frozen local bundle; lifecycle writes are skipped.")
        return 0
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["bootstrap", "feedback", "status", "hook", "knowledge", "update", "sessions", "session-read", "tool"])
    parser.add_argument("--name", help="Shared AP-Vibe MCP tool name for the shell fallback")
    parser.add_argument("--config", type=Path, help="Use this installation's local configuration")
    parser.add_argument("--harness", help="Filter session source (for example codex, claude, hermes or opencode)")
    parser.add_argument("--project-id")
    parser.add_argument("--query")
    parser.add_argument("--source-id")
    parser.add_argument("--filter-session-id")
    parser.add_argument("--filter-cwd")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--after", type=int)
    parser.add_argument("--before", type=int)
    parser.add_argument("--generation")
    parser.add_argument("--sections", default="")
    parser.add_argument("--revision", type=int)
    parser.add_argument("--file")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--session-id", default=(os.environ.get('AP_VIBE_SESSION_ID') or
        (os.environ.get('CODEX_THREAD_ID', '') if os.environ.get('AP_VIBE_CLIENT_KIND', 'codex') == 'codex' else '')))
    parser.add_argument("--goal", default="Continue the current task")
    parser.add_argument("--include-history", action="store_true", help="Read historical recovery and relevant observation details on demand")
    parser.add_argument("--request-id")
    parser.add_argument("--receipt-id")
    parser.add_argument("--adopted", action="append", default=[])
    parser.add_argument("--decision")
    parser.add_argument("--outcome")
    parser.add_argument("--evidence", action="append", default=[])
    args = parser.parse_args()
    caller_identity = {}
    if os.environ.get('AP_VIBE_CLIENT_KIND', 'codex') != 'codex':
        caller_identity['client_kind'] = os.environ['AP_VIBE_CLIENT_KIND']
    if os.environ.get('AP_VIBE_SELECTED_PROJECT_ID'):
        caller_identity['selected_project_id'] = os.environ['AP_VIBE_SELECTED_PROJECT_ID']
    if args.config:
        os.environ['AP_VIBE_CONFIG_PATH'] = str(args.config.expanduser().resolve())
    try:
        if args.action == "hook":
            raw = sys.stdin.buffer.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise ValueError("hook input exceeds budget")
            hook = json.loads(raw)
            event = hook.get("hook_event_name", "UserPromptSubmit")
            args.cwd = hook.get("cwd") or args.cwd
            args.session_id = hook.get("session_id") or hook.get("thread_id") or args.session_id
            args.goal = str(hook.get("prompt") or args.goal)[:2000]
            reset_closure_reminder(hook,args.cwd,args.session_id)
            record_lifecycle(hook,args.cwd,args.session_id)
            if event == "Stop":
                print(json.dumps(stop_hook_output(hook, args.cwd, args.session_id), ensure_ascii=False))
                return 0
            if event not in _HOOK_EVENTS:
                print("{}")
                return 0
        if args.action == 'tool':
            # The CLI uses exactly the MCP adapter, including its schemas and
            # idempotent task writes. It is available without a running MCP host.
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from tools.ap_vibe_mcp import invoke
            if not args.name:
                raise ValueError('tool requires --name; arguments use an optional UTF-8 JSON --file')
            payload = {}
            if args.file:
                path = Path(args.file)
                if path.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError('tool argument file exceeds 2MB')
                payload = json.loads(path.read_text(encoding='utf-8-sig'))
            result = invoke(args.name, payload)
        elif args.action in {'sessions', 'session-read'}:
            from urllib.parse import urlencode
            if args.action == 'sessions':
                query = {'harness':args.harness, 'cwd':args.filter_cwd, 'session_id':args.filter_session_id,
                         'project_id':args.project_id, 'query':args.query, 'offset':args.offset, 'limit':args.limit}
                route = 'sessions'
            else:
                if not args.source_id:
                    raise ValueError('session-read requires --source-id from the sessions directory')
                query = {'source_id':args.source_id, 'after':args.after, 'before':args.before,
                         'generation':args.generation, 'limit':args.limit}
                route = 'sessions/read'
            result = call(route + '?' + urlencode({k:v for k,v in query.items() if v is not None}))
        elif args.action == "status":
            result = call("status")
        else:
            if not args.session_id:
                if args.action in {"bootstrap", "hook", "knowledge"}:
                    # Read-only retrieval remains usable from a lightweight
                    # integration without CODEX_THREAD_ID.  update/feedback
                    # still require a real task identity.
                    args.session_id = readonly_session_id(args.cwd)
                else:
                    raise ValueError("当前应用的真实会话身份未提供；用 --session-id 指定原生会话ID。仍可查询会话和项目目录。")
            request_id = args.request_id or "task-client-" + uuid.uuid4().hex
            if args.action in {"bootstrap", "hook"}:
                result = call("bootstrap", {**caller_identity, "request_id": request_id, "cwd": args.cwd,
                                           "session_id": args.session_id, "goal": args.goal[:2000],
                                           **({'lifecycle_event':event} if args.action=='hook' else {}),
                                           **({'include_history': True} if args.include_history else {})})
                save_receipt(args.cwd, args.session_id, result)
            else:
                receipt_id = args.receipt_id
                if not receipt_id:
                    cache = cache_path(args.cwd, args.session_id)
                    try:
                        receipt_id = json.loads(cache.read_text(encoding="utf-8"))["receipt_id"]
                        if not isinstance(receipt_id, str) or receipt_id.startswith('deferred-'):
                            raise KeyError('No committed receipt in cache')
                    except (OSError, KeyError, TypeError, json.JSONDecodeError):
                        if args.action != "knowledge":
                            raise ValueError("没有可用的 AP-Vibe receipt；先执行 bootstrap")
                        # A direct read from a new integration should not be
                        # gated on a lifecycle hook having run first.  Create
                        # a bounded read-only receipt, then continue with the
                        # requested sections using the same session identity.
                        bootstrap_id = "auto-bootstrap-" + uuid.uuid4().hex
                        bootstrap = call("bootstrap", {**caller_identity, "request_id": bootstrap_id, "cwd": args.cwd,
                                                       "session_id": args.session_id, "goal": args.goal[:2000]})
                        if not bootstrap.get("ok"):
                            result = bootstrap
                            print(json.dumps(result, ensure_ascii=False, indent=2))
                            return 0
                        save_receipt(args.cwd, args.session_id, bootstrap)
                        receipt_id = bootstrap["receipt_id"]
                if args.action == "knowledge":
                    result = read_knowledge({"receipt_id": receipt_id, "session_id": args.session_id,
                                               "sections": [key.strip() for key in args.sections.split(",") if key.strip()],
                                               "revision": args.revision})
                elif args.action == "update":
                    if not args.file:
                        raise ValueError("update requires --file with expected_revision and sections")
                    path = Path(args.file)
                    try:
                        capacity = int(os.environ.get("AP_VIBE_DOCUMENT_PATCH_BYTES", "524288"))
                    except ValueError:
                        capacity = 524288
                    # Allow formatting overhead; the service checks normalized
                    # chapter bytes against the shared configured capacity.
                    if path.stat().st_size > 2 * (capacity if capacity > 0 else 524288) + 4096:
                        raise ValueError("knowledge patch exceeds byte budget")
                    patch = json.loads(path.read_text(encoding="utf-8-sig"))
                    if not isinstance(patch, dict) or set(patch) - {"request_id", "expected_revision", "sections"}:
                        raise ValueError("patch accepts only request_id, expected_revision, sections")
                    # A saved request id ensures a lost response can be retried
                    # without silently appending a second version.
                    if not isinstance(patch.get("request_id"), str):
                        raise ValueError("save a stable request_id in the patch before update")
                    result = call("update", {**patch, "receipt_id": receipt_id, "session_id": args.session_id})
                    if result.get("ok") and not result.get("write_deferred"):
                        mark_document_maintained(args.cwd, args.session_id, receipt_id)
                else:
                    # Repeating the same CLI feedback command after a lost
                    # response must address the same durable outcome.  An
                    # explicit --request-id still wins for callers that need
                    # to distinguish two deliberately different reports.
                    if not args.request_id:
                        stable_body = {"receipt_id": receipt_id, "session_id": args.session_id,
                                       "adopted_memory_ids": args.adopted,
                                       "decision": args.decision or "",
                                       "outcome": args.outcome or "",
                                       "evidence_refs": args.evidence}
                        request_id = "context-outcome-" + hashlib.sha256(
                            json.dumps(stable_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                        ).hexdigest()[:32]
                    result = call("feedback", {"request_id": request_id, "session_id": args.session_id,
                                          "receipt_id": receipt_id, "adopted_memory_ids": args.adopted,
                                          "decision": args.decision, "outcome": args.outcome,
                                          "evidence_refs": args.evidence})
        if args.action == "hook":
            print(json.dumps(hook_output(event, result), ensure_ascii=False))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, error.URLError) as exc:
        failure = {"ok": False, "source_mode": "unavailable", "retryable": True,
                   "error": str(exc)[:240], "vibe_formal_write": False}
        # A local outage must not block the user's original task or substitute
        # another project's cache. Context can be retried on the next prompt.
        print("{}" if args.action == "hook" else json.dumps(failure, ensure_ascii=False))
        return 0 if args.action == "hook" else 1


if __name__ == "__main__":
    raise SystemExit(main())
