"""Agent-maintained project documents, beside (never above) AP reviewed knowledge.

The existing product database holds immutable snapshots. This is a document
transport with provenance and optimistic concurrency, not a cognitive writer.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
import os
import re
from pathlib import Path
import tomllib
from typing import Any, Mapping
from urllib.parse import quote

from .codex_activity import _clean_text
from .contracts import ContractError, utc_now
from .project_knowledge import KNOWLEDGE_SECTIONS
from .product import _SECRET_PATTERNS

SECTION_INFO = {
    "identity": ("项目简介", "这个项目是什么、给谁用、解决什么问题", "填写 name、summary、audience，例如：家庭记账工具，帮助家人看懂收支。"),
    "requirements": ("用户目标与红线", "用户期待的效果、验收标准、不能违反的约束", "填写 goals、acceptance、redlines；例如：离线也可记账，不上传私人账单。"),
    "architecture": ("逻辑与架构", "系统怎样工作、模块如何连接、为什么这样设计", "填写 summary、flow、modules、intentional_design；解释数据从哪里进、怎样处理、到哪里出。"),
    "sources": ("设计书与资料入口", "设计书、代码和外部资料的准确位置", "填写 documents，每项包含 title、path、purpose；保留真实文件路径供 Codex 按需读取。"),
    "decisions": ("决策与逻辑变更", "当前决定、已撤销方案、新旧逻辑及变更理由", "填写 items，每项包含 id、status、old_logic、new_logic、reason、evidence_refs；撤销时保留历史。"),
    "work": ("完成与待办", "哪些已完成、哪些未实现、当前阻碍", "填写 completed、remaining、not_implemented、blocked、next_action；完成项要能找到验证依据。"),
    "risks": ("风险与十维评估", "薄弱环节、逐维评分、理由和改进方向", "填写 incidents、assessment。分数 0–100；未知用 null；有分数就必须有 reason 和 evidence_refs。"),
    "evidence": ("验证与新发现", "实际验证结果、有价值的新发现、历史事故教训", "填写 checks、discoveries、incidents；区分实际运行证据与推测，记录触发条件与影响。"),
    "dependencies": ("依赖与协作", "依赖哪些组件、服务或其他项目", "填写 items：name、purpose、status、entry；不要复制其他项目全部资料。"),
    "status": ("当前进度", "这一阶段做到了哪里、仍有什么不确定", "填写 summary、phase、unknown；例如：页面已构建，正在验收真实滚动行为。"),
    "recovery": ("恢复与下一步", "下次接手从哪里继续，哪些事情不应重复", "填写 summary、next_action、entry_points、do_not_repeat；入口指向上述章节或真实文件。"),
}
DIMENSIONS = (
    ("intent", "目标符合"), ("logic", "逻辑一致"), ("completeness", "功能完整"),
    ("reliability", "可靠恢复"), ("security", "安全隐私"), ("performance", "性能成本"),
    ("maintainability", "可维护性"), ("compatibility", "兼容扩展"),
    ("usability", "用户体验"), ("validation", "证据验证"),
)

_PROVISIONAL_FILE_BUDGET = 64 * 1024
_PROVISIONAL_CANDIDATES = ("README.md", "README.rst", "README.txt", "pyproject.toml", "package.json")


def _provisional_text(value: Any, limit: int = 420) -> str:
    if not isinstance(value, str):
        return ""
    value = _clean_text(value).strip()
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"[`*_>#]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit] + ("…" if len(value) > limit else "")


def _read_provisional_file(root: Path, name: str) -> str:
    path = root / name
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        with path.open("rb") as handle:
            raw = handle.read(_PROVISIONAL_FILE_BUDGET + 1)
        if len(raw) > _PROVISIONAL_FILE_BUDGET:
            raw = raw[:_PROVISIONAL_FILE_BUDGET]
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""


def provisional_sections(project: Any) -> dict[str, Any]:
    """Build a bounded, explicitly unverified project profile from fixed files.

    This is a display and navigation aid for newly discovered workspaces. It
    never becomes an agent document revision and never infers architecture or
    requirements from a directory name.
    """

    root = Path(str(project.root_path)).resolve()
    files: dict[str, str] = {name: _read_provisional_file(root, name) for name in _PROVISIONAL_CANDIDATES}
    readme_name = next((name for name in _PROVISIONAL_CANDIDATES[:3] if files[name].strip()), None)
    readme = files.get(readme_name or "", "")
    heading = ""
    paragraph = ""
    for line in readme.splitlines():
        value = line.strip()
        if not value or value.startswith(("[!", "<!--", "```", "---")):
            continue
        if not heading and re.match(r"^#{1,2}\s+", value):
            heading = _provisional_text(re.sub(r"^#{1,2}\s+", "", value), 160)
            continue
        if heading and not paragraph and not value.startswith("#"):
            paragraph = _provisional_text(value)
            if paragraph:
                break

    manifest_name = ""
    manifest_summary = ""
    if files["pyproject.toml"]:
        try:
            project_table = tomllib.loads(files["pyproject.toml"]).get("project", {})
            if isinstance(project_table, Mapping):
                manifest_name = _provisional_text(project_table.get("name"), 160)
                manifest_summary = _provisional_text(project_table.get("description"))
        except (ValueError, TypeError):
            pass
    if files["package.json"]:
        try:
            package = json.loads(files["package.json"])
            if isinstance(package, Mapping):
                manifest_name = manifest_name or _provisional_text(package.get("name"), 160)
                manifest_summary = manifest_summary or _provisional_text(package.get("description"))
        except (ValueError, TypeError):
            pass

    name = manifest_name or heading or str(getattr(project, "display_name", "本地工作区"))[:160]
    summary = manifest_summary or paragraph
    documents = []
    for filename in _PROVISIONAL_CANDIDATES:
        if not files[filename].strip():
            continue
        title = "项目说明" if filename.startswith("README") else "Python 项目元数据" if filename == "pyproject.toml" else "JavaScript 项目元数据"
        documents.append({"title": title, "path": filename, "purpose": "自动识别入口，任务中请由 Codex 核对后再作为项目事实使用"})

    source_note = "固定项目说明文件" if documents else "尚未找到固定项目说明文件"
    return {
        "identity": {
            "name": name,
            "summary": summary or "尚未从项目说明文件识别到简介。",
            "audience": "待 Codex 核对",
            "purpose": "待 Codex 核对",
            "provenance": "auto_detected",
            "confidence": "unverified",
            "source_files": [item["path"] for item in documents],
        },
        "requirements": {
            "goals": ["待 Codex 根据用户任务核对"],
            "acceptance": ["待 Codex 根据项目设计和测试核对"],
            "redlines": ["待 Codex 根据项目约束核对"],
            "provenance": "auto_detected",
        },
        "architecture": {
            "summary": "尚未读取代码结构；这只是自动建档入口。",
            "flow": ["Codex 任务开始 -> 读取项目资料目录 -> 按需核对代码和设计"],
            "modules": [],
            "intentional_design": [],
            "provenance": "auto_detected",
        },
        "sources": {"documents": documents, "provenance": "auto_detected"},
        "work": {
            "completed": [],
            "remaining": ["由 Codex 整理项目简介、目标、架构和当前进度"],
            "not_implemented": [],
            "blocked": [],
            "next_action": "任务开始时先核对自动识别的项目简介，再按需更新章节。",
            "provenance": "auto_detected",
        },
        "status": {"summary": "项目已自动登记，资料等待 Codex 核对。", "phase": "自动建档", "unknown": ["项目用途和真实完成情况尚未核对"], "provenance": "auto_detected"},
        "recovery": {"summary": "从项目资料目录开始，自动识别内容只作线索。", "next_action": "读取 identity、requirements、recovery 后再继续任务。", "entry_points": [item["path"] for item in documents], "do_not_repeat": ["不要把自动识别摘要当成已审项目事实"], "provenance": "auto_detected"},
        "_meta": {"state": "auto_detected", "source_note": source_note},
    }


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def document_patch_limit() -> int:
    try:
        value = int(os.environ.get("AP_VIBE_DOCUMENT_PATCH_BYTES", "524288"))
        return value if value > 0 else 524288
    except (TypeError, ValueError):
        return 524288


def clean(value: Any, depth: int = 0) -> Any:
    if depth > 10:
        raise ContractError("document_structure_too_deep")
    if isinstance(value, str):
        if len(value) > 12000:
            raise ContractError("document_text_too_long")
        text = _clean_text(value)
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(lambda m: ("".join(m.groups()) if m.lastindex else "") + "[REDACTED]", text)
        return text
    if isinstance(value, dict):
        if any(not isinstance(key, str) or len(key) > 128 for key in value):
            raise ContractError("document_fields_invalid")
        return {key: "[REDACTED]" if re.search(r"(?i)(?:api[_-]?key|password|authorization|access[_-]?token|secret)", key) and not re.search(r"(?i)(?:_location|_path|_ref)$", key) else clean(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        # Long-lived decisions/work histories grow naturally. Bound writes by
        # the configured byte budget, not an arbitrary number of history items.
        return [clean(item, depth + 1) for item in value]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ContractError("document_value_invalid")


def validate_assessment(value: Any) -> None:
    if not isinstance(value, list) or len(value) > len(DIMENSIONS):
        raise ContractError("document_assessment_invalid")
    seen = set()
    for item in value:
        if not isinstance(item, dict) or item.get("key") not in dict(DIMENSIONS) or item["key"] in seen:
            raise ContractError("document_assessment_dimension_invalid")
        seen.add(item["key"])
        score = item.get("score")
        if score is not None:
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
                raise ContractError("document_assessment_score_invalid")
            if not isinstance(item.get("reason"), str) or not item["reason"].strip():
                raise ContractError("document_assessment_reason_required")
            refs = item.get("evidence_refs")
            if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
                raise ContractError("document_assessment_evidence_required")


def assessment_quality(value: Any) -> dict[str, Any]:
    """Return a strict, display-safe quality summary for the ten dimensions.

    ``validate_assessment`` protects the write contract.  This helper is a
    read-side projection: legacy documents may be incomplete, so it reports
    the exact gap instead of rejecting the whole project catalogue.
    """

    expected = dict(DIMENSIONS)
    entries = value if isinstance(value, list) else []
    by_key = {
        item.get("key"): item
        for item in entries
        if isinstance(item, dict) and item.get("key") in expected
    }
    missing = [key for key in expected if key not in by_key]
    pending = [key for key, item in by_key.items() if not isinstance(item.get("score"), (int, float)) or isinstance(item.get("score"), bool)]
    scored = [key for key, item in by_key.items() if isinstance(item.get("score"), (int, float)) and not isinstance(item.get("score"), bool)
              and math.isfinite(item["score"]) and 0 <= item["score"] <= 100]
    issues: list[str] = []
    if not isinstance(value, list):
        issues.append("assessment_missing")
    if missing:
        issues.append("assessment_dimensions_missing:" + ",".join(missing))
    documented = 0
    for key, item in by_key.items():
        complete = True
        for field in ("reason", "risk", "improvement"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                issues.append(f"assessment_{field}_missing:" + key)
                complete = False
        refs = item.get("evidence_refs")
        if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs) or (item.get("score") is not None and not refs):
            issues.append("assessment_evidence_missing:" + key)
            complete = False
        score = item.get("score")
        if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 100):
            issues.append("assessment_score_invalid:" + key)
            complete = False
        documented += int(complete)
    return {
        "count": len(scored),
        "documented": documented,
        "total": len(expected),
        "pending": len(pending),
        "missing": missing,
        "issues": list(dict.fromkeys(issues)),
    }


def _provisional_marker(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return value.get("provenance") == "auto_detected" or value.get("confidence") == "unverified"


class ProjectDocuments:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.registry = service.product_registry
        with closing(self.registry._connect()) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS project_documents (
                    project_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    session_id TEXT NOT NULL, receipt_id TEXT NOT NULL,
                    parent_hash TEXT, content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(project_id, revision), UNIQUE(project_id, request_id)
                );
            """)
            connection.commit()

    @staticmethod
    def _decode(row) -> dict | None:
        if row is None:
            return None
        doc = json.loads(row["payload_json"])
        expected = hashlib.sha256((str(row["parent_hash"] or "") + canonical(doc)).encode()).hexdigest()
        if expected != row["content_hash"]:
            raise ContractError("document_hash_mismatch")
        return {**doc, "content_hash": expected, "parent_hash": row["parent_hash"]}

    def latest(self, project_id: str, revision: int | None = None) -> dict | None:
        with closing(self.registry._connect()) as connection:
            if revision is None:
                row = connection.execute("SELECT * FROM project_documents WHERE project_id=? ORDER BY revision DESC LIMIT 1", (project_id,)).fetchone()
            else:
                row = connection.execute("SELECT * FROM project_documents WHERE project_id=? AND revision=?", (project_id, revision)).fetchone()
        if revision is not None and row is None:
            raise ContractError("document_revision_not_found")
        return self._decode(row)

    def profile(self, project) -> dict:
        doc = self.latest(project.project_id)
        identity = (doc or {}).get("sections", {}).get("identity", {})
        provisional = provisional_sections(project)
        provisional_identity = provisional.get("identity", {})
        authority = (doc or {}).get("authority") or ""
        own = (doc or {}).get("sections", {})
        registered = project.extra.get("registration_state") == "registered" or project.source != "machine_auto_workspace" or bool(identity.get("summary") and authority in {"agent_reported", "user_edited"})
        missing = [key for key in SECTION_INFO if not own.get(key)]
        assessment = assessment_quality(own.get("risks", {}).get("assessment") if isinstance(own.get("risks"), Mapping) else None)
        quality_issues: list[str] = []
        if not doc:
            quality_issues.append("document_missing")
        if missing:
            quality_issues.append("chapters_missing:" + ",".join(missing))
        if _provisional_marker(identity):
            quality_issues.extend(["identity_auto_detected", "identity_unverified"])
        for key, value in own.items():
            if _provisional_marker(value):
                quality_issues.append("chapter_auto_detected:" + key)
        quality_issues.extend(assessment["issues"])
        quality_issues = list(dict.fromkeys(quality_issues))
        # ``documentation_state`` is retained for older clients and keeps the
        # historical user-edited compatibility semantics.  New clients should
        # use ``documentation_quality`` and the explicit issue list.
        quality = "ready" if not quality_issues else "needs_curation"
        quality_label = "资料已整理" if quality == "ready" else "可直接查询 · 证据待补"
        with closing(self.registry._connect()) as connection:
            renamed = connection.execute("SELECT 1 FROM project_admin_events WHERE project_id=? AND action='edit' LIMIT 1", (project.project_id,)).fetchone()
        return {**project.to_dict(), "display_name": project.display_name if renamed or project.source == "portable_import" else identity.get("name") or provisional_identity.get("name") or project.display_name,
                "description": identity.get("summary") or provisional_identity.get("summary", ""),
                "document_revision": (doc or {}).get("revision", 0),
                "registration_state": "registered" if registered else "discovered",
                "documentation_state": "maintained" if identity.get("summary") and authority in {"agent_reported", "user_edited", "portable_import"} else "needs_curation" if project.source == "curated_project" or project.extra.get("registration_state") == "registered" else "auto_detected",
                "documentation_complete": not missing,
                "documentation_quality": quality,
                "documentation_access": "available",
                "documentation_quality_label": quality_label,
                "quality_issues": quality_issues,
                "assessment_count": assessment["count"],
                "assessment_documented_count": assessment["documented"],
                "assessment_total": assessment["total"],
                "assessment_pending": assessment["pending"],
                "missing_sections": missing,
                "maintained_section_count": len(SECTION_INFO) - len(missing),
                "documentation_authority": authority or ("auto_detected" if provisional_identity else "unknown"),
                "provisional_source_files": provisional_identity.get("source_files", [])}

    def read(self, project_id: str, sections: list[str] | None = None, revision: int | None = None) -> dict:
        selected = self.service._require_project(project_id, active=False)
        if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int) or revision < 1):
            raise ContractError("document_revision_invalid")
        keys = sections or []
        if not isinstance(keys, list) or len(keys) > 3 or any(key not in KNOWLEDGE_SECTIONS for key in keys):
            raise ContractError("document_select_up_to_three_sections")
        doc = self.latest(project_id, revision)
        # The reviewed recovery chain is a separate source of evidence from
        # the project dossier.  A damaged or interrupted chain must be visible
        # to the caller, but it must not make the ordinary dossier unreadable.
        # ``validate`` returns the last valid prefix as ``baseline`` so that a
        # bounded read can still provide useful context without pretending the
        # chain is current.
        valid, reason, baseline = self.service.knowledge_store.validate(project_id)
        own = (doc or {}).get("sections", {})
        provisional = provisional_sections(selected)
        effective = {**provisional, **(dict(baseline.sections) if baseline and revision is None else {}), **own}
        base = "/v1/ap-vibe/knowledge/sections?project_id=" + quote(project_id, safe="")
        catalog = []
        for key, (label, description, example) in SECTION_INFO.items():
            value = effective.get(key) or {}
            summary = value.get("summary", "") if isinstance(value, dict) else ""
            authority_state = (doc or {}).get("section_authorities", {}).get(key, "agent_reported") if key in own else "local_reviewed_recovery" if baseline and key in baseline.sections and revision is None else "auto_detected" if key in provisional else "unknown"
            # A section written by the old discovery pass can carry an
            # agent-level document authority while its own payload remains an
            # unverified placeholder.  Keep that distinction visible.
            state = "user_edited" if authority_state == "user_edited" else "auto_detected" if _provisional_marker(value) else authority_state
            catalog.append({"key": key, "label": label, "description": description,
                            "example": example, "available": bool(value), "state": state, "summary": str(summary)[:200],
                            "authority": state,
                            "updated_at": (doc or {}).get("section_updates", {}).get(key, baseline.created_at if state == "local_reviewed_recovery" and baseline else None),
                            "read_url": base + "&sections=" + key})
        scores = {item["key"]: item for item in effective.get("risks", {}).get("assessment", []) if isinstance(item, dict) and item.get("key") in dict(DIMENSIONS)}
        assessment = [{"key": key, "label": label, "score": None, "reason": "还没有完成这个维度的评估。", "evidence_refs": [], **scores.get(key, {})} for key, label in DIMENSIONS]
        selected_sections = {key: effective.get(key, {}) for key in keys}
        return {"ok": True, "protocol": "ap-vibe.project-documents.v1", "project_id": selected.project_id,
                "project": self.profile(selected), "revision": (doc or {}).get("revision", 0),
                "content_hash": (doc or {}).get("content_hash"), "updated_at": (doc or {}).get("created_at"),
                "author_session_id": (doc or {}).get("session_id"), "authority": (doc or {}).get("authority") or "auto_detected",
                "reviewed_revision": baseline.revision_number if baseline else 0,
                "reviewed_chain": {"valid": bool(valid), "status": reason, "available_revision": baseline.revision_number if baseline else 0},
                "catalog": catalog, "sections": selected_sections, "import_origin": (doc or {}).get("import_origin"),
                "provisional": {"state": "auto_detected", "source_files": provisional.get("identity", {}).get("source_files", []), "sections": [key for key in keys if key in provisional and key not in own and not (baseline and key in baseline.sections and revision is None)]},
                "assessment": assessment if not keys or "risks" in keys else [],
                "history_url": base + "&revision=REVISION&sections=SECTION",
                "maintenance": "Codex 开始时确认简介与目标；按需读取目录中的章节；结束时只更新读过且改变的章节。",
                "vibe_formal_write": False}

    def update(self, project_id: str, session_id: str, receipt_id: str, raw: Mapping[str, Any], *, authority="agent_reported") -> dict:
        request_id = raw.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 256:
            raise ContractError("document_request_id_required")
        expected = raw.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise ContractError("document_expected_revision_required")
        sections = raw.get("sections")
        if not isinstance(sections, dict) or not sections or any(key not in KNOWLEDGE_SECTIONS for key in sections):
            raise ContractError("document_sections_invalid")
        sections = clean(sections)
        if any(not isinstance(value, dict) or not value for value in sections.values()):
            raise ContractError("document_section_must_be_nonempty_object")
        if len(canonical(sections).encode()) > document_patch_limit():
            raise ContractError(f"document_patch_too_large:{document_patch_limit()} bytes; update chapters separately or remove duplicate descriptions")
        identity = sections.get("identity", {})
        if "summary" in identity and not isinstance(identity["summary"], str):
            raise ContractError("document_project_summary_invalid")
        if "name" in identity and (not isinstance(identity["name"], str) or not identity["name"].strip() or len(identity["name"]) > 160):
            raise ContractError("document_project_name_invalid")
        if "assessment" in sections.get("risks", {}):
            validate_assessment(sections["risks"]["assessment"])
        fingerprint = hashlib.sha256(canonical({"session_id": session_id, "receipt_id": receipt_id,
                                               "expected_revision": expected, "sections": sections, "authority": authority}).encode()).hexdigest()
        with self.registry.transaction(), closing(self.registry._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute("SELECT * FROM project_documents WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise ContractError("document_request_conflict")
                doc = self._decode(prior)
                return {"ok": True, "replayed": True, "project_id": project_id, "revision": doc["revision"], "content_hash": doc["content_hash"], "authority": doc["authority"], "vibe_formal_write": False}
            parent = self._decode(connection.execute("SELECT * FROM project_documents WHERE project_id=? ORDER BY revision DESC LIMIT 1", (project_id,)).fetchone())
            if expected != (parent or {}).get("revision", 0):
                raise ContractError("document_revision_conflict")
            now = utc_now()
            doc = {"project_id": project_id, "revision": expected + 1, "session_id": session_id,
                   "receipt_id": receipt_id, "created_at": now, "authority": authority,
                   "section_authorities": {**(parent or {}).get("section_authorities", {}), **{key: authority for key in sections}},
                   "sections": {**(parent or {}).get("sections", {}), **sections},
                   "section_updates": {**(parent or {}).get("section_updates", {}), **{key: now for key in sections}}}
            if (parent or {}).get("import_origin"):
                doc["import_origin"] = parent["import_origin"]
            if len(canonical(doc).encode()) > max(384000, 2 * document_patch_limit()):
                raise ContractError("document_total_too_large")
            parent_hash = (parent or {}).get("content_hash")
            digest = hashlib.sha256((str(parent_hash or "") + canonical(doc)).encode()).hexdigest()
            connection.execute("INSERT INTO project_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (project_id, expected + 1, request_id, fingerprint, session_id, receipt_id, parent_hash, digest, now, canonical(doc)))
            if doc["sections"].get("identity", {}).get("summary") and authority in {"agent_reported", "user_edited"}:
                self.registry.mark_registered(project_id, connection=connection)
            connection.commit()
        return {"ok": True, "replayed": False, "project_id": project_id, "revision": expected + 1,
                "content_hash": digest, "updated_sections": list(sections), "authority": authority, "vibe_formal_write": False}

    def import_snapshot(self, project_id: str, snapshot: Mapping[str, Any], bundle_hash: str) -> dict:
        """Attach a validated portable dossier as the new project's first revision.

        Portable imports are already fenced by the registry draft and bundle
        hash.  They use the same immutable document writer, while avoiding a
        fake task receipt or silently merging into an existing dossier.
        """
        sections = snapshot.get("sections") if isinstance(snapshot, Mapping) else None
        if not isinstance(sections, Mapping) or not sections:
            return {"ok": True, "skipped": True, "reason": "portable_bundle_has_no_dossier"}
        clean_sections = {str(key): value for key, value in sections.items() if str(key) in KNOWLEDGE_SECTIONS}
        if not clean_sections:
            return {"ok": True, "skipped": True, "reason": "portable_bundle_has_no_dossier"}
        request_id = "portable:" + str(bundle_hash)
        fingerprint = hashlib.sha256(canonical(snapshot).encode()).hexdigest()
        with self.registry.transaction(), closing(self.registry._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute("SELECT * FROM project_documents WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise ContractError("document_request_conflict")
                return {"ok": True, "replayed": True, "revision": self._decode(prior)["revision"]}
            if connection.execute("SELECT 1 FROM project_documents WHERE project_id=?", (project_id,)).fetchone():
                raise ContractError("portable_import_target_document_exists")
            now = utc_now()
            doc = {"project_id": project_id, "revision": 1, "session_id": "portable-import", "receipt_id": request_id,
                   "created_at": now, "authority": "portable_import", "sections": clean(clean_sections),
                   "section_authorities": {key: snapshot.get("section_authorities", {}).get(key, "agent_reported") for key in clean_sections},
                   "section_updates": {key: snapshot.get("section_updates", {}).get(key, snapshot.get("created_at")) for key in clean_sections},
                   "import_origin": {"project_id": snapshot.get("project_id"), "revision": snapshot.get("revision"),
                                     "content_hash": snapshot.get("content_hash"), "bundle_hash": bundle_hash,
                                     "created_at": snapshot.get("created_at"), "imported_at": now}}
            digest = hashlib.sha256(canonical(doc).encode()).hexdigest()
            connection.execute("INSERT INTO project_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (project_id, 1, request_id, fingerprint, "portable-import", request_id, None, digest, now, canonical(doc)))
            self.registry.mark_registered(project_id, connection=connection)
            connection.commit()
        return {"ok": True, "replayed": False, "revision": 1}
