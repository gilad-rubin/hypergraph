# Runners API Reference

Complete method signatures and type definitions for all runners.

> **Looking for concepts and examples?** See [Runners](./runners.md) first.
>
> **Looking for observability and event processing?** See [Observability](./observability.md).

---

## BaseRunner (Abstract)

All runners inherit from `BaseRunner`:

```python
from abc import ABC, abstractmethod

class BaseRunner(ABC):
    """Abstract base for all runners."""

    @property
    @abstractmethod
    def capabilities(self) -> RunnerCapabilities:
        """Declare what this runner supports."""
        ...

    @abstractmethod
    def run(self, graph: Graph, values: dict[str, Any], **kwargs):
        """Execute graph. Return type varies by runner."""
        ...

    @abstractmethod
    def map(
        self,
        graph: Graph,
        values: dict[str, Any],
        *,
        map_over: str | list[str],
        map_mode: Literal["zip", "product"] = "zip",
        **kwargs,
    ):
        """Batch execution. Return type varies by runner."""
        ...
```

---

## RunnerCapabilities

```python
@dataclass
class RunnerCapabilities:
    # Graph feature support
    supports_cycles: bool = True
    supports_gates: bool = True
    supports_interrupts: bool = False
    supports_async_nodes: bool = False
    supports_streaming: bool = False
    supports_events: bool = True  # Does this runner emit hypergraph events?
    supports_distributed: bool = False

    # Execution interface
    returns_coroutine: bool = False  # Does .run() return a coroutine?

    def validate_graph(self, graph: Graph) -> None:
        """Raise IncompatibleRunnerError if graph uses unsupported features."""
        ...
```

**Per-runner capabilities:**

| Capability | SyncRunner | AsyncRunner | DBOSAsyncRunner | DaftRunner |
|------------|:----------:|:-----------:|:---------------:|:----------:|
| `supports_cycles` | ✅ | ✅ | ✅ | ❌ |
| `supports_gates` | ✅ | ✅ | ✅ | ❌ |
| `supports_interrupts` | ❌ | ✅ | ✅ | ❌ |
| `supports_async_nodes` | ❌ | ✅ | ✅ | ✅ |
| `supports_streaming` | ❌ | ✅ | ❌ | ❌ |
| `supports_events` | ✅ | ✅ | ❌ | ❌ |
| `supports_distributed` | ❌ | ❌ | ❌ | ✅ |
| `returns_coroutine` | ❌ | ✅ | ✅ | ❌ |
| `supports_automatic_recovery` | ❌ | ❌ | ✅ | ❌ |

**Note:** Event emission (`supports_events`) is a feature of the core runners. External runners (DBOS, Daft) delegate to systems with their own observability.

---

## SyncRunner

### Constructor

```python
class SyncRunner(BaseRunner):
    def __init__(
        self,
        *,
        cache: Cache | None = None,
        event_processors: list[EventProcessor] | None = None,
    ) -> None:
        """
        Create synchronous runner.

        Args:
            cache: Cache backend (e.g., DiskCache, MemoryCache).
            event_processors: Processors that receive execution events.
                See [Observability](./observability.md) for details.
        """
```

### run()

```python
def run(
    self,
    graph: Graph,
    values: dict[str, Any],
    *,
    select: list[str] | None = None,
    session_id: str | None = None,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    """
    Execute graph synchronously.

    Args:
        graph: Graph to execute.
        values: Input values. For cycles, determines starting point.
        select: Output names to return. Default: all leaf outputs.
        session_id: Group related runs (for logging/tracing).
        max_iterations: Maximum iterations before InfiniteLoopError.
            None means unlimited (use with caution on graphs with cycles).

    Returns:
        Dict mapping output names to values.

    Raises:
        GraphConfigError: Graph structure invalid.
        ConflictError: Parallel producers conflict.
        MissingInputError: Required input not provided.
        InfiniteLoopError: Exceeded max_iterations.
        IncompatibleRunnerError: Graph has async nodes.
    """
```

### map()

