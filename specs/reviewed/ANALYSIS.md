# Spec Analysis: Inconsistencies, Flaws, and Missing Pieces

Based on reviewing all 10 spec files in `specs/reviewed/`.

## High-Priority Issues (Design Decisions Needed)

### 1. Resume Semantics Conflict

**The problem:** Three different resume patterns exist in the specs:


| Location                                   | Pattern                                         |
| -------------------------------------------- | ------------------------------------------------- |
| `durable-execution.md:220`                 | "No resume=True needed!"                        |
| `persistence.md`                           | No special resume flag                          |
| `execution-types.md:448`, `runners.md:240` | Uses`resume=True`                               |
| `durable-execution.md:1177`                | Mentions "User calls`resume()`" (undefined API) |

**Files affected:** `execution-types.md`, `runners.md`, `durable-execution.md`, `persistence.md`

**Decision needed:** Choose one canonical pattern: #@A

- **Option A:** Implicit resume (same `workflow_id` auto-resumes)
- **Option B:** Explicit `resume=True` flag
- **Option C:** Separate `runner.resume()` method

---

### 2. GraphNode.inputs Type Mismatch

**The problem:**

- `graph.md:151` specifies `Graph.inputs` returns `InputSpec` (a dataclass)
- `node-types.md:1107` says `self.inputs = graph.inputs  # Already a tuple`

**Files affected:** `graph.md`, `node-types.md`

**Decision needed:** Either: #@use inputspec

- `Graph.inputs` returns `InputSpec` and `GraphNode` extracts `.all` as tuple
- Or `Graph.inputs` returns a tuple (but then `InputSpec` needs restructuring)

---

### 3. RunResult Shape Inconsistency

**The problem:**

- `execution-types.md` defines `RunResult.pause: PauseInfo | None` #@use this
- `runners.md:590-592` shows older shape with separate `pause_reason`, `pause_node`, `pause_value` fields

**Files affected:** `runners.md`

**Decision needed:** Update `runners.md` to use `PauseInfo` (matches `execution-types.md`)

---

### 4. RunStatus RUNNING Error

**The problem:**

- `graph.md:810` claims `RunResult.status` includes `RUNNING`
- `execution-types.md:153-157` defines only `COMPLETED`, `PAUSED`, `ERROR`
- `RUNNING` doesn't make sense for a returned result

**Files affected:** `graph.md`

**Fix:** Remove `RUNNING` from `graph.md:810` #@is there a case where this will make sense?

---

### 5. @node Parameter Name Drift

**The problem:**

- Decorator signature in `node-types.md:497` uses `output_name=`
- Many examples use `outputs=` (e.g., `graph.md:226`, `runners.md:257`)

**Files affected:** All files with `@node` examples

**Decision needed:** Either:

- Support both spellings (with one as alias)
- Standardize all examples to use `output_name=` #@yes. output_name

---

### 6. DBOS Runner Not in Taxonomy

**The problem:**

- `durable-execution.md` extensively describes `DBOSAsyncRunner` and `DBOSSyncRunner`
- `runners.md` only lists `SyncRunner`, `AsyncRunner`, `DaftRunner`
- `runners-api-reference.md` has no DBOS runner API

**Files affected:** `runners.md`, `runners-api-reference.md`

**Decision needed:** Either:

- Add DBOS runners to the official taxonomy #@yes
- Or clarify DBOS is an implementation detail of AsyncRunner

---

### 7. persistence.md Uses Old Checkpointer Interface

**The problem:**

- `persistence.md:691` uses `checkpointer.load("session-123")` (old interface)
- Canonical interface in `checkpointer.md` uses `get_workflow()`
- Many examples missing `await` (lines 419, 420, 444, etc.) but interface is async

**Files affected:** `persistence.md`

**Fix needed:** Update to use `get_workflow()` and add `await` to all async calls #@great

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

### Missing await in persistence.md

Lines 419, 420, 444, 466, 480, 493, 539 use `checkpointer.get_state()` without `await` but interface is async.

### Duplicate Type Hierarchies

Type hierarchy diagram appears differently in:

- `node-types.md`
- `graph.md`
- `execution-types.md`

Should have one canonical version.

### Inconsistent Module Paths

Examples import from `hypernodes.checkpointers` and `hypernodes.runners` inconsistently.

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
