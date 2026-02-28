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
    values: dict[str, Any] | None = None,
    *,
    select: str | list[str] = "**",
    on_missing: Literal["ignore", "warn", "error"] = "ignore",
    on_internal_override: Literal["ignore", "warn", "error"] = "warn",
    entrypoint: str | None = None,
    max_iterations: int | None = None,
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
    **input_values: Any,
) -> RunResult: ...
```

Execute a graph once.

**Args:**
- `graph` - The graph to execute
- `values` - Optional input values as `{param_name: value}`
- `select` - Which outputs to return. `"**"` (default) returns all outputs. Pass a list of names for specific outputs. Also narrows input validation -- only inputs needed to produce the selected outputs are required. See [InputSpec](inputspec.md) for details on scope narrowing.
- `on_missing` - How to handle missing selected outputs:
  - `"ignore"` (default): silently omit missing outputs
  - `"warn"`: warn about missing outputs, return what's available
  - `"error"`: raise error if any selected output is missing
- `on_internal_override` - How to handle non-conflicting internal/unknown override-style inputs:
  - `"warn"` (default): emit warning
  - `"ignore"`: allow silently
  - `"error"`: fail fast
  - Note: contradictory compute+inject inputs for the same node always fail
- `entrypoint` - Optional explicit cycle entrypoint node name. Disambiguates when multiple entrypoints match.
- `max_iterations` - Max supersteps for cyclic graphs (default: 1000)
- `error_handling` - How to handle node execution errors:
  - `"raise"` (default): Re-raise the original exception (e.g., `ValueError`). Clean traceback, no wrapper.
  - `"continue"`: Return `RunResult` with `status=FAILED` and partial values instead of raising.
- `event_processors` - Optional list of [event processors](events.md) to observe execution
- `**input_values` - Input shorthand (merged with `values`)

**Returns:** `RunResult` with outputs and status

**Raises:**
- `MissingInputError` - Required input not provided
- `IncompatibleRunnerError` - Graph contains async nodes
- `ValueError` - If `entrypoint` is invalid or ambiguous
- Node execution errors (e.g., `ValueError`, `TypeError`) when `error_handling="raise"` (the default)

**Example:**

```python
# Basic execution — raises on failure (default)
result = runner.run(graph, {"query": "What is RAG?"})

# kwargs shorthand
result = runner.run(graph, query="What is RAG?")

# Select specific outputs (also narrows required inputs)
result = runner.run(graph, values, select=["final_answer"])

# Limit iterations for cyclic graphs
result = runner.run(cyclic_graph, values, max_iterations=50)

# Strict output checking
result = runner.run(graph, values, select=["answer"], on_missing="error")

# Explicit cycle entrypoint
result = runner.run(cyclic_graph, {"messages": []}, entrypoint="generate")

# With progress bars
from hypergraph import RichProgressProcessor
result = runner.run(graph, values, event_processors=[RichProgressProcessor()])

# Collect partial results instead of raising on failure
from hypergraph import RunStatus
result = runner.run(graph, {"x": 5}, error_handling="continue")
if result.status == RunStatus.FAILED:
    print(result.error)        # the original exception
    print(result.values)       # outputs from nodes that completed before the failure
```

### map()

```python
def map(
    self,
    graph: Graph,
    values: dict[str, Any] | None = None,
    *,
    map_over: str | list[str],
    map_mode: Literal["zip", "product"] = "zip",
    select: str | list[str] = "**",
    on_missing: Literal["ignore", "warn", "error"] = "ignore",
    on_internal_override: Literal["ignore", "warn", "error"] = "warn",
    entrypoint: str | None = None,
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
    **input_values: Any,
) -> MapResult: ...
```

Execute a graph multiple times with different inputs.

**Args:**
- `graph` - The graph to execute
- `values` - Optional input values. Parameters in `map_over` should be lists
- `map_over` - Parameter name(s) to iterate over
- `map_mode` - `"zip"` for parallel iteration, `"product"` for cartesian product
- `select` - Which outputs to return. `"**"` (default) returns all outputs.
- `on_missing` - How to handle missing selected outputs (`"ignore"`, `"warn"`, or `"error"`)
- `on_internal_override` - How to handle non-conflicting internal/unknown overrides (`"ignore"`, `"warn"`, or `"error"`)
- `entrypoint` - Optional explicit cycle entrypoint (passed to each `run()` call)
- `error_handling` - How to handle failures:
  - `"raise"` (default): Stop on first failure and raise the exception
  - `"continue"`: Collect all results, including failures as `RunResult` with `status=FAILED`
- `event_processors` - Optional list of [event processors](events.md) to observe execution
- `**input_values` - Input shorthand (merged with `values`)

**Returns:** [`MapResult`](#mapresult) wrapping per-iteration RunResults with batch metadata

**Example:**

```python
# Single parameter
results = runner.map(graph, {"x": [1, 2, 3]}, map_over="x")

