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
    def run(self, graph: Graph, inputs: dict[str, Any], **kwargs):
        """Execute graph. Return type varies by runner."""
        ...

    @abstractmethod
    def map(
        self,
        graph: Graph,
        inputs: dict[str, Any],
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
    supports_distributed: bool = False

    # Execution interface
    returns_coroutine: bool = False  # Does .run() return a coroutine?

    def validate_graph(self, graph: Graph) -> None:
        """Raise IncompatibleRunnerError if graph uses unsupported features."""
        ...
```

**Per-runner capabilities:**

| Capability | SyncRunner | AsyncRunner | DaftRunner |
|------------|:----------:|:-----------:|:----------:|
| `supports_cycles` | ✅ | ✅ | ❌ |
| `supports_gates` | ✅ | ✅ | ❌ |
| `supports_interrupts` | ❌ | ✅ | ❌ |
| `supports_async_nodes` | ❌ | ✅ | ✅ |
| `supports_streaming` | ❌ | ✅ | ❌ |
| `supports_distributed` | ❌ | ❌ | ✅ |
| `returns_coroutine` | ❌ | ✅ | ❌ |

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
    inputs: dict[str, Any],
    *,
    select: list[str] | None = None,
    session_id: str | None = None,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    """
    Execute graph synchronously.

    Args:
        graph: Graph to execute.
        inputs: Input values. For cycles, determines starting point.
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
    inputs: dict[str, Any],
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
        inputs: Input values. map_over params should be lists.
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
    inputs: dict[str, Any],
    *,
    select: list[str] | None = None,
    session_id: str | None = None,
    max_iterations: int | None = None,
    max_concurrency: int | None = None,
    workflow_id: str | None = None,
    interrupt_handlers: dict[str, Callable] | None = None,
    event_processors: list[EventProcessor] | None = None,
) -> RunResult:
    """
    Execute graph asynchronously.

    When workflow_id is provided and a checkpointer is configured:
    1. Load checkpoint state (if workflow exists)
    2. Merge with inputs (inputs win on conflicts)
    3. Execute graph
    4. Append steps to history
    5. Return result

    Args:
        graph: Graph to execute.
        inputs: Input values. Merged with checkpoint state if workflow_id exists.
        select: Outputs to return.
        session_id: Session identifier.
        max_iterations: Max iterations before InfiniteLoopError.
            None means unlimited (use with caution on graphs with cycles).
        max_concurrency: Limit total concurrent async operations across
            all nodes and nested graphs. Propagated via contextvars.
            None means unlimited.
        workflow_id: Workflow identifier. If provided with a checkpointer,
            checkpoint state is loaded and merged with inputs automatically.
            Steps are appended to history after execution.
        interrupt_handlers: Map of interrupt names to handler functions.
            If all interrupts have handlers, runs to completion.
            Handler signature: async def handler(value) -> response
        event_processors: Additional processors for this run only.
            Appended to runner's processors, not replacing them.

    Returns:
        RunResult with outputs and status.
    """
```

### iter()

```python
def iter(
    self,
    graph: Graph,
    inputs: dict[str, Any],
    *,
    session_id: str | None = None,
    max_iterations: int | None = None,
    max_concurrency: int | None = None,
    workflow_id: str | None = None,
    event_processors: list[EventProcessor] | None = None,
) -> AsyncContextManager[RunHandle]:
    """
    Execute graph and yield events via context manager.

    Same execution semantics as run(): load → merge → execute → append.

    Args:
        graph: Graph to execute.
        inputs: Input values. Merged with checkpoint state if workflow_id exists.
        session_id: Session identifier.
        max_iterations: Max iterations before InfiniteLoopError.
            None means unlimited (use with caution on graphs with cycles).
        max_concurrency: Limit total concurrent async operations.
        workflow_id: Workflow identifier. Checkpoint state loaded and merged
            with inputs automatically.
        event_processors: Additional processors for this run only.
            Appended to runner's processors, not replacing them.

    Returns:
        AsyncContextManager yielding a RunHandle that is async-iterable
        and provides respond() for interrupts and result access.

    Example:
        async with runner.iter(graph, inputs={...}) as run:
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
    inputs: dict[str, Any],
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
        inputs: Input values.
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
        """
```

### map()

```python
def map(
    self,
    graph: Graph,
    inputs: dict[str, Any],
    *,
    map_over: str | list[str],
    map_mode: Literal["zip", "product"] = "zip",
    select: list[str] | None = None,
) -> "daft.DataFrame":
    """
    Execute graph distributed using Daft DataFrames.

    Args:
        graph: Must be DAG (no cycles).
        inputs: Input values.
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
    node: str                # Name of node that paused
    response_param: str      # Key to use in inputs dict when resuming
    value: Any               # Value to show user
```

---

## RunResult

Returned by `AsyncRunner.run()`:

```python
@dataclass
class RunResult:
    outputs: dict[str, Any]       # Output values
    status: RunStatus             # COMPLETED, PAUSED, or ERROR
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
            f"        result = runner.run(graph, inputs={{...item...}})\n"
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
