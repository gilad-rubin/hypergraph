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

Hypergraph's Daft extra requires `daft>=0.7.14`. The integration targets
Daft's current UDF APIs (`daft.func`, `daft.func.batch`, `daft.cls`,
`daft.method`, and `daft.method.batch`) and their typed resource/retry
options.

## The Mental Model

`DaftRunner` translates your graph into a Daft query plan:

1. Plain `@node` functions become `@daft.func` UDFs.
2. `hypergraph.integrations.daft.node(..., batch=True)` becomes `@daft.func.batch`.
3. Bound `@stateful` resources become `@daft.cls` wrappers with `@daft.method` or `@daft.method.batch`.
4. Nodes are chained in topological order via `df.with_column()`.
5. Nested `GraphNode`s run their inner graph via `SyncRunner` inside a single UDF.

An item can still contain lists, and those lists can still be processed with a
nested `GraphNode.map_over(...)`.

Pick the entrypoint that matches your data:

- Use `runner.map(...)` when your outer dataset is a Python collection — returns `MapResult`.
- Use `runner.map_dataframe(...)` when your dataset is a Daft DataFrame — returns a Daft DataFrame (no Python conversion).

Use the integration namespace only when you want Daft-specific execution
controls. Plain `@node` stays backend-neutral and still auto-lowers under
`DaftRunner`; `daft_node` is for Daft-only dtype, batch, retry, resource, and
concurrency settings.

Migration note: older examples that used `@node(..., batch=True)` should use
`@daft_node(..., batch=True, return_dtype=...)` instead.

## Standalone Examples

These checked-in scripts are the clearest place to start:

- `examples/daft/text_processing.py` - linear DAG with sync nodes, single run + batch
- `examples/daft/async_api_calls.py` - async nodes in a diamond DAG (Daft handles the event loop)
- `examples/daft/ml_embeddings.py` - `@stateful` decorator for once-per-worker model loading
- `examples/daft/batch_normalization.py` - vectorized batch UDFs with `daft_node(..., batch=True)`
- `examples/daft/advanced_udf_patterns.py` - stateful + batch UDF patterns inspired by Daft docs
- `examples/daft/nested_document_scoring.py` - `GraphNode` with `map_over` inside DaftRunner
- `examples/daft/quickstart_customer_enrichment.py` - quickstart-style tabular enrichment
- `examples/daft/hierarchical_document_batches.py` - nested `GraphNode.map_over(...)` inside a batch
- `examples/daft/nested_review_objects.py` - complex Python objects through Daft execution
- `examples/framework_ports/daft_workflows.py` - DataFrame-first ports for quickstart, dataset scoring, and nested analysis

## Quickstart-Style Batch Enrichment

This mirrors the spirit of Daft's quickstart tabular transforms, but with
Hypergraph's "think singular, then map it" pattern.

```python
from hypergraph import Graph, node
from hypergraph.integrations.daft import DaftRunner

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
from hypergraph import Graph, node
from hypergraph.integrations.daft import DaftRunner

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
        sentence_graph.as_node(name="analyze").rename_inputs(text="sentences").map_over("sentences"),
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

## DataFrame-First Execution

If the dataset already lives in a Daft DataFrame, use `map_dataframe` to keep
everything in Daft's columnar format — no roundtrip through Python dicts.

```python
import daft
from hypergraph import Graph, node
from hypergraph.integrations.daft import DaftRunner

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

graph = Graph([double], name="doubler")
runner = DaftRunner()

df = daft.from_pydict({"x": [1, 2, 3]})
result_df = runner.map_dataframe(graph, df)
# result_df is a Daft DataFrame with columns: x, doubled
result_df.show()
```

Broadcast values (shared across all rows) are passed as keyword arguments
and captured in UDF closures — they don't become DataFrame columns:

```python
@node(output_name="greeting")
def greet(name: str, prefix: str) -> str:
    return f"{prefix}, {name}!"

graph = Graph([greet], name="greeter")
df = daft.from_pydict(
    {
        "name": ["Alice", "Bob"],
        "source": ["crm", "csv"],
    }
)

