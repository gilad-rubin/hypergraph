"""SQLite-based checkpointer using aiosqlite."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from hypergraph.checkpointers._migrate import ensure_schema
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
    _new_attempt_series_id,
    _normalize_since,
    _require_series,
    _require_started,
    _resolve_fork_workflow_id,
)
from hypergraph.checkpointers.presenters import render_checkpointer_explorer_html
from hypergraph.checkpointers.serializers import JsonSerializer, Serializer
from hypergraph.checkpointers.types import (
    AttemptError,
    AttemptLedgerError,
    AttemptRecord,
    AttemptSeries,
    AttemptStatus,
    Checkpoint,
    LineageRow,
    LineageView,
    Run,
    RunTable,
    StepRecord,
    StepStatus,
    StepTable,
    WorkflowStatus,
)

# Explicit column lists for SELECT queries — avoids column-order bugs after migration
_RUNS_COLS = (
    "id, graph_name, status, duration_ms, node_count, error_count, "
    "created_at, completed_at, parent_run_id, forked_from, fork_superstep, retry_of, retry_index, config"
)
_STEPS_COLS = (
    "id, run_id, step_index, superstep, node_name, node_type, status, duration_ms, cached, error, decision, "
    "input_versions, values_data, child_run_id, created_at, completed_at, partial, attempt_series_id"
)
_STEP_TIME_ORDER = "COALESCE(completed_at, created_at), created_at, id"
_STEP_TIME_ORDER_DESC = "COALESCE(completed_at, created_at) DESC, created_at DESC, id DESC"
_STEP_TIME_ORDER_DESC_WITH_ALIAS = "COALESCE(s.completed_at, s.created_at) DESC, s.created_at DESC, s.id DESC"
_RETENTION_BASELINE_NODE_NAME = "__retained_state__"
_RETENTION_BASELINE_NODE_TYPE = "RetentionBaseline"
_PUBLIC_STEP_FILTER = f"node_name != '{_RETENTION_BASELINE_NODE_NAME}' AND (node_type IS NULL OR node_type != '{_RETENTION_BASELINE_NODE_TYPE}')"
_PUBLIC_STEP_FILTER_WITH_ALIAS = (
    f"s.node_name != '{_RETENTION_BASELINE_NODE_NAME}' AND (s.node_type IS NULL OR s.node_type != '{_RETENTION_BASELINE_NODE_TYPE}')"
)
_RETENTION_ROW_COLS = "id, step_index, superstep, node_name, values_data, created_at, completed_at, attempt_series_id"
_DELETE_BATCH_SIZE = 500
_STEP_UPSERT_SQL = """
    INSERT INTO steps (
        run_id, superstep, node_name, step_index, status,
        input_versions, values_data, duration_ms, cached,
        decision, error, node_type, created_at, completed_at, child_run_id, partial,
        attempt_series_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        attempt_series_id = excluded.attempt_series_id
"""

# === Attempt-ledger SQL (shared by async and sync paths) ===
_ATTEMPT_SERIES_COLS = "id, run_id, node_name, policy_fingerprint, max_attempts, opened_at, deadline_at, committed_superstep, closed_at"
_ATTEMPT_RECORD_COLS = (
    "series_id, attempt_number, scheduled_superstep, status, started_at, completed_at, error_type, error_message, retry_not_before, sampled_delay"
)
_ATTEMPT_SERIES_INSERT_SQL = f"INSERT INTO attempt_series ({_ATTEMPT_SERIES_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
_ATTEMPT_RECORD_INSERT_SQL = f"INSERT INTO attempt_records ({_ATTEMPT_RECORD_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
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
_ATTEMPT_FINAL_SQL = (
    "UPDATE attempt_records SET status = ?, completed_at = ?, error_type = ?, error_message = ? "
    "WHERE series_id = ? AND attempt_number = ? AND status = 'started'"
)
_ATTEMPT_SERIES_CLOSE_SQL = "UPDATE attempt_series SET closed_at = ?, committed_superstep = ? WHERE id = ? AND closed_at IS NULL"
_ATTEMPT_LIVE_SQL = f"SELECT {_ATTEMPT_RECORD_COLS} FROM attempt_records WHERE series_id = ? AND status = 'started' LIMIT 1"
_ATTEMPT_MAX_NUMBER_SQL = "SELECT COALESCE(MAX(attempt_number), 0) FROM attempt_records WHERE series_id = ?"
_RUN_EXISTS_SQL = "SELECT 1 FROM runs WHERE id = ?"


@dataclass(frozen=True, slots=True)
class _RetentionRow:
    id: int
    step_index: int
    superstep: int
    node_name: str
    values_data: bytes | None
    created_at: str | None
    completed_at: str | None
    attempt_series_id: str | None


@dataclass(frozen=True, slots=True)
class _RetentionPlan:
    kept_rows: tuple[_RetentionRow, ...]
    dropped_rows: tuple[_RetentionRow, ...]
    baseline_superstep: int


def _decode_retention_rows(rows: Sequence[tuple[Any, ...]]) -> tuple[_RetentionRow, ...]:
    decoded: list[_RetentionRow] = []
    for row in rows:
        row_id, step_index, superstep, node_name, values_data, created_at, completed_at, attempt_series_id = row
        decoded.append(
            _RetentionRow(
                id=int(row_id),
                step_index=int(step_index),
                superstep=int(superstep),
                node_name=str(node_name),
                values_data=values_data,
                created_at=created_at,
                completed_at=completed_at,
                attempt_series_id=attempt_series_id,
            )
        )
    return tuple(decoded)


def _row_to_attempt_series(row: tuple[Any, ...]) -> AttemptSeries:
    return AttemptSeries(
        id=row[0],
        run_id=row[1],
        node_name=row[2],
        policy_fingerprint=row[3],
        max_attempts=int(row[4]),
        opened_at=_parse_dt(row[5]),
        deadline_at=_parse_dt(row[6]),
        committed_superstep=row[7],
        closed_at=_parse_dt(row[8]),
    )


def _row_to_attempt_record(row: tuple[Any, ...]) -> AttemptRecord:
    error = AttemptError(type_name=row[6], message=row[7] or "") if row[6] is not None else None
    return AttemptRecord(
        series_id=row[0],
        attempt_number=int(row[1]),
        scheduled_superstep=int(row[2]),
        status=AttemptStatus(row[3]),
        started_at=_parse_dt(row[4]),
        completed_at=_parse_dt(row[5]),
        error=error,
        retry_not_before=_parse_dt(row[8]),
        sampled_delay=row[9],
    )


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _attempt_record_insert_params(record: AttemptRecord) -> tuple[Any, ...]:
    return (
        record.series_id,
        record.attempt_number,
        record.scheduled_superstep,
        record.status.value,
        record.started_at.isoformat(),
        _iso_or_none(record.completed_at),
        record.error.type_name if record.error else None,
        record.error.message if record.error else None,
        _iso_or_none(record.retry_not_before),
        record.sampled_delay,
    )


def _attempt_series_insert_params(series: AttemptSeries) -> tuple[Any, ...]:
    return (
        series.id,
        series.run_id,
        series.node_name,
        series.policy_fingerprint,
        series.max_attempts,
        series.opened_at.isoformat(),
        _iso_or_none(series.deadline_at),
        series.committed_superstep,
        _iso_or_none(series.closed_at),
    )


def _plan_retention(
    rows: Sequence[_RetentionRow],
    retention: Literal["full", "latest", "windowed"],
    window: int | None,
) -> _RetentionPlan | None:
    if retention == "full":
        return None

    if retention == "latest":
        latest_by_node: dict[str, _RetentionRow] = {}
        for row in rows:
            if row.node_name != _RETENTION_BASELINE_NODE_NAME:
                latest_by_node[row.node_name] = row
        kept_rows = tuple(latest_by_node.values())
        kept_ids = {row.id for row in kept_rows}
        dropped_rows = tuple(row for row in rows if row.id not in kept_ids)
        return _RetentionPlan(
            kept_rows=kept_rows,
            dropped_rows=dropped_rows,
            baseline_superstep=min((row.superstep for row in kept_rows), default=0) - 1,
        )

    if retention == "windowed" and window is not None:
        non_baseline_rows = tuple(row for row in rows if row.node_name != _RETENTION_BASELINE_NODE_NAME)
        if not non_baseline_rows:
            return None
        max_superstep = max(row.superstep for row in non_baseline_rows)
        cutoff = max_superstep - window + 1
        if cutoff <= 0:
            return None
        kept_rows = tuple(row for row in non_baseline_rows if row.superstep >= cutoff)
        dropped_rows = tuple(row for row in rows if row.node_name == _RETENTION_BASELINE_NODE_NAME or row.superstep < cutoff)
        return _RetentionPlan(
            kept_rows=kept_rows,
            dropped_rows=dropped_rows,
            baseline_superstep=cutoff - 1,
        )

    return None


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO datetime string, normalising the UTC 'Z' suffix.

    ``datetime.fromisoformat`` only accepts 'Z' on Python 3.11+; SQLite always
    emits Z-suffixed timestamps, so we normalise to '+00:00' for 3.10 compat.
    """
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _lineage_parent_id(run: Run) -> str | None:
    """Return the workflow-lineage parent for fork/retry traversal."""
    return run.forked_from or run.retry_of


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
        self._sync_conn: Any = None
        self._sync_lock = threading.RLock()
        self._init_lock: asyncio.Lock | None = None
        self._async_txn_lock: asyncio.Lock | None = None
        self._aiosqlite = _require_aiosqlite()

    def __del__(self) -> None:
        """Best-effort cleanup for forgotten checkpointers.

        Tests and callers should still prefer explicit ``await close()``.
        This fallback only exists to avoid unraisable GC-time warnings when an
        async sqlite connection is accidentally dropped without teardown.
        """
        sync_lock = getattr(self, "_sync_lock", None)
        with contextlib.suppress(Exception):
            if sync_lock is not None and sync_lock.acquire(blocking=False):
                try:
                    sync_conn = getattr(self, "_sync_conn", None)
                    if sync_conn is not None:
                        sync_conn.close()
                        self._sync_conn = None
                finally:
                    sync_lock.release()

        db = getattr(self, "_db", None)
        if db is None:
            return

        with contextlib.suppress(Exception):
            raw_conn = getattr(db, "_connection", None)
            if raw_conn is not None:
                raw_conn.close()
                db._connection = None

        with contextlib.suppress(Exception):
            db._running = False

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
                (stats["run_count"],) = db.execute("SELECT COUNT(*) FROM runs").fetchone()
                (stats["step_count"],) = db.execute(f"SELECT COUNT(*) FROM steps WHERE {_PUBLIC_STEP_FILTER}").fetchone()
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
                await db.execute("PRAGMA journal_mode=WAL")
                # Legacy FKs (runs.parent_run_id, steps.child_run_id) are cross-store by contract; enforcement requires schema cleanup first (tracked in a follow-up issue).
                # For in-memory DBs, schema must be created after async connect
                # so the shared-cache database stays alive across connections.
                if self._is_memory:
                    self._ensure_sync_schema()
                await db.commit()
            except BaseException:
                with contextlib.suppress(Exception):
                    await db.close()
                raise
            self._db = db

    def _ensure_sync_schema(self) -> None:
        """Set up schema using sync connection (migration logic is sync)."""
        import sqlite3

        with self._sync_lock:
            conn = sqlite3.connect(self._connect_path, uri=self._connect_uri)
            try:
                ensure_schema(conn)
            finally:
                conn.close()

    async def close(self) -> None:
        """Close database connections."""
        with self._sync_lock:
            if self._sync_conn is not None:
                self._sync_conn.close()
                self._sync_conn = None
        if self._db is not None:
            await self._db.close()
            self._db = None
        self._init_lock = None
        self._async_txn_lock = None

    async def _ensure_db(self) -> None:
        """Lazy-initialize on first use."""
        await self.initialize()

    def _txn_lock(self) -> asyncio.Lock:
        """Serialize multi-statement work on the shared async connection.

        aiosqlite shares ONE connection between coroutines, so without this
        lock an interleaved coroutine observes uncommitted half-state and its
        ``commit()`` can commit another coroutine's half-open transaction.
        Every async operation on the shared connection must hold it.
        """
        if self._async_txn_lock is None:
            self._async_txn_lock = asyncio.Lock()
        return self._async_txn_lock

    # === Write ===

    def _step_upsert_params(self, record: StepRecord) -> tuple[Any, ...]:
        """Build the parameter tuple for ``_STEP_UPSERT_SQL``."""
        values_blob = self._serializer.serialize(record.values) if record.values is not None else None
        return (
            record.run_id,
            record.superstep,
            record.node_name,
            record.index,
            record.status.value,
            json.dumps(record.input_versions),
            values_blob,
            record.duration_ms,
            int(record.cached),
            json.dumps(record.decision) if record.decision is not None else None,
            record.error,
            record.node_type,
            record.created_at.isoformat(),
            record.completed_at.isoformat() if record.completed_at else None,
            record.child_run_id,
            int(record.partial),
            record.attempt_series_id,
        )

    async def save_step(self, record: StepRecord) -> None:
        """Save a step with upsert semantics."""
        await self._ensure_db()
        async with self._txn_lock():
            await self._db.execute(_STEP_UPSERT_SQL, self._step_upsert_params(record))
            await self._apply_retention_policy_async(record.run_id)
            await self._db.commit()

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
    ) -> Run:
        """Create or reset a run record (upsert)."""
        await self._ensure_db()
        now = datetime.now(timezone.utc)
        config_json = json.dumps(config) if config is not None else None
        async with self._txn_lock():
            await self._db.execute(
                "INSERT INTO runs (id, status, graph_name, created_at, parent_run_id, forked_from, fork_superstep, retry_of, retry_index, config) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET status = ?, graph_name = ?, duration_ms = NULL, node_count = 0, "
                "error_count = 0, completed_at = NULL, parent_run_id = ?, forked_from = ?, "
                "fork_superstep = ?, retry_of = ?, retry_index = ?, config = ?",
                (
                    run_id,
                    WorkflowStatus.ACTIVE.value,
                    graph_name or "",
                    now.isoformat(),
                    parent_run_id,
                    forked_from,
                    fork_superstep,
                    retry_of,
                    retry_index,
                    config_json,
                    WorkflowStatus.ACTIVE.value,
                    graph_name or "",
                    parent_run_id,
                    forked_from,
                    fork_superstep,
                    retry_of,
                    retry_index,
                    config_json,
                ),
            )
            await self._db.commit()
        return Run(
            id=run_id,
            status=WorkflowStatus.ACTIVE,
            graph_name=graph_name,
            parent_run_id=parent_run_id,
            forked_from=forked_from,
            fork_superstep=fork_superstep,
            retry_of=retry_of,
            retry_index=retry_index,
            config=config,
            created_at=now,
        )

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
        completed_at = (
            datetime.now(timezone.utc).isoformat()
            if status in {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.PARTIAL, WorkflowStatus.STOPPED}
            else None
        )

        # Build SET clause dynamically based on what's provided
        sets = ["status = ?", "completed_at = ?"]
        params: list[Any] = [status.value, completed_at]

        if duration_ms is not None:
            sets.append("duration_ms = ?")
            params.append(duration_ms)
        if node_count is not None:
            sets.append("node_count = ?")
            params.append(node_count)
        if error_count is not None:
            sets.append("error_count = ?")
            params.append(error_count)

        params.append(run_id)
        async with self._txn_lock():
            await self._db.execute(
                f"UPDATE runs SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            await self._db.commit()

    # === Read ===

    async def get_state(self, run_id: str, *, superstep: int | None = None) -> dict[str, Any]:
        """Compute state by folding step values in timestamp execution order."""
        await self._ensure_db()
        async with self._txn_lock():
            if superstep is not None:
                cursor = await self._db.execute(
                    f"SELECT values_data FROM steps WHERE run_id = ? AND superstep <= ? ORDER BY {_STEP_TIME_ORDER}",
                    (run_id, superstep),
                )
            else:
                cursor = await self._db.execute(
                    f"SELECT values_data FROM steps WHERE run_id = ? ORDER BY {_STEP_TIME_ORDER}",
                    (run_id,),
                )

            state: dict[str, Any] = {}
            async for (values_blob,) in cursor:
                if values_blob is not None:
                    values = self._serializer.deserialize(values_blob)
                    if values:
                        state.update(values)
            return state

    async def get_steps(
        self,
        run_id: str,
        *,
        superstep: int | None = None,
        show_internal: bool = False,
    ) -> list[StepRecord]:
        """Get step records in execution order."""
        await self._ensure_db()

        conditions = ["run_id = ?"]
        params: list[Any] = [run_id]
        if superstep is not None:
            conditions.append("superstep <= ?")
            params.append(superstep)
        if not show_internal:
            conditions.append(_PUBLIC_STEP_FILTER)

        async with self._txn_lock():
            cursor = await self._db.execute(
                f"SELECT {_STEPS_COLS} FROM steps WHERE {' AND '.join(conditions)} ORDER BY {_STEP_TIME_ORDER}",
                params,
            )
            rows = await cursor.fetchall()
            return StepTable(self._row_to_step(row) for row in rows)

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
        async with self._txn_lock():
            cursor = await self._db.execute("SELECT COUNT(*) FROM runs WHERE retry_of = ?", (source_run_id,))
            (retry_count,) = await cursor.fetchone()
        retry_index = int(retry_count or 0) + 1
        checkpoint = await self.get_checkpoint(source_run_id, superstep=superstep)
        checkpoint.retry_of = source_run_id
        checkpoint.retry_index = retry_index
        new_workflow_id = workflow_id or f"{source_run_id}-retry-{retry_index}"
        return new_workflow_id, checkpoint

    async def get_run_async(self, run_id: str) -> Run | None:
        """Get run metadata."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(
                f"SELECT {_RUNS_COLS} FROM runs WHERE id = ?",
                (run_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_run(row)

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

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT {_RUNS_COLS} FROM runs{where} ORDER BY created_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        async with self._txn_lock():
            cursor = await self._db.execute(query, params)
            rows = await cursor.fetchall()
            return RunTable(self._row_to_run(row) for row in rows)

    async def count_runs(
        self,
        *,
        status: WorkflowStatus | None = None,
        parent_run_id: str | None | object = _UNSET,
        retry_of: str | None = None,
    ) -> int:
        """Count runs without materializing full run records."""
        await self._ensure_db()

        conditions: list[str] = []
        params: list[Any] = []
        if status is not None:
            conditions.append("status = ?")
            params.append(status.value)
        if parent_run_id is not _UNSET:
            if parent_run_id is None:
                conditions.append("parent_run_id IS NULL")
            else:
                conditions.append("parent_run_id = ?")
                params.append(parent_run_id)
        if retry_of is not None:
            conditions.append("retry_of = ?")
            params.append(retry_of)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        async with self._txn_lock():
            cursor = await self._db.execute(f"SELECT COUNT(*) FROM runs{where}", params)
            (count,) = await cursor.fetchone()
            return int(count or 0)

    _FTS_FIELDS = frozenset({"node_name", "error"})

    async def search_async(self, query: str, *, field: str | None = None, limit: int = 20) -> list[StepRecord]:
        """Search steps using FTS5 (async)."""
        await self._ensure_db()

        if field is not None and field not in self._FTS_FIELDS:
            raise ValueError(f"Invalid search field: {field!r}. Must be one of {sorted(self._FTS_FIELDS)}")
        fts_query = f"{field}:{query}" if field else query

        cols = ", ".join(f"s.{c.strip()}" for c in _STEPS_COLS.split(","))
        async with self._txn_lock():
            cursor = await self._db.execute(
                f"""
                SELECT {cols} FROM steps s
                JOIN steps_fts fts ON s.id = fts.rowid
                WHERE steps_fts MATCH ? AND {_PUBLIC_STEP_FILTER_WITH_ALIAS}
                ORDER BY {_STEP_TIME_ORDER_DESC_WITH_ALIAS}
                LIMIT ?
                """,
                (fts_query, limit),
            )
            rows = await cursor.fetchall()
            return StepTable(self._row_to_step(row) for row in rows)

    # === Attempt Ledger (async) ===
    #
    # Reservations and outcomes write through immediately: every method
    # commits before returning, independent of the CheckpointPolicy
    # durability timing the runner applies to StepRecords.
    #
    # Concurrency contract (wave-A review):
    # - every operation holds the async transaction lock, so coroutines
    #   sharing the aiosqlite connection never observe uncommitted half-state;
    # - every write path issues BEGIN IMMEDIATE before validation, so a
    #   competing writer on the OTHER connection blocks until commit and then
    #   re-validates against committed truth (no stale-snapshot decisions);
    # - settles are compare-and-set with checked rowcounts — losing a race
    #   raises loudly instead of silently overwriting;
    # - on any failure the open transaction is rolled back.

    async def _rollback_async(self) -> None:
        with contextlib.suppress(Exception):
            await self._db.rollback()

    async def _fetch_attempt_series(self, series_id: str) -> AttemptSeries | None:
        cursor = await self._db.execute(_ATTEMPT_SERIES_BY_ID_SQL, (series_id,))
        row = await cursor.fetchone()
        return _row_to_attempt_series(row) if row is not None else None

    async def _fetch_open_series(self, run_id: str, node_name: str) -> AttemptSeries | None:
        cursor = await self._db.execute(_ATTEMPT_SERIES_OPEN_SQL, (run_id, node_name))
        row = await cursor.fetchone()
        return _row_to_attempt_series(row) if row is not None else None

    async def _fetch_attempt_record(self, series_id: str, attempt_number: int) -> AttemptRecord | None:
        cursor = await self._db.execute(_ATTEMPT_RECORD_SQL, (series_id, attempt_number))
        row = await cursor.fetchone()
        return _row_to_attempt_record(row) if row is not None else None

    async def _fetch_attempt_records(self, series_id: str) -> list[AttemptRecord]:
        cursor = await self._db.execute(_ATTEMPT_RECORDS_SQL, (series_id,))
        rows = await cursor.fetchall()
        return [_row_to_attempt_record(row) for row in rows]

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
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                cursor = await self._db.execute(_RUN_EXISTS_SQL, (run_id,))
                _check_run_exists(await cursor.fetchone() is not None, run_id)
                _check_no_open_series(await self._fetch_open_series(run_id, node_name), run_id, node_name)
                series = AttemptSeries(
                    id=_new_attempt_series_id(),
                    run_id=run_id,
                    node_name=node_name,
                    policy_fingerprint=policy_fingerprint,
                    max_attempts=max_attempts,
                    opened_at=datetime.now(timezone.utc),
                    deadline_at=deadline_at,
                )
                await self._db.execute(_ATTEMPT_SERIES_INSERT_SQL, _attempt_series_insert_params(series))
                await self._db.commit()
                return series
            except BaseException:
                await self._rollback_async()
                raise

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
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                series = _require_series(await self._fetch_attempt_series(series_id), series_id)
                cursor = await self._db.execute(_ATTEMPT_COUNT_SQL, (series_id,))
                (consumed,) = await cursor.fetchone()
                _check_reservation(series, policy_fingerprint=policy_fingerprint, consumed=int(consumed), now=now)
                # A STARTED row may belong to a live invocation — never reserve over it.
                cursor = await self._db.execute(_ATTEMPT_LIVE_SQL, (series_id,))
                live_row = await cursor.fetchone()
                _check_no_live_reservation(_row_to_attempt_record(live_row) if live_row is not None else None, series_id)
                cursor = await self._db.execute(_ATTEMPT_MAX_NUMBER_SQL, (series_id,))
                (max_number,) = await cursor.fetchone()
                record = AttemptRecord(
                    series_id=series_id,
                    attempt_number=int(max_number) + 1,
                    scheduled_superstep=scheduled_superstep,
                    status=AttemptStatus.STARTED,
                    started_at=now,
                )
                await self._db.execute(_ATTEMPT_RECORD_INSERT_SQL, _attempt_record_insert_params(record))
                await self._db.commit()
                return record
            except BaseException:
                await self._rollback_async()
                raise

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
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                _require_series(await self._fetch_attempt_series(series_id), series_id)
                record = _require_started(await self._fetch_attempt_record(series_id, attempt_number), series_id, attempt_number)
                cursor = await self._db.execute(
                    _ATTEMPT_OUTCOME_SQL,
                    (
                        status.value,
                        now.isoformat(),
                        error.type_name if error else None,
                        error.message if error else None,
                        _iso_or_none(retry_not_before),
                        sampled_delay,
                        series_id,
                        attempt_number,
                    ),
                )
                self._check_settled_exactly_one(cursor.rowcount, f"Attempt #{attempt_number} in series {series_id!r}")
                await self._db.commit()
                return replace(
                    record,
                    status=status,
                    completed_at=now,
                    error=error,
                    retry_not_before=retry_not_before,
                    sampled_delay=sampled_delay,
                )
            except BaseException:
                await self._rollback_async()
                raise

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
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                series = _require_series(await self._fetch_attempt_series(series_id), series_id)
                _check_close_request(series, status, step_record)
                record = await self._fetch_attempt_record(series_id, attempt_number)
                cursor = await self._db.execute(_ATTEMPT_MAX_NUMBER_SQL, (series_id,))
                (max_number,) = await cursor.fetchone()
                settle = _check_closable(record, series_id, attempt_number, status, int(max_number))
                if settle:
                    cursor = await self._db.execute(
                        _ATTEMPT_FINAL_SQL,
                        (
                            status.value,
                            now.isoformat(),
                            error.type_name if error else None,
                            error.message if error else None,
                            series_id,
                            attempt_number,
                        ),
                    )
                    self._check_settled_exactly_one(cursor.rowcount, f"Attempt #{attempt_number} in series {series_id!r}")
                await self._db.execute(_STEP_UPSERT_SQL, self._step_upsert_params(step_record))
                cursor = await self._db.execute(_ATTEMPT_SERIES_CLOSE_SQL, (now.isoformat(), step_record.superstep, series_id))
                self._check_settled_exactly_one(cursor.rowcount, f"Attempt series {series_id!r}")
                await self._apply_retention_policy_async(step_record.run_id)
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise

    async def resolve_stranded_attempts(self, series_id: str) -> list[AttemptRecord]:
        await self._ensure_db()
        now = datetime.now(timezone.utc)
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                _require_series(await self._fetch_attempt_series(series_id), series_id)
                await self._db.execute(
                    _ATTEMPT_SETTLE_STRANDED_SQL,
                    (AttemptStatus.OUTCOME_UNKNOWN.value, now.isoformat(), series_id, AttemptStatus.STARTED.value),
                )
                await self._db.commit()
                return await self._fetch_attempt_records(series_id)
            except BaseException:
                await self._rollback_async()
                raise

    # === Internal ===

    def _row_to_step(self, row: tuple[Any, ...]) -> StepRecord:
        """Convert a database row (``_STEPS_COLS`` order) to StepRecord.

        Columns: id, run_id, step_index, superstep, node_name, node_type,
                 status, duration_ms, cached, error, decision, input_versions,
                 values_data, child_run_id, created_at, completed_at, partial,
                 attempt_series_id (trailing columns len-guarded for old rows).
        """
        values_blob = row[12]
        values = self._serializer.deserialize(values_blob) if values_blob is not None else None
        input_versions = json.loads(row[11]) if row[11] else {}
        decision_raw = row[10]
        decision = json.loads(decision_raw) if decision_raw else None

        return StepRecord(
            run_id=row[1],
            superstep=row[3],
            node_name=row[4],
            index=row[2],
            status=StepStatus(row[6]),
            input_versions=input_versions,
            values=values,
            duration_ms=row[7],
            cached=bool(row[8]),
            decision=decision,
            error=row[9],
            node_type=row[5],
            created_at=_parse_dt(row[14]),
            completed_at=_parse_dt(row[15]),
            child_run_id=row[13],
            partial=bool(row[16]) if len(row) > 16 and row[16] is not None else False,
            attempt_series_id=row[17] if len(row) > 17 else None,
        )

    def _row_to_run(self, row: tuple[Any, ...]) -> Run:
        """Convert a database row to Run."""
        config_raw = row[13] if len(row) > 13 else None
        config = json.loads(config_raw) if config_raw else None

        return Run(
            id=row[0],
            graph_name=row[1] or None,
            status=WorkflowStatus(row[2]),
            duration_ms=row[3],
            node_count=row[4] or 0,
            error_count=row[5] or 0,
            created_at=_parse_dt(row[6]),
            completed_at=_parse_dt(row[7]),
            parent_run_id=row[8],
            forked_from=row[9] if len(row) > 9 else None,
            fork_superstep=row[10] if len(row) > 10 else None,
            retry_of=row[11] if len(row) > 11 else None,
            retry_index=row[12] if len(row) > 12 else None,
            config=config,
        )

    # === Sync Reads ===

    def _sync_db(self):
        """Open a sync sqlite3 connection (lazy, cached).

        Creates/migrates schema if needed so sync reads work standalone.
        """
        with self._sync_lock:
            if self._sync_conn is None:
                import sqlite3

                # WAL mode allows concurrent readers alongside async writes.
                # Access is serialized because one cached connection is shared
                # by background workers and their caller.
                conn = sqlite3.connect(
                    self._connect_path,
                    uri=self._connect_uri,
                    check_same_thread=False,
                )
                conn.execute("PRAGMA journal_mode=WAL")
                # Legacy FKs (runs.parent_run_id, steps.child_run_id) are cross-store by contract; enforcement requires schema cleanup first (tracked in a follow-up issue).
                ensure_schema(conn)
                self._sync_conn = conn
            return self._sync_conn

    def state(self, run_id: str, *, superstep: int | None = None) -> dict[str, Any]:
        """Get accumulated state synchronously.

        Same as ``get_state`` but uses stdlib ``sqlite3`` — no await needed.
        """
        with self._sync_lock:
            db = self._sync_db()
            if superstep is not None:
                cursor = db.execute(
                    f"SELECT values_data FROM steps WHERE run_id = ? AND superstep <= ? ORDER BY {_STEP_TIME_ORDER}",
                    (run_id, superstep),
                )
            else:
                cursor = db.execute(
                    f"SELECT values_data FROM steps WHERE run_id = ? ORDER BY {_STEP_TIME_ORDER}",
                    (run_id,),
                )

            state: dict[str, Any] = {}
            for (values_blob,) in cursor:
                if values_blob is not None:
                    values = self._serializer.deserialize(values_blob)
                    if values:
                        state.update(values)
            return state

    def steps(
        self,
        run_id: str,
        *,
        superstep: int | None = None,
        show_internal: bool = False,
    ) -> list[StepRecord]:
        """Get step records synchronously."""
        with self._sync_lock:
            db = self._sync_db()
            conditions = ["run_id = ?"]
            params: list[Any] = [run_id]
            if superstep is not None:
                conditions.append("superstep <= ?")
                params.append(superstep)
            if not show_internal:
                conditions.append(_PUBLIC_STEP_FILTER)
            cursor = db.execute(
                f"SELECT {_STEPS_COLS} FROM steps WHERE {' AND '.join(conditions)} ORDER BY {_STEP_TIME_ORDER}",
                params,
            )
            return StepTable(self._row_to_step(row) for row in cursor.fetchall())

    def get_run(self, run_id: str) -> Run | None:
        """Get run metadata synchronously."""
        with self._sync_lock:
            db = self._sync_db()
            cursor = db.execute(f"SELECT {_RUNS_COLS} FROM runs WHERE id = ?", (run_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_run(row)

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
        with self._sync_lock:
            db = self._sync_db()
            conditions = []
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

            where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
            query = f"SELECT {_RUNS_COLS} FROM runs{where} ORDER BY created_at DESC"
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)

            cursor = db.execute(query, params)
            return RunTable(self._row_to_run(row) for row in cursor.fetchall())

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
            selected = self.get_run(workflow_id)
            if selected is None:
                raise ValueError(f"Unknown workflow_id: {workflow_id!r}")

            root = selected
            seen_ancestors = {root.id}
            while _lineage_parent_id(root):
                parent_id = _lineage_parent_id(root)
                if parent_id is None:
                    break
                parent = self.get_run(parent_id)
                if parent is None or parent.id in seen_ancestors:
                    break
                root = parent
                seen_ancestors.add(root.id)

            db = self._sync_db()
            run_by_id: dict[str, Run] = {root.id: root}
            children_by_parent: dict[str, list[Run]] = {}

            queue: list[str] = [root.id]
            while queue and len(run_by_id) < max_runs:
                parent_id = queue.pop(0)
                cursor = db.execute(
                    f"SELECT {_RUNS_COLS} FROM runs WHERE forked_from = ? OR retry_of = ? ORDER BY created_at ASC LIMIT ?",
                    (parent_id, parent_id, max_runs),
                )
                children = [self._row_to_run(row) for row in cursor.fetchall()]
                children_by_parent[parent_id] = children
                for child in children:
                    if child.id in run_by_id:
                        continue
                    run_by_id[child.id] = child
                    if len(run_by_id) >= max_runs:
                        break
                    queue.append(child.id)

            rows: list[LineageRow] = [LineageRow(lane="● ", run=root, depth=0, is_selected=(root.id == workflow_id))]

            def _walk(parent_id: str, *, flags: list[bool], depth: int) -> None:
                children = children_by_parent.get(parent_id, [])
                for idx, child in enumerate(children):
                    has_next = idx < len(children) - 1
                    prefix = "".join("│  " if flag else "   " for flag in flags)
                    lane = f"{prefix}{'├─ ' if has_next else '└─ '}"
                    rows.append(
                        LineageRow(
                            lane=lane,
                            run=child,
                            depth=depth,
                            is_selected=(child.id == workflow_id),
                        )
                    )
                    _walk(child.id, flags=[*flags, has_next], depth=depth + 1)

            _walk(root.id, flags=[], depth=1)

            steps_by_run: dict[str, StepTable] | None = None
            if include_steps:
                steps_by_run = {row.run.id: self.steps(row.run.id) for row in rows}

            return LineageView(
                rows,
                selected_run_id=workflow_id,
                root_run_id=root.id,
                steps_by_run=steps_by_run,
            )

    def search(self, query: str, *, field: str | None = None, limit: int = 20) -> list[StepRecord]:
        """Search steps using FTS5 (sync)."""
        with self._sync_lock:
            db = self._sync_db()

            if field is not None and field not in self._FTS_FIELDS:
                raise ValueError(f"Invalid search field: {field!r}. Must be one of {sorted(self._FTS_FIELDS)}")
            fts_query = f"{field}:{query}" if field else query

            # Use aliased column refs that match _STEPS_COLS order
            cols = ", ".join(f"s.{c.strip()}" for c in _STEPS_COLS.split(","))
            cursor = db.execute(
                f"""
                SELECT {cols} FROM steps s
                JOIN steps_fts fts ON s.id = fts.rowid
                WHERE steps_fts MATCH ? AND {_PUBLIC_STEP_FILTER_WITH_ALIAS}
                ORDER BY {_STEP_TIME_ORDER_DESC_WITH_ALIAS}
                LIMIT ?
                """,
                (fts_query, limit),
            )
            return StepTable(self._row_to_step(row) for row in cursor.fetchall())

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
            db = self._sync_db()
            cursor = db.execute(
                f"""
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
                """,
                (run_id,),
            )
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
        """Get a checkpoint synchronously."""
        with self._sync_lock:
            return Checkpoint(
                values=self.state(run_id, superstep=superstep),
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
        with self._sync_lock:
            db = self._sync_db()
            if self.get_run(source_run_id) is None:
                raise ValueError(f"Unknown source workflow_id: {source_run_id!r}")
            (retry_count,) = db.execute("SELECT COUNT(*) FROM runs WHERE retry_of = ?", (source_run_id,)).fetchone()
            retry_index = int(retry_count or 0) + 1
            checkpoint = self.checkpoint(source_run_id, superstep=superstep)
            checkpoint.retry_of = source_run_id
            checkpoint.retry_index = retry_index
            new_workflow_id = workflow_id or f"{source_run_id}-retry-{retry_index}"
            return new_workflow_id, checkpoint

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
    ) -> Run:
        """Create or reset a run record synchronously (upsert)."""
        with self._sync_lock:
            db = self._sync_db()
            now = datetime.now(timezone.utc)
            config_json = json.dumps(config) if config is not None else None
            db.execute(
                "INSERT INTO runs (id, status, graph_name, created_at, parent_run_id, forked_from, fork_superstep, retry_of, retry_index, config) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET status = ?, graph_name = ?, duration_ms = NULL, node_count = 0, "
                "error_count = 0, completed_at = NULL, parent_run_id = ?, forked_from = ?, "
                "fork_superstep = ?, retry_of = ?, retry_index = ?, config = ?",
                (
                    run_id,
                    WorkflowStatus.ACTIVE.value,
                    graph_name or "",
                    now.isoformat(),
                    parent_run_id,
                    forked_from,
                    fork_superstep,
                    retry_of,
                    retry_index,
                    config_json,
                    WorkflowStatus.ACTIVE.value,
                    graph_name or "",
                    parent_run_id,
                    forked_from,
                    fork_superstep,
                    retry_of,
                    retry_index,
                    config_json,
                ),
            )
            db.commit()
            return Run(
                id=run_id,
                status=WorkflowStatus.ACTIVE,
                graph_name=graph_name,
                parent_run_id=parent_run_id,
                forked_from=forked_from,
                fork_superstep=fork_superstep,
                retry_of=retry_of,
                retry_index=retry_index,
                config=config,
                created_at=now,
            )

    def save_step_sync(self, record: StepRecord) -> None:
        """Save a step with upsert semantics synchronously."""
        with self._sync_lock:
            db = self._sync_db()
            db.execute(_STEP_UPSERT_SQL, self._step_upsert_params(record))
            self._apply_retention_policy_sync(record.run_id)
            db.commit()

    def _merge_retained_state(self, rows: Sequence[_RetentionRow]) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for row in rows:
            values_blob = row.values_data
            if values_blob is None:
                continue
            values = self._serializer.deserialize(values_blob)
            if values:
                state.update(values)
        return state

    def _baseline_timestamp(
        self,
        kept_rows: Sequence[_RetentionRow],
        dropped_rows: Sequence[_RetentionRow],
    ) -> datetime:
        if kept_rows:
            kept_times = [_parse_dt(row.completed_at) or _parse_dt(row.created_at) for row in kept_rows]
            anchor = min(time for time in kept_times if time is not None)
            try:
                return anchor - timedelta(microseconds=1)
            except OverflowError:
                return anchor

        dropped_times = [_parse_dt(row.completed_at) or _parse_dt(row.created_at) for row in dropped_rows]
        return max((time for time in dropped_times if time is not None), default=datetime.now(timezone.utc))

    def _retention_baseline_params(
        self,
        run_id: str,
        *,
        dropped_rows: Sequence[_RetentionRow],
        kept_rows: Sequence[_RetentionRow],
        baseline_superstep: int,
    ) -> tuple[Any, ...] | None:
        values = self._merge_retained_state(dropped_rows)
        if not values:
            return None

        baseline_at = self._baseline_timestamp(kept_rows, dropped_rows).isoformat()
        return (
            run_id,
            baseline_superstep,
            _RETENTION_BASELINE_NODE_NAME,
            min(row.step_index for row in dropped_rows),
            StepStatus.COMPLETED.value,
            "{}",
            self._serializer.serialize(values),
            0.0,
            0,
            None,
            None,
            _RETENTION_BASELINE_NODE_TYPE,
            baseline_at,
            baseline_at,
            None,
            0,
            None,
        )

    @staticmethod
    def _delete_steps_sql(ids: list[int]) -> str:
        placeholders = ", ".join("?" for _ in ids)
        return f"DELETE FROM steps WHERE id IN ({placeholders})"

    @staticmethod
    def _delete_step_id_batches(ids: Sequence[Any]) -> Iterator[list[Any]]:
        for start in range(0, len(ids), _DELETE_BATCH_SIZE):
            yield list(ids[start : start + _DELETE_BATCH_SIZE])

    @staticmethod
    def _dropped_series_ids(dropped_rows: Sequence[_RetentionRow]) -> list[str]:
        return sorted({row.attempt_series_id for row in dropped_rows if row.attempt_series_id is not None})

    @staticmethod
    def _delete_closed_series_sql(ids: list[str]) -> tuple[str, str]:
        """SQL pair deleting closed series (+records) whose linked step was dropped.

        Open series are never pruned — the ``closed_at IS NOT NULL`` guard is
        the enforcement point.
        """
        placeholders = ", ".join("?" for _ in ids)
        records_sql = (
            f"DELETE FROM attempt_records WHERE series_id IN ({placeholders}) "
            "AND series_id IN (SELECT id FROM attempt_series WHERE closed_at IS NOT NULL)"
        )
        series_sql = f"DELETE FROM attempt_series WHERE id IN ({placeholders}) AND closed_at IS NOT NULL"
        return records_sql, series_sql

    async def _retention_rows_async(self, run_id: str) -> tuple[_RetentionRow, ...]:
        cursor = await self._db.execute(
            f"SELECT {_RETENTION_ROW_COLS} FROM steps WHERE run_id = ? ORDER BY {_STEP_TIME_ORDER}",
            (run_id,),
        )
        return _decode_retention_rows(await cursor.fetchall())

    def _retention_rows_sync(self, run_id: str) -> tuple[_RetentionRow, ...]:
        with self._sync_lock:
            db = self._sync_db()
            rows = db.execute(
                f"SELECT {_RETENTION_ROW_COLS} FROM steps WHERE run_id = ? ORDER BY {_STEP_TIME_ORDER}",
                (run_id,),
            ).fetchall()
            return _decode_retention_rows(rows)

    async def _compact_retention_async(
        self,
        run_id: str,
        *,
        dropped_rows: Sequence[_RetentionRow],
        kept_rows: Sequence[_RetentionRow],
        baseline_superstep: int,
    ) -> None:
        if not dropped_rows:
            return

        baseline_params = self._retention_baseline_params(
            run_id,
            dropped_rows=dropped_rows,
            kept_rows=kept_rows,
            baseline_superstep=baseline_superstep,
        )
        ids = [row.id for row in dropped_rows]
        for batch in self._delete_step_id_batches(ids):
            await self._db.execute(self._delete_steps_sql(batch), batch)
        for series_batch in self._delete_step_id_batches(self._dropped_series_ids(dropped_rows)):
            records_sql, series_sql = self._delete_closed_series_sql(series_batch)
            await self._db.execute(records_sql, series_batch)
            await self._db.execute(series_sql, series_batch)
        if baseline_params is not None:
            await self._db.execute(_STEP_UPSERT_SQL, baseline_params)

    def _compact_retention_sync(
        self,
        run_id: str,
        *,
        dropped_rows: Sequence[_RetentionRow],
        kept_rows: Sequence[_RetentionRow],
        baseline_superstep: int,
    ) -> None:
        with self._sync_lock:
            if not dropped_rows:
                return

            baseline_params = self._retention_baseline_params(
                run_id,
                dropped_rows=dropped_rows,
                kept_rows=kept_rows,
                baseline_superstep=baseline_superstep,
            )
            ids = [row.id for row in dropped_rows]
            db = self._sync_db()
            for batch in self._delete_step_id_batches(ids):
                db.execute(self._delete_steps_sql(batch), batch)
            for series_batch in self._delete_step_id_batches(self._dropped_series_ids(dropped_rows)):
                records_sql, series_sql = self._delete_closed_series_sql(series_batch)
                db.execute(records_sql, series_batch)
                db.execute(series_sql, series_batch)
            if baseline_params is not None:
                db.execute(_STEP_UPSERT_SQL, baseline_params)

    async def _apply_retention_policy_async(self, run_id: str) -> None:
        """Apply configured retention policy after persisting a step (async)."""
        retention = self.policy.retention
        if retention == "full":
            return

        rows = await self._retention_rows_async(run_id)
        plan = _plan_retention(rows, retention, self.policy.window)
        if plan is not None:
            await self._compact_retention_async(
                run_id,
                dropped_rows=plan.dropped_rows,
                kept_rows=plan.kept_rows,
                baseline_superstep=plan.baseline_superstep,
            )

    def _apply_retention_policy_sync(self, run_id: str) -> None:
        """Apply configured retention policy after persisting a step (sync)."""
        with self._sync_lock:
            retention = self.policy.retention
            if retention == "full":
                return

            rows = self._retention_rows_sync(run_id)
            plan = _plan_retention(rows, retention, self.policy.window)
            if plan is not None:
                self._compact_retention_sync(
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
        with self._sync_lock:
            db = self._sync_db()
            completed_at = (
                datetime.now(timezone.utc).isoformat()
                if status in {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.PARTIAL, WorkflowStatus.STOPPED}
                else None
            )

            sets = ["status = ?", "completed_at = ?"]
            params: list[Any] = [status.value, completed_at]

            if duration_ms is not None:
                sets.append("duration_ms = ?")
                params.append(duration_ms)
            if node_count is not None:
                sets.append("node_count = ?")
                params.append(node_count)
            if error_count is not None:
                sets.append("error_count = ?")
                params.append(error_count)

            params.append(run_id)
            db.execute(
                f"UPDATE runs SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            db.commit()

    # === Attempt Ledger (sync mirrors) ===
    #
    # Same write-through, BEGIN IMMEDIATE, and CAS/rowcount contract as the
    # async methods, over the cached sync connection used by SyncRunner. The
    # threading RLock serializes in-process sync users; BEGIN IMMEDIATE
    # serializes against the async connection at the database level.

    @staticmethod
    def _rollback_sync(db: Any) -> None:
        with contextlib.suppress(Exception):
            db.rollback()

    def _fetch_attempt_series_sync(self, db: Any, series_id: str) -> AttemptSeries | None:
        row = db.execute(_ATTEMPT_SERIES_BY_ID_SQL, (series_id,)).fetchone()
        return _row_to_attempt_series(row) if row is not None else None

    def _fetch_attempt_record_sync(self, db: Any, series_id: str, attempt_number: int) -> AttemptRecord | None:
        row = db.execute(_ATTEMPT_RECORD_SQL, (series_id, attempt_number)).fetchone()
        return _row_to_attempt_record(row) if row is not None else None

    def open_attempt_series_sync(
        self,
        run_id: str,
        node_name: str,
        *,
        policy_fingerprint: str,
        max_attempts: int,
        deadline_at: datetime | None = None,
    ) -> AttemptSeries:
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                _check_run_exists(db.execute(_RUN_EXISTS_SQL, (run_id,)).fetchone() is not None, run_id)
                open_row = db.execute(_ATTEMPT_SERIES_OPEN_SQL, (run_id, node_name)).fetchone()
                _check_no_open_series(_row_to_attempt_series(open_row) if open_row is not None else None, run_id, node_name)
                series = AttemptSeries(
                    id=_new_attempt_series_id(),
                    run_id=run_id,
                    node_name=node_name,
                    policy_fingerprint=policy_fingerprint,
                    max_attempts=max_attempts,
                    opened_at=datetime.now(timezone.utc),
                    deadline_at=deadline_at,
                )
                db.execute(_ATTEMPT_SERIES_INSERT_SQL, _attempt_series_insert_params(series))
                db.commit()
                return series
            except BaseException:
                self._rollback_sync(db)
                raise

    def get_attempt_series_sync(self, series_id: str) -> AttemptSeries | None:
        with self._sync_lock:
            return self._fetch_attempt_series_sync(self._sync_db(), series_id)

    def get_open_attempt_series_sync(self, run_id: str, node_name: str) -> AttemptSeries | None:
        with self._sync_lock:
            row = self._sync_db().execute(_ATTEMPT_SERIES_OPEN_SQL, (run_id, node_name)).fetchone()
            return _row_to_attempt_series(row) if row is not None else None

    def get_attempt_records_sync(self, series_id: str) -> list[AttemptRecord]:
        with self._sync_lock:
            rows = self._sync_db().execute(_ATTEMPT_RECORDS_SQL, (series_id,)).fetchall()
            return [_row_to_attempt_record(row) for row in rows]

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
        with self._sync_lock:
            db = self._sync_db()
            now = datetime.now(timezone.utc)
            try:
                db.execute("BEGIN IMMEDIATE")
                series = _require_series(self._fetch_attempt_series_sync(db, series_id), series_id)
                (consumed,) = db.execute(_ATTEMPT_COUNT_SQL, (series_id,)).fetchone()
                _check_reservation(series, policy_fingerprint=policy_fingerprint, consumed=int(consumed), now=now)
                # A STARTED row may belong to a live invocation — never reserve over it.
                live_row = db.execute(_ATTEMPT_LIVE_SQL, (series_id,)).fetchone()
                _check_no_live_reservation(_row_to_attempt_record(live_row) if live_row is not None else None, series_id)
                (max_number,) = db.execute(_ATTEMPT_MAX_NUMBER_SQL, (series_id,)).fetchone()
                record = AttemptRecord(
                    series_id=series_id,
                    attempt_number=int(max_number) + 1,
                    scheduled_superstep=scheduled_superstep,
                    status=AttemptStatus.STARTED,
                    started_at=now,
                )
                db.execute(_ATTEMPT_RECORD_INSERT_SQL, _attempt_record_insert_params(record))
                db.commit()
                return record
            except BaseException:
                self._rollback_sync(db)
                raise

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
        with self._sync_lock:
            db = self._sync_db()
            now = datetime.now(timezone.utc)
            try:
                db.execute("BEGIN IMMEDIATE")
                _require_series(self._fetch_attempt_series_sync(db, series_id), series_id)
                record = _require_started(self._fetch_attempt_record_sync(db, series_id, attempt_number), series_id, attempt_number)
                cursor = db.execute(
                    _ATTEMPT_OUTCOME_SQL,
                    (
                        status.value,
                        now.isoformat(),
                        error.type_name if error else None,
                        error.message if error else None,
                        _iso_or_none(retry_not_before),
                        sampled_delay,
                        series_id,
                        attempt_number,
                    ),
                )
                self._check_settled_exactly_one(cursor.rowcount, f"Attempt #{attempt_number} in series {series_id!r}")
                db.commit()
                return replace(
                    record,
                    status=status,
                    completed_at=now,
                    error=error,
                    retry_not_before=retry_not_before,
                    sampled_delay=sampled_delay,
                )
            except BaseException:
                self._rollback_sync(db)
                raise

    def close_attempt_series_sync(
        self,
        series_id: str,
        attempt_number: int,
        status: AttemptStatus,
        *,
        step_record: StepRecord,
        error: AttemptError | None = None,
    ) -> None:
        with self._sync_lock:
            db = self._sync_db()
            now = datetime.now(timezone.utc)
            try:
                db.execute("BEGIN IMMEDIATE")
                series = _require_series(self._fetch_attempt_series_sync(db, series_id), series_id)
                _check_close_request(series, status, step_record)
                record = self._fetch_attempt_record_sync(db, series_id, attempt_number)
                (max_number,) = db.execute(_ATTEMPT_MAX_NUMBER_SQL, (series_id,)).fetchone()
                settle = _check_closable(record, series_id, attempt_number, status, int(max_number))
                if settle:
                    cursor = db.execute(
                        _ATTEMPT_FINAL_SQL,
                        (
                            status.value,
                            now.isoformat(),
                            error.type_name if error else None,
                            error.message if error else None,
                            series_id,
                            attempt_number,
                        ),
                    )
                    self._check_settled_exactly_one(cursor.rowcount, f"Attempt #{attempt_number} in series {series_id!r}")
                db.execute(_STEP_UPSERT_SQL, self._step_upsert_params(step_record))
                cursor = db.execute(_ATTEMPT_SERIES_CLOSE_SQL, (now.isoformat(), step_record.superstep, series_id))
                self._check_settled_exactly_one(cursor.rowcount, f"Attempt series {series_id!r}")
                self._apply_retention_policy_sync(step_record.run_id)
                db.commit()
            except BaseException:
                self._rollback_sync(db)
                raise

    def resolve_stranded_attempts_sync(self, series_id: str) -> list[AttemptRecord]:
        with self._sync_lock:
            db = self._sync_db()
            now = datetime.now(timezone.utc)
            try:
                db.execute("BEGIN IMMEDIATE")
                _require_series(self._fetch_attempt_series_sync(db, series_id), series_id)
                db.execute(
                    _ATTEMPT_SETTLE_STRANDED_SQL,
                    (AttemptStatus.OUTCOME_UNKNOWN.value, now.isoformat(), series_id, AttemptStatus.STARTED.value),
                )
                db.commit()
            except BaseException:
                self._rollback_sync(db)
                raise
            rows = db.execute(_ATTEMPT_RECORDS_SQL, (series_id,)).fetchall()
            return [_row_to_attempt_record(row) for row in rows]
