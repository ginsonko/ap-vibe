"""Bounded, evidence-bearing local logic-field instrument for AP-Vibe.

This module deliberately implements a *project instrument*, not another mind
or a truth oracle.  It extracts conservative Python AST facts from one fixed
root and answers three small local questions.  Missing static edges remain
unknown; they never prove a runtime bug, dead code, formal validity, or
empirical truth.
"""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import time
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

from .contracts import ContractError, utc_now


LOGIC_FIELD_VERSION = "ap-vibe.logic-field.v1"
EXTRACTOR_VERSION = "python-ast.v1"
SUPPORTED_QUERY_KINDS = frozenset(
    {"module_responsibility", "change_impact", "first_broken_link"}
)
SUPPORTED_EDGE_KINDS = frozenset({"defines", "imports", "calls", "tests"})
SOURCE_EXTENSIONS = frozenset({".py", ".pyi"})
KNOWN_UNSUPPORTED_SOURCE_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx"})
SKIPPED_DIRECTORY_NAMES = frozenset(
    {
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        "coverage",
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
    }
)
MAX_QUERY_TEXT = 512
MAX_PATH_SEGMENTS = 12
MAX_REPORT_NODES = 96
MAX_REPORT_EDGES = 192
MAX_REPORT_DEPTH = 8
MAX_UNKNOWN_ITEMS = 64

_SECRET_PATTERNS = (
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(
        r"(?i)((?:api[_ -]?key|authorization|access[_ -]?token|secret|password)"
        r"\s*[:=]\s*[\"']?)[^\s,;\"']{8,}"
    ),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
)


def _stable_id(prefix: str, *parts: str) -> str:
    """Create an opaque identity; the digest is never used as semantics."""

    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: Any, name: str, *, limit: int = MAX_QUERY_TEXT, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{name}_must_be_text")
    clean = value.strip()
    if not clean and not allow_empty:
        raise ContractError(f"{name}_required")
    if len(clean) > limit:
        raise ContractError(f"{name}_too_long")
    if "\x00" in clean:
        raise ContractError(f"{name}_contains_nul")
    return clean


def _bounded_int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{name}_must_be_integer")
    if not low <= value <= high:
        raise ContractError(f"{name}_out_of_bounds")
    return value


def _target_is_unsafe(value: str) -> bool:
    normalized = value.replace("\\", "/")
    if (
        Path(value).is_absolute()
        or PurePosixPath(normalized).is_absolute()
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:[/\\]", value)
    ):
        return True
    return ".." in PurePosixPath(normalized).parts


def _source_ref(path: str, start_line: int, end_line: int) -> str:
    safe_path = quote(path, safe="/._-")
    return f"source://project/{safe_path}#L{start_line}-L{end_line}"


def _redact_source_summary(value: str) -> str:
    """Keep source documentation useful without projecting credentials."""

    clean = value
    for pattern in _SECRET_PATTERNS:
        clean = pattern.sub(
            lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]",
            clean,
        )
    return clean


@dataclass(frozen=True)
class LogicQuery:
    query_id: str
    project_id: str
    kind: str
    target: str
    expected_path: tuple[str, ...] = ()
    max_nodes: int = 48
    max_edges: int = 96
    max_depth: int = 4
    include_tests: bool = True
    source_ref: str = "user://ap-vibe/logic-query"

    def __post_init__(self) -> None:
        for name in ("query_id", "project_id", "target", "source_ref"):
            _text(getattr(self, name), name)
        if self.kind not in SUPPORTED_QUERY_KINDS:
            raise ContractError("logic_query_kind_unsupported")
        if _target_is_unsafe(self.target):
            raise ContractError("logic_query_target_outside_configured_root")
        if isinstance(self.expected_path, (str, bytes)) or not isinstance(self.expected_path, Sequence):
            raise ContractError("logic_query_expected_path_must_be_sequence")
        if len(self.expected_path) > MAX_PATH_SEGMENTS:
            raise ContractError("logic_query_expected_path_too_long")
        for part in self.expected_path:
            _text(part, "logic_query_expected_path_item")
            if _target_is_unsafe(part):
                raise ContractError("logic_query_expected_path_outside_configured_root")
        if self.kind == "first_broken_link" and len(self.expected_path) < 2:
            raise ContractError("first_broken_link_requires_expected_path")
        if self.kind != "first_broken_link" and self.expected_path:
            raise ContractError("logic_query_expected_path_only_for_first_broken_link")
        _bounded_int(self.max_nodes, "logic_query_max_nodes", 4, MAX_REPORT_NODES)
        _bounded_int(self.max_edges, "logic_query_max_edges", 4, MAX_REPORT_EDGES)
        _bounded_int(self.max_depth, "logic_query_max_depth", 1, MAX_REPORT_DEPTH)
        if not isinstance(self.include_tests, bool):
            raise ContractError("logic_query_include_tests_must_be_boolean")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LogicQuery":
        if not isinstance(raw, Mapping):
            raise ContractError("logic_query_must_be_an_object")
        allowed = {
            "query_id", "project_id", "kind", "target", "expected_path",
            "max_nodes", "max_edges", "max_depth", "include_tests", "source_ref",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ContractError("logic_query_unknown_fields")
        values = dict(raw)
        if "expected_path" in values:
            value = values["expected_path"]
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise ContractError("logic_query_expected_path_must_be_sequence")
            values["expected_path"] = tuple(value)
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "project_id": self.project_id,
            "kind": self.kind,
            "target": self.target,
            "expected_path": list(self.expected_path),
            "max_nodes": self.max_nodes,
            "max_edges": self.max_edges,
            "max_depth": self.max_depth,
            "include_tests": self.include_tests,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class SourceSnapshot:
    snapshot_id: str
    content_hash: str
    extractor_version: str
    root_scope: str
    root_label: str
    files: tuple[Mapping[str, Any], ...]
    scanned_files: int
    scanned_bytes: int
    skipped_files: int
    unsupported_source_files: int
    failed_files: tuple[Mapping[str, Any], ...]
    completeness: str
    limits_reached: tuple[str, ...]
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "content_hash": self.content_hash,
            "extractor_version": self.extractor_version,
            "root_scope": self.root_scope,
            "root_label": self.root_label,
            "files": [dict(item) for item in self.files],
            "scanned_files": self.scanned_files,
            "scanned_bytes": self.scanned_bytes,
            "skipped_files": self.skipped_files,
            "unsupported_source_files": self.unsupported_source_files,
            "failed_files": [dict(item) for item in self.failed_files],
            "completeness": self.completeness,
            "limits_reached": list(self.limits_reached),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class LogicNode:
    node_id: str
    language: str
    kind: str
    path: str
    module: str
    qualname: str
    name: str
    start_line: int
    end_line: int
    summary: str
    evidence_refs: tuple[str, ...]
    lifecycle: str = "implemented"

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "language": self.language,
            "kind": self.kind,
            "path": self.path,
            "module": self.module,
            "qualname": self.qualname,
            "name": self.name,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "lifecycle": self.lifecycle,
        }


