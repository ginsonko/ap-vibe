"""Versioned local recovery knowledge for the AP-Vibe environment.

This module owns one deliberately narrow responsibility: turning a
source-grounded ``ProjectKnowledgeProposal`` into a reviewed, append-only
local recovery milestone.  Browser payloads carry only identities; the
proposal itself must be resolved from the durable episode registry by the
service before it reaches this module.

Knowledge review is an ordinary AP episode.  The environment exposes commit,
defer, and observe affordances, while ``MindRuntime`` remains the only action
arena.  A revision is appended only after the selected commit action has a
physical dispatch/readback and that result re-enters the next cognitive tick.
The local revision is not a Vibe control-plane write.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from .contracts import ContractError, DispatchReceipt, EventEnvelope, ResultEvent, utc_now
from .environment import EnvironmentAdapter
from .gateway import NullGateway
from .runtime import MindRuntime
from .runtime_types import ActionCandidate, TickResult
from .storage import EventStore
from .vibe_mind import ProjectKnowledgeProposal


KNOWLEDGE_SECTIONS = (
    "identity",
    "status",
    "work",
    "architecture",
    "requirements",
    "decisions",
    "dependencies",
    "evidence",
    "risks",
    "sources",
    "recovery",
)
MIN_BRIEF_CHARS = 2_000
DEFAULT_BRIEF_CHARS = 8_000
MAX_BRIEF_CHARS = 24_000
MAX_PENDING_PROPOSALS = 24
MAX_SECTION_ITEMS = 64
MAX_BRIEF_ITEMS = 16
MAX_BRIEF_ITEM_CHARS = 480


class KnowledgeRevisionConflict(ContractError):
    """The requested parent is no longer the current reviewed revision."""


def _text(value: Any, name: str, *, limit: int = 2_048, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{name}_must_be_text")
    if not allow_empty and not value.strip():
        raise ContractError(f"{name}_must_not_be_empty")
    if len(value) > limit:
        raise ContractError(f"{name}_exceeds_bound")
    return value


def _optional_text(value: Any, name: str, *, limit: int = 2_048) -> str | None:
    if value is None:
        return None
    return _text(value, name, limit=limit)


def _texts(value: Any, name: str, *, max_items: int = MAX_SECTION_ITEMS) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractError(f"{name}_must_be_a_sequence")
    if len(value) > max_items:
        raise ContractError(f"{name}_exceeds_bound")
    return tuple(_text(item, f"{name}_item", limit=12_000) for item in value)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContractError(f"{name}_must_be_an_object")
    try:
        copied = json.loads(json.dumps(dict(value), ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_must_be_json") from exc
    if not isinstance(copied, dict):
        raise ContractError(f"{name}_must_be_an_object")
    return copied


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def _bounded_union(*groups: Sequence[str], limit: int = MAX_SECTION_ITEMS) -> tuple[str, ...]:
    output: list[str] = []
    for group in groups:
        for item in group:
            if isinstance(item, str) and item and item not in output:
                output.append(item)
                if len(output) >= limit:
                    return tuple(output)
    return tuple(output)


def _instant(value: Any, name: str) -> datetime:
    text = _text(value, name, limit=128)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_must_be_iso8601") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{name}_must_include_timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class KnowledgeReviewRequest:
    decision_id: str
    project_id: str
    proposal_id: str
    source_ref: str
    rationale: str = "用户要求把这条待审候选设为本地恢复基线"
    expected_parent_revision: str | None = None
    created_at: str = field(default_factory=utc_now)
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("decision_id", "project_id", "proposal_id", "source_ref"):
            _text(getattr(self, name), name, limit=512)
        _text(self.rationale, "rationale", limit=4_096)
        _optional_text(self.expected_parent_revision, "expected_parent_revision", limit=512)
        _text(self.created_at, "created_at", limit=128)
        _mapping(self.extra, "extra")

    def to_dict(self) -> dict[str, Any]:
        base = {
            "decision_id": self.decision_id,
            "project_id": self.project_id,
            "proposal_id": self.proposal_id,
            "source_ref": self.source_ref,
            "rationale": self.rationale,
            "expected_parent_revision": self.expected_parent_revision,
            "created_at": self.created_at,
        }
        for key, value in _mapping(self.extra, "extra").items():
            if key not in base:
                base[key] = value
        return base

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "KnowledgeReviewRequest":
        if not isinstance(raw, Mapping):
            raise ContractError("knowledge_review_must_be_an_object")
        names = {
            "decision_id", "project_id", "proposal_id", "source_ref",
            "rationale", "expected_parent_revision", "created_at", "extra",
        }
        extra = _mapping(raw.get("extra"), "extra")
        for key, value in raw.items():
            if key not in names:
                extra.setdefault(str(key), value)
        values = {key: raw[key] for key in names if key in raw and key != "extra"}
        values["extra"] = extra
        return cls(**values)

    def as_event(
        self,
        proposal: ProjectKnowledgeProposal,
        *,
        runtime_id: str,
        organism_id: str,
        episode_id: str,
    ) -> EventEnvelope:
        if proposal.project_id != self.project_id or proposal.proposal_id != self.proposal_id:
            raise ContractError("knowledge_review_proposal_identity_mismatch")
        return EventEnvelope(
            event_id=_stable_id("evt", self.project_id, self.decision_id, "knowledge_review"),
            runtime_id=runtime_id,
            organism_id=organism_id,
            environment_id=f"ap-vibe:{self.project_id}",
            subject_scope="project",
            episode_id=episode_id,
            source="user_feedback",
            role="command",
            modality="project_knowledge_review",
            occurred_at=self.created_at,
            observed_at=utc_now(),
            payload_inline={"review": self.to_dict(), "proposal": proposal.to_dict()},
            evidence_refs=tuple(dict.fromkeys((self.source_ref, *proposal.evidence_refs))),
            lineage_refs=tuple(dict.fromkeys((proposal.proposal_id, *proposal.source_refs))),
            privacy_scope="project",
            completeness="complete",
            idempotency_key=f"ap-vibe-knowledge-review:{self.project_id}:{self.decision_id}",
            extra={"gateway_consultation_allowed": False, "knowledge_control_event": True},
        )


@dataclass(frozen=True)
class LocalKnowledgeRevision:
    revision_id: str
    project_id: str
    revision_number: int
    decision_id: str
    parent_revision_id: str | None
    parent_hash: str | None
    content_hash: str
    included_proposal_refs: tuple[str, ...]
    sections: Mapping[str, Any]
    source_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    created_at: str
    authority: str = "local_reviewed_recovery"

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "project_id": self.project_id,
            "revision_number": self.revision_number,
            "decision_id": self.decision_id,
            "parent_revision_id": self.parent_revision_id,
            "parent_hash": self.parent_hash,
            "content_hash": self.content_hash,
            "included_proposal_refs": list(self.included_proposal_refs),
            "sections": _mapping(self.sections, "sections"),
            "source_refs": list(self.source_refs),
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
            "authority": self.authority,
            "vibe_formal_write": False,
        }


@dataclass(frozen=True)
class KnowledgeReviewRun:
    review: KnowledgeReviewRequest
    proposal: ProjectKnowledgeProposal
    episode_id: str
    results: tuple[TickResult, ...]
    revision: LocalKnowledgeRevision | None
    provider_mode: str = "provider_off"


def _content_hash(
    *,
    project_id: str,
    revision_number: int,
    parent_revision_id: str | None,
    parent_hash: str | None,
    included_proposal_refs: Sequence[str],
    sections: Mapping[str, Any],
    authority: str = "local_reviewed_recovery",
) -> str:
    value = {
        "project_id": project_id,
        "revision_number": int(revision_number),
        "parent_revision_id": parent_revision_id,
        "parent_hash": parent_hash,
        "included_proposal_refs": list(included_proposal_refs),
        "sections": dict(sections),
        "authority": authority,
    }
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest().upper()


def _merge_sections(
    project_id: str,
    proposal: ProjectKnowledgeProposal,
    parent: LocalKnowledgeRevision | None,
    created_at: str,
) -> dict[str, Any]:
    sections = {key: {} for key in KNOWLEDGE_SECTIONS}
    if parent is not None:
        previous = _mapping(parent.sections, "parent_sections")
        for key in KNOWLEDGE_SECTIONS:
            value = previous.get(key)
            sections[key] = json.loads(json.dumps(value, ensure_ascii=False)) if value is not None else {}

    if proposal.section_changes:
        # A correction is a declared local patch, not a new activity snapshot.
        # Running the legacy merge first would silently replace unrelated
        # status fields with the user's instruction text.
        from .knowledge_correction import apply_section_changes

        return apply_section_changes(sections, proposal.section_changes)

    prior_work = sections.get("work") if isinstance(sections.get("work"), Mapping) else {}
    prior_requirements = sections.get("requirements") if isinstance(sections.get("requirements"), Mapping) else {}
    prior_sources = sections.get("sources") if isinstance(sections.get("sources"), Mapping) else {}
    prior_remaining = tuple(prior_work.get("remaining", ()))
    prior_unknown = tuple(prior_work.get("unknown", ()))
    requested_resolved_remaining = set(proposal.resolved_remaining)
    requested_resolved_unknown = set(proposal.resolved_unknown)
    resolved_remaining = tuple(item for item in prior_remaining if item in requested_resolved_remaining)
    resolved_unknown = tuple(item for item in prior_unknown if item in requested_resolved_unknown)
    completed = _bounded_union(tuple(prior_work.get("completed", ())), proposal.completed)
    remaining = tuple(
        item
        for item in _bounded_union(prior_remaining, proposal.remaining)
        if item not in completed and item not in requested_resolved_remaining
    )
    unknown = tuple(
        item
        for item in _bounded_union(prior_unknown, proposal.unknown)
        if item not in requested_resolved_unknown
    )
    redlines = _bounded_union(tuple(prior_requirements.get("redlines", ())), proposal.redlines)
    source_refs = _bounded_union(tuple(prior_sources.get("source_refs", ())), proposal.source_refs)
    evidence_refs = _bounded_union(tuple(prior_sources.get("evidence_refs", ())), proposal.evidence_refs)
    next_action = proposal.next_action or prior_work.get("next_action")

    sections["identity"] = {
        "project_id": project_id,
        "authority": "local_reviewed_recovery",
        "vibe_formal_write": False,
    }
    sections["status"] = {
        "summary": proposal.summary,
        "source_completeness": proposal.extra.get("source_completeness", "unknown"),
        "confidence": proposal.confidence,
        "uncertainty": proposal.uncertainty,
        "conflicts": list(proposal.conflicts),
        "resolved_remaining": list(resolved_remaining),
        "resolved_unknown": list(resolved_unknown),
        "reviewed_at": created_at,
    }
    sections["work"] = {
        "completed": list(completed),
        "remaining": list(remaining),
        "unknown": list(unknown),
        "resolved_remaining": list(resolved_remaining),
        "resolved_unknown": list(resolved_unknown),
        "next_action": next_action,
    }
    sections["requirements"] = {**dict(prior_requirements), "redlines": list(redlines)}
    sections["sources"] = {
        **dict(prior_sources),
        "source_refs": list(source_refs),
        "evidence_refs": list(evidence_refs),
    }
    sections["recovery"] = {
        "summary": proposal.summary,
        "completed": list(completed),
        "remaining": list(remaining),
        "unknown": list(unknown),
        "resolved_remaining": list(resolved_remaining),
        "resolved_unknown": list(resolved_unknown),
        "redlines": list(redlines),
        "next_action": next_action,
        "source_refs": list(source_refs),
        "evidence_refs": list(evidence_refs),
        "source_completeness": proposal.extra.get("source_completeness", "unknown"),
    }
    return sections


class ProjectKnowledgeStore:
    """Append-only, hash-chained local recovery knowledge."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS project_knowledge_revisions (
                    revision_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    revision_number INTEGER NOT NULL,
                    decision_id TEXT NOT NULL UNIQUE,
                    proposal_id TEXT NOT NULL,
                    parent_revision_id TEXT,
                    parent_hash TEXT,
                    content_hash TEXT NOT NULL,
                    included_proposals_json TEXT NOT NULL,
                    sections_json TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    UNIQUE(project_id, revision_number)
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_project_knowledge_latest
                   ON project_knowledge_revisions(project_id, revision_number DESC)"""
            )
            connection.commit()

    @staticmethod
    def _row(row: sqlite3.Row) -> LocalKnowledgeRevision:
        return LocalKnowledgeRevision(
            revision_id=str(row["revision_id"]),
            project_id=str(row["project_id"]),
            revision_number=int(row["revision_number"]),
            decision_id=str(row["decision_id"]),
            parent_revision_id=str(row["parent_revision_id"]) if row["parent_revision_id"] is not None else None,
            parent_hash=str(row["parent_hash"]) if row["parent_hash"] is not None else None,
            content_hash=str(row["content_hash"]),
            included_proposal_refs=_texts(
                json.loads(str(row["included_proposals_json"])),
                "included_proposal_refs",
                max_items=1_024,
            ),
            sections=_mapping(json.loads(str(row["sections_json"])), "sections"),
            source_refs=_texts(json.loads(str(row["source_refs_json"])), "source_refs"),
            evidence_refs=_texts(json.loads(str(row["evidence_refs_json"])), "evidence_refs"),
            created_at=str(row["created_at"]),
            authority=str(row["authority"]),
        )

    def latest(self, project_id: str) -> LocalKnowledgeRevision | None:
        _text(project_id, "project_id", limit=512)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM project_knowledge_revisions
                   WHERE project_id = ? ORDER BY revision_number DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
        return self._row(row) if row is not None else None

    def count(self, project_id: str) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM project_knowledge_revisions WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    def _validate_rows(
        self, rows: Sequence[sqlite3.Row]
    ) -> tuple[bool, str, LocalKnowledgeRevision | None]:
        parent: LocalKnowledgeRevision | None = None
        for expected_number, row in enumerate(rows, start=1):
            revision = self._row(row)
            if revision.revision_number != expected_number:
                return False, "revision_number_gap", parent
            if revision.parent_revision_id != (parent.revision_id if parent else None):
                return False, "parent_revision_mismatch", parent
            if revision.parent_hash != (parent.content_hash if parent else None):
                return False, "parent_hash_mismatch", parent
            computed = _content_hash(
                project_id=revision.project_id,
                revision_number=revision.revision_number,
                parent_revision_id=revision.parent_revision_id,
                parent_hash=revision.parent_hash,
                included_proposal_refs=revision.included_proposal_refs,
                sections=revision.sections,
                authority=revision.authority,
            )
            if computed != revision.content_hash:
                return False, "content_hash_mismatch", parent
            parent = revision
        return True, "revision_chain_valid" if parent else "no_local_milestone", parent

    def validate(self, project_id: str) -> tuple[bool, str, LocalKnowledgeRevision | None]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM project_knowledge_revisions
                   WHERE project_id = ? ORDER BY revision_number ASC""",
                (project_id,),
            ).fetchall()
        return self._validate_rows(rows)

    def commit(
        self,
        review: KnowledgeReviewRequest,
        proposal: ProjectKnowledgeProposal,
    ) -> LocalKnowledgeRevision:
        if review.project_id != proposal.project_id or review.proposal_id != proposal.proposal_id:
            raise ContractError("knowledge_commit_identity_mismatch")
        if proposal.status != "staged":
            raise ContractError("knowledge_commit_requires_staged_proposal")
        if proposal.conflicts:
            raise KnowledgeRevisionConflict("knowledge_commit_conflicts_unresolved")

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM project_knowledge_revisions WHERE decision_id = ?",
                (review.decision_id,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._row(existing)
            parent_row = connection.execute(
                """SELECT * FROM project_knowledge_revisions
                   WHERE project_id = ? ORDER BY revision_number DESC LIMIT 1""",
                (review.project_id,),
            ).fetchone()
            parent = self._row(parent_row) if parent_row is not None else None
            current_parent = parent.revision_id if parent else None
            if review.expected_parent_revision != current_parent:
                connection.rollback()
                raise KnowledgeRevisionConflict("knowledge_parent_revision_changed")

            created_at = utc_now()
            revision_number = (parent.revision_number + 1) if parent else 1
            included = _bounded_union(
                parent.included_proposal_refs if parent else (),
                (proposal.proposal_id,),
                limit=1_024,
            )
            sections = _merge_sections(review.project_id, proposal, parent, created_at)
            parent_hash = parent.content_hash if parent else None
            content_hash = _content_hash(
                project_id=review.project_id,
                revision_number=revision_number,
                parent_revision_id=current_parent,
                parent_hash=parent_hash,
                included_proposal_refs=included,
                sections=sections,
                authority="local_reviewed_recovery",
            )
            revision = LocalKnowledgeRevision(
                revision_id=_stable_id("knowledge_revision", review.project_id, review.decision_id),
                project_id=review.project_id,
                revision_number=revision_number,
                decision_id=review.decision_id,
                parent_revision_id=current_parent,
                parent_hash=parent_hash,
                content_hash=content_hash,
                included_proposal_refs=included,
                sections=sections,
                source_refs=_bounded_union(parent.source_refs if parent else (), proposal.source_refs),
                evidence_refs=_bounded_union(parent.evidence_refs if parent else (), proposal.evidence_refs),
                created_at=created_at,
            )
            connection.execute(
                """INSERT INTO project_knowledge_revisions
                   (revision_id, project_id, revision_number, decision_id,
                    proposal_id, parent_revision_id, parent_hash, content_hash,
                    included_proposals_json, sections_json, source_refs_json,
                    evidence_refs_json, created_at, authority)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    revision.revision_id,
                    revision.project_id,
                    revision.revision_number,
                    revision.decision_id,
                    proposal.proposal_id,
                    revision.parent_revision_id,
                    revision.parent_hash,
                    revision.content_hash,
                    _canonical(list(revision.included_proposal_refs)),
                    _canonical(dict(revision.sections)),
                    _canonical(list(revision.source_refs)),
                    _canonical(list(revision.evidence_refs)),
                    revision.created_at,
                    revision.authority,
                ),
            )
            connection.commit()
        return revision

    def import_portable_revision(
        self,
        *,
        project_id: str,
        source_project_id: str,
        bundle_hash: str,
        sections: Mapping[str, Any],
        source_refs: Sequence[str] = (),
        evidence_refs: Sequence[str] = (),
    ) -> LocalKnowledgeRevision:
        """Restore reviewed knowledge into a new isolated project chain.

        This is an administrative recovery receipt, not a truth judgment and
        not an AP action.  It is intentionally limited to an empty target
        chain so imports cannot overwrite or merge an existing mind.
        """

        project_id = _text(project_id, "project_id", limit=512)
        source_project_id = _text(source_project_id, "source_project_id", limit=512)
        bundle_hash = _text(bundle_hash, "portable_bundle_hash", limit=64)
        if len(bundle_hash) != 64 or any(character not in "0123456789ABCDEF" for character in bundle_hash):
            raise ContractError("portable_bundle_hash_invalid")
        raw_sections = _mapping(sections, "portable_knowledge_sections")
        normalized_sections: dict[str, Any] = {}
        for key in KNOWLEDGE_SECTIONS:
            value = raw_sections.get(key, {})
            normalized_sections[key] = json.loads(json.dumps(value, ensure_ascii=False))
        identity = normalized_sections.get("identity")
        identity = dict(identity) if isinstance(identity, Mapping) else {}
        identity.update(
            {
                "project_id": project_id,
                "authority": "local_imported_recovery",
                "vibe_formal_write": False,
                "imported_from_project_id": source_project_id,
                "import_bundle_hash": bundle_hash,
            }
        )
        normalized_sections["identity"] = identity
        proposal_id = _stable_id("portable_import_proposal", project_id, bundle_hash)
        decision_id = _stable_id("portable_import_decision", project_id, bundle_hash)
        included = (proposal_id,)
        authority = "local_imported_recovery"
        created_at = utc_now()
        normalized_source_refs = _bounded_union(
            (f"portable-project://{bundle_hash}",),
            _texts(source_refs, "portable_source_refs"),
        )
        normalized_evidence_refs = _bounded_union(
            (f"portable-project://{bundle_hash}#content",),
            _texts(evidence_refs, "portable_evidence_refs"),
        )
        content_hash = _content_hash(
            project_id=project_id,
            revision_number=1,
            parent_revision_id=None,
            parent_hash=None,
            included_proposal_refs=included,
            sections=normalized_sections,
            authority=authority,
        )
        revision = LocalKnowledgeRevision(
            revision_id=_stable_id("knowledge_revision", project_id, decision_id),
            project_id=project_id,
            revision_number=1,
            decision_id=decision_id,
            parent_revision_id=None,
            parent_hash=None,
            content_hash=content_hash,
            included_proposal_refs=included,
            sections=normalized_sections,
            source_refs=normalized_source_refs,
            evidence_refs=normalized_evidence_refs,
            created_at=created_at,
            authority=authority,
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM project_knowledge_revisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._row(existing)
            target_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM project_knowledge_revisions WHERE project_id = ?",
                    (project_id,),
                ).fetchone()[0]
            )
            if target_count:
                connection.rollback()
                raise KnowledgeRevisionConflict("portable_import_requires_empty_project_chain")
            connection.execute(
                """INSERT INTO project_knowledge_revisions
                   (revision_id, project_id, revision_number, decision_id,
                    proposal_id, parent_revision_id, parent_hash, content_hash,
                    included_proposals_json, sections_json, source_refs_json,
                    evidence_refs_json, created_at, authority)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    revision.revision_id,
                    revision.project_id,
                    revision.revision_number,
                    revision.decision_id,
                    proposal_id,
                    None,
                    None,
                    revision.content_hash,
                    _canonical(list(revision.included_proposal_refs)),
                    _canonical(dict(revision.sections)),
                    _canonical(list(revision.source_refs)),
                    _canonical(list(revision.evidence_refs)),
                    revision.created_at,
                    revision.authority,
                ),
            )
            connection.commit()
        return revision

    def recovery(
        self,
        project_id: str,
        pending: Sequence[ProjectKnowledgeProposal] = (),
    ) -> dict[str, Any]:
        valid, reason, milestone = self.validate(project_id)
        included = set(milestone.included_proposal_refs if milestone else ())
        milestone_time = _instant(milestone.created_at, "milestone_created_at") if milestone else None
        delta_items = [
            item
            for item in pending
            if item.project_id == project_id
            and item.proposal_id not in included
            and (milestone_time is None or _instant(item.created_at, "proposal_created_at") > milestone_time)
        ]
        delta_items = delta_items[:MAX_PENDING_PROPOSALS]
        latest = delta_items[0] if delta_items else None
        latest_delta = None
        if latest is not None:
            latest_delta = {
                "source_kind": "staged_project_knowledge_proposal",
                "summary": latest.summary,
                "completed": list(latest.completed),
                "remaining": list(latest.remaining),
                "unknown": list(latest.unknown),
                "redlines": list(latest.redlines),
                "next_action": latest.next_action,
                "conflicts": list(latest.conflicts),
                "completeness": latest.extra.get("source_completeness", "unknown"),
                "proposal_ref": latest.proposal_id,
                "source_refs": list(latest.source_refs),
                "pending_count": len(delta_items),
                "authority": "unreviewed_delta",
            }
        return {
            "valid": valid,
            "reason": reason,
            "complete": milestone is not None and valid,
            "project_id": project_id,
            "milestone": milestone.to_dict() if milestone is not None else None,
            "latest_delta": latest_delta,
            "revision_count": self.count(project_id),
            "write_capability": {
                "local_reviewed_recovery": valid,
                "vibe_formal_write": False,
                "reason": "本地恢复基线可经 AP review 写入；Vibe 项目映射尚未确认。",
            },
            "unknowns": [
                *([] if milestone is not None else ["local_milestone_not_created"]),
                "live_vibe_round_trip_unavailable",
                "automatic_context_compaction_hook_not_connected",
            ],
        }

    def brief(
        self,
        project_id: str,
        *,
        goal: str,
        pending: Sequence[ProjectKnowledgeProposal] = (),
        max_chars: int = DEFAULT_BRIEF_CHARS,
    ) -> dict[str, Any]:
        if isinstance(max_chars, bool) or not MIN_BRIEF_CHARS <= int(max_chars) <= MAX_BRIEF_CHARS:
            raise ContractError("brief_max_chars_out_of_bounds")
        goal = _text(goal, "goal", limit=2_048)
        recovery = self.recovery(project_id, pending)
        milestone = recovery.get("milestone") if isinstance(recovery.get("milestone"), Mapping) else None
        sections = milestone.get("sections") if isinstance(milestone, Mapping) and isinstance(milestone.get("sections"), Mapping) else {}
        work = sections.get("work") if isinstance(sections.get("work"), Mapping) else {}
        requirements = sections.get("requirements") if isinstance(sections.get("requirements"), Mapping) else {}
        status = sections.get("status") if isinstance(sections.get("status"), Mapping) else {}
        delta = recovery.get("latest_delta") if isinstance(recovery.get("latest_delta"), Mapping) else None

        structured = {
            "protocol_version": "ap-vibe.agent-brief.v1",
            "project_id": project_id,
            "goal": goal,
            "recovery_valid": recovery["valid"],
            "authority": "local_reviewed_recovery" if milestone else "temporary_delta_only",
            "revision_id": milestone.get("revision_id") if milestone else None,
            "content_hash": milestone.get("content_hash") if milestone else None,
            "summary": status.get("summary") if milestone else None,
            "completed": list(work.get("completed", ()))[:MAX_BRIEF_ITEMS],
            "remaining": list(work.get("remaining", ()))[:MAX_BRIEF_ITEMS],
            "unknown": list(work.get("unknown", ()))[:MAX_BRIEF_ITEMS],
            "redlines": list(requirements.get("redlines", ()))[:MAX_BRIEF_ITEMS],
            "next_action": work.get("next_action"),
            "latest_delta": dict(delta) if delta else None,
            "source_pointers": list(milestone.get("source_refs", ()))[:MAX_BRIEF_ITEMS] if milestone else list(delta.get("source_refs", ()))[:MAX_BRIEF_ITEMS] if delta else [],
            "stop_conditions": [
                "项目归属未确认时不得写入外部 Vibe 正式知识",
                "latestDelta 不能覆盖完整 milestone",
                "未知和未完成必须原样保留",
            ],
        }

        def clipped(value: Any, limit: int = MAX_BRIEF_ITEM_CHARS) -> str:
            text = str(value or "")
            return text if len(text) <= limit else f"{text[: max(0, limit - 12)]}…[有界省略]"

        identity_lines = [
            "# AP‑Vibe Agent Brief",
            f"项目：{project_id}",
            f"当前目标：{clipped(goal)}",
            f"恢复有效：{'是' if recovery['valid'] else '否'}（{recovery['reason']}）",
            f"知识权威：{structured['authority']}",
            f"本地 revision：{structured['revision_id'] or '尚未建立'}",
            f"当前摘要：{clipped(structured['summary'] or '尚无已审 milestone；以下只能使用未审 delta')} ",
        ]

        # Build the text by semantic priority, not by the order of a long
        # completed list.  The structured brief always retains every bounded
        # field.  Its readable companion must keep identity, recovery
        # validity, redlines and the next action even when optional lists are
        # forced behind source pointers by a small budget.
        redline_values = structured["redlines"]
        redline_lines = ["\n## 不可越过的红线"]
        redline_lines.extend(
            f"- {clipped(item, 160)}" for item in redline_values
        ) if redline_values else redline_lines.append("- 暂无已审记录")
        next_action_lines = [
            "\n## 下一原子动作",
            f"- {clipped(structured['next_action'] or '先建立或审阅本地恢复基线', 360)}",
        ]
        stop_lines = ["\n## 停止条件", *(f"- {item}" for item in structured["stop_conditions"])]
        required_lines = [*identity_lines, *redline_lines, *next_action_lines, *stop_lines]

        optional_groups: list[list[str]] = []
        if delta:
            optional_groups.append(
                [
                    "\n## 最新未审增量",
                    f"- {clipped(delta.get('summary'), 320)}",
                    f"- 完整度：{delta.get('completeness')}；待审候选：{delta.get('pending_count')}",
                    f"- 建议下一步：{clipped(delta.get('next_action') or '审阅该增量', 320)}",
                ]
            )
        for label, key in (("仍未完成", "remaining"), ("诚实未知", "unknown"), ("已完成", "completed")):
            values = structured[key]
            group = [f"\n## {label}"]
            group.extend(f"- {clipped(item)}" for item in values) if values else group.append("- 暂无已审记录")
            optional_groups.append(group)
        pointers = structured["source_pointers"]
        pointer_group = ["\n## 来源指针"]
        pointer_group.extend(f"- {clipped(item, 320)}" for item in pointers) if pointers else pointer_group.append("- 暂无可用来源指针")
        optional_groups.append(pointer_group)

        required_text = "\n".join(required_lines)
        # MIN_BRIEF_CHARS leaves room for the bounded required envelope under
        # the contract limits.  Keep a defensive fallback for future schema
        # growth without ever returning text over max_chars.
        if len(required_text) > int(max_chars):
            compact_redlines = ["\n## 不可越过的红线"]
            compact_redlines.extend(
                f"- {clipped(item, 48)}" for item in redline_values
            ) if redline_values else compact_redlines.append("- 暂无已审记录")
            required_lines = [
                "# AP‑Vibe Agent Brief",
                f"项目：{clipped(project_id, 128)}",
                f"当前目标：{clipped(goal, 128)}",
                f"恢复有效：{'是' if recovery['valid'] else '否'}（{clipped(recovery['reason'], 72)}）",
                f"知识权威：{structured['authority']}",
                *compact_redlines,
                "\n## 下一原子动作",
                f"- {clipped(structured['next_action'] or '先建立或审阅本地恢复基线', 160)}",
                *stop_lines,
            ]
            required_text = "\n".join(required_lines)
        if len(required_text) > int(max_chars):  # defensive contract guard
            raise ContractError("brief_required_envelope_exceeds_budget")

        kept = list(required_lines)
        incomplete = False
        omission_marked = False
        for group in optional_groups:
            candidate = "\n".join((*kept, *group))
            if len(candidate) <= int(max_chars):
                kept.extend(group)
                continue
            incomplete = True
            available = int(max_chars) - len("\n".join(kept))
            marker = "\n- 其余内容因 brief 预算被指针化，请按结构化 source_pointers 查询。"
            if not omission_marked and available >= len(marker):
                kept.append(marker)
                omission_marked = True
        text = "\n".join(kept)
        return {
            "status": "partial" if incomplete else "success",
            "brief": structured,
            "text": text,
            "characters": len(text),
            "max_chars": int(max_chars),
            "brief_incomplete": incomplete,
            "vibe_formal_write": False,
        }


