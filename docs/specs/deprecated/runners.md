# Runners API Specification

**Runners execute graphs. Graphs define structure, runners handle execution.**

---

## Quick Example

```python
from hypergraph import Graph, SyncRunner, AsyncRunner, DiskCache

# Define graph (pure structure)
graph = Graph(nodes=[embed, retrieve, generate])

# Execute with different runners
runner = SyncRunner(cache=DiskCache("./cache"))
result = runner.run(graph, inputs={"query": "hello"})

# Async execution
async_runner = AsyncRunner(cache=DiskCache("./cache"))
result = await async_runner.run(graph, inputs={"query": "hello"})
```

---

## Runner Compatibility Matrix

| Feature | `SyncRunner` | `AsyncRunner` | `DaftRunner` |
|---------|----------|---------------|--------------|
| DAG execution | ✅ | ✅ | ✅ |
| Cycles | ✅ | ✅ | ❌ |
| `@branch` gates | ✅ | ✅ | ❌ |
| `@route` gates | ✅ | ✅ | ❌ |
| `InterruptNode` | ❌ | ✅ | ❌ |
| `.iter()` streaming | ❌ | ✅ | ❌ |
| `.map()` batch | ✅ | ✅ | ✅ |
| Async nodes | ❌ | ✅ | ✅ |
| Distributed execution | ❌ | ❌ | ✅ |

**Summary:**
- **SyncRunner** - Sync execution, full feature support except async and interrupts
- **AsyncRunner** - Async execution, full feature support + streaming + interrupts
- **DaftRunner** - Distributed execution for DAG-only graphs (no cycles/gates/interrupts)

---

## Async Execution Model

### Node Type Handling

**AsyncRunner** handles both `def` and `async def` nodes:

```python
# Sync node
@node(outputs="data")
def fetch_local(path: str) -> str:
    return open(path).read()

# Async node
@node(outputs="data")
async def fetch_api(url: str) -> str:
    return await httpx.get(url).text()

# Both work in same graph with AsyncRunner
graph = Graph(nodes=[fetch_local, fetch_api, process])
result = await AsyncRunner().run(graph, inputs={...})
```

### Concurrency Rules

**Independent async nodes run concurrently:**

```python
@node(outputs="a")
async def fetch_a(x: int) -> int:
    return await api_a.call(x)

@node(outputs="b")
async def fetch_b(x: int) -> int:
    return await api_b.call(x)

@node(outputs="c")
def combine(a: int, b: int) -> int:
    return a + b
```

Execution:
```
Step 1: asyncio.gather(fetch_a(), fetch_b())  # Concurrent
Step 2: combine()  # Sequential
```

**Sync nodes run sequentially** (no thread pool by default - GIL makes it pointless for CPU-bound work).

**Mixed async + sync in same generation:**
1. Async nodes gathered first (concurrent)
2. Then sync nodes run (sequential)

### Generator Handling

Generators are automatically accumulated:

```python
@node(outputs="response")
async def generate(prompt: str) -> str:
    """Generator is accumulated automatically."""
    async for chunk in llm.stream(prompt):
        yield chunk
    # Framework collects all chunks, stores final accumulated value

# In .run() mode: final value stored
result = await runner.run(graph, inputs={...})
result["response"]  # Complete accumulated response

# In .iter() mode: chunks streamed as events
async for event in runner.iter(graph, inputs={...}):
    if isinstance(event, StreamingChunkEvent):
        print(event.chunk, end="")  # Print each chunk
```

---

## Runner Architecture

### Class Hierarchy

All runners inherit from `BaseRunner` and declare their capabilities:

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

**Runner-specific methods** (not in base class):
- `AsyncRunner.iter()` - streaming events (only AsyncRunner)

### RunnerCapabilities

Each runner declares what it supports via a `RunnerCapabilities` dataclass:

