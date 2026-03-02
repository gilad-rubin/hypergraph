# Checkpoint Resume: Spec vs Implementation

Gap analysis between the reviewed specs and what PR #63 actually implements.

---

## What We Built (PR #63)

### run() resume: checkpoint value merge

When `run()` is called with a `workflow_id` and a checkpointer, the runner:

1. Loads checkpoint state via `checkpointer.get_state(workflow_id)`
2. Filters to `graph.inputs.all` — only graph-level input names are merged
3. Merges: `{**checkpoint_inputs, **runtime_values}` — runtime always wins
4. Validates inputs **after** merge (so checkpoint-provided values satisfy required inputs)

Guard: `_validation_ctx is None` skips the merge for map() children (they get their own values from the parent).

**Files**: `template_async.py:191-199`, `template_sync.py:183-190`

### map() resume: skip completed items

When `map()` is called with the same `workflow_id`:

1. Queries `checkpointer.list_runs(parent_run_id=workflow_id)` for child runs
2. Collects indices where `run.status == WorkflowStatus.COMPLETED`
3. For completed items: restores `RunResult` from `checkpointer.get_state(child_workflow_id)` instead of re-executing
4. For FAILED or ACTIVE items: re-executes normally

**Files**: `template_async.py:576-597`, `template_sync.py:494-515`

### Supporting changes

- **`base.py`**: Added `parent_run_id` filter to async `list_runs` (sync `runs()` already had it)
- **`sqlite.py`**: Implemented `parent_run_id` filter in async `list_runs` query
- **`create_run` upsert**: Both async and sync use `INSERT ... ON CONFLICT DO UPDATE` to handle re-runs without crashing
- **Tests**: 16 resume tests across async/sync, run/map in `test_resume.py`

---

## What the Specs Envision

The following sections map spec features to their implementation status.

### Value Resolution Hierarchy

**Spec** (persistence.md, state-model.md):
```
1. Edge value        — Produced by upstream node
2. Runtime input     — Explicit in runner.run(values={...})
3. Checkpoint value  — Loaded from persistence
4. Bound value       — Set via graph.bind()
5. Function default  — Default in function signature
```

**Implementation**: We merge checkpoint into runtime inputs before execution: `{**checkpoint, **runtime}`. This collapses levels 2+3 into one "merged input" bucket. state-model.md explicitly validates this: *"checkpoint values are just part of the merged inputs."*

**Status**: ALIGNED. The 5-level hierarchy is a conceptual model. The implementation correctly gives runtime values priority over checkpoint values, which is the invariant that matters.

### Execution Semantics (load → merge → execute → append)

**Spec** (persistence.md §Execution Semantics):
```
run(graph, values, workflow_id):
  1. Load   — Get checkpoint state (if workflow_id exists)
  2. Merge  — Combine with values (values win on conflicts)
  3. Execute — Run the graph
  4. Append — Add new steps to history
  5. Return — Give back result
```

**Implementation**: Exactly this. We load, merge, execute (which appends steps via save_step), and return.

**Status**: IMPLEMENTED.

### graph_hash (version mismatch detection)

**Spec** (checkpointer.md §Types):
```python
@dataclass
class Workflow:
    graph_hash: str | None  # For version mismatch detection
```

**Implementation**: The `Run` type has no `graph_hash` field. No hash is computed or compared.

**Status**: NOT IMPLEMENTED.

**Assessment**: This is the spec's answer to "what if the graph changed between runs?" Without it, resuming with a structurally different graph may silently produce wrong results or crash. The spec envisions:
- Compute a hash from the graph structure (node names, edges, types)
- Store it in the workflow/run record at creation
- On resume, compare hashes and warn (not block) on mismatch

**Recommendation**: Implement as a warning. Store `graph_name` (already available) + a structural hash. On resume, if hash differs, log a warning but proceed. This gives users visibility into graph drift without blocking legitimate use cases (bug fixes, added nodes).

### history parameter (workflow forking)

**Spec** (persistence.md §Explicit State and History Injection):
```python
result = await runner.run(
    graph,
    values={**state, "user_input": "new question"},
    history=steps,                                     # Execution trail
    workflow_id="session-456",                         # NEW workflow
)
```

The `history` parameter:
- Seeds the step index (new steps continue numbering)
- Copies the step trail into the new workflow
- Provides full audit trail via `get_steps()`
- Errors if used with an existing workflow (it has its own history)

**Implementation**: Not implemented. `run()` has no `history` parameter.

