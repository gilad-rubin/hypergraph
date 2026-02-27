# Execution Trace & Debugging — Plan v5 (Persistence-First)

## Motivation: The Debugging Gap

You run a graph over 50 items. Some are slow. Some fail. A gate routes half the items down an unexpected path. What happened?

**Today, you get this:**

```python
result = runner.run(graph, values={"queries": queries})

# Success or failure — that's it
result.status      # RunStatus.COMPLETED
result.values      # {"answers": [...]}
result.error       # None (or an exception)

# Questions you can't answer:
# - Which nodes were slow? How slow?
# - Which items failed and why?
# - What path did execution take through gates?
# - What prompt actually went into the LLM for item 5?
# - What did the retriever return before the LLM hallucinated?
# - What happened in yesterday's run that I already closed?
```

You can attach an `EventProcessor` and log events in real-time — but that requires setup *before* the run, and the data is gone once the process exits. There's no built-in way to inspect execution after the fact.

**The gap is twofold:**
1. **In-process**: No execution summary on RunResult. You have to set up event processors to see anything.
2. **Cross-process**: No persistent record. If the process exits, crashes, or you just close the REPL — everything is lost.

---

## Use Cases

### UC1: "Why was my run slow?"

**The scenario**: You ran an LLM pipeline over 50 items. Total time was 8 minutes. You expected 2 minutes. Where's the bottleneck?

**Before (today):**
```python
# No built-in way to answer this. You'd need to:
# 1. Write a custom EventProcessor before running
# 2. Manually track timestamps in on_node_end()
# 3. Aggregate and analyze yourself
# 4. Hope you set it up before the slow run happened
```

**After (Phase 1 — RunLog):**
```python
result = runner.run(graph, values={"queries": queries})

print(result.log)
# RunLog: rag_pipeline | 8m12s | 4 nodes | 0 errors
#
#   Node        Runs   Total    Avg     Errors  Cached
#   ──────────  ─────  ───────  ──────  ──────  ──────
#   embed         50    9.0s    180ms        0       0
#   llm_call      50   7m48s   9360ms        0       0
#   format        50    2.4s     48ms        0       0
#   validate      50    0.7s     14ms        0       0
```

Zero config. Always available. Every `RunResult` has `.log`.

And if you need programmatic access (for CI, assertions, dashboards):
```python
result.log.node_stats["llm_call"]
# NodeStats(count=50, avg_ms=9360, total_ms=468000, errors=0, cached=0)

result.log.timing
# {"embed": 9000, "llm_call": 468000, "format": 2400, "validate": 720}
```

---

### UC2: "What failed and why?"

**The scenario**: You mapped over 200 items. 193 succeeded, 7 failed. Which ones? What errors?

**Before:**
```python
results = runner.map(graph, values={...}, map_over="query")
# results is list[RunResult]

# Manual scanning:
for i, r in enumerate(results):
    if r.status == RunStatus.FAILED:
        print(f"Item {i}: {r.error}")
# Gets you the final error, but not WHICH node failed or timing context
```

**After:**
```python
results = runner.map(graph, values={...}, map_over="query")

# Quick overview — which items failed?
failed = [r for r in results if r.log.errors]
print(f"{len(failed)} of {len(results)} items failed")

# Detailed errors with formatting
for r in failed:
    print(r.log)
# RunLog: rag_pipeline [item 12] | 1.2s | 3 nodes | 1 error
#
#   Step  Node       Duration  Status
#   ────  ─────────  ────────  ──────────────────────────────────
#      0  embed        180ms   completed
#      1  llm_call         —   FAILED: 504 Gateway Timeout
#
# RunLog: rag_pipeline [item 45] | 0.8s | 3 nodes | 1 error
# ...
```

---

### UC3: "What path did execution take?"

**The scenario**: A routing graph classifies inputs and sends them down different paths. You need to verify the routing logic.

**Before:**
```python
# Attach a custom processor that listens for RouteDecisionEvent
# Hope you set it up before the run
# Manually correlate decisions with node execution order
```

