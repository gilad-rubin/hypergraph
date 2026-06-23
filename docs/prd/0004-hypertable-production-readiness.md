# PRD 0004 — HyperTable production readiness

status: ready-for-agent

## Problem Statement

HyperTable (PRDs 0002-0003) proves the core concept — a Hypergraph graph where each
node output is a stored column, with insert/update/delete/sync/recompute/backfill. But
it can't yet replace the hand-built storage layer in a real application like Panda's
knowledge base.

Panda's `LocalVectorCollection` + ingestion pipeline is ~400 lines of glue code that
manually runs a Hypergraph graph, collects outputs, maps them to store fields, manages
an index projection, and handles reconciliation — all things HyperTable already does
in principle. But four categories of gaps block a clean plug-in:

**1. Execution model.** Panda is fully async (`AsyncRunner`, `async def` nodes). HyperTable
mutations are sync-only. You can't call `table.insert()` from an async handler without
blocking the event loop.

**2. Store coupling.** HyperTable's type system (`_python_type_to_arrow`) is duplicated
between `_store.py` and `_table_store.py`, tied to LanceDB's Arrow schema. Panda needs
S3+Azure Search in production and a local equivalent in development. The store ABC
must be backend-agnostic, with Arrow as the intermediate type language.

**3. Read-path features.** Panda's collection exposes `set(where, fields)` for bulk
conditional updates and `filter(where)` for predicated reads. HyperTable has `get(id)`
and `children(parent_id)` but no bulk set and no filtered reads.

**4. Retrieval architecture.** Panda's production search is a multi-stage pipeline
(embed query → hybrid search with BM25 + HNSW + semantic reranking → truncate to top_m),
currently hardcoded inside `AzureSemanticVectorStore.search()` — invisible, untestable,
non-configurable. Retrieval should be a Hypergraph graph that calls store search
primitives, not a monolithic method buried in a store class.

Additionally, three correctness bugs in the mutation layer were discovered and fixed in
the current session (crash-leftover reads, broken fingerprint iteration, non-atomic child
cascades). These fixes are prerequisites — they're shipped but should be validated as part
of this PRD's testing.

## Solution

Make HyperTable production-ready in four areas, then demonstrate with Panda integration:

1. **Store decoupling** — Arrow as intermediate type system, store-owned configuration,
   get-or-create resource provisioning, component manifest protocol.
2. **Async mutations** — every mutation method gets an async counterpart.
3. **Read-path features** — bulk set, filtered reads.
4. **Retrieval as a graph** — stores expose a `search()` method, Hypergraph graphs compose
   it with query embedding into visible, testable retrieval pipelines.

After this PRD ships, Panda's ingestion becomes a `protocols_table` — a HyperTable
that is the **single source of truth**. PDF bytes are stored as a column (`content`);
everything else is either derived by the graph (embeddings, enrichments, visual
descriptions) or operator-set via `table.set()` (active, station, tags). There is no
separate catalog, manifest registry, or version store — the table IS the document store.

The config nests all its dependencies:

```python
# protocols_table_config — self-contained factory for the ingestion pipeline

def protocols_table_config(hp: HP) -> HyperTable:
    per_page = hp.nest(per_page_processing_config, name="per_page")
    ingestion = (
        per_page.as_node(name="ingestion")
        .rename_inputs(converted_page="converted_pages", page_image="page_images")
        .map_over("converted_pages", "page_images", mode="zip", identity="page_id")
    )

    table = HyperTable(
        nodes=[convert_document, render_page_images, count_converted_pages, ingestion],
        identity="doc_version_id",
        store=hp.nest(store_config, name="store"),
    )
    return table.bind(
        converter=hp.nest(document_converter_config, name="converter"),
        page_renderer=hp.nest(page_renderer_config, name="page_renderer"),
        vision_llm=hp.nest(llm_config, name="vision_llm", role="ingestion"),
        enrichment_llm=hp.nest(llm_config, name="enrichment_llm", role="ingestion"),
        embedder=hp.nest(embedder_config, name="embedder"),
        visual_prompt=load_prompt("page_vision_system"),
        visual_schema=VisualInformation,
        retrieval_enrichment_prompt=load_prompt("page_retrieval_enrichment"),
        retrieval_enrichment_schema=RetrievalEnrichment,
    ).with_runner(AsyncRunner())

# Usage:
protocols_table = instantiate(protocols_table_config, values={
    "store.settings.bucket": "panda-prod",
    "embedder.model": "text-embedding-3-large",
    "vision_llm.model": "gpt-4o",
})
await protocols_table.insert(doc_version_id="7_v1", content=pdf_bytes, filename="protocol.pdf")
```

And Panda's retrieval becomes a separate, visible graph:

```python
# retrieval_graph_config — self-contained factory for the search pipeline

def retrieval_graph_config(hp: HP) -> Graph:
    return Graph(
        nodes=[embed_query, search],
        name="retrieval",
    ).bind(
        store=hp.nest(store_config, name="store"),
        embedder=hp.nest(embedder_config, name="embedder"),
        table_name="page",
        retrieval_k=hp.int(50, name="retrieval_k", min=10, max=200),
        top_m=hp.int(10, name="top_m", min=1, max=50),
    )

# Usage:
retrieval = instantiate(retrieval_graph_config, values={
    "store.settings.bucket": "panda-prod",
    "embedder.model": "text-embedding-3-large",
})
results = await AsyncRunner().run(retrieval, {"query": "neonatal intubation", "top_k": 50})
```

This eliminates `LocalVectorCollection`, `IndexProjection`, `index_document` terminal
node, the hardcoded search pipeline inside `AzureSemanticVectorStore`, and manual
`upsert_many` loops. The `KnowledgeBase` facade class is no longer needed — the
`protocols_table` and `retrieval_graph` are independent peers; workflow logic (work items,
metadata confirmation) is application/UI code.

## User Stories

### Store decoupling

