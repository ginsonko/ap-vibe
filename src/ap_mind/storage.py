"""Small SQLite event store for the first AP Mind vertical slice.

The store is deliberately an event-first projection.  Runtime code never
updates an event in place; frames, dispatch receipts, results and checkpoints
are appended in short transactions.  This is enough for a local restart probe
without prematurely materialising the full whitepaper schema.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping

from .contracts import (
    ContractError,
    DispatchReceipt,
    EventEnvelope,
    ResultEvent,
)
from .gateway import GatewayCallReceipt


class EventStore:
    """Durable, bounded-schema storage for events and runtime projections."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=10.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield self._connection
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _create_schema(self) -> None:
        with self.transaction() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    ingest_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    episode_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    completeness TEXT NOT NULL,
                    idempotency_key TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS events_idempotency
                    ON events(idempotency_key)
                    WHERE idempotency_key IS NOT NULL;

                CREATE TABLE IF NOT EXISTS frames (
                    insert_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    frame_id TEXT NOT NULL UNIQUE,
                    episode_id TEXT NOT NULL,
                    tick_index INTEGER NOT NULL,
                    event_ref TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(episode_id, event_ref)
                );
                CREATE INDEX IF NOT EXISTS frames_episode_tick
                    ON frames(episode_id, tick_index);

                CREATE TABLE IF NOT EXISTS dispatches (
                    receipt_id TEXT PRIMARY KEY,
                    action_ref TEXT NOT NULL,
                    environment_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS results (
                    insert_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    result_id TEXT NOT NULL UNIQUE,
                    receipt_ref TEXT NOT NULL,
                    action_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(receipt_ref)
                );
                CREATE INDEX IF NOT EXISTS results_receipt
                    ON results(receipt_ref);

                CREATE TABLE IF NOT EXISTS processes (
                    process_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    insert_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    checkpoint_id TEXT NOT NULL UNIQUE,
                    event_cursor TEXT,
                    tick_index INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS gateway_calls (
                    insert_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL UNIQUE,
                    request_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS gateway_calls_request
                    ON gateway_calls(request_key);

                CREATE TABLE IF NOT EXISTS receptor_cursors (
                    receptor_key TEXT PRIMARY KEY,
                    cursor TEXT,
                    revision TEXT,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS output_outbox (
                    draft_id TEXT PRIMARY KEY,
                    proposition_ref TEXT NOT NULL,
                    next_index INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS output_outbox_status
                    ON output_outbox(status, updated_at);
                """
            )

    @staticmethod
    def _json(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ContractError(f"store_payload_not_json_serializable:{exc}") from exc

    @staticmethod
    def _load_json(value: str) -> dict[str, Any]:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:  # pragma: no cover - corrupt disk guard
            raise ContractError(f"store_payload_invalid_json:{exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise ContractError("store_payload_must_be_object")
        return parsed

    @staticmethod
    def _event_idempotency_signature(event: EventEnvelope) -> dict[str, Any]:
        """Return the retry-stable portion of an event.

        ``event_id`` and observation timestamps are transport-generated in a
        number of adapters.  They therefore must not make an otherwise
        identical retry look like a different operation.  The payload,
        source/role, identity scope and lineage remain part of the signature;
        changing any of those while reusing a key is a real conflict.
        """

        raw = event.to_dict()
        for key in ("event_id", "occurred_at", "observed_at"):
            raw.pop(key, None)
        return raw

    def append_event(self, event: EventEnvelope) -> EventEnvelope:
        if not isinstance(event, EventEnvelope):
            raise ContractError("store_accepts_event_envelopes_only")
        payload = self._json(event.to_dict())
        with self.transaction() as conn:
            try:
                conn.execute(
                    """INSERT INTO events
                    (event_id, episode_id, observed_at, source, completeness,
                     idempotency_key, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_id,
                        event.episode_id,
                        event.observed_at,
                        event.source,
                        event.completeness,
                        event.idempotency_key,
                        payload,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                prior_by_id = self.get_event(event.event_id)
                if prior_by_id is not None:
                    # Transport retries may legitimately carry a fresh
                    # observed/occurred timestamp.  Compare the canonical
                    # semantic envelope (payload, source, scope, lineage,
                    # etc.) rather than those timestamps; a changed payload
                    # or identity remains an explicit conflict.
                    if self._event_idempotency_signature(prior_by_id) == self._event_idempotency_signature(event):
                        return prior_by_id
                    raise ContractError("event_id_reused_for_different_payload") from exc
                if event.idempotency_key:
                    prior = self.get_event_by_idempotency(event.idempotency_key)
                    if prior is not None:
                        if self._event_idempotency_signature(prior) == self._event_idempotency_signature(event):
                            return prior
                        raise ContractError("event_idempotency_conflict") from exc
                raise ContractError(f"event_insert_conflict:{exc}") from exc
        return event

    def get_event(self, event_id: str) -> EventEnvelope | None:
        row = self._connection.execute(
            "SELECT payload_json FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return EventEnvelope.from_dict(self._load_json(row["payload_json"])) if row else None

    def get_event_by_idempotency(self, idempotency_key: str) -> EventEnvelope | None:
        row = self._connection.execute(
            "SELECT payload_json FROM events WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return EventEnvelope.from_dict(self._load_json(row["payload_json"])) if row else None

    def list_events(self, *, episode_id: str | None = None, limit: int = 64) -> list[EventEnvelope]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 10000:
            raise ContractError("event_limit_out_of_bounds")
        if episode_id is None:
            rows = self._connection.execute(
                "SELECT payload_json FROM events ORDER BY ingest_seq DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._connection.execute(
                """SELECT payload_json FROM events WHERE episode_id = ?
                ORDER BY ingest_seq DESC LIMIT ?""",
                (episode_id, limit),
            ).fetchall()
        return [EventEnvelope.from_dict(self._load_json(row["payload_json"])) for row in reversed(rows)]

    def append_frame(self, frame_id: str, episode_id: str, tick_index: int, event_ref: str,
                     payload: Mapping[str, Any], created_at: str) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (frame_id, episode_id, event_ref, created_at)):
            raise ContractError("frame_identity_fields_required")
        if isinstance(tick_index, bool) or not isinstance(tick_index, int) or tick_index < 0:
            raise ContractError("frame_tick_index_invalid")
        with self.transaction() as conn:
            try:
                conn.execute(
                    """INSERT INTO frames
                    (frame_id, episode_id, tick_index, event_ref, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (frame_id, episode_id, tick_index, event_ref, self._json(dict(payload)), created_at),
                )
            except sqlite3.IntegrityError as exc:
                existing = self.get_frame_for_event(event_ref)
                if existing is not None:
                    return
                raise ContractError(f"frame_insert_conflict:{exc}") from exc

    def list_frames(self, episode_id: str, *, limit: int = 64) -> list[dict[str, Any]]:
        if not isinstance(episode_id, str) or not episode_id.strip():
            raise ContractError("episode_id_required")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 10000:
            raise ContractError("frame_limit_out_of_bounds")
        rows = self._connection.execute(
            """SELECT payload_json FROM frames WHERE episode_id = ?
            ORDER BY tick_index ASC, insert_seq ASC LIMIT ?""",
            (episode_id, limit),
        ).fetchall()
        return [self._load_json(row["payload_json"]) for row in rows]

    def get_frame_for_event(self, event_ref: str) -> dict[str, Any] | None:
        if not isinstance(event_ref, str) or not event_ref.strip():
            raise ContractError("event_ref_required")
        row = self._connection.execute(
            """SELECT payload_json FROM frames WHERE event_ref = ?
            ORDER BY tick_index DESC LIMIT 1""",
            (event_ref,),
        ).fetchone()
        return self._load_json(row["payload_json"]) if row else None

    def append_dispatch(self, receipt: DispatchReceipt) -> DispatchReceipt:
        payload = self._json(receipt.to_dict())
        with self.transaction() as conn:
            prior = conn.execute(
                "SELECT payload_json FROM dispatches WHERE idempotency_key = ?",
                (receipt.idempotency_key,),
            ).fetchone()
            if prior:
                existing = DispatchReceipt.from_dict(self._load_json(prior["payload_json"]))
                if existing.action_ref != receipt.action_ref or existing.environment_id != receipt.environment_id:
                    raise ContractError("idempotency_key_reused_for_different_action")
                return existing
            conn.execute(
                """INSERT INTO dispatches
                (receipt_id, action_ref, environment_id, idempotency_key,
                 status, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt.receipt_id,
                    receipt.action_ref,
                    receipt.environment_id,
                    receipt.idempotency_key,
                    receipt.status,
                    payload,
                    receipt.accepted_at,
                ),
            )
        return receipt

    def get_dispatch_by_idempotency(self, idempotency_key: str) -> DispatchReceipt | None:
        row = self._connection.execute(
            "SELECT payload_json FROM dispatches WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return DispatchReceipt.from_dict(self._load_json(row["payload_json"])) if row else None

    def get_dispatch(self, receipt_id: str) -> DispatchReceipt | None:
        if not isinstance(receipt_id, str) or not receipt_id.strip():
            raise ContractError("dispatch_receipt_id_required")
        row = self._connection.execute(
            "SELECT payload_json FROM dispatches WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()
        return DispatchReceipt.from_dict(self._load_json(row["payload_json"])) if row else None

    def append_result(self, result: ResultEvent) -> ResultEvent:
        payload = self._json(result.to_dict())
        with self.transaction() as conn:
            prior = conn.execute(
                "SELECT payload_json FROM results WHERE result_id = ?", (result.result_id,)
            ).fetchone()
            if prior:
                return ResultEvent.from_dict(self._load_json(prior["payload_json"]))
            prior_for_receipt = conn.execute(
                "SELECT payload_json FROM results WHERE receipt_ref = ?", (result.receipt_ref,)
            ).fetchone()
            if prior_for_receipt:
                return ResultEvent.from_dict(self._load_json(prior_for_receipt["payload_json"]))
            try:
                conn.execute(
                    """INSERT INTO results
                    (result_id, receipt_ref, action_ref, status, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        result.result_id,
                        result.receipt_ref,
                        result.action_ref,
                        result.status,
                        payload,
                        result.observed_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                prior_for_receipt = conn.execute(
                    "SELECT payload_json FROM results WHERE receipt_ref = ?", (result.receipt_ref,)
                ).fetchone()
                if prior_for_receipt:
                    return ResultEvent.from_dict(self._load_json(prior_for_receipt["payload_json"]))
                raise ContractError(f"result_insert_conflict:{exc}") from exc
        return result

    def get_result_for_receipt(self, receipt_id: str) -> ResultEvent | None:
        row = self._connection.execute(
            """SELECT payload_json FROM results WHERE receipt_ref = ?
            ORDER BY insert_seq DESC LIMIT 1""",
            (receipt_id,),
        ).fetchone()
        return ResultEvent.from_dict(self._load_json(row["payload_json"])) if row else None

    def upsert_process(self, process_id: str, status: str, payload: Mapping[str, Any], updated_at: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO processes(process_id, status, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(process_id) DO UPDATE SET
                status=excluded.status, payload_json=excluded.payload_json,
                updated_at=excluded.updated_at""",
                (process_id, status, self._json(dict(payload)), updated_at),
            )

    def list_open_processes(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT payload_json FROM processes
            WHERE status NOT IN ('succeeded', 'failed', 'cancelled')
            ORDER BY updated_at ASC, process_id ASC"""
        ).fetchall()
        return [self._load_json(row["payload_json"]) for row in rows]

    def save_checkpoint(self, checkpoint_id: str, event_cursor: str | None,
                        tick_index: int, payload: Mapping[str, Any], created_at: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO checkpoints
                (checkpoint_id, event_cursor, tick_index, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (checkpoint_id, event_cursor, tick_index, self._json(dict(payload)), created_at),
            )

    def latest_checkpoint(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            """SELECT checkpoint_id, event_cursor, tick_index, payload_json, created_at
            FROM checkpoints ORDER BY insert_seq DESC LIMIT 1"""
        ).fetchone()
        if row is None:
            return None
        payload = self._load_json(row["payload_json"])
        payload.update(
            {
                "checkpoint_id": row["checkpoint_id"],
                "event_cursor": row["event_cursor"],
                "tick_index": row["tick_index"],
                "created_at": row["created_at"],
            }
        )
        return payload

    def append_gateway_call(self, receipt: GatewayCallReceipt) -> GatewayCallReceipt:
        """Persist one credential-free model call receipt idempotently."""

        if not isinstance(receipt, GatewayCallReceipt):
            raise ContractError("store_accepts_gateway_call_receipts_only")
        payload = self._json(receipt.to_dict())
        with self.transaction() as conn:
            prior = conn.execute(
                "SELECT payload_json FROM gateway_calls WHERE request_key = ?",
                (receipt.request_key,),
            ).fetchone()
            if prior:
                return GatewayCallReceipt.from_dict(self._load_json(prior["payload_json"]))
            try:
                conn.execute(
                    """INSERT INTO gateway_calls
                    (call_id, request_key, status, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (receipt.call_id, receipt.request_key, receipt.status, payload, receipt.created_at),
                )
            except sqlite3.IntegrityError as exc:
                prior = conn.execute(
                    "SELECT payload_json FROM gateway_calls WHERE call_id = ? OR request_key = ?",
                    (receipt.call_id, receipt.request_key),
                ).fetchone()
                if prior:
                    return GatewayCallReceipt.from_dict(self._load_json(prior["payload_json"]))
                raise ContractError(f"gateway_call_insert_conflict:{exc}") from exc
        return receipt

    def get_gateway_call(self, request_key: str) -> GatewayCallReceipt | None:
        row = self._connection.execute(
            "SELECT payload_json FROM gateway_calls WHERE request_key = ?", (request_key,)
        ).fetchone()
        return GatewayCallReceipt.from_dict(self._load_json(row["payload_json"])) if row else None

    def get_receptor_cursor(self, receptor_key: str) -> dict[str, Any] | None:
        if not isinstance(receptor_key, str) or not receptor_key.strip():
            raise ContractError("receptor_key_required")
        row = self._connection.execute(
            "SELECT cursor, revision, status, payload_json, updated_at FROM receptor_cursors WHERE receptor_key = ?",
            (receptor_key,),
        ).fetchone()
        if row is None:
            return None
        payload = self._load_json(row["payload_json"])
        payload.update({"receptor_key": receptor_key, "cursor": row["cursor"], "revision": row["revision"], "status": row["status"], "updated_at": row["updated_at"]})
        return payload

    def save_receptor_cursor(
        self,
        receptor_key: str,
        *,
        cursor: str | None,
        revision: str | None,
        status: str,
        payload: Mapping[str, Any] | None = None,
        updated_at: str,
    ) -> None:
        if not isinstance(receptor_key, str) or not receptor_key.strip():
            raise ContractError("receptor_key_required")
        if cursor is not None and not isinstance(cursor, str):
            raise ContractError("receptor_cursor_invalid")
        if revision is not None and not isinstance(revision, str):
            raise ContractError("receptor_revision_invalid")
        if not isinstance(status, str) or not status.strip():
            raise ContractError("receptor_status_required")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO receptor_cursors
                (receptor_key, cursor, revision, status, payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(receptor_key) DO UPDATE SET
                cursor=excluded.cursor, revision=excluded.revision,
                status=excluded.status, payload_json=excluded.payload_json,
                updated_at=excluded.updated_at""",
                (receptor_key, cursor, revision, status, self._json(dict(payload or {})), updated_at),
            )

    def save_output_outbox(
        self,
        *,
        draft_id: str,
        proposition_ref: str,
        next_index: int,
        status: str,
        payload: Mapping[str, Any],
        updated_at: str,
    ) -> dict[str, Any]:
        """Persist one monotonic expression cursor and its immutable draft.

        The outbox is a recovery projection, not a second action queue.  A
        caller may move a cursor forward or refine the status at the current
        unit, but it cannot silently rewind a physical expression or replace
        the proposition/draft snapshot under the same identity.
        """

        if not all(
            isinstance(value, str) and value.strip()
            for value in (draft_id, proposition_ref, status, updated_at)
        ):
            raise ContractError("output_outbox_identity_fields_required")
        if isinstance(next_index, bool) or not isinstance(next_index, int) or next_index < 0:
            raise ContractError("output_outbox_next_index_invalid")
        raw = dict(payload)
        if raw.get("draft_id") not in {None, draft_id}:
            raise ContractError("output_outbox_draft_payload_conflict")
        if raw.get("proposition_ref") not in {None, proposition_ref}:
            raise ContractError("output_outbox_proposition_payload_conflict")
        raw.update(
            {
                "draft_id": draft_id,
                "proposition_ref": proposition_ref,
                "next_index": next_index,
                "status": status,
                "updated_at": updated_at,
            }
        )
        payload_json = self._json(raw)
        terminal = {"completed", "withdrawn", "superseded"}
        with self.transaction() as conn:
            prior_row = conn.execute(
                """SELECT proposition_ref, next_index, status, payload_json, updated_at
                FROM output_outbox WHERE draft_id = ?""",
                (draft_id,),
            ).fetchone()
            if prior_row is not None:
                prior = self._load_json(prior_row["payload_json"])
                if str(prior_row["proposition_ref"]) != proposition_ref:
                    raise ContractError("output_outbox_proposition_conflict")
                prior_draft = prior.get("draft")
                next_draft = raw.get("draft")
                if prior_draft is not None and next_draft is not None and prior_draft != next_draft:
                    raise ContractError("output_outbox_draft_snapshot_conflict")
                prior_index = int(prior_row["next_index"])
                prior_status = str(prior_row["status"])
                if next_index < prior_index:
                    raise ContractError("output_outbox_cursor_cannot_rewind")
                if prior_status in terminal and (next_index != prior_index or status != prior_status):
                    raise ContractError("output_outbox_terminal_state_cannot_change")
                conn.execute(
                    """UPDATE output_outbox SET next_index = ?, status = ?,
                    payload_json = ?, updated_at = ? WHERE draft_id = ?""",
                    (next_index, status, payload_json, updated_at, draft_id),
                )
            else:
                conn.execute(
                    """INSERT INTO output_outbox
                    (draft_id, proposition_ref, next_index, status, payload_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (draft_id, proposition_ref, next_index, status, payload_json, updated_at),
                )
        return raw

    def get_output_outbox(self, draft_id: str) -> dict[str, Any] | None:
        if not isinstance(draft_id, str) or not draft_id.strip():
            raise ContractError("output_outbox_draft_id_required")
        row = self._connection.execute(
            """SELECT proposition_ref, next_index, status, payload_json, updated_at
            FROM output_outbox WHERE draft_id = ?""",
            (draft_id,),
        ).fetchone()
        if row is None:
            return None
        payload = self._load_json(row["payload_json"])
        payload.update(
            {
                "draft_id": draft_id,
                "proposition_ref": row["proposition_ref"],
                "next_index": int(row["next_index"]),
                "status": row["status"],
                "updated_at": row["updated_at"],
            }
        )
        return payload

    def supersede_output_outbox(
        self,
        *,
        old_draft_id: str,
        new_draft_id: str,
        proposition_ref: str,
        next_index: int,
        old_payload: Mapping[str, Any],
        new_payload: Mapping[str, Any],
        updated_at: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Atomically retire one outbox and install its revised successor."""

        if not all(
            isinstance(value, str) and value.strip()
            for value in (old_draft_id, new_draft_id, proposition_ref, updated_at)
        ):
            raise ContractError("output_revision_identity_fields_required")
        if old_draft_id == new_draft_id:
            raise ContractError("output_revision_requires_new_draft_id")
        if isinstance(next_index, bool) or not isinstance(next_index, int) or next_index < 0:
            raise ContractError("output_revision_next_index_invalid")
        old_raw = dict(old_payload)
        new_raw = dict(new_payload)
        old_raw.update(
            {
                "draft_id": old_draft_id,
                "proposition_ref": proposition_ref,
                "next_index": next_index,
                "status": "superseded",
                "updated_at": updated_at,
            }
        )
        new_raw.update(
            {
                "draft_id": new_draft_id,
                "proposition_ref": proposition_ref,
                "next_index": next_index,
                "status": "ready",
                "updated_at": updated_at,
            }
        )
        with self.transaction() as conn:
            prior = conn.execute(
                """SELECT proposition_ref, next_index, status FROM output_outbox
                WHERE draft_id = ?""",
                (old_draft_id,),
            ).fetchone()
            if prior is None:
                raise ContractError("output_revision_source_missing")
            if str(prior["proposition_ref"]) != proposition_ref:
                raise ContractError("output_revision_proposition_conflict")
            if int(prior["next_index"]) != next_index:
                raise ContractError("output_revision_cursor_changed")
            existing_new = conn.execute(
                "SELECT payload_json FROM output_outbox WHERE draft_id = ?", (new_draft_id,)
            ).fetchone()
            if existing_new is not None:
                existing_payload = self._load_json(existing_new["payload_json"])
                if existing_payload != new_raw:
                    raise ContractError("output_revision_successor_conflict")
                return old_raw, existing_payload
            conn.execute(
                """UPDATE output_outbox SET status = 'superseded', payload_json = ?,
                updated_at = ? WHERE draft_id = ?""",
                (self._json(old_raw), updated_at, old_draft_id),
            )
            conn.execute(
                """INSERT INTO output_outbox
                (draft_id, proposition_ref, next_index, status, payload_json, updated_at)
                VALUES (?, ?, ?, 'ready', ?, ?)""",
                (new_draft_id, proposition_ref, next_index, self._json(new_raw), updated_at),
            )
        return old_raw, new_raw

    def list_open_output_outbox(self, *, limit: int = 64) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ContractError("output_outbox_limit_out_of_bounds")
        rows = self._connection.execute(
            """SELECT draft_id FROM output_outbox
            WHERE status NOT IN ('completed', 'withdrawn', 'superseded')
            ORDER BY updated_at ASC, draft_id ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [item for row in rows if (item := self.get_output_outbox(str(row["draft_id"]))) is not None]

    def count(self, table: str) -> int:
        # Table names are selected internally; reject arbitrary SQL identifiers.
        allowed = {"events", "frames", "dispatches", "results", "processes", "checkpoints", "gateway_calls", "receptor_cursors", "output_outbox"}
        if table not in allowed:
            raise ContractError("unsupported_store_table")
        row = self._connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])


__all__ = ["EventStore"]
