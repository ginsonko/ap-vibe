"""Local product control-plane contracts for AP-Vibe Wave G1.

This module deliberately owns project identity, bounded Codex source discovery,
administrative memory disposition, and portable import drafts.  It does not
implement cognition and it never selects an AP action, feeling, memory, or
knowledge revision.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from .contracts import ContractError, utc_now


PRODUCT_SCHEMA_VERSION = "ap-vibe.product.v1"
PORTABLE_PROJECT_PROTOCOL = "ap-vibe.portable-project.v1"
MAX_PROJECTS = 128
MAX_PROJECT_NAME = 160
MAX_PROJECT_ID = 256
MAX_DISCOVERY_DIRECTORIES = 96
MAX_DISCOVERY_FILES = 192
MAX_SESSION_META_BYTES = 256 * 1024
MAX_PORTABLE_BYTES = 2 * 1024 * 1024
MAX_PORTABLE_MEMORIES = 2_000
MAX_PORTABLE_CURRICULA = 512
PORTABLE_IMPORT_STAGES = (
    "preview_only",
    "importing",
    "knowledge_imported",
    "learning_imported",
    "confirmed",
    "failed",
    "rolled_back",
)
# Import targets are transport metadata supplied by the receiving operator.
# They are validated and persisted in the draft, but never alter the source
# project's content identity.  Keep this list explicit: arbitrary unknown
# fields must remain visible and hash-bound rather than becoming an accidental
# bypass around the portable integrity contract.
PORTABLE_TRANSPORT_METADATA_FIELDS = frozenset(
    {"target_project_id", "target_display_name", "target_root"}
)
MACHINE_WORKSPACE_SOURCE = "machine_auto_workspace"
ORGANIZATION_TASK_SCOPES = frozenset({"recent_unclassified", "all_unclassified", "rebuild_all", "project_refresh", "logic_analysis"})
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s,;\"']{8,}"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;\"']{8,}"),
    re.compile(r"(?i)(password|secret|token)(\s*[:=]\s*)[^\s,;\"']{8,}"),
)
_ABSOLUTE_PATH_PATTERNS = (
    re.compile(r"(?i)(?<![A-Za-z0-9_])(?:[A-Z]:\\|\\\\)[^\"'\r\n,;]{2,}"),
    re.compile(r"(?<![:/A-Za-z0-9_])/(?!/)[^\"'\r\n,;]{2,}"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def _text(value: Any, name: str, *, limit: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{name}_must_be_text")
    result = value.strip()
    if not result and not allow_empty:
        raise ContractError(f"{name}_required")
    if len(result) > limit:
        raise ContractError(f"{name}_too_long")
    return result


def _project_id(value: Any) -> str:
    result = _text(value, "project_id", limit=MAX_PROJECT_ID)
    if not _PROJECT_ID.fullmatch(result):
        raise ContractError("project_id_invalid")
    return result


def _root(value: str | Path, name: str = "project_root") -> Path:
    if not isinstance(value, (str, Path)):
        raise ContractError(f"{name}_required")
    raw = str(value).strip()
    if not raw:
        raise ContractError(f"{name}_required")
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise ContractError(f"{name}_not_found")
    if path.is_symlink():
        raise ContractError(f"{name}_symlink_not_supported")
    try:
        with os.scandir(path) as iterator:
            next(iterator, None)
    except OSError as exc:
        raise ContractError(f"{name}_not_readable") from exc
    return path


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _safe_name(path: Path) -> str:
    return path.name[:240] or "codex-session.jsonl"


def redact_portable(value: Any, *, preserve_local_paths: bool = False) -> Any:
    """Recursively redact likely credentials while retaining useful context."""

    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)[:256]
            if re.search(r"(?i)(api[_-]?key|authorization|password|secret|access[_-]?token)", key):
                output[key] = "[REDACTED]"
            else:
                output[key] = redact_portable(item, preserve_local_paths=preserve_local_paths)
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_portable(item, preserve_local_paths=preserve_local_paths) for item in value]
    if isinstance(value, str):
        text = value
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(
                lambda match: (
                    ((match.group(1) or "") + (match.group(2) or ""))
                    if match.lastindex and match.lastindex >= 2
                    else ((match.group(1) or "") if match.lastindex else "")
                )
                + "[REDACTED]",
                text,
            )
        if not preserve_local_paths:
            for pattern in _ABSOLUTE_PATH_PATTERNS:
                text = pattern.sub("[LOCAL_PATH_REDACTED]", text)
        return text
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:2048]


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    display_name: str
    root_path: str
    root_fingerprint: str
    logic_root: str | None
    privacy_scope: str = "project"
    auto_monitor_enabled: bool = True
    status: str = "active"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    archived_at: str | None = None
    source: str = "local_registration"
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _project_id(self.project_id)
        _text(self.display_name, "project_display_name", limit=MAX_PROJECT_NAME)
        root = Path(_text(self.root_path, "project_root", limit=4096)).expanduser().resolve()
        if self.logic_root is not None:
            logic = Path(_text(self.logic_root, "logic_root", limit=4096)).expanduser().resolve()
            if not _is_within(logic, root):
                raise ContractError("logic_root_outside_project_root")
        if self.status not in {"active", "archived"}:
            raise ContractError("project_status_unsupported")
        if not isinstance(self.auto_monitor_enabled, bool):
            raise ContractError("project_auto_monitor_must_be_boolean")

    @property
    def effective_logic_root(self):
        roots = self.extra.get("logic_roots", [])
        return roots[0] if roots else self.logic_root

    def to_dict(self, *, include_local_root: bool = False) -> dict[str, Any]:
        root_available = Path(self.root_path).is_dir()
        logic_available = self.effective_logic_root is not None and Path(self.effective_logic_root).is_dir()
        value = {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "root_fingerprint": self.root_fingerprint,
            "privacy_scope": self.privacy_scope,
            "auto_monitor_enabled": self.auto_monitor_enabled,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived_at": self.archived_at,
            "source": self.source,
            "logic_configured": self.effective_logic_root is not None,
            "root_available": root_available,
            "logic_available": logic_available,
            "availability_status": "ready" if root_available else "root_unavailable",
            # Extra registration metadata is operator-facing but may contain a
            # path or a credential-shaped value supplied by an adapter.  Keep
            # the public projection safe; internal callers can still inspect
            # the immutable record directly when they are inside the daemon.
            "extra": redact_portable(self.extra),
        }
        if include_local_root:
            value["root_path"] = self.root_path
            value["logic_root"] = self.effective_logic_root
        return value


@dataclass(frozen=True)
class CodexSourceRecord:
    source_key: str
    project_id: str
    source_path: str
    source_name: str
    session_id: str | None
    session_cwd: str
    cursor: int | None
    source_size: int
    modified_at: str
    binding_kind: str = "project_root"
    status: str = "ready"
    last_batch: Mapping[str, Any] | None = None
    last_error: Mapping[str, Any] | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_polled_at: str | None = None

    def to_dict(self, *, include_local_path: bool = False) -> dict[str, Any]:
        value = {
            "source_key": self.source_key,
            "project_id": self.project_id,
            "source_name": self.source_name,
            "session_id": self.session_id,
            "binding_kind": self.binding_kind,
            "cursor": self.cursor,
            "source_size": self.source_size,
            "modified_at": self.modified_at,
            "status": self.status,
            "last_batch": dict(self.last_batch) if self.last_batch is not None else None,
            "last_error": dict(self.last_error) if self.last_error is not None else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_polled_at": self.last_polled_at,
        }
        if include_local_path:
            value["source_path"] = self.source_path
            value["session_cwd"] = self.session_cwd
        return value


@dataclass(frozen=True)
class DiscoveredCodexSession:
    source_key: str
    source_path: str
    source_name: str
    session_id: str | None
    cwd: str
    source_size: int
    modified_at: str


@dataclass(frozen=True)
class DiscoveryReport:
    sessions: tuple[DiscoveredCodexSession, ...]
    directories_scanned: int
    files_scanned: int
    files_skipped: int
    parse_errors: int
    elapsed_ms: int
    completeness: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": [
                {
                    "source_key": item.source_key,
                    "source_name": item.source_name,
                    "session_id": item.session_id,
                    "source_size": item.source_size,
                    "modified_at": item.modified_at,
                }
                for item in self.sessions
            ],
            "directories_scanned": self.directories_scanned,
            "files_scanned": self.files_scanned,
            "files_skipped": self.files_skipped,
            "parse_errors": self.parse_errors,
            "elapsed_ms": self.elapsed_ms,
            "completeness": self.completeness,
            "warnings": list(self.warnings),
        }


class CodexSessionDiscovery:
    """Find recent Codex JSONL sessions inside one fixed root and finite budget."""

    def __init__(
        self,
        sessions_root: str | Path,
        *,
        max_directories: int = MAX_DISCOVERY_DIRECTORIES,
        max_files: int = MAX_DISCOVERY_FILES,
        max_elapsed_ms: int = 1200,
    ) -> None:
        self.sessions_root = Path(sessions_root).expanduser().resolve()
        if not self.sessions_root.is_dir():
            raise ContractError("codex_sessions_root_not_found")
        if not 4 <= int(max_directories) <= 512:
            raise ContractError("codex_discovery_directory_budget_out_of_bounds")
        if not 4 <= int(max_files) <= 1024:
            raise ContractError("codex_discovery_file_budget_out_of_bounds")
        if not 100 <= int(max_elapsed_ms) <= 10_000:
            raise ContractError("codex_discovery_time_budget_out_of_bounds")
        self.max_directories = int(max_directories)
        self.max_files = int(max_files)
        self.max_elapsed_ms = int(max_elapsed_ms)

    @staticmethod
    def _meta(path: Path) -> tuple[str | None, str] | None:
        consumed = 0
        with path.open("rb") as handle:
            while consumed < MAX_SESSION_META_BYTES:
                line = handle.readline(min(64 * 1024, MAX_SESSION_META_BYTES - consumed) + 1)
                if not line:
                    break
                consumed += len(line)
                if len(line) > 64 * 1024 or not line.endswith(b"\n"):
                    continue
                try:
                    item = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(item, Mapping) or item.get("type") != "session_meta":
                    continue
                payload = item.get("payload")
                if not isinstance(payload, Mapping):
                    return None
                cwd = payload.get("cwd")
                if not isinstance(cwd, str) or not cwd.strip():
                    return None
                session_id = payload.get("id") or payload.get("session_id")
                return (str(session_id)[:256] if isinstance(session_id, str) else None, cwd.strip())
        return None

    def discover(self) -> DiscoveryReport:
        started = time.monotonic()
        pending: list[Path] = [self.sessions_root]
        directories = 0
        candidates: list[tuple[int, Path]] = []
        files_skipped = 0
        parse_errors = 0
        warnings: list[str] = []
        while pending and directories < self.max_directories:
            if (time.monotonic() - started) * 1000 >= self.max_elapsed_ms:
                warnings.append("discovery_time_budget_exhausted")
                break
            current = pending.pop()
            if not _is_within(current, self.sessions_root):
                files_skipped += 1
                continue
            directories += 1
            try:
                entries = list(os.scandir(current))
            except OSError:
                files_skipped += 1
                continue
            subdirs: list[tuple[int, Path]] = []
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append((entry.stat(follow_symlinks=False).st_mtime_ns, Path(entry.path)))
                    elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".jsonl"):
                        stat = entry.stat(follow_symlinks=False)
                        candidates.append((stat.st_mtime_ns, Path(entry.path)))
                    else:
                        files_skipped += 1
                except OSError:
                    files_skipped += 1
            for _, child in sorted(subdirs, reverse=False):
                pending.append(child)
        if pending:
            warnings.append("discovery_directory_budget_exhausted")
        sessions: list[DiscoveredCodexSession] = []
        ordered = sorted(candidates, key=lambda item: (-item[0], str(item[1]).casefold()))
        if len(ordered) > self.max_files:
            warnings.append("discovery_file_budget_exhausted")
        for modified_ns, path in ordered[: self.max_files]:
            if (time.monotonic() - started) * 1000 >= self.max_elapsed_ms:
                warnings.append("discovery_time_budget_exhausted")
                break
            try:
                meta = self._meta(path)
                if meta is None:
                    parse_errors += 1
                    continue
                session_id, cwd = meta
                stat = path.stat()
                # Match the legacy sampler identity so an upgraded daemon can
                # carry its durable cursor forward without replaying a session.
                source_key = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
                sessions.append(
                    DiscoveredCodexSession(
                        source_key=source_key,
                        source_path=str(path.resolve()),
                        source_name=_safe_name(path),
                        session_id=session_id,
                        cwd=cwd,
                        source_size=stat.st_size,
                        modified_at=datetime.fromtimestamp(modified_ns / 1_000_000_000, timezone.utc).isoformat().replace("+00:00", "Z"),
                    )
                )
            except OSError:
                parse_errors += 1
        elapsed = int((time.monotonic() - started) * 1000)
        completeness = "search_incomplete" if warnings else ("partial" if parse_errors else "complete")
        return DiscoveryReport(
            sessions=tuple(sessions),
            directories_scanned=directories,
            files_scanned=min(len(ordered), self.max_files),
            files_skipped=files_skipped,
            parse_errors=parse_errors,
            elapsed_ms=elapsed,
            completeness=completeness,
            warnings=tuple(dict.fromkeys(warnings)),
        )


class CodexActivityMonitor:
    """One finite background poller around the same service sync function."""

    def __init__(
        self,
        poll: Callable[[], Mapping[str, Any]],
        *,
        interval_seconds: float = 5.0,
    ) -> None:
        if not callable(poll):
            raise ContractError("codex_monitor_poll_required")
        if isinstance(interval_seconds, bool) or not 1.0 <= float(interval_seconds) <= 3600.0:
            raise ContractError("codex_monitor_interval_out_of_bounds")
        self._poll = poll
        self.interval_seconds = float(interval_seconds)
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._state_lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "configured": True,
            "running": False,
            "status": "stopped",
            "interval_seconds": self.interval_seconds,
            "poll_count": 0,
            "processed_count": 0,
            "last_poll_at": None,
            "last_success_at": None,
            "last_result": None,
            "last_error": None,
            "empty_polls_do_not_create_episodes": True,
        }

    def start(self) -> bool:
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._wakeup.clear()
            self._state.update({"running": True, "status": "running"})
            thread = threading.Thread(
                target=self._run,
                name="ap-vibe-codex-monitor",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:
                # Do not leave a misleading running projection when the OS
                # rejects thread creation.  The monitor has no external side
                # effects until its thread actually starts polling.
                self._thread = None
                self._state.update({"running": False, "status": "stopped"})
                raise
            return True

    def stop(self, *, timeout: float = 5.0) -> bool:
        with self._state_lock:
            thread = self._thread
        if thread is None:
            return False
        self._stop.set()
        self._wakeup.set()
        if thread is not threading.current_thread():
            thread.join(max(0.0, min(float(timeout), 30.0)))
        with self._state_lock:
            alive = thread.is_alive()
            self._state.update({"running": alive, "status": "stopping" if alive else "stopped"})
            if not alive:
                self._thread = None
        return not alive

    def wake(self) -> None:
        self._wakeup.set()

    def poll_once(self) -> dict[str, Any]:
        if not self._poll_lock.acquire(blocking=False):
            return {
                "status": "skipped",
                "reason": "poll_already_running",
                "processed_count": 0,
            }
        try:
            observed_at = _now()
            try:
                result = dict(self._poll())
                processed = int(result.get("processed_count", 0) or 0)
                result_status = str(result.get("status") or "unknown")
                redacted_result = redact_portable(result)
                with self._state_lock:
                    self._state["poll_count"] += 1
                    self._state["processed_count"] += max(0, processed)
                    self._state["last_poll_at"] = observed_at
                    self._state["last_result"] = redacted_result
                    if result_status == "success":
                        self._state["last_success_at"] = _now()
                        self._state["last_error"] = None
                        self._state["status"] = "running" if self._state["running"] else "stopped"
                    else:
                        # A partial/failed poll is still a completed monitor
                        # iteration, but it is not a successful health sample.
                        # Preserve the last successful timestamp and expose a
                        # bounded actionable error instead of masking the
                        # source failure as a healthy running state.
                        self._state["last_error"] = redacted_result
                        self._state["status"] = "degraded"
                return result
            except Exception as exc:  # individual failures remain observable; loop survives
                failure = {
                    "status": "failed",
                    "original_code": str(exc)[:240] or type(exc).__name__,
                    "error_type": type(exc).__name__,
                    "retryable": True,
                    "next_action": "核对 Codex sessions 根与项目目录后重试；上次成功数据保持不变。",
                    "processed_count": 0,
                }
                with self._state_lock:
                    self._state["poll_count"] += 1
                    self._state["last_poll_at"] = observed_at
                    self._state["last_error"] = failure
                    self._state["status"] = "degraded"
                return failure
        finally:
            self._poll_lock.release()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._wakeup.wait(self.interval_seconds)
            self._wakeup.clear()
        with self._state_lock:
            self._state.update({"running": False, "status": "stopped"})

    @staticmethod
    def _result_summary(result: Any) -> Any:
        if not isinstance(result, Mapping):
            return result

        def scalars(value: Mapping[str, Any]) -> dict[str, Any]:
            return {key: item[:512] if isinstance(item, str) else item
                    for key, item in value.items()
                    if item is None or isinstance(item, (str, bool, int, float))}

        summary = scalars(result)
        summary['projection'] = 'monitor_summary'
        summary['collection_counts'] = {key: len(value) for key, value in result.items()
                                        if isinstance(value, (list, tuple))}
        discovery = result.get('discovery')
        if isinstance(discovery, Mapping):
            brief = scalars(discovery)
            brief['collection_counts'] = {key: len(value) for key, value in discovery.items()
                                          if isinstance(value, (list, tuple))}
            report = discovery.get('report')
            if isinstance(report, Mapping):
                brief['report'] = scalars(report)
                warnings = report.get('warnings') or []
                brief['report']['warnings'] = [str(item)[:512] for item in warnings[:12]]
            summary['discovery'] = brief
        errors, error_count, statuses = [], 0, {}
        for project in result.get('projects') or []:
            if not isinstance(project, Mapping):
                continue
            status = str(project.get('status', 'unknown'))
            statuses[status] = statuses.get(status, 0) + 1
            for source in project.get('sources') or []:
                if not isinstance(source, Mapping) or source.get('status') in {'success', 'ready'}:
                    continue
                error_count += 1
                if len(errors) < 12:
                    sample = {'project_id': project.get('project_id'), **scalars(source)}
                    for key in ('error', 'last_error'):
                        if isinstance(source.get(key), Mapping):
                            sample[key] = scalars(source[key])
                    errors.append(sample)
        summary.update(project_status_counts=statuses, source_error_count=error_count,
                       source_error_samples=errors, source_errors_truncated=error_count > len(errors))
        return summary

    def state(self, *, compact: bool = False) -> dict[str, Any]:
        with self._state_lock:
            value = self._state
            if compact:
                value = {**value, 'last_result': self._result_summary(value['last_result']),
                         'last_error': self._result_summary(value['last_error']),
                         'details_url': '/v1/ap-vibe/codex/status?details=true'}
            return json.loads(json.dumps(value, ensure_ascii=False))


class _RegistryConnection(sqlite3.Connection):
    """Existing store helpers share one transaction during a curation commit."""
    managed = False

    def commit(self):
        if not self.managed:
            super().commit()

    def close(self):
        if not self.managed:
            super().close()

    def execute(self, sql, parameters=()):
        if self.managed and sql.strip().upper().startswith("BEGIN"):
            return super().execute("SELECT 1")
        return super().execute(sql, parameters)


class ProjectRegistry:
    """SQLite-backed administrative identity and portable-draft registry."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._transaction_local = threading.local()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        active = getattr(self._transaction_local, "connection", None)
        if active is not None:
            return active
        connection = sqlite3.connect(self.path, timeout=15.0, factory=_RegistryConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    @contextmanager
    def transaction(self):
        """Commit project, document, assignment and task receipt together."""
        if getattr(self._transaction_local, "connection", None) is not None:
            yield
            return
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        connection.managed = True
        self._transaction_local.connection = connection
        try:
            yield
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.managed = False
            connection.commit()
        finally:
            self._transaction_local.connection = None
            connection.managed = False
            connection.close()

    def mark_registered(self, project_id: str, *, connection=None):
        def write(conn):
            row = conn.execute("SELECT extra_json FROM projects WHERE project_id=?", (project_id,)).fetchone()
            extra = {**json.loads(row[0]), "registration_state": "registered"}
            conn.execute("UPDATE projects SET extra_json=? WHERE project_id=?", (_canonical(extra), project_id))
        if connection is not None:
            write(connection)
        else:
            with closing(self._connect()) as conn:
                write(conn)
                conn.commit()

    def configure_logic(self, project_id: str, root: str, *, documented_roots=()):
        project = self.get(project_id, include_archived=False)
        logic = _root(root, "logic_root")
        # Logical projects may span several explicitly classified workspaces.
        allowed = [Path(project.root_path)] if not project.extra.get("logical_container") else []
        allowed += [Path(s.session_cwd) for s in self.sources(project_id, limit=128)
                    if s.binding_kind in {"classified_session", "explicit_session"}]
        allowed += [Path(p) for p in documented_roots]
        if not any(_is_within(logic, p.resolve()) for p in allowed):
            raise ContractError("logic_root_outside_project_root")
        with closing(self._connect()) as conn:
            extra = {**project.extra, "logic_roots": [str(logic)]}
            # Preserve the original registration root and its routing fingerprint.
            conn.execute("UPDATE projects SET extra_json=?,updated_at=? WHERE project_id=?", (_canonical(extra), _now(), project_id))
            conn.commit()
        return self.get(project_id)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    root_path TEXT NOT NULL,
                    root_fingerprint TEXT NOT NULL UNIQUE,
                    logic_root TEXT,
                    privacy_scope TEXT NOT NULL,
                    auto_monitor_enabled INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    archived_at TEXT,
                    source TEXT NOT NULL,
                    extra_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS codex_sources (
                    source_key TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    session_id TEXT,
                    session_cwd TEXT NOT NULL,
                    cursor INTEGER,
                    source_size INTEGER NOT NULL,
                    modified_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_batch_json TEXT,
                    last_error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );
                CREATE INDEX IF NOT EXISTS idx_codex_sources_project ON codex_sources(project_id, modified_at DESC);
                CREATE TABLE IF NOT EXISTS memory_dispositions (
                    disposition_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    activity_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    prior_state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_dispositions_project ON memory_dispositions(project_id, activity_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS portable_drafts (
                    draft_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    bundle_hash TEXT NOT NULL,
                    source_project_id TEXT NOT NULL,
                    target_project_id TEXT NOT NULL,
                    target_display_name TEXT NOT NULL,
                    target_root TEXT NOT NULL,
                    bundle_json TEXT NOT NULL,
                    preview_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confirmed_project_id TEXT,
                    confirm_request_id TEXT,
                    confirm_fingerprint TEXT,
                    completed_stage TEXT NOT NULL DEFAULT 'preview_only',
                    failure_json TEXT,
                    imported_revision_id TEXT,
                    learning_snapshot_hash TEXT,
                    rolled_back_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS imported_learning_snapshots (
                    project_id TEXT PRIMARY KEY,
                    bundle_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_project_assignments (
                    assignment_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    source_key TEXT NOT NULL,
                    session_id TEXT,
                    from_project_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    confidence REAL,
                    rationale TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_session_assignments_source
                    ON session_project_assignments(source_key, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_session_assignments_project
                    ON session_project_assignments(project_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS organization_tasks (
                    task_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    days INTEGER NOT NULL,
                    included_json TEXT NOT NULL,
                    excluded_json TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_organization_tasks_created
                    ON organization_tasks(created_at DESC);
                CREATE TABLE IF NOT EXISTS project_admin_events (
                    event_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_project_admin_events_project
                    ON project_admin_events(project_id, created_at DESC);
                """
            )
            # G1 was developed against a few local databases before the
            # resumable import contract existed.  Migrate those rows in place;
            # SQLite's ALTER TABLE is intentionally limited to additive
            # columns so an interrupted upgrade cannot destroy a draft.
            draft_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(portable_drafts)").fetchall()
            }
            source_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(codex_sources)")}
            if "binding_kind" not in source_columns:
                connection.execute("ALTER TABLE codex_sources ADD COLUMN binding_kind TEXT NOT NULL DEFAULT 'project_root'")
            if "last_polled_at" not in source_columns:
                connection.execute("ALTER TABLE codex_sources ADD COLUMN last_polled_at TEXT")
            for column, declaration in (
                ("confirm_request_id", "TEXT"),
                ("confirm_fingerprint", "TEXT"),
                ("completed_stage", "TEXT NOT NULL DEFAULT 'preview_only'"),
                ("failure_json", "TEXT"),
                ("imported_revision_id", "TEXT"),
                ("learning_snapshot_hash", "TEXT"),
                ("rolled_back_at", "TEXT"),
            ):
                if column not in draft_columns:
                    connection.execute(
                        f"ALTER TABLE portable_drafts ADD COLUMN {column} {declaration}"
                    )
            connection.commit()

    @staticmethod
    def _row(row: sqlite3.Row) -> ProjectRecord:
        raw_logic_root = row["logic_root"]
        # Older G1 development rows were written with ``str(None)``.  Treat
        # that sentinel as the genuinely unconfigured state while retaining
        # every real filesystem path verbatim.
        logic_root = (
            None
            if raw_logic_root is None or str(raw_logic_root) == "None"
            else str(raw_logic_root)
        )
        return ProjectRecord(
            project_id=str(row["project_id"]),
            display_name=str(row["display_name"]),
            root_path=str(row["root_path"]),
            root_fingerprint=str(row["root_fingerprint"]),
            logic_root=logic_root,
            privacy_scope=str(row["privacy_scope"]),
            auto_monitor_enabled=bool(row["auto_monitor_enabled"]),
            status=str(row["status"]),
            archived_at=str(row["archived_at"]) if row["archived_at"] is not None else None,
            source=str(row["source"]),
            extra=json.loads(str(row["extra_json"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def register(
        self,
        *,
        display_name: str,
        root_path: str | Path,
        project_id: str | None = None,
        logic_root: str | Path | None = None,
        auto_monitor_enabled: bool = True,
        source: str = "local_registration",
        extra: Mapping[str, Any] | None = None,
    ) -> tuple[ProjectRecord, bool]:
        root = _root(root_path)
        # ``logic_root`` is intentionally optional.  A project can be tracked
        # before the operator opts into the static logic-field instrument; an
        # omitted value must not silently turn the registration root into a
        # configured analysis root.
        logic = _root(logic_root, "logic_root") if logic_root is not None else None
        if logic is not None and not _is_within(logic, root):
            raise ContractError("logic_root_outside_project_root")
        name = _text(display_name, "project_display_name", limit=MAX_PROJECT_NAME)
        identifier = _project_id(project_id) if project_id is not None else f"project-local-{uuid.uuid4().hex[:16]}"
        fingerprint = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()
        if not isinstance(auto_monitor_enabled, bool):
            raise ContractError("project_auto_monitor_must_be_boolean")
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_root = connection.execute(
                "SELECT * FROM projects WHERE root_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if existing_root is not None:
                record = self._row(existing_root)
                if record.project_id != identifier and project_id is not None:
                    connection.rollback()
                    raise ContractError("project_root_already_registered")
                connection.commit()
                return record, True
            existing_id = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?",
                (identifier,),
            ).fetchone()
            if existing_id is not None:
                connection.rollback()
                raise ContractError("project_id_already_registered")
            count = int(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0])
            if count >= MAX_PROJECTS:
                connection.rollback()
                raise ContractError("project_limit_reached")
            record = ProjectRecord(
                project_id=identifier,
                display_name=name,
                root_path=str(root),
                root_fingerprint=fingerprint,
                logic_root=str(logic) if logic is not None else None,
                auto_monitor_enabled=auto_monitor_enabled,
                source=_text(source, "project_source", limit=128),
                extra=dict(extra or {}),
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                """INSERT INTO projects
                (project_id, display_name, root_path, root_fingerprint, logic_root,
                 privacy_scope, auto_monitor_enabled, status, archived_at, source,
                 extra_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.project_id, record.display_name, record.root_path,
                    record.root_fingerprint, record.logic_root, record.privacy_scope,
                    int(record.auto_monitor_enabled), record.status, record.archived_at,
                    record.source, _canonical(dict(record.extra)), record.created_at,
                    record.updated_at,
                ),
            )
            connection.commit()
        return record, False

    def get(self, project_id: str, *, include_archived: bool = True) -> ProjectRecord:
        identifier = _project_id(project_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?",
                (identifier,),
            ).fetchone()
        if row is None or (not include_archived and str(row["status"]) == "archived"):
            raise ContractError("project_not_found")
        return self._row(row)

    def find_by_root(self, root_path: str | Path, *, include_archived: bool = True) -> ProjectRecord | None:
        """Return the explicit project already bound to one canonical root.

        This is used only during daemon cold start to recover an existing
        identity.  It never guesses from a basename or from database order.
        """

        root = _root(root_path)
        fingerprint = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE root_fingerprint = ? LIMIT 1",
                (fingerprint,),
            ).fetchone()
        if row is None or (not include_archived and str(row["status"]) == "archived"):
            return None
        return self._row(row)

    def resolve_legacy_project(
        self,
        project_id: str,
        *,
        compatibility_root: str | Path | None = None,
        include_archived: bool = True,
    ) -> ProjectRecord:
        """Resolve the one documented pre-G1 client identity.

        Older AP-Vibe callers used ``project-local`` while the productized
        daemon uses an explicit registered id.  The alias is accepted only
        when it is absent as an independent project and the compatibility
        root has exactly one persisted project.  It is deliberately not a
        fuzzy/basename/project-order lookup; all new clients must use the
        returned canonical id.
        """

        try:
            # A legacy alias must obey the caller's archive policy too.  The
            # previous direct-hit path silently used the default
            # ``include_archived=True`` and could therefore re-enter an
            # archived project through the compatibility name.
            return self.get(project_id, include_archived=include_archived)
        except ContractError as exc:
            if str(exc) != "project_not_found" or project_id != "project-local" or compatibility_root is None:
                raise
        record = self.find_by_root(compatibility_root, include_archived=include_archived)
        if record is None:
            raise ContractError("project_not_found")
        return record

    def list(self, *, include_archived: bool = True) -> tuple[ProjectRecord, ...]:
        query = "SELECT * FROM projects"
        args: tuple[Any, ...] = ()
        if not include_archived:
            query += " WHERE status = ?"
            args = ("active",)
        query += " ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC, project_id"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, args).fetchall()
        return tuple(self._row(row) for row in rows)

    def set_status(self, project_id: str, status: str) -> ProjectRecord:
        if status not in {"active", "archived"}:
            raise ContractError("project_status_unsupported")
        current = self.get(project_id)
        now = _now()
        archived_at = now if status == "archived" else None
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE projects SET status = ?, archived_at = ?, updated_at = ? WHERE project_id = ?",
                (status, archived_at, now, current.project_id),
            )
            connection.commit()
        return self.get(project_id)

    def set_auto_monitor(self, project_id: str, enabled: bool) -> ProjectRecord:
        if not isinstance(enabled, bool):
            raise ContractError("project_auto_monitor_must_be_boolean")
        current = self.get(project_id)
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE projects SET auto_monitor_enabled = ?, updated_at = ? WHERE project_id = ?",
                (int(enabled), _now(), current.project_id),
            )
            connection.commit()
        return self.get(project_id)

    def update_project(
        self,
        *,
        request_id: str,
        project_id: str,
        display_name: str | None = None,
        status: str | None = None,
        actor: str = "user",
        reason: str,
    ) -> tuple[ProjectRecord, bool]:
        """Apply a reversible project-container correction with an audit event."""

        request = _text(request_id, "request_id", limit=256)
        current = self.get(project_id)
        if display_name is None and status is None:
            raise ContractError("project_update_empty")
        name = current.display_name if display_name is None else _text(
            display_name, "project_display_name", limit=MAX_PROJECT_NAME
        )
        state = current.status if status is None else status
        if state not in {"active", "archived"}:
            raise ContractError("project_status_unsupported")
        actor_value = _text(actor, "project_update_actor", limit=128)
        reason_value = _text(reason, "project_update_reason", limit=2048)
        before = current.to_dict()
        intended = {"display_name": name, "status": state, "actor": actor_value, "reason": reason_value}
        fingerprint = hashlib.sha256(_canonical(intended).encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT * FROM project_admin_events WHERE request_id = ?", (request,)
            ).fetchone()
            if prior is not None:
                if str(prior["fingerprint"]) != fingerprint or str(prior["project_id"]) != current.project_id:
                    raise ContractError("project_update_request_conflict")
                connection.commit()
                return self.get(current.project_id), True
            now = _now()
            archived_at = now if state == "archived" else None
            connection.execute(
                """UPDATE projects SET display_name = ?, status = ?, archived_at = ?,
                auto_monitor_enabled = CASE WHEN ? = 'archived' THEN 0 WHEN status = 'archived' THEN 1 ELSE auto_monitor_enabled END,
                updated_at = ? WHERE project_id = ?""",
                (name, state, archived_at, state, now, current.project_id),
            )
            after_row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (current.project_id,)
            ).fetchone()
            after = self._row(after_row).to_dict()
            action = "archive" if state == "archived" and current.status != state else (
                "restore" if state == "active" and current.status != state else "edit"
            )
            connection.execute(
                """INSERT INTO project_admin_events
                (event_id, request_id, fingerprint, project_id, action, before_json,
                 after_json, actor, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (_stable_id("project_admin", request), request, fingerprint, current.project_id,
                 action, _canonical(before), _canonical(after), actor_value, reason_value, now),
            )
            connection.commit()
        return self.get(current.project_id), False

    @staticmethod
    def _assignment_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "assignment_id": str(row["assignment_id"]),
            "request_id": str(row["request_id"]),
            "source_key": str(row["source_key"]),
            "session_id": str(row["session_id"]) if row["session_id"] else None,
            "from_project_id": str(row["from_project_id"]),
            "project_id": str(row["project_id"]),
            "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
            "rationale": str(row["rationale"]),
            "evidence_refs": json.loads(str(row["evidence_json"])),
            "actor": str(row["actor"]), "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "revoked_at": str(row["revoked_at"]) if row["revoked_at"] else None,
        }

    def assign_source(
        self,
        *,
        request_id: str,
        source_key: str,
        session_id: str,
        project_id: str,
        confidence: float | None,
        rationale: str,
        evidence_refs: Sequence[str],
        actor: str,
    ) -> tuple[dict[str, Any], bool]:
        """Move one durable source into a project while retaining its assignment history."""

        request = _text(request_id, "request_id", limit=256)
        source = self.source(source_key)
        if source.session_id != _text(session_id, "codex_session_id", limit=256):
            raise ContractError("codex_binding_identity_changed")
        target = self.get(project_id, include_archived=False)
        if confidence is not None and (isinstance(confidence, bool) or not 0 <= float(confidence) <= 1):
            raise ContractError("session_assignment_confidence_invalid")
        confidence = float(confidence) if confidence is not None else None
        if not isinstance(evidence_refs, Sequence) or isinstance(evidence_refs, (str, bytes)) or len(evidence_refs) > 16:
            raise ContractError("session_assignment_evidence_invalid")
        evidence = [_text(item, "session_assignment_evidence", limit=2048) for item in evidence_refs]
        rationale_value = _text(rationale, "session_assignment_rationale", limit=4000)
        actor_value = _text(actor, "session_assignment_actor", limit=128)
        intended = {
            "source_key": source.source_key, "session_id": source.session_id,
            "project_id": target.project_id, "confidence": confidence,
            "rationale": rationale_value, "evidence_refs": evidence, "actor": actor_value,
        }
        fingerprint = hashlib.sha256(_canonical(intended).encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT * FROM session_project_assignments WHERE request_id = ?", (request,)
            ).fetchone()
            if prior is not None:
                prior_value = json.loads(str(prior["evidence_json"]))
                prior_intended = {
                    "source_key": str(prior["source_key"]), "session_id": str(prior["session_id"]),
                    "project_id": str(prior["project_id"]),
                    "confidence": float(prior["confidence"]) if prior["confidence"] is not None else None,
                    "rationale": str(prior["rationale"]), "evidence_refs": prior_value,
                    "actor": str(prior["actor"]),
                }
                if hashlib.sha256(_canonical(prior_intended).encode("utf-8")).hexdigest() != fingerprint:
                    raise ContractError("session_assignment_request_conflict")
                connection.commit()
                return self._assignment_dict(prior), True
            now = _now()
            connection.execute(
                "UPDATE session_project_assignments SET status = 'revoked', revoked_at = ? WHERE source_key = ? AND status = 'active'",
                (now, source.source_key),
            )
            assignment_id = _stable_id("session_assignment", request, source.source_key, target.project_id)
            connection.execute(
                """INSERT INTO session_project_assignments
                (assignment_id, request_id, source_key, session_id, from_project_id,
                 project_id, confidence, rationale, evidence_json, actor, status,
                 created_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL)""",
                (assignment_id, request, source.source_key, source.session_id, source.project_id,
                 target.project_id, confidence, rationale_value, _canonical(evidence), actor_value, now),
            )
            connection.execute(
                """UPDATE codex_sources SET project_id = ?, binding_kind = 'classified_session',
                status = CASE WHEN status = 'disabled' THEN status ELSE 'ready' END, updated_at = ?
                WHERE source_key = ?""",
                (target.project_id, now, source.source_key),
            )
            row = connection.execute(
                "SELECT * FROM session_project_assignments WHERE assignment_id = ?", (assignment_id,)
            ).fetchone()
            connection.commit()
        return self._assignment_dict(row), False

    def active_assignment(self, source_key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM session_project_assignments WHERE source_key = ?
                AND status = 'active' ORDER BY created_at DESC LIMIT 1""", (source_key,)
            ).fetchone()
        return self._assignment_dict(row) if row is not None else None

    def assignment_history(self, project_id: str | None = None, *, limit: int = 128) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 256:
            raise ContractError("session_assignment_limit_invalid")
        query = "SELECT * FROM session_project_assignments"
        args: tuple[Any, ...] = ()
        if project_id:
            self.get(project_id)
            query += " WHERE project_id = ? OR from_project_id = ?"
            args = (project_id, project_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, (*args, int(limit))).fetchall()
        return [self._assignment_dict(row) for row in rows]

    @staticmethod
    def _organization_task_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": str(row["task_id"]), "request_id": str(row["request_id"]),
            "scope": str(row["scope"]), "days": int(row["days"]),
            "included": json.loads(str(row["included_json"])),
            "excluded": json.loads(str(row["excluded_json"])),
            "prompt": str(row["prompt"]), "status": str(row["status"]),
            "result": json.loads(str(row["result_json"])) if row["result_json"] else None,
            "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"]),
        }

    def create_organization_task(
        self,
        *,
        request_id: str,
        scope: str,
        days: int,
        included: Sequence[Mapping[str, Any]],
        excluded: Sequence[Mapping[str, Any]],
        prompt: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        request = _text(request_id, "request_id", limit=256)
        if scope not in ORGANIZATION_TASK_SCOPES:
            raise ContractError("organization_scope_invalid")
        if isinstance(days, bool) or not 1 <= int(days) <= 3650:
            raise ContractError("organization_days_invalid")
        prompt_value = _text(prompt, "organization_prompt", limit=64_000)
        if len(included) > MAX_PROJECTS * 128:
            raise ContractError("organization_task_too_many_sources")
        compact_included = [{**redact_portable(dict(item)), **({"read_url": item["read_url"]} if str(item.get("read_url", "")).startswith("/v1/ap-vibe/organization/context?source_key=") else {})} for item in included]
        compact_excluded = redact_portable(list(excluded)[:128])
        intended = {"scope": scope, "days": int(days), "included": compact_included,
                    "excluded": compact_excluded, "prompt": prompt_value, "metadata": metadata}
        fingerprint = hashlib.sha256(_canonical(intended).encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute("SELECT * FROM organization_tasks WHERE request_id = ?", (request,)).fetchone()
            if prior is not None:
                if str(prior["fingerprint"]) != fingerprint:
                    raise ContractError("organization_task_request_conflict")
                connection.commit()
                return self._organization_task_dict(prior), True
            now = _now()
            task_id = _stable_id("organization_task", request)
            connection.execute(
                """INSERT INTO organization_tasks
                (task_id, request_id, fingerprint, scope, days, included_json,
                 excluded_json, prompt, status, result_json, created_at, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ready_for_codex', ?, ?, ?)""",
                (task_id, request, fingerprint, scope, int(days), _canonical(compact_included),
                 _canonical(compact_excluded), prompt_value, _canonical(dict(metadata or {})), now, now),
            )
            row = connection.execute("SELECT * FROM organization_tasks WHERE task_id = ?", (task_id,)).fetchone()
            connection.commit()
        return self._organization_task_dict(row), False

    def organization_tasks(self, *, limit: int = 12) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 64:
            raise ContractError("organization_task_limit_invalid")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM organization_tasks ORDER BY created_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [self._organization_task_dict(row) for row in rows]

    def match_project(self, cwd: str, *, include_archived: bool = False) -> tuple[ProjectRecord | None, str | None]:
        try:
            candidate = Path(cwd).expanduser().resolve()
        except (OSError, ValueError):
            return None, "session_cwd_unresolvable"
        records = self.list(include_archived=include_archived)
        # Automatic workspaces describe exactly one cwd. A common parent cwd
        # must never acquire every child project created beneath it later.
        manual = [record for record in records if record.source not in {MACHINE_WORKSPACE_SOURCE, "curated_project"}
                  and _is_within(candidate, Path(record.root_path))]
        automatic = [record for record in records if record.source == MACHINE_WORKSPACE_SOURCE
                     and os.path.normcase(str(candidate)) == os.path.normcase(str(Path(record.root_path).resolve()))]
        matches = manual or automatic
        if not matches:
            return None, "session_cwd_not_registered"
        matches.sort(key=lambda item: len(Path(item.root_path).parts), reverse=True)
        longest = len(Path(matches[0].root_path).parts)
        best = [item for item in matches if len(Path(item.root_path).parts) == longest]
        if len(best) != 1:
            return None, "session_project_assignment_ambiguous"
        return best[0], None

    @staticmethod
    def _source_row(row: sqlite3.Row) -> CodexSourceRecord:
        return CodexSourceRecord(
            source_key=str(row["source_key"]),
            project_id=str(row["project_id"]),
            source_path=str(row["source_path"]),
            source_name=str(row["source_name"]),
            session_id=str(row["session_id"]) if row["session_id"] is not None else None,
            session_cwd=str(row["session_cwd"]),
            cursor=int(row["cursor"]) if row["cursor"] is not None else None,
            source_size=int(row["source_size"]),
            modified_at=str(row["modified_at"]),
            binding_kind=str(row["binding_kind"]),
            status=str(row["status"]),
            last_batch=json.loads(str(row["last_batch_json"])) if row["last_batch_json"] else None,
            last_error=json.loads(str(row["last_error_json"])) if row["last_error_json"] else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_polled_at=str(row["last_polled_at"]) if row["last_polled_at"] else None,
        )

    def register_source(self, session: DiscoveredCodexSession, project_id: str, *, explicit: bool = False) -> tuple[CodexSourceRecord, bool]:
        project = self.get(project_id, include_archived=False)
        matched, reason = self.match_project(session.cwd)
        source_path = Path(session.source_path).resolve()
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM codex_sources WHERE source_key = ?", (session.source_key,)).fetchone()
            current = self._source_row(row) if row is not None else None
            pinned = explicit or (current is not None and current.binding_kind in {"explicit_session", "classified_session"})
            if explicit and not session.session_id:
                raise ContractError("codex_binding_session_id_required")
            membership = None
            if explicit and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_project_memberships'").fetchone():
                membership = connection.execute("SELECT project_id FROM task_project_memberships WHERE client_kind='codex' AND session_id=?", (session.session_id,)).fetchone()
                if membership and membership['project_id'] != project.project_id:
                    raise ContractError("task_context_membership_changed_bootstrap_required")
            if current is None and not pinned and (matched is None or matched.project_id != project.project_id):
                raise ContractError(reason or "codex_source_project_mismatch")
            if current is None and explicit and not membership and matched is not None and matched.project_id != project.project_id:
                raise ContractError("codex_source_project_mismatch")
            if row is not None:
                if current.project_id != project.project_id or Path(current.source_path).resolve() != source_path:
                    connection.rollback()
                    raise ContractError("codex_source_assignment_conflict")
                if os.path.normcase(str(Path(current.session_cwd).resolve())) != os.path.normcase(str(Path(session.cwd).resolve())):
                    raise ContractError("codex_binding_identity_changed")
                if pinned and (current.session_id != session.session_id or Path(current.session_cwd).resolve() != Path(session.cwd).resolve()):
                    raise ContractError("codex_binding_identity_changed")
                if explicit:
                    connection.execute("UPDATE codex_sources SET binding_kind = 'explicit_session' WHERE source_key = ?", (session.source_key,))
                rotated = bool(
                    current.session_id
                    and session.session_id
                    and current.session_id != session.session_id
                )
                if rotated:
                    # Codex may rotate a session into the same JSONL path. A
                    # larger replacement file cannot be detected by size alone:
                    # retaining the old cursor would silently skip the new
                    # session's prefix. Reset only the mutable source
                    # projection; historical episodes remain append-only.
                    connection.execute(
                        """UPDATE codex_sources SET cursor = NULL, source_size = ?,
                        modified_at = ?, session_id = ?, status = CASE WHEN status = 'disabled' THEN status ELSE 'ready' END,
                        last_batch_json = NULL, last_error_json = NULL, updated_at = ?
                        WHERE source_key = ?""",
                        (session.source_size, session.modified_at, session.session_id, now, session.source_key),
                    )
                else:
                    connection.execute(
                        """UPDATE codex_sources SET source_size = ?, modified_at = ?,
                        session_id = COALESCE(?, session_id), status = CASE WHEN status = 'disabled' THEN status ELSE 'ready' END,
                        updated_at = ? WHERE source_key = ?""",
                        (session.source_size, session.modified_at, session.session_id, now, session.source_key),
                    )
                connection.commit()
                return self.source(session.source_key), True
            connection.execute(
                """INSERT INTO codex_sources
                (source_key, project_id, source_path, source_name, session_id,
                 session_cwd, cursor, source_size, modified_at, status,
                 last_batch_json, last_error_json, created_at, updated_at, binding_kind)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 'ready', NULL, NULL, ?, ?, ?)""",
                (
                    session.source_key, project.project_id, str(source_path),
                    session.source_name, session.session_id, session.cwd,
                    session.source_size, session.modified_at, now, now,
                    "explicit_session" if explicit else "project_root",
                ),
            )
            connection.commit()
        return self.source(session.source_key), False

    def source(self, source_key: str) -> CodexSourceRecord:
        key = _text(source_key, "source_key", limit=128)
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM codex_sources WHERE source_key = ?", (key,)).fetchone()
        if row is None:
            raise ContractError("codex_source_not_found")
        return self._source_row(row)

    def sources(self, project_id: str, *, limit: int = 32) -> tuple[CodexSourceRecord, ...]:
        self.get(project_id)
        if isinstance(limit, bool) or not 1 <= int(limit) <= 128:
            raise ContractError("codex_source_limit_out_of_bounds")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM codex_sources WHERE project_id = ? ORDER BY modified_at DESC, source_key LIMIT ?",
                (project_id, int(limit)),
            ).fetchall()
        return tuple(self._source_row(row) for row in rows)

    def sources_for_session(self, session_id: str) -> tuple[CodexSourceRecord, ...]:
        identifier = _text(session_id, "codex_session_id", limit=256)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM codex_sources WHERE session_id = ? ORDER BY source_key LIMIT 128",
                (identifier,),
            ).fetchall()
        return tuple(self._source_row(row) for row in rows)

    def source_count(self, project_id: str, *, enabled_only: bool = False) -> int:
        self.get(project_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM codex_sources WHERE project_id = ?"
                + (" AND status <> 'disabled'" if enabled_only else ""),
                (project_id,),
            ).fetchone()
        return int(row[0])

    def sources_for_poll(self, project_id: str, *, limit: int = 16) -> tuple[CodexSourceRecord, ...]:
        self.get(project_id)
        if isinstance(limit, bool) or not 1 <= int(limit) <= 128:
            raise ContractError("codex_source_limit_out_of_bounds")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM codex_sources WHERE project_id = ? AND status <> 'disabled'
                   ORDER BY COALESCE(last_polled_at, ''), modified_at DESC, source_key LIMIT ?""",
                (project_id, int(limit)),
            ).fetchall()
        return tuple(self._source_row(row) for row in rows)

    def update_source(
        self,
        source_key: str,
        *,
        cursor: int | None = None,
        source_size: int | None = None,
        modified_at: str | None = None,
        status: str = "ready",
        last_batch: Mapping[str, Any] | None = None,
        last_error: Mapping[str, Any] | None = None,
        polled: bool = False,
    ) -> CodexSourceRecord:
        current = self.source(source_key)
        next_cursor = current.cursor if cursor is None else int(cursor)
        next_size = current.source_size if source_size is None else int(source_size)
        if next_cursor is not None and next_cursor < 0:
            raise ContractError("codex_cursor_invalid")
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE codex_sources SET cursor = ?, source_size = ?, modified_at = ?,
                status = ?, last_batch_json = ?, last_error_json = ?, updated_at = ?, last_polled_at = ?
                WHERE source_key = ?""",
                (
                    next_cursor, next_size, modified_at or current.modified_at,
                    _text(status, "codex_source_status", limit=64),
                    _canonical(dict(last_batch)) if last_batch is not None else (None if last_error is not None else _canonical(dict(current.last_batch)) if current.last_batch is not None else None),
                    _canonical(dict(last_error)) if last_error is not None else None,
                    _now(), _now() if polled else current.last_polled_at, current.source_key,
                ),
            )
            connection.commit()
        return self.source(source_key)

    def set_memory_disposition(
        self,
        *,
        request_id: str,
        project_id: str,
        activity_id: str,
        state: str,
        reason: str,
        source_ref: str,
    ) -> tuple[dict[str, Any], bool]:
        request = _text(request_id, "request_id", limit=256)
        self.get(project_id)
        activity = _text(activity_id, "activity_id", limit=512)
        if state not in {"active", "archived"}:
            raise ContractError("memory_disposition_state_unsupported")
        reason_value = _text(reason, "memory_disposition_reason", limit=2048)
        source = _text(source_ref, "memory_disposition_source_ref", limit=2048)
        with closing(self._connect()) as connection:
            prior_request = connection.execute(
                "SELECT * FROM memory_dispositions WHERE request_id = ?", (request,)
            ).fetchone()
            if prior_request is not None:
                result = dict(prior_request)
                if result["project_id"] != project_id or result["activity_id"] != activity or result["state"] != state:
                    raise ContractError("memory_disposition_request_conflict")
                return result, True
            prior = connection.execute(
                """SELECT state FROM memory_dispositions WHERE project_id = ? AND activity_id = ?
                ORDER BY created_at DESC, disposition_id DESC LIMIT 1""",
                (project_id, activity),
            ).fetchone()
            prior_state = str(prior["state"]) if prior is not None else "active"
            created_at = _now()
            disposition_id = _stable_id("memory_disposition", project_id, request)
            connection.execute(
                """INSERT INTO memory_dispositions
                (disposition_id, request_id, project_id, activity_id, state,
                 prior_state, reason, source_ref, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (disposition_id, request, project_id, activity, state, prior_state, reason_value, source, created_at),
            )
            connection.commit()
        return {
            "disposition_id": disposition_id,
            "request_id": request,
            "project_id": project_id,
            "activity_id": activity,
            "state": state,
            "prior_state": prior_state,
            "reason": reason_value,
            "source_ref": source,
            "created_at": created_at,
            "cognitive_effect": "applies_to_future_b_recall_only;_history_is_retained",
        }, False

    def memory_states(self, project_id: str) -> dict[str, dict[str, Any]]:
        self.get(project_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM memory_dispositions WHERE project_id = ?
                ORDER BY created_at ASC, disposition_id ASC""",
                (project_id,),
            ).fetchall()
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            output[str(row["activity_id"])] = dict(row)
        return output

    def save_import_draft(
        self,
        *,
        request_id: str,
        bundle: Mapping[str, Any],
        preview: Mapping[str, Any],
        target_project_id: str,
        target_display_name: str,
        target_root: str | Path,
    ) -> tuple[dict[str, Any], bool]:
        request = _text(request_id, "request_id", limit=256)
        target_id = _project_id(target_project_id)
        target_name = _text(target_display_name, "project_display_name", limit=MAX_PROJECT_NAME)
        root = _root(target_root)
        bundle_json = _canonical(redact_portable(bundle))
        if len(bundle_json.encode("utf-8")) > MAX_PORTABLE_BYTES:
            raise ContractError("portable_bundle_size_out_of_bounds")
        bundle_hash = str(bundle.get("content_hash") or "")
        if not re.fullmatch(r"[A-F0-9]{64}", bundle_hash):
            raise ContractError("portable_bundle_hash_invalid")
        # The registry is a second trust boundary.  Do not let a caller save a
        # draft whose advertised hash does not describe the persisted payload.
        if portable_content_hash(bundle) != bundle_hash:
            raise ContractError("portable_bundle_hash_mismatch")
        now = _now()
        draft_id = _stable_id("portable_import_draft", request, bundle_hash, target_id)
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM portable_drafts WHERE request_id = ?", (request,)).fetchone()
            if row is not None:
                if (
                    str(row["bundle_hash"]) != bundle_hash
                    or str(row["target_project_id"]) != target_id
                    or str(row["target_root"]) != str(root)
                    or str(row["target_display_name"]) != target_name
                ):
                    raise ContractError("portable_import_request_conflict")
                return self.import_draft(str(row["draft_id"])), True
            connection.execute(
                """INSERT INTO portable_drafts
                (draft_id, request_id, bundle_hash, source_project_id,
                 target_project_id, target_display_name, target_root, bundle_json,
                 preview_json, status, confirmed_project_id, confirm_request_id,
                 confirm_fingerprint, completed_stage, failure_json,
                 imported_revision_id, learning_snapshot_hash, rolled_back_at,
                 created_at, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'preview_only', NULL, NULL,
                         NULL, 'preview_only', NULL, NULL, NULL, NULL, ?, ?)""",
                (
                    draft_id, request, bundle_hash,
                    str(bundle.get("project", {}).get("project_id") or "unknown")[:MAX_PROJECT_ID],
                    target_id, target_name, str(root), bundle_json,
                    _canonical(dict(preview)), now, now,
                ),
            )
            connection.commit()
        return self.import_draft(draft_id), False

    def import_draft(self, draft_id: str) -> dict[str, Any]:
        identifier = _text(draft_id, "portable_draft_id", limit=256)
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM portable_drafts WHERE draft_id = ?", (identifier,)).fetchone()
        if row is None:
            raise ContractError("portable_import_draft_not_found")
        # The persisted bundle is redacted for safety, but import targets are
        # local transport metadata already stored in dedicated columns.  Add
        # those exact fields back for the server-side continuation path; they
        # remain excluded from the signed source-content hash.
        bundle = json.loads(str(row["bundle_json"]))
        if not isinstance(bundle, dict):
            raise ContractError("portable_import_bundle_invalid")
        bundle.update(
            {
                "target_project_id": str(row["target_project_id"]),
                "target_display_name": str(row["target_display_name"]),
                "target_root": str(row["target_root"]),
            }
        )
        return {
            **dict(row),
            "bundle": bundle,
            "preview": json.loads(str(row["preview_json"])),
            "failure": json.loads(str(row["failure_json"])) if row["failure_json"] else None,
        }

    def bind_import_confirmation(
        self,
        draft_id: str,
        *,
        request_id: str,
        fingerprint: str,
        bundle_hash: str,
    ) -> tuple[dict[str, Any], bool]:
        """Persist the one logical confirmation identity before side effects.

        Portable confirmation spans the registry, knowledge store and learning
        ledger, so it cannot be made one SQLite transaction.  Binding the
        identity first gives retries a durable fence: a changed hash or a
        different payload can never resume the old import accidentally.
        """

        identifier = _text(draft_id, "portable_draft_id", limit=256)
        request = _text(request_id, "request_id", limit=256)
        identity = _text(fingerprint, "portable_import_confirm_fingerprint", limit=128)
        content_hash = _text(bundle_hash, "portable_bundle_hash", limit=64)
        if not re.fullmatch(r"[A-F0-9]{64}", content_hash):
            raise ContractError("portable_bundle_hash_invalid")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM portable_drafts WHERE draft_id = ?", (identifier,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ContractError("portable_import_draft_not_found")
            if str(row["bundle_hash"]) != content_hash:
                connection.rollback()
                raise ContractError("portable_import_bundle_hash_conflict")
            status = str(row["status"])
            prior_request = row["confirm_request_id"]
            prior_fingerprint = row["confirm_fingerprint"]
            # A confirmation request id is a logical operation identity, not
            # merely a per-draft convenience.  Fence accidental reuse across
            # two drafts before either import can create a project.
            other = connection.execute(
                """SELECT draft_id, confirm_fingerprint FROM portable_drafts
                   WHERE confirm_request_id = ? AND draft_id <> ? LIMIT 1""",
                (request, identifier),
            ).fetchone()
            if other is not None:
                connection.rollback()
                raise ContractError("portable_import_confirmation_identity_conflict")
            if prior_request is not None:
                if str(prior_request) != request or str(prior_fingerprint or "") != identity:
                    connection.rollback()
                    raise ContractError("portable_import_confirmation_identity_conflict")
                if status == "rolled_back":
                    connection.rollback()
                    raise ContractError("portable_import_already_rolled_back")
                connection.commit()
                return self.import_draft(identifier), True
            if status == "confirmed":
                # A legacy row may have been confirmed before the new identity
                # columns were introduced.  It is safe to bind only the exact
                # bundle and target draft now; the response remains a replay.
                connection.execute(
                    """UPDATE portable_drafts
                       SET confirm_request_id = ?, confirm_fingerprint = ?,
                           updated_at = ? WHERE draft_id = ?""",
                    (request, identity, _now(), identifier),
                )
                connection.commit()
                return self.import_draft(identifier), True
            if status == "rolled_back":
                connection.rollback()
                raise ContractError("portable_import_already_rolled_back")
            connection.execute(
                """UPDATE portable_drafts
                   SET confirm_request_id = ?, confirm_fingerprint = ?,
                       status = 'importing', failure_json = NULL, updated_at = ?
                   WHERE draft_id = ?""",
                (request, identity, _now(), identifier),
            )
            connection.commit()
        return self.import_draft(identifier), False

    def mark_import_stage(
        self,
        draft_id: str,
        stage: str,
        *,
        project_id: str | None = None,
        revision_id: str | None = None,
        learning_snapshot_hash: str | None = None,
    ) -> dict[str, Any]:
        """Advance one monotonic import stage, preserving crash recovery."""

        identifier = _text(draft_id, "portable_draft_id", limit=256)
        if stage not in {"knowledge_imported", "learning_imported", "confirmed"}:
            raise ContractError("portable_import_stage_unsupported")
        ranks = {"preview_only": 0, "importing": 1, "knowledge_imported": 2, "learning_imported": 3, "confirmed": 4}
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM portable_drafts WHERE draft_id = ?", (identifier,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ContractError("portable_import_draft_not_found")
            if str(row["status"]) == "rolled_back":
                connection.rollback()
                raise ContractError("portable_import_already_rolled_back")
            prior_stage = str(row["completed_stage"] or "preview_only")
            if prior_stage not in ranks:
                prior_stage = "preview_only"
            if ranks[stage] < ranks[prior_stage]:
                connection.rollback()
                raise ContractError("portable_import_stage_regression")
            update_project = project_id if project_id is not None else row["confirmed_project_id"]
            connection.execute(
                """UPDATE portable_drafts SET status = ?, completed_stage = ?,
                   confirmed_project_id = COALESCE(?, confirmed_project_id),
                   imported_revision_id = COALESCE(?, imported_revision_id),
                   learning_snapshot_hash = COALESCE(?, learning_snapshot_hash),
                   failure_json = NULL, updated_at = ? WHERE draft_id = ?""",
                (
                    stage,
                    stage,
                    update_project,
                    revision_id,
                    learning_snapshot_hash,
                    _now(),
                    identifier,
                ),
            )
            connection.commit()
        return self.import_draft(identifier)

    def mark_import_failed(
        self,
        draft_id: str,
        *,
        code: str,
        message: str,
        retryable: bool = True,
        solution: str,
        stage: str | None = None,
    ) -> dict[str, Any]:
        """Record a bounded, user-actionable failure without losing progress."""

        identifier = _text(draft_id, "portable_draft_id", limit=256)
        failure = {
            "code": str(code)[:160],
            "message": str(message)[:1024],
            "retryable": bool(retryable),
            "solution": str(solution)[:2048],
            "stage": str(stage)[:64] if stage else None,
            "recorded_at": _now(),
        }
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE portable_drafts SET status = 'failed', failure_json = ?,
                   updated_at = ? WHERE draft_id = ? AND status <> 'confirmed'""",
                (_canonical(failure), _now(), identifier),
            )
            connection.commit()
        return self.import_draft(identifier)

    def mark_import_confirmed(
        self,
        draft_id: str,
        project_id: str,
        *,
        bundle_hash: str | None = None,
        request_id: str | None = None,
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        self.get(project_id)
        draft = self.import_draft(draft_id)
        if bundle_hash is not None and str(draft["bundle_hash"]) != str(bundle_hash):
            raise ContractError("portable_import_bundle_hash_conflict")
        if request_id is not None:
            if str(draft.get("confirm_request_id") or "") != str(request_id):
                raise ContractError("portable_import_confirmation_identity_conflict")
        if fingerprint is not None and str(draft.get("confirm_fingerprint") or "") != str(fingerprint):
            raise ContractError("portable_import_confirmation_identity_conflict")
        if str(draft.get("completed_stage") or "") not in {"learning_imported", "confirmed"}:
            raise ContractError("portable_import_incomplete")
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE portable_drafts SET status = 'confirmed', completed_stage =
                'confirmed', confirmed_project_id = ?, failure_json = NULL,
                updated_at = ? WHERE draft_id = ?""",
                (project_id, _now(), draft_id),
            )
            connection.commit()
        return self.import_draft(draft_id)

    def mark_import_rolled_back(self, draft_id: str, project_id: str) -> dict[str, Any]:
        draft = self.import_draft(draft_id)
        if str(draft.get("target_project_id")) != str(project_id):
            raise ContractError("portable_import_project_mismatch")
        if draft.get("status") == "rolled_back":
            return draft
        if not draft.get("confirmed_project_id") and draft.get("completed_stage") != "confirmed":
            raise ContractError("portable_import_not_confirmed")
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE portable_drafts SET status = 'rolled_back',
                   rolled_back_at = ?, updated_at = ? WHERE draft_id = ?""",
                (_now(), _now(), draft_id),
            )
            connection.commit()
        return self.import_draft(draft_id)

    def confirmed_import_for_project(self, project_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM portable_drafts WHERE confirmed_project_id = ?
                AND status = 'confirmed' ORDER BY updated_at DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return self.import_draft(str(row["draft_id"]))

    def import_draft_for_project(self, project_id: str) -> dict[str, Any] | None:
        """Return the durable import draft associated with a target project.

        The lookup includes a rolled-back draft so rollback itself is
        idempotent and can be read back after a process restart.  It is scoped
        to the exact target identity; no basename, root, or recency guessing
        is permitted.
        """

        identifier = _project_id(project_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM portable_drafts
                   WHERE target_project_id = ?
                   ORDER BY updated_at DESC, draft_id DESC LIMIT 1""",
                (identifier,),
            ).fetchone()
        if row is None:
            return None
        return self.import_draft(str(row["draft_id"]))

    def save_imported_learning_snapshot(self, project_id: str, bundle_hash: str, snapshot: Mapping[str, Any]) -> None:
        self.get(project_id)
        content_hash = _text(bundle_hash, "portable_bundle_hash", limit=64)
        if not re.fullmatch(r"[A-F0-9]{64}", content_hash):
            raise ContractError("portable_bundle_hash_invalid")
        encoded = _canonical(redact_portable(snapshot))
        with closing(self._connect()) as connection:
            prior = connection.execute(
                "SELECT bundle_hash, snapshot_json FROM imported_learning_snapshots WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if prior is not None:
                if str(prior["bundle_hash"]) != content_hash or str(prior["snapshot_json"]) != encoded:
                    raise ContractError("portable_learning_snapshot_conflict")
                return
            connection.execute(
                """INSERT INTO imported_learning_snapshots
                (project_id, bundle_hash, snapshot_json, created_at) VALUES (?, ?, ?, ?)
                """,
                (project_id, content_hash, encoded, _now()),
            )
            connection.commit()

    def imported_learning_snapshot(self, project_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT bundle_hash, snapshot_json, created_at FROM imported_learning_snapshots WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return {
            "bundle_hash": str(row["bundle_hash"]),
            "snapshot": json.loads(str(row["snapshot_json"])),
            "created_at": str(row["created_at"]),
            "status": "historical_not_installed",
        } if row is not None else None


def portable_content_hash(bundle_without_hash: Mapping[str, Any]) -> str:
    stable = {
        key: value
        for key, value in bundle_without_hash.items()
        if key not in {"content_hash", "exported_at", *PORTABLE_TRANSPORT_METADATA_FIELDS}
    }
    return hashlib.sha256(_canonical(redact_portable(stable)).encode("utf-8")).hexdigest().upper()


def validate_portable_bundle(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ContractError("portable_bundle_must_be_object")
    encoded = _canonical(raw).encode("utf-8")
    if len(encoded) > MAX_PORTABLE_BYTES:
        raise ContractError("portable_bundle_size_out_of_bounds")
    if raw.get("protocol") != PORTABLE_PROJECT_PROTOCOL:
        raise ContractError("portable_protocol_unsupported")
    content_hash = raw.get("content_hash")
    if not isinstance(content_hash, str) or not re.fullmatch(r"[A-F0-9]{64}", content_hash):
        raise ContractError("portable_bundle_hash_invalid")
    unsigned = {key: value for key, value in raw.items() if key != "content_hash"}
    computed = portable_content_hash(unsigned)
    if computed != content_hash:
        raise ContractError("portable_bundle_hash_mismatch")
    payload = raw.get("payload")
    if not isinstance(payload, Mapping):
        raise ContractError("portable_payload_required")
    memories = payload.get("memories", [])
    if not isinstance(memories, Sequence) or isinstance(memories, (str, bytes)):
        raise ContractError("portable_memories_invalid")
    if len(memories) > MAX_PORTABLE_MEMORIES:
        raise ContractError("portable_memory_count_out_of_bounds")
    document = payload.get("documents")
    document_summary = {"chapter_count": 0, "assessment_documented_count": 0, "assessment_count": 0, "source_revision": None}
    if document is not None:
        from .project_documents import SECTION_INFO, assessment_quality, validate_assessment
        if not isinstance(document, Mapping) or not isinstance(document.get("sections"), Mapping):
            raise ContractError("portable_document_invalid")
        sections = document["sections"]
        if any(not isinstance(value, Mapping) or not value for key, value in sections.items() if key in SECTION_INFO):
            raise ContractError("portable_document_invalid")
        if any(not isinstance(document.get(key, {}), Mapping) for key in ("section_authorities", "section_updates")):
            raise ContractError("portable_document_invalid")
        if any(not isinstance(value, str) for value in document.get("section_authorities", {}).values()):
            raise ContractError("portable_document_invalid")
        assessment = sections.get("risks", {}).get("assessment", [])
        validate_assessment(assessment)
        quality = assessment_quality(assessment)
        document_summary = {"chapter_count": sum(bool(sections.get(key)) for key in SECTION_INFO),
                            "assessment_documented_count": quality["documented"], "assessment_count": quality["count"],
                            "source_revision": document.get("revision"),
                            "unknown_chapters": [key for key in sections if key not in SECTION_INFO]}
    learning = payload.get("learning_snapshot", {})
    if learning is not None and not isinstance(learning, Mapping):
        raise ContractError("portable_learning_snapshot_invalid")
    curricula = learning.get("curricula", []) if isinstance(learning, Mapping) else []
    if isinstance(curricula, Sequence) and not isinstance(curricula, (str, bytes)) and len(curricula) > MAX_PORTABLE_CURRICULA:
        raise ContractError("portable_curriculum_count_out_of_bounds")
    known = {
        "protocol", "exported_at", "product_schema_version", "whitepaper_sha256",
        "project", "manifest", "payload", "limitations", "redactions", "content_hash",
        *PORTABLE_TRANSPORT_METADATA_FIELDS,
    }
    return {
        "bundle": redact_portable(raw),
        "computed_hash": computed,
        "unknown_fields": sorted(str(key) for key in raw if key not in known)[:64],
        "byte_count": len(encoded),
        "memory_count": len(memories),
        "documents": document_summary,
        "curriculum_count": len(curricula) if isinstance(curricula, Sequence) and not isinstance(curricula, (str, bytes)) else 0,
    }


__all__ = [
    "PRODUCT_SCHEMA_VERSION",
    "PORTABLE_PROJECT_PROTOCOL",
    "MAX_PORTABLE_BYTES",
    "PORTABLE_TRANSPORT_METADATA_FIELDS",
    "ProjectRecord",
    "CodexSourceRecord",
    "DiscoveredCodexSession",
    "DiscoveryReport",
    "CodexSessionDiscovery",
    "CodexActivityMonitor",
    "ProjectRegistry",
    "redact_portable",
    "portable_content_hash",
    "validate_portable_bundle",
]