**Status**: NOT IMPLEMENTED.

**Assessment**: This is a fork/branch feature. The current implementation supports the simpler pattern: read state from workflow A, pass as values to workflow B. The `history` parameter adds step-level provenance (audit trail continuity). This is a nice-to-have for compliance and debugging, not critical for basic resume.

**Recommendation**: Defer. The value-spreading pattern (`values={**checkpoint.values, ...}`) covers the core fork use case. `history` adds audit provenance — implement when users need it.

### get_checkpoint() (time travel)

**Spec** (checkpointer.md):
```python
async def get_checkpoint(self, run_id: str, *, superstep: int | None = None) -> Checkpoint:
    """Combines get_state() and get_steps() into a single Checkpoint object."""
```

**Implementation**: `get_checkpoint()` exists on the Checkpointer ABC with a default implementation that calls `get_state()` + `get_steps()`. However, `get_state(superstep=N)` for historical supersteps is not implemented in SqliteCheckpointer — it always returns latest state.

**Status**: PARTIALLY IMPLEMENTED. The method exists but historical time-travel queries don't work yet.

**Assessment**: Historical state queries need either step-folding or snapshot materialization (spec §State Materialization). Current `get_state()` uses a `latest_values` table which is fast for latest but can't do point-in-time.

**Recommendation**: Implement step-folding as fallback in SqliteCheckpointer when `superstep` is provided. This enables the fork-from-point use case.

### Concurrent execution guard

**Spec** (persistence.md §Concurrent Execution):
```python
# ❌ Error: workflow is already running
task1 = runner.run(graph, values={...}, workflow_id="order-123")
task2 = runner.run(graph, values={...}, workflow_id="order-123")  # Conflict!
```

**Implementation**: No guard. Two concurrent runs with the same `workflow_id` would race on checkpoint reads/writes and produce corrupt state.

**Status**: NOT IMPLEMENTED.

**Assessment**: This is a safety concern, not a feature. Without it, concurrent map() retries or accidental double-submissions can corrupt checkpoint state.

**Recommendation**: Implement using `status=ACTIVE` check. On `create_run`, if existing run has `status=ACTIVE`, raise `ConcurrentExecutionError`. The upsert already sets status to ACTIVE, so we just need the check before the upsert.

### CheckpointPolicy

**Spec** (checkpointer.md):
```python
@dataclass
class CheckpointPolicy:
    durability: Literal["sync", "async", "exit"] = "async"
    retention: Literal["full", "latest", "windowed"] = "full"
    window: int | None = None
    ttl: timedelta | None = None
```

