# HyperTable — Incremental Persistent Tables

HyperTable turns a Hypergraph graph into a persistent, incremental table. Each graph input becomes a source column, each node output becomes a derived column. A content-addressed fingerprint on each row decides whether to re-derive on the next insert — unchanged rows are skipped.

## When to Use HyperTable

Use HyperTable when your pipeline:

- **Re-runs frequently** and you want to skip unchanged work (nightly ingestion, continuous sync)
- **Processes collections with sub-items** — documents with pages, videos with utterances, datasets with rows — where each sub-item should be tracked independently
- **Calls expensive external services** (LLMs, embedding APIs) and you can't afford to re-call them for unchanged inputs
- **Needs partial failure tolerance** — one failed item shouldn't discard work done for all the others

## The Core Idea

A regular Hypergraph graph is stateless — run it, get output, forget:

```python
result = runner.run(graph, text="hello world")
# result["embedding"] = [0.1, 0.2, ...]
# ... gone after the process exits
```

HyperTable adds identity, storage, and incrementality:

```python
from hypergraph import node, Graph
from hypergraph.materialization import HyperTable
from hypergraph.runners import SyncRunner

@node(output_name="clean_text")
def clean(text: str) -> str:
    return text.strip().lower()

@node(output_name="embedding")
def embed(clean_text: str, embedder) -> list[float]:
    return embedder.embed(clean_text)

# Same graph, but persistent and incremental
docs = HyperTable(
    [clean, embed],
    identity="doc_id",
    store=store,
).bind(embedder=my_embedder).with_runner(SyncRunner())

# First insert: runs the graph, stores all columns
docs.insert(doc_id="d1", text="hello world")

# Second insert, same data: skipped (fingerprint match)
docs.insert(doc_id="d1", text="hello world")

# Changed source: re-derives clean_text and embedding
docs.insert(doc_id="d1", text="hello universe")

# Swap embedder: fingerprint changes (includes component config hash)
docs2 = docs.bind(embedder=better_embedder)
docs2.insert(doc_id="d1", text="hello universe")  # re-embeds
```

## What Triggers Re-Derivation

The row fingerprint is `hash(source values + node definition hashes + component config hashes)`. Any of these changing triggers re-derivation:

| What changed | Example | Effect |
|---|---|---|
| Source column value | `text="hello"` → `text="goodbye"` | Row re-derives |
| Node function body | Edit the `clean()` function | All rows re-derive on next insert/sync |
| Component config | Swap `Embedder("v1")` for `Embedder("v2")` | All rows re-derive on next insert/sync |
| Nothing | Same source, same code, same components | Row skipped |

## Child Tables

When a single item expands into many sub-items (a document into pages, a video into utterances), use `map_over` to create a child table:

```python
from typing import TypedDict

class Page(TypedDict):
    page_id: str
    text: str

@node(output_name="pages")
def split(raw_text: str) -> list[Page]:
    chunks = raw_text.split("\n\n")
    return [Page(page_id=f"p{i}", text=c) for i, c in enumerate(chunks)]

@node(output_name="embedding")
def embed_page(text: str, embedder) -> list[float]:
    return embedder.embed(text)

@node(output_name="raw_text")
def extract_text(content: str) -> str:
    return content  # your real extractor: PDF/HTML/etc. -> text

page_graph = Graph([embed_page], name="process_page")

docs = HyperTable(
    [extract_text, split,
     page_graph.as_node().map_over("pages", identity="page_id")],
    identity="doc_id",
    store=store,
).bind(embedder=my_embedder).with_runner(SyncRunner())
```

Each page gets its own row in a child table, its own fingerprint (scoped to the child graph), and its own skip logic. Re-inserting a document only re-processes pages whose inputs changed.

## Error Isolation

By default, a failed derivation raises an exception (backward compatible). Set `on_error="store"` to write error rows instead — successful siblings are unaffected:

```python
docs = HyperTable(
    [split, page_graph.as_node().map_over("pages", identity="page_id")],
    identity="doc_id",
    store=store,
    on_error="store",
).bind(embedder=my_embedder).with_runner(SyncRunner())

# Page 3 fails (rate limit), pages 1-2 and 4-5 succeed
docs.insert(doc_id="d1", text="...")

# Inspect errors
errors = docs.filter_children(
    where=[("_status", "eq", "error")],
    include_status=True,
)
# [{'page_id': 'p3', '_status': 'error', '_error': 'RateLimitError: ...'}]

# Retry — only the errored page re-runs, successful pages are skipped
docs.insert(doc_id="d1", text="...")
```

## How It Fits Together

```text
                    HyperTable
                   ┌───────────────────────────────┐
 insert()/sync()   │  Graph         TableStore      │
 ─────────────────►│  (compute) ──► (persistence)   │
                   │                                │
 get()/filter()    │                                │
 ◄─────────────────│          read from store       │
                   └───────────────────────────────┘
```

- **Graph** defines the computation (nodes, edges, auto-wiring)
- **Store** handles persistence (LanceDB, or any `TableStore` implementation)
- **Runner** executes the graph (`SyncRunner` or `AsyncRunner`)
- **Identity** is the stable key for each row — explicit, not convention-based
- **Fingerprint** decides skip vs re-derive — automatic, no manual cache invalidation

## Next Steps

- [Getting Started](getting-started.md) — build your first table, understand incrementality, child tables, error handling
- [API Reference](api-reference.md) — complete method documentation
- [Example: Document Processing](examples/document-processing.md) — full pipeline with LLM enrichment and embeddings
- [Example: Media Knowledge Base](examples/media-knowledge-base.md) — video/audio transcription and search index
