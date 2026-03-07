# How to Process Data in Batches

Use `runner.map()` to process multiple inputs through the same graph.

## The Pattern: Think Singular, Scale with Map

```python
# 1. Write logic for ONE item
@node(output_name="features")
def extract(document: str) -> dict:
    return analyze(document)

# 2. Build a graph
graph = Graph([extract])

# 3. Scale to many items
results = runner.map(graph, {"document": documents}, map_over="document")
```

This pattern is one of hypergraph's biggest advantages in practice:

- write the logic for one item
- compose it into a graph
- scale it with `runner.map()` or a mapped `GraphNode`

That same shape works for:

- ETL and document ingestion
- feature extraction
- model comparison
- evaluation datasets
- nested workflows where each item may branch internally

## Basic Usage

```python
from hypergraph import Graph, node, SyncRunner

@node(output_name="cleaned")
def clean(text: str) -> str:
    return text.strip().lower()

@node(output_name="word_count")
def count(cleaned: str) -> int:
    return len(cleaned.split())

graph = Graph([clean, count])
runner = SyncRunner()

# Process multiple texts
texts = ["Hello World", "  Foo Bar  ", "Testing 123"]
results = runner.map(graph, {"text": texts}, map_over="text")

# Results is a list of dicts
for r in results:
    print(f"{r['cleaned']}: {r['word_count']} words")
# hello world: 2 words
# foo bar: 2 words
# testing 123: 2 words
```

## Map Over Multiple Parameters

### Zip Mode (Default)

Parallel iteration — lists must be equal length:

```python
@node(output_name="result")
def add(a: int, b: int) -> int:
    return a + b

graph = Graph([add])

# Zip: (1,10), (2,20), (3,30)
results = runner.map(
    graph,
    {"a": [1, 2, 3], "b": [10, 20, 30]},
    map_over=["a", "b"],
    map_mode="zip",
)

print([r["result"] for r in results])  # [11, 22, 33]
```

### Product Mode

Cartesian product — all combinations:

```python
# Product: (1,10), (1,20), (2,10), (2,20), (3,10), (3,20)
results = runner.map(
    graph,
    {"a": [1, 2, 3], "b": [10, 20]},
    map_over=["a", "b"],
    map_mode="product",
)

print([r["result"] for r in results])  # [11, 21, 12, 22, 13, 23]
```

## Fixed Parameters

Parameters not in `map_over` are fixed across all iterations:

```python
@node(output_name="result")
def process(text: str, model: str) -> str:
    return models[model].process(text)

graph = Graph([process])

# 'model' is fixed, 'text' varies
results = runner.map(
    graph,
    {"text": ["hello", "world"], "model": "gpt-5.2"},
    map_over="text",  # Only text varies
)
```

## Async Batch Processing

Use `AsyncRunner` for concurrent processing:

```python
from hypergraph import AsyncRunner

@node(output_name="embedding")
async def embed(text: str) -> list[float]:
    return await embedder.embed(text)

graph = Graph([embed])
runner = AsyncRunner()

# Process concurrently with controlled parallelism
results = await runner.map(
    graph,
    {"text": texts},
    map_over="text",
    max_concurrency=10,  # Max 10 parallel requests
)
```

## Nested Graphs with Map

Fan out a nested graph over a collection:

```python
# Single-item graph
item_processor = Graph([clean, extract, classify], name="processor")

# Batch graph that fans out
@node(output_name="items")
def load_items(path: str) -> list[str]:
    return read_file(path).split("\n")

@node(output_name="summary")
def summarize(results: list[dict]) -> dict:
    return aggregate(results)

batch_pipeline = Graph([
    load_items,
    item_processor.as_node().map_over("item"),  # Fan out
    summarize,
])
```

For many real systems, this is the most natural Hypergraph pattern:

1. Build and test the single-item workflow first
2. Name it with `Graph(..., name="...")`
3. Reuse it as a node with `.as_node()`
4. Add `.map_over(...)` when you need batch scale

This keeps the core logic small and reusable instead of mixing per-item logic with batch orchestration.

## Working with MapResult

`runner.map()` returns a `MapResult` — a read-only sequence with batch-level metadata and aggregate accessors. It's fully backward compatible: `len()`, iteration, and indexing all work as before.

