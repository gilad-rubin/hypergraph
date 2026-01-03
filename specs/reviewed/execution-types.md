# Execution & Runtime Types

**Reference for execution state, results, events, and observability types.**

These types represent the runtime layer of hypergraph - what happens when graphs execute.

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

hypergraph separates two distinct record types:

| Concept | Events | Steps |
|---------|--------|-------|
| **Purpose** | Real-time observability | Durability and recovery |
| **Lifetime** | Ephemeral (in-memory) | Persisted (to database) |
| **Contains** | All execution details | All node outputs |
| **Consumers** | EventProcessor, `.iter()` | Checkpointer |

**Events** are emitted during execution for real-time streaming and observability. They include `NodeStartEvent`, `StreamingChunkEvent`, etc. Events are consumed by `EventProcessor` implementations or via `.iter()` for pull-based access. **Events are NOT persisted by default.**

**Steps** are persisted records saved by the checkpointer. Each step contains the node outputs. Steps enable crash recovery, resume, and workflow forking.

```
During Execution:
  Runner emits Events → EventProcessor (observability)
                     → .iter() (real-time UI)

After Node Completion:
  Runner saves Step  → Checkpointer (durability)
                     → All outputs saved
```

**Key insight:** When a node produces output, the value exists once in memory. Events reference this value (in memory) for observability. The checkpointer serializes and stores a copy for durability. They are separate concerns with separate interfaces.

### Step History as Implicit Cursor

Unlike sequential workflow systems (DBOS, Temporal) that track an explicit program counter, hypergraph uses **step history as an implicit cursor**. The combination of outputs + completed steps determines what runs next.

**Why outputs alone aren't sufficient:**

| Graph Type | Outputs Only? | Why Not? |
|------------|:-------------:|----------|
| DAG with unique outputs | ✅ | Output existence = node completed |
| Cycles | ❌ | Need iteration count (step index) |
| Branches with shared intermediates | ❌ | Need to know which branch was taken |

**Example 1: Cycles need iteration count**

```
generate(messages) → accumulate(messages, response) → check_done → generate
```

If checkpoint contains `{"messages": [...], "response": "..."}`:

- **Scenario A:** Crashed after `generate`, before `accumulate`
  - `messages` = [user message]
  - `response` = "answer" (fresh, needs to be accumulated)

- **Scenario B:** Crashed after `accumulate`, before `check_done`
  - `messages` = [user message, assistant response]
  - `response` = "answer" (stale, already in messages)

With just outputs, we can't distinguish A from B. In A, we should run `accumulate`. In B, we should run `check_done`. **Step history tells us which node last completed.**

**Example 2: Branches with shared intermediate outputs**

```python
@route(targets=["branch_a", "branch_b"])
def router(data: str) -> str: ...

# Branch A
@node(output_name="processed")
def process_a(data: str) -> str: ...

@node(output_name="result")
def finalize_a(processed: str) -> str: ...

# Branch B
@node(output_name="processed")
def process_b(data: str) -> str: ...

@node(output_name="result")
def finalize_b(processed: str) -> str: ...
```

If we crash after `process_a` with `outputs = {"processed": "..."}`:

- `finalize_a` needs `processed` → exists ✓ → can run
- `finalize_b` needs `processed` → exists ✓ → can run

**Both finalize nodes appear runnable!** Without step history, we don't know we're "in" branch A. Step history shows `process_a` completed (not `process_b`), disambiguating which finalize should run.

> **Note:** hypergraph does NOT require unique intermediate output names in branches. Instead, step history disambiguates. This is more flexible for users.

**The resume algorithm:**

```python
def get_runnable_nodes(graph, available_outputs, completed_steps):
    return [
        node for node in graph.nodes
        if all(input in available_outputs for input in node.inputs)  # Can run
        and node.name not in completed_steps  # Hasn't run yet
    ]
```

This is the **same algorithm** used for fresh starts and resumes - only the initial state differs:

| Scenario | `available_outputs` | `completed_steps` |
|----------|---------------------|-------------------|
| Fresh start | `inputs` dict | `{}` empty |
| Resume | Checkpoint outputs | Step records with `status=COMPLETED` |

