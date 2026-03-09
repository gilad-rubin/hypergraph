# Debug Workflows

Three tools for running and understanding graph execution, from quick in-process inspection to cross-process persistence and CLI execution/debugging.

| Tool | When to Use | Setup | Scope |
|------|-------------|-------|-------|
| **RunLog** | "What happened in this run?" | Zero — always on | In-process, current run |
| **Checkpointer** | "What happened yesterday?" | Pass to runner | Cross-process, persisted |
| **CLI** | "Run this graph" / "Show me the failing run" | `pip install hypergraph[cli]` | Terminal, any process |

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

graph = Graph([double, triple])
runner = SyncRunner()
result = runner.run(graph, {"x": 5})

# RunLog is always available
print(result.log)
```

Output:

```
RunLog: graph | 0.3ms | 2 nodes | 0 errors

  Step  Node    Duration  Status
  ────  ──────  ────────  ─────────
     0  double     0.1ms  completed
     1  triple     0.1ms  completed
```

### Finding Slow Nodes

```python
# Sort by duration (slowest first)
for record in result.log.slowest():
    print(f"{record.node_name}: {record.duration_ms:.1f}ms")

# Per-node aggregates (useful for map operations)
for name, stats in result.log.node_stats.items():
    print(f"{name}: avg={stats.avg_ms:.1f}ms, count={stats.count}")
```

### Finding Errors

```python
result = runner.run(graph, {"x": 5}, error_handling="continue")

# All errors
for record in result.log.errors:
    print(f"{record.node_name}: {record.error}")

# Summary
print(result.log.summary())
# "2 nodes, 1 error, 0 cached | total: 42.5ms"
```

### Routing Decisions

```python
# Which path did each gate take?
for record in result.log.records:
    if record.decision:
        print(f"{record.node_name} → {record.decision}")
```

### Serialization

```python
# Export for logging, dashboards, etc.
log_dict = result.log.to_dict()
# {"records": [...], "node_stats": {...}, "total_ms": 42.5, ...}
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

### runner.map() vs map_over for debugging

The batch pattern you choose affects what debugging data is available:

| | `runner.map()` | `map_over` |
|---|---|---|
| **RunLog granularity** | Per-item RunLogs with full traces | One RunLog (batch = one step) |
| **Error isolation** | Each item independent | One failure affects entire step |
| **Checkpointing** | Parent + per-item child runs | Persisted as one run step |
| **CLI access** | `runs ls --parent <batch-id>` | `hypergraph runs values <id>` |

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

Every step is now persisted. You can inspect it from another process, another day, or via the CLI.

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

The sync read methods (`runs()`, `run()`, `values()`, `steps()`, `search()`, `stats()`, `checkpoint()`) work without async/await, making them ideal for debugging scripts, notebooks, and the CLI. No `initialize()` call needed.

For branching semantics, use explicit checkpoints:
`checkpoint = cp.checkpoint("workflow-id", superstep=...)` and pass it to
`runner.run(..., checkpoint=checkpoint, workflow_id="new-id")`.

CLI equivalents:

```bash
hypergraph runs checkpoint my-run-1 --deep
hypergraph runs lineage my-run-1 --deep
hypergraph runs lineage my-run-1 --json --output /tmp/my-run-1.lineage.json
```

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
# Top-level runs only (default for CLI)
cp.runs(parent_run_id=None)

# Children of a specific run
cp.runs(parent_run_id="batch-001")

# All runs (including children)
cp.runs()
```

> **Note**: The Python API and CLI have different defaults for hierarchy filtering.
> `cp.runs()` returns all runs (including children) for backward compatibility,
> while `hypergraph runs ls` defaults to top-level only. Use `--all` to see everything.

### Default Database Location

The `resolve_db_path()` function determines the database path using a priority chain:

| Priority | Source | Example |
|----------|--------|---------|
| 1 | Explicit `--db` flag or parameter | `SqliteCheckpointer("./my.db")` |
| 2 | `HYPERGRAPH_DB` environment variable | `export HYPERGRAPH_DB=./runs.db` |
| 3 | `[tool.hypergraph] db` in pyproject.toml | `db = "./runs.db"` |
| 4 | Convention (if `[tool.hypergraph]` section exists) | `.hypergraph/runs.db` |

This means you can set it once in pyproject.toml and never think about it again:

```toml
[tool.hypergraph]
db = "./runs.db"
```

## CLI — Execute and Debug from the Terminal

The CLI lets you run graphs and inspect runs directly from the terminal, designed for both humans and AI agents.

### Install

```bash
pip install 'hypergraph[cli]'
```

### Quick Overview

```bash
# Run a graph
hypergraph run my_module:graph x=5

# Map over multiple inputs
hypergraph map my_module:graph --map-over x 'x=[1,2,3]'

