# Consolidated Design Review: Hypergraph Framework

**Date:** 2026-01-07
**Sources:** Gemini, Opus 4.5, GPT-o3, Composer
**Legend:** 🔴 Critical | 🟠 High | 🟡 Medium | 🟢 Low

---

## Executive Summary

Four independent AI reviews identified overlapping concerns around:

1. **Persistence gaps** - Determinism, routing decisions, artifact storage, history limits
2. **Production readiness** - Missing retry/timeout, no cancellation, no health checks
3. **API inconsistencies** - Forking API, iter() shape, separators, return types
4. **DX friction** - String-typed wiring, complex value resolution, missing testing utilities

---

## 1. Persistence & Durability

### 🔴 1.1 No Determinism Enforcement
**Identified by:** Opus, GPT

Non-deterministic code in nodes will cause replay bugs after crashes.

```python
@node(output_name="choice")
def make_choice(data: dict) -> str:
    return random.choice(["option_a", "option_b"])  # Breaks on replay!
```

**Recommendations:**
- Add `@step` decorator for non-deterministic operations (Opus)
- Provide deterministic utilities: `workflow.random()`, `workflow.now()` (Opus)
- Require gate functions to be pure or persist decisions (GPT)

**Refs:** [execution-types.md](specs/reviewed/execution-types.md), [durable-execution.md](specs/reviewed/durable-execution.md)

---

### 🔴 1.2 Routing Decisions Not Durable
**Identified by:** GPT

Gates (`RouteNode`, `BranchNode`) have `outputs=()` and `RouteDecisionEvent` is non-durable. Crash after gate but before target = different branch on resume.

**Recommendations:**
- Persist decision in `StepRecord.values` under reserved key `{"__route__": "A"}` (GPT)
- Add explicit `decision` field on StepRecord for gates (GPT)

**Refs:** [node-types.md](specs/reviewed/node-types.md), [checkpointer.md](specs/reviewed/checkpointer.md)

---

### 🔴 1.3 Missing Idempotency Key Support
**Identified by:** Opus

No API for idempotency keys = double-charging customers on retry.

```python
@node(output_name="receipt")
async def charge_card(amount: float) -> dict:
    return await payment_api.charge(amount)  # Retried = double charge!
```

**Recommendations:**
- Add `idempotency_key: str | Callable` to `@node` (Opus)
- Auto-generate from `workflow_id/superstep/node_name` (Opus)

**Refs:** [durable-execution.md](specs/reviewed/durable-execution.md)

---

### 🔴 1.4 Infinite History / Storage Leak
**Identified by:** Gemini, Opus

Cyclical graphs generate unbounded `StepRecords`. A monitor agent running for weeks = millions of steps.

**Recommendations:**
- Introduce step pruning / log compaction with TTL (Gemini)
- Implement "continue-as-new" pattern (Opus)
- Define max step count limit (e.g., 50K like Temporal) (Opus)

**Refs:** [checkpointer.md](specs/reviewed/checkpointer.md), [persistence.md](specs/reviewed/persistence.md)

---

### 🟠 1.5 Missing Artifact Storage Layer
**Identified by:** Gemini, Opus, GPT, Composer

Large values (embeddings, dataframes) will bloat DB. `ArtifactRef` is proposed but not integrated.

**Recommendations:**
- Add `ArtifactStore` + `ArtifactRef` as first-class (all reviewers)
- Add size thresholds for auto-offloading (Opus)
- Add `put_artifact()` / `get_artifact()` to Checkpointer interface (Composer)

**Refs:** [durability.md](specs/reviewed/durability.md), [checkpointer.md](specs/reviewed/checkpointer.md)

---

### 🟠 1.6 Serialization Security Posture Unclear
**Identified by:** Gemini, GPT, Composer

Pickle = RCE risk. Default serializer and security implications not documented.

**Recommendations:**
- Safe default (JSON/MsgPack) - pickle behind explicit flag (GPT)
- Attach version metadata to payloads (GPT)
- Add codec layer for compression/encryption (GPT)
- Enforce Pydantic models for inputs/outputs (Gemini)

