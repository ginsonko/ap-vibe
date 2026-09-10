"""Loopback HTTP daemon for AP Mind Studio and the AP-Vibe workbench.

The server intentionally stays dependency-free.  It owns HTTP request
idempotency and static delivery, not cognition.  Each demo request runs the
real ``MindRuntime`` against its own durable episode database; the registry
only caches the read-only UI projection and can recover by replaying that
episode after a process crash.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import shutil
import subprocess
import threading
import time
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

from .contracts import CapabilityOwnership, ContractError
from .codex_activity import CodexJsonlReceptor, _clean_text
from .task_context import TaskContext
from .read_projection import ReadProjectionCache
from .http_encoding import json_transfer
from .monitor_projection import group_task_sources
from .organization import Organization
from .teacher_settings import TeacherSettings
from .agent_studio import AgentStudio
from .collaboration import CollaborationStore
from .gateway import HybridGateway, NullGateway, OpenAICompatibleGateway
from .governance import GovernanceCompatibilityRecord
from .knowledge_correction import (
    KnowledgeCorrectionConflict,
    KnowledgeCorrectionInterpreter,
    KnowledgeCorrectionNotFound,
    KnowledgeCorrectionRequest,
    KnowledgeCorrectionStore,
    NullKnowledgeCorrectionInterpreter,
    OpenAICompatibleKnowledgeCorrectionInterpreter,
)
from .logic_field import LogicFieldInstrument, LogicQuery
from .product import (
    MACHINE_WORKSPACE_SOURCE,
    MAX_PORTABLE_BYTES,
    PORTABLE_PROJECT_PROTOCOL,
    PRODUCT_SCHEMA_VERSION,
    CodexActivityMonitor,
    CodexSessionDiscovery,
    DiscoveredCodexSession,
    ProjectRecord,
    ProjectRegistry,
    portable_content_hash,
    redact_portable,
    validate_portable_bundle,
)
from .project_knowledge import (
    DEFAULT_BRIEF_CHARS,
    KnowledgeRevisionConflict,
    KnowledgeReviewRequest,
    ProjectKnowledgeStore,
    run_knowledge_review_episode,
)
from .studio_demo import MAX_STUDIO_TICKS, StudioEpisodeRequest, run_studio_episode
from .studio_projection import STUDIO_PROJECTION_VERSION
from .vibe_mind import (
    CurriculumMaturityPolicy,
    ProjectActivity,
    ProjectFeedback,
    ProjectKnowledgeProposal,
    ProjectLearningLedger,
    run_feedback_episode,
    run_project_episode,
)
from .vibe_projection import (
    AP_VIBE_PROJECTION_VERSION,
    project_activity_run,
    project_feedback_run,
    project_knowledge_review_run,
)


STUDIO_API_VERSION = "0.1.0"
AP_VIBE_API_VERSION = "0.9.0"
MAX_REQUEST_BYTES = MAX_PORTABLE_BYTES + 64 * 1024
MAX_RECENT_PROJECT_EPISODES = 24
MAX_CODEX_OVERVIEW_SESSIONS = 128
MAX_CODEX_OVERVIEW_MESSAGES = 80
CODEX_ACTIVE_WINDOW_SECONDS = 15 * 60
HYBRID_WHITEPAPER_SHA256 = "440EA59A70902B0DB2384C7EE066B7EB25387299C8175321127172AF6AD19A39"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class StudioRequestConflict(ContractError):
    """The same client request identity was reused for different input."""


class ProjectTargetNotFound(ContractError):
    """Feedback references an activity episode not owned by this service."""


class KnowledgeProposalNotFound(ContractError):
    """A review references no durable staged proposal in this service."""


@dataclass(frozen=True)
class StudioServiceHealth:
    status: str
    registry_ready: bool
    studio_built: bool
    request_count: int
    project_request_count: int
    learning_ready: bool
    knowledge_ready: bool
    knowledge_revision_count: int
    codex_sampler: Mapping[str, Any]
    provider: Mapping[str, Any]
    logic_field: Mapping[str, Any]
    product: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "api_version": STUDIO_API_VERSION,
            "projection_version": STUDIO_PROJECTION_VERSION,
            "ap_vibe_api_version": AP_VIBE_API_VERSION,
            "ap_vibe_projection_version": AP_VIBE_PROJECTION_VERSION,
            "mode": "hybrid_teacher" if self.provider.get("configured") else "local_provider_off",
            "provider": dict(self.provider),
            "logic_field": dict(self.logic_field),
            "product": dict(self.product or {}),
            "registry": {"ready": self.registry_ready, "request_count": self.request_count},
            "studio": {"built": self.studio_built},
            "ap_vibe": {
                "ready": self.registry_ready and self.learning_ready and self.knowledge_ready,
                "request_count": self.project_request_count,
                "learning_ledger_ready": self.learning_ready,
                "local_recovery_ready": self.knowledge_ready,
                "local_recovery_revision_count": self.knowledge_revision_count,
                "local_recovery_write": True,
                "formal_knowledge_write": False,
                "vibe_formal_knowledge_write": False,
                "codex_sampler": dict(self.codex_sampler),
                "logic_field": dict(self.logic_field),
                "product": dict(self.product or {}),
            },
            "capability_boundary": {
                "product_effect": "local_runtime_episode",
                "ap_native": [
                    "tick",
                    "action_arena",
                    "output_timing",
                    "readback",
                    "revision_application",
                    "recovery",
                ],
                "assisted_fixture": ["surface_renderer", "readback_annotation"],
                "assisted": ["structured_cognition_teacher"] if self.provider.get("configured") else [],
                "absent": [
                    *([] if self.provider.get("configured") else ["real_llm"]),
                    "real_vlm",
                    "vibe_live_control",
                    "napcat",
                ],
            },
        }


class StudioEpisodeService:
    """Durable idempotency registry around real local runtime episodes."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        studio_dir: str | Path | None = None,
        codex_source: str | Path | None = None,
        codex_project_id: str = "ap-vibe-local",
        gateway: HybridGateway | None = None,
        governance: GovernanceCompatibilityRecord | None = None,
        capability: CapabilityOwnership | None = None,
        correction_interpreter: KnowledgeCorrectionInterpreter | None = None,
        maturity_policy: CurriculumMaturityPolicy | None = None,
        teacher_capabilities: Sequence[str] | None = None,
        logic_root: str | Path | None = None,
        project_root: str | Path | None = None,
        codex_sessions_root: str | Path | None = None,
        auto_monitor: bool = False,
        auto_onboard_workspaces: bool = False,
        monitor_interval_seconds: float = 5.0,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.episodes_dir = self.data_dir / "episodes"
        self.episodes_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.data_dir / "studio-registry.sqlite"
        self.learning_path = self.data_dir / "ap-vibe-learning.sqlite"
        self.learning_ledger = ProjectLearningLedger(
            self.learning_path,
            maturity_policy=maturity_policy,
        )
        self.knowledge_path = self.data_dir / "ap-vibe-project-knowledge.sqlite"
        self.knowledge_store = ProjectKnowledgeStore(self.knowledge_path)
        self.correction_path = self.data_dir / "ap-vibe-knowledge-corrections.sqlite"
        self.correction_store = KnowledgeCorrectionStore(self.correction_path)
        self.studio_dir = Path(studio_dir).resolve() if studio_dir is not None else None
        self.codex_source = Path(codex_source).resolve() if codex_source is not None else None
        # A missing logic root means that the optional static instrument is
        # disabled.  It must not be inferred from the process cwd (doing so
        # made an unconfigured daemon appear configured to the HTTP API).
        self.logic_field = None
        self.codex_project_id = str(codex_project_id).strip()
        self.gateway = gateway or NullGateway()
        self.governance = governance
        self.capability = capability
        self.teacher_capabilities = (
            tuple(dict.fromkeys(str(item) for item in teacher_capabilities))
            if teacher_capabilities is not None
            else None
        )
        self.correction_interpreter = correction_interpreter or NullKnowledgeCorrectionInterpreter()
        if not self.codex_project_id or len(self.codex_project_id) > 512:
            raise ContractError("codex_project_id_invalid")
        if self.codex_source is not None and not self.codex_source.is_file():
            raise ContractError("codex_source_file_not_found")
        self._lock = threading.RLock()
        self._workspace_lock = threading.RLock()
        self.product_registry = ProjectRegistry(self.data_dir / "ap-vibe-products.sqlite")
        self.task_context = TaskContext(self)
        self.organization = Organization(self)
        from .codex_messages import CodexMessages
        self.codex_messages = CodexMessages(self)
        default_root = Path(project_root or logic_root or Path.cwd()).resolve()
        # The AP-Vibe project id is the durable daemon identity.  A previous
        # auto-onboard pass may have registered the same source directory as a
        # provisional ``workspace-*`` container.  That container must never
        # steal the AP-Vibe default project on a cold restart: doing so makes
        # the home page, sampler and logic observer appear empty even though
        # the reviewed AP-Vibe dossier is still present.  Resolve the explicit
        # canonical id first; only register it when no such record exists.
        existing_canonical = None
        if self.codex_project_id == "ap-vibe-local":
            try:
                existing_canonical = self.product_registry.get(
                    self.codex_project_id, include_archived=True
                )
            except ContractError as exc:
                if str(exc) != "project_not_found":
                    raise
        if existing_canonical is not None:
            if existing_canonical.status == "archived":
                raise ContractError("default_project_archived")
            self.default_project = existing_canonical
        else:
            try:
                self.default_project, _ = self.product_registry.register(
                    project_id=self.codex_project_id,
                    display_name=("AP-Vibe" if self.codex_project_id == "ap-vibe-local" else default_root.name or self.codex_project_id),
                    root_path=default_root,
                    logic_root=logic_root,
                    auto_monitor_enabled=True,
                    source="legacy_default_project",
                )
            except ContractError as exc:
                # A cold restart must retain the explicit identity already
                # bound to this canonical root.  This compatibility path is
                # only for the daemon's default id; an operator-supplied
                # different id remains a real registration conflict.
                if str(exc) != "project_root_already_registered" or self.codex_project_id != "ap-vibe-local":
                    raise
                existing = self.product_registry.find_by_root(default_root, include_archived=True)
                if existing is None or existing.status == "archived":
                    raise
                self.default_project = existing
                self.codex_project_id = existing.project_id
        if logic_root is not None:
            self.logic_field = LogicFieldInstrument(logic_root)
        elif self.default_project.logic_root is not None:
            # Preserve an explicit, durable project setting across a cold
            # restart; a fresh project remains unconfigured when the argument
            # was omitted.
            self.logic_field = LogicFieldInstrument(self.default_project.logic_root)
        # Keep the one historical client label readable after a cold restart,
        # while preserving the canonical identity in every persisted row.
        self.learning_ledger.set_project_alias("project-local", self.codex_project_id)
        self.teacher_settings = TeacherSettings(self)
        self.agent_studio = AgentStudio(self)
        self.collaboration = CollaborationStore(self.data_dir / "collaboration.sqlite")
        from .claude_sessions import ClaudeSessions
        claude_config_path = self.data_dir / 'claude-monitor.json'
        try:
            claude_config = json.loads(claude_config_path.read_text(encoding='utf-8-sig')) if claude_config_path.is_file() else {}
            if not isinstance(claude_config, dict):
                raise ValueError('configuration must be an object')
            self.claude_sessions = ClaudeSessions(**{k:v for k,v in claude_config.items() if k in {'roots','window_bytes','max_sources','cache_seconds'}})
        except (ValueError, TypeError, OSError):
            self.claude_sessions = ClaudeSessions()
            self.claude_sessions.config_warning = 'Claude监看配置无法读取，暂用当前用户默认记录目录；其他功能继续可用。'
        from .session_directory import SessionDirectory
        self.session_directory = SessionDirectory(self)
        if self.teacher_settings.path.exists():
            self.teacher_settings.apply()
        self.codex_sessions_root = (
            Path(codex_sessions_root).expanduser().resolve()
            if codex_sessions_root is not None
            else None
        )
        if self.codex_sessions_root is not None and not self.codex_sessions_root.is_dir():
            raise ContractError("codex_sessions_root_not_found")
        if not isinstance(auto_onboard_workspaces, bool):
            raise ContractError("auto_onboard_workspaces_must_be_boolean")
        self.auto_onboard_workspaces = auto_onboard_workspaces
        self._logic_fields: dict[str, LogicFieldInstrument] = {}
        if self.logic_field is not None:
            self._logic_fields[self.codex_project_id] = self.logic_field
        self.codex_monitor: CodexActivityMonitor | None = None
        self._initialize()
        self._migrate_explicit_codex_source()
        if self.codex_sessions_root is not None:
            self.codex_monitor = CodexActivityMonitor(
                self.poll_codex_sources,
                interval_seconds=monitor_interval_seconds,
            )
            if auto_monitor:
                self.codex_monitor.start()

    def close(self) -> None:
        """Stop only this service's bounded monitor thread.

        The HTTP server owns the monitor; closing it must never search for or
        terminate unrelated Python/daemon processes.
        """

        if self.codex_monitor is not None:
            self.codex_monitor.stop(timeout=5.0)
        # Curation can launch a bounded Codex CLI child. Stop only children
        # owned by this service before the socket disappears; unrelated Codex
        # tasks and Python processes are never searched for or terminated.
        if getattr(self, "organization", None) is not None:
            self.organization.shutdown()
        if getattr(self, "codex_messages", None) is not None:
            self.codex_messages.shutdown()
        if getattr(self, "agent_studio", None) is not None:
            self.agent_studio.shutdown()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.registry_path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS studio_requests (
                    request_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    episode_db TEXT NOT NULL,
                    response_json TEXT,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS vibe_requests (
                    request_id TEXT PRIMARY KEY,
                    request_kind TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    episode_id TEXT,
                    episode_db TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS codex_source_state (
                    source_key TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    cursor INTEGER,
                    source_size INTEGER NOT NULL,
                    last_batch_json TEXT,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.commit()

    def _require_project(self, project_id: str | None = None, *, active: bool = True) -> ProjectRecord:
        requested = project_id or self.codex_project_id
        try:
            return self.product_registry.get(requested, include_archived=not active)
        except ContractError as exc:
            if str(exc) != "project_not_found":
                raise
            # ``project-local`` is the only historical client id we support.
            # Resolve it against the already-bound canonical root; never use a
            # name, hash, recency, or database order to guess another project.
            record = self.product_registry.resolve_legacy_project(
                requested,
                compatibility_root=self.default_project.root_path,
                include_archived=not active,
            )
            if active and record.status == "archived":
                raise ContractError("project_not_found")
            return record

    def _canonical_project_payload(
        self,
        raw: Mapping[str, Any],
        *,
        active: bool = True,
    ) -> tuple[dict[str, Any], ProjectRecord]:
        """Return a request payload using the persisted canonical project id."""

        if not isinstance(raw, Mapping):
            raise ContractError("project_payload_required")
        requested = raw.get("project_id")
        if not isinstance(requested, str) or not requested.strip():
            raise ContractError("project_id_required")
        project = self._require_project(requested, active=active)
        payload = dict(raw)
        payload["project_id"] = project.project_id
        return payload, project

    def register_project(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ContractError("project_registration_required")
        allowed = {"project_id", "display_name", "root_path", "logic_root", "auto_monitor_enabled"}
        if set(raw) - allowed:
            raise ContractError("project_registration_unknown_fields")
        record, replayed = self.product_registry.register(
            project_id=raw.get("project_id") if isinstance(raw.get("project_id"), str) else None,
            display_name=str(raw.get("display_name") or "").strip(),
            root_path=str(raw.get("root_path") or "").strip(),
            logic_root=raw.get("logic_root") if isinstance(raw.get("logic_root"), str) else None,
            auto_monitor_enabled=raw.get("auto_monitor_enabled", True),
        )
        return {"status": "success", "replayed": replayed, "project": record.to_dict()}

    def projects(self, *, include_archived: bool = True) -> dict[str, Any]:
        records = self.product_registry.list(include_archived=include_archived)
        profiles = [self.task_context.documents.profile(record) for record in records]
        return {
            "status": "success",
            "schema_version": PRODUCT_SCHEMA_VERSION,
            "default_project_id": self.codex_project_id,
            "projects": profiles,
            "registered_count": sum(p["registration_state"] == "registered" and p["status"] == "active" for p in profiles),
            "archived_count": sum(p["status"] == "archived" for p in profiles),
            "project_count": len(records),
            "vibe_formal_write": False,
        }

    def logic_status(self, project_id):
        project = self._require_project(project_id, active=False)
        doc = self.task_context.documents.latest(project.project_id) or {}
        sources = doc.get("sections", {}).get("sources", {})
        roots = [p for p in sources.get("code_roots", []) if isinstance(p, str)]
        if not project.extra.get("logical_container"):
            roots.append(project.root_path)
        roots.extend(s.session_cwd for s in self.product_registry.sources(project.project_id, limit=128)
                     if s.binding_kind in {"explicit_session", "classified_session"})
        candidates = [{"path": p, "available": Path(p).is_dir()} for p in dict.fromkeys(roots)]
        return {"ok": True, "project_id": project.project_id, "project_name": self.task_context.documents.profile(project)["display_name"],
                "root": project.effective_logic_root, "available": bool(project.effective_logic_root and Path(project.effective_logic_root).is_dir()),
                "candidates": candidates, "native_languages": ["Python"], "codex_languages": "Codex 按项目源码分析，支持其他语言；结论保留证据与未知。",
                "reason": "已配置，可执行源码观察" if project.effective_logic_root else "请选择档案或已归类任务中的实际代码目录；资料整理任务本身不一定包含代码。",
                "analysis_tasks": [t for t in self.product_registry.organization_tasks(limit=24) if t["scope"] == "logic_analysis" and (t.get("result") or {}).get("project_id") == project.project_id]}

    def configure_project_logic(self, raw):
        project = self._require_project(raw.get("project_id"))
        status = self.logic_status(project.project_id)
        root = raw.get("root_path")
        if not isinstance(root, str) or not root.strip():
            raise ContractError("logic_root_required")
        self.product_registry.configure_logic(project.project_id, root, documented_roots=[p["path"] for p in status["candidates"]])
        self._logic_fields.pop(project.project_id, None)
        return self.logic_status(project.project_id)

    def set_project_status(self, project_id: str, status: str) -> dict[str, Any]:
        current = self._require_project(project_id, active=False)
        if current.project_id == self.codex_project_id and status == "archived":
            raise ContractError("default_project_cannot_be_archived")
        record = self.product_registry.set_status(current.project_id, status)
        return {"status": "success", "project": record.to_dict()}

    def set_project_monitor(self, project_id: str, enabled: bool) -> dict[str, Any]:
        current = self._require_project(project_id, active=False)
        if current.status == "archived" and enabled:
            raise ContractError("archived_project_monitor_forbidden")
        record = self.product_registry.set_auto_monitor(current.project_id, enabled)
        if self.codex_monitor is not None:
            if enabled:
                self.codex_monitor.start()
                self.codex_monitor.wake()
            elif not any(
                item.auto_monitor_enabled
                for item in self.product_registry.list(include_archived=False)
            ):
                self.codex_monitor.stop(timeout=5.0)
        return {"status": "success", "project": record.to_dict()}

    @staticmethod
    def _session_meta_for_source(path: Path) -> DiscoveredCodexSession:
        session_id: str | None = None
        cwd = ""
        consumed = 0
        with path.open("rb") as handle:
            while consumed < 256 * 1024:
                line = handle.readline(64 * 1024 + 1)
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
                if isinstance(payload, Mapping) and isinstance(payload.get("cwd"), str):
                    cwd = str(payload["cwd"])
                    identity = payload.get("id") or payload.get("session_id")
                    session_id = str(identity) if isinstance(identity, str) else None
                break
        if not cwd:
            cwd = str(Path.cwd())
        stat = path.stat()
        source_key = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
        return DiscoveredCodexSession(
            source_key=source_key,
            source_path=str(path.resolve()),
            source_name=path.name,
            session_id=session_id,
            cwd=cwd,
            source_size=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
        )

    def _migrate_explicit_codex_source(self) -> None:
        if self.codex_source is None:
            return
        session = self._session_meta_for_source(self.codex_source)
        # Legacy tests and old daemon arguments may point at a synthetic JSONL
        # without session_meta.  Preserve that explicit operator binding.
        if not session.session_id:
            session = DiscoveredCodexSession(
                **{**session.__dict__, "cwd": self.default_project.root_path}
            )
        try:
            record, _ = self.product_registry.register_source(session, self.codex_project_id)
        except ContractError:
            # An explicit legacy source is an operator choice; if metadata cwd
            # drifted, bind it to the default project without exposing paths.
            forced = DiscoveredCodexSession(
                **{**session.__dict__, "cwd": self.default_project.root_path}
            )
            record, _ = self.product_registry.register_source(forced, self.codex_project_id)
        with closing(self._connect()) as connection:
            legacy = connection.execute(
                "SELECT * FROM codex_source_state WHERE source_key = ?",
                (record.source_key,),
            ).fetchone()
        if legacy is not None and record.cursor is None:
            last_batch = json.loads(str(legacy["last_batch_json"])) if legacy["last_batch_json"] else None
            self.product_registry.update_source(
                record.source_key,
                cursor=int(legacy["cursor"]) if legacy["cursor"] is not None else None,
                source_size=int(legacy["source_size"]),
                status=str(legacy["status"]),
                last_batch=last_batch,
            )

    def resolve_codex_workspace(self, cwd: str, session_id: str | None = None) -> ProjectRecord:
        """Resolve one local workspace without inferring external Vibe identity."""
        if not isinstance(cwd, str) or not cwd.strip() or len(cwd) > 4096:
            raise ContractError("codex_workspace_cwd_required")
        candidate = Path(cwd).expanduser()
        if not candidate.is_absolute():
            raise ContractError("codex_workspace_cwd_must_be_absolute")
        try:
            candidate = candidate.resolve(strict=True)
        except (OSError, ValueError):
            raise ContractError("codex_workspace_cwd_not_found") from None
        if not candidate.is_dir():
            raise ContractError("codex_workspace_cwd_not_directory")
        with self._workspace_lock:
            # Identity resolution must not queue behind a paid teacher call.
            # Internal analysis/curation jobs belong to AP-Vibe even though
            # their frozen working directory is separate from the code root.
            if (self.data_dir / "curation-jobs") in candidate.parents and (candidate / "index.json").is_file():
                return self.product_registry.get(self.codex_project_id, include_archived=False)
            if session_id is not None:
                sources = self.product_registry.sources_for_session(session_id)
                if len({(item.project_id, os.path.normcase(str(Path(item.session_cwd).resolve()))) for item in sources}) > 1:
                    raise ContractError("codex_session_identity_ambiguous")
                if sources:
                    source = sources[0]
                    if os.path.normcase(str(candidate)) != os.path.normcase(str(Path(source.session_cwd).resolve())):
                        raise ContractError("codex_binding_identity_changed")
                    try:
                        observed = self._session_meta_for_source(Path(source.source_path))
                    except OSError:
                        raise ContractError("codex_source_file_not_found") from None
                    if observed.session_id != source.session_id or os.path.normcase(str(Path(observed.cwd).resolve())) != os.path.normcase(str(candidate)):
                        raise ContractError("codex_binding_identity_changed")
                    project = self.product_registry.get(source.project_id)
                    if source.status == "disabled":
                        raise ContractError("codex_source_disabled")
                    if project.status == "archived":
                        return self.organization.unclassify(source)
                    if not project.auto_monitor_enabled:
                        raise ContractError("codex_workspace_monitor_disabled")
                    return project
            exact = self.product_registry.find_by_root(candidate, include_archived=True)
            if exact is not None and (exact.status == "archived" or not exact.auto_monitor_enabled):
                raise ContractError("codex_workspace_monitor_disabled")
            project, reason = self.product_registry.match_project(str(candidate), include_archived=True)
            if project is not None:
                if project.status == "archived" or not project.auto_monitor_enabled:
                    raise ContractError("codex_workspace_monitor_disabled")
                return project
            if not self.auto_onboard_workspaces:
                raise ContractError(reason or "session_cwd_not_registered")
            fingerprint = hashlib.sha256(os.path.normcase(str(candidate)).encode("utf-8")).hexdigest()
            project, _ = self.product_registry.register(
                project_id=f"workspace-{fingerprint[:32]}",
                display_name=candidate.name or "Local workspace",
                root_path=candidate,
                logic_root=None,
                auto_monitor_enabled=True,
                source=MACHINE_WORKSPACE_SOURCE,
                extra={"match_scope": "exact_cwd", "formal_vibe_binding": False},
            )
            if project.status == "archived" or not project.auto_monitor_enabled:
                raise ContractError("codex_workspace_monitor_disabled")
            return project

    def resolve_codex_workspace_readonly(
        self, cwd: str, session_id: str | None = None
    ) -> tuple[ProjectRecord, dict[str, Any]]:
        """Resolve a project for read-only context delivery.

        Strict identity resolution remains the write boundary.  A read should
        still be useful after a Codex session is moved, its JSONL metadata is
        temporarily unavailable, or the caller is operating from an unknown
        workspace.  In those cases this method keeps the strongest existing
        project association and reports the uncertainty to the caller instead
        of turning it into a permission failure.
        """

        issue: str | None = None
        try:
            project = self.resolve_codex_workspace(cwd, session_id)
            return project, {
                "identity_status": "confirmed",
                "identity_issue": None,
                "classification": "confirmed",
                "resolution": "strict_workspace_and_session_binding",
            }
        except ContractError as exc:
            issue = str(exc) or type(exc).__name__

        candidate: Path | None = None
        try:
            raw_candidate = Path(str(cwd)).expanduser()
            if raw_candidate.is_absolute():
                candidate = raw_candidate.resolve(strict=False)
        except (OSError, ValueError, TypeError):
            candidate = None

        # An existing session association is stronger than a changed cwd.  If
        # several sources exist, prefer the one whose saved cwd still matches
        # the caller; otherwise choose deterministically and expose ambiguity.
        sources: tuple[Any, ...] = ()
        if isinstance(session_id, str) and session_id.strip():
            try:
                sources = self.product_registry.sources_for_session(session_id)
            except ContractError:
                sources = ()
        if sources:
            matching = []
            if candidate is not None:
                for source in sources:
                    try:
                        if os.path.normcase(str(Path(source.session_cwd).resolve(strict=False))) == os.path.normcase(str(candidate)):
                            matching.append(source)
                    except (OSError, ValueError):
                        continue
            chosen = sorted(matching or list(sources), key=lambda item: (item.project_id, item.source_key))[0]
            try:
                project = self.product_registry.get(chosen.project_id, include_archived=True)
            except ContractError:
                project = None
            if project is not None:
                ambiguous = len({item.project_id for item in sources}) > 1 or len(matching) != len(sources)
                return project, {
                    "identity_status": "ambiguous" if ambiguous else "recovered",
                    "identity_issue": issue,
                    "classification": "bound_session" if not ambiguous else "ambiguous_session",
                    "resolution": "existing_session_project_association",
                    "source_count": len(sources),
                    "source_project_ids": sorted({item.project_id for item in sources}),
                }

        # A precise root match is safe even without a usable session file.  A
        # fuzzy basename match is deliberately never used here.
        if candidate is not None and candidate.is_dir():
            # A caller may be running from a protected or transient directory
            # (for example C:\\Windows\\Temp).  Registry root matching is a
            # read-only convenience; it must never turn an otherwise useful
            # context read into a hard failure when the directory cannot be
            # scanned.  Keep the concrete issue as evidence and continue to
            # the default AP-Vibe landing project below.
            try:
                exact = self.product_registry.find_by_root(candidate, include_archived=True)
                if exact is not None:
                    return exact, {
                        "identity_status": "recovered",
                        "identity_issue": issue,
                        "classification": "workspace_root",
                        "resolution": "exact_registered_workspace_root",
                    }
                matched, _ = self.product_registry.match_project(str(candidate), include_archived=True)
                if matched is not None:
                    return matched, {
                        "identity_status": "recovered",
                        "identity_issue": issue,
                        "classification": "workspace_root",
                        "resolution": "registered_workspace_root",
                    }
            except (ContractError, OSError, ValueError) as exc:
                issue = issue or str(exc) or type(exc).__name__
            if self.auto_onboard_workspaces:
                fingerprint = hashlib.sha256(os.path.normcase(str(candidate)).encode("utf-8")).hexdigest()
                try:
                    project, _ = self.product_registry.register(
                        project_id=f"workspace-{fingerprint[:32]}",
                        display_name=candidate.name or "Local workspace",
                        root_path=candidate,
                        logic_root=None,
                        auto_monitor_enabled=True,
                        source=MACHINE_WORKSPACE_SOURCE,
                        extra={"match_scope": "exact_cwd", "formal_vibe_binding": False},
                    )
                    return project, {
                        "identity_status": "registered",
                        "identity_issue": issue,
                        "classification": "new_workspace",
                        "resolution": "auto_onboarded_exact_workspace",
                    }
                except (ContractError, OSError, ValueError) as exc:
                    # Read-only context must remain available even when an
                    # unknown workspace cannot be registered (for example a
                    # protected or transient directory).  Keep the failure
                    # as evidence and fall through to the AP-Vibe landing
                    # project; strict write resolution remains unchanged.
                    issue = issue or str(exc) or type(exc).__name__

        # The daemon always has a local AP‑Vibe project.  It is the final
        # read-only landing area, clearly marked as a fallback, and never
        # authorizes a cross-project write.
        project = self.product_registry.get(self.codex_project_id, include_archived=True)
        return project, {
            "identity_status": "fallback",
            "identity_issue": issue,
            "classification": "unresolved",
            "resolution": "default_ap_vibe_project",
        }

    def discover_codex_sources(self) -> dict[str, Any]:
        if self.codex_sessions_root is None:
            return {
                "status": "unavailable",
                "configured": False,
                "processed_count": 0,
                "reason": "codex_sessions_root_not_configured",
                "next_action": "在启动配置中设置 --codex-sessions-root；现有手动来源仍可使用。",
            }
        report = CodexSessionDiscovery(self.codex_sessions_root).discover()
        assigned: list[dict[str, Any]] = []
        unassigned: list[dict[str, Any]] = []
        for session in report.sessions:
            try:
                existing = self.product_registry.source(session.source_key)
            except ContractError as exc:
                if str(exc) != "codex_source_not_found":
                    raise
                existing = None
            if existing is not None:
                project = self.product_registry.get(existing.project_id)
                if project.status == "archived" and session.modified_at > (project.archived_at or "") and existing.status != "disabled":
                    project = self.organization.unclassify(existing)
                    existing = self.product_registry.source(session.source_key)
                if existing.status == "disabled" or project.status == "archived":
                    continue
                try:
                    # Discovery is read-heavy and runs alongside the monitor.
                    # Avoid a BEGIN IMMEDIATE for every unchanged source; only
                    # re-enter the registration transaction when the observed
                    # file or session identity changed and the durable cursor
                    # projection needs reconciliation.
                    unchanged = (
                        Path(existing.source_path).resolve() == Path(session.source_path).resolve()
                        and existing.session_id == session.session_id
                        and os.path.normcase(str(Path(existing.session_cwd).resolve()))
                        == os.path.normcase(str(Path(session.cwd).resolve()))
                        and existing.source_size == session.source_size
                        and existing.modified_at == session.modified_at
                    )
                    if unchanged:
                        source, replayed = existing, True
                    else:
                        source, replayed = self.product_registry.register_source(session, project.project_id)
                    assigned.append({**source.to_dict(), "replayed": replayed})
                except ContractError as exc:
                    unassigned.append({"source_key": session.source_key, "source_name": session.source_name, "reason": str(exc)})
                continue
            try:
                membership = self.task_context.projects.membership('codex', session.session_id) if session.session_id else None
                if membership:
                    project = self.product_registry.get(membership['project_id'], include_archived=False)
                elif self.auto_onboard_workspaces:
                    if not session.session_id:
                        raise ContractError("codex_binding_session_id_required")
                    project = self.resolve_codex_workspace(session.cwd)
                else:
                    project, reason = self.product_registry.match_project(session.cwd)
                    if project is None:
                        raise ContractError(reason or "session_cwd_not_registered")
            except ContractError as exc:
                unassigned.append(
                    {
                        "source_name": session.source_name,
                        "source_key": session.source_key,
                        "reason": str(exc),
                    }
                )
                continue
            try:
                source, replayed = self.product_registry.register_source(session, project.project_id, explicit=bool(membership))
            except ContractError as exc:
                unassigned.append(
                    {
                        "source_name": session.source_name,
                        "source_key": session.source_key,
                        "reason": str(exc),
                    }
                )
                continue
            assigned.append({**source.to_dict(), "replayed": replayed})
        return {
            "status": "partial" if unassigned or report.completeness != "complete" else "success",
            "configured": True,
            "auto_onboard_workspaces": self.auto_onboard_workspaces,
            "report": report.to_dict(),
            "assigned": assigned,
            "unassigned": unassigned[:64],
            "processed_count": 0,
        }

    def bind_codex_source(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Pin an explicitly selected discovered session without accepting paths."""
        if set(raw) != {"project_id", "source_key", "session_id", "enabled"} or not isinstance(raw.get("enabled"), bool):
            raise ContractError("codex_binding_payload_invalid")
        for field in ("project_id", "source_key", "session_id"):
            if not isinstance(raw[field], str) or not raw[field].strip() or len(raw[field]) > 256:
                raise ContractError("codex_binding_payload_invalid")
        project = self._require_project(raw["project_id"])
        with self._lock:
            if not raw["enabled"]:
                source = self.product_registry.source(raw["source_key"])
                if source.project_id != project.project_id or source.session_id != raw["session_id"]:
                    raise ContractError("codex_binding_identity_changed")
                source = self.product_registry.update_source(source.source_key, status="disabled")
                return {"status": "success", "source": source.to_dict(), "formal_knowledge_write": False}
            if self.codex_sessions_root is None:
                raise ContractError("codex_sessions_root_not_configured")
            found = [item for item in CodexSessionDiscovery(self.codex_sessions_root).discover().sessions
                     if item.source_key == raw["source_key"] and item.session_id == raw["session_id"]]
            if len(found) != 1:
                raise ContractError("codex_binding_source_not_discovered")
            source, replayed = self.product_registry.register_source(found[0], project.project_id, explicit=True)
            if source.status == "disabled":
                source = self.product_registry.update_source(source.source_key, status="ready")
        if self.codex_monitor is not None:
            self.codex_monitor.wake()
        return {"status": "success", "replayed": replayed, "source": source.to_dict(), "formal_knowledge_write": False}

    @staticmethod
    def _episode_file(request_id: str) -> str:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return f"episode-{digest}.sqlite"

    @staticmethod
    def _vibe_episode_file(request_kind: str, request_id: str) -> str:
        digest = hashlib.sha256(f"{request_kind}\x00{request_id}".encode("utf-8")).hexdigest()
        return f"vibe-{request_kind}-{digest}.sqlite"

    @staticmethod
    def _vibe_fingerprint(request_kind: str, request_id: str, payload: Mapping[str, Any]) -> str:
        canonical = _json(
            {
                "request_kind": request_kind,
                "request_id": request_id,
                "payload": dict(payload),
            }
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _request_id(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ContractError("request_id_required")
        request_id = value.strip()
        if len(request_id) > 256:
            raise ContractError("request_id_too_long")
        return request_id

    def run(self, request: StudioEpisodeRequest) -> tuple[dict[str, Any], bool]:
        """Return ``(projection, replayed)`` for one idempotent request."""

        if not isinstance(request, StudioEpisodeRequest):
            raise ContractError("studio_episode_request_required")
        fingerprint = request.fingerprint()
        now = _now()
        with self._lock:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM studio_requests WHERE request_id = ?",
                    (request.request_id,),
                ).fetchone()
                if row is not None and str(row["fingerprint"]) != fingerprint:
                    raise StudioRequestConflict("studio_request_id_reused_with_different_input")
                if row is not None and row["state"] == "completed" and row["response_json"]:
                    return json.loads(str(row["response_json"])), True
                episode_file = (
                    str(row["episode_db"])
                    if row is not None
                    else self._episode_file(request.request_id)
                )
                if row is None:
                    connection.execute(
                        """INSERT INTO studio_requests
                        (request_id, fingerprint, state, episode_db, response_json,
                         error_json, created_at, updated_at)
                        VALUES (?, ?, 'running', ?, NULL, NULL, ?, ?)""",
                        (request.request_id, fingerprint, episode_file, now, now),
                    )
                else:
                    connection.execute(
                        """UPDATE studio_requests
                        SET state = 'running', error_json = NULL, updated_at = ?
                        WHERE request_id = ?""",
                        (now, request.request_id),
                    )
                connection.commit()

            episode_path = (self.episodes_dir / episode_file).resolve()
            if episode_path.parent != self.episodes_dir.resolve():
                raise ContractError("studio_episode_path_outside_data_root")
            try:
                view = run_studio_episode(episode_path, request).to_dict()
            except Exception as exc:
                failure = {
                    "code": "studio_episode_failed",
                    "error_type": type(exc).__name__,
                    "retryable": True,
                    "solution": "保留当前 request_id 并重试；已完成的物理回读会从 episode 数据库恢复，不会盲目重发。",
                }
                with closing(self._connect()) as connection:
                    connection.execute(
                        """UPDATE studio_requests
                        SET state = 'failed', error_json = ?, updated_at = ?
                        WHERE request_id = ?""",
                        (_json(failure), _now(), request.request_id),
                    )
                    connection.commit()
                raise

            with closing(self._connect()) as connection:
                connection.execute(
                    """UPDATE studio_requests
                    SET state = 'completed', response_json = ?, error_json = NULL,
                        updated_at = ? WHERE request_id = ?""",
                    (_json(view), _now(), request.request_id),
                )
                connection.commit()
            return view, row is not None

    def _existing_vibe_request(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str,
        request_kind: str,
        fingerprint: str,
        project_id: str,
    ) -> sqlite3.Row | None:
        storage_id = self._vibe_storage_request_id(project_id, request_id)
        row = connection.execute(
            "SELECT * FROM vibe_requests WHERE request_id = ?",
            (storage_id,),
        ).fetchone()
        if row is not None and (
            str(row["request_kind"]) != request_kind
            or str(row["fingerprint"]) != fingerprint
        ):
            raise StudioRequestConflict("vibe_request_id_reused_with_different_input")
        return row

    def _vibe_storage_request_id(self, project_id: str, request_id: str) -> str:
        """Namespace non-default request keys without changing client identity."""

        if project_id == self.codex_project_id:
            return request_id
        digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16]
        return f"project-{digest}:{request_id}"

    def _reserve_vibe_request(
        self,
        *,
        request_id: str,
        request_kind: str,
        project_id: str,
        payload: Mapping[str, Any],
    ) -> tuple[sqlite3.Row | None, Path, str]:
        fingerprint = self._vibe_fingerprint(request_kind, request_id, payload)
        storage_id = self._vibe_storage_request_id(project_id, request_id)
        now = _now()
        with closing(self._connect()) as connection:
            row = self._existing_vibe_request(
                connection,
                request_id=request_id,
                request_kind=request_kind,
                fingerprint=fingerprint,
                project_id=project_id,
            )
            if row is not None and row["state"] == "completed" and row["response_json"]:
                return row, Path(), str(row["response_json"])
            episode_file = (
                str(row["episode_db"])
                if row is not None
                else self._vibe_episode_file(request_kind, storage_id)
            )
            if row is None:
                connection.execute(
                    """INSERT INTO vibe_requests
                    (request_id, request_kind, fingerprint, state, project_id,
                     episode_id, episode_db, request_json, response_json,
                     error_json, created_at, updated_at)
                    VALUES (?, ?, ?, 'running', ?, NULL, ?, ?, NULL, NULL, ?, ?)""",
                    (
                        storage_id,
                        request_kind,
                        fingerprint,
                        project_id,
                        episode_file,
                        _json(dict(payload)),
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """UPDATE vibe_requests
                    SET state = 'running', error_json = NULL, updated_at = ?
                    WHERE request_id = ?""",
                    (now, storage_id),
                )
            connection.commit()
        episode_path = (self.episodes_dir / episode_file).resolve()
        if episode_path.parent != self.episodes_dir.resolve():
            raise ContractError("vibe_episode_path_outside_data_root")
        return row, episode_path, ""

    def _complete_vibe_request(
        self,
        *,
        request_id: str,
        project_id: str,
        episode_id: str,
        view: Mapping[str, Any],
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE vibe_requests
                SET state = 'completed', episode_id = ?, response_json = ?,
                    error_json = NULL, updated_at = ? WHERE request_id = ?""",
                (
                    episode_id,
                    _json(dict(view)),
                    _now(),
                    self._vibe_storage_request_id(project_id, request_id),
                ),
            )
            connection.commit()

    def _fail_vibe_request(self, request_id: str, project_id: str, code: str) -> None:
        failure = {
            "code": code,
            "retryable": True,
            "solution": "保留当前 request_id 重试；已完成的 dispatch/readback 和课程应用会从本地状态恢复。",
        }
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE vibe_requests SET state = 'failed', error_json = ?,
                updated_at = ? WHERE request_id = ?""",
                (
                    _json(failure),
                    _now(),
                    self._vibe_storage_request_id(project_id, request_id),
                ),
            )
            connection.commit()

    def run_project_activity(
        self,
        request_id: str,
        raw_activity: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Run or replay one source-grounded project activity episode."""

        request_id = self._request_id(request_id)
        if not isinstance(raw_activity, Mapping):
            raise ContractError("project_activity_required")
        canonical_activity, project = self._canonical_project_payload(raw_activity)
        activity = ProjectActivity.from_dict(canonical_activity)
        # Keep the canonical identity in every downstream event/ledger row;
        # the historical alias is accepted only at this boundary.
        if activity.project_id != project.project_id:
            raise ContractError("project_activity_project_mismatch")
        with self._lock:
            excluded_memory_activity_ids = tuple(
                sorted(
                    activity_id
                    for activity_id, disposition in self.product_registry.memory_states(project.project_id).items()
                    if isinstance(disposition, Mapping) and disposition.get("state") == "archived"
                )
            )
            row, episode_path, cached = self._reserve_vibe_request(
                request_id=request_id,
                request_kind="activity",
                project_id=activity.project_id,
                # Fingerprint the canonical payload so the historical
                # ``project-local`` alias and the persisted project identity
                # replay the same episode rather than creating two records.
                payload=canonical_activity,
            )
            if cached:
                return json.loads(cached), True
            try:
                run = run_project_episode(
                    episode_path,
                    activity,
                    learning_ledger=self.learning_ledger,
                    gateway=self.gateway,
                    governance=self.governance,
                    capability=self.capability,
                    teacher_capabilities=self.teacher_capabilities,
                    excluded_memory_activity_ids=excluded_memory_activity_ids,
                )
                view = project_activity_run(request_id, run).to_dict()
            except Exception:
                self._fail_vibe_request(request_id, activity.project_id, "project_activity_episode_failed")
                raise
            self._complete_vibe_request(
                request_id=request_id,
                project_id=activity.project_id,
                episode_id=run.episode_id,
                view=view,
            )
            return view, row is not None

    @staticmethod
    def _logic_activity(query: LogicQuery, report: Mapping[str, Any]) -> ProjectActivity:
        """Compress a local instrument report into one bounded AP occurrence."""

        snapshot = report.get("snapshot") if isinstance(report.get("snapshot"), Mapping) else {}
        first_break = report.get("first_break") if isinstance(report.get("first_break"), Mapping) else None
        raw_unknowns = report.get("unknowns")
        unknowns: list[str] = []
        if isinstance(raw_unknowns, Sequence) and not isinstance(raw_unknowns, (str, bytes)):
            for item in raw_unknowns[:12]:
                if not isinstance(item, Mapping):
                    continue
                code = str(item.get("code") or "logic_field_unknown")[:160]
                meaning = str(item.get("meaning") or "静态仪器没有足够证据")[:480]
                unknowns.append(f"{code}: {meaning}")
        limitations = report.get("limitations")
        if isinstance(limitations, Sequence) and not isinstance(limitations, (str, bytes)):
            for item in limitations[:5]:
                if isinstance(item, str):
                    unknowns.append(f"boundary: {item[:220]}")
        evidence_refs: list[str] = [query.source_ref]
        snapshot_id = snapshot.get("snapshot_id")
        if isinstance(snapshot_id, str) and snapshot_id:
            evidence_refs.append(f"logic-snapshot://{snapshot_id}")
        raw_nodes = report.get("nodes")
        if isinstance(raw_nodes, Sequence) and not isinstance(raw_nodes, (str, bytes)):
            for node in raw_nodes[:8]:
                if not isinstance(node, Mapping):
                    continue
                refs = node.get("evidence_refs")
                if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
                    evidence_refs.extend(str(ref) for ref in refs[:1] if isinstance(ref, str))
        detail = {
            "query_kind": query.kind,
            "target": query.target,
            "status": report.get("status"),
            "first_break": first_break,
            "next_check": report.get("next_check"),
            "node_count": len(raw_nodes) if isinstance(raw_nodes, Sequence) and not isinstance(raw_nodes, (str, bytes)) else 0,
            "edge_count": len(report.get("edges", ())) if isinstance(report.get("edges"), Sequence) else 0,
            "completeness": report.get("completeness"),
        }
        source_ref = f"logic-field://{snapshot_id or 'unknown'}/{report.get('report_id') or query.query_id}"
        next_check = str(report.get("next_check") or "核对静态观察所缺少的运行证据")[:2_048]
        return ProjectActivity(
            activity_id=f"logic-activity-{report.get('report_id') or query.query_id}",
            project_id=query.project_id,
            kind="logic_field_observation",
            summary=str(report.get("summary") or "本地逻辑场完成一次有界观察")[:12_000],
            detail=_json(detail),
            actor="local_instrument",
            status="observed_instrument_report",
            source_ref=source_ref,
            evidence_refs=tuple(dict.fromkeys(evidence_refs))[:16],
            completeness=str(report.get("completeness") or "unknown"),
            privacy_scope="project",
            observed_remaining=(next_check,),
            observed_unknown=tuple(dict.fromkeys(unknowns))[:32],
            observed_next_action=next_check,
            extra={
                "instrument_kind": "logic_field",
                "logic_report_ref": report.get("report_id"),
                "logic_snapshot_ref": snapshot_id,
                "logic_query_kind": query.kind,
                "logic_status": report.get("status"),
                "logic_first_break_status": first_break.get("status") if first_break else None,
                "instrument_is_truth_oracle": False,
                "instrument_sets_feeling": False,
                "instrument_sets_action_winner": False,
            },
        )

    def run_logic_query(
        self,
        request_id: str,
        raw_query: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Run one source snapshot query, then feed its observation to AP."""

        request_id = self._request_id(request_id)
        canonical_query, _ = self._canonical_project_payload(raw_query)
        query = LogicQuery.from_dict(canonical_query)
        project = self._require_project(query.project_id)
        logic_field = self._logic_fields.get(query.project_id)
        if logic_field is None:
            if project.effective_logic_root is None or not Path(project.effective_logic_root).is_dir():
                raise ContractError("logic_field_not_configured")
            logic_field = LogicFieldInstrument(project.effective_logic_root)
            self._logic_fields[query.project_id] = logic_field
        # The report timestamp is not part of request identity.  The frozen
        # source snapshot is: changing source bytes makes reuse conflict.
        report = logic_field.query(query)
        report_view = report.to_dict()
        activity = self._logic_activity(query, report_view)
        registry_payload = {
            "query": query.to_dict(),
            "snapshot_id": report.snapshot.snapshot_id,
            "snapshot_content_hash": report.snapshot.content_hash,
        }
        with self._lock:
            excluded_memory_activity_ids = tuple(
                sorted(
                    activity_id
                    for activity_id, disposition in self.product_registry.memory_states(query.project_id).items()
                    if isinstance(disposition, Mapping) and disposition.get("state") == "archived"
                )
            )
            row, episode_path, cached = self._reserve_vibe_request(
                request_id=request_id,
                request_kind="logic_query",
                project_id=query.project_id,
                payload=registry_payload,
            )
            if cached:
                return json.loads(cached), True
            try:
                run = run_project_episode(
                    episode_path,
                    activity,
                    learning_ledger=self.learning_ledger,
                    gateway=self.gateway,
                    governance=self.governance,
                    capability=self.capability,
                    teacher_capabilities=self.teacher_capabilities,
                    excluded_memory_activity_ids=excluded_memory_activity_ids,
                )
                view = project_activity_run(request_id, run).to_dict()
                view["episode_kind"] = "logic_field_observation"
                view["input"] = {
                    **dict(view.get("input", {})),
                    "logic_query": query.to_dict(),
                }
                view["logic_field"] = report_view
                view["product_effect"] = "local_logic_field_observation_processed_through_ap_episode"
                view["ownership"] = {
                    **dict(view.get("ownership", {})),
                    "logic_source_extraction": "native",
                    "logic_query_resolution": "native",
                    "logic_runtime_truth": "absent",
                    "logic_action_winner": "native",
                }
                view["limitations"] = list(
                    dict.fromkeys(
                        (*view.get("limitations", ()), *report.limitations)
                    )
                )
            except Exception:
                self._fail_vibe_request(request_id, query.project_id, "logic_query_episode_failed")
                raise
            self._complete_vibe_request(
                request_id=request_id,
                project_id=query.project_id,
                episode_id=run.episode_id,
                view=view,
            )
            return view, row is not None

    def _target_activity(self, target_episode_id: str, project_id: str) -> ProjectActivity:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT request_kind, request_json, response_json FROM vibe_requests
                WHERE request_kind IN ('activity', 'logic_query') AND state = 'completed'
                  AND episode_id = ? AND project_id = ?
                ORDER BY updated_at DESC LIMIT 1""",
                (target_episode_id, project_id),
            ).fetchone()
        if row is None:
            raise ProjectTargetNotFound("feedback_target_activity_not_found")
        raw = json.loads(str(row["request_json"]))
        if not isinstance(raw, Mapping):
            raise ProjectTargetNotFound("feedback_target_activity_invalid")
        if str(row["request_kind"]) == "logic_query":
            try:
                response = json.loads(str(row["response_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ProjectTargetNotFound("feedback_target_activity_invalid") from exc
            input_view = response.get("input") if isinstance(response, Mapping) else None
            raw = input_view.get("activity") if isinstance(input_view, Mapping) else None
            if not isinstance(raw, Mapping):
                raise ProjectTargetNotFound("feedback_target_activity_invalid")
        # Older rows may have been written by the compatibility alias.  The
        # enclosing query already proved ownership by project_id, so normalize
        # only that identity field before constructing the typed activity.
        if isinstance(raw, Mapping) and raw.get("project_id") != project_id:
            raw = {**dict(raw), "project_id": project_id}
        return ProjectActivity.from_dict(raw)

    def run_project_feedback(
        self,
        request_id: str,
        raw_feedback: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Run feedback through AP, readback, and the shared local learner."""

        request_id = self._request_id(request_id)
        if not isinstance(raw_feedback, Mapping):
            raise ContractError("project_feedback_required")
        canonical_feedback, _ = self._canonical_project_payload(raw_feedback)
        feedback = ProjectFeedback.from_dict(canonical_feedback)
        with self._lock:
            # Fingerprint the same canonical payload used by reservation.  This
            # keeps a historical ``project-local`` alias replay-compatible with
            # the persisted canonical project identity after a cold restart.
            fingerprint = self._vibe_fingerprint("feedback", request_id, canonical_feedback)
            with closing(self._connect()) as connection:
                existing = self._existing_vibe_request(
                    connection,
                    request_id=request_id,
                    request_kind="feedback",
                    fingerprint=fingerprint,
                    project_id=feedback.project_id,
                )
                if existing is not None and existing["state"] == "completed" and existing["response_json"]:
                    return json.loads(str(existing["response_json"])), True
            target_activity = self._target_activity(
                feedback.target_episode_id,
                feedback.project_id,
            )
            row, episode_path, cached = self._reserve_vibe_request(
                request_id=request_id,
                request_kind="feedback",
                project_id=feedback.project_id,
                payload=canonical_feedback,
            )
            if cached:
                return json.loads(cached), True
            try:
                run = run_feedback_episode(
                    episode_path,
                    feedback,
                    target_activity,
                    learning_ledger=self.learning_ledger,
                    gateway=self.gateway,
                    governance=self.governance,
                    capability=self.capability,
                )
                view = project_feedback_run(request_id, run).to_dict()
            except Exception:
                self._fail_vibe_request(request_id, feedback.project_id, "project_feedback_episode_failed")
                raise
            self._complete_vibe_request(
                request_id=request_id,
                project_id=feedback.project_id,
                episode_id=run.episode_id,
                view=view,
            )
            return view, row is not None

    def _knowledge_proposals(
        self,
        project_id: str,
        *,
        limit: int = 256,
    ) -> list[ProjectKnowledgeProposal]:
        """Resolve staged candidates from durable activity projections.

        The browser cannot submit proposal content.  This is the sole bridge
        from the request registry to the local recovery knowledge store.
        """

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT response_json FROM vibe_requests
                   WHERE request_kind IN ('activity', 'logic_query', 'knowledge_correction_confirm')
                     AND state = 'completed'
                     AND project_id = ? AND response_json IS NOT NULL
                   ORDER BY updated_at DESC LIMIT ?""",
                (project_id, max(1, min(512, int(limit)))),
            ).fetchall()
        proposals: list[ProjectKnowledgeProposal] = []
        seen: set[str] = set()
        for row in rows:
            try:
                view = json.loads(str(row["response_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            raw_items = view.get("knowledge_candidates") if isinstance(view, Mapping) else None
            if isinstance(raw_items, (str, bytes)) or not isinstance(raw_items, list):
                continue
            for raw in raw_items[:8]:
                if not isinstance(raw, Mapping):
                    continue
                try:
                    proposal = ProjectKnowledgeProposal.from_dict(raw)
                except ContractError:
                    continue
                if proposal.status != "staged" or proposal.proposal_id in seen:
                    continue
                seen.add(proposal.proposal_id)
                proposals.append(proposal)
        return proposals

    def preview_knowledge_correction(
        self,
        request_id: str,
        raw_correction: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Persist a reviewable draft without changing any knowledge revision."""

        request_id = self._request_id(request_id)
        canonical_correction, _ = self._canonical_project_payload(raw_correction)
        correction = KnowledgeCorrectionRequest.from_dict(canonical_correction)
        with self._lock:
            draft, replayed = self.correction_store.create_or_replay(
                request_id,
                correction,
                self.knowledge_store.latest(correction.project_id),
                self.correction_interpreter,
            )
        return draft.to_dict(), replayed

    def confirm_knowledge_correction(
        self,
        request_id: str,
        raw_confirmation: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Resolve the server-side draft and run the ordinary AP review flow."""

        request_id = self._request_id(request_id)
        allowed = {"decision_id", "project_id", "draft_id", "source_ref", "rationale", "expected_parent_revision_id"}
        if not isinstance(raw_confirmation, Mapping):
            raise ContractError("knowledge_correction_confirmation_required")
        canonical_confirmation, _ = self._canonical_project_payload(raw_confirmation)
        raw_confirmation = canonical_confirmation
        if set(raw_confirmation) - allowed:
            raise ContractError("knowledge_correction_confirmation_identity_only")
        project_id = raw_confirmation.get("project_id")
        if not isinstance(project_id, str):
            raise ContractError("knowledge_correction_project_mismatch")
        self._require_project(project_id)
        draft_id = raw_confirmation.get("draft_id")
        if not isinstance(draft_id, str) or not draft_id.strip():
            raise ContractError("knowledge_correction_draft_id_required")
        expected = raw_confirmation.get("expected_parent_revision_id")
        with self._lock:
            fingerprint = self._vibe_fingerprint("knowledge_correction_confirm", request_id, raw_confirmation)
            with closing(self._connect()) as connection:
                existing = self._existing_vibe_request(
                    connection,
                    request_id=request_id,
                    request_kind="knowledge_correction_confirm",
                    fingerprint=fingerprint,
                    project_id=project_id,
                )
                if existing is not None and existing["state"] == "completed" and existing["response_json"]:
                    return json.loads(str(existing["response_json"])), True
            draft = self.correction_store.get(project_id, draft_id.strip())
            if draft.proposal_incomplete or not draft.changes:
                raise ContractError("knowledge_correction_draft_incomplete")
            if expected != draft.parent_revision_id:
                raise KnowledgeCorrectionConflict("knowledge_correction_confirmation_parent_mismatch")
            latest = self.knowledge_store.latest(project_id)
            if expected != (latest.revision_id if latest is not None else None):
                raise KnowledgeCorrectionConflict("knowledge_correction_parent_revision_changed")
            proposal = draft.as_proposal()
            decision_id = raw_confirmation.get("decision_id")
            source_ref = raw_confirmation.get("source_ref")
            if not isinstance(decision_id, str) or not decision_id.strip():
                raise ContractError("knowledge_correction_decision_id_required")
            if not isinstance(source_ref, str) or not source_ref.strip():
                raise ContractError("knowledge_correction_source_ref_required")
            review = KnowledgeReviewRequest(
                decision_id=decision_id.strip(),
                project_id=project_id,
                proposal_id=proposal.proposal_id,
                source_ref=source_ref.strip(),
                rationale=str(raw_confirmation.get("rationale") or "用户确认应用这份项目知识纠正草稿"),
                expected_parent_revision=expected,
                extra={"knowledge_correction_draft_ref": draft.draft_id},
            )
            row, episode_path, cached = self._reserve_vibe_request(
                request_id=request_id,
                request_kind="knowledge_correction_confirm",
                project_id=project_id,
                payload=raw_confirmation,
            )
            if cached:
                return json.loads(cached), True
            try:
                run = run_knowledge_review_episode(
                    episode_path,
                    review,
                    proposal,
                    knowledge_store=self.knowledge_store,
                )
                view = project_knowledge_review_run(request_id, run).to_dict()
                view["episode_kind"] = "knowledge_correction_confirm"
                view["input"] = {
                    "knowledge_correction": {
                        "draft_id": draft.draft_id,
                        "instruction": draft.instruction,
                        "changes": [item.to_dict() for item in draft.changes],
                        "interpretation_source": draft.interpretation_source,
                    }
                }
                view["product_effect"] = (
                    "reviewed_knowledge_correction_revision_created"
                    if run.revision is not None
                    else "knowledge_correction_deferred_or_observed"
                )
                if run.revision is not None:
                    self.correction_store.mark_confirmed(draft.draft_id, run.revision.revision_id)
            except Exception:
                self._fail_vibe_request(request_id, project_id, "knowledge_correction_confirm_failed")
                raise
            self._complete_vibe_request(
                request_id=request_id,
                project_id=project_id,
                episode_id=run.episode_id,
                view=view,
            )
            return view, row is not None

    def _target_knowledge_proposal(
        self,
        project_id: str,
        proposal_id: str,
    ) -> ProjectKnowledgeProposal:
        proposal = next(
            (
                item
                for item in self._knowledge_proposals(project_id)
                if item.proposal_id == proposal_id
            ),
            None,
        )
        if proposal is None:
            raise KnowledgeProposalNotFound("knowledge_proposal_not_found")
        return proposal

    def run_project_knowledge_review(
        self,
        request_id: str,
        raw_review: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Run one user-authorized local recovery review through AP."""

        request_id = self._request_id(request_id)
        canonical_review, _ = self._canonical_project_payload(raw_review)
        review = KnowledgeReviewRequest.from_dict(canonical_review)
        with self._lock:
            fingerprint = self._vibe_fingerprint("knowledge_review", request_id, raw_review)
            with closing(self._connect()) as connection:
                existing = self._existing_vibe_request(
                    connection,
                    request_id=request_id,
                    request_kind="knowledge_review",
                    fingerprint=fingerprint,
                    project_id=review.project_id,
                )
                if existing is not None and existing["state"] == "completed" and existing["response_json"]:
                    return json.loads(str(existing["response_json"])), True
            proposal = self._target_knowledge_proposal(review.project_id, review.proposal_id)
            row, episode_path, cached = self._reserve_vibe_request(
                request_id=request_id,
                request_kind="knowledge_review",
                project_id=review.project_id,
                payload=raw_review,
            )
            if cached:
                return json.loads(cached), True
            try:
                run = run_knowledge_review_episode(
                    episode_path,
                    review,
                    proposal,
                    knowledge_store=self.knowledge_store,
                )
                view = project_knowledge_review_run(request_id, run).to_dict()
            except Exception:
                self._fail_vibe_request(request_id, review.project_id, "project_knowledge_review_failed")
                raise
            self._complete_vibe_request(
                request_id=request_id,
                project_id=review.project_id,
                episode_id=run.episode_id,
                view=view,
            )
            return view, row is not None

    def local_recovery(self, project_id: str | None = None) -> dict[str, Any]:
        selected = self._require_project(project_id or self.codex_project_id, active=False)
        return self.knowledge_store.recovery(
            selected.project_id,
            self._knowledge_proposals(selected.project_id),
        )

    def agent_brief(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ContractError("brief_project_mismatch")
        project_id = raw.get("project_id", self.codex_project_id)
        if not isinstance(project_id, str):
            raise ContractError("brief_project_mismatch")
        project = self._require_project(project_id, active=False)
        project_id = project.project_id
        goal = raw.get("goal", "恢复当前项目并从下一原子动作继续")
        max_chars = raw.get("max_chars", DEFAULT_BRIEF_CHARS)
        return self.knowledge_store.brief(
            project_id,
            goal=goal,
            pending=self._knowledge_proposals(project_id),
            max_chars=max_chars,
        )

    def recent_project_state(self, project_id: str | None = None, *, limit: int = 12) -> dict[str, Any]:
        selected = self._require_project(project_id or self.codex_project_id, active=False)
        project_id = selected.project_id
        if isinstance(limit, bool) or not 1 <= int(limit) <= MAX_RECENT_PROJECT_EPISODES:
            raise ContractError("recent_episode_limit_out_of_bounds")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT request_id, request_kind, project_id, episode_id,
                response_json, updated_at FROM vibe_requests
                WHERE state = 'completed' AND response_json IS NOT NULL
                  AND request_kind IN ('activity', 'logic_query', 'feedback')
                  AND project_id = ?
                ORDER BY updated_at DESC LIMIT ?""",
                (project_id, int(limit)),
            ).fetchall()
            review_rows = connection.execute(
                """SELECT request_id, request_kind, project_id, episode_id,
                response_json, updated_at FROM vibe_requests
                WHERE state = 'completed' AND response_json IS NOT NULL
                  AND request_kind IN ('knowledge_review', 'knowledge_correction_confirm')
                  AND project_id = ?
                ORDER BY updated_at DESC LIMIT 4"""
                , (project_id,)
            ).fetchall()
            logic_rows = connection.execute(
                """SELECT request_id, updated_at, response_json FROM vibe_requests
                WHERE state = 'completed' AND response_json IS NOT NULL
                  AND request_kind = 'logic_query' AND project_id = ?
                ORDER BY updated_at DESC LIMIT 40""", (project_id,)
            ).fetchall()
        episodes = []
        for row in rows:
            view = json.loads(str(row["response_json"]))
            episodes.append(
                {
                    "request_id": str(row["request_id"]),
                    "kind": str(row["request_kind"]),
                    "project_id": str(row["project_id"]),
                    "episode_id": str(row["episode_id"]),
                    "updated_at": str(row["updated_at"]),
                    "episode": view,
                }
            )
        knowledge_reviews = []
        for row in review_rows:
            knowledge_reviews.append(
                {
                    "request_id": str(row["request_id"]),
                    "kind": str(row["request_kind"]),
                    "project_id": str(row["project_id"]),
                    "episode_id": str(row["episode_id"]),
                    "updated_at": str(row["updated_at"]),
                    "episode": json.loads(str(row["response_json"])),
                }
            )
        recovery = self.local_recovery(project_id)
        return {
            "status": "ok",
            "mode": "hybrid_teacher" if self.provider_state()["configured"] else "local_provider_off",
            "provider": self.provider_state(),
            "episodes": episodes,
            "last_success": episodes[0] if episodes else None,
            "knowledge_reviews": knowledge_reviews,
            "logic_history": [
                {"request_id": row["request_id"], "updated_at": row["updated_at"],
                 "episode": json.loads(row["response_json"])} for row in logic_rows
            ],
            "project": selected.to_dict(),
            "knowledge_corrections": self.correction_store.recent(project_id),
            "recovery": recovery,
            "formal_knowledge_write": False,
            "vibe_formal_knowledge_write": False,
            "codex_sampler": self.codex_sampler_state(project_id),
            "monitor": self.codex_monitor.state(compact=True) if self.codex_monitor is not None else {
                "configured": False,
                "running": False,
                "status": "unavailable",
            },
            "unknowns": [
                "live_vibe_control_unavailable",
                "real_llm_teacher_long_run_quality_unmeasured"
                if self.provider_state()["configured"]
                else "real_llm_teacher_unmeasured",
            ],
        }

    def provider_state(self) -> dict[str, Any]:
        configured = not isinstance(self.gateway, NullGateway)
        return {
            "configured": configured,
            "status": "ready" if configured else "off",
            "provider": getattr(self.gateway, "provider", None) if configured else None,
            "model": getattr(self.gateway, "model", None) if configured else None,
            "advisor_role": getattr(self.gateway, "advisor_role", None) if configured else None,
            "max_retries": getattr(self.gateway, "max_retries", None) if configured else None,
            "max_output_tokens": getattr(self.gateway, "max_output_tokens", None) if configured else None,
        }

    def codex_sampler_state(self, project_id: str | None = None) -> dict[str, Any]:
        selected = self._require_project(project_id or self.codex_project_id, active=False)
        sources = self.product_registry.sources(selected.project_id)
        primary = sources[0] if sources else None
        return {
            "configured": bool(sources) or self.codex_sessions_root is not None,
            "mode": "bounded_multi_source_read_only" if sources else (
                "auto_discovery_ready" if self.codex_sessions_root is not None else "unavailable"
            ),
            "project_id": selected.project_id,
            "source_name": primary.source_name if primary else None,
            "source_key": primary.source_key if primary else None,
            "cursor": primary.cursor if primary else None,
            "source_size": primary.source_size if primary else None,
            "status": primary.status if primary else "ready" if self.codex_sessions_root is not None else "unavailable",
            "updated_at": primary.updated_at if primary else None,
            "last_batch": primary.last_batch if primary else None,
            "sources": [source.to_dict() for source in sources],
            "source_count": len(sources),
            "source_count_total": self.product_registry.source_count(selected.project_id),
            "auto_discovery_configured": self.codex_sessions_root is not None,
            "auto_onboard_workspaces": self.auto_onboard_workspaces,
            "workspace_match_scope": "exact_cwd" if selected.source == MACHINE_WORKSPACE_SOURCE else "project_root",
        }

    def codex_control_state(self, project_id: str | None = None, *, details: bool = False) -> dict[str, Any]:
        """Return the bounded source and monitor projection for one project."""

        selected = self._require_project(project_id or self.codex_project_id, active=False)
        return {
            "status": "success",
            "project_id": selected.project_id,
            "sampler": self.codex_sampler_state(selected.project_id),
            "monitor": self.codex_monitor.state(compact=not details) if self.codex_monitor is not None else {
                "configured": False,
                "running": False,
                "status": "unavailable",
            },
            "automatic_discovery_configured": self.codex_sessions_root is not None,
            "formal_knowledge_write": False,
        }

    @staticmethod
    def _codex_source_key_from_ref(source_ref: Any) -> str | None:
        """Extract only the opaque source key from a redacted Codex ref."""

        if not isinstance(source_ref, str) or not source_ref.startswith("codex-jsonl://"):
            return None
        try:
            values = parse_qs(urlsplit(source_ref).query, keep_blank_values=False).get("source", ())
        except Exception:
            return None
        value = values[0] if values else None
        return value.strip()[:128] if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _resolve_codex_source_key(source_key: Any, source_keys: Mapping[str, Any]) -> str | None:
        """Resolve current and legacy opaque source keys without guessing.

        Receptor versions before the full-key contract emitted a 16-character
        prefix.  A prefix is accepted only when it identifies exactly one
        currently registered source; malformed or ambiguous references stay
        unassigned so one conversation cannot be shown under another title.
        """

        if not isinstance(source_key, str):
            return None
        value = source_key.strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{16,64}", value):
            return None
        if value in source_keys:
            return value
        candidates = [candidate for candidate in source_keys if candidate.casefold().startswith(value)]
        return candidates[0] if len(candidates) == 1 else None

    def _codex_titles(self) -> dict[str, str]:
        """Read only the bounded, public task-name index beside sessions."""
        if self.codex_sessions_root is None:
            return {}
        index = self.codex_sessions_root.parent / "session_index.jsonl"
        titles: dict[str, str] = {}
        try:
            if index.is_symlink() or not index.is_file():
                return titles
            with index.open("rb") as handle:
                size = index.stat().st_size
                handle.seek(max(0, size - 2 * 1024 * 1024))
                if handle.tell():
                    handle.readline(64 * 1024)
                for line in handle.read(2 * 1024 * 1024).splitlines():
                    if len(line) > 64 * 1024:
                        continue
                    try:
                        item = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(item, dict):
                        continue
                    session_id, title = item.get("id"), item.get("thread_name")
                    if isinstance(session_id, str) and isinstance(title, str) and title.strip():
                        titles[session_id] = _clean_text(title)[:240]
        except OSError:
            pass
        return titles

    @staticmethod
    def _codex_task_identity(source) -> dict[str, str]:
        """Only public identity fields from the first bounded metadata line."""
        try:
            with Path(source.source_path).open("rb") as handle:
                line = handle.readline(64 * 1024 + 1)
            if len(line) > 64 * 1024:
                return {}
            item = json.loads(line)
            payload = item.get("payload", {})
            if item.get("type") != "session_meta" or payload.get("id") != source.session_id:
                return {}
            origin = payload.get("source")
            sub = origin.get("subagent", {}) if isinstance(origin, dict) else {}
            spawn = sub.get("thread_spawn", {}) if isinstance(sub, dict) else {}
            if not isinstance(spawn, dict):
                spawn = {}
            return {key: _clean_text(str(value))[:160] for key, value in {
                "parent_session_id": spawn.get("parent_thread_id"),
                "task_name": (spawn.get("agent_path") or "").rsplit("/", 1)[-1],
                "agent_name": payload.get("agent_nickname") or spawn.get("agent_nickname"),
            }.items() if value}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    @staticmethod
    def _codex_fallback_title(value: str) -> str:
        # Prompt wrappers are not a user's task title. Prefer the explicit
        # request section when it exists, then a short human-readable line.
        if "## My request:" in value:
            value = value.split("## My request:", 1)[1]
        value = re.sub(r"<[^>]+>[\s\S]*?</[^>]+>", "", value)
        value = re.sub(r":codex-[a-z-]+\{[^}]*\}", "", value)
        lines = [re.sub(r"^[#>*\s]+", "", line).strip() for line in value.splitlines() if line.strip()]
        title = lines[0] if lines else "标题尚未取得"
        if title.startswith(("Response annotations:", "The following is the Codex agent history")):
            return "后台审查任务"
        return title[:56] + ("…" if len(title) > 56 else "")

    @staticmethod
    def _codex_observation_text(activity: Mapping[str, Any]) -> str:
        """Return visible message text without exposing private event fields."""

        detail = activity.get("detail")
        summary = activity.get("summary")
        value = detail if isinstance(detail, str) and detail.strip() else summary
        return _clean_text(str(value or ""))[:12_000]

    @staticmethod
    def _codex_role(activity: Mapping[str, Any]) -> str | None:
        role = activity.get("source_role")
        if role in {"user", "assistant"}:
            return str(role)
        kind = activity.get("kind")
        if kind == "codex_visible_user_message":
            return "user"
        if kind == "codex_visible_assistant_message":
            return "assistant"
        return None

    @staticmethod
    def _codex_iso_seconds(value: Any) -> float | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return None

    def codex_overview(
        self,
        project_id: str | None = None,
        *,
        include_archived: bool = False,
        limit: int = MAX_CODEX_OVERVIEW_SESSIONS,
    ) -> dict[str, Any]:
        """Build a bounded, read-only session view for the monitoring UI.

        Titles prefer Codex's task-name index, matched by stable session ID.
        A visible-message summary is a labelled fallback. This projection does
        not create episodes or move cursors.
        """

        if isinstance(limit, bool) or not 1 <= int(limit) <= MAX_CODEX_OVERVIEW_SESSIONS:
            raise ContractError("codex_overview_limit_out_of_bounds")
        if project_id:
            projects = [self._require_project(project_id, active=not include_archived)]
        else:
            projects = list(self.product_registry.list(include_archived=include_archived))
        now_seconds = datetime.now(timezone.utc).timestamp()
        titles = self._codex_titles()
        sessions: list[dict[str, Any]] = []
        for project in projects:
            sources = self.product_registry.sources(project.project_id, limit=128)
            if not sources:
                continue
            project_name = self.task_context.documents.profile(project)["display_name"]
            source_keys = {source.source_key: source for source in sources}
            observations = self.learning_ledger.memory_observations(
                project.project_id,
                limit=min(2_000, max(128, len(sources) * MAX_CODEX_OVERVIEW_MESSAGES * 2)),
            )
            observations = list(observations)
            # Source assignment changes future routing, not historical truth.
            # Retrieve historical observations only through recorded lineage,
            # then filter by the exact source key below.
            with closing(self.product_registry._connect()) as history_connection:
                placeholders = ','.join('?' for _ in source_keys)
                previous_projects = {row[0] for row in history_connection.execute(
                    f'SELECT DISTINCT from_project_id FROM session_project_assignments WHERE source_key IN ({placeholders})', tuple(source_keys))
                    if row[0] != project.project_id}
            for prior_project in previous_projects:
                observations.extend(self.learning_ledger.memory_observations(prior_project, limit=2000))
            by_source: dict[str, list[dict[str, Any]]] = {}
            for item in observations:
                activity = item.get("activity") if isinstance(item.get("activity"), Mapping) else {}
                raw_source_key = self._codex_source_key_from_ref(item.get("source_ref") or activity.get("source_ref"))
                source_key = self._resolve_codex_source_key(raw_source_key, source_keys)
                role = self._codex_role(activity)
                text = self._codex_observation_text(activity)
                if not source_key or not role or not text:
                    continue
                by_source.setdefault(source_key, []).append(
                    {
                        "message_id": str(activity.get("activity_id") or item.get("activity_id") or "")[:256],
                        "role": role,
                        "text": text,
                        "timestamp": str(item.get("occurred_at") or activity.get("occurred_at") or item.get("created_at") or ""),
                        "source_ref": str(item.get("source_ref") or activity.get("source_ref") or "")[:512],
                        "completeness": str(item.get("completeness") or activity.get("completeness") or "unknown")[:64],
                    }
                )
            for source in sources:
                messages = by_source.get(source.source_key, [])
                messages.sort(key=lambda item: (self._codex_iso_seconds(item.get("timestamp")) or 0.0, item.get("message_id", "")))
                bounded_messages = messages[-MAX_CODEX_OVERVIEW_MESSAGES:]
                title_item = next((item for item in messages if item.get("role") == "user"), None)
                if title_item is None:
                    title_item = messages[0] if messages else None
                title_text = str(title_item.get("text") or "").strip() if title_item else ""
                title = self._codex_fallback_title(title_text) if title_text else "标题尚未取得"
                title_source = "首条可见用户消息" if title_item and title_item.get("role") == "user" else (
                    "首条可见助手消息" if title_item else "尚无可见消息"
                )
                if source.session_id in titles:
                    title = titles[source.session_id]
                    title_source = "Codex 会话标题"
                identity = self._codex_task_identity(source)
                if source.session_id not in titles and identity.get("parent_session_id"):
                    parent_title = titles.get(identity["parent_session_id"], "主任务标题待获取")
                    task_name = identity.get("task_name") or identity.get("agent_name") or "协作"
                    title = parent_title + " · 子任务：" + task_name
                    title_source = "Codex 子任务身份"
                latest = bounded_messages[-1] if bounded_messages else None
                # A discovered source can be touched while it is indexed even
                # when no visible message has been consumed.  Only a real
                # user/assistant observation can make a session active.
                latest_at = latest.get("timestamp") if latest else None
                latest_seconds = self._codex_iso_seconds(latest_at)
                active = bool(latest and latest_seconds is not None and 0 <= now_seconds - latest_seconds <= CODEX_ACTIVE_WINDOW_SECONDS)
                sessions.append(
                    {
                        "session_id": source.session_id,
                        "source_key": source.source_key,
                        "source_name": source.source_name,
                        "project_id": project.project_id,
                        "project_name": project_name,
                        **identity,
                        "project_status": project.status,
                        "source_status": source.status,
                        "binding_kind": source.binding_kind,
                        "classification": "confirmed" if source.binding_kind in {"explicit_session", "classified_session"} or project.source != MACHINE_WORKSPACE_SOURCE else "unclassified",
                        "modified_at": source.modified_at,
                        "title": title,
                        "title_source": title_source,
                        "active": active,
                        "last_activity_at": latest_at,
                        "last_activity_role": latest.get("role") if latest else None,
                        "last_activity_text": latest.get("text") if latest else "",
                        "message_count": len(messages),
                        "history_scope": "recent_collected_window",
                        "messages": messages,
                        "last_error": dict(source.last_error) if source.last_error else None,
                        "updated_at": source.updated_at,
                    }
                )
        source_count = len(sessions)
        sessions = group_task_sources(sessions, MAX_CODEX_OVERVIEW_MESSAGES)
        sessions.sort(
            key=lambda item: (
                not bool(item.get("active")),
                -(self._codex_iso_seconds(item.get("last_activity_at")) or 0.0),
                str(item.get("title") or ""),
            )
        )
        visible = sessions[: int(limit)]
        return {
            "status": "success",
            "project_id": project_id,
            "include_archived": include_archived,
            "sessions": visible,
            "session_count": len(visible),
            "total_source_count": source_count,
            "sessions_truncated": len(sessions) > len(visible),
            "empty_count": sum(1 for item in visible if not item.get("message_count")),
            "active_count": sum(1 for item in visible if item.get("active")),
            "inactive_count": sum(1 for item in visible if not item.get("active")),
            "history_limit": MAX_CODEX_OVERVIEW_MESSAGES,
            "active_window_seconds": CODEX_ACTIVE_WINDOW_SECONDS,
            "title_policy": "优先 Codex 会话标题；未命中时使用已采集消息摘要并标明来源",
            "privacy": "仅 user/assistant 可见消息；未返回隐藏推理、工具载荷、凭据和绝对路径",
        }

    def _sync_source(self, source_key: str) -> dict[str, Any]:
        # Serialize binding/disable with the complete consumption transaction.
        with self._lock:
            return self._sync_source_locked(source_key)

    def _sync_source_locked(self, source_key: str) -> dict[str, Any]:
        source = self.product_registry.source(source_key)
        project = self._require_project(source.project_id)
        if source.status == "disabled":
            return {"status": "success", "skipped": "source_disabled", "processed_count": 0}
        source_path = Path(source.source_path).resolve()
        if not source_path.is_file():
            failure = {
                "status": "failed",
                "original_code": "codex_source_file_not_found",
                "source_key": source.source_key,
                "source_name": source.source_name,
                "retryable": True,
                "next_action": "等待 Codex 恢复会话文件，或在项目来源页停用这条来源。",
                "processed_count": 0,
            }
            self.product_registry.update_source(
                source.source_key,
                status="unavailable",
                last_error=failure,
                polled=True,
            )
            return failure
        with self._lock:
            def verify_binding() -> None:
                if source.session_id is None:
                    return
                observed = self._session_meta_for_source(source_path)
                if observed.session_id != source.session_id or Path(observed.cwd).resolve() != Path(source.session_cwd).resolve():
                    raise ContractError("codex_binding_identity_changed")
            verify_binding()
            batch = CodexJsonlReceptor(source_path).sample(cursor=source.cursor)
            verify_binding()
            processed: list[dict[str, Any]] = []
            replayed_count = 0
            for occurrence in batch.occurrences:
                activity = occurrence.as_activity(project.project_id)
                request_id = f"codex-sync-{occurrence.occurrence_id}"
                view, replayed = self.run_project_activity(request_id, activity.to_dict())
                replayed_count += int(replayed)
                processed.append(
                    {
                        "request_id": request_id,
                        "episode_id": view.get("episode_id"),
                        "role": occurrence.role,
                        "summary": activity.summary,
                        "selected_action": view.get("selected_action"),
                        "replayed": replayed,
                    }
                )
            batch_view = batch.to_dict()
            batch_view["source_path"] = source.source_name
            stat = source_path.stat()
            updated = self.product_registry.update_source(
                source.source_key,
                cursor=batch.commit_offset,
                source_size=batch.source_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                status="ready" if batch.completeness == "complete" else "partial",
                last_batch=batch_view,
                polled=True,
            )
            return {
                "status": "success" if batch.completeness == "complete" else "partial",
                "project_id": project.project_id,
                "source": updated.to_dict(),
                "batch": batch_view,
                "processed": processed,
                "processed_count": len(processed),
                "replayed_count": replayed_count,
                "cursor_committed": batch.commit_offset,
                "formal_knowledge_write": False,
            }

    def _sync_codex_activity_for_project(
        self,
        selected: ProjectRecord,
        *,
        discovery_already_done: bool = False,
    ) -> dict[str, Any]:
        """Sync one project without making monitor discovery scan twice.

        The public single-project method owns its normal discovery behavior.
        A monitor poll first performs one global discovery, then passes the
        completed-discovery fence here for each project.  This is only an
        administrative optimization: source identity, cursor commits and the
        AP episode path remain exactly the same.
        """

        if self.codex_sessions_root is not None and not discovery_already_done:
            self.discover_codex_sources()
        sources = self.product_registry.sources_for_poll(selected.project_id)
        if not sources:
            if not self.product_registry.source_count(selected.project_id):
                raise ContractError("codex_source_not_configured")
        results: list[dict[str, Any]] = []
        processed_count = 0
        replayed_count = 0
        for source in sources:
            try:
                result = self._sync_source(source.source_key)
            except Exception as exc:
                result = {
                    "status": "failed", "original_code": str(exc)[:240],
                    "source_key": source.source_key, "processed_count": 0,
                    "retryable": True, "next_action": "Retry after restoring the source identity or disable this source.",
                }
                self.product_registry.update_source(source.source_key, status="failed", last_error=result, polled=True)
            results.append(result)
            processed_count += int(result.get("processed_count", 0) or 0)
            replayed_count += int(result.get("replayed_count", 0) or 0)
        failed = sum(1 for result in results if result.get("status") == "failed")
        primary_result = results[0] if results else {}
        return {
            "status": "partial" if failed or any(result.get("status") == "partial" for result in results) else "success",
            "project_id": selected.project_id,
            "sources": results,
            "source_count": len(results),
            "source_count_total": self.product_registry.source_count(selected.project_id, enabled_only=True),
            "processed": [item for result in results for item in result.get("processed", [])],
            "processed_count": processed_count,
            "replayed_count": replayed_count,
            "empty_poll": processed_count == 0,
            "formal_knowledge_write": False,
            # Legacy single-source clients consume these top-level fields.
            # They are a projection of the first source only; multi-source
            # callers must use ``sources`` and its per-source cursors.
            "batch": primary_result.get("batch"),
            "cursor_committed": primary_result.get("cursor_committed"),
        }

    def sync_codex_activity(self, project_id: str | None = None) -> dict[str, Any]:
        """Sync every registered source for one project, with independent failure."""

        selected = self._require_project(project_id or self.codex_project_id)
        return self._sync_codex_activity_for_project(selected)

    def poll_codex_sources(self) -> dict[str, Any]:
        discovery = self.discover_codex_sources()
        project_results: list[dict[str, Any]] = []
        processed = 0
        for project in self.product_registry.list(include_archived=False):
            if not project.auto_monitor_enabled:
                continue
            sources = self.product_registry.sources(project.project_id)
            if not sources:
                continue
            try:
                # Discovery has already run once for this monitor cycle.  Do
                # not repeat the same bounded filesystem scan for every
                # project; the per-project helper still owns all source,
                # episode and cursor semantics.
                result = self._sync_codex_activity_for_project(
                    project,
                    discovery_already_done=True,
                )
            except Exception as exc:
                result = {
                    "status": "failed",
                    "project_id": project.project_id,
                    "original_code": str(exc)[:240],
                    "retryable": True,
                    "processed_count": 0,
                }
            project_results.append(result)
            processed += int(result.get("processed_count", 0) or 0)
        # A poll can make useful progress while still leaving an unassigned or
        # malformed source.  Surface that as partial rather than claiming the
        # whole environment is healthy; the detailed discovery/project/source
        # receipts remain available for an actionable repair.
        # Unregistered cwd values are outside the selected projects. Preserve
        # them as pending assignments without making healthy consumers fail.
        discovery_partial = (
            not discovery.get("configured")
            or discovery.get("report", {}).get("completeness") != "complete"
            or any(item.get("reason") not in {
                "session_cwd_not_registered",
                "codex_workspace_monitor_disabled",
                # A closed/removed historical workspace is a visible pending
                # assignment, but it must not make healthy active projects
                # appear degraded on every poll.
                "codex_workspace_cwd_not_found",
            }
                   for item in discovery.get("unassigned", ()))
        )
        project_partial = any(
            str(result.get("status")) != "success"
            or any(
                str(source.get("status")) != "success"
                for source in result.get("sources", ())
                if isinstance(source, Mapping)
            )
            for result in project_results
        )
        return {
            "status": "partial" if discovery_partial or project_partial else "success",
            "discovery": discovery,
            "pending_assignment_count": len(discovery.get("unassigned", ())),
            "projects": project_results,
            "processed_count": processed,
            "empty_poll": processed == 0,
            "llm_calls_from_empty_poll": 0,
        }

    def project_data(self, project_id: str | None = None) -> dict[str, Any]:
        selected = self._require_project(project_id or self.codex_project_id, active=False)
        recovery = self.local_recovery(selected.project_id)
        memory_states = self.product_registry.memory_states(selected.project_id)
        memories = list(self.learning_ledger.memory_observations(selected.project_id, limit=128))
        for item in memories:
            disposition = memory_states.get(str(item.get("activity_id")))
            item["disposition"] = disposition or {
                "state": "active",
                "cognitive_effect": "eligible_for_future_b_recall;_history_is_retained",
            }
        snapshot = self.learning_ledger.snapshot(selected.project_id)
        imported = self.product_registry.import_draft_for_project(selected.project_id) if selected.source == "portable_import" else None
        imported_payload = (imported or {}).get("bundle", {}).get("payload", {})
        return {
            "status": "success",
            "project": selected.to_dict(),
            "knowledge": recovery,
            "documents": self.task_context.documents.read(selected.project_id),
            "memories": memories,
            "memory_count": len(memories),
            "learning": redact_portable(snapshot),
            "sources": [source.to_dict() for source in self.product_registry.sources(selected.project_id)],
            "imported_history": {"memories": imported_payload.get("memories", []),
                                 "learning": imported_payload.get("learning_snapshot", {}),
                                 "source_project_id": (imported or {}).get("source_project_id"),
                                 "read_only": True} if imported else None,
            "limitations": [
                "memory_disposition_changes_future_b_recall_eligibility; historical_observations_are_retained",
                "imported_curricula_are_historical_and_not_installed_as_active_rules",
            ],
        }

    def set_memory_disposition(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ContractError("memory_disposition_required")
        project_id = raw.get("project_id")
        if not isinstance(project_id, str):
            raise ContractError("memory_disposition_project_required")
        project = self._require_project(project_id)
        project_id = project.project_id
        result, replayed = self.product_registry.set_memory_disposition(
            request_id=str(raw.get("request_id") or ""),
            project_id=project_id,
            activity_id=str(raw.get("activity_id") or ""),
            state=str(raw.get("state") or ""),
            reason=str(raw.get("reason") or ""),
            source_ref=str(raw.get("source_ref") or "user://ap-vibe/memory-disposition"),
        )
        return {
            "status": "success",
            "replayed": replayed,
            "disposition": result,
            "cognitive_effect": "applies_to_future_b_recall_only;_history_is_retained",
        }

    def export_project(self, project_id: str | None = None) -> dict[str, Any]:
        selected = self._require_project(project_id or self.codex_project_id, active=False)
        data = self.project_data(selected.project_id)
        memories = []
        for item in data["memories"][:128]:
            activity = item.get("activity") if isinstance(item.get("activity"), Mapping) else {}
            memories.append(
                {
                    "activity_id": item.get("activity_id"),
                    "occurred_at": item.get("occurred_at"),
                    "summary": item.get("summary"),
                    "completeness": item.get("completeness"),
                    "source_ref": item.get("source_ref"),
                    "privacy_scope": item.get("privacy_scope"),
                    "disposition": item.get("disposition", {}).get("state", "active") if isinstance(item.get("disposition"), Mapping) else "active",
                    "activity": redact_portable(activity),
                }
            )
        imported_history = data.get("imported_history") or {}
        seen = {item.get("activity_id") for item in memories}
        for item in imported_history.get("memories", []):
            if item.get("activity_id") not in seen and len(memories) < 128:
                memories.append(redact_portable(item))
                seen.add(item.get("activity_id"))
        recovery = data["knowledge"]
        # Export the current structured dossier as an immutable snapshot. The
        # regular read endpoint intentionally returns a catalog by default;
        # portable transfer needs the actual chapters to remain useful offline.
        dossier = self.task_context.documents.latest(selected.project_id)
        milestone = recovery.get("milestone") if isinstance(recovery.get("milestone"), Mapping) else None
        payload = {
            "knowledge": {
                "revision": redact_portable(milestone) if milestone else None,
                "source_refs": list(milestone.get("source_refs", ())) if milestone else [],
            },
            "documents": redact_portable(dossier) if dossier else None,
            "memories": memories,
            "learning_snapshot": redact_portable(data["learning"]),
            "memory_dispositions": [
                redact_portable(item) for item in self.product_registry.memory_states(selected.project_id).values()
            ],
            "settings": {
                "privacy_scope": selected.privacy_scope,
                "auto_monitor_enabled": selected.auto_monitor_enabled,
            },
        }
        unsigned = {
            "protocol": PORTABLE_PROJECT_PROTOCOL,
            "product_schema_version": PRODUCT_SCHEMA_VERSION,
            "whitepaper_sha256": HYBRID_WHITEPAPER_SHA256,
            "project": {
                "project_id": selected.project_id,
                "display_name": selected.display_name,
                "root_fingerprint": selected.root_fingerprint,
            },
            "manifest": {
                "memory_count": len(memories),
                "has_reviewed_knowledge": bool(milestone),
                "document_chapter_count": len((dossier or {}).get("sections", {})),
                "contains_absolute_paths": False,
                "contains_secrets": False,
            },
            "payload": payload,
            "limitations": [
                "portable_pack_excludes_api_keys_codex_paths_episode_sqlite_hidden_reasoning_and_vibe_tokens",
                "imported_learning_is_historical_not_active",
                "current_dossier_snapshot_only_not_all_document_revisions_or_project_files",
            ],
            "redactions": ["credentials", "absolute_codex_source_paths", "runtime_episode_databases"],
        }
        # Outer memory summaries/source refs are separate from activity. Apply
        # redaction to the complete payload, not only the nested activity.
        unsigned = redact_portable(unsigned)
        bundle = {
            **unsigned,
            "exported_at": _now(),
            "content_hash": portable_content_hash(unsigned),
        }
        validate_portable_bundle(bundle)
        return {
            "status": "success",
            "project_id": selected.project_id,
            "bundle": bundle,
            "content_hash": bundle["content_hash"],
            "byte_count": len(_json(bundle).encode("utf-8")),
            "vibe_formal_write": False,
        }

    def preview_project_import(self, request_id: str, raw_bundle: Mapping[str, Any]) -> dict[str, Any]:
        validation = validate_portable_bundle(raw_bundle)
        source_project = raw_bundle.get("project") if isinstance(raw_bundle.get("project"), Mapping) else {}
        source_id = str(source_project.get("project_id") or "unknown")[:256]
        source_name = str(source_project.get("display_name") or "导入项目")[:160]
        target_id = str(raw_bundle.get("target_project_id") or "import-" + hashlib.sha256(str(request_id).encode()).hexdigest()[:16])
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", target_id):
            raise ContractError("portable_target_project_id_invalid")
        target_name = str(raw_bundle.get("target_display_name") or f"{source_name}（导入）")[:160]
        target_root = raw_bundle.get("target_root")
        if not isinstance(target_root, str) or not target_root.strip():
            raise ContractError("portable_target_root_required")
        preview = {
            "protocol": raw_bundle.get("protocol"),
            "source_project_id": source_id,
            "source_display_name": source_name,
            "target_project_id": target_id,
            "target_display_name": target_name,
            "memory_count": validation["memory_count"],
            "curriculum_count": validation["curriculum_count"],
            "documents": validation["documents"],
            "content_hash": validation["computed_hash"],
            "unknown_fields": validation["unknown_fields"],
            "will_create_new_project": True,
            "will_not_overwrite_existing_project": True,
            "will_not_install_active_curricula": True,
            "write_state": "preview_only",
            "limitations": list(raw_bundle.get("limitations", ()))[:16] if isinstance(raw_bundle.get("limitations"), Sequence) and not isinstance(raw_bundle.get("limitations"), (str, bytes)) else [],
        }
        draft, replayed = self.product_registry.save_import_draft(
            request_id=request_id,
            bundle=raw_bundle,
            preview=preview,
            target_project_id=target_id,
            target_display_name=target_name,
            target_root=target_root,
        )
        return {"status": "success", "replayed": replayed, "draft": {"draft_id": draft["draft_id"], **preview}}

    def confirm_project_import(self, request_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ContractError("portable_import_confirmation_required")
        allowed = {"draft_id", "bundle_hash"}
        if set(raw) - allowed:
            raise ContractError("portable_import_confirmation_identity_only")
        draft_id = raw.get("draft_id")
        if not isinstance(draft_id, str) or not draft_id.strip():
            raise ContractError("portable_import_draft_id_required")
        advertised_hash = raw.get("bundle_hash")
        if not isinstance(advertised_hash, str) or not advertised_hash.strip():
            raise ContractError("portable_import_bundle_hash_required")
        request_id = self._request_id(request_id)
        draft = self.product_registry.import_draft(draft_id.strip())
        bundle_hash = str(draft.get("bundle_hash") or "")
        if advertised_hash.strip() != bundle_hash:
            raise ContractError("portable_import_bundle_hash_conflict")
        # Confirmation identity is persisted before any project, knowledge or
        # learning store side effect.  A retry of the same identity resumes
        # the durable stage; a changed body cannot attach itself to the draft.
        fingerprint = self._vibe_fingerprint(
            "portable_import_confirm",
            request_id,
            {"draft_id": draft_id.strip(), "bundle_hash": bundle_hash},
        )
        with self._lock:
            draft, binding_replayed = self.product_registry.bind_import_confirmation(
                draft_id.strip(),
                request_id=request_id,
                fingerprint=fingerprint,
                bundle_hash=bundle_hash,
            )
            if draft.get("status") == "confirmed" and draft.get("confirmed_project_id"):
                project = self.product_registry.get(str(draft["confirmed_project_id"]))
                return {
                    "status": "success",
                    "replayed": True,
                    "project_id": project.project_id,
                    "project": project.to_dict(),
                    "bundle_hash": bundle_hash,
                    "learning_status": "historical_not_installed",
                    "draft": draft,
                    "vibe_formal_write": False,
                }

            target_id = str(draft["target_project_id"])
            target_root = str(draft["target_root"])
            target_name = str(draft["target_display_name"])
            stage = str(draft.get("completed_stage") or "importing")
            try:
                # A previous attempt may have registered the isolated target
                # before failing in a later store.  Reuse it only when its
                # durable import marker exactly matches this draft.
                try:
                    existing = self.product_registry.get(target_id, include_archived=True)
                except ContractError as exc:
                    if str(exc) != "project_not_found":
                        raise
                    existing = None
                if existing is None:
                    project, _ = self.product_registry.register(
                        project_id=target_id,
                        display_name=target_name,
                        root_path=target_root,
                        logic_root=target_root,
                        auto_monitor_enabled=(draft["bundle"].get("payload", {}).get("settings", {}).get("auto_monitor_enabled") is True),
                        source="portable_import",
                        extra={
                            "import_bundle_hash": bundle_hash,
                            "imported_from_project_id": draft["source_project_id"],
                        },
                    )
                else:
                    marker = existing.extra.get("import_bundle_hash") if isinstance(existing.extra, Mapping) else None
                    if (
                        existing.status != "active"
                        or existing.source != "portable_import"
                        or str(marker or "") != bundle_hash
                    ):
                        raise ContractError("portable_import_target_project_exists")
                    project = existing

                bundle = draft["bundle"]
                # The persisted draft was validated at preview time.  Recheck
                # the exact stored payload before resuming after a crash.
                validation = validate_portable_bundle(bundle)
                if validation["computed_hash"] != bundle_hash:
                    raise ContractError("portable_bundle_hash_mismatch")
                payload = bundle.get("payload") if isinstance(bundle.get("payload"), Mapping) else {}
                knowledge = payload.get("knowledge") if isinstance(payload.get("knowledge"), Mapping) else {}
                revision = knowledge.get("revision") if isinstance(knowledge.get("revision"), Mapping) else None
                dossier = payload.get("documents") if isinstance(payload.get("documents"), Mapping) else None

                if stage not in {"knowledge_imported", "learning_imported", "confirmed"}:
                    imported_revision = None
                    if revision and isinstance(revision.get("sections"), Mapping):
                        imported_revision = self.knowledge_store.import_portable_revision(
                            project_id=project.project_id,
                            source_project_id=str(draft["source_project_id"]),
                            bundle_hash=bundle_hash,
                            sections=revision["sections"],
                            source_refs=revision.get("source_refs", ()),
                            evidence_refs=revision.get("evidence_refs", ()),
                        )
                    draft = self.product_registry.mark_import_stage(
                        draft_id.strip(),
                        "knowledge_imported",
                        project_id=project.project_id,
                        revision_id=imported_revision.revision_id if imported_revision is not None else None,
                    )
                    stage = "knowledge_imported"

                # Keep the user-facing 11-chapter dossier alongside the
                # historical recovery import. Old bundles simply skip this
                # step and remain fully compatible.
                if dossier:
                    self.task_context.documents.import_snapshot(project.project_id, dossier, bundle_hash)

                learning = payload.get("learning_snapshot") if isinstance(payload.get("learning_snapshot"), Mapping) else {}
                if stage not in {"learning_imported", "confirmed"}:
                    self.product_registry.save_imported_learning_snapshot(
                        project.project_id,
                        bundle_hash,
                        learning,
                    )
                    learning_hash = hashlib.sha256(
                        _json(redact_portable(learning)).encode("utf-8")
                    ).hexdigest().upper()
                    draft = self.product_registry.mark_import_stage(
                        draft_id.strip(),
                        "learning_imported",
                        project_id=project.project_id,
                        learning_snapshot_hash=learning_hash,
                    )
                    stage = "learning_imported"

                if stage != "confirmed":
                    draft = self.product_registry.mark_import_confirmed(
                        draft_id.strip(),
                        project.project_id,
                        bundle_hash=bundle_hash,
                        request_id=request_id,
                        fingerprint=fingerprint,
                    )
                else:
                    draft = self.product_registry.import_draft(draft_id.strip())
            except Exception as exc:
                code = str(exc) if isinstance(exc, ContractError) else "portable_import_failed"
                retryable = code not in {
                    "portable_import_target_project_exists",
                    "portable_import_already_rolled_back",
                    "portable_import_confirmation_identity_conflict",
                    "portable_import_bundle_hash_conflict",
                }
                solution = (
                    "为导入选择尚未注册的新 project_id 和空目录后重新预览。"
                    if code == "portable_import_target_project_exists"
                    else "保留同一个 draft_id 和 bundle_hash 重试；系统会从最后成功阶段继续。"
                )
                try:
                    self.product_registry.mark_import_failed(
                        draft_id.strip(),
                        code=code,
                        message=str(exc)[:1024] or code,
                        retryable=retryable,
                        solution=solution,
                        stage=str(draft.get("completed_stage") or stage),
                    )
                except Exception:
                    # Preserve the original causal error if recording the
                    # failure receipt itself is unavailable.
                    pass
                raise

            return {
                "status": "success",
                "replayed": bool(binding_replayed),
                "project_id": project.project_id,
                "project": project.to_dict(),
                "bundle_hash": bundle_hash,
                "learning_status": "historical_not_installed",
                "draft": draft,
                "vibe_formal_write": False,
            }

    def rollback_project_import(self, project_id: str) -> dict[str, Any]:
        record = self._require_project(project_id, active=False)
        if record.source != "portable_import":
            raise ContractError("portable_import_rollback_only")
        draft = self.product_registry.import_draft_for_project(record.project_id)
        if draft is None:
            raise ContractError("portable_import_not_confirmed")
        if draft.get("status") == "rolled_back" and record.status == "archived":
            return {
                "status": "success",
                "project": record.to_dict(),
                "rollback": "project_archived_reversible",
                "data_deleted": False,
                "draft": draft,
                "replayed": True,
            }
        if draft.get("status") != "confirmed":
            raise ContractError("portable_import_not_confirmed")
        archived = self.product_registry.set_status(record.project_id, "archived")
        rolled_back = self.product_registry.mark_import_rolled_back(
            str(draft["draft_id"]),
            record.project_id,
        )
        return {
            "status": "success",
            "project": archived.to_dict(),
            "rollback": "project_archived_reversible",
            "data_deleted": False,
            "draft": rolled_back,
            "replayed": False,
        }

    def health(self) -> StudioServiceHealth:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute("SELECT COUNT(*) AS count FROM studio_requests").fetchone()
                project_row = connection.execute("SELECT COUNT(*) AS count FROM vibe_requests").fetchone()
                count = int(row["count"]) if row is not None else 0
                project_count = int(project_row["count"]) if project_row is not None else 0
            registry_ready = True
        except sqlite3.Error:
            count = 0
            project_count = 0
            registry_ready = False
        try:
            self.learning_ledger.snapshot("__health__")
            learning_ready = True
        except sqlite3.Error:
            learning_ready = False
        try:
            knowledge_ready, _, _ = self.knowledge_store.validate(self.codex_project_id)
            knowledge_revision_count = self.knowledge_store.count(self.codex_project_id)
        except sqlite3.Error:
            knowledge_ready = False
            knowledge_revision_count = 0
        studio_built = bool(self.studio_dir and (self.studio_dir / "index.html").is_file())
        # Health must describe both supported source modes.  The older fixed
        # ``codex_source`` check made a daemon with bounded automatic session
        # discovery look unconfigured even while its monitor was processing
        # real sources.  Reading the existing projection does not scan the
        # filesystem or advance a cursor.
        codex_sampler = self.codex_sampler_state()
        return StudioServiceHealth(
            status="ok" if registry_ready else "partial",
            registry_ready=registry_ready,
            studio_built=studio_built,
            request_count=count,
            project_request_count=project_count,
            learning_ready=learning_ready,
            knowledge_ready=knowledge_ready,
            knowledge_revision_count=knowledge_revision_count,
            codex_sampler=codex_sampler,
            provider=self.provider_state(),
            logic_field=(
                self.logic_field.health()
                if self.logic_field is not None
                else {
                    "configured": False,
                    "languages": [],
                    "root_scope": None,
                    "static_only": True,
                    "runtime_truth": False,
                    "subjective_closure": "not_assessed",
                }
            ),
        )

    def install_desktop_launcher(self) -> dict[str, Any]:
        """Create the user-scoped Windows shortcut through the lifecycle script.

        The daemon is already the owner of the current request, so only a
        healthy instance may perform this operation.  The script remains the
        single authority for process identity, receipt and port handling; the
        server passes a fixed argument list and never evaluates shell text.
        """

        if os.name != "nt":
            raise ContractError("desktop_launcher_windows_only")
        health = self.health().to_dict()
        if health.get("status") != "ok":
            raise ContractError("desktop_launcher_service_unhealthy")
        appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        config_path = (appdata / "AP-Vibe" / "config.json").resolve()
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("desktop_launcher_config_missing") from exc
        if not isinstance(config, Mapping) or config.get("product") != "AP-Vibe":
            raise ContractError("desktop_launcher_config_invalid")
        script_value = config.get("script_path")
        script_path = Path(str(script_value)).resolve() if isinstance(script_value, str) and script_value.strip() else Path(__file__).resolve().parents[2] / "scripts" / "ap-vibe.ps1"
        if not script_path.is_file() or script_path.name.casefold() != "ap-vibe.ps1":
            raise ContractError("desktop_launcher_script_missing")
        powershell = next((shutil.which(candidate) for candidate in ("pwsh.exe", "powershell.exe", "pwsh", "powershell") if shutil.which(candidate)), None)
        if not powershell:
            raise ContractError("desktop_launcher_powershell_missing")
        config_dir = Path(str(config.get("config_dir") or config_path.parent)).resolve()
        args = [
            str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path),
            "-Action", "install-desktop-launcher", "-ConfigDir", str(config_dir), "-SkipOpen",
        ]
        try:
            completed = subprocess.run(
                args,
                cwd=str(script_path.parent.parent),
                capture_output=True,
                text=True,
                timeout=45,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContractError("desktop_launcher_process_failed") from exc
        output = (completed.stdout or "").strip()
        payload: dict[str, Any] | None = None
        if output:
            try:
                payload = json.loads(output)
            except json.JSONDecodeError:
                payload = None
        if completed.returncode != 0 or not isinstance(payload, Mapping) or not payload.get("ok"):
            code = str(payload.get("code")) if isinstance(payload, Mapping) and payload.get("code") else "desktop_launcher_install_failed"
            known = {
                "desktop_launcher_identity_mismatch", "desktop_launcher_readback_failed", "desktop_launcher_unavailable",
                "service_not_healthy", "not_installed", "port_in_use", "config_identity_conflict",
            }
            raise ContractError(code if code in known else "desktop_launcher_install_failed")
        current_url = str(payload.get("current_url") or payload.get("url") or "")
        return {
            "ok": True,
            "status": str(payload.get("status") or "desktop_launcher_installed"),
            "shortcut_path": payload.get("shortcut_path"),
            "current_url": current_url,
            "url": current_url,
            "created": bool(payload.get("created")),
            "updated": bool(payload.get("updated")),
            "service_started": bool(payload.get("service_started")),
            "icon_path": payload.get("icon_path"),
            "readback": payload.get("readback"),
        }


def gateway_from_environment() -> tuple[
    HybridGateway,
    GovernanceCompatibilityRecord | None,
    CapabilityOwnership | None,
    KnowledgeCorrectionInterpreter,
]:
    """Assemble the optional teacher from process-only configuration."""

    base_url = os.environ.get("AP_VIBE_LLM_BASE_URL", "").strip()
    api_key = os.environ.get("AP_VIBE_LLM_API_KEY", "").strip()
    model = os.environ.get("AP_VIBE_LLM_MODEL", "").strip()
    configured = (bool(base_url), bool(api_key), bool(model))
    if any(configured) and not all(configured):
        raise ContractError("ap_vibe_provider_configuration_incomplete")
    if not all(configured):
        return NullGateway(), None, None, NullKnowledgeCorrectionInterpreter()
    provider = os.environ.get("AP_VIBE_LLM_PROVIDER", "yinzi-openai-compatible").strip() or "yinzi-openai-compatible"
    gateway = OpenAICompatibleGateway(
        base_url,
        api_key,
        model,
        provider=provider,
        advisor_role="joint_cognition_teacher",
        timeout=float(os.environ.get("AP_VIBE_LLM_TIMEOUT_SECONDS", "60")),
        max_retries=0,
        max_prompt_chars=int(os.environ.get("AP_VIBE_LLM_MAX_PROMPT_CHARS", "48000")),
        max_output_tokens=int(os.environ.get("AP_VIBE_LLM_MAX_OUTPUT_TOKENS", "1200")),
    )
    governance = GovernanceCompatibilityRecord(
        record_id="ap-vibe-hybrid-b3",
        project_id="ap-vibe-local",
        whitepaper_sha256=HYBRID_WHITEPAPER_SHA256,
        compatibility="compatible",
        llm_delegation_enabled=True,
        authority_sources={
            "whitepaper": HYBRID_WHITEPAPER_SHA256,
            "runtime_contract": OpenAICompatibleGateway.PROMPT_TEMPLATE_VERSION,
            "user_authorization": "ap-vibe-dedicated-thread-2026-09-03",
        },
        next_action="keep reality, execution, formal knowledge and final learning attribution outside provider authority",
    )
    capability = CapabilityOwnership(
        capability_key="hybrid.cognition",
        stage="assisted",
        decision_owner="ap_native",
        content_owner="mixed",
        evidence_owner="environment",
        execution_owner="environment",
        scope=("environment:ap-vibe", "low_risk_existing_action_candidates"),
        llm_allowed=True,
        reason="cold-start teacher may bias existing low-risk candidates in the single AP arena",
        version="0.1.0-b3",
    )
    correction_interpreter = OpenAICompatibleKnowledgeCorrectionInterpreter(
        base_url,
        api_key,
        model,
        provider=provider,
        timeout=float(os.environ.get("AP_VIBE_LLM_TIMEOUT_SECONDS", "60")),
        max_prompt_chars=int(os.environ.get("AP_VIBE_LLM_MAX_PROMPT_CHARS", "48000")),
        max_output_tokens=max(512, int(os.environ.get("AP_VIBE_LLM_MAX_OUTPUT_TOKENS", "2400"))),
    )
    return gateway, governance, capability, correction_interpreter


def _error_payload(
    *,
    code: str,
    message: str,
    retryable: bool,
    solution: str,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "solution": solution,
        },
    }


class StudioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler_class, *, service: StudioEpisodeService):
        super().__init__(server_address, handler_class)
        self.service = service
        self.service.client_endpoint = {'host': server_address[0], 'port': self.server_address[1]}
        self.projections = ReadProjectionCache(os.environ.get("AP_VIBE_UI_CACHE_SECONDS", "2"))
        self._wake_scan = None
        self._wake_at = 0.0

    def service_actions(self):
        # Also covers a crash between a terminal commit and its wake event.
        # Keep database scans off the HTTP accept loop.
        now = time.monotonic()
        if now - self._wake_at >= 2 and (self._wake_scan is None or not self._wake_scan.is_alive()):
            self._wake_at = now
            self._wake_scan = threading.Thread(target=self.service.agent_studio.tick, daemon=True)
            self._wake_scan.start()

    def server_close(self) -> None:
        """Close the socket and stop this service's monitor exactly once."""

        try:
            self.service.close()
        finally:
            super().server_close()


class StudioRequestHandler(BaseHTTPRequestHandler):
    server_version = "APMindStudio/0.1"

    @property
    def service(self) -> StudioEpisodeService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        # Keep stdout usable for the one-line startup receipt. HTTP access logs
        # are intentionally quiet in the local alpha.
        del format, args

    def _write_json(self, status: int, payload: Mapping[str, Any], **headers: str) -> None:
        if self.command == "POST" and 200 <= status < 300:
            self.server.projections.invalidate()
        raw = _json(dict(payload)).encode("utf-8")
        raw, compressed = json_transfer(raw, self.headers.get("Accept-Encoding", ""))
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Vary", "Accept-Encoding")
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for name, value in headers.items():
                self.send_header(name.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The caller may time out while a bounded AP or knowledge
            # operation is finishing.  The durable request/episode remains
            # available for the same request_id, so a disconnected socket is
            # a normal transport condition rather than a server traceback.
            return

    def _read_json(self) -> Mapping[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ContractError("request_content_length_required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ContractError("request_content_length_invalid") from exc
        # A browser upload transports the original JSON as a string to avoid
        # lossy numeric reserialization. Quotes/backslashes can double its
        # envelope size; the parsed bundle still has the normal 2 MB limit.
        body_limit = 2 * MAX_REQUEST_BYTES if urlsplit(self.path).path == "/v1/ap-vibe/portable/import/preview" else MAX_REQUEST_BYTES
        if length < 1 or length > body_limit:
            raise ContractError("request_body_size_out_of_bounds")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("request_body_must_be_utf8_json") from exc
        if not isinstance(value, Mapping):
            raise ContractError("request_body_must_be_object")
        return value

    @staticmethod
    def _request_from_json(raw: Mapping[str, Any]) -> StudioEpisodeRequest:
        max_ticks = raw.get("max_ticks", MAX_STUDIO_TICKS)
        renderer_enabled = raw.get("renderer_enabled", True)
        annotation_enabled = raw.get("annotation_enabled", True)
        if not isinstance(renderer_enabled, bool) or not isinstance(annotation_enabled, bool):
            raise ContractError("studio_feature_flags_must_be_boolean")
        surface = raw.get("surface_text")
        if surface is not None and not isinstance(surface, str):
            raise ContractError("studio_surface_text_invalid")
        return StudioEpisodeRequest(
            request_id=str(raw.get("request_id", "")),
            proposition_text=str(raw.get("proposition_text", "")),
            surface_text=surface,
            renderer_enabled=renderer_enabled,
            annotation_enabled=annotation_enabled,
            max_ticks=max_ticks,
        )

    @staticmethod
    def _vibe_envelope(raw: Mapping[str, Any], field: str) -> tuple[str, Mapping[str, Any]]:
        request_id = raw.get("request_id")
        payload = raw.get(field)
        if not isinstance(request_id, str) or not request_id.strip():
            raise ContractError("request_id_required")
        if not isinstance(payload, Mapping):
            raise ContractError(f"{field}_required")
        return request_id, payload

    @staticmethod
    def _query_value(split: Any, name: str) -> str | None:
        values = parse_qs(split.query, keep_blank_values=True).get(name, [])
        if len(values) > 1:
            raise ContractError(f"query_parameter_{name}_repeated")
        if not values or not str(values[0]).strip():
            return None
        return str(values[0]).strip()

    @classmethod
    def _query_project_id(cls, split: Any) -> str | None:
        return cls._query_value(split, "project_id")

    @classmethod
    def _query_limit(cls, split: Any, default: int) -> int:
        raw = cls._query_value(split, "limit")
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ContractError("query_limit_invalid") from exc
        return value

    @classmethod
    def _query_bool(cls, split: Any, name: str, default: bool) -> bool:
        raw = cls._query_value(split, name)
        if raw is None:
            return default
        if raw.casefold() in {"1", "true", "yes", "on"}:
            return True
        if raw.casefold() in {"0", "false", "no", "off"}:
            return False
        raise ContractError(f"query_{name}_invalid")

    def _write_contract_error(self, exc: ContractError) -> None:
        """Project a typed local error without erasing its causal code."""

        code = str(exc) or type(exc).__name__
        not_found = {
            "project_not_found",
            "codex_source_not_found",
            "portable_import_draft_not_found",
            "feedback_target_activity_not_found",
            "knowledge_proposal_not_found",
            "knowledge_correction_draft_not_found",
            "organization_task_not_found",
        }
        conflicts = {
            "project_root_already_registered",
            "project_id_already_registered",
            "portable_import_request_conflict",
            "portable_import_target_project_exists",
            "portable_import_confirmation_identity_conflict",
            "portable_import_bundle_hash_conflict",
            "portable_import_already_rolled_back",
            "portable_import_project_mismatch",
            "portable_import_not_confirmed",
            "portable_import_rollback_only",
            "request_id_conflict",
            "vibe_request_id_reused_with_different_input",
            "memory_disposition_request_conflict",
            "task_context_outcome_conflict",
        }
        messages = {
            "project_id_required": ("必须明确指定项目。", "刷新项目列表并选择一个项目后重试。"),
            "project_registration_required": ("项目注册信息不完整。", "填写项目名称和一个已存在的项目目录。"),
            "project_root_not_found": ("项目目录不存在或不可读取。", "选择一个真实且可读的目录后重试。"),
            "project_root_already_registered": ("这个目录已经绑定到另一个项目。", "改用原项目，或选择另一个目录和 project_id。"),
            "project_id_already_registered": ("这个 project_id 已经存在。", "使用项目列表中的现有项目，或换一个新的 project_id。"),
            "project_auto_monitor_must_be_boolean": ("自动监控开关必须是布尔值。", "使用 true 或 false 后重试。"),
            "default_project_cannot_be_archived": ("当前默认项目不能直接归档。", "先切换到另一个项目，再归档该项目。"),
            "archived_project_monitor_forbidden": ("已归档项目不能启动监控。", "先恢复项目，再开启自动监控。"),
            "codex_source_not_configured": ("当前项目还没有绑定 Codex 来源。", "配置 codex sessions 根目录或绑定一个固定来源后重试。"),
            "codex_sessions_root_not_configured": ("后台自动发现尚未配置。", "启动 daemon 时设置 --codex-sessions-root；手动来源仍可使用。"),
            "task_context_write_identity_required": ("只读上下文没有真实会话身份，不能代表任务写入。", "查询可以继续；需要更新档案或提交反馈时，从真实 Codex 任务重新 bootstrap 后重试。"),
            "memory_disposition_required": ("记忆归档请求不完整。", "提供 project_id、activity_id、state 和 reason。"),
            "memory_disposition_state_unsupported": ("记忆状态只支持 active 或 archived。", "选择归档或恢复，不会删除原始记录。"),
            "portable_target_root_required": ("导入目标目录不能为空。", "选择一个已存在且可读的空目录后重新预览。"),
            "portable_document_invalid": ("导出包里的项目档案格式不完整。", "请在原工作台重新导出。现有项目与档案保持不变。"),
            "portable_import_target_document_exists": ("这个导入目标已经有另一份档案。", "选择新的项目目录重新预览，原档案不会被覆盖。"),
            "portable_import_target_project_exists": ("导入目标项目已经存在且不是这份导入草稿。", "换一个新的 project_id 和空目录后重新预览。"),
            "portable_import_not_confirmed": ("只有已确认的 portable 导入项目才能回滚。", "先完成同一 draft 的确认，或选择正确的导入项目。"),
            "portable_import_already_rolled_back": ("这份 portable 导入已经回滚。", "项目数据仍保留；如需继续使用，请从 portable 包重新预览。"),
            "portable_import_confirmation_identity_conflict": ("确认身份与已保存的导入草稿不一致。", "保留原 request_id、draft_id 和 bundle_hash 重试，或重新预览。"),
            "portable_import_bundle_hash_conflict": ("导入包内容 hash 与预览草稿不一致。", "不要修改浏览器中的 bundle；重新导入原始 portable 包。"),
            "portable_import_draft_not_found": ("找不到这份 portable 导入草稿。", "重新上传 portable 包并先生成预览。"),
            "query_limit_invalid": ("查询数量必须是整数。", "删除 limit 或填写一个整数后重试。"),
            "agent_claude_not_found": ("尚未找到 Claude Code 执行器。", "安装 Claude Code 后重试；已保存的伙伴配置会保留。"),
            "agent_executor_invalid": ("请选择 Codex 或 Claude Code。", "重新打开伙伴配置并选择执行器。"),
            "agent_auth_mode_invalid": ("这个执行器不支持所选登录方式。", "Codex 可使用本机登录，Claude 伙伴请填写模型服务。"),
            "agent_protocol_not_implemented": ("连接格式与执行器不匹配。", "Codex 使用 Responses；Chat Completions 或 Anthropic 服务请选择 Claude Code。"),
            "agent_codex_max_turns_unsupported": ("本机 Codex 没有按轮数限制的执行参数。", "移除 max_turns 后启动；任务仍可停止并保留已有成果。"),
            "agent_api_key_required": ("请填写有效的 API Key。", "首次创建需要 Key；编辑已有配置时可留空保留。"),
            "agent_revision_conflict": ("这位伙伴的配置已被更新。", "重新读取配置后编辑，避免覆盖其他修改。"),
            "agent_url_invalid": ("服务地址格式不正确。", "填写不包含账号、密码或查询参数的 HTTP(S) 地址。"),
            "agent_remote_https_required": ("远端服务需要 HTTPS。", "本机测试可用 HTTP；远端连接请使用 HTTPS 地址。"),
            "agent_has_active_run": ("这位伙伴还有正在执行的任务。", "先处理该任务再归档；历史记录会保留。"),
            "agent_run_not_owned_or_finished": ("该任务已经结束，或不由当前服务进程控制。", "刷新查看最新状态；中断任务先核对成果，不重复提交。"),
            "agent_request_conflict": ("这个请求编号已经对应另一个任务。", "查看原任务；新目标需要新的请求编号。"),
            "agent_artifact_run_required": ("尚未选择要查看成果的任务。", "先选择一个任务。"),
            "agent_artifact_path_invalid": ("所选路径不属于这个任务的成果目录。", "从当前任务的成果列表重新选择文件。"),
            "agent_artifact_private_file": ("这是本机连接或凭据文件，不在成果预览中展示。", "查看任务产出的报告、代码或其它成果文件。"),
            "agent_artifact_not_found": ("文件目前不存在，可能尚未写入或已经移动。", "刷新成果列表并查看当前任务输出。"),
            "agent_artifact_unreadable": ("文件暂时无法读取，可能正在写入或被占用。", "稍后刷新，已显示的内容会保留。"),
            "agent_dependency_not_found": ("所选上游任务不存在。", "刷新任务列表后重新选择，上游需要先登记。"),
            "agent_dependency_project_mismatch": ("所选上游属于另一个项目。", "在当前项目中选择要衔接的任务，避免把不同项目的成果混在一起。"),
            "agent_handoff_target_unavailable": ("接手伙伴不存在或已归档。", "从可用伙伴中重新选择，原任务和成果继续保留。"),
            "agent_handoff_requires_failed_run": ("该任务还不需要失败交接。", "正常返回的任务可以安排伙伴核验；仍在运行的任务先查看进展。"),
            "collaboration_request_conflict": ("这条消息编号已经用于另一段内容。", "原消息已保留；新的内容请使用新的请求编号。"),
            "collaboration_handoff_owner_changed": ("任务已经交给另一位伙伴。", "先读取最新交接记录，再决定是否调整分工。"),
            "project_status_unsupported": ("项目状态不受支持。", "只使用 active 或 archived。"),
            "project_registration_unknown_fields": ("项目注册包含未知字段。", "只保留 project_id、display_name、root_path、logic_root 和 auto_monitor_enabled。"),
            "project_payload_required": ("项目请求体不能为空。", "选择项目后重新提交。"),
            "codex_sync_body_must_be_empty_or_project": ("Codex 同步请求字段不受支持。", "请求体留空，或只提供 project_id。"),
            "codex_discover_body_must_be_empty": ("来源发现不接受请求体。", "清空请求体后重试。"),
            "memory_disposition_request_conflict": ("同一个记忆处置请求已用于不同输入。", "保留原 request_id 重放；新处置请生成新的 request_id。"),
            "organization_no_candidates": ("当前范围内没有可整理的未归类会话。", "安装已经完成；以后有新活动时，可在“项目与记忆”页面重新发起整理。"),
            "organization_no_registered_projects": ("当前没有已登记的长期项目档案。", "先完成一次项目归类，或在项目页面新建并整理项目档案。"),
            "organization_project_selection_invalid": ("项目档案刷新范围不正确。", "只选择已登记项目，并为每个项目保留唯一 ID。"),
            "organization_project_not_registered": ("只能刷新已登记项目的档案。", "刷新项目目录后重新选择；自动发现的工作区需要先核对归类。"),
            "organization_task_request_conflict": ("这个整理任务编号已经用于另一份会话清单。", "保留原任务查看结果，或换一个新的 request_id 重新准备。"),
            "organization_task_already_running": ("已有一个整理任务正在运行。", "等待当前任务完成或失败后，再开始新的整理。"),
            "organization_codex_cli_missing": ("找不到本机 Codex 命令行。", "确认 Codex CLI 已安装并已登录；项目监看和基础模式仍可继续使用。"),
            "organization_result_invalid": ("Codex 返回的整理方案格式不完整。", "保留当前任务并重新发起整理；没有写回不完整方案。"),
            "organization_project_document_incomplete": ("整理方案缺少完整项目档案，尚未写回。", "方案已保留。每个长期项目需补齐 11 个章节；未知内容写明核对入口，再提交。"),
            "organization_refresh_result_invalid": ("档案刷新结果格式不完整。", "方案已保留；每个项目需要完整 11 章和十维评估。"),
            "organization_refresh_scope_conflict": ("档案刷新结果越过了本次冻结项目范围。", "没有写入任何项目；重新准备当前或全部已登记项目。"),
            "organization_project_identity_unverified": ("项目简介仍是自动识别或缺少核对依据。", "请让 Codex 读取真实代码、设计和任务证据后填写名称、用途和面向对象。"),
            "organization_project_assessment_incomplete": ("项目十维评估不完整。", "必须逐项列出十个维度；没有证据可以填 null，但要写理由、风险、改进和证据边界。"),
            "desktop_launcher_windows_only": ("桌面启动器只支持 Windows。", "在 Windows 本机运行 AP-Vibe 安装入口；当前浏览器页面仍可继续使用。"),
            "desktop_launcher_service_unhealthy": ("当前服务还没有通过健康检查。", "先恢复 AP-Vibe 本地服务，确认页面显示“在线”后再创建桌面启动器。"),
            "desktop_launcher_config_missing": ("找不到 AP-Vibe 的本机配置。", "先运行 AP-Vibe 安装入口，让它建立当前用户配置后再创建快捷方式。"),
            "desktop_launcher_config_invalid": ("AP-Vibe 本机配置无法核对。", "使用当前项目的安装脚本重新安装；没有修改桌面或其它启动项。"),
            "desktop_launcher_script_missing": ("找不到受控的 AP-Vibe 启动脚本。", "检查安装目录中的 scripts/ap-vibe.ps1，然后重新运行安装。"),
            "desktop_launcher_powershell_missing": ("找不到 Windows PowerShell。", "确认系统可运行 powershell.exe 或 pwsh.exe 后重试。"),
            "desktop_launcher_process_failed": ("桌面启动器创建过程没有完成。", "保留现有服务和项目数据，检查本机 PowerShell 后重试。"),
            "desktop_launcher_install_failed": ("桌面启动器没有创建成功。", "没有修改其它启动项；查看本地服务状态后重试。"),
            "desktop_launcher_identity_mismatch": ("桌面上已有同名但不属于 AP-Vibe 的快捷方式。", "请手动改名或移走 AP-Vibe.lnk，再重新创建；系统不会覆盖它。"),
            "desktop_launcher_readback_failed": ("快捷方式属性核对失败。", "不要使用这份快捷方式；检查桌面权限后重新创建。"),
            "desktop_launcher_unavailable": ("Windows 快捷方式组件暂时不可用。", "确认当前用户可以写入桌面后重试；其它启动项不会被改变。"),
            "desktop_launcher_body_must_be_empty": ("桌面启动器请求包含了不支持的字段。", "清空请求体后重试；启动器只使用当前 AP-Vibe 本机配置。"),
            "organization_codex_execution_failed": ("本机 Codex 未能完成这次任务。", "查看任务中的错误说明；项目与档案保留，可以重新发起。"),
            "organization_codex_timeout": ("本机 Codex 在本次运行时间内尚未完成。", "已保留任务；缩小选中的任务范围或稍后重试。"),
            "logic_question_required": ("还没有填写要观察的问题。", "用中文描述问题，例如：从点击开始到结果保存，经过哪些模块？"),
            "logic_analysis_result_invalid": ("Codex 分析结果缺少结论、发现或证据。", "已有档案保留；请重新分析并提供可核对的源码位置。"),
            "logic_analysis_sections_required": ("分析没有提供需要更新的档案章节。", "请提供与本次发现相关的架构、证据或恢复章节，系统会保存新版本。"),
            "logic_root_required": ("尚未选择代码目录。", "从当前项目的档案或已归类任务目录中选择。"),
            "logic_root_not_found": ("代码目录在本机不存在。", "先在项目档案的资料入口中更正代码位置。"),
            "logic_root_outside_project_root": ("所选目录不属于当前项目的已知代码入口。", "先核对项目归属，并在档案中登记实际代码目录；避免分析其他项目。"),
            "organization_result_incomplete": ("整理方案没有覆盖冻结清单中的每条会话。", "重新整理并明确跳过没有足够证据的会话；未覆盖内容不会被自动迁移。"),
            "organization_scope_conflict": ("整理方案包含清单之外或重复的会话。", "重新发起整理；系统不会写入越过本次冻结范围的来源。"),
            "organization_membership_changed": ("整理期间会话归属发生了变化。", "刷新项目与会话列表后重新准备任务，避免覆盖其他人的修改。"),
            "appearance_image_invalid": ("图片无法作为像素素材读取。", "请选择有效PNG，最长边不超过2048像素，文件不超过2MB。"),
            "appearance_frame_invalid": ("动作图集的帧尺寸或坐标不匹配。", "每帧必须完整位于图片内；可先只导入PNG，不选JSON。"),
            "appearance_manifest_invalid": ("素材说明格式不正确。", "使用第1版图集JSON，并至少包含idle动作。"),
            "appearance_name_invalid": ("请给外观填写一个名称。", "使用80字以内的普通名称。"),
            "appearance_not_found": ("这个外观还没有安装到本机。", "导入对应素材；任务和历史记录仍可使用。"),
            "document_revision_conflict": ("项目档案在整理期间被修改，版本已经变化。", "刷新档案并重新整理；系统保留现有人工内容。"),
            "session_assignment_evidence_required": ("会话归类缺少可核对的依据。", "提供真实会话来源或用户确认记录后再归类。"),
            "codex_session_identity_ambiguous": ("同一会话的来源文件身份不一致。", "先核对工作区和会话来源；身份不明确时不会批量迁移。"),
            "teacher_configuration_incomplete": ("教师增强模式还缺少接口地址、模型或 API Key。", "补齐配置，或关闭教师以继续使用基础模式。"),
            "teacher_url_invalid": ("教师接口地址不安全或格式不正确。", "使用不含账号、密码、查询密钥和片段的 OpenAI 兼容地址。"),
            "teacher_remote_https_required": ("远程教师接口必须使用 HTTPS。", "改用 HTTPS 地址；只有本机回环地址允许 HTTP。"),
            "teacher_secure_storage_unavailable": ("当前系统无法提供安全的教师密钥存储。", "不要把 Key 写入普通文件；在支持 Windows 当前用户加密的环境中配置。"),
            "teacher_hourly_limit_or_disabled": ("教师未开启，或已达到你设置的本小时预算。", "继续使用基础模式，或在教师设置中调整预算；留空表示不限额。"),
            "organization_service_closing": ("本地整理服务正在关闭。", "等待服务重新启动后查看任务状态；不会重复启动旧调用。"),
            "portable_import_confirmation_required": ("portable 确认请求不完整。", "提交 request_id 与 confirmation，其中包含 draft_id 和 bundle_hash。"),
            "portable_import_preview_required": ("portable 预览请求不完整。", "提交 request_id 与 bundle。"),
            "portable_import_rollback_required": ("portable 回滚请求不完整。", "提交已确认导入项目的 project_id。"),
            "portable_import_rollback_only": ("只有 portable 导入项目可以回滚。", "选择 source 为 portable_import 的已确认项目。"),
            "portable_import_incomplete": ("portable 导入还没有完成全部阶段。", "保留同一 draft_id 和 bundle_hash 重试，系统会从最后成功阶段继续。"),
            "portable_bundle_hash_mismatch": ("portable 内容 hash 不匹配。", "重新选择原始导出文件，不要手工修改 bundle。"),
            "portable_learning_snapshot_conflict": ("导入学习快照与已保存版本冲突。", "保留原 draft 重试；如内容确已改变，请重新预览。"),
        }
        message, solution = messages.get(
            code,
            ("本地请求没有完成。", "检查请求字段；如果是可重试的本地服务问题，请保留原 request_id 重试。"),
        )
        status = HTTPStatus.NOT_FOUND if code in not_found else HTTPStatus.CONFLICT if code in conflicts else HTTPStatus.BAD_REQUEST
        self._write_json(
            status,
            _error_payload(code=code, message=message, retryable=status >= 500 or code in {"codex_source_not_configured", "codex_sessions_root_not_configured"}, solution=solution),
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        split = urlsplit(self.path)
        path = split.path
        if path == "/v1/health":
            def health_projection():
                health = self.service.health().to_dict()
                # Liveness clients must not download full historical sampler
                # batches. Detailed source status has its own endpoint.
                sampler = health.get("ap_vibe", {}).get("codex_sampler", {})
                sampler.pop("sources", None)
                sampler.pop("last_batch", None)
                return health
            health = self.server.projections.read((path,), health_projection)
            self._write_json(HTTPStatus.OK if health["status"] == "ok" else HTTPStatus.SERVICE_UNAVAILABLE, health)
            return
        try:
            project_id = self._query_project_id(split)
            if path in {'/v1/ap-vibe/sessions', '/v1/ap-vibe/sessions/read'}:
                query = parse_qs(split.query)
                args = {key: values[0] for key, values in query.items()}
                allowed = ({'source_id', 'after', 'before', 'generation', 'limit'} if path.endswith('/read') else
                           {'harness', 'cwd', 'project_id', 'session_id', 'query', 'offset', 'limit'})
                if set(args) - allowed:
                    raise ContractError('session_query_invalid')
                for key in ('after', 'before', 'limit', 'offset'):
                    if key in args:
                        args[key] = int(args[key])
                if path.endswith('/read'):
                    args['expected_generation'] = args.pop('generation', None)
                    result = self.service.session_directory.read(args.pop('source_id', None), **args)
                else:
                    result = self.service.session_directory.catalog(**args)
                self._write_json(HTTPStatus.OK, result)
                return
            if path == "/v1/ap-vibe/agents":
                self._write_json(HTTPStatus.OK, self.service.agent_studio.profiles())
                return
            if path == "/v1/ap-vibe/agents/directory":
                self._write_json(HTTPStatus.OK, self.service.agent_studio.directory(self._query_value(split, 'agent_id')))
                return
            if path == "/v1/ap-vibe/agents/metrics":
                from .studio_metrics import snapshot
                self._write_json(HTTPStatus.OK, snapshot(self.service.agent_studio,
                    agent_id=self._query_value(split, 'agent_id'), project_id=self._query_value(split, 'project_id'),
                    days=int(self._query_value(split, 'days') or 0), tag=self._query_value(split, 'tag'),
                    configuration=self._query_value(split, 'configuration') or 'all',
                    offset=int(self._query_value(split, 'offset') or 0), limit=int(self._query_value(split, 'limit') or 20)))
                return
            if path == "/v1/ap-vibe/agents/presence":
                from .studio_presence import snapshot
                self._write_json(HTTPStatus.OK, snapshot(self.service.agent_studio))
                return
            if path == "/v1/ap-vibe/agents/appearances":
                self._write_json(HTTPStatus.OK, self.service.agent_studio.appearances.list(self._query_value(split, 'id')))
                return
            if path == "/v1/ap-vibe/agents/appearance-image":
                raw = self.service.agent_studio.appearances.image(self._query_value(split, 'id'))
                self.send_response(HTTPStatus.OK)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Cache-Control', 'private, max-age=31536000, immutable')
                self.end_headers()
                self.wfile.write(raw)
                return
            if path == "/v1/ap-vibe/agents/claude-sessions":
                source_id = self._query_value(split, 'source_id')
                if source_id:
                    after = self._query_value(split, 'after')
                    before = self._query_value(split, 'before')
                    self._write_json(HTTPStatus.OK, self.service.claude_sessions.read(source_id,
                        after=int(after) if after else None, before=int(before) if before else None,
                        generation=self._query_value(split, 'generation')))
                else:
                    self._write_json(HTTPStatus.OK, self.service.task_context.projects.annotate_claude_sources(self.service.claude_sessions.discover()))
                return
            if path == "/v1/ap-vibe/agents/runs":
                self._write_json(HTTPStatus.OK, self.service.agent_studio.runs(
                    self._query_value(split, "run_id"), int(self._query_value(split, "after") or 0),
                    compact=self._query_bool(split, 'compact', True)))
                return
            if path == "/v1/ap-vibe/agents/artifacts":
                run_id = self._query_value(split, 'run_id')
                name = self._query_value(split, 'name')
                artifacts = self.service.agent_studio.artifacts
                self._write_json(HTTPStatus.OK, artifacts.read(run_id, name) if name is not None else artifacts.list(run_id))
                return
            if path == "/v1/ap-vibe/collaboration/inbox":
                self._write_json(HTTPStatus.OK, self.service.agent_studio.inbox(
                    self._query_value(split, 'run_id'), int(self._query_value(split, 'after') or 0),
                    int(self._query_value(split, 'limit') or 8)))
                return
            if path == "/v1/ap-vibe/collaboration":
                self._write_json(HTTPStatus.OK, self.service.agent_studio.collaboration_state(
                    self._query_value(split, 'run_id'), int(self._query_value(split, 'after') or 0)))
                return
            if path == "/v1/ap-vibe/studio/tasks":
                self._write_json(HTTPStatus.OK, self.service.agent_studio.tasks.list(self._query_value(split, 'task_id'),
                    project_id=self._query_value(split, 'project_id'), state=self._query_value(split, 'state'),
                    offset=int(self._query_value(split, 'offset') or 0), limit=int(self._query_value(split, 'limit') or 200),
                    compact=self._query_bool(split, 'compact', False)))
                return
            if path == "/v1/ap-vibe/collaboration/ready":
                self._write_json(HTTPStatus.OK, self.service.collaboration.ready())
                return
            if path == "/v1/ap-vibe/codex/messages":
                self._write_json(HTTPStatus.OK, self.service.codex_messages.list(self._query_value(split, "session_id") or ""))
                return
            if path == "/v1/ap-vibe/logic/status":
                self._write_json(HTTPStatus.OK, self.service.logic_status(project_id))
                return
            if path == "/v1/ap-vibe/organization/tasks":
                self._write_json(HTTPStatus.OK, self.service.organization.tasks())
                return
            if path == "/v1/ap-vibe/organization/sessions":
                self._write_json(HTTPStatus.OK, self.service.organization.catalog(
                    self._query_value(split, "scope") or "recent_unclassified",
                    offset=int(self._query_value(split, "offset") or 0), limit=self._query_limit(split, 64)))
                return
            if path == "/v1/ap-vibe/organization/context":
                self._write_json(HTTPStatus.OK, self.service.organization.context(self._query_value(split, "source_key")))
                return
            if path == "/v1/ap-vibe/organization/task":
                self._write_json(HTTPStatus.OK, self.service.organization.task(self._query_value(split, "task_id"), int(self._query_value(split, "offset") or 0)))
                return
            if path == "/v1/ap-vibe/organization/status":
                self._write_json(HTTPStatus.OK, self.service.organization.cognition())
                return
            if path == "/v1/ap-vibe/teacher/settings":
                self._write_json(HTTPStatus.OK, self.service.teacher_settings.state())
                return
            if path == "/v1/ap-vibe/projects":
                include_archived = self._query_bool(split, "include_archived", True)
                self._write_json(HTTPStatus.OK, self.server.projections.read(
                    (path, include_archived), lambda: self.service.projects(include_archived=include_archived)))
                return
            if path == "/v1/ap-vibe/tasks/status":
                self._write_json(HTTPStatus.OK, self.service.task_context.status())
                return
            if path in {"/v1/ap-vibe/knowledge/catalog", "/v1/ap-vibe/knowledge/sections"}:
                sections = (self._query_value(split, "sections") or "").split(",")
                revision_text = self._query_value(split, "revision")
                if revision_text and not revision_text.isdigit():
                    raise ContractError("document_revision_invalid")
                self._write_json(HTTPStatus.OK, self.service.task_context.documents.read(
                    project_id or self.service.codex_project_id, [key for key in sections if key],
                    int(revision_text) if revision_text else None))
                return
            if path == "/v1/ap-vibe/state":
                limit = self._query_limit(split, MAX_RECENT_PROJECT_EPISODES)
                state = self.server.projections.read((path, project_id, limit),
                    lambda: self.service.recent_project_state(project_id, limit=limit))
                self._write_json(HTTPStatus.OK, state)
                return
            if path == "/v1/ap-vibe/recovery":
                self._write_json(HTTPStatus.OK, self.service.local_recovery(project_id))
                return
            if path == "/v1/ap-vibe/data":
                self._write_json(HTTPStatus.OK, self.server.projections.read(
                    (path, project_id), lambda: self.service.project_data(project_id)))
                return
            if path == "/v1/ap-vibe/codex/status":
                self._write_json(HTTPStatus.OK, self.service.codex_control_state(
                    project_id, details=self._query_bool(split, "details", False)))
                return
            if path == "/v1/ap-vibe/codex/overview":
                include_archived = self._query_bool(split, "include_archived", False)
                limit = self._query_limit(split, MAX_CODEX_OVERVIEW_SESSIONS)
                self._write_json(
                    HTTPStatus.OK,
                    self.server.projections.read((path, project_id, include_archived, limit), lambda: self.service.codex_overview(
                        project_id,
                        include_archived=include_archived,
                        limit=limit,
                    )),
                )
                return
            if path == "/v1/ap-vibe/portable/export":
                exported = self.service.export_project(project_id)
                self._write_json(
                    HTTPStatus.OK,
                    exported,
                    Content_Disposition='attachment; filename="ap-vibe-project.json"',
                )
                return
            if path == "/v1/ap-vibe/portable/import/draft":
                draft_id = self._query_value(split, "draft_id")
                if not draft_id:
                    raise ContractError("portable_import_draft_id_required")
                draft = self.service.product_registry.import_draft(draft_id)
                # The bundle is already redacted, but the durable target root
                # is a local detail and must not be projected by default.
                safe = dict(draft)
                safe.pop("target_root", None)
                if isinstance(safe.get("bundle"), Mapping):
                    safe["bundle"] = redact_portable(safe["bundle"])
                self._write_json(HTTPStatus.OK, {"status": "success", "draft": safe})
                return
            if path.startswith("/v1/"):
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    _error_payload(
                        code="route_not_found",
                        message="这个本地 API 路径不存在。",
                        retryable=False,
                        solution="检查 Mind Studio 版本和请求路径。",
                    ),
                )
                return
        except ContractError as exc:
            self._write_contract_error(exc)
            return
        except Exception:
            self._write_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                _error_payload(
                    code="project_query_unavailable",
                    message="本地项目查询没有完成，但页面可以继续保留上次成功现场。",
                    retryable=True,
                    solution="稍后刷新本地 daemon；不要把失败刷新当成项目数据为空。",
                ),
            )
            return
        if path.startswith("/v1/"):
            self._write_json(
                HTTPStatus.NOT_FOUND,
                _error_payload(
                    code="route_not_found",
                    message="这个本地 API 路径不存在。",
                    retryable=False,
                    solution="检查 Mind Studio 版本和请求路径。",
                ),
            )
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path not in {
            "/v1/ap-vibe/agents/save",
            "/v1/ap-vibe/agents/appearances/save",
            "/v1/ap-vibe/agents/appearances/archive",
            "/v1/ap-vibe/agents/archive",
            "/v1/ap-vibe/agents/start",
            "/v1/ap-vibe/agents/cancel",
            "/v1/ap-vibe/agents/review",
            "/v1/ap-vibe/agents/handoff",
            "/v1/ap-vibe/studio/tasks/save", "/v1/ap-vibe/studio/tasks/claim",
            "/v1/ap-vibe/studio/tasks/release", "/v1/ap-vibe/studio/tasks/archive",
            "/v1/ap-vibe/studio/tasks/verdict",
            "/v1/ap-vibe/collaboration/message", "/v1/ap-vibe/collaboration/broadcast", "/v1/ap-vibe/collaboration/claim",
            "/v1/ap-vibe/collaboration/handoff", "/v1/ap-vibe/collaboration/dependency",
            "/v1/ap-vibe/codex/messages",
            "/v1/ap-vibe/codex/messages/cancel",
            "/v1/demo/episodes",
            "/v1/ap-vibe/activities",
            "/v1/ap-vibe/feedback",
            "/v1/ap-vibe/knowledge/commit",
            "/v1/ap-vibe/knowledge/corrections/preview",
            "/v1/ap-vibe/knowledge/corrections/confirm",
            "/v1/ap-vibe/logic/query",
            "/v1/ap-vibe/logic/configure",
            "/v1/ap-vibe/logic/analyze",
            "/v1/ap-vibe/organization/apply",
            "/v1/ap-vibe/brief",
            "/v1/ap-vibe/codex/sync",
            "/v1/ap-vibe/codex/overview",
            "/v1/ap-vibe/projects",
            "/v1/ap-vibe/organization/prepare",
            "/v1/ap-vibe/organization/dispatch",
            "/v1/ap-vibe/organization/assign",
            "/v1/ap-vibe/organization/projects/create",
            "/v1/ap-vibe/organization/projects/update",
            "/v1/ap-vibe/organization/documents/update",
            "/v1/ap-vibe/teacher/settings",
            "/v1/ap-vibe/launcher/desktop",
            "/v1/ap-vibe/projects/status",
            "/v1/ap-vibe/projects/monitor",
            "/v1/ap-vibe/memories/disposition",
            "/v1/ap-vibe/codex/discover",
            "/v1/ap-vibe/codex/bind",
            "/v1/ap-vibe/tasks/bootstrap", "/v1/ap-vibe/tasks/projects", "/v1/ap-vibe/tasks/classify",
            "/v1/ap-vibe/tasks/knowledge",
            "/v1/ap-vibe/tasks/update",
            "/v1/ap-vibe/tasks/feedback",
            "/v1/ap-vibe/portable/import/preview",
            "/v1/ap-vibe/portable/import/confirm",
            "/v1/ap-vibe/portable/import/rollback",
        }:
            self._write_json(
                HTTPStatus.NOT_FOUND,
                _error_payload(
                    code="route_not_found",
                    message="这个本地 API 路径不存在。",
                    retryable=False,
                    solution="使用已公布的 Studio 或 AP-Vibe episode 路径。",
                ),
            )
            return
        try:
            raw = self._read_json()
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                raise ContractError("cross_origin_write_forbidden")
            if path == "/v1/ap-vibe/codex/messages":
                self._write_json(HTTPStatus.OK, self.service.codex_messages.enqueue(raw))
                return
            if path == "/v1/ap-vibe/codex/messages/cancel":
                self._write_json(HTTPStatus.OK, self.service.codex_messages.cancel(raw))
                return
            organization_routes = {
                "/v1/ap-vibe/logic/configure": self.service.configure_project_logic,
                "/v1/ap-vibe/logic/analyze": self.service.organization.prepare_logic,
                "/v1/ap-vibe/organization/apply": lambda r: self.service.organization.apply_result(r.get("task_id"), r.get("result")),
                "/v1/ap-vibe/organization/prepare": self.service.organization.prepare,
                "/v1/ap-vibe/organization/assign": self.service.organization.assign,
                "/v1/ap-vibe/organization/projects/create": self.service.organization.create_project,
                "/v1/ap-vibe/organization/projects/update": self.service.organization.update_project,
                "/v1/ap-vibe/organization/documents/update": self.service.organization.update_document,
                "/v1/ap-vibe/teacher/settings": self.service.teacher_settings.update,
                "/v1/ap-vibe/agents/save": self.service.agent_studio.save,
                "/v1/ap-vibe/agents/appearances/save": self.service.agent_studio.appearances.save,
                "/v1/ap-vibe/agents/appearances/archive": self.service.agent_studio.appearances.archive,
                "/v1/ap-vibe/agents/archive": self.service.agent_studio.archive,
                "/v1/ap-vibe/agents/start": self.service.agent_studio.start,
                "/v1/ap-vibe/agents/cancel": self.service.agent_studio.cancel,
                "/v1/ap-vibe/agents/review": self.service.agent_studio.review,
                "/v1/ap-vibe/agents/handoff": self.service.agent_studio.handoff_failed,
                "/v1/ap-vibe/collaboration/message": self.service.agent_studio.send_message,
                "/v1/ap-vibe/collaboration/broadcast": self.service.agent_studio.broadcast_message,
                "/v1/ap-vibe/collaboration/claim": self.service.collaboration.claim,
                "/v1/ap-vibe/collaboration/handoff": self.service.collaboration.handoff,
                "/v1/ap-vibe/collaboration/dependency": self.service.collaboration.dependency,
                "/v1/ap-vibe/studio/tasks/save": self.service.agent_studio.tasks.save,
                "/v1/ap-vibe/studio/tasks/claim": self.service.agent_studio.tasks.claim,
                "/v1/ap-vibe/studio/tasks/release": self.service.agent_studio.tasks.release,
                "/v1/ap-vibe/studio/tasks/archive": self.service.agent_studio.tasks.archive,
                "/v1/ap-vibe/studio/tasks/verdict": self.service.agent_studio.tasks.verdicts.submit,
            }
            if path in organization_routes:
                self._write_json(HTTPStatus.OK, organization_routes[path](raw))
                return
            if path == "/v1/ap-vibe/organization/dispatch":
                port = self.server.server_address[1]
                self._write_json(HTTPStatus.OK, self.service.organization.dispatch(raw, f"http://127.0.0.1:{port}"))
                return
            if path == "/v1/ap-vibe/launcher/desktop":
                if set(raw) - {"request_id"}:
                    raise ContractError("desktop_launcher_body_must_be_empty")
                self._write_json(HTTPStatus.OK, self.service.install_desktop_launcher())
                return
            if path == "/v1/ap-vibe/tasks/projects":
                self._write_json(HTTPStatus.OK, self.service.task_context.projects.catalog(raw))
                return
            if path == "/v1/ap-vibe/tasks/classify":
                self._write_json(HTTPStatus.OK, self.service.task_context.projects.classify(raw))
                return
            if path == "/v1/ap-vibe/tasks/bootstrap":
                self._write_json(HTTPStatus.OK, self.service.task_context.bootstrap(raw))
                return
            if path == "/v1/ap-vibe/tasks/knowledge":
                self._write_json(HTTPStatus.OK, self.service.task_context.knowledge(raw))
                return
            if path == "/v1/ap-vibe/tasks/update":
                self._write_json(HTTPStatus.OK, self.service.task_context.update_knowledge(raw))
                return
            if path == "/v1/ap-vibe/tasks/feedback":
                self._write_json(HTTPStatus.OK, self.service.task_context.feedback(raw))
                return
            if path == "/v1/demo/episodes":
                request = self._request_from_json(raw)
                view, replayed = self.service.run(request)
            elif path == "/v1/ap-vibe/activities":
                request_id, activity = self._vibe_envelope(raw, "activity")
                view, replayed = self.service.run_project_activity(request_id, activity)
            elif path == "/v1/ap-vibe/feedback":
                request_id, feedback = self._vibe_envelope(raw, "feedback")
                view, replayed = self.service.run_project_feedback(request_id, feedback)
            elif path == "/v1/ap-vibe/knowledge/commit":
                request_id, review = self._vibe_envelope(raw, "review")
                view, replayed = self.service.run_project_knowledge_review(request_id, review)
            elif path == "/v1/ap-vibe/knowledge/corrections/preview":
                request_id, correction = self._vibe_envelope(raw, "correction")
                draft, replayed = self.service.preview_knowledge_correction(request_id, correction)
                self._write_json(
                    HTTPStatus.OK,
                    {"status": "partial" if draft.get("proposal_incomplete") else "success", "replayed": replayed, "draft": draft},
                    X_AP_Mind_Replayed="true" if replayed else "false",
                )
                return
            elif path == "/v1/ap-vibe/knowledge/corrections/confirm":
                request_id, confirmation = self._vibe_envelope(raw, "confirmation")
                view, replayed = self.service.confirm_knowledge_correction(request_id, confirmation)
            elif path == "/v1/ap-vibe/logic/query":
                request_id, query = self._vibe_envelope(raw, "query")
                view, replayed = self.service.run_logic_query(request_id, query)
            elif path == "/v1/ap-vibe/brief":
                brief = self.service.agent_brief(raw)
                self._write_json(HTTPStatus.OK, brief)
                return
            elif path == "/v1/ap-vibe/projects":
                result = self.service.register_project(raw)
                self._write_json(HTTPStatus.OK, result)
                return
            elif path == "/v1/ap-vibe/projects/status":
                if set(raw) != {"project_id", "status"}:
                    raise ContractError("project_payload_required")
                project_id = raw.get("project_id")
                status = raw.get("status")
                if not isinstance(project_id, str) or not isinstance(status, str):
                    raise ContractError("project_payload_required")
                result = self.service.set_project_status(project_id, status)
                self._write_json(HTTPStatus.OK, result)
                return
            elif path == "/v1/ap-vibe/projects/monitor":
                if set(raw) != {"project_id", "enabled"}:
                    raise ContractError("project_payload_required")
                project_id = raw.get("project_id")
                enabled = raw.get("enabled")
                if not isinstance(project_id, str) or not isinstance(enabled, bool):
                    raise ContractError("project_payload_required")
                result = self.service.set_project_monitor(project_id, enabled)
                self._write_json(HTTPStatus.OK, result)
                return
            elif path == "/v1/ap-vibe/memories/disposition":
                result = self.service.set_memory_disposition(raw)
                self._write_json(HTTPStatus.OK, result)
                return
            elif path == "/v1/ap-vibe/codex/bind":
                self._write_json(HTTPStatus.OK, self.service.bind_codex_source(raw))
                return
            elif path == "/v1/ap-vibe/codex/discover":
                if raw:
                    raise ContractError("codex_discover_body_must_be_empty")
                self._write_json(HTTPStatus.OK, self.service.discover_codex_sources())
                return
            elif path == "/v1/ap-vibe/portable/import/preview":
                if "file_text" in raw:
                    if set(raw) - {"request_id", "file_text", "target_root", "target_display_name"} or not isinstance(raw.get("file_text"), str):
                        raise ContractError("portable_import_preview_required")
                    request_id = self.service._request_id(raw.get("request_id"))
                    try:
                        uploaded = json.loads(raw["file_text"])
                    except ValueError as exc:
                        raise ContractError("portable_bundle_must_be_object") from exc
                    if not isinstance(uploaded, Mapping):
                        raise ContractError("portable_bundle_must_be_object")
                    source_bundle = uploaded.get("bundle", uploaded)
                    if not isinstance(source_bundle, Mapping):
                        raise ContractError("portable_bundle_must_be_object")
                    bundle = {key: value for key, value in source_bundle.items() if key not in {"target_project_id", "target_root", "target_display_name"}}
                    bundle.update(target_root=raw.get("target_root"), target_display_name=raw.get("target_display_name"))
                else:
                    request_id, bundle = self._vibe_envelope(raw, "bundle")
                result = self.service.preview_project_import(request_id, bundle)
                self._write_json(
                    HTTPStatus.OK,
                    result,
                    X_AP_Mind_Replayed="true" if result.get("replayed") else "false",
                )
                return
            elif path == "/v1/ap-vibe/portable/import/confirm":
                request_id, confirmation = self._vibe_envelope(raw, "confirmation")
                result = self.service.confirm_project_import(request_id, confirmation)
                self._write_json(
                    HTTPStatus.OK,
                    result,
                    X_AP_Mind_Replayed="true" if result.get("replayed") else "false",
                )
                return
            elif path == "/v1/ap-vibe/portable/import/rollback":
                if set(raw) != {"project_id"} or not isinstance(raw.get("project_id"), str):
                    raise ContractError("portable_import_rollback_required")
                result = self.service.rollback_project_import(str(raw["project_id"]))
                self._write_json(
                    HTTPStatus.OK,
                    result,
                    X_AP_Mind_Replayed="true" if result.get("replayed") else "false",
                )
                return
            else:
                if path == "/v1/ap-vibe/codex/sync":
                    if not raw:
                        sync = self.service.sync_codex_activity()
                    elif set(raw) == {"project_id"} and isinstance(raw.get("project_id"), str):
                        sync = self.service.sync_codex_activity(raw["project_id"])
                    else:
                        raise ContractError("codex_sync_body_must_be_empty_or_project")
                else:
                    if raw:
                        raise ContractError("codex_sync_body_must_be_empty")
                    sync = self.service.sync_codex_activity()
                self._write_json(HTTPStatus.OK, sync)
                return
        except StudioRequestConflict:
            self._write_json(
                HTTPStatus.CONFLICT,
                _error_payload(
                    code="request_id_conflict",
                    message="同一个 request_id 已经用于不同输入。",
                    retryable=False,
                    solution="保留原请求用于重放；新输入请生成新的 request_id。",
                ),
            )
            return
        except KnowledgeRevisionConflict as exc:
            self._write_json(
                HTTPStatus.CONFLICT,
                _error_payload(
                    code=str(exc),
                    message="本地恢复基线在审阅期间已经变化，没有覆盖较新的版本。",
                    retryable=True,
                    solution="刷新页面读取最新 revision，再重新审阅这条候选。",
                ),
            )
            return
        except KnowledgeCorrectionConflict as exc:
            self._write_json(
                HTTPStatus.CONFLICT,
                _error_payload(
                    code=str(exc),
                    message="这份纠正草稿与当前知识版本不再精确匹配，没有覆盖较新的内容。",
                    retryable=True,
                    solution="刷新当前 revision，核对修改前内容并重新生成预览；旧草稿会保留供比较。",
                ),
            )
            return
        except KnowledgeCorrectionNotFound:
            self._write_json(
                HTTPStatus.NOT_FOUND,
                _error_payload(
                    code="knowledge_correction_draft_not_found",
                    message="当前项目中找不到这份纠正草稿。",
                    retryable=False,
                    solution="刷新页面并从仍可见的纠正原话重新生成预览。",
                ),
            )
            return
        except KnowledgeProposalNotFound:
            self._write_json(
                HTTPStatus.NOT_FOUND,
                _error_payload(
                    code="knowledge_proposal_not_found",
                    message="这条待审知识候选不在当前项目的持久 episode 中。",
                    retryable=False,
                    solution="刷新页面并从当前待审候选重新发起审阅；不要手工填写 proposal 内容。",
                ),
            )
            return
        except ProjectTargetNotFound:
            self._write_json(
                HTTPStatus.NOT_FOUND,
                _error_payload(
                    code="feedback_target_activity_not_found",
                    message="这条纠正对应的工程活动不在当前本地服务中。",
                    retryable=False,
                    solution="先提交或选择当前列表中的活动 episode，再发送纠正。",
                ),
            )
            return
        except ContractError as exc:
            code = str(exc)
            logic_contract_errors = {
                "logic_field_not_configured": (
                    "当前 daemon 尚未配置可分析的项目根目录。",
                    "在逻辑观察页选择正确项目和代码目录，点击保存目录；然后开始观察或交给 Codex 分析。",
                ),
                "logic_query_target_outside_configured_root": (
                    "目标超出了当前逻辑场固定的项目根目录。",
                    "改用项目内相对路径或 Python 完整限定名；如项目根选错，请用 --logic-root 重启 daemon。",
                ),
                "logic_query_expected_path_outside_configured_root": (
                    "预期链中有节点超出了当前逻辑场固定根目录。",
                    "只填写项目内相对路径或 Python 完整限定名，然后重新查询。",
                ),
                "first_broken_link_requires_expected_path": (
                    "首断点查询至少需要两个按顺序排列的节点。",
                    "填写“入口 → 中间处理 → 结果处理”；最少保留起点和终点。",
                ),
                "logic_query_project_mismatch": (
                    "查询项目与当前 daemon 绑定的项目不一致。",
                    "刷新页面并使用当前项目；若 daemon 绑定错误，请以正确 --codex-project-id 重启。",
                ),
            }
            if code in logic_contract_errors:
                message, solution = logic_contract_errors[code]
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    _error_payload(
                        code=code,
                        message=message,
                        retryable=False,
                        solution=solution,
                    ),
                )
            else:
                self._write_contract_error(exc)
            return
        except Exception:
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                _error_payload(
                    code="studio_episode_failed",
                    message="本地 episode 没有完成，但已保存可恢复状态。",
                    retryable=True,
                    solution="使用相同 request_id 重试；如果持续失败，查看本地 daemon 终端。",
                ),
            )
            return
        self._write_json(
            HTTPStatus.OK,
            {"status": view.get("status", "unknown"), "replayed": replayed, "episode": view},
            X_AP_Mind_Replayed="true" if replayed else "false",
        )

    def _serve_static(self, requested_path: str) -> None:
        studio_dir = self.service.studio_dir
        if studio_dir is None or not (studio_dir / "index.html").is_file():
            self._write_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                _error_payload(
                    code="studio_not_built",
                    message="Mind Studio 前端尚未构建。",
                    retryable=True,
                    solution="在 apps/studio 运行 npm install 和 npm run build，然后刷新页面。",
                ),
            )
            return
        relative = unquote(requested_path).lstrip("/") or "index.html"
        candidate = (studio_dir / relative).resolve()
        if candidate.parent != studio_dir and studio_dir not in candidate.parents:
            self._write_json(
                HTTPStatus.FORBIDDEN,
                _error_payload(
                    code="static_path_outside_root",
                    message="静态资源路径越界。",
                    retryable=False,
                    solution="返回 Mind Studio 首页。",
                ),
            )
            return
        if not candidate.is_file():
            candidate = studio_dir / "index.html"
        raw = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(raw)


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    data_dir: str | Path,
    studio_dir: str | Path | None = None,
    codex_source: str | Path | None = None,
    codex_project_id: str = "ap-vibe-local",
    gateway: HybridGateway | None = None,
    governance: GovernanceCompatibilityRecord | None = None,
    capability: CapabilityOwnership | None = None,
    correction_interpreter: KnowledgeCorrectionInterpreter | None = None,
    maturity_policy: CurriculumMaturityPolicy | None = None,
    teacher_capabilities: Sequence[str] | None = None,
    logic_root: str | Path | None = None,
    project_root: str | Path | None = None,
    codex_sessions_root: str | Path | None = None,
    auto_monitor: bool = False,
    auto_onboard_workspaces: bool = False,
    monitor_interval_seconds: float = 5.0,
) -> StudioHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ContractError("studio_server_must_bind_loopback")
    if isinstance(port, bool) or not 0 <= int(port) <= 65535:
        raise ContractError("studio_server_port_out_of_bounds")
    service = StudioEpisodeService(
        data_dir,
        studio_dir=studio_dir,
        codex_source=codex_source,
        codex_project_id=codex_project_id,
        gateway=gateway,
        governance=governance,
        capability=capability,
        correction_interpreter=correction_interpreter,
        maturity_policy=maturity_policy,
        teacher_capabilities=teacher_capabilities,
        logic_root=logic_root,
        project_root=project_root,
        codex_sessions_root=codex_sessions_root,
        auto_monitor=auto_monitor,
        auto_onboard_workspaces=auto_onboard_workspaces,
        monitor_interval_seconds=monitor_interval_seconds,
    )
    return StudioHTTPServer((host, int(port)), StudioRequestHandler, service=service)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local AP Mind Studio daemon")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=str(Path.cwd() / ".ap-mind-studio"))
    parser.add_argument("--studio-dir", default=str(Path.cwd() / "apps" / "studio" / "dist"))
    parser.add_argument("--codex-source", default=None)
    parser.add_argument("--codex-project-id", default="ap-vibe-local")
    parser.add_argument("--logic-root", default=str(Path.cwd()))
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--codex-sessions-root", default=None)
    parser.add_argument("--auto-monitor", action="store_true")
    parser.add_argument("--auto-onboard-workspaces", action="store_true")
    parser.add_argument("--monitor-interval-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    gateway, governance, capability, correction_interpreter = gateway_from_environment()
    server = create_server(
        host=args.host,
        port=args.port,
        data_dir=args.data_dir,
        studio_dir=args.studio_dir,
        codex_source=args.codex_source,
        codex_project_id=args.codex_project_id,
        gateway=gateway,
        governance=governance,
        capability=capability,
        correction_interpreter=correction_interpreter,
        logic_root=args.logic_root,
        project_root=args.project_root,
        codex_sessions_root=args.codex_sessions_root,
        auto_monitor=args.auto_monitor,
        auto_onboard_workspaces=args.auto_onboard_workspaces,
        monitor_interval_seconds=args.monitor_interval_seconds,
    )
    address, port = server.server_address[:2]
    provider_state = server.service.provider_state()
    print(
        _json(
            {
                "status": "ready",
                "url": f"http://{address}:{port}",
                "mode": "hybrid_teacher" if provider_state["configured"] else "local_provider_off",
                "provider": provider_state,
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "STUDIO_API_VERSION",
    "AP_VIBE_API_VERSION",
    "MAX_REQUEST_BYTES",
    "StudioRequestConflict",
    "ProjectTargetNotFound",
    "KnowledgeProposalNotFound",
    "HYBRID_WHITEPAPER_SHA256",
    "StudioServiceHealth",
    "StudioEpisodeService",
    "StudioHTTPServer",
    "StudioRequestHandler",
    "create_server",
    "gateway_from_environment",
    "main",
]
