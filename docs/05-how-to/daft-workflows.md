# How to Use Hypergraph with Daft

- **Best fit** - Use `DaftRunner` when you want columnar batch execution: each node becomes a Daft UDF chained via `df.with_column()`.
- **DAGs only** - DaftRunner requires DAG graphs. No cycles, gates, or interrupts.
- **Async support** - Async nodes work natively (Daft handles the event loop).
- **Where Hypergraph helps** - Nested graphs and `GraphNode.map_over(...)` still work inside each item, so you can keep the "think singular, then scale" pattern.

This guide ports the shape of several official Daft examples into Hypergraph's
workflow model:

- [Daft Quickstart](https://docs.daft.ai/en/stable/quickstart/)
- [Tutorials](https://docs.daft.ai/en/stable/tutorials/)
- [Examples](https://docs.daft.ai/en/stable/examples/)

## Install

```bash
uv sync --extra daft
```

## The Mental Model

`DaftRunner` translates your graph into a Daft query plan:

1. Each node becomes a Daft UDF (via `@daft.func`, `@daft.cls`, or `@daft.func.batch`).
2. Nodes are chained in topological order via `df.with_column()`.
3. Nested `GraphNode`s run their inner graph via `SyncRunner` inside a single UDF.

An item can still contain lists, and those lists can still be processed with a
nested `GraphNode.map_over(...)`.

Pick the entrypoint that matches your data:

- Use `runner.map(...)` when your outer dataset is already a Python collection.
- Use `runner.map_dataframe(...)` when your dataset already lives in a Daft DataFrame.

## Standalone Examples

These checked-in scripts are the clearest place to start:

- `examples/daft/text_processing.py` - linear DAG with sync nodes, single run + batch
- `examples/daft/async_api_calls.py` - async nodes in a diamond DAG (Daft handles the event loop)
- `examples/daft/ml_embeddings.py` - `DaftStateful` protocol for once-per-worker model loading
- `examples/daft/batch_normalization.py` - vectorized batch UDFs with `mark_batch()`
- `examples/daft/nested_document_scoring.py` - `GraphNode` with `map_over` inside DaftRunner
- `examples/daft/quickstart_customer_enrichment.py` - quickstart-style tabular enrichment
- `examples/daft/hierarchical_document_batches.py` - nested `GraphNode.map_over(...)` inside a batch
- `examples/daft/nested_review_objects.py` - complex Python objects through Daft execution
- `examples/framework_ports/daft_workflows.py` - DataFrame-first ports for quickstart, dataset scoring, and nested analysis

## Quickstart-Style Batch Enrichment

This mirrors the spirit of Daft's quickstart tabular transforms, but with
Hypergraph's "think singular, then map it" pattern.

```python
from hypergraph import DaftRunner, Graph, node

@node(output_name="full_name")
def full_name(first_name: str, last_name: str) -> str:
    return f"{first_name.strip().title()} {last_name.strip().title()}"

@node(output_name="age_band")
def age_band(age: int) -> str:
    if age < 30:
        return "early-career"
    if age < 50:
        return "mid-career"
    return "senior"

graph = Graph([full_name, age_band], name="customer_enrichment")
runner = DaftRunner()

results = runner.map(
    graph,
    {
        "first_name": ["shandra", " zaya "],
        "last_name": ["shamas", "zaphora"],
        "age": [57, 40],
    },
    map_over=["first_name", "last_name", "age"],
)
```

Standalone example:

- `examples/daft/quickstart_customer_enrichment.py`

## Hierarchical Graphs and Nested `map_over`

This is where Hypergraph adds something useful to Daft-style pipelines:
reusable inner workflows.

```python
from hypergraph import DaftRunner, Graph, node

@node(output_name="sentences")
def split_sentences(document: str) -> list[str]:
    return [part.strip() for part in document.split(".") if part.strip()]

@node(output_name="cleaned")
def clean_sentence(text: str) -> str:
    return " ".join(text.lower().split())

@node(output_name="score")
def score_sentence(cleaned: str) -> int:
    return len(cleaned.split())

sentence_graph = Graph([clean_sentence, score_sentence], name="sentence_graph")

workflow = Graph(
    [
        split_sentences,
        sentence_graph.as_node(name="analyze").with_inputs(text="sentences").map_over("sentences"),
    ],
    name="document_triage",
)

runner = DaftRunner()
result = runner.run(workflow, document="Refund requested. Checkout blocked.")
batch = runner.map(workflow, {"document": ["Refund requested.", "Weekly roadmap update."]}, map_over="document")
```

This is the key Hypergraph move:

1. Write the sentence scorer once.
2. Wrap it with `.as_node()`.
3. Add `.map_over("sentences")`.
4. Run the outer graph through `DaftRunner`.

Standalone example:

- `examples/daft/hierarchical_document_batches.py`

## DataFrame-First Row Execution

If the dataset already lives in Daft, keep the DataFrame at the outer boundary
and hand each row to a Hypergraph workflow.

```python
import daft

from hypergraph import DaftRunner
from examples.framework_ports.daft_workflows import build_daft_llm_dataset_graph

frame = daft.from_pylist(
    [
        {"query": "alpha", "chunks": ["alpha alpha beta", "alpha beta gamma"]},
        {"query": "refund", "chunks": ["refund api timeout fixed in patch 2026.03"]},
    ]
)

runner = DaftRunner()
results = runner.map_dataframe(build_daft_llm_dataset_graph(), frame)

print(results[0]["chunk_score"])  # [2, 1]
```

## Async Nodes

Daft handles async UDFs natively, so async nodes work without any extra
configuration:

```python
import asyncio
from hypergraph import DaftRunner, Graph, node

async def _mock_embed(text: str) -> list[float]:
    await asyncio.sleep(0.01)
    return [float(ord(c)) / 100 for c in text[:5]]

@node(output_name="embedding")
async def embed(text: str) -> list[float]:
    return await _mock_embed(text)

@node(output_name="score")
def score(embedding: list[float]) -> float:
    return sum(embedding) / len(embedding) if embedding else 0.0

graph = Graph([embed, score], name="async_pipeline")
runner = DaftRunner()
results = runner.map(graph, {"text": ["hello", "world"]}, map_over="text")
```

DaftRunner detects async functions and wraps them with `@daft.func` — Daft
manages the event loop internally. See `examples/daft/async_api_calls.py`.

## Complex Python Objects

DaftRunner carries Python-object payloads through Daft UDF columns, so you can
keep rich Python types at graph boundaries when that makes the workflow
clearer.

Standalone example:

- `examples/daft/nested_review_objects.py`

## When to Use `DaftRunner`

Use it when:

- your graph is a DAG (no cycles, gates, or interrupts)
- you want columnar batch execution or distributed fan-out via Daft
- the item workflow benefits from nested graphs, graph reuse, or internal `map_over(...)`
- you have stateful resources (ML models) that should load once per worker (`DaftStateful`)
- you need vectorized batch operations (`mark_batch`)

Prefer `SyncRunner` or `AsyncRunner` when:

- your graph has cycles, gates, or interrupts
- you need per-step event processors or progress bars
- you need checkpointing, resume, or fork semantics
- your inputs are already plain Python collections and you do not need Daft at the dataset boundary

## Stateful UDFs

Heavy resources (ML models, DB connections) can be initialized once per Daft
worker instead of once per row. Mark the class with `__daft_stateful__ = True`
and bind it to the graph:

```python
from hypergraph import DaftRunner, Graph, node

class Embedder:
    __daft_stateful__ = True

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

DaftRunner wraps stateful objects with `@daft.cls` so `__init__` runs once per
worker process. See `examples/daft/ml_embeddings.py` for a complete example.

## Vectorized Batch UDFs

For operations that benefit from processing all rows at once (NumPy, Arrow),
use `mark_batch()` to get `daft.Series` inputs instead of scalars:

```python
import daft
from hypergraph import DaftRunner, Graph, node
from hypergraph.runners.daft import mark_batch

@node(output_name="normalized")
def normalize(values: daft.Series) -> daft.Series:
    arr = values.to_pylist()
    mean = sum(arr) / len(arr)
    std = (sum((x - mean) ** 2 for x in arr) / len(arr)) ** 0.5
    if std == 0:
        return daft.Series.from_pylist([0.0] * len(arr))
    return daft.Series.from_pylist([round((x - mean) / std, 4) for x in arr])

mark_batch(normalize.func)

graph = Graph([normalize], name="batch_norm")
runner = DaftRunner()
results = runner.map(graph, {"values": [10.0, 20.0, 30.0, 40.0, 50.0]}, map_over="values")
```

See `examples/daft/batch_normalization.py` for a complete example.

## Current Limitations

- **DAGs only** - Cycles, gates, and interrupts are not supported. Use `SyncRunner` or `AsyncRunner` for those.
- **No checkpointing** - `DaftRunner` does not support `workflow_id`, resume, or fork semantics.
- **No event processors** - Event processors are accepted but ignored with a warning.
- **Nested GraphNodes use SyncRunner** - The inner graph runs via `SyncRunner` inside a Daft UDF, not as a native Daft subplan.