@dataclass(frozen=True)
class LogicEdge:
    edge_id: str
    kind: str
    source_ref: str
    target_ref: str
    path: str
    start_line: int
    end_line: int
    confidence: float
    evidence_refs: tuple[str, ...]
    evidence_state: str = "static_observed"
    runtime_state: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "kind": self.kind,
            "source_ref": self.source_ref,
            "target_ref": self.target_ref,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "confidence": round(self.confidence, 6),
            "evidence_refs": list(self.evidence_refs),
            "evidence_state": self.evidence_state,
            "runtime_state": self.runtime_state,
        }


@dataclass(frozen=True)
class LogicFieldReport:
    report_id: str
    query: LogicQuery
    snapshot: SourceSnapshot
    status: str
    summary: str
    resolved_targets: tuple[Mapping[str, Any], ...]
    nodes: tuple[LogicNode, ...]
    edges: tuple[LogicEdge, ...]
    paths: tuple[Mapping[str, Any], ...]
    first_break: Mapping[str, Any] | None
    suspected_orphans: tuple[Mapping[str, Any], ...]
    unknowns: tuple[Mapping[str, Any], ...]
    completeness: str
    budgets: Mapping[str, Any]
    logic_axes: Mapping[str, str]
    limitations: tuple[str, ...]
    next_check: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": LOGIC_FIELD_VERSION,
            "report_id": self.report_id,
            "query": self.query.to_dict(),
            "snapshot": self.snapshot.to_dict(),
            "status": self.status,
            "summary": self.summary,
            "resolved_targets": [dict(item) for item in self.resolved_targets],
            "nodes": [item.to_dict() for item in self.nodes],
            "edges": [item.to_dict() for item in self.edges],
            "paths": [dict(item) for item in self.paths],
            "first_break": dict(self.first_break) if self.first_break else None,
            "suspected_orphans": [dict(item) for item in self.suspected_orphans],
            "unknowns": [dict(item) for item in self.unknowns],
            "completeness": self.completeness,
            "budgets": dict(self.budgets),
            "logic_axes": dict(self.logic_axes),
            "limitations": list(self.limitations),
            "next_check": self.next_check,
            "product_effect": "bounded_local_source_relationship_observation",
            "ownership": {
                "source_extraction": "native",
                "query_resolution": "native",
                "runtime_truth": "absent",
                "semantic_advisor": "absent",
                "action_winner": "absent",
            },
        }


@dataclass(frozen=True)
class LogicFieldPolicy:
    max_files: int = 512
    max_total_bytes: int = 8 * 1024 * 1024
    max_file_bytes: int = 768 * 1024
    max_index_nodes: int = 8_192
    max_index_edges: int = 32_768
    max_seconds: float = 5.0

    def __post_init__(self) -> None:
        _bounded_int(self.max_files, "logic_policy_max_files", 1, 4_096)
        _bounded_int(self.max_total_bytes, "logic_policy_max_total_bytes", 1_024, 64 * 1024 * 1024)
        _bounded_int(self.max_file_bytes, "logic_policy_max_file_bytes", 1_024, 4 * 1024 * 1024)
        _bounded_int(self.max_index_nodes, "logic_policy_max_index_nodes", 32, 65_536)
        _bounded_int(self.max_index_edges, "logic_policy_max_index_edges", 64, 262_144)
        if isinstance(self.max_seconds, bool) or not 0.1 <= float(self.max_seconds) <= 30.0:
            raise ContractError("logic_policy_max_seconds_out_of_bounds")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_files": self.max_files,
            "max_total_bytes": self.max_total_bytes,
            "max_file_bytes": self.max_file_bytes,
            "max_index_nodes": self.max_index_nodes,
            "max_index_edges": self.max_index_edges,
            "max_seconds": self.max_seconds,
        }


@dataclass
class _ParsedFile:
    path: str
    module: str
    tree: ast.AST
    content_hash: str
    size: int


@dataclass
class _Index:
    snapshot: SourceSnapshot
    nodes: dict[str, LogicNode]
    edges: tuple[LogicEdge, ...]
    unknowns: tuple[Mapping[str, Any], ...]
    limits_reached: tuple[str, ...]


