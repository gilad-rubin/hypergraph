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
import logging
from collections.abc import Collection
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hypergraph.checkpointers.base import CheckpointPolicy

# host/ is the same persistence subsystem as checkpointers/ (a RunHome IS a
# SqliteCheckpointer), so reaching its private column list here is deliberate.
from hypergraph.checkpointers.sqlite import _RUNS_COLS, SqliteCheckpointer
from hypergraph.host.definition import DefinitionId
from hypergraph.host.errors import AlreadyTerminalError, HostError, WorkflowIdConflictError
from hypergraph.host.fingerprint import fingerprint_mismatch_aspect

if TYPE_CHECKING:
    from hypergraph.checkpointers.types import Run

logger = logging.getLogger("hypergraph.host")

_SUBMISSION_COLS = (
    "workflow_id, definition_name, def_version, def_struct_hash, inputs_json, "
    "start_at, state, recovery_attempts, recovery_cap, source_ref, created_at, claimed_at, finished_at, "
    "fingerprint, compat_state, retry_of, forked_from, fork_reason, last_progress_step_count"
)
_SUBMISSION_PLACEHOLDERS = ", ".join("?" for _ in _SUBMISSION_COLS.split(", "))
_QUALIFIED_SUBMISSION_COLS = ", ".join(f"s.{name}" for name in _SUBMISSION_COLS.split(", "))
_QUALIFIED_RUN_COLS = ", ".join(f"r.{name}" for name in _RUNS_COLS.split(", "))
_TERMINAL_STATUS_VALUES = ("completed", "failed", "partial", "stopped")
_PROGRESS_STATUSES = frozenset({"paused", *_TERMINAL_STATUS_VALUES})
_CLAIM_BATCH = 16


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_submission(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(_SUBMISSION_COLS.split(", "), row, strict=True))


def _is_committed_progress(kind: str, payload: dict[str, Any]) -> bool:
    """True when a run mutation is NEW committed progress (resets the brake).

    Only step saves, durable pauses, and terminal transitions count. A status
    flip to ``active`` at claim/re-adoption (``run_started``/``status``) is
    recovery bookkeeping, not progress, and must NOT reset the counter.
    Journal-skipped steps during recovery are not re-saved, so resume alone
    never touches the counter either.
    """
    if kind == "step":
        return True
    return kind == "status" and payload.get("status") in _PROGRESS_STATUSES