1. As a developer, I want `_python_type_to_arrow()` defined once at the HyperTable
   level so that every store receives `pa.DataType` and handles it natively, instead of
   each store reimplementing type mapping.

2. As a developer, I want `ColumnSpec` to carry a `pa.DataType` so that stores receive
   Arrow types directly and map them to their native format (Arrow for Lance/DuckDB,
   JSON text for SQLite, JSONB for Postgres).

3. As a developer, I want structured types (Pydantic models, TypedDicts, dataclasses)
   serialized as `pa.struct()` in Arrow-native stores and as JSON text in others, with
   HyperTable reconstructing typed objects on read using the node's return annotation.

4. As a developer, I want `save_manifest()` and `load_manifest()` on the `TableStore`
   protocol so that table metadata (graph hash, component configs, column specs) persists
   alongside data and survives store reopening.

5. As a developer, I want the `TableStore` ABC to remain database-agnostic — no
   `import pyarrow` at the protocol level. Arrow types are passed as method arguments,
   not required in the protocol definition.

6. As a developer, I want `store.open()` to be idempotent (get-or-create) so that
   calling it on every startup is safe, even with concurrent processes.

### Async mutations

7. As a developer, I want `await table.insert(...)` so that async ingestion pipelines
   don't block the event loop.

8. As a developer, I want `await table.update(id, **changes)` so that async handlers
   can update rows without thread offloading.

9. As a developer, I want `await table.sync(items)` so that batch reconciliation works
   in async contexts.

10. As a developer, I want the async path to use `AsyncRunner` automatically when the
    table's runner is async, so that I don't have to choose between sync/async method
    variants manually.

### Read-path features

11. As a developer, I want `table.set(where, fields)` to update all matching rows
    (e.g., `table.set({"doc_id": 7}, active=True)` activates all pages for a document),
    so that bulk conditional updates don't require loading all rows client-side.

12. As a developer, I want `table.filter(where)` to return matching rows without
    loading the full table, so that filtered reads are efficient.

### Retrieval as a graph

13. As a developer, I want to build retrieval pipelines as Hypergraph graphs that call
    store search methods alongside processing nodes (query embedding, post-processing),
    so that retrieval is visible, testable, and configurable.

14. As a developer, I want stores to expose a `search()` method that wraps the
    backend's native search capabilities (Azure's hybrid search, local cosine similarity),
    so that retrieval graphs can call them as bound components.

15. As a developer, I want retrieval graphs to be independent of HyperTable and live
    outside the `kb/` module, sharing only the store via config values, so that write-path
    and read-path concerns don't mix.

### Hierarchical nesting

16. As a developer, I want `map_over` inside `map_over` to produce multi-level child
    tables (grandchildren, great-grandchildren), because Hypergraph treats nested graphs
    as first-class and HyperTable should match.

17. As a developer, I want `_analyze_map_over` to recurse into inner graphs that
    themselves contain `map_over` nodes, so that N-level hierarchies work without
    artificial depth limits.

### Component integration

18. As a developer, I want `_compute_row_fingerprint` to detect `__component_config__`
    (from `@component` decorator) alongside legacy `_config()`, so that component config
    changes invalidate fingerprints correctly regardless of which pattern is used.

19. As a developer, I want a column-component dependency map built at graph analysis
    time, so that HyperTable knows which columns depend on which components and can
    scope staleness checks.

### External store integration

20. As a developer, I want to implement a custom `TableStore` (e.g., S3 truth +
    Azure Search index) in my application repo and pass it to HyperTable, so that
    HyperTable handles orchestration while my store handles infrastructure.

## Implementation Decisions

### TableStore as ABC (not Protocol)

`TableStore` is an abstract base class, not a `typing.Protocol`. This is an extension
point for external store authors — ABC gives better DX:

```python
from abc import ABC, abstractmethod

class TableStore(ABC):
    @abstractmethod
    def open(self, spec, children) -> dict[str, list[str]]: ...

    @abstractmethod
    def write_rows(self, table_name, rows) -> None: ...

    @abstractmethod
    def read_one(self, table_name, identity_column, identity_value) -> dict | None: ...

    @abstractmethod
    def read_rows(self, table_name, *, where=None, limit=None) -> list[dict]: ...

    @abstractmethod
    def delete_rows(self, table_name, where) -> int: ...

    @abstractmethod
    def max_write_gen(self, table_name) -> int: ...

    @abstractmethod
    def evolve_schema(self, table_name, new_columns) -> list[str]: ...

    # Optional — stores that support search override this
    def search(self, table_name, *, query, query_vector, **kwargs) -> list[dict]:
        raise NotImplementedError("This store does not support search")

    # Optional — manifest persistence
    def save_manifest(self, table_name, manifest) -> None: ...
    def load_manifest(self, table_name) -> dict | None: ...
```

**Why ABC over Protocol:**

- **Immediate TypeError** on `class MyStore(TableStore)` if abstract methods are missing —
  the author sees the error when they define the class, not when HyperTable tries to call
  a missing method at runtime.
- **IDE autocomplete** — subclassing shows all methods to implement with signatures.
- **`validate_store()` helper** — a convenience for test suites and CI:

```python
from hypergraph.materialization import validate_store

def test_my_store_is_valid():
    store = MyStore(...)
    validate_store(store)  # checks isinstance + calls open() with a dummy spec
```

`validate_store(store)` checks `isinstance(store, TableStore)`, then calls `open()` with
a minimal `TableSpec` to verify the store can create/verify physical tables. Useful for
store authors to validate their implementation without wiring up a full HyperTable.

### Single source of truth — no catalog

The `protocols_table` stores PDF bytes as a column. There is no separate catalog,
manifest registry, or version tracking system. The table IS the document store:

```python
@node(output_name="content")
def identity_content(content: bytes) -> bytes:
    """Pass-through — stores the raw PDF as a column."""
    return content

# protocols_table stores:
# - content (bytes)       ← the actual PDF, stored as a column
# - filename (str)        ← passed in at insert time
# - embedding (list[float]) ← derived by graph
# - summary (str)         ← derived by graph
# - keywords (str)        ← derived by graph
# - active (bool)         ← operator-set via table.set()
# - station (str)         ← operator-set via table.set()
# - tags (list[str])      ← operator-set via table.set()
```

