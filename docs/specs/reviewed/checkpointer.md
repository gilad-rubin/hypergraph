# Checkpointer

**The interface for workflow persistence and state management.**

Checkpointers store workflow state to enable resume, crash recovery, and multi-turn conversations. Unlike EventProcessor (which handles observability), Checkpointer is a bidirectional interface for reading and writing durable state.

---

## Overview

### Design Principles

1. **Steps are the source of truth** - State is computed from steps, not stored separately
2. **Bidirectional interface** - Both read (load) and write (save) operations
3. **Configurable durability** - Users choose their performance/durability tradeoff via [CheckpointPolicy](#checkpoint-policy)
4. **Separate from observability** - Checkpointer is not an EventProcessor (see [Why](#why-checkpointer-is-separate))

### Architecture

```
Runner
  │
  ├── Events ──► EventProcessor (observability, write-only)
  │
  └── Steps ──► Checkpointer (durability, read + write)
                  │
                  ├── save_step()      Write
                  ├── get_state()      Read (computed)
                  ├── get_steps()      Read
                  └── get_workflow()   Read
```

---

## Checkpoint Policy

Control when checkpoints are written and what history is retained.

### The Two Dimensions

| Dimension | Question | Trade-off |
|-----------|----------|-----------|
| **Durability** | When/how to write? | Performance vs crash recovery |
| **Retention** | What history to keep? | Storage vs time travel |

### CheckpointPolicy Class

```python
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

@dataclass
class CheckpointPolicy:
    """
    Controls checkpoint behavior.

    Args:
        durability: When/how to write checkpoints
            - "sync": After each step, block until written (safest)
            - "async": After each step, write in background (default)
            - "exit": Only at run completion (fastest, no mid-run recovery)
        retention: What history to keep
            - "full": All steps (time travel enabled)
            - "latest": Only materialized state (bounded storage)
            - "windowed": Keep last N supersteps
        window: Number of supersteps to keep (required if retention="windowed")
        ttl: Auto-expire completed workflows after duration (optional)

    Raises:
        ValueError: If durability="exit" with retention="full" or "windowed"
        ValueError: If retention="windowed" without window parameter
    """
    durability: Literal["sync", "async", "exit"] = "async"
    retention: Literal["full", "latest", "windowed"] = "full"
    window: int | None = None
    ttl: timedelta | None = None

    def __post_init__(self):
        if self.durability == "exit" and self.retention != "latest":
            raise ValueError(
                f'durability="exit" requires retention="latest", '
                f'got retention="{self.retention}". '
                f'With exit mode, steps are not persisted mid-run, '
                f'so keeping history is not possible.'
            )

        if self.retention == "windowed" and self.window is None:
            raise ValueError(
                'retention="windowed" requires window parameter'
            )

        if self.retention != "windowed" and self.window is not None:
            raise ValueError(
                f'window parameter only valid with retention="windowed", '
                f'got retention="{self.retention}"'
            )
```

### Durability Options

| Value | Behavior | Use Case |
|-------|----------|----------|
| `"sync"` | Block after each step until checkpoint written | HITL, critical workflows where no step can be lost |
| `"async"` | Write in background while next step runs | Default — good balance of performance and safety |
| `"exit"` | Only write final state at run completion | Long-running agents, idempotent/retryable work |

**Crash recovery by durability:**

```
sync:   [node A] → [write] → [node B] → [write] → [node C]
                   ↑ blocks            ↑ blocks
        Crash at any point → resume from last completed step

async:  [node A] → [node B] → [node C] → ...
                   ↳ write A   ↳ write B   (background)
        Crash → may lose 1 in-flight step

exit:   [node A] → [node B] → [node C] → [write final]
        Crash mid-run → lose entire run, must restart
```

### Retention Options

| Value | What's Kept | Time Travel | Use Case |
|-------|-------------|-------------|----------|
| `"full"` | All steps | Yes | Debugging, audit trails, compliance |
| `"latest"` | Only materialized state | No | Chat apps, monitors, long-running agents |
| `"windowed"` | Last N supersteps | Recent only | Production with limited debugging |

### Valid Combinations

| durability ↓ / retention → | `"full"` | `"latest"` | `"windowed"` |
|----------------------------|:--------:|:----------:|:------------:|
| `"sync"` | ✓ | ✓ | ✓ |
| `"async"` | ✓ | ✓ | ✓ |
| `"exit"` | ✗ Error | ✓ | ✗ Error |

**Why `exit` + `full` is invalid:** With `durability="exit"`, steps are not persisted mid-run. If the process crashes, there's nothing to recover. Keeping "full history" of steps that were never written is contradictory.

### Usage Examples

```python
from hypergraph.checkpointers import SqliteCheckpointer, CheckpointPolicy

# Default: good balance of safety and performance
checkpointer = SqliteCheckpointer("./workflows.db")
# Equivalent to: CheckpointPolicy(durability="async", retention="full")

# Maximum safety: block on every write, keep all history
checkpointer = SqliteCheckpointer(
    "./workflows.db",
    policy=CheckpointPolicy(durability="sync", retention="full"),
)

# Bounded storage: checkpoint each step, but prune old history
checkpointer = SqliteCheckpointer(
    "./workflows.db",
    policy=CheckpointPolicy(durability="async", retention="latest"),
)

# Fast + bounded: only checkpoint at end, keep latest state
checkpointer = SqliteCheckpointer(
    "./workflows.db",
    policy=CheckpointPolicy(durability="exit", retention="latest"),
)

# Rolling window: keep last 50 supersteps for debugging
checkpointer = SqliteCheckpointer(
    "./workflows.db",
    policy=CheckpointPolicy(
        durability="async",
        retention="windowed",
        window=50,
    ),
)

# Auto-expire completed workflows after 7 days
checkpointer = SqliteCheckpointer(
    "./workflows.db",
    policy=CheckpointPolicy(
        durability="async",
        retention="full",
        ttl=timedelta(days=7),
    ),
)
```

### Choosing a Policy

| Scenario | Recommended Policy |
|----------|-------------------|
| Development / debugging | `durability="async"`, `retention="full"` (default) |
| Production chat app | `durability="async"`, `retention="latest"` |
| Long-running monitor agent | `durability="exit"`, `retention="latest"` |
| Compliance / audit required | `durability="sync"`, `retention="full"`, `ttl=timedelta(days=90)` |
| High-throughput pipeline | `durability="async"`, `retention="windowed"`, `window=10` |

---

## Checkpointer Interface

### Base Class

```python
from abc import ABC, abstractmethod
from typing import Any

class Checkpointer(ABC):
    """
    Base class for workflow persistence.

    Implementations store workflow steps and provide state retrieval.
    Steps are the source of truth; state is computed from steps.

    Args:
        policy: Controls durability and retention behavior.
                Defaults to CheckpointPolicy() (async + full).
    """

    def __init__(self, policy: CheckpointPolicy | None = None):
        self.policy = policy or CheckpointPolicy()

    # === Write Operations ===

    @abstractmethod
    async def save_step(self, record: StepRecord) -> None:
        """
        Save a step atomically.

        Called by the runner after each node completes. The entire record
        is persisted in a single atomic operation - either all data is
        saved, or nothing. This prevents corrupted state from crashes.

        Implementations should use:
        - Database transactions for SQL backends
        - Atomic document writes for document stores
        - Upsert semantics with unique constraint on (workflow_id, superstep, node_name)

        Args:
            record: Complete step record with metadata and values
        """
        ...

    # Internal: Called by runner, not user-facing
    @abstractmethod
    async def create_workflow(
        self,
        workflow_id: str,
    ) -> Workflow:
        """
        Create a new workflow record.

        Internal method called by the runner when starting a new workflow.
        Users should call runner.run() with a workflow_id instead.

        Args:
            workflow_id: Unique workflow identifier

        Returns:
            The created Workflow object
        """
        ...

    @abstractmethod
    async def update_workflow_status(
        self,
        workflow_id: str,
        status: WorkflowStatus,
    ) -> None:
        """Update workflow status (ACTIVE, COMPLETED, or FAILED)."""
        ...

    # === Read Operations ===

    @abstractmethod
    async def get_state(
        self,
        workflow_id: str,
        superstep: int | None = None,
    ) -> dict[str, Any]:
        """
        Get accumulated state through a superstep.

        State is logically COMPUTED by folding over steps through `superstep`.
        Implementations may use materialized state/snapshots for performance,
        as long as results match the fold over StepRecords.

        Args:
            workflow_id: Unique workflow identifier
            superstep: Include outputs through this superstep (None = latest)

        Returns:
            Accumulated output values: {"messages": [...], "answer": "..."}
        """
        ...

    @abstractmethod
    async def get_steps(
        self,
        workflow_id: str,
        superstep: int | None = None,
    ) -> list[StepRecord]:
        """
        Get step records through a superstep.

        Args:
            workflow_id: Unique workflow identifier
            superstep: Include steps through this superstep (None = all)

        Returns:
            List of StepRecord in execution order
        """
        ...

    async def get_checkpoint(
        self,
        workflow_id: str,
        superstep: int | None = None,
    ) -> Checkpoint:
        """
        Get a checkpoint for forking workflows.

        Combines get_state() and get_steps() into a single Checkpoint object.
        Default implementation calls both; subclasses may optimize.

        Args:
            workflow_id: Unique workflow identifier
            superstep: Checkpoint through this superstep (None = latest)

        Returns:
            Checkpoint with computed outputs and step history
        """
        values = await self.get_state(workflow_id, superstep=superstep)
        steps = await self.get_steps(workflow_id, superstep=superstep)
        return Checkpoint(values=values, steps=steps)

    @abstractmethod
    async def get_workflow(
        self,
        workflow_id: str,
    ) -> Workflow | None:
        """
        Get workflow metadata.

        Returns:
            Workflow object or None if not found
        """
        ...

    @abstractmethod
    async def list_workflows(
        self,
        status: WorkflowStatus | None = None,
        limit: int = 100,
    ) -> list[Workflow]:
        """
        List workflows, optionally filtered by status.

        Args:
            status: Filter by status (None = all)
            limit: Maximum number to return

        Returns:
            List of Workflow objects
        """
        ...

    # === Lifecycle ===

    async def initialize(self) -> None:
        """
        Initialize the checkpointer (create tables, etc.).

        Called once when runner starts. Default is no-op.
        """
        pass

    async def close(self) -> None:
        """
        Clean up resources (close connections, etc.).

        Called when runner shuts down. Default is no-op.
        """
        pass
```

### SyncRunner: No Checkpointer (Use Cache Instead)

**SyncRunner does not support checkpointing.** This is by design:

- SyncRunner is for simple blocking scripts
- No workflow identity or step history needed
- No HITL (InterruptNode requires async)

**For long-running sync DAGs, use cache as "poor man's durability":**

```python
from hypergraph import SyncRunner, DiskCache

runner = SyncRunner(cache=DiskCache("./cache"))

# First run — all nodes execute, results cached
result = runner.run(graph, values={"data": big_file})
# 💥 CRASH at node 5

# Restart with same inputs — nodes 1-4 cache hit, only 5+ execute
result = runner.run(graph, values={"data": big_file})
```

**When cache is enough:**
- DAGs only (no cycles — cache key changes each iteration)
- Same inputs on restart
- Don't need workflow identity or status queries
- No human-in-the-loop

**When you need checkpointer:**
- Cycles, HITL, or different inputs on resume → use AsyncRunner + Checkpointer

See [Durable Execution](durable-execution.md#syncrunner-cache-based-durability) for the full pattern.

---

## Why Checkpointer Is Separate

Checkpointer is **not** an EventProcessor. This is deliberate:

| Concern | EventProcessor | Checkpointer |
|---------|----------------|--------------|
| **Direction** | Write-only (push) | Read + Write (bidirectional) |
| **Data** | All events | All outputs |
| **Purpose** | Observability | Durability |
| **Failure mode** | Fire-and-forget | Must succeed |

**The read path doesn't fit the event model.** When resuming a workflow, the runner needs to *query* for existing state. Events are write-only; they flow from runner to consumers.

**Configuration is different.** Streaming recovery modes, serialization, and storage backends are checkpointer concerns, not observability concerns.

See [Observability](observability.md) for EventProcessor details.

---

## State vs Steps

### Steps Are the Source of Truth

**State is computed from StepRecords, not stored separately.**

```
StepRecords (stored atomically):
  Superstep 0: embed, validate (parallel)  → values={"embedding": [...], "valid": true}
  Superstep 1: retrieve                    → values={"docs": [...]}
  Superstep 2: generate                    → values={"answer": "..."}

State (computed by folding values):
  get_state(superstep=2) → {"embedding": [...], "valid": true, "docs": [...], "answer": "..."}
```

### What Gets Saved

**Everything is saved atomically.** Each StepRecord contains both metadata and outputs in a single write.

| Field | Contains |
|-------|----------|
| workflow_id, superstep, node_name, index | Identity |
| status | Execution status |
| input_versions | For staleness detection |
| values | Output values |

This ensures full crash recovery — on resume, completed nodes are skipped, incomplete nodes re-run. Because metadata and values are in one atomic write, there's no possibility of corrupted state from crashes.

### Implications

- **Single source of truth**: Steps are authoritative; state is derived
- **Time travel**: Get state at any historical point
- **No sync issues**: State can never be "out of sync" with steps
- **Full durability**: All nodes are recoverable on crash
- **Atomic writes**: No partial state from crashes between writes

### When to Use Each

| Operation | API | Use Case |
|-----------|-----|----------|
| Continue conversation | `get_state()` | Need accumulated values |
| Debug execution | `get_steps()` | Need step-by-step trail |
| Fork workflow | Both | Need state + steps up to a point |

---

## State Materialization (Performance)

`get_state()` is defined as "fold StepRecords", but long-lived workflows (chat threads, ETL, schedulers) cannot afford O(n) replay on every resume/inspection.

**Design rules:**
- **Steps remain the source of truth** (correctness, time travel) when `retention="full"`.
- **Checkpointers SHOULD materialize derived state** to make the common path fast.
- **Materialization is REQUIRED** when `retention="latest"` — steps are pruned, so state must be stored directly.

### Recommended Materializations

Most backends will want two derived structures (both rebuildable from StepRecords):

1. **Latest values index (fast latest state)**
   - Key: `(workflow_id, output_name)`
   - Value: `{value, last_step_index, last_superstep, version}`
   - Updated transactionally inside `save_step()`

2. **Periodic snapshots (fast historical state)**
   - Key: `(workflow_id, superstep)`
   - Value: full state dict at that superstep (or a compacted representation)
   - Written periodically (e.g., every superstep, every N supersteps, or size-based)

### How `get_state()` Should Work

For `superstep=None` (latest):
- Prefer `latest_values` → O(number of outputs) to assemble the dict.

For `superstep=X` (historical):
- Load the nearest snapshot at `S <= X`.
- Apply deltas from StepRecords in `(S, X]` (state.update(step.values)).

This keeps:
- **Correctness:** exactly matches folding StepRecords.
- **Performance:** bounded by snapshot interval + number of distinct outputs, not total workflow age.

### Rebuild, Retention, and Compaction

Materializations are caches. Implementations should be able to:
- Rebuild `latest_values` and snapshots by replaying StepRecords.
- Optionally archive cold StepRecords (e.g., to object storage) if required for cost,
  as long as `get_state(superstep=...)` remains correct and reconstructible.

---

## Built-in Implementations

### SqliteCheckpointer

```python
from hypergraph.checkpointers import SqliteCheckpointer, CheckpointPolicy

checkpointer = SqliteCheckpointer(
    path="./workflows.db",          # Database file path
    # policy=CheckpointPolicy(),    # Optional (default: async + full)
    # serializer=JsonSerializer(),  # Optional custom serializer
)

runner = AsyncRunner(checkpointer=checkpointer)

# Example: bounded storage for long-running workflows
checkpointer = SqliteCheckpointer(
    path="./workflows.db",
    policy=CheckpointPolicy(durability="async", retention="latest"),
)
```

**Best for:** Local development, single-server deployments, simple production.

### PostgresCheckpointer

```python
from hypergraph.checkpointers import PostgresCheckpointer, CheckpointPolicy

checkpointer = PostgresCheckpointer(
    connection_string="postgresql://user:pass@host/db",
    # policy=CheckpointPolicy(),    # Optional (default: async + full)
    # pool_size=10,                 # Connection pool size
    # serializer=JsonSerializer(),  # Optional custom serializer
)

runner = AsyncRunner(checkpointer=checkpointer)

# Example: production with 30-day retention
checkpointer = PostgresCheckpointer(
    connection_string="postgresql://...",
    policy=CheckpointPolicy(
        durability="async",
        retention="full",
        ttl=timedelta(days=30),
    ),
)
```

**Best for:** Multi-server deployments, high availability, production.

### Capabilities Comparison

| Capability | SqliteCheckpointer | PostgresCheckpointer | DBOS |
|------------|:------------------:|:--------------------:|:----:|
| Resume from latest | ✅ | ✅ | ✅ |
| Resume from specific superstep | ✅ | ✅ | ✅ |
| Get current state | ✅ | ✅ | ✅ |
| List workflows | ✅ | ✅ | ✅ |
| Step history | ✅ | ✅ | ✅ |
| Automatic crash recovery | ❌ | ❌ | ✅ |
| Workflow forking (get_checkpoint) | ✅ | ✅ | ✅ |
| Multi-server | ❌ | ✅ | ✅ |
| CheckpointPolicy support | ✅ | ✅ | ⚠️ Limited |

> **Note:** DBOS has its own durability model. When using `DBOSAsyncRunner`, the policy is partially respected — `retention` works, but `durability` is controlled by DBOS's internal mechanisms.

### Behavior by Retention Policy

| Method | `retention="full"` | `retention="latest"` | `retention="windowed"` |
|--------|:------------------:|:--------------------:|:----------------------:|
| `get_state()` | ✅ Any superstep | ✅ Latest only | ✅ Within window |
| `get_state(superstep=N)` | ✅ | ❌ Error | ⚠️ If N in window |
| `get_steps()` | ✅ All steps | ❌ Empty list | ✅ Steps in window |
| `get_checkpoint()` | ✅ Full fork | ⚠️ State only | ⚠️ Partial history |

---

## Implementing a Custom Checkpointer

### Minimal Implementation

```python
from hypergraph.checkpointers import Checkpointer
from hypergraph.types import StepRecord, Workflow, WorkflowStatus

class RedisCheckpointer(Checkpointer):
    """Example Redis-based checkpointer."""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.client = None

    async def initialize(self) -> None:
        import redis.asyncio as redis
        self.client = await redis.from_url(self.redis_url)

    async def close(self) -> None:
        if self.client:
            await self.client.close()

    async def save_step(self, record: StepRecord) -> None:
        """Save step atomically - single write contains all data."""
        key = f"workflow:{record.workflow_id}:step:{record.index}"
        await self.client.hset(key, mapping={
            "superstep": record.superstep,
            "node_name": record.node_name,
            "status": record.status.value,
            "input_versions": json.dumps(record.input_versions),
            "values": json.dumps(record.values),
        })
        # Update step count
        await self.client.hset(
            f"workflow:{record.workflow_id}",
            "step_count",
            record.index + 1
        )

    async def get_state(
        self,
        workflow_id: str,
        superstep: int | None = None,
    ) -> dict[str, Any]:
        # Fold over steps to compute state
        steps = await self.get_steps(workflow_id, superstep=superstep)
        state = {}
        for step in sorted(steps, key=lambda s: s.index):
            if step.values:
                state.update(step.values)
        return state

    async def get_steps(
        self,
        workflow_id: str,
        superstep: int | None = None,
    ) -> list[StepRecord]:
        # Implementation details...
        pass

    # ... other methods
```

### Key Implementation Notes

1. **save_step() is atomic** - Single write per step, all data together
2. **get_state() is a fold** - Implementations may materialize snapshots/indexes for performance
3. **Handle serialization** - Outputs can be complex objects
4. **Initialize/close for resource management** - Connections, pools, etc.

---

## Serialization

### Default: JSON

By default, outputs are serialized as JSON. This works for most cases:

```python
checkpointer = SqliteCheckpointer("./workflows.db")
# Uses JsonSerializer by default
```

### Custom Serializers

For complex objects (numpy arrays, custom classes), provide a custom serializer:

```python
from hypergraph.checkpointers import Serializer
import pickle

class PickleSerializer(Serializer):
    def serialize(self, value: Any) -> bytes:
        return pickle.dumps(value)

    def deserialize(self, data: bytes) -> Any:
        return pickle.loads(data)

checkpointer = SqliteCheckpointer(
    path="./workflows.db",
    serializer=PickleSerializer(),
)
```

### Serializer Interface

```python
class Serializer(ABC):
    """Base class for output serialization."""

    @abstractmethod
    def serialize(self, value: Any) -> bytes:
        """Convert value to bytes for storage."""
        ...

    @abstractmethod
    def deserialize(self, data: bytes) -> Any:
        """Convert bytes back to value."""
        ...
```

---

## Usage with Runner

### Basic Usage

```python
from hypergraph import Graph, node, AsyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

@node(output_name="answer")
async def generate(query: str) -> str:
    return await llm.generate(query)

graph = Graph(nodes=[generate])
runner = AsyncRunner(checkpointer=SqliteCheckpointer("./workflows.db"))

# Run with workflow_id for persistence
result = await runner.run(
    graph,
    values={"query": "What is RAG?"},
    workflow_id="session-123",
)
```

### Execution Is Simple

The runner has one behavior: **load → merge → execute → append**.

```python
# First run
result = await runner.run(graph, values={...}, workflow_id="session-123")
# 💥 CRASH

# Resume - just run with same workflow_id
result = await runner.run(graph, values={...}, workflow_id="session-123")
# State loaded, merged with values, graph executes, steps appended

# Continue conversation - same pattern
result = await runner.run(graph, values={"user_input": "more"}, workflow_id="session-123")
# No special handling - just load, merge, execute, append
```

No state machine. No special cases. History is append-only.

See [Execution Semantics](persistence.md#execution-semantics) for full details.

---

## Nested Workflows

For nested graphs, workflow IDs use path convention:

```python
outer = Graph(nodes=[preprocess, rag.as_node(name="rag"), postprocess])

result = await runner.run(outer, values={...}, workflow_id="order-123")

# Workflow IDs:
# Parent: "order-123"
# Child:  "order-123/rag"
```

### Accessing Nested State

```python
# Get parent state
state = await checkpointer.get_state("order-123")

# Get nested graph state
rag_state = await checkpointer.get_state("order-123/rag")
```

---

## API Reference

### Types

> **Full definitions:** See [Execution Types](execution-types.md#persistence-types) for complete type definitions with docstrings.

```python
@dataclass
class CheckpointPolicy:
    """Controls checkpoint durability and retention."""
    durability: Literal["sync", "async", "exit"] = "async"
    retention: Literal["full", "latest", "windowed"] = "full"
    window: int | None = None
    ttl: timedelta | None = None

@dataclass(frozen=True)
class StepRecord:
    """Single atomic record - metadata + values together."""
    workflow_id: str                # Which workflow
    superstep: int                  # Which superstep (batch) - user-facing
    node_name: str                  # Name of the node that executed
    index: int                      # Unique sequential ID (internal, for DB key)
    status: StepStatus              # COMPLETED, FAILED, PAUSED, STOPPED
    input_versions: dict[str, int]  # Versions consumed (for staleness detection)
    values: dict[str, Any] | None   # Node output values
    error: str | None
    pause: PauseInfo | None         # Set when step is paused at InterruptNode
    partial: bool = False           # True if streaming output was truncated by stop
    created_at: datetime
    completed_at: datetime | None
    child_workflow_id: str | None   # For nested graphs

class StepStatus(Enum):
    COMPLETED = "completed"  # Finished with usable output
    FAILED = "failed"        # Terminated with error
    PAUSED = "paused"        # At InterruptNode, waiting for response
    STOPPED = "stopped"      # User stopped, no usable output

@dataclass
class Workflow:
    id: str
    status: WorkflowStatus
    steps: list[StepRecord]         # Unified metadata + values
    graph_hash: str | None          # For version mismatch detection
    created_at: datetime
    completed_at: datetime | None

class WorkflowStatus(Enum):
    ACTIVE = "active"        # Can be resumed (running, paused, or stopped)
    COMPLETED = "completed"  # Terminal success
    FAILED = "failed"        # Terminal failure

@dataclass
class Checkpoint:
    """A point-in-time snapshot for forking workflows."""
    values: dict[str, Any]      # Computed state at this point
    steps: list[StepRecord]     # Step history (the implicit cursor)
```

### Pause Persistence

When an `InterruptNode` executes and waits for a response, the step is saved with:
- `StepStatus.PAUSED` - indicates the step is blocked
- `StepRecord.pause` - contains `PauseInfo` with reason, node, response_param, and value

This enables external systems to query "what is this workflow waiting for?" even after a crash:

```python
# Get workflow to find paused step
workflow = await checkpointer.get_workflow("session-123")

for step in workflow.steps:
    if step.status == StepStatus.PAUSED and step.pause:
        print(f"Waiting for: {step.pause.response_param}")
        print(f"Value to show user: {step.pause.value}")
```

### DBOS Compatibility

These types map to DBOS tables for compatibility:

| hypergraph Type | DBOS Table |
|-----------------|------------|
| `Workflow` | `dbos.workflow_status` |
| `StepRecord` | `dbos.operation_outputs` |

For status mapping between hypergraph and DBOS, see [DBOS Mapping](execution-types.md#dbos-mapping).

---

## See Also

- [Persistence Tutorial](persistence.md) - How to use persistence
- [Durable Execution](durable-execution.md) - DBOS integration and advanced patterns
- [Execution Types](execution-types.md) - Type definitions
- [Observability](observability.md) - EventProcessor (separate from Checkpointer)
