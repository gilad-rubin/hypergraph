# Runners

**Graphs define structure. Runners execute them.**

A `Graph` is a pure data structure describing computation flow. A `Runner` takes that structure and actually executes it—handling scheduling, concurrency, caching, and error propagation.

---

## The Four Runners

hypergraph provides four runners, each optimized for different execution contexts:

| Runner | Description | Primary Use Case |
|--------|-------------|------------------|
| **SyncRunner** | Synchronous, blocking execution | Scripts, CLI tools, simple pipelines |
| **AsyncRunner** | Async execution with full feature support | Web APIs, streaming, human-in-the-loop |
| **DBOSAsyncRunner** | AsyncRunner with DBOS durability | Production with automatic crash recovery |
| **DaftRunner** | Distributed execution via Daft DataFrames | Large-scale batch processing |

**Note:** `DBOSAsyncRunner` extends `AsyncRunner` with DBOS-powered durability. See [Durable Execution](durable-execution.md) for details.

---

## Basic Usage

### SyncRunner

```python
from hypergraph import Graph, SyncRunner, DiskCache

graph = Graph(nodes=[fetch, process, save])

runner = SyncRunner(cache=DiskCache("./cache"))
result = runner.run(graph, inputs={"query": "hello"})

print(result["response"])  # dict[str, Any]
```

### AsyncRunner

```python
from hypergraph import Graph, AsyncRunner

graph = Graph(nodes=[fetch, process, save])

runner = AsyncRunner(cache=DiskCache("./cache"))
result = await runner.run(graph, values={"query": "hello"})

print(result.values["response"])  # Access output value
```

### DaftRunner

```python
from hypergraph import Graph, DaftRunner

graph = Graph(nodes=[embed, process])  # Must be a DAG

runner = DaftRunner()
df = runner.map(
    graph,
    inputs={"texts": large_text_list},
    map_over="texts",
)
results = df.collect()  # Execution happens here
```

---

## Choosing the Right Runner

```
                    ┌─────────────────────────┐
                    │  What do you need?      │
                    └───────────┬─────────────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
              ▼                 ▼                 ▼
        ┌───────────┐    ┌─────────────┐   ┌─────────────┐
        │ Blocking  │    │ Streaming,  │   │ Distributed │
        │ execution │    │ interrupts, │   │ batch       │
        │ sync nodes│    │ async nodes │   │ processing  │
        └─────┬─────┘    └──────┬──────┘   └──────┬──────┘
              │                 │                 │
              ▼                 ▼                 ▼
        ┌───────────┐    ┌─────────────┐   ┌─────────────┐
        │SyncRunner │    │ AsyncRunner │   │ DaftRunner  │
        └───────────┘    └─────────────┘   └─────────────┘
```

### Decision Guide

**SyncRunner** when:
- Building scripts or CLI tools
- All nodes are synchronous (`def`, not `async def`)
- No need for streaming output or interrupts
- Simplest mental model is preferred

**AsyncRunner** when:
- Nodes use `async def` (API calls, database queries)
- Streaming LLM responses token-by-token
- Human-in-the-loop workflows with `InterruptNode`
- Building web APIs or interactive applications

**DaftRunner** when:
- Processing large batches (thousands+ items)
- Graph is a pure DAG (no cycles, no conditional routing)
- Want distributed execution across workers
- Willing to trade flexibility for scale

### Feature Compatibility Matrix

| Feature | SyncRunner | AsyncRunner | DBOSAsyncRunner | DaftRunner |
|---------|:----------:|:-----------:|:---------------:|:----------:|
| DAG execution | ✅ | ✅ | ✅ | ✅ |
| Cycles (loops) | ✅ | ✅ | ✅ | ❌ |
| `@branch` / `@route` gates | ✅ | ✅ | ✅ | ❌ |
| `InterruptNode` | ❌ | ✅ | ✅ | ❌ |
| `.iter()` streaming | ❌ | ✅ | ✅ | ❌ |
| `.map()` batch | ✅ | ✅ | ✅ | ✅ |
| Async nodes (`async def`) | ❌ | ✅ | ✅ | ✅ |
| Distributed execution | ❌ | ❌ | ❌ | ✅ |
| Automatic crash recovery | ❌ | ❌ | ✅ | ❌ |
| Durable sleep/scheduling | ❌ | ❌ | ✅ | ❌ |

---

## Batch Processing with `.map()`

All runners support `.map()` for processing multiple inputs:

```python
# Process a list of queries
results = runner.map(
    graph,
    inputs={"queries": ["q1", "q2", "q3"], "config": shared_config},
    map_over="queries",  # This parameter gets iterated
)
# Returns: [{"response": "r1"}, {"response": "r2"}, {"response": "r3"}]
```