```python
def map(
    self,
    graph: Graph,
    values: dict[str, Any],
    *,
    map_over: str | list[str],
    map_mode: Literal["zip", "product"] = "zip",
    select: list[str] | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Execute graph for each item in mapped parameter(s).

    Args:
        graph: Graph to execute.
        values: Input values. map_over params should be lists.
        map_over: Parameter name(s) to iterate over. REQUIRED.
        map_mode: How to combine multiple mapped parameters:
                  - "zip": Iterate in parallel (requires same-length iterables)
                  - "product": Cartesian product of all combinations
        select: Outputs to return per item.
        session_id: Group all runs under one session.

    Returns:
        List of output dicts, one per input item.

    Raises:
        GraphConfigError: If graph contains interrupts.
    """
```

---

## AsyncRunner

### Constructor

```python
class AsyncRunner(BaseRunner):
    def __init__(
        self,
        *,
        cache: Cache | None = None,
        checkpointer: Checkpointer | None = None,
        event_processors: list[EventProcessor] | None = None,
    ) -> None:
        """
        Create asynchronous runner.

        Args:
            cache: Cache backend.
            checkpointer: Checkpointer for workflow persistence and resume.
                See [Durable Execution](./durable-execution.md) for details.
            event_processors: Processors that receive execution events.
                See [Observability](./observability.md) for details.
        """
```

### run()

```python
async def run(
    self,
    graph: Graph,
    values: dict[str, Any],
    *,
    select: list[str] | None = None,
    session_id: str | None = None,
    max_iterations: int | None = None,
    max_concurrency: int | None = None,
    workflow_id: str | None = None,
    force_resume: bool = False,
    interrupt_handlers: dict[str, Callable] | None = None,
    event_processors: list[EventProcessor] | None = None,
) -> RunResult:
    """
    Execute graph asynchronously.

    When workflow_id is provided and a checkpointer is configured:
    1. Load checkpoint state (if workflow exists)
    2. Check graph hash (raise VersionMismatchError if changed, unless force_resume=True)
    3. Merge with values (values win on conflicts)
    4. Execute graph
    5. Append steps to history
    6. Return result

    Args:
        graph: Graph to execute.
        values: Input values. Merged with checkpoint state if workflow_id exists.
            Use "." separator to provide values to nested graphs:
            - {"query": "hello"} → top-level value
            - {"rag.top_k": 5} → value for nested "rag" GraphNode
            - {"rag.inner.threshold": 0.8} → deeply nested value
            See graph.md "Nested Graph Values" for details.
        select: Outputs to return.
        session_id: Session identifier.
        max_iterations: Max iterations before InfiniteLoopError.
            None means unlimited (use with caution on graphs with cycles).
        max_concurrency: Limit total concurrent async operations across
            all nodes and nested graphs. Propagated via contextvars.
            None means unlimited.
        workflow_id: Workflow identifier. If provided with a checkpointer,
            checkpoint state is loaded and merged with values automatically.
            Steps are appended to history after execution.
        force_resume: If True, resume workflow even if graph.definition_hash
            differs from the stored workflow.graph_hash. Use when you've fixed
            a bug and want to continue an existing workflow. Data integrity
            is not guaranteed when force resuming with a changed graph.
            Default: False (strict mode - raises VersionMismatchError).
        interrupt_handlers: Map of interrupt names to handler functions.
            If all interrupts have handlers, runs to completion.
            Handler signature: async def handler(value) -> response
        event_processors: Additional processors for this run only.
            Appended to runner's processors, not replacing them.

    Returns:
        RunResult with values and status.

    Raises:
        VersionMismatchError: If workflow exists and graph.definition_hash
            differs from stored graph_hash (unless force_resume=True).
            The error message includes both hashes and suggests how to fix.
    """
```

### iter()