**What this eliminates:**

- `ManifestRecord` — the table's rows ARE the manifest
- `VersionRecord` — version is part of the identity key (`doc_version_id = "7_v1"`)
- `WorkItemRecord` — work-item state machine stays in app/UI code (Streamlit session state)
- `InMemoryDocumentStore` / `LocalFileDocumentStore` / `S3DocumentStore` — the table
  store handles all storage
- The `catalog.py` module — never needed; there's one source of truth

**Why store PDF bytes in the table?** Because anything stored outside the table
becomes a coordination problem — "is the PDF still there?", "which version?",
"how to clean up orphans?". With bytes in the table, `table.delete(id)` removes
everything atomically. `table.sync()` can re-derive everything from the stored bytes.
The tradeoff is storage cost, but for a medical protocols KB with hundreds (not millions)
of documents, this is negligible.

### Store-owned configuration (not HyperTable column hints)

Each store defines its own configuration vocabulary for backend-specific capabilities.
HyperTable does NOT carry `column_hints` — that would force HyperTable to speak every
store's language. Instead, the store's constructor is where you configure how columns
are treated:

```python
# Each store defines its own config vocabulary:

# S3+Azure — needs to know what to project into the search index
store = S3AzureStore(
    s3_client=s3, bucket="my-bucket",
    search_client=search_client,
    vector_columns=["embedding"],
    searchable_columns=["content", "summary", "keywords"],
    filterable_columns=["active", "station", "tags"],
    use_vectors=True, use_keywords=True, use_semantic_reranker=True,
)

# Local dev — same API, backed by JSON files + cosine similarity
store = LocalTableStore(path="data/kb/pages")

# HyperTable stays storage-agnostic:
table = HyperTable(nodes=[...], identity="page_id", store=store)
```

The store receives `ColumnSpec` objects (with `arrow_type`) in `open()` and maps them
to its native schema. Column-level configuration is the store's business, not
HyperTable's.

### Provider pattern for stores

Store selection follows the same provider pattern as AI components
(`panda/components/ai/providers/`). Each provider module exports a `store_config()`
function that returns the appropriate Hypster config:

```python
# panda/kb/stores/local.py — local development store

class LocalTableStore(TableStore):
    """Same API as S3AzureStore, backed by JSON files + cosine similarity."""

    def __init__(self, *, path: str) -> None: ...

    # TableStore ABC methods
    def open(self, spec, children): ...
    def write_rows(self, table_name, rows): ...
    def read_one(self, table_name, identity_column, identity_value): ...
    def read_rows(self, table_name, *, where=None): ...
    def delete_rows(self, table_name, identity_column, identity_values): ...

    # Search — local cosine similarity + text matching
    def search(self, table_name, *, query, query_vector, retrieval_k=50,
               top_m=10, where=None) -> list[dict]: ...

def local_store_config(hp: HP) -> LocalTableStore:
    return LocalTableStore(path=hp.text("data/kb/pages", name="path"))
```

```python
# panda/kb/stores/s3_azure.py — production store

class S3AzureStore(TableStore):
    """S3 truth + Azure AI Search hybrid index."""

    def __init__(
        self, *, s3_client, bucket, prefix="kb/pages",
        search_client,
        vector_columns=(), searchable_columns=(), filterable_columns=(),
        use_vectors=True, use_keywords=True, use_semantic_reranker=True,
        semantic_config_name="default-semantic",
    ) -> None: ...

    # TableStore ABC methods
    def write_rows(self, table_name, rows): ...
    def read_one(self, table_name, identity_column, identity_value): ...

    # Search — Azure hybrid (BM25 + HNSW + semantic reranker) in one call
    def search(self, table_name, *, query, query_vector,
               retrieval_k=50, top_m=10, where=None) -> list[dict]:
        kwargs = self._build_search_kwargs(query, query_vector,
                                           retrieval_k=retrieval_k, where=where)
        results = self.search_client.search(**kwargs)
        return self._to_dicts(results)[:top_m]

def s3_azure_store_config(hp: HP) -> S3AzureStore:
    # ... boto3 + Azure SearchClient setup ...
    return S3AzureStore(
        s3_client=s3_client,
        bucket=hp.text(os.getenv("PANDA_KB_S3_BUCKET", ""), name="bucket"),
        prefix=hp.text("kb/pages", name="prefix"),
        search_client=search_client,
        vector_columns=["embedding"],
        searchable_columns=["content", "summary", "keywords"],
        filterable_columns=["active", "station", "tags"],
        use_vectors=hp.bool(True, name="use_vectors"),
        use_keywords=hp.bool(True, name="use_keywords"),
        use_semantic_reranker=hp.bool(True, name="use_semantic_reranker"),
    )
```

```python
# panda/kb/stores/config.py — provider dispatch (same pattern as converters)

def store_config(hp: HP):
    from panda.components.ai.providers import provider
    return hp.nest(provider().store_config(), name="settings")
```

Each provider module (`providers/local.py`, `providers/azure.py`) exports `store_config()`
returning the appropriate config function. The unused provider is never imported.

### Store.open() as get-or-create

`open()` is idempotent — safe to call on every startup, even with concurrent processes.
This follows the pattern used by ChromaDB (`get_or_create_collection`), Azure AI Search
(`create_or_update_index`), and LanceDB (`open_table` with auto-create).

```python
class S3AzureStore:
    def open(self, spec: TableSpec, children: list[TableSpec]) -> dict[str, list[str]]:
        for table_spec in [spec, *children]:
            self._ensure_s3_prefix(table_spec.name)
            self._ensure_search_index(table_spec)  # create_or_update_index — idempotent
        return {s.name: [c.name for c in s.columns] for s in [spec, *children]}
```

