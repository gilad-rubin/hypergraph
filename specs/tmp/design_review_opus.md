# Hypergraph Design Review: Critical Analysis

**Date:** 2026-01-07
**Reviewer:** Claude (Opus 4.5)
**Scope:** All specs in `specs/reviewed/` + competitive framework analysis

---

## Executive Summary

Hypergraph presents a thoughtful graph-based workflow framework with a compelling "outputs ARE state" philosophy. The design shows sophisticated understanding of workflow execution, durability patterns, and observability. However, after deep analysis of the specifications and comparison with competing frameworks (LangGraph, Mastra, Temporal, DBOS, Pydantic-graph), several critical design flaws and missing capabilities emerge.

### Top 5 Critical Issues

1. **No Determinism Enforcement** - The spec doesn't require or validate workflow determinism, which will cause subtle replay bugs
2. **Missing Idempotency Primitives** - No first-class support for idempotency keys, essential for exactly-once semantics
3. **No Saga/Compensation Pattern** - No built-in rollback mechanism for partial failures
4. **Event History Limits Undefined** - No strategy for workflows with unbounded history
5. **Versioning Story is Incomplete** - `graph_hash` exists but versioning/patching patterns are underdeveloped

---

## 1. Persistence & Durability Issues

### 1.1 No Determinism Enforcement (CRITICAL)

**The Problem:**
The spec allows non-deterministic code in node functions without any guardrails:

```python
@node(output_name="choice")
def make_choice(data: dict) -> str:
    return random.choice(["option_a", "option_b"])  # NON-DETERMINISTIC!
```

On replay after a crash, this could return a different value, causing downstream nodes to behave differently than in the original run.

**What Competitors Do:**
- **Temporal:** Strictly enforces deterministic workflows. `Math.random()`, `Date.now()` are REPLACED with deterministic versions in the sandbox
- **DBOS:** Requires all non-deterministic code to live in `@step` decorated functions
- **LangGraph:** Uses `@task` decorator to mark functions whose results should be persisted

**Recommendation:**
1. Add a `@step` decorator (or reuse `cache=True`?) that marks functions whose outputs MUST be persisted for replay
2. Provide deterministic utilities: `workflow.random()`, `workflow.now()`, `workflow.uuid()`
3. Consider lint rules or static analysis to catch common non-deterministic patterns

### 1.2 Missing Idempotency Key Support (HIGH)

**The Problem:**
The spec mentions idempotency only in passing (durability.md says "uses idempotency keys for exactly-once semantics") but provides no concrete API for it.

**Real-world scenario:**
```python
@node(output_name="receipt")
async def charge_credit_card(amount: float, card: str) -> dict:
    return await payment_api.charge(amount, card)  # What if this is retried?
```

If the workflow crashes after `charge_credit_card` runs but before the step is checkpointed, a retry could double-charge the customer.

**What Competitors Do:**
- **DBOS:** Auto-generates idempotency keys per step based on workflow_id + step index
- **Temporal:** Activity IDs serve as idempotency tokens
- **Inngest:** Provides `step.idempotencyKey()` API

**Recommendation:**
1. Add `idempotency_key: str | Callable` parameter to `@node` decorator
2. Provide auto-generation option: `idempotency_key="auto"` generates from `workflow_id/superstep/node_name`
3. Document that external systems should use this key for at-least-once + key = effectively exactly-once

### 1.3 No Saga/Compensation Pattern (MEDIUM-HIGH)

**The Problem:**
There's no built-in way to roll back partial work when a workflow fails midway:

```python
# If step 3 fails, how do we undo steps 1 and 2?
@node(output_name="reservation")
def reserve_flight(): ...

@node(output_name="hotel")
def book_hotel(): ...

@node(output_name="car")
def rent_car(): ...  # FAILS - but flight is reserved, hotel is booked!
```

**What Competitors Do:**
- **Temporal:** Built-in Saga pattern support with compensating transactions
- **DBOS:** Allows defining compensation functions for each step
- **Mastra:** Documents saga patterns for complex workflows

**Recommendation:**
1. Add `compensation: Callable` parameter to `@node` or provide `@node.compensation` decorator
2. On failure, runner walks backward through completed steps calling compensations
3. Compensations must be idempotent (can be retried)

### 1.4 Event History Limits Not Defined (MEDIUM)

**The Problem:**
Long-running workflows (chat threads, schedulers, ETL) can accumulate unbounded step history. The spec mentions "state materialization for performance" but doesn't address history limits.

