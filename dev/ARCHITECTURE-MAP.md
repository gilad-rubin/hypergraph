# Architecture Map

A hierarchical mental model of Hypergraph — designed for the maintainer who needs shared vocabulary to discuss changes, not just a file listing.

## What This Project Actually Is

**One framework for the full spectrum of Python workflows** — from batch data pipelines to multi-turn AI agents.

Data pipelines and agentic AI share more than you'd expect. Both are graphs of functions — the difference is whether the graph has cycles. Hypergraph gives you one framework that handles the full spectrum: DAGs, branches, loops, nesting, and everything in between.

The user-facing promise is deliberately small:
- **Unified** — one framework for data pipelines and agentic AI
- **Hierarchical** — graphs nest as nodes; build big from small, tested pieces
- **Minimal** — no state schemas, no boilerplate, just functions
- **Versatile** — sync, async, streaming, branches, loops, human-in-the-loop

The internal machinery that keeps this promise is not small.

**Why the codebase feels broad**: The semantic model itself is compact. But the framework insists that nesting be first-class *everywhere* — execution, checkpointing, debugging, CLI, observability, visualization. Each outer surface must understand enough of the core model to stay faithful to it. That's the source of breadth.

---

## Three Concentric Layers

```
┌──────────────────────────────────────────────────────────────────┐
│                      OUTER SURFACES                              │
│  Durability · Observability · Visualization · CLI                │
│  How execution becomes durable, observable, explorable, usable   │
├──────────────────────────────────────────────────────────────────┤
│                     EXECUTION KERNEL                             │
│  Runners · Scheduling · Supersteps · Staleness · Gate activation │
│  How the semantic model gets executed                            │
├──────────────────────────────────────────────────────────────────┤
│                      SEMANTIC CORE                               │
│  Nodes · Graph · InputSpec · Validation · Edge inference         │
│  What the workflow means                                         │
└──────────────────────────────────────────────────────────────────┘
```

---

## Layer 1: Semantic Core

**What it is**: The conceptual heart. Defines contracts, naming, edge inference, scoping, entrypoints, shared params, and build-time invariants.

**Key files**:
- `nodes/base.py` — `HyperNode` abstract base, immutable `with_*` API
- `nodes/function.py` — `FunctionNode`, `@node` decorator
- `nodes/gate.py` — `RouteNode`, `IfElseNode`, `@route`, `@ifelse`, `END`
- `nodes/graph_node.py` — `GraphNode` — wraps a `Graph` as a node (the hierarchy bridge)
- `nodes/interrupt.py` — `InterruptNode`, `@interrupt` for human-in-the-loop
- `graph/core.py` — `Graph` class (~1500 lines, the build pipeline)
- `graph/input_spec.py` — `InputSpec` — classifies inputs as required/optional/entrypoint
- `graph/validation.py` — all build-time checks (~12 validators)
- `graph/_conflict.py` — duplicate output producer detection

### Node Type Hierarchy

```
HyperNode (base.py)                    — abstract, immutable with_* API
├── FunctionNode (function.py)         — wraps any callable
│   └── InterruptNode (interrupt.py)   — pauses execution for human input
├── GateNode (gate.py)                 — control flow (abstract)
│   ├── RouteNode                      — N-way routing to named targets
│   └── IfElseNode                     — binary True/False routing
└── GraphNode (graph_node.py)          — wraps a Graph as a node (nesting)
    └── .map_over()                    — configures iteration over inputs
```

### Graph Build Pipeline (in `Graph.__init__`)

```
node list
  → normalize into dict
  → _collect_output_sources (which node produces which output)
  → _add_data_edges (output name == input name → auto-wire)
  → _add_control_edges (gate → target relationships)
  → _add_ordering_edges (emit/wait_for → ordering edges)
  → validate everything (12 checks)
  → freeze as NetworkX DiGraph
```

### InputSpec — "What Do I Need To Provide?"

`InputSpec` is a frozen dataclass with three fields: `required`, `optional`, `bound`. It's a derived answer — the graph computes it from the active scope.

The active scope itself is shaped by two Graph-level configurations:

| Configuration | Set by | Effect on active scope |
|---------------|--------|------------------------|
| **entrypoints** | `Graph(entrypoint=...)` / `.with_entrypoint()` | Narrows forward from entry nodes |
| **selected** | `.select()` | Narrows backward from requested outputs |

Within that active scope, each param is categorized:

| Category | Condition |
|----------|-----------|
| **required** | No edge produces it, no default, not bound |
| **optional** | No edge produces it, but has a default or is bound |
| *(excluded)* | Produced by an edge — internal, not a user input |

