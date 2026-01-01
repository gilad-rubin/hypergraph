# Execution & Runtime Types

**Reference for execution state, results, events, and observability types.**

These types represent the runtime layer of HyperNodes - what happens when graphs execute.

---

## Overview

### The Execution Model

When a graph runs, it progresses through three conceptual layers:

1. **Structure** (Graph + Nodes) - What to execute
2. **State** (GraphState) - What has been executed and current values (internal)
3. **Results** (RunResult + Events) - What was produced

```
Graph Definition  →  Runtime State  →  Results & Events
  (structure)         (internal)        (user-facing)
```

### Key Concepts

**Versioned State**: Every value has a version number that increments on update. This enables:
- Staleness detection (when to re-execute nodes)
- Cycle support (accumulators in loops)
- Checkpointing (pause/resume workflows)

**Event Streaming**: Runners emit events during execution for:
- Real-time UI updates (`NodeStartEvent`, `StreamingChunkEvent`)
- Observability/logging (`NodeEndEvent`, `RouteDecisionEvent`)
- Human-in-the-loop (`InterruptEvent`)

**Layered Architecture**: Events flow through pluggable layers:
- **UI Protocol** - WebSocket streaming to frontends
- **Observability** - Logging, tracing (Langfuse, Logfire)
- **Durability** - Checkpoint persistence (Redis, PostgreSQL, SQLite)

### Events vs Steps: Ephemeral vs Durable

HyperNodes separates two distinct record types:

| Concept | Events | Steps |
|---------|--------|-------|
| **Purpose** | Real-time observability | Durability and recovery |
| **Lifetime** | Ephemeral (in-memory) | Persisted (to database) |
| **Contains** | All execution details | Only `persist=True` outputs |
| **Consumers** | EventProcessor, `.iter()` | Checkpointer |

**Events** are emitted during execution for real-time streaming and observability. They include `NodeStartEvent`, `StreamingChunkEvent`, etc. Events are consumed by `EventProcessor` implementations or via `.iter()` for pull-based access. **Events are NOT persisted by default.**

**Steps** are persisted records saved by the checkpointer. Each step contains the node outputs (for `persist=True` nodes only). Steps enable crash recovery, resume, and workflow forking.

```
During Execution:
  Runner emits Events → EventProcessor (observability)
                     → .iter() (real-time UI)

After Node Completion:
  Runner saves Step  → Checkpointer (durability)
                     → Only persist=True outputs
```

**Key insight:** When a node produces output, the value exists once in memory. Events reference this value (in memory) for observability. The checkpointer serializes and stores a copy (only if `persist=True`) for durability. They are separate concerns with separate interfaces.

---

## Quick Navigation