```python
from dataclasses import dataclass

@dataclass
class RunnerCapabilities:
    supports_cycles: bool = True
    supports_gates: bool = True
    supports_interrupts: bool = False
    supports_async_nodes: bool = False
    supports_streaming: bool = False
    supports_distributed: bool = False
    
    def validate_graph(self, graph: Graph) -> None:
        """Raise IncompatibleRunnerError if graph uses unsupported features."""
        ...
```

**Runner capability comparison:**

| Capability | SyncRunner | AsyncRunner | DaftRunner |
|------------|------------|-------------|------------|
| `supports_cycles` | ✅ | ✅ | ❌ |
| `supports_gates` | ✅ | ✅ | ❌ |
| `supports_interrupts` | ❌ | ✅ | ❌ |
| `supports_async_nodes` | ❌ | ✅ | ✅ |
| `supports_streaming` | ❌ | ✅ | ❌ |
| `supports_distributed` | ❌ | ❌ | ✅ |

### Why This Design?

**Separate classes (not dual methods):**
1. **Clear intent** - You know at construction time whether you're in sync or async mode
2. **Type safety** - `SyncRunner.run()` returns `dict`, `AsyncRunner.run()` returns `Awaitable[RunResult]`
3. **No confusion** - No "when do I use which method?" question

**Capabilities dataclass (not scattered flags):**
1. **Discoverable** - `runner.capabilities.supports_cycles` is clear
2. **Validated** - Automatic graph compatibility checking
3. **Single source of truth** - One place declares what the runner supports

### Shared Validation Functions

#### validate_map_compatible()

Map operations and interrupts are fundamentally incompatible. This validation is shared across:
- `BaseRunner.map()` - runtime validation
- `Graph._validate()` - build-time validation for GraphNodes with `map_over`

```python
def validate_map_compatible(graph: Graph, context: str) -> None:
    """
    Validate graph is compatible with map operations.

    Args:
        graph: Graph to validate.
        context: Description of where this is being called from (for error messages).

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
            f"Interrupts found: {[n.name for n in graph.interrupt_nodes]}\n\n"
            f"How to fix:\n"
            f"  Use runner.run() in a loop instead of map:\n"
            f"    for item in items:\n"
            f"        result = runner.run(graph, inputs={{...item...}})\n"
            f"        if result.interrupted:\n"
            f"            # Handle interrupt\n"
        )
```

---

## SyncRunner (Synchronous)

### Class Definition