@dataclass
class KnowledgeReviewEnvironment(EnvironmentAdapter):
    project_id: str
    review: KnowledgeReviewRequest
    proposal: ProjectKnowledgeProposal
    parent_matches: bool
    environment_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.environment_id = f"ap-vibe:{self.project_id}:knowledge-review"

    def describe(self) -> Mapping[str, Any]:
        return {
            "environment_id": self.environment_id,
            "name": "AP-Vibe local knowledge review",
            "live": True,
            "observations": ["project_knowledge_review", "readback"],
            "actions": ["commit_local_milestone", "defer", "observe_only"],
            "permissions": ["local_reviewed_recovery_only"],
            "vibe_formal_write": False,
        }

    def candidates(self, event: EventEnvelope, frame_view: Mapping[str, Any]) -> Sequence[ActionCandidate]:
        if event.source in {"readback", "internal"} or event.role == "result":
            return ()
        if event.modality != "project_knowledge_review" or self.review.project_id != self.project_id:
            return ()
        sa = frame_view.get("sa") if isinstance(frame_view.get("sa"), Mapping) else {}
        novelty = max(0.0, min(1.0, float(sa.get("novelty", 0.0) or 0.0)))
        uncertainty = max(0.0, min(1.0, float(self.proposal.uncertainty)))
        conflicts = bool(self.proposal.conflicts)
        commit_eligible = self.parent_matches and not conflicts and self.proposal.status == "staged"
        common = {
            "review": self.review.to_dict(),
            "proposal": self.proposal.to_dict(),
            "project_id": self.project_id,
        }
        candidates: list[ActionCandidate] = []
        if commit_eligible:
            candidates.append(
                ActionCandidate(
                    candidate_id=f"action_{event.event_id}_commit_local_milestone",
                    kind="commit_local_milestone",
                    target=f"project:{self.project_id}:local-recovery",
                    proposition="把用户确认的待审候选追加为本地恢复基线，不写外部 Vibe",
                    components={
                        "goal_fit": 0.96,
                        "evidence_fit": self.proposal.confidence,
                        "novelty": novelty,
                        "closure_gain": max(0.45, 0.90 - 0.30 * uncertainty),
                        "uncertainty_cost": 1.0 - uncertainty,
                        "pressure_relief": 0.42,
                        "risk_cost": 0.05 + 0.18 * uncertainty,
                    },
                    expected_outcome={"knowledge_commit_prepared": True, **common},
                    source="environment",
                    owner="mixed",
                    idempotency_key=f"ap-vibe-local-milestone:{self.project_id}:{self.review.decision_id}",
                    completeness="complete",
                )
            )
        defer_gain = 0.88 if not self.parent_matches or conflicts else 0.24 + 0.42 * uncertainty
        candidates.extend(
            (
                ActionCandidate(
                    candidate_id=f"action_{event.event_id}_defer",
                    kind="defer",
                    target=f"project:{self.project_id}:knowledge-review",
                    proposition="保留这次审阅，等待冲突或父版本问题解决后再提交",
                    components={
                        "goal_fit": 0.40,
                        "evidence_fit": defer_gain,
                        "novelty": novelty * 0.18,
                        "closure_gain": 0.18,
                        "uncertainty_cost": uncertainty,
                        "pressure_relief": 0.16,
                        "risk_cost": 0.0,
                    },
                    expected_outcome={
                        "knowledge_review_deferred": True,
                        "reasons": [
                            *([] if self.parent_matches else ["parent_revision_changed"]),
                            *([] if not conflicts else ["proposal_conflicts_unresolved"]),
                        ],
                        **common,
                    },
                    source="environment",
                    owner="ap_native",
                    idempotency_key=f"ap-vibe-knowledge-defer:{self.project_id}:{self.review.decision_id}",
                    completeness="complete",
                ),
                ActionCandidate(
                    candidate_id=f"action_{event.event_id}_observe",
                    kind="observe_only",
                    target=f"project:{self.project_id}:knowledge-review",
                    proposition="只保留审阅事件，不改变本地恢复基线",
                    components={
                        "goal_fit": 0.25,
                        "evidence_fit": 0.38,
                        "novelty": novelty * 0.12,
                        "closure_gain": 0.08,
                        "uncertainty_cost": 0.52,
                        "pressure_relief": 0.08,
                        "risk_cost": 0.0,
                    },
                    expected_outcome={"knowledge_review_observed": True, **common},
                    source="environment",
                    owner="ap_native",
                    idempotency_key=f"ap-vibe-knowledge-observe:{self.project_id}:{self.review.decision_id}",
                    completeness="complete",
                ),
            )
        )
        return tuple(candidates)

    def dispatch(self, candidate: ActionCandidate, idempotency_key: str) -> DispatchReceipt:
        supported = {"commit_local_milestone", "defer", "observe_only"}
        if candidate.kind not in supported:
            return DispatchReceipt(
                action_ref=candidate.candidate_id,
                environment_id=self.environment_id,
                idempotency_key=idempotency_key,
                status="rejected",
                accepted_at=utc_now(),
                connector_ref="ap-vibe-local-knowledge",
                error_code="knowledge_review_action_unsupported",
                retryable=False,
            )
        return DispatchReceipt(
            action_ref=candidate.candidate_id,
            environment_id=self.environment_id,
            idempotency_key=idempotency_key,
            status="accepted",
            accepted_at=utc_now(),
            connector_ref="ap-vibe-local-knowledge",
            evidence_refs=(f"ap-vibe-knowledge-readback:{candidate.candidate_id}",),
            extra={
                "kind": candidate.kind,
                "decision_id": self.review.decision_id,
                "proposal_id": self.proposal.proposal_id,
                "vibe_formal_write": False,
            },
        )

    def readback(self, receipt: DispatchReceipt) -> ResultEvent:
        if receipt.status != "accepted":
            return ResultEvent(
                receipt_ref=receipt.receipt_id,
                action_ref=receipt.action_ref,
                environment_id=self.environment_id,
                status="unknown",
                completeness="unknown",
                error_code=receipt.error_code or "knowledge_review_dispatch_not_accepted",
                retryable=receipt.retryable,
            )
        kind = str(receipt.extra.get("kind", "unknown"))
        return ResultEvent(
            receipt_ref=receipt.receipt_id,
            action_ref=receipt.action_ref,
            environment_id=self.environment_id,
            status="success",
            observed_at=utc_now(),
            payload_inline={
                "action_kind": kind,
                "knowledge_commit_prepared": kind == "commit_local_milestone",
                "project_id": self.project_id,
                "proposal_id": self.proposal.proposal_id,
                "vibe_formal_write": False,
            },
            evidence_refs=receipt.evidence_refs,
            completeness="complete",
            lineage_refs=(receipt.receipt_id, receipt.action_ref, self.proposal.proposal_id),
        )


