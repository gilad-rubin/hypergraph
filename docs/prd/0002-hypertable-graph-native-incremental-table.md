# PRD 0002 — HyperTable: graph-native incremental table

status: ready-for-agent

## Problem Statement

DerivedTable (built in PRD 0001) works — insert, update, delete, sync, recompute,
cascade, content-key incrementality, streaming via sinks — but requires the user to
manually wire chains of per-stage dataclass tables (`Document → Chunk → EmbeddedChunk`),
declare `Identity` and `ContentKey` via `Annotated` markers on frozen dataclasses, and
manage the parent-child chain topology themselves.

This creates three friction points:

1. **Schema repetition.** Every stage needs its own dataclass. The column list is
   duplicated between the dataclass and the derive function. Adding a column means
   touching both.
2. **Topology management.** The user manually chains `DerivedTable(source=parent_table, ...)`
   for each 1:N expansion and each derivation step. The chain is code, not visible
   structure — and it doesn't compose with Hypergraph's graph visualization.
3. **Two mental models.** Users who already know Hypergraph (`@node`, `Graph`, `.bind()`,
   `map_over`, runners) must learn a second set of concepts (DerivedTable chains,
   `Annotated[str, Identity]`, `explode`) to get persistence. The graph and the table
   should be the same thing.

## Solution

HyperTable — a Hypergraph `Graph` where each node's output is a stored column, and a
content-key check decides whether to re-run.

The user writes standard `@node` functions, composes them into a `HyperTable` (same
syntax as `Graph`, plus `identity=` and `store=`), and calls `.bind()` and `.with_runner()`
exactly as they would on a graph. HyperTable analyzes the graph structure at construction
to infer the table schema: source columns from `graph.inputs.required`, derived columns
from `node.data_outputs`, grain boundaries from `map_over` nodes, parent links
auto-stamped. No dataclasses, no `Annotated` markers, no manual chain wiring.

CRUD operations (`insert`, `update`, `delete`, `sync`, `recompute`, `search`,
`filter`) work on the HyperTable directly. Schema evolution is safe-auto for additive
changes (new column, logic change) and error-with-guidance for destructive ones (drop,
rename, type change). Query-time graphs (hybrid search, reranking) attach to a `.queries`
namespace as standard Hypergraph graphs.

## User Stories

1. As a developer, I want to declare a HyperTable with standard `@node` functions and
   `Graph`-style composition, so that I don't have to learn a second API for persistence.

2. As a developer, I want HyperTable to infer source columns from `graph.inputs.required`,
   so that I don't have to declare them in a dataclass or annotate them with markers.

3. As a developer, I want each node's output to automatically become a stored column,
   so that I don't have to wire column→node mappings manually.

4. As a developer, I want to declare identity explicitly (`identity="video_id"` and
   `map_over(..., identity="utterance_id")`), so that there is no naming-convention magic.

5. As a developer, I want content keys to be inferred from the graph (any source column
   that feeds a node), so that I don't have to annotate them.

6. As a developer, I want `.bind()` on HyperTable to work exactly like on Graph, so that
   components (model, embedder) are injected by argument name and not stored as columns.

7. As a developer, I want `.with_runner(SyncRunner())` to set a default runner without
   coupling it to the constructor, so that I can override per-call and so that read
   operations work without a runner.

8. As a developer, I want `.with_runner(SyncRunner())` to be the only supported
   runner in v1, so that the implementation is simple and correct before adding
   concurrency later.

9. As a developer, I want read operations (`.search()`, `.filter()`, `.count()`) to
   work without a runner, so that I can query the table without configuring execution.

10. As a developer, I want to insert a single item via kwargs
    (`subtext.insert(video_id="v1", path="...")`), so that the API is natural for
    single-item use.

11. As a developer, I want to insert a batch via a list of dicts
    (`subtext.insert([{...}, {...}])`), so that batch ingest is straightforward.

12. As a developer, I want extra kwargs at insert time (e.g., `title`) that don't match
    any graph input to be stored as metadata columns without triggering re-derivation,
    so that I can attach non-computed data.

13. As a developer, I want updating a content-key column to re-derive all downstream
    columns and cascade to child tables, so that derived data stays consistent.

14. As a developer, I want updating a metadata column to store the new value without
    re-derivation, so that metadata edits are cheap.

15. As a developer, I want `.delete(id)` to cascade-delete all child rows, so that
    referential integrity is maintained.

16. As a developer, I want `.sync(current_items)` to reconcile: insert new, update
    changed (by content key), delete missing, skip unchanged — and return a SyncResult.

