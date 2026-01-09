# Persistence & Checkpointing

**Minimal durable execution that can grow incrementally.**

---

## Design Philosophy

This design follows the principle: **start simple, extend when needed**.

| Principle | Application |
|-----------|-------------|
| **YAGNI** | Only core checkpointing in v1. Streaming, time travel, distributed locks deferred. |
| **KISS** | One durability mode, one reuse policy. Add options when users need them. |
| **Consistency** | Matches codebase patterns: ABC with defaults, frozen dataclasses, properties. |

### What's in v1 (Build Now)

- Step snapshots (save/load execution records)
- Checkpoints (save/load workflow state)
- Resume from interrupts
- `Checkpointer` base class with in-memory default
- `SQLiteCheckpointer` for production

### What's Deferred (Add Later)

| Feature | When to Add | Breaking Changes? |
|---------|-------------|-------------------|
| Streaming persistence | When LLM streaming needs recovery | No - add methods to subclasses |
| Time travel (fork, replay) | When users request debugging tools | No - add methods to subclasses |
| Retry policies | When users need automatic retries | No - add to node decorator |
| Distributed locks | When multi-worker deployment needed | No - add LeaseManager |
| Multiple durability modes | When performance tuning needed | No - add parameter |

---

## Quick Example

```python
from hypergraph import Graph, AsyncRunner
from hypergraph.persistence import SQLiteCheckpointer

# Create graph with durable execution
graph = Graph(nodes=[fetch, process, generate])
checkpointer = SQLiteCheckpointer("./workflows.db")

runner = AsyncRunner(checkpointer=checkpointer)

# First run - interrupted by user
result = await runner.run(
    graph,
    inputs={"query": "hello"},
    workflow_id="conversation-123",
)

if result.interrupted:
    print(f"Paused at: {result.interrupt_name}")

# Resume exactly where we left off
result = await runner.run(
    graph,
    inputs={"user_decision": "approve"},
    workflow_id="conversation-123",
    resume=True,
)
```

---

## Core Types

### StepIdentity

Hierarchical step identifier supporting nested graphs.

```python
@dataclass(frozen=True)
class StepIdentity:
    """Hierarchical step identifier.

    Supports nested graphs with path-based addressing:
    - Root step: ("generate:0",)
    - Nested step: ("outer:0", "rag:1", "retrieve:0")
    """

    path: tuple[str, ...]

    @property
    def step_id(self) -> str:
        """Full path as string. Example: 'outer:0/rag:1/retrieve:0'"""
        return "/".join(self.path)

    @property
    def node_name(self) -> str:
        """Node name from local step. Example: 'retrieve' for 'retrieve:0'"""
        return self.path[-1].rsplit(":", 1)[0]

    @property
    def batch_index(self) -> int:
        """Batch index from local step. Example: 0 for 'retrieve:0'"""
        return int(self.path[-1].rsplit(":", 1)[1])

    @property
    def depth(self) -> int:
        """Nesting depth (1 = root level)."""
        return len(self.path)

    @classmethod
    def root(cls, node_name: str, batch_index: int) -> "StepIdentity":
        """Create a root-level step identity."""
        return cls(path=(f"{node_name}:{batch_index}",))

    def child(self, local_id: str) -> "StepIdentity":
        """Create a child step identity for nested graphs."""
        return StepIdentity(path=self.path + (local_id,))
```

### StepSnapshot

Record of a single step execution.

```python
@dataclass
class StepSnapshot:
    """Record of a step execution."""

    # Identity
    identity: StepIdentity
    run_id: str
    node_name: str

    # State
    status: Literal["created", "running", "completed", "failed", "interrupted"] = "created"
    outputs: dict[str, Any] | None = None
    error: str | None = None

    # For gates
    decision: str | None = None

    # Metadata
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None

    @property
    def step_id(self) -> str:
        return self.identity.step_id

    @property
    def batch_index(self) -> int:
        return self.identity.batch_index
```

### Checkpoint

Snapshot of workflow state at a point in time.

