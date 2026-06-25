# HyperTable — Design Context

HyperTable is a persistent, incremental table built on Hypergraph graphs.

A Hypergraph graph defines stateless compute — nodes, edges, runners. It runs once, produces output, and forgets. HyperTable adds persistence, identity, and incrementality: run the graph, store the results, and on the next change only recompute what's affected.

The tagline: **a Hypergraph graph where each node's output is a stored column, and a content-key check decides whether to re-run.**


## Glossary

**HyperTable** — the top-level object. Wraps a Hypergraph graph + a store + an identity declaration. Owns materialization operations such as insert/update/delete/sync. Orchestrates runners and sinks.

**Source column** — a column you provide at insert time. Either feeds a node (content key) or doesn't (metadata). Not computed — stored directly.

**Derived column** — a column produced by a node. Stored alongside source columns on the same row. Recomputed when its input column's content key changes.

**Content key** — any source column that feeds a node. Inferred from the graph structure, not annotated. Changing a content key triggers re-derivation of downstream columns. Changing a metadata column (one that feeds no node) does not.

**Column provenance** — per-derived-column state that tracks what produced the current stored value. For each derived column on each row, stored as: `hash(upstream input values + producer node definition hash + relevant component config hashes)`. This enables scoped recompute — swapping an embedder only invalidates columns whose provenance includes that embedder's config, not the whole row. Replaces the single-`_content_key`-per-row model from DerivedTable.

**Row fingerprint** — a row-level hash `hash(all source content-key values + all node definition hashes + all component config hashes)`. Used as a fast path: if unchanged, no per-column provenance checks are needed. Incorporates the full derivation plan — source values, node code, and component configs — so swapping a component invalidates the fingerprint even when source values are unchanged. Parent and child rows each have their own fingerprint scoped to their respective graph.

**Child fingerprint** — a row fingerprint scoped to the child graph, not the parent graph. Computed from the child's source column values (from the child item dict), the child graph's node definition hashes, and component config hashes filtered to the child graph's inputs. This makes child rows skippable on re-insert — if the child's source inputs and graph definition haven't changed, the child is skipped. A `_compute_child_fingerprint` method handles this separately from the parent fingerprint.

**Identity** — the stable, user-facing key for a row. Declared explicitly on the table (`identity="video_id"`) and at each grain boundary (`map_over(..., identity="utterance_id")`). Used for update, delete, sync matching, and parent-child links. Always explicit — no naming-convention magic.

**Grain** — the unit of identity for a table's rows. A video-grain table has one row per video. An utterance-grain table has one row per utterance. A new grain starts at a `map_over` boundary.

**Grain boundary** — the point where a 1:N expansion creates a new collection of rows with its own identity. In the graph, this is a `map_over` node. Before the boundary: parent-grain rows. After: child-grain rows. No special `explode` primitive needed — `map_over` is standard Hypergraph.

**Derived table** — the child-grain table created at a grain boundary. Has its own identity, its own derived columns, and a parent link back to the root table. Example: the utterance table is derived from the video table via `split_utterances` + `map_over`.

**Parent link** — auto-stamped on each child row. Records which parent row produced it (e.g., `_parent_id="v1"` on every utterance from video v1). Used for cascade delete and scoped re-derivation. The child table's primary key is the composite `(_parent_id, child_identity)` — child identities are scoped by parent, not globally unique. Two parents can both emit `utterance_id="u0"` without collision.

**Write generation (`_write_gen`)** — a monotonic counter per table, incremented on each mutating operation. Stored as an internal column on every row. Used for crash-safe upserts: new rows are written with the current generation, old rows are deleted by `(logical_key) AND _write_gen < current`. On recovery, duplicates (same logical key, different generation) are resolved by keeping the highest generation. Skipped children (fingerprint match) get their `_write_gen` bumped to survive cleanup.

**Row status (`_status`)** — an internal column on every row: `"complete"` (derivation succeeded) or `"error"` (derivation failed under `on_error="store"`). Rows with `_status=None` are treated as `"complete"` for migration safety — pre-upgrade rows lack this column and must not be re-processed.

