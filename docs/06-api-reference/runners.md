# Runners API Reference

Runners execute graphs. They handle the execution loop, node scheduling, and concurrency.

- **SyncRunner** - Sequential execution for synchronous nodes
- **DaftRunner** - Columnar execution via Daft for DAG-only graphs (batch, distributed)
- **AsyncRunner** - Concurrent execution with async support and `max_concurrency`
- **RunResult** - Output values, status, and error information
- **map()** - Batch processing with zip or cartesian product modes

## Overview

| Runner | Async Nodes | Cycles | Distributed | Returns |
|--------|-------------|--------|-------------|---------|
| `SyncRunner` | No | Yes | No | `RunResult` |
| `DaftRunner` | Yes (Daft-native) | No | Yes | `RunResult` / `MapResult` |
| `AsyncRunner` | Yes | Yes | No | `Coroutine[RunResult]` |

## SyncRunner

Sequential execution for synchronous graphs.

`SyncRunner` is not built for interrupts or HITL flows. If a graph contains
`InterruptNode`s, `SyncRunner` raises `IncompatibleRunnerError`; use
`AsyncRunner` instead.

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
    def __init__(
        self,
        cache: CacheBackend | None = None,
        checkpointer: Checkpointer | None = None,
        show_progress: bool = False,
    ) -> None: ...
```

**Args:**
- `cache` — Optional [cache backend](../03-patterns/08-caching.md) for node result caching. Nodes opt in with `@node(..., cache=True)`. Supports `InMemoryCache`, `DiskCache`, or any `CacheBackend` implementation.
- `checkpointer` — Optional [checkpointer](../05-how-to/batch-processing.md#checkpointing-with-map) for persistent run history. For `run()`, enables strict lineage semantics (resume/fork) and auto-generates `workflow_id` when omitted. For `map()`, persistence is enabled when `workflow_id` is provided. Requires `SqliteCheckpointer` or any `SyncCheckpointerProtocol` implementation.
- `show_progress` — If `True`, automatically attaches a Rich progress processor to `run()` and `map()` calls. Per-call `show_progress` overrides this default.

### run()

```python
def run(
    self,
    graph: Graph,
    values: dict[str, Any] | None = None,
    *,
    select: str | list[str] = _UNSET_SELECT,
    on_missing: Literal["ignore", "warn", "error"] = "ignore",
    max_iterations: int | None = None,
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
    checkpoint: Checkpoint | None = None,
    workflow_id: str | None = None,
    override_workflow: bool = False,
    fork_from: str | None = None,
    retry_from: str | None = None,
    **input_values: Any,
) -> RunResult: ...
```

Execute a graph once.

**Args:**
- `graph` - The graph to execute
- `values` - Optional input values as `{param_name: value}`
- `select` - Runtime select overrides are not supported. Configure output scope on the graph with `graph.select(...)` before execution.
- `on_missing` - How to handle missing selected outputs:
  - `"ignore"` (default): silently omit missing outputs
  - `"warn"`: warn about missing outputs, return what's available
  - `"error"`: raise error if any selected output is missing
- `max_iterations` - Max local iterations per cyclic execution region (SCC) (default: 1000)
- `error_handling` - How to handle node execution errors:
  - `"raise"` (default): Re-raise the original exception (e.g., `ValueError`). Clean traceback, no wrapper.
  - `"continue"`: Return `RunResult` with `status=FAILED` and partial values instead of raising.
- `event_processors` - Optional list of [event processors](events.md) to observe execution
- `checkpoint` - Optional low-level checkpoint snapshot (`values + steps`) for explicit fork restores.
- `workflow_id` - Optional workflow identifier for lineage tracking. With a checkpointer:
  - omitted: auto-generated for `run()`
  - existing: strict resume only (no runtime values; same graph structure). Existing persisted workflows may be `active`, `paused`, or `failed`; completed workflows are terminal.
  - new + `checkpoint`: explicit fork
- `override_workflow` - Convenience shortcut for existing `workflow_id`s. When `True` and the `workflow_id` already exists, `run()` auto-forks from that workflow (generates a new workflow ID and uses its checkpoint) instead of raising strict resume errors.
- `fork_from` - Workflow ID to fork from directly (no manual checkpoint plumbing). Requires a checkpointer.
- `retry_from` - Workflow ID to retry from directly (records retry lineage metadata). Requires a checkpointer.
- Lineage hashing: checkpoint compatibility uses a structural hash; a separate code hash is recorded for observability/caching workflows.
- `**input_values` - Input shorthand for flat graph input names. Use `values` for dotted/nested inputs or names that match runner options.

**Returns:** `RunResult` with outputs and status

**Raises:**
- `MissingInputError` - Required input not provided
- `IncompatibleRunnerError` - Graph contains async nodes
- `GraphConfigError` - If graph is cyclic and has no configured entrypoint
- `ValueError` - If runtime `select` or `entrypoint` overrides are passed
- Node execution errors (e.g., `ValueError`, `TypeError`) when `error_handling="raise"` (the default)

**Example:**

```python
# Basic execution — raises on failure (default)
result = runner.run(graph, {"query": "What is RAG?"})

