# Runners API Reference

Runners execute graphs. They handle the execution loop, node scheduling, and concurrency.

- **SyncRunner** - Sequential execution for synchronous nodes
- **AsyncRunner** - Concurrent execution with async support and `max_concurrency`
- **RunResult** - Output values, status, and error information
- **map()** - Batch processing with zip or cartesian product modes

## Overview

| Runner | Async Nodes | Concurrent | Returns |
|--------|-------------|------------|---------|
| `SyncRunner` | No | No | `RunResult` |
| `AsyncRunner` | Yes | Yes | `Coroutine[RunResult]` |

## SyncRunner

Sequential execution for synchronous graphs.

```python
from hypergraph import Graph, node, SyncRunner

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

graph = Graph([double])
runner = SyncRunner()
result = runner.run(graph, {"x": 5})

print(result["doubled"])  # 10
```

### Constructor

```python
class SyncRunner:
    def __init__(self, cache: CacheBackend | None = None) -> None: ...
```

**Args:**
- `cache` — Optional [cache backend](../03-patterns/08-caching.md) for node result caching. Nodes opt in with `@node(..., cache=True)`. Supports `InMemoryCache`, `DiskCache`, or any `CacheBackend` implementation.

### run()

```python
def run(
    self,
    graph: Graph,
    values: dict[str, Any],
    *,
    select: list[str] | None = None,
    max_iterations: int | None = None,
    event_processors: list[EventProcessor] | None = None,
) -> RunResult: ...
```

Execute a graph once.

**Args:**
- `graph` - The graph to execute
- `values` - Input values as `{param_name: value}`
- `select` - Optional list of output names to return (default: all outputs)
- `max_iterations` - Max supersteps for cyclic graphs (default: 1000)
- `event_processors` - Optional list of [event processors](events.md) to observe execution

**Returns:** `RunResult` with outputs and status

**Raises:**
- `MissingInputError` - Required input not provided
- `IncompatibleRunnerError` - Graph contains async nodes

**Example:**

```python
# Basic execution
result = runner.run(graph, {"query": "What is RAG?"})

# Select specific outputs
result = runner.run(graph, values, select=["final_answer"])

# Limit iterations for cyclic graphs
result = runner.run(cyclic_graph, values, max_iterations=50)

# With progress bars
from hypergraph import RichProgressProcessor
result = runner.run(graph, values, event_processors=[RichProgressProcessor()])
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
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
) -> list[RunResult]: ...
```

Execute a graph multiple times with different inputs.

**Args:**
- `graph` - The graph to execute
- `values` - Input values. Parameters in `map_over` should be lists
- `map_over` - Parameter name(s) to iterate over
- `map_mode` - `"zip"` for parallel iteration, `"product"` for cartesian product
- `select` - Optional list of outputs to return
- `error_handling` - How to handle failures:
  - `"raise"` (default): Stop on first failure and raise the exception
  - `"continue"`: Collect all results, including failures as `RunResult` with `status=FAILED`
- `event_processors` - Optional list of [event processors](events.md) to observe execution

**Returns:** List of `RunResult`, one per iteration

**Example:**

```python
# Single parameter
results = runner.map(graph, {"x": [1, 2, 3]}, map_over="x")

# Multiple parameters with zip
results = runner.map(
    graph,
    {"a": [1, 2], "b": [10, 20]},
    map_over=["a", "b"],
    map_mode="zip",  # (1,10), (2,20)
)

# Cartesian product
results = runner.map(
    graph,
    {"a": [1, 2], "b": [10, 20]},
    map_over=["a", "b"],
    map_mode="product",  # (1,10), (1,20), (2,10), (2,20)
)

# Continue on errors — collect partial results
results = runner.map(
    graph,
    {"x": [1, 2, 3]},
    map_over="x",
    error_handling="continue",
)
successes = [r for r in results if r.status == RunStatus.COMPLETED]
failures = [r for r in results if r.status == RunStatus.FAILED]
```

### capabilities

```python
@property
def capabilities(self) -> RunnerCapabilities: ...
```

Returns capabilities for compatibility checking:

```python
runner = SyncRunner()
caps = runner.capabilities

caps.supports_cycles       # True
caps.supports_async_nodes  # False
caps.supports_streaming    # False
caps.returns_coroutine     # False
caps.supports_interrupts   # False
```