**Error message (`_error`)** — an internal column storing `None` (success) or `"{ExceptionType}: {message}"` (failure). Only populated when `on_error="store"` and derivation fails.

**Error policy (`on_error`)** — a HyperTable constructor parameter controlling failure behavior. `"raise"` (default) propagates exceptions as before. `"store"` writes an error row with source columns preserved, derived columns as `None`, `_status="error"`, and the exception recorded in `_error`. Error rows with matching fingerprints are retried (not skipped) on the next insert/sync. The policy propagates through `bind()` and `with_runner()`.

**Error row** — a row written under `on_error="store"` when derivation fails. Contains: identity column, source columns (preserved from input), derived columns (`None`), `_row_fingerprint` (computed normally), `_status="error"`, `_error` (exception string), `_write_gen` (current), provenance columns (`None`). On retry, the fingerprint matches but `_status="error"` prevents skipping, so the graph re-runs.

**Reserved names** — column names that collide with internal columns are rejected at graph analysis time: `_status`, `_error`, `_row_fingerprint`, `_write_gen`, `_parent_id`, and any name starting with `_provenance_`. This applies to identity and source columns. Derived column names with a `_` prefix are already rejected by the Graph layer's output name validation.

**`include_status`** — a parameter on `get()`, `children()`, `filter()`, and `filter_children()` that includes `_status` and `_error` in returned rows. Without it, these fields are stripped for backward compatibility.

**`SyncResult.errors`** — a `tuple[ErrorRow, ...]` field on `SyncResult` populated when `on_error="store"`. Each `ErrorRow` contains `identity` (dict), `error_type` (str), and `error_msg` (str) for programmatic inspection of which items failed during `sync()`.

**Sink** — writes results to the store as they're ready. The `Sink` protocol + `LanceSink` are already built (on `feat/materialization-streaming`). Supports write-as-ready streaming — no buffering the whole batch.

**Pin / Override** (v2) — manually set a derived column's value, skipping its node. The pinned value survives re-derivation from upstream (e.g., a pinned transcription isn't overwritten when re-syncing). Downstream columns still cascade from the pinned value. Deferred to v2 pending a full state-machine spec (allowed columns, unpin behavior, content-key updates, recompute/sync/delete rules).

**Recompute** — re-run a node for all rows, triggered by a component change (e.g., swapping the embedder model). Only the affected column and its downstream columns recompute. Unrelated columns are untouched.

**Schema evolution** — what happens when you change the graph and reopen an existing table. Safe changes (add column, change logic) happen automatically. Destructive changes (drop column, rename, backfill) require an explicit call. The principle: no silent data loss, no unbounded surprise compute.

**Query graph** — a normal Hypergraph graph that reads from a populated store at query time (e.g., hybrid search with BM25 + vector + reranking). It is agnostic to HyperTable: HyperTable may have produced the stored rows, but query-time retrieval does not bind or call the HyperTable object.

**Ephemeral output** — a node output marked `ephemeral=True`. Flows through graph wiring (downstream nodes can consume it) but is NOT stored as a column. Exists only during execution. Use for intermediate values like raw LLM responses, cost metadata, or large temporary data that only the next node needs.

**Schema mismatch** — what HyperTable detects when the graph's derived columns don't match the stored table's columns. Each mismatch type has a specific resolution: auto for safe changes, error-with-guidance for destructive changes.


## How Construction Works (Graph Analysis)

When you write `HyperTable([nodes], identity=..., store=...)`, the constructor analyzes the graph to build the table schema. No magic — it reads the structure Hypergraph already exposes.

```python
# Hypergraph already provides all of this:
graph.inputs.required    # → {"path"}               unbound inputs = source columns
graph.inputs.bound       # → {"model", "embedder"}  bound values = components (not stored)
node.data_inputs         # → ["path"]               what a node consumes
node.data_outputs        # → ["audio_path"]          what a node produces = derived column
nx.topological_sort(g)   # → execution order
map_over_node.map_config # → {map_over="utterances", identity="utterance_id"}
map_over_node.inner_graph # → the subgraph (process_utterance)
```

