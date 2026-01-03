# Durable Execution

**From development save points to production-grade crash recovery.**

> **Type Reference:** For all type definitions (`RunResult`, `RunStatus`, `PauseReason`, `Workflow`, `Step`, etc.), see [Execution Types](execution-types.md). This document focuses on usage patterns.

---

## Overview

hypergraph provides two paths to durability:

1. **Built-in Checkpointer** — Manual resume, simple production, development
2. **DBOS Runners** — Automatic crash recovery, production infrastructure

Both paths keep your graph code **pure and portable**. The graph never imports durability-specific code.

```
┌─────────────────────────────────────────────────────────────────────┐
│  YOUR CODE                                                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  hypergraph LAYER                                           │   │
│  │                                                             │   │
│  │  @node, Graph, InterruptNode, generators (yield)           │   │
│  │                                                             │   │
│  │  → Pure, portable, runner-agnostic                         │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                              │                                      │
│                              ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  RUNNER + DURABILITY LAYER                                  │   │
│  │                                                             │   │
│  │  AsyncRunner + Checkpointer    OR    DBOSAsyncRunner       │   │
│  │  (manual resume)                     (automatic recovery)   │   │
│  │                                                             │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Design Principles

1. **Graph code stays pure** — No durability imports in nodes
2. **Explicit over implicit** — No magic defaults, user knows what they're opting into
3. **Declarative nodes, not ambient side effects** — `InterruptNode` in graph, not `interrupt()` in function
4. **Generators for streaming** — Use `yield` for progress and streaming, not special functions
5. **Clear upgrade path** — Start simple, add durability when needed
6. **Outputs ARE state** — No separate state schema; node outputs are the state (see [State Model](state-model.md))
7. **State flows through nodes** — No external mutation of checkpointed state. Use `InterruptNode` for human input, `fork_workflow` for retries. See [State Model FAQ](state-model.md#faq) for why hypergraph doesn't have `update_state()`.

---

## Persistence Model

When a checkpointer is present, **all outputs are persisted**. This is the only behavior — there is no selective persistence.

```python
graph = Graph(nodes=[embed, retrieve, generate])
runner = AsyncRunner(checkpointer=SqliteCheckpointer("./db"))
# All outputs checkpointed automatically
```

### What Gets Saved

| What | Saved? | Purpose |
|------|:------:|---------|
| Step metadata (index, node_name, status) | Always | Implicit cursor for cycles/branches |
| StepResult.outputs | Always | Value recovery on resume |

Step history serves as the **implicit cursor** for resumption:
- **Cycles:** Step count tracks iteration number
- **Branches:** Steps show which branch was taken

See [Step History as Implicit Cursor](execution-types.md#step-history-as-implicit-cursor) for details.

### Why No Selective Persistence (Design Decision)

We considered a `persist=[...]` parameter to control which outputs are checkpointed. We decided against it because **selective persistence creates footguns**:

| Node Type | If Not Persisted | Impact |
|-----------|------------------|--------|
| `embed()` | Re-computes embedding | OK — deterministic |
| `send_email()` | Sends email **twice** | Dangerous |
| `InterruptNode` | Asks human **again** | Confusing UX |
| `charge_card()` | Double charges | Very dangerous |

The mental model is leaky: users think "persist = storage optimization" but it actually means "this might run twice on crash." This conflates two concerns:

1. **Durability** — "Must this survive crashes?" (correctness)
2. **Efficiency** — "Can we skip recomputation?" (performance)

These have different failure modes. Mixing them creates footguns.

**Our decision:** Durability is non-negotiable. When you add a checkpointer, everything is saved. Storage optimization should happen at a different layer:

- **Serializer compression** — reduce size at storage level
- **Workflow cleanup** — delete old workflows (TTL, retention policies)
- **Future: Cache layer** — separate mechanism for cross-workflow memoization

This keeps the model simple: checkpointer = full durability, always.

---

## Quick Comparison

| Need | Solution | Install |
|------|----------|---------|
| Long-running sync DAGs | `SyncRunner(cache=DiskCache(...))` | `pip install hypergraph` |
| Development save points | `AsyncRunner(checkpointer=SqliteCheckpointer(...))` | `pip install hypergraph` |
| Simple production (manual resume) | Same as above | `pip install hypergraph` |
| Automatic crash recovery | `DBOS()` config + `DBOSAsyncRunner()` + `DBOS.launch()` | `pip install hypergraph[dbos]` |
| Durable queues, scheduling | Above + DBOS APIs directly | `pip install hypergraph[dbos]` |

---

## SyncRunner: Cache-Based Durability

**"Poor man's durability for DAGs"** — For long-running synchronous DAGs, use cache instead of checkpointer.

### The Pattern

```python
from hypergraph import SyncRunner, DiskCache

runner = SyncRunner(cache=DiskCache("./cache"))

# First run — all nodes execute, results cached by input hash
result = runner.run(graph, inputs={"data": "big_file.csv"})
# 💥 CRASH at node 5 of 10

# Restart with same inputs — nodes 1-4 are cache hits, only 5+ execute
result = runner.run(graph, inputs={"data": "big_file.csv"})
# ✅ Completes from where it left off (approximately)
```

### How It Works

Cache keys are based on **input hash**, not workflow identity:

```
Node execution:
  inputs = {"x": 1, "y": 2}
  cache_key = hash(node_name, inputs)