The `map_over` parameter specifies which input(s) to iterate. Other inputs are broadcast to all executions.

### Multiple Parameters

**zip mode** (default): Iterate parameters in parallel (must have equal lengths)

```python
results = runner.map(
    graph,
    inputs={"x": [1, 2], "y": [3, 4]},
    map_over=["x", "y"],
    map_mode="zip",
)
# Executes: (x=1, y=3), (x=2, y=4)
```

**product mode**: Cartesian product of all combinations

```python
results = runner.map(
    graph,
    inputs={"x": [1, 2], "y": [3, 4]},
    map_over=["x", "y"],
    map_mode="product",
)
# Executes: (x=1, y=3), (x=1, y=4), (x=2, y=3), (x=2, y=4)
```

### Map and Interrupts Are Incompatible

Map operations batch multiple executions, but interrupts pause for human input—these don't mix. Attempting to `.map()` a graph containing `InterruptNode` raises `GraphConfigError`:

```python
# This will raise GraphConfigError
runner.map(graph_with_interrupts, inputs={...}, map_over="x")

# Instead, use run() in a loop:
for item in items:
    result = await runner.run(graph, inputs={...})
    if result.pause:
        # Handle interrupt individually
```

---

## Streaming with `.iter()` (AsyncRunner Only)

Watch execution unfold in real-time:

```python
async for event in runner.iter(graph, inputs={"prompt": "Tell me a story"}):
    match event:
        case StreamingChunkEvent(chunk=chunk):
            print(chunk, end="", flush=True)
        case NodeEndEvent(node_name=name, duration_ms=ms):
            print(f"\n[{name} completed in {ms:.1f}ms]")
        case InterruptEvent():
            # Handle human-in-the-loop
            break
```

### Event Types

| Event | When Emitted |
|-------|--------------|
| `RunStartEvent` | Execution begins |
| `NodeStartEvent` | Node begins execution |
| `NodeEndEvent` | Node completes (includes duration, cache status) |
| `StreamingChunkEvent` | Generator yields a chunk |
| `CacheHitEvent` | Node result retrieved from cache |
| `RouteDecisionEvent` | Gate makes routing decision |
| `InterruptEvent` | Execution paused for human input |
| `RunEndEvent` | Execution completes |

---

## Human-in-the-Loop with Interrupts (AsyncRunner Only)

Pause execution for human input and resume:

```python
from hypergraph import AsyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

runner = AsyncRunner(checkpointer=SqliteCheckpointer("./dev.db"))

result = await runner.run(
    graph,
    inputs={"draft": content},
    workflow_id="review-123",
)

if result.pause:
    # Show the value to the user
    print(f"Review needed: {result.pause.value}")
    user_decision = await get_user_approval()

    # Resume using same workflow_id (checkpointer auto-detects paused state)
    result = await runner.run(
        graph,
        values={result.pause.response_param: user_decision},
        workflow_id="review-123",
    )

print(result.values["final_result"])
```

**See [Execution Types](execution-types.md)** for `RunResult`, `RunStatus`, and `PauseReason` definitions.

---

## Async Execution Model

### Node Type Handling

AsyncRunner handles both `def` and `async def` nodes in the same graph:

```python
@node(output_name="data")
def fetch_local(path: str) -> str:
    return open(path).read()  # Sync node

@node(output_name="data")
async def fetch_api(url: str) -> str:
    return await httpx.get(url).text()  # Async node

# Both work together
graph = Graph(nodes=[fetch_local, fetch_api, process])
result = await AsyncRunner().run(graph, inputs={...})
```

### Concurrency Rules

**Independent async nodes run concurrently:**

```python
@node(output_name="a")
async def fetch_a(x: int) -> int:
    return await api_a.call(x)

@node(output_name="b")
async def fetch_b(x: int) -> int:
    return await api_b.call(x)

@node(output_name="c")
def combine(a: int, b: int) -> int:
    return a + b
```

Execution order:
```
Step 1: asyncio.gather(fetch_a(), fetch_b())  # Concurrent
Step 2: combine()                              # After both complete
```

**Sync nodes run sequentially.** No thread pool by default—the GIL makes threading pointless for CPU-bound work.

**Mixed async + sync in same generation:**
1. Async nodes gathered first (concurrent)
2. Then sync nodes execute (sequential)

### Generator Handling

Generators are automatically accumulated by the framework:

```python
@node(output_name="response")
async def stream_llm(prompt: str) -> str:
    async for chunk in llm.stream(prompt):
        yield chunk
    # Framework collects all chunks into final value
```

**In `.run()` mode:** Final accumulated value is stored

```python
result = await runner.run(graph, values={...})
result.values["response"]  # Complete text
```