**The implicit cursor is:** `(available_outputs, set of completed node names)`

This is encoded in the step history, not as a separate pointer. The graph structure constrains execution paths, so given this state, there's exactly one deterministic answer to "what runs next?"

**Partial superstep recovery:** If a crash occurs mid-superstep, only incomplete nodes re-run:

```
Superstep 0: [embed, validate, fetch] running in parallel
  → embed completes   (status=COMPLETED)
  → validate completes (status=COMPLETED)
  → 💥 CRASH before fetch completes

On resume:
  → embed: COMPLETED → skip (output loaded from checkpoint)
  → validate: COMPLETED → skip (output loaded from checkpoint)
  → fetch: RUNNING → re-execute
```

This ensures at-least-once semantics per node, not per superstep.

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

hypergraph uses a **unified event stream** that flows through pluggable layers. The core execution engine produces events; layers consume what they need.

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

**hypergraph execution status.** Returned in `RunResult.status`.

```python
from enum import Enum

class RunStatus(Enum):
    """Workflow execution status (hypergraph layer)."""
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
```

> **Note:** Additional pause reasons (`SLEEP`, `SCHEDULED`, `EVENT`) may be added when using DBOSAsyncRunner for durable sleep and scheduling features. See [Durable Execution](durable-execution.md) for DBOS capabilities.

### Status Semantics

| Status | Meaning | Typical Cause | Resume Action |
|--------|---------|---------------|---------------|
| `COMPLETED` | Finished | Normal completion or routed to `END` | None needed |
| `PAUSED` | Waiting | `InterruptNode` | Provide response via `resume()` or `DBOS.send()` |
| `ERROR` | Failed | Uncaught exception | Fix and retry |

### Pause Reasons

| Reason | Meaning | Resume Action |
|--------|---------|---------------|
| `HUMAN_INPUT` | Waiting for human decision | Provide response via `resume()` or `DBOS.send()` |

### DBOS Mapping

hypergraph status maps to DBOS status as follows:

```
┌─────────────────────────────────────────────────────────────┐
│  hypergraph RunStatus        →    DBOS WorkflowStatus       │
├─────────────────────────────────────────────────────────────┤
│  COMPLETED                   →    SUCCESS                   │
│  PAUSED (any reason)         →    PENDING (blocked on recv) │
│  ERROR                       →    ERROR                     │
└─────────────────────────────────────────────────────────────┘
```

**Key insight:** DBOS has no "PAUSED" status. When a workflow calls `DBOS.recv()` and waits for human input, DBOS still reports it as `PENDING`. hypergraph adds the `PAUSED` + `PauseReason` abstraction on top.

---

## GraphState

### Purpose

**Runtime storage for value versions and execution history.** Used internally by runners to track what's been computed and when to re-execute nodes. **This is not user-facing.**

### Important: GraphState Holds ALL Values

`GraphState.values` contains **all outputs from all executed nodes**. When a checkpointer is present, all values are also saved for durability:

```
GraphState.values = {"embedding": [...], "answer": "..."}  # ALL values at runtime
                      ↓
Checkpointer saves = {"embedding": [...], "answer": "..."}  # ALL values persisted
```

This means:
- During execution, all values are available for downstream nodes
- On crash recovery, all values are loaded from checkpoint (nodes skipped)

### NodeExecution (Internal)

```python
@dataclass
class NodeExecution:
    """Record of a single node execution. Internal to runners."""

    node_name: str
    """Name of the node that executed."""

    started_at: float
    """Timestamp when execution started."""

    completed_at: float | None
    """Timestamp when execution completed (None if still running)."""

    outputs: dict[str, Any] | None
    """Output values produced (None if failed or still running)."""

    error: str | None
    """Error message if execution failed."""

    cached: bool = False
    """True if result was retrieved from cache."""
```

### Class Definition

