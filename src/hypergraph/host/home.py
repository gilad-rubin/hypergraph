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
from collections.abc import Collection
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hypergraph.checkpointers.base import CheckpointPolicy
from hypergraph.checkpointers.sqlite import SqliteCheckpointer
from hypergraph.host.definition import DefinitionId
from hypergraph.host.errors import AlreadyTerminalError, WorkflowIdConflictError
from hypergraph.host.fingerprint import fingerprint_mismatch_aspect

_SUBMISSION_COLS = (
    "workflow_id, definition_name, def_version, def_struct_hash, inputs_json, "
    "start_at, state, recovery_attempts, recovery_cap, source_ref, created_at, claimed_at, finished_at, "
    "fingerprint, compat_state, retry_of, forked_from, fork_reason"
)
_SUBMISSION_PLACEHOLDERS = ", ".join("?" for _ in _SUBMISSION_COLS.split(", "))
_TERMINAL_STATUS_VALUES = ("completed", "failed", "partial", "stopped")
_CLAIM_BATCH = 16


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_submission(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(_SUBMISSION_COLS.split(", "), row, strict=True))


def _raise_on_conflicting_reuse(
    existing: dict[str, Any],
    run_status: str | None,
    *,
    workflow_id: str,
    fingerprint: str,
    definition_name: str,
    def_version: str,
    def_struct_hash: str,
    inputs_json: str,
    start_at: str | None,
) -> None:
    """Apply the dedup/conflict contract to an existing submission row.

    Terminal reuse wins first (completed history never changes identity),
    then fingerprint mismatch is a distinct typed conflict; an identical
    fingerprint falls through to use-existing dedup. Caller holds the write
    transaction and rolls back on raise.
    """
    if existing["state"] == "finished" and run_status is not None and run_status in _TERMINAL_STATUS_VALUES:
        raise AlreadyTerminalError(workflow_id)
    if existing["fingerprint"] != fingerprint:
        aspect = fingerprint_mismatch_aspect(
            existing,
            definition_name=definition_name,
            def_version=def_version,
            def_struct_hash=def_struct_hash,
            inputs_json=inputs_json,
            start_at=start_at,
        )
        raise WorkflowIdConflictError(workflow_id, aspect)


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
        *,
        fingerprint: str,
        retry_of: str | None = None,
        forked_from: str | None = None,
        fork_reason: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Insert one submission plus its 'submitted' update, atomically.

        Returns ``(created, row)``. When a submission already exists for
        ``workflow_id`` nothing is written: a fingerprint-identical
        nonterminal row returns ``(False, existing)`` (use-existing dedup),
        terminal reuse raises ``AlreadyTerminalError``, and a fingerprint
        mismatch raises ``WorkflowIdConflictError``.
        """
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                existing_row = db.execute(
                    f"SELECT {_SUBMISSION_COLS} FROM host_submissions WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                if existing_row is not None:
                    status_row = db.execute("SELECT status FROM runs WHERE id = ?", (workflow_id,)).fetchone()
                    _raise_on_conflicting_reuse(
                        _row_to_submission(existing_row),
                        status_row[0] if status_row is not None else None,
                        workflow_id=workflow_id,
                        fingerprint=fingerprint,
                        definition_name=definition_name,
                        def_version=def_version,
                        def_struct_hash=def_struct_hash,
                        inputs_json=inputs_json,
                        start_at=start_at,
                    )
                    db.rollback()
                    return False, _row_to_submission(existing_row)
                now = _now_iso()
                db.execute(
                    f"INSERT INTO host_submissions ({_SUBMISSION_COLS}) VALUES ({_SUBMISSION_PLACEHOLDERS})",
                    (
                        workflow_id,
                        definition_name,
                        def_version,
                        def_struct_hash,
                        inputs_json,
                        start_at,
                        "pending",
                        0,
                        3,
                        source_ref,
                        now,
                        None,
                        None,
                        fingerprint,
                        "compatible",
                        retry_of,
                        forked_from,
                        fork_reason,
                    ),
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
        *,
        fingerprint: str,
        retry_of: str | None = None,
        forked_from: str | None = None,
        fork_reason: str | None = None,
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
                existing_row = await cursor.fetchone()
                if existing_row is not None:
                    status_cursor = await self._db.execute("SELECT status FROM runs WHERE id = ?", (workflow_id,))
                    status_row = await status_cursor.fetchone()
                    _raise_on_conflicting_reuse(
                        _row_to_submission(existing_row),
                        status_row[0] if status_row is not None else None,
                        workflow_id=workflow_id,
                        fingerprint=fingerprint,
                        definition_name=definition_name,
                        def_version=def_version,
                        def_struct_hash=def_struct_hash,
                        inputs_json=inputs_json,
                        start_at=start_at,
                    )
                    await self._db.rollback()
                    return False, _row_to_submission(existing_row)
                now = _now_iso()
                await self._db.execute(
                    f"INSERT INTO host_submissions ({_SUBMISSION_COLS}) VALUES ({_SUBMISSION_PLACEHOLDERS})",
                    (
                        workflow_id,
                        definition_name,
                        def_version,
                        def_struct_hash,
                        inputs_json,
                        start_at,
                        "pending",
                        0,
                        3,
                        source_ref,
                        now,
                        None,
                        None,
                        fingerprint,
                        "compatible",
                        retry_of,
                        forked_from,
                        fork_reason,
                    ),
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

    def _count_retries_sync(self, workflow_id: str) -> int:
        """Count runs rows with retry_of=workflow_id (rerun id derivation).

        Mirrors the checkpointer's ``retry_workflow`` derivation
        (``<source>-retry-N`` with N = count + 1) so a rerun's workflow id
        and the runner-derived retry_index agree.
        """
        with self._sync_lock:
            db = self._sync_db()
            (count,) = db.execute("SELECT COUNT(*) FROM runs WHERE retry_of = ?", (workflow_id,)).fetchone()
            return int(count or 0)

    async def _count_retries(self, workflow_id: str) -> int:
        """Async mirror of ``_count_retries_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute("SELECT COUNT(*) FROM runs WHERE retry_of = ?", (workflow_id,))
            (count,) = await cursor.fetchone()
            return int(count or 0)

    async def _claim_eligible(self, now_iso: str, served: Collection[DefinitionId], limit: int = _CLAIM_BATCH) -> list[dict[str, Any]]:
        """CAS-claim eligible pending submissions (state -> 'claimed').

        Eligible means ``state='pending'``, ``compat_state='compatible'``,
        and ``start_at`` absent or past (store-authoritative time). A
        submission is claimed only when its pinned Definition identity is in
        ``served`` — the exact served identities plus any ``accepts=``
        declarations. An unserved submission is marked
        ``compat_state='incompatible'`` (idempotent): later scans skip it so
        incompatible rows never starve claimable ones, and clients see
        ``WaitingCondition.VERSION_INCOMPATIBLE``.
        """
        served_set = frozenset(served)
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                cursor = await self._db.execute(
                    f"SELECT {_SUBMISSION_COLS} FROM host_submissions "
                    "WHERE state = 'pending' AND compat_state = 'compatible' AND (start_at IS NULL OR start_at <= ?) "
                    "ORDER BY created_at LIMIT ?",
                    (now_iso, limit),
                )
                rows = await cursor.fetchall()
                claimed: list[dict[str, Any]] = []
                for row in rows:
                    submission = _row_to_submission(row)
                    identity = DefinitionId(
                        submission["definition_name"],
                        submission["def_version"],
                        submission["def_struct_hash"],
                    )
                    if identity not in served_set:
                        # Refuse loudly and durably: this worker cannot serve
                        # the pinned identity; a new worker/version
                        # re-evaluates via the restart scan.
                        await self._db.execute(
                            "UPDATE host_submissions SET compat_state = 'incompatible' WHERE workflow_id = ? AND state = 'pending'",
                            (submission["workflow_id"],),
                        )
                        continue
                    result = await self._db.execute(
                        "UPDATE host_submissions SET state = 'claimed', claimed_at = ? WHERE workflow_id = ? AND state = 'pending'",
                        (now_iso, row[0]),
                    )
                    if result.rowcount == 1:
                        claimed.append(submission)
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
        resubmission. Pending submissions also reset to
        ``compat_state='compatible'`` so a new worker/deployment
        re-evaluates version compatibility from scratch. Full recovery
        semantics (attempts, caps, pause handling) are a later host ticket.
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
                # A new worker (possibly a new deployment) re-evaluates
                # version compatibility from scratch.
                await self._db.execute(
                    "UPDATE host_submissions SET compat_state = 'compatible' WHERE state = 'pending'",
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
