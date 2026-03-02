# Checkpoint Resume: Design Session Notes

Deep design discussion exploring resume semantics, version replay, fork model, and the fundamental relationship between values and execution history.

**Date**: 2026-03-02
**Status**: Exploration — not yet finalized into a plan
**Prior work**: [spec-vs-implementation.md](spec-vs-implementation.md) (gap analysis from previous session)

---

## Starting Point

The previous session produced a plan with 5 gaps (step history, graph hash, terminal status, silent filter, fork/time-travel) and a "remap-to-1" version alignment approach. This session challenged and refined that design.

---

## Key Design Decisions

### 1. Version Replay, Not Remap-to-1

**Principle**: "There should be no difference between running a graph end-to-end and running it with a stop/failure in the middle."

**Remap-to-1** (rejected): Set all checkpoint values to version 1, remap all restored `input_versions` to 1. Creates a synthetic GraphState that never existed during the original execution. Breaks mid-cycle crash recovery.

**Version replay** (adopted): Reconstruct exact version counts from step history.

```python
def initialize_state_with_checkpoint(checkpoint_values, runtime_values, steps, graph):
    state = GraphState()

    # Compute correct version counts
    graph_input_names = set(graph.inputs.all)
    versions = {}
    for name in checkpoint_values:
        if name in graph_input_names:
            versions[name] = 1                           # initial user input
    for step in completed_steps:
        if step.values:
            for name in step.values:
                versions[name] = versions.get(name, 0) + 1  # each production +1

    # Set state directly (bypass update_value — exact versions)
    state.values = dict(checkpoint_values)
    state.versions = dict(versions)

    # Restore node_executions with ACTUAL input_versions (no remapping)
    for step in completed_steps:
        state.node_executions[step.node_name] = NodeExecution(
            node_name=step.node_name,
            input_versions=dict(step.input_versions),      # actual, as recorded
            outputs=step.values or {},
        )
        if step.decision is not None:
            state.routing_decisions[step.node_name] = _denormalize_decision(step.decision)

    # Runtime overrides via update_value (bumps version if changed)
    for name, value in runtime_values.items():
        state.update_value(name, value)

    return state
```

**Why version replay is correct**: The version of a value = `1 (initial set) + N (step productions)`. This is deterministic from step records. Mid-cycle crash example:

- Original: count goes 0→v1, 1→v2, 2→v3, 3→v4. increment consumed count@v3 last, check_done consumed count@v3 last.
- Replay: count=3 from checkpoint→v1, step outputs 1→v2, 2→v3, 3→v4. Same final versions.
- check_done: consumed@3, current@4 → stale → runs. Correct!
- With remap-to-1: consumed@1, current@1 → not stale → skips. BUG!

**Eliminates "mid-cycle crash" from Phase 2 deferrals.**

### 2. Values vs History — Fundamentally Different State

**Values** = WHAT was computed (data, portable across graph versions)
**History** = WHERE you are in the graph (execution records, routing decisions, graph-specific)

| | In values | In step history |
|---|---|---|
| Node outputs | `{embedding: [...]}` | `step.values` |
| Routing decisions | **NOT stored** | `step.decision` |
| Version timeline | **NOT stored** | `step.input_versions` |
| Execution record | **NOT stored** | `step.status` (COMPLETED/FAILED) |

**Gate decisions are invisible in checkpoint values.** `get_state()` only folds `step.values`. Routing decisions exist only in `step.decision` and `state.routing_decisions`. Without history, gates re-evaluate — which may pick different branches.

### 3. Values-Only Resume Is Broken for Graphs with Gates

**Tested empirically** (not just theory). Running with pre-seeded intermediate values:

```python
runner.run(graph, {"x": 5, "result": 999})
# ValueError: Cannot mix compute and inject for node 'branch_a':
# injected outputs ['result'] and also seeded inputs ['x'].
```