The construction logic walks the graph in topological order:

1. **Identity column** — from the explicit `identity=` parameter.
2. **Source columns** — from `graph.inputs.required` (unbound graph inputs). Each is a content key because it feeds a node.
3. **Metadata columns** — discovered at first insert. Extra kwargs that don't match any graph input are stored but don't trigger re-derivation.
4. **Derived columns** — for each non-map_over node, its `data_outputs` become derived columns on the current table.
5. **Grain boundary** — when a `map_over` node is encountered, a child table is created. The child's identity comes from `map_over(..., identity=...)`. The child's source columns come from the split function's output fields. The child's derived columns come from the subgraph's nodes (analyzed recursively).
6. **Parent link** — auto-added on the child table. Named `_parent_id`, equals the parent's identity value.

This produces a `TableSpec` per grain — the column names, roles (identity/source/derived/parent_link), which node produces each derived column, and the parent-child relationship.

**Deferred analysis:** Graph analysis and store open happen lazily at first use (first read or write operation), NOT in the constructor. This ensures `.bind()` has already been called, so `graph.inputs.bound` correctly reflects components. The constructor only validates the node list structure (types, wiring).


## Core Design Decisions

### 1. A HyperTable is a Graph with persistence

The declaration looks like `Graph([nodes])` but with a store and identity:

```python
subtext = HyperTable(
    [extract_audio, transcribe, to_markdown,
     split_utterances,
     process_utterance.as_node().map_over("utterances", identity="utterance_id")],
    identity="video_id",
    store="lancedb://./data",
    on_error="store",  # write error rows for failed children instead of raising
).bind(model=Whisper(), embedder=Embedder()).with_runner(SyncRunner())
```

Nodes are standard `@node` from Hypergraph. Auto-wiring by name works the same way. `.bind()` works the same way. `.visualize()` works because it IS a graph.

### 2. Nodes are derived columns, not derived tables

Each node adds a column to the table via the equivalent of `df.with_column()`. The DaftRunner already does this — `execute_plan` chains `op.apply(df)` for each node. V1 uses SyncRunner and achieves the same per-row via `map_iter`.

Within a single grain, there is one table with source columns + derived columns. No intermediate tables, no intermediate dataclasses.

### 3. Grain boundaries are map_over, not a new primitive

The 1:N expansion (e.g., split a video into utterances) uses standard Hypergraph `map_over`. No `explode` method needed. Write logic for ONE item (the `process_utterance` subgraph), scale with `map_over`.

The subgraph is a regular Hypergraph `Graph` — testable on its own, has its own `.visualize()`.

### 4. Content keys are inferred from the graph

Any source column consumed by a node is a content key. Any source column NOT consumed is metadata. The graph structure determines this — no `ContentKey` annotation needed.

- `path` feeds `extract_audio` → content key (change triggers re-derivation)
- `title` feeds no node → metadata (change is stored, no re-derivation)

### 5. Identity is always explicit

No naming-convention inference. Two declarations: one on the table, one per grain boundary.

```python
HyperTable([...], identity="video_id")
process_utterance.as_node().map_over("utterances", identity="utterance_id")
```

**Prerequisite:** `GraphNode.map_over(..., identity=..., schema=...)` is a new Hypergraph core API addition. Current `map_over` does not accept `identity=` or `schema=`. Both must be added:
- `identity=` declares the child row identity field.
- `schema=` declares the child item type (TypedDict or dataclass) so HyperTable can resolve child table columns at construction without executing user code.

If `schema=` is omitted, HyperTable infers it from the split node's typed return annotation (e.g., `list[Utterance]`). If neither is available, construction raises `SchemaError`. Values are stored in `map_config` and exposed via `map_over_node.map_config["identity"]` and `map_over_node.map_config["schema"]`.

### 6. Functions are simple and portable

Node functions take a value and return a value. No framework types, no dataclasses, no HyperTable imports. Components are injected by argument name via `.bind()`.