Temporal limits event history to 50,000 events. Exceeding this terminates the workflow.

**Recommendation:**
1. Define maximum step count limit (e.g., 50K or configurable)
2. Implement "continue-as-new" pattern: archive current workflow, start fresh with final state
3. Add `max_steps` or `max_supersteps` parameter to runners

### 1.5 Large Payload Handling is "Proposed" Not Implemented (MEDIUM)

**The Problem:**
The durability spec proposes `ArtifactRef` and `ArtifactStore` for large values but marks it as a "proposed missing piece" - not actually designed:

```python
# From durability.md - still conceptual
@dataclass(frozen=True)
class ArtifactRef:
    storage: str
    key: str
    size: int
    content_type: str
    checksum: str
```

Without this, large outputs (embeddings, dataframes, images) will bloat the checkpoint database.

**Recommendation:**
1. Fully design the ArtifactStore interface
2. Define size thresholds for auto-offloading
3. Add `artifact=True` parameter to mark outputs that should be stored externally

---

## 2. Developer Experience Issues

### 2.1 Confusing Separation: Checkpointer vs Cache (MEDIUM)

**The Problem:**
The spec has both `cache` (on nodes) and `checkpointer` (on runners) with overlapping but different purposes:

| Concept | Purpose | Granularity |
|---------|---------|-------------|
| `cache=True` on node | Skip re-execution if same inputs | Per-node |
| Checkpointer | Resume after crash, HITL | Per-workflow |

Users will ask: "If I have a checkpointer, why do I need cache? If I use cache, do I need a checkpointer?"

**LangGraph's Approach:**
Uses `@task` decorator which unifies both: "This function's result should be persisted and reused on replay."

**Recommendation:**
1. Add clearer documentation explaining when to use which
2. Consider: cache for expensive deterministic computations, checkpointer for workflow-level durability
3. Maybe unify under a single concept with different scopes?

### 2.2 Nested Graph Complexity (MEDIUM)

**The Problem:**
The nested graph handling is sophisticated but creates cognitive load:

- `workflow_id` uses `/` separator: `order-123/rag`
- `response_key` uses `.` separator: `review.decision`
- `node_name` uses `/` separator: `review/approval`

Why different separators? This will confuse users.

```python
# Is it slash or dot?
result.pause.node_name          # "review/approval" (slash)
result.pause.response_key       # "review.decision" (dot)
await checkpointer.get_state("order-123/rag")  # slash for workflow_id
```

**Recommendation:**
1. Unify on a single separator or document the rationale clearly
2. Add helper methods: `result.pause.workflow_id_for_nested_graph()`
3. Add comprehensive examples showing nested graph resume patterns

### 2.3 InterruptNode API is Non-Obvious (LOW-MEDIUM)

**The Problem:**
InterruptNode uses `input_param` and `response_param` which is correct but non-intuitive:

```python
approval = InterruptNode(
    name="approval",
    input_param="draft",        # What human sees
    response_param="decision",  # What human provides
)
```

New users might expect something more action-oriented like:
```python
approval = InterruptNode(
    name="approval",
    show="draft",       # More intuitive?
    expect="decision",
)
```

**Recommendation:**
Consider renaming or adding aliases for clarity. The current names are technically correct but `input_param` for "what to show user" is counterintuitive.

### 2.4 Multiple Ways to Run Same Code (LOW)

**The Problem:**
Same graph can be run via multiple paths:

```python
# Option 1: SyncRunner.run()
result = SyncRunner().run(graph, values={...})

# Option 2: AsyncRunner.run()
result = await AsyncRunner().run(graph, values={...})

# Option 3: runner.iter()
async for event in runner.iter(graph, values={...}):
    ...

# Option 4: runner.map()
results = runner.map(graph, values={...}, map_over="x")
```

Each returns different types (`dict`, `RunResult`, events, `list`). Not necessarily a problem, but documentation should make the mental model clear.

---

## 3. Reliability & Production Readiness Gaps

### 3.1 No Retry Configuration (HIGH)

**The Problem:**
There's no built-in retry mechanism for transient failures:

```python
@node(output_name="data")
async def call_flaky_api(url: str) -> dict:
    return await httpx.get(url).json()  # What if this fails transiently?
```

**What Competitors Do:**
- **Temporal:** Extensive retry policies: `maximum_attempts`, `initial_interval`, `backoff_coefficient`, `maximum_interval`, `non_retryable_error_types`
- **DBOS:** `@step(retries=3)` with configurable backoff
- **LangGraph:** `@task(retry=RetryPolicy(...))`

