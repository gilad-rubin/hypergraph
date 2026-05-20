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

**Rule**: Node objects are immutable values. Rename/configuration methods return new instances. Never mutate a node in place.

### graph/

Graph construction and build-time validation.

| File | Purpose |
|------|---------|
| `core.py` | `Graph` class — the build pipeline, `bind`/`select`/`unbind`/`with_entrypoint` |
| `input_spec.py` | `InputSpec` — classifies inputs as required/optional/entrypoint. Also computes active subgraph scope from entrypoints and selection. |
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

**InputSpec computation** depends on three dimensions configured via immutable copy methods:
- `_bound` (from `bind()`) — which params have pre-filled values
- `_selected` (from `select()`) — which outputs are requested (narrows active set backward from outputs)
- `_entrypoints` (from `with_entrypoint()`) — where execution starts (narrows active set forward from entry nodes)

All three are cleared in `_shallow_copy` → `inputs` cache invalidation.

**Rule**: All structural errors must be caught at `Graph()` construction time, not during execution.

### runners/

Execution engines. Template Method pattern with pluggable executors per node type.

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

**Execution model**: SCC-based.

- Build a static execution plan by collapsing the active graph into strongly connected components (SCCs)
- Topologically order the SCC DAG
- Execute one topo layer at a time
- Within each layer, run local supersteps until the layer reaches quiescence
- Gates are dynamic activation, not static startup dependencies

Practical mental model:
- **DAGs**: execute in topo order
- **Cycles**: execute as one local fixed-point region
- **Gates**: decide which nodes inside the current region are activated

**`_shared/` contents**:
- `cache_observer.py` — Bridge nested hypercache telemetry into Hypergraph events during node execution
- `caching.py` — Cache key computation and lookup
- `checkpoint_helpers.py` — Build persisted `StepRecord`s from runtime state
- `event_helpers.py` — Emit lifecycle events
- `gate_execution.py` — Route/ifelse decision execution
- `helpers.py` — Input resolution, output storage, active-scope computation, SCC planning, and localized readiness scheduling
- `input_normalization.py` — Normalize user inputs for execution
- `protocols.py` — executor protocols for sync and async runners
- `run_log.py` — always-on `RunLog` collection helpers
- `template_sync.py` / `template_async.py` — Template Method base for runner lifecycle. Threads runtime select, entrypoint config, and checkpoint semantics into validation and execution.
- `types.py` — `GraphState`, `RunResult`, `RunStatus`, `PauseInfo`, `RunLog`, `MapLog`, `ExecutionContext`
- `validation.py` — Runner-level validation, runtime select resolution, InputSpec scoping

**Rule**: Sync and async runners have parallel implementations. Adding a feature to one means adding it to both.

### checkpointers/

Durability, lineage, and inspection for persisted runs.

| File | Purpose |
|------|---------|
| `base.py` | `Checkpointer` ABC and `CheckpointPolicy` |
| `sqlite.py` | Durable SQLite-backed checkpointer |
| `memory.py` | In-memory checkpointer for tests and lightweight experiments |
| `types.py` | `Run`, `StepRecord`, `Checkpoint`, lineage and table display types |
| `inspection.py` | Sync inspection adapters used by CLI and notebooks |
| `presenters.py` | HTML renderers for explorer/table-style checkpoint widgets |
| `protocols.py` | Sync write protocol for `SyncRunner` |
| `serializers.py` | Payload serializers |
| `_migrate.py` | SQLite schema migrations |

**Rule**: Checkpointing is not just persistence. It participates in resume, fork, retry, lineage, CLI inspection, and notebook UX.

### events/

Observability system, decoupled from execution logic.

| File | Purpose |
|------|---------|
| `types.py` | Frozen dataclasses with `span_id`/`parent_span_id` envelope |
| `dispatcher.py` | `EventDispatcher` — routes events to processors |
| `processor.py` | `EventProcessor`, `AsyncEventProcessor`, `TypedEventProcessor` |
| `rich_progress.py` | `RichProgressProcessor` — Rich console progress bars |
| `otel.py` | OpenTelemetry processor integration |

**Rule**: Events are best-effort. Observability must never alter execution logic or raise exceptions that break a run.

Public event contracts have mirror obligations:
- update `hypergraph.events` and package-root exports when the event is part of the public API
- update processor handler docs/examples when a new `TypedEventProcessor` callback is added
- add constructor + typed-dispatch coverage in `tests/events/test_types.py`

### integrations/

Optional alternate runtimes that project the core graph model into another execution backend.

| File | Purpose |
|------|---------|
| `daft/runner.py` | `DaftRunner` for DataFrame-style DAG execution |

**Rule**: Integrations may intentionally support only a subset of Hypergraph semantics, but those constraints must be explicit and validated.

### viz/

Graph visualization. Generates interactive HTML with ReactFlow.

**Rule**: Viz has its own `CLAUDE.md` and `DEBUGGING.md` inside `src/hypergraph/viz/`. Read those before modifying viz code.

## Public API

Root-level exports from `src/hypergraph/__init__.py` are public API. Package modules with their own `__init__.py` exports, such as `hypergraph.checkpointers`, are also supported package surfaces.

Internal modules remain internal even if they sit under a public package. In particular, `_shared/` is runtime architecture, not public API.

## Framework Context Injection

When the framework needs to provide a runtime object to user functions (e.g., `NodeContext` for stop signals and streaming), we use **type-hint-based injection** — the same pattern FastAPI uses for `Request`, `BackgroundTasks`, etc.

### How It Works

The user adds a typed parameter to their function signature. The framework's existing signature inspection (which already extracts input names for edge inference) recognizes the type and injects the object at execution time instead of treating it as a graph input.

```python
@node(output_name="response")
async def llm_reply(messages: list, ctx: NodeContext) -> str:
    if ctx.stop_requested:
        break
    ctx.stream(chunk)
    return response
```

- `messages` → graph input (wired via edge inference)
- `ctx` → framework-injected (excluded from `node.inputs`, never appears in graph)

### Why This Pattern

| Pattern | Used By | Visible in Signature? | Testable? |
|---|---|---|---|
| **Type-hint injection** | FastAPI, DI libraries, hypergraph | Yes | Yes — pass mock directly |
| ContextVar accessor | Prefect, Temporal, LangGraph | No — hidden in body | No — needs contextvar setup |
| Explicit positional | Django, Celery | Yes — always there | Yes |

Type-hint injection was chosen because:

1. **Dependency is visible** — you see `ctx: NodeContext` in the signature. ContextVar accessors (`get_node_context()`) hide the dependency inside the function body.
2. **Testing is plain Python** — `llm_reply(messages=["hi"], ctx=mock_context)`. No framework setup needed.
3. **Consistent with existing mechanism** — signature inspection for edge inference already exists. Recognizing `NodeContext` as "framework-provided" is one additional case in the same codepath.
4. **No opt-in boilerplate** — no decorator flag (`context=True`), no import + function call inside the body.

### Constraints

- Only **one** framework-injectable type (`NodeContext`). If we ever need more, revisit the pattern — a growing list of magic types is a smell.
- The parameter name doesn't matter (`ctx`, `context`, `nc` — all work). Only the **type annotation** matters.
- Functions without `NodeContext` work exactly as before. Backward compatible.

## Naming Rules

- Reserved characters: `.` and `/` cannot appear in node or output names
- Node names must be valid Python identifiers
- Output names must be valid Python identifiers and not Python keywords