**After:**
```python
result = runner.run(graph, values={"query": "How do I reset my password?"})

print(result.log)
# RunLog: support_router | 2.6s | 3 nodes | 0 errors
#
#   Step  Node              Duration  Status     Decision
#   ────  ────────────────  ────────  ─────────  ─────────────────
#      0  classify            120ms   completed  → account_support
#      1  account_support    2400ms   completed
#      2  format_response      45ms   completed
```

---

### UC4: "What prompt went into the LLM for item 5?"

**The scenario**: You mapped a RAG pipeline over 50 queries. Item 5's answer is wrong. You need to see the actual intermediate values — what did the retriever return? What prompt was assembled? What went into the LLM?

This is the **intermediate value inspection** problem. RunLog can tell you *timing and status* but not *what data flowed between nodes*. Values are O(data) — they could be embeddings, full documents, LLM responses. Too large for always-on in-memory capture.

**Before:**
```python
results = runner.map(graph, values={"queries": queries}, map_over="query")

# You can see the final output:
results[5]["answer"]  # "Some wrong answer"

# But you CAN'T see:
# - What documents were retrieved for item 5?
# - What prompt was assembled from those documents?
# - What the classifier decided before routing?
# There's no record of intermediate node outputs.
```

**After — in-process (no checkpointer):**

RunLog tells you *where* to look:
```python
print(results[5].log)
# RunLog: rag_pipeline [item 5] | 3.2s | 4 nodes | 0 errors
#
#   Step  Node          Duration  Status
#   ────  ────────────  ────────  ─────────
#      0  embed           180ms   completed
#      1  retrieve        220ms   completed
#      2  build_prompt     12ms   completed
#      3  generate       2800ms   completed

# Timing is fine — no errors, no unusual latency.
# The problem is in the DATA, not the execution.
# But RunLog doesn't store values (by design — O(data) is too expensive always-on).

# For in-process value access, you still have the final result:
results[5]["answer"]          # final output
results[5]["retrieved_docs"]  # IF this was a selected output
# But intermediate values that aren't graph outputs are gone.
```

**After — with checkpointer (full intermediate inspection):**

```python
runner = AsyncRunner(checkpointer=SqliteCheckpointer("./workflows.db"))
results = await runner.map(
    graph, values={"queries": queries}, map_over="query",
    workflow_id="batch-50-queries",  # enables persistence
)

# Now inspect item 5's intermediate values at any superstep:

# What did retriever return?
state_after_retrieve = await checkpointer.get_state(
    "batch-50-queries/item-5", superstep=1
)
print(state_after_retrieve["retrieved_docs"])
# [{"title": "Password Reset Guide", "content": "..."}, ...]
# ^ Aha — wrong documents were retrieved!

# What prompt was assembled from those docs?
state_after_prompt = await checkpointer.get_state(
    "batch-50-queries/item-5", superstep=2
)
print(state_after_prompt["prompt"])
# "Based on the following documents:\n1. Password Reset Guide\n..."
# ^ Now you can see exactly what the LLM received

# Or inspect individual step records:
steps = await checkpointer.get_steps("batch-50-queries/item-5")
retrieve_step = next(s for s in steps if s.node_name == "retrieve")
print(retrieve_step.values)     # {"retrieved_docs": [...]}
print(retrieve_step.duration_ms)  # 220.0
print(retrieve_step.input_versions)  # {"query": 1, "embedding": 1}
```

**The key insight**: RunLog answers "what happened?" (timing, status, routing). Checkpointer answers "what data flowed?" (intermediate values at every step). This is why persistence is the key enabler for deep debugging.

---

### UC5: "What happened in yesterday's run?"

**The scenario**: You ran a workflow yesterday. It completed but the results look wrong. The process is long gone. You need to inspect intermediate values and execution history.

**Before:**
```python
# Impossible. No persistent record of execution.
# You'd need to re-run with logging, or use an external
# observability tool (Langfuse, etc.)
```