```python
@dataclass
class Checkpoint:
    """Snapshot of workflow execution state."""

    # Identity
    checkpoint_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    workflow_id: str = ""
    run_id: str = ""

    # Position
    batch_index: int = 0

    # State
    values: dict[str, Any] = field(default_factory=dict)
    status: Literal["running", "interrupted", "completed", "error"] = "running"

    # Interrupt state (only populated if status == "interrupted")
    interrupt_name: str | None = None
    interrupt_value: Any = None

    # Metadata
    created_at: datetime = field(default_factory=datetime.utcnow)
    step_count: int = 0

    @property
    def is_interrupted(self) -> bool:
        return self.status == "interrupted"

    @property
    def is_complete(self) -> bool:
        return self.status in ("completed", "error")


@dataclass(frozen=True)
class CheckpointMetadata:
    """Lightweight checkpoint info for listing."""

    checkpoint_id: str
    workflow_id: str
    run_id: str
    status: Literal["running", "interrupted", "completed", "error"]
    created_at: datetime
    interrupt_name: str | None = None
```

---

## Checkpointer

### Base Class (In-Memory Default)

```python
from abc import ABC

class Checkpointer(ABC):
    """
    Base checkpointer with in-memory storage.

    Subclass and override methods for persistent storage.
    The base implementation works for testing and development.

    Design: Like HyperNode - an ABC with real implementation,
    not pure abstract methods. Subclasses override what they need.
    """

    def __init__(self):
        self._steps: dict[str, dict[str, StepSnapshot]] = {}
        self._checkpoints: dict[str, list[Checkpoint]] = {}

    # ==========================================
    # Step Operations
    # ==========================================

    async def save_step(self, run_id: str, step: StepSnapshot) -> None:
        """Save a step execution record."""
        if run_id not in self._steps:
            self._steps[run_id] = {}
        self._steps[run_id][step.step_id] = step

    async def load_step(self, run_id: str, step_id: str) -> StepSnapshot | None:
        """Load a step by ID."""
        return self._steps.get(run_id, {}).get(step_id)

    async def load_steps(self, run_id: str) -> list[StepSnapshot]:
        """Load all steps for a run, in creation order."""
        steps = self._steps.get(run_id, {}).values()
        return sorted(steps, key=lambda s: s.created_at)

    async def update_step(
        self,
        run_id: str,
        step_id: str,
        **updates: Any,
    ) -> StepSnapshot | None:
        """Update fields on an existing step."""
        step = await self.load_step(run_id, step_id)
        if step is None:
            return None
        for key, value in updates.items():
            setattr(step, key, value)
        await self.save_step(run_id, step)
        return step

    # ==========================================
    # Checkpoint Operations
    # ==========================================

    async def save_checkpoint(self, checkpoint: Checkpoint) -> str:
        """Save a checkpoint, return its ID."""
        wf_id = checkpoint.workflow_id
        if wf_id not in self._checkpoints:
            self._checkpoints[wf_id] = []
        self._checkpoints[wf_id].append(checkpoint)
        return checkpoint.checkpoint_id

    async def load_checkpoint(
        self,
        workflow_id: str,
        checkpoint_id: str | None = None,
    ) -> Checkpoint | None:
        """Load a checkpoint. If no ID given, returns latest."""
        cps = self._checkpoints.get(workflow_id, [])
        if not cps:
            return None
        if checkpoint_id:
            return next((c for c in cps if c.checkpoint_id == checkpoint_id), None)
        return cps[-1]

    async def list_checkpoints(
        self,
        workflow_id: str,
        limit: int | None = None,
    ) -> list[CheckpointMetadata]:
        """List checkpoints for a workflow (newest first)."""
        cps = self._checkpoints.get(workflow_id, [])
        result = [
            CheckpointMetadata(
                checkpoint_id=c.checkpoint_id,
                workflow_id=c.workflow_id,
                run_id=c.run_id,
                status=c.status,
                created_at=c.created_at,
                interrupt_name=c.interrupt_name,
            )
            for c in reversed(cps)
        ]
        return result[:limit] if limit else result
```

### SQLiteCheckpointer