# kwargs shorthand
result = runner.run(graph, query="What is RAG?")

# Configure output scope on the graph
scoped = graph.select("final_answer")
result = runner.run(scoped, values)

# Limit iterations for cyclic graphs
result = runner.run(cyclic_graph, values, max_iterations=50)

# Strict output checking
result = runner.run(graph.select("answer"), values, on_missing="error")

# With progress bars
from hypergraph import RichProgressProcessor
result = runner.run(graph, values, event_processors=[RichProgressProcessor()])

# Collect partial results instead of raising on failure
from hypergraph import RunStatus
result = runner.run(graph, {"x": 5}, error_handling="continue")
if result.status == RunStatus.FAILED:
    print(result.error)        # the original exception
    print(result.values)       # outputs from nodes that completed before the failure

# Fork from an existing run (workflow-id based)
from hypergraph.checkpointers import SqliteCheckpointer
cp = SqliteCheckpointer("./runs.db")
runner = SyncRunner(checkpointer=cp)

runner.run(graph, {"x": 5}, workflow_id="job-1")
result = runner.run(
    graph,
    {"x": 100},
    fork_from="job-1",
)

# Convenience override (auto-forks if "job-1" already exists)
result = runner.run(
    graph,
    {"x": 100},
    workflow_id="job-1",
    override_workflow=True,
)
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
    clone: bool | list[str] = False,
    select: str | list[str] = _UNSET_SELECT,
    on_missing: Literal["ignore", "warn", "error"] = "ignore",
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
    workflow_id: str | None = None,
    **input_values: Any,
) -> MapResult: ...
```

Execute a graph multiple times with different inputs.

**Args:**
- `graph` - The graph to execute
- `values` - Optional input values. Parameters in `map_over` should be lists
- `map_over` - Parameter name(s) to iterate over
- `map_mode` - `"zip"` for parallel iteration, `"product"` for cartesian product
- `clone` - Deep-copy mutable values for each iteration. `True` clones all non-`map_over` values; pass a list of names to clone selectively. Prevents cross-iteration mutation.
- `select` - Runtime select overrides are not supported. Configure output scope on the graph with `graph.select(...)` before execution.
- `on_missing` - How to handle missing selected outputs (`"ignore"`, `"warn"`, or `"error"`)
- `error_handling` - How to handle failures:
  - `"raise"` (default): Stop on first failure and raise the exception
  - `"continue"`: Collect all results, including failures as `RunResult` with `status=FAILED`
- `event_processors` - Optional list of [event processors](events.md) to observe execution
- `workflow_id` - Optional workflow identifier for checkpoint persistence and resume. Creates a parent batch run with per-item child runs (`{workflow_id}/0`, `{workflow_id}/1`, ...). On re-run, completed items are skipped. See [Resuming Batches](../05-how-to/batch-processing.md#resuming-batches).
- `**input_values` - Input shorthand for flat graph input names. Use `values` for dotted/nested inputs or names that match runner options.

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

## DaftRunner

Translation runner: converts DAGs into chained Daft `df.with_column()` UDF calls.

```python
from hypergraph.integrations.daft import DaftRunner
```

`DaftRunner` translates each node into a Daft UDF and chains them via
`df.with_column()`. The entire graph becomes a single Daft query plan executed
columnar-style. This is a good fit when:

- you want columnar batch execution over a dataset
- you need distributed fan-out via Daft
- your graph is a DAG (no cycles, no gates, no interrupts)

It supports `FunctionNode` and `GraphNode` (including nested `map_over`).
Async nodes are handled natively by Daft's async UDF support.

### Constructor

```python
class DaftRunner:
    def __init__(
        self,
        *,
        cache: CacheBackend | None = None,
    ) -> None: ...
