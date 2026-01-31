# Roadmap

What's implemented, what's coming, and the vision for hypergraph.

## Current Status: Alpha

Core features are working and stable. API may still change before 1.0.

## What's Working Now

### Core
- `@node` decorator for wrapping functions (sync, async, generators)
- `Graph` construction with automatic edge inference
- `InputSpec` categorization (required, optional, bound, internal)
- Rename API (`.with_inputs()`, `.with_outputs()`, `.with_name()`)
- Build-time validation with helpful error messages

### Composition
- Hierarchical composition (`.as_node()`)
- Batch processing (`.map_over()`)

### Execution
- `SyncRunner` for sequential execution
- `AsyncRunner` with concurrency control (`max_concurrency`)
- Batch processing with `runner.map()` (zip and product modes)

### Control Flow
- `@route` for conditional routing with `END` sentinel
- `@ifelse` for binary boolean routing
- Cyclic graphs for agentic loops and multi-turn workflows

### Human-in-the-Loop
- `InterruptNode` to pause execution and wait for input
- Resume with user-provided data
- Auto-resolve with handler functions

### Events & Observability
- Event system with `EventProcessor`, `AsyncEventProcessor`, `TypedEventProcessor`
- `RichProgressProcessor` for hierarchical Rich progress bars
- Event types: `RunStartEvent`, `RunEndEvent`, `NodeStartEvent`, `NodeEndEvent`, `NodeErrorEvent`, `RouteDecisionEvent`, `InterruptEvent`

### Validation
- `strict_types=True` for type checking at graph construction
- Type mismatch detection with helpful error messages
- Route target validation

## Coming Soon

### Checkpointing and Durability
- Save graph state after each node execution
- Resume from checkpoint after failures
- Support for various storage backends (local, Redis, S3)

### Observability Integrations
- Integration with tracing systems (OpenTelemetry)
- Structured logging for each node execution
- Performance metrics (latency, token usage)

## Future Considerations

### Distributed Execution
- Integration with Daft for distributed processing
- Parallel execution across machines
- Automatic data partitioning

### Caching
- Content-addressable cache (hash inputs + function definition)
- Skip re-execution for unchanged nodes
- Cache invalidation strategies

### Visualization
- Graph visualization (Mermaid, Graphviz)
- Execution trace visualization
- Interactive debugging

### Persistence
- State snapshots for long-running workflows
- Version control for graph definitions
- Migration utilities

## Non-Goals

Things we're explicitly not building:

- **Infrastructure orchestration** — Use Prefect, Airflow, or Temporal for job scheduling
- **Model serving** — Use dedicated serving infrastructure
- **Data storage** — Hypergraph orchestrates, doesn't store
- **UI framework** — We're a library, not a platform

## Contributing

See the GitHub repository for contribution guidelines. Areas where help is welcome:

- Additional runner implementations
- Storage backend integrations
- Visualization tools
- Documentation improvements
- Real-world example contributions