**After (Phase 3 — Checkpointer):**
```python
from hypergraph.checkpointers import SqliteCheckpointer

checkpointer = SqliteCheckpointer("./workflows.db")

# --- Today, in a new process (yesterday's run is long gone) ---

# What ran?
workflow = await checkpointer.get_workflow("batch-2024-01-15")
print(workflow)
# Workflow: batch-2024-01-15 | completed | 12 steps | 4m32s
#
#   Step  Node          Duration  Status     Decision
#   ────  ────────────  ────────  ─────────  ────────
#      0  embed           180ms   completed
#      1  retrieve        820ms   completed
#      2  classify        120ms   completed  → detailed_answer
#      3  build_prompt     12ms   completed
#      4  generate       3100ms   completed
#   ...

# What were the intermediate values after step 2?
state = await checkpointer.get_state("batch-2024-01-15", superstep=2)
print(state["embedding"])       # The actual embedding vector
print(state["retrieved_docs"])  # The retrieved documents

# Drill into a single step's outputs
steps = await checkpointer.get_steps("batch-2024-01-15")
classify_step = next(s for s in steps if s.node_name == "classify")
classify_step.values    # {"category": "detailed_answer"}
classify_step.decision  # "detailed_answer"
```

---

### UC6: "Show me all failed workflows"

**The scenario**: You're running a production service. Multiple workflows run daily. You need a dashboard-like view of what's healthy and what's broken.

**Before:**
```python
# No workflow registry. Each run is fire-and-forget.
# You'd build your own tracking on top.
```

**After:**
```python
# List all workflows — formatted by default
workflows = await checkpointer.list_workflows()
print(workflows)
# Workflows (3 total)
#
#   ID                  Status     Steps  Duration  Created
#   ──────────────────  ─────────  ─────  ────────  ───────────────────
#   batch-2024-01-15    completed     12    4m32s   2024-01-15 09:00
#   batch-2024-01-16    FAILED         8    2m10s   2024-01-16 09:00
#   chat-session-42     active        24   12m05s   2024-01-16 14:30

# Filter to failures — drill into the failing step
failed = await checkpointer.list_workflows(status=WorkflowStatus.FAILED)
for wf in failed:
    print(wf)  # Shows step table with the FAILED step highlighted

# Workflow: batch-2024-01-16 | FAILED | 8 steps | 2m10s
#
#   Step  Node          Duration  Status
#   ────  ────────────  ────────  ─────────────────────────────────
#      0  embed           180ms   completed
#      1  retrieve        820ms   completed
#      2  llm_call            —   FAILED: 503 Service Unavailable
```

---

### UC7: AI Agent Debugging a Workflow

**The scenario**: An AI coding agent (Claude, Cursor, etc.) is helping a user debug a failing pipeline. The agent needs structured data it can reason over — not a wall of logs.

**Before:**
```python
# Agent would need to:
# 1. Ask user to add EventProcessors
# 2. Re-run the failing pipeline
# 3. Parse unstructured log output
# 4. Hope the relevant data was captured
```

**After — in-memory (same process):**

The agent can use formatted output (human-readable) or structured dict (machine-readable):

```python
result = runner.run(graph, values={...})

# Human-readable — agent can paste this into chat
str(result.log)
# "RunLog: rag_pipeline | 4.2s | 3 nodes | 1 error
#
#    Step  Node       Duration  Status
#    ────  ─────────  ────────  ──────────────────────────────────
#       0  embed        200ms   completed
#       1  retrieve     800ms   completed
#       2  generate    3100ms   FAILED: Context window exceeded"

# Machine-readable — agent can reason over this programmatically
result.log.to_dict()
# {"graph_name": "rag_pipeline", "total_duration_ms": 4200,
#  "steps": [{"node_name": "embed", "duration_ms": 200, "status": "completed"}, ...],
#  "node_stats": {"generate": {"count": 1, "errors": 1, ...}}}
```

**After — persistent (cross-process, from CLI or background):**
```python
# Agent queries the checkpointer directly — can inspect ACTUAL VALUES
steps = await checkpointer.get_steps("failing-workflow-123")
state_at_failure = await checkpointer.get_state("failing-workflow-123", superstep=1)

# "Ah, the retrieved context is 50K tokens — that's why generate failed"
state_at_failure["retrieved_docs"]  # The actual documents
state_at_failure["prompt"]          # The assembled prompt — 50K tokens!
```

