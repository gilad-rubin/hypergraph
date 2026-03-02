# Checkpoint Resume: Design Session Notes

Deep design discussion exploring resume semantics, the runner/checkpointer boundary, minimal state for scheduling, and the relationship between values, steps, and history.

**Date**: 2026-03-02
**Status**: Exploration — not yet finalized into a plan
**Prior work**: [spec-vs-implementation.md](spec-vs-implementation.md) (gap analysis from previous session)

---

## Terminology

| Term | Meaning | Example |
|---|---|---|
| **Values** | The accumulated data outputs at a point in time | `{x: 5, _check: True, data: 10}` |
| **Steps** | Individual `StepRecord`s stored by the checkpointer | `StepRecord(node_name="A", status=COMPLETED, ...)` |
| **History** | The full chronological sequence of steps — for observability and continuity | All steps from superstep 0 to N |
| **State** (`GraphState`) | The runtime snapshot the engine operates on: values + versions + node_executions + routing_decisions | The 4-field struct in `types.py` |
| **Snapshot** | The minimal data needed to reconstruct a `GraphState` for resume | Values (with gate outputs) + node completions |

---

## The Core Architecture: Runner ≠ Checkpointer

The most important insight from this session. The runner and checkpointer have completely separate concerns:

```
Runner's world:        GraphState → get_ready_nodes() → execute → update state → repeat
                       (doesn't know about history, forks, checkpointers, or persistence)

Checkpointer's world:  Steps, Run metadata, fork lineage, graph_hash
                       (stores history, builds snapshots, tracks relationships between runs)

The bridge:            Resume: checkpointer → reconstruct GraphState → hand to runner
                       Execute: runner → produces step records → checkpointer stores them
```

**The runner doesn't need history. It needs a GraphState snapshot.** Give it a correctly populated `GraphState` and it doesn't know (or care) whether this is a fresh run or a resume from a checkpoint. `get_ready_nodes()` just sees state and schedules accordingly.

