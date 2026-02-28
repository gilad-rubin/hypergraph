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

The use cases above reveal three independent axes, not two:

| Axis | Purpose | Data | Config | Backend examples |
|------|---------|------|--------|------------------|
| **In-process observability** | "Why was this run slow?" | Timing, status, routing | None (always on) | RunLog |
| **External observability** | "Show me in Logfire/Datadog" | Same, streamed to external tool | EventProcessor | Logfire, Datadog, Jaeger, custom |
| **Durability + queryable history** | "What happened yesterday?" / resume | Full I/O + timing + status | Checkpointer | SQLite (built-in), DBOS, Postgres |

**These are independent.** Users opt into each axis separately:

```python
# 1. Observability only — no persistence, no external tools
runner = AsyncRunner()
result = await runner.run(graph, values={...})
print(result.log)  # RunLog always works. Zero config.

# 2. Add external observability — stream to Logfire/OTel
runner = AsyncRunner(processors=[OpenTelemetryProcessor()])
# Events stream to your OTel backend. RunLog still works.

# 3. Add durability — our CLI + resume + crash recovery
runner = AsyncRunner(checkpointer=SqliteCheckpointer("./db"))
# CLI works, resume works. No external dashboard.

# 4. All three — full stack
runner = AsyncRunner(
    checkpointer=SqliteCheckpointer("./db"),
    processors=[OpenTelemetryProcessor()],
)
# RunLog + OTel export + CLI + resume. Everything.

# 5. DBOS — brings its own durability + observability
runner = DBOSAsyncRunner()
# DBOS handles persistence + its own dashboard.
# RunLog still works. Our CLI does NOT (data is in DBOS, not our checkpointer).
```

**Architecture — the three axes:**

```
Runner
  │
  ├── Events ──► RunLog (in-memory, always-on)           ← Axis 1
  │         └──► EventProcessor[]                         ← Axis 2
  │                ├── OpenTelemetryProcessor (opt-in)
  │                ├── RichProgressProcessor
  │                └── Custom processors
  │
  └── Steps ──► Checkpointer (persistent, opt-in)        ← Axis 3
                  ├── SqliteCheckpointer (built-in)
                  ├── DBOS (via DBOSAsyncRunner)
                  └── Custom backends
```

### OTel Compatibility: By Construction, Not By Dependency

Our events and types are **OTel-compatible by design** — they use `span_id`, `parent_span_id`, `duration_ms`, `cached`, and a span hierarchy that maps directly to OTel's trace model. But we do NOT depend on the OTel SDK. This is deliberate:

- **Zero deps for the common case.** `pip install hypergraph` gives you RunLog + Checkpointer with no telemetry baggage.
- **Opt-in OTel export.** `pip install hypergraph[otel]` adds the OTel SDK and enables `OpenTelemetryProcessor` — a thin mapping layer that converts our events to OTel spans.
- **Any OTel backend.** Once exported, traces flow to Logfire, Jaeger, Datadog, Honeycomb — whatever the user configures in their OTel collector.

The mapping is trivial because our data model was designed for it:

| hypergraph | OTel Span |
|------------|-----------|
| `span_id` | `span_id` |
| `parent_span_id` | `parent_span_id` |
| `NodeStartEvent` | span start |
| `NodeEndEvent` | span end |
| `duration_ms` | span duration |
| `cached`, `node_name`, `decision` | span attributes |
| `RouteDecisionEvent` | span event (annotation) |

**StepRecord also carries `span_id`** so users can correlate checkpointer data (values, intermediate state) with OTel traces in their external dashboard. The span_id is the join key between "what happened" (OTel) and "what values flowed" (checkpointer).

### Why Durability and Debugging Share the Checkpointer

The plan's CLI (`hypergraph workflows show/state/steps`) queries the Checkpointer. This means the debugging story and the durability story share one backend. This is intentional:

1. **The data is identical.** What you need for debugging (intermediate values, step history, timing) IS what StepRecord stores for resume. A separate trace store would duplicate it.
2. **External tools have their own dashboards.** Logfire users query Logfire. Datadog users query Datadog. Our CLI serves users who DON'T want an external platform.
3. **Same backend, two read patterns.** `save_step()` writes for resume. `get_steps()` reads for debugging. Same data, different consumers.

**What if you use DBOS for durability?** Then DBOS provides its own debugging tools. Our CLI won't see that data (it queries our Checkpointer). RunLog still works in-process. If you also want OTel, add an `OpenTelemetryProcessor` alongside the DBOS runner.