If a gate is in the active set, ALL its targets and their descendants are included (pessimistic expansion).

---

## Layer 2: Execution Kernel

**What it is**: The runtime engine. Where most hidden complexity lives.

**Key files**:
- `runners/sync/runner.py` + `superstep.py` — sequential execution
- `runners/async_/runner.py` + `superstep.py` — concurrent execution
- `runners/_shared/helpers.py` (~1100 lines) — the runtime constitution
- `runners/_shared/types.py` (~1300 lines) — `GraphState`, `RunResult`, `RunStatus`, `ExecutionContext`
- `runners/_shared/gate_execution.py` — `execute_route()`, `execute_ifelse()`

### The Superstep Model

This is the heartbeat of the framework. Both runners follow the same loop:

```
initialize state with provided inputs
repeat:
    ready_nodes = get_ready_nodes(state, graph, active_nodes)
    if no ready nodes: break
    execute all ready nodes (sync: sequential, async: concurrent)
      → each node's outputs are written to state
      → gate nodes record routing decisions during execution
return RunResult
```

One subtle detail: all nodes in a superstep see the **same input snapshot** — inputs are collected from the original state before any execution starts. A node's output in this superstep doesn't affect sibling nodes' inputs until the next superstep.

Every execution question reduces to: **"which nodes are ready, and why?"**

### Scheduling Deep Dive

`get_ready_nodes` (in `_shared/helpers.py`) runs in three phases every superstep.

**Phase 1 — Who's allowed?** Determine which nodes gates currently permit.

```
_get_activated_nodes(graph, state):
  first: clear stale gate decisions
    (if a gate's inputs changed, its old routing decision is outdated — delete it)
  then for each node:
    ├── no controlling gate? → always allowed
    └── has a controlling gate?
        ├── gate never ran + default_open + node never ran → allowed (first-pass startup)
        ├── gate ran but decision was cleared → blocked (wait for gate to re-run)
        └── gate has active decision → allowed if decision includes this node
```

The `default_open` rule is what lets a graph "start up" — gate targets can fire once before the gate runs. After that first pass, they need an explicit routing decision.

**Phase 2 — Who's ready?** For each allowed node, check five conditions (all must pass):

```
_is_node_ready(node, ...):
  1. activated?          — Phase 1 said this node is gate-allowed
  2. predecessors done?  — all upstream nodes (data + ordering edges) have executed
  3. inputs available?   — every input param has a value (from state, bound, or default)
  4. wait_for satisfied? — ordering signals received (and fresh, on re-execution)
  5. needs execution?    — either never ran, or inputs changed since last run
```

Step 5 is where the subtlety lives. See "Staleness" below.

**Phase 3 — Resolve conflicts.** Two post-filters prevent same-superstep races:

```
1. Gate-first priority:
   If a gate and its target are both ready → remove the target.
   The gate runs first; its routing decision takes effect next superstep.

2. Wait-for ordering:
   If a producer and its wait_for consumer are both ready (first time only)
   → defer the consumer. Producer runs first.
```

### Staleness — "Should This Node Run Again?"

A node that already ran only re-runs if its inputs changed. But not every change counts.

`_is_stale` (in `_shared/helpers.py`) uses **version numbers** — each value in `GraphState` has a version counter that increments when the value **actually changes** (same-value writes are skipped, which matters for cycle convergence). Each node records which versions it consumed. If the current version > consumed version, the input changed.

Two exceptions prevent false re-triggers in cycles:

| Rule | Plain English | Example |
|------|--------------|---------|
| **Don't re-trigger yourself** | If this node *produces* the param it also *consumes*, ignore the version bump. | `accumulate(messages) → messages` — it just wrote `messages`, don't re-run because of its own write. |
| **Ignore downstream writes** | If ALL producers of a param are *downstream* of this node, ignore them. (DAGs only.) | An interrupt node consumes `messages`, a downstream accumulator produces `messages` — the upstream interrupt shouldn't re-run. |

**Both rules are disabled for gate-controlled nodes.** Gates explicitly drive cycle re-execution, so their targets should always respond to version changes.

There's one more nuance: when multiple upstream nodes could produce the same param, `_is_stale` checks which *specific upstream producer* actually wrote the new version (via `_latest_upstream_output_version`). A version bump from a non-wired source doesn't count.

### Gate Lifecycle

Gates don't produce data — they produce *routing decisions* stored in `GraphState.routing_decisions`.

```
1. Gate runs → records decision (e.g., "go to node_B")
2. Target executes → decision is consumed
3. Gate must re-run to re-activate the target
```