**In `.iter()` mode:** Each chunk is emitted as an event

```python
async for event in runner.iter(graph, values={...}):
    if isinstance(event, StreamingChunkEvent):
        print(event.chunk, end="")  # Real-time streaming
```

### Concurrency Control

#### `max_concurrency` Parameter

Limit the total number of concurrent async operations across the entire execution:

```python
# Limit total concurrent operations (across all nodes, all levels)
result = await runner.run(graph, inputs={...}, max_concurrency=10)

# Also works with map
results = await runner.map(graph, inputs={...}, map_over="x", max_concurrency=20)
```

This limit is **shared across all levels** of execution:
- All items in a `.map()` call
- All independent async nodes within each execution
- All nested graphs

```
runner.run(graph, max_concurrency=10)
│
├── Node A (async) ───── acquires slot ────┐
├── Node B (async) ───── acquires slot ────┤  All share
├── Nested graph:                          │  the same
│   ├── Node C (async) ── acquires slot ───┤  10 slots
│   └── Node D (async) ── acquires slot ───┘
```

The limiter is propagated via `contextvars`, so nested graphs automatically respect the parent's limit.

#### Why One Parameter?

Previous designs had separate `concurrency` (map-level) and node-level limits. This led to surprising multiplication effects. A single `max_concurrency` is easier to reason about: "at most N operations in flight at once."

---

## Nested Graphs and Runner Inheritance

### Default: Inherit Parent Runner

Nested graphs inherit their parent's runner by default:

```python
inner = Graph(nodes=[node_a, node_b])
outer = Graph(nodes=[inner.as_node(name="inner"), other_node])

await AsyncRunner().run(outer, inputs={...})
# inner executes with AsyncRunner (inherited)
```

### Override with Explicit Runner

Use `runner=` on `.as_node()` to override:

```python
inner = Graph(nodes=[batch_nodes])

outer = Graph(nodes=[
    preprocess,
    inner.as_node(name="batch", runner=DaftRunner()),  # Override
    postprocess,
])

await AsyncRunner().run(outer, inputs={...})
# outer: AsyncRunner
# inner: DaftRunner (explicit override)
```

### Resolution Order

1. Explicit `runner=` on `.as_node()` (highest priority)
2. Parent runner (inheritance)
3. `SyncRunner` (default fallback)

### Cross-Runner Execution

When nested graphs use different runners, two things happen:

#### 1. Compatibility Validation

The runner's capabilities are checked against the graph's features:

```python
# This will fail at .as_node() time
inner = Graph(nodes=[node_with_interrupt])
outer = Graph(nodes=[
    inner.as_node(name="inner", runner=DaftRunner()),  # Error!
])
# IncompatibleRunnerError: DaftRunner doesn't support interrupts
```

Validation checks:
- `supports_async_nodes` vs graph's async nodes
- `supports_cycles` vs graph's cycles
- `supports_gates` vs graph's gates
- `supports_interrupts` vs graph's interrupt nodes

This validation happens recursively for all nested graphs.

#### 2. Execution Strategy

Then, *how* to call the nested runner is derived from `returns_coroutine`:

| Parent `returns_coroutine` | Nested `returns_coroutine` | Strategy |
|:--------------------------:|:--------------------------:|----------|
| ✅ | ✅ | Direct `await` |
| ✅ | ❌ | `asyncio.to_thread()` |
| ❌ | ✅ | `asyncio.run()` |
| ❌ | ❌ | Direct call |

Adding a new runner only requires declaring its capabilities—no need to update a matrix of combinations.

**Key rules:**
- Sync runners in async context run via thread pool (avoids blocking event loop)
- Inherited runner must be compatible with graph features
- Incompatible combinations fail with clear `IncompatibleRunnerError`

---

## DaftRunner: Distributed Execution