**Refs:** [durability.md](specs/reviewed/durability.md), [checkpointer.md](specs/reviewed/checkpointer.md)

---

### 🟠 1.7 Deadlock by Skipping (Branch Not Taken)
**Identified by:** Gemini

Node D needs inputs from B and C. Gate routes to B, skips C. Node D waits forever.

**Recommendations:**
- Introduce Optional Inputs or Skip Propagation (Gemini)
- Propagate "Signal: Skipped" token to downstream consumers (Gemini)

**Refs:** [node-types.md](specs/reviewed/node-types.md), [runners.md](specs/reviewed/runners.md)

---

### 🟠 1.8 Nested Graph Atomicity Window
**Identified by:** GPT

Child workflow completes → crash before parent step saved → resume may duplicate child work.

**Recommendations:**
- Make child id deterministic + specify idempotent behavior (GPT)
- If child exists and completed, treat GraphNode as "replayed" (GPT)

**Refs:** [node-types.md](specs/reviewed/node-types.md), [checkpointer.md](specs/reviewed/checkpointer.md)

---

### 🟡 1.9 Interrupt Persistence Model Incomplete
**Identified by:** GPT

When response arrives: update paused StepRecord or append new one? How avoid re-pausing on resume?

**Recommendations:**
- Specify exact persistence transitions for interrupts (GPT)
- Document how unique constraint interacts with interrupt flow (GPT)

**Refs:** [node-types.md](specs/reviewed/node-types.md), [execution-types.md](specs/reviewed/execution-types.md)

---

### 🟡 1.10 No Selective Persistence Strategy
**Identified by:** Composer

No escape hatch for non-persistable outputs (file handles, generators, connections).

**Recommendations:**
- Add `@node(persist=False)` annotation (Composer)
- Document what to do when serialization fails (Composer)

**Refs:** [durable-execution.md](specs/reviewed/durable-execution.md)

---

## 2. Reliability & Production Readiness

### 🔴 2.1 No Retry Configuration
**Identified by:** Opus, Composer

No built-in retry mechanism for transient failures. Users must implement themselves.

**Recommendations:**
```python
@node(output_name="data", retry=RetryPolicy(
    max_attempts=3,
    initial_interval=1.0,
    backoff_multiplier=2.0,
    retryable_exceptions=[HTTPError, TimeoutError],
))
async def call_flaky_api(url: str) -> dict: ...
```

**Refs:** [durable-execution.md](specs/reviewed/durable-execution.md), [runners.md](specs/reviewed/runners.md)

---

### 🔴 2.2 No Timeout Configuration
**Identified by:** Opus

Nodes can run indefinitely. No `start_to_close_timeout` like Temporal.

**Recommendations:**
- Add `timeout: float` to `@node` (Opus)
- Add `default_timeout` to runner config (Opus)

**Refs:** [runners.md](specs/reviewed/runners.md), [runners-api-reference.md](specs/reviewed/runners-api-reference.md)

---

### 🔴 2.3 No Workflow Cancellation
**Identified by:** Opus, Composer

No way to cancel a running workflow from outside.

**Recommendations:**
- Add `runner.cancel_workflow(workflow_id, reason)` (Opus)
- Add `RunHandle.cancel()` (Composer)
- Support timeout: `runner.run(..., timeout=timedelta(minutes=5))` (Composer)

**Refs:** [runners-api-reference.md](specs/reviewed/runners-api-reference.md)

---

### 🟠 2.4 No Saga/Compensation Pattern
**Identified by:** Opus

No rollback mechanism for partial failures (flight reserved, hotel booked, car rental fails).

**Recommendations:**
- Add `compensation: Callable` to `@node` (Opus)
- On failure, runner walks backward calling compensations (Opus)

**Refs:** [durable-execution.md](specs/reviewed/durable-execution.md)

---

### 🟠 2.5 Workflow Concurrency Control Underspecified
**Identified by:** GPT, Composer

"One active execution per workflow_id" but no enforcement across multiple workers.