This is the same pattern as:
- **Dolt**: WorkingSet (mutable, in-memory runtime) vs commits (immutable, stored history)
- **Git**: working tree (what you're editing) vs commit log (what happened)
- **Beads**: workspace (active work) vs archive (completed work)

---

## Key Design Decisions

### 1. Version Replay, Not Remap-to-1

**Principle**: "There should be no difference between running a graph end-to-end and running it with a stop/failure in the middle."

**Remap-to-1** (rejected): Set all checkpoint values to version 1, remap all restored `input_versions` to 1. Creates a synthetic GraphState that never existed during the original execution. Breaks mid-cycle crash recovery.

**Version replay** (adopted): Reconstruct exact version counts from step records.

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

**Why version replay is correct**: The version of a value = `1 (initial set) + N (step productions)`. This is deterministic from step records. Eliminates "mid-cycle crash" from Phase 2 deferrals.

### 2. Gate Nodes Produce Output Values

**The problem**: Gate routing decisions were invisible in `state.values`. Without gate outputs, there's no way to know which branch was taken from values alone — routing lived only in step history.

**The solution**: Gates produce their return value as a regular data output, using a reserved `_` prefix namespace.

| Gate type | Function name | Output name | Output value | Routing decision |
|---|---|---|---|---|
| `@ifelse` | `is_valid` | `_is_valid` | `True` / `False` | Derived: `gate.when_true if value else gate.when_false` |
| `@route` | `decide` | `_decide` | `"process_a"` (target name) | The value itself |
| `@route(multi_target=True)` | `fan_out` | `_fan_out` | `["a", "b"]` (target list) | The value itself |

**Namespace safety**: Output names starting with `_` are rejected at graph build time (**implemented** in `validation.py`). Gate outputs are generated internally, bypassing this check. No user output can ever collide with a gate output.

**What this changes**:

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

**Why this matters for resume**: `routing_decisions` becomes derivable from values. On resume, read `_is_valid = True` from checkpoint values → look up gate config → derive decision. The scheduler code doesn't change — it still reads `routing_decisions` — but we populate it from values instead of needing step history.

**Implementation status**: `_` prefix validation is committed. Gate output production is pending.

### 3. Minimal State for Resume (The Snapshot)

**What the scheduler (`get_ready_nodes`) actually reads from `GraphState`:**

| GraphState field | Used by scheduler | For what |
|---|---|---|
| `values` | Yes | Input availability — are this node's inputs present? |
| `versions` | Yes | Current version — for staleness comparison |
| `node_executions` | Yes | Did this node run? What `input_versions` did it consume? |
| `routing_decisions` | Yes | Which branch is active? (derivable from gate output values) |

**Within `NodeExecution`, what the scheduler reads:**

| NodeExecution field | Used by scheduler? | Notes |
|---|---|---|
| `node_name` | Yes | Identity (dict key) |
| `input_versions` | Yes | Consumed versions for staleness check |
| `wait_for_versions` | Yes | Wait-for freshness check |
| `outputs` | **No** | Redundant copy of data already in `state.values`. Only used by checkpoint persistence. |
| `duration_ms` | **No** | Observability only |
| `cached` | **No** | Observability only |

**The truly minimal independent data for a checkpoint snapshot:**

```python
# INDEPENDENT (must be persisted):
values: dict[str, Any]          # all computed data, including _gate outputs
node_completions: dict[str, {   # per node, latest completed record only
    input_versions: dict[str, int],
    wait_for_versions: dict[str, int],
}]

# DERIVED (computed on resume, not persisted separately):
versions            # from version replay over steps, or from loading sequence
routing_decisions   # from _gate values + gate config
NodeExecution.outputs    # already in values
NodeExecution.duration_ms  # observability
NodeExecution.cached       # observability
```

**Key implication**: The checkpointer doesn't need to provide the runner with step history. It needs to provide a **snapshot** — enough data to reconstruct a `GraphState`. Steps are one way to build that snapshot, but the runner never reads steps directly.

### 4. Values Carry More Signal Than Before

With gate output values, the table from earlier sessions needs updating:

| | In values | In steps | In node_completions |
|---|---|---|---|
| Node outputs | `{data: 10}` | `step.values` | — |
| Gate decisions | **`{_check: True}`** (NEW) | `step.decision` | — |
| Version timeline | — | `step.input_versions` | `input_versions` |
| Execution record | Implicit (output exists = ran) | `step.status` | Present = completed |
| Side-effect completion | **Gap** (no output to check) | `step.status` | Present = completed |

**Values alone handle most scheduling** — the remaining gap is side-effect nodes (no outputs) and exact version tracking (for cycles). That's what `node_completions` fills.

### 5. Checkpoint = Values + Steps (Already Exists)

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

**Naming inconsistency to address**: `GraphState` has 4 fields, but the checkpointer's `get_state()` returns only values. These are different things. The checkpointer's `get_state()` should ideally return enough to reconstruct a full `GraphState`, not just values.

### 6. Workflow ID Semantics

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

**Checkpointer + no workflow_id = error.** Not warning, not auto-generate.

### 7. Terminal Status: COMPLETED = Error, FAILED = Allow

| Status | Same `workflow_id` | Behavior |
|---|---|---|
| ACTIVE | Allow | Resume (load state, continue) |
| FAILED | Allow | Retry (load state, re-run failed nodes) |
| COMPLETED | Error | `WorkflowAlreadyCompletedError` |

No escape hatch for completed workflows — use fork instead.

### 8. Graph Changes = Fork (Git Model)

**Same workflow_id requires same graph hash.** No exceptions, no `on_graph_change` param.

**Why**: Mixing graph versions in one workflow's step history creates internally inconsistent state. Step records reference node names, edges, version numbers from a specific graph structure. A different graph makes that history nonsensical.

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

User experience:
```python
# Run fails
await runner.run(graph_v1, {"x": 5}, workflow_id="job-1")  # FAILED

# Fix → graph_v2. Same workflow_id → error
await runner.run(graph_v2, workflow_id="job-1")
# GraphChangedError: "Graph structure changed. Fork instead."

# Fork (new workflow_id + checkpoint from old run)
checkpoint = await cp.get_checkpoint("job-1")
await runner.run(graph_v2, checkpoint=checkpoint, workflow_id="job-1-v2")
```

### 9. History Across Graph Changes (Fork with Steps)

When forking with a different graph, the checkpoint has steps from graph_v1 but we're running graph_v2. The staleness machinery handles most cases:

| Change | What happens |
|---|---|
| Fixed function body (same name/inputs/outputs) | Node was FAILED → no execution record → re-runs ✓ |
| Added new node | Not in history → "never executed" → runs ✓ |
| Removed node | Not in new graph → never scheduled → ignored ✓ |
| Changed node inputs | `_is_stale` finds missing input → version 0 ≠ current → stale → re-runs ✓ |
| Gate routing decision | Preserved (from `_gate` values) → correct branch active ✓ |
| Routing to removed target | Target not in graph → not activated ✓ |

**Risk**: Node name collision (old "process" vs new "process" doing different things).

### 10. Immutable History with Visibility

Step history is append-only. If node C fails at step 3 and succeeds on retry at step 7, both records exist:
```
step 3: C → FAILED (error: "timeout")
step 7: C → COMPLETED (values: {...})
```

The execution is identical to running end-to-end (same final state), but the history tells the story of what actually happened. Already how the system works — `save_step` appends, never overwrites.

---

## Steps vs History vs Snapshot — The Three Layers

| Layer | What | Who needs it | Persistence |
|---|---|---|---|
| **Snapshot** | GraphState at superstep N (values + versions + node_executions + routing_decisions) | The **runner** — for scheduling | Reconstructed on resume from steps or stored directly |
| **Steps** | Individual `StepRecord`s with status, timing, input_versions, outputs | The **checkpointer** — stores them, builds snapshots from them | SQLite `steps` table |
| **History** | Full chronological sequence of steps across the run lifetime | The **user** — observability, debugging, time travel, fork lineage | Same storage as steps, but a conceptual view |

The runner consumes **snapshots**. The checkpointer stores **steps** and can build snapshots from them. **History** is the user-facing view of all steps — it's never consumed by the engine.

---

## Updated Before / After (User-Facing)

### 1. Completed nodes re-execute on resume
```python
# Before: A, B, C all re-execute
await runner.run(pipeline, workflow_id="job-1")

# After: A, B skipped (snapshot shows they completed), only failed C re-runs
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

### 4. Intermediate values silently dropped → restored via snapshot
```python
# Before: only graph inputs loaded, intermediates dropped
# After: all values restored from snapshot, completed nodes skip
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

---

## Open Questions

### ~~Branch Ambiguity Without History~~ → Resolved by Gate Output Values (§2)
**Resolved**: Gate outputs (`_is_valid`, `_decide`) make routing decisions visible in `state.values`. Routing decisions are derived from gate output values + gate config.

### History in Fork with Changed Graph
When forking across graph versions, inherited steps reference the old graph's structure. The staleness machinery handles most cases correctly, but node name collisions across versions remain a risk.

**Question**: Should we validate inherited steps against the new graph at fork time? Or just let the engine handle it and warn?

### Definition Hash Sensitivity
`FunctionNode.definition_hash` hashes source code, so even a bug fix changes the graph hash. This makes graph hash mismatches very common in the "fix and retry" workflow, forcing a fork even for trivial changes.

**Question**: Should the graph hash be structural only (node names + edges) or include implementation (source code)? Structural-only would allow same-workflow_id resume after bug fixes but miss implementation changes that affect correctness.

---

## Analogies to Other Systems

Every system with resumable branching workflows separates two concerns:

| System | Position ("where am I?") | Data ("what was computed?") |
|---|---|---|
| **Petri nets** | Marking vector (tokens in places) | Token colors |
| **Temporal** | Event history (command sequence) | Field values in events |
| **Flink** | Source offsets + operator UIDs | Keyed state |
| **Git** | HEAD + branch refs (mutable pointers) | Tree/blob objects (immutable) |
| **Dolt** | Branch pointer + HEAD + WorkingSet | RootValue (content-addressed Prolly tree) |
| **Beads (Yegge)** | Task status + dependency graph | Task details + attachments |

### The critical insight

**A checkpoint is a Petri net marking + token colors.**
- Marking = `{node_completions, routing_decisions}` → position (graph-specific)
- Colors = `{name: value}` → data (portable)

With gate output values, the marking is partially IN the colors — routing decisions are derivable from `_gate` values. The remaining marking-only data is `node_completions` (who ran and what they consumed).

### Dolt as backend: evaluated and rejected

Dolt's conceptual primitives (branches, commits, forks, time travel, three-way merge) map almost perfectly to our checkpoint model. However, from Python, Dolt is a **client-server database**, not an embedded one:

- 103MB Go binary, installed separately (not pip-installable)
- Requires running `dolt sql-server` as a separate daemon process
- Python connects via MySQL protocol (`mysql-connector-python`)
- No in-memory mode for testing (SQLite has `":memory:"`)
- The Go embedded driver (`file://` DSN) has no Python equivalent
- `doltpy` Python package is deprecated (last release Jan 2023)

**Verdict**: We adopt Dolt's design patterns, implement them on SQLite.

---

## Design Patterns Adopted from Research

### From Dolt

| Pattern | What we adopt | How we implement it |
|---|---|---|
| **Fork as reference** | New run points to parent, doesn't copy data | `Run.forked_from` + `Run.fork_superstep` fields |
| **Content-addressed equality** | Cheap "did anything change?" check | `graph.definition_hash` for graph structure |
| **WorkingSet vs Committed** | Separate in-flight from persisted state | `GraphState` (ephemeral, in-memory) vs `StepRecord` (durable, checkpointed) |
| **Schema diff granularity** | Report what changed, not just "hash mismatch" | Future: `GraphDiff` with added/removed nodes. MVP: hash comparison only |

### From Beads (Yegge)

| Pattern | What we adopt | How we implement it |
|---|---|---|
| **"Ready work" as universal primitive** | Resume = normal scheduling on restored state | `get_ready_nodes()` — no special resume logic, just correct state restoration |
| **Externalized state** | Checkpoint is self-describing; fresh engine can load and continue | Snapshot = values + node_completions (complete scheduling state) |
| **Append-only history** | Step records are immutable once written | `save_step` appends, never overwrites |

### From Git

| Pattern | What we adopt | How we implement it |
|---|---|---|
| **Immutable commits + mutable refs** | Steps are immutable, run status is mutable | `StepRecord` (append-only) + `Run.status` (updated on completion/failure) |
| **Branch = cheap pointer** | Fork doesn't duplicate history | `forked_from` references parent; `get_steps` walks the fork chain |

### Not Adopted

| Pattern | Source | Why not |
|---|---|---|
| **Dolt as storage backend** | Dolt | Client-server from Python, 103MB binary, deprecated Python package |
| **Prolly tree storage** | Dolt | Content-addressed dedup overkill for our checkpoint sizes |
| **Full replay** | Temporal | Expensive (51K event limit). "Restore + Continue" is cheaper |
| **Stable UIDs across renames** | Flink | We use node names as identifiers. Simpler, renames are rare |

---

## Implementation Status

| Item | Status |
|---|---|
| `_` prefix validation for output names | **Committed** |
| Gate output value production (§2) | Design complete, implementation pending |
| Version replay (§1) | Design complete, implementation pending |
| Snapshot reconstruction from steps (§3) | Design complete, implementation pending |
| Schema migration (graph_hash column) | Design complete, implementation pending |
| New exceptions | Design complete, implementation pending |
| Checkpointer guards (terminal status, graph hash) | Design complete, implementation pending |
| Fork API (`checkpoint` param on `run()`) | Design complete, implementation pending |
| Plan file update | **Outdated** — still has remap-to-1, needs full rewrite |
