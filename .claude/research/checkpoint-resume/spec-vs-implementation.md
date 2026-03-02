# Checkpoint Resume: Spec vs Implementation

Gap analysis between the reviewed specs and what PR #63 actually implements.

**Spec sources**: `execution-types.md`, `persistence.md`, `state-model.md`, `checkpointer.md`, `durability.md`, `durable-execution.md`

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

## Critical Gap: Step History as Implicit Cursor

**This is the biggest thing we got wrong.** The spec (execution-types.md §Step History as Implicit Cursor) is very clear:

> Unlike sequential workflow systems that track an explicit program counter, hypergraph uses **step history as an implicit cursor**. The combination of outputs + completed steps determines what runs next.

### Why values alone are insufficient

The spec gives two concrete examples:

**1. Cycles need iteration count**

```
generate(messages) → accumulate(messages, response) → check_done → generate
```

If checkpoint contains `{"messages": [...], "response": "..."}`:

- **Scenario A**: Crashed after `generate`, before `accumulate` → should run `accumulate`
- **Scenario B**: Crashed after `accumulate`, before `check_done` → should run `check_done`

With just outputs, both scenarios look identical. Step history tells us which node last completed.

**2. Branches with shared intermediate outputs**

If branch A and branch B both produce `processed`, and we crash after `process_a`:
- Both `finalize_a` and `finalize_b` appear runnable (both see `processed` in state)
- Step history shows `process_a` completed, disambiguating which branch is active

### The spec's resume algorithm

```python
def should_run_node(node, state, steps):
    # 1. Inputs available?
    if not all(inp in state.values for inp in node.inputs):
        return False
    # 2. Find last step for this node
    last_step = find_last_step(node.name, steps)
    if last_step is None:
        return True  # Never ran
    # 3. Compare consumed vs current versions (staleness detection)
    consumed = last_step.input_versions
    current = {inp: state.versions[inp] for inp in node.inputs}
    return consumed != current  # Run if any input changed
```

This algorithm is **already implemented** in `_needs_execution()` in `helpers.py` — but only for in-memory `GraphState.node_executions`. On resume, we create a fresh `GraphState` with no execution history. The step records from the previous run are never loaded.

### What our implementation actually does on resume

1. Load values from `get_state()` → `{"messages": [...], "count": 3}`
2. Merge into fresh `GraphState` → versions all start at 1
3. Run graph from scratch with these values as "inputs"
4. Hope that the graph's logic (gates, conditions) correctly handles the resumed state

This **works for simple cases** (multi-turn chat, simple DAGs) because:
- In chat: `messages` from checkpoint + new `user_input` → graph produces correct output
- In DAGs: edge values from loaded state satisfy downstream inputs, upstream nodes don't run

