# Checkpointer

**The interface for workflow persistence and state management.**

Checkpointers store workflow state to enable resume, crash recovery, and multi-turn conversations. Unlike EventProcessor (which handles observability), Checkpointer is a bidirectional interface for reading and writing durable state.

---

## Overview

### Design Principles

1. **Steps are the source of truth** - State is computed from steps, not stored separately
2. **Bidirectional interface** - Both read (load) and write (save) operations
3. **Full persistence** - All outputs are stored when checkpointer is present
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
    """

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
result = runner.run(graph, inputs={"data": big_file})
# 💥 CRASH at node 5

# Restart with same inputs — nodes 1-4 cache hit, only 5+ execute
result = runner.run(graph, inputs={"data": big_file})
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

`get_state()` is defined as “fold StepRecords”, but long-lived workflows (chat threads, ETL, schedulers) cannot afford O(n) replay on every resume/inspection.

**Design rule:**
- **Steps remain the source of truth** (correctness, time travel).
- **Checkpointers SHOULD materialize derived state** to make the common path fast.

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
from hypergraph.checkpointers import SqliteCheckpointer

checkpointer = SqliteCheckpointer(
    path="./workflows.db",      # Database file path
    # serializer=JsonSerializer(),  # Optional custom serializer
)

runner = AsyncRunner(checkpointer=checkpointer)
```

**Best for:** Local development, single-server deployments, simple production.

### PostgresCheckpointer

```python
from hypergraph.checkpointers import PostgresCheckpointer

checkpointer = PostgresCheckpointer(
    connection_string="postgresql://user:pass@host/db",
    # pool_size=10,              # Connection pool size
    # serializer=JsonSerializer(),  # Optional custom serializer
)

runner = AsyncRunner(checkpointer=checkpointer)
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
