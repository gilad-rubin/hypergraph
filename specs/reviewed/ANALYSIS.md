# Spec Analysis: Inconsistencies, Flaws, and Missing Pieces

Based on reviewing all 10 spec files in `specs/reviewed/`.

## Medium-Priority Issues (Design Clarifications)

### ~~1. Paused Workflow Persistence Model~~ ✓

**The problem:**

- `PauseInfo` exists at runtime in `RunResult`
- `Workflow` persistence type in `execution-types.md:994` has no pause fields
- If workflow pauses and process dies, how does external system query "what are we waiting for?"

**Solution (Resolved):**

Pause metadata is stored in `StepResult`:
- Added `StepStatus.WAITING` - indicates step is blocked at InterruptNode
- Added `StepResult.pause: PauseInfo | None` - contains the pause details
- External systems query `get_workflow()`, find steps with `WAITING` status, read `pause` field

Updated in: `execution-types.md`, `checkpointer.md`

---

### ~~2. "Steps Are Source of Truth" vs initial_state~~ RESOLVED

**The problem:**

- Core principle: "state is computed from steps"
- But `create_workflow(..., initial_state=...)` allowed non-step state
- If `initial_state` isn't a step, it breaks single source of truth

**Resolution:** Removed `initial_state` parameter entirely.

- `create_workflow(workflow_id)` is now internal-only (called by runner)
- For forking/time-travel, users call `runner.run()` with `history` parameter
- State is always computed from steps - no exceptions

Updated in: `checkpointer.md`, `persistence.md`

---

### ~~3. Step Indexing + Parallel Execution~~ ✓

**The problem:**

- Pre-registering pending steps described in `durable-execution.md`
- But checkpointer interface doesn't support that lifecycle
- Need deterministic mapping: `(workflow_id, step_index)` <-> node execution instance

**Solution (Resolved):** Pre-assigned indices per batch, alphabetically by node name.

- `batch_index` groups nodes that run in parallel (already in Step type)
- `index` is assigned alphabetically by `node_name` within each batch
- Completion order doesn't affect indices — deterministic regardless of timing

Example:
```
Batch 0: fetch_orders=0, fetch_products=1, fetch_users=2  (alphabetical)
Batch 1: combine=3
```

Updated in: `execution-types.md`

---

### ~~4. Cache vs Checkpoint Semantics~~ RESOLVED

**The problem:**

- `NodeEndEvent.cached` exists for cache hits
- "Loaded from checkpoint" is a different concept
- Not clear if checkpoint loads emit `cached=True`, a new flag, or distinct event

**Resolution:** Added separate `replayed` boolean flag.

`NodeEndEvent` now has two flags:
- `cached: bool` - True if loaded from cache (same inputs seen before)
- `replayed: bool` - True if loaded from checkpoint (crash recovery/resume)

Simple, backwards compatible, and each boolean answers a clear question.

Updated in: `execution-types.md`, `runners-api-reference.md`, `observability.md`

---

### ~~5. Nested Graph Output Namespace Collision~~ ✓

**The problem:**

- `RunResult.outputs` holds `Any | RunResult` (execution-types.md:312)
- Nested graphs appear under the GraphNode name
- What if output key equals nested graph node name?
- Also: `select` path syntax uses `/` - should ban `/` in names?

**Solution (Resolved):** Validate at graph build time + ban `/` in names.

Two new validations in `Graph.__init__`:

1. **Node name validation** (`_validate_node_names`): Node and output names cannot contain `/`. The slash is reserved as the path separator for nested graph access (`result['outer/inner/value']`, `select=['rag/*']`). If allowed in names, paths would be ambiguous.

2. **Namespace collision validation** (`_validate_no_namespace_collision`): Output names cannot match GraphNode names in the same graph. Since `RunResult.outputs` stores both regular outputs and nested RunResults in the same dict, a collision would make `result['name']` ambiguous.

Updated in: `graph.md`

---

### 6. What Is Persisted with persist=[...] Allowlist?

**The problem:**

- If a node has multiple outputs, can you persist only some?
- If node re-runs to regenerate non-persisted outputs, persisted ones may change (defeats purpose)