On restart:
  Same inputs → cache hit → skip execution
  Different inputs → cache miss → re-execute
```

### Cache vs Checkpointer

| Aspect | Cache | Checkpointer |
|--------|-------|--------------|
| **Semantics** | "Same inputs = same output" | "Resume workflow from step N" |
| **Key** | Hash of node + inputs | workflow_id + step_index |
| **Workflow identity** | None | workflow_id tracks job |
| **Works with cycles** | ❌ (inputs change each iteration) | ✅ |
| **Works with HITL** | ❌ | ✅ |
| **Query job status** | ❌ | ✅ |
| **Complexity** | Simple | More complex |

### When Cache Is Enough

Use SyncRunner + cache for:
- ✅ ETL pipelines
- ✅ Batch data processing
- ✅ ML training workflows
- ✅ Report generation
- ✅ Any long-running DAG with deterministic inputs

### When You Need More

Use AsyncRunner + Checkpointer (or DBOS) for:
- ❌ Cycles (multi-turn conversations)
- ❌ HITL (InterruptNode)
- ❌ Workflow identity (dashboard showing job status)
- ❌ Different inputs on resume (continuing conversation)

### Why No SyncCheckpointer or DBOSSyncRunner?

**DBOS is async-native.** Its core primitives (`@DBOS.workflow`, `recv`/`send`, `DBOS.sleep`) are all async. Wrapping them in sync would be awkward and hide what's actually happening.

**Checkpointer requires workflow semantics.** SyncRunner is designed for simple scripts without workflow identity. If you need workflow semantics, you need the async execution model anyway (for HITL, streaming, etc.).

**AsyncRunner handles sync nodes fine.** If you really need checkpointing but prefer sync-style code:

```python
# Write sync functions, use AsyncRunner for durability
@node(output_name="result")
def my_sync_node(x: int) -> int:  # Sync function
    return expensive_computation(x)

runner = AsyncRunner(checkpointer=SqliteCheckpointer("./db"))
result = await runner.run(graph, inputs={...}, workflow_id="job-123")
```

---

## Path 1: Built-in Checkpointer

For development and simple production use cases where manual resume is acceptable.

### Basic Usage

```python
from hypergraph import Graph, node, AsyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

@node(output_name="result")
async def fetch(query: str) -> dict:
    return await api.search(query)

@node(output_name="summary")
async def summarize(result: dict) -> str:
    return await llm.summarize(result)

graph = Graph(nodes=[fetch, summarize])

# Add checkpointing
runner = AsyncRunner(checkpointer=SqliteCheckpointer("./dev.db"))

result = await runner.run(
    graph,
    inputs={"query": "hello"},
    workflow_id="session-123",  # Required with checkpointer
)
```

### Implicit Resume

Resume is automatic when a `workflow_id` exists. The checkpointer loads state as part of the value resolution hierarchy.

```python
# First run — creates workflow
result = await runner.run(
    graph,
    inputs={"query": "hello"},
    workflow_id="session-123",
)

# Later run — automatically loads checkpoint
# No resume=True needed!
result = await runner.run(
    graph,
    inputs={"query": "hello"},
    workflow_id="session-123",
)
# Checkpointed values are loaded, new inputs override if provided
```

**Value Resolution Order:**
```
1. Edge value        ← Produced by upstream node
2. Runtime input     ← Explicit in runner.run(inputs={...})
3. Checkpoint value  ← Loaded from persistence
4. Bound value       ← Set via graph.bind()
5. Function default  ← Default in function signature
```

This means:
- **Continuation state** (like `messages`) loads from checkpoint
- **New inputs** (like `user_input`) come from runtime
- Nodes with unchanged inputs are skipped (staleness detection)

See [State Model](state-model.md#value-resolution-hierarchy) for the full hierarchy.

### Replay from Specific Step

To replay from a specific point in history, get the state at that step and fork:

```python
# Get state at step 1
state = await checkpointer.get_state("session-123", at_step=1)

# Fork to a new workflow with that state
result = await runner.run(
    graph,
    inputs={**state, "query": "hello"},
    workflow_id="session-123-retry",  # New workflow
)
```

**Use cases:**
- **Retry after fix:** Fixed a bug, want to re-run from a known good state
- **Testing:** Replay from a specific point with modified inputs
- **Debugging:** Isolate where something went wrong

With DBOS, use `fork_workflow()` for more advanced time-travel capabilities.

### Human-in-the-Loop with InterruptNode

```python
from hypergraph import Graph, node, InterruptNode

@node(output_name="draft")
async def generate(prompt: str) -> str:
    return await llm.generate(prompt)

# Declarative pause point — visible in graph structure
approval = InterruptNode(
    name="approval",
    input_param="draft",       # Value shown to user
    response_param="decision", # Where user response goes
)

@node(output_name="final")
def finalize(draft: str, decision: str) -> str:
    if decision == "approve":
        return draft
    return f"REJECTED: {draft}"

graph = Graph(nodes=[generate, approval, finalize])

# First run — pauses at approval
result = await runner.run(
    graph,
    inputs={"prompt": "Write a poem"},
    workflow_id="poem-456",
)