The validation layer already rejects this — you can't provide both a node's inputs AND its outputs. This is the "compute vs inject" conflict in `validate_inputs`.

Even if validation were bypassed, there's a deeper problem:

**Branch ambiguity**: In an ifelse where both branches produce `data`:
```
check(x) → [A1(x)→A2(data) | B1(x)→B2(data)] → merge(result)
```

If we provide `{data: 10}` without routing decisions:
- A2 sees `data` available → ready
- B2 sees `data` available → ready
- No routing decision → both activated (default_open=True)
- Engine can't tell which branch we're in

**And**: Even for nodes NOT behind gates, pre-seeded intermediates make downstream nodes ready before upstream nodes run, breaking execution order.

**Conclusion: History is non-negotiable for correct resume. The framework already knows this.**

### 4. Checkpoint = Values + Steps (Already Exists)

The `Checkpoint` type bundles both:
```python
@dataclass
class Checkpoint:
    values: dict[str, Any]      # accumulated state (from get_state)
    steps: list[StepRecord]     # execution history (from get_steps)
```

Three separate read APIs already exist:
- `get_state(run_id)` → just values
- `get_steps(run_id)` → just steps
- `get_checkpoint(run_id)` → both (convenience for fork)

`CheckpointPolicy.retention` controls what's kept:
- `"full"` — all steps (time travel works)
- `"latest"` — only materialized values (no steps, no skip logic on resume)
- `"windowed"` — last N supersteps

### 5. Workflow ID Semantics

**From the spec** (execution-types.md):

