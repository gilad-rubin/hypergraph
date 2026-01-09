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

### Terminology: values

The term `values` is used consistently throughout hypergraph:

| Context | Example | Meaning |
|---------|---------|---------|
| Runner parameter | `runner.run(values={...})` | Input values for graph execution |
| RunResult | `result.values` | Output values from execution |
| StepRecord | `step.values` | Node outputs persisted in checkpoint |
| GraphState | `state.values` | Runtime value storage (internal) |
| NodeStartEvent | `event.inputs` | Values passed to a specific node |

**Note:** `NodeStartEvent.inputs` uses "inputs" because it refers to what a *node* receives (distinct from graph-level values).

### Terminology: interrupt vs pause

| Term | Where used | Meaning |
|------|------------|---------|
| **Interrupt** | `InterruptNode`, `InterruptEvent` | The action/mechanism that causes execution to wait |
| **Pause** | `RunStatus.PAUSED`, `PauseInfo`, `PauseReason` | The resulting state when execution is waiting |

**Think of it as:** The `InterruptNode` *interrupts* execution, causing the run to be in a *paused* state.

| Layer | Term Used |
|-------|-----------|
| Node type | `InterruptNode` (the action) |
| Event | `InterruptEvent` (the action happening) |
| Status | `PAUSED` (the resulting state) |
| Info | `PauseInfo` (details about the paused state) |

### Events vs Steps: Ephemeral vs Durable

hypergraph separates two distinct record types:

| Concept | Events | Steps |
|---------|--------|-------|
| **Purpose** | Real-time observability | Durability and recovery |
| **Lifetime** | Ephemeral (in-memory) | Persisted (to database) |
| **Contains** | All execution details | All node outputs |
| **Consumers** | EventProcessor, `.iter()` | Checkpointer |