17. As a developer, I want `.recompute(column, components={...})` to re-derive only the
    affected column for all rows, so that swapping a component doesn't recompute
    everything.

18. As a developer, I want `map_over` in the graph to create a derived (child-grain)
    table with its own identity and a `_parent_id` parent link, so that 1:N expansion
    uses standard Hypergraph composition.

19. As a developer, I want `.children(parent_id)` to return all child rows for a given
    parent, so that I can navigate the grain hierarchy.

20. As a developer, I want `.visualize()` to render the graph with grain boundaries
    (root table → child table), so that the table structure is visible.

21. As a developer, I want adding a new node to the graph to auto-add the column with
    NULL for existing rows, so that safe schema evolution is automatic.

24. As a developer, I want changing a node's function body to auto-invalidate existing
    rows (via definition hash in content key), so that logic fixes propagate on next sync.

25. As a developer, I want changing a component config to auto-invalidate only the
    affected column, so that re-derivation is scoped.

26. As a developer, I want removing a node from the graph to error at construction with
    a message telling me to call `drop_column()`, so that column removal is never silent.

27. As a developer, I want changing a column's type to error at construction with a
    message telling me to call `rebuild_column()`, so that type changes are never silent.

28. As a developer, I want `.backfill(column)` to derive a new column for all existing
    rows that have NULL, so that I can populate new columns on demand.

29. As a developer, I want to mark a node output `ephemeral=True`, so that it flows
    through graph wiring but is not stored as a column.

30. As a developer, I want ephemeral outputs to still affect downstream content keys
    (via definition hash), so that changes in ephemeral logic trigger correct
    re-derivation.

31. As a developer, I want to attach a query graph to `subtext.queries.hybrid = graph`,
    so that I can name retrieval strategies dynamically.

32. As a developer, I want to call `subtext.queries.hybrid(query="...")`, so that the
    table binds itself and runs the query graph.

33. As a developer, I want query graphs to be normal Hypergraph graphs (testable,
    visualizable), so that retrieval logic is not a special HyperTable concept.

34. As a developer, I want the HyperTable to support multimodal column types (str, bytes,
    list[float]) based on node return types, so that binary data (audio, images) is
    stored natively in LanceDB.

35. As a developer, I want streaming writes via the sink during insert/sync, so that
    large binary columns don't buffer the whole batch in memory.

36. As a developer, I want error handling per row (a failed node stores an error marker,
    skips downstream columns), so that one bad row doesn't abort the batch.

37. As a developer, I want `.count()` and `.count(child_table_name)` to return row counts
    for root and child tables respectively.

## Implementation Decisions

### Graph analysis at construction

HyperTable analyzes the graph using Hypergraph's existing introspection API:
- `graph.inputs.required` → source columns (content keys, because they feed nodes)
- `graph.inputs.bound` → components (not stored)
- `node.data_outputs` → derived columns (one per non-ephemeral node output)
- `nx.topological_sort(graph)` → derivation order
- `map_over_node.map_config` → grain boundary detection
- `map_over_node.inner_graph` → recursive analysis for child table schema

The result is a `TableSpec` per grain: column names, roles
(identity/source/derived/parent_link/internal), which node produces each derived column,
and the parent-child relationship. Physical tables are created or opened in the store
based on this spec.

### Runner separation

The runner is NOT on the constructor. Three-step construction:
1. `HyperTable([nodes], identity=..., store=...)` — graph + identity + store (validates
   node list structure only — no schema resolution yet)
2. `.bind(model=..., embedder=...)` — component injection (same as Graph)
3. `.with_runner(SyncRunner())` — default runner (returns new immutable instance)

**Graph analysis and store open are deferred** until the first operation (read or write).
At that point `graph.inputs.bound` correctly reflects bound components (from step 2), so
components are never confused with source columns. The analysis result (TableSpec) is
cached for subsequent operations.

Read operations (search, filter, count) work without a runner. Write operations
(insert, update, delete, sync, recompute, backfill) require a runner — error if none set.

**V1 is sync-only.** Only `SyncRunner` is supported in v1. This aligns with ADR 0001's
decision to split sync/async into separate classes. AsyncRunner and DaftRunner support
are future work requiring a superseding ADR.

### Schema evolution

On open (connecting a graph to an existing store), compare new graph schema to stored
schema:
- **New column (node added):** auto-add with NULL. Safe.
- **Logic change (node body edited):** definition hash in content key mismatches → auto
  re-derive on next sync. Safe.
- **Component config change:** config hash in content key mismatches → auto re-derive on
  next sync. Safe.
- **Orphaned column (node removed):** `SchemaEvolutionError` with guidance to call
  `drop_column()`. Explicit.
