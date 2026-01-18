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
    {"text": ["hello", "world"], "model": "gpt-4"},
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
    item_processor.as_node(map_over="item"),  # Fan out
    summarize,
])
```

## Error Handling

By default, errors in one item don't stop others:

```python
results = runner.map(graph, {"text": texts}, map_over="text")

for r in results:
    if r.status == RunStatus.FAILED:
        print(f"Error: {r.error}")
    else:
        print(f"Success: {r['result']}")
```

## When to Use Map vs Loop

| Use `runner.map()` | Use a Python loop |
|-------------------|-------------------|
| Same graph, different inputs | Different graphs per item |
| Want parallel execution | Need sequential dependencies |
| Processing a collection | One-off processing |

## What's Next?

- [Testing Without Framework](test-without-framework.md) — Test your nodes directly
- [Hierarchical Composition](../03-patterns/04-hierarchical.md) — Nest graphs with map_over
