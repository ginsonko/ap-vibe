"""Read-only AP-Vibe recovery consumer with one verified portable fallback."""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping, Sequence
from urllib import error as url_error
from urllib import parse as url_parse
from urllib import request as url_request


MANIFEST_PROTOCOL = "ap-vibe.project-recovery-manifest.v1"
SNAPSHOT_PROTOCOL = "ap-vibe.portable-recovery.v1"
EXPECTED_AUTHORITY = "local_reviewed_recovery"
MIN_BRIEF_CHARS = 2_000
MAX_BRIEF_CHARS = 24_000
MAX_HTTP_BYTES = 512 * 1024
MAX_ITEMS = 24
MAX_ITEM_CHARS = 512
HASH_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
SECRET_RE = re.compile(
    r"(?i)(?:\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}\b|"
    r"\bAuthorization\s*[:=]|\bBearer\s+[A-Za-z0-9._-]{12,})"
)
WINDOWS_PATH_RE = re.compile(r"(?i)(?:^|[\s\"'(])(?:[A-Z]:[\\/]|\\\\)[^\s\"'<>]+")


class RecoveryError(RuntimeError):
    """A bounded, user-actionable recovery contract failure."""

    def __init__(self, code: str, solution: str) -> None:
        super().__init__(code)
        self.code = code
        self.solution = solution