Index management (creating/migrating/deleting Azure Search indexes with HNSW params,
semantic ranking config, proxy-aware fallbacks) is **infrastructure tooling** orthogonal
to HyperTable. It's analogous to Alembic vs SQLAlchemy:

```
Index Management (infra — runs on deploy, from CLI)
  AzureSearchIndexManager.create_index()       ← like 'alembic upgrade'
  HNSW params, semantic config, proxy fallbacks
  NOT called during normal app operation
                    │
                    │ provisions the index that...
                    ▼
S3AzureStore (runtime — runs constantly)
  .open() → verify index exists (NOT create)   ← like SQLAlchemy Engine
  .write_rows() → S3 put + Azure upload
  .search() → Azure hybrid query
  Called on every request
                    │
                    │ implements TableStore for...
                    ▼
HyperTable (doesn't know Azure exists)         ← like SQLAlchemy ORM
```

The index manager lives in the application repo (Panda), not in Hypergraph.

### Arrow as intermediate type system

`_python_type_to_arrow()` moves to `_hypertable.py` (or a shared `_schema.py`) and runs
once during graph analysis. `ColumnSpec` gains an `arrow_type: pa.DataType` field:

```python
@dataclass(frozen=True)
class ColumnSpec:
    name: str
    role: str
    produced_by: Any = None
    content_key: bool = False
    arrow_type: pa.DataType | None = None  # new
```

Stores receive `ColumnSpec` (which carries `arrow_type`) in `open()` and `evolve_schema()`.
Each store maps Arrow types to its native format:

| Arrow type | LanceDB | DuckDB | SQLite | Postgres |
|---|---|---|---|---|
| `pa.utf8()` | string | VARCHAR | TEXT | TEXT |
| `pa.int64()` | int64 | BIGINT | INTEGER | BIGINT |
| `pa.list_(pa.float32())` | vector | FLOAT[] | JSON text | FLOAT[] |
| `pa.struct(...)` | struct | STRUCT | JSON text | JSONB |

The duplicated `_python_type_to_arrow()` in `_store.py` (DerivedTable) is left as-is
for backward compatibility. The new canonical location is HyperTable-level.

### Structured type round-trip

For nodes that return Pydantic models, TypedDicts, or dataclasses:

1. **Write path:** HyperTable serializes to dict (`model_dump()` / `asdict()` / `dict`).
   Arrow-native stores receive the dict directly (LanceDB handles `pa.struct()` natively).
   Non-Arrow stores receive JSON text.

2. **Read path:** HyperTable reconstructs the typed object using the known Python type
   from the node's return annotation. `get()` returns dicts (public API), but provenance
   comparison works on the serialized form.

No `pa.ExtensionType` — DuckDB can't read them, compute kernels ignore them,
registration is fragile across processes.

### Component manifest

`TableStore` gains two optional methods:

```python
class TableStore(ABC):
    # ... existing abstract methods ...
    def save_manifest(self, table_name: str, manifest: dict[str, Any]) -> None: ...
    def load_manifest(self, table_name: str) -> dict[str, Any] | None: ...
```

The manifest stores:
- Graph definition hash (detects code changes across reopens)
- Component config hashes (detects component swaps)
- Column specs with Arrow types (detects schema drift)
- HyperTable version (for future migration paths)

LanceDB stores the manifest as a separate `_{table_name}_manifest` table with one row.
Other stores choose their format (SQLite: metadata table, S3: sidecar JSON file).

### Async mutations: runner-aware dispatch

HyperTable detects whether its runner is async (`isinstance(runner, AsyncRunner)`) and
dispatches accordingly. The public API uses a single set of method names — `insert`,
`update`, `delete`, etc. — that return coroutines when the runner is async and plain
values when sync.

```python
# Sync — works today
table = HyperTable([...], ...).with_runner(SyncRunner())
table.insert(doc_id="d1", text="hello")

# Async — same API, returns coroutine
table = HyperTable([...], ...).with_runner(AsyncRunner())
await table.insert(doc_id="d1", text="hello")
```

Internally, the graph execution call (`self._runner.run(self._graph, **inputs)`) is
the only part that differs between sync and async. Store operations remain sync — S3
and most local stores are synchronous. If a store needs async I/O (e.g., a remote
database), the store ABC gains optional async methods in a future iteration.

### Bulk set

`table.set(where, **fields)` updates all matching rows:

```python
count = table.set({"doc_id": 7}, active=True)
# Updates all rows where doc_id=7, sets active=True
```

Semantics:
- `where` is a dict converted to `RowPredicate` (all `"eq"` operators).
- Only metadata fields (non-content-key) can be set without re-derivation.
- Setting a content-key field raises `ValueError` — use `update(id, ...)` instead.
- Returns the count of updated rows.

Under the hood: `read_rows(where)` → mutate in Python → `write_rows` + `delete_rows`
(same write-then-delete pattern). A future optimization can push simple updates to the
store directly.

### Retrieval as a Hypergraph graph

HyperTable does **not** have a `.search()` method. HyperTable owns the write path
(insert, sync, set, delete). Retrieval is a separate Hypergraph graph that shares the
same store via Hypster config values (not via a class that wraps both). This follows
the same pattern as ingestion: components are bound into a graph; nodes call them; the
graph orchestrates.

**Azure AI Search does hybrid retrieval in a single API call.** The prod code in
`AzureSemanticVectorStore._build_search_kwargs()` constructs one request that includes
BM25 keyword search, HNSW vector search, RRF fusion, and semantic reranking — all
server-side. Client-side truncation to `top_m` is the only post-processing.

This means the retrieval graph is simple — two nodes, not five:

```python
# panda/retrieval/nodes.py

@node(output_name="query_vector")
async def embed_query(embedder: Any, query: str) -> list[float]:
    return await embedder.embed_text(query)

@node(output_name="results")
def search(
    store: Any, table_name: str, query: str, query_vector: list[float],
    retrieval_k: int, top_m: int,
) -> list[dict]:
    return store.search(
        table_name, query=query, query_vector=query_vector,
        retrieval_k=retrieval_k, top_m=top_m,
    )
```