```python
class SyncRunner(BaseRunner):
    """Synchronous execution runner."""

    capabilities = RunnerCapabilities(
        supports_cycles=True,
        supports_gates=True,
        supports_interrupts=False,
        supports_async_nodes=False,
        supports_streaming=False,
        supports_distributed=False,
    )

### Constructor

```python
class SyncRunner:
    def __init__(
        self,
        *,
        cache: Cache | None = None,
        callbacks: list[Callback] | None = None,
    ) -> None:
        """
        Create synchronous runner.
        
        Args:
            cache: Cache backend (e.g., DiskCache, MemoryCache).
            callbacks: Observability callbacks.
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
    max_iterations: int = 1000,
) -> dict[str, Any]:
    """
    Execute graph synchronously.
    
    Args:
        graph: Graph to execute.
        inputs: Input values. For cycles, determines starting point.
        select: Output names to return. Default: all leaf outputs.
        session_id: Group related runs (for logging/tracing).
        max_iterations: Maximum iterations before InfiniteLoopError.
    
    Returns:
        Dict mapping output names to values.
    
    Raises:
        GraphConfigError: Graph structure invalid.
        ConflictError: Parallel producers conflict.
        MissingInputError: Required input not provided.
        InfiniteLoopError: Exceeded max_iterations.
        IncompatibleRunnerError: Graph has async nodes.
    
    Example:
        runner = SyncRunner(cache=DiskCache("./cache"))
        result = runner.run(graph, inputs={"query": "hello"})
        print(result["response"])
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

    This is THE primary API for batch processing. All map_over configuration
    (on GraphNode, as_node) ultimately uses the same validation logic.

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
            Map and interrupts are incompatible - use run() in a loop instead.

    Note:
        Calls validate_map_compatible() before execution.

    Example:
        # Single parameter
        results = runner.map(
            graph,
            inputs={"queries": ["q1", "q2", "q3"], "config": shared_config},
            map_over="queries",
        )
        # results = [{"response": "r1"}, {"response": "r2"}, {"response": "r3"}]

        # Multiple parameters with zip (parallel iteration)
        results = runner.map(
            graph,
            inputs={"x": [1, 2], "y": [3, 4]},
            map_over=["x", "y"],
            map_mode="zip",
        )
        # Executes with (1,3), then (2,4)

        # Multiple parameters with product (cartesian)
        results = runner.map(
            graph,
            inputs={"x": [1, 2], "y": [3, 4]},
            map_over=["x", "y"],
            map_mode="product",
        )
        # Executes with (1,3), (1,4), (2,3), (2,4)
    """
```

---

## AsyncRunner (Asynchronous)

### Class Definition

```python
class AsyncRunner(BaseRunner):
    """Asynchronous runner with full feature support."""

    capabilities = RunnerCapabilities(
        supports_cycles=True,
        supports_gates=True,
        supports_interrupts=True,
        supports_async_nodes=True,
        supports_streaming=True,
        supports_distributed=False,
    )

### Constructor

```python
class AsyncRunner:
    def __init__(
        self,
        *,
        cache: Cache | None = None,
        callbacks: list[Callback] | None = None,
    ) -> None:
        """
        Create asynchronous runner.
        
        Args:
            cache: Cache backend.
            callbacks: Observability callbacks.
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
    max_iterations: int = 1000,
    checkpoint: bytes | None = None,
) -> RunResult:
    """
    Execute graph asynchronously.
    
    Args:
        graph: Graph to execute.
        inputs: Input values.
        select: Outputs to return.
        session_id: Session identifier.
        max_iterations: Max iterations.
        checkpoint: Resume from saved state (for InterruptNode).
    
    Returns:
        RunResult with outputs and optional checkpoint.
    
    Example:
        runner = AsyncRunner()
        result = await runner.run(graph, inputs={"query": "hello"})
        
        if result.interrupted:
            # Handle interrupt, get user input
            result = await runner.run(
                graph,
                inputs={"user_decision": decision},
                checkpoint=result.checkpoint,
            )
    """
```

### RunResult

```python
@dataclass
class RunResult:
    outputs: dict[str, Any]      # Output values
    interrupted: bool            # True if stopped at InterruptNode
    checkpoint: bytes | None     # State for resume (if interrupted)
    run_id: str                  # Unique run identifier
    interrupt_name: str | None   # Name of interrupt (if interrupted)
    interrupt_value: Any | None  # Value to show user (if interrupted)
```

### iter()

```python
async def iter(
    self,
    graph: Graph,
    inputs: dict[str, Any],
    *,
    session_id: str | None = None,
    checkpoint: bytes | None = None,
) -> AsyncIterator[Event]:
    """
    Execute graph and yield events.
    
    Args:
        graph: Graph to execute.
        inputs: Input values.
        session_id: Session identifier.
        checkpoint: Resume from saved state.
    
    Yields:
        Event objects as they occur.
    
    Event types:
        - RunStartEvent: Execution beginning
        - NodeStartEvent: Node starting
        - NodeEndEvent: Node completed
        - StreamingChunkEvent: Token from generator
        - CacheHitEvent: Cache hit occurred
        - RouteDecisionEvent: Gate made decision
        - InterruptEvent: Paused for human input
        - RunEndEvent: Execution complete
    
    Example:
        async for event in runner.iter(graph, inputs=inputs):
            if isinstance(event, StreamingChunkEvent):
                print(event.chunk, end="", flush=True)
            elif isinstance(event, InterruptEvent):
                # Handle human-in-the-loop
                break
    """
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
    concurrency: int = 10,
) -> list[dict[str, Any]]:
    """
    Execute graph for each item with controlled concurrency.

    This is THE primary API for async batch processing.

    Args:
        graph: Graph to execute.
        inputs: Input values.
        map_over: Parameter(s) to iterate. REQUIRED.
        map_mode: "zip" (parallel) or "product" (cartesian). Default: "zip".
        select: Outputs to return.
        concurrency: Max concurrent executions (default: 10).

    Returns:
        List of output dicts.

    Raises:
        GraphConfigError: If graph contains interrupts.
            Map and interrupts are incompatible - use run() in a loop instead.

    Note:
        Calls validate_map_compatible() before execution.

    Example:
        results = await runner.map(
            graph,
            inputs={"query": queries},
            map_over="query",
            concurrency=100,  # High throughput
        )
    """