---

### UC8: "Fork and retry from a specific point"

**The scenario**: A 5-step pipeline failed at step 4. Steps 1-3 were expensive (embeddings, API calls). You want to fix the code and retry from step 3, not re-run everything.

**Before:**
```python
# Use DiskCache to avoid re-computing cached nodes.
# But this only works with identical inputs and the same process.
# No way to fork from a specific point in history.
```

**After:**
```python
# Get the checkpoint at step 3 (before the failure)
checkpoint = await checkpointer.get_checkpoint("order-456", superstep=3)

# Fork into a new workflow with the state from step 3
result = await runner.run(
    graph,  # Updated graph with fixed code
    values={**checkpoint.values, "extra_context": "additional data"},
    history=checkpoint.steps,
    workflow_id="order-456-retry",
)
# Steps 1-3 outputs are already in the state — only steps 4-5 re-execute
```

---

## The Two-Layer Architecture

The use cases above reveal two distinct needs:

| Need | Data | Lifetime | Config |
|------|------|----------|--------|
| "Why was this run slow?" | Timing, status, routing | Current process | None |
| "What happened yesterday?" | Everything above + values | Across processes | Checkpointer |

This maps to two layers:

```
┌──────────────────────────────────────────────────────┐
│  Layer 1: RunLog (always-on, in-memory)              │
│  Timing + status per node. On every RunResult.       │
│  Zero config. Zero IO. O(nodes) memory.              │
│  Answers: UC1, UC2, UC3, UC4 (timing), UC7 (in-proc)  │
└───────────────────┬──────────────────────────────────┘
                    │ same execution, more data
                    ▼
┌──────────────────────────────────────────────────────┐
│  Layer 2: StepRecord via Checkpointer (persistent)   │
│  Full I/O + timing + status + routing. In SQLite/PG. │
│  Requires config. Survives crashes.                  │
│  Answers: UC4 (values), UC5, UC6, UC7 (cross), UC8   │
└──────────────────────────────────────────────────────┘
```

**RunLog** is a strict subset of **StepRecord**. Both capture the same execution — RunLog stores timing metadata (O(nodes)), StepRecord adds full values (O(data)).

**Why not just the checkpointer?** Because:
- Not everyone wants to configure a database path
- `result.log.summary()` should work on your first `pip install`
- In-memory metadata is negligible; persisting LLM response values is not

**Why not just RunLog?** Because:
- It dies with the process
- Can't query yesterday's run
- Can't fork from a specific point
- AI agent debugging needs cross-process access

---

## What Learned From Other Frameworks

| Framework | Key Lesson | Applied How |
|-----------|-----------|-------------|
| **Hatchet** | PostgreSQL IS both execution state AND observability — no separate trace store | Checkpointer IS the trace store. `get_steps()` is the trace query. |
| **Hatchet** | Four timestamps per step (created, assigned, started, completed) | StepRecord has `created_at`, `completed_at`, `duration_ms` |
| **LangGraph** | Checkpoints power resume AND time-travel AND debugging | Same `get_state(superstep=N)` does debugging and resume |
| **LangGraph** | No native per-node timing (needs LangSmith) | RunLog provides timing by default — better than LangGraph's base offering |
| **Temporal** | Append-only event history is the universal primitive | Steps are append-only. History is immutable. |
| **All three** | Execution data always captured by default | RunLog is always-on. StepRecord written when checkpointer present. |
| **All three** | Same data serves multiple purposes | StepRecord powers resume + debugging + time travel + forking |
| **All three** | Progressive disclosure: summary → detail → raw | `result.log.summary()` → `result.log.steps` → `checkpointer.get_steps()` |

---

## Progressive Disclosure in Practice

