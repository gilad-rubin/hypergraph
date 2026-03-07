"""SQLite-based checkpointer using aiosqlite."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from hypergraph.checkpointers._migrate import ensure_schema
from hypergraph.checkpointers.base import Checkpointer, CheckpointPolicy
from hypergraph.checkpointers.serializers import JsonSerializer, Serializer
from hypergraph.checkpointers.types import (
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
_STEPS_COLS = "id, run_id, step_index, superstep, node_name, node_type, status, duration_ms, cached, error, decision, input_versions, values_data, child_run_id, created_at, completed_at"
_STEP_TIME_ORDER = "COALESCE(completed_at, created_at), created_at, id"
_STEP_TIME_ORDER_DESC = "COALESCE(completed_at, created_at) DESC, created_at DESC, id DESC"
_STEP_TIME_ORDER_DESC_WITH_ALIAS = "COALESCE(s.completed_at, s.created_at) DESC, s.created_at DESC, s.id DESC"

# Sentinel for "parameter not provided" — distinct from None (which means "top-level only")
_UNSET = object()


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
        self._init_lock: asyncio.Lock | None = None
        self._aiosqlite = _require_aiosqlite()

    def _db_stats(self) -> dict[str, Any]:
        """Gather quick DB stats for display (uses sync connection)."""
        import os

        stats: dict[str, Any] = {"path": self._path}
        try:
            stats["size_bytes"] = os.path.getsize(self._path)
        except OSError:
            stats["size_bytes"] = None
        try:
            db = self._sync_db()
            (stats["run_count"],) = db.execute("SELECT COUNT(*) FROM runs").fetchone()
            (stats["step_count"],) = db.execute("SELECT COUNT(*) FROM steps").fetchone()
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

    def _repr_html_(self) -> str:
        from hypergraph._repr import MUTED_COLOR, _code, html_detail, html_kv, html_panel, theme_wrap, widget_state_key
        from hypergraph._utils import plural

        state_key = widget_state_key("checkpointer", self._path)
        try:
            stats = self._db_stats()
        except Exception:
            return theme_wrap(_code(f"SqliteCheckpointer: {self._path}"), state_key=state_key)

        kvs = [html_kv("Path", _code(str(stats["path"])))]
        if stats["size_bytes"] is not None:
            size_mb = stats["size_bytes"] / (1024 * 1024)
            kvs.append(html_kv("Size", f"{size_mb:.1f} MB" if size_mb >= 1 else f"{stats['size_bytes'] / 1024:.0f} KB"))
        if stats["run_count"] is not None:
            kvs.append(html_kv("Runs", str(stats["run_count"])))
            kvs.append(html_kv("Steps", str(stats["step_count"])))
        body = " &nbsp;|&nbsp; ".join(kvs)

        # Collapsible recent runs with inline steps
        try:
            recent = self.runs(limit=5)
            if recent:
                steps_by_run = {}
                for run in recent:
                    with contextlib.suppress(Exception):
                        steps_by_run[run.id] = self.steps(run.id)
                recent._steps_by_run = steps_by_run
                body += html_detail(
                    f"Recent runs ({plural(len(recent), 'shown')})",
                    recent._repr_html_(),
                    state_key="recent-runs",
                )
        except Exception:
            pass

        # API hints
        body += (
            f'<div style="color:{MUTED_COLOR}; font-size:0.85em; margin-top:8px">'
            f"Explore: {_code('.runs()')} {_code('.steps(run_id)')} "
            f"{_code('.search(query)')} {_code('.stats(run_id)')}"
            "</div>"
        )
        return theme_wrap(html_panel("SqliteCheckpointer", body), state_key=state_key)

    async def initialize(self) -> None:
        """Create database and tables if they don't exist."""
        # For file-backed DBs, create/migrate schema before async connect to avoid
        # opening a second connection while async holds a write lock.
        if not self._is_memory:
            self._ensure_sync_schema()

        self._db = await self._aiosqlite.connect(self._connect_path, uri=self._connect_uri)
        await self._db.execute("PRAGMA journal_mode=WAL")
        # For in-memory DBs, schema must be created after async connect so the
        # shared-cache database stays alive across connections.
        if self._is_memory:
            self._ensure_sync_schema()
        await self._db.commit()

    def _ensure_sync_schema(self) -> None:
        """Set up schema using sync connection (migration logic is sync)."""
        import sqlite3

        conn = sqlite3.connect(self._connect_path, uri=self._connect_uri)
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
        if self._db is not None:
            return
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
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
        await self._db.execute(
            "INSERT INTO runs (id, status, graph_name, created_at, parent_run_id, forked_from, fork_superstep, retry_of, retry_index, config) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET status = ?, graph_name = ?, parent_run_id = ?, "
            "forked_from = ?, fork_superstep = ?, retry_of = ?, retry_index = ?, config = ?",
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
        """Compute state by folding step values in timestamp execution order."""
        await self._ensure_db()

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

    async def get_steps(self, run_id: str, *, superstep: int | None = None) -> list[StepRecord]:
        """Get step records in execution order."""
        await self._ensure_db()

        if superstep is not None:
            cursor = await self._db.execute(
                f"SELECT {_STEPS_COLS} FROM steps WHERE run_id = ? AND superstep <= ? ORDER BY {_STEP_TIME_ORDER}",
                (run_id, superstep),
            )
        else:
            cursor = await self._db.execute(
                f"SELECT {_STEPS_COLS} FROM steps WHERE run_id = ? ORDER BY {_STEP_TIME_ORDER}",
                (run_id,),
            )

        rows = await cursor.fetchall()
        return StepTable(self._row_to_step(row) for row in rows)

    async def fork_workflow_async(
        self,
        source_run_id: str,
        *,
        workflow_id: str | None = None,
        superstep: int | None = None,
    ) -> tuple[str, Checkpoint]:
        """Prepare a fork checkpoint + target workflow id."""
        await self._ensure_db()
        source = await self.get_run_async(source_run_id)
        if source is None:
            raise ValueError(f"Unknown source workflow_id: {source_run_id!r}")
        checkpoint = await self.get_checkpoint(source_run_id, superstep=superstep)
        new_workflow_id = workflow_id or f"{source_run_id}-fork-{uuid.uuid4().hex[:6]}"
        return new_workflow_id, checkpoint

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
        parent_run_id: str | None = None,
        limit: int = 100,
    ) -> list[Run]:
        """List runs, optionally filtered by status and/or parent."""
        await self._ensure_db()

        conditions: list[str] = []
        params: list[Any] = []

        if status is not None:
            conditions.append("status = ?")
            params.append(status.value)
        if parent_run_id is not None:
            conditions.append("parent_run_id = ?")
            params.append(parent_run_id)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        cursor = await self._db.execute(
            f"SELECT {_RUNS_COLS} FROM runs{where} ORDER BY created_at DESC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        return RunTable(self._row_to_run(row) for row in rows)

    _FTS_FIELDS = frozenset({"node_name", "error"})

    async def search_async(self, query: str, *, field: str | None = None, limit: int = 20) -> list[StepRecord]:
        """Search steps using FTS5 (async)."""
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
            ORDER BY {_STEP_TIME_ORDER_DESC_WITH_ALIAS}
            LIMIT ?
            """,
            (fts_query, limit),
        )
        rows = await cursor.fetchall()
        return StepTable(self._row_to_step(row) for row in rows)

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
            created_at=_parse_dt(row[14]),
            completed_at=_parse_dt(row[15]),
            child_run_id=row[13],
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
        if self._sync_conn is None:
            import sqlite3

            # WAL mode allows concurrent readers alongside async writes
            # without "database is locked" errors
            conn = sqlite3.connect(self._connect_path, uri=self._connect_uri)
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

    def steps(self, run_id: str, *, superstep: int | None = None) -> list[StepRecord]:
        """Get step records synchronously."""
        db = self._sync_db()
        if superstep is not None:
            cursor = db.execute(
                f"SELECT {_STEPS_COLS} FROM steps WHERE run_id = ? AND superstep <= ? ORDER BY {_STEP_TIME_ORDER}",
                (run_id, superstep),
            )
        else:
            cursor = db.execute(
                f"SELECT {_STEPS_COLS} FROM steps WHERE run_id = ? ORDER BY {_STEP_TIME_ORDER}",
                (run_id,),
            )
        return StepTable(self._row_to_step(row) for row in cursor.fetchall())

    def get_run(self, run_id: str) -> Run | None:
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
        parent_run_id: str | None | object = _UNSET,
        limit: int = 100,
    ) -> list[Run]:
        """List runs synchronously with optional filters.

        Args:
            parent_run_id: Filter by parent relationship.
                Not provided (default) → all runs (backward compat).
                None → top-level only (no parent).
                "X" → children of run X.
        """
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
        if parent_run_id is not _UNSET:
            if parent_run_id is None:
                conditions.append("parent_run_id IS NULL")
            else:
                conditions.append("parent_run_id = ?")
                params.append(parent_run_id)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        cursor = db.execute(
            f"SELECT {_RUNS_COLS} FROM runs{where} ORDER BY created_at DESC LIMIT ?",
            params,
        )
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
        selected = self.get_run(workflow_id)
        if selected is None:
            raise ValueError(f"Unknown workflow_id: {workflow_id!r}")

        root = selected
        seen_ancestors = {root.id}
        while root.forked_from:
            parent = self.get_run(root.forked_from)
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
            ORDER BY {_STEP_TIME_ORDER_DESC_WITH_ALIAS}
            LIMIT ?
            """,
            (fts_query, limit),
        )
        return StepTable(self._row_to_step(row) for row in cursor.fetchall())

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
                   COUNT(*) as step_runs,
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
        if self.get_run(source_run_id) is None:
            raise ValueError(f"Unknown source workflow_id: {source_run_id!r}")
        checkpoint = self.checkpoint(source_run_id, superstep=superstep)
        new_workflow_id = workflow_id or f"{source_run_id}-fork-{uuid.uuid4().hex[:6]}"
        return new_workflow_id, checkpoint

    def retry_workflow(
        self,
        source_run_id: str,
        *,
        workflow_id: str | None = None,
        superstep: int | None = None,
    ) -> tuple[str, Checkpoint]:
        """Prepare a retry checkpoint + target workflow id (sync)."""
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
        db = self._sync_db()
        now = datetime.now(timezone.utc)
        config_json = json.dumps(config) if config is not None else None
        db.execute(
            "INSERT INTO runs (id, status, graph_name, created_at, parent_run_id, forked_from, fork_superstep, retry_of, retry_index, config) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET status = ?, graph_name = ?, parent_run_id = ?, "
            "forked_from = ?, fork_superstep = ?, retry_of = ?, retry_index = ?, config = ?",
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
        db = self._sync_db()
        values_blob = self._serializer.serialize(record.values) if record.values is not None else None
        input_versions_json = json.dumps(record.input_versions)
        decision_json = json.dumps(record.decision) if record.decision is not None else None

        db.execute(
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
        self._apply_retention_policy_sync(record.run_id)
        db.commit()

    async def _apply_retention_policy_async(self, run_id: str) -> None:
        """Apply configured retention policy after persisting a step (async)."""
        retention = self.policy.retention
        if retention == "full":
            return

        if retention == "latest":
            await self._db.execute(
                """
                DELETE FROM steps
                WHERE id IN (
                    SELECT id FROM (
                        SELECT
                            id,
                            ROW_NUMBER() OVER (
                                PARTITION BY node_name
                                ORDER BY COALESCE(completed_at, created_at) DESC, created_at DESC, id DESC
                            ) AS rn
                        FROM steps
                        WHERE run_id = ?
                    )
                    WHERE rn > 1
                )
                """,
                (run_id,),
            )
            return

        if retention == "windowed" and self.policy.window is not None:
            cursor = await self._db.execute("SELECT MAX(superstep) FROM steps WHERE run_id = ?", (run_id,))
            row = await cursor.fetchone()
            max_superstep = row[0] if row else None
            if max_superstep is None:
                return
            cutoff = max_superstep - self.policy.window + 1
            if cutoff <= 0:
                return
            await self._db.execute(
                "DELETE FROM steps WHERE run_id = ? AND superstep < ?",
                (run_id, cutoff),
            )

    def _apply_retention_policy_sync(self, run_id: str) -> None:
        """Apply configured retention policy after persisting a step (sync)."""
        retention = self.policy.retention
        if retention == "full":
            return

        db = self._sync_db()
        if retention == "latest":
            db.execute(
                """
                DELETE FROM steps
                WHERE id IN (
                    SELECT id FROM (
                        SELECT
                            id,
                            ROW_NUMBER() OVER (
                                PARTITION BY node_name
                                ORDER BY COALESCE(completed_at, created_at) DESC, created_at DESC, id DESC
                            ) AS rn
                        FROM steps
                        WHERE run_id = ?
                    )
                    WHERE rn > 1
                )
                """,
                (run_id,),
            )
            return

        if retention == "windowed" and self.policy.window is not None:
            row = db.execute("SELECT MAX(superstep) FROM steps WHERE run_id = ?", (run_id,)).fetchone()
            max_superstep = row[0] if row else None
            if max_superstep is None:
                return
            cutoff = max_superstep - self.policy.window + 1
            if cutoff <= 0:
                return
            db.execute(
                "DELETE FROM steps WHERE run_id = ? AND superstep < ?",
                (run_id, cutoff),
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
        db = self._sync_db()
        completed_at = datetime.now(timezone.utc).isoformat() if status != WorkflowStatus.ACTIVE else None

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