```

**Args:**
- `cache` - Optional cache backend for node-level caching.

**Raises:**
- `ImportError` - If the `daft` dependency is not installed. Install with `pip install 'hypergraph[daft]'`.

### run()

```python
def run(
    self,
    graph: Graph,
    values: dict[str, Any] | None = None,
    *,
    select: str | list[str] = _UNSET_SELECT,
    on_missing: Literal["ignore", "warn", "error"] = "ignore",
    entrypoint: str | None = None,
    max_iterations: int | None = None,
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
    **input_values: Any,
) -> RunResult: ...
```

Execute a graph once via a 1-row Daft plan.

**Args:**
- `graph` - The graph to execute (must be a DAG)
- `values` - Optional input values as `{param_name: value}`
- `select` - Runtime select overrides are not supported. Configure output scope on the graph with `graph.select(...)`.
- `on_missing` - How to handle missing selected outputs (`"ignore"`, `"warn"`, or `"error"`)
- `entrypoint` - Runtime entrypoint overrides are not supported
- `max_iterations` - Accepted for API compatibility but not used (DaftRunner does not support cycles)
- `error_handling` - `"raise"` re-raises the original exception; `"continue"` returns a failed `RunResult`
- `event_processors` - Accepted but ignored with a warning (DaftRunner does not support events)
- `**input_values` - Input shorthand for flat graph input names. Use `values` for dotted/nested inputs or names that match runner options.

**Example:**

```python
from hypergraph import Graph, node
from hypergraph.integrations.daft import DaftRunner

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

graph = Graph([double])
runner = DaftRunner()
result = runner.run(graph, x=5)

print(result["doubled"])  # 10
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
    clone: bool | list[str] = False,
    select: str | list[str] = _UNSET_SELECT,
    on_missing: Literal["ignore", "warn", "error"] = "ignore",
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
    **input_values: Any,
) -> MapResult: ...
```

Execute a graph for each item via Daft columnar execution.

All items are packed into a single Daft DataFrame, and the entire graph
executes as chained `df.with_column()` UDF calls. This is the primary batch
entrypoint when your data is a Python collection.

**Args:**
- `graph` - The graph to execute (must be a DAG)
- `values` - Optional input values. Parameters in `map_over` should be lists
- `map_over` - Parameter name(s) to iterate over
- `map_mode` - `"zip"` for parallel iteration or `"product"` for cartesian product
- `clone` - Deep-copy mutable broadcast values for each row. `True` clones all non-`map_over` values; pass a list of names to clone selectively
- `select` - Runtime select overrides are not supported
- `on_missing` - How to handle missing selected outputs (`"ignore"`, `"warn"`, or `"error"`)
- `error_handling` - `"raise"` re-raises the first failed item's original exception; `"continue"` falls back to per-item execution and preserves failures inside `MapResult`
- `event_processors` - Accepted but ignored with a warning
- `**input_values` - Input shorthand for flat graph input names. Use `values` for dotted/nested inputs or names that match runner options.

**Example:**

```python
from hypergraph import Graph, node
from hypergraph.integrations.daft import DaftRunner

@node(output_name="sentences")
def split_sentences(document: str) -> list[str]:
    return [part.strip() for part in document.split(".") if part.strip()]

@node(output_name="cleaned")
def clean_sentence(text: str) -> str:
    return " ".join(text.lower().split())

sentence_graph = Graph([clean_sentence], name="sentence_graph")
workflow = Graph(
    [
        split_sentences,
        sentence_graph.as_node(name="analyze").rename_inputs(text="sentences").map_over("sentences"),
    ]
)

