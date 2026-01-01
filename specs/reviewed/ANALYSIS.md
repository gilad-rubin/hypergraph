# Spec Analysis: Inconsistencies, Flaws, and Missing Pieces

Based on reviewing all 10 spec files in `specs/reviewed/`.

## High-Priority Issues (All Resolved ✓)

### 1. Resume Semantics Conflict ✓

**Was:** Three different resume patterns existed (`resume=True`, implicit, `resume()` method)

**Resolution:** **Option A — Implicit resume via same `workflow_id`**
- Removed all `resume=True` flags from examples
- Checkpointer auto-detects paused state when same `workflow_id` is used
- Files fixed: `execution-types.md`, `runners.md`

---

### 2. GraphNode.inputs Type Mismatch ✓

**Was:** `Graph.inputs` returns `InputSpec` but `GraphNode` assigned it directly as tuple

**Resolution:** `GraphNode` now extracts `.all` from `InputSpec`
- `node-types.md:1107` changed to `self.inputs = graph.inputs.all`

---

### 3. RunResult Shape Inconsistency ✓

**Was:** `runners.md` had old shape with separate `pause_reason`, `pause_node`, `pause_value` fields

**Resolution:** Updated to use `PauseInfo` dataclass
- `runners.md` now shows `pause: PauseInfo | None` matching `execution-types.md`

---

### 4. RunStatus RUNNING Error ✓

**Was:** `graph.md:810` listed `RUNNING` as a valid status

**Resolution:** Removed `RUNNING` — only `COMPLETED`, `PAUSED`, `ERROR` are valid
- `RUNNING` doesn't make sense for a returned result (execution is complete when result is returned)

---

### 5. @node Parameter Name Drift ✓

**Was:** Examples mixed `outputs=` and `output_name=`

**Resolution:** Standardized to `output_name=` across all examples
- Files fixed: `graph.md`, `runners.md`

---

### 6. DBOS Runner Not in Taxonomy ✓

**Was:** `DBOSAsyncRunner` described in `durable-execution.md` but missing from `runners.md`

**Resolution:** Added `DBOSAsyncRunner` to official taxonomy
- Updated "The Four Runners" table
- Added to Feature Compatibility Matrix
- Added to Capability values table
- Added `supports_durable_execution` capability

---

### 7. persistence.md Uses Old Checkpointer Interface ✓

**Was:** Used `checkpointer.load()` and missing `await` on async calls

**Resolution:** Updated to canonical interface
- Changed `load()` to `get_workflow()`
- Added `await` to all `get_state()`, `get_history()`, `get_workflow()` calls

---

## Medium-Priority Issues (Design Clarifications)

### 1. Paused Workflow Persistence Model

**The problem:**

- `PauseInfo` exists at runtime in `RunResult`
- `Workflow` persistence type in `execution-types.md:994` has no pause fields
- If workflow pauses and process dies, how does external system query "what are we waiting for?"

**Decision needed:** Define whether pause metadata is:

- Stored in `Workflow`
- In a dedicated "pending interrupts" table
- Derivable from steps (and how)

---

### 2. "Steps Are Source of Truth" vs initial_state

**The problem:**

- Core principle: "state is computed from steps"
- But `create_workflow(..., initial_state=...)` in `checkpointer.md:73` allows non-step state
- If `initial_state` isn't a step, it breaks single source of truth

**Decision needed:** Define if `initial_state` is:

- Banned
- A synthetic "INIT step"
- Separate "base_state" explicitly part of folding model

---

### 3. Step Indexing + Parallel Execution

**The problem:**

- Pre-registering pending steps described in `durable-execution.md`
- But checkpointer interface doesn't support that lifecycle
- Need deterministic mapping: `(workflow_id, step_index)` <-> node execution instance

**Decision needed:** Choose one model:

- Append-only in completion order (simple, nondeterministic under concurrency)
- Pre-assigned indices per batch/superstep (deterministic, needs richer checkpointer ops)

---

### 4. Cache vs Checkpoint Semantics

**The problem:**

- `NodeEndEvent.cached` exists for cache hits
- "Loaded from checkpoint" is a different concept
- Not clear if checkpoint loads emit `cached=True`, a new flag, or distinct event

**Decision needed:** Define distinction for observability correctness

---

### 5. Nested Graph Output Namespace Collision

**The problem:**

- `RunResult.outputs` holds `Any | RunResult` (execution-types.md:312)
- Nested graphs appear under the GraphNode name
- What if output key equals nested graph node name?
- Also: `select` path syntax uses `/` - should ban `/` in names?

**Decision needed:** Either:

- Reserve namespace / validate collisions at graph build time
- Separate `outputs` from `children`/`subruns`

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
| Resolved | SyncRunner checkpointer      | `checkpointer.md`                                | Done           |
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
| Medium   | Pause persistence model      | Design                                           | Needs decision |
| Medium   | initial_state vs steps       | Design                                           | Needs decision |
| Medium   | Step indexing                | Design                                           | Needs decision |
| Medium   | Cache vs checkpoint          | Design                                           | Needs decision |
| Medium   | Namespace collision          | `graph.md`                                       | Needs decision |
| Medium   | persist allowlist semantics  | Design                                           | Needs decision |
| Low      | Remaining broken links       | Multiple                                         | Needs fix      |
| Low      | Duplicate hierarchies        | Multiple                                         | Cleanup        |
