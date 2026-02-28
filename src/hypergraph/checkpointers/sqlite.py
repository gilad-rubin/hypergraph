"""SQLite-based checkpointer using aiosqlite."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from hypergraph.checkpointers.base import Checkpointer, CheckpointPolicy
from hypergraph.checkpointers.serializers import JsonSerializer, Serializer
from hypergraph.checkpointers.types import StepRecord, StepStatus, Workflow, WorkflowStatus


def _require_aiosqlite() -> Any:
    """Import aiosqlite with a clear error message if not installed."""
    try:
        import aiosqlite

        return aiosqlite
    except ImportError:
        raise ImportError("SqliteCheckpointer requires aiosqlite. Install it with: pip install hypergraph[checkpoint]") from None


_CREATE_WORKFLOWS = """
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    graph_name TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
)
"""

_CREATE_STEPS = """
CREATE TABLE IF NOT EXISTS steps (
    workflow_id TEXT NOT NULL,
    superstep INTEGER NOT NULL,
    node_name TEXT NOT NULL,
    idx INTEGER NOT NULL,
    status TEXT NOT NULL,
    input_versions TEXT,
    values_data BLOB,
    duration_ms REAL NOT NULL DEFAULT 0.0,
    cached INTEGER NOT NULL DEFAULT 0,
    decision TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    child_workflow_id TEXT,
    UNIQUE(workflow_id, superstep, node_name)
)
"""

_CREATE_STEPS_IDX = "CREATE INDEX IF NOT EXISTS idx_steps_workflow ON steps(workflow_id, idx)"


class SqliteCheckpointer(Checkpointer):
    """SQLite-based workflow persistence.

    Best for: local development, single-server deployments, simple production.

    Args:
        path: Path to SQLite database file.
        policy: Checkpoint policy (default: async + full).
        serializer: Value serializer (default: JSON).

    Example::

        checkpointer = SqliteCheckpointer("./workflows.db")
        runner = AsyncRunner(checkpointer=checkpointer)
        result = await runner.run(graph, {"x": 1}, workflow_id="wf-1")

        # Query later
        state = await checkpointer.get_state("wf-1")
        steps = await checkpointer.get_steps("wf-1")
    """

    def __init__(
        self,
        path: str,
        *,
        policy: CheckpointPolicy | None = None,
        serializer: Serializer | None = None,
    ):
        super().__init__(policy=policy)
        self._path = path
        self._serializer = serializer or JsonSerializer()
        self._db: Any = None
        self._aiosqlite = _require_aiosqlite()

    async def initialize(self) -> None:
        """Create database and tables if they don't exist."""
        self._db = await self._aiosqlite.connect(self._path)
        await self._db.execute(_CREATE_WORKFLOWS)
        await self._db.execute(_CREATE_STEPS)
        await self._db.execute(_CREATE_STEPS_IDX)
        await self._db.commit()

    async def close(self) -> None:
        """Close database connection."""
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
                workflow_id, superstep, node_name, idx, status,
                input_versions, values_data, duration_ms, cached,
                decision, error, created_at, completed_at, child_workflow_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workflow_id, superstep, node_name) DO UPDATE SET
                status = excluded.status,
                values_data = excluded.values_data,
                duration_ms = excluded.duration_ms,
                cached = excluded.cached,
                decision = excluded.decision,
                error = excluded.error,
                completed_at = excluded.completed_at
            """,
            (
                record.workflow_id,
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
                record.created_at.isoformat(),
                record.completed_at.isoformat() if record.completed_at else None,
                record.child_workflow_id,
            ),
        )
        await self._db.commit()

    async def create_workflow(self, workflow_id: str, *, graph_name: str | None = None) -> Workflow:
        """Create a new workflow record."""
        await self._ensure_db()
        now = datetime.now(timezone.utc)
        await self._db.execute(
            "INSERT INTO workflows (id, status, graph_name, created_at) VALUES (?, ?, ?, ?)",
            (workflow_id, WorkflowStatus.ACTIVE.value, graph_name, now.isoformat()),
        )
        await self._db.commit()
        return Workflow(id=workflow_id, status=WorkflowStatus.ACTIVE, graph_name=graph_name, created_at=now)

    async def update_workflow_status(self, workflow_id: str, status: WorkflowStatus) -> None:
        """Update workflow status."""
        await self._ensure_db()
        completed_at = datetime.now(timezone.utc).isoformat() if status != WorkflowStatus.ACTIVE else None
        await self._db.execute(
            "UPDATE workflows SET status = ?, completed_at = ? WHERE id = ?",
            (status.value, completed_at, workflow_id),
        )
        await self._db.commit()

    # === Read ===

    async def get_state(self, workflow_id: str, *, superstep: int | None = None) -> dict[str, Any]:
        """Compute state by folding step values in index order."""
        await self._ensure_db()

        if superstep is not None:
            cursor = await self._db.execute(
                "SELECT values_data FROM steps WHERE workflow_id = ? AND superstep <= ? ORDER BY idx",
                (workflow_id, superstep),
            )
        else:
            cursor = await self._db.execute(
                "SELECT values_data FROM steps WHERE workflow_id = ? ORDER BY idx",
                (workflow_id,),
            )

        state: dict[str, Any] = {}
        async for (values_blob,) in cursor:
            if values_blob is not None:
                values = self._serializer.deserialize(values_blob)
                if values:
                    state.update(values)
        return state

    async def get_steps(self, workflow_id: str, *, superstep: int | None = None) -> list[StepRecord]:
        """Get step records in execution order."""
        await self._ensure_db()

        if superstep is not None:
            cursor = await self._db.execute(
                "SELECT * FROM steps WHERE workflow_id = ? AND superstep <= ? ORDER BY idx",
                (workflow_id, superstep),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM steps WHERE workflow_id = ? ORDER BY idx",
                (workflow_id,),
            )

        rows = await cursor.fetchall()
        return [self._row_to_step(row) for row in rows]

    async def get_workflow(self, workflow_id: str) -> Workflow | None:
        """Get workflow metadata."""
        await self._ensure_db()
        cursor = await self._db.execute(
            "SELECT id, status, graph_name, created_at, completed_at FROM workflows WHERE id = ?",
            (workflow_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_workflow(row)

    async def list_workflows(self, *, status: WorkflowStatus | None = None, limit: int = 100) -> list[Workflow]:
        """List workflows, optionally filtered by status."""
        await self._ensure_db()

        if status is not None:
            cursor = await self._db.execute(
                "SELECT id, status, graph_name, created_at, completed_at FROM workflows WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status.value, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT id, status, graph_name, created_at, completed_at FROM workflows ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )

        rows = await cursor.fetchall()
        return [self._row_to_workflow(row) for row in rows]

    # === Internal ===

    def _row_to_step(self, row: tuple[Any, ...]) -> StepRecord:
        """Convert a database row to StepRecord."""
        values_blob = row[6]
        values = self._serializer.deserialize(values_blob) if values_blob is not None else None
        input_versions = json.loads(row[5]) if row[5] else {}
        decision_raw = row[9]
        decision = json.loads(decision_raw) if decision_raw else None

        return StepRecord(
            workflow_id=row[0],
            superstep=row[1],
            node_name=row[2],
            index=row[3],
            status=StepStatus(row[4]),
            input_versions=input_versions,
            values=values,
            duration_ms=row[7],
            cached=bool(row[8]),
            decision=decision,
            error=row[10],
            created_at=datetime.fromisoformat(row[11]),
            completed_at=datetime.fromisoformat(row[12]) if row[12] else None,
            child_workflow_id=row[13],
        )

    def _row_to_workflow(self, row: tuple[Any, ...]) -> Workflow:
        """Convert a database row to Workflow."""
        return Workflow(
            id=row[0],
            status=WorkflowStatus(row[1]),
            graph_name=row[2],
            created_at=datetime.fromisoformat(row[3]),
            completed_at=datetime.fromisoformat(row[4]) if row[4] else None,
        )