runner = DaftRunner()
results = runner.map(
    workflow,
    {"document": ["Refund requested. Checkout blocked.", "Weekly roadmap update."]},
    map_over="document",
)

print(results["cleaned"])  # [['refund requested', 'checkout blocked'], ['weekly roadmap update']]
```

### map_dataframe()

```python
def map_dataframe(
    self,
    graph: Graph,
    dataframe: DataFrame,
    *,
    columns: str | Iterable[str] | None = None,
    values: dict[str, Any] | None = None,
    clone: bool | list[str] = False,
    **input_values: Any,
) -> DataFrame: ...
```

Apply a DAG graph to a Daft DataFrame and return a new Daft DataFrame.

Use this when your dataset already lives in a Daft DataFrame. Each row
provides graph inputs from columns, while graph outputs are added as new
DataFrame columns without materializing rows back into Python.

**Args:**
- `graph` - The graph to execute (must be a DAG)
- `dataframe` - Daft DataFrame supplying row-wise inputs
- `columns` - Optional subset of DataFrame columns to use as graph inputs. Defaults to all columns.
- `values` / `**input_values` - Additional broadcast inputs merged into every row. `**input_values` only accepts flat graph input names; use `values` for dotted/nested inputs or names that match runner options. Must not overlap with DataFrame column names.
- `clone` - Deep-copy mutable broadcast values for each row

**Returns:** Daft DataFrame with original input columns plus graph output columns.

Output columns are added to the original DataFrame; passthrough columns are
preserved. If a graph output would overwrite an existing DataFrame column,
`map_dataframe` raises a graph configuration error instead of replacing data.

**Example:**

```python
import daft
from hypergraph import Graph, node
from hypergraph.integrations.daft import DaftRunner

@node(output_name="cleaned_text")
def clean(text: str) -> str:
    return " ".join(text.lower().strip().split())

@node(output_name="word_count")
def count(cleaned_text: str) -> int:
    return len(cleaned_text.split())

graph = Graph([clean, count], name="text_pipeline")

frame = daft.from_pydict({
    "text": ["  Alpha beta alpha  ", "Gamma delta epsilon zeta eta"],
})

runner = DaftRunner()
result_df = runner.map_dataframe(graph, frame)
# result_df is a Daft DataFrame with columns: text, cleaned_text, word_count
result_df.show()
```

Broadcast values (shared across all rows) are passed as keyword arguments
and captured in UDF closures:

```python
@node(output_name="greeting")
def greet(name: str, prefix: str) -> str:
    return f"{prefix}, {name}!"

graph = Graph([greet])
df = daft.from_pydict({"name": ["Alice", "Bob"]})

result_df = DaftRunner().map_dataframe(graph, df, prefix="Hi")
# Each row gets prefix="Hi" via the UDF closure
```

### capabilities

```python
runner = DaftRunner()
caps = runner.capabilities

caps.supports_cycles          # False
caps.supports_gates           # False
caps.supports_async_nodes     # True  (Daft handles async UDFs natively)
caps.supports_interrupts      # False
caps.supports_events          # False
caps.supports_distributed     # True
caps.supports_checkpointing   # False
```

### @stateful

Decorator to mark a class for once-per-worker initialization. DaftRunner wraps
stateful objects with `@daft.cls` instead of `@daft.func`, so heavy resources
(ML models, DB connections) are created once per worker process rather than
once per row.

```python
from hypergraph import Graph, node
from hypergraph.integrations.daft import DaftRunner, stateful

@stateful(max_concurrency=2)
class Embedder:
    def __init__(self):
        self.model = load_heavy_model()

    def embed(self, text: str) -> list[float]:
        return self.model.encode(text)

@node(output_name="embedding")
def embed(text: str, embedder: Embedder) -> list[float]:
    return embedder.embed(text)

graph = Graph([embed]).bind(embedder=Embedder())
runner = DaftRunner()
results = runner.map(graph, {"text": texts}, map_over="text")
```

The class must support zero-argument construction (`__init__()` with no
required args) so Daft can re-create it on each worker.

### daft_node(..., batch=True)

Use `hypergraph.integrations.daft.node` for vectorized `@daft.func.batch`
execution. Batch UDFs receive `daft.Series` instead of scalar values and must
declare an explicit Daft `return_dtype`.

```python
import daft
from hypergraph import Graph
from hypergraph.integrations.daft import DaftRunner
from hypergraph.integrations.daft import node as daft_node