```python
class SQLiteCheckpointer(Checkpointer):
    """SQLite-backed checkpointer for single-server production."""

    def __init__(self, path: str | Path):
        # Don't call super().__init__() - we use SQLite, not dicts
        self.path = Path(path)
        self._conn = self._setup_db()

    def _setup_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS steps (
                run_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                node_name TEXT NOT NULL,
                status TEXT NOT NULL,
                outputs BLOB,
                error TEXT,
                decision TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                PRIMARY KEY (run_id, step_id)
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                batch_index INTEGER NOT NULL,
                status TEXT NOT NULL,
                values_data BLOB,
                interrupt_name TEXT,
                interrupt_value BLOB,
                created_at TEXT NOT NULL,
                step_count INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_checkpoints_workflow
                ON checkpoints(workflow_id, created_at DESC);
        """)
        return conn

    # ---- Step Operations ----

    async def save_step(self, run_id: str, step: StepSnapshot) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO steps
            (run_id, step_id, node_name, status, outputs, error, decision, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                step.step_id,
                step.node_name,
                step.status,
                self._serialize(step.outputs) if step.outputs else None,
                step.error,
                step.decision,
                step.created_at.isoformat(),
                step.completed_at.isoformat() if step.completed_at else None,
            ),
        )
        self._conn.commit()

    async def load_step(self, run_id: str, step_id: str) -> StepSnapshot | None:
        row = self._conn.execute(
            "SELECT * FROM steps WHERE run_id = ? AND step_id = ?",
            (run_id, step_id),
        ).fetchone()
        return self._row_to_step(row) if row else None

    async def load_steps(self, run_id: str) -> list[StepSnapshot]:
        rows = self._conn.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        ).fetchall()
        return [self._row_to_step(row) for row in rows]

    # ---- Checkpoint Operations ----

    async def save_checkpoint(self, checkpoint: Checkpoint) -> str:
        self._conn.execute(
            """
            INSERT INTO checkpoints
            (checkpoint_id, workflow_id, run_id, batch_index, status,
             values_data, interrupt_name, interrupt_value, created_at, step_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint.checkpoint_id,
                checkpoint.workflow_id,
                checkpoint.run_id,
                checkpoint.batch_index,
                checkpoint.status,
                self._serialize(checkpoint.values),
                checkpoint.interrupt_name,
                self._serialize(checkpoint.interrupt_value) if checkpoint.interrupt_value else None,
                checkpoint.created_at.isoformat(),
                checkpoint.step_count,
            ),
        )
        self._conn.commit()
        return checkpoint.checkpoint_id

    async def load_checkpoint(
        self,
        workflow_id: str,
        checkpoint_id: str | None = None,
    ) -> Checkpoint | None:
        if checkpoint_id:
            row = self._conn.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM checkpoints WHERE workflow_id = ? ORDER BY created_at DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
        return self._row_to_checkpoint(row) if row else None

    async def list_checkpoints(
        self,
        workflow_id: str,
        limit: int | None = None,
    ) -> list[CheckpointMetadata]:
        query = "SELECT * FROM checkpoints WHERE workflow_id = ? ORDER BY created_at DESC"
        if limit:
            query += f" LIMIT {limit}"
        rows = self._conn.execute(query, (workflow_id,)).fetchall()
        return [
            CheckpointMetadata(
                checkpoint_id=row[0],
                workflow_id=row[1],
                run_id=row[2],
                status=row[4],
                created_at=datetime.fromisoformat(row[8]),
                interrupt_name=row[6],
            )
            for row in rows
        ]

    # ---- Helpers ----

    def _serialize(self, obj: Any) -> bytes:
        import pickle
        return pickle.dumps(obj)

    def _deserialize(self, data: bytes) -> Any:
        import pickle
        return pickle.loads(data)

    def _row_to_step(self, row: tuple) -> StepSnapshot:
        return StepSnapshot(
            identity=StepIdentity.from_string(row[1]),
            run_id=row[0],
            node_name=row[2],
            status=row[3],
            outputs=self._deserialize(row[4]) if row[4] else None,
            error=row[5],
            decision=row[6],
            created_at=datetime.fromisoformat(row[7]),
            completed_at=datetime.fromisoformat(row[8]) if row[8] else None,
        )

    def _row_to_checkpoint(self, row: tuple) -> Checkpoint:
        return Checkpoint(
            checkpoint_id=row[0],
            workflow_id=row[1],
            run_id=row[2],
            batch_index=row[3],
            status=row[4],
            values=self._deserialize(row[5]) if row[5] else {},
            interrupt_name=row[6],
            interrupt_value=self._deserialize(row[7]) if row[7] else None,
            created_at=datetime.fromisoformat(row[8]),
            step_count=row[9],
        )

    def close(self) -> None:
        """Close database connection."""
        self._conn.close()
```

