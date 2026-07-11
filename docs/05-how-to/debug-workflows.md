# Debug Workflows

Two tools for understanding graph execution, from quick in-process inspection to cross-process persistence.

| Tool | When to Use | Setup | Scope |
|------|-------------|-------|-------|
| **RunLog** | "What happened in this run?" | Zero — always on | In-process, current run |
| **Checkpointer** | "What happened yesterday?" | Pass to runner | Cross-process, persisted |

## RunLog — Always-On Run Trace

Every `runner.run()` and `runner.map()` call returns a `RunResult` with a `.log` attribute — an always-on run trace that requires zero configuration.

### Quick Start

```python
from hypergraph import Graph, SyncRunner, node

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3

graph = Graph([double, triple], name="graph")
runner = SyncRunner()
result = runner.run(graph, {"x": 5})

# RunLog is always available
print(result.log)
```

Output:

```
RunLog: graph | 4ms | 2 nodes | 0 errors

  Step            Node              Duration          Status
────────────────  ────────────────  ────────────────  ────────────────
     0            double            3ms               completed
     1            triple            0ms               completed
```

### Finding Slow Nodes

```python
# result.log.steps is a tuple[NodeRecord, ...] in execution order
# Sort by duration (slowest first)
for record in sorted(result.log.steps, key=lambda r: r.duration_ms, reverse=True):
    print(f"{record.node_name}: {record.duration_ms:.1f}ms")

# Per-node aggregates (useful for map operations)
for name, stats in result.log.node_stats.items():
    print(f"{name}: avg={stats.avg_ms:.1f}ms, count={stats.count}")
```

`result.log.steps` yields `NodeRecord` — one per node execution, with `node_name`, `superstep`, `duration_ms`, `status`, `decision`, `error`. `result.log.node_stats` maps node name to `NodeStats` — aggregate `avg_ms`/`count` across all executions of that node in the run. `result.log.errors` is the subset of `.steps` where `status == "failed"`.

### Finding Errors

```python
result = runner.run(graph, {"x": 5}, error_handling="continue")

# All errors
for record in result.log.errors:
    print(f"{record.node_name}: {record.error}")

# Summary
print(result.log.summary())
# "2 nodes | 21ms | 0 errors | slowest: double (16ms)"
```

### Routing Decisions

```python
# Which path did each gate take?
for record in result.log.steps:
    if record.decision:
        print(f"{record.node_name} → {record.decision}")
```

### Serialization

```python
# Export for logging, dashboards, etc.
log_dict = result.log.to_dict()
# {"graph_name": "...", "run_id": "...", "total_duration_ms": 42.5,
#  "steps": [...], "node_stats": {...}}
```

### RunLog with map()

`runner.map()` returns a `MapResult` with a batch-level summary and per-item RunLogs:

```python
results = runner.map(graph, {"x": [1, 2, 3]}, map_over="x")

# Batch-level overview
print(results.summary())  # "3 items | 3 completed | 12ms"

# Per-item RunLogs
for i, r in enumerate(results):
    print(f"Item {i}: {r.log.summary()}")

# Failed items
if results.failed:
    for f in results.failures:
        print(f"Failed: {f.error}")
```

`results.log` also returns a single batch-level `MapLog` (`graph_name`, `total_duration_ms`, `items` — a tuple of the per-item `RunLog`s, plus an aggregate `.errors`), for when you want one object instead of iterating `results` yourself.

### runner.map() vs map_over for debugging

The batch pattern you choose affects what debugging data is available:

| | `runner.map()` | `map_over` |
|---|---|---|
| **RunLog granularity** | Per-item RunLogs with full traces | One RunLog (batch = one step) |
| **Error isolation** | Each item independent | One failure affects entire step |
| **Checkpointing** | Parent + per-item child runs | Persisted as one run step |
| **Checkpointer drill-down** | `cp.runs(parent_run_id="<batch-id>")` | `cp.values("<id>")` |

Both patterns now support checkpointing. `runner.map()` creates a hierarchical structure (parent batch run + child runs per item), while `map_over` records the batch as a single step within the parent run.

## Checkpointer — Persistent Run History

The Checkpointer persists every step to a database, enabling cross-process inspection, crash recovery, and time-travel debugging.

### Quick Start

```bash
pip install 'hypergraph[checkpoint]'
```