@daft_node(output_name="normalized", batch=True, return_dtype=daft.DataType.float64())
def normalize(values: daft.Series) -> list[float]:
    arr = values.to_pylist()
    mean = sum(arr) / len(arr)
    std = (sum((x - mean) ** 2 for x in arr) / len(arr)) ** 0.5
    if std == 0:
        return [0.0] * len(arr)
    return [round((x - mean) / std, 4) for x in arr]

graph = Graph([normalize], name="batch_norm")
runner = DaftRunner()
results = runner.map(graph, {"values": [10.0, 20.0, 30.0]}, map_over="values")
```

### Options

Typed Daft lowering options can be passed directly to `daft_node` or reused via
`Options`:

```python
import daft
from hypergraph.integrations.daft import Options
from hypergraph.integrations.daft import node as daft_node

batch_options = Options(
    return_dtype=daft.DataType.int64(),
    batch=True,
    batch_size=128,
    max_retries=2,
    on_error="log",
)

@daft_node(output_name="token_count", options=batch_options)
def count_tokens(text: daft.Series) -> list[int | None]:
    return [len(value.split()) for value in text.to_pylist()]
```

Use `@stateful(cpus=..., gpus=..., max_concurrency=...)` for class-level
`@daft.cls` controls. `stateful(options=...)` accepts only class resource,
retry, and error-handling settings; dtype, batch, and unnest settings belong on
`daft_node(...)`.

### Dashboard and Extensions

`DaftRunner` does not add a Hypergraph-specific dashboard or extension API.
Start Daft's dashboard and set `DAFT_DASHBOARD_URL` before running, or call
`daft.load_extension(...)` before constructing/executing the Daft plan.

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
    def __init__(
        self,
        cache: CacheBackend | None = None,
        checkpointer: Checkpointer | None = None,
        show_progress: bool = False,
    ) -> None: ...
```

**Args:**
- `cache` — Optional [cache backend](../03-patterns/08-caching.md) for node result caching. Nodes opt in with `@node(..., cache=True)`.
- `checkpointer` — Optional checkpointer for persistent run history. For `run()`, enables strict lineage semantics (resume/fork) and auto-generates `workflow_id` when omitted. For `map()`, persistence is enabled when `workflow_id` is provided. Requires `SqliteCheckpointer` or any `Checkpointer` implementation.
- `show_progress` — If `True`, automatically attaches a Rich progress processor to `run()` and `map()` calls. Per-call `show_progress` overrides this default.

### run()

```python
async def run(
    self,
    graph: Graph,
    values: dict[str, Any] | None = None,
    *,
    select: str | list[str] = _UNSET_SELECT,
    on_missing: Literal["ignore", "warn", "error"] = "ignore",
    entrypoint: str | None = None,
    max_iterations: int | None = None,
    max_concurrency: int | None = None,
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
    checkpoint: Checkpoint | None = None,
    workflow_id: str | None = None,
    override_workflow: bool = False,
    fork_from: str | None = None,
    retry_from: str | None = None,
    **input_values: Any,
) -> RunResult: ...
```

Execute a graph asynchronously.

**Args:**
- `graph` - The graph to execute
- `values` - Optional input values
- `select` - Runtime select overrides are not supported. Configure output scope on the graph with `graph.select(...)` before execution.
- `on_missing` - How to handle missing selected outputs (`"ignore"`, `"warn"`, or `"error"`)
- `entrypoint` - Runtime entrypoint overrides are not supported. Configure entrypoints on the graph via `Graph(..., entrypoint=...)` or `graph.with_entrypoint(...)`.
- `max_iterations` - Max local iterations per cyclic execution region (SCC) (default: 1000)
- `max_concurrency` - Max parallel node executions (default: unlimited)
- `error_handling` - How to handle node execution errors:
  - `"raise"` (default): Re-raise the original exception. Clean traceback, no wrapper.
  - `"continue"`: Return `RunResult` with `status=FAILED` and partial values instead of raising.
