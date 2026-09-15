"""Read-only Claude Code curation with native completion and request accounting."""
import json
import os
import subprocess
import threading
import time

from .contracts import ContractError, utc_now
from .project_documents import clean
from .organization_execution import claude_launch, resume_identity


def run_once(organization, task_id, cwd, prompt, output, timeout, executor):
    from .organization_runner import _read_json, _write_json, _decode_final, _terminate_process_tree, _idle_timeout_seconds
    session_file = cwd / "claude-runner-session.json"
    prior = _read_json(session_file) or {}
    identity = resume_identity(executor)
    resume = prior.get("session_id") if (prior.get("task_id") == task_id and
        prior.get("cwd") == str(cwd.resolve()) and prior.get("executor") == identity) else None
    process = None
    stop = threading.Event()
    last_event = [time.monotonic()]
    expired = []
    diagnostics = []
    started = utc_now()
    session_id = None
    final = None
    failure = ""
    try:
        with claude_launch(organization, task_id, executor, cwd, resume) as (args, env):
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(cwd), env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            organization.register_runner_process(task_id, process)
            def watch():
                start = time.monotonic()
                while not stop.wait(1):
                    now = time.monotonic()
                    if now - start >= timeout or now - last_event[0] >= _idle_timeout_seconds():
                        expired.append("total_timeout" if now - start >= timeout else "idle_timeout")
                        _terminate_process_tree(process)
                        return
            def errors():
                for line in process.stderr:
                    diagnostics.append(clean(line.decode("utf-8", errors="replace"))[:1000])
                    del diagnostics[:-8]
            watcher = threading.Thread(target=watch, daemon=True)
            drain = threading.Thread(target=errors, daemon=True)
            watcher.start()
            drain.start()
            process.stdin.write(prompt.encode("utf-8"))
            process.stdin.close()
            for line in process.stdout:
                last_event[0] = time.monotonic()
                if len(line) > 4 * 1024 * 1024:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if event.get("type") == "system" and event.get("session_id"):
                    session_id = event["session_id"]
                    _write_json(session_file, {"task_id": task_id, "cwd": str(cwd.resolve()),
                                              "session_id": session_id, "executor": identity})
                    organization.job_state(task_id, "running", {"session_id": session_id,
                        "session_harness": "claude", "phase": executor["name"] + " 正在读取真实来源"})
                if event.get("type") == "result":
                    session_id = event.get("session_id") or session_id
                    if event.get("is_error"):
                        failure = clean(str(event.get("errors") or event.get("result") or event.get("subtype")))[:1500]
                    else:
                        structured = event.get("structured_output")
                        final = json.dumps(structured, ensure_ascii=False) if isinstance(structured, dict) else event.get("result")
            code = process.wait(timeout=10)
            drain.join(timeout=1)
            if final is None or failure or code != 0:
                detail = failure or (expired[0] if expired else "\n".join(diagnostics)[-1500:] or "未收到成功结束标记")
                raise ContractError("organization_claude_execution_failed:" + detail)
            if not isinstance(final, str):
                raise ContractError("organization_claude_result_invalid")
            # Never parse an earlier attempt's result.json as this call's output.
            output.write_text(final, encoding="utf-8")
            result = _decode_final(output, final)
            return result, {"session_id": session_id, "harness": "claude", "executor": executor["id"],
                            "started_at": started, "finished_at": utc_now()}
    finally:
        stop.set()
        if process and process.poll() is None:
            _terminate_process_tree(process)
        organization.unregister_runner_process(task_id, process)