**Recommendations:**
- Add workflow lock/lease mechanism (GPT)
- Acquire lease when starting, renew heartbeat, release on completion (GPT)
- Add `ConcurrentExecutionError` type (Composer)

**Refs:** [persistence.md](specs/reviewed/persistence.md), [checkpointer.md](specs/reviewed/checkpointer.md)

---

### 🟠 2.6 No Heartbeat Mechanism
**Identified by:** Opus

For long-running nodes: no way to report progress, detect stuck executions, or resume from last progress.

**Recommendations:**
- Add heartbeat API (Opus)
- Store progress in heartbeats (Opus)
- On retry, provide last heartbeat details (Opus)

**Refs:** [durable-execution.md](specs/reviewed/durable-execution.md)

---

### 🟠 2.7 No Graceful Shutdown
**Identified by:** Opus

No handling for process shutdown: completing in-flight nodes, checkpointing partial progress.

**Recommendations:**
- Add `runner.shutdown()` method (Opus)
- Define modes: `immediate` vs `graceful` (finish current superstep) (Opus)

**Refs:** [runners.md](specs/reviewed/runners.md)

---

### 🟠 2.8 No Rate Limiting
**Identified by:** Gemini, Opus, Composer

No mechanism to throttle API calls or handle backpressure.

**Recommendations:**
- Add `@node(rate_limit="10/minute")` (Gemini)
- Runner-level throttling policies (Gemini)
- Circuit breaker support (Composer)

**Refs:** [runners.md](specs/reviewed/runners.md)

---

### 🟡 2.9 No Dead Letter Queue
**Identified by:** Opus

No handling for workflows that repeatedly fail.

**Recommendations:**
- Add `max_workflow_attempts` config (Opus)
- Add `on_workflow_exhausted` callback (Opus)

**Refs:** [persistence.md](specs/reviewed/persistence.md)

---

### 🟡 2.10 No Health Checks
**Identified by:** Composer

No way to check runner or checkpointer health for production deployments.

**Recommendations:**
- Add `runner.health_check()` → `{"status": "healthy", "checkpointer": "connected"}` (Composer)

**Refs:** [runners.md](specs/reviewed/runners.md)

---

## 3. API Inconsistencies

### 🔴 3.1 Forking/Resume API Inconsistent
**Identified by:** GPT

- `persistence.md`: uses `history=checkpoint.steps`
- `execution-types.md`: uses `checkpoint=checkpoint` parameter
- `runners-api-reference.md`: no `history` or `checkpoint` parameter at all

**Recommendations:**
- Pick ONE canonical fork API (GPT)
- Update all docs and runner signatures to match (GPT)

**Refs:** [persistence.md](specs/reviewed/persistence.md), [execution-types.md](specs/reviewed/execution-types.md), [runners-api-reference.md](specs/reviewed/runners-api-reference.md)

---

### 🔴 3.2 `iter()` Shape Contradictory
**Identified by:** GPT

Examples show both:
- `async for event in runner.iter(...)` (iterator)
- `async with runner.iter(...) as run: ...` (context manager)

**Recommendations:**
- Decide on one pattern (GPT)
- Make type signature and examples consistent (GPT)

**Refs:** [runners.md](specs/reviewed/runners.md), [execution-types.md](specs/reviewed/execution-types.md), [observability.md](specs/reviewed/observability.md)

---

### 🟠 3.3 Default Outputs Inconsistent (All vs Leaf)
**Identified by:** GPT

- `execution-types.md`, `state-model.md`: imply "all outputs unless `select=` filters"
- `runners-api-reference.md`: says `select=None` returns **leaf outputs** by default

**Recommendations:**
- Make default explicit and consistent (GPT)
- Define nested `RunResult` behavior under `select` (GPT)

**Refs:** [execution-types.md](specs/reviewed/execution-types.md), [runners-api-reference.md](specs/reviewed/runners-api-reference.md)

---

### 🟠 3.4 Stop + Partial Output Contradiction
**Identified by:** GPT

