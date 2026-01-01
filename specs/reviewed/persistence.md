# Persistence

**Save workflow progress, resume after interruptions, and build human-in-the-loop applications.**

---

## Why Persistence?

Persistence enables powerful capabilities for your hypergraph graphs:

| Capability | What It Enables |
|------------|-----------------|
| **Multi-Turn Conversations** | Continue conversations across runs |
| **Human-in-the-Loop** | Pause for approval, resume with user input |
| **Fault Tolerance** | Resume from last checkpoint after crashes |
| **Debugging** | Inspect step history, replay from any point |
| **Long-Running Workflows** | Handle workflows that span hours or days |

---

## Quick Start

```python
from hypergraph import Graph, node, AsyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

@node(output_name="answer")
async def generate(query: str) -> str:
    return await llm.generate(query)

graph = Graph(nodes=[generate])

# Add persistence with a checkpointer
runner = AsyncRunner(checkpointer=SqliteCheckpointer("./workflows.db"))

# Run with a workflow_id
result = await runner.run(
    graph,
    inputs={"query": "What is RAG?"},
    workflow_id="session-123",
)

print(result["answer"])
```

That's it. Your workflow is now persisted and resumable.

---

## Core Concepts

### Workflow ID

The `workflow_id` uniquely identifies a workflow execution. Think of it like a conversation thread — all runs with the same `workflow_id` share checkpointed state.

```python
# First run - creates workflow
result = await runner.run(graph, inputs={...}, workflow_id="order-456")

# Later run - automatically loads checkpoint
result = await runner.run(graph, inputs={...}, workflow_id="order-456")
```

**Naming conventions:**
- User sessions: `"user-{user_id}-{session_id}"`
- Business processes: `"order-{order_id}"`, `"ticket-{ticket_id}"`
- Development: `"dev-{timestamp}"`

### Value Resolution Hierarchy

When determining what value to use for a parameter, hypergraph checks these sources in order:

```
1. Edge value        ← Produced by upstream node (if it has executed)
2. Runtime input     ← Explicit in runner.run(inputs={...})
3. Checkpoint value  ← Loaded from persistence
4. Bound value       ← Set via graph.bind()
5. Function default  ← Default in function signature
```

**Key insight:** Edge values are about execution state. If you provide a value as input that would otherwise come from an upstream node, the upstream node doesn't run — its output is already available.

This hierarchy enables powerful patterns:
- **Skip expensive nodes** by providing their outputs as inputs
- **Checkpoint provides continuation state** for values not in inputs
- **Inputs override checkpoint** when you want to change something
- **Bound values initialize seeds** (like empty message lists)

### Outputs ARE State

Unlike frameworks that require explicit state schemas, **hypergraph infers state from your node outputs**.

```python
# LangGraph - explicit state schema
class State(TypedDict):
    messages: list
    answer: str

graph = StateGraph(State)
```

```python
# hypergraph - no schema needed
@node(output_name="response")
def get_response(messages: list, user_input: str) -> str:
    """Get LLM response for the conversation."""
    full_messages = messages + [{"role": "user", "content": user_input}]
    return llm.chat(full_messages)

@node(output_name="messages")
def update_messages(messages: list, user_input: str, response: str) -> list:
    """Append user message and response to conversation history."""
    return messages + [
        {"role": "user", "content": user_input},
        {"role": "assistant", "content": response},
    ]

# State is inferred: {"messages": [...], "response": "..."}
graph = Graph(nodes=[get_response, update_messages])
```

Your node outputs become the state. No reducers, no explicit schema.

### Checkpoints

A checkpoint is a snapshot of workflow state at a specific step. By default, hypergraph saves a checkpoint after each node completes.

```
Step 0: fetch     → checkpoint saved
Step 1: process   → checkpoint saved
Step 2: generate  → checkpoint saved ← latest
```

Checkpoints are identified by `workflow_id` + `step_index`:

```python
# Get step history
history = await checkpointer.get_history("order-456")
# [Step(index=0, node_name="fetch", status="completed"),
#  Step(index=1, node_name="process", status="completed"),
#  Step(index=2, node_name="generate", status="completed")]
```

---

## Selective Persistence

Not all outputs need to survive crashes. Use the `persist` parameter at the Graph level to control what's checkpointed.

### Default: Persist Everything

When a checkpointer is present, **all outputs are persisted by default**. This is the safe default — you opt into durability by adding a checkpointer, so persist everything.

```python
# All outputs persisted (default)
graph = Graph(nodes=[embed, retrieve, generate])
runner = AsyncRunner(checkpointer=SqliteCheckpointer("./db"))
```

### Allowlist: Persist Specific Outputs

Use `persist=[...]` to specify which **outputs** to checkpoint. This is an optimization to reduce storage.

```python
graph = Graph(
    nodes=[embed, retrieve, generate],
    persist=["docs", "answer"],  # Only these outputs are checkpointed
)
```

**Multi-output nodes:** For nodes with multiple outputs (e.g., `@node(outputs=("mean", "std"))`), you must include ALL or NONE of the node's outputs. Partial inclusion raises a build-time error — nodes execute atomically, so you can't persist some outputs without the others.

### Why Selective Persistence?

| Node | What It Produces | Persist? | Reason |
|------|------------------|:--------:|--------|
| `accumulate` | `messages` | ✅ | Can't reconstruct conversation |
| `generate` | `answer` | ✅ | User expects this |
| `embed` | `embedding` | ❌ | Can regenerate (deterministic) |
| `retrieve` | `docs` | ❌ | Can refetch |

### Resume Behavior

On crash and resume:

```
Original run:
  embed("hello") → [0.1, 0.2, ...]  ← NOT saved (not in persist list)
  generate(...)  → "answer"         ← SAVED
  💥 CRASH

Resume:
  embed("hello") → [0.1, 0.2, ...]  ← Re-executed
  generate(...)  → (loaded)         ← Loaded from checkpoint
  ✅ Complete
```

**Key insight:** Non-persisted nodes re-execute on resume. This is fine for deterministic operations like embedding — you trade storage for compute.

---

## Multi-Turn Conversations

The value resolution hierarchy makes multi-turn conversations effortless.

### The Pattern

```python
from hypergraph import Graph, node, AsyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

@node(output_name="response")
def get_response(messages: list, user_input: str) -> str:
    """Get LLM response for the conversation."""
    full_messages = messages + [{"role": "user", "content": user_input}]
    return llm.chat(full_messages)

@node(output_name="messages")
def update_messages(messages: list, user_input: str, response: str) -> list:
    """Append user message and response to conversation history."""
    return messages + [
        {"role": "user", "content": user_input},
        {"role": "assistant", "content": response},
    ]

# bind() provides the initial seed value for messages
chat_graph = Graph(nodes=[get_response, update_messages]).bind(messages=[])

runner = AsyncRunner(checkpointer=SqliteCheckpointer("./chats.db"))
```

### Usage

```python
# Turn 1 - no checkpoint exists
result = await runner.run(
    chat_graph,
    inputs={"user_input": "What is RAG?"},
    workflow_id="chat-123",
)
# Resolution: messages from bound ([]), user_input from input
# Output: messages = [{user}, {assistant}] → saved to checkpoint

print(result["response"])  # "RAG stands for..."

# Turn 2 - checkpoint exists
result = await runner.run(
    chat_graph,
    inputs={"user_input": "Tell me more"},
    workflow_id="chat-123",
)
# Resolution: messages from checkpoint, user_input from input
# Bound value is never consulted (checkpoint wins)

print(result["response"])  # "Sure! RAG works by..."
print(result["messages"])  # Full conversation history
```

**The magic:**
- User never passes `messages` — it's handled automatically
- First turn: `messages` comes from `bind([])`
- Later turns: `messages` comes from checkpoint
- New `user_input` always comes from runtime inputs

### Why This Works

The value resolution hierarchy:

```
Turn 1:
  messages: checkpoint? NO → bound? YES (=[]) → use []
  user_input: runtime input → "What is RAG?"

Turn 2:
  messages: checkpoint? YES → use [{user}, {assistant}]
  user_input: runtime input → "Tell me more"
```

The checkpoint sits between runtime inputs and bound values, so:
- **Continuation state** (messages) loads from checkpoint
- **New inputs** (user_input) come from runtime

---

## Human-in-the-Loop

Use `InterruptNode` to pause workflows for human input.

### Basic Pattern

```python
from hypergraph import Graph, node, InterruptNode

@node(output_name="draft")
async def generate(prompt: str) -> str:
    return await llm.generate(prompt)

# Declarative pause point
approval = InterruptNode(
    name="approval",
    input_param="draft",       # What human sees
    response_param="decision", # What human provides
)

@node(output_name="final")
def finalize(draft: str, decision: str) -> str:
    if decision == "approve":
        return draft
    return f"REJECTED: {draft}"

graph = Graph(nodes=[generate, approval, finalize])
```

### Execution Flow

```python
runner = AsyncRunner(checkpointer=SqliteCheckpointer("./dev.db"))

# Step 1: Run until interrupt
result = await runner.run(
    graph,
    inputs={"prompt": "Write a poem"},
    workflow_id="poem-456",
)

# Check if paused
if result.status == RunStatus.PAUSED:
    print(f"Draft: {result.pause.value}")       # Show to user
    print(f"Waiting for: {result.pause.response_param}")  # "decision"

# Step 2: Resume with user's decision (just run again with same workflow_id)
result = await runner.run(
    graph,
    inputs={"decision": "approve"},  # User's response
    workflow_id="poem-456",
)

print(result["final"])  # Approved poem
```

### Pause Information

When a workflow pauses, `result.pause` contains everything you need:

```python
@dataclass
class PauseInfo:
    reason: PauseReason      # HUMAN_INPUT, SLEEP, etc.
    node: str                # Name of the InterruptNode
    value: Any               # Value to show user (input_param)
    response_param: str      # Where to put user's response
```

---

## Resuming Workflows

Resume is automatic. Just run with the same `workflow_id`:

```python
# First run - workflow created
result = await runner.run(graph, inputs={...}, workflow_id="session-123")
# 💥 CRASH

# Resume - just run with same workflow_id
result = await runner.run(graph, inputs={...}, workflow_id="session-123")
# Checkpoint state loaded → merged with inputs → graph executes → steps appended
```

There's no special "resume" flag. The execution semantics are always the same:
1. Load checkpoint state (if workflow exists)
2. Merge with inputs
3. Execute graph
4. Append steps