### Why NOT Just Use OTel For Everything?

| Concern | Why custom is better |
|---------|---------------------|
| **Always-on, zero config** | OTel SDK requires setup (provider, exporter, sampler). RunLog works on `pip install`. |
| **Framework-aware data** | Routing decisions, superstep structure, seed values — OTel doesn't model these natively. They'd be flattened to string attributes. |
| **Bidirectional persistence** | OTel is write-only (export spans). Checkpointer is bidirectional (load state for resume). |
| **Dependency weight** | `opentelemetry-api` + `opentelemetry-sdk` is significant. Not everyone wants it. |
| **Offline / local-first** | SQLite checkpointer works without any network. OTel backends typically need a collector. |

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
    span_id: str                                   # correlates with OTel traces
    error: str | None = None                       # error message if failed
    cached: bool = False                           # was this a cache hit?
    decision: str | list[str] | None = None  # gate routing decision (matches spec type)
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

    # Trace (NEW — 3 fields that make StepRecord trace-complete)
    duration_ms: float | None = None     # wall-clock execution time
    cached: bool = False                 # cache hit?
    span_id: str | None = None           # join key to OTel traces
    # decision already exists in spec: str | list[str] | None

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

## Current Codebase State (Grounding)

Before implementation, it's important to know what exists and what doesn't:

| What | Status | Where |
|------|--------|-------|
| `RunResult.workflow_id` field | Exists (optional, defaults to None) | `runners/_shared/types.py:162` |
| `workflow_id` in runner `run()`/`map()`/`iter()` signatures | **Does NOT exist** | `runners/base.py:39` — must be added |
| `workflow_id` population in templates | **Not populated** | `template_async.py:197` — always None |
| `NodeEndEvent.duration_ms` | Exists | `events/types.py` |
| `NodeEndEvent.cached` | Exists | `events/types.py` |
| `RouteDecisionEvent.decision` | Exists | `events/types.py` |
| `StepRecord` in code | **Does NOT exist** | Spec-only (`specs/reviewed/execution-types.md`) |
| `Checkpointer` in code | **Does NOT exist** | Spec-only (`specs/reviewed/checkpointer.md`) |
| `src/hypergraph/checkpointers/` | **Does NOT exist** | Must be created |
| CLI entry point | **Does NOT exist** | No `[project.scripts]` in `pyproject.toml` |
| `RunnerCapabilities.supports_checkpointing` | **Does NOT exist** | Must be added |

**Implication**: Phase 1 (RunLog) is self-contained. Phase 3 (Checkpointer) requires adding `workflow_id` to runner signatures first — this is Phase 2.5 work (see below).

---

## Phase 0: Prerequisites (before any implementation)

Before starting Phase 1, verify these baseline assumptions with targeted tests:

1. **Event ordering test**: Write a test confirming `RouteDecisionEvent` fires before `NodeEndEvent` (this is the ordering the collector depends on).
2. **Dispatcher always-active**: Verify that prepending an internal processor activates the dispatcher even when no user processors exist.
3. **NodeExecution data availability**: Confirm all data needed for NodeRecord (timing, cached flag, etc.) is accessible at the points where the collector will consume events.

These are 3-5 small tests, not a large migration. They catch any assumption drift before building on it.

---

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
| `RouteDecisionEvent` | **Buffer** decision keyed by `(node_name, superstep)` |
| `NodeEndEvent` | Append `NodeRecord` (completed/cached), apply any buffered decision |
| `NodeErrorEvent` | Append `NodeRecord` (failed) |

**Event ordering note**: In the current codebase, `RouteDecisionEvent` is emitted *before* `NodeEndEvent` (see `sync/superstep.py:111-114`, `async_/superstep.py:144-147`). The collector must buffer decisions and apply them when the corresponding `NodeEndEvent` arrives. Keying by `(node_name, superstep)` ensures correct correlation even with concurrent node execution.

The collector is **always prepended** to the dispatcher's processor list — even when no user processors exist. This means `dispatcher.active` must be `True` whenever the collector is present.

**Superstep tracking**: The runner's main execution loop already has a superstep counter. Before each superstep, the runner calls `collector.set_superstep(i, node_names)` so the collector can tag records with the correct superstep number.

**Build**: After execution completes, the runner calls `collector.build(graph_name, run_id, total_duration_ms)` to produce the immutable `RunLog`.