```python
from hypergraph import AsyncRunner, Graph, node
from hypergraph.checkpointers import SqliteCheckpointer

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3

graph = Graph([double, triple])

# Create a checkpointer (SQLite database)
checkpointer = SqliteCheckpointer("./runs.db")
runner = AsyncRunner(checkpointer=checkpointer)

# Run with a workflow_id to enable persistence
result = await runner.run(graph, {"x": 5}, workflow_id="my-run-1")
```

Every step is now persisted. You can inspect it from another process or another day.

Both `SyncRunner` and `AsyncRunner` support checkpointing:

```python
from hypergraph import SyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

cp = SqliteCheckpointer("./runs.db")
runner = SyncRunner(checkpointer=cp)
result = runner.run(graph, {"x": 5}, workflow_id="my-run-1")
```

`SyncRunner` writes steps synchronously via the `SyncCheckpointerProtocol`. `SqliteCheckpointer` implements this out of the box.

### Inspecting Persisted State

```python
# From any process that can access the DB file:
from hypergraph.checkpointers import SqliteCheckpointer, WorkflowStatus

cp = SqliteCheckpointer("./runs.db")

# Sync reads — no await needed, works from any context
cp.runs()                             # List all runs
cp.get_run("my-run-1")                # Run metadata (status, duration, counts)
cp.values("my-run-1")                 # {"doubled": 10, "tripled": 30}
cp.steps("my-run-1")                  # Step records with timing
cp.stats("my-run-1")                  # Per-node duration/frequency breakdown
cp.checkpoint("my-run-1")             # Full snapshot (values + steps)

# Filter by status, graph, or time
cp.runs(status=WorkflowStatus.FAILED)
cp.runs(graph_name="my_graph", since=datetime(2024, 1, 1))

# Full-text search across step records
cp.search("generate")                 # Match node names and errors

# Time travel: state at a specific superstep
cp.state("my-run-1", superstep=1)     # {"doubled": 10}

# Git-like fork visualization (lanes + expandable step traces)
cp.lineage("my-run-1")
```

The sync read methods (`runs()`, `get_run()`, `values()`, `steps()`, `search()`, `stats()`, `checkpoint()`) work without async/await, making them ideal for debugging scripts and notebooks. No `initialize()` call needed.

#### Interrupt Steps

For workflows with `@interrupt` nodes, the step log shows the pause/resume cycle. Each interrupt appears twice: first as `paused` (waiting for input), then as `completed` (resolved with the provided value):

```python
steps = cp.steps("my-chat")
for s in steps:
    print(f"  ss={s.superstep}  {s.node_name:20s}  {s.status}")

#   ss=0   add_user_message      completed
#   ss=1   llm_reply             completed
#   ss=2   add_response          completed
#   ss=3   should_continue       completed    (decision: wait_for_user)
#   ss=4   wait_for_user         paused
#   ss=5   wait_for_user         completed    (values: {user_input: "..."})
```