if result.pause:
    print(f"Draft: {result.pause.value}")
    # Wait for user...

# Later — resume with user's decision
# Pass the response using the response_param name
result = await runner.run(
    graph,
    inputs={"decision": "approve"},  # Uses response_param name
    workflow_id="poem-456",
)
# Checkpointed state is loaded automatically via value resolution
```

### Streaming with Generators

Use `yield` for streaming — no special functions needed:

```python
@node(output_name="response")
async def stream_llm(prompt: str):
    """Generator node that streams tokens."""
    async for chunk in llm.stream(prompt):
        yield chunk
    # Final value is the concatenation of all chunks
```

**In `.run()` mode with EventProcessor (push-based):**

```python
class StreamingHandler(TypedEventProcessor):
    def on_streaming_chunk(self, event: StreamingChunkEvent) -> None:
        print(event.chunk, end="", flush=True)

runner = AsyncRunner(
    checkpointer=SqliteCheckpointer("./dev.db"),
    event_processors=[StreamingHandler()],
)
result = await runner.run(graph, inputs={...}, workflow_id="123")
# Chunks printed as they arrive, final value in result.outputs
```

**In `.iter()` mode (pull-based):**

```python
async for event in runner.iter(graph, inputs={...}, workflow_id="123"):
    match event:
        case StreamingChunkEvent(chunk=chunk):
            print(chunk, end="", flush=True)
        case InterruptEvent(value=draft):
            # Handle interrupt
            break
```

### Progress Reporting

Yield progress objects for UI updates:

```python
from dataclasses import dataclass

@dataclass
class Progress:
    percent: float
    message: str

@node(output_name="result")
async def process_with_progress(items: list):
    results = []
    for i, item in enumerate(items):
        yield Progress(i / len(items), f"Processing {i+1}/{len(items)}")
        results.append(await process_item(item))
    yield Progress(1.0, "Complete")
    # Return final value by yielding it last
    yield results
```

Filter by type in your event handler:

```python
class ProgressHandler(TypedEventProcessor):
    def on_streaming_chunk(self, event: StreamingChunkEvent) -> None:
        if isinstance(event.chunk, Progress):
            update_progress_bar(event.chunk.percent, event.chunk.message)
```

### Checkpointer Interface

The full `Checkpointer` interface is defined in [checkpointer.md](checkpointer.md). Key methods:

```python
class Checkpointer(ABC):
    """Base class for workflow persistence. See checkpointer.md for full interface."""

    # Write Operations (per-step, not per-workflow)
    async def save_step(self, workflow_id: str, step: Step, result: StepResult) -> None: ...
    async def create_workflow(self, workflow_id: str) -> Workflow: ...  # Internal
    async def update_workflow_status(self, workflow_id: str, status: WorkflowStatus) -> None: ...

    # Read Operations
    async def get_state(self, workflow_id: str, at_step: int | None = None) -> dict[str, Any]: ...
    async def get_history(self, workflow_id: str, up_to_step: int | None = None) -> list[Step]: ...
    async def get_workflow(self, workflow_id: str) -> Workflow | None: ...
    async def list_workflows(self, status: WorkflowStatus | None = None, limit: int = 100) -> list[Workflow]: ...
```

**Key principle:** Steps are the source of truth. State is computed from steps via `get_state()`, not stored separately.

**See also:**
- [Checkpointer API](checkpointer.md) - Full interface definition
- [Execution Types](execution-types.md#persistence-types) - `Workflow`, `Step`, `StepResult` definitions

### Checkpointer Capabilities

| Capability | Supported |
|------------|:---------:|
| Resume from latest checkpoint | ✅ |
| Resume from specific step | ✅ |
| Get current state | ✅ |
| List workflows | ✅ |
| Step history | ✅ |
| Automatic crash recovery | ❌ |
| Workflow forking (time travel) | ❌ |
| Durable sleep | ❌ |
| Durable queues | ❌ |

For workflow forking (creating a new workflow from a specific step), use DBOS.

---

## Path 2: DBOS Runners

For production workloads requiring automatic crash recovery.

### Integration Philosophy: Thin Wrapper

hypergraph takes a **thin wrapper** approach to DBOS integration, similar to [Pydantic AI's approach](https://ai.pydantic.dev/dbos/):

| What hypergraph wraps | What users handle directly |
|----------------------|---------------------------|
| Graph → DBOS workflow | `DBOS()` configuration |
| `persist=True` → `@DBOS.step` | `DBOS.launch()` for auto-recovery |
| `InterruptNode` → `DBOS.recv()` | `DBOS.send()` for resume |
| Same `RunResult` API | Fork, time travel, sleep, queues, scheduling |

**Why this design?**
- **Core loop is wrapped** — You don't need to learn DBOS to run graphs with durability
- **Advanced features are DBOS-native** — Fork, queues, cron use DBOS APIs directly
- **No leaky abstractions** — We don't try to wrap every DBOS feature poorly
- **Users learn DBOS gradually** — Start with auto-recovery, add queues/cron when needed

### What DBOS Provides

DBOS is a **library** that runs in your process and checkpoints to Postgres/SQLite using pickle serialization. If your process crashes and restarts, DBOS automatically recovers pending workflows.

```
┌─────────────────────────────────────────────────────────────────────┐
│  DBOS RECOVERY (automatic)                                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  1. Your process runs graph                                         │
│     step_1() → checkpoint ✓                                         │
│     step_2() → checkpoint ✓                                         │
│     step_3() → 💥 CRASH                                             │
│                                                                     │
│  2. Process restarts                                                │
│     DBOS.launch()  ← User calls this (triggers recovery)            │
│                                                                     │
│  3. DBOS replays from checkpoints                                   │
│     step_1() → cached ⚡ (skip)                                      │
│     step_2() → cached ⚡ (skip)                                      │
│     step_3() → execute → checkpoint ✓                               │
│     done! ✓                                                         │
│                                                                     │
│  No resume=True needed — DBOS handles it!                           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### How hypergraph Maps to DBOS