DaftRunner uses [Daft](https://www.getdaft.io/) DataFrames for distributed batch processing.

### Constraints

DaftRunner only supports **DAG graphs**:

| Feature | Supported |
|---------|:---------:|
| Linear pipelines | ✅ |
| Parallel branches | ✅ |
| Async nodes | ✅ |
| Cycles | ❌ |
| `@branch` / `@route` gates | ❌ |
| `InterruptNode` | ❌ |
| `.iter()` streaming | ❌ |

### Usage

```python
runner = DaftRunner(cache=DiskCache("./cache"))

# Returns a Daft DataFrame (lazy)
df = runner.map(
    graph,
    inputs={"texts": large_text_list},
    map_over="texts",
)

# Trigger distributed execution
results = df.collect()
```

### Validation

DaftRunner validates graph compatibility at execution time:

```python
# Graph with cycles → IncompatibleRunnerError
DaftRunner().map(cyclic_graph, inputs={...}, map_over="x")
# Error: "This graph has cycles, but DaftRunner doesn't support cycles."

# Graph with gates → IncompatibleRunnerError
DaftRunner().map(graph_with_routes, inputs={...}, map_over="x")
# Error: "This graph has gates (@route/@branch), but DaftRunner doesn't support gates."
```

**Note:** `MemoryCache` with DaftRunner is per-worker (not shared across distributed workers).

---

## Runner Architecture

### Class Hierarchy

All runners inherit from `BaseRunner`:

```python
class BaseRunner(ABC):
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
    def map(self, graph: Graph, inputs: dict[str, Any], *, map_over: str | list[str], **kwargs):
        """Batch execution. Return type varies by runner."""
        ...
```

**Runner-specific methods** (not in base class):
- `AsyncRunner.iter()` — streaming events

### RunnerCapabilities

Each runner declares its capabilities via a dataclass:

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
```

Capability values per runner:

| Capability | SyncRunner | AsyncRunner | DBOSAsyncRunner | DaftRunner |
|------------|:----------:|:-----------:|:---------------:|:----------:|
| `supports_cycles` | ✅ | ✅ | ✅ | ❌ |
| `supports_gates` | ✅ | ✅ | ✅ | ❌ |
| `supports_interrupts` | ❌ | ✅ | ✅ | ❌ |
| `supports_async_nodes` | ❌ | ✅ | ✅ | ✅ |
| `supports_streaming` | ❌ | ✅ | ✅ | ❌ |
| `supports_distributed` | ❌ | ❌ | ❌ | ✅ |
| `supports_durable_execution` | ❌ | ❌ | ✅ | ❌ |
| `returns_coroutine` | ❌ | ✅ | ✅ | ❌ |

Note: `returns_coroutine` indicates whether `.run()` must be awaited. DaftRunner handles async nodes internally but returns a DataFrame synchronously. `DBOSAsyncRunner` inherits all AsyncRunner capabilities and adds DBOS durability.

### Design Rationale

**Why separate runner classes (not dual methods)?**
1. **Clear intent** — Construction determines sync vs async mode
2. **Type safety** — `SyncRunner.run()` returns `dict`, `AsyncRunner.run()` returns `Awaitable[RunResult]`
3. **No ambiguity** — No "which method do I call?" question

**Why a capabilities dataclass (not scattered flags)?**
1. **Discoverable** — `runner.capabilities.supports_cycles` is self-documenting
2. **Validated** — Automatic graph compatibility checking
3. **Single source of truth** — One location declares all runner constraints

---

## RunResult

`AsyncRunner.run()` returns a `RunResult` object:

```python
@dataclass
class RunResult:
    values: dict[str, Any]           # Output values
    status: RunStatus                # COMPLETED, FAILED, PAUSED, or STOPPED
    workflow_id: str | None          # Workflow ID (required with checkpointer)
    run_id: str                      # Unique execution identifier
    pause: PauseInfo | None = None   # Pause details (when status == PAUSED)

    @property
    def paused(self) -> bool: ...    # True if status == PAUSED
```

**See [Execution Types](execution-types.md#runresult)** for full definition.

`SyncRunner.run()` returns a plain `dict[str, Any]` (no interrupt support).

---

## Session and Run IDs

```python
result = await runner.run(
    graph,
    inputs={...},
    session_id="conversation-123",  # User-provided: groups related runs
)
# result.run_id → "run-abc-456"     # Framework-generated: unique per execution
```

**Use cases:**
- `session_id`: Group multi-turn conversations, log correlation
- `run_id`: Trace and debug specific executions

---

## Error Handling

| Error | Cause |
|-------|-------|
| `MissingInputError` | Required input not provided |
| `GraphConfigError` | Invalid graph structure, or incompatible operation (e.g., map + interrupts) |
| `ConflictError` | Parallel nodes produced conflicting values for same output |
| `InfiniteLoopError` | Exceeded `max_iterations` (default: 1000) |
| `IncompatibleRunnerError` | Runner doesn't support graph features (cycles, gates, async nodes) |
| `VersionMismatchError` | Graph changed since workflow started (use `force_resume=True` to override) |

---

## Caching

All runners accept a `cache` parameter:

```python
from hypergraph import DiskCache, MemoryCache

# Persistent cache
runner = SyncRunner(cache=DiskCache("./cache"))

# In-memory cache (faster, not persistent)
runner = AsyncRunner(cache=MemoryCache())
```

Cached nodes skip execution on re-runs with identical inputs.

---

## API Reference

For complete method signatures with all parameters, see [Runners API Reference](./runners-api-reference.md).