Routing decisions are stored on the step record (`s.decision`), and resolved interrupt values appear in `s.values`. See [Human-in-the-Loop](../03-patterns/07-human-in-the-loop.md#inspecting-checkpoint-history) for the full pattern.

For branching semantics, use explicit checkpoints:
`checkpoint = cp.checkpoint("workflow-id", superstep=...)` and pass it to
`runner.run(..., checkpoint=checkpoint, workflow_id="new-id")`.

### Durability Modes

The `CheckpointPolicy` controls when steps are written to the database:

```python
from hypergraph.checkpointers import CheckpointPolicy, SqliteCheckpointer

# Default: async — steps saved in background tasks (fastest)
cp = SqliteCheckpointer("./runs.db")

# Sync: steps saved immediately after each superstep (safest)
cp = SqliteCheckpointer(
    "./runs.db",
    policy=CheckpointPolicy(durability="sync"),
)

# Exit: steps buffered, flushed once after the run (lowest overhead)
cp = SqliteCheckpointer(
    "./runs.db",
    policy=CheckpointPolicy(durability="exit", retention="latest"),
)
```

| Mode | Behavior | Crash Safety | Performance |
|------|----------|-------------|-------------|
| `async` | Background save tasks | Most steps saved | Good (default) |
| `sync` | Await each save | Full | Slight overhead |
| `exit` | Buffer, flush at end | None during run | Best |

### Without workflow_id

For `runner.run()`, if a checkpointer is configured and you omit `workflow_id`, hypergraph auto-generates one and persists the run. This reduces boilerplate while keeping runs addressable.

```python
# Auto-generated workflow_id (when checkpointer exists)
result = await runner.run(graph, {"x": 5})
print(result.workflow_id)  # e.g. run-20260302-a7b3c2

# Explicit workflow_id
result = await runner.run(graph, {"x": 5}, workflow_id="my-run-1")
```

Paused interrupt-driven workflows are now persisted as **`PAUSED`** runs rather than overloading `ACTIVE`. In the notebook explorer, that means:

- `active` means currently executing and non-terminal
- `paused` means waiting for interrupt input, resumable, and also non-terminal
- `completed` and `failed` remain terminal

`runner.map()` still requires an explicit `workflow_id` to persist parent/child batch runs.

### Hierarchical Checkpointing

When you use nested graphs or `runner.map()` with a `workflow_id`, hypergraph automatically creates a parent-child run hierarchy. This lets you drill into specific sub-runs without losing the big picture.

#### Nested Graphs

A `GraphNode` step automatically creates a child run with the ID `{workflow_id}/{node_name}`:

```python
from hypergraph import Graph, SyncRunner, node
from hypergraph.checkpointers import SqliteCheckpointer

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3

inner = Graph([double], name="inner")
outer = Graph([inner.as_node(name="embed"), triple], name="outer")

cp = SqliteCheckpointer("./runs.db")
runner = SyncRunner(checkpointer=cp)
result = runner.run(outer, {"x": 5}, workflow_id="pipeline-001")

# Two runs are created:
#   pipeline-001       (parent — outer graph)
#   pipeline-001/embed (child — inner graph)

# The parent's step record for "embed" includes a child_run_id
steps = cp.steps("pipeline-001")
embed_step = [s for s in steps if s.node_name == "embed"][0]
print(embed_step.child_run_id)  # "pipeline-001/embed"

# Drill into the child run
cp.values("pipeline-001/embed")  # {"doubled": 10}
```

#### Batch Runs with map()

`runner.map()` creates a parent batch run and per-item child runs with IDs `{workflow_id}/{index}`:

```python
results = runner.map(
    graph,
    {"x": [1, 2, 3]},
    map_over="x",
    workflow_id="batch-001",
)
# Creates:
#   batch-001    (parent batch run)
#   batch-001/0  (child — x=1)
#   batch-001/1  (child — x=2)
#   batch-001/2  (child — x=3)
```

#### Querying the Hierarchy

```python
# Top-level runs only
cp.runs(parent_run_id=None)

# Children of a specific run
cp.runs(parent_run_id="batch-001")

# All runs (including children)
cp.runs()
```

> **Note**: `cp.runs()` returns all runs (including children) by default.
> Pass `parent_run_id=None` to see top-level runs only.

## Choosing the Right Tool

| Question | Tool |
|----------|------|
| "What happened in the run I just finished?" | RunLog (`result.log`) |
| "Which node was slowest?" | RunLog (`sorted(result.log.steps, key=...)`) or Checkpointer (`cp.stats(...)`) |
| "What happened in yesterday's run?" | Checkpointer (`cp.runs()`, `cp.steps(...)`) |
| "What values were produced at step 3?" | Checkpointer (`cp.state("<id>", superstep=3)`) |
| "What failed runs exist?" | Checkpointer (`cp.runs(status=WorkflowStatus.FAILED)`) |
| "What paused runs can I resume?" | Checkpointer (`cp.runs(status=WorkflowStatus.PAUSED)`) |
| "What did the nested graph produce?" | Checkpointer (`cp.values("<parent-id>/<node-name>")`) |
| "What happened in batch item 5?" | Checkpointer (`cp.steps("<batch-id>/5")`) |
| "Find all steps that hit a specific error" | Checkpointer (`cp.search("error message")`) |
| "I need to inspect from another process" | Checkpointer sync reads |
| "I need JSON for my monitoring system" | RunLog (`to_dict()`) |

## See Also

- [Observe Execution](observe-execution.md) — Event processors (Rich progress, custom metrics)
- [Events API Reference](../06-api-reference/events.md) — Event type definitions