---

## Runner Integration

### AsyncRunner with Checkpointer

```python
class AsyncRunner:
    def __init__(
        self,
        *,
        checkpointer: Checkpointer | None = None,
        # ... other params
    ):
        """
        Args:
            checkpointer: Persistence backend. If None, no durability.
        """
        self.checkpointer = checkpointer

    async def run(
        self,
        graph: Graph,
        inputs: dict[str, Any],
        *,
        workflow_id: str | None = None,
        resume: bool = False,
        # ... other params
    ) -> RunResult:
        """
        Execute graph with optional checkpointing.

        Args:
            graph: Graph to execute.
            inputs: Input values.
            workflow_id: Durable workflow identifier. Required if checkpointer set.
            resume: If True, load latest checkpoint and continue.

        Resume behavior:
            1. Load latest checkpoint for workflow_id
            2. Merge checkpoint.values with inputs (inputs win on conflict)
            3. Skip completed steps (use cached outputs)
            4. Continue from checkpoint.batch_index
        """
        ...
```

### Execution Flow with Persistence

```python
# Simplified execution flow

async def _execute_with_persistence(self, graph, inputs, workflow_id, resume):
    # 1. Load or create checkpoint
    if resume and self.checkpointer:
        checkpoint = await self.checkpointer.load_checkpoint(workflow_id)
        if checkpoint:
            values = {**checkpoint.values, **inputs}  # inputs override
            batch_index = checkpoint.batch_index
        else:
            values = inputs
            batch_index = 0
    else:
        values = inputs
        batch_index = 0

    run_id = uuid.uuid4().hex[:12]

    # 2. Execute batches
    for batch in graph.batches[batch_index:]:
        for node in batch:
            step_id = f"{node.name}:{batch.index}"

            # Check if already completed (replay)
            if self.checkpointer:
                existing = await self.checkpointer.load_step(run_id, step_id)
                if existing and existing.status == "completed":
                    values.update(existing.outputs)
                    continue

            # Create step snapshot
            step = StepSnapshot(
                identity=StepIdentity.root(node.name, batch.index),
                run_id=run_id,
                node_name=node.name,
                status="running",
            )
            if self.checkpointer:
                await self.checkpointer.save_step(run_id, step)

            # Execute
            try:
                outputs = await self._execute_node(node, values)
                step.status = "completed"
                step.outputs = outputs
                step.completed_at = datetime.utcnow()
                values.update(outputs)
            except Exception as e:
                step.status = "failed"
                step.error = str(e)
                raise
            finally:
                if self.checkpointer:
                    await self.checkpointer.save_step(run_id, step)

        # 3. Save checkpoint after each batch
        if self.checkpointer:
            checkpoint = Checkpoint(
                workflow_id=workflow_id,
                run_id=run_id,
                batch_index=batch.index + 1,
                values=values,
                status="running",
                step_count=len(values),
            )
            await self.checkpointer.save_checkpoint(checkpoint)

    # 4. Final checkpoint
    if self.checkpointer:
        checkpoint.status = "completed"
        await self.checkpointer.save_checkpoint(checkpoint)

    return RunResult(outputs=values, status="completed")
```

### Interrupt Handling

```python
# When an InterruptNode is reached

async def _handle_interrupt(self, node, values, workflow_id, run_id):
    # Get the value to show user
    interrupt_value = values[node.input_param]

    # Save interrupted checkpoint
    if self.checkpointer:
        checkpoint = Checkpoint(
            workflow_id=workflow_id,
            run_id=run_id,
            batch_index=current_batch,
            values=values,
            status="interrupted",
            interrupt_name=node.name,
            interrupt_value=interrupt_value,
        )
        await self.checkpointer.save_checkpoint(checkpoint)

    # Return interrupted result
    return RunResult(
        outputs=values,
        status="interrupted",
        interrupt_name=node.name,
        interrupt_value=interrupt_value,
    )
```