See [Execution Semantics](#execution-semantics) for the full model

---

## State vs History

hypergraph separates two concepts:

| Concept | What It Is | API |
|---------|------------|-----|
| **State** | Accumulated output values at a point in time | `get_state(workflow_id, at_step=N)` |
| **History** | Execution audit trail (step records) | `get_history(workflow_id)` |

### Steps Are the Source of Truth

**State is computed from Steps, not stored separately.** The checkpointer stores step records (each containing the node name, status, and outputs). When you call `get_state()`, the checkpointer computes the accumulated state by folding over the steps up to that point.

```
Steps (stored):
  Step 0: node="fetch",    outputs={"data": {...}}
  Step 1: node="process",  outputs={"result": {...}}
  Step 2: node="generate", outputs={"answer": "..."}

State (computed):
  get_state(at_step=2) → {"data": {...}, "result": {...}, "answer": "..."}
```

This design has important implications:
- **Single source of truth**: Steps are the authoritative record; state is derived
- **Time travel**: Get state at any historical point by folding steps up to that point
- **No sync issues**: State can never be "out of sync" with steps

### Why Separate Them?

Different operations need different data:

- **Continue conversation:** Need state (messages), don't need history
- **Debug a failure:** Need history (what ran), may not need full state
- **Fork and retry:** Need both state and history up to a point
- **Time travel debugging:** Get state at any historical step

### Checkpointer API

```python
# Get accumulated state at a point (computed from steps)
state = await checkpointer.get_state("session-123")           # Latest
state = await checkpointer.get_state("session-123", at_step=5)  # At step 5
# Returns: {"messages": [...], "answer": "..."}

# Get execution history (the actual stored records)
history = await checkpointer.get_history("session-123")
# Returns: [StepInfo(index=0, node="embed", output=...), ...]

# Get full workflow metadata
workflow = await checkpointer.get_workflow("session-123")
# Returns: Workflow(id=..., status=..., created_at=...)
```

See [Checkpointer API Reference](checkpointer.md) for the full interface definition.

---

## Explicit State and History Injection

The `runner.run()` method supports explicit injection of state and history, decoupling **where you read from** and **where you write to**.

### The `history` Parameter

```python
# Get state and history from an existing workflow
state = await checkpointer.get_state("session-123", at_step=5)
history = await checkpointer.get_history("session-123", up_to_step=5)

# Start a NEW workflow with that context
result = await runner.run(
    graph,
    inputs={**state, "user_input": "new question"},  # State via spread
    history=history,                                   # Execution trail
    workflow_id="session-456",                         # NEW workflow
)
```

**What `history` does:**
- Seeds the step index (new steps continue numbering from where history left off)
- Copies the step trail into the new workflow
- Provides full audit trail via `get_history()`

### Use Cases

**Fork and continue:**
```python
# Fork from step 5 of workflow X into new workflow Y
state = await checkpointer.get_state("order-123", at_step=5)
history = await checkpointer.get_history("order-123", up_to_step=5)

result = await runner.run(
    graph,
    inputs={**state, "new_param": "modified"},
    history=history,
    workflow_id="order-123-retry",
)
```

**Continue conversation in new session:**
```python
# Get conversation state from old session
state = await checkpointer.get_state("chat-old")

# Start fresh session with that context (no history needed)
result = await runner.run(
    graph,
    inputs={**state, "user_input": "Hello again!"},
    workflow_id="chat-new",
)
```

**Debug replay:**
```python
# Replay from specific point with full history
state = await checkpointer.get_state("debug-me", at_step=3)
history = await checkpointer.get_history("debug-me", up_to_step=3)

result = await runner.run(
    graph,
    inputs=state,
    history=history,
    workflow_id="debug-replay-1",
)
```

### Decoupling Read from Write

The key insight: **`inputs` and `history` control what goes IN, `workflow_id` controls where it's WRITTEN.**

```
┌─────────────────────────────────────────────────────────────┐
│  READ (from anywhere)          WRITE (to workflow_id)       │
│                                                             │
│  inputs={**state, ...}    ──►  Checkpoints saved to         │
│  history=history          ──►  "session-456"                │
│                                                             │
│  State from "session-123"      New workflow "session-456"   │
│  History from "session-123"    continues independently      │
└─────────────────────────────────────────────────────────────┘
```

This enables:
- **Branching:** Fork from any workflow into a new one
- **Copying:** Continue from one workflow's state in another
- **Testing:** Inject known state without touching production workflows

---

## Forking Workflows

To fork a workflow (start a new one from a point in an existing workflow), use `runner.run()` with the source's state and history:

```python
# Fork from step 5 of workflow X into new workflow Y
state = await checkpointer.get_state("order-123", at_step=5)
history = await checkpointer.get_history("order-123", up_to_step=5)

result = await runner.run(
    graph,
    inputs={**state, "new_input": "value"},  # Merge with new inputs
    workflow_id="order-123-fork",            # New workflow ID
    history=history,                         # Carry forward history
)
```

**Key design decision:** Forks are pure data copies — no references or pointers to the source. This keeps the model simple:
- Deleting the source doesn't affect forks
- No maintenance of reference chains
- Each workflow is self-contained

---

## Execution Semantics

The `run()` method is intentionally simple — it has one behavior regardless of workflow state.

### What `run()` Does

```
run(graph, inputs, workflow_id):
  1. Load   — Get checkpoint state (if workflow_id exists)
  2. Merge  — Combine with inputs (inputs win on conflicts)
  3. Execute — Run the graph
  4. Append — Add new steps to history
  5. Return — Give back result
```

**That's it.** No state machine. No special cases for "completed" vs "paused" vs "error" workflows.

### The `workflow_id=` Sugar

When you provide a `workflow_id`, you get automatic load-and-save:

```python
# This single line does a lot:
result = await runner.run(graph, inputs={"user_input": "Hi"}, workflow_id="chat-123")

# Equivalent to:
# 1. state = await checkpointer.get_state("chat-123")  # Load (if exists)
# 2. merged = {**state, "user_input": "Hi"}            # Merge (inputs win)
# 3. result = execute(graph, merged)                   # Execute
# 4. await checkpointer.append_steps("chat-123", ...)  # Append
```

### Append-Only History

History never overwrites — each run appends new steps:

```python
# Turn 1
result = await runner.run(graph, inputs={"user_input": "Hi"}, workflow_id="chat")
# Steps: [0, 1]

# Turn 2
result = await runner.run(graph, inputs={"user_input": "Tell me more"}, workflow_id="chat")
# Steps: [0, 1, 2, 3]  ← Appended, not replaced

# Turn 3 (even if previous had an error!)
result = await runner.run(graph, inputs={"user_input": "Try again"}, workflow_id="chat")
# Steps: [0, 1, 2, 3, 4, 5]  ← Just keeps appending
```

### Inputs Override Checkpoint

Per the value resolution hierarchy, runtime inputs win over checkpointed values:

```python
# Workflow "chat-123" has checkpointed messages=[{...}, {...}]

# This OVERRIDES the checkpointed messages (intentional)
result = await runner.run(
    graph,
    inputs={"messages": [], "user_input": "Hi"},  # Fresh start
    workflow_id="chat-123",
)
```

This is useful for:
- Resetting conversation state mid-session
- Overriding specific values for testing
- Providing updated context

### The `history=` Parameter (New Workflows Only)

The `history` parameter seeds a **new** workflow with step history. It's for forking:

```python
# Fork from step 5 of workflow X into new workflow Y
state = await checkpointer.get_state("order-123", at_step=5)
history = await checkpointer.get_history("order-123", up_to_step=5)

result = await runner.run(
    graph,
    inputs={**state, "new_param": "modified"},
    history=history,              # Seeds the new workflow
    workflow_id="order-123-fork", # Must be NEW workflow
)
```

Providing `history=` to an existing workflow errors — it already has its own history.

### Concurrent Execution

A workflow can only have one active execution at a time (this is a safety constraint, not complexity):

```python
# ❌ Error: workflow is already running
task1 = runner.run(graph, inputs={...}, workflow_id="order-123")
task2 = runner.run(graph, inputs={...}, workflow_id="order-123")  # Conflict!

# ✅ Correct: use different workflow_ids for parallel work
task1 = runner.run(graph, inputs={...}, workflow_id="order-123-a")
task2 = runner.run(graph, inputs={...}, workflow_id="order-123-b")
```

### Patterns Summary

| Goal | Pattern |
|------|---------|
| Continue conversation | Same `workflow_id`, new `user_input` |
| Resume from pause | Same `workflow_id`, provide response in inputs |
| Retry after error | Same `workflow_id`, just run again |
| Fork from any point | New `workflow_id` + spread state as inputs |
| Full fork with history | New `workflow_id` + `history=` parameter |
| Override state | Same `workflow_id` + provide values in inputs |
| Parallel processing | Different `workflow_id` per task |

---

## Getting Current State

### From RunResult

The simplest way — just access outputs from the result:

```python
result = await runner.run(graph, inputs={...}, workflow_id="session-123")

# Dict-like access
result["answer"]           # Latest value
result["messages"]         # Conversation history
"embedding" in result      # Check if output exists
```

### From Checkpointer

For inspection without running:

```python
# Get latest workflow metadata
workflow = await checkpointer.get_workflow("session-123")
if workflow:
    print(f"Status: {workflow.status}")
    print(f"Steps completed: {len(workflow.steps)}")

# Get step history
history = await checkpointer.get_history("session-123")
for step in history:
    print(f"Step {step.index}: {step.node_name} ({step.status})")
```

### List All Workflows

```python
workflows = await checkpointer.list_workflows()
for wf in workflows:
    print(f"{wf.id}: {wf.status} ({len(wf.steps)} steps)")
```

---

## Nested Graph Persistence

When using nested graphs, each nested graph gets its own workflow:

```python
rag = Graph(nodes=[embed, retrieve, generate], name="rag")
outer = Graph(nodes=[preprocess, rag.as_node(), postprocess])

result = await runner.run(outer, inputs={...}, workflow_id="order-123")

# Workflow IDs are hierarchical
# Parent: "order-123"
# Child:  "order-123/rag"
```

### Accessing Nested Results

```python
result["final_output"]          # Top-level output
result["rag"]                   # Nested RunResult
result["rag"]["embedding"]      # Output from nested graph
result["rag"].status            # RunStatus.COMPLETED
result["rag"].workflow_id       # "order-123/rag"
```

### Nested Pauses

If a nested graph pauses, the parent pauses too:

```python
if result.status == RunStatus.PAUSED:
    # Check which graph paused
    if result["review"].status == RunStatus.PAUSED:
        print(f"Review waiting for: {result['review'].pause.response_param}")
```

---

## Checkpointer Options

### SqliteCheckpointer (Development & Simple Production)

```python
from hypergraph.checkpointers import SqliteCheckpointer

runner = AsyncRunner(checkpointer=SqliteCheckpointer("./workflows.db"))
```

**Best for:** Local development, simple deployments, single-server production.

### PostgresCheckpointer (Production)

```python
from hypergraph.checkpointers import PostgresCheckpointer

runner = AsyncRunner(
    checkpointer=PostgresCheckpointer("postgresql://user:pass@host/db")
)
```

**Best for:** Multi-server deployments, high availability requirements.

### Capabilities

| Capability | Checkpointer |
|------------|:------------:|
| Resume from latest | ✅ |
| Resume from specific step | ✅ |
| Get current state | ✅ |
| List workflows | ✅ |
| Step history | ✅ |
| Automatic crash recovery | ❌ |
| Workflow forking | ❌ |

For automatic crash recovery and advanced features, use DBOS.

---

## Upgrading to DBOS

When you need automatic crash recovery, switch to `DBOSAsyncRunner`:

```python
from hypergraph.runners import DBOSAsyncRunner

# Before: Manual resume
runner = AsyncRunner(checkpointer=SqliteCheckpointer("./dev.db"))

# After: Automatic recovery
runner = DBOSAsyncRunner()  # Uses SQLite by default
# or
runner = DBOSAsyncRunner(database_url="postgresql://...")
```

### Key Differences

| Aspect | Checkpointer | DBOS |
|--------|:------------:|:----:|
| Resume | Manual (run with same `workflow_id`) | Automatic on restart |
| Human-in-the-loop | `runner.run()` with same `workflow_id` | `DBOS.send()` from external |
| Crash recovery | Manual | Automatic |
| `.iter()` streaming | ✅ | ❌ (use EventProcessor) |

### DBOS Human-in-the-Loop

With DBOS, external systems send responses via `DBOS.send()`:

```python
# Workflow pauses at InterruptNode...

# From webhook, API, or external process:
from dbos import DBOS

DBOS.send(
    destination_id="poem-456",  # workflow_id
    message={"decision": "approve"},
    topic="approval",  # InterruptNode name
)
# Workflow auto-resumes — no runner.run() needed!
```

### Advanced DBOS Features

```python
from dbos import DBOS

# Fork workflow from specific step (time travel)
DBOS.fork_workflow(
    original_workflow_id="order-123",
    start_step=2,
    new_workflow_id="order-123-retry",
)

# Durable sleep (survives crashes)
DBOS.sleep(3600)  # Sleep for 1 hour

# Durable queues with concurrency limits
queue = Queue("processing", concurrency=10)
```

---

## Comparison with LangGraph

| Aspect | LangGraph | hypergraph |
|--------|-----------|------------|
| **State definition** | Explicit `TypedDict` schema | Inferred from outputs |
| **What's persisted** | Everything in schema | Controlled by `persist` |
| **Identifier** | `thread_id` | `workflow_id` |
| **Get state** | `graph.get_state(config)` | `result.outputs` or `checkpointer.get_state()` |
| **Update state** | `graph.update_state()` | Not supported (use `InterruptNode`) |
| **Resume** | Provide `thread_id` + `checkpoint_id` | Same `workflow_id` (automatic) |
| **Human input** | `interrupt()` + `update_state()` | `InterruptNode` |
| **Reducers** | Required for shared keys | Not needed |

### Why No `update_state()`?

hypergraph intentionally doesn't have `update_state()`. Here's why:

1. **Breaks dataflow** — State should flow through nodes, not be injected externally
2. **Debugging complexity** — Hard to trace where values came from
3. **Better alternatives** — `InterruptNode` for human input, fork for retries

**Migration guide:**

| LangGraph Pattern | hypergraph Equivalent |
|-------------------|----------------------|
| `update_state()` for human input | `InterruptNode` |
| `update_state()` for retries | Fork with `get_state(at_step=N)` or DBOS `fork_workflow()` |
| `update_state()` for testing | Pass different inputs to `runner.run()` |

---

## Best Practices

### 1. Choose Meaningful Workflow IDs

```python
# ✅ Good: Meaningful, unique, traceable
workflow_id = f"order-{order_id}"
workflow_id = f"user-{user_id}-session-{session_id}"

# ❌ Bad: Random, untraceable
workflow_id = str(uuid.uuid4())
```

### 2. Use Selective Persistence

```python
# ✅ Good: Only persist what matters (output names)
graph = Graph(
    nodes=[embed, retrieve, generate],
    persist=["answer"],  # Only persist the answer output
)

# ❌ Bad: Persisting outputs that are large and reproducible
graph = Graph(
    nodes=[embed, retrieve, generate],
    # persist=None means persist everything - embedding outputs are large!
)
```

### 3. Handle Pauses Gracefully

```python
result = await runner.run(graph, inputs={...}, workflow_id="...")

match result.status:
    case RunStatus.COMPLETED:
        return result["answer"]
    case RunStatus.PAUSED:
        # Store for later, notify user
        await save_pending(result.workflow_id, result.pause)
        return {"status": "pending", "waiting_for": result.pause.response_param}
    case RunStatus.ERROR:
        # Log and handle error
        logger.error(f"Workflow failed: {result.error}")
```

### 4. Clean Up Old Workflows

```python
# Implement cleanup based on your retention policy
async def cleanup_old_workflows(days: int = 30):
    workflows = await checkpointer.list_workflows()
    cutoff = datetime.now() - timedelta(days=days)

    for wf in workflows:
        if wf.completed_at and wf.completed_at < cutoff:
            await checkpointer.delete(wf.id)
```

---

## Summary

| Concept | Description |
|---------|-------------|
| `workflow_id` | Unique identifier for a workflow execution |
| `persist` | Controls which **outputs** are checkpointed (None = all, [...] = allowlist) |
| `InterruptNode` | Pause for human input |
| `RunResult.pause` | Information about why workflow paused |
| `get_state()` | Get accumulated values at a point in time |
| `get_history()` | Get execution audit trail (step records) |
| `history` param | Inject history into a new run (seeds step index) |
| Checkpointer | Storage backend (SQLite, Postgres) |
| DBOS | Production upgrade with automatic recovery |

**The philosophy:** Outputs flow through nodes. Persistence is orthogonal — you choose what survives a crash (per-output, at Graph level). History is append-only. Human input comes through `InterruptNode`, not external state mutation.
