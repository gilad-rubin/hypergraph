# Checkpointer

**The interface for workflow persistence and state management.**

Checkpointers store workflow state to enable resume, crash recovery, and multi-turn conversations. Unlike EventProcessor (which handles observability), Checkpointer is a bidirectional interface for reading and writing durable state.

---

## Overview

### Design Principles

1. **Steps are the source of truth** - State is computed from steps, not stored separately
2. **Bidirectional interface** - Both read (load) and write (save) operations
3. **Selective persistence** - Only `persist=True` outputs are stored
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
                  ├── get_history()    Read
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
    async def save_step(
        self,
        workflow_id: str,
        step: Step,
        result: StepResult,
    ) -> None:
        """
        Save a completed step and its outputs.

        Called by the runner after each node with persist=True completes.

        Args:
            workflow_id: Unique workflow identifier
            step: Step metadata (index, node_name, status)
            result: Step outputs (only persist=True values)
        """
        ...

    @abstractmethod
    async def create_workflow(
        self,
        workflow_id: str,
        initial_state: dict[str, Any] | None = None,
        history: list[Step] | None = None,
    ) -> Workflow:
        """
        Create a new workflow record.

        Args:
            workflow_id: Unique workflow identifier
            initial_state: Optional initial state values
            history: Optional step history (for forking)

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
        """Update workflow status (PENDING, SUCCESS, ERROR, etc.)."""
        ...

    # === Read Operations ===

    @abstractmethod
    async def get_state(
        self,
        workflow_id: str,
        at_step: int | None = None,
    ) -> dict[str, Any]:
        """
        Get accumulated state at a point in time.

        State is COMPUTED by folding over steps up to `at_step`.
        This is not a simple lookup - it reconstructs state from history.

        Args:
            workflow_id: Unique workflow identifier
            at_step: Step index to compute state at (None = latest)

        Returns:
            Accumulated output values: {"messages": [...], "answer": "..."}
        """
        ...

    @abstractmethod
    async def get_history(
        self,
        workflow_id: str,
        up_to_step: int | None = None,
    ) -> list[Step]:
        """
        Get step execution history.

        Args:
            workflow_id: Unique workflow identifier
            up_to_step: Maximum step index to include (None = all)

        Returns:
            List of Step records in execution order
        """
        ...

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
| **Data** | All events | Only `persist=True` outputs |
| **Purpose** | Observability | Durability |
| **Failure mode** | Fire-and-forget | Must succeed |

**The read path doesn't fit the event model.** When resuming a workflow, the runner needs to *query* for existing state. Events are write-only; they flow from runner to consumers.

**Configuration is different.** Streaming recovery modes, serialization, and storage backends are checkpointer concerns, not observability concerns.

See [Observability](observability.md) for EventProcessor details.

---

## State vs History

### Steps Are the Source of Truth

**State is computed from Steps, not stored separately.**

```
Steps (stored):
  Step 0: node="fetch",    outputs={"data": {...}}
  Step 1: node="process",  outputs={"result": {...}}
  Step 2: node="generate", outputs={"answer": "..."}

State (computed):
  get_state(at_step=2) → {"data": {...}, "result": {...}, "answer": "..."}
```

### Implications

- **Single source of truth**: Steps are authoritative; state is derived
- **Time travel**: Get state at any historical point
- **No sync issues**: State can never be "out of sync" with steps
- **Storage efficiency**: No duplicate state snapshots

### When to Use Each

| Operation | API | Use Case |
|-----------|-----|----------|
| Continue conversation | `get_state()` | Need accumulated values |
| Debug execution | `get_history()` | Need step-by-step trail |
| Fork workflow | Both | Need state + history up to a point |

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
| Resume from specific step | ✅ | ✅ | ✅ |
| Get current state | ✅ | ✅ | ✅ |
| List workflows | ✅ | ✅ | ✅ |
| Step history | ✅ | ✅ | ✅ |
| Automatic crash recovery | ❌ | ❌ | ✅ |
| Workflow forking | Manual | Manual | ✅ |
| Multi-server | ❌ | ✅ | ✅ |

---

## Implementing a Custom Checkpointer

### Minimal Implementation

```python
from hypergraph.checkpointers import Checkpointer
from hypergraph.types import Step, StepResult, Workflow, WorkflowStatus

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

    async def save_step(
        self,
        workflow_id: str,
        step: Step,
        result: StepResult,
    ) -> None:
        key = f"workflow:{workflow_id}:step:{step.index}"
        await self.client.hset(key, mapping={
            "node_name": step.node_name,
            "status": step.status.value,
            "outputs": json.dumps(result.outputs),
        })
        # Update step count
        await self.client.hset(f"workflow:{workflow_id}", "step_count", step.index + 1)

    async def get_state(
        self,
        workflow_id: str,
        at_step: int | None = None,
    ) -> dict[str, Any]:
        # Fold over steps to compute state
        history = await self.get_history(workflow_id, up_to_step=at_step)
        state = {}
        for step in history:
            result = await self._get_step_result(workflow_id, step.index)
            if result and result.outputs:
                state.update(result.outputs)
        return state

    async def get_history(
        self,
        workflow_id: str,
        up_to_step: int | None = None,
    ) -> list[Step]:
        # Implementation details...
        pass

    # ... other methods
```

### Key Implementation Notes

1. **get_state() must compute from steps** - Don't store state separately
2. **save_step() is called per-node** - Only for `persist=True` nodes
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
    inputs={"query": "What is RAG?"},
    workflow_id="session-123",
)
```

### Execution Is Simple

The runner has one behavior: **load → merge → execute → append**.

```python
# First run
result = await runner.run(graph, inputs={...}, workflow_id="session-123")
# 💥 CRASH

# Resume - just run with same workflow_id
result = await runner.run(graph, inputs={...}, workflow_id="session-123")
# State loaded, merged with inputs, graph executes, steps appended

# Continue conversation - same pattern
result = await runner.run(graph, inputs={"user_input": "more"}, workflow_id="session-123")
# No special handling - just load, merge, execute, append
```

No state machine. No special cases. History is append-only.

See [Execution Semantics](persistence.md#execution-semantics) for full details.

---

## Nested Workflows

For nested graphs, workflow IDs use path convention:

```python
outer = Graph(nodes=[preprocess, rag.as_node(name="rag"), postprocess])

result = await runner.run(outer, inputs={...}, workflow_id="order-123")

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

```python
@dataclass
class Step:
    index: int                      # Monotonically increasing step ID
    node_name: str                  # Name of the node that executed
    batch_index: int                # Which batch/superstep this belongs to
    status: StepStatus              # PENDING, RUNNING, COMPLETED, FAILED
    created_at: datetime
    completed_at: datetime | None
    child_workflow_id: str | None   # For nested graphs

@dataclass
class StepResult:
    step_index: int
    outputs: dict[str, Any] | None  # Only persist=True values
    error: str | None
    pause: PauseInfo | None         # Set when step is waiting at InterruptNode

class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING = "waiting"  # Paused at InterruptNode, waiting for response

@dataclass
class Workflow:
    id: str
    status: WorkflowStatus
    steps: list[Step]
    results: dict[int, StepResult]
    created_at: datetime
    completed_at: datetime | None

class WorkflowStatus(Enum):
    PENDING = "PENDING"
    ENQUEUED = "ENQUEUED"
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"
```

### Pause Persistence

When an `InterruptNode` executes and waits for a response, the step is saved with:
- `StepStatus.WAITING` - indicates the step is blocked
- `StepResult.pause` - contains `PauseInfo` with reason, node, response_param, and value

This enables external systems to query "what is this workflow waiting for?" even after a crash:

```python
# Get workflow to find waiting step
workflow = await checkpointer.get_workflow("session-123")

for step in workflow.steps:
    if step.status == StepStatus.WAITING:
        result = workflow.results.get(step.index)
        if result and result.pause:
            print(f"Waiting for: {result.pause.response_param}")
            print(f"Value to show user: {result.pause.value}")
```

### DBOS Compatibility

These types map to DBOS tables for compatibility:

| hypergraph Type | DBOS Table |
|-----------------|------------|
| `Workflow` | `dbos.workflow_status` |
| `Step` | `dbos.operation_outputs` |
| `StepResult` | `dbos.operation_outputs` |

---

## See Also

- [Persistence Tutorial](persistence.md) - How to use persistence
- [Durable Execution](durable-execution.md) - DBOS integration and advanced patterns
- [Execution Types](execution-types.md) - Type definitions
- [Observability](observability.md) - EventProcessor (separate from Checkpointer)