```python
results = runner.map(graph, {"text": texts}, map_over="text")

# Quick overview
print(results.summary())    # "5 items | 5 completed | 42ms"

# Collect values across all items by key
word_counts = results["word_count"]  # [2, 2, 2]

# Collect with a default for failed items
word_counts = results.get("word_count", 0)  # [2, 2, 0, 2, ...]

# Batch metadata
results.run_id              # Unique batch ID
results.total_duration_ms   # Wall-clock time for the entire batch
results.map_over            # ("text",)

# JSON-serializable export (for logging, dashboards, agents)
results.to_dict()           # Full batch metadata + per-item results
```

## Error Handling

Control what happens when individual items fail using the `error_handling` parameter.

### Fail-Fast (Default)

By default, `map()` stops on the first failure and raises the exception. This is useful during development and when failures indicate a systematic bug:

```python
# Raises immediately if any item fails
results = runner.map(graph, {"text": texts}, map_over="text")
```

### Continue on Error

Use `error_handling="continue"` to collect all results, including failures. `MapResult` provides aggregate status and filtering:

```python
results = runner.map(
    graph,
    {"text": texts},
    map_over="text",
    error_handling="continue",
)

# Aggregate status
if results.failed:
    print(f"{len(results.failures)} items failed out of {len(results)}")

# Collect values with None for failures
results["result"]                  # [value, value, None, value, ...]

# Collect with a custom default
results.get("result", "N/A")      # [value, value, "N/A", value, ...]

# Iterate individual results
for r in results:
    if r.failed:
        print(f"Error: {r.error}")
    else:
        print(f"Success: {r['result']}")
```

### Error Handling in Nested Graphs

When using `map_over()` on a nested graph, error handling works the same way. Failed items produce `None` placeholders to preserve list alignment with inputs:

```python
gn = processor.as_node().map_over("item", error_handling="continue")

batch = Graph([load_items, gn, summarize])
result = runner.run(batch, {"path": "data.txt"})

# result["output"] → [value, value, None, value, ...]
# None entries correspond to failed items
```

## runner.map() vs map_over

Two batch patterns, different tradeoffs. Both start from the same idea — **write logic for one item, scale to many** — but they give different guarantees:

| | `runner.map()` | `map_over` |
|---|---|---|
| **Returns** | `MapResult` (N `RunResult`s) | One `RunResult` with list outputs |
| **Error isolation** | Per-item — failures don't affect other items | Whole step — one failure can fail the batch |
| **Tracing** | Per-item RunLogs with full routing/timing | One RunLog (batch is a single step) |
| **Checkpointing** | Parent batch run + per-item child runs | Persisted as one run step |
| **Product mode** | Yes — `map_mode="product"` with multi-key `map_over` | Yes — `mode="product"` for cartesian product |
| **Use in pipelines** | Top-level batch processing | Step inside a larger graph |

### When to use runner.map()

```python
# Independent items where failures should be isolated
results = runner.map(graph, {"url": urls}, map_over="url", error_handling="continue")

# results.failures → only failed items
# results["data"] → [value, value, None, value, ...]
```

- Processing independent items (scraping, embedding, classification)
- When you need per-item RunLogs for debugging
- When batch items should behave like separate workflow runs
- Quick fan-out over a single parameter
- With `workflow_id`: persisted batch with per-item child runs

### When to use map_over

```python
# Batch step inside a larger pipeline
inner = Graph([embed, classify], name="processor")
pipeline = Graph([
    load_items,
    inner.as_node().map_over("item"),  # Fan out
    aggregate,
])
result = runner.run(pipeline, {"path": "data.csv"})
```

- Batch step inside a larger pipeline (load → process_all → aggregate)
- When you need checkpoint persistence for the batch
- Cartesian product mode (`mode="product"`)
- When the batch is part of a nested graph hierarchy

### Checkpointing with map()

Pass a `workflow_id` to `runner.map()` to persist batch results. This creates a parent batch run and per-item child runs:

```python
from hypergraph import SyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

cp = SqliteCheckpointer("./runs.db")
runner = SyncRunner(checkpointer=cp)

results = runner.map(
    graph,
    {"x": [1, 2, 3]},
    map_over="x",
    workflow_id="batch-001",
)
# Creates:
#   batch-001    (parent batch run)
#   batch-001/0  (child — x=1)
#   batch-001/1  (child — x=2)
#   batch-001/2  (child — x=3)

# Query later
cp.runs(parent_run_id="batch-001")  # list child runs
cp.values("batch-001/1")            # values for item 1
```

From the CLI:

```bash
hypergraph runs ls --parent batch-001
hypergraph runs show batch-001/1
```

Without `workflow_id`, `runner.map()` still works but results exist only in-process.

### Run Lineage: Resume vs Fork

`run()` now uses strict, git-like lineage semantics when a checkpointer is configured:

- Same `workflow_id` means "same lineage"
- Resume is strict: no new runtime values
- Structural graph changes require fork
- Completed workflows are terminal (fork to branch)

When `workflow_id` is omitted and a checkpointer exists, `run()` auto-generates one and returns it in `result.workflow_id`.

```python
result = await runner.run(graph, {"x": 5})  # auto-id when checkpointer is set
print(result.workflow_id)  # e.g. "run-20260302-a7b3c2"
```

#### Resume (same workflow_id, no new values)

```python
from hypergraph import Graph, node, AsyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3

cp = SqliteCheckpointer("./runs.db")
runner = AsyncRunner(checkpointer=cp)

# First run
await runner.run(Graph([double, triple]), {"x": 5}, workflow_id="job-1")

# Resume attempt on completed workflow raises WorkflowAlreadyCompletedError
# await runner.run(Graph([double, triple]), workflow_id="job-1")
```

Resume is intended for ACTIVE/FAILED workflows (e.g., retry after failure), not for appending new work to completed lineages.

#### Fork (new workflow_id, optional overrides)

Fork by workflow ID when you want to branch history, override inputs, or run a changed graph:

```python
forked = await runner.run(
    Graph([double, triple]),
    {"x": 100},                      # optional overrides
    fork_from="job-1",
)
assert forked["tripled"] == 600
```

Retry is symmetrical:

```python
retried = await runner.run(
    Graph([double, triple]),
    retry_from="job-1",
)
```

If you pass runtime values with an existing workflow ID, `run()` raises `InputOverrideRequiresForkError`.
If graph structure changed for an existing workflow ID, `run()` raises `GraphChangedError`.

### Resuming Batches

When you re-run `map()` with the same `workflow_id`, completed items are automatically skipped. Only failed or unfinished items are re-executed:

```python
# First run: item x=20 fails
results = await runner.map(
    graph,
    {"x": [10, 20, 30]},
    map_over="x",
    workflow_id="batch-retry",
    error_handling="continue",
)
# batch-retry/0 → COMPLETED
# batch-retry/1 → FAILED (x=20 error)
# batch-retry/2 → COMPLETED

# Fix the bug, then re-run with the same workflow_id
results = await runner.map(
    graph,
    {"x": [10, 20, 30]},
    map_over="x",
    workflow_id="batch-retry",
    error_handling="continue",
)
# batch-retry/0 → skipped (already COMPLETED)
# batch-retry/1 → re-executed (was FAILED)
# batch-retry/2 → skipped (already COMPLETED)
```

This makes it safe to retry large batches — you only pay for the items that actually need re-processing.

### CLI batch execution

You can also run batch operations directly from the terminal:

```bash
# Map over a parameter
hypergraph map my_module:graph --map-over x --values '{"x": [1, 2, 3]}'

# With checkpointing (requires --workflow-id to activate persistence)
hypergraph map my_module:graph --map-over x --values '{"x": [1, 2, 3]}' --workflow-id batch-001 --db ./runs.db
```

See [Debug Workflows — CLI](debug-workflows.md#run--execute-a-graph) for full CLI reference.

## When to Use Map vs Loop

| Use `runner.map()` or `map_over` | Use a Python loop |
|----------------------------------|-------------------|
| Same graph, different inputs | Different graphs per item |
| Want parallel execution | Need sequential dependencies |
| Processing a collection | One-off processing |

## What's Next?

- [Testing Without Framework](test-without-framework.md) — Test your nodes directly
- [Hierarchical Composition](../03-patterns/04-hierarchical.md) — Nest graphs with map_over