The store's `search()` wraps the backend's native capabilities:

```python
# S3AzureStore.search() — wraps Azure's hybrid search
def search(self, table_name, *, query, query_vector, retrieval_k=50, top_m=10, where=None):
    kwargs: dict = {"top": retrieval_k}

    if self.use_keywords:
        kwargs["search_text"] = query

    if self.use_vectors:
        kwargs["vector_queries"] = [
            VectorizedQuery(
                vector=query_vector,
                k_nearest_neighbors=retrieval_k,
                fields="embedding",
            ),
        ]

    if self.use_semantic_reranker:
        kwargs["query_type"] = "semantic"
        kwargs["semantic_configuration_name"] = self.semantic_config_name
        kwargs["query_caption"] = "extractive"
        if not self.use_keywords:
            kwargs["semantic_query"] = query

    if where:
        kwargs["filter"] = self._build_odata_filter(where)
        kwargs["vector_filter_mode"] = "preFilter"

    results = self.search_client.search(**kwargs)
    return self._to_dicts(results)[:top_m]
```

The graph's value is at the **boundaries** — query embedding is client-side, search is
server-side. If you later add query expansion, post-processing, or re-ranking with a
different model, those are new nodes before/after `search`. The store's internal search
pipeline (BM25 + HNSW + RRF + semantic reranking) stays atomic.

The retrieval graph lives **outside `kb/`** — in `panda/retrieval/`. It's a search
pipeline that happens to read from the same store. The connection is config values
(`store.settings.bucket`), not code coupling.

### Hypster config wiring: Option A (self-contained factories)

Every config is a **complete, self-contained factory**. Each nests its own dependencies.
No config requires kwargs from a parent — any config can be the top-level `instantiate()`
target.

```python
# panda/kb/protocols_table.py

def protocols_table_config(hp: HP) -> HyperTable:
    """Full ingestion pipeline — nests ALL its own dependencies."""
    from panda.components.ai.embedder import embedder_config
    from panda.components.ai.llm import llm_config
    from panda.components.ingestion.converters.config import document_converter_config
    from panda.components.ingestion.page_renderer import page_renderer_config
    from panda.kb.stores.config import store_config
    from panda.prompts.markdown import load_prompt
    from hypergraph.materialization import HyperTable
    from hypergraph import AsyncRunner

    per_page = hp.nest(per_page_processing_config, name="per_page")
    ingestion = (
        per_page.as_node(name="ingestion")
        .rename_inputs(converted_page="converted_pages", page_image="page_images")
        .map_over("converted_pages", "page_images", mode="zip", identity="page_id")
    )

    table = HyperTable(
        nodes=[convert_document, render_page_images, count_converted_pages, ingestion],
        identity="doc_version_id",
        store=hp.nest(store_config, name="store"),
    )
    return table.bind(
        converter=hp.nest(document_converter_config, name="converter"),
        page_renderer=hp.nest(page_renderer_config, name="page_renderer"),
        vision_llm=hp.nest(llm_config, name="vision_llm", role="ingestion"),
        enrichment_llm=hp.nest(llm_config, name="enrichment_llm", role="ingestion"),
        embedder=hp.nest(embedder_config, name="embedder"),
        visual_prompt=load_prompt("page_vision_system"),
        visual_schema=VisualInformation,
        retrieval_enrichment_prompt=load_prompt("page_retrieval_enrichment"),
        retrieval_enrichment_schema=RetrievalEnrichment,
    ).with_runner(AsyncRunner())
```

```python
# panda/retrieval/config.py

def retrieval_graph_config(hp: HP) -> Graph:
    """Hybrid retrieval pipeline — nests ALL its own dependencies."""
    from panda.components.ai.embedder import embedder_config
    from panda.kb.stores.config import store_config
    from panda.retrieval.nodes import embed_query, search

    return Graph(
        nodes=[embed_query, search],
        name="retrieval",
    ).bind(
        store=hp.nest(store_config, name="store"),
        embedder=hp.nest(embedder_config, name="embedder"),
        table_name="page",
        retrieval_k=hp.int(50, name="retrieval_k", min=10, max=200),
        top_m=hp.int(10, name="top_m", min=1, max=50),
    )
```

**Why self-contained (Option A) over shared instances (Option B)?**

Each graph is an independent peer — there is no "main" graph. The `protocols_table` can
be used standalone for batch re-ingestion. The `retrieval_graph` can be used standalone
for a read-only API. Either can be composed into an eval graph. They share the same
store via Hypster config values (`"store.settings.bucket": "panda-prod"`), not via a
parent class that wraps both.

For stores with expensive resources (e.g., a GPU-loaded local embedding model), the
component owns deduplication (via `lru_cache` or `__new__`), not the config topology.
Remote API clients (S3, Azure Search, OpenAI embeddings) are cheap to construct — two
instances with the same config are functionally identical.

### Usage — every entry point is standalone

```python
from hypster import instantiate
from hypergraph import AsyncRunner

# ─── Ingest a document ───
protocols_table = instantiate(protocols_table_config, values={
    "store.settings.bucket": "panda-prod",
    "embedder.model": "text-embedding-3-large",
    "vision_llm.model": "gpt-4o",
})
await protocols_table.insert(
    doc_version_id="7_v1", content=pdf_bytes, filename="intubation.pdf",
)

# ─── Activate pages after operator confirms metadata ───
await protocols_table.set({"doc_id": 7}, active=True, station="NICU", tags=["neonatal"])

# ─── Search (from a read-only API — ingestion components never loaded) ───
retrieval = instantiate(retrieval_graph_config, values={
    "store.settings.bucket": "panda-prod",
    "embedder.model": "text-embedding-3-large",
    "retrieval_k": 50, "top_m": 10,
})
results = await AsyncRunner().run(retrieval, {"query": "neonatal intubation protocol"})

# ─── Re-ingest with a new embedder model ───
protocols_table_v2 = instantiate(protocols_table_config, values={
    "store.settings.bucket": "panda-prod",
    "embedder.model": "text-embedding-4-large",
})
await protocols_table_v2.sync(items=[("7_v1", {"content": pdf_bytes, "filename": "intubation.pdf"})])

# ─── Evaluate retrieval quality ───
retrieval = instantiate(retrieval_graph_config, values={...})
eval_graph = Graph(
    [retrieval.as_node(name="retrieve"), score_answer, aggregate],
    name="retrieval_eval",
)
scores = await AsyncRunner().map(eval_graph, {"queries": test_set}, map_over="queries")
```