Special cases:
- `default_open=True`: targets can fire once before the gate runs (startup)
- `END` sentinel: terminates execution along that branch; never cleared even if inputs change
- Stale decisions: if a gate's inputs changed, its previous decision is deleted before scheduling — prevents acting on outdated routing

### Cycle Handling

- **Seeds**: params both consumed and produced within the same cycle. Marked "optional" in InputSpec — the user provides the initial value.
- Cycles terminate via gate routing to `END`, or via `max_iterations` (raises `InfiniteLoopError`).
- The staleness rules above are what make cycles work — without "don't re-trigger yourself," a cycle node would immediately re-run after its own write, causing infinite loops even with correct gate logic.

### Sync/Async Parity

Both runners implement the same behavior through Template Method pattern:
- `SyncRunnerTemplate` / `AsyncRunnerTemplate` — lifecycle ABCs
- `SyncRunner` / `AsyncRunner` — concrete implementations
- Per-node-type executors: `FunctionNodeExecutor`, `GraphNodeExecutor`, `IfElseNodeExecutor`, `RouteNodeExecutor`, + `InterruptNodeExecutor` (async only)
- Executors follow the `NodeExecutor` / `AsyncNodeExecutor` protocol: `(node, state, inputs, ctx) -> outputs`
- `ExecutionContext` (frozen dataclass) carries per-run context through the executor call chain: event processors, span IDs, workflow ID, provided values, inner_log callback

**Rule**: Adding a feature to one runner means adding it to both.

---

## Layer 3: Outer Surfaces

**What it is**: How execution becomes durable, observable, explorable, and usable. Each surface must faithfully represent the core model, including nested graphs.

### Durability — Checkpointing (`checkpointers/`)

```
Checkpointing
├── Checkpointer ABC (base.py)      — create_run, save_step, load_checkpoint
├── SqliteCheckpointer (sqlite.py)   — async SQLite, ~1000 lines
│   ├── Steps are source of truth (not full state snapshots)
│   ├── Hierarchy: parent_run_id for nested GraphNode runs
│   ├── Forking: fork_superstep + forked_from
│   └── Lineage queries: parent → child run trees
├── Serializers (serializers.py)     — JSON or Pickle
└── CheckpointPolicy                 — durability, retention, TTL
```

Key insight: checkpoints are not just logging. Persistence participates in resume, fork, retry, lineage, and interrupt semantics.

### Observability — Events (`events/`)

```
Events
├── Event Types (types.py)           — frozen dataclasses
│   └── RunStart/End, NodeStart/End/Error, RouteDecision, Interrupt, CacheHit
├── Dispatcher (dispatcher.py)       — fan-out to processor list
├── Processors:
│   ├── RunLogCollector              — always present, builds RunLog for RunResult
│   ├── RichProgressProcessor        — terminal progress bars
│   └── OtelEventProcessor           — OpenTelemetry spans
└── Pattern: emit(event) → dispatcher → each processor.on_X()
```

**Rule**: Events are best-effort. Observability must never alter execution or break a run.

### Visualization — Viz (`viz/`)

```
Visualization
├── Widget (widget.py)               — iframe-based Jupyter/VSCode display
├── Mermaid (mermaid.py)             — text-based flowchart export
├── Debugger (debug.py)              — trace_node, find_issues, validate
├── Renderer Pipeline:
│   ├── Instructions → node/edge visual specs
│   ├── Precompute → all expansion state combinations
│   ├── Scope → which graph outputs are visible per state
│   └── Nodes + Edges → React Flow JSON
├── HTML Generator → assembles full HTML doc with embedded JS
└── Assets → bundled React + ReactFlow + Dagre JS
```

Viz is a **projection** of the core model — Python precomputes a contract (`{nodes, edges, meta}` dict), JavaScript lays it out and renders it.

### CLI (`cli/`)

```
CLI
├── run / map commands           — execute graphs from terminal
├── graph ls / inspect           — show registered graphs and topology
├── runs ls/show/values/steps    — query checkpointed run history
└── Config: reads [tool.hypergraph.graphs] from pyproject.toml
```

---

## Foundational Primitives

These are the building blocks everything else uses:

| Primitive | File | Purpose |
|-----------|------|---------|
| NetworkX DiGraph | (external) | All graph structure, analysis, traversal |
| Type compatibility | `_typing.py` | Recursive type checker for edge validation |
| Edge inference | `graph/core.py` | Output name == input name → auto-wire |
| Rename tracking | `nodes/_rename.py` | Chains of `with_inputs()`/`with_outputs()` renames |
| Cache keys | `cache.py` | SHA256 of function hash + pickled inputs |
| Utilities | `_utils.py` | `ensure_tuple`, formatting helpers |
| HTML rendering | `_repr.py` | `_repr_html_` primitives for Jupyter display |