- `event_processors` - Optional list of [event processors](events.md) to observe execution (supports `AsyncEventProcessor`)
- `checkpoint` - Optional low-level checkpoint snapshot (`values + steps`) for explicit fork restores.
- `workflow_id` - Optional workflow identifier for lineage tracking. With a checkpointer:
  - omitted: auto-generated for `run()`
  - existing: strict resume only (no runtime values; same graph structure). Existing persisted workflows may be `active`, `paused`, or `failed`; completed workflows are terminal.
  - new + `checkpoint`: explicit fork
- `override_workflow` - Convenience shortcut for existing `workflow_id`s. When `True` and the `workflow_id` already exists, `run()` auto-forks from that workflow (generates a new workflow ID and uses its checkpoint) instead of raising strict resume errors.
- `fork_from` - Workflow ID to fork from directly (no manual checkpoint plumbing). Requires a checkpointer.
- `retry_from` - Workflow ID to retry from directly (records retry lineage metadata). Requires a checkpointer.
- Lineage hashing: checkpoint compatibility uses a structural hash; a separate code hash is recorded for observability/caching workflows.
- `**input_values` - Input shorthand for flat graph input names. Use `values` for dotted/nested inputs or names that match runner options.

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
    clone: bool | list[str] = False,
    select: str | list[str] = _UNSET_SELECT,
    on_missing: Literal["ignore", "warn", "error"] = "ignore",
    entrypoint: str | None = None,
    max_concurrency: int | None = None,
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
    workflow_id: str | None = None,
    **input_values: Any,
) -> MapResult: ...
```

Execute graph multiple times concurrently.

**Args:**
- `graph` - The graph to execute
- `values` - Optional input values
- `map_over` - Parameter name(s) to iterate over
- `map_mode` - `"zip"` or `"product"`
- `clone` - Deep-copy mutable values for each iteration. `True` clones all non-`map_over` values; pass a list of names to clone selectively.
- `select` - Runtime select overrides are not supported. Configure output scope on the graph with `graph.select(...)` before execution.
- `on_missing` - How to handle missing selected outputs (`"ignore"`, `"warn"`, or `"error"`)
- `entrypoint` - Runtime entrypoint overrides are not supported.
- `max_concurrency` - Shared limit across all executions
- `error_handling` - How to handle failures:
  - `"raise"` (default): Stop on first failure and raise the exception
  - `"continue"`: Collect all results, including failures as `RunResult` with `status=FAILED`
- `event_processors` - Optional list of [event processors](events.md) to observe execution
- `workflow_id` - Optional workflow identifier for checkpoint persistence and resume. Creates per-item child runs that can be skipped on re-run. See [Resuming Batches](../05-how-to/batch-processing.md#resuming-batches).
- `**input_values` - Input shorthand for flat graph input names. Use `values` for dotted/nested inputs or names that match runner options.

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
    status: RunStatus           # COMPLETED, FAILED, PAUSED, or STOPPED
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
results.status       # RunStatus.COMPLETED, PARTIAL, FAILED, PAUSED, or STOPPED
results.completed    # True if all completed
results.failed       # True if any failed
results.partial      # True if some items completed and some failed
results.stopped      # True if any item stopped and none failed/paused
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

Empty batch -> `COMPLETED`.

- `FAILED` when at least one item failed and none completed.
- `PARTIAL` when some items completed and some failed.
- `PAUSED` if any item paused and no item failed.
- `STOPPED` if any item stopped and no item failed or paused.
- `COMPLETED` when all items completed.

---

## RunStatus

Enum for execution status.

```python
from hypergraph import RunStatus

class RunStatus(Enum):
    COMPLETED = "completed"  # Success
    FAILED = "failed"        # Error occurred
    PAUSED = "paused"        # Waiting for human input (InterruptNode)
    PARTIAL = "partial"      # Batch had mixed completed and failed items
    STOPPED = "stopped"      # Run stopped cooperatively
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
    case RunStatus.STOPPED:
        return result.values
