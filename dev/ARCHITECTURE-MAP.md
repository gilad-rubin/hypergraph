# Architecture Map

A maintainer-oriented mental model of the current Hypergraph codebase.

This file is not an exhaustive API reference. It exists to answer higher-level questions:

- Where does a behavior live?
- Which subsystem owns a concern?
- What changes are architectural versus local implementation detail?
- Which surfaces must stay aligned when a core abstraction changes?

## What The Project Is Now

Hypergraph is one framework for Python workflows across DAGs, branches, loops, nested graphs, map-style batch execution, human-in-the-loop interrupts, durable lineage, and multiple inspection surfaces.

The user-facing API is intentionally small:

- Define nodes as plain Python functions
- Build graphs from nodes
- Run them with sync or async runners
- Compose by nesting graphs as nodes

The internal system is broader because Hypergraph treats nested graphs as first-class across:

- execution
- checkpointing
- lineage
- CLI inspection
- observability
- visualization
- notebook/HTML presentation

That is the main source of architectural breadth.

## Five Working Zones

```text
semantic core
  nodes + graph construction + input scoping + validation

execution kernel
  runners + scheduling + supersteps + staleness + gate activation

durability and inspection
  checkpointers + lineage + snapshots + CLI/query adapters + HTML presenters

observability and UX surfaces
  events + run logs + widgets + viz + CLI output

optional integrations
  runner implementations that project the core model into another runtime
```

These zones are coupled in one direction:

```text
nodes -> graph -> runners -> events
                    |
                    +-> checkpointers
                    +-> viz
                    +-> cli
                    +-> integrations
```

The key architectural rule is still: the semantic model stays small, while outer surfaces adapt to it without redefining it.

## Zone 1: Semantic Core

This is the "what the workflow means" layer.

### Nodes

Key files:

- `src/hypergraph/nodes/base.py`
- `src/hypergraph/nodes/function.py`
- `src/hypergraph/nodes/gate.py`
- `src/hypergraph/nodes/graph_node.py`
- `src/hypergraph/nodes/interrupt.py`
- `src/hypergraph/nodes/_callable.py`
- `src/hypergraph/nodes/_rename.py`

Current node hierarchy:

```text
HyperNode
├── FunctionNode
│   └── InterruptNode
├── GateNode
│   ├── RouteNode
│   └── IfElseNode
└── GraphNode
    ├── map_over(...)
    └── with_runner(...)
```

Key ideas:

- Nodes are immutable value objects.
- `FunctionNode` keeps functions testable outside the framework.
- `GateNode` owns control-flow decisions, not heavy computation.
- `InterruptNode` is a specialized function node for pause/resume semantics.
- `GraphNode` is the hierarchy bridge.

Important recent reality:

- `GraphNode.with_runner()` and `Graph.as_node(runner=...)` let nested graphs delegate execution to a different runner.
- `GraphNode.map_over()` is now part of the core composition story, not a fringe add-on.

### Graph Construction

Key files:

- `src/hypergraph/graph/core.py`
- `src/hypergraph/graph/input_spec.py`
- `src/hypergraph/graph/validation.py`
- `src/hypergraph/graph/_helpers.py`
- `src/hypergraph/graph/_conflict.py`

The build pipeline in `Graph` still does the same high-level job:

1. normalize nodes
2. resolve output producers
3. infer data edges from matching names
4. infer control edges from gates
5. infer ordering edges from `emit` / `wait_for`
6. validate structure

The important modern nuance is scope computation.

`InputSpec` and active-scope logic are now shaped by multiple graph-level dimensions:

- `bind()`
- `select()`
- `with_entrypoint()`
- `shared=...`

That means the graph layer owns more than "validation". It also owns the definition of which subgraph is active for both validation and execution.

### Shared Vocabulary For The Core

- **active scope**: the nodes and outputs still relevant after entrypoint and select slicing
- **shared params**: parameters intentionally excluded from auto-wiring
- **seed**: a value needed to start a cycle
- **rename history**: the mapping chain created by `with_inputs()` / `with_outputs()`
- **runner delegation**: using a specific runner for a nested graph node

## Zone 2: Execution Kernel

This is where most hidden complexity lives.

Key files:

- `src/hypergraph/runners/base.py`
- `src/hypergraph/runners/sync/runner.py`
- `src/hypergraph/runners/sync/superstep.py`
- `src/hypergraph/runners/async_/runner.py`
- `src/hypergraph/runners/async_/superstep.py`
- `src/hypergraph/runners/_shared/helpers.py`
- `src/hypergraph/runners/_shared/types.py`
- `src/hypergraph/runners/_shared/protocols.py`
- `src/hypergraph/runners/_shared/template_sync.py`
- `src/hypergraph/runners/_shared/template_async.py`
- `src/hypergraph/runners/_shared/checkpoint_helpers.py`
- `src/hypergraph/runners/_shared/validation.py`
- `src/hypergraph/runners/_shared/input_normalization.py`
- `src/hypergraph/runners/_shared/run_log.py`

### Execution Model

The runner is no longer best understood as "simple supersteps over ready nodes".

The current model is:

```text
Graph
  -> compute execution scope
  -> collapse active graph into SCC execution components
  -> build an ExecutionFrontier
  -> repeatedly ask the frontier for the next ready batch
  -> execute one local superstep for that batch
  -> record outputs, logs, routing decisions, checkpoints, and status
```

Important types:

- `ExecutionScope`: precomputed active subgraph + component graph
- `ExecutionComponent`: one SCC or DAG fragment in execution order
- `ExecutionFrontier`: runtime scheduler state for SCC-level progress
- `ExecutionContext`: per-node execution environment threaded into executors
- `GraphState`: runtime mutable state
- `RunResult` / `MapResult`: execution outputs
- `RunLog` / `MapLog` / `NodeRecord`: always-on trace surfaces

### The Scheduler

The scheduler logic lives primarily in `runners/_shared/helpers.py`.

Core responsibilities:

- active node filtering
- stale gate-decision clearing
- readiness detection
- staleness checks
- explicit-edge-aware producer tracking
- SCC execution planning
- checkpoint state restoration
- input resolution precedence

When discussing bugs in this area, "scheduler" usually means:

- `get_ready_nodes`
- `_needs_execution`
- `_is_stale`
- gate activation / routing consumption
- SCC planning and frontier advancement

### Sync / Async Structure

The sync and async runners remain parallel implementations built around template base classes.

Current split of responsibilities:

- `template_sync.py` / `template_async.py`
  - public `run()` / `map()` lifecycle
  - input normalization and runtime validation
  - resume/fork/retry semantics
  - top-level dispatcher and run-status handling
  - batch persistence behavior
- concrete runner modules
  - scheduler loop
  - frontier stepping
  - superstep orchestration
- executor modules
  - node-type-specific execution

### Executors

Executor directories:

- `src/hypergraph/runners/sync/executors/`
- `src/hypergraph/runners/async_/executors/`

Current important architectural fact:

- Executors now consume an `ExecutionContext` rather than ad hoc side-channels.

That context is the transport for:

- parent span IDs
- event processors
- workflow IDs
- resume payload visibility
- nested inner-log propagation

This is now part of the core execution design, not just a local implementation detail.

### Nested Graph Execution

Nested execution is where the execution kernel touches almost every other subsystem.

`GraphNode` execution has to preserve:

- renamed inputs and outputs
- map-over behavior
- delegated runner behavior
- nested checkpoint workflow IDs
- pause propagation
- nested run-log propagation

That makes `graph_node.py` executors one of the highest-risk change zones in the repo.

## Zone 3: Durability And Inspection

This zone grew substantially and deserves to be treated as a first-class subsystem.

Key files:

- `src/hypergraph/checkpointers/base.py`
- `src/hypergraph/checkpointers/sqlite.py`
- `src/hypergraph/checkpointers/memory.py`
- `src/hypergraph/checkpointers/types.py`
- `src/hypergraph/checkpointers/inspection.py`
- `src/hypergraph/checkpointers/presenters.py`
- `src/hypergraph/checkpointers/protocols.py`
- `src/hypergraph/checkpointers/serializers.py`
- `src/hypergraph/checkpointers/_migrate.py`

### What This Subsystem Owns

Not just persistence.

It owns:

- run lifecycle records
- step records
- status transitions
- checkpoint snapshots
- fork / retry lineage
- sync inspection adapters
- HTML / notebook explorers for persisted runs

### Core Model

Important types in `checkpointers/types.py`:

- `Run`
- `StepRecord`
- `Checkpoint`
- `WorkflowStatus`
- `RunTable`
- `StepTable`
- `LineageRow`
- `LineageView`

Architecturally, the important point is:

- steps are the durable source of truth
- checkpoints are derived snapshots for restore/fork flows
- lineage is explicit and queryable

### Two Usage Modes

1. **runtime durability**
   - runners call checkpointer methods during execution
2. **inspection**
   - CLI and notebooks query persisted runs without participating in execution

That split is why `inspection.py` exists. It keeps inspection consumers from reaching directly into backend-specific helper methods.

### Backends And Helpers

- `SqliteCheckpointer`: durable, async write path, sync read helpers, lineage queries, migration support
- `MemoryCheckpointer`: test/lightweight backend with retention policy behavior
- `SyncCheckpointerProtocol`: lets `SyncRunner` demand sync-write support
- serializers: JSON vs Pickle payload strategies
- presenters: rich HTML renderers for explorer-like notebook surfaces

This subsystem is now broad enough that "checkpointer change" can mean any of:

- runtime resume semantics
- persistence format
- inspection UX
- lineage behavior
- notebook rendering

Those are related, but not interchangeable.

## Zone 4: Observability And UX Surfaces

This zone contains multiple outward-facing surfaces that all reflect execution.

### Events

Key files:

- `src/hypergraph/events/types.py`
- `src/hypergraph/events/dispatcher.py`
- `src/hypergraph/events/processor.py`
- `src/hypergraph/events/rich_progress.py`
- `src/hypergraph/events/otel.py`

Responsibilities:

- event envelopes and event types
- processor contracts
- dispatch fan-out
- terminal progress
- OpenTelemetry integration

The key rule remains:

- events are observational, not control-flow.

### Run Logs

Run logs live in runner shared types, but conceptually they are an observability surface.

Important exported types:

- `RunLog`
- `MapLog`
- `NodeRecord`
- `NodeStats`

These are not optional debugging extras anymore. They are part of the user-facing execution contract.

### Visualization

Key files:

- `src/hypergraph/viz/widget.py`
- `src/hypergraph/viz/mermaid.py`
- `src/hypergraph/viz/debug.py`
- `src/hypergraph/viz/renderer/`
- `src/hypergraph/viz/html/`
- `src/hypergraph/viz/styles/`
- `src/hypergraph/viz/assets/`

Current mental model:

```text
Graph
  -> to_flat_graph()
  -> renderer/ (nodes, edges, precompute, scope, instructions)
  -> html/generator.py
  -> notebook iframe widget or saved HTML file
```

The viz subsystem also has its own debugging surface:

- `VizDebugger`
- `validate_graph`
- `find_issues`
- debug overlays

This is not just "draw the graph". It is a projection pipeline plus diagnostics.

### CLI

Key files:

- `src/hypergraph/cli/run_cmd.py`
- `src/hypergraph/cli/graph_cmd.py`
- `src/hypergraph/cli/runs.py`
- `src/hypergraph/cli/_config.py`
- `src/hypergraph/cli/_db.py`
- `src/hypergraph/cli/_format.py`

Current CLI responsibilities:

- execute graphs from the terminal
- discover configured graphs
- inspect topology
- inspect persisted runs
- inspect checkpoints
- query lineage and search surfaces indirectly through the run inspector

`cli/runs.py` is now a serious inspection surface, not a thin debug helper.

## Zone 5: Optional Integrations

Key files:

- `src/hypergraph/integrations/__init__.py`
- `src/hypergraph/integrations/daft/runner.py`

This subsystem is intentionally small right now, but it is architecturally important.

`DaftRunner` demonstrates a new pattern:

- keep the semantic core the same
- project execution into a different runtime
- constrain compatibility explicitly when the external runtime cannot express all Hypergraph semantics

Current Daft constraints:

- DAG only
- `FunctionNode` only
- one output per node
- no interrupts
- no async nodes
- no checkpointing
- no nested `GraphNode` support yet

This makes integrations a separate architectural zone from the main runners.

## Public API Surface

The package exports now span more than the original core abstractions.

High-level public families in `src/hypergraph/__init__.py`:

- node decorators and node classes
- graph types
- runners and run-log types
- events and processors
- caches
- checkpointer types

Notably, the public surface now includes:

- `MapLog`
- `NodeRecord`
- `NodeStats`
- `CheckpointPolicy`
- `SqliteCheckpointer`

That means "architecture-only" changes in these areas often leak into user expectations quickly.

## File Map