def _raise_on_conflicting_reuse(
    existing: dict[str, Any],
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

    Terminal reuse wins first (completed history never changes identity): a
    finished submission is terminal even when no runs row exists (stopped
    before first execution). Then fingerprint mismatch is a distinct typed
    conflict; an identical fingerprint falls through to use-existing dedup.
    Caller holds the write transaction and rolls back on raise.
    """
    if existing["state"] == "finished":
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
        """Append one run_updates row; caller holds the write transaction.

        Single INSERT...SELECT so the seq allocation and the insert are one
        statement (no read-then-write race).
        """
        db.execute(
            "INSERT INTO run_updates (run_id, seq, kind, payload, created_at) "
            "SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ? FROM run_updates WHERE run_id = ?",
            (run_id, kind, json.dumps(payload), _now_iso(), run_id),
        )

    async def _append_run_update(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Append one run_updates row; caller holds the write transaction."""
        await self._db.execute(
            "INSERT INTO run_updates (run_id, seq, kind, payload, created_at) "
            "SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ? FROM run_updates WHERE run_id = ?",
            (run_id, kind, json.dumps(payload), _now_iso(), run_id),
        )

    def _reset_recovery_attempts_sync(self, db: Any, run_id: str) -> None:
        """Reset the recovery brake on NEW committed progress (same transaction)."""
        db.execute(
            "UPDATE host_submissions SET recovery_attempts = 0 WHERE workflow_id = ? AND recovery_attempts > 0",
            (run_id,),
        )

    def _after_run_mutation_sync(self, db: Any, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        self._append_run_update_sync(db, run_id, kind, payload)
        if _is_committed_progress(kind, payload):
            self._reset_recovery_attempts_sync(db, run_id)

    async def _after_run_mutation(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        await self._append_run_update(run_id, kind, payload)
        if _is_committed_progress(kind, payload):
            await self._db.execute(
                "UPDATE host_submissions SET recovery_attempts = 0 WHERE workflow_id = ? AND recovery_attempts > 0",
                (run_id,),
            )

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
        recovery_cap: int = 3,
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
                    _raise_on_conflicting_reuse(
                        _row_to_submission(existing_row),
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
                        recovery_cap,
                        source_ref,
                        now,
                        None,
                        None,
                        fingerprint,
                        "compatible",
                        retry_of,
                        forked_from,
                        fork_reason,
                        0,
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
        recovery_cap: int = 3,
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
                    _raise_on_conflicting_reuse(
                        _row_to_submission(existing_row),
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
                        recovery_cap,
                        source_ref,
                        now,
                        None,
                        None,
                        fingerprint,
                        "compatible",
                        retry_of,
                        forked_from,
                        fork_reason,
                        0,
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
                        logger.warning(
                            "Worker cannot serve submission %s: pinned identity %s is not served by this host; "
                            "marking it version-incompatible (it stays parked until a serving worker or explicit migration).",
                            submission["workflow_id"],
                            identity.to_dict(),
                        )
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
        finished. Every other claimed submission is a recovery attempt and
        gets ``recovery_attempts += 1``: when the incremented count reaches
        the submission's ``recovery_cap`` it is parked as 'exhausted' (a
        durable ``recovery_exhausted`` update is appended in the same
        transaction) so a crash-looping run is braked instead of resumed
        forever; otherwise it returns to 'pending' with the incremented
        count. The counter is NOT evaluated against step history here —
        NEW committed progress (step saves, durable pauses, terminal
        transitions) resets it to 0 at commit time via
        ``_after_run_mutation``, so a run killed WITH committed steps still
        shows the incremented attempt count after re-adoption (prototype
        Scenario 3). Pending submissions also reset to
        ``compat_state='compatible'`` so a new worker/deployment
        re-evaluates version compatibility from scratch.
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
                cursor = await self._db.execute(
                    f"SELECT {_SUBMISSION_COLS} FROM host_submissions WHERE state = 'claimed'",
                )
                for row in await cursor.fetchall():
                    submission = _row_to_submission(row)
                    workflow_id = submission["workflow_id"]
                    attempts = submission["recovery_attempts"] + 1
                    if attempts >= submission["recovery_cap"]:
                        # Recovery brake: the run was re-adopted without new
                        # committed progress too many times. Park it
                        # exhausted; client.rerun() revives.
                        await self._db.execute(
                            "UPDATE host_submissions SET state = 'exhausted', recovery_attempts = ? WHERE workflow_id = ?",
                            (attempts, workflow_id),
                        )
                        await self._append_run_update(
                            workflow_id,
                            "recovery_exhausted",
                            {"recovery_attempts": attempts, "recovery_cap": submission["recovery_cap"]},
                        )
                        continue
                    await self._db.execute(
                        "UPDATE host_submissions SET state = 'pending', claimed_at = NULL, recovery_attempts = ? WHERE workflow_id = ?",
                        (attempts, workflow_id),
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

    # === host_commands (durable stop channel) ===

    def _write_stop_command_sync(self, workflow_id: str, info: Any, source_ref: str | None = None) -> bool:
        """Record a durable stop command plus its 'command' update, atomically.

        Returns True when a new command row was written; False when an
        unapplied stop already exists (the first stop owns its ``info`` and
        nothing new is written). Raises ``HostError`` for an unknown run and
        ``AlreadyTerminalError`` when the run is already terminal (or its
        submission already finished) at write time. ``source_ref`` is an
        opaque caller provenance marker (ADR 0005 A11) stored on the command
        row; it never affects dedup.
        """
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                submission_row = db.execute(
                    "SELECT state FROM host_submissions WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                run_row = db.execute("SELECT status FROM runs WHERE id = ?", (workflow_id,)).fetchone()
                if submission_row is None and run_row is None:
                    raise HostError(f"Cannot stop {workflow_id!r}: no such run in this Run Home.")
                if run_row is not None and run_row[0] in _TERMINAL_STATUS_VALUES:
                    raise AlreadyTerminalError(workflow_id)
                if submission_row is not None and submission_row[0] == "finished":
                    raise AlreadyTerminalError(workflow_id)
                existing = db.execute(
                    "SELECT id FROM host_commands WHERE run_id = ? AND verb = 'stop' AND applied_at IS NULL LIMIT 1",
                    (workflow_id,),
                ).fetchone()
                if existing is not None:
                    db.rollback()
                    return False
                db.execute(
                    "INSERT INTO host_commands (run_id, verb, payload, source_ref, created_at) VALUES (?, 'stop', ?, ?, ?)",
                    (workflow_id, json.dumps({"info": info}), source_ref, _now_iso()),
                )
                self._append_run_update_sync(db, workflow_id, "command", {"verb": "stop", "info": info})
                db.commit()
                return True
            except BaseException:
                self._rollback_sync(db)
                raise

    async def _write_stop_command(self, workflow_id: str, info: Any, source_ref: str | None = None) -> bool:
        """Async mirror of ``_write_stop_command_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                submission_cursor = await self._db.execute(
                    "SELECT state FROM host_submissions WHERE workflow_id = ?",
                    (workflow_id,),
                )
                submission_row = await submission_cursor.fetchone()
                run_cursor = await self._db.execute("SELECT status FROM runs WHERE id = ?", (workflow_id,))
                run_row = await run_cursor.fetchone()
                if submission_row is None and run_row is None:
                    raise HostError(f"Cannot stop {workflow_id!r}: no such run in this Run Home.")
                if run_row is not None and run_row[0] in _TERMINAL_STATUS_VALUES:
                    raise AlreadyTerminalError(workflow_id)
                if submission_row is not None and submission_row[0] == "finished":
                    raise AlreadyTerminalError(workflow_id)
                existing_cursor = await self._db.execute(
                    "SELECT id FROM host_commands WHERE run_id = ? AND verb = 'stop' AND applied_at IS NULL LIMIT 1",
                    (workflow_id,),
                )
                if await existing_cursor.fetchone() is not None:
                    await self._db.rollback()
                    return False
                await self._db.execute(
                    "INSERT INTO host_commands (run_id, verb, payload, source_ref, created_at) VALUES (?, 'stop', ?, ?, ?)",
                    (workflow_id, json.dumps({"info": info}), source_ref, _now_iso()),
                )
                await self._append_run_update(workflow_id, "command", {"verb": "stop", "info": info})
                await self._db.commit()
                return True
            except BaseException:
                await self._rollback_async()
                raise

    async def _unapplied_stop_commands(self) -> list[tuple[int, str, Any]]:
        """Read unapplied stop commands as (command_id, workflow_id, info)."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(
                "SELECT id, run_id, payload FROM host_commands WHERE verb = 'stop' AND applied_at IS NULL ORDER BY id",
            )
            rows = await cursor.fetchall()
            return [(int(row[0]), str(row[1]), json.loads(row[2]).get("info")) for row in rows]

    async def _apply_stop_commands(self, command_ids: Collection[int]) -> None:
        """Mark stop commands applied: the worker observed and acted on them."""
        ids = list(command_ids)
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                await self._db.execute(
                    f"UPDATE host_commands SET applied_at = ? WHERE id IN ({placeholders}) AND applied_at IS NULL",
                    (_now_iso(), *ids),
                )
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise

    async def _apply_stop_never_started(self, workflow_id: str) -> bool:
        """Pre-run gate for a stop that arrived before first execution.

        Returns True when an unapplied stop exists AND no runs row exists:
        the command is marked applied and the submission finished (the run
        never executes and no runs row is invented). Returns False when no
        stop is pending or a runs row already exists (a resume proceeds and
        the periodic scan delivers the stop to the live execution).
        """
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                stop_cursor = await self._db.execute(
                    "SELECT id FROM host_commands WHERE run_id = ? AND verb = 'stop' AND applied_at IS NULL LIMIT 1",
                    (workflow_id,),
                )
                if await stop_cursor.fetchone() is None:
                    await self._db.commit()
                    return False
                run_cursor = await self._db.execute("SELECT 1 FROM runs WHERE id = ?", (workflow_id,))
                if await run_cursor.fetchone() is not None:
                    await self._db.commit()
                    return False
                now = _now_iso()
                await self._db.execute(
                    "UPDATE host_commands SET applied_at = ? WHERE run_id = ? AND verb = 'stop' AND applied_at IS NULL",
                    (now, workflow_id),
                )
                await self._db.execute(
                    "UPDATE host_submissions SET state = 'finished', finished_at = ? WHERE workflow_id = ? AND state IN ('pending', 'claimed')",
                    (now, workflow_id),
                )
                await self._db.commit()
                return True
            except BaseException:
                await self._rollback_async()
                raise

    # === listing (client.list) ===

    def _list_run_rows_sync(self) -> list[tuple[dict[str, Any] | None, Run | None]]:
        """All submissions with their runs row, plus bare Tier-0 runs."""
        with self._sync_lock:
            db = self._sync_db()
            rows: list[tuple[dict[str, Any] | None, Run | None]] = []
            cursor = db.execute(
                f"SELECT {_QUALIFIED_SUBMISSION_COLS}, {_QUALIFIED_RUN_COLS} FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id"
            )
            sub_count = len(_SUBMISSION_COLS.split(", "))
            for row in cursor.fetchall():
                submission = _row_to_submission(row[:sub_count])
                run = self._row_to_run(row[sub_count:]) if row[sub_count] is not None else None
                rows.append((submission, run))
            cursor = db.execute(f"SELECT {_RUNS_COLS} FROM runs WHERE id NOT IN (SELECT workflow_id FROM host_submissions)")
            for row in cursor.fetchall():
                rows.append((None, self._row_to_run(row)))
            return rows

    async def _list_run_rows(self) -> list[tuple[dict[str, Any] | None, Run | None]]:
        """Async mirror of ``_list_run_rows_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            rows: list[tuple[dict[str, Any] | None, Run | None]] = []
            cursor = await self._db.execute(
                f"SELECT {_QUALIFIED_SUBMISSION_COLS}, {_QUALIFIED_RUN_COLS} FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id"
            )
            sub_count = len(_SUBMISSION_COLS.split(", "))
            for row in await cursor.fetchall():
                submission = _row_to_submission(row[:sub_count])
                run = self._row_to_run(row[sub_count:]) if row[sub_count] is not None else None
                rows.append((submission, run))
            cursor = await self._db.execute(f"SELECT {_RUNS_COLS} FROM runs WHERE id NOT IN (SELECT workflow_id FROM host_submissions)")
            for row in await cursor.fetchall():
                rows.append((None, self._row_to_run(row)))
            return rows

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
