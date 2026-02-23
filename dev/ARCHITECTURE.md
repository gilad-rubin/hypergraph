# Architecture

Module boundaries and dependency rules for `src/hypergraph/`.

## Dependency Direction

```
nodes  →  graph  →  runners  →  events
                        ↓
                     _shared/
```

One-way: downstream modules import upstream, never the reverse. `nodes` knows nothing about `graph`. `graph` knows nothing about `runners`.

## Module Boundaries

### nodes/

Node types and decorators. Each node is a value object wrapping a callable.

| File | Purpose |
|------|---------|
| `base.py` | `HyperNode` abstract base, `END` sentinel |
| `function.py` | `FunctionNode`, `@node` decorator |
| `gate.py` | `GateNode`, `IfElseNode`, `RouteNode`, `@ifelse`, `@route` |
| `graph_node.py` | `GraphNode` — wraps a `Graph` as a node (`.as_node()`, `map_over`) |
| `interrupt.py` | `InterruptNode`, `@interrupt` for HITL |
| `_callable.py` | Internal: callable introspection (signatures, type hints) |
| `_rename.py` | Internal: copy-on-rename machinery, batch ID tracking |

**Rule**: Node objects are immutable values. `with_*` methods return new instances. Never mutate a node in place.

### graph/

Graph construction and build-time validation.

| File | Purpose |
|------|---------|
| `core.py` | `Graph` class — the build pipeline, `bind`/`select`/`unbind` |
| `input_spec.py` | `InputSpec` — classifies inputs as required/optional/entrypoint |
| `validation.py` | All build-time checks (names, edges, gates, types, conflicts) |
| `_conflict.py` | Name conflict detection and resolution |
| `_helpers.py` | Graph construction helpers |

**Build pipeline** (in `Graph.__init__`):
1. Normalize nodes into dict
2. Resolve output sources (which node produces which output)
3. Infer data edges (matching output → input names)
4. Infer control edges (gate targets)
5. Compute ordering edges (topological + cycle detection)
6. Validate everything

**Rule**: All structural errors must be caught at `Graph()` construction time, not during execution.

### runners/

Execution engines. Template Method pattern with pluggable `NodeExecutor` per node type.

| File | Purpose |
|------|---------|
| `base.py` | `BaseRunner` interface (shared by sync and async) |
| `_shared/` | Common utilities shared across runners |
| `sync/runner.py` | `SyncRunner` |
| `sync/superstep.py` | Superstep loop (sync) |
| `sync/executors/` | Per-node-type executors (function, graph, ifelse, route) |
| `async_/runner.py` | `AsyncRunner` |
| `async_/superstep.py` | Superstep loop (async) |
| `async_/executors/` | Per-node-type executors (function, graph, ifelse, route, interrupt) |

**Execution model**: Superstep-based. Each superstep executes all ready nodes (those with satisfied inputs), then loops until no new nodes are ready or convergence.

**`_shared/` contents**:
- `caching.py` — Cache key computation and lookup
- `event_helpers.py` — Emit lifecycle events
- `gate_execution.py` — Route/ifelse decision execution
- `helpers.py` — Input resolution, output storage
- `input_normalization.py` — Normalize user inputs for execution
- `routing_validation.py` — Validate routing decisions at runtime
- `template_sync.py` / `template_async.py` — Template Method base for superstep loops
- `types.py` — `GraphState`, `RunResult`, `RunStatus`, `PauseInfo`
- `protocols.py` — `NodeExecutor` protocol
- `validation.py` — Runner-level validation

**Rule**: Sync and async runners have parallel implementations. Adding a feature to one means adding it to both.

### events/

Observability system, decoupled from execution logic.

| File | Purpose |
|------|---------|
| `types.py` | Frozen dataclasses with `span_id`/`parent_span_id` envelope |
| `dispatcher.py` | `EventDispatcher` — routes events to processors |
| `processor.py` | `EventProcessor`, `AsyncEventProcessor`, `TypedEventProcessor` |
| `rich_progress.py` | `RichProgressProcessor` — Rich console progress bars |

**Rule**: Events are best-effort. Observability must never alter execution logic or raise exceptions that break a run.

### viz/

Graph visualization. Generates interactive HTML with ReactFlow.

**Rule**: Viz has its own `CLAUDE.md` and `DEBUGGING.md` inside `src/hypergraph/viz/`. Read those before modifying viz code.

## Public API

Everything exported from `src/hypergraph/__init__.py` with `__all__` is public API. Internal modules use `_` prefix (`_callable.py`, `_rename.py`, `_conflict.py`, `_shared/`).

## Naming Rules

- Reserved characters: `.` and `/` cannot appear in node or output names
- Node names must be valid Python identifiers
- Output names must be valid Python identifiers and not Python keywords
