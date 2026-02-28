# Debug Workflows

Three tools for understanding what happened during graph execution, from quick in-process inspection to cross-process persistence and CLI debugging.

| Tool | When to Use | Setup | Scope |
|------|-------------|-------|-------|
| **RunLog** | "What happened in this run?" | Zero — always on | In-process, current run |
| **Checkpointer** | "What happened yesterday?" | Pass to runner | Cross-process, persisted |
| **CLI** | "Show me the failing workflow" | `pip install hypergraph[cli]` | Terminal, any process |

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

For `runner.map()`, each individual result has its own RunLog:

```python
results = runner.map(graph, {"x": [1, 2, 3]}, map_over="x")

for i, r in enumerate(results):
    print(f"Item {i}: {r.log.summary()}")
```

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
checkpointer = SqliteCheckpointer("./workflows.db")
await checkpointer.initialize()

# List all workflows
workflows = await checkpointer.list_workflows()
for wf in workflows:
    print(f"{wf.id}: {wf.status.value}")

# Get the accumulated state (all node outputs merged)
state = await checkpointer.get_state("my-run-1")
print(state)  # {"doubled": 10, "tripled": 30}

# Time travel: state at a specific superstep
state_at_step1 = await checkpointer.get_state("my-run-1", superstep=1)
print(state_at_step1)  # {"doubled": 10}

# Get individual step records
steps = await checkpointer.get_steps("my-run-1")
for step in steps:
    print(f"Step {step.index}: {step.node_name} ({step.duration_ms:.1f}ms)")
```

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

## CLI — Terminal Debugging Interface

The CLI provides terminal-based workflow inspection, designed for both humans and AI agents.

### Install

```bash
pip install 'hypergraph[cli]'
```

### Quick Overview

```bash
# What workflows exist?
hypergraph workflows ls

# What happened in a specific run?
hypergraph workflows show my-run-1

# What values were produced?
hypergraph workflows state my-run-1

# Drill into one value
hypergraph workflows state my-run-1 --key doubled

# Detailed step records
hypergraph workflows steps my-run-1
```

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
hypergraph graph inspect my_module:graph

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
