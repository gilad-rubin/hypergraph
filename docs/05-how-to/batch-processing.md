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
print(results.summary())    # "5 items | 5 completed | avg 42ms/item"

# Collect values across all items by key
word_counts = results["word_count"]  # [2, 2, 2]

# Collect with a default for failed items
word_counts = results.get("word_count", 0)  # [2, 2, 0, 2, ...]

# Batch metadata
results.run_id              # Unique batch ID
results.total_duration_ms   # Wall-clock time for the entire batch
results.map_over            # ("text",)
results.requested_count     # Requested inputs, including unstarted work
results.unstarted_item_indexes  # Original indexes never claimed after stop
results.restored_count      # Checkpoint-skipped successes (subset of completed)

# JSON-serializable export (for logging, dashboards, agents)
results.to_dict()           # Full batch metadata + per-item results
```

For an ordinary completed or empty map,
`results.requested_count == len(results)` and
`results.unstarted_item_indexes == ()`. If cooperative stop curtails a
background map, `len(results)` counts only real claimed outcomes while
`requested_count` preserves the original scope. No placeholder `RunResult` is
created for an input that never started.

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

# Any-item failure check (status-independent)
if results.any_failed:
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

`map_over()` does not support nested graphs that contain interrupts. If the
wrapped graph has an `@interrupt`, graph construction raises a
`GraphConfigError`. For human-in-the-loop batch workflows, use
`AsyncRunner.map()` with one item per run instead.

## Keep Control While a Batch Runs

Use `start_map()` when the application must accept another action—such as a
user's Stop request—before the batch settles:

```python
# Before: the caller waits here for the entire batch.
batch = runner.map(order_graph, {"order_id": order_ids}, map_over="order_id")

# After: the caller immediately receives a live control handle.
handle = runner.start_map(
    order_graph,
    {"order_id": order_ids},
    map_over="order_id",
)
show_stop_button()

# Later, if Maya stops the batch:
handle.stop(info={"requested_by": "Maya"})
batch = handle.result(raise_on_failure=False)
```

Background mapping captures item failures and continues claiming siblings
until the batch settles or stop curtails it. Default `result()` then raises the
first real failed item in original input order; `raise_on_failure=False`
returns the settled batch with `failures` intact.

```python
print(len(batch))                    # real claimed outcomes only
print(batch.requested_count)         # original requested scope
print(batch.unstarted_item_indexes)  # sorted original indexes never claimed
```

A curtailed batch has `status == RunStatus.STOPPED` even when a real attempted
item failed. In that case `batch.stopped` is true and `batch.failed` is false
(it mirrors the aggregate status), while `batch.any_failed` and
`batch.failures` preserve the attempted-item failures. A stop received only
after every input settles does not rewrite the ordinary
completed/partial/failed aggregation.

See [Control Work After It Starts](control-background-execution.md) for sync
and async submission, cancellation isolation, duplicate workflow IDs, and
process recovery.

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

Without `workflow_id`, `runner.map()` still works but results exist only in-process.

### Run Lineage: Resume vs Fork

See [Checkpointers](../06-api-reference/checkpointers.md) for the `Checkpointer` ABC, `CheckpointPolicy`, and the `lineage()` view used to audit fork/retry trees. This section covers the `run()`-level resume/fork input rules.

`run()` now uses strict, git-like lineage semantics when a checkpointer is configured:

- Same `workflow_id` means "same lineage"
- Resume is strict: active/failed runs reject new runtime values; paused runs accept interrupt responses
- A stopped run requires a non-empty runtime mapping to resume, or `override_workflow=True` to fork
- Structural graph changes require fork
- Completed workflows are terminal (fork to branch)

When `workflow_id` is omitted on a fresh or retry run and a checkpointer exists, `run()` generates a generic ID and returns it in `result.workflow_id`. A `fork_from=` call instead derives `{source}-fork-{hex}`.

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

Ordinary resume covers ACTIVE/PAUSED/FAILED workflows (for example: still-running work, interrupt-paused workflows, or retry after failure). STOPPED workflows use the explicit-signal rule below. Completed lineages remain terminal.

A persisted `STOPPED` workflow needs an explicit signal. `None` and `{}` raise `WorkflowStoppedError` before a new run event or persistence write. Pass any non-empty, otherwise-valid runtime mapping to resume the same ID, or use `override_workflow=True` to leave the source untouched and create a fork:

```python
# Same lineage
resumed = await runner.run(graph, {"x": 2}, workflow_id="stopped-job")

# New source-derived lineage
forked = await runner.run(
    graph,
    {"x": 2},
    workflow_id="stopped-job",
    override_workflow=True,
)
assert forked.workflow_id.startswith("stopped-job-fork-")
```

#### Fork (new workflow_id, optional overrides)

Fork by workflow ID when you want to branch history, override inputs, or run a changed graph:

```python
forked = await runner.run(
    Graph([double, triple]),
    {"x": 100},                      # optional overrides
    fork_from="job-1",
)
assert forked["tripled"] == 600
assert forked.workflow_id.startswith("job-1-fork-")
```

Retry is symmetrical:

```python
retried = await runner.run(
    Graph([double, triple]),
    retry_from="job-1",
)
```

If you pass runtime values while resuming an ordinary active or failed workflow ID, `run()` raises `InputOverrideRequiresForkError`. Stopped workflows use the explicit-signal rule above, and paused workflows accept interrupt responses.
If graph structure changed for an existing workflow ID, `run()` raises `GraphChangedError`. If a node's retry/timeout policy changed, it raises `RetryPolicyChangedError` with a field-level diff (see [Checkpointers — Policy Compatibility on Resume](../06-api-reference/checkpointers.md#policy-compatibility-on-resume)).

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

assert [item.restored for item in results] == [True, False, True]
assert results.restored_count == 2
assert results[0].log.steps[0].status == "restored"
```

Restored items remain included in completed counts, but are also reported as a separate subset in `MapResult`, `MapLog`, parent events, and telemetry. Average item duration uses only freshly executed completed items with real logs, so a fully restored batch omits the average instead of displaying fake `0ms` work. This makes it safe to retry large batches — you only pay for the items that actually need re-processing, and the result shows which items were skipped.

**Compatibility.** Each completed item is matched by a persisted signature of its inputs, and that signature is authoritative: an item is restored only when its current inputs match, even if the item sits at the same position as before — changed or unmatched inputs re-execute fresh (never an error, never a stale result). Children persisted by pre-signature versions of hypergraph carry no signature, and only those legacy children keep the old position-based fallback: they are restored by their numeric index. Each stored child is restored at most once per resume, so duplicate inputs claim their matching runs one-for-one (in stable run-id order) and any extra duplicates execute fresh.

## When to Use Map vs Loop

| Use `runner.map()` or `map_over` | Use a Python loop |
|----------------------------------|-------------------|
| Same graph, different inputs | Different graphs per item |
| Want parallel execution | Need sequential dependencies |
| Processing a collection | One-off processing |

## What's Next?

- [Testing Without Framework](test-without-framework.md) — Test your nodes directly
- [Hierarchical Composition](../03-patterns/04-hierarchical.md) — Nest graphs with map_over