```

#### Concurrency Control

| Setting | Use Case |
|---------|----------|
| `concurrency=100` | High throughput |
| `concurrency=10` | Default (balanced) |
| `concurrency=1` | Debugging, strict rate limits |

**Example:**

```python
# High throughput embedding generation
results = await async_runner.map(
    embedding_graph,
    inputs={"texts": large_text_list},
    map_over="texts",
    concurrency=100,  # 100 items in flight
)

# Rate-limited API calls
results = await async_runner.map(
    api_graph,
    inputs={"queries": queries},
    map_over="queries",
    concurrency=5,  # Respect rate limit
)
```

---

## Nested Graphs and Runners

### Runner Inheritance (SyncRunner/AsyncRunner)

By default, nested graphs inherit the parent's runner:

```python
# Simple: inner inherits parent's AsyncRunner
inner = Graph(nodes=[node_a, node_b])
outer = Graph(nodes=[inner.as_node(name="inner"), other_node])

await AsyncRunner().run(outer, inputs={...})
# inner executes with AsyncRunner (inherited)
```

### Explicit Runner Override (SyncRunner/AsyncRunner)

Use `runner=` on `.as_node()` to override for specific nested graphs:

```python
# Inner uses specific runner
inner = Graph(nodes=[batch_node1, batch_node2])

outer = Graph(nodes=[
    async_node,
    inner.as_node(name="inner", runner=DaftRunner()),  # Override
])

await AsyncRunner().run(outer, inputs={...})
# outer: AsyncRunner
# inner: DaftRunner (override)
```

### Runner Resolution Order

1. Explicit `runner=` on `.as_node()` (override)
2. Parent runner (inheritance)
3. `SyncRunner` (default fallback)

### Cross-Runner Execution

| Parent Runner | Nested Runner | Execution |
|---------------|---------------|-----------|
| `AsyncRunner` | `DaftRunner` (sync) | `asyncio.to_thread()` |
| `AsyncRunner` | `AsyncRunner` | `await` nested |
| `SyncRunner` | `DaftRunner` | Direct call |
| `SyncRunner` | `AsyncRunner` | `asyncio.run()` |
| `SyncRunner` | None (inherit) + async nodes | **Error** |

### Rules

1. The `runner=` parameter on `.as_node()` is optional—use it as an escape hatch
2. If no runner specified, inherit from parent; if no parent, use `SyncRunner`
3. Inherited runner must be compatible—`SyncRunner` with async nodes in nested → runtime error
4. Sync nested runners run in thread pool when parent is async (avoids blocking event loop)

**Example:**

```python
# Mixed runners
daft_graph = Graph(nodes=[heavy_compute_nodes])
async_graph = Graph(nodes=[api_nodes])

outer = Graph(nodes=[
    preprocess,
    daft_graph.as_node(name="batch", runner=DaftRunner()),
    async_graph.as_node(name="api", runner=AsyncRunner()),
    postprocess,
])

