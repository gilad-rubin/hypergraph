"""RunHome — the SQLite Run Home for the durable host (Tier 1).

A RunHome IS the existing SQLite checkpointer plus coordination tables
(schema v6): durable submissions, the per-Run durable update sequence, the
host command channel, and the Home-scoped coordination settings every
process that opens the store agrees on (``max_active_runs``). Steps stay the
sole execution journal; host coordination facts never enter
``RunStatus``/``WorkflowStatus``.

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
from hypergraph.checkpointers.types import WorkflowStatus
from hypergraph.host.batch import BatchTolerance, tolerance_trips
from hypergraph.host.definition import DefinitionId
from hypergraph.host.errors import AlreadyTerminalError, HostError, WorkflowIdConflictError
from hypergraph.host.fingerprint import batch_mismatch_aspect, canonical_json, fingerprint_mismatch_aspect, start_fingerprint
from hypergraph.host.views import (
    BATCH_OUTCOME_RECOVERY_EXHAUSTED,
    SUBMISSION_STATE_EXHAUSTED,
    TERMINAL_STATUS_VALUES,
    is_child_settled,
)

if TYPE_CHECKING:
    from hypergraph.checkpointers.types import Run

logger = logging.getLogger("hypergraph.host")

_SUBMISSION_COLS = (
    "workflow_id, definition_name, def_version, def_struct_hash, inputs_json, "
    "start_at, state, recovery_attempts, recovery_cap, source_ref, created_at, claimed_at, finished_at, "
    "fingerprint, compat_state, retry_of, forked_from, fork_reason, last_progress_step_count, batch_id, item_key"
)
_SUBMISSION_PLACEHOLDERS = ", ".join("?" for _ in _SUBMISSION_COLS.split(", "))
_QUALIFIED_SUBMISSION_COLS = ", ".join(f"s.{name}" for name in _SUBMISSION_COLS.split(", "))
_QUALIFIED_RUN_COLS = ", ".join(f"r.{name}" for name in _RUNS_COLS.split(", "))
_TERMINAL_STATUS_VALUES = tuple(sorted(TERMINAL_STATUS_VALUES))
_PROGRESS_STATUSES = frozenset({"paused", *_TERMINAL_STATUS_VALUES})
_CLAIM_BATCH = 16
# THE active-Run count for host work admission: claimed submissions, the
# ones this worker owns end to end. Both the claim gate and the view read
# this same definition so "holds a slot" never means two different things.
_ACTIVE_RUN_COUNT_SQL = "SELECT COUNT(*) FROM host_submissions WHERE state = 'claimed'"
# The active-Run cap is a Home-scoped fact in the store, not a per-instance
# Python attribute: the worker enforcing it and the operator tuning or reading
# it hold different RunHome objects (often in different processes), and a
# process-local cap would make them disagree about the same queue.
_MAX_ACTIVE_RUNS_KEY = "max_active_runs"
_SELECT_SETTING_SQL = "SELECT value FROM host_settings WHERE key = ?"
_UPSERT_SETTING_SQL = (
    "INSERT INTO host_settings (key, value, updated_at) VALUES (?, ?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
)

_BATCH_COLS = (
    "batch_id, workflow_id, definition_name, def_version, def_struct_hash, items_json, "
    "tolerance_json, start_at, fingerprint, source_ref, created_at, retry_of"
)
_BATCH_PLACEHOLDERS = ", ".join("?" for _ in _BATCH_COLS.split(", "))

# Batch update kinds that are matched (not just written) in SQL.
_TRIP_UPDATE_KIND = "tolerance_tripped"
_UNSTARTED_UPDATE_KIND = "child_unstarted"
_SETTLED_UPDATE_KIND = "child_settled"

# Failure equivalence (PRD 0019): a child counts toward tolerance when its
# runs row failed or its submission is recovery-exhausted. Paused, queued,
# delayed, admission-limited, and unstarted children never count — and
# neither do partial or stopped runs.
_COUNT_FAILURE_EQUIVALENT = (
    "SELECT COUNT(*) FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id "
    f"WHERE s.batch_id = ? AND (r.status = '{WorkflowStatus.FAILED.value}' OR s.state = '{SUBMISSION_STATE_EXHAUSTED}')"
)
_SELECT_TRIPPED = f"SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = '{_TRIP_UPDATE_KIND}' LIMIT 1"


class _Unset:
    """Sentinel for 'argument omitted', which ``None`` cannot express here.

    ``max_active_runs=None`` is a real value (unlimited), so ``open()`` needs a
    third state to mean "adopt whatever the store already holds".
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET = _Unset()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cap_from_row(row: tuple[Any, ...] | None) -> int | None:
    """The stored active-Run cap, or None when uncapped.

    A missing row and a NULL value both mean unlimited, so callers never have
    to distinguish "never configured" from "explicitly unlimited".
    """
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _slots_left(cap_row: tuple[Any, ...] | None, count_row: tuple[Any, ...] | None) -> int | None:
    """Free slots under the stored cap; None when uncapped.

    THE definition of "a free slot" for host work admission, shared by the
    claim gate and the view, sync and async. A restart that re-adopted more
    claimed Runs than a lowered cap yields a negative budget, which simply
    claims nothing new — admission delays work, it never cancels it.
    """
    cap = _cap_from_row(cap_row)
    if cap is None:
        return None
    return cap - (int(count_row[0]) if count_row else 0)


def _row_to_submission(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(_SUBMISSION_COLS.split(", "), row, strict=True))


def _row_to_batch(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(_BATCH_COLS.split(", "), row, strict=True))