---

## Extending the Design

The base design supports incremental extension without breaking changes.

### Extension Pattern

When adding a new feature:

1. **Add methods to the base `Checkpointer` class** with working in-memory defaults
2. **Override in subclasses** (SQLiteCheckpointer, etc.) for durable storage
3. **Runner just calls the methods** - no capability checks needed

This means:
- No `hasattr()` checks
- No `isinstance()` checks
- Everything just works - base class uses memory, subclasses use storage

```python
# When we add streaming support, it goes in the BASE class:

class Checkpointer(ABC):
    def __init__(self):
        self._steps = {}
        self._checkpoints = {}
        self._stream_chunks = {}  # NEW storage for streaming

    # ... existing methods ...

    # NEW: Streaming with in-memory default
    async def save_stream_chunk(
        self,
        run_id: str,
        step_id: str,
        chunk: str,
        index: int,
    ) -> None:
        """Save a streaming chunk. In-memory by default."""
        key = (run_id, step_id)
        if key not in self._stream_chunks:
            self._stream_chunks[key] = []
        self._stream_chunks[key].append(chunk)

    async def load_stream_chunks(self, run_id: str, step_id: str) -> list[str]:
        """Load streaming chunks. In-memory by default."""
        return self._stream_chunks.get((run_id, step_id), [])


class SQLiteCheckpointer(Checkpointer):
    # ... existing overrides ...

    # NEW: Override for durable streaming
    async def save_stream_chunk(
        self,
        run_id: str,
        step_id: str,
        chunk: str,
        index: int,
    ) -> None:
        """Save a streaming chunk to SQLite."""
        self._conn.execute(
            "INSERT INTO stream_chunks VALUES (?, ?, ?, ?)",
            (run_id, step_id, index, chunk),
        )
        self._conn.commit()
```

The runner code is simple - no checks:

```python
# Runner just calls the method. Works with any checkpointer.
async for chunk in stream:
    yield chunk
    await self.checkpointer.save_stream_chunk(run_id, step_id, chunk, index)
```

- Using `Checkpointer()` (base): chunks stored in memory, lost on crash
- Using `SQLiteCheckpointer()`: chunks stored durably, survive crash

### Adding Streaming Persistence (Future)

Add to base class with in-memory default, override in SQLite for durability.

**New methods:**
- `save_stream_chunk(run_id, step_id, chunk, index)`
- `load_stream_chunks(run_id, step_id) -> list[str]`
- `complete_stream(run_id, step_id, final_value)`

**New table in SQLite:**
```sql
CREATE TABLE stream_chunks (
    run_id TEXT,
    step_id TEXT,
    chunk_index INTEGER,
    chunk TEXT,
    PRIMARY KEY (run_id, step_id, chunk_index)
);
```

### Adding Time Travel (Future)

Add to base class with in-memory default, override in SQLite for durability.

**New methods:**
- `fork(checkpoint_id, new_workflow_id) -> Checkpoint`
- `get_history(workflow_id) -> list[Checkpoint]`

### Adding Retry Policies (Future)

Add to node configuration, not checkpointer:

```python
@dataclass
class RetryPolicy:
    """Retry configuration for a node."""
    max_attempts: int = 3
    initial_delay: float = 1.0
    backoff_multiplier: float = 2.0
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,)

@node(
    output_name="data",
    retry=RetryPolicy(max_attempts=5, retryable_exceptions=(ConnectionError,)),
)
async def fetch(url: str) -> dict:
    return await httpx.get(url).json()
```

The runner handles retries during execution. Add `attempts: list[Attempt]` to `StepSnapshot` when implementing.

### Adding Distributed Locks (Future)

Create a separate class (composition, not inheritance):