**Decision needed:** Define whether persistence is per-node or per-output, and skip/rehydration rules for multi-output nodes

---

## Low-Priority Issues (Doc Cleanup)

### Additional Broken Links


| Location             | Broken Link                          | Should Be                                          |
| ---------------------- | -------------------------------------- | ---------------------------------------------------- |
| `graph.md:596`       | `runners.md#validate_map_compatible` | `runners-api-reference.md#validate_map_compatible` |
| `persistence.md:432` | Links to`checkpointer.md`            | Verify path is correct                             |

### ~~Missing await in persistence.md~~ ✓

~~Lines 419, 420, 444, 466, 480, 493, 539 use `checkpointer.get_state()` without `await` but interface is async.~~

Fixed as part of High-Priority Issue #7.

### Duplicate Type Hierarchies

Type hierarchy diagram appears differently in:

- `node-types.md`
- `graph.md`
- `execution-types.md`

Should have one canonical version.

### Inconsistent Module Paths

Examples import from `hypergraph.checkpointers` and `hypergraph.runners` inconsistently.

---

## Missing Specifications


| Spec                      | Description                                                    |
| --------------------------- | ---------------------------------------------------------------- |
| Cache Interface           | `runners-api-reference.md` mentions `cache: Cache` but no spec |
| Error Handling            | How errors propagate, recovery patterns, cycle failures        |
| Visualization             | `graph.md` has `visualize(**kwargs)` with no details           |
| BaseEvent                 | `observability.md` shows structure but never formally defines  |
| RunStartEvent/RunEndEvent | Listed in hierarchy but not fully defined                      |

---

## Summary Table


| Priority | Issue                        | Files Affected                                   | Status         |
| ---------- | ------------------------------ | -------------------------------------------------- | ---------------- |
| Resolved | Checkpointer interface       | `durable-execution.md`                           | Done           |
| Resolved | InterruptEvent fields        | `observability.md`                               | Done           |
| Resolved | SyncRunner checkpointer      | `checkpointer.md`, `durable-execution.md`        | Done (no checkpointer, use cache) |
| Resolved | NodeExecution undefined      | `execution-types.md`                             | Done           |
| Resolved | StepSnapshot undefined       | `durable-execution.md`                           | Done           |
| Resolved | Some dead links              | Multiple                                         | Done           |
| Resolved | YAGNI PauseReason            | `execution-types.md`, `runners-api-reference.md` | Done           |
| Resolved | Resume semantics conflict    | Multiple                                         | Done (Option A: implicit resume) |
| Resolved | GraphNode.inputs type        | `graph.md`, `node-types.md`                      | Done (uses InputSpec.all) |
| Resolved | RunResult shape              | `runners.md`                                     | Done (uses PauseInfo) |
| Resolved | RunStatus RUNNING            | `graph.md`                                       | Done (removed) |
| Resolved | @node param name             | All examples                                     | Done (output_name=) |
| Resolved | DBOS runner taxonomy         | `runners.md`                                     | Done (added DBOSAsyncRunner) |
| Resolved | persistence.md old interface | `persistence.md`                                 | Done (get_workflow + await) |
| Resolved | Pause persistence model      | `execution-types.md`, `checkpointer.md`          | Done (WAITING status + StepResult.pause) |
| Resolved | DBOS integration philosophy  | `durable-execution.md`, `runners-api-reference.md` | Done (thin wrapper pattern) |
| Resolved | SyncRunner durability        | `durable-execution.md`, `checkpointer.md`        | Done (cache-based, no checkpointer) |
| Resolved | initial_state vs steps       | `checkpointer.md`, `persistence.md`              | Done (removed initial_state) |
| Resolved | Step indexing                | `execution-types.md`                             | Done (alphabetical within batch) |
| Resolved | Cache vs checkpoint          | `execution-types.md`, `observability.md`         | Done (added replayed flag) |
| Resolved | Namespace collision          | `graph.md`                                       | Done (build-time validation) |
| Medium   | persist allowlist semantics  | Design                                           | Needs decision |
| Low      | Remaining broken links       | Multiple                                         | Needs fix      |
| Low      | Duplicate hierarchies        | Multiple                                         | Cleanup        |