# kwargs shorthand
results = runner.map(graph, map_over="x", x=[1, 2, 3])

# Batch-level metadata
print(results.summary())   # "3 items | 3 completed | 12ms"
print(results["doubled"])  # [2, 4, 6] — collect values across items

# Multiple parameters with zip
results = runner.map(
    graph,
    {"a": [1, 2], "b": [10, 20]},
    map_over=["a", "b"],
    map_mode="zip",  # (1,10), (2,20)
)

# Continue on errors — aggregate status
results = runner.map(
    graph,
    {"x": [1, 2, 3]},
    map_over="x",
    error_handling="continue",
)
if results.failed:
    print(f"{len(results.failures)} items failed")
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
    values: dict[str, Any] | None = None,
    *,
    select: str | list[str] = "**",
    on_missing: Literal["ignore", "warn", "error"] = "ignore",
    on_internal_override: Literal["ignore", "warn", "error"] = "warn",
    entrypoint: str | None = None,
    max_iterations: int | None = None,
    max_concurrency: int | None = None,
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
    **input_values: Any,
) -> RunResult: ...
```

Execute a graph asynchronously.

**Args:**
- `graph` - The graph to execute
- `values` - Optional input values
- `select` - Which outputs to return. `"**"` (default) returns all outputs. Also narrows input validation to only what's needed for the selected outputs.
- `on_missing` - How to handle missing selected outputs (`"ignore"`, `"warn"`, or `"error"`)
- `on_internal_override` - How to handle non-conflicting internal/unknown overrides (`"ignore"`, `"warn"`, or `"error"`). Contradictory compute+inject inputs always fail.
- `entrypoint` - Optional explicit cycle entrypoint node name
- `max_iterations` - Max supersteps for cyclic graphs (default: 1000)
- `max_concurrency` - Max parallel node executions (default: unlimited)
- `error_handling` - How to handle node execution errors:
  - `"raise"` (default): Re-raise the original exception. Clean traceback, no wrapper.
  - `"continue"`: Return `RunResult` with `status=FAILED` and partial values instead of raising.
- `event_processors` - Optional list of [event processors](events.md) to observe execution (supports `AsyncEventProcessor`)
- `**input_values` - Input shorthand (merged with `values`)

**Returns:** `RunResult` with outputs and status

**Example:**

```python
# Basic async execution
result = await runner.run(graph, {"query": "What is RAG?"})

# kwargs shorthand
result = await runner.run(graph, query="What is RAG?")

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
    values: dict[str, Any] | None = None,
    *,
    map_over: str | list[str],
    map_mode: Literal["zip", "product"] = "zip",
    select: str | list[str] = "**",
    on_missing: Literal["ignore", "warn", "error"] = "ignore",
    on_internal_override: Literal["ignore", "warn", "error"] = "warn",
    entrypoint: str | None = None,
    max_concurrency: int | None = None,
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
    **input_values: Any,
) -> MapResult: ...
```

Execute graph multiple times concurrently.

**Args:**
- `graph` - The graph to execute
- `values` - Optional input values
- `map_over` - Parameter name(s) to iterate over
- `map_mode` - `"zip"` or `"product"`
- `select` - Which outputs to return. `"**"` (default) returns all outputs.
- `on_missing` - How to handle missing selected outputs (`"ignore"`, `"warn"`, or `"error"`)
- `on_internal_override` - How to handle non-conflicting internal/unknown overrides (`"ignore"`, `"warn"`, or `"error"`)
- `entrypoint` - Optional explicit cycle entrypoint (passed to each `run()` call)
- `max_concurrency` - Shared limit across all executions
- `error_handling` - How to handle failures:
  - `"raise"` (default): Stop on first failure and raise the exception
  - `"continue"`: Collect all results, including failures as `RunResult` with `status=FAILED`
- `event_processors` - Optional list of [event processors](events.md) to observe execution
- `**input_values` - Input shorthand (merged with `values`)

**Example:**

```python
# Process documents concurrently
results = await runner.map(
    graph,
    {"doc": documents},
    map_over="doc",
    max_concurrency=20,  # Limit total concurrent operations
)