```
Level 1: One-liner
    result.log.summary()
    # "4 nodes, 2.1s, 0 errors | slowest: llm_call (1.8s)"

Level 2: Per-node stats
    result.log.node_stats["llm_call"]
    # NodeStats(count=50, avg_ms=4100, errors=2, cached=5)

Level 3: Step-by-step trace (in-memory)
    result.log.steps
    # (NodeRecord(node_name="embed", superstep=0, duration_ms=200, ...), ...)

Level 4: Full persistent record with values (cross-process)
    await checkpointer.get_steps("workflow-123")
    # [StepRecord(node_name="embed", values={"embedding": [...]}, ...), ...]

Level 5: Intermediate values at any point ("what went into the LLM?")
    await checkpointer.get_state("workflow-123", superstep=2)
    # {"embedding": [...], "docs": [...], "prompt": "Based on..."}
    # ^ Every node's output through superstep 2, accumulated

Level 6: Individual step's inputs and outputs
    steps = await checkpointer.get_steps("workflow-123")
    llm_step = next(s for s in steps if s.node_name == "generate")
    llm_step.values          # {"answer": "The capital is..."}
    llm_step.input_versions  # {"prompt": 1, "temperature": 1}
```

---

## API Surface Summary

### Always available (no config):

```python
result = runner.run(graph, values={...})

# ── Display (formatted output) ──
print(result.log)                       # Full step table with header + stats
result.log.summary()                    # str — one-line overview
str(result.log)                         # Same as print() output (for agents)
result.log._repr_html_()                # HTML table (auto in Jupyter notebooks)

# ── Programmatic access ──
result.log.steps                        # tuple[NodeRecord, ...] — per-node trace
result.log.errors                       # tuple[NodeRecord, ...] — failed nodes only
result.log.timing                       # dict[str, float] — total ms per node name
result.log.node_stats                   # dict[str, NodeStats] — aggregate per node
result.log.node_stats["x"].avg_ms       # float
result.log.node_stats["x"].count        # int
result.log.node_stats["x"].errors       # int
result.log.node_stats["x"].cached       # int
result.log.to_dict()                    # dict — JSON-serializable for AI agents
result.log.total_duration_ms            # float — wall-clock total
result.log.graph_name                   # str
result.log.run_id                       # str
```

### With checkpointer configured:

```python
from hypergraph.checkpointers import SqliteCheckpointer

runner = AsyncRunner(checkpointer=SqliteCheckpointer("./workflows.db"))
result = await runner.run(graph, values={...}, workflow_id="my-workflow")

# Query execution history (cross-process)
steps = await checkpointer.get_steps("my-workflow")              # list[StepRecord]
steps = await checkpointer.get_steps("my-workflow", superstep=3) # through superstep 3

# Query accumulated state (time travel)
state = await checkpointer.get_state("my-workflow")              # latest
state = await checkpointer.get_state("my-workflow", superstep=3) # at superstep 3

# Query workflow metadata
workflow = await checkpointer.get_workflow("my-workflow")         # Workflow | None
workflows = await checkpointer.list_workflows()                   # all workflows
workflows = await checkpointer.list_workflows(status=WorkflowStatus.FAILED)

# Fork from a point in history
checkpoint = await checkpointer.get_checkpoint("my-workflow", superstep=3)
result = await runner.run(graph, values={**checkpoint.values}, history=checkpoint.steps,
                          workflow_id="my-workflow-fork")
```

### NodeRecord fields (in-memory trace):

```python
@dataclass(frozen=True)
class NodeRecord:
    node_name: str                                 # "embed", "llm_call"
    superstep: int                                 # 0, 1, 2, ...
    duration_ms: float                             # wall-clock ms
    status: Literal["completed", "failed", "cached", "skipped"]
    error: str | None = None                       # error message if failed
    cached: bool = False                           # was this a cache hit?
    decision: str | tuple[str, ...] | None = None  # gate routing decision
```

### StepRecord fields (persistent trace — superset of NodeRecord):

```python
@dataclass(frozen=True)
class StepRecord:
    # Identity
    workflow_id: str
    superstep: int
    node_name: str
    index: int                           # unique sequential ID

    # Execution
    status: StepStatus                   # COMPLETED, FAILED, PAUSED, STOPPED
    input_versions: dict[str, int]       # for staleness detection
    values: dict[str, Any] | None        # node output values
    error: str | None
    pause: PauseInfo | None

    # Trace (NEW — the 3 fields that make StepRecord trace-complete)
    duration_ms: float | None = None     # wall-clock execution time
    cached: bool = False                 # cache hit?
    decision: str | tuple[str, ...] | None = None  # gate decision

    # Metadata
    partial: bool = False
    created_at: datetime
    completed_at: datetime | None
    child_workflow_id: str | None        # for nested graphs
```