- **Type change (return type changed):** `SchemaEvolutionError` with guidance to call
  `rebuild_column()`. Explicit.
- **Renamed column (output_name changed):** orphan error (old) + auto-add (new). Explicit
  for orphan; can use `rename_column()` to migrate.

### Ephemeral outputs

`@node(output_name="x", ephemeral=True)` — the output flows through graph wiring but is
not stored as a column. At construction, HyperTable skips ephemeral outputs when building
the `TableSpec`. During execution, the value is computed and passed to downstream nodes
normally, then discarded. The ephemeral node's definition hash is still part of downstream
content keys, so logic changes propagate correctly.

### Query namespace

`subtext.queries` is a `QueryNamespace` object. Assigning a graph
(`subtext.queries.hybrid = graph`) stores it. Calling it
(`subtext.queries.hybrid(query="...")`) runs
`runner.run(graph, table=self, **kwargs)`. The table binds itself as the `table` parameter.
This avoids collisions with table methods and allows any name.

### Per-column provenance (content key)

Each derived column on each row has its own provenance hash:
`hash(upstream input values consumed by this node + producer node definition hash + relevant component config hashes for this node)`

Stored as `_provenance_{column_name}` internal columns. On update/sync, provenance is
recomputed per column and compared. Match → skip that column. Mismatch → re-derive that
column and its downstream dependents.

This enables scoped recompute: swapping an embedder only invalidates columns whose
provenance includes the embedder config, not the entire row. It replaces DerivedTable's
single `_content_key` per row with per-column granularity.

For the row-level "should anything re-derive?" fast path, a top-level
`_row_fingerprint = hash(all source content-key values + all node definition hashes + all component config hashes)` is also stored. If the row fingerprint is unchanged, no
per-column checks are needed (all provenances will match). This fingerprint incorporates
the full derivation plan — swapping a component, editing a node body, or changing source
values all invalidate it, ensuring per-column checks run whenever anything relevant changes.

### Child table primary key

The child table's **logical** primary key is the composite `(_parent_id, child_identity)`.
Child identities are scoped by parent — two parents can emit the same child identity
without collision. Cascade operations (update/delete) scope by `_parent_id` first, then
match by child identity within that parent's rows.

**Uniqueness enforcement:** LanceDB does not enforce uniqueness constraints natively.
HyperTable enforces logical uniqueness at the API layer: before writing child rows,
validate no duplicate `(parent_id, child_identity)` pairs exist in the batch. If a derive
produces duplicates, raise `DuplicateIdentityError` before any write.

**Update ordering (crash safety):** each mutating operation increments a monotonic
`_write_gen` counter (stored per-table, persisted alongside the data). Child updates:
1. Write new rows with the current `_write_gen` value.
2. Delete old rows identified by `(_parent_id, child_identity)` AND `_write_gen < current`.

On recovery (after a crash mid-cascade), reconciliation finds duplicates (same logical
key, different `_write_gen`) and keeps the row with the highest `_write_gen`. This is safe
because `_write_gen` provides temporal ordering that provenance hashes cannot. Matches
DerivedTable's write-new-then-delete-old pattern but uses generation for disambiguation.

### Prerequisite: `map_over(..., identity=..., schema=...)`

HyperTable requires a new Hypergraph core API addition: `GraphNode.map_over(..., identity="field_name", schema=TypedDict|dataclass)`. Current `map_over` does not accept `identity=` or `schema=`. Both must be added to Hypergraph:
- `identity=` declares which field is the child row identity.
- `schema=` declares the child item type (a TypedDict or dataclass). This lets HyperTable resolve child table columns at construction time without executing user code.

If `schema=` is omitted but the split node's return type annotation is `list[SomeTypedDict]` or `list[SomeDataclass]`, HyperTable infers the schema from the type annotation. If neither is available, construction raises `SchemaError("map_over requires schema= or a typed return annotation on the split node")`.

The values are stored in `map_config` and exposed for graph analysis.

### Physical schema mapping

HyperTable implements its own schema builder (`_schema.py`) rather than reusing
DerivedTable's `_store.py` type mapper (which lacks bytes/vector support). Node return
type annotations determine LanceDB column types:
- `str` → `pa.utf8()`
- `int` → `pa.int64()`
- `float` → `pa.float64()`
- `bool` → `pa.bool_()`
- `bytes` → `pa.large_binary()`
- `list[float]` → `pa.list_(pa.float32())` (vector column, enables ANN search in LanceDB)
- `dict` / complex → `pa.utf8()` (JSON-serialized via `json.dumps`)