def _selected_kind(results: Sequence[TickResult]) -> str | None:
    if not results:
        return None
    frame = results[0].frame
    selected_ref = frame.decision.get("selected_candidate_ref")
    return next(
        (
            str(item.get("kind"))
            for item in frame.actions
            if isinstance(item, Mapping) and item.get("candidate_id") == selected_ref
        ),
        None,
    )


def run_knowledge_review_episode(
    database_path: str | Path,
    review: KnowledgeReviewRequest,
    proposal: ProjectKnowledgeProposal,
    *,
    knowledge_store: ProjectKnowledgeStore,
    max_result_ticks: int = 2,
) -> KnowledgeReviewRun:
    if not isinstance(review, KnowledgeReviewRequest):
        raise ContractError("knowledge_review_episode_requires_review")
    if not isinstance(proposal, ProjectKnowledgeProposal):
        raise ContractError("knowledge_review_episode_requires_proposal")
    if not isinstance(knowledge_store, ProjectKnowledgeStore):
        raise ContractError("knowledge_review_episode_requires_store")
    if isinstance(max_result_ticks, bool) or not 1 <= int(max_result_ticks) <= 8:
        raise ContractError("knowledge_review_result_tick_budget_out_of_bounds")
    current_parent = knowledge_store.latest(review.project_id)
    parent_matches = review.expected_parent_revision == (current_parent.revision_id if current_parent else None)
    environment = KnowledgeReviewEnvironment(
        review.project_id,
        review,
        proposal,
        parent_matches=parent_matches,
    )
    database = Path(database_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    episode_id = _stable_id("knowledge_review_episode", review.project_id, review.decision_id)
    results: list[TickResult] = []
    with EventStore(database) as store:
        runtime = MindRuntime(
            store,
            environment,
            runtime_id=f"ap-vibe-runtime:{review.project_id}",
            organism_id=f"ap-vibe-organism:{review.project_id}",
            episode_id=episode_id,
            gateway=NullGateway(),
            max_internal_ticks=4,
            max_wake_attempts=8,
        )
        current = runtime.tick(
            review.as_event(
                proposal,
                runtime_id=runtime.runtime_id,
                organism_id=runtime.organism_id,
                episode_id=episode_id,
            )
        )
        results.append(current)
        result_back_seen = False
        for _ in range(int(max_result_ticks)):
            if current.result_envelope is None:
                break
            current = runtime.tick(current.result_envelope)
            results.append(current)
            result_back_seen = True

    revision = None
    first_result = results[0].result if results else None
    if (
        _selected_kind(results) == "commit_local_milestone"
        and first_result is not None
        and first_result.status == "success"
        and first_result.completeness == "complete"
        and result_back_seen
    ):
        revision = knowledge_store.commit(review, proposal)
    return KnowledgeReviewRun(
        review=review,
        proposal=proposal,
        episode_id=episode_id,
        results=tuple(results),
        revision=revision,
    )


__all__ = [
    "KNOWLEDGE_SECTIONS",
    "MIN_BRIEF_CHARS",
    "DEFAULT_BRIEF_CHARS",
    "MAX_BRIEF_CHARS",
    "KnowledgeRevisionConflict",
    "KnowledgeReviewRequest",
    "LocalKnowledgeRevision",
    "KnowledgeReviewRun",
    "ProjectKnowledgeStore",
    "KnowledgeReviewEnvironment",
    "run_knowledge_review_episode",
]