**Events** are emitted during execution for real-time streaming and observability. They include `NodeStartEvent`, `StreamingChunkEvent`, etc. Events are consumed by `EventProcessor` implementations or via `.iter()` for pull-based access. **Events are NOT persisted by default.** Note: Not all runners emit events—see [Which Runners Emit Events?](observability.md#which-runners-emit-events)

**Steps** are persisted records saved by the checkpointer. Each step contains the node outputs. Steps enable crash recovery, resume, and workflow forking.

```
During Execution (SyncRunner, AsyncRunner, DBOSAsyncRunner):
  Runner emits Events → EventProcessor (observability)
                     → .iter() (real-time UI)

After Node Completion:
  Runner saves StepRecord → Checkpointer (durability)
                     → All outputs saved

Note: DaftRunner does not emit events (Daft controls execution).
```

**Key insight:** When a node produces output, the value exists once in memory. Events reference this value (in memory) for observability. The checkpointer serializes and stores a copy for durability. They are separate concerns with separate interfaces.

**Persistence policy: All outputs are persisted.** There is no selective persistence - when a checkpointer is configured, every node's output is saved. This ensures reliable crash recovery and workflow forking. If you need to exclude sensitive data from persistence, handle it at the serialization layer (e.g., custom serializer that redacts fields).

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
def should_run_node(node: HyperNode, state: GraphState, steps: list[StepRecord]) -> bool:
    """Unified algorithm for DAGs and cycles."""

    # 1. Inputs available?
    if not all(inp in state.values for inp in node.inputs):
        return False

    # 2. Find last step for this node
    last_step = find_last_step(node.name, steps)
    if last_step is None:
        return True  # Never ran

    # 3. Compare consumed vs current versions (staleness detection)
    consumed = last_step.result.input_versions
    current = {inp: state.versions[inp] for inp in node.inputs}
    return consumed != current  # Run if any input changed
```

This algorithm handles both DAGs and cycles:

| Scenario | Behavior |
|----------|----------|
| Fresh DAG | No steps exist, all nodes with satisfied inputs run |
| Resume mid-DAG | Steps have input_versions, skip if versions match |
| Cycle iteration | Version increments trigger staleness, node re-runs |
| Resume mid-cycle | Last step's input_versions determines staleness |

**The implicit cursor is:** `(available_outputs, step history with input_versions)`

This is encoded in the step history, not as a separate pointer. The graph structure constrains execution paths, so given this state, there's exactly one deterministic answer to "what runs next?"

**Partial superstep recovery:** If a crash occurs mid-superstep, only incomplete nodes re-run:

```
Superstep 0: [embed, validate, fetch] running in parallel
  → embed completes   (step saved: COMPLETED)
  → validate completes (step saved: COMPLETED)
  → 💥 CRASH before fetch completes

On resume:
  → embed: has step → skip (output loaded from checkpoint)
  → validate: has step → skip (output loaded from checkpoint)
  → fetch: no step → re-execute
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
| [Persistence Types](#persistence-types) | Checkpoint storage | Workflow, StepRecord, Checkpoint |

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
async for event in runner.iter(graph, values={...}, workflow_id="session-123"):
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
result = await runner.run(graph, values={...}, workflow_id="session-123")
```

---

## Status Enums

### Design Philosophy: Status as Decision Driver

A **status** is a small set of mutually exclusive states that changes what a consumer does next. Everything else is extra metadata.

**Who consumes statuses?**

| Consumer | What they need | Level |
|----------|---------------|-------|
| App code | "Do I have a result? Need input? Error? User stopped?" | Run |
| UI/streaming | Render experience, show partial output indicator | Run + `partial` flag |
| Checkpointer | Which steps have usable output for resume? | StepRecord |
| Observability/DBOS | Label traces, map to foreign status models | Workflow |

**Key principle:** Statuses are minimal. Each status implies a different "what do I do next?" branch.

### RunStatus

**How did `.run()` end?** Returned in `RunResult.status`.

```python
from enum import Enum

class RunStatus(Enum):
    """Status of a single .run() or .iter() invocation."""

    COMPLETED = "completed"
    """Run finished normally. All planned nodes executed."""

    FAILED = "failed"
    """Run terminated due to unhandled exception."""

    PAUSED = "paused"
    """Run waiting for external input (InterruptNode)."""

    STOPPED = "stopped"
    """Run ended because caller requested stop.

    This status is always used when stop is requested, even if:
    - Some streaming nodes saved partial output (check StepRecord.partial)
    - Remaining nodes continued to completion (complete_on_stop=True)

    The distinction is: COMPLETED means "no stop requested".
    """
```

### StepStatus

**What happened to this step?** Used in persistence layer for resume logic.

```python
class StepStatus(Enum):
    """Execution status of a single step (persisted)."""

    COMPLETED = "completed"
    """Step finished with usable output.

    Check StepRecord.partial to see if output was truncated
    due to stop request (streaming nodes only).
    """

    FAILED = "failed"
    """Step terminated due to exception."""

    PAUSED = "paused"
    """Step at InterruptNode, waiting for response.

    StepRecord.pause contains the pause details.
    """

    STOPPED = "stopped"
    """Step ended due to stop request, no usable output.

    This happens when:
    - Non-streaming node was stopped mid-execution
    - Streaming node was stopped with complete_on_stop=False

    StepRecord.values will be None.
    """
```

**Note:** Steps that never started have no record. There is no PENDING or RUNNING status in persistence — these are operational states for live monitoring, not needed for recovery correctness.

### WorkflowStatus

**Can this workflow be resumed?** Used for workflow lifecycle management.

```python
class WorkflowStatus(Enum):
    """Lifecycle status of an entire workflow (across multiple runs)."""

    ACTIVE = "active"
    """Workflow can be resumed.

    Covers: currently running, paused at InterruptNode,
    or stopped but resumable.
    """

    COMPLETED = "completed"
    """Workflow finished successfully. Terminal state."""

    FAILED = "failed"
    """Workflow terminated due to unrecoverable error. Terminal state."""
```

### PauseReason

**Why a workflow is paused.** Only set when `status == PAUSED`.

```python
class PauseReason(Enum):
    """Why a workflow is paused (when status is PAUSED)."""
    HUMAN_INPUT = "human_input"  # InterruptNode waiting for response
```

> **Note:** Additional pause reasons (`SLEEP`, `SCHEDULED`, `EVENT`) may be added when using DBOSAsyncRunner for durable sleep and scheduling features. See [Durable Execution](durable-execution.md) for DBOS capabilities.

### Status Decision Matrix

**Run-level decisions:**

| Status | Has usable result? | What to do next |
|--------|:------------------:|-----------------|
| COMPLETED | ✅ Full | Use `result.values` |
| FAILED | ❌ | Handle `result.error` |
| PAUSED | Partial | Prompt user via `result.pause`, then resume |
| STOPPED | Maybe partial | Check steps for `partial=True` outputs |

**Step-level decisions (for resume):**

| Status | Has `values`? | Resume action |
|--------|:-------------:|---------------|
| COMPLETED | ✅ | Skip (use saved output) |
| FAILED | ❌ | Handle error or retry |
| PAUSED | Partial | Provide input and continue |
| STOPPED | ❌ | Re-run this step |

**Workflow-level decisions:**

| Status | Can resume? | Typical action |
|--------|:-----------:|----------------|
| ACTIVE | ✅ | Show "Continue" button |
| COMPLETED | ❌ | Archive, show results |
| FAILED | ❌ | Show error, allow retry |

### The `partial` Flag

For streaming nodes stopped with `complete_on_stop=True`, the step saves partial output. The `partial` flag distinguishes this from normal completion:

```python
# StepRecord has partial flag:
partial: bool = False  # True = values contains usable but truncated output
```

| Scenario | status | values | partial |
|----------|--------|--------|:-------:|
| Normal finish | COMPLETED | `{...}` | `False` |
| Stopped, partial saved | COMPLETED | `{...}` | `True` |
| Stopped, no output | STOPPED | `None` | `False` |

**Consumer code:**

```python
step: StepRecord = ...

if step.status == StepStatus.COMPLETED:
    use(step.values)
    if step.partial:
        show_indicator("(truncated)")
elif step.status == StepStatus.STOPPED:
    # No usable output
    pass
```

### DBOS Mapping

hypergraph status maps to DBOS status for storage compatibility:

```
┌─────────────────────────────────────────────────────────────┐
│  hypergraph                  →    DBOS                      │
├─────────────────────────────────────────────────────────────┤
│  WorkflowStatus.ACTIVE       →    PENDING                   │
│  WorkflowStatus.COMPLETED    →    SUCCESS                   │
│  WorkflowStatus.FAILED       →    ERROR                     │
├─────────────────────────────────────────────────────────────┤
│  RunStatus.COMPLETED         →    (workflow) SUCCESS        │
│  RunStatus.FAILED            →    (workflow) ERROR          │
│  RunStatus.PAUSED            →    (workflow) PENDING        │
│  RunStatus.STOPPED           →    (workflow) PENDING        │
└─────────────────────────────────────────────────────────────┘
```

**Key insight:** DBOS has no "PAUSED" or "STOPPED" status. Both map to PENDING because the workflow is still active (resumable). hypergraph adds finer-grained status on top for better developer experience.

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

**Note:** Users don't interact with `GraphState` directly. Use `RunResult.values` to access execution results.

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

    node_name: str
    """Name of the node that caused the pause.

    For nested graphs, this is the full path using "/" (e.g., "review/approval").
    """

    response_param: str
    """Local parameter name defined by the InterruptNode (e.g., "decision").

    For the full namespaced key to use in values dict, use response_key property.
    """

    value: Any
    """Value to show user (e.g., the draft for approval)."""

    @property
    def response_key(self) -> str:
        """Namespaced key for the values dict when resuming.

        Uses "." separator for values (attribute access convention).

        For top-level interrupts: returns response_param as-is.
        For nested interrupts: prefixes with GraphNode path.

        Examples:
            - Top-level: response_param="decision" → "decision"
            - Nested: node_name="review/approval", response_param="decision" → "review.decision"
            - Deeply nested: node_name="outer/inner/approval" → "outer.inner.decision"

        Usage:
            result = await runner.run(graph, values={...})
            if result.pause:
                response = get_user_input(result.pause.value)
                result = await runner.run(
                    graph,
                    values={result.pause.response_key: response},
                    workflow_id=result.workflow_id,
                )
        """
        if "/" not in self.node_name:
            return self.response_param
        # Convert path separator "/" to value separator "."
        namespace = self.node_name.rsplit("/", 1)[0].replace("/", ".")
        return f"{namespace}.{self.response_param}"
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

    values: dict[str, Any | "RunResult"]
    """Output name → value mapping. For nested graphs, contains nested RunResult objects."""

    status: RunStatus
    """Execution status: COMPLETED, PAUSED, STOPPED, or FAILED."""

    workflow_id: str | None
    """Workflow identifier (required with checkpointer, None otherwise)."""

    run_id: str
    """Unique identifier for this execution."""

    pause: PauseInfo | None = None
    """Pause details (only set when status == PAUSED)."""

    error: str | None = None
    """Error message (only set when status == FAILED)."""

    # === Dict-like access for convenience ===

    def __getitem__(self, key: str) -> Any | "RunResult":
        """Dict-like access: result['answer'] or result['nested_graph']['value']"""
        return self.values[key]

    def __contains__(self, key: str) -> bool:
        """Check if value exists: 'answer' in result"""
        return key in self.values

    def keys(self):
        """Get output names."""
        return self.values.keys()

    def items(self):
        """Get output name-value pairs."""
        return self.values.items()

    @property
    def paused(self) -> bool:
        """True if workflow is paused."""
        return self.pause is not None
```

### Key Design Decisions

- **Nested `RunResult` for nested graphs** - When a graph contains `GraphNode`s, their results are nested `RunResult` objects, preserving status and pause info per subgraph.
- **No `checkpoint: bytes`** - The checkpointer manages state internally by `workflow_id`. You don't pass checkpoint bytes around.
- **`workflow_id` for resume** - Same `workflow_id` auto-resumes from where you left off (checkpointer detects paused state).
- **Dict-like access** - `result["answer"]` is equivalent to `result.values["answer"]`.
- **All values by default** - `values` contains all node outputs unless filtered with `select=`.

### Basic Example

```python
from hypergraph import AsyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

runner = AsyncRunner(checkpointer=SqliteCheckpointer("./dev.db"))
result = await runner.run(
    graph,
    values={"query": "hello"},
    workflow_id="session-123",
)

# Dict-like access
print(result["answer"])           # Same as result.values["answer"]
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

result = await runner.run(outer, values={...}, workflow_id="order-123")

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
result = await runner.run(outer, values={...}, workflow_id="order-123")

# Check overall status
if result.status == RunStatus.PAUSED:
    # Find which nested graph paused
    if result["rag"].status == RunStatus.COMPLETED:
        print("RAG completed")

    if result["review"].status == RunStatus.PAUSED:
        print(f"Review paused at: {result['review'].pause.node_name}")
        print(f"Value to show user: {result['review'].pause.value}")
        print(f"Nested workflow ID: {result['review'].workflow_id}")  # "order-123/review"

# The top-level pause info points to the nested interrupt
print(result.pause.node_name)  # "review/approval" (path to the interrupt)
```

**Pause propagation rules:**
- If any nested graph pauses, the parent graph pauses
- `result.pause` contains info about the first pause encountered
- **First pause wins** - if multiple nested graphs could pause in parallel, execution stops at the first one

### Resume Nested Graph

To resume a paused nested graph:

```python
# Option 1: Resume the outer graph (recommended)
# response_key uses "." for nested: "review.decision"
result = await runner.run(
    outer,
    values={result.pause.response_key: user_response},
    workflow_id="order-123",
)

# Option 2: Resume the nested graph directly (advanced)
result = await runner.run(
    review_pipeline,
    values={"decision": user_response},
    workflow_id="order-123/review",  # Nested workflow ID uses "/"
)
```

### With DBOS

When using `DBOSAsyncRunner`, resume happens via `DBOS.send()` from an external system:

```python
from hypergraph.runners import DBOSAsyncRunner

runner = DBOSAsyncRunner()
result = await runner.run(
    graph,
    values={"prompt": "Write a poem"},
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

    def stop(self) -> None:
        """
        Request graceful stop of execution.

        - Currently executing nodes will complete (or save partial output if streaming)
        - No new nodes will start
        - Iteration will end after in-flight work completes
        - Final result will have status=STOPPED

        Behavior depends on node's `complete_on_stop` setting:
        - complete_on_stop=True (default for streaming): Save partial output
        - complete_on_stop=False: No output saved for this node

        This is a request, not immediate cancellation. Use for user-initiated
        cancellation (e.g., "Stop" button in UI).
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
async with runner.iter(graph, values={"query": "hello"}, workflow_id="session-123") as run:
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

### RunStartEvent

```python
@dataclass
class RunStartEvent:
    run_id: str
    span_id: str             # Root span for this run
    parent_span_id: str | None  # None for top-level, set for nested graphs
    workflow_id: str | None  # Workflow identifier if using checkpointer
    graph_name: str          # Name of the graph being executed
    timestamp: float
```

### RunEndEvent

```python
@dataclass
class RunEndEvent:
    run_id: str
    span_id: str             # Same as RunStartEvent.span_id
    parent_span_id: str | None
    workflow_id: str | None
    status: RunStatus        # COMPLETED, FAILED, PAUSED, or STOPPED
    error: str | None        # Error message if status == FAILED
    duration_ms: float
    timestamp: float
```

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

### CacheHitEvent

Emitted when a node's result is retrieved from cache (before `NodeEndEvent`). Useful for cache analytics.

```python
@dataclass
class CacheHitEvent:
    run_id: str
    span_id: str
    parent_span_id: str | None
    node_name: str
    cache_key: str           # The cache key that matched
    timestamp: float
```

**Note:** When a cache hit occurs, you'll see both `CacheHitEvent` (for cache analytics) and `NodeEndEvent` with `cached=True` (for general observability). The `CacheHitEvent` provides cache-specific details.

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
    node_name: str          # Name of the InterruptNode (path for nested graphs)
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
    node_name: str   # Name of the gate/route node
    decision: str    # Target node name or "END"
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

### StopRequestedEvent

Emitted when `RunHandle.stop()` is called. Allows UIs to react immediately (e.g., show "Stopping..." indicator) without waiting for `RunEndEvent`.

```python
@dataclass
class StopRequestedEvent:
    run_id: str
    span_id: str             # Run's root span
    parent_span_id: str | None
    workflow_id: str | None
    timestamp: float
```

**Note:** After `StopRequestedEvent`, in-flight nodes will complete and `RunEndEvent` will follow with `status=STOPPED`.

### Example

```python
async with runner.iter(graph, values={...}) as run:
    async for event in run:
        match event:
            case NodeStartEvent(node_name=name):
                print(f"Starting: {name}")

            case NodeEndEvent(node_name=name, duration_ms=ms):
                print(f"Finished: {name} in {ms}ms")

            case StreamingChunkEvent(chunk=chunk):
                print(chunk, end="")

            case InterruptEvent(node_name=name, value=prompt):
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
    values={"query": "hello"},
    workflow_id="session-123",
)

if result.pause:
    # Execution paused at InterruptNode
    prompt = result.pause.value

    # Get user response (your application logic)
    response = await get_user_response(prompt)

    # Resume using same workflow_id (checkpointer auto-detects paused state)
    # Use response_key for namespaced value access (uses "." separator)
    result = await runner.run(
        graph,
        values={result.pause.response_key: response},
        workflow_id="session-123",
    )

# Now complete
assert not result.pause
print(result.values["answer"])
```

### Using `.iter()` - Handle Inline

```python
async with runner.iter(graph, values={"query": "hello"}, workflow_id="session-123") as run:
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
    print(run.result.values)
```

### Using `.run()` with Handlers

Pass handlers per-call for automatic interrupt resolution:

```python
result = await runner.run(
    graph,
    values={"query": "hello"},
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
result = await runner.run(outer_graph, values={...})
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
result = await runner.run(outer_graph, values={...})
for path, paused_result in find_paused_graphs(result):
    print(f"Paused at: {path}")
    print(f"  Waiting for: {paused_result.pause.response_param}")
    print(f"  Value: {paused_result.pause.value}")
```

### Event Filtering and Routing

```python
async def process_events(graph: Graph, workflow_id: str):
    """Route events to different handlers based on type."""
    async with runner.iter(graph, values={...}, workflow_id=workflow_id) as run:
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
        "INSERT INTO pending_approvals (workflow_id, node_name, value) VALUES (?, ?, ?)",
        (event.workflow_id, event.node_name, serialize(event.value))
    )

# Resume from stored workflow_id
async def resume_execution(workflow_id: str, user_response: Any):
    pending = await db.fetch_one(
        "SELECT node_name FROM pending_approvals WHERE workflow_id = ?",
        (workflow_id,)
    )

    result = await runner.run(
        graph,
        values={pending["node_name"]: user_response},
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

    async def consume(self, graph: Graph, values: dict):
        async with runner.iter(graph, values=values) as run:
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

> **Note:** `StepStatus` and `WorkflowStatus` are defined in [Status Enums](#status-enums) above.

### StepRecord

```python
@dataclass(frozen=True)
class StepRecord:
    """A single atomic record of node execution.

    Combines step metadata and result values into one type to ensure
    atomic persistence - either the entire record is saved, or nothing.
    This eliminates the possibility of corrupted state from crashes
    between separate writes.

    Maps to DBOS `operation_outputs` table.
    """

    # === Identity ===

    workflow_id: str
    """Which workflow this step belongs to."""

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

    # === Status ===

    status: StepStatus
    """Execution status: COMPLETED, FAILED, PAUSED, or STOPPED."""

    # === Outputs ===

    input_versions: dict[str, int]
    """Version of each input when this node executed.

    Used for staleness detection on resume. If current versions differ
    from input_versions, the node should re-execute (its inputs changed).
    """

    values: dict[str, Any] | None = None
    """Output values. Present when status is COMPLETED (or PAUSED with partial output)."""

    error: str | None = None
    """Error message. Present when status is FAILED."""

    pause: PauseInfo | None = None
    """Pause details. Present when status is PAUSED."""

    partial: bool = False
    """True if values contains truncated output due to stop request.

    Only meaningful for streaming nodes with complete_on_stop=True.
    When True, status will be COMPLETED (output is usable, just truncated).
    """

    # === Timestamps ===

    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None

    # === Nested ===

    child_workflow_id: str | None = None
    """For GraphNode steps, the nested workflow ID (e.g., 'order-123/rag')."""
```

### Interrupt Persistence Model

An interrupt step represents the entire lifecycle of an `InterruptNode` execution—from pause to response. The step is **updated** (not appended) when the response arrives.

**Step lifecycle:**

```
Node starts → saves PAUSED step → waits → response arrives → updates to COMPLETED
         └──────────────────── one step, updated in place ────────────────────┘
```

**Why update, not append?**

- One step per node per superstep (matches all other node types)
- Unique constraint `(workflow_id, node_name, superstep)` works naturally
- No "find latest step" ambiguity

**Initial save (when interrupt triggers):**

```python
StepRecord(
    workflow_id="order-123",
    superstep=2,
    node_name="approval",
    index=3,
    status=StepStatus.PAUSED,
    input_versions={"draft": 1},
    values=None,  # No output yet
    pause=PauseInfo(
        reason=PauseReason.HUMAN_INPUT,
        node_name="approval",
        response_param="decision",
        value={"draft": "The poem content..."}
    )
)
```

**After response arrives (update same record):**

```python
StepRecord(
    ...  # Same identity fields
    status=StepStatus.COMPLETED,
    values={"decision": "approve"},
    pause=None,  # Cleared
)
```

#### Crash-Safety: Write-Ahead Response Pattern

The transition from PAUSED to COMPLETED has a crash-safety gap:

```
User clicks "Approve"
    ↓
Response received         ← In memory only
    ↓
💥 CRASH                  ← Response lost
    ↓
Step.update(COMPLETED)    ← Never happens
```

**Solution: Persist the response before updating the step.**

With DBOS, this is automatic—`DBOS.recv()` durably stores the response. On resume, the response is still available.

Without DBOS, implementations should use a **write-ahead log** pattern:

1. **Phase 1:** Write response to `pending_responses` table
2. **Phase 2:** Update step to COMPLETED
3. **Phase 3:** Delete pending response

On resume, check for orphaned pending responses and apply them before continuing.

**The invariant:** At any point, exactly one of these is true:

| State | Condition | Resume Action |
|-------|-----------|---------------|
| Waiting | Step is PAUSED, no pending response | Re-prompt user |
| Transitioning | Step is PAUSED, pending response exists | Apply pending → update step |
| Complete | Step is COMPLETED | Continue execution |

This ensures no response is ever lost, regardless of crash timing.

### What Gets Saved

**Everything is saved atomically.** Each step record contains both metadata and outputs in a single write.

```
┌─────────────────────────────────────────────────────────────────┐
│  StepRecord (single atomic write)                               │
├─────────────────────────────────────────────────────────────────┤
│  workflow_id: "order-123"                                       │
│  superstep: 0                                                   │
│  node_name: "embed"                                             │
│  index: 0                                                       │
│  status: COMPLETED                                              │
│  input_versions: {"text": 1}                                    │
│  values: {"embedding": [...]}                                   │
├─────────────────────────────────────────────────────────────────┤
│  workflow_id: "order-123"                                       │
│  superstep: 1                                                   │
│  node_name: "generate"                                          │
│  index: 1                                                       │
│  status: COMPLETED                                              │
│  input_versions: {"embedding": 1}                               │
│  values: {"answer": "..."}                                      │
└─────────────────────────────────────────────────────────────────┘
```

Steps serve as the **implicit cursor** (see [Step History as Implicit Cursor](#step-history-as-implicit-cursor)):
- **Cycles:** Step count tracks iteration number
- **Branches:** Steps show which branch was taken

### Resume Behavior

On resume, a node is skipped if its last step's `input_versions` match current versions (see [Step History as Implicit Cursor](#step-history-as-implicit-cursor) for the full algorithm):

```python
def should_skip_node(node, state, steps):
    last_step = find_last_step(node.name, steps)
    if last_step is None:
        return False  # Never ran, don't skip
    consumed = last_step.input_versions
    current = {inp: state.versions[inp] for inp in node.inputs}
    return consumed == current  # Skip if versions match (not stale)
```

This ensures full crash recovery for DAGs. For cycles, version changes trigger re-execution.

### DBOSWorkflowStatus (Storage Adapter)

```python
class DBOSWorkflowStatus(Enum):
    """DBOS-native workflow status values.

    Used internally by DBOSAsyncRunner for storage compatibility.
    Users should use WorkflowStatus (ACTIVE/COMPLETED/FAILED) instead.
    """
    PENDING = "PENDING"       # Running or waiting (includes recv() blocked)
    ENQUEUED = "ENQUEUED"     # In queue, not started
    SUCCESS = "SUCCESS"       # Completed successfully
    ERROR = "ERROR"           # Failed with exception
    CANCELLED = "CANCELLED"   # Manually cancelled or timeout
```

**Mapping:** See [DBOS Mapping](#dbos-mapping) in Status Enums for how hypergraph statuses translate to DBOS.

### Workflow

```python
@dataclass
class Workflow:
    """A workflow execution with its steps.

    Maps to DBOS `workflow_status` table.
    """
    id: str
    """Unique workflow identifier."""

    status: WorkflowStatus = WorkflowStatus.ACTIVE
    """Lifecycle status: ACTIVE, COMPLETED, or FAILED."""

    steps: list[StepRecord] = field(default_factory=list)
    """All step records for this workflow (unified metadata + values)."""

    graph_hash: str | None = None
    """Hash of the graph definition at workflow creation.

    Used to detect version mismatches on resume. If current graph hash
    differs from stored hash, resume will fail with VersionMismatchError
    unless force_resume=True is specified.
    """

    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None
```

### DBOS Table Mapping

| hypergraph Type | DBOS Table | Key Columns |
|-----------------|------------|-------------|
| `Workflow` | `dbos.workflow_status` | `workflow_uuid`, `status`, `created_at` |
| `StepRecord` | `dbos.operation_outputs` | `workflow_uuid`, `function_id`, `function_name`, `output`, `error` |

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

    Bundles values + step history together. Used for:
    - Forking from a past point
    - Manual resume without checkpointer
    - Testing and debugging
    """
    values: dict[str, Any]
    """Computed output values at this checkpoint (folded from StepRecords)."""

    steps: list[StepRecord]
    """Step history up to this checkpoint (the implicit cursor).

    Each StepRecord contains both metadata and values, but the pre-computed
    `values` dict above provides fast access without iterating.
    """
```

**Why bundle values + steps?**

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
    values={"decision": "approve"},
    workflow_id="order-123",  # Checkpointer finds and loads state
)
```

The checkpointer handles everything. User just provides new inputs.

**Fork: Start new workflow from past point**

```python
# Get checkpoint at a specific superstep
checkpoint = await checkpointer.get_checkpoint("order-123", superstep=5)

# Fork with different inputs - requires NEW workflow_id
result = await runner.run(
    graph,
    values={"decision": "reject"},  # Different choice this time
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
| Fresh start | `values=`, `workflow_id=` (new ID) | New workflow |
| Resume | `values=`, `workflow_id=` (existing ID) | Continue paused/crashed workflow |
| Fork | `values=`, `checkpoint=`, `workflow_id=` (new ID) | Retry from past point |

**Without checkpointer (manual state management):**

```python
# You manage storage - must provide checkpoint
result = await runner.run(
    graph,
    values={**new_inputs},  # New inputs only
    checkpoint=checkpoint,   # Contains outputs + steps
)
# No workflow_id needed without checkpointer
```

---

## Type Hierarchy

```
User-Facing Types:
├── RunResult (primary result type, supports nesting)
│   ├── values: dict[str, Any | RunResult]  ← nested graphs are RunResult
│   ├── status: RunStatus
│   ├── pause: PauseInfo | None  ← only when PAUSED
│   ├── error: str | None        ← only when FAILED
│   └── workflow_id, run_id
├── RunStatus (enum: COMPLETED, FAILED, PAUSED, STOPPED)
├── PauseReason (enum: HUMAN_INPUT)
├── PauseInfo (pause details: reason, node_name, value, response_param)
└── RunHandle (streaming execution handle from .iter())

Internal Types (not user-facing):
└── GraphState (runtime values with versioning)

Persistence Types:
├── StepStatus (enum: COMPLETED, FAILED, PAUSED, STOPPED)
├── WorkflowStatus (enum: ACTIVE, COMPLETED, FAILED)
├── DBOSWorkflowStatus (DBOS adapter: PENDING, ENQUEUED, SUCCESS, ERROR, CANCELLED)
├── StepRecord (atomic step record: metadata + values in one type)
├── Workflow (workflow execution record with steps: list[StepRecord])
└── Checkpoint (point-in-time snapshot: computed values + step history)

Event Hierarchy (all include span_id, parent_span_id for hierarchy):
├── RunStartEvent
├── RunEndEvent
├── NodeStartEvent
├── NodeEndEvent
├── NodeErrorEvent
├── StreamingChunkEvent
├── CacheHitEvent
├── InterruptEvent
├── RouteDecisionEvent
└── StopRequestedEvent

Observability:
├── EventProcessor (base interface)
└── TypedEventProcessor (convenience class with typed methods)
```

### Type Layer Separation (Comprehensive)

```
┌─────────────────────────────────────────────────────────────────────┐
│                        EPHEMERAL (in-memory)                        │
├─────────────────────────────────────────────────────────────────────┤
│  Events (real-time observability, NOT persisted)                    │
│  ├── RunStartEvent       - run begins                               │
│  ├── RunEndEvent         - run completes (has status, error)        │
│  ├── NodeStartEvent      - node begins execution                    │
│  ├── NodeEndEvent        - node completes (has outputs)             │
│  ├── CacheHitEvent       - node result from cache                   │
│  ├── StreamingChunkEvent - streaming token                          │
│  ├── InterruptEvent      - HITL pause                               │
│  ├── RouteDecisionEvent  - which branch taken                       │
│  ├── NodeErrorEvent      - exception raised                         │
│  └── StopRequestedEvent  - stop() called                            │
│                                                                     │
│  GraphState (internal to runner, NOT user-facing)                   │
│  ├── values: ALL outputs from executed nodes                        │
│  ├── versions: version number per value (staleness detection)       │
│  └── history: in-memory execution log                               │
│                                                                     │
│  Nested graphs: Each GraphNode has its own GraphState.              │
│  Parent GraphState stores child's RunResult as a value.             │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              │ Runner translates to user-facing types
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     USER-FACING (API layer)                         │
├─────────────────────────────────────────────────────────────────────┤
│  RunResult                                                          │
│  ├── values: dict[str, Any | RunResult]  ← nested graphs here       │
│  ├── status: RunStatus (COMPLETED, FAILED, PAUSED, STOPPED)         │
│  ├── pause: PauseInfo | None    ← only when PAUSED                  │
│  ├── error: str | None          ← only when FAILED                  │
│  ├── workflow_id: str | None                                        │
│  └── run_id: str                                                    │
│                                                                     │
│  Dict-like access: result["answer"], result["rag"]["docs"]          │
│                                                                     │
│  Nested graphs: RunResult.values["rag"] returns nested RunResult.   │
│  Pause/error propagate up: if nested fails/pauses, parent does too. │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              │ Checkpointer saves ALL values
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      PERSISTED (checkpointer)                       │
├─────────────────────────────────────────────────────────────────────┤
│  Workflow                                                           │
│  ├── id: str                                                        │
│  ├── status: WorkflowStatus (ACTIVE, COMPLETED, FAILED)             │
│  ├── steps: list[StepRecord]  ← unified metadata + values           │
│  ├── graph_hash: str | None   ← for version mismatch detection      │
│  └── created_at, completed_at                                       │
│                                                                     │
│  StepRecord (one atomic record per node execution)                  │
│  ├── workflow_id: str                                               │
│  ├── index: int (monotonically increasing, DB primary key)          │
│  ├── superstep: int (parallel nodes share same superstep)           │
│  ├── node_name: str                                                 │
│  ├── status: StepStatus (COMPLETED, FAILED, PAUSED, STOPPED)        │
│  ├── input_versions: dict[str, int]  ← for staleness detection      │
│  ├── values: dict[str, Any] | None  ← THE ACTUAL VALUES             │
│  ├── error: str | None                                              │
│  ├── pause: PauseInfo | None                                        │
│  ├── partial: bool  ← True if output was cut short by stop          │
│  ├── child_workflow_id: str | None  ← nested graph reference        │
│  └── created_at, completed_at                                       │
│                                                                     │
│  KEY: Metadata + values in ONE atomic write = no corrupt state.     │
│                                                                     │
│  Nested graphs: GraphNode step has child_workflow_id pointing to    │
│  child workflow (e.g., "order-123/rag"). Child has its own steps.   │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              │ computed (fold over steps)
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  State (COMPUTED, not stored)                                       │
│  get_state(workflow_id, superstep=N) → dict[str, Any]               │
│                                                                     │
│  Folds over StepRecords up to superstep N, merging values.          │
│  Later values overwrite earlier ones (same key).                    │
│                                                                     │
│  Nested graphs: Child workflow state is computed separately.        │
│  Parent state includes child's RunResult as a value.                │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              │ snapshot for forking
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Checkpoint (point-in-time snapshot)                                │
│  ├── values: dict[str, Any]  ← computed state at this point         │
│  └── steps: list[StepRecord] ← step history (implicit cursor)       │
│                                                                     │
│  Used for: forking, manual resume, testing.                         │
│  Nested graphs: Checkpoint includes nested RunResults in values.    │
└─────────────────────────────────────────────────────────────────────┘
```

### Nested Graph Handling Summary

| Layer | How nested graphs appear |
|-------|-------------------------|
| Events | Span hierarchy via `parent_span_id` linking child to parent |
| GraphState | Child has own GraphState; parent stores child RunResult as value |
| RunResult | `result.values["rag"]` returns nested `RunResult` object |
| Workflow | GraphNode step has `child_workflow_id` (e.g., `"order-123/rag"`) |
| StepRecord | Child workflow has its own independent step records |
| Checkpoint | Nested RunResults included in `values` dict |

**See also:**
- [Checkpointer API](checkpointer.md) - Full interface definition and custom implementations
- [Durable Execution](durable-execution.md) - DBOS integration and advanced durability patterns
- [Observability](observability.md) - EventProcessor interface and integration patterns
- [Node Types](node-types.md#type-hierarchy-summary) - Complete node hierarchy
- [Graph Types](graph.md#runner-compatibility) - Runner compatibility matrix