```python
@dataclass
class GraphState:
    """Runtime value storage with versioning. Internal to runners."""

    values: dict[str, Any]
    """Current values by name. Includes all outputs from all executed nodes."""

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
    """Why we're paused (currently only HUMAN_INPUT)."""

    node: str
    """Name of the node that caused the pause."""

    response_param: str
    """Parameter name to use when resuming (the key for inputs dict)."""

    value: Any
    """Value to show user (e.g., the draft for approval)."""
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
- **`workflow_id` for resume** - Same `workflow_id` auto-resumes from where you left off (checkpointer detects paused state).
- **Dict-like access** - `result["answer"]` is equivalent to `result.outputs["answer"]`.
- **All outputs by default** - `outputs` contains all node outputs unless filtered with `select=`.

### Basic Example

```python
from hypergraph import AsyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

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
)

# Option 2: Resume the nested graph directly (advanced)
result = await runner.run(
    review_pipeline,
    inputs={"decision": user_response},
    workflow_id="order-123/review",  # Nested workflow ID
)
```

### With DBOS

When using `DBOSAsyncRunner`, resume happens via `DBOS.send()` from an external system:

```python
from hypergraph.runners import DBOSAsyncRunner

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
    cached: bool      # True if loaded from cache (same inputs seen before)
    replayed: bool    # True if loaded from checkpoint (crash recovery/resume)
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
from hypergraph import AsyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

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

    # Resume using same workflow_id (checkpointer auto-detects paused state)
    result = await runner.run(
        graph,
        inputs={result.pause.response_param: response},
        workflow_id="session-123",
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
        workflow_id=workflow_id,  # Checkpointer auto-detects paused state
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
    WAITING = "waiting"  # Paused at InterruptNode, waiting for response
```

### Step

```python
@dataclass
class Step:
    """A single step in a workflow.

    Maps to DBOS `operation_outputs` table.
    """
    superstep: int
    """Which superstep (batch) this step belongs to.

    Nodes that can run in parallel share the same superstep number.
    This is the user-facing identifier for checkpointing/forking.
    Follows LangGraph/Pregel terminology.
    """

    node_name: str
    """Name of the node that executed."""

    index: int
    """Unique sequential ID for this step (internal).

    Used as DB primary key. Within a superstep, indices are assigned
    alphabetically by node_name for deterministic ordering regardless
    of completion order.
    """

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
    pause: PauseInfo | None = None  # Set when step is an InterruptNode waiting for response
```

**Pause persistence:** When an `InterruptNode` executes and waits for a response, the step is saved with `status=WAITING` and `pause` containing the pause metadata. This enables external systems to query "what is this workflow waiting for?" even after a crash.

```python
# Example: Step saved when InterruptNode pauses
step = Step(index=3, node_name="approval", status=StepStatus.WAITING)
result = StepResult(
    step_index=3,
    pause=PauseInfo(
        reason=PauseReason.HUMAN_INPUT,
        node="approval",
        response_param="decision",
        value={"draft": "The poem content..."}
    )
)
```

### Steps vs StepResults: What Gets Saved

**Everything is saved.** Both steps and their outputs are persisted for all executed nodes.

```
┌─────────────────────────────────────────────────────────────────┐
│  Step (always saved)           │  StepResult (always saved)     │
├────────────────────────────────┼────────────────────────────────┤
│  index: 0                      │  outputs: {"embedding": [...]} │
│  node_name: "embed"            │                                │
│  status: COMPLETED             │                                │
├────────────────────────────────┼────────────────────────────────┤
│  index: 1                      │  outputs: {"answer": "..."}    │
│  node_name: "generate"         │                                │
│  status: COMPLETED             │                                │
└────────────────────────────────┴────────────────────────────────┘
```

Steps serve as the **implicit cursor** (see [Step History as Implicit Cursor](#step-history-as-implicit-cursor)):
- **Cycles:** Step count tracks iteration number
- **Branches:** Steps show which branch was taken

### Resume Behavior

On resume, a node is **skipped** if it appears in step history as `COMPLETED` (its output is always available since everything is saved):

```python
def should_skip_node(node, checkpoint):
    return node.name in {s.node_name for s in checkpoint.steps if s.status == COMPLETED}
```

This ensures full crash recovery — no nodes re-execute on resume.

### WorkflowStatus (DBOS-compatible)

```python
class WorkflowStatus(Enum):
    """DBOS-compatible workflow status values.

    These match DBOS exactly for storage compatibility.
    Use RunStatus for hypergraph API layer.
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

| hypergraph Type | DBOS Table | Key Columns |
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

### Checkpoint

```python
@dataclass
class Checkpoint:
    """A point-in-time snapshot of workflow state.

    Bundles outputs + step history together. Used for:
    - Forking from a past point
    - Manual resume without checkpointer
    - Testing and debugging
    """
    outputs: dict[str, Any]
    """Computed output values at this checkpoint (folded from StepResults)."""

    steps: list[Step]
    """Step history up to this checkpoint (the implicit cursor).

    Note: This is Step metadata only (index, node_name, status).
    The actual output values are pre-computed in `outputs`.
    """
```

**Why bundle outputs + steps?**

As established in [Step History as Implicit Cursor](#step-history-as-implicit-cursor), outputs alone are insufficient for:
- Cycles (need iteration count)
- Branches with shared outputs (need to know which branch)

The `Checkpoint` type ensures these always travel together.

**Why no workflow_id?**

The checkpoint is a snapshot of *state*, not identity. When forking, you provide a new `workflow_id` separately. Including the source workflow_id would be confusing since it's not the target workflow.

### Resume vs Fork

Two distinct patterns for continuing execution:

**Resume: Continue the same workflow**

```python
# Same workflow_id → checkpointer loads state automatically
result = await runner.run(
    graph,
    inputs={"decision": "approve"},
    workflow_id="order-123",  # Checkpointer finds and loads state
)
```

The checkpointer handles everything. User just provides new inputs.

**Fork: Start new workflow from past point**

```python
# Get checkpoint at a specific step
checkpoint = await checkpointer.get_checkpoint("order-123", at_step=5)

# Fork with different inputs - requires NEW workflow_id
result = await runner.run(
    graph,
    inputs={"decision": "reject"},  # Different choice this time
    checkpoint=checkpoint,
    workflow_id="order-123-retry",  # NEW workflow ID for the fork
)
```

Fork creates a new workflow that starts from the checkpoint state.

**Parameter Combinations:**

| `workflow_id` | `checkpoint` | Behavior |
|:-------------:|:------------:|----------|
| ❌ None | ❌ None | With checkpointer: Error. Without: OK (ephemeral run) |
| ❌ None | ✅ Yes | Fork with auto-generated workflow_id |
| ✅ New ID | ❌ None | Fresh start |
| ✅ Existing ID | ❌ None | Resume from checkpointer state |
| ✅ New ID | ✅ Yes | Fork with explicit workflow_id |
| ✅ Existing ID | ✅ Yes | Error: can't fork into existing workflow |

**API Summary:**

| Pattern | Parameters | Use Case |
|---------|------------|----------|
| Fresh start | `inputs=`, `workflow_id=` (new ID) | New workflow |
| Resume | `inputs=`, `workflow_id=` (existing ID) | Continue paused/crashed workflow |
| Fork | `inputs=`, `checkpoint=`, `workflow_id=` (new ID) | Retry from past point |

**Without checkpointer (manual state management):**

```python
# You manage storage - must provide checkpoint
result = await runner.run(
    graph,
    inputs={**new_inputs},  # New inputs only
    checkpoint=checkpoint,   # Contains outputs + steps
)
# No workflow_id needed without checkpointer
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
└── GraphState (runtime values with versioning)

Persistence Types:
├── StepStatus (enum: PENDING, RUNNING, COMPLETED, FAILED, WAITING)
├── WorkflowStatus (enum: PENDING, ENQUEUED, SUCCESS, ERROR, CANCELLED)
├── Step (individual step record: index, node_name, status)
├── StepResult (step outputs + pause info)
├── Workflow (workflow execution record)
└── Checkpoint (point-in-time snapshot: computed outputs + step history)

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
│  hypergraph API Layer (what users see)                          │
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
│  All values from executed nodes                                 │
│  Version tracking for staleness detection                       │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ Checkpointer saves all values
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Persistence Layer (Workflow, Step, StepResult)                 │
│                                                                 │
│  All values saved for full durability                           │
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