### `explore()` param tree

```
protocols_table_config
├── store/
│   └── settings/               # provider-specific (s3_azure or local)
│       ├── index_name: "documents"
│       ├── bucket: ""
│       └── prefix: "kb/pages"
├── embedder/
│   └── model: "text-embedding-3-large"
├── converter/                  # heavy — only in ingestion
│   └── settings/ ...
├── page_renderer/              # heavy — only in ingestion
│   └── settings/ ...
├── vision_llm/                 # heavy — only in ingestion
│   └── model: "gpt-4o"
├── enrichment_llm/             # heavy — only in ingestion
│   └── model: "gpt-4o-mini"
└── per_page/
    ├── image_detail: "auto"
    └── max_embedding_chars: 8000

retrieval_graph_config
├── store/
│   └── settings/
│       ├── index_name: "documents"
│       ├── bucket: ""
│       └── prefix: "kb/pages"
├── embedder/
│   └── model: "text-embedding-3-large"
├── retrieval_k: 50
└── top_m: 10
```

### Single-string identity

HyperTable identity is always one string column. No composite key support. Users who
need multi-field keys construct them in a node:

```python
@node(output_name="page_id")
def make_page_id(doc_id: int, version_id: str, page_number: int) -> str:
    return f"{doc_id}_{version_id}_{page_number}"
```

This keeps `read_one(table, identity_col, value)` a simple equality check, avoids
composite key machinery, and gives the user full control over key format (UUIDs, slugs,
hierarchical concatenation — whatever fits their domain).

### Multi-level nesting

`_analyze_map_over` recurses into inner graphs. If an inner graph itself contains a
`map_over` node, that creates a grandchild `TableSpec`:

```
doc (identity="doc_id")
  └── version (identity="version_id", via map_over)
        └── page (identity="page_number", via map_over inside map_over)
```

Each level gets its own physical table with `_parent_id` linking to the level above.
Cascade operations (delete, update) propagate downward through all levels recursively.

This matches Hypergraph's core thesis: nested graphs are first-class. A `map_over` node
whose inner graph contains another `map_over` is just a graph inside a graph — no special
depth limit.

### Column-component dependency map

At graph analysis time, HyperTable traces which components flow into which derived
columns:

```python
# Built during _analyze_graph():
self._component_deps = {
    "embedding": {"embedder"},
    "source_text": set(),
    "retrieval_enrichment": {"enrichment_llm"},
}
```

This enables scoped staleness: when `table.bind(embedder=new_embedder).recompute("embedding")`
is called, only columns whose `_component_deps` include `"embedder"` are invalidated.

### Child graph fingerprint coverage

Row fingerprints must include child graph definitions. Currently, `_compute_row_fingerprint`
hashes nodes from `self._graph.iter_nodes()`, but `map_over` nodes are split out of the
root graph. This means changing a per-page processing node (e.g., improving the embedding
text builder) doesn't invalidate the parent fingerprint — `sync()` returns "skipped"
even though child columns are stale.

Fix: include the hash of each `map_over` node's inner graph definition in the parent
row fingerprint. When the child graph changes, the parent fingerprint changes, and
`sync()` knows to re-derive children.

```python
# In _compute_row_fingerprint:
# 1. Hash root graph nodes (existing)
# 2. For each map_over child spec, hash the child graph's node definitions
#    This makes parent fingerprint sensitive to child code changes
for child_spec in self._child_specs:
    h.update(child_spec.graph_hash.encode())
```

This is a Phase 1 item — required before `sync()` is reliable with nested tables.

### LanceDB reference implementation: known limitations

The LanceDBStore is the reference/development store. Two known issues should be fixed
before it's used in any production-adjacent context:

1. **String interpolation in delete filters** — `_build_lance_filter()` interpolates
   values directly into SQL-like strings. Identity values containing quotes can break
   or broaden deletes. Fix: escape literals or use LanceDB's native predicate API.

2. **Non-atomic schema evolution** — `evolve_schema()` drops the table before creating
   the replacement. A crash between drop and recreate loses data. Fix: create under a
   temporary name, validate, then swap.

These don't affect Panda (which uses S3AzureStore), but they should be addressed for
correctness of the reference implementation.

Additionally, three mutation-ordering issues exist in HyperTable itself:

3. **Insert-upsert child ordering** — `_insert_one` deletes old children before writing
   replacements. A crash between delete and insert loses children. Fix: write new children
   first, then delete old generation (same pattern `update()` already uses).

4. **Public reads expose crash leftovers** — `children()` returns all physical rows
   matching `_parent_id` without dedup. After a crash-interrupted write, stale + current
   rows appear together. Fix: deduplicate by logical identity + highest `_write_gen` in
   `children()` and `count()`.

5. **Metadata-only updates drop new columns** — `update()` metadata-only path writes
   without schema evolution. A new metadata column is silently dropped. Fix: call
   `_evolve_for_metadata()` before both update branches.

### Correctness fixes (shipped)

Three bugs were fixed in this session as prerequisites:

1. **Fingerprint iteration** — `_compute_row_fingerprint` now uses `iter_nodes()` (actual
   node objects, not string dict keys) and reads `.func` (not `.fn`). Component config
   detection supports both `__component_config__` and `_config()`.