```

With the default `error_handling="raise"`, node failures raise before returning a `RunResult`, so a single run normally returns `COMPLETED`, `PAUSED`, or `STOPPED`. `PARTIAL` is a batch aggregate status exposed by `MapResult`.

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

### ExecutionError

Wraps an exception raised inside a node during graph execution and carries the partial `GraphState` accumulated before the failure (`partial_state` attribute).

`runner.run()` with the default `error_handling="raise"` unwraps it and re-raises the original node exception, so application code normally catches the node's own exception type. `ExecutionError` is exported for advanced integrations (custom runners, executors, or superstep-level code) that need access to the partial state alongside the failure.

```python
from hypergraph import ExecutionError

try:
    advanced_execution_surface(...)
except ExecutionError as e:
    print(e.partial_state)   # state accumulated before the failure
    print(e.__cause__)       # the original node exception
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
- kwargs shorthand only accepts flat graph inputs, such as `query=...`
- use `values={...}` for dotted or nested graph inputs, such as `{"inner.x": 1}`
- option names like `select`, `map_over`, `max_concurrency` are reserved for runner options
- if an input name matches an option name, pass that input through `values={...}`

```python
# input named "select" must go through values
runner.run(graph, values={"select": "fast"})

# namespaced inputs must also go through values
runner.run(outer, values={"inner.x": 5})
```

### Execution Model

Runners execute graphs in two phases:

1. Build a static execution plan from the active graph
2. Walk that plan until each region reaches quiescence

The static plan is a DAG of **strongly connected components** (SCCs):

- **DAG region** — a component with no feedback loop, usually runs in one pass
- **Cyclic region** — a component with feedback, iterates locally until no node in that region is ready
- **Gate edges** — participate in planning so gate-driven loops stay in one region, but gate decisions still act as runtime activation

Within one topo layer, runners still use supersteps as the execution batch:

1. Find ready nodes inside the current layer
2. Execute them (sequentially for Sync, concurrently for Async)
3. Update values, versions, and routing decisions
4. Repeat until that layer reaches quiescence

```text
Layer 1: [embed]
Layer 2: [retrieve]
Layer 3: [generate]
```

For cycles:

```text
Layer 1: [generate, should_continue]  → local iteration until quiescent
Layer 2: [finalize]
```

### Value Resolution Order

When collecting inputs for a node, values are resolved in this order:

1. **Edge value** - Output from upstream node
2. **Input value** - Provided via `values`/kwargs for fresh runs and explicit fork/retry runs.
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

Checkpoint lineage has two different input rules:

- **Strict resume**: using an existing `workflow_id` resumes persisted state and rejects fresh runtime inputs, because new inputs would silently rewrite history. Interrupt response payloads are the exception; pass the paused response key/value to resolve the pending interrupt.
- **Fork/retry**: `fork_from` and `retry_from` create a new lineage from a checkpoint, so runtime inputs may override restored values for the new run.

See [Run Lineage](../05-how-to/batch-processing.md#run-lineage-resume-vs-fork). For the `Checkpointer` ABC, `CheckpointPolicy`, backend comparison, and the no-checkpointer re-drive alternative, see [Checkpointers](checkpointers.md).

### Cyclic Graphs

Graphs with cycles (feedback loops) execute as local SCC regions until quiescent:

```python
@node(output_name="count")
def increment(count: int) -> int:
    return count + 1 if count < 5 else count

# Cycle: count feeds back into increment
# Runs until the cyclic region has no more ready work
```

The runner:
1. Tracks value and `wait_for` freshness via versions
2. Re-executes nodes in the current cyclic region when their inputs become fresh
3. Stops when that region has no more ready nodes, or `max_iterations` is hit

---

## Nested Graphs

Each GraphNode inherits the parent runner by default:

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

Override the runner only when a subgraph has a different compatible execution strategy:

```python
from hypergraph.integrations.daft import DaftRunner

inner = Graph([normalize], name="columnar_step")
outer = Graph([inner.as_node(runner=DaftRunner()), summarize])

# Equivalent immutable form
outer = Graph([inner.as_node().with_runner(DaftRunner()), summarize])
```

Compatibility is checked at execution time. A delegated runner must support the
inner graph's features (cycles, gates, interrupts, async nodes, and events as
needed). `DaftRunner` supports DAG-style `FunctionNode` and `GraphNode` plans,
but does not support runner overrides inside a Daft plan.