# Outer runner determines execution mode
await AsyncRunner().run(outer, inputs={...})
```

---

## DaftRunner (Distributed)

### Class Definition

```python
class DaftRunner(BaseRunner):
    """Distributed execution runner (DAG-only)."""

    capabilities = RunnerCapabilities(
        supports_cycles=False,
        supports_gates=False,
        supports_interrupts=False,
        supports_async_nodes=True,
        supports_streaming=False,
        supports_distributed=True,
    )

### Constructor

```python
class DaftRunner:
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
        map_mode: "zip" (parallel) or "product" (cartesian). Default: "zip".
        select: Outputs to return.

    Returns:
        Daft DataFrame with results.

    Raises:
        IncompatibleRunnerError: If graph has cycles/gates/interrupts.
        GraphConfigError: If graph contains interrupts (via validate_map_compatible).

    Note:
        Calls validate_map_compatible() before execution.
        DaftRunner also validates via capabilities (no interrupts support).

    Example:
        runner = DaftRunner()
        df = runner.map(
            graph,
            inputs={"texts": large_text_list},
            map_over="texts",
        )
        results = df.collect()  # Trigger execution
    """
```

### Compatibility Validation

```python
def _validate_graph(self, graph: Graph) -> None:
    """Ensure graph is compatible with distributed execution."""
    
    if graph.has_cycles:
        raise IncompatibleRunnerError(
            "This graph has cycles, but DaftRunner doesn't support cycles.\n\n"
            f"The problem: DaftRunner uses Daft DataFrames for distributed\n"
            f"execution, which requires a DAG structure.\n\n"
            f"Cycles found: {graph.cycles}\n\n"
            f"How to fix:\n"
            f"  Option A: Use SyncRunner or AsyncRunner instead\n"
            f"            → runner = AsyncRunner(cache=...)\n"
            f"  Option B: Restructure as a DAG"
        )
    
    if graph.gates:
        raise IncompatibleRunnerError(
            "This graph has gates (@route/@branch), but DaftRunner doesn't support gates.\n\n"
            f"Gates found: {[g.name for g in graph.gates]}\n\n"
            f"How to fix:\n"
            f"  Use SyncRunner or AsyncRunner instead"
        )
    
    if graph.interrupt_nodes:
        raise IncompatibleRunnerError(
            "This graph has InterruptNodes, but DaftRunner doesn't support interrupts.\n\n"
            f"How to fix:\n"
            f"  Use AsyncRunner for human-in-the-loop workflows"
        )
```

---

## Event Types

### RunStartEvent

```python
@dataclass
class RunStartEvent:
    run_id: str
    session_id: str | None
    inputs: dict[str, Any]
    timestamp: float
```

### NodeStartEvent

```python
@dataclass
class NodeStartEvent:
    run_id: str
    node_name: str
    inputs: dict[str, Any]
    timestamp: float
```

### NodeEndEvent

```python
@dataclass
class NodeEndEvent:
    run_id: str
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
    node_name: str
    timestamp: float
```

### RouteDecisionEvent

```python
@dataclass
class RouteDecisionEvent:
    run_id: str
    gate_name: str
    decision: str  # Target node name or "END"
    timestamp: float
```

### InterruptEvent

```python
@dataclass
class InterruptEvent:
    run_id: str
    interrupt_name: str
    value: Any              # Value to show user
    response_param: str     # Where to put response
    checkpoint: bytes       # State for resume
    timestamp: float
```

### RunEndEvent

```python
@dataclass
class RunEndEvent:
    run_id: str
    outputs: dict[str, Any]
    duration_ms: float
    iterations: int
    timestamp: float
```

---

## Identity Model

```python
# session_id: User-provided, groups related runs
# run_id: Framework-generated, identifies single execution

result = await runner.run(
    graph,
    inputs={...},
    session_id="conversation-123",  # User provides
)
# result.run_id → "run-abc-456"  # Framework generates
```

Use cases:
- `session_id`: Group multi-turn conversation runs
- `run_id`: Trace/debug specific execution