---

## Before / After Summary

| Capability | Before | After |
|------------|--------|-------|
| "Why was this slow?" | Write custom EventProcessor, set up before run | `result.log.summary()` — always available |
| "Which node failed?" | Catch exception, check `result.error` | `result.log.errors` — per-node with context |
| "What path did the gate take?" | Custom processor for RouteDecisionEvent | `step.decision` on the trace step |
| "What prompt went into the LLM?" | No record of intermediate values | `checkpointer.get_state("wf", superstep=2)` → see exact inputs |
| "Inspect yesterday's run" | Impossible | `checkpointer.get_steps("workflow-id")` |
| "State at step 3" | Impossible | `checkpointer.get_state("wf", superstep=3)` |
| "All failed workflows" | Build your own tracking | `checkpointer.list_workflows(status=FAILED)` |
| "AI agent reads execution data" | Parse unstructured logs | `result.log.to_dict()` or `get_steps()` |
| "Retry from step 3" | Re-run everything or use DiskCache | `get_checkpoint(superstep=3)` → fork |

---

---

# Implementation

Everything above is the *what* and *why*. Below is the *how*.

## Phase 1: In-Memory RunLog (always-on, zero-config)

### New Types

Add to `src/hypergraph/runners/_shared/types.py`:

- **`NodeRecord`** — frozen dataclass, fields shown above
- **`NodeStats`** — mutable aggregate with `count`, `total_ms`, `errors`, `cached`, `avg_ms` property
- **`RunLog`** — holds `tuple[NodeRecord, ...]`, computed properties for `node_stats`, `errors`, `timing`, `summary()`, `to_dict()`, plus rich display methods (see below)

Add `log: RunLog | None = None` field to **`RunResult`**.

### Collection Mechanism: `_RunLogCollector`

New file: `src/hypergraph/runners/_shared/run_log.py`

A `TypedEventProcessor` that passively listens to events already emitted during execution:

| Event | Collector Action |
|-------|-----------------|
| `RunStartEvent` | Record start timestamp |
| `NodeEndEvent` | Append `NodeRecord` (completed/cached) |
| `NodeErrorEvent` | Append `NodeRecord` (failed) |
| `RouteDecisionEvent` | Attach `decision` to most recent record for that node |

The collector is **always prepended** to the dispatcher's processor list — even when no user processors exist. This means `dispatcher.active` must be `True` whenever the collector is present.

**Superstep tracking**: The runner's main execution loop already has a superstep counter. Before each superstep, the runner calls `collector.set_superstep(i, node_names)` so the collector can tag records with the correct superstep number.

**Build**: After execution completes, the runner calls `collector.build(graph_name, run_id, total_duration_ms)` to produce the immutable `RunLog`.

### Runner Integration

In both `_execute_graph_impl` (sync) and `_execute_graph_impl_async` (async):

1. Create `_RunLogCollector` at start
2. Prepend to processor list before creating dispatcher
3. Call `collector.set_superstep()` before each superstep iteration
4. After execution: `result.log = collector.build(...)`

**Map support**: Each map item creates its own collector → its own `RunLog` on its `RunResult`. No aggregate needed.

### Display & Formatting

RunLog, Workflow, and list[Workflow] all have rich display out of the box. No manual iteration needed.

**Three display contexts:**

| Context | Method | Format |
|---------|--------|--------|
| Terminal / `print()` | `__str__` / `__repr__` | Aligned text table with unicode box-drawing |
| Jupyter notebook | `_repr_html_` | HTML table with styling |
| AI agent / CI | `to_dict()` | JSON-serializable dict |

**RunLog display (`__str__`):**
```
RunLog: rag_pipeline | 2.6s | 3 nodes | 0 errors

  Step  Node              Duration  Status     Decision
  ────  ────────────────  ────────  ─────────  ─────────────────
     0  classify            120ms   completed  → account_support
     1  account_support    2400ms   completed
     2  format_response      45ms   completed
```