result_df = DaftRunner().map_dataframe(graph, df, prefix="Hi")
# Each row gets prefix="Hi" via the UDF closure
# The source passthrough column is preserved in result_df
```

`map_dataframe` preserves passthrough columns by default. Graph outputs must
not collide with existing DataFrame columns; rename the node output or drop the
existing column before calling `map_dataframe`.

## Async Nodes

Daft handles async UDFs natively, so async nodes work without any extra
configuration:

```python
import asyncio
from hypergraph import Graph, node
from hypergraph.integrations.daft import DaftRunner

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
- you have stateful resources (ML models) that should load once per worker (`@stateful`)
- you need vectorized batch operations (`daft_node(..., batch=True)`)

Prefer `SyncRunner` or `AsyncRunner` when:

- your graph has cycles, gates, or interrupts
- you need per-step event processors or progress bars
- you need checkpointing, resume, or fork semantics
- your inputs are already plain Python collections and you do not need Daft at the dataset boundary

## Stateful UDFs

Heavy resources (ML models, DB connections) can be initialized once per Daft
worker instead of once per row. Use the `@stateful` decorator and bind the lazy
handle to the graph:

```python
from hypergraph import Graph, node
from hypergraph.integrations.daft import DaftRunner, stateful

@stateful(max_concurrency=2)
class Embedder:
    def __init__(self, model_name: str):
        self.model = load_heavy_model(model_name)

    def embed(self, text: str) -> list[float]:
        return self.model.encode(text)

@node(output_name="embedding")
def embed(text: str, embedder: Embedder) -> list[float]:
    return embedder.embed(text)

graph = Graph([embed]).bind(embedder=Embedder("all-MiniLM-L6-v2"))
runner = DaftRunner()
results = runner.map(graph, {"text": texts}, map_over="text")
```

DaftRunner wraps stateful objects with `@daft.cls` so `__init__` runs once per
worker process. The constructor arguments are captured by the lazy handle and
used inside the Daft worker, so expensive model/client setup is not accidentally
triggered while defining the graph or building a lazy plan.
See `examples/daft/ml_embeddings.py` for a complete example.

## Vectorized Batch UDFs

For operations that benefit from processing all rows at once (NumPy, Arrow),
use `hypergraph.integrations.daft.node` to opt into Daft batch execution.
Batch nodes receive `daft.Series` inputs instead of scalars:

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
results = runner.map(graph, {"values": [10.0, 20.0, 30.0, 40.0, 50.0]}, map_over="values")
```

See `examples/daft/batch_normalization.py` for a complete example.

## Daft Options

Pass Daft UDF controls directly to `daft_node` as keyword arguments:

```python
import daft
from hypergraph.integrations.daft import node as daft_node

@daft_node(
    output_name="embedding",
    return_dtype=daft.DataType.list(daft.DataType.float64()),
    batch=True,
    batch_size=64,
    max_retries=2,
    on_error="log",
)
def embed_batch(text: daft.Series) -> list[list[float] | None]:
    return embed_many(text.to_pylist())
```

These options are validated against Daft's current public rules at definition
time: `on_error` is one of `"raise"`, `"log"`, or `"ignore"`; `max_concurrency`
must be positive; `gpus` above `1.0` must be whole numbers; and `ray_options`
cannot include `"num_cpus"`, `"num_gpus"`, or `"memory"`. Put class-level
resource controls (`cpus`, `gpus`, `max_concurrency`, ...) on `@stateful(...)`;
put dtype, batch, and unnest settings on `daft_node(...)`.

## Dashboard and Extensions

Hypergraph does not wrap Daft's dashboard or native extension APIs. Configure
them the same way you would for a direct Daft script:

```bash
daft dashboard start
DAFT_DASHBOARD_URL=http://localhost:3238 python my_workflow.py
```

Native extensions should be loaded before running the graph:

```python
import daft
import hello_extension

daft.load_extension(hello_extension)

result_df = DaftRunner().map_dataframe(graph, frame)
```

The Daft dashboard protocol and native extension ABI are still evolving
upstream, so this integration keeps those as Daft-owned setup concerns.

## Current Limitations

- **DAGs only** - Cycles and gates are not supported; use `SyncRunner` or `AsyncRunner` for those. Interrupts require `AsyncRunner`.
- **No checkpointing** - `DaftRunner` does not support `workflow_id`, resume, or fork semantics.
- **No event processors** - Event processors are accepted but ignored with a warning.
- **No runner delegation** - `with_runner()` on nested GraphNodes is rejected. DaftRunner translates the entire graph to Daft UDFs.
- **Nested GraphNodes use SyncRunner internally** - The inner graph runs via `SyncRunner` inside a Daft UDF. Nested graphs with async nodes are rejected at plan time.