---

## AsyncRunner

Concurrent execution with async support.

```python
import asyncio
from hypergraph import Graph, node, AsyncRunner

@node(output_name="data")
async def fetch(url: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.json()

graph = Graph([fetch])
runner = AsyncRunner()
result = await runner.run(graph, {"url": "https://api.example.com"})
```

### Constructor

```python
class AsyncRunner:
    def __init__(self, cache: CacheBackend | None = None) -> None: ...
```

**Args:**
- `cache` — Optional [cache backend](../03-patterns/08-caching.md) for node result caching. Nodes opt in with `@node(..., cache=True)`.

### run()

```python
async def run(
    self,
    graph: Graph,
    values: dict[str, Any],
    *,
    select: list[str] | None = None,
    max_iterations: int | None = None,
    max_concurrency: int | None = None,
    event_processors: list[EventProcessor] | None = None,
) -> RunResult: ...
```

Execute a graph asynchronously.

**Args:**
- `graph` - The graph to execute
- `values` - Input values
- `select` - Optional list of output names to return
- `max_iterations` - Max supersteps for cyclic graphs (default: 1000)
- `max_concurrency` - Max parallel node executions (default: unlimited)
- `event_processors` - Optional list of [event processors](events.md) to observe execution (supports `AsyncEventProcessor`)

**Returns:** `RunResult` with outputs and status

**Example:**

```python
# Basic async execution
result = await runner.run(graph, {"query": "What is RAG?"})

# Limit concurrency (important for rate-limited APIs)
result = await runner.run(
    graph,
    {"prompts": prompts},
    max_concurrency=10,
)
```

### Concurrency Control

The `max_concurrency` parameter limits how many nodes execute simultaneously:

```python
# Process 100 items, but only 5 API calls at once
runner = AsyncRunner()
result = await runner.run(
    graph,
    {"items": large_list},
    max_concurrency=5,
)
```

Concurrency limits are shared across:
- All nodes in a superstep
- Nested GraphNodes
- All items in `map()` calls

This prevents overwhelming external services when processing large batches.

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
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
) -> list[RunResult]: ...
```

Execute graph multiple times concurrently.

**Args:**
- `graph` - The graph to execute
- `values` - Input values
- `map_over` - Parameter name(s) to iterate over
- `map_mode` - `"zip"` or `"product"`
- `select` - Optional list of outputs to return
- `max_concurrency` - Shared limit across all executions
- `error_handling` - How to handle failures:
  - `"raise"` (default): Stop on first failure and raise the exception
  - `"continue"`: Collect all results, including failures as `RunResult` with `status=FAILED`
- `event_processors` - Optional list of [event processors](events.md) to observe execution

**Example:**

```python
# Process documents concurrently
results = await runner.map(
    graph,
    {"doc": documents},
    map_over="doc",
    max_concurrency=20,  # Limit total concurrent operations
)

# Continue on errors with async
results = await runner.map(
    graph,
    {"doc": documents},
    map_over="doc",
    max_concurrency=20,
    error_handling="continue",
)
```

### capabilities

```python
@property
def capabilities(self) -> RunnerCapabilities: ...
```

```python
runner = AsyncRunner()
caps = runner.capabilities

caps.supports_cycles       # True
caps.supports_async_nodes  # True
caps.supports_streaming    # False (Phase 2)
caps.returns_coroutine     # True
caps.supports_interrupts   # True
```

---

## RunResult

Result of a graph execution.

```python
from hypergraph import RunResult, RunStatus

result = runner.run(graph, values)

# Access outputs (dict-like)
value = result["output_name"]
value = result.get("output_name", default)
exists = "output_name" in result

# Check status
if result.status == RunStatus.COMPLETED:
    process(result.values)
else:
    handle_error(result.error)
```

### Attributes

```python
@dataclass
class RunResult:
    values: dict[str, Any]      # Output values
    status: RunStatus           # COMPLETED, FAILED, or PAUSED
    run_id: str                 # Unique identifier (auto-generated)
    workflow_id: str | None     # Optional workflow tracking
    error: BaseException | None # Exception if FAILED
    pause: PauseInfo | None     # Pause info if PAUSED (InterruptNode)
