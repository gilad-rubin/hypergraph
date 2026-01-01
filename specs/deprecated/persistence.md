# Persistence & Durable Execution

**Make workflows resilient to any failure. Resume from crashes, interrupts, or restarts without losing progress.**

---

## Quick Example

```python
from hypergraph import Graph, AsyncRunner
from hypergraph.persistence import SQLiteCheckpointer

# Create graph with durable execution
graph = Graph(nodes=[fetch, process, generate])
checkpointer = SQLiteCheckpointer("./workflows.db")

runner = AsyncRunner(checkpointer=checkpointer)

# First run - interrupted by user
result = await runner.run(
    graph,
    inputs={"query": "hello"},
    workflow_id="conversation-123",
)

if result.interrupted:
    # Save checkpoint, close app, come back tomorrow...
    print(f"Paused at: {result.interrupt_name}")

# Resume exactly where we left off
result = await runner.run(
    graph,
    inputs={"user_decision": "approve"},
    workflow_id="conversation-123",
    resume=True,  # Load last checkpoint
)
```

---

## Design Influences

This design is informed by production-proven patterns from:

- **[Temporal](https://temporal.io)** — Event history, replay, heartbeats for long-running activities
- **[DBOS](https://dbos.dev)** — Step-level checkpointing, `fork_workflow` for recovery
- **[Inngest](https://inngest.com)** — Step IDs for memoization, simple developer experience
- **[LangGraph](https://langchain.com/langgraph)** — Pending writes, subgraph persistence propagation
- **[Mastra](https://mastra.ai)** — Suspend/resume schemas, structured suspension context, `bail()` for graceful exit

---

## Why Durable Execution?

| Problem | Solution |
|---------|----------|
| Server crashes mid-workflow | Resume from last completed step |
| User closes browser during approval | Resume when they return |
| Network timeout calling external API | Retry with backoff, don't re-run completed steps |
| Debug production issue | Replay exact execution, inspect every step |
| Explore "what if" scenarios | Fork from any checkpoint, try different paths |

---

## Core Concepts

### Workflows and Steps

hypergraph borrows proven concepts from [Temporal](https://temporal.io), [DBOS](https://dbos.dev), and [Inngest](https://inngest.com):

| Concept | hypergraph | Temporal | DBOS | Inngest |
|---------|------------|----------|------|---------|
| **Durable identity** | `workflow_id` | `workflow_id` | `workflow_uuid` | `function_id` |
| **Single execution** | `run_id` | `run_id` | `workflow_id` | `run_id` |
| **Atomic work unit** | Step | Activity | Step | Step |
| **Execution history** | Step History | Event History | Step Results | State Store |

**A workflow is a graph execution identified by `workflow_id`.** It can span multiple runs (resume after interrupt), crash and recover, or fork into alternative paths.

**A step is a single node execution.** Steps are the atomic unit of persistence — when a step completes, its result is saved. If the workflow crashes, completed steps don't re-execute.

### The Execution Model

```
workflow_id: "order-12345"
├── run_id: "run_001"           ← First execution
│   ├── step: fetch:0           ✓ completed
│   ├── step: validate:1        ✓ completed
│   └── step: approval:2        ⏸ interrupted
│
└── run_id: "run_002"           ← Resume after user approves
    ├── step: fetch:0           ⟲ replayed (cached)
    ├── step: validate:1        ⟲ replayed (cached)
    ├── step: approval:2        ⟲ replayed (cached)
    ├── step: process:3         ✓ completed
    └── step: notify:4          ✓ completed
```

**On resume, hypergraph replays from the beginning** but uses cached results for completed steps. This matches [Temporal's replay model](https://docs.temporal.io/encyclopedia/event-history) and [DBOS's recovery mechanism](https://docs.dbos.dev/architecture).

---

## Workflow Identity and Idempotency

### workflow_id as Idempotency Key

The `workflow_id` serves as the **idempotency key** for workflow execution. This is the foundation of exactly-once semantics:

```python
# Same workflow_id = same logical workflow
# Only one can be "open" (running/interrupted) at a time

result1 = await runner.run(graph, inputs={...}, workflow_id="order-123")
result2 = await runner.run(graph, inputs={...}, workflow_id="order-123")
# result2 returns cached result, doesn't re-execute
```

This matches [DBOS's idempotency model](https://docs.dbos.dev/python/tutorials/workflow-tutorial): "An assigned workflow ID acts as an idempotency key: if a workflow is called multiple times with the same ID, it executes only once."

### Workflow ID Reuse Policies

Control what happens when a `workflow_id` is reused:

| Policy | Behavior | Use Case |
|--------|----------|----------|
| `reject_duplicate` | Error if workflow exists (any status) | Strictly unique operations |
| `allow_if_failed` | Allow only if previous failed/cancelled | Retry failed workflows |
| `terminate_running` | Terminate existing, start new | Replace running workflow |
| `return_existing` | Return existing result if complete | **Default** — idempotent |

```python
runner = AsyncRunner(
    checkpointer=SQLiteCheckpointer("./db.sqlite"),
    workflow_reuse_policy="return_existing",  # Default
)
```

This mirrors [Temporal's Workflow ID Reuse Policy](https://docs.temporal.io/workflow-execution/workflowid-runid).

### Cross-Run Memoization

When a workflow resumes (new `run_id`, same `workflow_id`), step results are looked up by:

```python
cache_key = (workflow_id, step_identity, inputs_hash)
```

The `workflow_id` scopes the cache — run_002 can find run_001's cached results because they share the same `workflow_id`.

---

## Exactly-Once Semantics

hypergraph provides **exactly-once step completion** through atomic checkpointing:

### How It Works

1. **Step starts** → Status set to `running`
2. **Step code executes** → Side effects happen here
3. **Step completes atomically** → Outputs + status saved in single transaction
4. **On replay** → Completed steps return cached result, code doesn't run

```python
# This is what the runner does internally
async with checkpointer.atomic_step_completion(run_id, step_id) as step:
    # If step already completed, this context manager returns cached result
    if step.cached:
        return step.outputs

    # Otherwise, execute and commit atomically
    result = await node.execute(inputs)
    step.set_outputs(result)
    # On context exit: outputs + status=completed saved atomically
```

### The Crash Window Problem

Without atomic completion, there's a dangerous window:

```
1. Step executes (side effect: email sent)
2. ← CRASH HERE →
3. Outputs saved, status=completed
```

If crash happens at step 2, the step will re-run on recovery, sending the email twice.

**Solution: Atomic step completion.** The status change and output save happen in a single database transaction. Either both succeed or neither does. This matches [DBOS's guarantee](https://docs.dbos.dev/python/tutorials/workflow-tutorial): "Transactions commit exactly once."

### What This Means for Your Code

| Guarantee | Description |
|-----------|-------------|
| **Steps complete exactly once** | Once a step's outputs are saved, it never re-executes |
| **Retries are visible** | Failed attempts are recorded before retry |
| **Side effects need idempotency** | If step crashes mid-execution, it may retry |

---

## Idempotency Requirements

**Steps should be idempotent** — running them multiple times with the same inputs produces the same result without unwanted side effects.

### Why Idempotency Matters

```
Step execution timeline:
1. Step starts
2. Step calls external API (email sent!) ← side effect
3. Step crashes before saving outputs
4. Recovery: step retries from beginning
5. Step calls external API again (duplicate email!)
```

The exactly-once guarantee applies to **step completion**, not to the side effects within a step. This is the same constraint as [Temporal activities](https://temporal.io/blog/idempotency-and-durable-execution) and [DBOS steps](https://docs.dbos.dev/python/tutorials/workflow-tutorial).

### Making Steps Idempotent

**Naturally idempotent operations:**
```python
@node(outputs="result")
async def upsert_user(user_id: str, data: dict) -> dict:
    # UPSERT is naturally idempotent
    return await db.upsert("users", {"id": user_id}, data)
```

**Using idempotency keys for external services:**
```python
@node(outputs="result")
async def charge_payment(order_id: str, amount: float) -> dict:
    # Stripe uses idempotency keys to prevent duplicate charges
    return await stripe.charges.create(
        amount=int(amount * 100),
        idempotency_key=f"order-{order_id}",  # Same key = same result
    )
```

**One side effect per step:**
```python
# ❌ BAD: Multiple side effects in one step
@node(outputs="result")
async def process_order(order: dict) -> dict:
    await charge_payment(order)  # If this succeeds...
    await send_email(order)      # ...but this crashes, payment is charged twice on retry
    return {"status": "done"}

# ✓ GOOD: Split into separate steps
@node(outputs="payment")
async def charge_payment(order: dict) -> dict:
    return await stripe.charge(...)

@node(outputs="email_sent")
async def send_confirmation(order: dict, payment: dict) -> bool:
    return await email.send(...)
```

This matches [Inngest's recommendation](https://www.inngest.com/docs/reference/functions/step-run): "Each step should have a single side effect."

---

## Serialization and Payload Limits

### Supported Types

hypergraph uses JSON-compatible serialization by default, with msgpack for efficiency:

| Type | Support |
|------|---------|
| Primitives (str, int, float, bool, None) | ✓ Native |
| Lists, dicts | ✓ Native |
| Dataclasses, Pydantic models | ✓ Automatic |
| datetime, UUID, Enum | ✓ Built-in codecs |
| bytes | ✓ Base64 encoded |
| Custom classes | Requires custom codec |

### Payload Size Limits

Following [Temporal's blob size limits](https://docs.temporal.io/cloud/limits):

| Limit | Value | Behavior |
|-------|-------|----------|
| **Warning threshold** | 256 KB | Log warning |
| **Error threshold** | 2 MB | Raise `PayloadTooLargeError` |
| **History limit** | 50 MB total | Use external storage |

```python
# Configure limits
runner = AsyncRunner(
    checkpointer=SQLiteCheckpointer("./db.sqlite"),
    max_payload_size=2 * 1024 * 1024,  # 2 MB default
    payload_warning_size=256 * 1024,   # 256 KB default
)
```

### Large Payloads

For data exceeding limits, store externally and pass references:

```python
@node(outputs="data_ref")
async def process_large_file(file_path: str) -> str:
    data = await process(file_path)

    # Store in blob storage, return reference
    blob_url = await s3.upload(data)
    return blob_url  # Small reference, not the data

@node(outputs="result")
async def use_data(data_ref: str) -> dict:
    data = await s3.download(data_ref)
    return analyze(data)
```

This is [Temporal's recommended pattern](https://community.temporal.io/t/best-practice-to-handle-large-workflow-activity-payload-blob-size-limit-2mb/9814) for large payloads.

### Custom Serialization

Register custom codecs for unsupported types:

```python
from hypergraph.persistence import Serializer

serializer = Serializer()

@serializer.register(pd.DataFrame)
def serialize_dataframe(df: pd.DataFrame) -> bytes:
    return df.to_parquet()

@serializer.decoder(pd.DataFrame)
def deserialize_dataframe(data: bytes) -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(data))

runner = AsyncRunner(
    checkpointer=SQLiteCheckpointer("./db.sqlite", serializer=serializer),
)
```

---

## Versioning and Code Changes

Workflow code must be **deterministic** — but code evolves. hypergraph supports safe code changes through versioning.

### Safe Changes (No Action Needed)

- Adding new steps after existing ones
- Changing step implementation (same inputs/outputs)
- Adding optional parameters with defaults

### Unsafe Changes (Require Versioning)

- Removing steps
- Reordering steps
- Changing step inputs/outputs
- Renaming steps

### Step-Level Patching

Inspired by [Temporal's patching](https://docs.temporal.io/develop/python/versioning), use patches for in-flight workflow compatibility:

```python
@node(outputs="result")
async def process_data(data: dict) -> dict:
    if workflow.patched("v2-new-algorithm"):
        # New code path for new workflows
        return new_algorithm(data)
    else:
        # Old code path for in-flight workflows
        return old_algorithm(data)
```

**Patch lifecycle:**
1. Deploy with `patched()` check — new workflows use new path
2. Wait for old workflows to complete
3. Remove old code, replace `patched()` with `deprecate_patch()`
4. After retention period, remove patch entirely

### Workflow Type Versioning

For major changes, create a new workflow type:

```python
# v1 — keep running for existing workflows
graph_v1 = Graph(nodes=[...], name="order_processing_v1")

# v2 — new workflows use this
graph_v2 = Graph(nodes=[...], name="order_processing_v2")
```

### Step ID Stability

Step IDs are the memoization key. [Inngest's approach](https://www.inngest.com/docs/learn/inngest-steps): "The ID is used to memoize step state across function versions."

```python
# Changing step name forces re-execution
@node(outputs="result", name="process_v1")  # Explicit stable name
async def process(data: dict) -> dict:
    ...
```

---

## Distributed Execution

> **Note:** Multi-worker distributed execution is a future capability. This section documents the design for correctness.

### Worker Coordination

For multi-worker deployments, workers coordinate through the checkpointer:

```python
# Worker claims next available step with lease
step = await checkpointer.claim_step(
    run_id=run_id,
    worker_id="worker-1",
    lease_ttl=30,  # 30 second lease
)

if step:
    async with step.execution_lease() as lease:
        # Renew lease periodically during long operations
        await lease.renew()
        result = await execute(step)
```

### Leases and Heartbeats

[Leases prevent stuck steps](https://www.linkedin.com/pulse/leases-fences-distributed-designpatterns-pratik-pandey):

| Concept | Purpose |
|---------|---------|
| **Lease** | Time-bound lock on a step |
| **Heartbeat** | Renew lease during execution |
| **TTL** | Auto-release if worker dies |

```python
# Lease configuration
checkpointer = PostgresCheckpointer(
    connection_string="...",
    lease_ttl=30,           # 30 second lease
    heartbeat_interval=10,  # Renew every 10 seconds
)
```

### Workflow-Level Locking

Only one worker can resume a workflow at a time:

```python
# Acquire workflow lock before resuming
async with checkpointer.workflow_lock(workflow_id, ttl=60):
    result = await runner.run(graph, workflow_id=workflow_id, resume=True)
```

Uses PostgreSQL's `FOR UPDATE SKIP LOCKED` or Redis distributed locks.

---

## What Gets Persisted?

### Step Snapshots

Every step execution creates a snapshot:

```python
StepSnapshot:
  step_id: "generate:3"         # Node name + batch index
  status: completed | failed | interrupted
  inputs_hash: "a1b2c3"         # For cache matching
  outputs: {"response": "..."}  # The result
  attempts: [...]               # Retry history
  duration_ms: 1234.5
```

### Pending Writes (Parallel Steps)

When running parallel steps, completed ones are saved immediately as **pending writes**:

```
Batch 2: [fetch_a, fetch_b, fetch_c] running in parallel

Timeline:
  t=0:   All three start
  t=100: fetch_a completes → pending write saved
  t=200: fetch_c completes → pending write saved
  t=250: CRASH!

On resume:
  - fetch_a: cached (from pending write)
  - fetch_c: cached (from pending write)
  - fetch_b: re-executed (no pending write)
```

This matches [LangGraph's pending_writes](https://docs.langchain.com/oss/python/langgraph/persistence) pattern.

### Streaming Progress

**Principle: Resume from what the user saw.**

If a user watches streaming output and the app crashes, they expect to continue from exactly where they left off — not re-watch content they already saw. The persistence mode controls this trade-off:

| Mode | Behavior | Trade-off |
|------|----------|-----------|
| **`immediate`** | Persist each chunk as it's sent to user | Highest durability, more I/O |
| **`batched`** | Persist every N chunks or N seconds | Balanced (may replay a few chunks) |
| **`on_complete`** | Only persist when stream finishes | Fastest, but lose all on crash |

```python
from hypergraph.persistence import StreamPersistenceMode

runner = AsyncRunner(
    checkpointer=SQLiteCheckpointer("./db.sqlite"),
    stream_persistence=StreamPersistenceMode.IMMEDIATE,  # Default
)
```

**Default is `immediate`** — what the user saw is what gets saved. Choose `batched` for high-volume scenarios where replaying a few chunks is acceptable.

```python
StreamingState:
  step_id: "generate:3"
  chunks: ["The ", "answer ", "is ", ...]
  chunk_count: 8000
  last_persisted_index: 8000   # With immediate mode, always current
```

This is inspired by [Temporal's heartbeat mechanism](https://docs.temporal.io/encyclopedia/detecting-activity-failures) for long-running activities, with the key difference that we default to immediate persistence to match user expectations.

### Checkpoints

Checkpoints are snapshots of complete workflow state at key boundaries:

```python
Checkpoint:
  workflow_id: "order-12345"
  run_id: "run_001"
  batch_index: 3                # Current position
  values: {...}                 # All accumulated outputs
  pending_interrupt: "approval" # If paused
  nested_runs: {...}            # Child graph run IDs
```

---

## Durability Modes

Choose the trade-off between safety and performance:

| Mode | When Persisted | Use Case |
|------|----------------|----------|
| **`sync`** | Immediately, blocks until saved | Payment processing, critical workflows |
| **`async`** | Asynchronously, during next step | General workloads (good balance) |
| **`exit`** | Only on completion/interrupt | High-throughput batch processing |

```python
runner = AsyncRunner(
    checkpointer=SQLiteCheckpointer("./db.sqlite"),
    durability_mode="sync",  # Maximum safety
)
```

This matches [LangGraph's durability modes](https://docs.langchain.com/oss/python/langgraph/durable-execution).

---

## Structured Interrupts (Suspend/Resume)

Inspired by [Mastra's suspend/resume pattern](https://mastra.ai/docs/v1/workflows/suspend-and-resume), interrupts have clear input/output schemas:

### Suspend Context

When an interrupt pauses execution, it provides structured context explaining **why** it paused and **what's needed**:

```python
approval = InterruptNode(
    name="approval",
    # What to show the user
    input_param="approval_prompt",
    # Schema for what we need back
    response_schema=ApprovalResponse,
    # Optional: structured reason for suspension
    suspend_schema=SuspendReason,
)

@dataclass
class SuspendReason:
    reason: str
    required_fields: list[str]
    deadline: datetime | None = None

@dataclass
class ApprovalResponse:
    approved: bool
    feedback: str | None = None
```

### Accessing Suspend Context

```python
result = await runner.run(graph, inputs={...})

if result.interrupted:
    # Structured suspend context
    print(result.suspend_payload.reason)
    print(result.suspend_payload.required_fields)

    # What we need to resume
    print(result.resume_schema)  # ApprovalResponse
```

### Graceful Exit with `bail()`

Sometimes users reject an action and the workflow should exit gracefully without error. Inspired by [Mastra's bail()](https://mastra.ai/docs/v1/workflows/human-in-the-loop):

```python
@node(outputs="result")
async def process_with_approval(data: dict, user_decision: ApprovalResponse) -> dict:
    if not user_decision.approved:
        # Exit gracefully without error
        bail(reason="User rejected the action", outputs={"status": "rejected"})

    return process(data)
```

The workflow completes with `status="bailed"` — not an error, but an intentional early exit.

---

## Retry Handling

Steps can fail and retry automatically:

```python
@node(
    outputs="result",
    retry=RetryPolicy(
        max_attempts=3,
        initial_delay=1.0,
        backoff_multiplier=2.0,
        retryable_exceptions=(TimeoutError, ConnectionError),
    )
)
async def call_api(query: str) -> dict:
    return await external_api.call(query)
```

**Retry behavior:**

1. Step fails with retryable exception
2. Wait `initial_delay` seconds
3. Retry (attempt 2)
4. If fails again, wait `initial_delay * backoff_multiplier`
5. Continue until `max_attempts` reached
6. If all attempts fail, workflow fails (or handles error)

Each attempt is recorded:

```python
StepSnapshot.attempts = [
    Attempt(number=1, status="failed", error="Connection timeout"),
    Attempt(number=2, status="failed", error="Connection timeout"),
    Attempt(number=3, status="success", outputs={...}),
]
```

---

## Nested Graphs (Hierarchical Execution)

Nested graphs create hierarchical step identities:

```python
# Nested graph structure
outer_graph = Graph(nodes=[
    preprocess,
    rag_pipeline.as_node(name="rag"),  # Nested graph
    postprocess,
])
```

Step IDs form a path:

```
outer:0/preprocess:0           # Top level
outer:0/rag:1/embed:0          # Inside rag_pipeline
outer:0/rag:1/retrieve:1       # Inside rag_pipeline
outer:0/rag:1/generate:2       # Inside rag_pipeline
outer:0/postprocess:2          # Top level
```

**Checkpointer propagates automatically** — you only configure it on the outer runner:

```python
runner = AsyncRunner(checkpointer=SQLiteCheckpointer("./db.sqlite"))
result = await runner.run(outer_graph, inputs={...})
# All nested graphs use the same checkpointer
```

This matches [LangGraph's subgraph persistence](https://docs.langchain.com/oss/python/langgraph/use-subgraphs).

---

## Time Travel

Inspired by [Mastra's time-travel](https://mastra.ai/docs/v1/workflows/time-travel), [DBOS's fork_workflow](https://www.dbos.dev/blog/handling-failures-workflow-forks), and [LangGraph's update_state](https://docs.langchain.com/oss/python/langgraph/use-time-travel).

### View Execution History

```python
# Get all checkpoints for a workflow
history = await checkpointer.get_history("order-12345")

for checkpoint in history:
    print(f"{checkpoint.checkpoint_id}: batch {checkpoint.batch_index}")
    print(f"  Status: {checkpoint.status}")
    print(f"  Values: {list(checkpoint.values.keys())}")
```

### Inspect Steps

```python
# Get all steps for a specific run
steps = await checkpointer.load_all_steps("run_001")

for step in steps:
    print(f"{step.step_id}: {step.status}")
    if step.status == "failed":
        print(f"  Error: {step.last_error}")
        print(f"  Attempts: {step.attempt_count}")

    # Detailed step context (inspired by Mastra)
    print(f"  Inputs: {step.inputs_hash}")
    print(f"  Duration: {step.total_duration_ms}ms")
    if step.streaming_state:
        print(f"  Streamed: {step.streaming_state.chunk_count} chunks")
```

### Time Travel to Specific Step

Re-execute from any step without starting over (Mastra's pattern):

```python
# Replay from a specific step
result = await runner.time_travel(
    graph,
    workflow_id="order-12345",
    target_step="process:2",      # Start here
    inputs={"temperature": 0.9},  # Override inputs
)

# Target nested steps with path notation
result = await runner.time_travel(
    graph,
    workflow_id="order-12345",
    target_step="rag:1/generate:0",  # Nested step
)
```

**Use cases:**
- **Failed step recovery** — Re-run with corrected inputs
- **Transient failure retry** — Retry after network/rate-limit issues
- **Testing specific steps** — Execute one step with custom data

### Fork from Checkpoint

Explore alternative paths without affecting the original:

```python
# Fork from a specific checkpoint
forked = await checkpointer.fork_from(
    checkpoint_id="chk_abc123",
    new_workflow_id="order-12345-retry",
)

# Continue with different inputs
result = await runner.run(
    graph,
    inputs={"user_decision": "reject"},  # Try different path
    checkpoint=forked,
)
```

### Modify State and Resume

```python
# Update state at a checkpoint
modified = await checkpointer.update_state(
    checkpoint_id="chk_abc123",
    updates={"temperature": 0.9},  # Change a value
)

# Resume with modified state
result = await runner.run(graph, checkpoint=modified)
```

---

## Determinism Requirements

For safe replay, workflows must be **deterministic**: given the same inputs and step results, they should make the same decisions.

**DO** put non-deterministic operations in nodes:

```python
@node(outputs="timestamp")
def get_time() -> float:
    return time.time()  # ✓ Result is cached on replay

@node(outputs="data")
async def fetch_api(url: str) -> dict:
    return await httpx.get(url).json()  # ✓ Result is cached on replay
```

**DON'T** use non-deterministic logic in gates:

```python
# ❌ BAD: Gate decision depends on current time
@route(targets=["day_flow", "night_flow"])
def choose_flow(data: dict) -> str:
    if datetime.now().hour < 12:  # Non-deterministic!
        return "day_flow"
    return "night_flow"

# ✓ GOOD: Gate decision based on persisted value
@route(targets=["day_flow", "night_flow"])
def choose_flow(data: dict, time_of_day: str) -> str:
    if time_of_day == "morning":  # Deterministic (from previous step)
        return "day_flow"
    return "night_flow"
```

This requirement is shared by [Temporal](https://docs.temporal.io/workflows#determinism), [DBOS](https://docs.dbos.dev/python/tutorials/workflow-tutorial), and [Inngest](https://www.inngest.com/docs/learn/how-functions-are-executed).

---

## Checkpointer Implementations

| Implementation | Use Case | Persistence |
|----------------|----------|-------------|
| `MemoryCheckpointer` | Testing, development | In-memory (lost on restart) |
| `FileCheckpointer` | Simple apps, prototypes | JSON files |
| `SQLiteCheckpointer` | Single-server production | SQLite database |
| `PostgresCheckpointer` | Multi-server production | PostgreSQL |
| `RedisCheckpointer` | High-throughput | Redis |

**Bring your own:** Implement the `Checkpointer` protocol for custom backends.

---

## When to Use Persistence

| Scenario | Recommendation |
|----------|----------------|
| Short-lived, stateless API calls | Skip persistence (overhead not worth it) |
| Human-in-the-loop approvals | **Use persistence** (may wait hours/days) |
| Multi-step LLM workflows | **Use persistence** (expensive to re-run) |
| Critical business workflows | **Use persistence** (must not lose progress) |
| Batch processing millions of items | Consider `durability_mode="exit"` |
| Development/debugging | Use `MemoryCheckpointer` or `FileCheckpointer` |

---

## Summary

| Feature | How It Works |
|---------|--------------|
| **Exactly-once steps** | Atomic step completion — outputs + status in single transaction |
| **Workflow idempotency** | `workflow_id` as idempotency key, reuse policies control behavior |
| **Cross-run memoization** | Cache key = `(workflow_id, step_identity, inputs_hash)` |
| **Resume after crash** | Replay from start, skip completed steps (cached) |
| **Resume after interrupt** | Load checkpoint, continue from interrupt point |
| **Structured interrupts** | `suspend_payload` explains why, `resume_schema` validates input |
| **Graceful exit** | `bail()` exits without error when user rejects |
| **Parallel step recovery** | Pending writes save completed steps before batch finishes |
| **Streaming recovery** | Immediate persistence by default (what user saw = saved) |
| **Retry handling** | Configurable policy with backoff, attempts tracked |
| **Nested graphs** | Hierarchical step IDs, checkpointer propagates |
| **Time travel** | `time_travel()` to any step, fork, modify state |
| **Payload limits** | 2 MB per payload, 50 MB total; external storage for large data |
| **Versioning** | Patching API for in-flight compatibility, step ID stability |
| **Distributed (future)** | Leases, heartbeats, workflow locks for multi-worker |

---

**See also:**
- [Persistence API Reference](../api/persistence-api.md) — Complete protocol and type definitions
- [Execution Types](../api/execution-types.md) — Events and runtime types
- [Runners](../api/runners.md) — How runners integrate with persistence