hypergraph wraps all nodes as DBOS steps for full durability:

```python
graph = Graph(nodes=[embed, generate])

# Translates to DBOS workflow:
@DBOS.workflow()
async def graph_workflow(inputs: dict) -> dict:
    embedding = await embed_step(inputs["text"])  # @DBOS.step
    answer = await generate_step(inputs["prompt"])  # @DBOS.step
    return {"embedding": embedding, "answer": answer}
```

Every node becomes a `@DBOS.step`, ensuring full crash recovery. DBOS's recommendation:
> "Skip the decorator if durability isn't needed, so you avoid the extra DB checkpoint write."

When using `.get_dbos_workflow()` for advanced DBOS features, the same mapping applies.

### Basic Usage

**Step 1: Configure DBOS (user responsibility)**

```python
from dbos import DBOS

# User configures DBOS directly - hypergraph doesn't wrap this
DBOS(config={
    "name": "my_app",
    "database_url": "sqlite:///./workflow.db",  # Or PostgreSQL for production
})
```

**Step 2: Define and run graph (hypergraph wraps this)**

```python
from hypergraph import Graph, node
from hypergraph.runners import DBOSAsyncRunner

@node(output_name="result")
async def fetch(query: str) -> dict:
    return await api.search(query)

@node(output_name="summary")
async def summarize(result: dict) -> str:
    return await llm.summarize(result)

graph = Graph(nodes=[fetch, summarize])

# DBOSAsyncRunner is a thin wrapper - no config needed
runner = DBOSAsyncRunner()

result = await runner.run(
    graph,
    inputs={"query": "hello"},
    workflow_id="order-123",  # Required for DBOS
)
```

**Step 3: Enable auto-recovery (user responsibility)**

```python
async def main():
    DBOS.launch()  # User calls this to enable crash recovery
    result = await runner.run(graph, inputs={...}, workflow_id="order-123")
```

### With Postgres (Production)

```python
from dbos import DBOS

# User configures Postgres directly
DBOS(config={
    "name": "my_app",
    "database_url": "postgresql://user:pass@host/db",
})

# Runner remains the same - no database_url parameter
runner = DBOSAsyncRunner()
```

### Human-in-the-Loop with DBOS

`InterruptNode` maps to DBOS's `recv()`/`send()` messaging pattern:

```python
from hypergraph import Graph, node, InterruptNode

@node(output_name="draft")
async def generate(prompt: str) -> str:
    return await llm.generate(prompt)

approval = InterruptNode(
    name="approval",
    input_param="draft",
    response_param="decision",
)

@node(output_name="final")
def finalize(draft: str, decision: str) -> str:
    return draft if decision == "approve" else "REJECTED"

graph = Graph(nodes=[generate, approval, finalize])
runner = DBOSAsyncRunner()

# First run — workflow pauses at InterruptNode
# Under the hood: hypergraph maps InterruptNode to DBOS.recv("approval")
result = await runner.run(
    graph,
    inputs={"prompt": "Write a poem"},
    workflow_id="poem-456",
)

print(result.pause is not None)  # True
print(result.pause.value)        # The draft
```

**Resuming with DBOS — user calls `DBOS.send()` directly:**

```python
from dbos import DBOS

# From webhook, API endpoint, or external process:
# User calls DBOS.send() directly — NOT wrapped by hypergraph
DBOS.send(
    destination_id="poem-456",  # workflow_id
    message={"decision": "approve"},
    topic="approval",  # InterruptNode name
)
# Workflow automatically continues — no runner.run() call needed!
```

**Key distinction from checkpointer approach:**

| Aspect | Built-in Checkpointer | DBOS |
|--------|----------------------|------|
| Resume mechanism | Call `runner.run()` again | Call `DBOS.send()` directly |
| State loading | Via value resolution hierarchy | DBOS handles internally |
| Who triggers resume | Your code | External system or webhook |
| Automatic recovery | No | Yes (via `DBOS.launch()`) |

### Streaming with DBOS

**Important:** `.iter()` is not recommended with DBOS due to limitations in how DBOS wraps workflows. Use `EventProcessor` (push-based) instead:

```python
class StreamingHandler(TypedEventProcessor):
    def on_streaming_chunk(self, event: StreamingChunkEvent) -> None:
        print(event.chunk, end="", flush=True)

    def on_interrupt(self, event: InterruptEvent) -> None:
        # Persist interrupt for external handling
        save_pending_approval(event.workflow_id, event.value)

runner = DBOSAsyncRunner(event_processors=[StreamingHandler()])
result = await runner.run(graph, inputs={...}, workflow_id="123")
```