```python
def iter(
    self,
    graph: Graph,
    values: dict[str, Any],
    *,
    session_id: str | None = None,
    max_iterations: int | None = None,
    max_concurrency: int | None = None,
    workflow_id: str | None = None,
    force_resume: bool = False,
    event_processors: list[EventProcessor] | None = None,
) -> AsyncContextManager[RunHandle]:
    """
    Execute graph and yield events via context manager.

    Same execution semantics as run(): load → check hash → merge → execute → append.

    Args:
        graph: Graph to execute.
        values: Input values. Merged with checkpoint state if workflow_id exists.
        session_id: Session identifier.
        max_iterations: Max iterations before InfiniteLoopError.
            None means unlimited (use with caution on graphs with cycles).
        max_concurrency: Limit total concurrent async operations.
        workflow_id: Workflow identifier. Checkpoint state loaded and merged
            with values automatically.
        force_resume: If True, resume even if graph hash changed. See run().
        event_processors: Additional processors for this run only.
            Appended to runner's processors, not replacing them.

    Returns:
        AsyncContextManager yielding a RunHandle that is async-iterable
        and provides respond() for interrupts and result access.

    Example:
        async with runner.iter(graph, values={...}) as run:
            async for event in run:
                if isinstance(event, InterruptEvent):
                    run.respond(event.response_param, user_response)
            result = run.result  # RunResult after iteration
    """
```

### RunHandle

The handle returned by `iter()` context manager:

```python
class RunHandle:
    """Handle for streaming graph execution with interrupt support."""

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

### map()

```python
async def map(
    self,
    graph: Graph,
    values: dict[str, Any],
    *,
    map_over: str | list[str],
    map_mode: Literal["zip", "product"] = "zip",
    select: list[str] | None = None,
    max_concurrency: int | None = None,
) -> list[dict[str, Any]]:
    """
    Execute graph for each item with controlled concurrency.

    Args:
        graph: Graph to execute.
        values: Input values.
        map_over: Parameter(s) to iterate. REQUIRED.
        map_mode: "zip" (parallel) or "product" (cartesian).
        select: Outputs to return.
        max_concurrency: Limit total concurrent async operations across
            all items, nodes, and nested graphs. None means unlimited.

    Returns:
        List of output dicts.

    Raises:
        GraphConfigError: If graph contains interrupts.
    """
```

---

## DBOSAsyncRunner

For DBOS integration with automatic crash recovery. See [Durable Execution](./durable-execution.md) for concepts and usage patterns.

### Constructor

```python
class DBOSAsyncRunner(BaseRunner):
    def __init__(self) -> None:
        """
        Create DBOS-integrated async runner.

        IMPORTANT: User must configure DBOS separately before creating this runner:
            from dbos import DBOS
            DBOS(config={"name": "my_app", "database_url": "..."})

        Note:
            - No database_url parameter — user configures DBOS directly
            - No checkpointer parameter — DBOS handles persistence
            - No event_processors parameter — use DBOS observability
            - User must call DBOS.launch() for automatic crash recovery
        """
```

### run()

```python
async def run(
    self,
    graph: Graph,
    values: dict[str, Any],
    *,
    select: list[str] | None = None,
    session_id: str | None = None,
    max_iterations: int | None = None,
    max_concurrency: int | None = None,
    workflow_id: str,
) -> RunResult:
    """
    Execute graph with DBOS durability.

    Under the hood:
    - Graph execution is wrapped as a DBOS workflow
    - All nodes are wrapped with @DBOS.step (outputs persisted)
    - InterruptNode maps to DBOS.recv()

    Args:
        graph: Graph to execute.
        values: Input values.
        select: Outputs to return.
        session_id: Session identifier.
        max_iterations: Max iterations before InfiniteLoopError.
        max_concurrency: Limit total concurrent async operations.
        workflow_id: REQUIRED. Unique workflow identifier for DBOS.

    Returns:
        RunResult with values and status.

    Note:
        - To resume an interrupted workflow, use DBOS.send() directly:
            DBOS.send(destination_id="workflow_id", message={...}, topic="interrupt_name")
        - Do NOT call runner.run() again to resume — DBOS handles this
        - No event_processors — use DBOS observability for workflow tracking
    """
```

### Capabilities

```python
@dataclass
class DBOSRunnerCapabilities(RunnerCapabilities):
    supports_cycles: bool = True
    supports_gates: bool = True
    supports_interrupts: bool = True
    supports_async_nodes: bool = True
    supports_streaming: bool = False  # .iter() not available
    supports_events: bool = False     # Use DBOS observability
    supports_distributed: bool = False
    returns_coroutine: bool = True

    # DBOS-specific
    supports_automatic_recovery: bool = True
    supports_workflow_fork: bool = True
    supports_durable_sleep: bool = True
    supports_durable_queues: bool = True