# kwargs shorthand
results = await runner.map(graph, map_over="doc", doc=documents)

# Continue on errors with async
results = await runner.map(
    graph,
    {"doc": documents},
    map_over="doc",
    max_concurrency=20,
    error_handling="continue",
)
```

For very large batches, prefer setting `max_concurrency` explicitly. If `max_concurrency=None`
and the fan-out is extremely large, `AsyncRunner.map()` raises `ValueError` to avoid unbounded
task creation.

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
result.completed  # True if status == COMPLETED
result.paused     # True if status == PAUSED
result.failed     # True if status == FAILED
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

By default, `run()` raises the original exception on node failure. To get partial results instead, use `error_handling="continue"`:

```python
# Use error_handling="continue" to get partial results instead of raising
result = runner.run(graph, {"x": 5}, error_handling="continue")

if result.status == RunStatus.FAILED:
    # values contains outputs from nodes that succeeded before the failure
    partial = result.values  # e.g. {"step1_output": 10}
    error = result.error     # the exception that caused the failure
```

This is useful for debugging — you can inspect which nodes completed successfully.

### Progressive Disclosure

```python
# One-line summary
result.summary()   # "3 nodes | 12ms | 0 errors | slowest: generate (8ms)"

# JSON-serializable metadata (no raw values or exception objects)
result.to_dict()   # {"status": "completed", "run_id": "run-abc", "log": {...}}
```

---

## MapResult

Result of a batch `map()` execution. Wraps individual `RunResult` items with batch-level metadata.

Supports read-only sequence protocol — `len()`, `iter()`, `[int]` work; mutable list ops do not.

```python
from hypergraph import MapResult

results = runner.map(graph, {"x": [1, 2, 3]}, map_over="x")

# Sequence protocol (backward compatible)
len(results)     # 3
results[0]       # RunResult
for r in results: ...

# String key access — collect values across items
results["doubled"]           # [2, 4, 6]
results.get("doubled", 0)   # [2, 4, 6] (with default for missing)

# Aggregate status
results.status       # RunStatus.COMPLETED (or FAILED if any failed)
results.completed    # True if all completed
results.failed       # True if any failed
results.failures     # List of failed RunResult items

# Progressive disclosure
results.summary()    # "3 items | 3 completed | 12ms"
results.to_dict()    # JSON-serializable batch metadata + per-item results
```

### Attributes

```python
@dataclass(frozen=True)
class MapResult:
    results: tuple[RunResult, ...]   # Individual results
    run_id: str | None               # None for empty maps
    total_duration_ms: float         # Wall-clock batch time
    map_over: tuple[str, ...]        # Parameter names iterated
    map_mode: str                    # "zip" or "product"
    graph_name: str                  # Name of the executed graph
```

### Status Precedence

`FAILED > PAUSED > COMPLETED`. If any item failed, the batch status is FAILED. Empty batch → COMPLETED.

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
# With error_handling="continue", check the status to handle failures
result = runner.run(graph, values, error_handling="continue")

match result.status:
    case RunStatus.COMPLETED:
        return result["output"]
    case RunStatus.PAUSED:
        print(result.pause.value)  # Value from InterruptNode
    case RunStatus.FAILED:
        raise result.error  # Re-raise manually if needed
```

With the default `error_handling="raise"`, node failures raise before returning a `RunResult`, so the status will be `COMPLETED` or `PAUSED`.

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

### Input Normalization

Runners accept inputs in two equivalent ways:

```python
# explicit dict
runner.run(graph, values={"query": "hello", "llm": llm})

# kwargs shorthand
runner.run(graph, query="hello", llm=llm)
```

Rules:
- `values` + kwargs are merged
- duplicate keys raise `ValueError`
- option names like `select`, `map_over`, `max_concurrency` are reserved for runner options
- reserved option names in kwargs raise `ValueError`
- if an input name matches an option name, pass that input through `values={...}`

```python
# input named "select" must go through values
runner.run(graph, values={"select": "fast"}, select=["answer"])
```

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
2. **Input value** - Provided via `values` or kwargs shorthand
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