### Runner Integration

**Applies to all execution methods**: `run()`, `map()`, and in the future `iter()`. Every `RunResult` has a `.log` — this is not a map-specific feature.

In both `_execute_graph_impl` (sync) and `_execute_graph_impl_async` (async):

1. Create `_RunLogCollector` at start
2. Prepend to processor list before creating dispatcher
3. Call `collector.set_superstep()` before each superstep iteration
4. After execution: `result.log = collector.build(...)`

**Per-method behavior**:
- **`run()`**: Single RunLog on the single RunResult. This is the primary use case.
- **`map()`**: Each map item gets its own collector → its own RunLog on its RunResult.
- **`iter()` (future)**: RunLog builds incrementally, available on the final RunResult.

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

### Phase 1b: Built-in OpenTelemetryProcessor (opt-in)

Ship an `OpenTelemetryProcessor` as a built-in EventProcessor behind an optional dependency:

```bash
pip install hypergraph[otel]  # adds opentelemetry-api + opentelemetry-sdk
```

```python
from hypergraph.events.otel import OpenTelemetryProcessor

runner = AsyncRunner(processors=[OpenTelemetryProcessor()])
```

The processor is a thin mapping layer (~50 lines) that converts our events to OTel spans. It already exists as a spec example in `observability.md` — this promotes it to real, tested code.

| File | Contents |
|------|----------|
| `events/otel.py` | `OpenTelemetryProcessor` — converts events to OTel spans |
| `pyproject.toml` | Add `[project.optional-dependencies] otel = ["opentelemetry-api", "opentelemetry-sdk"]` |

**Not a hard dependency.** Import is guarded: if `opentelemetry` isn't installed, importing `OpenTelemetryProcessor` raises a clear error with install instructions.

---

## Phase 2: Extend StepRecord Spec with Trace Fields

Add three fields to the `StepRecord` definition in specs:

| Field | Type | Why |
|-------|------|-----|
| `duration_ms` | `float \| None` | Explicit timing beats deriving from nullable timestamps. Enables queries like "steps > 5s". |
| `cached` | `bool` | Distinguishes cache hits from genuinely fast nodes (`duration_ms ≈ 0` is ambiguous). |
| `span_id` | `str \| None` | Join key to OTel traces. Enables: "show me the Logfire trace for the step that failed" by correlating checkpointer data with external observability. Copied from the `NodeEndEvent.span_id` at save time. |

**`decision` already exists.** StepRecord in `specs/reviewed/execution-types.md` already defines `decision: str | list[str] | None` (see line ~1428). This field is used for deterministic gate replay on resume. We reuse it as-is for trace/debugging — no changes needed.

**Type alignment note**: The spec uses `str | list[str] | None` for `decision`. NodeRecord (in-memory) should match this type exactly — NOT `tuple[str, ...]`.

**Spec-only change.** No code — StepRecord doesn't exist in code yet. These fields ship with the initial checkpointer implementation.

### Files Changed

| File | Change |
|------|--------|
| `specs/reviewed/execution-types.md` | Add `duration_ms`, `cached`, and `span_id` to StepRecord definition |
| `specs/reviewed/checkpointer.md` | Document new fields |

---

## Phase 2.5: Runner API Migration (prerequisite for Phase 3)

Add `workflow_id` to runner signatures and wire it through to `RunResult`:

| File | Change |
|------|--------|
| `runners/base.py` | Add `workflow_id: str | None = None` to `run()`, `map()`, and `iter()` signatures |
| `runners/_shared/template_async.py` | Accept and propagate `workflow_id`, populate on `RunResult` |
| `runners/_shared/template_sync.py` | Same |
| `runners/_shared/types.py` | Add `supports_checkpointing: bool = False` to `RunnerCapabilities` |

