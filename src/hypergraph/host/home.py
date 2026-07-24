"""RunHome — the SQLite Run Home for the durable host (Tier 1).

A RunHome IS the existing SQLite checkpointer plus coordination tables
(schema v6): durable submissions, the per-Run durable update sequence, and
the host command channel. Steps stay the sole execution journal; host
coordination facts never enter ``RunStatus``/``WorkflowStatus``.

Home-bound runners persist every mutation immediately: the Home forces
checkpoint durability ``"sync"`` and rejects ``"exit"`` policies.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hypergraph.checkpointers.base import CheckpointPolicy
from hypergraph.checkpointers.sqlite import SqliteCheckpointer

_SUBMISSION_COLS = (
    "workflow_id, definition_name, def_version, def_struct_hash, inputs_json, "
    "start_at, state, recovery_attempts, recovery_cap, source_ref, created_at, claimed_at, finished_at"
)
_TERMINAL_STATUS_VALUES = ("completed", "failed", "partial", "stopped")
_CLAIM_BATCH = 16


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_submission(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(_SUBMISSION_COLS.split(", "), row, strict=True))


class RunHome(SqliteCheckpointer):
    """SQLite Run Home: checkpointer plus durable-host coordination tables.

    Open with ``RunHome.open("file:./runs.db")`` (or ``":memory:"`` for
    tests). A RunHome works everywhere a ``SqliteCheckpointer`` does —
    direct runner execution against it is unchanged Tier-0 behavior.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        policy: CheckpointPolicy | None = None,
        serializer: Any = None,
    ):
        if policy is not None and policy.durability == "exit":
            raise ValueError(
                'RunHome rejects durability="exit": host runs must persist every mutation immediately, not buffer checkpoint writes to run exit.'
            )
        effective_policy = CheckpointPolicy(
            durability="sync",
            retention=policy.retention if policy is not None else "full",
            window=policy.window if policy is not None else None,
            ttl=policy.ttl if policy is not None else None,
        )
        super().__init__(path, policy=effective_policy, serializer=serializer)

    @classmethod
    def open(cls, uri: str | Path, *, policy: CheckpointPolicy | None = None, serializer: Any = None) -> RunHome:
        """Open (or create) a Run Home at ``uri``.

        Args:
            uri: ``"file:./path.db"``, a plain filesystem path, or
                ``":memory:"`` for an isolated in-memory Home.
            policy: Optional retention/ttl settings. Durability is forced to
                ``"sync"``; ``"exit"`` durability is rejected.
            serializer: Optional value serializer (default: JSON).
        """
        return cls(uri, policy=policy, serializer=serializer)

    @property
    def uri(self) -> str:
        """The Home URI string as passed to ``open()`` (used in refs)."""
        return self._path

    # === run_updates appends (same-transaction as run mutations) ===

    def _append_run_update_sync(self, db: Any, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Append one run_updates row; caller holds the write transaction."""
        (seq,) = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM run_updates WHERE run_id = ?", (run_id,)).fetchone()
        db.execute(
            "INSERT INTO run_updates (run_id, seq, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, seq, kind, json.dumps(payload), _now_iso()),
        )

    async def _append_run_update(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Append one run_updates row; caller holds the write transaction."""
        cursor = await self._db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM run_updates WHERE run_id = ?", (run_id,))
        (seq,) = await cursor.fetchone()
        await self._db.execute(
            "INSERT INTO run_updates (run_id, seq, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, seq, kind, json.dumps(payload), _now_iso()),
        )

    def _after_run_mutation_sync(self, db: Any, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        self._append_run_update_sync(db, run_id, kind, payload)

    async def _after_run_mutation(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        await self._append_run_update(run_id, kind, payload)

    # === Submissions ===

    def _submit_sync(
        self,
        workflow_id: str,
        definition_name: str,
        def_version: str,
        def_struct_hash: str,
        inputs_json: str,
        start_at: str | None,
        source_ref: str | None,
    ) -> tuple[bool, dict[str, Any]]:
        """Insert one submission plus its 'submitted' update, atomically.

        Returns ``(created, row)``: when a submission already exists for
        ``workflow_id`` nothing is written and ``created`` is False.
        """
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    f"SELECT {_SUBMISSION_COLS} FROM host_submissions WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                if existing is not None:
                    db.rollback()
                    return False, _row_to_submission(existing)
                now = _now_iso()
                db.execute(
                    f"INSERT INTO host_submissions ({_SUBMISSION_COLS}) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 3, ?, ?, NULL, NULL)",
                    (workflow_id, definition_name, def_version, def_struct_hash, inputs_json, start_at, source_ref, now),
                )
                self._append_run_update_sync(
                    db,
                    workflow_id,
                    "submitted",
                    {"definition_name": definition_name, "workflow_id": workflow_id},
                )
                db.commit()
            except BaseException:
                self._rollback_sync(db)
                raise
        row = self._get_submission_sync(workflow_id)
        assert row is not None
        return True, row

    async def _submit(
        self,
        workflow_id: str,
        definition_name: str,
        def_version: str,
        def_struct_hash: str,
        inputs_json: str,
        start_at: str | None,
        source_ref: str | None,
    ) -> tuple[bool, dict[str, Any]]:
        """Async mirror of ``_submit_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                cursor = await self._db.execute(
                    f"SELECT {_SUBMISSION_COLS} FROM host_submissions WHERE workflow_id = ?",
                    (workflow_id,),
                )
                existing = await cursor.fetchone()
                if existing is not None:
                    await self._db.rollback()
                    return False, _row_to_submission(existing)
                now = _now_iso()
                await self._db.execute(
                    f"INSERT INTO host_submissions ({_SUBMISSION_COLS}) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 3, ?, ?, NULL, NULL)",
                    (workflow_id, definition_name, def_version, def_struct_hash, inputs_json, start_at, source_ref, now),
                )
                await self._append_run_update(
                    workflow_id,
                    "submitted",
                    {"definition_name": definition_name, "workflow_id": workflow_id},
                )
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise
        row = await self._get_submission(workflow_id)
        assert row is not None
        return True, row

    def _get_submission_sync(self, workflow_id: str) -> dict[str, Any] | None:
        with self._sync_lock:
            db = self._sync_db()
            row = db.execute(
                f"SELECT {_SUBMISSION_COLS} FROM host_submissions WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            return _row_to_submission(row) if row is not None else None

    async def _get_submission(self, workflow_id: str) -> dict[str, Any] | None:
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(
                f"SELECT {_SUBMISSION_COLS} FROM host_submissions WHERE workflow_id = ?",
                (workflow_id,),
            )
            row = await cursor.fetchone()
            return _row_to_submission(row) if row is not None else None

    async def _claim_eligible(self, now_iso: str, limit: int = _CLAIM_BATCH) -> list[dict[str, Any]]:
        """CAS-claim eligible pending submissions (state -> 'claimed').

        Eligible means ``state='pending'`` and ``start_at`` absent or past
        (store-authoritative time). Simple ordering by creation; the full
        delayed-start contract is a later host ticket.
        """
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                cursor = await self._db.execute(
                    f"SELECT {_SUBMISSION_COLS} FROM host_submissions "
                    "WHERE state = 'pending' AND (start_at IS NULL OR start_at <= ?) "
                    "ORDER BY created_at LIMIT ?",
                    (now_iso, limit),
                )
                rows = await cursor.fetchall()
                claimed: list[dict[str, Any]] = []
                for row in rows:
                    result = await self._db.execute(
                        "UPDATE host_submissions SET state = 'claimed', claimed_at = ? WHERE workflow_id = ? AND state = 'pending'",
                        (now_iso, row[0]),
                    )
                    if result.rowcount == 1:
                        claimed.append(_row_to_submission(row))
                await self._db.commit()
                return claimed
            except BaseException:
                await self._rollback_async()
                raise

    async def _finish_submission(self, workflow_id: str) -> None:
        """Mark a claimed submission finished after its run settled."""
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                await self._db.execute(
                    "UPDATE host_submissions SET state = 'finished', finished_at = ? WHERE workflow_id = ?",
                    (_now_iso(), workflow_id),
                )
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise

    async def _restart_scan(self) -> None:
        """Re-adopt unfinished claimed submissions on worker startup.

        Claimed submissions whose run settled terminally are marked
        finished; every other claimed submission (run absent or nonterminal)
        returns to 'pending' so unfinished work continues without
        resubmission. Full recovery semantics (attempts, caps, pause
        handling) are a later host ticket.
        """
        await self._ensure_db()
        now = _now_iso()
        terminal_placeholders = ", ".join("?" for _ in _TERMINAL_STATUS_VALUES)
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                await self._db.execute(
                    f"UPDATE host_submissions SET state = 'finished', finished_at = ? "
                    f"WHERE state = 'claimed' AND workflow_id IN "
                    f"(SELECT id FROM runs WHERE status IN ({terminal_placeholders}))",
                    (now, *_TERMINAL_STATUS_VALUES),
                )
                await self._db.execute(
                    "UPDATE host_submissions SET state = 'pending', claimed_at = NULL WHERE state = 'claimed'",
                )
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise

    # === run_updates reads (watch replay) ===

    def _read_run_updates_sync(self, run_id: str, after_seq: int = 0) -> list[tuple[int, str, str, str]]:
        """Read run_updates rows with seq > after_seq, in seq order."""
        with self._sync_lock:
            db = self._sync_db()
            cursor = db.execute(
                "SELECT seq, kind, payload, created_at FROM run_updates WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            )
            return [(int(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in cursor.fetchall()]

    async def _read_run_updates(self, run_id: str, after_seq: int = 0) -> list[tuple[int, str, str, str]]:
        """Async mirror of ``_read_run_updates_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(
                "SELECT seq, kind, payload, created_at FROM run_updates WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            )
            rows = await cursor.fetchall()
            return [(int(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows]