| `workflow_id` | Checkpointer | `checkpoint` param | Behavior |
|---|---|---|---|
| None | No | None | OK — ephemeral run |
| None | Yes | None | **Error** (spec is explicit) |
| None | Yes | Checkpoint | Fork with auto-generated ID |
| New | — | None | Fresh start |
| Existing | — | None | Resume |
| New | — | Checkpoint | Fork with explicit ID |
| Existing | — | Checkpoint | Error (can't fork into existing) |

**Checkpointer + no workflow_id = error.** Not warning, not auto-generate. The spec calls `uuid4()` an anti-pattern.

### 6. Terminal Status: COMPLETED = Error, FAILED = Allow

| Status | Same `workflow_id` | Behavior |
|---|---|---|
| ACTIVE | Allow | Resume (load state, continue) |
| FAILED | Allow | Retry (load state, re-run failed nodes) |
| COMPLETED | Error | `WorkflowAlreadyCompletedError` |

Error message: `"Workflow 'job-1' already completed. Use a new workflow_id for a fresh run."`

No escape hatch for completed workflows — use fork instead.

### 7. Graph Changes = Fork (Git Model)

**Same workflow_id requires same graph hash.** No exceptions, no `on_graph_change` param.

**Why**: Mixing graph versions in one workflow's step history creates internally inconsistent state. Step records reference node names, edges, version numbers from a specific graph structure. A different graph makes that history nonsensical.

**Important**: `FunctionNode.definition_hash` hashes SOURCE CODE (via `hash_definition`). So even a bug fix to a function body changes the graph hash. This means graph hash mismatches are common in the "fix and retry" workflow.

**The git model for forks**:

```
job-1 (graph_v1):
  step 0: check → branch_a         ← immutable
  step 1: A1 → data=10             ← immutable
  step 2: A2 → FAILED              ← immutable

job-1-v2 (graph_v2, forked from job-1 @ step 1):
  steps 0-1: inherited from job-1  ← shared, read-only
  step 2: A2 (fixed) → data2=20   ← new execution
  step 3: A3 → result=30          ← new execution
```

Run type gains fork fields:
```python
@dataclass
class Run:
    id: str
    status: WorkflowStatus
    graph_hash: str | None = None
    forked_from: str | None = None      # parent workflow_id
    fork_superstep: int | None = None   # fork point
```

Properties:
- Each workflow's own steps are internally consistent (one graph version)
- History is immutable (forking references/copies, never modifies parent)
- Lineage is traceable (`forked_from` chain)
- Full history view: walk fork chain for complete timeline
- `get_steps("job-1-v2")` returns inherited steps + new steps

User experience:
```python
# Run fails
await runner.run(graph_v1, {"x": 5}, workflow_id="job-1")  # FAILED

# Fix → graph_v2. Same workflow_id → error
await runner.run(graph_v2, workflow_id="job-1")
# GraphChangedError: "Graph structure changed. Fork instead."

# Fork (inherits history → routing preserved → correct resume)
checkpoint = await cp.get_checkpoint("job-1")
await runner.run(graph_v2, checkpoint=checkpoint, workflow_id="job-1-v2")
```

### 8. History Across Graph Changes (Fork with Steps)

When forking with a different graph, the checkpoint has steps from graph_v1 but we're running graph_v2. The staleness machinery handles most cases:

| Change | What happens |
|---|---|
| Fixed function body (same name/inputs/outputs) | Node was FAILED → no execution record → re-runs ✓ |
| Added new node | Not in history → "never executed" → runs ✓ |
| Removed node | Not in new graph → never scheduled → ignored ✓ |
| Changed node inputs | `_is_stale` finds missing input → version 0 ≠ current → stale → re-runs ✓ |
| Gate routing decision | Preserved → correct branch active ✓ |
| Routing to removed target | Target not in graph → not activated ✓ |

**Risk**: Node name collision (old "process" vs new "process" doing different things). The warning covers this.

### 9. Immutable History with Visibility

Step history is append-only. If node C fails at step 3 and succeeds on retry at step 7, both records exist:
```
step 3: C → FAILED (error: "timeout")
step 7: C → COMPLETED (values: {...})
```

The execution is identical to running end-to-end (same final state), but the history tells the story of what actually happened. Already how the system works — `save_step` appends, never overwrites.

### 10. No `on_graph_change` Param

Removed from the design. Graph change detection stored for observability but doesn't gate `run()`:
- Same hash + same workflow_id → resume
- Different hash + same workflow_id → GraphChangedError (must fork)
- Different hash + fork (new workflow_id + checkpoint) → load steps, let engine handle it

### 11. Gate Nodes Produce Output Values

**The problem**: Gate routing decisions are invisible in `state.values`. The `get_state()` API only returns values — routing decisions live only in step history (`step.decision`) and `state.routing_decisions`. Without history, there's no way to know which branch was taken. This is the "branch ambiguity" problem.

**The solution**: Gates produce their return value as a regular data output, using a reserved `_` prefix namespace.

| Gate type | Function name | Output name | Output value | Routing decision |
|---|---|---|---|---|
| `@ifelse` | `is_valid` | `_is_valid` | `True` / `False` | Derived: `gate.when_true if value else gate.when_false` |
| `@route` | `decide` | `_decide` | `"process_a"` (target name) | The value itself |
| `@route(multi_target=True)` | `fan_out` | `_fan_out` | `["a", "b"]` (target list) | The value itself |

**Namespace safety**: Output names starting with `_` are rejected at graph build time (validation added). Gate outputs are generated internally, bypassing this check. No user output can ever collide with a gate output.

**How this helps resume**:

On a fresh run, the gate `is_valid` executes, stores `_is_valid = True` in `state.values`, and sets `state.routing_decisions["is_valid"] = "approve"`. Both happen automatically.

On resume with checkpoint values:
1. `state.values["_is_valid"] = True` is restored from checkpoint
2. `state.routing_decisions["is_valid"]` is reconstructed: read `_is_valid` from values, look up gate config (`when_true="approve"`), derive decision
3. The scheduler sees the routing decision → only the correct branch is activated
4. The gate itself participates in staleness: if its inputs change, `_is_valid` version bumps → downstream nodes cascade

**What this changes in the execution model**:

```
Before:
  GateNode.data_outputs = ()           # gates produce nothing
  execute_ifelse → return {}            # discard the bool
  state.routing_decisions["is_valid"]   # only place the decision lives

After:
  GateNode.data_outputs = ("_is_valid",)  # gates produce their return value
  execute_ifelse → return {"_is_valid": True}  # store the bool
  state.routing_decisions["is_valid"]     # still set (for scheduler)
  state.values["_is_valid"] = True        # also in values (for checkpoint)
```

**Implementation changes needed**:

| File | Change |
|---|---|
| `nodes/gate.py` | `GateNode.__init__` sets data output name `_<name>`. `data_outputs` returns it. `outputs` = data output + emit outputs |
| `gate_execution.py` | `execute_ifelse` passes `result` (not `None`) to `wrap_outputs`. `execute_route` passes `decision` to `wrap_outputs` |
| `graph/validation.py` | Skip `_` prefix check for `GateNode` outputs (already reserved internally) |
| `graph/_helpers.py` | Edge building: gates now have data edges from `_<name>` to any consumer. Verify no unintended edges |
| `runners/_shared/helpers.py` | On resume, derive `routing_decisions` from gate output values in checkpoint |
| Tests | Gate execution tests verify output value is stored. Resume tests verify routing is reconstructed from values |

**What this does NOT change**:
- User API for gates: `@ifelse(when_true=..., when_false=...)` — no new params
- The scheduler: still reads `state.routing_decisions` — gate outputs feed into it, not replace it
- Existing graphs without checkpointing: gates now produce `_` outputs that sit unused unless consumed

**Interaction with version replay**:

Gate outputs integrate naturally into version replay because they're regular values:

```python
# Version replay counts gate outputs like any other value
for step in completed_steps:
    if step.values:  # now includes {"_is_valid": True}
        for name in step.values:
            versions[name] = versions.get(name, 0) + 1
```

The gate's `_is_valid` gets a version, participates in staleness checks, and cascades correctly. No special-casing needed.

**Resolves**: Open question "Branch Ambiguity Without History" — gate decisions are now IN the values, not only in history. Values carry enough signal for routing even without full step history.

---

## Updated Before / After (User-Facing)

### 1. Completed nodes re-execute on resume
```python
# Before: A, B, C all re-execute
await runner.run(pipeline, workflow_id="job-1")

# After: A, B skipped (history), only failed C re-runs
await runner.run(pipeline, workflow_id="job-1")

# After with override: cascade re-execution
await runner.run(pipeline, {"x": 100}, workflow_id="job-1")
# A re-runs (x changed) → B re-runs (cascade) → C re-runs
```

### 2. Graph changes go undetected → now error + fork
```python
# Before: silently resumes with wrong graph
await runner.run(graph_v2, workflow_id="job-1")

# After: error, guides to fork
await runner.run(graph_v2, workflow_id="job-1")
# GraphChangedError: "Fork instead"

checkpoint = await cp.get_checkpoint("job-1")
await runner.run(graph_v2, checkpoint=checkpoint, workflow_id="job-1-v2")
```

### 3. Completed workflows silently reset → now error
```python
# Before: silently resets to ACTIVE
await runner.run(graph, {"x": 10}, workflow_id="job-1")

# After: error
# WorkflowAlreadyCompletedError: "Workflow 'job-1' already completed."
```

### 4. Intermediate values silently dropped → restored via step history
```python
# Before: only graph inputs loaded, intermediates dropped
# After: all values + history restored, completed nodes skip
```

### 5. Fork / time travel
```python
checkpoint = await cp.get_checkpoint("job-1", superstep=3)
await runner.run(graph, {"decision": "reject"}, checkpoint=checkpoint, workflow_id="fork-1")
```

### 6. Mid-cycle crash recovery (now works, not deferred)
```python
# Before: check_done skips (remap-to-1 bug)
# After: check_done picks up correctly (version replay)
```

### 7. Checkpointer without workflow_id → error
```python
# Before: silently ignores checkpointer
await runner.run(graph, {"x": 5})

# After: error
# MissingWorkflowIdError: "Checkpointer configured but no workflow_id provided."
```

---

## New API Surface

| Addition | Type |
|---|---|
| `checkpoint` param on `run()` | `Checkpoint \| None` |
| `WorkflowAlreadyCompletedError` | Exception |
| `GraphChangedError` | Exception |
| `WorkflowForkError` | Exception |
| `MissingWorkflowIdError` | Exception |
| `Run.graph_hash` | `str \| None` |
| `Run.forked_from` | `str \| None` |
| `Run.fork_superstep` | `int \| None` |

**Removed from earlier plan**: `on_graph_change` param (always error for same workflow_id).

---

## Open Questions

### ~~Branch Ambiguity Without History~~ → Resolved by Gate Output Values (§11)
~~When two branches share parameter names, values alone can't disambiguate which branch is active.~~

**Resolved**: Gate outputs (`_is_valid`, `_decide`) make routing decisions visible in `state.values`. On resume, routing decisions are derived from gate output values + gate config. Values now carry enough signal for routing — history is still better (gives version-accurate state), but values alone no longer lose branch information.

### History in Fork with Changed Graph
When forking across graph versions, inherited steps reference the old graph's structure. The staleness machinery handles most cases correctly, but node name collisions across versions remain a risk.

**Question**: Should we validate inherited steps against the new graph at fork time? Or just let the engine handle it and warn?

### Definition Hash Sensitivity
`FunctionNode.definition_hash` hashes source code, so even a bug fix changes the graph hash. This makes graph hash mismatches very common in the "fix and retry" workflow, forcing a fork even for trivial changes.

**Question**: Should the graph hash be structural only (node names + edges) or include implementation (source code)? Structural-only would allow same-workflow_id resume after bug fixes but miss implementation changes that affect correctness.

---

## Engine Mental Model: Restore + Continue

Full walkthrough documented in [spec-vs-implementation.md](spec-vs-implementation.md) §Engine Mental Model.

Key points:
- The engine is a state machine loop: `initialize → get_ready_nodes → execute → record → repeat`
- GraphState has 4 fields: values, versions, node_executions, routing_decisions
- Resume = restore GraphState to look as if execution already happened, then let the normal loop take over
- The engine doesn't know it's resuming — it just sees a richer starting state
- `get_ready_nodes` is the scheduling brain — checks inputs available, gate activation, staleness
- Staleness: `consumed_version ≠ current_version` (from `_is_stale` in helpers.py)

---

## Analogies to Other Systems

Every system with resumable branching workflows separates two concerns:

| System | Position ("where am I?") | Data ("what was computed?") |
|---|---|---|
| **Petri nets** | Marking vector (tokens in places) | Token colors |
| **Temporal** | Event history (command sequence) | Field values in events |
| **Flink** | Source offsets + operator UIDs | Keyed state |
| **Event sourcing** | Log offset / sequence number | Aggregate state from handlers |
| **Git** | HEAD + branch refs (mutable pointers) | Tree/blob objects (immutable) |
| **Dolt** | Branch pointer + HEAD + WorkingSet | RootValue (content-addressed Prolly tree) |
| **Beads (Yegge)** | Task status + dependency graph | Task details + attachments |

### What breaks when definitions change (universal)

Every system has the same failure modes:
1. **Node removed** → checkpoint references non-existent node
2. **Node added** → new node has no checkpoint data
3. **Branch structure changed** → data was computed assuming one branch, new graph has different branching
4. **Data schema changed** → old checkpoint may not deserialize

### How each system handles it

| System | Strategy |
|---|---|
| **Temporal** | Error (non-determinism). Patching via `get_version()` marker events. Full replay required. |
| **Flink** | Stable UIDs map state to operators. Validates topology at restore time. `allowNonRestoredState` for orphaned data. |
| **Event sourcing** | Upcasting pipeline transforms old events at read time. Event store immutable. |
| **Git** | Immutable commits. Fork = new ref pointing to same commit. Three-way merge using common ancestor. |
| **Petri nets** | Marking IS position. Saving marking + colors = complete state. Topology change = new net. |
| **Dolt** | Content-addressed root hash. Schema diff reports *what* changed. Three-way merge at cell level. |
| **Beads** | Load task statuses, find ready work. Append-only log. Hash-based IDs for collision-free distributed creation. |

### Key design insights for hypergraph

1. **Petri net marking = our `node_executions` + `routing_decisions`**. The set of completed nodes and gate decisions IS the position marker. Values are the token colors. They're conceptually separate even though stored together.

2. **Flink's stable UIDs**: Use stable node identities for checkpoint keys. If a node is renamed but keeps its UID, checkpoint data survives. (We currently use node names — fragile across renames.)

3. **Event sourcing's upcasting**: Schema evolution at read time, not write time. Store checkpoints with version metadata. Transform on load if needed.

4. **Git's fork model**: Immutable history + mutable position pointers. Fork is cheap (new pointer into shared history). Divergent histories are clean because the common ancestor is always findable.

5. **Temporal's lesson**: Full replay is correct but expensive (51,200 event limit). Our "Restore + Continue" approach avoids replay entirely — better for large graphs. But we need Temporal-level strictness about position tracking.

6. **Dolt's fork-as-reference**: A fork is just a pointer into shared history, not a copy of data. Our `forked_from` + `fork_superstep` on `Run` follows this — the new run references the parent's steps rather than duplicating them.

7. **Dolt's schema diff over hash comparison**: When graph structure changes, report *what* changed (which nodes added/removed, which edges changed) rather than just "hash mismatch." More actionable error messages. Not for MVP, but the right long-term direction.

8. **Beads' "ready work" = our scheduling problem**: `bd ready` does a topological sort and returns only actionable items. Our `get_ready_nodes()` is the same pattern. The resume problem IS the scheduling problem — restore state correctly and the normal scheduler produces the right answer. No special "resume logic" needed.

9. **Beads' append-only externalized state**: All task state lives outside the agent's context window. Fresh agents load state and pick up where the last one left off. Validates our "Restore + Continue" approach — a fresh engine loads checkpoint state and runs, just like a fresh Beads agent runs `bd ready` and starts working.

### Dolt as backend: evaluated and rejected

Dolt's conceptual primitives (branches, commits, forks, time travel, three-way merge) map almost perfectly to our checkpoint model. However, from Python, Dolt is a **client-server database**, not an embedded one:

- 103MB Go binary, installed separately (not pip-installable)
- Requires running `dolt sql-server` as a separate daemon process
- Python connects via MySQL protocol (`mysql-connector-python`)
- No in-memory mode for testing (SQLite has `":memory:"`)
- The Go embedded driver (`file://` DSN) has no Python equivalent
- `doltpy` Python package is deprecated (last release Jan 2023)

**Verdict**: Operationally closer to "add PostgreSQL support" than "add SQLite support." The ~100 lines of SQL we need on SQLite are dramatically simpler than asking users to install and operate a Go database server. We adopt Dolt's design patterns, implement them on SQLite.

### The critical insight

**A checkpoint is a Petri net marking + token colors.**
- Marking = `{completed_nodes, routing_decisions}` → position (graph-specific)
- Colors = `{name: value}` → data (portable)

Compatibility checking should operate on the marking against the current graph structure. If the marking references nodes/decisions that don't exist in the new graph, that's an incompatibility — handle it explicitly (error, warn, or transform), never silently.

---

## Design Patterns Adopted from Research

Concrete patterns we're bringing into hypergraph's checkpoint/resume design, with their origin:

### From Dolt

| Pattern | What we adopt | How we implement it |
|---|---|---|
| **Fork as reference** | New run points to parent, doesn't copy data | `Run.forked_from` + `Run.fork_superstep` fields |
| **Content-addressed equality** | Cheap "did anything change?" check | `graph.definition_hash` for graph structure |
| **Three-way merge model** | Framework for reasoning about value conflicts | `checkpoint_values ← runtime_values` (runtime wins, no conflicts for now) |
| **Schema diff granularity** | Report what changed, not just "hash mismatch" | Future: `GraphDiff` with added/removed nodes. MVP: hash comparison only |
| **WorkingSet vs Committed** | Separate in-flight from persisted state | `GraphState` (ephemeral, in-memory) vs `StepRecord` (durable, checkpointed) |

### From Beads (Yegge)

| Pattern | What we adopt | How we implement it |
|---|---|---|
| **"Ready work" as universal primitive** | Resume = normal scheduling on restored state | `get_ready_nodes()` — no special resume logic, just correct state restoration |
| **Externalized state for bounded agents** | Checkpoint is self-describing; fresh engine can load and continue | Checkpoint = values + steps + routing decisions (complete state) |
| **Append-only history** | Step records are immutable once written | `save_step` appends, never overwrites. Failed + retried = both records exist |
| **Gates as checkpoint boundaries** | Natural pause points where state should be persisted | `InterruptNode` already does this; generalizable to any blocking operation |
| **Source of truth vs read cache** | Durable log + fast query layer | SQLite serves both roles for now; JSONL export as future portable format |

### From Petri Nets

| Pattern | What we adopt | How we implement it |
|---|---|---|
| **Marking = position** | `node_executions` + `routing_decisions` IS the position | Restored from step history on resume |
| **Token colors = data** | `values` dict IS the data (portable across graph versions) | Restored from checkpoint values |
| **Topology change = new net** | Graph change requires new workflow (fork) | Same `workflow_id` + different `graph_hash` = error |

### From Git

| Pattern | What we adopt | How we implement it |
|---|---|---|
| **Immutable commits + mutable refs** | Steps are immutable, run status is mutable | `StepRecord` (append-only) + `Run.status` (updated on completion/failure) |
| **Branch = cheap pointer** | Fork doesn't duplicate history | `forked_from` references parent; `get_steps` walks the fork chain |
| **Common ancestor for merge** | Fork point is always findable | `Run.fork_superstep` records where the fork happened |

### Gate Output Values (New — from this session)

Not from external research, but from our own design discussion. Resolves the "branch ambiguity without history" problem.

| Decision | Rationale | Status |
|---|---|---|
| Gates produce output values | Makes routing decisions visible in `state.values`, enables values-based resume for gates | Design complete, implementation pending |
| Output name = `_<function_name>` | Reserved `_` prefix namespace — user output names can't start with `_` | **Implemented** (`validation.py`) |
| ifelse output = `bool` | The raw return value of the gate function | Implementation pending |
| route output = `str` | The target name string (value and decision are the same) | Implementation pending |
| Routing decisions derivable from values | `_is_valid = True` + gate config `when_true="approve"` → decision = `"approve"` | Implementation pending |

See §11 in Key Design Decisions for full details including implementation changes needed.

### Not Adopted

| Pattern | Source | Why not |
|---|---|---|
| **Dolt as storage backend** | Dolt | Client-server from Python, 103MB binary, deprecated Python package. Implement patterns on SQLite instead. |
| **Prolly tree storage** | Dolt | Content-addressed dedup is elegant but overkill for our checkpoint sizes |
| **Hash-based IDs** | Beads | Our `workflow_id` is user-provided, `run_id` is UUID. Both are fine for single-process. |
| **Full replay** | Temporal | Expensive (51K event limit). Our "Restore + Continue" is cheaper and sufficient. |
| **Semantic memory decay** | Beads | LLM-summarized old history. Interesting for `CheckpointPolicy` long-term, not for MVP. |
| **Stable UIDs across renames** | Flink | We use node names as identifiers. Rename = different node. Simpler, and renames are rare. |