**RunLog display with errors:**
```
RunLog: rag_pipeline | 1.2s | 3 nodes | 1 error

  Step  Node       Duration  Status
  ────  ─────────  ────────  ──────────────────────────────────
     0  embed        180ms   completed
     1  llm_call         —   FAILED: 504 Gateway Timeout
```

**RunLog display with stats (map runs — many executions per node):**
```
RunLog: rag_pipeline | 8m12s | 4 nodes | 0 errors

  Node        Runs   Total    Avg     Errors  Cached
  ──────────  ─────  ───────  ──────  ──────  ──────
  embed         50    9.0s    180ms        0       0
  llm_call      50   7m48s   9360ms        0       0
  format        50    2.4s     48ms        0       0
  validate      50    0.7s     14ms        0       0
```

**Adaptive layout**: When each node executes once (simple DAG), show the step-by-step view. When nodes execute multiple times (map or cycles), show the aggregated stats view. `result.log` picks the right layout automatically.

**Workflow display (`__str__`):**
```
Workflow: batch-2024-01-16 | FAILED | 8 steps | 2m10s

  Step  Node          Duration  Status
  ────  ────────────  ────────  ─────────────────────────────────
     0  embed           180ms   completed
     1  retrieve        820ms   completed
     2  llm_call            —   FAILED: 503 Service Unavailable
```

**Implementation**: The formatting logic lives in the `__str__` method of each type. Column widths are computed dynamically from data. Duration formatting uses human-readable units (`120ms`, `2.4s`, `4m32s`). The `Decision` column is omitted when no steps have routing decisions (keep it clean).

### Files Changed

| File | Change |
|------|--------|
| `runners/_shared/types.py` | Add `NodeRecord`, `NodeStats`, `RunLog`. Add `log` field to `RunResult`. |
| `runners/_shared/run_log.py` | New: `_RunLogCollector` |
| `runners/async_/runner.py` | Create collector, integrate into execution loop |
| `runners/sync/runner.py` | Same |
| `events/dispatcher.py` | Stay active when internal collector is present |
| `__init__.py` | Export `RunLog`, `NodeRecord`, `NodeStats` |

**Not changing**: Event types, superstep execution, node executors, cache, graph construction, validation.

---

## Phase 2: Extend StepRecord Spec with Trace Fields

Add three fields to the `StepRecord` definition in specs:

| Field | Type | Why |
|-------|------|-----|
| `duration_ms` | `float \| None` | Explicit timing beats deriving from nullable timestamps. Enables queries like "steps > 5s". |
| `cached` | `bool` | Distinguishes cache hits from genuinely fast nodes (`duration_ms ≈ 0` is ambiguous). |
| `decision` | `str \| tuple[str, ...] \| None` | Without it, can't reconstruct "why did execution take this path?" from step records alone. |

**Spec-only change.** No code — StepRecord doesn't exist in code yet. These fields ship with the initial checkpointer implementation.

### Files Changed

| File | Change |
|------|--------|
| `specs/reviewed/execution-types.md` | Add 3 fields to StepRecord definition |
| `specs/reviewed/checkpointer.md` | Document new fields |

---

## Phase 3: SqliteCheckpointer

### New Package: `src/hypergraph/checkpointers/`

| File | Contents |
|------|----------|
| `__init__.py` | Package exports |
| `base.py` | `Checkpointer` ABC, `CheckpointPolicy` |
| `types.py` | `StepRecord`, `StepStatus`, `Workflow`, `WorkflowStatus`, `Checkpoint` |
| `sqlite.py` | `SqliteCheckpointer` |
| `serializers.py` | `Serializer` ABC, `JsonSerializer`, `PickleSerializer` |

### SqliteCheckpointer Internals