This is a **non-breaking, additive change** — `workflow_id` defaults to `None`, existing code continues to work. Tests should verify that `RunResult.workflow_id` is populated when provided.

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
| `index` | Deterministic: `(superstep, graph-constructor-order)`. Within a superstep, nodes are ordered by their position in the original `Graph([...])` constructor, not by task completion time. This ensures index is stable across reruns regardless of async timing. |
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
| RunLog overhead | O(nodes) per run — same cost as existing `NodeExecution`. For map operations, each item gets its own RunLog. At extreme scale (1M+ items via `map_mode="product"`), consider `RunLog(aggregate_only=True)` mode that stores only `NodeStats` (counts/timing) without individual `NodeRecord` entries. Default: full records. |
| Dispatcher always-active | Collector is lightweight. Optimize: activate event emission only, not full processor pipeline. |
| StepRecord serialization failures | JSON default + pluggable Serializer. **Fail the step on serialization error** — per spec, checkpointer = full durability, always (see `durable-execution.md:59-109`). Skipping values would corrupt resume semantics. Users must ensure outputs are serializable when using a checkpointer. |
| Phase 3 scope creep | Deliver SQLite only. Resist Postgres/Redis until SQLite is solid. |
| Large StepRecord values | Inherent to persistence. Document limits. Future: ArtifactRef (store blob elsewhere, pass pointer). |

---

## Phase 4: CLI — AI Agent-Friendly Debugging Interface

### Motivation

The SDK is powerful, but agents and CLI scripts shouldn't need to write Python to answer "what failed?" Every use case from UC1–UC8 should be a one-liner from the terminal.

**Cross-framework landscape:**

| Tool | CLI Query? | Agent-Friendly? | Filter Power |
|------|:----------:|:---------------:|:------------:|
| Temporal | `temporal workflow list --query "..."` | `--output json` | SQL-like (best) |
| Prefect | `prefect flow-run ls --state Failed` | `--output json` | Flags only |
| LangSmith | `langsmith-fetch traces` (time/count only) | MCP server | SDK only |
| Hatchet | TUI only | No | SDK only |
| **Hypergraph** | **`hypergraph workflows ls --status failed`** | **`--json` + MCP** | **Flags + `--where`** |

**Hypergraph's unique advantage**: We know our own graph topology. We can filter by node name, node type (gate/function), branch taken, duration — dimensions that don't exist in any other framework's CLI.

### Command Structure

**v1 (ship with checkpointer):**
```
hypergraph
  workflows
    ls          List workflows (filtered)
    show        Show execution trace for a workflow
    state       Show accumulated values at a point
    steps       Show step records (detailed)
  graph
    inspect     Show graph structure (nodes, edges, inputs)
```

**v2 (deferred — after v1 is solid):**
```
hypergraph
  workflows
    replay      Replay a workflow from a specific point
    diff        Compare two workflow executions
    prune       Delete old workflow records
```

**Deferred from CLI v1**: `--where` DSL (node-level filtering), `replay`, `diff`, `prune`. These are powerful but each involves significant design decisions. Ship `ls/show/state/steps/inspect` first with basic flags (`--status`, `--since`, `--json`), validate with real usage, then add power features.

### JSON Output Contract

All `--json` output includes a stable envelope:

```json
{
  "schema_version": 1,
  "command": "workflows.ls",
  "generated_at": "2024-01-16T09:00:00Z",
  "data": [...]
}
```

This ensures agents can detect format changes across CLI versions. `schema_version` is bumped on breaking changes to the JSON structure. Non-breaking additions (new fields) don't bump the version.

### Command Reference

#### `hypergraph workflows ls` — List workflows

```bash
# All workflows
hypergraph workflows ls

# Workflows (3 total)
#
#   ID                  Status     Steps  Duration  Created
#   ──────────────────  ─────────  ─────  ────────  ───────────────────
#   batch-2024-01-15    completed     12    4m32s   2024-01-15 09:00
#   batch-2024-01-16    FAILED         8    2m10s   2024-01-16 09:00
#   chat-session-42     active        24   12m05s   2024-01-16 14:30

# Filter by status
hypergraph workflows ls --status failed
hypergraph workflows ls --status active --status completed

# Filter by time
hypergraph workflows ls --since "2024-01-15"
hypergraph workflows ls --since "1h ago"
hypergraph workflows ls --since "yesterday"

# Filter by node behavior (v2 — unique to hypergraph!)
# hypergraph workflows ls --where "node:llm_call status=failed"
# hypergraph workflows ls --where "node:classify decision=account_support"
# hypergraph workflows ls --where "duration > 5m"

# Combine filters (v1)
hypergraph workflows ls --status failed --since "24h ago"

# JSON output for agents
hypergraph workflows ls --json
hypergraph workflows ls --status failed --json | jq '.[].id'

# Limit
hypergraph workflows ls --limit 10
```

**Flags:**