```

### Convenience Properties

```python
result.paused     # True if status == PAUSED
result.completed  # True if status == COMPLETED
```

### Dict-like Access

```python
# These are equivalent
result["key"]
result.values["key"]

# Safe access with default
result.get("key", default_value)

# Check existence
"key" in result
```

### Partial Values on Failure

When a graph execution fails, `RunResult` preserves any outputs computed before the error:

```python
result = runner.run(graph, {"x": 5})

if result.status == RunStatus.FAILED:
    # values contains outputs from nodes that succeeded before the failure
    partial = result.values  # e.g. {"step1_output": 10}
    error = result.error     # the exception that caused the failure
```

This is useful for debugging — you can inspect which nodes completed successfully.

---

## RunStatus

Enum for execution status.

```python
from hypergraph import RunStatus

class RunStatus(Enum):
    COMPLETED = "completed"  # Success
    FAILED = "failed"        # Error occurred
    PAUSED = "paused"        # Waiting for human input (InterruptNode)
```

**Usage:**

```python
result = runner.run(graph, values)

match result.status:
    case RunStatus.COMPLETED:
        return result["output"]
    case RunStatus.PAUSED:
        print(result.pause.value)  # Value from InterruptNode
    case RunStatus.FAILED:
        raise result.error
```

---

## Errors

### MissingInputError

Raised when required inputs are not provided.

```python
from hypergraph import MissingInputError

try:
    result = runner.run(graph, {})  # Missing required input
except MissingInputError as e:
    print(e)
    # Missing required input(s): ['query']
    #
    # How to fix:
    #   Provide value for 'query' in the values dict
```

### IncompatibleRunnerError

Raised when runner can't execute graph.

```python
from hypergraph import IncompatibleRunnerError

@node(output_name="data")
async def fetch(url: str) -> dict:
    return {}

graph = Graph([fetch])

try:
    SyncRunner().run(graph, {"url": "..."})
except IncompatibleRunnerError as e:
    print(e)
    # SyncRunner cannot execute async nodes.
    # Found async node: 'fetch'
    #
    # How to fix:
    #   Use AsyncRunner instead
```

### InfiniteLoopError

Raised when cyclic graph exceeds max iterations.

```python
from hypergraph import InfiniteLoopError

try:
    result = runner.run(cyclic_graph, values, max_iterations=100)
except InfiniteLoopError as e:
    print(e)
    # Graph execution exceeded 100 iterations
```

---

## Execution Model

### Supersteps

Runners execute graphs in **supersteps**. Each superstep:

1. Finds all "ready" nodes (inputs satisfied)
2. Executes them (sequentially for Sync, concurrently for Async)
3. Updates outputs
4. Repeats until no nodes are ready

```
Superstep 1: [embed]           → produces "embedding"
Superstep 2: [retrieve]        → produces "docs"
Superstep 3: [generate]        → produces "answer"
```

### Value Resolution Order

When collecting inputs for a node, values are resolved in this order:

1. **Edge value** - Output from upstream node
2. **Input value** - Provided in `values` dict
3. **Bound value** - From `graph.bind()`
4. **Function default** - From function signature

```python
@node(output_name="result")
def process(x: int = 10) -> int:  # default=10
    return x * 2

graph = Graph([process]).bind(x=5)  # bound=5

# Edge value wins (if exists)
# Then input value: runner.run(graph, {"x": 3})  → x=3
# Then bound value: runner.run(graph, {})        → x=5
# Then default: (if no bind) runner.run(graph, {}) → x=10
```

### Cyclic Graphs

Graphs with cycles (feedback loops) execute until stable:

```python
@node(output_name="count")
def increment(count: int) -> int:
    return count + 1 if count < 5 else count

# Cycle: count feeds back into increment
# Runs until count stops changing (stability)
```

The runner:
1. Tracks value versions
2. Re-executes nodes when inputs change
3. Stops when no values changed (stable) or max_iterations hit

---

## Nested Graphs

GraphNodes (nested graphs) are executed by the same runner:

```python
inner = Graph([double], name="inner")
outer = Graph([inner.as_node(), triple])

runner = SyncRunner()
result = runner.run(outer, {"x": 5})
```

The runner automatically:
- Delegates to the inner graph
- Shares concurrency limits (AsyncRunner)
- Propagates the cache backend to nested graphs
- Propagates errors