- `execution-types.md`: `StepStatus.STOPPED` = no usable output
- `durable-execution.md`: stopped mid-stream saves `STOPPED` but downstream uses partial output

**Recommendations:**
- Single rule: usable partial = `COMPLETED + partial=True + values present` (GPT)
- `STOPPED` only when NO usable output (GPT)

**Refs:** [execution-types.md](specs/reviewed/execution-types.md), [durable-execution.md](specs/reviewed/durable-execution.md)

---

### ✅ 3.5 Separator Convention Confusion (`.` vs `/`) - RESOLVED
**Identified by:** GPT, Composer

**Resolution:**
- `values=` parameter accepts ALL formats: `.`, `/`, and nested dict (flexible input)
- All other paths use `/` only (file system mental model)
- Node/output names cannot contain `.` or `/` (reserved characters)

This eliminates ambiguity: `values={"rag.query": "hello"}` always means "node `rag`, input `query`" — never "node named `rag.query`".

**Refs:** [graph.md](specs/reviewed/graph.md), [execution-types.md](specs/reviewed/execution-types.md)

---

### 🟠 3.6 RunResult vs dict Return Type Mismatch
**Identified by:** Composer

`SyncRunner.run()` returns `dict` while `AsyncRunner.run()` returns `RunResult`.

**Recommendations:**
- Unify: both return `RunResult` (Composer)
- Or provide `RunResult.to_dict()` adapter (Composer)

**Refs:** [runners.md](specs/reviewed/runners.md), [runners-api-reference.md](specs/reviewed/runners-api-reference.md)

---

### 🟡 3.7 Ordering Uses Unordered Containers
**Identified by:** GPT

`frozenset` iteration order is not stable (hash randomization). Claims deterministic order.

**Recommendations:**
- Use `tuple` for ordering guarantees (GPT)
- Use sets only for membership (GPT)

**Refs:** [graph.md](specs/reviewed/graph.md)

---

### 🟡 3.8 Step Indexing Rule Disagrees
**Identified by:** GPT

- `execution-types.md`: alphabetically by node_name
- `graph.md`: node list order passed to Graph

**Recommendations:**
- Choose one canonical ordering (ideally Graph construction order) (GPT)

**Refs:** [execution-types.md](specs/reviewed/execution-types.md), [graph.md](specs/reviewed/graph.md)

---

### 🟡 3.9 StepStatus vs RunStatus vs WorkflowStatus Overlap
**Identified by:** Opus

Three status enums with overlapping values. `RunStatus` and `StepStatus` are identical.

**Recommendations:**
- Add explanatory comment showing when each is used (Opus)

**Refs:** [execution-types.md](specs/reviewed/execution-types.md), [checkpointer.md](specs/reviewed/checkpointer.md)

---

## 4. Developer Experience

### 🔴 4.1 String-Typed Wiring / Refactoring Fragility
**Identified by:** Gemini

`node.with_inputs(param="upstream_name")` - renaming breaks silently, no type checking.

**Recommendations:**
- Typed references: `node_b.bind(x=node_a.outputs.result)` (Gemini)
- Add static analysis tool / "Graph Linter" (Gemini)
- Add `graph.visualize()` for Mermaid export (Gemini)

**Refs:** [graph.md](specs/reviewed/graph.md)

---

### 🟠 4.2 Schema Evolution / Versioning Incomplete
**Identified by:** Gemini, Opus, Composer

`graph_hash` exists but blocking all resumes on minor code changes is unacceptable.

**Recommendations:**
- Semantic versioning: `v1`, `v2` (Gemini)
- Distinguish "Logic Changes" (safe) from "Topology Changes" (unsafe) (Gemini)
- Add migration: `migrate_workflow(from_version, to_version, migrator_fn)` (Composer)

**Refs:** [execution-types.md](specs/reviewed/execution-types.md), [runners-api-reference.md](specs/reviewed/runners-api-reference.md)

---

### 🟠 4.3 Value Resolution Hierarchy Too Complex
**Identified by:** Composer