| Flag | Type | Description |
|------|------|-------------|
| `--status` | `completed\|failed\|active` | Filter by workflow status (repeatable) |
| `--since` | datetime or relative | Only workflows created after this time |
| `--until` | datetime or relative | Only workflows created before this time |
| `--where` | string | Node-level filter — **v2** (see below) |
| `--limit` | int | Max results (default: 50) |
| `--parent` | string | Only child workflows of this parent ID |
| `--json` | flag | JSON output for piping/agents |
| `--db` | path | Database path (default: `./workflows.db`) |

**`--where` syntax** (v2 — node-level filter, deferred from v1):

```bash
# Filter by node name and outcome
--where "node:llm_call status=failed"      # Any workflow where llm_call failed
--where "node:classify decision=detailed"  # Where classify routed to 'detailed'
--where "node:embed duration > 1s"         # Where embed took over 1 second
--where "node:* cached=true"               # Any cached node
--where "duration > 5m"                    # Total workflow duration
--where "steps > 20"                       # Workflows with many steps
```

---

#### `hypergraph workflows show <id>` — Execution trace

Maps to **UC1** (slow runs), **UC2** (failures), **UC3** (routing).

```bash
hypergraph workflows show batch-2024-01-16

# Workflow: batch-2024-01-16 | FAILED | 8 steps | 2m10s
#
#   Step  Node          Duration  Status     Decision
#   ────  ────────────  ────────  ─────────  ────────────────────────────
#      0  embed           180ms   completed
#      1  retrieve        820ms   completed
#      2  classify        120ms   completed  → detailed_answer
#      3  build_prompt     12ms   completed
#      4  generate            —   FAILED: 503 Service Unavailable

# JSON for agents
hypergraph workflows show batch-2024-01-16 --json

# Show only errors
hypergraph workflows show batch-2024-01-16 --errors

# Show specific superstep range
hypergraph workflows show batch-2024-01-16 --superstep 2..4
```

**Flags:**

| Flag | Type | Description |
|------|------|-------------|
| `--json` | flag | JSON output |
| `--errors` | flag | Only show failed steps |
| `--superstep` | `N` or `N..M` | Filter to superstep range |
| `--node` | string | Filter to specific node name |
| `--tree` | flag | Expand nested workflows inline |

---

#### `hypergraph workflows state <id>` — Intermediate values