**Recommendation:**
```python
@node(output_name="data", retry=RetryPolicy(
    max_attempts=3,
    initial_interval=1.0,
    backoff_multiplier=2.0,
    max_interval=30.0,
    retryable_exceptions=[HTTPError, TimeoutError],
))
async def call_flaky_api(url: str) -> dict: ...
```

### 3.2 No Timeout Configuration (HIGH)

**The Problem:**
Nodes can run indefinitely with no timeout:

```python
@node(output_name="result")
async def long_running_task(data: dict) -> dict:
    # This could hang forever
    return await some_slow_service.process(data)
```

**What Competitors Do:**
- **Temporal:** `start_to_close_timeout`, `schedule_to_close_timeout`, `heartbeat_timeout`
- **DBOS:** Configurable timeouts per step

**Recommendation:**
1. Add `timeout: float` parameter to `@node` decorator
2. Add `default_timeout` to runner configuration
3. Raise `TimeoutError` that can trigger retries or failure handling

### 3.3 No Heartbeat Mechanism (MEDIUM)

**The Problem:**
For long-running nodes, there's no way to:
- Report progress
- Detect stuck executions
- Resume from last progress point

**Temporal's Approach:**
Activities can call `RecordHeartbeat(details)` periodically. If no heartbeat for `heartbeat_timeout`, activity is considered failed and retried.

**Recommendation:**
1. Add heartbeat API for long-running nodes
2. Allow storing progress metadata in heartbeats
3. On retry, provide last heartbeat details for resumption

### 3.4 No Dead Letter Queue / Poisoned Workflow Handling (MEDIUM)

**The Problem:**
What happens when a workflow repeatedly fails? There's no mechanism to:
- Move it to a "dead letter" state after N attempts
- Alert operators
- Prevent infinite retry loops

**Recommendation:**
1. Add `max_workflow_attempts` configuration
2. Add `on_workflow_exhausted` callback or event
3. Consider a dead letter queue pattern

### 3.5 No Graceful Shutdown (MEDIUM)

**The Problem:**
What happens when the process running workflows needs to shut down? The spec doesn't address:
- Completing in-flight nodes vs interrupting them
- Checkpointing partial progress
- Coordinating multiple workers

**Recommendation:**
1. Add `shutdown()` method to runners
2. Define shutdown modes: `immediate` (stop now) vs `graceful` (finish current superstep)
3. Document coordination patterns for multiple workers

---

## 4. API Mismatches & Redundancies

### 4.1 Inconsistent stop Behavior Documentation

**The Problem:**
The `complete_on_stop` parameter appears in multiple places with subtle differences:
- `Graph(..., complete_on_stop=)`
- `GraphNode(..., complete_on_stop=)`

The spec says GraphNode can override the Graph setting, but the interaction is complex. What happens with deeply nested graphs?

**Recommendation:**
Create a truth table showing all combinations and behaviors.

### 4.2 session_id vs workflow_id Confusion

**The Problem:**
The spec mentions both:
- `workflow_id` - for persistence, resumption
- `session_id` - for grouping related runs

But `session_id` appears only briefly in runners.md without full explanation. Are these orthogonal? Can one workflow span multiple sessions?

**Recommendation:**
Clarify the relationship and use cases for each identifier.

### 4.3 StepStatus vs RunStatus vs WorkflowStatus Overlap

**The Problem:**
Three different status enums with overlapping values:

```python
class RunStatus(Enum):     # COMPLETED, FAILED, PAUSED, STOPPED
class StepStatus(Enum):    # COMPLETED, FAILED, PAUSED, STOPPED
class WorkflowStatus(Enum): # ACTIVE, COMPLETED, FAILED
```

`RunStatus` and `StepStatus` are identical. Why both?

**Explanation from Spec:**
- `RunStatus` is for a single `.run()` invocation
- `StepStatus` is for persisted step records
- `WorkflowStatus` is for overall workflow lifecycle

**Recommendation:**
This is actually fine but could use an explanatory comment in the code/docs showing when each is used.

### 4.4 Two Ways to Get Nested Graph State

**The Problem:**
```python
# Option 1: Through parent
result["nested_graph"]["output_name"]

# Option 2: Directly via checkpointer
await checkpointer.get_state("parent-id/nested-graph")
```

Both work but could get out of sync if the mental model isn't clear.

---

## 5. Missing Features

### 5.1 No Durable Scheduling / Cron (HIGH for certain use cases)