```python
def extract_audio(path: str) -> str: ...
def transcribe(audio_path: str, model: WhisperModel) -> str: ...
def clean_text(text: str) -> str: ...
def embed(clean_text: str, embedder: Embedder) -> list[float]: ...
```

Testable directly: `assert embed("hello", my_embedder) == [0.1, 0.2, ...]`.

### 6a. Columns store whatever the node returns — including multimodal data

HyperTable doesn't care about column types. A node that returns `str` produces a string column. A node that returns `bytes` produces a binary column. A node that returns `list[float]` produces a vector column.

LanceDB is a multimodal database — it stores text, vectors, images, audio, and video natively. HyperTable leverages this: a node can return actual audio bytes, image data, or any binary payload, and the column stores it directly in LanceDB.

```python
@node(output_name="audio")
def extract_audio(path: str) -> bytes:        # returns actual audio data, not a file path
    with open(path, "rb") as f:
        return f.read()

@node(output_name="json_data")
def transcribe(audio: bytes, model: WhisperModel) -> str:  # takes audio bytes directly
    return model.transcribe(audio)

@node(output_name="thumbnail")
def extract_thumbnail(path: str) -> bytes:    # returns image bytes
    return ffmpeg_extract_frame(path, t=0)
```

The root table stores the video file, extracted audio, and thumbnail as binary columns alongside text columns like `json_data` and `markdown`. All queryable, all incrementally maintained.

This matters for streaming writes: when columns contain large binary data (a 500MB video), write-as-ready via the sink avoids buffering the whole batch in memory.

**Physical schema mapping** (node return type → PyArrow/LanceDB column type):
- `str` → `pa.utf8()`
- `int` → `pa.int64()`
- `float` → `pa.float64()`
- `bool` → `pa.bool_()`
- `bytes` → `pa.large_binary()`
- `list[float]` → `pa.list_(pa.float32())` (LanceDB vector column, enables ANN search)
- `dict` / complex → `pa.utf8()` (JSON-serialized)

Type annotation on the node function determines the physical column type at construction. Missing annotations default to `pa.utf8()` (store as string).

### 7. Runner is separate from the table — set via `.with_runner()`

The runner is not part of the constructor. It's set once via `.with_runner()` and can be overridden per-call. Read-only operations (search, filter, count) don't need a runner at all.

HyperTable supports both `SyncRunner` and `AsyncRunner`. DaftRunner support is future work. With `AsyncRunner`, write operations (`insert`, `sync`, `update`) return coroutines.

```python
# Construction — no runner
subtext = HyperTable(
    [extract_audio, transcribe, ...],
    identity="video_id",
    store="lancedb://./data",
).bind(model=Whisper(), embedder=Embedder())

# Set a default runner (returns a new instance)
subtext = subtext.with_runner(SyncRunner())

# Now write operations use SyncRunner by default
subtext.insert(video_id="v1", path="/data/meeting.mp4")

# Read operations never need a runner
subtext.get("v1")
subtext.count()

# Async variant
subtext_async = subtext.with_runner(AsyncRunner())
await subtext_async.insert(video_id="v2", path="/data/talk.mp4")
```

### 8. Dataclasses are optional, not required

No intermediate dataclasses for each stage. The schema is inferred from:
- Graph inputs → source columns
- Node outputs → derived columns
- Insert kwargs → metadata columns
- `identity=` → identity column

You only need a dataclass if YOU want one (e.g., for type checking the return of `split_utterances`).

### 9. No new library — lives inside Hypergraph

`from hypergraph.materialization import HyperTable`. Same repo, same ecosystem. HyperTable uses Hypergraph's runners, nodes, graphs, and auto-wiring. Hypergraph never knows HyperTable exists — the dependency is one-directional.

### 10. Schema evolution — safe changes auto, destructive changes explicit

When you change the graph and reopen an existing table, HyperTable detects the difference between the new graph schema and the stored table schema. Safe changes apply automatically. Destructive changes require an explicit call.