| Type | Purpose | Usage |
|------|---------|-------|
| [Status Enums](#status-enums) | Execution state values | RunStatus, PauseReason |
| [GraphState](#graphstate) | Runtime value storage | Internal to runners (not user-facing) |
| [PauseInfo](#pauseinfo) | Pause details | Nested in `RunResult.pause` |
| [RunResult](#runresult) | Execution result | Returned by `runner.run()`, supports nesting |
| [RunHandle](#runhandle) | Streaming execution handle | Returned by `AsyncRunner.iter()` |
| [Event Types](#event-types) | Streaming events | Yielded during iteration |
| [Persistence Types](#persistence-types) | Checkpoint storage | Workflow, Step, StepResult |

**See also:**
- [Node Types](node-types.md) - Building blocks (includes InterruptNode)
- [Graph Types](graph.md) - Structure and composition
- [Runners API](runners.md) - Execution guide
- [Durable Execution](durable-execution.md) - Checkpointing and DBOS integration

---

## Three-Layer Architecture

HyperNodes uses a **unified event stream** that flows through pluggable layers. The core execution engine produces events; layers consume what they need.

```
                    ┌─────────────────────────┐
                    │   Core Execution        │
                    │   (Runners)             │
                    └───────────┬─────────────┘
                                │ Events (with span hierarchy)
                    ┌───────────▼─────────────┐
                    │   Event Stream          │
                    └─┬──────────┬──────────┬─┘
                      │          │          │
        ┌─────────────▼──┐  ┌────▼─────┐  ┌▼────────────┐
        │  UI Protocol   │  │ Event    │  │ Durability  │
        │  (WebSocket)   │  │Processors│  │ (Checkpoint)│
        └────────────────┘  └──────────┘  └─────────────┘
```

### Layer Details

| Layer | Purpose | Example | Protocol |
| --- | --- | --- | --- |
| **UI Protocol** | Real-time streaming to frontends | AG-UI compatible streaming | Events → WebSocket |
| **Observability** | Logging, tracing, analytics | Langfuse, Logfire integration | Events → EventProcessor |
| **Durability** | Checkpoint persistence | Redis, PostgreSQL, SQLite | Checkpointer interface |

**Key principle:** Layers consume a unified event stream. The core produces events; layers subscribe to what they need. See [Observability](observability.md) for the `EventProcessor` interface and integration patterns.

### Event Flow Example

```python
# Runner produces events via .iter()
async for event in runner.iter(graph, inputs={...}, workflow_id="session-123"):
    # UI layer consumes streaming chunks
    if isinstance(event, StreamingChunkEvent):
        await websocket.send(event.chunk)

    # Observability layer logs all events
    if hasattr(event, 'node_name'):
        logger.info(f"{event.node_name}: {getattr(event, 'duration_ms', 'N/A')}ms")

    # Handle interrupts (checkpointer saves state automatically)
    if isinstance(event, InterruptEvent):
        await notify_user(event.workflow_id, event.value)

# Or use EventProcessor for push-based observability
runner = AsyncRunner(event_processors=[LangfuseProcessor()])
result = await runner.run(graph, inputs={...}, workflow_id="session-123")
```

---

## Status Enums

### RunStatus

**HyperNodes execution status.** Returned in `RunResult.status`.

```python
from enum import Enum

class RunStatus(Enum):
    """Workflow execution status (HyperNodes layer)."""
    COMPLETED = "completed"  # Finished all steps (or routed to END)
    PAUSED = "paused"        # Waiting for something (see PauseReason)
    ERROR = "error"          # Failed with exception
```

### PauseReason

**Why a workflow is paused.** Only set when `status == PAUSED`.

```python
class PauseReason(Enum):
    """Why a workflow is paused (when status is PAUSED)."""
    HUMAN_INPUT = "human_input"  # InterruptNode waiting for response
    SLEEP = "sleep"              # Waiting for duration (future)
    SCHEDULED = "scheduled"      # Waiting for specific time (future)
    EVENT = "event"              # Waiting for external event (future)
```

### Status Semantics

| Status | Meaning | Typical Cause | Resume Action |
|--------|---------|---------------|---------------|
| `COMPLETED` | Finished | Normal completion or routed to `END` | None needed |
| `PAUSED` | Waiting | `InterruptNode`, sleep, scheduled wait | Depends on `pause_reason` |
| `ERROR` | Failed | Uncaught exception | Fix and retry |

### Pause Reasons

| Reason | Meaning | Resume Action |
|--------|---------|---------------|
| `HUMAN_INPUT` | Waiting for human decision | Provide response via `resume()` or `DBOS.send()` |
| `SLEEP` | Waiting for duration | Automatic (timer) |
| `SCHEDULED` | Waiting for specific time | Automatic (scheduler) |
| `EVENT` | Waiting for external event | Send event via messaging |

### DBOS Mapping

HyperNodes status maps to DBOS status as follows:

```
┌─────────────────────────────────────────────────────────────┐
│  HyperNodes RunStatus        →    DBOS WorkflowStatus       │
├─────────────────────────────────────────────────────────────┤
│  COMPLETED                   →    SUCCESS                   │
│  PAUSED (any reason)         →    PENDING (blocked on recv) │
│  ERROR                       →    ERROR                     │
└─────────────────────────────────────────────────────────────┘
```

**Key insight:** DBOS has no "PAUSED" status. When a workflow calls `DBOS.recv()` and waits for human input, DBOS still reports it as `PENDING`. HyperNodes adds the `PAUSED` + `PauseReason` abstraction on top.

---

## GraphState

### Purpose

**Runtime storage for value versions and execution history.** Used internally by runners to track what's been computed and when to re-execute nodes. **This is not user-facing.**

### Important: GraphState Holds ALL Values

`GraphState.values` contains **all outputs from all executed nodes**, including those with `persist=False`. The `persist` flag only controls what gets saved to the checkpointer:

```
GraphState.values = {"embedding": [...], "answer": "..."}  # ALL values at runtime
                      ↓
                   persist filter
                      ↓
Checkpointer saves = {"answer": "..."}  # Only persist=True values
```

This means:
- During execution, all values are available for downstream nodes
- On crash recovery, `persist=False` values are re-computed (nodes re-execute)
- `persist=True` values are loaded from checkpoint (nodes skipped)

### Class Definition

```python
@dataclass
class GraphState:
    """Runtime value storage with versioning. Internal to runners."""

    values: dict[str, Any]
    """Current values by name. Includes ALL outputs (persist=True and persist=False)."""

    versions: dict[str, int]
    """Version number for each value (increments on update)."""

    node_executions: dict[str, NodeExecution]
    """Last execution record per node."""

    history: list[NodeExecution]
    """Chronological execution history."""
```

### Methods (Internal)

```python
def get(self, name: str, default=None) -> Any:
    """Get value by name."""

def set(self, name: str, value: Any) -> GraphState:
    """Set value and increment version (returns new state)."""

def is_stale(self, node: HyperNode) -> bool:
    """Check if node needs to re-execute based on input versions."""
```

**Note:** Users don't interact with `GraphState` directly. Use `RunResult.outputs` to access execution results.

---

## PauseInfo

### Purpose

**Information about why a workflow is paused.** Only present when `RunResult.status == PAUSED`. Groups all pause-related fields into a single object.

### Class Definition

```python
@dataclass
class PauseInfo:
    """Details about a paused workflow."""

    reason: PauseReason
    """Why we're paused (HUMAN_INPUT, SLEEP, SCHEDULED, EVENT)."""

    node: str
    """Name of the node that caused the pause."""

    response_param: str
    """Parameter name to use when resuming (the key for inputs dict)."""

    value: Any
    """Value to show user (e.g., the draft for approval)."""

    resume_at: datetime | None = None
    """When to resume (for SCHEDULED pause reason)."""
```

---

## RunResult

### Purpose

**The primary result type from graph execution.** Returned by `runner.run()`. Contains outputs, status, pause information, and supports nesting for composed graphs.

### Class Definition

```python
@dataclass
class RunResult:
    """Result from graph execution."""

    outputs: dict[str, Any | "RunResult"]
    """Output values. For nested graphs, contains nested RunResult objects."""

    status: RunStatus
    """Execution status: COMPLETED, PAUSED, or ERROR."""

    workflow_id: str | None
    """Workflow identifier (required with checkpointer, None otherwise)."""

    run_id: str
    """Unique identifier for this execution."""

    pause: PauseInfo | None = None
    """Pause details (only set when status == PAUSED)."""

    # === Dict-like access for convenience ===

    def __getitem__(self, key: str) -> Any | "RunResult":
        """Dict-like access: result['answer'] or result['nested_graph']['value']"""
        return self.outputs[key]

    def __contains__(self, key: str) -> bool:
        """Check if output exists: 'answer' in result"""
        return key in self.outputs

    def keys(self):
        """Get output names."""
        return self.outputs.keys()

    def items(self):
        """Get output name-value pairs."""
        return self.outputs.items()

    @property
    def paused(self) -> bool:
        """True if workflow is paused."""
        return self.pause is not None
```

### Key Design Decisions

- **Nested `RunResult` for nested graphs** - When a graph contains `GraphNode`s, their results are nested `RunResult` objects, preserving status and pause info per subgraph.
- **No `checkpoint: bytes`** - The checkpointer manages state internally by `workflow_id`. You don't pass checkpoint bytes around.
- **`workflow_id` for resume** - Use `workflow_id` + `resume=True` to continue from where you left off.
- **Dict-like access** - `result["answer"]` is equivalent to `result.outputs["answer"]`.
- **All outputs by default** - `outputs` contains all node outputs unless filtered with `select=`.

### Basic Example

```python
from hypernodes import AsyncRunner
from hypernodes.checkpointers import SqliteCheckpointer

runner = AsyncRunner(checkpointer=SqliteCheckpointer("./dev.db"))
result = await runner.run(
    graph,
    inputs={"query": "hello"},
    workflow_id="session-123",
)

# Dict-like access
print(result["answer"])           # Same as result.outputs["answer"]
print("answer" in result)         # True

# Check status
if result.status == RunStatus.COMPLETED:
    print("Done!")
```

### Nested Graph Results

When a graph contains nested graphs (via `GraphNode`), each nested graph's result is a nested `RunResult`:

```python
# Define nested structure
rag_pipeline = Graph(nodes=[embed, retrieve, generate], name="rag")
review_pipeline = Graph(nodes=[draft, approval_interrupt, finalize], name="review")

outer = Graph(nodes=[
    preprocess,
    rag_pipeline.as_node(),
    review_pipeline.as_node(),
    postprocess,
])

result = await runner.run(outer, inputs={...}, workflow_id="order-123")

# Access nested graph results
result["final_output"]                    # Top-level output
result["rag"]                             # RunResult for rag_pipeline
result["rag"]["embedding"]                # Output from nested graph
result["rag"].status                      # RunStatus.COMPLETED
result["rag"].workflow_id                 # "order-123/rag"

result["review"]                          # RunResult for review_pipeline
result["review"].status                   # Could be PAUSED if interrupt hit
result["review"]["draft"]                 # Output from nested graph
```

### Nested Graph Pauses

When a nested graph contains an `InterruptNode` and pauses, the pause propagates up:

```python
result = await runner.run(outer, inputs={...}, workflow_id="order-123")

# Check overall status
if result.status == RunStatus.PAUSED:
    # Find which nested graph paused
    if result["rag"].status == RunStatus.COMPLETED:
        print("RAG completed")

    if result["review"].status == RunStatus.PAUSED:
        print(f"Review paused at: {result['review'].pause.node}")
        print(f"Value to show user: {result['review'].pause.value}")
        print(f"Nested workflow ID: {result['review'].workflow_id}")  # "order-123/review"

# The top-level pause info points to the nested interrupt
print(result.pause.node)  # "review/approval" (path to the interrupt)
```

**Pause propagation rules:**
- If any nested graph pauses, the parent graph pauses
- `result.pause` contains info about the first pause encountered
- **First pause wins** - if multiple nested graphs could pause in parallel, execution stops at the first one

### Resume Nested Graph

To resume a paused nested graph:

```python
# Option 1: Resume the outer graph (checkpointer handles nesting)
result = await runner.run(
    outer,
    inputs={result.pause.response_param: user_response},
    workflow_id="order-123",
    resume=True,
)

# Option 2: Resume the nested graph directly (advanced)
result = await runner.run(
    review_pipeline,
    inputs={"decision": user_response},
    workflow_id="order-123/review",  # Nested workflow ID
    resume=True,
)
```

### With DBOS

When using `DBOSAsyncRunner`, resume happens via `DBOS.send()` from an external system:

```python
from hypernodes.runners import DBOSAsyncRunner

runner = DBOSAsyncRunner()
result = await runner.run(
    graph,
    inputs={"prompt": "Write a poem"},
    workflow_id="poem-456",
)

if result.pause:
    # Workflow is now waiting on DBOS.recv()
    # External system sends response via DBOS.send()
    # No runner.run() call needed - workflow auto-resumes
    pass
```

```python
# In webhook or external process:
from dbos import DBOS

DBOS.send(
    destination_id="poem-456",
    message={"decision": "approve"},
    topic="approval",  # InterruptNode name
)
```

---

## RunHandle

### Purpose

**Streaming execution handle returned by `AsyncRunner.iter()`.** Provides async iteration over events, interrupt response capability, and access to the final result.

### Class Definition

```python
class RunHandle:
    """Handle for streaming graph execution with interrupt support.

    Returned by AsyncRunner.iter() context manager.
    """

    async def __aiter__(self) -> AsyncIterator[Event]:
        """Iterate over events as they occur."""
        ...

    def respond(self, param: str, value: Any) -> None:
        """
        Provide a response for an interrupt.

        Args:
            param: The response parameter name (from InterruptEvent.response_param)
            value: The response value

        Must be called after receiving InterruptEvent before continuing iteration.
        Raises RuntimeError if called when no interrupt is pending.
        """
        ...

    @property
    def result(self) -> RunResult:
        """
        Final result after iteration completes.

        Raises:
            RuntimeError: If accessed before iteration completes.
        """
        ...
```

### Example

```python
async with runner.iter(graph, inputs={"query": "hello"}, workflow_id="session-123") as run:
    async for event in run:
        match event:
            case StreamingChunkEvent(chunk=chunk):
                print(chunk, end="")

            case InterruptEvent(value=prompt, response_param=target):
                # Handle interrupt inline
                response = await get_user_response(prompt)
                run.respond(target, response)
                # Iteration continues automatically

            case NodeEndEvent(node_name=name):
                print(f"\nCompleted: {name}")

    # Access final result after iteration
    final_result = run.result
    print(f"Status: {final_result.status}")
```

---

## Event Types

Events are emitted by `AsyncRunner.iter()` for real-time observability and UI updates. Events can also be consumed via `EventProcessor` for push-based integrations.

All events include **span hierarchy fields** for nested graph support:

```python
# Common fields on all events
run_id: str              # Unique per .run() invocation
span_id: str             # Unique per node execution
parent_span_id: str | None  # Links to parent span (None for root nodes)
timestamp: float         # Unix timestamp
```

The `span_id` → `parent_span_id` relationship forms a tree, enabling observability tools to visualize nested graph execution. See [Observability](observability.md) for details.

### NodeStartEvent

```python
@dataclass
class NodeStartEvent:
    run_id: str
    span_id: str
    parent_span_id: str | None
    node_name: str
    inputs: dict[str, Any]
    timestamp: float
```

### NodeEndEvent

```python
@dataclass
class NodeEndEvent:
    run_id: str
    span_id: str
    parent_span_id: str | None
    node_name: str
    outputs: Any
    duration_ms: float
    cached: bool
    timestamp: float
```

### StreamingChunkEvent

```python
@dataclass
class StreamingChunkEvent:
    run_id: str
    span_id: str
    parent_span_id: str | None
    node_name: str
    chunk: str | Any
    chunk_index: int
    timestamp: float
```

### InterruptEvent

```python
@dataclass
class InterruptEvent:
    run_id: str
    span_id: str
    parent_span_id: str | None
    workflow_id: str        # Use this to resume via checkpointer or DBOS.send()
    interrupt_name: str
    value: Any              # Value to show user
    response_param: str     # Where to write response
    timestamp: float
```

**Note:** `InterruptEvent` does not include `checkpoint: bytes`. The checkpointer manages state internally - use `workflow_id` to resume.

### RouteDecisionEvent

```python
@dataclass
class RouteDecisionEvent:
    run_id: str
    span_id: str
    parent_span_id: str | None
    gate_name: str
    decision: str  # Target node name or "END"
    timestamp: float
```

### NodeErrorEvent

```python
@dataclass
class NodeErrorEvent:
    run_id: str
    span_id: str
    parent_span_id: str | None
    node_name: str
    error: Exception        # The exception that was raised
    error_type: str         # Class name, e.g., "ValueError"
    timestamp: float
```

**Note:** After `NodeErrorEvent`, execution may continue (if error is handled) or terminate. `RunEndEvent` is still emitted with error status. Processors' `shutdown()` is always called.

### Example

```python
async with runner.iter(graph, inputs={...}) as run:
    async for event in run:
        match event:
            case NodeStartEvent(node_name=name):
                print(f"Starting: {name}")

            case NodeEndEvent(node_name=name, duration_ms=ms):
                print(f"Finished: {name} in {ms}ms")

            case StreamingChunkEvent(chunk=chunk):
                print(chunk, end="")

            case InterruptEvent(interrupt_name=name, value=prompt):
                print(f"Paused at: {name}")
                # Handle interrupt
```

---

## Interrupt Handling with AsyncRunner

**AsyncRunner supports interrupts in `.run()` and `.iter()`, but NOT in `.map()`.**

### Using `.run()` - Pause and Resume

```python
from hypernodes import AsyncRunner
from hypernodes.checkpointers import SqliteCheckpointer

runner = AsyncRunner(checkpointer=SqliteCheckpointer("./dev.db"))

result = await runner.run(
    graph,
    inputs={"query": "hello"},
    workflow_id="session-123",
)

if result.pause:
    # Execution paused at InterruptNode
    prompt = result.pause.value

    # Get user response (your application logic)
    response = await get_user_response(prompt)

    # Resume using workflow_id (checkpointer loads state internally)
    result = await runner.run(
        graph,
        inputs={result.pause.response_param: response},
        workflow_id="session-123",
        resume=True,
    )

# Now complete
assert not result.pause
print(result.outputs["answer"])
```

### Using `.iter()` - Handle Inline

```python
async with runner.iter(graph, inputs={"query": "hello"}, workflow_id="session-123") as run:
    async for event in run:
        match event:
            case StreamingChunkEvent(chunk=chunk):
                print(chunk, end="")

            case InterruptEvent(value=prompt, response_param=target, workflow_id=wf_id):
                # Handle interrupt inline
                response = await get_user_response(prompt)
                run.respond(target, response)
                # Iteration continues automatically

            case NodeEndEvent(node_name=name):
                print(f"Completed: {name}")

    # After iteration, result is available
    print(run.result.outputs)
```

### Using `.run()` with Handlers

Pass handlers per-call for automatic interrupt resolution:

```python
result = await runner.run(
    graph,
    inputs={"query": "hello"},
    interrupt_handlers={
        "approval": handle_approval,
        "topic_selection": handle_topic,
    },
)

# If all interrupts have handlers → runs to completion
# If some handlers missing → returns interrupted at first unhandled
```

Handler signature:

```python
async def handle_approval(prompt: ApprovalPrompt) -> ApprovalResponse:
    """
    Receives: The value from InterruptNode's input_param
    Returns: The value to write to InterruptNode's response_param
    """
    choice = await show_dialog(prompt.message, prompt.options)
    return ApprovalResponse(choice=choice)
```

### `.map()` Does Not Support Interrupts

Batch processing with `.map()` cannot handle interrupts:

```python
# This will raise an error at validation time
if graph.has_interrupts:
    raise IncompatibleRunnerError(
        "Graph has interrupts but .map() doesn't support them.\n"
        "Use .run() or .iter() for graphs with interrupts."
    )
```

Rationale: Each batch item would potentially pause at different points, making the execution model complex and confusing. Use `.run()` in a loop if you need batch processing with interrupts.

---

## Common Patterns

### Working with Nested Results

```python
def extract_all_values(result: RunResult, prefix="") -> dict[str, Any]:
    """Recursively extract all values from nested results."""
    flat = {}

    for key, value in result.items():
        full_key = f"{prefix}{key}" if prefix else key

        if isinstance(value, RunResult):
            # Recurse into nested result
            nested = extract_all_values(value, f"{full_key}/")
            flat.update(nested)
        else:
            flat[full_key] = value

    return flat

# Usage
result = await runner.run(outer_graph, inputs={...})
all_values = extract_all_values(result)
# {"answer": "...", "rag/embedding": [...], "rag/docs": [...]}
```

### Finding Paused Nested Graphs

```python
def find_paused_graphs(result: RunResult, path="") -> list[tuple[str, RunResult]]:
    """Find all paused graphs in a nested result."""
    paused = []

    if result.status == RunStatus.PAUSED:
        paused.append((path or "root", result))

    for key, value in result.items():
        if isinstance(value, RunResult):
            nested_path = f"{path}/{key}" if path else key
            paused.extend(find_paused_graphs(value, nested_path))

    return paused

# Usage
result = await runner.run(outer_graph, inputs={...})
for path, paused_result in find_paused_graphs(result):
    print(f"Paused at: {path}")
    print(f"  Waiting for: {paused_result.pause.response_param}")
    print(f"  Value: {paused_result.pause.value}")
```

### Event Filtering and Routing

```python
async def process_events(graph: Graph, workflow_id: str):
    """Route events to different handlers based on type."""
    async with runner.iter(graph, inputs={...}, workflow_id=workflow_id) as run:
        async for event in run:
            match event:
                case StreamingChunkEvent():
                    await ui_layer.handle_chunk(event)

                case NodeEndEvent():
                    await observability_layer.log_execution(event)

                case InterruptEvent():
                    # Checkpointer saves state automatically
                    # Just notify UI to prompt user
                    await ui_layer.prompt_user(event)
```

### Resume via Workflow ID

The checkpointer manages state internally - you resume by `workflow_id`, not by passing checkpoint bytes:

```python
# When interrupt occurs, store the workflow_id (not checkpoint bytes)
async def handle_interrupt(event: InterruptEvent):
    await db.execute(
        "INSERT INTO pending_approvals (workflow_id, interrupt_name, value) VALUES (?, ?, ?)",
        (event.workflow_id, event.interrupt_name, serialize(event.value))
    )

# Resume from stored workflow_id
async def resume_execution(workflow_id: str, user_response: Any):
    pending = await db.fetch_one(
        "SELECT interrupt_name FROM pending_approvals WHERE workflow_id = ?",
        (workflow_id,)
    )

    result = await runner.run(
        graph,
        inputs={pending["interrupt_name"]: user_response},
        workflow_id=workflow_id,
        resume=True,  # Checkpointer loads state internally
    )
    return result
```

### Multi-Layer Event Consumer

```python
class EventRouter:
    """Route events to multiple layers simultaneously."""

    def __init__(self):
        self.ui_layer = WebSocketLayer()
        self.observability_layer = LogfuseLayer()
        self.durability_layer = PostgresCheckpointer()

    async def consume(self, graph: Graph, inputs: dict):
        async with runner.iter(graph, inputs=inputs) as run:
            async for event in run:
                # All layers receive all events - they filter what they need
                await asyncio.gather(
                    self.ui_layer.handle(event),
                    self.observability_layer.handle(event),
                    self.durability_layer.handle(event),
                )

        return run.result
```

---

## Persistence Types

These types represent how workflow state is stored in checkpointers and DBOS. They map directly to database tables.

### StepStatus

```python
class StepStatus(Enum):
    """Execution status of a single step."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
```

### Step

```python
@dataclass
class Step:
    """A single step in a workflow.

    Maps to DBOS `operation_outputs` table.
    """
    index: int
    """Monotonically increasing step ID (maps to DBOS function_id)."""

    node_name: str
    """Name of the node that executed."""

    batch_index: int
    """Which batch/superstep this step belongs to."""

    status: StepStatus = StepStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None

    # Added when nested graphs are implemented
    child_workflow_id: str | None = None
    """For GraphNode steps, the nested workflow ID (e.g., 'order-123/rag')."""
```

### StepResult

```python
@dataclass
class StepResult:
    """Step outputs. Stored separately (can be large)."""
    step_index: int
    outputs: dict[str, Any] | None = None
    error: str | None = None
```

### WorkflowStatus (DBOS-compatible)

```python
class WorkflowStatus(Enum):
    """DBOS-compatible workflow status values.

    These match DBOS exactly for storage compatibility.
    Use RunStatus for HyperNodes API layer.
    """
    PENDING = "PENDING"       # Running or waiting (includes recv() blocked)
    ENQUEUED = "ENQUEUED"     # In queue, not started
    SUCCESS = "SUCCESS"       # Completed successfully
    ERROR = "ERROR"           # Failed with exception
    CANCELLED = "CANCELLED"   # Manually cancelled or timeout
```

### Workflow

```python
@dataclass
class Workflow:
    """A workflow execution with its steps.

    Maps to DBOS `workflow_status` table.
    """
    id: str
    """Unique workflow identifier."""

    status: WorkflowStatus = WorkflowStatus.PENDING
    steps: list[Step] = field(default_factory=list)
    results: dict[int, StepResult] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None
```

### DBOS Table Mapping

| HyperNodes Type | DBOS Table | Key Columns |
|-----------------|------------|-------------|
| `Workflow` | `dbos.workflow_status` | `workflow_uuid`, `status`, `created_at` |
| `Step` | `dbos.operation_outputs` | `workflow_uuid`, `function_id`, `function_name` |
| `StepResult` | `dbos.operation_outputs` | `output`, `error`, `child_workflow_id` |

### Workflow ID Convention

For nested graphs, workflow IDs use path convention:

```python
def child_workflow_id(parent_id: str, node_name: str) -> str:
    """Derive child workflow ID from parent."""
    return f"{parent_id}/{node_name}"

def parent_workflow_id(workflow_id: str) -> str | None:
    """Extract parent ID from path, or None if top-level."""
    if "/" not in workflow_id:
        return None
    return workflow_id.rsplit("/", 1)[0]
```

**Examples:**
```
Parent: "order-123"
Child:  "order-123/rag"
Deeply nested: "order-123/rag/inner"
```

---

## Type Hierarchy

```
User-Facing Types:
├── RunResult (primary result type, supports nesting)
│   ├── outputs: dict[str, Any | RunResult]  ← nested graphs are RunResult
│   ├── status: RunStatus
│   ├── pause: PauseInfo | None
│   └── workflow_id, run_id
├── RunStatus (enum: COMPLETED, PAUSED, ERROR)
├── PauseReason (enum: HUMAN_INPUT, SLEEP, SCHEDULED, EVENT)
├── PauseInfo (pause details: reason, node, value, response_param)
└── RunHandle (streaming execution handle from .iter())

Internal Types (not user-facing):
└── GraphState (runtime values with versioning, includes persist=False values)

Persistence Types:
├── StepStatus (enum: PENDING, RUNNING, COMPLETED, FAILED)
├── WorkflowStatus (enum: PENDING, ENQUEUED, SUCCESS, ERROR, CANCELLED)
├── Step (individual step record)
├── StepResult (step outputs, only persist=True values)
└── Workflow (workflow execution record)

Event Hierarchy (all include span_id, parent_span_id for hierarchy):
├── RunStartEvent
├── RunEndEvent
├── NodeStartEvent
├── NodeEndEvent
├── NodeErrorEvent
├── StreamingChunkEvent
├── CacheHitEvent
├── InterruptEvent
└── RouteDecisionEvent

Observability:
├── EventProcessor (base interface)
└── TypedEventProcessor (convenience class with typed methods)
```

### Type Layer Separation

```
┌─────────────────────────────────────────────────────────────────┐
│  HyperNodes API Layer (what users see)                          │
│                                                                 │
│  RunResult                                                      │
│  ├── outputs: all values (or filtered by select=)              │
│  ├── status: RunStatus (COMPLETED, PAUSED, ERROR)              │
│  ├── pause: PauseInfo | None                                   │
│  └── [nested_graph]: RunResult (for nested graphs)             │
│                                                                 │
│  Dict-like access: result["answer"], result["rag"]["docs"]     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ Runners translate between layers
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Internal Runtime (GraphState)                                  │
│                                                                 │
│  All values including persist=False                             │
│  Version tracking for staleness detection                       │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ Checkpointer filters by persist=
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Persistence Layer (Workflow, Step, StepResult)                 │
│                                                                 │
│  Only persist=True values are saved                             │
│  DBOS-compatible format                                         │
│  Checkpoint ID = workflow_id + step_index                       │
└─────────────────────────────────────────────────────────────────┘
```

**See also:**
- [Checkpointer API](checkpointer.md) - Full interface definition and custom implementations
- [Durable Execution](durable-execution.md) - DBOS integration and advanced durability patterns
- [Observability](observability.md) - EventProcessor interface and integration patterns
- [Node Types](node-types.md#type-hierarchy-summary) - Complete node hierarchy
- [Graph Types](graph.md#runner-compatibility) - Runner compatibility matrix