**Implementation**: `CheckpointPolicy` exists in `base.py` with full validation. SqliteCheckpointer accepts it. However:
- `durability`: Only "exit" mode is effectively implemented (steps are saved synchronously during execution — there's no background write pipeline for "async"). In practice, "sync" and "async" behave the same.
- `retention`: Only "full" is implemented. No pruning for "latest" or "windowed".
- `ttl`: Not implemented (no cleanup job).

**Status**: PARTIALLY IMPLEMENTED (type + validation exist, but behavior is uniform).

**Assessment**: The policy is structural scaffolding. The "async" vs "sync" distinction requires a background write queue. "latest" retention requires a pruning pass after save_step. These are performance optimizations, not correctness concerns.

**Recommendation**: Defer. The current behavior (sync writes, full retention) is the safest default and sufficient for alpha/beta.

### ArtifactRef (tiered storage for large values)

**Spec** (durability.md §6):
```python
@dataclass(frozen=True)
class ArtifactRef:
    storage: str
    key: str
    size: int
    content_type: str
    checksum: str
```

**Implementation**: Not implemented. All values are stored inline in the SQLite `values` column.

**Status**: NOT IMPLEMENTED.

**Assessment**: Critical for production with large outputs (embeddings, images, dataframes). Not needed for the current alpha.

**Recommendation**: Defer to production readiness phase. The serializer interface already exists as an extension point.

### GraphNode durability boundaries

**Spec** (durability.md §7-8):
```python
rag.as_node(durability="nested")   # default: inner steps persisted individually
rag.as_node(durability="atomic")   # single StepRecord for entire subgraph
```

**Implementation**: Not implemented. Nested graphs always use the "nested" pattern (child workflow + per-step records).

**Status**: NOT IMPLEMENTED.

**Assessment**: "atomic" mode is an optimization for subgraphs with non-serializable intermediates. The default "nested" behavior is correct and implemented.

**Recommendation**: Defer. Add when users hit serialization issues with nested graph intermediates.

### SyncRunner checkpointing

**Spec** (checkpointer.md §SyncRunner):
> "SyncRunner does not support checkpointing. This is by design."

**Implementation**: SyncRunner fully supports checkpointing via `SyncCheckpointerProtocol`. This is a deliberate deviation from the spec — we added sync support because the framework targets data scientists who often work synchronously.

**Status**: IMPLEMENTED (spec is outdated).

**Assessment**: The spec should be updated. `SyncCheckpointerProtocol` is a runtime-checkable Protocol with sync methods (`state()`, `runs()`, `create_run_sync()`, etc.) that SqliteCheckpointer implements alongside its async interface.

### HITL (Human-in-the-Loop) with InterruptNode

**Spec** (persistence.md §Human-in-the-Loop):
```python
approval = InterruptNode(
    name="approval",
    input_param="draft",
    response_param="decision",
)
```

Resume pattern: run with same `workflow_id`, provide response in values.

**Implementation**: InterruptNode exists and works with checkpointing. The resume mechanism (load state → merge with response → re-execute) works because the checkpoint provides all prior state, and the response comes as a runtime value which overrides checkpoint.

**Status**: IMPLEMENTED (via the general resume mechanism, not a special HITL path).

### No update_state()

**Spec** (state-model.md, persistence.md):
> "hypergraph intentionally does not have update_state(). State flows through nodes."

**Implementation**: Correct. No external state mutation API. InterruptNode is the sanctioned way to inject human input.

**Status**: ALIGNED.

---

## Gap Summary

| Feature | Spec | Implementation | Priority |
|---------|------|----------------|----------|
| Value merge (load→merge→execute→append) | Defined | Done | — |
| map() skip completed | Implied | Done | — |
| SyncRunner checkpointing | Excluded | Done (deviation) | — |
| InterruptNode resume | Defined | Works via general mechanism | — |
| graph_hash warning | Defined (Workflow type) | Missing | Medium |
| Concurrent execution guard | Defined | Missing | Medium |
| get_state(superstep=N) | Defined | Missing (always latest) | Low |
| history parameter | Defined | Missing | Low |
| CheckpointPolicy behavior | Defined | Scaffolding only | Low |
| ArtifactRef | Defined | Missing | Future |
| GraphNode durability modes | Defined | Missing | Future |
| ttl / retention pruning | Defined | Missing | Future |

### Recommended next steps (in priority order)

1. **graph_hash warning** — Low effort, high safety value. Compute a hash from node names + edge structure, store in `runs` table, warn on mismatch during resume.

2. **Concurrent execution guard** — Low effort, prevents data corruption. Check `status=ACTIVE` before upsert in `create_run`.

3. **get_state(superstep=N)** — Enables fork-from-point. Implement as step-folding in SqliteCheckpointer when `superstep` is not None.

4. **history parameter** — Enables audit-trail-preserving forks. Add `history: list[StepRecord] | None = None` to `run()`.

Everything else (ArtifactRef, durability modes, retention pruning, TTL) is production-readiness work, not alpha-blocking.

---

## Design Decisions We Made (and why)

### State injection, not replay

The spec defines "load → merge → execute → append", which is state injection. The graph re-executes from the top with merged inputs — there's no deterministic replay of prior steps (Temporal-style).

This is the right call because:
- Hypergraph graphs are not deterministic (LLM calls, external APIs)
- Replay would require recording all non-deterministic inputs (massive complexity)
- State injection is simple: load the outputs, skip the nodes that already ran (via edge values)

### Filter to graph.inputs.all

We filter checkpoint state to only names in `graph.inputs.all` before merging. This prevents intermediate values from a prior run leaking into the new run's input resolution.

Why this matters: if a prior run produced `embedding` as an intermediate value, and the new graph also has an `embed` node, we don't want the stale `embedding` to short-circuit the node. Only values that are actual graph inputs (required, optional, seeds) get merged.

### _validation_ctx guard

Map() children don't need checkpoint merge because the parent already distributes values to each child. Without the guard, each child would redundantly query the DB for checkpoint state — wasted I/O for potentially thousands of items.

### Upsert semantics for create_run

The original `create_run` did `INSERT` which crashed on re-run (`UNIQUE constraint failed`). We changed to `INSERT ... ON CONFLICT DO UPDATE` to support the resume pattern. The upsert preserves `created_at` while resetting `status` to ACTIVE.