```

### get_dbos_workflow()

```python
def get_dbos_workflow(self, graph: Graph) -> Callable:
    """
    Get the DBOS workflow function that wraps this graph.

    Useful for advanced DBOS features like queues and scheduling:

    Example:
        workflow_fn = runner.get_dbos_workflow(graph)

        # Use with queues
        queue = Queue("processing", concurrency=10)
        handle = queue.enqueue(workflow_fn, {"query": "hello"})

        # Use with scheduling
        @DBOS.scheduled('0 9 * * *')
        @DBOS.workflow()
        def daily_job(scheduled_time, actual_time):
            workflow_fn({"report_date": scheduled_time.date()})
    """
```

---

## DaftRunner

### Constructor

```python
class DaftRunner(BaseRunner):
    def __init__(
        self,
        *,
        cache: Cache | None = None,
    ) -> None:
        """
        Create distributed runner using Daft.

        Args:
            cache: Cache backend. Note: MemoryCache is per-worker.

        Note:
            DaftRunner only supports DAG graphs.
            Cycles, gates, and interrupts are not supported.

            No event_processors parameter: DaftRunner does not emit
            hypergraph events. Daft controls distributed execution
            internally; use Daft's native observability instead.
        """
```

### map()

```python
def map(
    self,
    graph: Graph,
    values: dict[str, Any],
    *,
    map_over: str | list[str],
    map_mode: Literal["zip", "product"] = "zip",
    select: list[str] | None = None,
) -> "daft.DataFrame":
    """
    Execute graph distributed using Daft DataFrames.

    Args:
        graph: Must be DAG (no cycles).
        values: Input values.
        map_over: Parameter(s) to distribute. REQUIRED.
        map_mode: "zip" (parallel) or "product" (cartesian).
        select: Outputs to return.

    Returns:
        Daft DataFrame with results.

    Raises:
        IncompatibleRunnerError: If graph has cycles/gates/interrupts.
    """
```

---

## PauseInfo

Pause details (only present when `RunResult.status == PAUSED`):

```python
@dataclass
class PauseInfo:
    reason: PauseReason      # Currently only HUMAN_INPUT
    node_name: str           # Path to node that paused (uses "/" separator)
    response_param: str      # Local parameter name from InterruptNode
    value: Any               # Value to show user

    @property
    def response_key(self) -> str:
        """Namespaced key for values dict (uses "." separator).

        Examples:
            - Top-level: "decision"
            - Nested: "review.decision" (from node_name="review/approval")
        """
```

**Resume pattern:**
```python
if result.pause:
    response = get_user_input(result.pause.value)
    result = await runner.run(
        graph,
        values={result.pause.response_key: response},  # Uses "." for nested
        workflow_id=result.workflow_id,
    )
```

---

## RunResult

Returned by `AsyncRunner.run()`:

```python
@dataclass
class RunResult:
    values: dict[str, Any | "RunResult"]  # Output name → value (or nested RunResult)
    status: RunStatus             # COMPLETED, PAUSED, STOPPED, or FAILED
    workflow_id: str | None       # For persistence/resume (if checkpointer configured)
    run_id: str                   # Unique run identifier
    pause: PauseInfo | None = None  # Pause details (only set when paused)
```

See [Execution Types](./execution-types.md#runresult) for full documentation.

---

## Event Types

Events yielded by `AsyncRunner.iter()` and sent to `EventProcessor` instances.

**All events include span hierarchy fields** for nested graph support:
- `run_id: str` - Unique per `.run()` invocation
- `span_id: str` - Unique per node execution
- `parent_span_id: str | None` - Links to parent span (None for root nodes)
- `timestamp: float` - Unix timestamp

See [Execution Types](./execution-types.md#event-types) for detailed documentation and [Observability](./observability.md) for integration patterns.

### RunStartEvent

```python
@dataclass
class RunStartEvent:
    run_id: str
    span_id: str
    parent_span_id: str | None
    session_id: str | None
    inputs: dict[str, Any]
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
    cached: bool      # True if loaded from cache
    replayed: bool    # True if loaded from checkpoint
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

### CacheHitEvent

