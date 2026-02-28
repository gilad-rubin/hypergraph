"""SQLite-based checkpointer using aiosqlite."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from hypergraph.checkpointers._migrate import ensure_schema
from hypergraph.checkpointers.base import Checkpointer, CheckpointPolicy
from hypergraph.checkpointers.serializers import JsonSerializer, Serializer
from hypergraph.checkpointers.types import Checkpoint, Run, StepRecord, StepStatus, WorkflowStatus

# Explicit column lists for SELECT queries — avoids column-order bugs after migration
_RUNS_COLS = "id, graph_name, status, duration_ms, node_count, error_count, created_at, completed_at, parent_run_id, config"
_STEPS_COLS = "id, run_id, step_index, superstep, node_name, node_type, status, duration_ms, cached, error, decision, input_versions, values_data, child_run_id, created_at, completed_at"


def _require_aiosqlite() -> Any:
    """Import aiosqlite with a clear error message if not installed."""
    try:
        import aiosqlite

        return aiosqlite
    except ImportError:
        raise ImportError("SqliteCheckpointer requires aiosqlite. Install it with: pip install hypergraph[checkpoint]") from None


class SqliteCheckpointer(Checkpointer):
    """SQLite-based run persistence.

    Best for: local development, single-server deployments, simple production.

    Args:
        path: Path to SQLite database file.
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
        path: str,
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
        self._path = path
        self._serializer = serializer or JsonSerializer()
        self._db: Any = None
        self._sync_conn: Any = None
        self._aiosqlite = _require_aiosqlite()

    async def initialize(self) -> None:
        """Create database and tables if they don't exist."""
        self._db = await self._aiosqlite.connect(self._path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        # Run migration/creation synchronously through the async connection
        conn = await self._db.execute("SELECT 1")  # ensure connection is open
        await conn.close()
        # Use a sync connection for schema setup (ensure_schema uses sync sqlite3)
        self._ensure_sync_schema()
        await self._db.commit()

    def _ensure_sync_schema(self) -> None:
        """Set up schema using sync connection (migration logic is sync)."""
        import sqlite3

        conn = sqlite3.connect(self._path)
        try:
            ensure_schema(conn)
        finally:
            conn.close()

    async def close(self) -> None:
        """Close database connections."""
        if self._sync_conn is not None:
            self._sync_conn.close()
            self._sync_conn = None
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _ensure_db(self) -> None:
        """Lazy-initialize on first use."""
        if self._db is None:
            await self.initialize()

    # === Write ===

    async def save_step(self, record: StepRecord) -> None:
        """Save a step with upsert semantics."""
        await self._ensure_db()

        values_blob = self._serializer.serialize(record.values) if record.values is not None else None
        input_versions_json = json.dumps(record.input_versions)
        decision_json = json.dumps(record.decision) if record.decision is not None else None

        await self._db.execute(
            """
            INSERT INTO steps (
                run_id, superstep, node_name, step_index, status,
                input_versions, values_data, duration_ms, cached,
                decision, error, node_type, created_at, completed_at, child_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, superstep, node_name) DO UPDATE SET
                status = excluded.status,
                values_data = excluded.values_data,
                duration_ms = excluded.duration_ms,
                cached = excluded.cached,
                decision = excluded.decision,
                error = excluded.error,
                node_type = excluded.node_type,
                completed_at = excluded.completed_at
            """,
            (
                record.run_id,
                record.superstep,
                record.node_name,
                record.index,
                record.status.value,
                input_versions_json,
                values_blob,
                record.duration_ms,
                int(record.cached),
                decision_json,
                record.error,
                record.node_type,
                record.created_at.isoformat(),
                record.completed_at.isoformat() if record.completed_at else None,
                record.child_run_id,
            ),
        )
        await self._db.commit()

    async def create_run(self, run_id: str, *, graph_name: str | None = None) -> Run:
        """Create a new run record."""
        await self._ensure_db()
        now = datetime.now(timezone.utc)
        await self._db.execute(
            "INSERT INTO runs (id, status, graph_name, created_at) VALUES (?, ?, ?, ?)",
            (run_id, WorkflowStatus.ACTIVE.value, graph_name or "", now.isoformat()),
        )
        await self._db.commit()
        return Run(id=run_id, status=WorkflowStatus.ACTIVE, graph_name=graph_name, created_at=now)

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
        completed_at = datetime.now(timezone.utc).isoformat() if status != WorkflowStatus.ACTIVE else None

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
        await self._db.execute(
            f"UPDATE runs SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        await self._db.commit()

    # === Read ===

    async def get_state(self, run_id: str, *, superstep: int | None = None) -> dict[str, Any]:
        """Compute state by folding step values in index order."""
        await self._ensure_db()

        if superstep is not None:
            cursor = await self._db.execute(
                "SELECT values_data FROM steps WHERE run_id = ? AND superstep <= ? ORDER BY step_index",
                (run_id, superstep),
            )
        else:
            cursor = await self._db.execute(
                "SELECT values_data FROM steps WHERE run_id = ? ORDER BY step_index",
                (run_id,),
            )

        state: dict[str, Any] = {}
        async for (values_blob,) in cursor:
            if values_blob is not None:
                values = self._serializer.deserialize(values_blob)
                if values:
                    state.update(values)
        return state

    async def get_steps(self, run_id: str, *, superstep: int | None = None) -> list[StepRecord]:
        """Get step records in execution order."""
        await self._ensure_db()

        if superstep is not None:
            cursor = await self._db.execute(
                f"SELECT {_STEPS_COLS} FROM steps WHERE run_id = ? AND superstep <= ? ORDER BY step_index",
                (run_id, superstep),
            )
        else:
            cursor = await self._db.execute(
                f"SELECT {_STEPS_COLS} FROM steps WHERE run_id = ? ORDER BY step_index",
                (run_id,),
            )

        rows = await cursor.fetchall()
        return [self._row_to_step(row) for row in rows]

    async def get_run(self, run_id: str) -> Run | None:
        """Get run metadata."""
        await self._ensure_db()
        cursor = await self._db.execute(
            f"SELECT {_RUNS_COLS} FROM runs WHERE id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_run(row)

    async def list_runs(self, *, status: WorkflowStatus | None = None, limit: int = 100) -> list[Run]:
        """List runs, optionally filtered by status."""
        await self._ensure_db()

        if status is not None:
            cursor = await self._db.execute(
                f"SELECT {_RUNS_COLS} FROM runs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status.value, limit),
            )
        else:
            cursor = await self._db.execute(
                f"SELECT {_RUNS_COLS} FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )

        rows = await cursor.fetchall()
        return [self._row_to_run(row) for row in rows]

    _FTS_FIELDS = frozenset({"node_name", "error"})

    async def search(self, query: str, *, field: str | None = None, limit: int = 20) -> list[StepRecord]:
        """Search steps using FTS5."""
        await self._ensure_db()

        if field is not None and field not in self._FTS_FIELDS:
            raise ValueError(f"Invalid search field: {field!r}. Must be one of {sorted(self._FTS_FIELDS)}")
        fts_query = f"{field}:{query}" if field else query

        cols = ", ".join(f"s.{c.strip()}" for c in _STEPS_COLS.split(","))
        cursor = await self._db.execute(
            f"""
            SELECT {cols} FROM steps s
            JOIN steps_fts fts ON s.id = fts.rowid
            WHERE steps_fts MATCH ?
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (fts_query, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_step(row) for row in rows]

    # === Internal ===

    def _row_to_step(self, row: tuple[Any, ...]) -> StepRecord:
        """Convert a v2 database row to StepRecord.

        v2 columns: id, run_id, step_index, superstep, node_name, node_type,
                     status, duration_ms, cached, error, decision,
                     input_versions, values_data, child_run_id,
                     created_at, completed_at
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
            created_at=datetime.fromisoformat(row[14]),
            completed_at=datetime.fromisoformat(row[15]) if row[15] else None,
            child_run_id=row[13],
        )

    def _row_to_run(self, row: tuple[Any, ...]) -> Run:
        """Convert a v2 database row to Run.

        v2 columns: id, graph_name, status, duration_ms, node_count,
                     error_count, created_at, completed_at, parent_run_id, config
        """
        config_raw = row[9] if len(row) > 9 else None
        config = json.loads(config_raw) if config_raw else None

        return Run(
            id=row[0],
            graph_name=row[1] or None,
            status=WorkflowStatus(row[2]),
            duration_ms=row[3],
            node_count=row[4] or 0,
            error_count=row[5] or 0,
            created_at=datetime.fromisoformat(row[6]),
            completed_at=datetime.fromisoformat(row[7]) if row[7] else None,
            parent_run_id=row[8],
            config=config,
        )

    # === Sync Reads ===

    def _sync_db(self):
        """Open a sync sqlite3 connection (lazy, cached).

        Creates/migrates schema if needed so sync reads work standalone.
        """
        if self._sync_conn is None:
            import sqlite3

            # WAL mode allows concurrent readers alongside async writes
            # without "database is locked" errors
            conn = sqlite3.connect(self._path)
            conn.execute("PRAGMA journal_mode=WAL")
            ensure_schema(conn)
            self._sync_conn = conn
        return self._sync_conn

    def state(self, run_id: str, *, superstep: int | None = None) -> dict[str, Any]:
        """Get accumulated state synchronously.

        Same as ``get_state`` but uses stdlib ``sqlite3`` — no await needed.
        """
        db = self._sync_db()
        if superstep is not None:
            cursor = db.execute(
                "SELECT values_data FROM steps WHERE run_id = ? AND superstep <= ? ORDER BY step_index",
                (run_id, superstep),
            )
        else:
            cursor = db.execute(
                "SELECT values_data FROM steps WHERE run_id = ? ORDER BY step_index",
                (run_id,),
            )

        state: dict[str, Any] = {}
        for (values_blob,) in cursor:
            if values_blob is not None:
                values = self._serializer.deserialize(values_blob)
                if values:
                    state.update(values)
        return state

    def steps(self, run_id: str, *, superstep: int | None = None) -> list[StepRecord]:
        """Get step records synchronously."""
        db = self._sync_db()
        if superstep is not None:
            cursor = db.execute(
                f"SELECT {_STEPS_COLS} FROM steps WHERE run_id = ? AND superstep <= ? ORDER BY step_index",
                (run_id, superstep),
            )
        else:
            cursor = db.execute(
                f"SELECT {_STEPS_COLS} FROM steps WHERE run_id = ? ORDER BY step_index",
                (run_id,),
            )
        return [self._row_to_step(row) for row in cursor.fetchall()]

    def run(self, run_id: str) -> Run | None:
        """Get run metadata synchronously."""
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
        limit: int = 100,
    ) -> list[Run]:
        """List runs synchronously with optional filters."""
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
            params.append(since.isoformat())

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        cursor = db.execute(
            f"SELECT {_RUNS_COLS} FROM runs{where} ORDER BY created_at DESC LIMIT ?",
            params,
        )
        return [self._row_to_run(row) for row in cursor.fetchall()]

    def search_sync(self, query: str, *, field: str | None = None, limit: int = 20) -> list[StepRecord]:
        """Search steps synchronously using FTS5."""
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
            WHERE steps_fts MATCH ?
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (fts_query, limit),
        )
        return [self._row_to_step(row) for row in cursor.fetchall()]

    def values(self, run_id: str, *, key: str | None = None) -> dict[str, Any]:
        """Get run output values synchronously. Optionally filter to a single key."""
        full_state = self.state(run_id)
        if key is not None:
            return {key: full_state[key]} if key in full_state else {}
        return full_state

    def stats(self, run_id: str) -> dict[str, Any]:
        """Get per-node duration/frequency stats for a run."""
        db = self._sync_db()
        cursor = db.execute(
            """
            SELECT node_name, node_type,
                   COUNT(*) as executions,
                   SUM(duration_ms) as total_ms,
                   AVG(duration_ms) as avg_ms,
                   MAX(duration_ms) as max_ms,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as errors,
                   SUM(cached) as cache_hits
            FROM steps WHERE run_id = ?
            GROUP BY node_name
            ORDER BY total_ms DESC
            """,
            (run_id,),
        )
        return {
            row[0]: {
                "node_type": row[1],
                "executions": row[2],
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
        return Checkpoint(
            values=self.state(run_id, superstep=superstep),
            steps=self.steps(run_id, superstep=superstep),
        )
