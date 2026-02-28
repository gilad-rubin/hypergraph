# Debug Workflows

Three tools for running and understanding graph execution, from quick in-process inspection to cross-process persistence and CLI execution/debugging.

| Tool | When to Use | Setup | Scope |
|------|-------------|-------|-------|
| **RunLog** | "What happened in this run?" | Zero — always on | In-process, current run |
| **Checkpointer** | "What happened yesterday?" | Pass to runner | Cross-process, persisted |
| **CLI** | "Run this graph" / "Show me the failing workflow" | `pip install hypergraph[cli]` | Terminal, any process |

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
| **Checkpointing** | Ephemeral — not persisted | Persisted as one workflow |
| **CLI access** | Not available | `hypergraph workflows state <id>` |

Use `runner.map()` when you need to debug individual items. Use `map_over` when you need persistence and CLI access.

## Checkpointer — Persistent Workflow History

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
checkpointer = SqliteCheckpointer("./workflows.db")
runner = AsyncRunner(checkpointer=checkpointer)

# Run with a workflow_id to enable persistence
result = await runner.run(graph, {"x": 5}, workflow_id="my-run-1")
```

Every step is now persisted. You can inspect it from another process, another day, or via the CLI.

### Inspecting Persisted State

```python
# From any process that can access the DB file:
cp = SqliteCheckpointer("./workflows.db")

# Sync reads — no await needed, works from any context
cp.workflows()                        # List all workflows
cp.state("my-run-1")                  # {"doubled": 10, "tripled": 30}
cp.steps("my-run-1")                  # Step records with timing
cp.checkpoint("my-run-1")             # Full snapshot (values + steps)

# Filter by status
cp.workflows(status=WorkflowStatus.FAILED)

# Time travel: state at a specific superstep
cp.state("my-run-1", superstep=1)     # {"doubled": 10}
```

The sync read methods (`workflows()`, `state()`, `steps()`, `checkpoint()`) work without async/await, making them ideal for debugging scripts, notebooks, and the CLI. No `initialize()` call needed.

### Durability Modes

The `CheckpointPolicy` controls when steps are written to the database:

```python
from hypergraph.checkpointers import CheckpointPolicy, SqliteCheckpointer

# Default: async — steps saved in background tasks (fastest)
cp = SqliteCheckpointer("./workflows.db")

# Sync: steps saved immediately after each superstep (safest)
cp = SqliteCheckpointer(
    "./workflows.db",
    policy=CheckpointPolicy(durability="sync"),
)

# Exit: steps buffered, flushed once after the run (lowest overhead)
cp = SqliteCheckpointer(
    "./workflows.db",
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

The CLI lets you run graphs and inspect workflows directly from the terminal, designed for both humans and AI agents.

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

# Debug persisted workflows
hypergraph workflows ls
hypergraph workflows show my-run-1
hypergraph workflows state my-run-1
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
hypergraph run my_module:graph x=5 --db ./workflows.db --workflow-id exp-42

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
db = "./workflows.db"  # default --db for all commands
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

### Workflow Debugging

The following commands inspect persisted workflows (requires `--db` or a `db` setting in pyproject.toml).

### workflows show — Execution Trace

```bash
hypergraph workflows show my-run-1

# Workflow: my-run-1 | completed | 2 steps | 0.3ms
#
#   Step  Node    Duration  Status     Decision
#   ────  ──────  ────────  ─────────  ────────
#      0  double     0.1ms  completed
#      1  triple     0.1ms  completed
#
#   To see values at a step: hypergraph workflows state my-run-1 --step N
#   To save full trace:      hypergraph workflows show my-run-1 --json --output trace.json
```

### workflows state — Progressive Value Disclosure

By default, `state` shows type and size only (protecting AI agent context windows from large embeddings):

```bash
hypergraph workflows state my-run-1

# State: my-run-1 (through step 1)
#
#   Output   Type  Size  Step  Node
#   ───────  ────  ────  ────  ──────
#   doubled  int   10    0     double
#   tripled  int   30    1     triple
#
#   Values hidden. Use --values to show, --key <name> for one value.
```

Drill in:

```bash
# Show all values
hypergraph workflows state my-run-1 --values

# Show one value
hypergraph workflows state my-run-1 --key doubled
# 10
```

### workflows ls — List and Filter

```bash
# All workflows
hypergraph workflows ls

# Only failures
hypergraph workflows ls --status failed

# Limit results
hypergraph workflows ls --limit 10
```

### JSON Output for Agents

Every command supports `--json` for machine-readable output with a stable envelope:

```bash
hypergraph workflows show my-run-1 --json
```

```json
{
  "schema_version": 1,
  "command": "workflows.show",
  "generated_at": "2024-01-16T09:00:00Z",
  "data": {
    "workflow": {"id": "my-run-1", "status": "completed", ...},
    "steps": [...]
  }
}
```

The `schema_version` field ensures forward compatibility — agents can detect breaking changes.

### Save to File

```bash
# Dump full state to file (avoids flooding terminal)
hypergraph workflows state my-run-1 --values --output state.json

# Dump step records
hypergraph workflows steps my-run-1 --json --output steps.json
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
hypergraph workflows ls --db /path/to/workflows.db
```

## Choosing the Right Tool

| Question | Tool |
|----------|------|
| "I want to run a graph from the terminal" | CLI (`hypergraph run`) |
| "I want to batch-run with different inputs" | CLI (`hypergraph map`) |
| "What happened in the run I just finished?" | RunLog (`result.log`) |
| "Which node was slowest?" | RunLog (`result.log.slowest()`) |
| "What happened in yesterday's run?" | Checkpointer + CLI |
| "What values were produced at step 3?" | CLI (`workflows state --step 3`) |
| "What failed workflows exist?" | CLI (`workflows ls --status failed`) |
| "I need to inspect from another process" | Checkpointer |
| "I need JSON for my monitoring system" | CLI (`--json`) or RunLog (`to_dict()`) |

## See Also

- [Observe Execution](observe-execution.md) — Event processors (Rich progress, custom metrics)
- [Events API Reference](../06-api-reference/events.md) — Event type definitions
