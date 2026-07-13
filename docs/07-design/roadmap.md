# Roadmap

What's implemented, what's coming, and the vision for hypergraph.

## Current Status: Alpha

Core features are working and stable. API may still change before 1.0.

## What's Working Now

### Core
- `@node` decorator for wrapping functions (sync, async, generators)
- `Graph` construction with automatic edge inference
- `InputSpec` categorization (required, optional, and bound values)
- Rename API (`.rename_inputs()`, `.rename_outputs()`, `.with_name()`)
- Build-time validation with helpful error messages

### Composition
- Hierarchical composition (`.as_node()`)
- Batch processing (`.map_over()`)

### Execution
- `SyncRunner` for sequential execution
- `AsyncRunner` with concurrency control (`max_concurrency`)
- Batch processing with `runner.map()` (zip and product modes)
- Process-local `SyncHandle` / `AsyncHandle` from `start_run()` and `start_map()`
- Cooperative stop for live runs, nested graphs, and sparse stopped maps
- `DaftRunner` for columnar and distributed DAG execution

### Checkpointing and Durability
- `SqliteCheckpointer` for persistent run history
- Resume from checkpoint after failures
- Fork and retry with lineage tracking
- Workflow-id based forking/retrying on `run()`

### Materialization (HyperTable)
- `HyperTable` — persistent incremental tables backed by Hypergraph graphs
- Graph-inferred schema (source columns, derived columns, grain boundaries)
- Content-key incrementality with row fingerprints and column provenance
- `insert()`, `update()`, `delete()`, `sync()` CRUD operations
- Child tables via `map_over` grain boundaries with parent links
- `on_error="store"` for partial-success tolerance with error rows
- Child fingerprints for resumable inserts after crashes
- `SyncResult.errors` for programmatic error inspection
- `TableStore` protocol with `LanceDBStore` implementation
- Both `SyncRunner` and `AsyncRunner` support

### Control Flow
- `@route` for conditional routing with `END` sentinel
- `@ifelse` for binary boolean routing
- Cyclic graphs for agentic loops and multi-turn workflows

### Human-in-the-Loop
- `InterruptNode` to pause execution and wait for input
- Resume with user-provided data
- Auto-resolve with handler functions

### Caching
- `@node(cache=True)` opt-in caching per node
- `InMemoryCache` (LRU, ephemeral) and `DiskCache` (persistent via diskcache)
- `CacheBackend` protocol for custom backends
- Automatic cache invalidation on code changes (`definition_hash`)
- `CacheHitEvent` for observability

### Events & Observability
- Event system with `EventProcessor`, `AsyncEventProcessor`, `TypedEventProcessor`
- `RichProgressProcessor` for hierarchical Rich progress bars
- Event types: `RunStartEvent`, `RunEndEvent`, `NodeStartEvent`, `NodeEndEvent`, `NodeErrorEvent`, `RouteDecisionEvent`, `InterruptEvent`, `CacheHitEvent`, `StopRequestedEvent`
- `OpenTelemetryProcessor` with graph/map/node spans and truthful batch outcomes

### Visualization
- `graph.visualize()` for interactive graph rendering in notebooks
- Dark/light/auto theme support
- Expand/collapse nested graphs interactively
- Type annotation display (`show_types=True`)
- Standalone HTML export (`filepath="graph.html"`)
- Constraint-based layout with bundled React Flow — works offline

### Validation
- `strict_types=True` for type checking at graph construction
- Type mismatch detection with helpful error messages
- Route target validation

## Coming Soon

### Materialization Enhancements
- Pin/override semantics for manually setting derived column values
- Streaming writes via `Sink` protocol (built, not yet integrated with HyperTable)
- `DaftRunner` support for columnar materialization
- Lineage visualization (storage-aware view of the table DAG)

## Future Considerations

### Distributed Execution
- Additional distributed runner backends beyond Daft
- Automatic data partitioning

### Execution Trace Visualization
- Overlay execution traces on graph visualization
- Show timing, values, and errors per node

## Non-Goals

Things we're explicitly not building:

- **Infrastructure orchestration** — Use Prefect, Airflow, or Temporal for job scheduling
- **Model serving** — Use dedicated serving infrastructure
- **Storage engine** — HyperTable delegates storage to pluggable `TableStore` backends (LanceDB, etc.). Hypergraph itself is not a database.
- **UI framework** — We're a library, not a platform

## Contributing

See the GitHub repository for contribution guidelines. Areas where help is welcome:

- Additional runner implementations
- Storage backend integrations
- Visualization tools
- Documentation improvements
- Real-world example contributions