class _CallVisitor(ast.NodeVisitor):
    """Visit calls in one symbol body without stealing nested symbol calls."""

    def __init__(self) -> None:
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - AST API
        self.calls.append(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return


class LogicFieldInstrument:
    """Read one configured project root and answer finite local questions."""

    def __init__(self, root: str | Path, *, policy: LogicFieldPolicy | None = None) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ContractError("logic_field_root_not_found")
        self.policy = policy or LogicFieldPolicy()

    def health(self) -> dict[str, Any]:
        return {
            "configured": True,
            "protocol_version": LOGIC_FIELD_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "languages": ["python"],
            "root_scope": "configured_project_root",
            "root_label": self.root.name,
            "budgets": self.policy.to_dict(),
            "static_only": True,
            "runtime_truth": False,
            "subjective_closure": "not_assessed",
        }

    def query(self, query: LogicQuery) -> LogicFieldReport:
        if not isinstance(query, LogicQuery):
            raise ContractError("logic_query_required")
        index = self._build_index(include_tests=query.include_tests)
        if query.kind == "module_responsibility":
            result = self._module_responsibility(query, index)
        elif query.kind == "change_impact":
            result = self._change_impact(query, index)
        else:
            result = self._first_broken_link(query, index)
        report_payload = {
            "query": query.to_dict(),
            "snapshot": index.snapshot.snapshot_id,
            "status": result["status"],
            "node_ids": [node.node_id for node in result["nodes"]],
            "edge_ids": [edge.edge_id for edge in result["edges"]],
            "first_break": result.get("first_break"),
        }
        return LogicFieldReport(
            report_id=_stable_id("logic_report", _canonical(report_payload)),
            query=query,
            snapshot=index.snapshot,
            status=result["status"],
            summary=result["summary"],
            resolved_targets=tuple(result.get("resolved_targets", ())),
            nodes=tuple(result["nodes"]),
            edges=tuple(result["edges"]),
            paths=tuple(result.get("paths", ())),
            first_break=result.get("first_break"),
            suspected_orphans=tuple(result.get("suspected_orphans", ())),
            unknowns=tuple(result.get("unknowns", ())),
            completeness=result["completeness"],
            budgets={
                "max_nodes": query.max_nodes,
                "max_edges": query.max_edges,
                "max_depth": query.max_depth,
                "returned_nodes": len(result["nodes"]),
                "returned_edges": len(result["edges"]),
                "scan": self.policy.to_dict(),
                "limits_reached": list(dict.fromkeys((*index.limits_reached, *result.get("limits_reached", ())))),
            },
            logic_axes={
                "subjective_closure": "not_assessed",
                "execution_consistency": "unknown",
                "formal_validity": "static_structure_only",
                "empirical_truth": "unknown",
            },
            limitations=(
                "python_ast_static_view_only",
                "dynamic_dispatch_reflection_and_dependency_injection_may_be_missing",
                "static_reachability_is_not_runtime_impact_or_bug_proof",
                "suspected_orphan_never_authorizes_deletion",
                "logic_field_does_not_set_ap_feeling_attention_or_action",
            ),
            next_check=result["next_check"],
        )

    @staticmethod
    def _module_name(relative_path: str) -> str:
        parts = list(PurePosixPath(relative_path).parts)
        if parts and parts[0] == "src":
            parts = parts[1:]
        if not parts:
            return "project"
        leaf = parts[-1]
        if leaf.endswith(".pyi"):
            parts[-1] = leaf[:-4]
        elif leaf.endswith(".py"):
            parts[-1] = leaf[:-3]
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts) or "project"

    def _iter_source_files(self) -> tuple[list[Path], int, int]:
        supported: list[Path] = []
        skipped = 0
        unsupported = 0
        for current, dirs, files in os.walk(self.root, followlinks=False):
            current_path = Path(current)
            dirs[:] = sorted(
                name
                for name in dirs
                if name not in SKIPPED_DIRECTORY_NAMES and not name.startswith(".")
            )
            for name in sorted(files):
                path = current_path / name
                suffix = path.suffix.lower()
                if suffix in KNOWN_UNSUPPORTED_SOURCE_EXTENSIONS:
                    unsupported += 1
                if suffix not in SOURCE_EXTENSIONS:
                    skipped += 1
                    continue
                try:
                    resolved = path.resolve()
                    resolved.relative_to(self.root)
                except (OSError, ValueError):
                    skipped += 1
                    continue
                supported.append(resolved)
        supported.sort(key=lambda item: item.relative_to(self.root).as_posix())
        return supported, skipped, unsupported

    def _build_index(self, *, include_tests: bool) -> _Index:
        started = time.monotonic()
        paths, skipped, unsupported = self._iter_source_files()
        parsed: list[_ParsedFile] = []
        failed: list[Mapping[str, Any]] = []
        file_rows: list[Mapping[str, Any]] = []
        limits: list[str] = []
        scanned_bytes = 0
        for path in paths:
            relative = path.relative_to(self.root).as_posix()
            if not include_tests and (
                relative.startswith("tests/") or PurePosixPath(relative).name.startswith("test_")
            ):
                skipped += 1
                continue
            if len(parsed) >= self.policy.max_files:
                limits.append("file_budget_exhausted")
                break
            if time.monotonic() - started > self.policy.max_seconds:
                limits.append("time_budget_exhausted")
                break
            try:
                size = path.stat().st_size
            except OSError:
                failed.append({"path": relative, "code": "stat_failed"})
                continue
            if size > self.policy.max_file_bytes:
                skipped += 1
                failed.append({"path": relative, "code": "file_too_large", "size": size})
                continue
            if scanned_bytes + size > self.policy.max_total_bytes:
                limits.append("byte_budget_exhausted")
                break
            try:
                raw = path.read_bytes()
                content_hash = hashlib.sha256(raw).hexdigest().upper()
                text = raw.decode("utf-8-sig")
                tree = ast.parse(text, filename=relative)
            except UnicodeDecodeError:
                failed.append({"path": relative, "code": "utf8_decode_failed"})
                scanned_bytes += size
                file_rows.append({"path": relative, "sha256": content_hash, "size": size, "status": "failed"})
                continue
            except SyntaxError as exc:
                failed.append(
                    {
                        "path": relative,
                        "code": "python_syntax_error",
                        "line": int(exc.lineno or 0),
                    }
                )
                scanned_bytes += size
                file_rows.append({"path": relative, "sha256": content_hash, "size": size, "status": "failed"})
                continue
            except OSError:
                failed.append({"path": relative, "code": "read_failed"})
                continue
            scanned_bytes += size
            file_rows.append({"path": relative, "sha256": content_hash, "size": size, "status": "parsed"})
            parsed.append(
                _ParsedFile(
                    path=relative,
                    module=self._module_name(relative),
                    tree=tree,
                    content_hash=content_hash,
                    size=size,
                )
            )

        aggregate = hashlib.sha256(_canonical(file_rows).encode("utf-8")).hexdigest().upper()
        completeness = "search_incomplete" if limits else ("partial" if failed or unsupported else "complete")
        snapshot = SourceSnapshot(
            snapshot_id=_stable_id("source_snapshot", EXTRACTOR_VERSION, aggregate, str(include_tests)),
            content_hash=aggregate,
            extractor_version=EXTRACTOR_VERSION,
            root_scope="configured_project_root",
            root_label=self.root.name,
            files=tuple(file_rows),
            scanned_files=len(parsed),
            scanned_bytes=scanned_bytes,
            skipped_files=skipped,
            unsupported_source_files=unsupported,
            failed_files=tuple(failed[:MAX_UNKNOWN_ITEMS]),
            completeness=completeness,
            limits_reached=tuple(dict.fromkeys(limits)),
        )
        nodes: dict[str, LogicNode] = {}
        ast_symbols: dict[str, ast.AST] = {}
        module_nodes: dict[str, str] = {}
        for item in parsed:
            if len(nodes) >= self.policy.max_index_nodes:
                limits.append("index_node_budget_exhausted")
                break
            end_line = max((getattr(node, "end_lineno", 1) or 1 for node in ast.walk(item.tree)), default=1)
            module_node = self._node(item.path, item.module, item.module, item.module.rsplit(".", 1)[-1], "module", 1, end_line, "")
            nodes[module_node.node_id] = module_node
            module_nodes[item.module] = module_node.node_id
            ast_symbols[module_node.node_id] = item.tree
            self._collect_definitions(item, item.tree.body, module_node, nodes, ast_symbols, limits)

        edges: list[LogicEdge] = []
        unknowns: list[Mapping[str, Any]] = []
        lookup_by_qualname: dict[str, list[str]] = defaultdict(list)
        for node in nodes.values():
            lookup_by_qualname[node.qualname].append(node.node_id)
        for item in parsed:
            module_ref = module_nodes.get(item.module)
            if module_ref is None:
                continue
            self._collect_defines(nodes, module_ref, edges, limits)
            aliases = self._import_aliases(item, module_nodes, lookup_by_qualname, edges, nodes, limits, unknowns)
            symbol_ids = [node_id for node_id, node in nodes.items() if node.path == item.path and node.kind != "module"]
            for symbol_id in sorted(symbol_ids, key=lambda ref: (nodes[ref].start_line, nodes[ref].qualname)):
                syntax = ast_symbols.get(symbol_id)
                if syntax is None or not isinstance(syntax, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                visitor = _CallVisitor()
                for statement in syntax.body:
                    visitor.visit(statement)
                for call in visitor.calls:
                    target_id, reason = self._resolve_call(call.func, nodes[symbol_id], aliases, lookup_by_qualname, nodes)
                    if target_id is None:
                        if len(unknowns) < MAX_UNKNOWN_ITEMS:
                            unknowns.append(
                                {
                                    "code": reason,
                                    "source_ref": symbol_id,
                                    "path": item.path,
                                    "line": int(getattr(call, "lineno", nodes[symbol_id].start_line)),
                                    "meaning": "static_target_not_uniquely_observed",
                                }
                            )
                        continue
                    kind = "tests" if nodes[symbol_id].kind == "test" and nodes[target_id].kind != "test" else "calls"
                    self._add_edge(
                        edges,
                        kind,
                        symbol_id,
                        target_id,
                        item.path,
                        int(getattr(call, "lineno", nodes[symbol_id].start_line)),
                        int(getattr(call, "end_lineno", getattr(call, "lineno", nodes[symbol_id].start_line))),
                        0.96 if kind == "tests" else 0.92,
                        limits,
                    )

        unique_edges = {edge.edge_id: edge for edge in edges}
        ordered_edges = tuple(
            sorted(
                unique_edges.values(),
                key=lambda edge: (edge.kind, nodes[edge.source_ref].qualname, nodes[edge.target_ref].qualname, edge.path, edge.start_line),
            )
        )
        final_limits = tuple(dict.fromkeys(limits))
        if final_limits != snapshot.limits_reached:
            snapshot = SourceSnapshot(
                **{
                    **snapshot.__dict__,
                    "completeness": "search_incomplete",
                    "limits_reached": final_limits,
                }
            )
        return _Index(snapshot, nodes, ordered_edges, tuple(unknowns), final_limits)

    def _node(
        self,
        path: str,
        module: str,
        qualname: str,
        name: str,
        kind: str,
        start_line: int,
        end_line: int,
        docstring: str,
    ) -> LogicNode:
        identity = ("python", path, qualname, kind)
        summary = (
            _redact_source_summary(" ".join(docstring.strip().split()))[:240]
            if docstring
            else f"{kind} {qualname}"
        )
        return LogicNode(
            node_id=_stable_id("logic_node", *identity),
            language="python",
            kind=kind,
            path=path,
            module=module,
            qualname=qualname,
            name=name,
            start_line=max(1, int(start_line)),
            end_line=max(max(1, int(start_line)), int(end_line)),
            summary=summary,
            evidence_refs=(_source_ref(path, max(1, int(start_line)), max(max(1, int(start_line)), int(end_line))),),
        )

    def _collect_definitions(
        self,
        item: _ParsedFile,
        statements: Sequence[ast.stmt],
        parent: LogicNode,
        nodes: dict[str, LogicNode],
        ast_symbols: dict[str, ast.AST],
        limits: list[str],
    ) -> None:
        for syntax in statements:
            if not isinstance(syntax, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if len(nodes) >= self.policy.max_index_nodes:
                limits.append("index_node_budget_exhausted")
                return
            qualname = f"{parent.qualname}.{syntax.name}"
            is_test = (
                isinstance(syntax, (ast.FunctionDef, ast.AsyncFunctionDef))
                and syntax.name.startswith("test_")
                and (item.path.startswith("tests/") or PurePosixPath(item.path).name.startswith("test_"))
            )
            if is_test:
                kind = "test"
            elif isinstance(syntax, ast.ClassDef):
                kind = "class"
            elif parent.kind == "class":
                kind = "method"
            else:
                kind = "function"
            node = self._node(
                item.path,
                item.module,
                qualname,
                syntax.name,
                kind,
                int(getattr(syntax, "lineno", 1)),
                int(getattr(syntax, "end_lineno", getattr(syntax, "lineno", 1))),
                ast.get_docstring(syntax, clean=True) or "",
            )
            nodes[node.node_id] = node
            ast_symbols[node.node_id] = syntax
            self._collect_definitions(item, syntax.body, node, nodes, ast_symbols, limits)

    def _collect_defines(
        self,
        nodes: Mapping[str, LogicNode],
        module_ref: str,
        edges: list[LogicEdge],
        limits: list[str],
    ) -> None:
        module = nodes[module_ref]
        candidates = [node for node in nodes.values() if node.path == module.path and node.node_id != module_ref]
        by_qualname = {node.qualname: node for node in candidates}
        for node in candidates:
            parent_qualname = node.qualname.rsplit(".", 1)[0]
            parent = by_qualname.get(parent_qualname, module)
            self._add_edge(
                edges,
                "defines",
                parent.node_id,
                node.node_id,
                node.path,
                node.start_line,
                node.start_line,
                1.0,
                limits,
            )

    def _import_aliases(
        self,
        item: _ParsedFile,
        module_nodes: Mapping[str, str],
        lookup: Mapping[str, list[str]],
        edges: list[LogicEdge],
        nodes: Mapping[str, LogicNode],
        limits: list[str],
        unknowns: list[Mapping[str, Any]],
    ) -> dict[str, str]:
        aliases: dict[str, str] = {}
        module_ref = module_nodes[item.module]
        for syntax in item.tree.body:
            if isinstance(syntax, ast.Import):
                for alias in syntax.names:
                    target_ref = module_nodes.get(alias.name)
                    local_name = alias.asname or alias.name.split(".", 1)[0]
                    if target_ref is not None:
                        aliases[local_name] = target_ref
                        self._add_edge(edges, "imports", module_ref, target_ref, item.path, syntax.lineno, getattr(syntax, "end_lineno", syntax.lineno), 1.0, limits)
                    elif len(unknowns) < MAX_UNKNOWN_ITEMS:
                        unknowns.append({"code": "external_import_not_indexed", "path": item.path, "line": syntax.lineno, "name": alias.name})
            elif isinstance(syntax, ast.ImportFrom):
                base = self._absolute_import_module(
                    item.module,
                    syntax.module or "",
                    syntax.level,
                    is_package=PurePosixPath(item.path).name in {"__init__.py", "__init__.pyi"},
                )
                target_module = module_nodes.get(base)
                if target_module is not None:
                    self._add_edge(edges, "imports", module_ref, target_module, item.path, syntax.lineno, getattr(syntax, "end_lineno", syntax.lineno), 1.0, limits)
                for alias in syntax.names:
                    if alias.name == "*":
                        if len(unknowns) < MAX_UNKNOWN_ITEMS:
                            unknowns.append({"code": "wildcard_import_unresolved", "path": item.path, "line": syntax.lineno, "name": base})
                        continue
                    local_name = alias.asname or alias.name
                    refs = lookup.get(f"{base}.{alias.name}", [])
                    if len(refs) == 1:
                        aliases[local_name] = refs[0]
                    elif target_module is not None and alias.name == "__init__":
                        aliases[local_name] = target_module
        return aliases

    @staticmethod
    def _absolute_import_module(current: str, module: str, level: int, *, is_package: bool) -> str:
        if level <= 0:
            return module
        parts = current.split(".")
        package = parts if is_package else parts[:-1]
        keep = max(0, len(package) - (level - 1))
        prefix = package[:keep]
        if module:
            prefix.extend(module.split("."))
        return ".".join(prefix)

    def _resolve_call(
        self,
        expression: ast.expr,
        caller: LogicNode,
        aliases: Mapping[str, str],
        lookup: Mapping[str, list[str]],
        nodes: Mapping[str, LogicNode],
    ) -> tuple[str | None, str]:
        candidates: list[str] = []
        if isinstance(expression, ast.Name):
            if expression.id in aliases:
                return aliases[expression.id], "resolved_import_alias"
            candidates.extend(lookup.get(f"{caller.module}.{expression.id}", ()))
            if caller.kind in {"method", "function"}:
                parent = caller.qualname.rsplit(".", 1)[0]
                candidates.extend(lookup.get(f"{parent}.{expression.id}", ()))
        elif isinstance(expression, ast.Attribute) and isinstance(expression.value, ast.Name):
            base = expression.value.id
            if base in {"self", "cls"} and caller.kind == "method":
                class_name = caller.qualname.rsplit(".", 1)[0]
                candidates.extend(lookup.get(f"{class_name}.{expression.attr}", ()))
            elif base in aliases:
                target = nodes[aliases[base]]
                candidates.extend(lookup.get(f"{target.qualname}.{expression.attr}", ()))
        if isinstance(expression, ast.Attribute) and not candidates:
            # Chained attributes often represent typed service or adapter
            # calls (for example ``self.service.run_query``).  Without a
            # complete type engine we accept the final method only when its
            # identity is globally unique in this frozen snapshot.  Ambiguity
            # remains an explicit unknown rather than a ranking decision.
            candidates.extend(
                node.node_id
                for node in nodes.values()
                if node.name == expression.attr and node.kind in {"function", "method"}
            )
        unique = tuple(dict.fromkeys(candidates))
        if len(unique) == 1:
            return unique[0], "resolved"
        if len(unique) > 1:
            return None, "call_target_ambiguous"
        return None, "dynamic_or_external_call_unresolved"

    def _add_edge(
        self,
        edges: list[LogicEdge],
        kind: str,
        source: str,
        target: str,
        path: str,
        start: int,
        end: int,
        confidence: float,
        limits: list[str],
    ) -> None:
        if len(edges) >= self.policy.max_index_edges:
            limits.append("index_edge_budget_exhausted")
            return
        if kind not in SUPPORTED_EDGE_KINDS or source == target:
            return
        evidence = _source_ref(path, max(1, int(start)), max(1, int(end)))
        edge = LogicEdge(
            edge_id=_stable_id("logic_edge", kind, source, target, path, str(start), str(end)),
            kind=kind,
            source_ref=source,
            target_ref=target,
            path=path,
            start_line=max(1, int(start)),
            end_line=max(max(1, int(start)), int(end)),
            confidence=confidence,
            evidence_refs=(evidence,),
        )
        edges.append(edge)

    @staticmethod
    def _target_candidates(target: str, nodes: Mapping[str, LogicNode]) -> tuple[LogicNode, ...]:
        normalized = target.replace("\\", "/").strip().rstrip("/")
        tiers: list[list[LogicNode]] = [[], [], [], [], []]
        for node in nodes.values():
            if normalized == node.node_id:
                tiers[0].append(node)
            elif normalized == node.path or normalized == node.qualname:
                tiers[1].append(node)
            elif normalized == f"{node.path}:{node.qualname}" or normalized == f"{node.path}::{node.qualname}":
                tiers[2].append(node)
            elif node.qualname.endswith(f".{normalized}"):
                tiers[3].append(node)
            elif normalized == node.name:
                tiers[4].append(node)
        for tier in tiers:
            if tier:
                return tuple(sorted(tier, key=lambda item: (item.path, item.start_line, item.qualname, item.kind)))
        return ()

    @staticmethod
    def _resolution(target: str, candidates: Sequence[LogicNode]) -> Mapping[str, Any]:
        return {
            "target": target,
            "status": "resolved" if len(candidates) == 1 else ("ambiguous" if candidates else "unresolved"),
            "candidate_refs": [item.node_id for item in candidates],
            "candidate_count": len(candidates),
        }

    @staticmethod
    def _adjacency(edges: Iterable[LogicEdge], *, reverse: bool = False) -> Mapping[str, tuple[tuple[str, LogicEdge], ...]]:
        table: dict[str, list[tuple[str, LogicEdge]]] = defaultdict(list)
        for edge in edges:
            origin, neighbor = (edge.target_ref, edge.source_ref) if reverse else (edge.source_ref, edge.target_ref)
            table[origin].append((neighbor, edge))
        return {
            key: tuple(sorted(value, key=lambda item: (item[1].kind, item[0], item[1].edge_id)))
            for key, value in table.items()
        }

    def _local_graph(
        self,
        start_refs: Sequence[str],
        index: _Index,
        query: LogicQuery,
        *,
        reverse: bool,
        edge_kinds: frozenset[str] | None = None,
    ) -> tuple[tuple[LogicNode, ...], tuple[LogicEdge, ...], tuple[str, ...]]:
        selected_nodes: dict[str, LogicNode] = {}
        selected_edges: dict[str, LogicEdge] = {}
        limits: list[str] = []
        adjacency = self._adjacency(index.edges, reverse=reverse)
        queue: deque[tuple[str, int]] = deque((ref, 0) for ref in start_refs)
        seen: set[str] = set()
        while queue:
            ref, depth = queue.popleft()
            if ref in seen:
                continue
            seen.add(ref)
            node = index.nodes.get(ref)
            if node is None:
                continue
            if len(selected_nodes) >= query.max_nodes:
                limits.append("report_node_budget_exhausted")
                break
            selected_nodes[ref] = node
            neighbors = adjacency.get(ref, ())
            if depth >= query.max_depth:
                if any(edge_kinds is None or edge.kind in edge_kinds for _, edge in neighbors):
                    limits.append("report_depth_budget_exhausted")
                continue
            for neighbor, edge in neighbors:
                if edge_kinds is not None and edge.kind not in edge_kinds:
                    continue
                if len(selected_edges) >= query.max_edges:
                    limits.append("report_edge_budget_exhausted")
                    break
                selected_edges[edge.edge_id] = edge
                queue.append((neighbor, depth + 1))
            if "report_edge_budget_exhausted" in limits:
                break
        nodes = tuple(sorted(selected_nodes.values(), key=lambda item: (item.path, item.start_line, item.qualname)))
        edges = tuple(sorted(selected_edges.values(), key=lambda item: (item.kind, item.path, item.start_line, item.edge_id)))
        return nodes, edges, tuple(dict.fromkeys(limits))

    def _module_responsibility(self, query: LogicQuery, index: _Index) -> dict[str, Any]:
        candidates = self._target_candidates(query.target, index.nodes)
        resolution = self._resolution(query.target, candidates)
        if len(candidates) != 1:
            status = "target_ambiguous" if candidates else "target_unresolved"
            return self._unresolved_result(query, index, resolution, status)
        focus = candidates[0]
        outgoing_nodes, outgoing_edges, out_limits = self._local_graph((focus.node_id,), index, query, reverse=False)
        incoming_nodes, incoming_edges, in_limits = self._local_graph((focus.node_id,), index, query, reverse=True)
        nodes_by_id = {item.node_id: item for item in (*outgoing_nodes, *incoming_nodes)}
        edges_by_id = {item.edge_id: item for item in (*outgoing_edges, *incoming_edges)}
        nodes, edges, clipped = self._clip_graph(nodes_by_id.values(), edges_by_id.values(), query, focus.node_id)
        limits = tuple(dict.fromkeys((*out_limits, *in_limits, *clipped)))
        suspected = self._suspected_orphans(nodes, index.edges)
        unknowns = self._related_unknowns(index, {node.node_id for node in nodes})
        completeness = self._completeness(index, limits, unknowns)
        dependencies = sum(1 for edge in edges if edge.source_ref == focus.node_id and edge.kind != "defines")
        consumers = sum(1 for edge in edges if edge.target_ref == focus.node_id and edge.kind in {"calls", "tests", "imports"})
        return {
            "status": "success",
            "summary": f"{focus.qualname}：静态观察到 {dependencies} 个直接依赖方向、{consumers} 个直接消费者方向；运行入口与动态调用仍需证据。",
            "resolved_targets": (resolution,),
            "nodes": nodes,
            "edges": edges,
            "paths": (),
            "first_break": None,
            "suspected_orphans": suspected,
            "unknowns": unknowns,
            "completeness": completeness,
            "limits_reached": limits,
            "next_check": "查看直接消费者和相关测试；需要运行结论时补充真实 trace/readback。",
        }

    def _change_impact(self, query: LogicQuery, index: _Index) -> dict[str, Any]:
        candidates = self._target_candidates(query.target, index.nodes)
        resolution = self._resolution(query.target, candidates)
        if len(candidates) != 1:
            status = "target_ambiguous" if candidates else "target_unresolved"
            return self._unresolved_result(query, index, resolution, status)
        focus = candidates[0]
        nodes, edges, limits = self._local_graph(
            (focus.node_id,),
            index,
            query,
            reverse=True,
            edge_kinds=frozenset({"imports", "calls", "tests"}),
        )
        paths = self._paths_from_focus(focus.node_id, nodes, edges, reverse=True)
        unknowns = self._related_unknowns(index, {node.node_id for node in nodes})
        completeness = self._completeness(index, limits, unknowns)
        affected = max(0, len(nodes) - 1)
        qualifier = "至少" if completeness == "search_incomplete" else ""
        return {
            "status": "success",
            "summary": f"对 {focus.qualname} 的修改在当前静态边界内有 {qualifier}{affected} 个潜在消费者；这不是运行故障或完整 blast radius。",
            "resolved_targets": (resolution,),
            "nodes": nodes,
            "edges": edges,
            "paths": paths,
            "first_break": None,
            "suspected_orphans": (),
            "unknowns": unknowns,
            "completeness": completeness,
            "limits_reached": limits,
            "next_check": "优先运行最靠近目标的真实消费者或测试，并把 readback 与本报告 snapshot 对齐。",
        }

    def _first_broken_link(self, query: LogicQuery, index: _Index) -> dict[str, Any]:
        resolutions: list[Mapping[str, Any]] = []
        refs: list[str] = []
        first_break: Mapping[str, Any] | None = None
        for ordinal, target in enumerate(query.expected_path):
            candidates = self._target_candidates(target, index.nodes)
            resolution = self._resolution(target, candidates)
            resolutions.append(resolution)
            if len(candidates) != 1:
                code = "target_ambiguous" if candidates else "target_unresolved"
                first_break = {
                    "status": code,
                    "segment_index": ordinal,
                    "from_target": query.expected_path[ordinal - 1] if ordinal else None,
                    "to_target": target,
                    "meaning": "下一步应先消除目标身份歧义，尚不能判定运行 Bug。",
                }
                break
            refs.append(candidates[0].node_id)
        paths: list[Mapping[str, Any]] = []
        used_edges: dict[str, LogicEdge] = {}
        if first_break is None:
            for ordinal, (source, target) in enumerate(zip(refs, refs[1:])):
                path, exhausted = self._shortest_path(source, target, index.edges, query.max_depth)
                if path is None:
                    code = "search_incomplete" if exhausted or index.limits_reached else "static_link_unobserved"
                    first_break = {
                        "status": code,
                        "segment_index": ordinal,
                        "from_target": query.expected_path[ordinal],
                        "to_target": query.expected_path[ordinal + 1],
                        "meaning": (
                            "搜索预算已耗尽，不能判断该段是否存在。"
                            if code == "search_incomplete"
                            else "当前 Python 静态视图未观察到支持路径；动态调用、外部入口和运行事实仍未知，不能据此判定 Bug。"
                        ),
                    }
                    break
                for edge in path:
                    used_edges[edge.edge_id] = edge
                paths.append(
                    {
                        "from_ref": source,
                        "to_ref": target,
                        "edge_refs": [edge.edge_id for edge in path],
                        "status": "static_path_observed",
                    }
                )
        node_ids = set(refs)
        for edge in used_edges.values():
            node_ids.add(edge.source_ref)
            node_ids.add(edge.target_ref)
        ordered_nodes = tuple(sorted((index.nodes[ref] for ref in node_ids), key=lambda item: (item.path, item.start_line, item.qualname)))
        ordered_edges = tuple(sorted(used_edges.values(), key=lambda item: (item.path, item.start_line, item.edge_id)))
        nodes, edges, clipped = self._clip_graph(ordered_nodes, ordered_edges, query, refs[0] if refs else None)
        limits = tuple(dict.fromkeys(clipped))
        related = self._related_unknowns(index, {node.node_id for node in nodes})
        unknowns = list(related)
        if first_break is not None:
            unknowns.insert(0, {"code": first_break["status"], "meaning": first_break["meaning"]})
        status = "static_chain_observed" if first_break is None else str(first_break["status"])
        completeness = self._completeness(index, limits, tuple(unknowns))
        summary = (
            "给定路径在当前 Python 静态视图中逐段可达；这不证明运行成功。"
            if first_break is None
            else f"首个需要核查的位置：{first_break['from_target'] or '起点'} → {first_break['to_target']}（{first_break['status']}）。"
        )
        return {
            "status": status,
            "summary": summary,
            "resolved_targets": tuple(resolutions),
            "nodes": nodes,
            "edges": edges,
            "paths": tuple(paths),
            "first_break": first_break,
            "suspected_orphans": (),
            "unknowns": tuple(unknowns[:MAX_UNKNOWN_ITEMS]),
            "completeness": completeness,
            "limits_reached": limits,
            "next_check": (
                "对该段补充唯一符号、运行 trace、测试或 connector readback。"
                if first_break is not None
                else "继续用真实 episode/readback 核对静态链中的执行一致性和经验真实性。"
            ),
        }

    @staticmethod
    def _shortest_path(
        source: str,
        target: str,
        edges: Sequence[LogicEdge],
        max_depth: int,
    ) -> tuple[tuple[LogicEdge, ...] | None, bool]:
        adjacency = LogicFieldInstrument._adjacency(edges)
        queue: deque[tuple[str, tuple[LogicEdge, ...]]] = deque(((source, ()),))
        seen = {source}
        exhausted = False
        while queue:
            ref, path = queue.popleft()
            if len(path) >= max_depth:
                if adjacency.get(ref):
                    exhausted = True
                continue
            for neighbor, edge in adjacency.get(ref, ()):
                next_path = (*path, edge)
                if neighbor == target:
                    return next_path, exhausted
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append((neighbor, next_path))
        return None, exhausted

    @staticmethod
    def _paths_from_focus(
        focus_ref: str,
        nodes: Sequence[LogicNode],
        edges: Sequence[LogicEdge],
        *,
        reverse: bool,
    ) -> tuple[Mapping[str, Any], ...]:
        allowed = {node.node_id for node in nodes}
        adjacency = LogicFieldInstrument._adjacency(edges, reverse=reverse)
        queue: deque[tuple[str, tuple[str, ...]]] = deque(((focus_ref, ()),))
        seen = {focus_ref}
        paths: list[Mapping[str, Any]] = []
        while queue:
            ref, edge_refs = queue.popleft()
            for neighbor, edge in adjacency.get(ref, ()):
                if neighbor not in allowed or neighbor in seen:
                    continue
                seen.add(neighbor)
                next_refs = (*edge_refs, edge.edge_id)
                paths.append(
                    {
                        "from_ref": focus_ref,
                        "to_ref": neighbor,
                        "edge_refs": list(next_refs),
                        "status": "potential_static_impact",
                    }
                )
                queue.append((neighbor, next_refs))
        return tuple(paths)

    @staticmethod
    def _clip_graph(
        nodes: Iterable[LogicNode],
        edges: Iterable[LogicEdge],
        query: LogicQuery,
        focus_ref: str | None,
    ) -> tuple[tuple[LogicNode, ...], tuple[LogicEdge, ...], tuple[str, ...]]:
        ordered_nodes = sorted(nodes, key=lambda item: (0 if item.node_id == focus_ref else 1, item.path, item.start_line, item.qualname))
        limits: list[str] = []
        if len(ordered_nodes) > query.max_nodes:
            ordered_nodes = ordered_nodes[: query.max_nodes]
            limits.append("report_node_budget_exhausted")
        allowed = {item.node_id for item in ordered_nodes}
        ordered_edges = sorted(
            (edge for edge in edges if edge.source_ref in allowed and edge.target_ref in allowed),
            key=lambda item: (item.kind, item.path, item.start_line, item.edge_id),
        )
        if len(ordered_edges) > query.max_edges:
            ordered_edges = ordered_edges[: query.max_edges]
            limits.append("report_edge_budget_exhausted")
        return tuple(ordered_nodes), tuple(ordered_edges), tuple(limits)

    @staticmethod
    def _suspected_orphans(
        nodes: Sequence[LogicNode],
        all_edges: Sequence[LogicEdge],
    ) -> tuple[Mapping[str, Any], ...]:
        incoming = defaultdict(int)
        for edge in all_edges:
            if edge.kind in {"calls", "tests", "imports"}:
                incoming[edge.target_ref] += 1
        items: list[Mapping[str, Any]] = []
        for node in nodes:
            if node.kind not in {"function", "method", "class"} or node.name.startswith("_"):
                continue
            if incoming[node.node_id] == 0:
                items.append(
                    {
                        "node_ref": node.node_id,
                        "status": "no_static_consumer_observed",
                        "meaning": "疑似孤立；CLI、路由、插件、反射、外部调用和未扫描语言仍可能使用它，不能据此删除。",
                    }
                )
        return tuple(items[:16])

    @staticmethod
    def _related_unknowns(index: _Index, refs: set[str]) -> tuple[Mapping[str, Any], ...]:
        related = [item for item in index.unknowns if item.get("source_ref") in refs]
        if index.snapshot.unsupported_source_files:
            related.append(
                {
                    "code": "unsupported_source_languages_present",
                    "count": index.snapshot.unsupported_source_files,
                    "meaning": "JavaScript/TypeScript 等文件尚未进入本次 Python 静态图。",
                }
            )
        related.extend(index.snapshot.failed_files)
        return tuple(related[:MAX_UNKNOWN_ITEMS])

    @staticmethod
    def _completeness(
        index: _Index,
        limits: Sequence[str],
        unknowns: Sequence[Mapping[str, Any]],
    ) -> str:
        if index.limits_reached or limits:
            return "search_incomplete"
        if index.snapshot.completeness != "complete" or unknowns:
            return "partial"
        return "complete"

    def _unresolved_result(
        self,
        query: LogicQuery,
        index: _Index,
        resolution: Mapping[str, Any],
        status: str,
    ) -> dict[str, Any]:
        candidates = [index.nodes[ref] for ref in resolution["candidate_refs"][: query.max_nodes]]
        meaning = (
            "目标匹配到多个符号，请使用相对路径或完整限定名缩小范围。"
            if status == "target_ambiguous"
            else "目标未在当前 Python snapshot 中解析；请核对名称、相对路径或支持语言。"
        )
        return {
            "status": status,
            "summary": meaning,
            "resolved_targets": (resolution,),
            "nodes": tuple(candidates),
            "edges": (),
            "paths": (),
            "first_break": {"status": status, "segment_index": 0, "from_target": None, "to_target": query.target, "meaning": meaning},
            "suspected_orphans": (),
            "unknowns": ({"code": status, "meaning": meaning},),
            "completeness": "partial",
            "limits_reached": (),
            "next_check": meaning,
        }


__all__ = [
    "EXTRACTOR_VERSION",
    "LOGIC_FIELD_VERSION",
    "LogicEdge",
    "LogicFieldInstrument",
    "LogicFieldPolicy",
    "LogicFieldReport",
    "LogicNode",
    "LogicQuery",
    "SourceSnapshot",
]