```python
class LeaseManager:
    """Distributed lock manager for multi-worker deployments."""

    async def acquire_workflow_lock(
        self,
        workflow_id: str,
        worker_id: str,
        ttl: float = 60.0,
    ) -> bool:
        """Try to acquire exclusive lock on a workflow."""
        ...

    async def release_workflow_lock(
        self,
        workflow_id: str,
        worker_id: str,
    ) -> None:
        """Release lock on a workflow."""
        ...


# Usage - separate from checkpointer
runner = AsyncRunner(
    checkpointer=PostgresCheckpointer(...),
    lease_manager=PostgresLeaseManager(...),  # Separate concern
)
```

---

## Exceptions

```python
class PersistenceError(Exception):
    """Base exception for persistence errors."""
    pass


class CheckpointNotFoundError(PersistenceError):
    """Requested checkpoint doesn't exist."""

    def __init__(self, workflow_id: str, checkpoint_id: str | None = None):
        self.workflow_id = workflow_id
        self.checkpoint_id = checkpoint_id
        msg = f"No checkpoint found for workflow '{workflow_id}'"
        if checkpoint_id:
            msg = f"Checkpoint '{checkpoint_id}' not found"
        super().__init__(msg)


class WorkflowNotFoundError(PersistenceError):
    """Requested workflow doesn't exist."""

    def __init__(self, workflow_id: str):
        self.workflow_id = workflow_id
        super().__init__(f"Workflow '{workflow_id}' not found")
```

---

## Usage Examples

### Basic Persistence

```python
from hypergraph import Graph, AsyncRunner, node
from hypergraph.persistence import SQLiteCheckpointer

@node(output_name="result")
async def process(data: str) -> str:
    return data.upper()

graph = Graph(nodes=[process])
checkpointer = SQLiteCheckpointer("./workflows.db")
runner = AsyncRunner(checkpointer=checkpointer)

result = await runner.run(
    graph,
    inputs={"data": "hello"},
    workflow_id="job-123",
)
```

### Resume After Interrupt

```python
from hypergraph import Graph, AsyncRunner, node, InterruptNode
from hypergraph.persistence import SQLiteCheckpointer

@node(output_name="draft")
async def generate(prompt: str) -> str:
    return await llm.generate(prompt)

approval = InterruptNode(
    name="approval",
    input_param="draft",
    response_param="decision",
)

@node(output_name="final")
def finalize(draft: str, decision: str) -> str:
    if decision == "approve":
        return draft
    return f"REJECTED: {draft}"

graph = Graph(nodes=[generate, approval, finalize])
runner = AsyncRunner(checkpointer=SQLiteCheckpointer("./workflows.db"))

# First run - will pause at approval
result = await runner.run(
    graph,
    inputs={"prompt": "Write a poem"},
    workflow_id="poem-456",
)

assert result.interrupted
print(f"Draft: {result.interrupt_value}")

# ... later, user approves ...

# Resume with decision
result = await runner.run(
    graph,
    inputs={"decision": "approve"},
    workflow_id="poem-456",
    resume=True,
)

print(result.outputs["final"])
```

### Testing with In-Memory Checkpointer

```python
from hypergraph.persistence import Checkpointer

# Base class works as in-memory checkpointer
checkpointer = Checkpointer()
runner = AsyncRunner(checkpointer=checkpointer)

# Run tests - state is lost when process ends
result = await runner.run(graph, inputs={...}, workflow_id="test-1")
```

---

## Summary

| Component | v1 Scope | Extension Point |
|-----------|----------|-----------------|
| `StepSnapshot` | Status, outputs, error | Add `attempts: list` for retry tracking |
| `Checkpoint` | Values, interrupt state | Add `pending_writes` for parallel recovery |
| `Checkpointer` | save/load steps & checkpoints | Add methods to base class with in-memory defaults |
| `SQLiteCheckpointer` | Full implementation | Override new methods for durable storage |
| Runner integration | Basic resume | Just call checkpointer methods - no checks needed |

**Design principles:**
- ABC with default implementation (like `HyperNode`)
- Frozen dataclasses for identity types (like `StepIdentity`)
- Mutable dataclasses for state (like `StepSnapshot`)
- Properties for derived values
- Subclasses override storage, don't call super
- New features added to base class with working defaults (no capability checks)
