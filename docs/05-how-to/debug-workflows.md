# Debug Workflows

Three tools for running and understanding graph execution, from quick in-process inspection to cross-process persistence and CLI execution/debugging.

| Tool | When to Use | Setup | Scope |
|------|-------------|-------|-------|
| **RunLog** | "What happened in this run?" | Zero — always on | In-process, current run |
| **Checkpointer** | "What happened yesterday?" | Pass to runner | Cross-process, persisted |
| **CLI** | "Run this graph" / "Show me the failing run" | `pip install hypergraph[cli]` | Terminal, any process |

## RunLog — Always-On Execution Trace

Every `runner.run()` and `runner.map()` call returns a `RunResult` with a `.log` attribute — an always-on execution trace that requires zero configuration.

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
| **Checkpointing** | Ephemeral — not persisted | Persisted as one run |
| **CLI access** | Not available | `hypergraph runs values <id>` |

Use `runner.map()` when you need to debug individual items. Use `map_over` when you need persistence and CLI access.

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

### Inspecting Persisted State

```python
# From any process that can access the DB file:
from hypergraph.checkpointers import SqliteCheckpointer, WorkflowStatus

cp = SqliteCheckpointer("./runs.db")

# Sync reads — no await needed, works from any context
cp.runs()                             # List all runs
cp.run("my-run-1")                    # Run metadata (status, duration, counts)
cp.values("my-run-1")                 # {"doubled": 10, "tripled": 30}
cp.steps("my-run-1")                  # Step records with timing
cp.stats("my-run-1")                  # Per-node duration/frequency breakdown
cp.checkpoint("my-run-1")             # Full snapshot (values + steps)

# Filter by status, graph, or time
cp.runs(status=WorkflowStatus.FAILED)
cp.runs(graph_name="my_graph", since=datetime(2024, 1, 1))

# Full-text search across step records
cp.search_sync("generate")            # Match node names and errors

# Time travel: state at a specific superstep
cp.state("my-run-1", superstep=1)     # {"doubled": 10}
```

The sync read methods (`runs()`, `run()`, `values()`, `steps()`, `stats()`, `checkpoint()`) work without async/await, making them ideal for debugging scripts, notebooks, and the CLI. No `initialize()` call needed.

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

If you don't pass `workflow_id`, no checkpointing occurs — the runner behaves exactly as before. Checkpointing is opt-in per run.

```python
# No checkpointing — workflow_id omitted
result = await runner.run(graph, {"x": 5})

# With checkpointing
result = await runner.run(graph, {"x": 5}, workflow_id="my-run-1")
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

# Select specific outputs
hypergraph run my_module:graph x=5 --select doubled

# JSON output
hypergraph run my_module:graph x=5 --json

# With checkpointing
hypergraph run my_module:graph x=5 --db ./runs.db --workflow-id exp-42

# Verbose (shows timing and step count)
hypergraph run my_module:graph x=5 --verbose
```

The runner is auto-detected: SyncRunner for sync-only graphs, AsyncRunner if the graph has async nodes or `--db` is used. Override with `--runner sync` or `--runner async`.

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

### runs show — Execution Trace

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

```bash
# All runs
hypergraph runs ls

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
#   Node      Type          Runs  Total   Avg     Max     Errors  Cached
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
| "Find all steps that hit a specific error" | CLI (`runs search "error message"`) |
| "I need to inspect from another process" | Checkpointer sync reads |
| "I need JSON for my monitoring system" | CLI (`--json`) or RunLog (`to_dict()`) |

## See Also

- [Observe Execution](observe-execution.md) — Event processors (Rich progress, custom metrics)
- [Events API Reference](../06-api-reference/events.md) — Event type definitions
