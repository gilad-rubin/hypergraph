"""SQLite-based checkpointer using aiosqlite.

Two halves live in this file — an async one over ``aiosqlite`` and a sync one
over stdlib ``sqlite3`` — and they are deliberately NOT copies of each other:
the sync half takes a per-thread lock and opens ``BEGIN IMMEDIATE``
explicitly, the async half serializes coroutines on one shared connection.
What they must never differ about is the SQL and the records. So no method
here spells a statement or a parameter tuple of its own: statements are the
module constants and builders below, records are decoded and encoded in
``_rows``, retention is planned and carried out in ``_retention``. A change
to what is stored is therefore one edit, and drift between the halves is an
edit that fails to compile rather than a bug that shows up in production.

Nor does a write method spell its own transaction: each half enters one
through ``_write_txn`` (async) or ``_write_txn_sync`` (sync), so the four
obligations a write path owes — the half's lock, ``BEGIN IMMEDIATE`` before
any validation, commit on normal exit, rollback on any ``BaseException`` —
are stated once per half instead of once per method.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from hypergraph.checkpointers._lineage import build_lineage
from hypergraph.checkpointers._migrate import ensure_schema
from hypergraph.checkpointers._retention import (
    BASELINE_NODE_NAME,
    BASELINE_NODE_TYPE,
    RETENTION_ROW_COLS,
    RetentionPlan,
    RetentionRow,
    baseline_step_params,
    compaction_deletes,
    decode_retention_rows,
    plan_retention,
)
from hypergraph.checkpointers._rows import ATTEMPT_RECORD_COLS as _ATTEMPT_RECORD_COLS
from hypergraph.checkpointers._rows import ATTEMPT_SERIES_COLS as _ATTEMPT_SERIES_COLS
from hypergraph.checkpointers._rows import NODE_BOUNDARY_COLS as _NODE_BOUNDARY_COLS
from hypergraph.checkpointers._rows import PAUSE_SLOT_COLS as _PAUSE_SLOT_COLS
from hypergraph.checkpointers._rows import RUNS_COLS as _RUNS_COLS
from hypergraph.checkpointers._rows import STEPS_COLS as _STEPS_COLS
from hypergraph.checkpointers._rows import (
    attempt_final_params,
    attempt_outcome_params,
    attempt_record_insert_params,
    attempt_series_insert_params,
    deserialize_run_inputs,
    pause_slot_insert_params,
    pending_node_params,
    placeholders,
    row_to_attempt_record,
    row_to_attempt_series,
    row_to_node_boundary,
    row_to_pause_slot,
    row_to_run,
    row_to_step,
    run_upsert,
    serialize_run_inputs,
    step_upsert_params,
)
from hypergraph.checkpointers.base import (
    _UNSET,
    Checkpointer,
    CheckpointPolicy,
    _check_closable,
    _check_close_request,
    _check_no_live_reservation,
    _check_no_open_series,
    _check_recordable_outcome,
    _check_reservation,
    _check_run_exists,
    _check_settlement,
    _lost_settlement_race,
    _new_attempt_series_id,
    _normalize_since,
    _require_series,
    _require_started,
    _resolve_fork_workflow_id,
)
from hypergraph.checkpointers.presenters import render_checkpointer_explorer_html
from hypergraph.checkpointers.serializers import JsonSerializer, Serializer
from hypergraph.checkpointers.types import (
    NO_RUN_TOTALS,
    AttemptError,
    AttemptLedgerError,
    AttemptRecord,
    AttemptSeries,
    AttemptStatus,
    Checkpoint,
    LineageView,
    NodeBoundary,
    PauseSlot,
    PendingNode,
    Run,
    RunTable,
    RunTotals,
    StepFailure,
    StepRecord,
    StepTable,
    WorkflowStatus,
)

_logger = logging.getLogger(__name__)

_STEP_TIME_ORDER = "COALESCE(completed_at, created_at), created_at, id"
_STEP_TIME_ORDER_DESC = "COALESCE(completed_at, created_at) DESC, created_at DESC, id DESC"
_STEP_TIME_ORDER_DESC_WITH_ALIAS = "COALESCE(s.completed_at, s.created_at) DESC, s.created_at DESC, s.id DESC"
# SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds; chunk
# well under it so a large Batch read never trips the host's sqlite limit.
_MAX_SQL_VARIABLES = 500

#: How long a writer waits for another writer's lock before raising
#: ``database is locked``. Stated rather than inherited, because the driver
#: default (5 s) is a number nobody chose for this store and WAL does not
#: cover the case: WAL removes reader/writer blocking, not writer/writer
#: contention. A Run Home legally carries several worker processes now, each
#: opening short ``BEGIN IMMEDIATE`` transactions to claim, renew a lease,
#: reclaim an expired one, or commit a step — every one of which is
#: milliseconds long, so a wait this generous means "the holder is wedged",
#: never "the machine is busy". It applies to every connection this module
#: opens: the async one, the cached sync one, and the short-lived migration
#: connection, which is the one that would otherwise fail a process START
#: because another worker happened to be mid-write.
SQLITE_BUSY_TIMEOUT_MS = 30_000
_BUSY_TIMEOUT_PRAGMA = f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}"
#: How often the WAL conversion re-tries while another connection holds the
#: database lock. See ``_ensure_wal`` for why it cannot simply wait.
_WAL_RETRY_INTERVAL = 0.01
#: Every multi-statement write opens with this: a transaction that takes the
#: write lock up front, so a competing writer on the OTHER connection blocks
#: and then re-validates against committed truth instead of deciding on a
#: stale snapshot.
_BEGIN_IMMEDIATE = "BEGIN IMMEDIATE"
#: Defense-in-depth for same-store references (steps.run_id, fork/retry
#: lineage, attempt ledger); cross-store lineage columns
#: (runs.parent_run_id, steps.child_run_id) carry no FK since schema v5. Set
#: on EVERY connection this module opens, and named once so the async and
#: per-thread sync connections cannot enforce different rules on one
#: database.
_FOREIGN_KEYS_PRAGMA = "PRAGMA foreign_keys=ON"
_PUBLIC_STEP_FILTER = f"node_name != '{BASELINE_NODE_NAME}' AND (node_type IS NULL OR node_type != '{BASELINE_NODE_TYPE}')"
_PUBLIC_STEP_FILTER_WITH_ALIAS = f"s.node_name != '{BASELINE_NODE_NAME}' AND (s.node_type IS NULL OR s.node_type != '{BASELINE_NODE_TYPE}')"

# === Runs ===
_RUN_UPSERT_SQL = (
    "INSERT INTO runs (id, status, graph_name, created_at, parent_run_id, forked_from, fork_superstep, retry_of, retry_index, config, inputs_data) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(id) DO UPDATE SET status = ?, graph_name = ?, duration_ms = NULL, node_count = 0, "
    "error_count = 0, completed_at = NULL, parent_run_id = ?, forked_from = ?, "
    "fork_superstep = ?, retry_of = ?, retry_index = ?, config = ?, "
    "inputs_data = COALESCE(runs.inputs_data, ?)"
)
_RUN_BY_ID_SQL = f"SELECT {_RUNS_COLS} FROM runs WHERE id = ?"
_RUN_EXISTS_SQL = "SELECT 1 FROM runs WHERE id = ?"
_RUN_STATUS_SQL = "SELECT status FROM runs WHERE id = ?"
_RUN_INPUTS_SQL = "SELECT inputs_data FROM runs WHERE id = ?"
_RUN_COUNT_SQL = "SELECT COUNT(*) FROM runs"
_LINEAGE_CHILDREN_SQL = f"SELECT {_RUNS_COLS} FROM runs WHERE forked_from = ? OR retry_of = ? ORDER BY created_at ASC LIMIT ?"

# === Steps ===
_STEP_UPSERT_SQL = """
    INSERT INTO steps (
        run_id, superstep, node_name, step_index, status,
        input_versions, values_data, duration_ms, cached,
        decision, error, node_type, created_at, completed_at, child_run_id, partial,
        attempt_series_id, folded_producers, public_reason
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(run_id, superstep, node_name) DO UPDATE SET
        status = excluded.status,
        values_data = excluded.values_data,
        duration_ms = excluded.duration_ms,
        cached = excluded.cached,
        decision = excluded.decision,
        error = excluded.error,
        node_type = excluded.node_type,
        completed_at = excluded.completed_at,
        partial = excluded.partial,
        attempt_series_id = excluded.attempt_series_id,
        folded_producers = excluded.folded_producers,
        public_reason = excluded.public_reason
"""
_STEP_COUNT_SQL = f"SELECT COUNT(*) FROM steps WHERE {_PUBLIC_STEP_FILTER}"
_RETENTION_ROWS_SQL = f"SELECT {RETENTION_ROW_COLS} FROM steps WHERE run_id = ? ORDER BY {_STEP_TIME_ORDER}"
# Aliased to match _STEPS_COLS order for the FTS join.
_ALIASED_STEPS_COLS = ", ".join(f"s.{name.strip()}" for name in _STEPS_COLS.split(","))
_SEARCH_STEPS_SQL = f"""
    SELECT {_ALIASED_STEPS_COLS} FROM steps s
    JOIN steps_fts fts ON s.id = fts.rowid
    WHERE steps_fts MATCH ? AND {_PUBLIC_STEP_FILTER_WITH_ALIAS}
    ORDER BY {_STEP_TIME_ORDER_DESC_WITH_ALIAS}
    LIMIT ?
"""
_NODE_STATS_SQL = f"""
    SELECT node_name, node_type,
           COUNT(*) as step_runs,
           SUM(duration_ms) as total_ms,
           AVG(duration_ms) as avg_ms,
           MAX(duration_ms) as max_ms,
           SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as errors,
           SUM(cached) as cache_hits
    FROM steps WHERE run_id = ? AND {_PUBLIC_STEP_FILTER}
    GROUP BY node_name
    ORDER BY total_ms DESC
"""

# === Pending node boundaries (PRD 0013) ===
#
# Intent is recorded before the first sibling of a superstep dispatches;
# the boundary's state is DERIVED by outer-joining the execution journal, so
# the table can never claim a node ran.
# One statement carries BOTH writes to a boundary, because they address the
# same row: recording intent (``settled_at`` NULL) and the runner's per-node
# settlement (``settled_at`` set). The ``ON CONFLICT`` branch tells them
# apart, and its guard is why a re-record is still harmless: the address IS
# the boundary occurrence, so re-recording intent (a history-less run
# restarting fresh at superstep 0) must not rewrite when it first became
# pending, must never clear a ``dispatched_at`` that PRD 0014 will write
# before a provider call, and must never un-settle a node that ran. First
# settlement wins; a node can only complete once per occurrence.
_PENDING_NODE_UPSERT_SQL = """
    INSERT INTO pending_nodes (run_id, superstep, node_name, node_type, created_at, dispatched_at, settled_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(run_id, superstep, node_name) DO UPDATE SET settled_at = excluded.settled_at
    WHERE pending_nodes.settled_at IS NULL AND excluded.settled_at IS NOT NULL
"""
# The projection IS _NODE_BOUNDARY_COLS, in that order, because that is what
# the decoder zips against — derived rather than retyped, so the join and the
# record cannot drift apart. Only the joined status column is aliased: it is
# the journal's status, read as the boundary's.
_NODE_BOUNDARY_PROJECTION = ", ".join("s.status AS step_status" if name == "step_status" else f"p.{name}" for name in _NODE_BOUNDARY_COLS.split(", "))
# Intent joined with the journal: the one join both boundary reads share.
_NODE_BOUNDARY_JOIN = """
    FROM pending_nodes AS p
    LEFT JOIN steps AS s
      ON s.run_id = p.run_id AND s.superstep = p.superstep AND s.node_name = p.node_name
"""
_NODE_BOUNDARY_SELECT_SQL = f"""
    SELECT {_NODE_BOUNDARY_PROJECTION}
    {_NODE_BOUNDARY_JOIN}
    WHERE p.run_id = ?
    ORDER BY p.superstep, p.node_name
"""


def _node_boundaries_query(run_ids: Sequence[str]) -> str:
    """``get_node_boundaries`` for several runs at once, grouped by run."""
    return (
        f"SELECT {_NODE_BOUNDARY_PROJECTION} {_NODE_BOUNDARY_JOIN} "
        f"WHERE p.run_id IN ({placeholders(run_ids)}) ORDER BY p.run_id, p.superstep, p.node_name"
    )


# === Durable pause slots (PRD 0010) ===
#
# One row per interrupt occurrence, keyed by its node address, written in the
# SAME transaction as the paused step's records and the runs-row transition to
# 'paused'. ``DO NOTHING`` on conflict: the address IS the occurrence, so a
# replayed pause must not rewrite when it was asked nor clear a settlement.
# ``rowid DESC`` is commit order, which is occurrence order — the newest row is
# the current pause.
_PAUSE_SLOT_INSERT_SQL = f"INSERT INTO pause_slots ({_PAUSE_SLOT_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(pause_id) DO NOTHING"
_PAUSE_SLOT_CURRENT_SQL = f"SELECT {_PAUSE_SLOT_COLS} FROM pause_slots WHERE run_id = ? ORDER BY rowid DESC LIMIT 1"
_PAUSE_SLOT_BY_ID_SQL = f"SELECT {_PAUSE_SLOT_COLS} FROM pause_slots WHERE run_id = ? AND pause_id = ?"
_PAUSE_SLOT_IDS_SQL = "SELECT pause_id FROM pause_slots WHERE run_id = ?"
# Compare-and-set: a competing answer that lost the race matches 0 rows, so the
# first settlement wins and the loser gets a truthful refusal.
_PAUSE_SLOT_SETTLE_SQL = "UPDATE pause_slots SET settled_at = ?, answer = ? WHERE pause_id = ? AND settled_at IS NULL"

# === Attempt-ledger SQL (shared by async and sync paths) ===
_ATTEMPT_SERIES_INSERT_SQL = f"INSERT INTO attempt_series ({_ATTEMPT_SERIES_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
_ATTEMPT_RECORD_INSERT_SQL = f"INSERT INTO attempt_records ({_ATTEMPT_RECORD_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
_ATTEMPT_SERIES_BY_ID_SQL = f"SELECT {_ATTEMPT_SERIES_COLS} FROM attempt_series WHERE id = ?"
_ATTEMPT_SERIES_OPEN_SQL = f"SELECT {_ATTEMPT_SERIES_COLS} FROM attempt_series WHERE run_id = ? AND node_name = ? AND closed_at IS NULL"
_ATTEMPT_RECORDS_SQL = f"SELECT {_ATTEMPT_RECORD_COLS} FROM attempt_records WHERE series_id = ? ORDER BY attempt_number"
_ATTEMPT_RECORD_SQL = f"SELECT {_ATTEMPT_RECORD_COLS} FROM attempt_records WHERE series_id = ? AND attempt_number = ?"
_ATTEMPT_COUNT_SQL = "SELECT COUNT(*) FROM attempt_records WHERE series_id = ?"
_ATTEMPT_SETTLE_STRANDED_SQL = "UPDATE attempt_records SET status = ?, completed_at = ? WHERE series_id = ? AND status = ?"
# Compare-and-set updates: the trailing status/closed_at guards make a settle
# strictly one-shot — a competing writer that lost the race matches 0 rows and
# the checked rowcount raises loudly instead of silently overwriting.
_ATTEMPT_OUTCOME_SQL = (
    "UPDATE attempt_records SET status = ?, completed_at = ?, error_type = ?, error_message = ?, "
    "retry_not_before = ?, sampled_delay = ? WHERE series_id = ? AND attempt_number = ? AND status = 'started'"
)
_ATTEMPT_DEADLINE_SQL = (
    "UPDATE attempt_records SET deadline_elapsed = 1, cancellation_requested = 1 WHERE series_id = ? AND attempt_number = ? AND status = 'started'"
)
_ATTEMPT_FINAL_SQL = (
    "UPDATE attempt_records SET status = ?, completed_at = ?, error_type = ?, error_message = ? "
    "WHERE series_id = ? AND attempt_number = ? AND status = 'started'"
)
_ATTEMPT_SERIES_CLOSE_SQL = "UPDATE attempt_series SET closed_at = ?, committed_superstep = ? WHERE id = ? AND closed_at IS NULL"
_ATTEMPT_LIVE_SQL = f"SELECT {_ATTEMPT_RECORD_COLS} FROM attempt_records WHERE series_id = ? AND status = 'started' LIMIT 1"
_ATTEMPT_MAX_NUMBER_SQL = "SELECT COALESCE(MAX(attempt_number), 0) FROM attempt_records WHERE series_id = ?"

#: The aiosqlite internals ``__del__`` reaches for. aiosqlite offers no way to
#: shut a connection down from a finalizer — ``close()`` is a coroutine and
#: there is no loop left to await it — so the fallback names them explicitly
#: instead of hiding the dependency inside a blanket ``suppress``. A canary
#: test pins both; if an upgrade renames one, that test fails rather than the
#: cleanup silently stopping and GC-time unraisable warnings coming back.
_AIOSQLITE_RAW_CONNECTION = "_connection"
_AIOSQLITE_RUNNING_FLAG = "_running"


class WrongEventLoopError(RuntimeError):
    """An async store bound to one event loop was used from another.

    The store's transaction lock is an ``asyncio.Lock``, which belongs to the
    loop it was first awaited on. ``Lock.acquire()`` only consults that loop
    on the CONTENDED branch, so a caller on a second loop worked perfectly
    until two coroutines overlapped — and then failed deep inside somebody
    else's transaction, naming neither the caller nor the mistake (#408).
    This is that mistake, raised on the first operation instead.

    A ``RuntimeError`` because that is what asyncio itself raises for
    cross-loop misuse: a caller already catching one keeps working.
    """

    def __init__(self, opened_on: asyncio.AbstractEventLoop, running: asyncio.AbstractEventLoop) -> None:
        self.opened_on = opened_on
        self.running = running
        super().__init__(
            f"This store's async transaction lock belongs to event loop {opened_on!r} (id {hex(id(opened_on))}) "
            f"and is being used from {running!r} (id {hex(id(running))}).\n\n"
            "How to fix: keep one store per event loop — open a second RunHome/SqliteCheckpointer for "
            "the other loop, use the sync API (get_run, state, values, the *_sync verbs) from the other "
            "thread, or await close() before handing the store to a new loop."
        )


def _ensure_wal(conn: Any) -> None:
    """Put the DATABASE in WAL — waiting out another connection's lock.

    ``PRAGMA journal_mode`` is the one statement here that does NOT consult
    the busy handler: a connection arriving while somebody else holds the
    database lock is refused on the spot with "database is locked", however
    long it just said it was willing to wait. That refusal really happened —
    a second worker on one Run Home died at startup because the first was
    mid-write — so the wait is spelled out here rather than delegated.

    The mode is a property of the FILE and persists in its header, so an
    already-WAL database is left alone: nothing to set, no lock to take, and
    the common case — every connection after the first — costs one read.
    """
    import sqlite3

    if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
        return
    deadline = time.monotonic() + SQLITE_BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_WAL_RETRY_INTERVAL)


# === Query builders ===
#
# Every statement whose shape depends on its arguments is built here, once,
# and both halves bind the result. A read that one half filters and the other
# does not is then not something review has to catch.


def _state_query(run_id: str, superstep: int | None) -> tuple[str, tuple[Any, ...]]:
    """The value blobs a folded state is computed from, in execution order."""
    if superstep is None:
        return f"SELECT values_data FROM steps WHERE run_id = ? ORDER BY {_STEP_TIME_ORDER}", (run_id,)
    return f"SELECT values_data FROM steps WHERE run_id = ? AND superstep <= ? ORDER BY {_STEP_TIME_ORDER}", (run_id, superstep)


def _states_query(run_ids: Sequence[str]) -> str:
    """``get_state``'s blobs for several runs at once, grouped by run."""
    return f"SELECT run_id, values_data FROM steps WHERE run_id IN ({placeholders(run_ids)}) ORDER BY run_id, {_STEP_TIME_ORDER}"


def _failures_query(run_ids: Sequence[str]) -> str:
    """Errored steps for several runs at once, earliest first per run."""
    return (
        f"SELECT run_id, error, node_name, superstep, public_reason FROM steps "
        f"WHERE run_id IN ({placeholders(run_ids)}) AND error IS NOT NULL ORDER BY run_id, {_STEP_TIME_ORDER}"
    )


def _steps_query(run_id: str, *, superstep: int | None, show_internal: bool) -> tuple[str, list[Any]]:
    """Step records in execution order, internal carriers hidden by default."""
    conditions = ["run_id = ?"]
    params: list[Any] = [run_id]
    if superstep is not None:
        conditions.append("superstep <= ?")
        params.append(superstep)
    if not show_internal:
        conditions.append(_PUBLIC_STEP_FILTER)
    return f"SELECT {_STEPS_COLS} FROM steps WHERE {' AND '.join(conditions)} ORDER BY {_STEP_TIME_ORDER}", params


def _run_filters(
    *,
    status: WorkflowStatus | None,
    graph_name: str | None,
    since: datetime | None,
    parent_run_id: str | None | object,
) -> tuple[list[str], list[Any]]:
    """The WHERE terms every runs read shares, in one order.

    ``parent_run_id`` has three states, not two: absent (every run), ``None``
    (top-level runs only) and an id (that run's children).
    """
    conditions: list[str] = []
    params: list[Any] = []
    if status is not None:
        conditions.append("status = ?")
        params.append(status.value)
    if graph_name is not None:
        conditions.append("graph_name = ?")
        params.append(graph_name)
    if since is not None:
        conditions.append("created_at >= ?")
        params.append(_normalize_since(since).isoformat())
    if parent_run_id is not _UNSET:
        if parent_run_id is None:
            conditions.append("parent_run_id IS NULL")
        else:
            conditions.append("parent_run_id = ?")
            params.append(parent_run_id)
    return conditions, params


def _where(conditions: Sequence[str]) -> str:
    return f" WHERE {' AND '.join(conditions)}" if conditions else ""


def _runs_query(
    *,
    status: WorkflowStatus | None,
    graph_name: str | None,
    since: datetime | None,
    parent_run_id: str | None | object,
    limit: int | None,
) -> tuple[str, list[Any]]:
    """Run records newest first, optionally filtered and capped."""
    conditions, params = _run_filters(status=status, graph_name=graph_name, since=since, parent_run_id=parent_run_id)
    query = f"SELECT {_RUNS_COLS} FROM runs{_where(conditions)} ORDER BY created_at DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return query, params


def _run_count_query(
    *,
    status: WorkflowStatus | None = None,
    parent_run_id: str | None | object = _UNSET,
    retry_of: str | None = None,
) -> tuple[str, list[Any]]:
    """How many runs match, without materialising them."""
    conditions, params = _run_filters(status=status, graph_name=None, since=None, parent_run_id=parent_run_id)
    if retry_of is not None:
        conditions.append("retry_of = ?")
        params.append(retry_of)
    return f"{_RUN_COUNT_SQL}{_where(conditions)}", params


def _collect_first_failures(rows: Iterable[Any], failures: dict[str, StepFailure]) -> None:
    """Keep the FIRST errored step per run — the failure that started it.

    Rows arrive in execution order, so a run's first row is its earliest
    failure; later ones are downstream fallout of the same collapse.
    """
    for run_id, error, node_name, superstep, public_reason in rows:
        if run_id not in failures:
            failures[run_id] = StepFailure(error, node_name, None if superstep is None else int(superstep), public_reason)


def _run_status_update(status: WorkflowStatus, totals: RunTotals) -> tuple[str, list[Any]]:
    """Build the runs-row SET clause and params for one status transition.

    Shared so the plain status write and the atomic pause commit cannot drift
    about which columns a transition touches.
    """
    completed_at = (
        datetime.now(timezone.utc).isoformat()
        if status in {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.PARTIAL, WorkflowStatus.STOPPED}
        else None
    )
    sets = ["status = ?", "completed_at = ?"]
    params: list[Any] = [status.value, completed_at]
    if totals.duration_ms is not None:
        sets.append("duration_ms = ?")
        params.append(totals.duration_ms)
    if totals.node_count is not None:
        sets.append("node_count = ?")
        params.append(totals.node_count)
    if totals.error_count is not None:
        sets.append("error_count = ?")
        params.append(totals.error_count)
    return f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", params


def _debug(message: str, *args: Any) -> None:
    """Log at DEBUG, never letting the log itself escape a finalizer.

    ``__del__`` can run during interpreter shutdown, where logging handlers
    may already be torn down; a raise there becomes exactly the unraisable
    warning this cleanup exists to prevent.
    """
    with contextlib.suppress(Exception):
        _logger.debug(message, *args)


def _close_orphaned_aiosqlite(db: Any) -> None:
    """Shut down a dropped aiosqlite connection and its worker thread.

    Reaching for private attributes is deliberate and is the only option:
    ``Connection.close()`` is a coroutine, and a finalizer has no loop to
    await it on. What is NOT acceptable is doing so silently — an aiosqlite
    upgrade that renames either attribute would turn this into a no-op, and
    the GC-time unraisable warnings it prevents would come back as CI flakes
    under ``-W error``. So a missing attribute is reported, distinctly from a
    close that was attempted and failed.

    The two steps are independent on purpose, and in practice the first one
    usually loses: sqlite3 refuses a connection closed from a thread other
    than the one that opened it, and the aiosqlite worker opened this one. So
    clearing the running flag — which is what actually lets that worker's loop
    exit — must not be skipped because the raw close was refused.
    """
    missing = [name for name in (_AIOSQLITE_RAW_CONNECTION, _AIOSQLITE_RUNNING_FLAG) if not hasattr(db, name)]
    if missing:
        _debug(
            "SqliteCheckpointer.__del__ left an aiosqlite connection open: it no longer exposes %s. "
            "Close checkpointers explicitly with await close().",
            ", ".join(missing),
        )
        return

    try:
        raw_conn = getattr(db, _AIOSQLITE_RAW_CONNECTION)
        if raw_conn is not None:
            raw_conn.close()
            setattr(db, _AIOSQLITE_RAW_CONNECTION, None)
    except Exception as error:
        _debug("SqliteCheckpointer.__del__ could not close the raw sqlite3 connection: %r", error)

    try:
        setattr(db, _AIOSQLITE_RUNNING_FLAG, False)
    except Exception as error:
        _debug("SqliteCheckpointer.__del__ could not stop the aiosqlite worker: %r", error)


def _require_aiosqlite() -> Any:
    """Import aiosqlite with a clear error message if not installed."""
    try:
        import aiosqlite

        return aiosqlite
    except ImportError:
        raise ImportError("SqliteCheckpointer requires aiosqlite. Install it with: pip install hypergraph-ai[checkpoint]") from None


class SqliteCheckpointer(Checkpointer):
    """SQLite-based run persistence.

    Best for: local development, single-server deployments, simple production.

    Args:
        path: SQLite database path (`str` or `pathlib.Path`).
        durability: When to write — "sync", "async" (default), or "exit".
        retention: What to keep — "full" (default), "latest", or "windowed".
        policy: Full CheckpointPolicy (overrides durability/retention if given).
        serializer: Value serializer (default: JSON).

    Example::

        checkpointer = SqliteCheckpointer("./runs.db")
        runner = AsyncRunner(checkpointer=checkpointer)
        result = await runner.run(graph, {"x": 1}, workflow_id="run-1")

        # Query later
        state = await checkpointer.get_state("run-1")
        steps = await checkpointer.get_steps("run-1")
    """

    def __init__(
        self,
        path: str | Path,
        *,
        durability: Literal["sync", "async", "exit"] | None = None,
        retention: Literal["full", "latest", "windowed"] | None = None,
        policy: CheckpointPolicy | None = None,
        serializer: Serializer | None = None,
    ):
        if policy is not None and (durability is not None or retention is not None):
            raise ValueError("Cannot pass both 'policy' and 'durability'/'retention'. Use one or the other.")
        if policy is None and (durability is not None or retention is not None):
            policy = CheckpointPolicy(
                durability=durability or "async",
                retention=retention or "full",
            )
        super().__init__(policy=policy)
        self._path = str(path)
        self._is_memory = self._path == ":memory:"
        self._connect_path = self._path
        self._connect_uri = self._path.startswith("file:")
        if self._is_memory:
            # Use a shared in-memory URI so sync/async connections see the same schema/data.
            shared_name = f"hypergraph-{uuid.uuid4().hex}"
            self._connect_path = f"file:{shared_name}?mode=memory&cache=shared"
            self._connect_uri = True
        self._serializer = serializer or JsonSerializer()
        self._db: Any = None
        self._sync_state = threading.local()
        self._sync_connections: list[tuple[threading.RLock, Any]] = []
        self._sync_registry_lock = threading.Lock()
        self._schema_ready = False
        self._init_lock: asyncio.Lock | None = None
        self._async_txn_lock: asyncio.Lock | None = None
        self._async_txn_loop: asyncio.AbstractEventLoop | None = None
        self._aiosqlite = _require_aiosqlite()

    # === The synchronous connection: one per THREAD ===
    #
    # A single shared sync connection deadlocked a durable Host. Three
    # parties, one cycle: an async write transaction holds SQLite's write
    # lock across its ``await``s (that is what ``_txn_lock`` serializes); the
    # run executing in a ``to_thread`` worker holds the sync lock and waits
    # for that write lock; and the event loop then makes one of the
    # documented synchronous reads (``get_run``, ``state``, ``values``) and
    # waits for the sync lock. Nothing can commit, so nothing moves until
    # ``busy_timeout`` expires 30 s later and the RUN fails with "database is
    # locked" — a run that never executed a node, reported as a failure.
    #
    # WAL exists precisely so a reader never waits for a writer; sharing one
    # connection is what took that away. Per thread, each caller reads its
    # own snapshot, and cross-thread serialization is SQLite's write lock
    # alone — the same arbitration two worker PROCESSES on one Home already
    # rely on. Every multi-statement sync write already opens ``BEGIN
    # IMMEDIATE``, so no transaction depended on the shared lock for its
    # atomicity.

    @property
    def _sync_lock(self) -> threading.RLock:
        """This thread's re-entrant guard for its own sync connection.

        Re-entrant because sync methods call each other; per thread because a
        lock shared across threads is the deadlock edge described above.
        """
        lock = getattr(self._sync_state, "lock", None)
        if lock is None:
            lock = threading.RLock()
            self._sync_state.lock = lock
        return lock

    @property
    def _sync_conn(self) -> Any:
        """This thread's sqlite3 connection, or None before first use."""
        return getattr(self._sync_state, "conn", None)

    @_sync_conn.setter
    def _sync_conn(self, conn: Any) -> None:
        self._sync_state.conn = conn

    def _take_sync_connections(self) -> list[tuple[threading.RLock, Any]]:
        """Detach every thread's connection so the caller can close them."""
        with self._sync_registry_lock:
            connections = list(self._sync_connections)
            self._sync_connections.clear()
        # A fresh thread-local drops every thread's handle at once; a thread
        # that reads again after close() lazily opens a new connection,
        # exactly as it did before its first read.
        self._sync_state = threading.local()
        return connections

    def __del__(self) -> None:
        """Best-effort cleanup for forgotten checkpointers.

        Tests and callers should still prefer explicit ``await close()``.
        This fallback only exists to avoid unraisable GC-time warnings when an
        async sqlite connection is accidentally dropped without teardown.

        Each connection is closed under its OWNING thread's lock, tried
        without blocking: a finalizer must never wait on a thread that is
        mid-read, and a connection it could not take stays registered rather
        than being forgotten while still open.
        """
        with contextlib.suppress(Exception):
            with self._sync_registry_lock:
                registered = list(self._sync_connections)
            for entry in registered:
                lock, conn = entry
                if not lock.acquire(blocking=False):
                    continue
                try:
                    conn.close()
                    with self._sync_registry_lock, contextlib.suppress(ValueError):
                        self._sync_connections.remove(entry)
                finally:
                    lock.release()

        db = getattr(self, "_db", None)
        if db is not None:
            _close_orphaned_aiosqlite(db)

    def _db_stats(self) -> dict[str, Any]:
        """Gather quick DB stats for display (uses sync connection)."""
        import os

        with self._sync_lock:
            stats: dict[str, Any] = {"path": self._path}
            try:
                stats["size_bytes"] = os.path.getsize(self._path)
            except OSError:
                stats["size_bytes"] = None
            try:
                db = self._sync_db()
                (stats["run_count"],) = db.execute(_RUN_COUNT_SQL).fetchone()
                (stats["step_count"],) = db.execute(_STEP_COUNT_SQL).fetchone()
            except Exception:
                stats["run_count"] = None
                stats["step_count"] = None
            return stats

    def __repr__(self) -> str:
        from hypergraph._utils import plural

        try:
            stats = self._db_stats()
            parts = [f"SqliteCheckpointer: {self._path}"]
            if stats["run_count"] is not None:
                parts.append(plural(stats["run_count"], "run"))
                parts.append(plural(stats["step_count"], "step"))
            return " | ".join(parts)
        except Exception:
            return f"SqliteCheckpointer: {self._path}"

    @property
    def path(self) -> str:
        """Database path."""
        return self._path

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import _code, plain_reprs, theme_wrap, widget_state_key

        if plain_reprs():
            return None

        state_key = widget_state_key("checkpointer", self._path)
        try:
            stats = self._db_stats()
        except Exception:
            return theme_wrap(_code(f"SqliteCheckpointer: {self._path}"), state_key=state_key)

        explorer_runs: list[Run] = []
        steps_by_run: dict[str, list[StepRecord]] = {}
        explorer_limit = 30
        try:
            explorer_runs = list(self.runs(limit=explorer_limit))
            if explorer_runs:
                for run in explorer_runs:
                    with contextlib.suppress(Exception):
                        steps_by_run[run.id] = list(self.steps(run.id))
        except Exception:
            pass

        return render_checkpointer_explorer_html(
            title="SqliteCheckpointer",
            path=str(stats["path"]),
            state_key=state_key,
            run_count=stats["run_count"],
            step_count=stats["step_count"],
            size_bytes=stats["size_bytes"],
            runs=explorer_runs,
            steps_by_run=steps_by_run,
            run_limit=explorer_limit,
        )

    async def initialize(self) -> None:
        """Create database and tables if they don't exist."""
        if self._db is not None:
            return
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self._db is not None:
                return

            # For file-backed DBs, create/migrate schema before async connect to
            # avoid opening a second connection while async holds a write lock.
            if not self._is_memory:
                self._ensure_sync_schema()

            db = await self._aiosqlite.connect(self._connect_path, uri=self._connect_uri)
            try:
                # The journal mode is not set here: it belongs to the FILE,
                # and ``_ensure_sync_schema`` above (or, for ``:memory:``,
                # below) already put it in WAL. Re-declaring it per
                # connection took a database-wide lock for no gain — and
                # that statement does not honour ``busy_timeout``.
                await db.execute(_BUSY_TIMEOUT_PRAGMA)
                # For in-memory DBs, schema must be created after async connect
                # so the shared-cache database stays alive across connections.
                if self._is_memory:
                    self._ensure_sync_schema()
                await db.execute(_FOREIGN_KEYS_PRAGMA)
                await db.commit()
            except BaseException:
                with contextlib.suppress(Exception):
                    await db.close()
                raise
            self._db = db

    def _ensure_sync_schema(self) -> None:
        """Set up schema using sync connection (migration logic is sync)."""
        import sqlite3

        with self._sync_registry_lock:
            conn = sqlite3.connect(self._connect_path, uri=self._connect_uri)
            try:
                conn.execute(_BUSY_TIMEOUT_PRAGMA)
                _ensure_wal(conn)
                ensure_schema(conn)
            finally:
                conn.close()
            self._schema_ready = True

    async def close(self) -> None:
        """Close database connections."""
        for lock, conn in self._take_sync_connections():
            with lock:
                conn.close()
        if self._db is not None:
            await self._db.close()
            self._db = None
        # A reopened store re-checks its schema: for ``:memory:`` the shared
        # cache died with the last connection, so the next one starts empty.
        self._schema_ready = False
        self._init_lock = None
        self._async_txn_lock = None
        self._async_txn_loop = None

    async def _ensure_db(self) -> None:
        """Lazy-initialize on first use."""
        await self.initialize()

    def _txn_lock(self) -> asyncio.Lock:
        """Serialize multi-statement work on the shared async connection.

        aiosqlite shares ONE connection between coroutines, so without this
        lock an interleaved coroutine observes uncommitted half-state and its
        ``commit()`` can commit another coroutine's half-open transaction.
        Every async operation on the shared connection must hold it.

        The lock stays LAZY — there is no loop to bind to at construction —
        so the loop it binds to is recorded here, beside it, and every later
        call compares: one identity check on a path that is about to do I/O,
        in exchange for a cross-loop caller failing at the mistake instead of
        at the first contention (``WrongEventLoopError``). ``close()`` clears
        both, so the documented reopen-after-close path is free to land on a
        different loop.
        """
        running = asyncio.get_running_loop()
        lock = self._async_txn_lock
        if lock is None:
            lock = self._async_txn_lock = asyncio.Lock()
            self._async_txn_loop = running
            return lock
        opened_on = self._async_txn_loop
        if opened_on is not None and opened_on is not running:
            raise WrongEventLoopError(opened_on, running)
        return lock

    # === Run-mutation hooks (no-op in base) ===
    #
    # Called INSIDE the write transaction, before commit, on every run
    # mutation path (create_run/save_step/update_run_status, sync and async).
    # The base implementation is a no-op so plain-checkpointer behavior is
    # unchanged; RunHome (hypergraph.host) overrides these to append
    # run_updates rows in the same transaction as the run mutation.

    def _after_run_mutation_sync(self, db: Any, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Hook after a sync run mutation (no-op in the base checkpointer)."""

    async def _after_run_mutation(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Hook after an async run mutation (no-op in the base checkpointer)."""

    # === Write ===

    def _step_upsert_params(self, record: StepRecord) -> tuple[Any, ...]:
        """Build the parameter tuple for ``_STEP_UPSERT_SQL``."""
        return step_upsert_params(self._serializer, record)

    async def save_step(self, record: StepRecord) -> None:
        """Save a step with upsert semantics."""
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute(_STEP_UPSERT_SQL, self._step_upsert_params(record))
                await self._apply_retention_policy_async(record.run_id)
                await self._after_run_mutation(record.run_id, "step", _step_mutation_payload(record))
                await self._before_step_commit(record)
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise
        await self._after_step_commit(record)

    async def _before_step_commit(self, record: StepRecord) -> None:
        """Subclass hook for mutations that must commit atomically with a step."""

    async def _after_step_commit(self, record: StepRecord) -> None:
        """Subclass hook that may delay the runner after a committed step."""

    # === Pending node boundaries (PRD 0013) ===

    async def record_pending_nodes(self, boundaries: Sequence[PendingNode]) -> None:
        """Durably record what is true of these node boundaries right now.

        Two writes share this verb because they address the same rows: the
        superstep's runnable siblings recorded as pending intent, and one
        node marking itself settled the moment its result is in hand. A
        record whose ``settled_at`` is ``None`` can only create; one carrying
        a ``settled_at`` can only settle a boundary that is not settled yet.

        Writes through immediately whatever ``CheckpointPolicy.durability``
        says about StepRecord timing: a buffered boundary would not survive
        the process death it exists to describe. One transaction covers the
        whole batch, so a superstep's siblings become attributable together
        or not at all: a failure rolls the whole batch back and re-raises, so
        no later write can publish it.
        """
        if not boundaries:
            return
        await self._ensure_db()
        async with self._write_txn() as db:
            await db.executemany(_PENDING_NODE_UPSERT_SQL, [pending_node_params(b) for b in boundaries])

    async def get_node_boundaries(self, run_id: str) -> list[NodeBoundary]:
        """Recovery view: every recorded boundary of a run, state derived."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(_NODE_BOUNDARY_SELECT_SQL, (run_id,))
            rows = await cursor.fetchall()
        return [row_to_node_boundary(row) for row in rows]

    def record_pending_nodes_sync(self, boundaries: Sequence[PendingNode]) -> None:
        """Sync mirror of :meth:`record_pending_nodes`."""
        if not boundaries:
            return
        with self._write_txn_sync() as db:
            db.executemany(_PENDING_NODE_UPSERT_SQL, [pending_node_params(b) for b in boundaries])

    def get_node_boundaries_sync(self, run_id: str) -> list[NodeBoundary]:
        """Sync mirror of :meth:`get_node_boundaries`."""
        with self._sync_lock:
            rows = self._sync_db().execute(_NODE_BOUNDARY_SELECT_SQL, (run_id,)).fetchall()
        return [row_to_node_boundary(row) for row in rows]

    async def _node_boundaries_of(self, run_ids: Sequence[str]) -> dict[str, list[NodeBoundary]]:
        """``get_node_boundaries`` for several runs: one statement per id chunk.

        Runs with no recorded boundary are absent. Each boundary's state is
        derived by ``row_to_node_boundary``, exactly as the per-run read does.
        """
        boundaries: dict[str, list[NodeBoundary]] = {}
        if not run_ids:
            return boundaries
        await self._ensure_db()
        for chunk in self._chunk_run_ids(run_ids):
            async with self._txn_lock():
                cursor = await self._db.execute(_node_boundaries_query(chunk), chunk)
                rows = await cursor.fetchall()
            for row in rows:
                boundary = row_to_node_boundary(row)
                boundaries.setdefault(boundary.run_id, []).append(boundary)
        return boundaries

    def _node_boundaries_of_sync(self, run_ids: Sequence[str]) -> dict[str, list[NodeBoundary]]:
        """Sync mirror of :meth:`_node_boundaries_of`."""
        boundaries: dict[str, list[NodeBoundary]] = {}
        for chunk in self._chunk_run_ids(run_ids):
            with self._sync_lock:
                rows = self._sync_db().execute(_node_boundaries_query(chunk), chunk).fetchall()
            for row in rows:
                boundary = row_to_node_boundary(row)
                boundaries.setdefault(boundary.run_id, []).append(boundary)
        return boundaries

    # === Durable pause slots (PRD 0010) ===

    async def record_pause(
        self,
        slot: PauseSlot,
        *,
        step_records: Sequence[StepRecord] = (),
        totals: RunTotals = NO_RUN_TOTALS,
    ) -> None:
        """Commit the pause slot, any buffered step records, and ``PAUSED``.

        ONE ``BEGIN IMMEDIATE`` transaction covers everything handed to this
        call. What that buys depends on ``CheckpointPolicy.durability``, so
        state the guarantee precisely rather than claiming three writes are
        always one:

        - ``durability="exit"`` buffers the paused StepRecord to run exit, so
          it arrives here in ``step_records`` and genuinely commits with the
          slot and the status.
        - ``durability="sync"``/``"async"`` already committed the paused
          StepRecord through the ordinary per-superstep path, so
          ``step_records`` is empty and the atomic unit is slot + ``PAUSED``.

        In both modes the step record is ``<=`` the slot and never after it,
        which is the invariant PRD 0010 requires: **no reader can observe a
        committed ``PAUSED`` run without its slot.** The write is immediate
        whatever durability says about StepRecord timing — a buffered pause
        slot would not survive the process death it exists to describe.

        First record wins per address (``ON CONFLICT(pause_id) DO NOTHING``):
        a replayed occurrence leaves the WHOLE stored row alone.
        """
        await self._ensure_db()
        sql, params = _run_status_update(WorkflowStatus.PAUSED, totals)
        async with self._write_txn() as db:
            for record in step_records:
                await db.execute(_STEP_UPSERT_SQL, self._step_upsert_params(record))
            if step_records:
                await self._apply_retention_policy_async(slot.run_id)
            await db.execute(_PAUSE_SLOT_INSERT_SQL, pause_slot_insert_params(slot))
            await db.execute(sql, [*params, slot.run_id])
            for record in step_records:
                await self._after_run_mutation(record.run_id, "step", _step_mutation_payload(record))
            await self._after_run_mutation(slot.run_id, "status", _pause_mutation_payload(slot))

    async def get_pause_slot(self, run_id: str, *, pause_id: str | None = None) -> PauseSlot | None:
        """The run's current pause occurrence, or a named earlier one."""
        await self._ensure_db()
        async with self._txn_lock():
            if pause_id is None:
                cursor = await self._db.execute(_PAUSE_SLOT_CURRENT_SQL, (run_id,))
            else:
                cursor = await self._db.execute(_PAUSE_SLOT_BY_ID_SQL, (run_id, pause_id))
            row = await cursor.fetchone()
        return row_to_pause_slot(row) if row is not None else None

    async def _read_settlement_inputs(self, run_id: str) -> tuple[PauseSlot | None, list[str], WorkflowStatus | None]:
        """The three facts ``_check_settlement`` decides on, read in one place.

        Caller owns the surrounding transaction. Shared by ``settle_pause``
        and by the durable host's scheduled-answer write (ticket 14), so a
        timer is admitted against exactly the occurrence state a human answer
        would be — there is no second view of "which pause is current".
        """
        cursor = await self._db.execute(_PAUSE_SLOT_CURRENT_SQL, (run_id,))
        row = await cursor.fetchone()
        ids_cursor = await self._db.execute(_PAUSE_SLOT_IDS_SQL, (run_id,))
        id_rows = await ids_cursor.fetchall()
        run_cursor = await self._db.execute(_RUN_STATUS_SQL, (run_id,))
        run_row = await run_cursor.fetchone()
        return _settlement_inputs(row, id_rows, run_row)

    def _read_settlement_inputs_sync(self, db: Any, run_id: str) -> tuple[PauseSlot | None, list[str], WorkflowStatus | None]:
        """Sync mirror of :meth:`_read_settlement_inputs`."""
        row = db.execute(_PAUSE_SLOT_CURRENT_SQL, (run_id,)).fetchone()
        id_rows = db.execute(_PAUSE_SLOT_IDS_SQL, (run_id,)).fetchall()
        run_row = db.execute(_RUN_STATUS_SQL, (run_id,)).fetchone()
        return _settlement_inputs(row, id_rows, run_row)

    async def _settle_pause_in_txn(self, run_id: str, *, pause_id: str | None, value: Any) -> PauseSlot:
        """THE settlement body, inside a transaction the CALLER owns.

        Reads the occurrence, runs the shared refusal cascade, performs the
        compare-and-set on ``settled_at IS NULL``, writes the resume input,
        and appends the durable ``answer`` fact — but neither begins nor
        commits: it is extracted so a caller that must commit MORE than the
        settlement (the durable host's scheduled answer commits the timer's
        recorded outcome with it) still takes exactly this path instead of
        re-deriving the CAS. Every refusal is raised before any write, so a
        caller may catch one and keep writing in the same transaction.
        """
        current, known, run_status = await self._read_settlement_inputs(run_id)
        slot = _check_settlement(
            run_id=run_id,
            pause_id=pause_id,
            current=current,
            known_pause_ids=known,
            run_status=run_status,
            value=value,
        )
        settled_at = datetime.now(timezone.utc)
        result = await self._db.execute(_PAUSE_SLOT_SETTLE_SQL, _pause_settle_params(slot, settled_at, value))
        if result.rowcount != 1:
            raise _lost_settlement_race(run_id, slot.pause_id)
        await self._after_run_mutation(run_id, "answer", _answer_mutation_payload(slot))
        return replace(slot, settled_at=settled_at, answer=value)

    async def settle_pause(self, run_id: str, *, pause_id: str | None = None, value: Any) -> PauseSlot:
        """Validate one typed value, then atomically settle the named occurrence.

        The whole cascade — read the current occurrence, check the schema,
        compare-and-set on ``settled_at IS NULL``, write the resume input,
        and append the durable ``answer`` fact — runs in ONE ``BEGIN
        IMMEDIATE`` transaction. A rejected value raises before any write and
        leaves the occurrence open; a second settle of the same occurrence
        loses to the first.

        This is THE settlement path for every answer, human or scheduled: a
        durable host timer fires through the same
        :meth:`_settle_pause_in_txn` body, so the two can never diverge about
        who won a race (ADR 0008).
        """
        await self._ensure_db()
        async with self._write_txn():
            return await self._settle_pause_in_txn(run_id, pause_id=pause_id, value=value)

    def record_pause_sync(
        self,
        slot: PauseSlot,
        *,
        step_records: Sequence[StepRecord] = (),
        totals: RunTotals = NO_RUN_TOTALS,
    ) -> None:
        """Sync mirror of :meth:`record_pause`."""
        status_sql, status_params = _run_status_update(WorkflowStatus.PAUSED, totals)
        with self._write_txn_sync() as db:
            for record in step_records:
                db.execute(_STEP_UPSERT_SQL, self._step_upsert_params(record))
            if step_records:
                self._apply_retention_policy_sync(slot.run_id)
            db.execute(_PAUSE_SLOT_INSERT_SQL, pause_slot_insert_params(slot))
            db.execute(status_sql, [*status_params, slot.run_id])
            for record in step_records:
                self._after_run_mutation_sync(db, record.run_id, "step", _step_mutation_payload(record))
            self._after_run_mutation_sync(db, slot.run_id, "status", _pause_mutation_payload(slot))

    def get_pause_slot_sync(self, run_id: str, *, pause_id: str | None = None) -> PauseSlot | None:
        """Sync mirror of :meth:`get_pause_slot`."""
        with self._sync_lock:
            db = self._sync_db()
            if pause_id is None:
                row = db.execute(_PAUSE_SLOT_CURRENT_SQL, (run_id,)).fetchone()
            else:
                row = db.execute(_PAUSE_SLOT_BY_ID_SQL, (run_id, pause_id)).fetchone()
        return row_to_pause_slot(row) if row is not None else None

    def _settle_pause_in_txn_sync(self, db: Any, run_id: str, *, pause_id: str | None, value: Any) -> PauseSlot:
        """Sync mirror of :meth:`_settle_pause_in_txn`; caller owns the transaction."""
        current, known, run_status = self._read_settlement_inputs_sync(db, run_id)
        slot = _check_settlement(
            run_id=run_id,
            pause_id=pause_id,
            current=current,
            known_pause_ids=known,
            run_status=run_status,
            value=value,
        )
        settled_at = datetime.now(timezone.utc)
        result = db.execute(_PAUSE_SLOT_SETTLE_SQL, _pause_settle_params(slot, settled_at, value))
        if result.rowcount != 1:
            raise _lost_settlement_race(run_id, slot.pause_id)
        self._after_run_mutation_sync(db, run_id, "answer", _answer_mutation_payload(slot))
        return replace(slot, settled_at=settled_at, answer=value)

    def settle_pause_sync(self, run_id: str, *, pause_id: str | None = None, value: Any) -> PauseSlot:
        """Sync mirror of :meth:`settle_pause`."""
        with self._write_txn_sync() as db:
            return self._settle_pause_in_txn_sync(db, run_id, pause_id=pause_id, value=value)

    async def create_run(
        self,
        run_id: str,
        *,
        graph_name: str | None = None,
        parent_run_id: str | None = None,
        forked_from: str | None = None,
        fork_superstep: int | None = None,
        retry_of: str | None = None,
        retry_index: int | None = None,
        config: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> Run:
        """Create or reset a run record (upsert).

        ``inputs`` are the run's graph-boundary values. They are written
        once, on the first creation of this run id, and every later upsert
        leaves them alone (``COALESCE``): a resume passes only the interrupt
        answer, and overwriting the original inputs with it would destroy
        the very state resume needs. See ``get_checkpoint``.
        """
        await self._ensure_db()
        params, run = run_upsert(
            run_id,
            graph_name=graph_name,
            created_at=datetime.now(timezone.utc),
            parent_run_id=parent_run_id,
            forked_from=forked_from,
            fork_superstep=fork_superstep,
            retry_of=retry_of,
            retry_index=retry_index,
            config=config,
            inputs_blob=serialize_run_inputs(self._serializer, run_id, inputs),
        )
        async with self._txn_lock():
            await self._db.execute(_RUN_UPSERT_SQL, params)
            await self._after_run_mutation(run_id, "run_started", {"graph_name": graph_name or ""})
            await self._db.commit()
        return run

    async def update_run_status(
        self,
        run_id: str,
        status: WorkflowStatus,
        *,
        duration_ms: float | None = None,
        node_count: int | None = None,
        error_count: int | None = None,
    ) -> None:
        """Update run status with optional stats."""
        await self._ensure_db()
        sql, params = _run_status_update(status, RunTotals(duration_ms, node_count, error_count))
        async with self._txn_lock():
            await self._db.execute(sql, [*params, run_id])
            await self._after_run_mutation(run_id, "status", {"status": status.value})
            await self._db.commit()

    # === Read ===

    async def get_run_inputs(self, run_id: str) -> dict[str, Any]:
        """The graph-boundary values this run started from.

        Empty for a run created before this column existed, which is exactly
        the legacy case ``build_resume_validation_values`` still tolerates.
        """
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(_RUN_INPUTS_SQL, (run_id,))
            row = await cursor.fetchone()
        return deserialize_run_inputs(self._serializer, row)

    def get_run_inputs_sync(self, run_id: str) -> dict[str, Any]:
        """Sync mirror of ``get_run_inputs``."""
        with self._sync_lock:
            row = self._sync_db().execute(_RUN_INPUTS_SQL, (run_id,)).fetchone()
        return deserialize_run_inputs(self._serializer, row)

    def _fold_step_values(self, rows: Iterable[Any]) -> dict[str, Any]:
        """Fold ``(values_blob,)`` rows into one state, oldest write first."""
        state: dict[str, Any] = {}
        for (values_blob,) in rows:
            if values_blob is not None:
                values = self._serializer.deserialize(values_blob)
                if values:
                    state.update(values)
        return state

    async def get_state(self, run_id: str, *, superstep: int | None = None) -> dict[str, Any]:
        """Compute state by folding step values in timestamp execution order."""
        await self._ensure_db()
        sql, params = _state_query(run_id, superstep)
        async with self._txn_lock():
            cursor = await self._db.execute(sql, params)
            return self._fold_step_values(await cursor.fetchall())

    # -- Batched projection reads ---------------------------------------------
    #
    # ``get_state`` answers for ONE run. A Batch result read asks the same
    # question of every child at once, so these fold many runs in a bounded
    # number of statements instead of one round trip per child. Same rows,
    # same fold order, same serializer — no second store, and no second notion
    # of what a run produced.

    def _chunk_run_ids(self, run_ids: Sequence[str]) -> list[list[str]]:
        """Split ids into SQLite-variable-safe batches, order preserved."""
        unique = list(dict.fromkeys(run_ids))
        return [unique[i : i + _MAX_SQL_VARIABLES] for i in range(0, len(unique), _MAX_SQL_VARIABLES)]

    def _fold_states(self, rows: Iterable[Any]) -> dict[str, dict[str, Any]]:
        """Fold ``(run_id, values_blob)`` rows exactly as ``get_state`` does."""
        states: dict[str, dict[str, Any]] = {}
        for run_id, values_blob in rows:
            state = states.setdefault(run_id, {})
            if values_blob is not None:
                values = self._serializer.deserialize(values_blob)
                if values:
                    state.update(values)
        return states

    async def get_states(self, run_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Folded state for several runs at once. Runs with no steps are absent."""
        await self._ensure_db()
        states: dict[str, dict[str, Any]] = {}
        for chunk in self._chunk_run_ids(run_ids):
            async with self._txn_lock():
                cursor = await self._db.execute(_states_query(chunk), chunk)
                rows = await cursor.fetchall()
            states.update(self._fold_states(rows))
        return states

    def get_states_sync(self, run_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Sync mirror of ``get_states``."""
        states: dict[str, dict[str, Any]] = {}
        for chunk in self._chunk_run_ids(run_ids):
            with self._sync_lock:
                rows = self._sync_db().execute(_states_query(chunk), chunk).fetchall()
            states.update(self._fold_states(rows))
        return states

    async def get_step_failures(self, run_ids: Sequence[str]) -> dict[str, StepFailure]:
        """First errored step per run, as a :class:`StepFailure`.

        The error text is whatever the step persisted — the privacy-safe
        projection from ``safe_error_text``, never raw message text — and
        the public reason is the static wording the exception class declared.
        """
        await self._ensure_db()
        failures: dict[str, StepFailure] = {}
        for chunk in self._chunk_run_ids(run_ids):
            async with self._txn_lock():
                cursor = await self._db.execute(_failures_query(chunk), chunk)
                rows = await cursor.fetchall()
            _collect_first_failures(rows, failures)
        return failures

    def get_step_failures_sync(self, run_ids: Sequence[str]) -> dict[str, StepFailure]:
        """Sync mirror of ``get_step_failures``."""
        failures: dict[str, StepFailure] = {}
        for chunk in self._chunk_run_ids(run_ids):
            with self._sync_lock:
                rows = self._sync_db().execute(_failures_query(chunk), chunk).fetchall()
            _collect_first_failures(rows, failures)
        return failures

    async def get_steps(
        self,
        run_id: str,
        *,
        superstep: int | None = None,
        show_internal: bool = False,
    ) -> list[StepRecord]:
        """Get step records in execution order."""
        await self._ensure_db()
        sql, params = _steps_query(run_id, superstep=superstep, show_internal=show_internal)
        async with self._txn_lock():
            cursor = await self._db.execute(sql, params)
            return StepTable(self._row_to_step(row) for row in await cursor.fetchall())

    async def retry_workflow_async(
        self,
        source_run_id: str,
        *,
        workflow_id: str | None = None,
        superstep: int | None = None,
    ) -> tuple[str, Checkpoint]:
        """Prepare a retry checkpoint + target workflow id."""
        await self._ensure_db()
        source = await self.get_run_async(source_run_id)
        if source is None:
            raise ValueError(f"Unknown source workflow_id: {source_run_id!r}")
        sql, params = _run_count_query(retry_of=source_run_id)
        async with self._txn_lock():
            cursor = await self._db.execute(sql, params)
            (retry_count,) = await cursor.fetchone()
        checkpoint = await self.get_checkpoint(source_run_id, superstep=superstep)
        return _as_retry(checkpoint, source_run_id, retry_count, workflow_id)

    async def get_run_async(self, run_id: str) -> Run | None:
        """Get run metadata, including the run's current pause occurrence."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(_RUN_BY_ID_SQL, (run_id,))
            row = await cursor.fetchone()
            if row is None:
                return None
            run = self._row_to_run(row)
            slot_cursor = await self._db.execute(_PAUSE_SLOT_CURRENT_SQL, (run_id,))
            slot_row = await slot_cursor.fetchone()
        if slot_row is not None:
            run.pause_slot = row_to_pause_slot(slot_row)
        return run

    async def list_runs(
        self,
        *,
        status: WorkflowStatus | None = None,
        graph_name: str | None = None,
        since: datetime | None = None,
        parent_run_id: str | None | object = _UNSET,
        limit: int | None = 100,
    ) -> list[Run]:
        """List runs, optionally filtered by status and/or parent."""
        await self._ensure_db()
        query, params = _runs_query(
            status=status,
            graph_name=graph_name,
            since=since,
            parent_run_id=parent_run_id,
            limit=limit,
        )
        async with self._txn_lock():
            cursor = await self._db.execute(query, params)
            return RunTable(self._row_to_run(row) for row in await cursor.fetchall())

    async def count_runs(
        self,
        *,
        status: WorkflowStatus | None = None,
        parent_run_id: str | None | object = _UNSET,
        retry_of: str | None = None,
    ) -> int:
        """Count runs without materializing full run records."""
        await self._ensure_db()
        query, params = _run_count_query(status=status, parent_run_id=parent_run_id, retry_of=retry_of)
        async with self._txn_lock():
            cursor = await self._db.execute(query, params)
            (count,) = await cursor.fetchone()
            return int(count or 0)

    _FTS_FIELDS = frozenset({"node_name", "error"})

    def _search_query(self, query: str, *, field: str | None, limit: int) -> tuple[str, tuple[Any, ...]]:
        """The FTS5 step search, refusing a field the index cannot match on."""
        if field is not None and field not in self._FTS_FIELDS:
            raise ValueError(f"Invalid search field: {field!r}. Must be one of {sorted(self._FTS_FIELDS)}")
        return _SEARCH_STEPS_SQL, (f"{field}:{query}" if field else query, limit)

    async def search_async(self, query: str, *, field: str | None = None, limit: int = 20) -> list[StepRecord]:
        """Search steps using FTS5 (async)."""
        await self._ensure_db()
        sql, params = self._search_query(query, field=field, limit=limit)
        async with self._txn_lock():
            cursor = await self._db.execute(sql, params)
            return StepTable(self._row_to_step(row) for row in await cursor.fetchall())

    @contextlib.asynccontextmanager
    async def _write_txn(self) -> AsyncIterator[Any]:
        """One async write transaction: lock, BEGIN IMMEDIATE, commit or roll back.

        The four things every async write path owes, said once: hold the
        transaction lock so no coroutine sharing the connection sees
        half-state, ``BEGIN IMMEDIATE`` before any validation so a competing
        writer blocks and this one re-validates against committed truth,
        commit on normal exit, roll back on any ``BaseException`` — including
        a ``CancelledError`` thrown back in at the ``yield``.

        Yields the shared connection, so a body reads like its sync mirror.
        Does NOT call :meth:`_ensure_db`: initialization stays where each
        method already puts it, so a pre-transaction refusal keeps refusing at
        the same point relative to schema creation. ``_txn_lock`` is a plain
        ``asyncio.Lock``, so no body may open a second transaction.
        """
        async with self._txn_lock():
            try:
                await self._db.execute(_BEGIN_IMMEDIATE)
                yield self._db
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise

    # === Attempt Ledger (async) ===
    #
    # Reservations and outcomes write through immediately: every method
    # commits before returning, independent of the CheckpointPolicy
    # durability timing the runner applies to StepRecords.
    #
    # Concurrency contract (wave-A review): lock, BEGIN IMMEDIATE before
    # validation, commit, rollback on failure — all four now live in
    # _write_txn above. What stays a per-method obligation: settles are
    # compare-and-set with checked rowcounts, so losing a race raises loudly
    # instead of silently overwriting.

    async def _rollback_async(self) -> None:
        with contextlib.suppress(Exception):
            await self._db.rollback()

    async def _fetch_attempt_series(self, series_id: str) -> AttemptSeries | None:
        cursor = await self._db.execute(_ATTEMPT_SERIES_BY_ID_SQL, (series_id,))
        row = await cursor.fetchone()
        return row_to_attempt_series(row) if row is not None else None

    async def _fetch_open_series(self, run_id: str, node_name: str) -> AttemptSeries | None:
        cursor = await self._db.execute(_ATTEMPT_SERIES_OPEN_SQL, (run_id, node_name))
        row = await cursor.fetchone()
        return row_to_attempt_series(row) if row is not None else None

    async def _fetch_attempt_record(self, series_id: str, attempt_number: int) -> AttemptRecord | None:
        cursor = await self._db.execute(_ATTEMPT_RECORD_SQL, (series_id, attempt_number))
        row = await cursor.fetchone()
        return row_to_attempt_record(row) if row is not None else None

    async def _fetch_attempt_records(self, series_id: str) -> list[AttemptRecord]:
        cursor = await self._db.execute(_ATTEMPT_RECORDS_SQL, (series_id,))
        rows = await cursor.fetchall()
        return [row_to_attempt_record(row) for row in rows]

    @staticmethod
    def _check_settled_exactly_one(rowcount: int, what: str) -> None:
        """Invariant check behind the CAS guards — a lost race fails loudly."""
        if rowcount != 1:
            raise AttemptLedgerError(f"{what} was concurrently modified; the write was aborted and rolled back.")

    async def open_attempt_series(
        self,
        run_id: str,
        node_name: str,
        *,
        policy_fingerprint: str,
        max_attempts: int,
        deadline_at: datetime | None = None,
    ) -> AttemptSeries:
        await self._ensure_db()
        async with self._write_txn() as db:
            cursor = await db.execute(_RUN_EXISTS_SQL, (run_id,))
            _check_run_exists(await cursor.fetchone() is not None, run_id)
            _check_no_open_series(await self._fetch_open_series(run_id, node_name), run_id, node_name)
            series = _new_series(run_id, node_name, policy_fingerprint, max_attempts, deadline_at)
            await db.execute(_ATTEMPT_SERIES_INSERT_SQL, attempt_series_insert_params(series))
            return series

    async def get_attempt_series(self, series_id: str) -> AttemptSeries | None:
        await self._ensure_db()
        async with self._txn_lock():
            return await self._fetch_attempt_series(series_id)

    async def get_open_attempt_series(self, run_id: str, node_name: str) -> AttemptSeries | None:
        await self._ensure_db()
        async with self._txn_lock():
            return await self._fetch_open_series(run_id, node_name)

    async def get_attempt_records(self, series_id: str) -> list[AttemptRecord]:
        await self._ensure_db()
        async with self._txn_lock():
            return await self._fetch_attempt_records(series_id)

    async def remaining_attempts(self, series_id: str) -> int:
        await self._ensure_db()
        async with self._txn_lock():
            series = _require_series(await self._fetch_attempt_series(series_id), series_id)
            cursor = await self._db.execute(_ATTEMPT_COUNT_SQL, (series_id,))
            (consumed,) = await cursor.fetchone()
            return series.max_attempts - int(consumed)

    async def begin_attempt(
        self,
        series_id: str,
        *,
        policy_fingerprint: str,
        scheduled_superstep: int,
    ) -> AttemptRecord:
        await self._ensure_db()
        now = datetime.now(timezone.utc)
        async with self._write_txn() as db:
            series = _require_series(await self._fetch_attempt_series(series_id), series_id)
            cursor = await db.execute(_ATTEMPT_COUNT_SQL, (series_id,))
            (consumed,) = await cursor.fetchone()
            _check_reservation(series, policy_fingerprint=policy_fingerprint, consumed=int(consumed), now=now)
            # A STARTED row may belong to a live invocation — never reserve over it.
            cursor = await db.execute(_ATTEMPT_LIVE_SQL, (series_id,))
            live_row = await cursor.fetchone()
            _check_no_live_reservation(row_to_attempt_record(live_row) if live_row is not None else None, series_id)
            cursor = await db.execute(_ATTEMPT_MAX_NUMBER_SQL, (series_id,))
            (max_number,) = await cursor.fetchone()
            record = _next_attempt(series_id, max_number, scheduled_superstep, now)
            await db.execute(_ATTEMPT_RECORD_INSERT_SQL, attempt_record_insert_params(record))
            return record

    async def record_attempt_outcome(
        self,
        series_id: str,
        attempt_number: int,
        status: AttemptStatus,
        *,
        error: AttemptError | None = None,
        retry_not_before: datetime | None = None,
        sampled_delay: float | None = None,
    ) -> AttemptRecord:
        await self._ensure_db()
        _check_recordable_outcome(status)
        now = datetime.now(timezone.utc)
        async with self._write_txn() as db:
            _require_series(await self._fetch_attempt_series(series_id), series_id)
            record = _require_started(await self._fetch_attempt_record(series_id, attempt_number), series_id, attempt_number)
            cursor = await db.execute(
                _ATTEMPT_OUTCOME_SQL,
                attempt_outcome_params(
                    series_id,
                    attempt_number,
                    status,
                    now=now,
                    error=error,
                    retry_not_before=retry_not_before,
                    sampled_delay=sampled_delay,
                ),
            )
            self._check_settled_exactly_one(cursor.rowcount, _attempt_label(series_id, attempt_number))
            return replace(
                record,
                status=status,
                completed_at=now,
                error=error,
                retry_not_before=retry_not_before,
                sampled_delay=sampled_delay,
            )

    async def record_attempt_deadline(
        self,
        series_id: str,
        attempt_number: int,
    ) -> AttemptRecord:
        await self._ensure_db()
        async with self._write_txn() as db:
            _require_series(await self._fetch_attempt_series(series_id), series_id)
            record = _require_started(
                await self._fetch_attempt_record(series_id, attempt_number),
                series_id,
                attempt_number,
            )
            cursor = await db.execute(_ATTEMPT_DEADLINE_SQL, (series_id, attempt_number))
            self._check_settled_exactly_one(cursor.rowcount, _attempt_label(series_id, attempt_number))
            return replace(record, deadline_elapsed=True, cancellation_requested=True)

    async def close_attempt_series(
        self,
        series_id: str,
        attempt_number: int,
        status: AttemptStatus,
        *,
        step_record: StepRecord,
        error: AttemptError | None = None,
    ) -> None:
        await self._ensure_db()
        now = datetime.now(timezone.utc)
        async with self._write_txn() as db:
            series = _require_series(await self._fetch_attempt_series(series_id), series_id)
            _check_close_request(series, status, step_record)
            record = await self._fetch_attempt_record(series_id, attempt_number)
            cursor = await db.execute(_ATTEMPT_MAX_NUMBER_SQL, (series_id,))
            (max_number,) = await cursor.fetchone()
            if _check_closable(record, series_id, attempt_number, status, int(max_number)):
                cursor = await db.execute(
                    _ATTEMPT_FINAL_SQL,
                    attempt_final_params(series_id, attempt_number, status, now=now, error=error),
                )
                self._check_settled_exactly_one(cursor.rowcount, _attempt_label(series_id, attempt_number))
            await db.execute(_STEP_UPSERT_SQL, self._step_upsert_params(step_record))
            cursor = await db.execute(_ATTEMPT_SERIES_CLOSE_SQL, (now.isoformat(), step_record.superstep, series_id))
            self._check_settled_exactly_one(cursor.rowcount, f"Attempt series {series_id!r}")
            await self._apply_retention_policy_async(step_record.run_id)
            await self._after_run_mutation(step_record.run_id, "step", _step_mutation_payload(step_record))
            await self._before_step_commit(step_record)
        await self._after_step_commit(step_record)

    async def resolve_stranded_attempts(self, series_id: str) -> list[AttemptRecord]:
        # NOT `_write_txn`: the records are read AFTER the commit but STILL
        # under the same lock hold, so that no writer can commit between the
        # settle and the read. A commit-and-release CM cannot express that.
        await self._ensure_db()
        now = datetime.now(timezone.utc)
        async with self._txn_lock():
            try:
                await self._db.execute(_BEGIN_IMMEDIATE)
                _require_series(await self._fetch_attempt_series(series_id), series_id)
                await self._db.execute(_ATTEMPT_SETTLE_STRANDED_SQL, _stranded_params(series_id, now))
                await self._db.commit()
                return await self._fetch_attempt_records(series_id)
            except BaseException:
                await self._rollback_async()
                raise

    # === Internal ===

    def _row_to_step(self, row: Sequence[Any]) -> StepRecord:
        """Convert a ``_STEPS_COLS`` row to a StepRecord."""
        return row_to_step(self._serializer, row)

    def _row_to_run(self, row: Sequence[Any]) -> Run:
        """Convert a ``_RUNS_COLS`` row to a Run."""
        return row_to_run(row)

    # === Sync Reads ===

    def _sync_db(self):
        """This THREAD's sync sqlite3 connection (lazy, cached per thread).

        Creates/migrates schema if needed so sync reads work standalone.
        Per thread, so that WAL's promise holds where it matters: a reader
        never waits behind another thread's writer (see the deadlock note on
        ``_sync_lock``).
        """
        with self._sync_lock:
            if self._sync_conn is None:
                import sqlite3

                # WAL mode allows concurrent readers alongside async writes.
                conn = sqlite3.connect(
                    self._connect_path,
                    uri=self._connect_uri,
                    check_same_thread=False,
                )
                conn.execute(_BUSY_TIMEOUT_PRAGMA)
                if not self._schema_ready:
                    # Schema and journal mode belong to the DATABASE, not to a
                    # connection: settling them once per thread would take a
                    # database-wide lock on every new thread to answer a
                    # question already answered. A sync-only caller that never
                    # awaited ``initialize`` settles them here instead.
                    _ensure_wal(conn)
                    ensure_schema(conn)
                    self._schema_ready = True
                # After ensure_schema, so a v4->v5 table rebuild runs with
                # foreign keys off.
                conn.execute(_FOREIGN_KEYS_PRAGMA)
                self._sync_conn = conn
                with self._sync_registry_lock:
                    self._sync_connections.append((self._sync_lock, conn))
            return self._sync_conn

    def state(self, run_id: str, *, superstep: int | None = None) -> dict[str, Any]:
        """Get accumulated state synchronously.

        Same as ``get_state`` but uses stdlib ``sqlite3`` — no await needed.
        """
        sql, params = _state_query(run_id, superstep)
        with self._sync_lock:
            return self._fold_step_values(self._sync_db().execute(sql, params))

    def steps(
        self,
        run_id: str,
        *,
        superstep: int | None = None,
        show_internal: bool = False,
    ) -> list[StepRecord]:
        """Get step records synchronously."""
        sql, params = _steps_query(run_id, superstep=superstep, show_internal=show_internal)
        with self._sync_lock:
            rows = self._sync_db().execute(sql, params).fetchall()
        return StepTable(self._row_to_step(row) for row in rows)

    def get_run(self, run_id: str) -> Run | None:
        """Get run metadata synchronously, including its current pause occurrence."""
        with self._sync_lock:
            db = self._sync_db()
            row = db.execute(_RUN_BY_ID_SQL, (run_id,)).fetchone()
            if row is None:
                return None
            run = self._row_to_run(row)
            slot_row = db.execute(_PAUSE_SLOT_CURRENT_SQL, (run_id,)).fetchone()
        if slot_row is not None:
            run.pause_slot = row_to_pause_slot(slot_row)
        return run

    def runs(
        self,
        *,
        status: WorkflowStatus | None = None,
        graph_name: str | None = None,
        since: datetime | None = None,
        parent_run_id: str | None | object = _UNSET,
        limit: int | None = 100,
    ) -> list[Run]:
        """List runs synchronously with optional filters.

        Args:
            parent_run_id: Filter by parent relationship.
                Not provided (default) → all runs (backward compat).
                None → top-level only (no parent).
                "X" → children of run X.
        """
        query, params = _runs_query(
            status=status,
            graph_name=graph_name,
            since=since,
            parent_run_id=parent_run_id,
            limit=limit,
        )
        with self._sync_lock:
            rows = self._sync_db().execute(query, params).fetchall()
        return RunTable(self._row_to_run(row) for row in rows)

    def lineage(
        self,
        workflow_id: str,
        *,
        include_steps: bool = True,
        max_runs: int = 200,
    ) -> LineageView:
        """Render git-like fork lineage for a workflow id (sync).

        Shows root ancestor + all fork descendants in tree order. When
        ``include_steps=True`` each run can be expanded to inspect its steps.
        """
        with self._sync_lock:
            return build_lineage(
                workflow_id,
                get_run=self.get_run,
                get_children=self._lineage_children,
                get_steps=self.steps if include_steps else None,  # type: ignore[arg-type]
                max_runs=max_runs,
            )

    def _lineage_children(self, parent_id: str, limit: int) -> list[Run]:
        """Runs forked or retried from ``parent_id``, oldest first."""
        with self._sync_lock:
            rows = self._sync_db().execute(_LINEAGE_CHILDREN_SQL, (parent_id, parent_id, limit)).fetchall()
        return [self._row_to_run(row) for row in rows]

    def search(self, query: str, *, field: str | None = None, limit: int = 20) -> list[StepRecord]:
        """Search steps using FTS5 (sync)."""
        sql, params = self._search_query(query, field=field, limit=limit)
        with self._sync_lock:
            rows = self._sync_db().execute(sql, params).fetchall()
        return StepTable(self._row_to_step(row) for row in rows)

    def values(self, run_id: str, *, key: str | None = None) -> dict[str, Any]:
        """Get run output values synchronously. Optionally filter to a single key."""
        with self._sync_lock:
            full_state = self.state(run_id)
            if key is not None:
                return {key: full_state[key]} if key in full_state else {}
            return full_state

    def stats(self, run_id: str) -> dict[str, Any]:
        """Get per-node duration/frequency stats for a run."""
        with self._sync_lock:
            cursor = self._sync_db().execute(_NODE_STATS_SQL, (run_id,))
            return {
                row[0]: {
                    "node_type": row[1],
                    "steps": row[2],
                    "total_ms": row[3],
                    "avg_ms": round(row[4], 3) if row[4] else 0,
                    "max_ms": row[5],
                    "errors": row[6],
                    "cache_hits": row[7],
                }
                for row in cursor.fetchall()
            }

    def checkpoint(self, run_id: str, *, superstep: int | None = None) -> Checkpoint:
        """Get a checkpoint synchronously — see ``get_checkpoint`` for the model."""
        with self._sync_lock:
            return Checkpoint(
                values={**self.get_run_inputs_sync(run_id), **self.state(run_id, superstep=superstep)},
                steps=self.steps(run_id, superstep=superstep),
                source_run_id=run_id,
                source_superstep=superstep,
            )

    def fork_workflow(
        self,
        source_run_id: str,
        *,
        workflow_id: str | None = None,
        superstep: int | None = None,
    ) -> tuple[str, Checkpoint]:
        """Prepare a fork checkpoint + target workflow id (sync)."""
        with self._sync_lock:
            if self.get_run(source_run_id) is None:
                raise ValueError(f"Unknown source workflow_id: {source_run_id!r}")
            new_workflow_id = _resolve_fork_workflow_id(source_run_id, workflow_id)
            checkpoint = self.checkpoint(source_run_id, superstep=superstep)
            return new_workflow_id, checkpoint

    def retry_workflow(
        self,
        source_run_id: str,
        *,
        workflow_id: str | None = None,
        superstep: int | None = None,
    ) -> tuple[str, Checkpoint]:
        """Prepare a retry checkpoint + target workflow id (sync)."""
        sql, params = _run_count_query(retry_of=source_run_id)
        with self._sync_lock:
            db = self._sync_db()
            if self.get_run(source_run_id) is None:
                raise ValueError(f"Unknown source workflow_id: {source_run_id!r}")
            (retry_count,) = db.execute(sql, params).fetchone()
            checkpoint = self.checkpoint(source_run_id, superstep=superstep)
            return _as_retry(checkpoint, source_run_id, retry_count, workflow_id)

    # === Sync Writes (SyncCheckpointerProtocol) ===

    def create_run_sync(
        self,
        run_id: str,
        *,
        graph_name: str | None = None,
        parent_run_id: str | None = None,
        forked_from: str | None = None,
        fork_superstep: int | None = None,
        retry_of: str | None = None,
        retry_index: int | None = None,
        config: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> Run:
        """Create or reset a run record synchronously (upsert).

        ``inputs`` follow the same first-write-wins rule as ``create_run``.
        """
        with self._sync_lock:
            db = self._sync_db()
            params, run = run_upsert(
                run_id,
                graph_name=graph_name,
                created_at=datetime.now(timezone.utc),
                parent_run_id=parent_run_id,
                forked_from=forked_from,
                fork_superstep=fork_superstep,
                retry_of=retry_of,
                retry_index=retry_index,
                config=config,
                inputs_blob=serialize_run_inputs(self._serializer, run_id, inputs),
            )
            db.execute(_RUN_UPSERT_SQL, params)
            self._after_run_mutation_sync(db, run_id, "run_started", {"graph_name": graph_name or ""})
            db.commit()
            return run

    def save_step_sync(self, record: StepRecord) -> None:
        """Save a step with upsert semantics synchronously."""
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute(_STEP_UPSERT_SQL, self._step_upsert_params(record))
                self._apply_retention_policy_sync(record.run_id)
                self._after_run_mutation_sync(db, record.run_id, "step", _step_mutation_payload(record))
                self._before_step_commit_sync(db, record)
                db.commit()
            except BaseException:
                self._rollback_sync(db)
                raise
        self._after_step_commit_sync(record)

    def _before_step_commit_sync(self, db: Any, record: StepRecord) -> None:
        """Sync subclass hook for mutations that commit with a step."""

    def _after_step_commit_sync(self, record: StepRecord) -> None:
        """Sync subclass hook that may delay after a committed step."""

    # === Retention ===
    #
    # The policy and the statements live in ``_retention``; these two are the
    # executors. Whatever the plan says to delete, both halves delete — in the
    # same order, from the same tables.

    async def _apply_retention_policy_async(self, run_id: str) -> None:
        """Apply configured retention policy after persisting a step (async)."""
        if self.policy.retention == "full":
            return
        cursor = await self._db.execute(_RETENTION_ROWS_SQL, (run_id,))
        plan = plan_retention(decode_retention_rows(await cursor.fetchall()), self.policy.retention, self.policy.window)
        if plan is None or not plan.dropped_rows:
            return
        baseline = self._baseline_params(run_id, plan)
        for sql, params in compaction_deletes(run_id, plan.dropped_rows):
            await self._db.execute(sql, params)
        if baseline is not None:
            await self._db.execute(_STEP_UPSERT_SQL, baseline)

    def _apply_retention_policy_sync(self, run_id: str) -> None:
        """Apply configured retention policy after persisting a step (sync)."""
        with self._sync_lock:
            if self.policy.retention == "full":
                return
            db = self._sync_db()
            plan = plan_retention(
                decode_retention_rows(db.execute(_RETENTION_ROWS_SQL, (run_id,)).fetchall()),
                self.policy.retention,
                self.policy.window,
            )
            if plan is None or not plan.dropped_rows:
                return
            baseline = self._baseline_params(run_id, plan)
            for sql, params in compaction_deletes(run_id, plan.dropped_rows):
                db.execute(sql, params)
            if baseline is not None:
                db.execute(_STEP_UPSERT_SQL, baseline)

    def _baseline_params(self, run_id: str, plan: RetentionPlan[RetentionRow]) -> tuple[Any, ...] | None:
        """The carrier row this plan folds its dropped rows into."""
        return baseline_step_params(
            self._serializer,
            run_id,
            dropped_rows=plan.dropped_rows,
            kept_rows=plan.kept_rows,
            baseline_superstep=plan.baseline_superstep,
        )

    def update_run_status_sync(
        self,
        run_id: str,
        status: WorkflowStatus,
        *,
        duration_ms: float | None = None,
        node_count: int | None = None,
        error_count: int | None = None,
    ) -> None:
        """Update run status with optional stats synchronously."""
        sql, params = _run_status_update(status, RunTotals(duration_ms, node_count, error_count))
        with self._sync_lock:
            db = self._sync_db()
            db.execute(sql, [*params, run_id])
            self._after_run_mutation_sync(db, run_id, "status", {"status": status.value})
            db.commit()

    @contextlib.contextmanager
    def _write_txn_sync(self) -> Iterator[Any]:
        """Sync mirror of :meth:`_write_txn`, over this thread's connection.

        ``_sync_lock`` is a reentrant ``RLock``, so a body may still call a
        sync helper that takes it again.
        """
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute(_BEGIN_IMMEDIATE)
                yield db
                db.commit()
            except BaseException:
                self._rollback_sync(db)
                raise

    # === Attempt Ledger (sync mirrors) ===
    #
    # Same write-through and CAS/rowcount contract as the async methods, over
    # the cached sync connection used by SyncRunner; lock, BEGIN IMMEDIATE,
    # commit and rollback live in _write_txn_sync below. The threading RLock
    # serializes in-process sync users; BEGIN IMMEDIATE serializes against the
    # async connection at the database level.

    @staticmethod
    def _rollback_sync(db: Any) -> None:
        with contextlib.suppress(Exception):
            db.rollback()

    def _fetch_attempt_series_sync(self, db: Any, series_id: str) -> AttemptSeries | None:
        row = db.execute(_ATTEMPT_SERIES_BY_ID_SQL, (series_id,)).fetchone()
        return row_to_attempt_series(row) if row is not None else None

    def _fetch_attempt_record_sync(self, db: Any, series_id: str, attempt_number: int) -> AttemptRecord | None:
        row = db.execute(_ATTEMPT_RECORD_SQL, (series_id, attempt_number)).fetchone()
        return row_to_attempt_record(row) if row is not None else None

    def _fetch_open_series_sync(self, db: Any, run_id: str, node_name: str) -> AttemptSeries | None:
        row = db.execute(_ATTEMPT_SERIES_OPEN_SQL, (run_id, node_name)).fetchone()
        return row_to_attempt_series(row) if row is not None else None

    def _fetch_attempt_records_sync(self, db: Any, series_id: str) -> list[AttemptRecord]:
        rows = db.execute(_ATTEMPT_RECORDS_SQL, (series_id,)).fetchall()
        return [row_to_attempt_record(row) for row in rows]

    def open_attempt_series_sync(
        self,
        run_id: str,
        node_name: str,
        *,
        policy_fingerprint: str,
        max_attempts: int,
        deadline_at: datetime | None = None,
    ) -> AttemptSeries:
        with self._write_txn_sync() as db:
            _check_run_exists(db.execute(_RUN_EXISTS_SQL, (run_id,)).fetchone() is not None, run_id)
            _check_no_open_series(self._fetch_open_series_sync(db, run_id, node_name), run_id, node_name)
            series = _new_series(run_id, node_name, policy_fingerprint, max_attempts, deadline_at)
            db.execute(_ATTEMPT_SERIES_INSERT_SQL, attempt_series_insert_params(series))
            return series

    def get_attempt_series_sync(self, series_id: str) -> AttemptSeries | None:
        with self._sync_lock:
            return self._fetch_attempt_series_sync(self._sync_db(), series_id)

    def get_open_attempt_series_sync(self, run_id: str, node_name: str) -> AttemptSeries | None:
        with self._sync_lock:
            return self._fetch_open_series_sync(self._sync_db(), run_id, node_name)

    def get_attempt_records_sync(self, series_id: str) -> list[AttemptRecord]:
        with self._sync_lock:
            return self._fetch_attempt_records_sync(self._sync_db(), series_id)

    def remaining_attempts_sync(self, series_id: str) -> int:
        with self._sync_lock:
            db = self._sync_db()
            series = _require_series(self._fetch_attempt_series_sync(db, series_id), series_id)
            (consumed,) = db.execute(_ATTEMPT_COUNT_SQL, (series_id,)).fetchone()
            return series.max_attempts - int(consumed)

    def begin_attempt_sync(
        self,
        series_id: str,
        *,
        policy_fingerprint: str,
        scheduled_superstep: int,
    ) -> AttemptRecord:
        with self._write_txn_sync() as db:
            now = datetime.now(timezone.utc)
            series = _require_series(self._fetch_attempt_series_sync(db, series_id), series_id)
            (consumed,) = db.execute(_ATTEMPT_COUNT_SQL, (series_id,)).fetchone()
            _check_reservation(series, policy_fingerprint=policy_fingerprint, consumed=int(consumed), now=now)
            # A STARTED row may belong to a live invocation — never reserve over it.
            live_row = db.execute(_ATTEMPT_LIVE_SQL, (series_id,)).fetchone()
            _check_no_live_reservation(row_to_attempt_record(live_row) if live_row is not None else None, series_id)
            (max_number,) = db.execute(_ATTEMPT_MAX_NUMBER_SQL, (series_id,)).fetchone()
            record = _next_attempt(series_id, max_number, scheduled_superstep, now)
            db.execute(_ATTEMPT_RECORD_INSERT_SQL, attempt_record_insert_params(record))
            return record

    def record_attempt_outcome_sync(
        self,
        series_id: str,
        attempt_number: int,
        status: AttemptStatus,
        *,
        error: AttemptError | None = None,
        retry_not_before: datetime | None = None,
        sampled_delay: float | None = None,
    ) -> AttemptRecord:
        _check_recordable_outcome(status)
        with self._write_txn_sync() as db:
            now = datetime.now(timezone.utc)
            _require_series(self._fetch_attempt_series_sync(db, series_id), series_id)
            record = _require_started(self._fetch_attempt_record_sync(db, series_id, attempt_number), series_id, attempt_number)
            cursor = db.execute(
                _ATTEMPT_OUTCOME_SQL,
                attempt_outcome_params(
                    series_id,
                    attempt_number,
                    status,
                    now=now,
                    error=error,
                    retry_not_before=retry_not_before,
                    sampled_delay=sampled_delay,
                ),
            )
            self._check_settled_exactly_one(cursor.rowcount, _attempt_label(series_id, attempt_number))
            return replace(
                record,
                status=status,
                completed_at=now,
                error=error,
                retry_not_before=retry_not_before,
                sampled_delay=sampled_delay,
            )

    def close_attempt_series_sync(
        self,
        series_id: str,
        attempt_number: int,
        status: AttemptStatus,
        *,
        step_record: StepRecord,
        error: AttemptError | None = None,
    ) -> None:
        with self._write_txn_sync() as db:
            now = datetime.now(timezone.utc)
            series = _require_series(self._fetch_attempt_series_sync(db, series_id), series_id)
            _check_close_request(series, status, step_record)
            record = self._fetch_attempt_record_sync(db, series_id, attempt_number)
            (max_number,) = db.execute(_ATTEMPT_MAX_NUMBER_SQL, (series_id,)).fetchone()
            if _check_closable(record, series_id, attempt_number, status, int(max_number)):
                cursor = db.execute(
                    _ATTEMPT_FINAL_SQL,
                    attempt_final_params(series_id, attempt_number, status, now=now, error=error),
                )
                self._check_settled_exactly_one(cursor.rowcount, _attempt_label(series_id, attempt_number))
            db.execute(_STEP_UPSERT_SQL, self._step_upsert_params(step_record))
            cursor = db.execute(_ATTEMPT_SERIES_CLOSE_SQL, (now.isoformat(), step_record.superstep, series_id))
            self._check_settled_exactly_one(cursor.rowcount, f"Attempt series {series_id!r}")
            self._apply_retention_policy_sync(step_record.run_id)
            self._after_run_mutation_sync(db, step_record.run_id, "step", _step_mutation_payload(step_record))
            self._before_step_commit_sync(db, step_record)
        self._after_step_commit_sync(step_record)

    def resolve_stranded_attempts_sync(self, series_id: str) -> list[AttemptRecord]:
        # NOT `_write_txn_sync`: like :meth:`resolve_stranded_attempts`, the
        # post-commit read must stay inside the same lock hold.
        with self._sync_lock:
            db = self._sync_db()
            now = datetime.now(timezone.utc)
            try:
                db.execute(_BEGIN_IMMEDIATE)
                _require_series(self._fetch_attempt_series_sync(db, series_id), series_id)
                db.execute(_ATTEMPT_SETTLE_STRANDED_SQL, _stranded_params(series_id, now))
                db.commit()
            except BaseException:
                self._rollback_sync(db)
                raise
            return self._fetch_attempt_records_sync(db, series_id)


# === Shared bodies for the two halves ===
#
# Not SQL, but the same "both halves must say this identically" problem: a
# payload key, a label in an error, or the record a write returns.


def _step_mutation_payload(record: StepRecord) -> dict[str, Any]:
    """What a run-update subscriber is told about a committed step."""
    return {"node_name": record.node_name, "superstep": record.superstep, "status": record.status.value}


def _pause_mutation_payload(slot: PauseSlot) -> dict[str, Any]:
    return {"status": WorkflowStatus.PAUSED.value, "pause_id": slot.pause_id}


def _answer_mutation_payload(slot: PauseSlot) -> dict[str, Any]:
    return {"pause_id": slot.pause_id, "response_key": slot.response_key}


def _pause_settle_params(slot: PauseSlot, settled_at: datetime, value: Any) -> tuple[Any, ...]:
    return (settled_at.isoformat(), json.dumps(value), slot.pause_id)


def _settlement_inputs(
    slot_row: Sequence[Any] | None,
    id_rows: Iterable[Sequence[Any]],
    run_row: Sequence[Any] | None,
) -> tuple[PauseSlot | None, list[str], WorkflowStatus | None]:
    """Shape the three rows ``_check_settlement`` decides on."""
    return (
        row_to_pause_slot(slot_row) if slot_row is not None else None,
        [str(item[0]) for item in id_rows],
        WorkflowStatus(run_row[0]) if run_row is not None else None,
    )


def _attempt_label(series_id: str, attempt_number: int) -> str:
    """How a concurrently-modified attempt is named in the refusal."""
    return f"Attempt #{attempt_number} in series {series_id!r}"


def _new_series(
    run_id: str,
    node_name: str,
    policy_fingerprint: str,
    max_attempts: int,
    deadline_at: datetime | None,
) -> AttemptSeries:
    return AttemptSeries(
        id=_new_attempt_series_id(),
        run_id=run_id,
        node_name=node_name,
        policy_fingerprint=policy_fingerprint,
        max_attempts=max_attempts,
        opened_at=datetime.now(timezone.utc),
        deadline_at=deadline_at,
    )


def _next_attempt(series_id: str, max_number: Any, scheduled_superstep: int, now: datetime) -> AttemptRecord:
    return AttemptRecord(
        series_id=series_id,
        attempt_number=int(max_number) + 1,
        scheduled_superstep=scheduled_superstep,
        status=AttemptStatus.STARTED,
        started_at=now,
    )


def _stranded_params(series_id: str, now: datetime) -> tuple[Any, ...]:
    return (AttemptStatus.OUTCOME_UNKNOWN.value, now.isoformat(), series_id, AttemptStatus.STARTED.value)


def _as_retry(checkpoint: Checkpoint, source_run_id: str, retry_count: Any, workflow_id: str | None) -> tuple[str, Checkpoint]:
    """Stamp a checkpoint as the Nth retry of its source and name the target."""
    retry_index = int(retry_count or 0) + 1
    checkpoint.retry_of = source_run_id
    checkpoint.retry_index = retry_index
    return workflow_id or f"{source_run_id}-retry-{retry_index}", checkpoint