**Safe (automatic):**
- **Add a column** — a new node in the graph. The column is added to the table with NULL for existing rows. Backfill happens on next sync/recompute if desired.
- **Change node logic** — the content key includes a definition hash of the node function. If the function body changes, existing rows' content keys mismatch → re-derivation on next sync.
- **Change a component config** — swap `Embedder("v1")` for `Embedder("v2")`. The content key includes the component config hash → affected columns recompute on next sync.

**Destructive (explicit call required):**
- **Drop a column** — `subtext.drop_column("markdown")`. Removes the column from the table and the node from the derivation plan. No silent data loss.
- **Rename a column** — `subtext.rename_column("markdown", "md")`. Preserves data, updates the schema.
- **Backfill** — `subtext.backfill("new_column")`. Runs the new node for all existing rows.
- **Full recompute** — `subtext.recompute("vector", components={"embedder": Embedder("v2")})`. Re-derives a column for every row with the new component.

The principle: if you can lose data or trigger unbounded compute, you say so explicitly. If it's safe and bounded, it happens automatically.

### 11. Query-time graphs read the store directly

HyperTable materializes rows into a store. Retrieval is a separate Hypergraph graph that opens or receives the queryable store/table directly. The retrieval graph is not attached to HyperTable and does not call HyperTable methods.

```python
@node(output_name="bm25_hits")
def bm25_search(query: str, lance_table) -> list[dict]:
    return lance_table.search(query, query_type="fts").limit(20).to_list()

@node(output_name="query_vector")
def embed_query(query: str, embedder: Embedder) -> list[float]:
    return embedder.embed(query)

@node(output_name="vector_hits")
def vector_search(query_vector: list[float], lance_table) -> list[dict]:
    return lance_table.search(query_vector, vector_column_name="vector").limit(20).to_list()

@node(output_name="merged")
def rrf_merge(bm25_hits: list[dict], vector_hits: list[dict]) -> list[dict]: ...

@node(output_name="results")
def rerank(merged: list[dict], query: str, reranker: CrossEncoder) -> list[dict]: ...

hybrid_search = Graph([embed_query, bm25_search, vector_search, rrf_merge, rerank])

results = SyncRunner().run(
    hybrid_search,
    query="quarterly revenue",
    lance_table=db.open_table("documents"),
    embedder=Embedder(),
    reranker=CrossEncoder(),
)
```

**Why this separation matters:**
- The materialization graph (insert/update) and the query graph are different graphs with different purposes. The materialization graph writes; the query graph reads.
- Query graphs work over any LanceDB table with the expected columns, including tables not produced by HyperTable.
- The query graph is a normal Hypergraph graph — testable, visualizable, composable. Not a special HyperTable concept.

### 12. Ephemeral outputs — intermediate values that aren't stored

By default, every node output becomes a stored column. For intermediate values that should flow between nodes but NOT be persisted, mark the output `ephemeral=True`:

```python
@node(output_name="raw_response", ephemeral=True)
def call_llm(prompt: str, llm: LLM) -> dict:
    return llm.generate(prompt)   # {"answer": "...", "usage": {"tokens": 150, "cost": 0.003}}

@node(output_name="answer")
def extract_answer(raw_response: dict) -> str:
    return raw_response["answer"]
```

`raw_response` flows through graph wiring — `extract_answer` receives it. But the table has no `raw_response` column. Only `answer` is stored.

Use cases:
- **LLM usage/cost metadata** that feeds a logging node but shouldn't persist as a column.
- **Large temporary data** (e.g., a full API response parsed by multiple downstream nodes).
- **Intermediate computations** only useful for the next node.

If you don't need the intermediate value elsewhere in the graph, the simpler answer is: keep it inside the node. One node calls the LLM and returns only the answer string. `ephemeral=True` is only needed when an intermediate must flow between nodes.