**The Problem:**
The spec mentions that DBOSAsyncRunner supports "durable sleep/scheduling" via DBOS, but there's no native scheduling capability:

```python
# Can't do this natively:
@schedule("0 9 * * *")  # Run at 9am daily
def daily_report(): ...
```

**What Competitors Have:**
- **Temporal:** Schedules feature for cron-like recurring workflows
- **DBOS:** `@dbos.scheduled("*/5 * * * *")` decorator
- **Inngest:** Built-in cron and delayed execution

**Recommendation:**
1. If DBOS integration is the answer, document it clearly
2. Otherwise, add native scheduling support

### 5.2 No Workflow Signals / External Events (MEDIUM-HIGH)

**The Problem:**
InterruptNode handles human-in-the-loop, but there's no way for external systems to send signals to running workflows:

```python
# Can't do this:
@workflow
def order_processing(order_id: str):
    # ... processing ...
    await workflow.wait_for_signal("payment_received")
    # ... continue after payment ...
```

**What Competitors Have:**
- **Temporal:** Signals and queries for external interaction
- **DBOS:** `DBOS.recv()` / `DBOS.send()` for workflow communication

The spec mentions DBOS.recv/send but only for InterruptNode. General-purpose signals are missing.

**Recommendation:**
Add a `SignalNode` or `WaitNode` for waiting on external events beyond human input.

### 5.3 No Workflow Queries (MEDIUM)

**The Problem:**
No way to query the current state of a running workflow from outside:

```python
# Can't do this:
state = runner.query_workflow("order-123", "get_current_status")
```

**What Competitors Have:**
- **Temporal:** Queries allow reading workflow state without affecting it

**Recommendation:**
Consider adding a query mechanism, or document how to achieve this with checkpointer.

### 5.4 No Workflow Cancellation (MEDIUM)

**The Problem:**
`RunHandle.stop()` exists for streaming, but there's no way to cancel a workflow from outside:

```python
# Can't do this:
await runner.cancel_workflow("order-123")
```

**Recommendation:**
Add `cancel_workflow(workflow_id, reason)` method to runners.

### 5.5 No Multi-Tenancy Support (LOW-MEDIUM)

**The Problem:**
No built-in support for isolating workflows by tenant:
- Separate storage per tenant
- Tenant-aware querying
- Resource quotas per tenant

**Recommendation:**
Document patterns for multi-tenancy or add tenant_id as first-class concept.

### 5.6 No Built-in Rate Limiting (LOW)

**The Problem:**
`max_concurrency` limits concurrent operations but there's no rate limiting:

```python
# Can't limit to 10 requests per second to external API
@node(output_name="data", rate_limit="10/second")
async def call_external_api(): ...
```

**Recommendation:**
Consider adding rate limiting as a node option or runner configuration.

### 5.7 No Workflow Forking UI Story (LOW)

**The Problem:**
The `Checkpoint` type and fork capability exist, but there's no guidance on how to build a time-travel debugging UI:

```python
# Low-level API exists:
checkpoint = await checkpointer.get_checkpoint("order-123", superstep=5)
result = await runner.run(graph, values={...}, checkpoint=checkpoint)
```

But how do users:
- Visualize step history?
- Choose a fork point?
- Compare original vs forked execution?

**Recommendation:**
Add documentation for building time-travel UIs, or reference external tooling.

---

## 6. Design Inconsistencies

### 6.1 "Outputs ARE State" vs Persistence Everything

**Tension:**
The spec says "outputs ARE state" and "persist everything by default" but also acknowledges this is problematic:

> "Persisting everything without tiers and guardrails causes storage explosion, serialization failures, security risks, version brittleness"

The solution (ArtifactRef) is only "proposed" not designed.

### 6.2 GraphNode as Durability Boundary is Complex

**The Problem:**
Two modes (`durability="nested"` vs `durability="atomic"`) with different trade-offs:

| Capability | nested | atomic |
|------------|:------:|:------:|
| Resume inside subgraph | Yes | No |
| Exactly-once per inner step | Yes | No |
| Fork/time-travel inside | Yes | No |
| Interrupts inside | Yes | No |
| Heavy intermediates persisted | Yes | No |

This is powerful but complex. Users need to understand the implications.

**Recommendation:**
Default to `nested` (full capabilities) and only recommend `atomic` for specific performance optimization cases.

### 6.3 Event System Limitations