This aligns with how other frameworks (like Pydantic AI) integrate with DBOS.

### DBOS Runner Capabilities

```python
@dataclass
class DBOSRunnerCapabilities(RunnerCapabilities):
    supports_cycles: bool = True
    supports_gates: bool = True
    supports_interrupts: bool = True
    supports_async_nodes: bool = True
    supports_streaming: bool = True  # Via EventProcessor only
    supports_distributed: bool = False
    returns_coroutine: bool = True

    # DBOS-specific
    supports_automatic_recovery: bool = True
    supports_workflow_fork: bool = True
    supports_durable_sleep: bool = True
    supports_durable_queues: bool = True
```

---

## Parallel Execution

### What Is a Superstep?

A **superstep** is a synchronized batch of parallel computations. The term comes from Google's [Pregel paper](https://15799.courses.cs.cmu.edu/fall2013/static/papers/p135-malewicz.pdf) and is used by [LangGraph](https://medium.com/@maksymilian.pilzys/langgraph-transactions-pregel-message-passing-and-super-steps-0e101e620f10).

```
DAG execution as supersteps:

  Superstep 0: [embed, validate, fetch]  ← All can run in parallel (no dependencies)
       ↓ barrier (wait for all to complete)
  Superstep 1: [retrieve, analyze]       ← Depend on superstep 0 outputs
       ↓ barrier
  Superstep 2: [generate]                ← Depends on superstep 1 outputs
```

**Why supersteps matter for checkpointing:**
- Supersteps are the natural unit for user-facing checkpoint operations (fork, resume)
- Each superstep represents a consistent state (all parallel nodes have completed)
- Users don't need to think about individual node indices within a superstep

**Within a superstep:**
- Nodes run concurrently
- Each node is checkpointed individually (partial superstep recovery on crash)
- Alphabetical ordering by node_name provides deterministic replay

### The Challenge

```
Superstep 0: [fetch, embed, retrieve]  ← 3 async nodes run in parallel
             ↓
             Process crashes after fetch and embed complete
             ↓
             On resume: How to know which completed?
```

### Solution: Per-Node Status Tracking

Each node within a superstep is tracked individually. On resume, only incomplete nodes re-run.

> **Note:** This is simplified pseudocode illustrating the concept. See [checkpointer.md](checkpointer.md) for the actual Checkpointer interface.

```python
# Execution flow (conceptual pseudocode)
async def execute_superstep(superstep_nodes: list[HyperNode], workflow_id: str, superstep: int):
    # 1. Execute all in parallel with individual checkpointing
    async def execute_one(node: HyperNode, idx: int):
        # Check if already completed (resume case)
        existing = get_step_if_completed(workflow_id, superstep, node.name)
        if existing:
            return existing.outputs  # Skip, use cached result

        try:
            outputs = await node.execute(inputs)
            # Save step with result
            await checkpointer.save_step(
                workflow_id,
                Step(superstep=superstep, node_name=node.name, index=idx, status=StepStatus.COMPLETED),
                StepResult(outputs=outputs),
            )
            return outputs
        except Exception as e:
            await checkpointer.save_step(
                workflow_id,
                Step(superstep=superstep, node_name=node.name, index=idx, status=StepStatus.FAILED),
                StepResult(error=str(e)),
            )
            raise

    # 2. Run all concurrently
    # Indices assigned alphabetically by node_name for deterministic ordering
    sorted_nodes = sorted(superstep_nodes, key=lambda n: n.name)
    results = await asyncio.gather(*[
        execute_one(node, idx)
        for idx, node in enumerate(sorted_nodes)
    ])

    return results
```

### Partial Superstep Recovery

**Only incomplete nodes re-run.** This ensures at-least-once semantics per node:

```
Superstep 0: [embed, fetch, validate] running in parallel
  → embed completes    (status=COMPLETED)
  → fetch completes    (status=COMPLETED)
  → 💥 CRASH before validate completes

On resume:
  → embed: COMPLETED → skip (output loaded from checkpoint)
  → fetch: COMPLETED → skip (output loaded from checkpoint)
  → validate: RUNNING → re-execute
```

### Deterministic Ordering Within Supersteps