# List registered graphs
hypergraph graph ls

# Inspect graph structure
hypergraph graph inspect my_module:graph

# Debug persisted runs
hypergraph runs ls
hypergraph runs show my-run-1
hypergraph runs values my-run-1
hypergraph runs stats my-run-1
hypergraph runs search "error"
```

### run — Execute a Graph

```bash
# Pass inputs as key=value
hypergraph run my_module:graph x=5

# Use JSON string or file for complex inputs
hypergraph run my_module:graph --values '{"x": 5, "y": [1, 2]}'
hypergraph run my_module:graph --values params.json

# key=value args override --values
hypergraph run my_module:graph --values params.json x=10

# Configure output scope in Python, then run that graph object
# my_module:selected_graph = graph.select("doubled")
hypergraph run my_module:selected_graph x=5

# JSON output
hypergraph run my_module:graph x=5 --json

# With checkpointing
hypergraph run my_module:graph x=5 --db ./runs.db --workflow-id exp-42

# Verbose (shows timing and step count)
hypergraph run my_module:graph x=5 --verbose
```

The runner is auto-detected: SyncRunner for sync-only graphs, AsyncRunner if the graph has async nodes or `--db` is used. Override with `--runner sync` or `--runner async`.

> **Note:** Runtime `--select` overrides are not supported. Configure output scope on the graph itself with `graph.select(...)`, then expose that configured graph object or registry entry to the CLI.

### map — Batch Execution

```bash
# Map over a single parameter
hypergraph map my_module:graph --map-over x --values '{"x": [1, 2, 3]}'

# Map over multiple parameters (zip mode)
hypergraph map my_module:graph --map-over a,b --values '{"a": [1, 2], "b": [10, 20]}'

# Cartesian product
hypergraph map my_module:graph --map-over a,b --map-mode product \
  --values '{"a": [1, 2], "b": [10, 20]}'

# key=value args work too
hypergraph map my_module:graph --map-over x 'x=[1,2,3]'
```

`map` defaults to `--error-handling continue` (CLI-friendly: see all results, don't abort on first failure). `run` defaults to `raise`.

### Graph Registry — pyproject.toml Shortcuts

Register graph names in `pyproject.toml` to avoid typing module paths:

```toml
[tool.hypergraph.graphs]
pipeline = "my_module:graph"
etl = "etl.main:pipeline"

[tool.hypergraph]
db = "./runs.db"  # default --db for all commands
```

Then use short names everywhere:

```bash
hypergraph run pipeline x=5
hypergraph map pipeline --map-over x 'x=[1,2,3]'
hypergraph graph inspect pipeline
```

### graph ls — List Registered Graphs

```bash
hypergraph graph ls

#   Registered graphs (2):
#
#   Name      Module Path
#   ────────  ──────────────────
#   etl       etl.main:pipeline
#   pipeline  my_module:graph
```

### Run Debugging

The following commands inspect persisted runs (requires `--db` or a `db` setting in pyproject.toml).

### runs show — Run Trace

```bash
hypergraph runs show my-run-1

# Run: my-run-1 | completed | 2 steps | 0.3ms
#
#   Step  Node    Type          Duration  Status     Decision
#   ────  ──────  ────────────  ────────  ─────────  ────────
#      0  double  FunctionNode     0.1ms  completed
#      1  triple  FunctionNode     0.1ms  completed
#
#   → hypergraph runs values my-run-1            for output values
#   → hypergraph runs stats my-run-1             for performance breakdown
```

For runs with nested graphs, the step table includes a `Child Run` column linking to the child run ID. For batch parents, a children summary is shown:

```bash
hypergraph runs show pipeline-001

# Run: pipeline-001 (outer) | completed | 2 steps | 5.3ms
#
#   Step  Node    Type       Duration  Status     Child Run
#   ────  ──────  ─────────  ────────  ─────────  ──────────────────
#      0  embed   GraphNode     3.1ms  completed  pipeline-001/embed
#      1  triple  FunctionNode  0.1ms  completed  —
#
#   Children: 1 (1 completed, 0 failed)
#
#   → hypergraph runs ls --parent pipeline-001   to list child runs
```

Single-step drill-down:

```bash
hypergraph runs show my-run-1 --step 0

# Step [0] double | completed | 0.1ms
#   type: FunctionNode
#   input_versions: {"x": 0}
#   values: {'doubled': '<int, 2B>'}
#   cached: False

# Show the actual values:
hypergraph runs show my-run-1 --step 0 --values
```

Filter to errors or specific nodes:

```bash
hypergraph runs show my-run-1 --errors          # Only failed steps
hypergraph runs show my-run-1 --node generate   # Specific node
```

### runs values — Progressive Value Disclosure

By default, `values` shows type and size only (protecting AI agent context windows from large embeddings):

```bash
hypergraph runs values my-run-1