**The Problem:**
- Events are ephemeral (not persisted by default)
- DaftRunner doesn't emit events
- DBOSAsyncRunner doesn't emit events (delegates to DBOS)

This means observability depends on which runner you use.

**Recommendation:**
Consider adding optional event persistence or a way to reconstruct events from steps.

---

## 7. Competitive Analysis Insights

### 7.1 What LangGraph Does Better

1. **Task Decorator:** Unifies caching and durability for non-deterministic operations
2. **Memory Management:** Built-in conversation memory patterns
3. **Streaming Modes:** Multiple streaming modes (`values`, `updates`, `debug`, `events`)
4. **Subgraph Communication:** Cleaner parent-child state passing

### 7.2 What Mastra Does Better

1. **Suspend/Resume Schema:** Typed schemas for suspension payloads
2. **Inngest Integration:** Production-grade durability via external service
3. **Step-level Control Flow:** More explicit branching/looping primitives

### 7.3 What Temporal/DBOS Do Better

1. **Determinism Enforcement:** Cannot write non-deterministic workflow code
2. **Activity Separation:** Clear boundary between orchestration and side effects
3. **Versioning:** Sophisticated patching and worker versioning for safe deployments
4. **Retry Policies:** Comprehensive retry configuration
5. **Heartbeats:** Progress reporting for long-running activities

### 7.4 What pydantic-graph Does Better

1. **Simplicity:** Minimal API surface
2. **Type Safety:** Pydantic validation throughout
3. **DBOS Integration:** Clean mapping to durable execution

---

## 8. Recommendations Summary

### Must Fix (Before v1)

| Issue | Priority | Effort | Impact |
|-------|:--------:|:------:|:------:|
| Add determinism utilities or `@step` decorator | Critical | Medium | High |
| Add idempotency key support | Critical | Low | High |
| Add retry configuration | High | Medium | High |
| Add timeout configuration | High | Low | High |
| Design ArtifactStore for large values | High | Medium | High |

### Should Fix (v1 or soon after)

| Issue | Priority | Effort | Impact |
|-------|:--------:|:------:|:------:|
| Add saga/compensation pattern | Medium-High | High | Medium |
| Add heartbeat mechanism | Medium | Medium | Medium |
| Add external signals/events | Medium | Medium | Medium |
| Define event history limits | Medium | Low | Medium |
| Add workflow cancellation API | Medium | Low | Medium |

### Nice to Have (Future)

| Issue | Priority | Effort | Impact |
|-------|:--------:|:------:|:------:|
| Native scheduling support | Low-Medium | Medium | Low |
| Workflow queries | Low | Low | Low |
| Built-in rate limiting | Low | Low | Low |
| Multi-tenancy patterns | Low | Medium | Low |

---

## 9. Conclusion

Hypergraph has a strong foundation with its "outputs ARE state" philosophy, clean separation of graph/runner concerns, and sophisticated nested graph support. However, several gaps must be addressed for production readiness:

1. **Durability gaps** around determinism, idempotency, and compensation
2. **Reliability gaps** around retries, timeouts, and heartbeats
3. **Missing primitives** like external signals and workflow cancellation

The good news: most of these are additive - they can be added without breaking the existing design. The core architecture is sound.

The framework should learn from Temporal's strict determinism enforcement while maintaining the simpler mental model of "just write nodes, the framework handles durability." The key insight from competitors: **determinism is the foundation of durable execution**. Non-deterministic work must be isolated and its results persisted.

---

## Appendix A: Research Sources

### Frameworks Analyzed
- LangGraph (Python/TypeScript) - https://github.com/langchain-ai/langgraph
- Mastra (TypeScript) - https://mastra.ai
- Pydantic-graph (Python) - https://ai.pydantic.dev/graph/
- Temporal (Go/Java/Python/TypeScript) - https://temporal.io
- DBOS (Python/TypeScript) - https://dbos.dev

### Key References
- "Demystifying Determinism in Durable Execution" - Jack Vanlightly
- Temporal Anti-Patterns Blog Series
- DBOS Architecture Documentation
- LangGraph v1 Roadmap GitHub Issue

### Files Reviewed
- specs/reviewed/graph.md
- specs/reviewed/node-types.md
- specs/reviewed/state-model.md
- specs/reviewed/runners.md
- specs/reviewed/runners-api-reference.md
- specs/reviewed/execution-types.md
- specs/reviewed/persistence.md
- specs/reviewed/checkpointer.md
- specs/reviewed/durable-execution.md
- specs/reviewed/observability.md
- specs/reviewed/durability.md