---

## Where The Complexity Actually Is

The hard parts are not the decorators or the `Graph(...)` API. They are four cross-cutting concerns:

### 1. Scope Math (the "scope engine")
`bind`, `select`, `with_entrypoint`, defaults, and cycles all interact in `graph/input_spec.py`. This is the "what inputs are valid right now?" layer. Changes here ripple into validation, runners, and viz.

### 2. Scheduling Semantics (the "scheduler")
`runners/_shared/helpers.py` is the runtime constitution — ready-node detection, stale input logic, gate activation, wait-for ordering, cycle behavior. If you don't understand `get_ready_nodes()`, you don't understand the engine.

### 3. Hierarchical Composition (the "hierarchy bridge")
`nodes/graph_node.py` makes nested graphs feel like normal nodes. This bridge must work correctly with every other subsystem — scheduling, checkpointing, viz, input mapping, rename propagation.

### 4. Durable History (the "durability layer")
`checkpointers/` treats steps as source of truth. This means persistence participates in resume, fork, retry, lineage, and interrupt semantics — it's not a write-only log.

---

## Conversation Vocabulary

Use these terms to discuss changes precisely:

| Term | Scope |
|------|-------|
| **semantic core** | Nodes, Graph, InputSpec, validation, edge inference |
| **scope engine** | InputSpec computation, active set, entrypoint/select interaction |
| **scheduler** | `get_ready_nodes`, staleness, gate activation, superstep loop |
| **hierarchy bridge** | GraphNode, nested execution, flat graph expansion |
| **durability layer** | Checkpointing, resume, fork, lineage |
| **observability layer** | Events, processors, run log |
| **viz projection** | Renderer pipeline, widget, mermaid, debugger |
| **surface API** | CLI, decorators, `__init__.py` exports |

### Example usage

Instead of "this fix touched eight files," say:

- *"This was a **scheduler** + **durability** change with no **semantic core** change."*
- *"The **scope engine** needed updating because we added a new dimension to InputSpec."*
- *"This is a **viz projection** fix — the core model is correct, the rendering was wrong."*
- *"This touched the **hierarchy bridge** so we need to verify checkpointing and viz still work with nested graphs."*

---

## Detailed Term Glossary

| Term | Meaning |
|------|---------|
| **Superstep** | One tick of the execution loop: find ready → execute → repeat |
| **Staleness** | Should a previously-run node run again? Version-based — plus "don't re-trigger yourself" and "ignore downstream writes" rules |
| **Active scope** | The set of nodes that *will* run (from entrypoints + selected outputs) |
| **Gate** | Control flow node: routes execution to targets, produces no data outputs |
| **Seed** | A param that's both consumed and produced within a cycle — needs initial value |
| **Routing decision** | A gate's output: which target(s) to activate next. Stored in `GraphState.routing_decisions`, consumed after target runs. |
| **default_open** | Gate setting: targets can fire once before the gate runs (first-pass startup) |
| **Version** | Integer counter per value in state. Incremented on every write. Staleness compares current vs consumed versions. |
| **InputSpec** | Frozen dataclass (`required`, `optional`, `bound`) derived from active scope |
| **Flat graph** | Nested GraphNodes expanded into single NX graph with hierarchical IDs |
| **Expansion state** | Viz concept: which nested graphs are currently expanded/collapsed |
| **Rename history** | Chain of `with_inputs()`/`with_outputs()` transformations tracked per node |
| **Edge inference** | Output name == input name → automatic data edge |
| **Shared params** | Params that skip auto-wiring (`Graph(nodes, shared=["param"])`) |

---

## File Map