4-level hierarchy (edge → input → checkpoint → bound → default) is hard to reason about.

**Recommendations:**
- Simplify mental model (Composer)
- Add debugging API: `runner.debug_resolution(graph, param_name, values)` (Composer)

**Refs:** [state-model.md](specs/reviewed/state-model.md), [graph.md](specs/reviewed/graph.md)

---

### 🟠 4.4 Nested Graph Output Lifting Confusing
**Identified by:** GPT, Composer

Default lifts all outputs = collision risk, leaking heavy intermediates.

**Recommendations:**
- Add explicit `.lift("output1", "output2")` API (GPT, Composer)
- Default to empty lifting (safer) (Composer)

**Refs:** [graph.md](specs/reviewed/graph.md), [node-types.md](specs/reviewed/node-types.md)

---

### 🟠 4.5 InterruptNode API Non-Obvious
**Identified by:** Opus

`input_param="draft"` for "what human sees" is counterintuitive.

**Recommendations:**
- Consider renaming: `show="draft"`, `expect="decision"` (Opus)

**Refs:** [node-types.md](specs/reviewed/node-types.md)

---

### 🟠 4.6 No Testing Utilities
**Identified by:** Gemini, Composer

No mocking utilities, test fixtures, or `MockRunner`.

**Recommendations:**
- Provide `MockRunner` or `graph.test_mode()` (Gemini)
- Add `from hypergraph.testing import MockCheckpointer, TestRunner` (Composer)

**Refs:** N/A (missing feature)

---

### 🟡 4.7 Confusing Separation: Cache vs Checkpointer
**Identified by:** Opus

`cache=True` on node vs `checkpointer` on runner have overlapping purposes.

**Recommendations:**
- Add clearer documentation (Opus)
- Consider unifying under single concept with different scopes (Opus)

**Refs:** [runners.md](specs/reviewed/runners.md), [durable-execution.md](specs/reviewed/durable-execution.md)

---

### 🟡 4.8 Implicit Wiring Easy to Get Wrong
**Identified by:** GPT

Accidental coupling: two nodes with `config` output/input get auto-wired.

**Recommendations:**
- Tooling: "why is node B receiving `config`?" (GPT)
- Consider "explicit edges mode" for advanced users (GPT)

**Refs:** [graph.md](specs/reviewed/graph.md)

---

### 🟡 4.9 Error Messages Could Be More Helpful
**Identified by:** Composer

Runtime errors may lack context (node name, available outputs).

**Recommendations:**
- Rich error context: node name, input values, available outputs (Composer)
- "Did you mean X?" suggestions (Composer)

**Refs:** [execution-types.md](specs/reviewed/execution-types.md)

---

## 5. Streaming & Progress

### 🟠 5.1 Streaming Semantics Underspecified
**Identified by:** GPT

What does `yield` mean? async generators can't `return value`.

**Recommendations:**
- Define strict contract (GPT):
  - Option A: yields = output chunks, final = concat
  - Option B: yields can be `Progress`, `Chunk`, `Final` events
- Add "custom event writer" like LangGraph for progress (GPT)

**Refs:** [execution-types.md](specs/reviewed/execution-types.md), [observability.md](specs/reviewed/observability.md)

---

### 🟠 5.2 Durable Streaming Needs Pending Writes
**Identified by:** GPT

Crash mid-stream = step reruns = duplicate output + LLM nondeterminism.

**Recommendations:**
- Add "pending writes" for streaming nodes (GPT)
- Periodically persist partial chunks (GPT)

**Refs:** [durable-execution.md](specs/reviewed/durable-execution.md)

---

## 6. Missing Features

### 🟠 6.1 No Workflow Signals / External Events
**Identified by:** Opus, Gemini

Can't send signals to running workflows ("payment_received", "cancel").

**Recommendations:**
- Add `SignalNode` or `WaitNode` (Opus)
- Allow signal channels like Temporal (Gemini)

**Refs:** [node-types.md](specs/reviewed/node-types.md)

---