```text
src/hypergraph/
├── __init__.py                        public API surface
├── _repr.py                          notebook/HTML rendering primitives
├── _typing.py                        type-compatibility helpers
├── _utils.py                         formatting and utility helpers
├── cache.py                          cache backends and cache contract
├── exceptions.py                     shared exception types
│
├── nodes/                            semantic core
│   ├── base.py
│   ├── function.py
│   ├── gate.py
│   ├── graph_node.py
│   ├── interrupt.py
│   ├── _callable.py
│   └── _rename.py
│
├── graph/                            semantic core
│   ├── core.py
│   ├── input_spec.py
│   ├── validation.py
│   ├── _helpers.py
│   └── _conflict.py
│
├── runners/                          execution kernel
│   ├── base.py
│   ├── _shared/
│   │   ├── helpers.py
│   │   ├── types.py
│   │   ├── protocols.py
│   │   ├── template_sync.py
│   │   ├── template_async.py
│   │   ├── validation.py
│   │   ├── input_normalization.py
│   │   ├── checkpoint_helpers.py
│   │   ├── caching.py
│   │   ├── event_helpers.py
│   │   └── run_log.py
│   ├── sync/
│   │   ├── runner.py
│   │   ├── superstep.py
│   │   └── executors/
│   └── async_/
│       ├── runner.py
│       ├── superstep.py
│       └── executors/
│
├── checkpointers/                    durability and inspection
│   ├── base.py
│   ├── sqlite.py
│   ├── memory.py
│   ├── inspection.py
│   ├── presenters.py
│   ├── protocols.py
│   ├── serializers.py
│   ├── types.py
│   └── _migrate.py
│
├── events/                           observability
│   ├── dispatcher.py
│   ├── processor.py
│   ├── types.py
│   ├── rich_progress.py
│   └── otel.py
│
├── viz/                              visualization
│   ├── widget.py
│   ├── mermaid.py
│   ├── debug.py
│   ├── geometry.py
│   ├── _common.py
│   ├── html/
│   ├── renderer/
│   ├── styles/
│   └── assets/
│
├── cli/                              terminal surface
│   ├── run_cmd.py
│   ├── graph_cmd.py
│   ├── runs.py
│   ├── _config.py
│   ├── _db.py
│   └── _format.py
│
└── integrations/                     optional runtimes
    ├── __init__.py
    └── daft/
        └── runner.py
```

## Where Changes Tend To Fan Out

These are the change zones with the highest blast radius:

### 1. `graph/input_spec.py`

Touches:

- graph validation
- runtime validation
- runner input handling
- docs and examples

### 2. `runners/_shared/helpers.py`

Touches:

- readiness
- staleness
- SCC planning
- checkpoint restore behavior
- gate behavior

### 3. `nodes/graph_node.py` plus graph-node executors

Touches:

- nesting
- renames
- checkpointing
- map-over
- delegated runners
- pause propagation

### 4. `template_sync.py` / `template_async.py`

Touches:

- public run semantics
- resume/fork/retry behavior
- top-level validation
- batch persistence

### 5. `checkpointers/types.py` and `presenters.py`

Touches:

- CLI output expectations
- notebook rendering
- human inspection workflows
- serialized display assumptions in docs/tests

## Conversation Vocabulary

Use these terms when discussing changes:

- **semantic core**: node and graph meaning
- **scope engine**: active-scope and InputSpec computation
- **execution kernel**: runners, supersteps, scheduler, frontier
- **scheduler**: readiness, staleness, activation, SCC progression
- **hierarchy bridge**: `GraphNode` and nested execution behavior
- **durability layer**: checkpointers, snapshots, lineage, persistence semantics
- **inspection surface**: CLI and notebook querying of persisted runs
- **observability layer**: events and run logs
- **viz projection**: flat graph + render pipeline + HTML/widget output
- **integration runner**: alternate runtime like Daft that projects the core model elsewhere

## Quick Re-entry Guide

If you need to rebuild context quickly, use this order:

1. `dev/CORE-BELIEFS.md`
2. `dev/ARCHITECTURE.md`
3. `src/hypergraph/graph/core.py`
4. `src/hypergraph/runners/_shared/helpers.py`
5. `src/hypergraph/runners/_shared/template_sync.py`
6. `src/hypergraph/runners/_shared/template_async.py`
7. the specific surface you are touching:
   - checkpointers
   - CLI
   - viz
   - integrations

That path gets you from meaning to execution to surface behavior in the right order.