```
src/hypergraph/
├── __init__.py              ← public API surface
├── _utils.py                ← ensure_tuple, formatting
├── _typing.py               ← type compatibility checker
├── _repr.py                 ← Jupyter HTML rendering primitives
├── exceptions.py            ← all runtime exceptions
├── cache.py                 ← InMemoryCache, DiskCache, cache keys
│
├── nodes/                   [SEMANTIC CORE]
│   ├── base.py              ← HyperNode ABC
│   ├── function.py          ← FunctionNode, @node
│   ├── gate.py              ← RouteNode, IfElseNode, @route, @ifelse, END
│   ├── graph_node.py        ← GraphNode, .map_over()
│   ├── interrupt.py         ← InterruptNode, @interrupt
│   ├── _callable.py         ← callable introspection mixin
│   └── _rename.py           ← rename tracking, batch IDs
│
├── graph/                   [SEMANTIC CORE]
│   ├── core.py              ← Graph class (build pipeline, mutations)
│   ├── input_spec.py        ← InputSpec, active scope computation
│   ├── validation.py        ← build-time validators
│   ├── _conflict.py         ← output conflict detection
│   └── _helpers.py          ← edge/source analysis helpers
│
├── runners/                 [EXECUTION KERNEL]
│   ├── base.py              ← BaseRunner interface
│   ├── _shared/
│   │   ├── helpers.py       ← THE scheduler, staleness, ready nodes
│   │   ├── types.py         ← GraphState, RunResult, RunStatus
│   │   ├── template_sync.py ← SyncRunner lifecycle template
│   │   ├── template_async.py← AsyncRunner lifecycle template
│   │   ├── validation.py    ← runtime input/runner validation
│   │   ├── input_normalization.py
│   │   ├── gate_execution.py← execute_route, execute_ifelse
│   │   ├── caching.py       ← cache check/store for supersteps
│   │   ├── event_helpers.py ← event construction helpers
│   │   ├── routing_validation.py
│   │   ├── checkpoint_helpers.py
│   │   ├── run_log.py       ← RunLogCollector processor
│   │   └── protocols.py     ← NodeExecutor protocol
│   ├── sync/
│   │   ├── runner.py        ← SyncRunner
│   │   ├── superstep.py     ← superstep loop (sequential)
│   │   └── executors/       ← function, graph, ifelse, route
│   └── async_/
│       ├── runner.py        ← AsyncRunner (concurrent, max_concurrency)
│       ├── superstep.py     ← superstep loop (asyncio.gather)
│       └── executors/       ← function, graph, ifelse, route, interrupt
│
├── events/                  [OBSERVABILITY]
│   ├── types.py             ← frozen event dataclasses
│   ├── dispatcher.py        ← fan-out to processors
│   ├── processor.py         ← EventProcessor, TypedEventProcessor
│   ├── rich_progress.py     ← terminal progress display
│   └── otel.py              ← OpenTelemetry integration
│
├── checkpointers/           [DURABILITY]
│   ├── base.py              ← Checkpointer ABC, CheckpointPolicy
│   ├── types.py             ← StepRecord, Run, Checkpoint, Lineage
│   ├── sqlite.py            ← SqliteCheckpointer (async SQLite)
│   ├── serializers.py       ← JSON, Pickle serializers
│   ├── protocols.py         ← sync checkpointer protocol
│   └── _migrate.py          ← schema migrations
│
├── viz/                     [VISUALIZATION]
│   ├── widget.py            ← ScrollablePipelineWidget (iframe)
│   ├── mermaid.py           ← Mermaid flowchart export
│   ├── debug.py             ← VizDebugger
│   ├── _common.py           ← shared viz utilities
│   ├── geometry.py          ← layout geometry helpers
│   ├── styles/nodes.py      ← node visual styles
│   ├── renderer/
│   │   ├── __init__.py      ← render_graph() entry point
│   │   ├── instructions.py  ← VizInstructions data contract
│   │   ├── nodes.py         ← node rendering
│   │   ├── edges.py         ← edge rendering
│   │   ├── precompute.py    ← expansion state precomputation
│   │   ├── scope.py         ← output visibility per state
│   │   └── _format.py       ← label formatting
│   ├── html/
│   │   ├── generator.py     ← HTML document assembly
│   │   └── estimator.py     ← iframe dimension estimation
│   └── assets/              ← bundled JS (React, ReactFlow, Dagre)
│
└── cli/                     [SURFACE API]
    ├── __init__.py           ← app entry point
    ├── run_cmd.py            ← run/map commands
    ├── graph_cmd.py          ← graph ls/inspect
    ├── runs.py               ← run history queries
    ├── _config.py            ← pyproject.toml graph registry
    ├── _db.py                ← database path helper
    └── _format.py            ← output formatting
```

---

## Approaches For Reclaiming Understanding

### 1. Trace a run (recommended starting point)
Take a 3-node graph (A→B→C) and trace the full execution path through actual code. Which functions get called, in what order, with what data? Then do the same for: a cycle, a gate, and a nested graph. Four traces cover ~90% of the engine.

### 2. Zone-by-zone walkthroughs
Pick one zone (e.g., "scheduler"), walk through the actual code with concrete examples — "here's a graph, here's what `get_ready_nodes` returns at each superstep."

### 3. "Break it" experiments
For each zone, deliberately introduce a bug and predict what test fails and why. Forces understanding of causality, not just structure.

### 4. Visual state notebooks
For complex zones (staleness, gate activation, active scope), build notebooks that show state at each superstep — not code, but data flowing through it.