Missing annotations default to `pa.utf8()`. Internal columns (`_row_fingerprint`,
`_provenance_*`) are always `pa.utf8()` (hex-encoded hashes).

### Relationship to DerivedTable

HyperTable replaces DerivedTable's public API. Internally, it reuses DerivedTable's
sink protocol (`_sink.py`, `LanceSink`) and error types (`_types.py`). It has its own
schema builder (`_schema.py`) for multimodal types and its own provenance engine
(`_provenance.py`) for per-column hashing. The chain-of-tables topology is replaced by
graph analysis; per-stage dataclasses are replaced by inferred column schemas; `explode`
is replaced by `map_over`.

## Testing Decisions

### What makes a good test

Tests assert external behavior through HyperTable's public API — never internal
implementation. A test inserts/updates/deletes/syncs, then verifies via get/filter/search/
count/children/errors. No test inspects `TableSpec` internals, content-key hashes, or
sink calls directly.

### Primary seam

**HyperTable public mutation + query API.** Same seam DerivedTable tests use (PRD 0001).
All CRUD operations are public methods; all verification is through public query methods.

### Test modules

- **`test_hypertable_construction.py`** — graph analysis produces correct columns and
  grain boundaries. Constructor validation (missing identity, ambiguous wiring, etc.).
- **`test_hypertable_crud.py`** — insert (single + batch), update (content key + metadata),
  delete (with cascade), sync (reconcile), recompute (scoped), count, filter, get,
  children. Core behavioral tests.
- **`test_hypertable_schema_evolution.py`** — add column (auto), logic change (auto via
  hash), node removed (error), type change (error), rename (error + migrate), backfill.
- **`test_hypertable_ephemeral.py`** — ephemeral outputs flow but aren't stored; downstream
  content keys include ephemeral node hash.
- **`test_hypertable_queries.py`** — `.queries` namespace: assign, call, table auto-binds,
  multiple strategies.
- **`test_hypertable_multimodal.py`** — bytes/vector column types stored and retrieved
  correctly in LanceDB.

### Prior art

- `tests/test_derived_table.py` — class-based grouping, frozen dataclasses with Annotated
  markers, mock components with `_config()`, autouse fixture for store cleanup.
- `tests/test_derived_table_sink.py` — runner inheritance, graph-derive materialization,
  multi-output handling, component swap hashing.

HyperTable tests follow the same style but use plain `@node` functions and dict-based
inserts instead of dataclasses (since HyperTable infers schema from the graph).

## Out of Scope

- **Pin/override** — edit a derived column value and have it survive re-derivation.
  Requires a state-machine spec (pin, unpin, cascade, content-key interaction). Deferred
  to v2 pending design.
- **AsyncHyperTable** — async execution support is deferred. HyperTable starts sync-only
  (SyncRunner). AsyncRunner support is a follow-up once the sync API is stable.
- **Arrow-native bulk reads (`scan()`)** — `get()` returns a Python dict (fine for
  single rows), but bulk access should stay Arrow-native: `table.scan()` returns
  `pa.Table` directly, zero-copy into Daft/Polars/DuckDB. Avoids the current
  Arrow → pandas → dict → (caller converts back to) Arrow triple-copy path.
  Prerequisite for efficient DaftRunner integration.
- **DaftRunner integration** — DaftRunner's `map_dataframe` already implements the
  graph-to-table translation, but wiring it into HyperTable is deferred.
- **Progress/tracing during materialization** — the `event_processors` gap. SyncRunner
  supports events, but threading them through HyperTable is deferred.
- **Lineage visualization** — storage-aware rendering of the full table DAG. `.visualize()`
  delegates to the underlying graph for now.
- **Cross-grain fan-in** — a child-grain node consuming parent-grain data across the
  grain boundary. Within a grain, auto-wiring handles it.
- **Hypster config integration** — wrapping HyperTable construction in a `*_config(hp)`
  factory is the user's responsibility, not a HyperTable feature.
- **Superposition `sp` integration** — Operation Bindings, Studio surfaces, and CLI
  commands that consume HyperTable belong in the `sp` layer.

## Further Notes

- HyperTable lives at `from hypergraph.materialization import HyperTable`. Same repo,
  same ecosystem. Hypergraph core never imports from materialization — the dependency is
  one-directional.
- PRD 0001 should be flipped to `done` — its deliverables (map_iter, Sink, LanceSink,
  streaming DerivedTable) are built and green.
- The design documents live at `docs/07-design/hypertable/CONTEXT.md` (13 decisions,
  glossary, graph analysis walkthrough) and `docs/07-design/hypertable/internals-walkthrough.py`
  (step-by-step pseudocode for every operation).