2. **Generation-aware reads** — `read_one()` returns the highest `_write_gen` when
   duplicates exist (crash leftovers). `_dedup_rows()` helper applied in sync/recompute/
   backfill bulk reads.

3. **Child cascade atomicity** — `update()` writes new children before deleting old ones.
   `delete()` removes children before parent. Combined with fix #2, any crash state is
   self-healing on next read.

## Testing Decisions

### What makes a good test

Same principle as PRDs 0002-0003: tests assert external behavior through HyperTable's
public API. No test inspects `TableSpec`, Arrow schemas, or store internals directly.

### Test modules

- **`test_hypertable_async.py`** — async insert, update, delete, sync, recompute, backfill
  using `AsyncRunner`. Mirror the sync tests from `test_hypertable_mutations.py` with
  `pytest.mark.asyncio`.

- **`test_hypertable_readpath.py`** — `.filter(where)` returns matching rows,
  `.set(where, fields)` updates matching rows.

- **`test_hypertable_store_protocol.py`** — Arrow type round-trip (Python type → Arrow →
  store native → read back → correct Python type). Structured types (Pydantic, TypedDict).
  Manifest save/load. Schema evolution with Arrow types.

- **`test_hypertable_mutations.py`** (existing) — already includes regression tests for
  the three correctness fixes (crash recovery, fingerprint correctness, child cascade
  ordering).

### Prior art

- Panda's `tests/kb/` — async tests with `pytest-asyncio`, mock components, real stores.
  Same patterns apply to HyperTable's async tests.
- `test_hypertable_e2e.py` — end-to-end with real graphs, real store, real runner.

## Phasing

### Phase 1: Store decoupling + nesting + correctness (no new public API)

- Lift `_python_type_to_arrow()` to HyperTable level, add `arrow_type` to `ColumnSpec`.
- Make `_analyze_map_over` recursive for multi-level nesting.
- Add `save_manifest()` / `load_manifest()` to `TableStore` ABC.
- Column-component dependency map in `_analyze_graph()`.
- `_compute_provenance` includes component config.
- Update `LanceDBStore` to subclass `TableStore` ABC.
- Add `validate_store()` helper for store authors.
- Include child graph hashes in parent row fingerprints (nested `map_over` coverage).
- Fix LanceDBStore: escape delete filter literals, non-atomic schema evolution.
- Fix insert-upsert child ordering (write new before deleting old).
- Deduplicate `children()` and `count()` by identity + `_write_gen`.
- Fix metadata-only update to evolve schema for new columns.
- Store.open() documented as idempotent get-or-create contract.

### Phase 2: Async mutations

- Runner-aware dispatch in every mutation method.
- Async graph execution via `await self._runner.run(...)`.
- `test_hypertable_async.py` test module.

### Phase 3: Read-path features

- `table.filter(where)` on HyperTable.
- `table.set(where, **fields)` on HyperTable.
- `test_hypertable_readpath.py` test module.

### Phase 4: Store search method + reference retrieval

- `search()` method documented as optional store capability (not part of core
  `TableStore` ABC — stores that support search expose it directly).
- Reference retrieval graph nodes (`embed_query`, `search`) shipped as examples
  in docs or a contrib module.
- `LanceDBStore` implements `search()` with local vector similarity.

### Phase 5: Panda integration (in panda repo)

- Build `S3AzureStore` implementing `TableStore` + `search()` using Azure's native
  hybrid search (BM25 + HNSW + semantic reranking in one call).
- Build `LocalTableStore` with the same API for development.
- Wire both into the provider pattern (`providers/local.py`, `providers/azure.py`).
- Build `AzureSearchIndexManager` as infrastructure tooling (CLI/deploy, not runtime).
- Replace `LocalVectorCollection` + `DocumentIngestionProcessor` with
  `protocols_table_config` (HyperTable).
- Build `retrieval_graph_config` in `panda/retrieval/` replacing hardcoded
  `AzureSemanticVectorStore.search()`.
- Delete `IndexProjection`, `projection_for()`, `index_document` terminal node,
  `KnowledgeBase` facade class, `ManifestRecord`, `VersionRecord`, document stores
  (`InMemoryDocumentStore`, `LocalFileDocumentStore`, `S3DocumentStore`).
- Store PDF bytes as a column in `protocols_table` — single source of truth, no catalog.
- Verify search quality parity with existing Azure Search integration.

## Panda Architecture After This PRD

### No KnowledgeBase facade

The `KnowledgeBase` class is eliminated. Its responsibilities split:

- **Ingestion pipeline** → `protocols_table` (HyperTable, self-contained Hypster config)
- **Search pipeline** → `retrieval_graph` (Hypergraph graph, self-contained Hypster config,
  lives in `panda/retrieval/`, NOT in `panda/kb/`)
- **Document storage (PDF bytes)** → `protocols_table` stores bytes as a column
- **Document metadata (station, tags)** → `table.set()` on the protocols_table
- **Work-item state machine** → app/UI code (Streamlit session state)
- **Duplicate detection** → app logic (reads table, compares hashes)

The `protocols_table` and `retrieval_graph` are independent peers. Neither needs the
other to function. They share the same store via Hypster config values, not via a parent
class.

### Three-layer separation

```
Infrastructure (runs on deploy/CLI — orthogonal to HyperTable)
  AzureSearchIndexManager: create/delete/migrate Azure indexes
  HNSW params, semantic ranking config, proxy-aware fallbacks
  Analogous to Alembic — manages physical schema, not used at runtime

Runtime Store (implements TableStore — used by HyperTable)
  S3AzureStore: S3 truth + Azure Search index
  .open() verifies index exists (does NOT create)
  .write_rows() → dual-write to S3 + Azure
  .search() → Azure hybrid query (BM25 + HNSW + reranker in one call)

HyperTable (framework — doesn't know Azure exists)
  .insert() / .sync() / .set() / .delete()
  Runs ingestion graph, manages incrementality
  Delegates storage to whatever TableStore is plugged in
```