Maps to **UC4** (intermediate inspection) and **UC5** (yesterday's run).

```bash
# Full state (latest)
hypergraph workflows state batch-2024-01-16

# State: batch-2024-01-16 (through superstep 4)
#
#   Output           Type    Size     Superstep  Node
#   ───────────────  ──────  ───────  ─────────  ────────────
#   embedding        list    1536     0          embed
#   retrieved_docs   list    3 items  1          retrieve
#   category         str     16B      2          classify
#   prompt           str     2.4KB    3          build_prompt
#   answer           —       —        4          generate (FAILED)

# State at a specific superstep (time travel!)
hypergraph workflows state batch-2024-01-16 --superstep 2

# Show actual values (not just summary — for debugging)
hypergraph workflows state batch-2024-01-16 --superstep 2 --values

# State: batch-2024-01-16 (through superstep 2)
#
#   embedding: [0.123, -0.456, 0.789, ...] (1536 floats)
#   retrieved_docs:
#     [0]: {"title": "Password Reset Guide", "content": "To reset your passw..."}
#     [1]: {"title": "Account Recovery", "content": "If you've lost access..."}
#     [2]: {"title": "Security FAQ", "content": "We recommend changing..."}
#   category: "account_support"

# Single value
hypergraph workflows state batch-2024-01-16 --superstep 2 --key prompt
# "Based on the following documents:\n1. Password Reset Guide..."

# JSON output (agent gets full values)
hypergraph workflows state batch-2024-01-16 --superstep 2 --json
```

**Flags:**

| Flag | Type | Description |
|------|------|-------------|
| `--superstep` | int | State through this superstep (default: latest) |
| `--values` | flag | Show actual values, not just type/size summary |
| `--key` | string | Show single output value |
| `--json` | flag | JSON output with full values |

---

#### `hypergraph workflows steps <id>` — Step records (detailed)

For deep debugging — shows the full StepRecord data including input versions, timing, values.

```bash
hypergraph workflows steps batch-2024-01-16

# Step [0] embed | completed | 180ms
#   superstep: 0
#   input_versions: {query: 1}
#   values: {embedding: <list, 1536 items>}
#   cached: false
#   created_at: 2024-01-16 09:00:01.234
#   completed_at: 2024-01-16 09:00:01.414
#
# Step [1] retrieve | completed | 820ms
#   superstep: 1
#   input_versions: {embedding: 1, top_k: 1}
#   values: {retrieved_docs: <list, 3 items>}
#   ...

# Single step by node name
hypergraph workflows steps batch-2024-01-16 --node retrieve

# JSON for full programmatic access
hypergraph workflows steps batch-2024-01-16 --json
```

---

#### `hypergraph workflows replay <id>` — Replay from a point

Maps to **UC8** (fork and retry).

```bash
# Replay from superstep 3 into a new workflow
hypergraph workflows replay batch-2024-01-16 \
  --from-superstep 3 \
  --new-id batch-2024-01-16-retry

# Replaying batch-2024-01-16 from superstep 3...
# Created new workflow: batch-2024-01-16-retry
# Reusing 3 completed steps, re-executing from superstep 3
#
# To run: await runner.run(graph, workflow_id="batch-2024-01-16-retry")

# Replay and immediately run (requires graph module path)
hypergraph workflows replay batch-2024-01-16 \
  --from-superstep 3 \
  --new-id batch-2024-01-16-retry \
  --run my_module:graph
```

---

#### `hypergraph workflows diff <id1> <id2>` — Compare executions

For debugging regressions — compare two runs of the same graph.

```bash
hypergraph workflows diff batch-2024-01-15 batch-2024-01-16

# Diff: batch-2024-01-15 vs batch-2024-01-16
#
#   Node          Run 1       Run 2       Delta
#   ────────────  ──────────  ──────────  ─────────────────
#   embed          180ms       180ms      same
#   retrieve       420ms       820ms      +95% ⚠
#   classify       115ms       120ms      same
#   build_prompt    12ms        12ms      same
#   generate      2800ms       FAILED     ← failure point
#
# Run 1: completed in 3.5s
# Run 2: FAILED at step 4 (generate)
```

---

#### `hypergraph graph inspect` — Graph structure

Not about execution — shows the graph's static structure. Useful for agents understanding what they're debugging.

```bash
hypergraph graph inspect my_module:graph

# Graph: rag_pipeline | 5 nodes | 6 edges
#
#   Node              Type      Inputs              Outputs
#   ────────────────  ────────  ──────────────────  ────────────
#   embed             function  query               embedding
#   retrieve          function  embedding, top_k    retrieved_docs
#   classify          route     query               category
#   build_prompt      function  retrieved_docs, q…  prompt
#   generate          function  prompt              answer
#
# Entrypoints: query
# Required inputs: query
# Optional inputs: top_k (default: 5)

# JSON for agents
hypergraph graph inspect my_module:graph --json
```

---

### Use Case → CLI Mapping

| Use Case | CLI Command |
|----------|-------------|
| UC1: "Why was my run slow?" | `hypergraph workflows show <id>` |
| UC2: "What failed and why?" | `hypergraph workflows show <id> --errors` |
| UC3: "What path did execution take?" | `hypergraph workflows show <id>` (Decision column) |
| UC4: "What prompt went into the LLM?" | `hypergraph workflows state <id> --superstep 2 --values` |
| UC5: "What happened yesterday?" | `hypergraph workflows ls --since yesterday` → `show` |
| UC6: "All failed workflows" | `hypergraph workflows ls --status failed` |
| UC7: AI agent debugging | Any command with `--json` piped to agent |
| UC8: Fork and retry | `hypergraph workflows replay <id> --from-superstep 3` **(v2)** |

### Nested Graphs — First-Class Support

Nested graphs (via `graph.as_node()`) are a core hypergraph feature. The CLI, RunLog, and checkpointer must handle them naturally.

**Workflow ID convention**: Parent `"order-123"`, child `"order-123/rag@s1@s1"` (where `s1` = superstep 1), grandchild `"order-123/rag@s1@s1/summarize@s0"`.

**Why the superstep suffix?** Without it, if a GraphNode executes multiple times (e.g., in a loop or map), both invocations would get the same workflow ID `"order-123/rag@s1"`, causing step history to overwrite/mix. The `@sN` suffix disambiguates by invocation context. For map operations within a GraphNode, the map index is also included: `"order-123/rag@s1@s1.i5"` (superstep 1, map item 5).

#### CLI: Nested workflow display

```bash
# Show parent — nested runs appear as single steps with child link
hypergraph workflows show order-123

# Workflow: order-123 | completed | 5 steps | 12.4s
#
#   Step  Node        Duration  Status     Child Workflow
#   ────  ──────────  ────────  ─────────  ─────────────────
#      0  preprocess    200ms   completed
#      1  rag          8200ms   completed  → order-123/rag@s1
#      2  postprocess   150ms   completed

# Drill into nested workflow — same commands, child ID
hypergraph workflows show order-123/rag@s1

# Workflow: order-123/rag@s1 | completed | 3 steps | 8.1s
#
#   Step  Node       Duration  Status
#   ────  ─────────  ────────  ─────────
#      0  embed        180ms   completed
#      1  retrieve     620ms   completed
#      2  generate    7300ms   completed

# Show full tree (all levels expanded)
hypergraph workflows show order-123 --tree

# Workflow: order-123 | completed | 12.4s
#
#   Step  Node                    Duration  Status
#   ────  ──────────────────────  ────────  ─────────
#      0  preprocess                200ms   completed
#      1  rag/                     8200ms   completed
#      1.0  rag/embed               180ms   completed
#      1.1  rag/retrieve            620ms   completed
#      1.2  rag/generate           7300ms   completed
#      2  postprocess               150ms   completed

# State of nested workflow
hypergraph workflows state order-123/rag@s1 --superstep 1 --values

# List child workflows
hypergraph workflows ls --parent order-123
```

**`--tree` flag**: Expands nested workflows inline. Without it, nested workflows show as collapsed single steps with a link to drill into. With it, the full tree is shown with indented node paths.

**`--parent` filter**: List only child workflows of a given parent. Useful for map operations where the parent spawns many children.

#### RunLog: Nested execution

```python
result = await runner.run(outer_graph, values={...})

# Parent log shows nested graphs as single entries
print(result.log)
# RunLog: outer | 12.4s | 3 nodes | 0 errors
#
#   Step  Node        Duration  Status     Child
#   ────  ──────────  ────────  ─────────  ────────────
#      0  preprocess    200ms   completed
#      1  rag          8200ms   completed  (3 substeps)
#      2  postprocess   150ms   completed

# Drill into nested RunLog
result["rag"].log
# RunLog: rag | 8.1s | 3 nodes | 0 errors
#
#   Step  Node       Duration  Status
#   ────  ─────────  ────────  ─────────
#      0  embed        180ms   completed
#      1  retrieve     620ms   completed
#      2  generate    7300ms   completed

# to_dict() includes nested logs
result.log.to_dict()
# {"steps": [..., {"node_name": "rag", "child_log": {"steps": [...]}}]}
```

#### Checkpointer: Nested persistence

```python
# Parent and child are separate workflows — standard checkpointer API
parent_steps = await checkpointer.get_steps("order-123")
child_steps = await checkpointer.get_steps("order-123/rag@s1")

# Parent state includes child outputs (flattened into parent namespace)
parent_state = await checkpointer.get_state("order-123")
parent_state["answer"]  # Output from rag/generate, surfaced to parent

# Child state is isolated
child_state = await checkpointer.get_state("order-123/rag@s1")
child_state["embedding"]  # Only visible inside rag
```

The key principle: **nested graphs are separate workflows with path-based IDs**. This is already in the persistence spec (`child_workflow_id` on StepRecord). The CLI and RunLog just need to surface the hierarchy naturally.

---

### Agent Workflow Example

An AI agent debugging a failing pipeline would do:

```bash
# 1. Find the failing workflow
hypergraph workflows ls --status failed --since "1h ago" --json

# 2. See what happened
hypergraph workflows show batch-2024-01-16 --json

# 3. Inspect intermediate values at the failure point
hypergraph workflows state batch-2024-01-16 --superstep 3 --json

# 4. Check the graph structure to understand the pipeline
hypergraph graph inspect my_module:graph --json

# 5. Get detailed step records for the failing node
hypergraph workflows steps batch-2024-01-16 --node generate --json
```

All commands return structured JSON with `--json` and a stable `schema_version` envelope. The agent never needs to parse human-formatted text.

**v2 additions for agents** (deferred):
```bash
# Compare with a successful run
hypergraph workflows diff batch-2024-01-15 batch-2024-01-16 --json

# Set up a replay
hypergraph workflows replay batch-2024-01-16 --from-superstep 3 --new-id retry-1
```

### Why CLI First (Not MCP)

The CLI is the universal agent interface. Every AI coding tool — Claude Code, Cursor, Windsurf, Copilot — can run shell commands. MCP requires per-tool integration and is becoming less differentiated as agents get better at CLI usage.

**The CLI IS the agent interface.** `--json` output + shell piping gives agents everything they need. An MCP server is a nice-to-have wrapper that can come later, and it would just call the same functions the CLI calls.

### Implementation Notes

- Built with `click` (already a dev dependency convention in Python CLIs)
- `--db` flag defaults to `./workflows.db` (SQLite) — can point to any checkpointer
- `--json` uses the same `to_dict()` methods from RunLog and StepRecord
- Human display uses the same formatting logic from RunLog/Workflow `__str__`
- Entry point: `hypergraph` command via `pyproject.toml` `[project.scripts]`

---

## Deferred

- **PostgresCheckpointer** — separate effort
- **MapResult aggregate** — not needed for v1
- **Value capture in RunLog** — use checkpointer for values
- **Visual overlay** — coloring viz nodes by timing/status
- **MCP server** — thin wrapper over the same functions the CLI calls. Low priority — CLI covers the agent use case.
- **DBOS alignment** — DBOSAsyncRunner has its own persistence
- **Event field extensions** — events stay lightweight; StepRecord stores values
- **CLI v2 commands** — `replay`, `diff`, `prune`, `--where` DSL (node-level filtering)
- **RunLog aggregate_only mode** — for extreme-scale map/product workloads (1M+ items), store only NodeStats without individual NodeRecords
- **CLI pagination** — `--limit`, `--cursor` for large output streaming (NDJSON)

---

## Review Log

### Round 1: Codex Review (gpt-5.3-codex, session 019ca0c5)

**VERDICT: REVISE** — 8 findings, all addressed below.

| # | Finding | Severity | Resolution |
|---|---------|----------|------------|
| 1 | RouteDecisionEvent fires BEFORE NodeEndEvent — collector would drop decisions | Bug | Fixed: buffer decisions keyed by `(node_name, superstep)`, apply on NodeEndEvent |
| 2 | "Warn + skip value on serialization failure" breaks durability guarantee | Bug | Fixed: fail step on serialization error (per spec: checkpointer = full durability) |
| 3 | Nested workflow ID collision when GraphNode runs multiple times | Bug | Fixed: include superstep suffix `@sN` in child workflow IDs |
| 4 | Step index ordering underspecified for async concurrency | Gap | Fixed: deterministic ordering by `(superstep, graph-constructor-order)` |
| 5 | Plan says StepRecord/decision don't exist — they do | Factual error | Fixed: Phase 2 scoped to `duration_ms` + `cached` only. Decision type aligned to `str \| list[str]` |
| 6 | No workflow_id in runner signatures yet | Expected | Added "Current Codebase State" grounding table |
| 7 | Memory risk understated for extreme map/product scale | Risk | Added aggregate_only mode note + deferred item |
| 8 | CLI v1 scope too broad | Scope | Split into v1 (`ls/show/state/steps/inspect`) and v2 (`replay/diff/prune/--where`). Added JSON schema versioning |

**Additional Codex suggestion considered**: Option B (direct runner instrumentation vs event-processor collector). Decided to keep event-processor approach (Option A) because: (a) with the event ordering fix it's correct, (b) it follows Core Belief #9 (observability decoupled from execution), (c) it's less invasive to runner code. The collector being a TypedEventProcessor is consistent with RichProgressProcessor pattern.

### Round 2: Codex Review (gpt-5.3-codex, session 019ca0c5)

**VERDICT: REVISE** — 5 consistency issues from incomplete example updates.

| # | Finding | Resolution |
|---|---------|------------|
| 1 | NodeRecord/StepRecord snippets still used `tuple[str, ...]` for decision | Fixed: all instances now use `str \| list[str] \| None` (matches spec) |
| 2 | Nested workflow ID examples still used unsuffixed `order-123/rag` | Fixed: all instances now use `order-123/rag@s1` |
| 3 | No explicit Phase 0 / migration sequence | Fixed: added Phase 0 (prerequisite tests) and Phase 2.5 (runner API migration) |
| 4 | Memory-risk mitigation advisory only | Acknowledged — deferred item is sufficient for plan scope |
| 5 | `--where` and `replay/diff` still shown as v1 | Fixed: `--where` examples marked as v2, UC8 marked `(v2)` |
