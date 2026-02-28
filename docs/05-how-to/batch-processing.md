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
| **Checkpointing** | Ephemeral — not persisted | Persisted as one run step |
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
- Ephemeral batch jobs that don't need persistence
- Quick fan-out over a single parameter

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

### Persistence note

`runner.map()` is **ephemeral** — results exist only in-process. If you're using a checkpointer and need batch results to persist across process restarts, use `map_over` with a `workflow_id`:

```python
# Ephemeral — gone after process exits
results = runner.map(graph, {"x": items}, map_over="x")

# Persistent — queryable from CLI or another process
inner = Graph([process], name="pipeline")
outer = Graph([inner.as_node().map_over("x")])
result = await runner.run(outer, {"x": items}, workflow_id="batch-001")

# Later, from CLI:
# $ hypergraph runs values batch-001
```

### CLI batch execution

You can also run batch operations directly from the terminal:

```bash
# Map over a parameter
hypergraph map my_module:graph --map-over x --values '{"x": [1, 2, 3]}'

# With checkpointing
hypergraph map my_module:graph --map-over x --values '{"x": [1, 2, 3]}' --db ./runs.db
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