class NoRedirect(url_request.HTTPRedirectHandler):
    """Do not let a loopback request redirect project data elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RecoveryError(f"{name}_must_be_object", "刷新 AP-Vibe daemon 后重新恢复。")
    return value


def _text(value: Any, name: str, *, limit: int = 12_000, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > limit:
        raise RecoveryError(f"{name}_invalid", "重新生成一份满足有界恢复合同的 Agent Brief。")
    return value


def _optional_text(value: Any, name: str, *, limit: int = 12_000) -> str | None:
    if value is None:
        return None
    return _text(value, name, limit=limit, empty=True) or None


def _items(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) > MAX_ITEMS:
        raise RecoveryError(f"{name}_invalid", "缩小恢复列表后重新生成 Agent Brief。")
    return [_text(item, f"{name}_item", limit=MAX_ITEM_CHARS) for item in value]


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RecoveryError(f"{name}_invalid", "刷新并校验 AP-Vibe 本地恢复版本。")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise RecoveryError(f"{name}_invalid", "刷新并校验 AP-Vibe 本地恢复合同。")
    return value


def _hash(value: Any) -> str:
    text = _text(value, "content_hash", limit=64)
    if not HASH_RE.fullmatch(text):
        raise RecoveryError("content_hash_invalid", "在 AP-Vibe 中重新校验 revision hash 链。")
    return text.upper()


def _optional_hash(value: Any, name: str) -> str | None:
    if value is None or value == "":
        return None
    text = _text(value, name, limit=64)
    if not HASH_RE.fullmatch(text):
        raise RecoveryError(f"{name}_invalid", "保留可读上下文，并在 AP-Vibe 中重新校验 revision hash 链。")
    return text.upper()


def _authority(value: Any) -> str:
    if value is None:
        return "unknown"
    if not isinstance(value, str) or len(value) > 128:
        raise RecoveryError("authority_invalid", "保留上下文并标记其权威状态后重试。")
    return value.strip() or "unknown"


def _fallback_brief_text(brief: Mapping[str, Any], reason: str | None = None) -> str:
    """Render a bounded read-only envelope when the server omitted prose."""

    lines = [
        "# AP-Vibe Agent Brief（降级只读）",
        f"项目：{brief['project_id']}",
        f"当前目标：{brief['goal']}",
        f"恢复有效：{'是' if brief['recovery_valid'] else '否'}",
        f"知识权威：{brief['authority']}",
        f"当前摘要：{brief.get('summary') or '没有可用的已审摘要；以下内容只能作为未审上下文。'}",
    ]
    if reason:
        lines.append(f"恢复状态：{reason[:160]}")
    lines.extend([
        "",
        "## 下一步",
        f"- {brief['next_action']}",
        "",
        "## 读取边界",
        "- 这是只读恢复结果；未知内容保持未知，不代表正式 Vibe 知识。",
    ])
    if brief.get("latest_delta"):
        delta = brief["latest_delta"]
        lines.extend(["", "## 最新未审增量", f"- {delta.get('summary') or '未提供增量摘要'}"])
    return "\n".join(lines)


def _safe_payload(value: Any) -> None:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if SECRET_RE.search(rendered):
        raise RecoveryError("recovery_contains_secret", "移除恢复知识中的密钥或授权头后重新审阅。")
    if WINDOWS_PATH_RE.search(rendered) or "file://" in rendered.lower():
        raise RecoveryError("recovery_contains_absolute_path", "把绝对路径替换为脱敏来源指针后重新审阅。")


def _manifest() -> tuple[Path, Mapping[str, Any]]:
    skill_root = Path(__file__).resolve().parents[1]
    path = skill_root / "references" / "project.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RecoveryError("project_manifest_unavailable", "恢复或重新生成 Skill 的 references/project.json。") from exc
    data = _object(raw, "project_manifest")
    if data.get("protocol_version") != MANIFEST_PROTOCOL:
        raise RecoveryError("project_manifest_protocol_mismatch", "使用与当前客户端匹配的项目 manifest。")
    return skill_root, data


def _loopback_endpoint(value: str) -> str:
    parsed = url_parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RecoveryError("endpoint_must_be_loopback_http", "使用 http://127.0.0.1:8765 这类明确的本地 endpoint。")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RecoveryError("endpoint_port_invalid", "使用 1-65535 范围内的本地端口。") from exc
    if port is not None and not 1 <= port <= 65_535:
        raise RecoveryError("endpoint_port_invalid", "使用 1-65535 范围内的本地端口。")
    return value.rstrip("/")


def _read_json(request: url_request.Request, timeout: float) -> Mapping[str, Any]:
    opener = url_request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_HTTP_BYTES + 1)
    except url_error.HTTPError as exc:
        raise RecoveryError(f"daemon_http_{exc.code}", "确认 AP-Vibe daemon 版本和本地 API 后重试。") from exc
    except (url_error.URLError, TimeoutError, OSError) as exc:
        raise RecoveryError("daemon_unavailable", "启动 127.0.0.1:8765 的 AP-Vibe daemon，或使用已验证的 portable snapshot。") from exc
    if len(raw) > MAX_HTTP_BYTES:
        raise RecoveryError("daemon_response_too_large", "缩小恢复响应后重试。")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise RecoveryError("daemon_response_invalid_json", "修复本地 daemon 响应后重试。") from exc
    return _object(decoded, "daemon_response")


def _clean_brief(value: Any, project_id: str) -> dict[str, Any]:
    raw = _object(value, "brief")
    if raw.get("project_id") != project_id:
        raise RecoveryError("brief_identity_invalid", "为 ap-vibe-local 的上下文重新生成 Brief。")
    reported_recovery_valid = raw.get("recovery_valid") is True
    protocol_version = _optional_text(raw.get("protocol_version"), "brief_protocol_version", limit=128) or "ap-vibe.agent-brief.v1"
    goal = _optional_text(raw.get("goal"), "brief_goal", limit=2_048) or "继续读取可用的 AP-Vibe 上下文"
    authority = _authority(raw.get("authority"))
    revision_id = _optional_text(raw.get("revision_id"), "brief_revision_id", limit=512)
    content_hash = _optional_hash(raw.get("content_hash"), "brief_content_hash")
    next_action = _optional_text(raw.get("next_action"), "brief_next_action", limit=2_048) or "按需读取可用章节，并保留未知和证据边界"
    latest = raw.get("latest_delta")
    latest_delta = None
    if latest is not None:
        delta = _object(latest, "latest_delta")
        latest_delta = {
            "authority": _authority(delta.get("authority")),
            "summary": _optional_text(delta.get("summary"), "latest_delta_summary", limit=MAX_ITEM_CHARS) or "未提供增量摘要",
            "completed": _items(delta.get("completed"), "latest_delta_completed"),
            "remaining": _items(delta.get("remaining"), "latest_delta_remaining"),
            "unknown": _items(delta.get("unknown"), "latest_delta_unknown"),
            "redlines": _items(delta.get("redlines"), "latest_delta_redlines"),
            "next_action": _optional_text(delta.get("next_action"), "latest_delta_next_action", limit=MAX_ITEM_CHARS),
            "conflicts": _items(delta.get("conflicts"), "latest_delta_conflicts"),
            "completeness": _optional_text(delta.get("completeness"), "latest_delta_completeness", limit=64) or "unknown",
            "proposal_ref": _optional_text(delta.get("proposal_ref"), "latest_delta_proposal_ref", limit=512) or "unreferenced",
            "source_refs": _items(delta.get("source_refs"), "latest_delta_source_refs"),
            "pending_count": _integer(delta.get("pending_count") if delta.get("pending_count") is not None else 0, "latest_delta_pending_count"),
        }
    verified_recovery = bool(
        reported_recovery_valid
        and authority == EXPECTED_AUTHORITY
        and revision_id
        and content_hash
    )
    return {
        "protocol_version": protocol_version,
        "project_id": project_id,
        "goal": goal,
        "recovery_valid": verified_recovery,
        "reported_recovery_valid": reported_recovery_valid,
        "degraded": not verified_recovery,
        "authority": authority,
        "revision_id": revision_id,
        "content_hash": content_hash,
        "summary": _optional_text(raw.get("summary"), "brief_summary", limit=MAX_ITEM_CHARS),
        "completed": _items(raw.get("completed"), "brief_completed"),
        "remaining": _items(raw.get("remaining"), "brief_remaining"),
        "unknown": _items(raw.get("unknown"), "brief_unknown"),
        "redlines": _items(raw.get("redlines"), "brief_redlines"),
        "next_action": next_action,
        "latest_delta": latest_delta,
        "source_pointers": _items(raw.get("source_pointers"), "brief_source_pointers"),
        "stop_conditions": _items(raw.get("stop_conditions"), "brief_stop_conditions"),
    }


def _online_recovery(endpoint: str, project_id: str, goal: str, max_chars: int, timeout: float) -> dict[str, Any]:
    recovery = _read_json(url_request.Request(f"{endpoint}/v1/ap-vibe/recovery", method="GET"), timeout)
    if recovery.get("project_id") != project_id:
        raise RecoveryError("recovery_project_mismatch", "启动绑定 ap-vibe-local 的 daemon；不要猜测或切换项目。")
    reported_recovery_valid = recovery.get("valid") is True
    recovery_reason = str(recovery.get("reason") or "recovery_not_verified")[:160]
    milestone = recovery.get("milestone") if isinstance(recovery.get("milestone"), Mapping) else {}
    milestone_authority = _authority(milestone.get("authority"))
    revision_id = _optional_text(milestone.get("revision_id"), "revision_id", limit=512)
    revision_number = _integer(milestone.get("revision_number") if milestone.get("revision_number") is not None else 0, "revision_number", minimum=0)
    content_hash = _optional_hash(milestone.get("content_hash"), "content_hash")
    write_capability = recovery.get("write_capability") if isinstance(recovery.get("write_capability"), Mapping) else {}
    formal_write = write_capability.get("vibe_formal_write")
    body = json.dumps(
        {"project_id": project_id, "goal": goal, "max_chars": max_chars},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    brief_response = _read_json(
        url_request.Request(
            f"{endpoint}/v1/ap-vibe/brief",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        ),
        timeout,
    )
    brief = _clean_brief(brief_response.get("brief"), project_id)
    consistency_issues: list[str] = []
    if revision_id and brief["revision_id"] and brief["revision_id"] != revision_id:
        consistency_issues.append("brief_revision_mismatch")
    if content_hash and brief["content_hash"] and brief["content_hash"] != content_hash:
        consistency_issues.append("brief_content_hash_mismatch")
    effective_revision_id = brief["revision_id"] or revision_id
    effective_content_hash = brief["content_hash"] or content_hash
    effective_authority = brief["authority"] if brief["authority"] != "unknown" else milestone_authority
    recovery_valid = bool(
        reported_recovery_valid
        and brief["recovery_valid"]
        and not consistency_issues
    )
    text = _optional_text(brief_response.get("text"), "brief_text", limit=max_chars)
    if not text:
        text = _fallback_brief_text(brief, recovery_reason)
    characters_value = brief_response.get("characters")
    characters = characters_value if isinstance(characters_value, int) and not isinstance(characters_value, bool) else len(text)
    if characters != len(text) or characters > max_chars:
        characters = len(text)
    incomplete_value = brief_response.get("brief_incomplete")
    incomplete = incomplete_value if isinstance(incomplete_value, bool) else True
    limitations: list[str] = []
    if not reported_recovery_valid:
        limitations.append("reviewed_recovery_unavailable:" + recovery_reason)
    if brief["authority"] not in {EXPECTED_AUTHORITY, "unknown"}:
        limitations.append("context_authority_is_not_reviewed:" + brief["authority"])
    if brief["authority"] == "unknown" and milestone_authority not in {EXPECTED_AUTHORITY, "unknown"}:
        limitations.append("milestone_authority_is_not_reviewed:" + milestone_authority)
    if consistency_issues:
        limitations.extend(consistency_issues)
    if formal_write is not False or brief_response.get("vibe_formal_write") is not False:
        limitations.append("formal_vibe_write_status_unverified_do_not_write")
    snapshot = {
        "protocol_version": SNAPSHOT_PROTOCOL,
        "project_id": project_id,
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_mode": "online_daemon",
        "recovery_valid": recovery_valid,
        "reported_recovery_valid": reported_recovery_valid,
        "degraded": not recovery_valid,
        "revision_id": effective_revision_id,
        "revision_number": revision_number,
        "content_hash": effective_content_hash,
        "authority": effective_authority,
        "vibe_formal_write": False,
        "brief": brief,
        "brief_text": text,
        "brief_incomplete": incomplete,
        "limitations": limitations,
    }
    _safe_payload(snapshot)
    return snapshot


def _atomic_snapshot(path: Path, snapshot: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = stream.name
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _portable(path: Path, project_id: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RecoveryError("portable_snapshot_missing", "先在 daemon 在线时成功运行一次 restore，生成已校验快照。") from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RecoveryError("portable_snapshot_invalid", "修复或重新生成 portable-recovery.json。") from exc
    snapshot = dict(_object(raw, "portable_snapshot"))
    if snapshot.get("template_only"):
        raise RecoveryError("portable_snapshot_missing", "发布包不携带其他用户的项目历史；本机首次在线恢复后会自动保存快照。")
    required = {
        "protocol_version", "project_id", "exported_at", "source_mode",
        "recovery_valid", "revision_id", "revision_number", "content_hash",
        "authority", "vibe_formal_write", "brief", "brief_text", "brief_incomplete",
    }
    optional = {"reported_recovery_valid", "degraded", "limitations"}
    if not required.issubset(snapshot) or set(snapshot) - required - optional or snapshot.get("protocol_version") != SNAPSHOT_PROTOCOL:
        raise RecoveryError("portable_snapshot_schema_mismatch", "使用当前 Skill 在线重新生成 portable snapshot。")
    if snapshot.get("project_id") != project_id:
        raise RecoveryError("portable_snapshot_identity_invalid", "为 ap-vibe-local 重新生成只读快照。")
    if snapshot.get("source_mode") != "online_daemon":
        raise RecoveryError("portable_snapshot_authority_invalid", "只使用由本地 daemon 产生的快照。")
    if not isinstance(snapshot.get("recovery_valid"), bool):
        snapshot["recovery_valid"] = False
    snapshot["reported_recovery_valid"] = snapshot.get("reported_recovery_valid") is True
    snapshot["authority"] = _authority(snapshot.get("authority"))
    snapshot["revision_id"] = _optional_text(snapshot.get("revision_id"), "portable_revision_id", limit=512)
    snapshot["revision_number"] = _integer(snapshot.get("revision_number") if snapshot.get("revision_number") is not None else 0, "portable_revision_number", minimum=0)
    snapshot["content_hash"] = _optional_hash(snapshot.get("content_hash"), "portable_content_hash")
    snapshot["exported_at"] = _text(snapshot.get("exported_at"), "portable_exported_at", limit=64)
    try:
        datetime.fromisoformat(snapshot["exported_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecoveryError("portable_exported_at_invalid", "在线重新生成带有效时间的快照。") from exc
    snapshot["brief"] = _clean_brief(snapshot.get("brief"), project_id)
    if (
        snapshot["revision_id"]
        and snapshot["brief"]["revision_id"]
        and snapshot["brief"]["revision_id"] != snapshot["revision_id"]
    ) or (
        snapshot["content_hash"]
        and snapshot["brief"]["content_hash"]
        and snapshot["brief"]["content_hash"] != snapshot["content_hash"]
    ):
        raise RecoveryError("portable_brief_revision_mismatch", "在线重新生成一致的 portable snapshot。")
    snapshot["brief_text"] = _optional_text(snapshot.get("brief_text"), "portable_brief_text", limit=MAX_BRIEF_CHARS) or _fallback_brief_text(snapshot["brief"])
    incomplete_value = snapshot.get("brief_incomplete")
    snapshot["brief_incomplete"] = incomplete_value if isinstance(incomplete_value, bool) else True
    snapshot["recovery_valid"] = bool(snapshot["recovery_valid"] and snapshot["brief"]["recovery_valid"])
    snapshot["degraded"] = bool(snapshot.get("degraded", not snapshot["recovery_valid"]) or not snapshot["recovery_valid"])
    snapshot["limitations"] = list(snapshot.get("limitations") or []) if isinstance(snapshot.get("limitations", []), list) else ["portable_limitations_invalid"]
    _safe_payload(snapshot)
    return snapshot


def _receipt(snapshot: Mapping[str, Any], *, source_mode: str, online_error: str | None = None) -> dict[str, Any]:
    brief = _object(snapshot["brief"], "brief")
    recovery_valid = bool(snapshot.get("recovery_valid"))
    authority = _authority(snapshot.get("authority"))
    degraded = bool(snapshot.get("degraded", not recovery_valid))
    limitations = [
        "read_only_recovery_context",
        "latest_delta_is_unreviewed",
        "automatic_context_compaction_hook_not_connected",
    ]
    limitations.extend(str(item)[:512] for item in snapshot.get("limitations", []) if isinstance(item, str))
    if not recovery_valid:
        limitations.append("reviewed_recovery_not_verified; use context with evidence boundary")
    stale = source_mode == "portable_snapshot"
    if stale:
        limitations.extend(("portable_snapshot_may_be_outdated", f"online_restore_failed:{online_error or 'unknown'}"))
    stale = bool(stale or degraded)
    brief_text = str(snapshot.get("brief_text") or "")
    context_available = bool(brief_text.strip() or brief.get("summary") or brief.get("latest_delta"))
    return {
        "ok": True,
        "source_mode": source_mode,
        "context_available": context_available,
        "degraded": degraded,
        "project_id": snapshot["project_id"],
        "recovery_valid": recovery_valid,
        "revision_id": snapshot.get("revision_id"),
        "revision_number": snapshot["revision_number"],
        "content_hash": snapshot.get("content_hash"),
        "authority": authority,
        "vibe_formal_write": False,
        "brief_text": brief_text,
        "brief": dict(brief),
        "brief_incomplete": snapshot["brief_incomplete"],
        "exported_at": snapshot["exported_at"],
        "stale": stale,
        "limitations": limitations,
        "next_action": brief["next_action"],
    }


def _failure(primary: RecoveryError, fallback: RecoveryError | None, project_id: str) -> dict[str, Any]:
    return {
        "ok": True,
        "source_mode": "unavailable",
        "context_available": False,
        "degraded": True,
        "project_id": project_id,
        "recovery_valid": False,
        "revision_id": None,
        "revision_number": 0,
        "content_hash": None,
        "authority": "unavailable",
        "vibe_formal_write": False,
        "stale": True,
        "error": primary.code,
        "fallback_error": fallback.code if fallback else None,
        "solution": fallback.solution if fallback else primary.solution,
        "limitations": ["no_recovery_context_available", "original_task_may_continue_without_AP-Vibe_context"],
        "next_action": fallback.solution if fallback else primary.solution,
    }


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        skill_root, manifest = _manifest()
        local_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "AP-Vibe"
        installed_endpoint = manifest.get("endpoint")
        installed_project = manifest.get("project_id")
        descriptor = skill_root / 'references/installation.json'
        config_path = local_root / 'config.json'
        explicit = bool(os.environ.get('AP_VIBE_CONFIG_PATH')) or descriptor.is_file()
        if os.environ.get('AP_VIBE_CONFIG_PATH'):
            config_path = Path(os.environ['AP_VIBE_CONFIG_PATH']).expanduser().resolve()
        elif descriptor.is_file():
            try:
                config_path = Path(json.loads(descriptor.read_text(encoding='utf-8'))['config_path']).resolve()
            except (OSError, ValueError, KeyError, TypeError):
                raise RecoveryError('installation_descriptor_invalid', '按实际安装位置重新安装 AP-Vibe Skill；现有项目数据保留。')
        local_root = config_path.parent
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            installed_endpoint = f"http://{config['host']}:{config['port']}"
            installed_project = config.get('project_id') or installed_project
        except (OSError, ValueError, KeyError, TypeError):
            if explicit:
                raise RecoveryError('installation_config_unavailable', '读取本次安装配置失败；按 installation.json 指向的位置恢复配置，不连接其他工作台。')
        parser = argparse.ArgumentParser(description="Restore one verified AP-Vibe local recovery brief")
        parser.add_argument("command", nargs="?", choices=("restore",), default="restore")
        parser.add_argument("--endpoint", default=installed_endpoint)
        parser.add_argument("--project-id", default=installed_project)
        parser.add_argument("--goal", default=manifest.get("default_goal"))
        parser.add_argument("--max-chars", type=int, default=manifest.get("default_max_chars"))
        parser.add_argument("--timeout-seconds", type=float, default=manifest.get("default_timeout_seconds"))
        parser.add_argument("--portable-path", default=None)
        args = parser.parse_args(argv)
        manifest_project = _text(installed_project, "manifest_project_id", limit=512)
        project_id = _text(args.project_id, "project_id", limit=512)
        if project_id != manifest_project:
            raise RecoveryError("project_id_not_pinned", "使用本次安装配置的项目；其他项目可通过任务上下文客户端按需查询。")
        endpoint = _loopback_endpoint(_text(args.endpoint, "endpoint", limit=512))
        goal = _text(args.goal, "goal", limit=2_048)
        if isinstance(args.max_chars, bool) or not MIN_BRIEF_CHARS <= args.max_chars <= MAX_BRIEF_CHARS:
            raise RecoveryError("max_chars_out_of_bounds", "将 max_chars 设为 2000-24000。")
        if not 0.2 <= float(args.timeout_seconds) <= 30.0:
            raise RecoveryError("timeout_out_of_bounds", "将 timeout_seconds 设为 0.2-30。")
        default_snapshot = _text(manifest.get("portable_snapshot"), "portable_snapshot", limit=128)
        portable_path = Path(args.portable_path).resolve() if args.portable_path else local_root / "recovery-cache" / (hashlib.sha256(project_id.encode()).hexdigest() + ".json")
        if not args.portable_path and not portable_path.exists():
            try:
                legacy = _portable(skill_root / "references" / default_snapshot, project_id)
                _atomic_snapshot(portable_path, legacy)
            except (RecoveryError, OSError):
                pass

        try:
            snapshot = _online_recovery(endpoint, project_id, goal, args.max_chars, float(args.timeout_seconds))
            try:
                _atomic_snapshot(portable_path, snapshot)
            except (OSError, RecoveryError):
                snapshot.setdefault("limitations", []).append("local_snapshot_write_unavailable")
            receipt = _receipt(snapshot, source_mode="online_daemon")
        except RecoveryError as online_error:
            try:
                snapshot = _portable(portable_path, project_id)
                receipt = _receipt(snapshot, source_mode="portable_snapshot", online_error=online_error.code)
            except RecoveryError as fallback_error:
                receipt = _failure(online_error, fallback_error, project_id)
                print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
                return 0
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except RecoveryError as exc:
        print(json.dumps(_failure(exc, None, "ap-vibe-local"), ensure_ascii=False, sort_keys=True, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
