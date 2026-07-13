# Runners API Reference

Runners execute graphs. They handle the execution loop, node scheduling, and concurrency.

- **SyncRunner** - Sequential execution for synchronous nodes
- **DaftRunner** - Columnar execution via Daft for DAG-only graphs (batch, distributed)
- **AsyncRunner** - Concurrent execution with async support and `max_concurrency`
- **SyncHandle / AsyncHandle** - Process-local control of live background work
- **RunResult** - Output values, status, and error information
- **map()** - Batch processing with zip or cartesian product modes

## Overview

| Runner | Async Nodes | Cycles | Distributed | Blocking return | Background start |
|--------|-------------|--------|-------------|-----------------|------------------|
| `SyncRunner` | No | Yes | No | `RunResult` / `MapResult` | `SyncHandle` |
| `DaftRunner` | Yes (Daft-native) | No | Yes | `RunResult` / `MapResult` | Not supported |
| `AsyncRunner` | Yes | Yes | No | Awaitable `RunResult` / `MapResult` | `AsyncHandle` |

Use `run()` / `map()` when the caller should wait for the result. Use
`start_run()` / `start_map()` when the application must regain control while
work is live—for example, to serve a Stop button. See
[Control Work After It Starts](../05-how-to/control-background-execution.md).

All four methods on `SyncRunner` and `AsyncRunner` accept keyword-only
`inspect: bool = False`. The value must be a real boolean; invalid values raise
`TypeError` with guidance instead of silently enabling capture.

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
- `checkpointer` — Optional [checkpointer](../05-how-to/batch-processing.md#checkpointing-with-map) for persistent run history. For `run()`, enables strict lineage semantics, generic IDs for fresh/retry runs, and source-derived IDs for `fork_from`. For `map()`, persistence is enabled when `workflow_id` is provided. Requires `SqliteCheckpointer` or any `SyncCheckpointerProtocol` implementation.
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
    entrypoint: str | None = None,
    max_iterations: int | None = None,
    inspect: bool = False,
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
    show_progress: bool | None = None,
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
- `entrypoint` - Runtime entrypoint overrides are not supported. Configure the graph with `Graph(..., entrypoint=...)` or `graph.with_entrypoint(...)`.
- `max_iterations` - Max local iterations per cyclic execution region (SCC) (default: 1000)
- `inspect` - Capture shallow successful-node inputs and outputs for live and settled inspection. Defaults to `False`; `.inspect()` still returns a degraded view from always-on facts.
- `error_handling` - How to handle node execution errors:
  - `"raise"` (default): Re-raise the original exception (e.g., `ValueError`). Clean traceback, no wrapper.
  - `"continue"`: Return `RunResult` with `status=FAILED` and partial values instead of raising.
- `event_processors` - Optional list of [event processors](events.md) to observe execution, merged after any processors the graph carries (see [Graph-Carried Processors](events.md#graph-carried-processors))
- `show_progress` - Override the runner-level progress setting for this call.
- `checkpoint` - Optional low-level checkpoint snapshot (`values + steps`) for explicit fork restores.
- `workflow_id` - Optional workflow identifier for lineage tracking. With a checkpointer:
  - omitted on a fresh or retry run: a generic `run-...` ID is generated
  - omitted with `fork_from`: a `{source}-fork-{hex}` ID is derived
  - existing: strict resume only (same graph structure). Active/failed runs reject fresh values, paused runs accept interrupt responses, and stopped runs require a non-empty runtime mapping; completed workflows are terminal.
  - new + `checkpoint`: explicit fork
- `override_workflow` - Convenience shortcut for existing `workflow_id`s. When `True`, `run()` creates a source-derived fork and leaves the source row unchanged.
- `fork_from` - Workflow ID to fork from directly (no manual checkpoint plumbing). Without `workflow_id=`, the target is `{source}-fork-{hex}`. Requires a checkpointer.
- `retry_from` - Workflow ID to retry from directly (records retry lineage metadata). Without `workflow_id=`, runner naming remains generic `run-...`. Requires a checkpointer.
- Lineage hashing: checkpoint compatibility uses a structural hash; a separate code hash is recorded for observability/caching workflows.
- `**input_values` - Input shorthand for flat graph input names. Use `values` for dotted/nested inputs or names that match runner options.

Because `inspect` is a runner option, a graph input with that name belongs in
the values mapping:

```python
result = runner.run(
    graph,
    values={"inspect": "graph-owned"},
    inspect=True,
)
```

**Returns:** `RunResult` with outputs and status

**Raises:**
- `MissingInputError` - Required input not provided
- `IncompatibleRunnerError` - Graph contains async nodes
- `GraphConfigError` - If graph is cyclic and has no configured entrypoint
- `WorkflowStoppedError` - A stopped persisted workflow is rerun without a non-empty runtime mapping or `override_workflow=True`
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
result = runner.run(
    graph,
    {"x": 5},
    inspect=True,
    error_handling="continue",
)
if result.status == RunStatus.FAILED:
    print(result.error)        # the original exception
    print(result.values)       # outputs from nodes that completed before the failure

# In a notebook, keep this as the final expression.
result.inspect()

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
    entrypoint: str | None = None,
    inspect: bool = False,
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
    show_progress: bool | None = None,
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
- `entrypoint` - Runtime entrypoint overrides are not supported; configure it on the graph.
- `inspect` - Capture shallow successful-node inputs and outputs for live and settled inspection. Defaults to `False`.
- `error_handling` - How to handle failures:
  - `"raise"` (default): Stop on first failure and raise the exception
  - `"continue"`: Collect all results, including failures as `RunResult` with `status=FAILED`
- `event_processors` - Optional list of [event processors](events.md) to observe execution, merged after any processors the graph carries (see [Graph-Carried Processors](events.md#graph-carried-processors))
- `show_progress` - Override the runner-level progress setting for this call.
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
    inspect=True,
    error_handling="continue",
)
if results.failed:
    print(f"{len(results.failures)} items failed")

results.inspect()
```

### start_run() and start_map()

```python
def start_run(
    self,
    graph: Graph,
    values: dict[str, Any] | None = None,
    *,
    select: str | list[str] = _UNSET_SELECT,
    on_missing: Literal["ignore", "warn", "error"] = "ignore",
    entrypoint: str | None = None,
    max_iterations: int | None = None,
    inspect: bool = False,
    event_processors: list[EventProcessor] | None = None,
    show_progress: bool | None = None,
    checkpoint: Checkpoint | None = None,
    workflow_id: str | None = None,
    **input_values: Any,
) -> SyncHandle[RunResult]: ...

def start_map(
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
    inspect: bool = False,
    event_processors: list[EventProcessor] | None = None,
    show_progress: bool | None = None,
    workflow_id: str | None = None,
    **input_values: Any,
) -> SyncHandle[MapResult]: ...
```

Both methods return immediately with a process-local
[`SyncHandle`](#synchandle-and-asynchandle). Their parameters match the
corresponding blocking operation except that background retrieval owns failure
policy, so there is no `error_handling`. Passing `inspect=True` captures values
while the work is live; call `.inspect()` on the settled result, not the
handle. `start_run()` also omits
`override_workflow`, `fork_from`, and `retry_from`; prepare lineage changes
through the checkpointer before starting the resulting checkpoint and ID.
Passing any blocking runner control absent from the `start_*()` signature
directly raises `TypeError` before launch; use `values={...}` when the name
belongs to the graph rather than the runner.

```python
handle = runner.start_run(graph, {"order_id": "order-100"}, inspect=True)
do_other_work()
result = handle.result(raise_on_failure=False)
result.inspect()
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
caps.supports_streaming    # True (map_iter + ctx.stream chunks)
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
    show_progress: bool | None = None,
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
- `show_progress` - Accepted for runner API compatibility; Daft does not use Hypergraph progress processors.
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
    event_processors: list[EventProcessor] | None = None,
    show_progress: bool | None = None,
    error_handling: Literal["raise", "continue"] = "raise",
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
- `show_progress` - Accepted for runner API compatibility; Daft does not use Hypergraph progress processors.
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

If the graph has an output selection set via `graph.select(...)`, it prunes
both the computation (only nodes needed for the selected outputs run) and the
returned columns (only the selected outputs are added), matching `run()` and
`map()`. `columns=` and `graph.select(...)` compose independently: `columns=`
governs which DataFrame columns feed graph inputs (every DataFrame column
passes through either way), while `graph.select(...)` governs which output
columns appear.

Selected emit-only ordering signals never become DataFrame columns, matching
`run()`.

```python
graph = Graph([clean, count], name="text_pipeline").select("word_count")

result_df = runner.map_dataframe(graph, frame)
# Columns: text, word_count — the intermediate cleaned_text is not added
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

`DaftRunner` does not implement `start_run()` or `start_map()`. It translates
an operation into a Daft query plan; use Daft's execution controls when that
plan needs job-level orchestration.

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
- `checkpointer` — Optional checkpointer for persistent run history. For `run()`, enables strict lineage semantics, generic IDs for fresh/retry runs, and source-derived IDs for `fork_from`. For `map()`, persistence is enabled when `workflow_id` is provided. Requires `SqliteCheckpointer` or any `Checkpointer` implementation.
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
    inspect: bool = False,
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
    show_progress: bool | None = None,
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
- `max_concurrency` - Max parallel node executions (default: unlimited); when provided, it must be at least `1`.
- `inspect` - Capture shallow successful-node inputs and outputs for live and settled inspection. Defaults to `False`; `.inspect()` still returns a degraded view from always-on facts.
- `error_handling` - How to handle node execution errors:
  - `"raise"` (default): Re-raise the original exception. Clean traceback, no wrapper.
  - `"continue"`: Return `RunResult` with `status=FAILED` and partial values instead of raising.
- `event_processors` - Optional list of [event processors](events.md) to observe execution, merged after any processors the graph carries (see [Graph-Carried Processors](events.md#graph-carried-processors)) (supports `AsyncEventProcessor`)
- `show_progress` - Override the runner-level progress setting for this call.
- `checkpoint` - Optional low-level checkpoint snapshot (`values + steps`) for explicit fork restores.
- `workflow_id` - Optional workflow identifier for lineage tracking. With a checkpointer:
  - omitted on a fresh or retry run: a generic `run-...` ID is generated
  - omitted with `fork_from`: a `{source}-fork-{hex}` ID is derived
  - existing: strict resume only (same graph structure). Active/failed runs reject fresh values, paused runs accept interrupt responses, and stopped runs require a non-empty runtime mapping; completed workflows are terminal.
  - new + `checkpoint`: explicit fork
- `override_workflow` - Convenience shortcut for existing `workflow_id`s. When `True`, `run()` creates a source-derived fork and leaves the source row unchanged.
- `fork_from` - Workflow ID to fork from directly (no manual checkpoint plumbing). Without `workflow_id=`, the target is `{source}-fork-{hex}`. Requires a checkpointer.
- `retry_from` - Workflow ID to retry from directly (records retry lineage metadata). Without `workflow_id=`, runner naming remains generic `run-...`. Requires a checkpointer.
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
    inspect=True,
)
result.inspect()
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
    inspect: bool = False,
    error_handling: Literal["raise", "continue"] = "raise",
    event_processors: list[EventProcessor] | None = None,
    show_progress: bool | None = None,
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
- `max_concurrency` - Shared limit across all executions; when provided, it must be at least `1`.
- `inspect` - Capture shallow successful-node inputs and outputs for live and settled inspection. Defaults to `False`.
- `error_handling` - How to handle failures:
  - `"raise"` (default): Stop on first failure and raise the exception
  - `"continue"`: Collect all results, including failures as `RunResult` with `status=FAILED`
- `event_processors` - Optional list of [event processors](events.md) to observe execution, merged after any processors the graph carries (see [Graph-Carried Processors](events.md#graph-carried-processors))
- `show_progress` - Override the runner-level progress setting for this call.
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
    inspect=True,
    error_handling="continue",
)
results.inspect()
```

For very large batches, prefer setting `max_concurrency` explicitly. If `max_concurrency=None`
and the fan-out is extremely large, `AsyncRunner.map()` raises `ValueError` to avoid unbounded
task creation.

### start_run() and start_map()

```python
def start_run(
    self,
    graph: Graph,
    values: dict[str, Any] | None = None,
    *,
    select: str | list[str] = _UNSET_SELECT,
    on_missing: Literal["ignore", "warn", "error"] = "ignore",
    entrypoint: str | None = None,
    max_iterations: int | None = None,
    max_concurrency: int | None = None,
    inspect: bool = False,
    event_processors: list[EventProcessor] | None = None,
    show_progress: bool | None = None,
    checkpoint: Checkpoint | None = None,
    workflow_id: str | None = None,
    **input_values: Any,
) -> AsyncHandle[RunResult]: ...

def start_map(
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
    inspect: bool = False,
    event_processors: list[EventProcessor] | None = None,
    show_progress: bool | None = None,
    workflow_id: str | None = None,
    **input_values: Any,
) -> AsyncHandle[MapResult]: ...
```

These are ordinary methods, not coroutine functions. Call them from a running
event loop without `await`, then await the handle's result:

```python
handle = runner.start_run(graph, {"order_id": "order-200"}, inspect=True)
serve_other_requests()
result = await handle.result(raise_on_failure=False)
result.inspect()
```

As with the synchronous forms, background start methods omit
`error_handling`; `start_run()` also omits the lineage-changing
`override_workflow`, `fork_from`, and `retry_from` shortcuts. A provided
`max_concurrency` must be at least `1`; invalid limits fail retrieval loudly
instead of producing an empty result or a handle that never settles. Passing a
blocking runner control absent from the `start_*()` signature directly raises
`TypeError` before launch; use `values={...}` when that name is a graph input.

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
caps.supports_streaming    # True (map_iter + ctx.stream chunks)
caps.returns_coroutine     # True
caps.supports_interrupts   # True
```

---

## SyncHandle and AsyncHandle

Process-local handles control one live background execution and retrieve its
settled result. Both types use composition; neither subclasses
`concurrent.futures.Future` nor `asyncio.Task`.

```python
class SyncHandle(Generic[T]):
    @property
    def done(self) -> bool: ...

    def stop(self, *, info: Any = None) -> None: ...

    def result(self, *, raise_on_failure: bool = True) -> T: ...


class AsyncHandle(Generic[T]):
    @property
    def done(self) -> bool: ...

    def stop(self, *, info: Any = None) -> None: ...

    async def result(self, *, raise_on_failure: bool = True) -> T: ...
```

Both types are exported from `hypergraph` and `hypergraph.runners`:

```python
from hypergraph import AsyncHandle, SyncHandle

handle.done                                  # bool property
handle.stop(info={"requested_by": "Maya"})  # synchronous, cooperative, returns None
handle.result(raise_on_failure=True)         # await only for AsyncHandle
```

`SyncHandle.result()` blocks the caller. `AsyncHandle.result()` is a coroutine.
The handle itself is not awaitable. Cancelling one task awaiting an async
result cancels that waiter only; it does not cancel framework-owned execution.

Default retrieval raises the original captured node failure after execution
settles. Pass `raise_on_failure=False` to receive the failed `RunResult` or
`MapResult`. Failures that prevent construction of a result propagate in both
modes.

Settled truth stays on results, so handles do **not** provide `status`,
`wait()`, `failure`, `failures`, `failed_item_indexes`, `view`, `inspect`,
`cancel()`, `cancelled()`, `exception()`, `add_done_callback()`, `running()`,
or a handle-level `__await__`. They also have no lookup, reconnect,
serialization, lease, worker, or durable-job API.

`stop()` is a cooperative request. The first accepted call owns its `info`;
later calls and calls after settlement return `None` without rewriting the
outcome. A handle can stop work started without a workflow ID. With an ID,
`runner.stop(workflow_id, info=...)` controls the same execution.

See [Control Work After It Starts](../05-how-to/control-background-execution.md)
for complete sync/async examples, sparse stopped maps, and process recovery.

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
    log: RunLog | None          # Execution trace, if collected
    checkpoint_ok: bool         # False when a best-effort async step save failed
    checkpoint_errors: tuple[str, ...]  # String-only checkpoint save errors
    restored: bool              # True only for a checkpoint-skipped map child
    node_failures: tuple[FailureEvidence, ...]  # Attributable leaf failures
```

With async checkpoint durability, step saves are best-effort: a persistence gap
does not change the execution status. Check `checkpoint_ok` and
`checkpoint_errors` when durable history is required.

### Convenience Properties

```python
result.completed  # True if status == COMPLETED
result.paused     # True if status == PAUSED
result.failed     # True if status == FAILED
result.stopped    # True if status == STOPPED
result.failure    # First FailureEvidence, or None
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

### Structured Failure Evidence

Use `result.failure` when you need to reproduce the exact leaf-node call that
failed, rather than only reading its exception:

```python
@node(output_name="order")
def parse_order(raw_order: str) -> dict:
    raise ValueError(f"invalid order: {raw_order}")

graph = Graph([parse_order], name="orders")
result = SyncRunner().run(
    graph,
    {"raw_order": "missing-price"},
    error_handling="continue",
)

failure = result.failure
assert failure is not None
failure.node_name       # "parse_order"
failure.inputs          # {"raw_order": "missing-price"}
failure.error is result.error  # True: the original exception object
result.node_failures    # Deterministic tuple of every attributable failure
```

Default raise mode still raises the original exception type and object. Use
`get_failure_evidence()` when you catch it:

```python
from hypergraph import get_failure_evidence

try:
    SyncRunner().run(graph, {"raw_order": "missing-price"})
except ValueError as error:
    failure = get_failure_evidence(error)[0]
    assert failure.inputs == {"raw_order": "missing-price"}
```

Mapped failures retain their source item index. `MapResult.failures` remains a
list of failed `RunResult` children:

```python
results = SyncRunner().map(
    graph,
    {"raw_order": ["missing-price", "missing-sku"]},
    map_over="raw_order",
    error_handling="continue",
)

[
    (item.failure.item_index, item.failure.inputs)
    for item in results.failures
    if item.failure is not None
]
# [(0, {"raw_order": "missing-price"}), (1, {"raw_order": "missing-sku"})]
```

Nested GraphNodes report the leaf once and prefix its path at each boundary:

```python
inner = Graph([parse_order], name="order-parser")
middle = Graph([inner.as_node(name="parser")], name="order-worker")
outer = Graph([middle.as_node(name="worker")], name="batch")

result = SyncRunner().run(
    outer,
    {"raw_order": "missing-price"},
    error_handling="continue",
)
result.failure.node_name   # "worker/parser/parse_order"
result.failure.graph_name  # "order-parser" (the leaf graph)
```

`FailureEvidence.inputs` is deliberately ephemeral debugging state. It is a
shallow copy of the resolved graph-input mapping; contained values retain their
identity, and framework-injected `NodeContext` is excluded. Explicitly reading
`.inputs` returns the raw objects, which may include large values or secrets.
Those objects remain referenced until the `RunResult` or raised exception and
its suppressed evidence context are collected.

Raw inputs never appear in the ordinary result repr/HTML, `to_dict()`, run
logs, events, checkpoints, or OpenTelemetry. The explicit inspect display is
the exception: with `inspect=True`, it intentionally contains bounded captured
inputs and outputs. Failed `RunResult.to_dict()` output includes safe
`node_failures` metadata—node path, error text/type, timing, graph/workflow, and
item index—but no `inputs` or exception objects. Infrastructure failures that
cannot be attributed to a node executor use `node_failures == ()` and
`failure is None`. Daft integrations may likewise return no evidence until
they execute through the core node seam.

### inspect()

```python
def inspect(self) -> InspectionDisplay[Any]: ...
```

Return one explicit rich display for this settled result. Calling the method
does not mutate the result and does not display anything by itself.

```python
# Before: read status, log, and failure separately.
result = runner.run(graph, values, error_handling="continue")
print(result.status, result.log, result.failure)

# After: capture successful values, then return one joined view.
result = runner.run(graph, values, inspect=True, error_handling="continue")
result.inspect()
```

`inspect=True` does not require a checkpointer. Without that flag, `inspect()`
returns a degraded view: successful values report `not captured; rerun with
inspect=True`, while failures can still expose always-on `FailureEvidence`.
Restored nodes report their real status and metadata but do not reconstruct
successful values. Daft and custom/delegated runners may therefore produce the
degraded settled view.

Capture is shallow: top-level mappings are copied, while contained values keep
their object identity until the result is collected. Saved notebook output can
contain sensitive values. Rendering enforces per-value limits of depth 6, 100
mapping items, 200 sequence items, tables of 200 rows by 20 columns, and 20,000
text characters. See [Debug Workflows](../05-how-to/debug-workflows.md) for the
full privacy and live-to-saved contract.

### Progressive Disclosure

```python
# One-line summary
result.summary()   # "3 nodes | 12ms | 0 errors | slowest: generate (8ms)"

# JSON-serializable metadata (no raw values or exception objects)
result.to_dict()   # {"status": "completed", "run_id": "run-abc", "checkpoint_ok": True, "checkpoint_errors": [], "restored": False, "log": {...}}
```

A checkpoint-skipped map child remains `status=COMPLETED` for compatibility but has `restored=True`. Its summary, repr, HTML, serialized metadata, and synthetic `RunLog` all disclose restoration rather than reporting a fake execution duration.

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
results.stopped      # True if status is STOPPED, including curtailed scope
results.failures     # List of failed RunResult items
results.requested_count          # Real outcomes + inputs never started
results.unstarted_item_indexes   # Sorted original indexes never claimed
results.restored_count  # Restored successes (a subset of completed items)

# Derived durability aggregation (properties, not dataclass fields)
results.checkpoint_ok      # True only when every item persisted successfully
results.checkpoint_errors  # Tuple of save errors in stable item order

# Progressive disclosure
results.summary()    # "3 items | 3 completed | avg 12ms/item"
results.to_dict()    # JSON-serializable batch metadata + per-item results
results.inspect()    # Explicit rich batch view
```

Completed counts stay inclusive of restored successes. Duration averages use only freshly executed completed items with real logs; a fully restored map omits the average. `results.log` exposes the same `restored_count` and per-item provenance through `MapLog`.

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
    unstarted_item_indexes: tuple[int, ...] = ()
```

`requested_count` is a derived property:

```python
results.requested_count == len(results) + len(results.unstarted_item_indexes)
```

For a completed or genuinely empty map, `unstarted_item_indexes == ()` and
`requested_count == len(results)`. When cooperative stop curtails a batch,
`results` contains only real claimed outcomes; Hypergraph does not fabricate
item results, logs, events, run IDs, or checkpoint rows for unstarted inputs.

### inspect()

```python
def inspect(self) -> InspectionDisplay[Any]: ...
```

Return one explicit batch display, opening on original map items. Use failure
evidence to locate sparse or stopped items; an original item index is not a
compact sequence position:

```python
failed = next(
    result
    for result in results.failures
    if result.failure is not None and result.failure.item_index == 3
)
failure = failed.failure
assert failure is not None
print(failure.inputs)

results.inspect()
```

The same degraded, sensitivity, limit, and no-checkpointer rules as
`RunResult.inspect()` apply.

### Status Precedence

Empty batch -> `COMPLETED`.

- `STOPPED` when cooperative stop leaves requested input indexes unstarted,
  even if one real attempted item failed. `failed` and `failures` still expose
  that real failure, and `partial` remains false.

- `FAILED` when at least one item failed and none completed.
- `PARTIAL` when some items completed and some failed.
- `PAUSED` if any item paused and no item failed.
- `STOPPED` if any item stopped and no item failed or paused when no requested
  inputs remain unstarted.
- `COMPLETED` when all items completed.

If stop arrives only after every requested item settles,
`unstarted_item_indexes == ()` and ordinary failure-first aggregation remains
in force.

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

Wraps an exception raised inside a node during graph execution and carries the partial `GraphState` accumulated before the failure (`partial_state` attribute). While this internal wrapper is in hand, `node_failures` exposes the same deterministic tuple as `RunResult`, and `failure` returns its first item or `None`.

`runner.run()` with the default `error_handling="raise"` unwraps it and re-raises the original node exception, so application code normally catches the node's own exception type. `ExecutionError` is exported for advanced integrations (custom runners, executors, or superstep-level code) that need access to the partial state alongside the failure.

```python
from hypergraph import ExecutionError

try:
    advanced_execution_surface(...)
except ExecutionError as e:
    print(e.partial_state)   # state accumulated before the failure
    print(e.__cause__)       # the original node exception
    print(e.node_failures)   # attributable leaf failures, if any
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

- **Strict resume**: using an existing `workflow_id` resumes persisted state and ordinarily rejects fresh runtime inputs, because new inputs would silently rewrite history. Interrupt response payloads resolve paused runs. A stopped run instead requires an explicit signal: pass any non-empty valid runtime mapping to resume the same lineage, or pass `override_workflow=True` to create a source-derived fork. `None` and `{}` raise `WorkflowStoppedError` before execution events or persistence writes.
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