def _children_settled_rows(rows: list[tuple[Any, ...]]) -> bool:
    """True when every (submission_state, run_status) pair is settled.

    A child is settled when its run reached a terminal status, its
    submission finished (terminal run, stop-before-start, or a tolerance
    trip that closed admission before it ever ran), or its submission is
    recovery-exhausted (parked; v1 treats parked work as settled).

    A tolerance trip needs no special case here: it marks every remaining
    item's submission finished in the tripping transaction, so those items
    are settled-and-unstarted by the same rule stop-before-start already
    uses.
    """
    return all(is_child_settled(sub_state, run_status) for sub_state, run_status in rows)


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
        max_active_runs: int | None | _Unset = _UNSET,
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
        if not isinstance(max_active_runs, _Unset):
            # Explicit argument writes through; omitting it adopts whatever the
            # store already holds (see the `max_active_runs` property).
            self.max_active_runs = max_active_runs

    @classmethod
    def open(
        cls,
        uri: str | Path,
        *,
        policy: CheckpointPolicy | None = None,
        serializer: Any = None,
        max_active_runs: int | None | _Unset = _UNSET,
    ) -> RunHome:
        """Open (or create) a Run Home at ``uri``.

        Args:
            uri: ``"file:./path.db"``, a plain filesystem path, or
                ``":memory:"`` for an isolated in-memory Home.
            policy: Optional retention/ttl settings. Durability is forced to
                ``"sync"``; ``"exit"`` durability is rejected.
            serializer: Optional value serializer (default: JSON).
            max_active_runs: Host work admission — how many Runs a worker
                executes at once. The cap lives in the store, so **passing it
                writes through** (including an explicit ``None`` for
                unlimited) and **omitting it adopts the stored value**. A
                store that was never configured is unlimited. Tunable later
                via the ``max_active_runs`` attribute.
        """
        return cls(uri, policy=policy, serializer=serializer, max_active_runs=max_active_runs)

    @property
    def uri(self) -> str:
        """The Home URI string as passed to ``open()`` (used in refs)."""
        return self._path

    @property
    def max_active_runs(self) -> int | None:
        """Host work admission: concurrent Runs a worker may execute.

        The cap is a **Home-scoped fact in the store**, not a per-instance
        attribute: the worker that enforces it and the operator process that
        tunes or reads it hold different ``RunHome`` objects. Assigning
        writes through immediately, so a worker already polling this Home
        honors the new value on its next claim scan — no restart, no shared
        Python object. Reading returns what the store holds.

        Lowering it never rejects or cancels anything: over-limit Runs stay
        pending and wait in claim order as
        ``WaitingCondition.ADMISSION_LIMITED``. Raising it lets the oldest
        waiting Run in first.

        This counts *Runs a worker executes*, so it is not the same control
        as a ``ProcessLocalLimiter`` (``provider_limit``), which counts
        concurrent calls to an external provider. A claimed Run parked on a
        provider permit is still executing and still holds its slot here.
        None means unlimited (no Run ever reports ADMISSION_LIMITED).
        """
        with self._sync_lock:
            row = self._sync_db().execute(_SELECT_SETTING_SQL, (_MAX_ACTIVE_RUNS_KEY,)).fetchone()
        return _cap_from_row(row)

    @max_active_runs.setter
    def max_active_runs(self, value: int | None) -> None:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ValueError(
                f"max_active_runs must be an int >= 1 concurrent Runs, or None for unlimited; got {value!r}.\n\n"
                "How to fix:\n"
                "  home.max_active_runs = 4     # this Home admits 4 Runs at once\n"
                "  home.max_active_runs = None  # unlimited (the default)"
            )
        with self._sync_lock:
            db = self._sync_db()
            db.execute(_UPSERT_SETTING_SQL, (_MAX_ACTIVE_RUNS_KEY, None if value is None else str(value), _now_iso()))
            db.commit()

    # === Host work admission ===

    async def _admission_is_full(self) -> bool:
        """True when the active-Run cap leaves no slot for new work.

        A claimed submission is one a worker owns end to end — starting,
        executing, parked on a provider permit, or settling — so it holds a
        slot. Pending (queued, scheduled, version-incompatible), exhausted,
        and finished submissions are not claimed and hold none.
        """
        await self._ensure_db()
        async with self._txn_lock():
            free = await self._free_admission_slots()
        return free is not None and free <= 0

    def _admission_is_full_sync(self) -> bool:
        """Sync mirror of ``_admission_is_full``."""
        with self._sync_lock:
            db = self._sync_db()
            cap_row = db.execute(_SELECT_SETTING_SQL, (_MAX_ACTIVE_RUNS_KEY,)).fetchone()
            if _cap_from_row(cap_row) is None:  # uncapped is never full
                return False
            free = _slots_left(cap_row, db.execute(_ACTIVE_RUN_COUNT_SQL).fetchone())
        return free is not None and free <= 0

    async def _free_admission_slots(self) -> int | None:
        """Slots left under the active-Run cap; None when uncapped.

        Both the cap and the count are read inside the caller's transaction,
        so a claim decision uses the numbers that transaction commits
        against — including a cap another process wrote a moment ago.
        """
        cursor = await self._db.execute(_SELECT_SETTING_SQL, (_MAX_ACTIVE_RUNS_KEY,))
        cap_row = await cursor.fetchone()
        if _cap_from_row(cap_row) is None:
            return None
        cursor = await self._db.execute(_ACTIVE_RUN_COUNT_SQL)
        return _slots_left(cap_row, await cursor.fetchone())

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
        if kind == "status" and payload.get("status") in _TERMINAL_STATUS_VALUES:
            self._append_child_settled_sync(db, run_id, payload["status"])

    async def _after_run_mutation(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        await self._append_run_update(run_id, kind, payload)
        if _is_committed_progress(kind, payload):
            await self._db.execute(
                "UPDATE host_submissions SET recovery_attempts = 0 WHERE workflow_id = ? AND recovery_attempts > 0",
                (run_id,),
            )
        if kind == "status" and payload.get("status") in _TERMINAL_STATUS_VALUES:
            await self._append_child_settled(run_id, payload["status"])

    # === batch_updates appends (same-transaction as Batch facts) ===

    def _append_batch_update_sync(self, db: Any, batch_id: str, kind: str, payload: dict[str, Any], item_key: str | None = None) -> None:
        """Append one batch_updates row; caller holds the write transaction.

        Single INSERT...SELECT so the bseq allocation and the insert are one
        statement (no read-then-write race) — same discipline as run_updates.
        """
        db.execute(
            "INSERT INTO batch_updates (batch_id, bseq, kind, item_key, payload, created_at) "
            "SELECT ?, COALESCE(MAX(bseq), 0) + 1, ?, ?, ?, ? FROM batch_updates WHERE batch_id = ?",
            (batch_id, kind, item_key, json.dumps(payload), _now_iso(), batch_id),
        )

    async def _append_batch_update(self, batch_id: str, kind: str, payload: dict[str, Any], item_key: str | None = None) -> None:
        """Append one batch_updates row; caller holds the write transaction."""
        await self._db.execute(
            "INSERT INTO batch_updates (batch_id, bseq, kind, item_key, payload, created_at) "
            "SELECT ?, COALESCE(MAX(bseq), 0) + 1, ?, ?, ?, ? FROM batch_updates WHERE batch_id = ?",
            (batch_id, kind, item_key, json.dumps(payload), _now_iso(), batch_id),
        )

    def _append_child_settled_sync(self, db: Any, run_id: str, status: str) -> None:
        """Mirror a child settling for good onto its Batch, same transaction.

        ``status`` is a terminal ``WorkflowStatus`` value, or
        ``BATCH_OUTCOME_RECOVERY_EXHAUSTED`` when the recovery brake parked
        the child — the same string ``BatchView.outcomes`` reports, so a
        stream consumer reconstructs the view's outcome verbatim.

        No-op for runs without Batch membership. Idempotent per child: a
        repeated settle write (e.g. a resume path) never duplicates the
        child_settled fact, so the per-Batch sequence stays gap-free without
        backpressuring the child's commit path.
        """
        row = db.execute(
            "SELECT batch_id, item_key FROM host_submissions WHERE workflow_id = ?",
            (run_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return
        batch_id, item_key = row
        exists = db.execute(
            "SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = ? AND item_key = ? LIMIT 1",
            (batch_id, _SETTLED_UPDATE_KIND, item_key),
        ).fetchone()
        if exists is not None:
            return
        self._append_batch_update_sync(
            db,
            batch_id,
            _SETTLED_UPDATE_KIND,
            {"item_key": item_key, "workflow_id": run_id, "status": status},
            item_key=item_key,
        )
        self._maybe_trip_tolerance_sync(db, batch_id)

    async def _append_child_settled(self, run_id: str, status: str) -> None:
        """Async mirror of ``_append_child_settled_sync``."""
        cursor = await self._db.execute(
            "SELECT batch_id, item_key FROM host_submissions WHERE workflow_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            return
        batch_id, item_key = row
        exists_cursor = await self._db.execute(
            "SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = ? AND item_key = ? LIMIT 1",
            (batch_id, _SETTLED_UPDATE_KIND, item_key),
        )
        if await exists_cursor.fetchone() is not None:
            return
        await self._append_batch_update(
            batch_id,
            _SETTLED_UPDATE_KIND,
            {"item_key": item_key, "workflow_id": run_id, "status": status},
            item_key=item_key,
        )
        await self._maybe_trip_tolerance(batch_id)

    async def _append_child_unstarted(self, run_id: str) -> None:
        """Record that a Batch item ended unstarted, in the caller's transaction.

        THE unstarted-child fact (PRD 0019 A9). Every path that settles a
        child WITHOUT ever executing it — admission refusing a child of an
        already-tripped Batch, and a stop that landed before first
        execution — appends this row in the SAME transaction as the state
        flip that causes it, so a detached ``watch(batch_ref)`` accounts
        the item from the durable sequence alone. Items the trip fact
        itself named in ``unstarted_items`` never reach here: their flip
        happened inside the trip.

        No-op for runs without Batch membership. Idempotent per item, like
        ``_append_child_settled``: the flip is already once-only (it moves
        the row out of 'pending'/'claimed'), and the guard keeps a replayed
        path from double-accounting the item even so.

        Async-only on purpose: both callers are worker paths
        (``_claim_eligible``, ``_apply_stop_never_started``) that have no
        sync mirror, so a ``_sync`` twin here would be dead code.
        """
        cursor = await self._db.execute(
            "SELECT batch_id, item_key FROM host_submissions WHERE workflow_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            return
        batch_id, item_key = row
        exists_cursor = await self._db.execute(
            "SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = ? AND item_key = ? LIMIT 1",
            (batch_id, _UNSTARTED_UPDATE_KIND, item_key),
        )
        if await exists_cursor.fetchone() is not None:
            return
        await self._append_batch_update(
            batch_id,
            _UNSTARTED_UPDATE_KIND,
            {"item_key": item_key, "workflow_id": run_id},
            item_key=item_key,
        )

    # === Pinned tolerance (ticket 06) ===

    def _maybe_trip_tolerance_sync(self, db: Any, batch_id: str) -> None:
        """Evaluate the pinned tolerance and trip the Batch, same transaction.

        Called immediately after a child fact that can change failure
        truth, INSIDE the caller's transaction (PRD 0019 A9): the child's
        terminal record and the trip land together, so the per-Batch
        sequence stays gap-free and a reader never sees a Batch that should
        have tripped but has not.

        Tripping does three things, all durable at once:

        1. Appends the ``tolerance_tripped`` fact at the next ``bseq``.
        2. Marks every still-pending child finished — those items are
           explicitly unstarted, never fabricated failures (ADR 0004) —
           and names them in the trip fact's ``unstarted_items``.
        3. Leaves claimed children alone: already-claimed work settles. A
           claimed child that never settles (a crash returns it to
           'pending') is refused at ``_claim_eligible``, which appends its
           own ``child_unstarted`` fact — so every manifest item is
           accounted by the durable sequence, not just by the view.

        Idempotent per Batch (a Batch trips exactly once) and a no-op when
        no tolerance was pinned. The Batch keeps no status of its own: a
        trip is a Batch fact, never a ``WorkflowStatus``.
        """
        batch_row = db.execute("SELECT items_json, tolerance_json FROM host_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if batch_row is None or batch_row[1] is None:
            return
        if db.execute(_SELECT_TRIPPED, (batch_id,)).fetchone() is not None:
            return
        (failure_count,) = db.execute(_COUNT_FAILURE_EQUIVALENT, (batch_id,)).fetchone()
        payload = self._trip_payload(batch_row[0], batch_row[1], int(failure_count))
        if payload is None:
            return
        unstarted = [
            str(row[0])
            for row in db.execute(
                "SELECT item_key FROM host_submissions WHERE batch_id = ? AND state = 'pending' ORDER BY rowid",
                (batch_id,),
            ).fetchall()
        ]
        db.execute(
            "UPDATE host_submissions SET state = 'finished', finished_at = ? WHERE batch_id = ? AND state = 'pending'",
            (_now_iso(), batch_id),
        )
        self._append_batch_update_sync(db, batch_id, _TRIP_UPDATE_KIND, {**payload, "unstarted_items": unstarted})

    async def _maybe_trip_tolerance(self, batch_id: str) -> None:
        """Async mirror of ``_maybe_trip_tolerance_sync``; caller holds the transaction."""
        batch_cursor = await self._db.execute("SELECT items_json, tolerance_json FROM host_batches WHERE batch_id = ?", (batch_id,))
        batch_row = await batch_cursor.fetchone()
        if batch_row is None or batch_row[1] is None:
            return
        tripped_cursor = await self._db.execute(_SELECT_TRIPPED, (batch_id,))
        if await tripped_cursor.fetchone() is not None:
            return
        count_cursor = await self._db.execute(_COUNT_FAILURE_EQUIVALENT, (batch_id,))
        (failure_count,) = await count_cursor.fetchone()
        payload = self._trip_payload(batch_row[0], batch_row[1], int(failure_count))
        if payload is None:
            return
        pending_cursor = await self._db.execute(
            "SELECT item_key FROM host_submissions WHERE batch_id = ? AND state = 'pending' ORDER BY rowid",
            (batch_id,),
        )
        unstarted = [str(row[0]) for row in await pending_cursor.fetchall()]
        await self._db.execute(
            "UPDATE host_submissions SET state = 'finished', finished_at = ? WHERE batch_id = ? AND state = 'pending'",
            (_now_iso(), batch_id),
        )
        await self._append_batch_update(batch_id, _TRIP_UPDATE_KIND, {**payload, "unstarted_items": unstarted})

    @staticmethod
    def _trip_payload(items_json: str, tolerance_json: str, failure_count: int) -> dict[str, Any] | None:
        """Return the trip fact payload, or None when the Batch has not tripped.

        The percentage denominator is the pinned manifest length, read from
        the immutable manifest row — never a live count of remaining work.
        """
        tolerance = BatchTolerance.from_dict(json.loads(tolerance_json))
        total_items = len(json.loads(items_json))
        if not tolerance_trips(tolerance, failure_count=failure_count, total_items=total_items):
            return None
        return {
            "failed": failure_count,
            "total_items": total_items,
            "max_failed": tolerance.max_failed,
            "max_failed_percent": tolerance.max_failed_percent,
        }

    def _batch_tripped_sync(self, batch_id: str) -> bool:
        """True when a durable ``tolerance_tripped`` fact exists for this Batch."""
        with self._sync_lock:
            db = self._sync_db()
            return db.execute(_SELECT_TRIPPED, (batch_id,)).fetchone() is not None

    async def _batch_tripped(self, batch_id: str) -> bool:
        """Async mirror of ``_batch_tripped_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(_SELECT_TRIPPED, (batch_id,))
            return await cursor.fetchone() is not None

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
        batch_id: str | None = None,
        item_key: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Insert one submission plus its 'submitted' update, atomically.

        Returns ``(created, row)``. When a submission already exists for
        ``workflow_id`` nothing is written: a fingerprint-identical
        nonterminal row returns ``(False, existing)`` (use-existing dedup),
        terminal reuse raises ``AlreadyTerminalError``, and a fingerprint
        mismatch raises ``WorkflowIdConflictError``. Reusing an id owned by
        a Batch is likewise a conflict (AlreadyTerminalError once that
        Batch is settled) — run and Batch workflow ids share one namespace.
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
                batch_row = db.execute(
                    f"SELECT {_BATCH_COLS} FROM host_batches WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                if batch_row is not None:
                    if _children_settled_rows(
                        db.execute(
                            "SELECT s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                            (batch_row[0],),
                        ).fetchall()
                    ):
                        raise AlreadyTerminalError(workflow_id)
                    raise WorkflowIdConflictError(workflow_id, "an existing Batch owns this workflow_id")
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
                        batch_id,
                        item_key,
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
        batch_id: str | None = None,
        item_key: str | None = None,
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
                batch_cursor = await self._db.execute(
                    f"SELECT {_BATCH_COLS} FROM host_batches WHERE workflow_id = ?",
                    (workflow_id,),
                )
                batch_row = await batch_cursor.fetchone()
                if batch_row is not None:
                    children_cursor = await self._db.execute(
                        "SELECT s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                        (batch_row[0],),
                    )
                    if _children_settled_rows(await children_cursor.fetchall()):
                        raise AlreadyTerminalError(workflow_id)
                    raise WorkflowIdConflictError(workflow_id, "an existing Batch owns this workflow_id")
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
                        batch_id,
                        item_key,
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

    def _count_batch_retries_sync(self, batch_id: str) -> int:
        """Count Batches minted as a rerun of ``batch_id`` (rerun id derivation).

        Mirrors the Run rule (``<source>-retry-N`` with N = count + 1) so a
        Batch rerun and a Run rerun read the same way.
        """
        with self._sync_lock:
            db = self._sync_db()
            (count,) = db.execute("SELECT COUNT(*) FROM host_batches WHERE retry_of = ?", (batch_id,)).fetchone()
            return int(count or 0)

    async def _count_batch_retries(self, batch_id: str) -> int:
        """Async mirror of ``_count_batch_retries_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute("SELECT COUNT(*) FROM host_batches WHERE retry_of = ?", (batch_id,))
            (count,) = await cursor.fetchone()
            return int(count or 0)

    # === Batches (ticket 05) ===

    def _submit_batch_sync(
        self,
        batch_id: str,
        workflow_id: str,
        definition_name: str,
        def_version: str,
        def_struct_hash: str,
        items: list[tuple[str, str]],
        tolerance_json: str | None,
        start_at: str | None,
        source_ref: str | None,
        *,
        fingerprint: str,
        recovery_cap: int = 3,
        batch_retry_of: str | None = None,
        child_retry_of: dict[str, str] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Accept one Batch atomically: manifest + child submissions + bseq 1.

        ONE transaction persists the host_batches manifest row, one child
        submission (with its own 'submitted' run update) per item key, and
        the 'manifest' batch_updates row at bseq 1 — a partial Batch can
        never appear accepted. ``items`` is a manifest-ordered list of
        ``(item_key, inputs_json)`` pairs.

        ``batch_retry_of`` records Batch lineage when an item-scoped rerun
        mints this manifest from a source Batch (the source batch id);
        ``child_retry_of`` maps item key -> source child workflow id so each
        new child records ``retry_of`` against its source child. Both are
        None for ordinary submissions.

        Returns ``(created, batch_row)``. Dedup mirrors the Run rules: a
        fingerprint-identical nonterminal resubmission returns
        ``(False, existing)`` (use-existing), a settled Batch raises
        ``AlreadyTerminalError``, and a fingerprint mismatch raises
        ``WorkflowIdConflictError``. Run and Batch workflow ids share one
        namespace: reusing an id owned by a plain Run submission is a
        conflict (AlreadyTerminalError once that Run is finished), and so
        is a child workflow id owned by unrelated existing work.
        """
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                existing_batch_row = db.execute(
                    f"SELECT {_BATCH_COLS} FROM host_batches WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                if existing_batch_row is not None:
                    existing = _row_to_batch(existing_batch_row)
                    settled = _children_settled_rows(
                        db.execute(
                            "SELECT s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                            (existing["batch_id"],),
                        ).fetchall()
                    )
                    if settled:
                        raise AlreadyTerminalError(workflow_id)
                    if existing["fingerprint"] != fingerprint:
                        aspect = batch_mismatch_aspect(
                            existing,
                            definition_name=definition_name,
                            def_version=def_version,
                            def_struct_hash=def_struct_hash,
                            items_canonical=canonical_json({key: json.loads(inputs_json) for key, inputs_json in items}),
                            tolerance_json=tolerance_json,
                            start_at=start_at,
                        )
                        raise WorkflowIdConflictError(workflow_id, aspect)
                    db.rollback()
                    return False, existing
                submission_row = db.execute(
                    "SELECT state FROM host_submissions WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                if submission_row is not None:
                    if submission_row[0] == "finished":
                        raise AlreadyTerminalError(workflow_id)
                    raise WorkflowIdConflictError(workflow_id, "an existing Run owns this workflow_id")
                child_ids = [(key, f"{workflow_id}:{key}", inputs_json) for key, inputs_json in items]
                for key, child_workflow_id, _inputs_json in child_ids:
                    collision = db.execute(
                        "SELECT 1 FROM host_submissions WHERE workflow_id = ? UNION SELECT 1 FROM host_batches WHERE workflow_id = ?",
                        (child_workflow_id, child_workflow_id),
                    ).fetchone()
                    if collision is not None:
                        raise WorkflowIdConflictError(
                            child_workflow_id,
                            f"the child workflow id for item {key!r} collides with existing work in this Run Home",
                        )
                now = _now_iso()
                items_json = json.dumps({key: json.loads(inputs_json) for key, inputs_json in items})
                db.execute(
                    f"INSERT INTO host_batches ({_BATCH_COLS}) VALUES ({_BATCH_PLACEHOLDERS})",
                    (
                        batch_id,
                        workflow_id,
                        definition_name,
                        def_version,
                        def_struct_hash,
                        items_json,
                        tolerance_json,
                        start_at,
                        fingerprint,
                        source_ref,
                        now,
                        batch_retry_of,
                    ),
                )
                for key, child_workflow_id, inputs_json in child_ids:
                    db.execute(
                        f"INSERT INTO host_submissions ({_SUBMISSION_COLS}) VALUES ({_SUBMISSION_PLACEHOLDERS})",
                        (
                            child_workflow_id,
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
                            start_fingerprint(
                                DefinitionId(definition_name, def_version, def_struct_hash),
                                inputs_json,
                                start_at,
                            ),
                            "compatible",
                            (child_retry_of or {}).get(key),
                            None,
                            None,
                            0,
                            batch_id,
                            key,
                        ),
                    )
                    self._append_run_update_sync(
                        db,
                        child_workflow_id,
                        "submitted",
                        {"definition_name": definition_name, "workflow_id": child_workflow_id, "batch_id": batch_id, "item_key": key},
                    )
                self._append_batch_update_sync(
                    db,
                    batch_id,
                    "manifest",
                    {
                        "batch_id": batch_id,
                        "workflow_id": workflow_id,
                        "definition_id": {
                            "name": definition_name,
                            "deployment_version": def_version,
                            "structural_hash": def_struct_hash,
                        },
                        "item_keys": [key for key, _ in items],
                        "tolerance": json.loads(tolerance_json) if tolerance_json is not None else None,
                        "start_at": start_at,
                        "source_ref": source_ref,
                        "retry_of": batch_retry_of,
                    },
                )
                db.commit()
            except BaseException:
                self._rollback_sync(db)
                raise
        row = self._get_batch_sync(batch_id)
        assert row is not None
        return True, row

    async def _submit_batch(
        self,
        batch_id: str,
        workflow_id: str,
        definition_name: str,
        def_version: str,
        def_struct_hash: str,
        items: list[tuple[str, str]],
        tolerance_json: str | None,
        start_at: str | None,
        source_ref: str | None,
        *,
        fingerprint: str,
        recovery_cap: int = 3,
        batch_retry_of: str | None = None,
        child_retry_of: dict[str, str] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Async mirror of ``_submit_batch_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                batch_cursor = await self._db.execute(
                    f"SELECT {_BATCH_COLS} FROM host_batches WHERE workflow_id = ?",
                    (workflow_id,),
                )
                existing_batch_row = await batch_cursor.fetchone()
                if existing_batch_row is not None:
                    existing = _row_to_batch(existing_batch_row)
                    settled_cursor = await self._db.execute(
                        "SELECT s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                        (existing["batch_id"],),
                    )
                    if _children_settled_rows(await settled_cursor.fetchall()):
                        raise AlreadyTerminalError(workflow_id)
                    if existing["fingerprint"] != fingerprint:
                        aspect = batch_mismatch_aspect(
                            existing,
                            definition_name=definition_name,
                            def_version=def_version,
                            def_struct_hash=def_struct_hash,
                            items_canonical=canonical_json({key: json.loads(inputs_json) for key, inputs_json in items}),
                            tolerance_json=tolerance_json,
                            start_at=start_at,
                        )
                        raise WorkflowIdConflictError(workflow_id, aspect)
                    await self._db.rollback()
                    return False, existing
                submission_cursor = await self._db.execute(
                    "SELECT state FROM host_submissions WHERE workflow_id = ?",
                    (workflow_id,),
                )
                submission_row = await submission_cursor.fetchone()
                if submission_row is not None:
                    if submission_row[0] == "finished":
                        raise AlreadyTerminalError(workflow_id)
                    raise WorkflowIdConflictError(workflow_id, "an existing Run owns this workflow_id")
                child_ids = [(key, f"{workflow_id}:{key}", inputs_json) for key, inputs_json in items]
                for key, child_workflow_id, _inputs_json in child_ids:
                    collision_cursor = await self._db.execute(
                        "SELECT 1 FROM host_submissions WHERE workflow_id = ? UNION SELECT 1 FROM host_batches WHERE workflow_id = ?",
                        (child_workflow_id, child_workflow_id),
                    )
                    if await collision_cursor.fetchone() is not None:
                        raise WorkflowIdConflictError(
                            child_workflow_id,
                            f"the child workflow id for item {key!r} collides with existing work in this Run Home",
                        )
                now = _now_iso()
                items_json = json.dumps({key: json.loads(inputs_json) for key, inputs_json in items})
                await self._db.execute(
                    f"INSERT INTO host_batches ({_BATCH_COLS}) VALUES ({_BATCH_PLACEHOLDERS})",
                    (
                        batch_id,
                        workflow_id,
                        definition_name,
                        def_version,
                        def_struct_hash,
                        items_json,
                        tolerance_json,
                        start_at,
                        fingerprint,
                        source_ref,
                        now,
                        batch_retry_of,
                    ),
                )
                for key, child_workflow_id, inputs_json in child_ids:
                    await self._db.execute(
                        f"INSERT INTO host_submissions ({_SUBMISSION_COLS}) VALUES ({_SUBMISSION_PLACEHOLDERS})",
                        (
                            child_workflow_id,
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
                            start_fingerprint(
                                DefinitionId(definition_name, def_version, def_struct_hash),
                                inputs_json,
                                start_at,
                            ),
                            "compatible",
                            (child_retry_of or {}).get(key),
                            None,
                            None,
                            0,
                            batch_id,
                            key,
                        ),
                    )
                    await self._append_run_update(
                        child_workflow_id,
                        "submitted",
                        {"definition_name": definition_name, "workflow_id": child_workflow_id, "batch_id": batch_id, "item_key": key},
                    )
                await self._append_batch_update(
                    batch_id,
                    "manifest",
                    {
                        "batch_id": batch_id,
                        "workflow_id": workflow_id,
                        "definition_id": {
                            "name": definition_name,
                            "deployment_version": def_version,
                            "structural_hash": def_struct_hash,
                        },
                        "item_keys": [key for key, _ in items],
                        "tolerance": json.loads(tolerance_json) if tolerance_json is not None else None,
                        "start_at": start_at,
                        "source_ref": source_ref,
                        "retry_of": batch_retry_of,
                    },
                )
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise
        row = await self._get_batch(batch_id)
        assert row is not None
        return True, row

    def _get_batch_sync(self, batch_id: str) -> dict[str, Any] | None:
        with self._sync_lock:
            db = self._sync_db()
            row = db.execute(
                f"SELECT {_BATCH_COLS} FROM host_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            return _row_to_batch(row) if row is not None else None

    async def _get_batch(self, batch_id: str) -> dict[str, Any] | None:
        """Async mirror of ``_get_batch_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(
                f"SELECT {_BATCH_COLS} FROM host_batches WHERE batch_id = ?",
                (batch_id,),
            )
            row = await cursor.fetchone()
            return _row_to_batch(row) if row is not None else None

    def _batch_child_rows_sync(self, batch_id: str) -> dict[str, tuple[dict[str, Any], Run | None]]:
        """Batch children joined with their runs rows, keyed by item key."""
        with self._sync_lock:
            db = self._sync_db()
            rows: dict[str, tuple[dict[str, Any], Run | None]] = {}
            cursor = db.execute(
                f"SELECT {_QUALIFIED_SUBMISSION_COLS}, {_QUALIFIED_RUN_COLS} FROM host_submissions s "
                "LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                (batch_id,),
            )
            sub_count = len(_SUBMISSION_COLS.split(", "))
            for row in cursor.fetchall():
                submission = _row_to_submission(row[:sub_count])
                run = self._row_to_run(row[sub_count:]) if row[sub_count] is not None else None
                rows[submission["item_key"]] = (submission, run)
            return rows

    async def _batch_child_rows(self, batch_id: str) -> dict[str, tuple[dict[str, Any], Run | None]]:
        """Async mirror of ``_batch_child_rows_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            rows: dict[str, tuple[dict[str, Any], Run | None]] = {}
            cursor = await self._db.execute(
                f"SELECT {_QUALIFIED_SUBMISSION_COLS}, {_QUALIFIED_RUN_COLS} FROM host_submissions s "
                "LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                (batch_id,),
            )
            sub_count = len(_SUBMISSION_COLS.split(", "))
            for row in await cursor.fetchall():
                submission = _row_to_submission(row[:sub_count])
                run = self._row_to_run(row[sub_count:]) if row[sub_count] is not None else None
                rows[submission["item_key"]] = (submission, run)
            return rows

    def _read_batch_updates_sync(self, batch_id: str, after_bseq: int = 0) -> list[tuple[int, str, str, str]]:
        """Read batch_updates rows with bseq > after_bseq, in bseq order."""
        with self._sync_lock:
            db = self._sync_db()
            cursor = db.execute(
                "SELECT bseq, kind, payload, created_at FROM batch_updates WHERE batch_id = ? AND bseq > ? ORDER BY bseq",
                (batch_id, after_bseq),
            )
            return [(int(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in cursor.fetchall()]

    async def _read_batch_updates(self, batch_id: str, after_bseq: int = 0) -> list[tuple[int, str, str, str]]:
        """Async mirror of ``_read_batch_updates_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(
                "SELECT bseq, kind, payload, created_at FROM batch_updates WHERE batch_id = ? AND bseq > ? ORDER BY bseq",
                (batch_id, after_bseq),
            )
            rows = await cursor.fetchall()
            return [(int(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows]

    async def _batch_settlement(self, batch_id: str) -> tuple[bool, bool]:
        """Return (stopped, all_children_settled) for watch termination."""
        await self._ensure_db()
        async with self._txn_lock():
            stopped_cursor = await self._db.execute(
                "SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = 'stopped' LIMIT 1",
                (batch_id,),
            )
            stopped = await stopped_cursor.fetchone() is not None
            children_cursor = await self._db.execute(
                "SELECT s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                (batch_id,),
            )
            settled = _children_settled_rows(await children_cursor.fetchall())
            return stopped, settled

    def _write_batch_stop_sync(self, batch_id: str, info: Any, source_ref: str | None = None) -> bool:
        """Record a durable Batch stop, atomically, in ONE transaction.

        Appends the 'stopped' batch_updates row and writes a durable stop
        command (with its 'command' run update) for every child that has
        not settled yet — pending children then finish via the existing
        stop-before-start gate (no runs row invented, and the gate appends
        each one's ``child_unstarted`` Batch fact, since the 'stopped' row
        names no items) and executing children receive the stop on the
        worker's next scan. Children with an unapplied stop already keep
        their first stop (first stop wins).

        Returns True when the Batch stop was newly written; False when the
        Batch was already stopped (duplicate). Raises ``HostError`` for an
        unknown batch and ``AlreadyTerminalError`` when every child is
        already settled.
        """
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                batch_row = db.execute(
                    f"SELECT {_BATCH_COLS} FROM host_batches WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()
                if batch_row is None:
                    raise HostError(f"Cannot stop batch {batch_id!r}: no such batch in this Run Home.")
                batch = _row_to_batch(batch_row)
                child_rows = db.execute(
                    "SELECT s.workflow_id, s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                    (batch_id,),
                ).fetchall()
                if _children_settled_rows([(state, status) for _wf, state, status in child_rows]):
                    raise AlreadyTerminalError(batch["workflow_id"])
                stopped_row = db.execute(
                    "SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = 'stopped' LIMIT 1",
                    (batch_id,),
                ).fetchone()
                if stopped_row is not None:
                    db.rollback()
                    return False
                now = _now_iso()
                self._append_batch_update_sync(
                    db,
                    batch_id,
                    "stopped",
                    {"verb": "stop", "info": info, "source_ref": source_ref},
                )
                for child_workflow_id, _state, _status in child_rows:
                    if is_child_settled(_state, _status):
                        continue
                    existing = db.execute(
                        "SELECT id FROM host_commands WHERE run_id = ? AND verb = 'stop' AND applied_at IS NULL LIMIT 1",
                        (child_workflow_id,),
                    ).fetchone()
                    if existing is not None:
                        continue
                    db.execute(
                        "INSERT INTO host_commands (run_id, verb, payload, source_ref, created_at) VALUES (?, 'stop', ?, ?, ?)",
                        (child_workflow_id, json.dumps({"info": info}), source_ref, now),
                    )
                    self._append_run_update_sync(db, child_workflow_id, "command", {"verb": "stop", "info": info})
                db.commit()
                return True
            except BaseException:
                self._rollback_sync(db)
                raise

    async def _write_batch_stop(self, batch_id: str, info: Any, source_ref: str | None = None) -> bool:
        """Async mirror of ``_write_batch_stop_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                batch_cursor = await self._db.execute(
                    f"SELECT {_BATCH_COLS} FROM host_batches WHERE batch_id = ?",
                    (batch_id,),
                )
                batch_row = await batch_cursor.fetchone()
                if batch_row is None:
                    raise HostError(f"Cannot stop batch {batch_id!r}: no such batch in this Run Home.")
                batch = _row_to_batch(batch_row)
                children_cursor = await self._db.execute(
                    "SELECT s.workflow_id, s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                    (batch_id,),
                )
                child_rows = await children_cursor.fetchall()
                if _children_settled_rows([(state, status) for _wf, state, status in child_rows]):
                    raise AlreadyTerminalError(batch["workflow_id"])
                stopped_cursor = await self._db.execute(
                    "SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = 'stopped' LIMIT 1",
                    (batch_id,),
                )
                if await stopped_cursor.fetchone() is not None:
                    await self._db.rollback()
                    return False
                now = _now_iso()
                await self._append_batch_update(
                    batch_id,
                    "stopped",
                    {"verb": "stop", "info": info, "source_ref": source_ref},
                )
                for child_workflow_id, _state, _status in child_rows:
                    if is_child_settled(_state, _status):
                        continue
                    existing_cursor = await self._db.execute(
                        "SELECT id FROM host_commands WHERE run_id = ? AND verb = 'stop' AND applied_at IS NULL LIMIT 1",
                        (child_workflow_id,),
                    )
                    if await existing_cursor.fetchone() is not None:
                        continue
                    await self._db.execute(
                        "INSERT INTO host_commands (run_id, verb, payload, source_ref, created_at) VALUES (?, 'stop', ?, ?, ?)",
                        (child_workflow_id, json.dumps({"info": info}), source_ref, now),
                    )
                    await self._append_run_update(child_workflow_id, "command", {"verb": "stop", "info": info})
                await self._db.commit()
                return True
            except BaseException:
                await self._rollback_async()
                raise

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

        This is also the single admission choke point a tolerance trip
        closes: a child of a tripped Batch is never claimed. It is finished
        without executing instead, so a child that a crash returned to
        'pending' after its Batch tripped becomes an explicit unstarted
        item rather than sitting claimable forever — and a durable
        ``child_unstarted`` Batch fact commits with that flip, so the
        per-Batch sequence accounts the item too (PRD 0019 A9).

        It is also where host work admission applies: at most the Home's
        stored ``max_active_runs`` submissions are claimed at once, counting
        the claims already outstanding. Over-limit work is left pending and
        waits in claim order (oldest ``created_at`` first, ``rowid``
        breaking ties) — it is never rejected or cancelled, and it is
        reported as
        ``WaitingCondition.ADMISSION_LIMITED``. A full cap never starves the
        non-claiming dispositions: tripped-Batch children and
        version-incompatible rows are still settled on this scan.
        """
        served_set = frozenset(served)
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                cursor = await self._db.execute(
                    f"SELECT {_SUBMISSION_COLS} FROM host_submissions "
                    "WHERE state = 'pending' AND compat_state = 'compatible' AND (start_at IS NULL OR start_at <= ?) "
                    # rowid breaks created_at ties so claim order is TOTAL:
                    # two submissions accepted inside the same microsecond
                    # would otherwise be ordered arbitrarily, and "over-limit
                    # work waits in claim order" would be undefined for them.
                    "ORDER BY created_at, rowid LIMIT ?",
                    (now_iso, limit),
                )
                submissions = [_row_to_submission(row) for row in await cursor.fetchall()]
                free_slots = await self._free_admission_slots()
                tripped = await self._tripped_batch_ids({s["batch_id"] for s in submissions if s["batch_id"] is not None})
                claimed: list[dict[str, Any]] = []
                for submission in submissions:
                    if submission["batch_id"] in tripped:
                        result = await self._db.execute(
                            "UPDATE host_submissions SET state = 'finished', finished_at = ? WHERE workflow_id = ? AND state = 'pending'",
                            (now_iso, submission["workflow_id"]),
                        )
                        if result.rowcount == 1:
                            # A9: this item ends unstarted AFTER the trip
                            # fact already listed its unstarted items, so it
                            # gets its own durable row in the SAME
                            # transaction as the state flip. Without it a
                            # detached watch() would never learn the item's
                            # outcome and the stream could not reconstruct
                            # the view.
                            await self._append_child_unstarted(submission["workflow_id"])
                        continue
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
                    if free_slots is not None and free_slots <= 0:
                        # Over the active-Run cap: leave it pending. Claim
                        # order is the scan order, so the oldest waiting
                        # submission takes the next freed slot.
                        continue
                    result = await self._db.execute(
                        "UPDATE host_submissions SET state = 'claimed', claimed_at = ? WHERE workflow_id = ? AND state = 'pending'",
                        (now_iso, submission["workflow_id"]),
                    )
                    if result.rowcount == 1:
                        claimed.append(submission)
                        if free_slots is not None:
                            free_slots -= 1
                await self._db.commit()
                return claimed
            except BaseException:
                await self._rollback_async()
                raise

    async def _tripped_batch_ids(self, batch_ids: Collection[str]) -> frozenset[str]:
        """Which of ``batch_ids`` have a durable trip; caller holds the transaction."""
        ids = list(batch_ids)
        if not ids:
            return frozenset()
        placeholders = ", ".join("?" for _ in ids)
        cursor = await self._db.execute(
            f"SELECT DISTINCT batch_id FROM batch_updates WHERE kind = '{_TRIP_UPDATE_KIND}' AND batch_id IN ({placeholders})",
            ids,
        )
        return frozenset(str(row[0]) for row in await cursor.fetchall())

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

    async def _reset_unstarted_run(self, run_id: str) -> bool:
        """Delete a runs row that carries zero execution history.

        A claimed run killed before its first committed step leaves an
        empty ``active`` runs row that can neither resume (checkpoint state
        has no seed inputs) nor restart (input override is forbidden).
        Deleting the history-less row lets the worker start fresh from the
        submission's pinned inputs; the submission (and its recorded
        lineage) stays the source of truth. Any committed step or
        attempt-ledger row — or a non-active status — keeps the runs row;
        that work resumes through the normal crash-recovery path instead.

        Returns True when the row was deleted. The deletion and its durable
        ``run_reset`` update commit in one transaction.
        """
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                result = await self._db.execute(
                    "DELETE FROM runs WHERE id = ? AND status = 'active' "
                    "AND NOT EXISTS (SELECT 1 FROM steps WHERE run_id = ?) "
                    "AND NOT EXISTS (SELECT 1 FROM attempt_series WHERE run_id = ?)",
                    (run_id, run_id, run_id),
                )
                if result.rowcount == 1:
                    await self._append_run_update(
                        run_id,
                        "run_reset",
                        {"reason": "claimed run lost before its first committed step; starting fresh from pinned inputs"},
                    )
                await self._db.commit()
                return result.rowcount == 1
            except BaseException:
                await self._rollback_async()
                raise

    async def _restart_scan(self) -> None:
        """Re-adopt unfinished claimed submissions on worker startup.

        Claimed submissions whose run settled terminally are marked
        finished. Every other claimed submission is a recovery attempt and
        gets ``recovery_attempts += 1``: when the incremented count reaches
        the submission's ``recovery_cap`` it is parked as 'exhausted' (a
        durable ``recovery_exhausted`` run update — and, for a Batch child,
        a ``child_settled`` Batch fact — is appended in the same
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
                        # A parked child is a settled, failure-equivalent
                        # child OUTCOME (PRD 0019), so it is Batch truth:
                        # its Batch learns it as a child_settled fact whose
                        # status is the same "recovery_exhausted" string
                        # BatchView.outcomes reports, in this same
                        # transaction. That fact can also trip the Batch,
                        # like any other child fact. Without it the view
                        # counted the parked child and the durable stream
                        # stayed silent, so a detached watch() under-
                        # accounted the manifest (A9).
                        await self._append_child_settled(workflow_id, BATCH_OUTCOME_RECOVERY_EXHAUSTED)
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

        A Batch child stopped this way is an explicit unstarted item, so
        the flip commits with its own ``child_unstarted`` Batch fact (PRD
        0019 A9). The ``stopped`` fact carries only the verb and info — it
        never names which items ended unstarted — so without this row a
        detached ``watch(batch_ref)`` could not account those items from
        the stream alone.
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
                result = await self._db.execute(
                    "UPDATE host_submissions SET state = 'finished', finished_at = ? WHERE workflow_id = ? AND state IN ('pending', 'claimed')",
                    (now, workflow_id),
                )
                if result.rowcount == 1:
                    # Only a real transition emits, so a Batch item is never
                    # accounted twice by the stream.
                    await self._append_child_unstarted(workflow_id)
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