On re-derivation, ephemeral values are recomputed from scratch (there's no stored value to compare against). The content key for downstream nodes still includes the ephemeral node's definition hash — so a change in the ephemeral node's logic correctly triggers re-derivation of everything downstream.

### 13. Schema mismatch — every edge case has a specific resolution

When you change the graph and connect to an existing table, HyperTable compares the new graph schema to the stored schema. Every type of mismatch has a defined behavior:

| What changed | What happens | Auto or explicit? |
|---|---|---|
| **Node body edited** | Definition hash changes → content key mismatch → re-derive on next sync | Auto |
| **Component config changed** | Config hash changes → same mechanism as node body | Auto |
| **New node added** | New column added with NULL for existing rows | Auto |
| **Node removed** | Stored column has no producer → error: "column X has no producing node — call `drop_column('X')`" | Explicit |
| **Output type changed** (str → bytes) | Schema type mismatch → error: "column X type changed — call `rebuild_column('X')`" | Explicit |
| **Node renamed** (output_name) | Old column orphaned + new column added → error for orphan (see "node removed"), auto-add for new | Explicit + Auto |
| **Node input wiring changed** | Content key changes (different inputs) → re-derive on next sync | Auto |
| **External table modification** | Not supported — HyperTable owns the schema | N/A |

The principle: safe and bounded → automatic. Ambiguous or lossy → error with a message telling you exactly what explicit call to make.


## What's NOT HyperTable's Job

- **Stateless compute** — that's Hypergraph (graphs, runners, visualization, tracing)
- **Component caching** — that's Hypercache (inside components, cross-id dedup)
- **Config construction** — that's Hypster (factories, `hp.select`, presets)
- **Studio/CLI/product layer** — that's Superposition `sp`

HyperTable only owns: persistent storage, identity tracking, content-key incrementality, cascade, and CRUD operations.


## Open Design Questions

1. **Pin/override semantics** — how does a pinned derived value interact with sync? Current intent: re-derivation from upstream skips pinned rows. Needs a concrete spec.

2. **Multi-column nodes** — a node with multiple outputs (Hypergraph supports this via multi-output packing). How do these map to table columns? The DaftRunner already handles this with `_pack_`/`_unpack_` — same pattern applies.

3. **Fan-in at the table level** — a node that takes inputs from two different sources (e.g., utterance text + video metadata). Within a single grain, auto-wiring handles this. Across grains (child needs parent data), the parent link provides the join key, but the execution model needs to support it.

4. ~~**Error rows**~~ — Resolved in PRD 0005. `on_error="store"` writes error rows with `_status="error"`, source columns preserved, derived columns `None`. Error rows are retried on next insert/sync. `include_status=True` exposes `_status`/`_error` on read methods. `SyncResult.errors` provides programmatic access. See the error policy and error row glossary entries above.

5. **Progress/tracing during materialization** — the `event_processors` gap from the DerivedTable work. SyncRunner/AsyncRunner support events; DaftRunner does not yet.

6. **Lineage visualization** — rendering the full table DAG (source columns → derived columns → grain boundaries → child tables). The graph's `.visualize()` shows compute; HyperTable needs a storage-aware view.

7. **`on_error` for `update()`** — Currently `update()` always raises on failure. Unlike `insert()`, an update operates on an existing good row — writing an error row would destroy valid derived data. Pixeltable's `update()` is also always-raise (atomic rollback via PostgreSQL). The right semantics may be "leave existing row untouched, report failure" rather than "write error row." Deferred pending a concrete use case.


## Relationship to Existing Work

The `feat/materialization-streaming` branch in hypergraph built the foundations:

- **`map_iter`** on SyncRunner/AsyncRunner — streaming execution (Step 1, built)
- **`Sink` protocol + `LanceSink`** — write-as-ready to LanceDB (Steps 2-3, built)
- **`DerivedTable`** — content-key incrementality, cascade, CRUD (built, 92 tests green)
- **DaftRunner `map_dataframe`** — graph-to-columnar translation (built, on main)

HyperTable is the evolution of DerivedTable. It replaces the explicit chain-of-tables API with graph-native composition, replaces per-stage dataclasses with inferred column schemas, and uses standard Hypergraph `map_over` instead of a custom `explode` concept.