This **breaks for**:
- Mid-cycle crashes (can't distinguish iteration N from iteration N+1)
- Branches with shared output names (can't tell which branch was taken)
- Partial superstep recovery (can't skip node A that completed but not node B that was running in parallel)

### What we should do

The spec says `Checkpoint = values + steps`. We only load values. We should also load steps and use them to populate `GraphState.node_executions` so the existing `_needs_execution()` / `_is_stale()` machinery works correctly on resume.

This is not "nice to have" — it's **correctness for cycles and branches**.

---

## The `graph.inputs.all` Filter Problem

**User concern**: "I don't like magic... I like explicit and fail fast."

### What we do

```python
checkpoint_state = await checkpointer.get_state(workflow_id)
if checkpoint_state:
    graph_input_names = set(graph.inputs.all)
    checkpoint_inputs = {k: v for k, v in checkpoint_state.items() if k in graph_input_names}
```

We silently drop any checkpoint value not in `graph.inputs.all`. The user never sees which values were dropped or why.

### Why this is wrong

1. **Silent filtering is magic**. If someone stores `embedding` as a checkpoint value and the graph expects it, but it's classified as an edge-produced intermediate rather than an "input", it silently disappears. No error, no warning.

2. **The spec's approach is different**. The spec loads ALL checkpoint state into the execution state, then uses the `should_run_node()` algorithm (with input_versions) to decide what to skip. The filtering happens at the execution level (staleness detection), not at the loading level.

3. **The "don't short-circuit intermediates" concern is real but solved differently**. The worry was: if checkpoint has `embedding` from a prior run, and the new run has an `embed` node, we don't want stale `embedding` to skip the node. But the spec solves this with **input_versions** — on a fresh run (no step history), `embed` would be treated as "never ran" and would execute regardless of whether its output exists in state.

### What we should do

Two options:
- **Option A (spec-aligned)**: Load all checkpoint values, use step history + input_versions for staleness. This is correct but requires implementing step-history loading first.
- **Option B (explicit/fail-fast)**: Keep filtering, but make it explicit — log which values were loaded and which were dropped. Better: let the user control the filter with a parameter.

Option A is the right long-term answer. Option B is a reasonable interim step.

---

## graph_hash: Error by Default, Not Warning

### What the spec says (execution-types.md)

```python
@dataclass
class Workflow:
    graph_hash: str | None = None
    """Hash of the graph definition at workflow creation.

    Used to detect version mismatches on resume. If current graph hash
    differs from stored hash, resume will fail with VersionMismatchError
    unless force_resume=True is specified.
    """
```

This is **error by default** with `force_resume=True` as the escape hatch. Not a warning.

### Why error is the right default

Resuming with a different graph is dangerous:
- Step history may reference nodes that no longer exist
- Node input/output signatures may have changed
- Gate routing decisions may be invalid for the new graph structure
- The implicit cursor (step history + versions) may be meaningless

The spec's approach: **fail loudly**, let the user opt in with `force_resume=True` when they know what they're doing (e.g., bug fix that doesn't change structure).

### What to implement

1. Compute `graph_hash` from: sorted node names, sorted edge tuples, node types, output names
2. Store in `runs` table when creating a run
3. On resume (when `workflow_id` exists in checkpointer):
   - Load stored `graph_hash`
   - Compute current graph hash
   - If different: raise `VersionMismatchError` unless `force_resume=True`
4. Add `force_resume: bool = False` to `run()` and `map()` signatures

---

## Recursive Resume for Nested Graphs

**User question**: "does this whole system work recursively? because we can have multiple nested graphs (with .map runs, nested graphs with .map_over in them)"

### Current state

Our skip-completed logic only works at the **top-level** `map()` call:
- `_get_completed_child_indices()` queries direct children of the map's `workflow_id`
- Nested graphs within each map item create their own child workflows (`batch-001/0/rag`)
- But when a map item is **re-executed** (not skipped), the entire nested graph runs from scratch

### What should happen

For nested `map_over` inside a nested graph:
```
runner.map(graph, {...}, workflow_id="batch")
  → batch/0 → runs inner graph → inner graph has map_over("item")
    → batch/0/processor/0
    → batch/0/processor/1
    → batch/0/processor/2
  → batch/1 → ...
```

If `batch/0` is COMPLETED, we skip it entirely (restore from checkpoint) — ✅ works.
If `batch/0` is FAILED, we re-execute it. But inside, `batch/0/processor/0` might have completed while `batch/0/processor/1` failed. The inner `map_over` should also skip its completed items.

### Does this work today?

**Partially.** The GraphNode executor for nested graphs calls `runner.run()` with a child `workflow_id`. If that child workflow already has completed steps, AND we implement step-history loading (the critical gap above), then the inner graph's execution would benefit from resume.

But `map_over` on GraphNode uses a different path than `runner.map()` — it's handled in the GraphNode executor. That executor does NOT currently have skip-completed logic for individual items.

### What needs to happen

1. Step-history loading (the critical gap) — enables nested graph resume within a single run
2. `map_over` on GraphNode should use the same skip-completed pattern as `runner.map()` — query completed children, skip them
3. This needs to be recursive: each level of nesting should check its own children

---

## Re-Running Completed/Failed/Partial Workflows

**User question**: "what should be the default in cases where the graph ran fully (succeeded, failed, partial) - and then rerun again?"

### What the spec says

The `WorkflowStatus` enum (execution-types.md):

```python
class WorkflowStatus(Enum):
    ACTIVE = "active"       # Can be resumed
    COMPLETED = "completed" # Terminal state — "Can resume? ❌"
    FAILED = "failed"       # Terminal state — "Can resume? ❌"
```

But persistence.md also says:
> "Retry after error: Same `workflow_id`, just run again"

And our `create_run` upsert resets ANY status back to ACTIVE:
```sql
INSERT ... ON CONFLICT(id) DO UPDATE SET status = 'active', ...
```

### The contradiction

The WorkflowStatus table says COMPLETED and FAILED are terminal ("Can resume? ❌"). But the execution semantics say "just run again". And our upsert blindly resets to ACTIVE.

### What should actually happen

| Prior Status | Re-run with same `workflow_id` | Rationale |
|---|---|---|
| ACTIVE | Allow (resume) | Normal resume / HITL continue |
| COMPLETED | **Error by default** | Workflow is done — re-running would append to a completed workflow, creating confusing state |
| FAILED | Allow (retry) | "Fix the bug and try again" pattern |

For COMPLETED → re-run:
- The user probably wants a **new** workflow, not to append to the old one
- If they really want to continue, they should use `force_resume=True` or fork

For FAILED → re-run:
- This is the "retry after fixing the bug" pattern — should work
- Step history should allow skipping successfully completed nodes

### What we should implement

Add status checks in `run()` before the upsert:
```python
if existing_run:
    if existing_run.status == WorkflowStatus.COMPLETED and not force_resume:
        raise WorkflowAlreadyCompletedError(workflow_id)
    # ACTIVE and FAILED allow re-run
```

---

## The `history` / `checkpoint` Parameter

**User concern**: "we need the history w.r.t the node execution order and/or the values in order to understand exactly where we are in the graph. there are some ambiguous cases where we need this."

### What the spec defines

Two related parameters on `run()`:

**1. `checkpoint` parameter (execution-types.md §Resume vs Fork)**:
```python
result = await runner.run(
    graph,
    values={"decision": "reject"},
    checkpoint=checkpoint,           # Checkpoint = values + steps
    workflow_id="order-123-retry",   # NEW workflow
)
```

Parameter combinations:
| `workflow_id` | `checkpoint` | Behavior |
|:---:|:---:|---|
| None | None | Ephemeral run (no checkpointer) or error (with checkpointer) |
| New | None | Fresh start |
| Existing | None | Resume from checkpointer state |
| New | Yes | Fork with explicit workflow_id |
| Existing | Yes | Error: can't fork into existing workflow |

**2. `history` parameter (persistence.md)**:
```python
result = await runner.run(
    graph,
    values={**state, "user_input": "new question"},
    history=steps,                    # Seeds step index, copies trail
    workflow_id="session-456",        # NEW workflow
)
```

These serve overlapping purposes. The `checkpoint` parameter is probably the cleaner API since `Checkpoint = values + steps` bundles them together.

### Why this matters for correctness

The `Checkpoint.steps` contain `input_versions` for each step. These are the **implicit cursor** — they tell the resume algorithm exactly where execution left off:

- In a cycle: which iteration was last completed
- In a branch: which branch was taken
- In a parallel superstep: which nodes completed before the crash

Without loading steps, we lose this information. Our current implementation works by accident for simple cases but is fundamentally incomplete.

### What to implement

1. Add `checkpoint: Checkpoint | None = None` to `run()` signature
2. When resuming (existing `workflow_id`, no explicit `checkpoint`), load `Checkpoint` from checkpointer (not just values)
3. Use `checkpoint.steps` to populate `GraphState.node_executions` before execution begins
4. The existing `_needs_execution()` / `_is_stale()` machinery then handles resume correctly

---

## Gap Summary (Revised)

### Correctness gaps (must fix)

| Gap | What Breaks Without It | Spec Source |
|-----|----------------------|-------------|
| **Step history loading** | Mid-cycle resume, branch disambiguation, partial superstep recovery | execution-types.md §Implicit Cursor |
| **graph_hash + VersionMismatchError** | Silent corruption when graph changes between runs | execution-types.md §Workflow type |
| **Terminal status guard** | Re-running COMPLETED workflow creates confusing mixed state | execution-types.md §WorkflowStatus |
| **Silent value filter** | Values silently dropped, no visibility into what was loaded vs dropped | (fail-fast principle) |

### Safety gaps (should fix)

| Gap | Risk | Spec Source |
|-----|------|-------------|
| **Concurrent execution guard** | Corrupt checkpoint state from parallel writes | persistence.md §Concurrent Execution |
| **Nested map_over resume** | Inner completed items re-execute unnecessarily | (recursive correctness) |

### Feature gaps (defer)

| Gap | What It Enables | Spec Source |
|-----|-----------------|-------------|
| `checkpoint` param on `run()` | Explicit fork from any point | execution-types.md §Resume vs Fork |
| `get_state(superstep=N)` | Time travel / historical queries | checkpointer.md, persistence.md |
| `force_resume` param | Override graph_hash and terminal status checks | execution-types.md §Workflow type |
| CheckpointPolicy behavior | Async writes, retention pruning, TTL | checkpointer.md §Policy |
| ArtifactRef | Large value storage | durability.md §6 |
| GraphNode durability modes | atomic vs nested persistence | durability.md §7-8 |

---

## What the Implementation Does Right

Despite the gaps, the core mechanics are sound:

1. **Value merge with runtime-wins semantics** — correctly implements the spec's priority hierarchy
2. **map() skip-completed** — correct for flat (non-nested) batch operations
3. **Upsert semantics** — correctly handles re-runs without crash
4. **SyncRunner support** — pragmatic deviation from spec that serves real users
5. **Append-only history** — each run appends steps, never overwrites
6. **_validation_ctx guard** — correct optimization to avoid redundant DB reads for map children

The fundamental load → merge → execute → append cycle is right. The gaps are about **what we load** (values only vs values + steps) and **what we check** (graph_hash, terminal status, concurrent execution).

---

## Engine Mental Model: Restore + Continue

The resume approach is **not replay**. It's "restore state as if execution already happened, then let the normal loop take over."

### The execution loop (state machine)

```python
state = initialize(values)

while True:
    ready = get_ready_nodes(graph, state)
    if not ready: break
    execute(ready)           # updates state.values, state.versions
    record(ready)            # updates state.node_executions, state.routing_decisions
    save_steps(ready)        # checkpoint to DB
```

### GraphState — 4 fields

| Field | Type | Purpose |
|-------|------|---------|
| `values` | `{name: Any}` | Current value of every input/output |
| `versions` | `{name: int}` | How many times each value changed (for staleness) |
| `node_executions` | `{node_name: NodeExecution}` | Last execution per node — what versions it consumed |
| `routing_decisions` | `{gate_name: target}` | Gate routing decisions (which branch/target is active) |

### Scheduling: `get_ready_nodes`

A node is "ready" when:
1. **Inputs available**: all required values exist in `state.values`
2. **Gate activation**: if the node is a gate target, the gate must have routed to it (checked via `state.routing_decisions`)
3. **Needs execution**: never ran (`not in node_executions`) OR stale (`consumed_version ≠ current_version`)

### How resume works (the remap-to-1 trick)

Instead of `state = initialize(user_values)` (empty state), we do:

```python
state = initialize_with_checkpoint(
    checkpoint_values,   # ALL values from checkpoint
    user_values,         # user's runtime overrides
    checkpoint_steps,    # step records → node_executions + routing_decisions
)
```

The version alignment trick:
1. Load ALL checkpoint values → each gets version 1
2. Apply user runtime values on top → changed values get version 2
3. Restore `node_executions` with ALL `input_versions` remapped to 1

Result:
- **No user override**: versions = 1, consumed = 1 → not stale → skip
- **User provides different value**: version = 2, consumed = 1 → stale → re-execute → cascade
- **Cascade**: upstream re-executes → bumps downstream input version → downstream stale too

The engine doesn't know it's resuming. It just sees a GraphState and finds what's ready.

### Walkthrough: DAG resume (A,B completed, C failed)

```
Restore:
  values:           {x: 5, a_out: 10, b_out: 20}   (all from checkpoint)
  versions:         {x: 1,  a_out: 1,  b_out: 1}    (all version 1)
  node_executions:  {A: consumed{x:1}, B: consumed{a_out:1}}  (remapped to 1)

get_ready_nodes:
  A: consumed{x:1}, current x=v1 → 1==1 → NOT stale → skip ✓
  B: consumed{a_out:1}, current a_out=v1 → 1==1 → NOT stale → skip ✓
  C: not in node_executions → needs execution → RUNS ✓
```

### Walkthrough: Completed cycle resume

```
Restore:
  values:             {count: 3}
  versions:           {count: 1}
  node_executions:    {increment: consumed{count:1}, check_done: consumed{count:1}}
  routing_decisions:  {check_done: END}

get_ready_nodes:
  increment: gate says END → NOT activated → skip ✓
  check_done: consumed{count:1}, current count=v1 → 1==1 → NOT stale → skip ✓
  No ready nodes → DONE immediately ✓
```

### Walkthrough: DAG resume with override (cascade)

```
Restore:
  checkpoint_values: {x: 5, a_out: 10, b_out: 20}  → all version 1
  user_values:       {x: 100}                        → x bumps to version 2

  versions:          {x: 2, a_out: 1, b_out: 1}
  node_executions:   {A: consumed{x:1}, B: consumed{a_out:1}, C: consumed{b_out:1}}

get_ready_nodes:
  A: consumed{x:1}, current x=v2 → 2≠1 → STALE → runs
  A produces a_out=200 → version bumps to 2
  B: consumed{a_out:1}, current a_out=v2 → STALE → runs (cascade!)
  B produces b_out=400 → version bumps to 2
  C: consumed{b_out:1}, current b_out=v2 → STALE → runs (cascade!)
```

### Progress bar

Event-driven: `NodeStartEvent`/`NodeEndEvent` fire during execution. Restored nodes don't execute → don't emit events. The progress bar only shows nodes that actually run. Optional: `NodeRestoredEvent` for "restored" display.

### Checkpoint continuation

New steps append to existing history. Two offsets needed:
- `step_counter`: starts at `len(checkpoint_steps)` (new step indices continue from old)
- `superstep_idx`: starts at `max_superstep + 1` (new supersteps don't collide)

### Why Restore + Continue, not Replay

| | Replay | Restore + Continue |
|---|---|---|
| **Speed** | O(all steps) — re-executes everything | O(remaining) — only pending/stale nodes |
| **Side effects** | Re-triggers API calls, DB writes | No side effects for skipped nodes |
| **Simplicity** | Simple but slow and dangerous | Uses existing engine logic (no special resume path) |
| **Correctness** | Correct if deterministic | Correct for DAGs + completed cycles via remap-to-1 |

The key insight: the engine already knows how to decide "what runs next" — that's `get_ready_nodes`. We don't teach it a new concept. We just give it a richer starting state.

---

## Known Limitation: Mid-Cycle Crash Recovery

### The problem

The remap-to-1 trick breaks when a cycle crashes **mid-iteration** — i.e., some nodes in the cycle completed the current iteration but not all.

### Concrete example

```
increment(count) → check_done(count) → [END or "increment"]
```

Process crashes after iteration 3's `increment` (count=3) but **before `check_done` runs**.

Checkpoint state:
- `count=3` (latest value)
- Steps: `increment` ran 3 times, `check_done` ran 2 times

With remap-to-1:
```
values:           {count: 3}
versions:         {count: 1}
node_executions:  {increment: consumed{count:1}, check_done: consumed{count:1}}
routing_decisions: {check_done: "increment"}  # last decision was "keep going"
```

Problem: `check_done` consumed count@1, current count=v1 → `1==1` → **NOT stale → skips**

But `check_done` **should run** — it never saw count=3. The remap erased the fact that `increment` ran one more time than `check_done`.

### Why it happens

Remap-to-1 collapses all version history into a single point. In a DAG, each node runs exactly once, so there's only one "last execution" per node — remap works perfectly. In a completed cycle, the gate's `END` decision prevents re-entry regardless of versions — remap works.

But mid-cycle, two nodes have different execution counts (increment ran N times, check_done ran N-1 times). The version difference between them is the signal that check_done needs to run. Remap-to-1 destroys this signal.

### The fix (Phase 2)

**Version-aligned step replay**: Instead of remapping everything to 1, replay the version increments in chronological step order so that `increment`'s last execution consumed `count@v5` but `check_done`'s last execution consumed `count@v4`. The version mismatch correctly triggers re-execution.

This requires:
1. Sorting steps by `(superstep_idx, index)` to replay in execution order
2. Tracking which value each step produced and what version it would have gotten
3. Setting `input_versions` to the actual versions the step would have consumed

### When it matters

Only affects: crash **during** a cycle iteration (between node completions within the same cycle). In practice this is rare — most crashes happen during long-running operations (API calls, etc.), and the cycle typically completes its iteration before the next one starts.

Does NOT affect:
- DAGs (each node runs once — remap is exact)
- Completed cycles (gate decision restored, blocks re-entry)
- Cycle that finished all iterations but crashed after (same as completed)
- Cycle that crashed between iterations (gate decision from previous iteration is correct)