### 🟠 6.2 No Cross-Workflow Memory Store
**Identified by:** Gemini, Opus

Users want simple "remember user name" across sessions.

**Recommendations:**
- Add `Store` or `Memory` interface distinct from Checkpointer (Gemini)
- Document patterns for multi-tenancy (Opus)

**Refs:** [persistence.md](specs/reviewed/persistence.md)

---

### 🟠 6.3 No Durable Scheduling / Cron
**Identified by:** Opus, Composer

No native scheduling capability.

**Recommendations:**
- If DBOS is the answer, document clearly (Opus)
- Otherwise add `runner.schedule(graph, schedule="0 9 * * *")` (Composer)

**Refs:** [durable-execution.md](specs/reviewed/durable-execution.md)

---

### 🟡 6.4 No Map/Join/Reduce Primitive
**Identified by:** GPT

Missing "collect/join/reduce" for parallel branches and map fan-out.

**Recommendations:**
- Add explicit `JoinNode`/reducer concept (GPT)
- Define standard "map then aggregate" pattern (GPT)

**Refs:** [graph.md](specs/reviewed/graph.md), [runners.md](specs/reviewed/runners.md)

---

### 🟡 6.5 No Workflow Queries
**Identified by:** Opus

No way to query running workflow state from outside.

**Recommendations:**
- Add query mechanism or document checkpointer patterns (Opus)

**Refs:** [runners-api-reference.md](specs/reviewed/runners-api-reference.md)

---

### 🟡 6.6 Priority Queues
**Identified by:** Gemini

"User Interrupt Response" should take precedence over "Background Indexing".

**Recommendations:**
- Add priority levels for Runs or Nodes (Gemini)

**Refs:** [runners.md](specs/reviewed/runners.md)

---

### 🟡 6.7 No Workflow Visualization/Debugging Tools
**Identified by:** Composer

No way to visualize execution or inspect checkpoint state.

**Recommendations:**
- Add `graph.visualize()` for Mermaid/Graphviz (Composer)
- Add `runner.debug_state(workflow_id, superstep)` (Composer)

**Refs:** [graph.md](specs/reviewed/graph.md)

---

### 🟡 6.8 Workflow Lifecycle Management Missing
**Identified by:** Composer

No `cancel_workflow()`, `delete_workflow()`, `archive_workflow()`.

**Recommendations:**
- Add full lifecycle API (Composer)
- Document state machine: ACTIVE → PAUSED → COMPLETED/FAILED/CANCELLED (Composer)

**Refs:** [checkpointer.md](specs/reviewed/checkpointer.md)

---

## Summary: Priority Matrix

| Priority | Count | Key Themes |
|----------|-------|------------|
| 🔴 Critical | 9 | Determinism, routing durability, idempotency, API contradictions |
| 🟠 High | 22 | Artifacts, serialization, retry/timeout, cancellation, DX |
| 🟡 Medium | 15 | Status overlap, testing, memory store, visualization |
| 🟢 Low | 4 | Priority queues, templates, analytics |

### Top 10 Issues to Fix Before v1

1. **Determinism enforcement** (Opus, GPT)
2. **Routing decisions durability** (GPT)
3. **Idempotency key support** (Opus)
4. **Artifact storage layer** (all reviewers)
5. **Forking/resume API unification** (GPT)
6. **Retry configuration** (Opus, Composer)
7. **Timeout configuration** (Opus)
8. **Workflow cancellation** (Opus, Composer)
9. **String-typed wiring / type safety** (Gemini)
10. **History limits / continue-as-new** (Gemini, Opus)

---

## Appendix: Review Sources

| Reviewer | Focus Areas | Unique Insights |
|----------|-------------|-----------------|
| **Gemini** | Storage, DX, comparisons | "Infinite History" problem, Skip Propagation |
| **Opus** | Durability, reliability, production | Determinism, idempotency, heartbeats, sagas |
| **GPT** | API consistency, streaming, atomicity | Routing durability, iter() shape, pending writes |
| **Composer** | DX, lifecycle, observability | Testing utilities, health checks, error messages |