- **Backend**: `aiosqlite` (async SQLite)
- **Tables**: `workflows` (id, status, graph_hash, created_at, completed_at), `steps` (all StepRecord fields)
- **Unique constraint**: `(workflow_id, superstep, node_name)` for upsert semantics
- **`save_step()`**: Single INSERT with upsert
- **`get_state()`**: Fold steps through superstep. Materialized `latest_values` index for fast latest-state queries.
- **`get_steps()`**: `SELECT ... WHERE workflow_id = ? ORDER BY index`
- **Serialization**: JSON by default, pluggable `Serializer` for complex types

### Runner Integration

The runner builds a `StepRecord` from data already available after each node execution:

| StepRecord field | Source |
|-----------------|--------|
| `workflow_id` | From `runner.run(workflow_id=...)` |
| `superstep` | Loop counter in `_execute_graph_impl` |
| `node_name` | `node.name` |
| `index` | Auto-incrementing counter |
| `status` | Success/failure/pause of the node |
| `input_versions` | Already captured in `NodeExecution` |
| `values` | Already captured in `NodeExecution.outputs` |
| `duration_ms` | From `NodeEndEvent` timing |
| `cached` | From cache hit check |
| `decision` | From `GraphState.routing_decisions` |
| `error` | From caught exception |
| `created_at/completed_at` | Timestamps around node execution |

**StepRecord construction site**: Inside `run_superstep_async()` / `run_superstep_sync()`, right after the existing `NodeExecution` recording. All data is already there — the only new work is packaging it and calling `save_step()`.

**Durability modes** (from CheckpointPolicy):
- `"sync"`: `await save_step()` — block until written
- `"async"`: Fire in background task
- `"exit"`: Batch all records, write at run completion

### Additional Runner Changes

| File | Change |
|------|--------|
| `runners/base.py` | Add `workflow_id` to `run()` signature |
| `runners/async_/runner.py` | Accept checkpointer, create workflows, build+save StepRecords |
| `runners/async_/superstep.py` | Return timing/cached/decision alongside state |
| `runners/_shared/types.py` | Add `supports_checkpointing` to `RunnerCapabilities` |

**SyncRunner**: Gets RunLog (Phase 1) but NOT checkpointing — per existing spec, SyncRunner uses DiskCache for durability.

---

## Design Decisions

### 1. RunLog and Checkpointer are independent
RunLog works without a checkpointer. Checkpointer works without RunLog. Together they cover all use cases. Matches LangGraph's separation of checkpoints from traces.

### 2. StepRecord is the superset of NodeRecord
Same execution data, different fidelity. NodeRecord = timing metadata. StepRecord = timing + values + everything else.

### 3. No new abstractions
The v4 plan proposed `TraceCollector`, `ExecutionTrace`, `StepTrace`. All eliminated. RunLog + StepRecord cover everything with fewer types.

### 4. The checkpointer IS the trace store
`get_steps()` is the trace query. `get_state(superstep=N)` is time travel. No separate trace/debug API. Follows Hatchet's "persistence and observability are the same layer."

### 5. Events remain best-effort (Core Belief #9)
`_RunLogCollector` is a passive listener. The checkpointer is called explicitly by the runner, not through the event system.

### 6. SyncRunner gets RunLog but not Checkpointer
Per spec: SyncRunner uses cache for durability. But timing metadata is always useful.

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| RunLog overhead | O(nodes) list appends — negligible. Same cost as existing `NodeExecution`. |
| Dispatcher always-active | Collector is lightweight. Optimize: activate event emission only, not full processor pipeline. |
| StepRecord serialization failures | JSON default + pluggable Serializer. Warn + skip value on failure (never crash execution). |
| Phase 3 scope creep | Deliver SQLite only. Resist Postgres/Redis until SQLite is solid. |
| Large StepRecord values | Inherent to persistence. Document limits. Future: ArtifactRef (store blob elsewhere, pass pointer). |

---

## Deferred

- **PostgresCheckpointer** — separate effort
- **MapResult aggregate** — not needed for v1
- **Value capture in RunLog** — use checkpointer for values
- **Visual overlay** — coloring viz nodes by timing/status
- **MCP server** — AI agent interface wrapping checkpointer
- **DBOS alignment** — DBOSAsyncRunner has its own persistence
- **Event field extensions** — events stay lightweight; StepRecord stores values