# Values: my-run-1 (through step 1)
#
#   Output   Type  Size  Step  Node
#   ───────  ────  ────  ────  ──────
#   doubled  int   2B    0     double
#   tripled  int   2B    1     triple
#
#   → hypergraph runs values my-run-1 --key <name>  for a single value
#   → hypergraph runs values my-run-1 --full        to show values inline
#   → hypergraph runs values my-run-1 --json        for full JSON
```

Drill in:

```bash
# Show all values inline
hypergraph runs values my-run-1 --full

# Show one value
hypergraph runs values my-run-1 --key doubled
# 10

# Time-travel: state at a specific superstep
hypergraph runs values my-run-1 --superstep 0
```

### runs ls — List and Filter

By default, `runs ls` shows top-level runs only (no children). Use `--parent` or `--all` to navigate the hierarchy.

```bash
# Top-level runs (default)
hypergraph runs ls

# Children of a specific run
hypergraph runs ls --parent batch-001

# All runs including children
hypergraph runs ls --all

# Only failures
hypergraph runs ls --status failed

# Filter by graph name or recency
hypergraph runs ls --graph my_graph
hypergraph runs ls --since 1h

# Limit results
hypergraph runs ls --limit 10
```

### runs steps — Detailed Step Records

```bash
# All steps in a run
hypergraph runs steps my-run-1

# Filter to a specific node
hypergraph runs steps my-run-1 --node generate

# With actual output values
hypergraph runs steps my-run-1 --values --full
```

### runs search — Full-Text Search

```bash
# Search across all step records (node names and errors)
hypergraph runs search "generate"

# Limit to errors only
hypergraph runs search "timeout" --field error
```

### runs stats — Performance Breakdown

```bash
hypergraph runs stats my-run-1

# Stats: my-run-1 | completed
#
#   Node      Type          Steps Total   Avg     Max     Errors  Cached
#   ────────  ────────────  ────  ──────  ──────  ──────  ──────  ──────
#   generate  FunctionNode  5     120ms   24ms    45ms    1       0
#   embed     FunctionNode  5     15ms    3ms     5ms     0       3
```

### JSON Output for Agents

Every command supports `--json` for machine-readable output with a stable envelope:

```bash
hypergraph runs show my-run-1 --json
```

```json
{
  "schema_version": 2,
  "command": "runs.show",
  "generated_at": "2024-01-16T09:00:00Z",
  "data": {
    "run": {"id": "my-run-1", "status": "completed", ...},
    "steps": [...]
  }
}
```

The `schema_version` field ensures forward compatibility — agents can detect breaking changes.

### Save to File

```bash
# Dump full state to file (avoids flooding terminal)
hypergraph runs values my-run-1 --json --output state.json

# Dump step records
hypergraph runs steps my-run-1 --json --output steps.json
```

### graph inspect — Static Structure

```bash
# By module path or registered name
hypergraph graph inspect my_module:graph
hypergraph graph inspect pipeline

# Graph: my_graph | 5 nodes | 6 edges
#
#   Node      Type          Inputs          Outputs
#   ────────  ────────────  ──────────────  ────────────
#   embed     FunctionNode  query           embedding
#   retrieve  FunctionNode  embedding       docs
#   generate  FunctionNode  docs, query     answer
#
#   Required inputs: query
```

### Custom Database Path

All commands accept `--db` to point to a specific database:

```bash
hypergraph runs ls --db /path/to/runs.db
```

## Choosing the Right Tool

| Question | Tool |
|----------|------|
| "I want to run a graph from the terminal" | CLI (`hypergraph run`) |
| "I want to batch-run with different inputs" | CLI (`hypergraph map`) |
| "What happened in the run I just finished?" | RunLog (`result.log`) |
| "Which node was slowest?" | RunLog (`result.log.slowest()`) or CLI (`runs stats`) |
| "What happened in yesterday's run?" | Checkpointer + CLI |
| "What values were produced at step 3?" | CLI (`runs values --superstep 3`) |
| "What failed runs exist?" | CLI (`runs ls --status failed`) |
| "What did the nested graph produce?" | CLI (`runs show <id>` + `runs ls --parent <id>`) |
| "What happened in batch item 5?" | CLI (`runs show <batch-id>/5`) |
| "Find all steps that hit a specific error" | CLI (`runs search "error message"`) |
| "I need to inspect from another process" | Checkpointer sync reads |
| "I need JSON for my monitoring system" | CLI (`--json`) or RunLog (`to_dict()`) |

## See Also

- [Observe Execution](observe-execution.md) — Event processors (Rich progress, custom metrics)
- [Events API Reference](../06-api-reference/events.md) — Event type definitions