```python
@dataclass
class CacheHitEvent:
    run_id: str
    span_id: str
    parent_span_id: str | None
    node_name: str
    timestamp: float
```

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
    error: Exception        # The raised exception
    error_type: str         # e.g., "ValueError"
    timestamp: float
```

### InterruptEvent

```python
@dataclass
class InterruptEvent:
    run_id: str
    span_id: str
    parent_span_id: str | None
    workflow_id: str        # Use this to resume
    interrupt_name: str
    value: Any              # Value to show user
    response_param: str     # Where to put response
    timestamp: float
```

See [Execution Types](./execution-types.md#interruptevent) for full documentation.

### RunEndEvent

```python
@dataclass
class RunEndEvent:
    run_id: str
    span_id: str
    parent_span_id: str | None
    outputs: dict[str, Any]
    duration_ms: float
    iterations: int
    timestamp: float
```

---

## Shared Validation

### validate_map_compatible()

Used internally by all runners before `.map()` operations:

```python
def validate_map_compatible(graph: Graph, context: str) -> None:
    """
    Validate graph is compatible with map operations.

    Args:
        graph: Graph to validate.
        context: Description for error messages.

    Raises:
        GraphConfigError: If graph contains interrupts.

    Called from:
        - BaseRunner.map() at call time
        - Graph._validate() for GraphNodes with map_over at build time
    """
    if graph.has_interrupts:
        raise GraphConfigError(
            f"{context}, but the graph contains interrupts.\n\n"
            f"The problem: map runs the graph multiple times in batch,\n"
            f"but interrupts pause for human input - these don't mix.\n\n"
            f"How to fix:\n"
            f"  Use runner.run() in a loop instead of map:\n"
            f"    for item in items:\n"
            f"        result = runner.run(graph, values={{...item...}})\n"
            f"        if result.pause:\n"
            f"            # Handle interrupt\n"
        )
```

---

## Cross-Runner Execution (Nested Graphs)

Two things happen when a nested graph has a different runner:

### 1. Compatibility Validation

First, validate that the nested runner can handle the nested graph's features:

```python
def validate_runner_compatibility(graph: Graph, runner: BaseRunner) -> None:
    """
    Called at:
    - .as_node(runner=X) for explicit runners (build time)
    - runner.run() for inherited runners (runtime, recursive)
    """
    caps = runner.capabilities

    if graph.has_async_nodes and not caps.supports_async_nodes:
        raise IncompatibleRunnerError(
            f"Graph has async nodes, but {runner.__class__.__name__} "
            f"doesn't support async nodes."
        )

    if graph.has_cycles and not caps.supports_cycles:
        raise IncompatibleRunnerError(...)

    if graph.has_gates and not caps.supports_gates:
        raise IncompatibleRunnerError(...)

    if graph.has_interrupts and not caps.supports_interrupts:
        raise IncompatibleRunnerError(...)

    # Recurse into nested graphs
    for nested in graph.nested_graphs:
        nested_runner = nested.explicit_runner or runner  # Inherit if not explicit
        validate_runner_compatibility(nested.graph, nested_runner)
```

**Example:** DaftRunner with a nested graph containing interrupts → `IncompatibleRunnerError`

### 2. Execution Strategy

Then, determine *how* to call the nested runner based on `returns_coroutine`:

```python
def get_execution_strategy(parent: BaseRunner, nested: BaseRunner) -> ExecutionStrategy:
    parent_async = parent.capabilities.returns_coroutine
    nested_async = nested.capabilities.returns_coroutine

    match (parent_async, nested_async):
        case (True, True):   return ExecutionStrategy.AWAIT
        case (True, False):  return ExecutionStrategy.THREAD_POOL
        case (False, True):  return ExecutionStrategy.ASYNCIO_RUN
        case (False, False): return ExecutionStrategy.DIRECT
```

| Parent `returns_coroutine` | Nested `returns_coroutine` | Strategy |
|:--------------------------:|:--------------------------:|----------|
| ✅ | ✅ | Direct `await` |
| ✅ | ❌ | `asyncio.to_thread()` |
| ❌ | ✅ | `asyncio.run()` |
| ❌ | ❌ | Direct call |

### Runner Resolution Order

1. Explicit `runner=` on `.as_node()` (override)
2. Parent runner (inheritance)
3. `SyncRunner` (default fallback)