### File layout

```
panda/
├── kb/
│   ├── protocols_table.py      # protocols_table_config (HyperTable)
│   ├── ingestion.py            # ingestion graph nodes (convert, render, enrich, embed)
│   ├── models.py               # ProtocolPage, VisualInformation, etc.
│   └── stores/
│       ├── config.py           # provider dispatch
│       ├── local.py            # LocalTableStore (dev)
│       └── s3_azure.py         # S3AzureStore (prod)
├── retrieval/
│   ├── nodes.py                # embed_query, search
│   └── config.py               # retrieval_graph_config
├── infra/
│   └── index_manager.py        # AzureSearchIndexManager (deploy/CLI)
└── components/ai/providers/
    ├── local.py                # + store_config() returning local_store_config
    └── azure.py                # + store_config() returning s3_azure_store_config
```

### What Panda keeps vs what HyperTable replaces

**HyperTable replaces:**

| Panda code | Lines | HyperTable replacement |
|---|---|---|
| `LocalVectorCollection` class | ~150 | `HyperTable(store=...)` |
| `IndexProjection` + `projection_for()` | ~20 | Store-owned projection config |
| `index_document` terminal node | ~10 | Eliminated — storage IS the table |
| Manual `upsert_many` loop | ~10 | `table.insert(...)` |
| `rebuild()` reindex | ~5 | `table.recompute("embedding")` |
| `set(where, fields)` with index merge | ~15 | `table.set(where, **fields)` |
| `patch(key, fields)` with embed check | ~25 | `table.update(id, **fields)` |
| `AzureSemanticVectorStore.search()` | ~100 | Retrieval graph |
| `KnowledgeBase` facade | ~200 | Eliminated — independent configs |
| `InMemory/LocalFile/S3 DocumentStore` | ~100 | Eliminated — PDF bytes in table |
| `ManifestRecord` / `VersionRecord` | ~50 | Eliminated — table rows ARE the manifest |
| **Total** | **~685** | **Built into HyperTable + retrieval graph** |

**Panda keeps (application logic above HyperTable):**

- **Work-item state machine** — operator-in-the-loop workflow (queue → checking →
  metadata → indexing → indexed). This is app/UI orchestration, not data pipeline.
- **Duplicate detection** — comparing document hashes before ingestion. Application
  logic that reads the table and makes business decisions.
- **Manual overrides** — operator corrects a derived value (e.g., marks page 3 as a
  diagram page). Implemented via `table.set()` for page-level fields. For overrides that
  must survive re-derivation, use separate override columns
  (`has_diagrams_override: bool | None`) so the graph never touches the operator's
  decision. UI reads `override ?? derived`.
- **Metadata confirmation** — human-in-the-loop approval before publishing pages.
  `table.set(where, active=True, station=..., tags=...)` after operator confirms.

Note: document-level metadata (station, tags, active) is stored directly in the
protocols_table via `table.set()`. There is no separate metadata store — the table is
the single source of truth.

### UI action → code mapping

| UI Action | Where it lives | Code |
|---|---|---|
| Upload document | app state | work item queue |
| Duplicate check | app logic | `table.filter({"filename": f})` + hash comparison |
| Run ingestion | `protocols_table` | `await table.insert(doc_version_id=..., ...)` |
| View pages for review | `protocols_table` | `table.filter({"doc_id": 7})` |
| Override a page field | `protocols_table` | `table.set({"page_id": "7_v1_3"}, has_diagrams_override=True)` |
| Confirm & publish | `protocols_table` | `table.set({"doc_id": 7}, active=True, station="NICU")` |
| Search content | `retrieval_graph` | `await runner.run(retrieval, {"query": "..."})` |
| Archive document | `protocols_table` | `table.set({"doc_id": 7}, active=False)` |
| Re-ingest (new model) | `protocols_table` | `await table.sync(items=[...])` |
| Get diagram pages | `protocols_table` | `table.filter({"doc_id": 7, "has_diagrams": True})` |

## Out of Scope

- **Remote async stores** — async `TableStore` methods for remote databases. Current
  stores are local/sync; async wrapping (e.g., `run_in_executor`) suffices for now.
- **DaftRunner integration** — `scan()` returning `pa.Table` for zero-copy into
  Daft/Polars. Important for performance, but orthogonal to this PRD's goals.
- **Streaming writes via sink** — large binary column optimization. Deferred.
- **Ephemeral outputs** — `ephemeral=True` node flag. Orthogonal to this PRD.
- **Pin/override at HyperTable level** — framework-level "pin this column's value"
  (skip re-derivation). For now, use separate override columns in the application.
  Deferred to v2.
- **Per-row error handling** — error markers on failed rows. Separate concern.

## Further Notes

- The `@component` decorator is already implemented in Hypercache on
  `feat/component-config-support`. It captures `__init__` args as
  `__component_config__` — the fingerprint fix in this session already detects it.
- Arrow type inconsistency: `_store.py` maps `list[float]` → `pa.list_(pa.float64())`
  while `_table_store.py` uses `pa.list_(pa.float32())`. The consolidated version should
  use `pa.float32()` (standard for embeddings, LanceDB vector search requirement).
- The retrieval graph pattern naturally extends to advanced strategies: query expansion
  (add a node before `embed_query`), post-processing (add a node after `search`),
  multi-index search (add more search nodes). All composable via standard Hypergraph
  graph operations.
- Azure AI Search handles BM25 + HNSW + RRF fusion + semantic reranking server-side in
  one API call. Decomposing these into separate graph nodes would fight the SDK. The
  graph's value is at the boundaries (client-side embedding, server-side search,
  optional post-processing), not inside the server's pipeline.
- The `S3AzureStore._build_search_kwargs()` pattern is directly adapted from
  `panda_v2/prod_code/.../vector_store.py`, preserving the exact Azure SDK configuration
  (use_vectors, use_keywords, use_semantic_reranker, preFilter mode, semantic_query
  fallback when keywords are off).