From [Temporal](https://docs.temporal.io/workflows) and [DBOS](https://docs.dbos.dev/architecture):

> "Workflows must be deterministic... the order of *starting* steps must be the same on replay."

**Within a superstep, nodes are ordered alphabetically by node_name.** This ensures deterministic `index` assignment regardless of completion order.

```
Superstep 0 starts (nodes sorted alphabetically):
  → index=0: embed    (alphabetically first)
  → index=1: fetch    (alphabetically second)
  → index=2: validate (alphabetically third)

Completion order may vary, but indices are stable.
```

### Checkpoint Identification

Users identify checkpoints by **superstep number**, not individual step indices:

```python
# Get state after superstep 2 completes (all parallel nodes in that superstep)
state = await checkpointer.get_state("order-123", superstep=2)

# Fork workflow from superstep 1
checkpoint = await checkpointer.get_checkpoint("order-123", superstep=1)
```

**Superstep vs Index:**

| Concept | User-Facing? | Purpose |
|---------|:------------:|---------|
| `superstep` | ✅ Yes | Identifies batch boundaries for checkpointing/forking |
| `node_name` | ✅ Yes | Identifies which node within a superstep |
| `index` | ❌ Internal | Unique DB key, assigned alphabetically within superstep |

```python
# Example: Superstep with 3 parallel nodes
Step(superstep=0, node_name="embed", index=0)     # ─┐
Step(superstep=0, node_name="fetch", index=1)     # ─┼─ Same superstep, concurrent
Step(superstep=0, node_name="validate", index=2)  # ─┘

Step(superstep=1, node_name="generate", index=3)  # Next superstep
```

### Parallel Nodes Are Steps, Not Child Workflows

**Important distinction:**

| Concept | What It Is | Checkpoint Model |
|---------|-----------|------------------|
| **Parallel nodes** | Multiple nodes in same superstep | Steps within current workflow |
| **Nested graph** | GraphNode containing subgraph | Child workflow |

Parallel nodes do NOT become child workflows. They're just steps that happen to run concurrently within the same workflow.

---

## Nested Graphs

When a `GraphNode` executes, it creates a step with `child_workflow_id` pointing to a separate workflow. Nested graphs are fully supported with flat storage and string-based parent-child relationships.

### Workflow ID Convention

```python
parent_id = "order-123"
child_id = child_workflow_id(parent_id, "rag")  # "order-123/rag"

# Helper functions (not classes)
def child_workflow_id(parent_id: str, node_name: str) -> str:
    return f"{parent_id}/{node_name}"

def parent_workflow_id(workflow_id: str) -> str | None:
    if "/" not in workflow_id:
        return None
    return workflow_id.rsplit("/", 1)[0]
```

### Flat Storage Model

Two separate `Workflow` records linked by string reference:

```
Workflow(id="order-123")
├── Step(index=0, node_name="fetch")
├── Step(index=1, node_name="embed")
├── Step(index=2, node_name="rag", child_workflow_id="order-123/rag")
└── Step(index=3, node_name="postprocess")

Workflow(id="order-123/rag")  # Separate record
├── Step(index=0, node_name="inner_embed")
└── Step(index=1, node_name="inner_retrieve")
```

No recursive in-memory structure. Just flat records with string references.

### Execution (Simplified)

```python
async def execute_graph_node(
    graph_node: GraphNode,
    workflow_id: str,
    step_index: int,
    inputs: dict,
):
    # 1. Create step with child reference
    child_id = child_workflow_id(workflow_id, graph_node.name)

    step = Step(
        index=step_index,
        node_name=graph_node.name,
        batch_index=batch.index,
        status=StepStatus.RUNNING,
        child_workflow_id=child_id,
    )
    await checkpointer.save_step(workflow_id, step)

    # 2. Execute child as separate workflow
    child_workflow = Workflow(id=child_id)
    result = await execute_graph(graph_node.graph, child_workflow, inputs)

    # 3. Mark parent step complete
    step.status = StepStatus.COMPLETED
    await checkpointer.save_step(workflow_id, step)

    return result
```

### Parallel vs Nested

| Aspect | Parallel Nodes | Nested Graphs |
|--------|---------------|---------------|
| **Storage** | Same workflow | Separate workflow record |
| **ID** | Same `workflow_id` | `parent_id/node_name` |
| **Linking** | None | `child_workflow_id` field |
| **Recovery** | Skip completed steps | Resume child workflow |

**The key distinction:**
- **Parallel** = multiple steps, one workflow
- **Nested** = one step links to child workflow

---

## DBOS vs Built-in Checkpointer

| Capability | Checkpointer | DBOS |
|------------|:------------:|:----:|
| Resume from latest | ✅ Implicit (load → merge) | ✅ Automatic |
| Replay from specific step | ✅ (manual fork with `get_state`) | ✅ (`fork_workflow`) |
| Get current state | ✅ | ✅ |
| List workflows | ✅ | ✅ |
| Step history | ✅ | ✅ |
| **Automatic crash recovery** | ❌ | ✅ |
| Workflow forking (time travel) | ✅ (manual) | ✅ (built-in) |
| Durable sleep | ❌ | ✅ |
| Durable queues | ❌ | ✅ |
| Queue concurrency limits | ❌ | ✅ |
| Queue rate limiting | ❌ | ✅ |
| Workflow messaging | ❌ | ✅ (`send`/`recv`) |
| Scheduled workflows (cron) | ❌ | ✅ |
| `.iter()` streaming | ✅ | ❌ (use EventProcessor) |

**Forking with Checkpointer:** Use `get_state(at_step=N)` to get state at a point, then run with a new `workflow_id`. History is append-only; forks create new workflows.

---

## Advanced DBOS Features

For features beyond hypergraph primitives, **users call DBOS APIs directly**. This is by design — we wrap the core loop, not every DBOS feature.

| Feature | hypergraph Wraps? | How to Use |
|---------|:-----------------:|------------|
| Run graph with durability | ✅ | `runner.run()` |
| Crash recovery | ❌ | `DBOS.launch()` |
| Resume interrupted workflow | ❌ | `DBOS.send()` |
| Workflow forking (time travel) | ❌ | `DBOS.fork_workflow()` |
| Durable sleep | ❌ | `DBOS.sleep()` |
| Durable queues | ❌ | `Queue().enqueue()` |
| Scheduled workflows (cron) | ❌ | `@DBOS.scheduled()` |

### Workflow Forking (Time Travel)

Restart a workflow from a specific step:

```python
from dbos import DBOS

# Fork from step 2 to retry with fixed code
DBOS.fork_workflow(
    original_workflow_id="order-123",
    start_step=2,
    new_workflow_id="order-123-retry",
)
```

### Durable Queues

```python
from dbos import Queue

# Get the DBOS workflow that wraps this graph
workflow_fn = runner.get_dbos_workflow(graph)

queue = Queue("processing-queue", concurrency=10)

# Enqueue work
handles = []
for item in items:
    handle = queue.enqueue(workflow_fn, {"item": item})
    handles.append(handle)

# Wait for results
results = [h.get_result() for h in handles]
```

### Scheduled Workflows (Cron)

```python
from dbos import DBOS

workflow_fn = runner.get_dbos_workflow(graph)

@DBOS.scheduled('0 9 * * *')  # Every day at 9am
@DBOS.workflow()
def daily_report(scheduled_time, actual_time):
    workflow_fn({"report_date": scheduled_time.date()})
```

### Durable Sleep

For long delays that survive crashes:

```python
from dbos import DBOS

@DBOS.workflow()
def reminder_workflow(remind_at: datetime, message: str):
    seconds_until = (remind_at - datetime.now()).total_seconds()
    DBOS.sleep(seconds_until)  # Survives any interruption
    send_reminder(message)
```

---

## Runner Selection Guide

```
┌─────────────────────────────────────────────────────────────────────┐
│  What do you need?                                                  │
└───────────┬─────────────────────────────────────────────────────────┘
            │
            ▼
┌───────────────────────┐     ┌───────────────────────┐
│ Just development?     │ YES │ AsyncRunner()         │
│ No persistence needed │────►│ (no checkpointer)     │
└───────────┬───────────┘     └───────────────────────┘
            │ NO
            ▼
┌───────────────────────┐     ┌───────────────────────┐
│ Need save points?     │ YES │ AsyncRunner +         │
│ Manual resume is OK?  │────►│ SqliteCheckpointer    │
└───────────┬───────────┘     └───────────────────────┘
            │ NO
            ▼
┌───────────────────────┐     ┌───────────────────────┐
│ Need auto recovery?   │ YES │ DBOSAsyncRunner       │
│ Production workload?  │────►│ (SQLite or Postgres)  │
└───────────┬───────────┘     └───────────────────────┘
            │ NO
            ▼
┌───────────────────────┐     ┌───────────────────────┐
│ Need queues,          │ YES │ DBOSAsyncRunner +     │
│ scheduling, etc?      │────►│ DBOS features         │
└───────────────────────┘     └───────────────────────┘
```

---

## Parameter Reference

### AsyncRunner with Checkpointer

```python
result = await runner.run(
    graph,
    inputs={"query": "hello"},
    workflow_id="session-123",  # Required with checkpointer
)
```

| Parameter | Required | Description |
|-----------|:--------:|-------------|
| `workflow_id` | With checkpointer | Identifies the workflow for state storage |

**Execution semantics:** When `workflow_id` is provided, the runner loads checkpoint state, merges with inputs (inputs win), executes the graph, and appends steps to history. No special "resume" flag needed.

### DBOSAsyncRunner

```python
from dbos import DBOS
from hypergraph.runners import DBOSAsyncRunner

# User configures DBOS directly (not via runner)
DBOS(config={"name": "my_app", "database_url": "postgresql://..."})

# Runner is a thin wrapper — no config parameters
runner = DBOSAsyncRunner()

async def main():
    DBOS.launch()  # User calls this for auto-recovery

    result = await runner.run(
        graph,
        inputs={"query": "hello"},
        workflow_id="order-123",    # Required for DBOS
    )
```

| Parameter | Required | Description |
|-----------|:--------:|-------------|
| `workflow_id` | Yes | Unique workflow identifier for DBOS durability |

**Note:**
- No `database_url` parameter — user configures DBOS directly via `DBOS()`
- No `resume` parameter — DBOS handles recovery automatically via `DBOS.launch()`
- Resume interrupted workflows via `DBOS.send()`, not `runner.run()`

---

## Module Structure

```
hypergraph/
├── runners/
│   ├── __init__.py
│   ├── base.py              # BaseRunner, RunnerCapabilities
│   ├── sync.py              # SyncRunner
│   ├── async_.py            # AsyncRunner
│   ├── daft.py              # DaftRunner
│   └── dbos.py              # DBOSAsyncRunner
│
├── checkpointers/
│   ├── __init__.py
│   ├── base.py              # Checkpointer ABC
│   ├── sqlite.py            # SqliteCheckpointer
│   └── postgres.py          # PostgresCheckpointer
│
└── ...
```

---

## Installation

```bash
# Core (includes checkpointers)
pip install hypergraph

# With DBOS
pip install hypergraph[dbos]
```

---

## Migration Path

### From Checkpointer to DBOS

```python
# Before: Implicit resume with checkpointer
runner = AsyncRunner(checkpointer=SqliteCheckpointer("./dev.db"))
result = await runner.run(graph, inputs={...}, workflow_id="123")
if result.pause:
    # Wait for user input...
    result = await runner.run(
        graph,
        inputs={result.pause.response_param: response},
        workflow_id="123",
    )
    # State loaded automatically via value resolution

# After: Automatic recovery with DBOS
from dbos import DBOS

# 1. User configures DBOS directly
DBOS(config={"name": "my_app", "database_url": "postgresql://..."})

# 2. Use thin wrapper runner (no config)
runner = DBOSAsyncRunner()

async def main():
    # 3. User calls DBOS.launch() for auto-recovery
    DBOS.launch()

    result = await runner.run(graph, inputs={...}, workflow_id="123")
    if result.pause:
        # 4. External system resumes via DBOS.send() — NOT wrapped
        # Workflow auto-resumes — no runner.run() call needed
        pass
```

**What changes:**
- Add `DBOS()` configuration (user responsibility)
- Add `DBOS.launch()` call (user responsibility)
- Remove `checkpointer=` parameter from runner
- Resume via `DBOS.send()` instead of `runner.run()`

**What doesn't change:**
- Graph definition
- Node functions
- InterruptNode usage
- `workflow_id` parameter
- `RunResult` API

---

## Retry Configuration

Transient failures are common. hypergraph has no built-in retry — just stack a retry decorator on your node.

### Design Principle

**Decorator stacking.** Use [stamina](https://stamina.hynek.me/) or any retry library you prefer.

```python
import stamina
import httpx
from hypergraph import node

@node(output_name="result")
@stamina.retry(on=httpx.HTTPError, attempts=5, timeout=60)
async def fetch(query: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"https://api.example.com/search?q={query}")
        response.raise_for_status()
        return response.json()
```

No retry params in `@node()`. No hypergraph retry API. Just decorators.

### Why Stamina?

| Feature | Benefit |
|---------|---------|
| Exponential backoff with jitter | Production-ready defaults (100ms → 45s) |
| Explicit exception types | Forces you to think about what to retry |
| Built-in observability | Prometheus, structlog integration |
| Testing mode | `set_testing()` disables retries in tests |

```bash
pip install stamina
```

### Selective Retry

```python
import httpx
import stamina
from hypergraph import node

def is_retryable(exc: httpx.HTTPStatusError) -> bool:
    """Don't retry 4xx client errors (except 429 rate limit)."""
    return exc.response.status_code >= 500 or exc.response.status_code == 429

@node(output_name="result")
@stamina.retry(on=is_retryable, attempts=5)
async def fetch(url: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()
```

### Retries and Checkpointing

Retries happen *inside* the node, before checkpointing:

```
Runner calls node
  → Stamina wraps execution
    → attempt 1: fails
    → attempt 2: fails
    → attempt 3: succeeds ✓
  → Stamina returns result
→ Checkpointer saves result
```

The checkpointer only sees the final result. Same behavior with any runner.

**On crash recovery:** Retry count resets (retry state is in-memory, not persisted).

### Retry vs Recovery

| Concept | Scope | Trigger | Handled By |
|---------|-------|---------|------------|
| **Retry** | Single node | Transient exception | stamina (or any retry lib) |
| **Resume** | Entire workflow | User calls `resume()` | Checkpointer |
| **Recovery** | Entire workflow | Process restart | DBOS automatic |

### Best Practices

1. **Be explicit about exceptions** — Never retry on bare `Exception`
   ```python
   # ❌ Bad
   @stamina.retry(on=Exception, attempts=5)

   # ✅ Good
   @stamina.retry(on=(httpx.HTTPError, asyncio.TimeoutError), attempts=5)
   ```

2. **Don't retry forever** — Always set `attempts` and/or `timeout`

3. **Consider idempotency** — Retried operations should be safe to repeat

4. **Use testing mode** — Disable retries in tests
   ```python
   def test_my_node():
       with stamina.set_testing(attempts=1):
           result = my_node("input")
   ```

---

## Summary

| Layer | Responsibility | User Imports |
|-------|----------------|--------------|
| **Graph** | Structure, nodes, routing | `hypergraph` |
| **Runner** | Execution, event dispatch | `hypergraph.runners` |
| **Checkpointer** | Manual persistence | `hypergraph.checkpointers` |
| **Retries** | Transient failure handling | `stamina` (or any retry lib) |
| **DBOS (optional)** | Automatic durability, queues, scheduling | `dbos` |

**The principles:**
- **Graph code stays pure** — No durability imports in nodes
- **Durability is a runner concern** — Same graph works with any runner
- **DBOS is a thin wrapper** — We wrap the core loop, users call DBOS APIs for advanced features
- **Retries are decorator stacking** — No hypergraph-specific retry API

---

## See Also

- [Checkpointer API](checkpointer.md) - Full interface definition and custom implementations
- [Persistence Tutorial](persistence.md) - How to use persistence
- [Execution Types](execution-types.md) - Step, Workflow, and other type definitions
- [Observability](observability.md) - EventProcessor (separate from Checkpointer)
