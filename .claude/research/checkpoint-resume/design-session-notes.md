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

| `workflow_id` | Checkpointer | `checkpoint` param | Behavior |
|---|---|---|---|
| None | No | None | Ephemeral run (no persistence) |
| None | Yes | None | **Auto-generate** workflow_id |
| None | Yes | Checkpoint | **Auto-generate** workflow_id, fork from checkpoint |
| Explicit (new) | — | None | Fresh start |
| Explicit (existing) | — | None | Resume |
| Explicit (new) | — | Checkpoint | Fork with explicit ID |
| Explicit (existing) | — | Checkpoint | Error (can't fork into existing) |

**Checkpointer + no workflow_id = auto-generate.** Updated from original spec which said "error." Auto-generating is better DX — less friction for the common case. The workflow_id is returned via `result.workflow_id` so the user can reference it later.

Auto-generated IDs use a short format: `"run-{date}-{short_hash}"` (e.g., `"run-20260302-a7b3c2"`).

### 7. Terminal Status: COMPLETED = Error, FAILED = Allow

| Status | Same `workflow_id` | Behavior |
|---|---|---|
| ACTIVE | Allow | Resume (load state, continue) |
| FAILED | Allow | Retry (load state, re-run failed nodes) |
| COMPLETED | Error | `WorkflowAlreadyCompletedError` |

No escape hatch for completed workflows — use fork instead.

### 8. Graph Changes = Fork (Git Model) — Partially Open

**Same workflow_id requires same graph hash.** No exceptions, no `on_graph_change` param.

**Why**: Mixing graph versions in one workflow's step history creates internally inconsistent state. Step records reference node names, edges, version numbers from a specific graph structure. A different graph makes that history nonsensical.

**Open**: What exactly constitutes the "graph hash" — see the Definition Hash open question. The fork model itself is settled; what triggers it is not.

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

### 10. Resume = No New Values; New Values = Fork

**The DX question**: When resuming a workflow, should you be able to pass new input values?

**Answer: No.** Passing new values to the same workflow_id is conceptually a fork — you're branching from the existing state with different inputs. Making this explicit prevents confusion about what happened:

```python
# Resume: continue exactly where you left off
await runner.run(graph, workflow_id="job-1")

# Fork: start fresh from old state with different inputs
checkpoint = await cp.get_checkpoint("job-1")
await runner.run(graph, {"x": 100}, checkpoint=checkpoint, workflow_id="job-1-retry")

# ERROR: passing values to resume is ambiguous
await runner.run(graph, {"x": 100}, workflow_id="job-1")
# ValueError: "Cannot pass input values when resuming. Fork instead."
```

**Why not allow it?** Two reasons:
1. **Semantic ambiguity**: Does it mean "override and re-run affected nodes" or "replace but keep everything else"? Different users expect different things.
2. **History integrity**: The workflow's step history shows what inputs were used. Silently changing inputs mid-workflow makes the history inconsistent.

**What about retry with the same inputs?** That's just resume — no values needed. The snapshot has everything.

### 11. Caching vs Checkpointing: Different Tools for Different Problems

**Insight from DX exploration**: The "fix a bug and retry" scenario is better served by caching than checkpointing.

| | Caching | Checkpointing |
|---|---|---|
| **Purpose** | Avoid re-computing unchanged nodes | Track and continue specific workflows |
| **Best for** | Development iteration, expensive nodes | Production pipelines, failure recovery |
| **Change detection** | Automatic (content-addressed) | Manual (fork required) |
| **Identity** | None (stateless) | Workflow ID (stateful) |

They compose naturally. A production pipeline uses both: checkpointing for workflow identity/history, caching for efficiency. A development iteration uses caching alone.

### 12. Immutable History with Visibility

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

## The Three Mechanisms: Caching vs Checkpoint vs Fork

These are distinct tools that compose, not alternatives:

| | **Caching** | **Checkpoint (Resume)** | **Fork** |
|---|---|---|---|
| **Identity** | Function + inputs hash (content-addressed) | Workflow ID (identity-addressed) | New workflow ID, inherited state |
| **Scope** | Per-node | Per-workflow | Per-workflow |
| **Invalidation** | Automatic (inputs changed → miss) | Manual (retry or override) | Explicit (new identity) |
| **State** | Individual node results | Entire workflow snapshot | Snapshot from parent |
| **Use case** | Avoid re-computing expensive nodes | Continue a failed/paused workflow | Branch from old state, possibly different graph |
| **Already exists?** | Yes (`cache=True`, `DiskCache`) | Partially (PR #63 — values only, no skip) | No |

### When to use which

| Scenario | Mechanism | Why |
|---|---|---|
| Expensive API call, same inputs across runs | **Caching** | Content-addressed, automatic, no workflow identity needed |
| Pipeline fails at step 5 of 10, retry | **Resume** | Same graph, same identity, skip completed steps |
| Fix a bug in code, re-run | **Fork** | Code changed → graph hash changed → new identity |
| "What if I took the other branch?" | **Fork** | Load old checkpoint, provide different inputs |
| Iterating on node code during development | **Caching** | Avoid re-computing unchanged upstream nodes |

### How they compose

```python
# Caching alone (no checkpointer, no workflow identity)
@node(output_name="embedding", cache=True)
def embed(text: str) -> list[float]:
    return expensive_api_call(text)

runner = AsyncRunner()
await runner.run(pipeline, {"text": "hello"})  # computes embedding
await runner.run(pipeline, {"text": "hello"})  # cache hit, skips embed

# Resume alone (checkpointer, no per-node cache)
runner = AsyncRunner(checkpointer=cp)
await runner.run(pipeline, {"text": "hello"}, workflow_id="job-1")  # fails at step 3
await runner.run(pipeline, workflow_id="job-1")  # skips steps 1-2, retries step 3

# Both: checkpoint tracks workflow, cache avoids re-computation
runner = AsyncRunner(checkpointer=cp)
await runner.run(pipeline, {"text": "hello"}, workflow_id="job-1")  # fails at step 3
await runner.run(pipeline, workflow_id="job-1")  # resume skips 1-2, cache may help step 3

# Fork: new identity from old state
checkpoint = await cp.get_checkpoint("job-1")
await runner.run(graph_v2, checkpoint=checkpoint, workflow_id="job-1-v2")
```

### The "silent bug" scenario

```
Pipeline: A → B (buggy) → C
Run completes successfully. B produced WRONG results, C used them.
User fixes B's code.
```

| Approach | What happens |
|---|---|
| **New run (no checkpoint)** | Everything re-executes from scratch. Correct but wasteful. |
| **New run + caching** | A cache hit (skip), B cache miss (code changed → different hash), C re-runs (B's output changed). Correct and efficient. |
| **Fork** | Load old snapshot. B was COMPLETED. If graph hash includes code → B marked stale → re-runs → C cascades. If structural-only → B skips (WRONG). |
| **Resume same ID** | `WorkflowAlreadyCompletedError` (completed = terminal). |

**Insight**: For the "fix a bug and retry" workflow during development, **caching is the better primitive**. It's automatic (input hash detects changes), works without workflow identity, and handles code changes naturally (function code is part of the cache key). Checkpointing is for production workflows where you need identity, history, and observability.

---

## Updated Before / After (User-Facing)

### 1. Completed nodes re-execute on resume
```python
# Before: A, B, C all re-execute
await runner.run(pipeline, workflow_id="job-1")

# After: A, B skipped (snapshot shows they completed), only failed C re-runs
await runner.run(pipeline, workflow_id="job-1")
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

### 7. Checkpointer without workflow_id → auto-generate
```python
# Before: silently ignores checkpointer
await runner.run(graph, {"x": 5})

# After: auto-generates workflow_id, returns it
result = await runner.run(graph, {"x": 5})
print(result.workflow_id)  # "run-20260302-a7b3c2"
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

### Definition Hash: What To Hash and Why — OPEN

**Research finding**: Neither Temporal nor Restate hash workflow definitions. Both detect code changes lazily at execution time through replay mismatch. But our system is fundamentally different — we don't do replay. We restore a snapshot and continue. So we can't get code change detection "for free."

**Two separate concerns:**

| Concern | Question | Purpose |
|---|---|---|
| **Structural compatibility** | "Does the snapshot fit this graph?" | Prevent nonsensical resume (snapshot references nodes that don't exist) |
| **Code change detection** | "Did any node's implementation change?" | Prevent stale results (node ran with buggy code) |

**Options under consideration:**

| Approach | Structural | Code changes | Complexity | Notes |
|---|---|---|---|---|
| **No hash** | Not detected | Not detected | Zero | Like Temporal pre-replay; risky for us since we don't replay |
| **Structural hash only** | Error + fork | Not detected | Low | Safe for structure; code changes invisible to checkpoint layer |
| **Structural + code hash** | Error + fork | Error + fork | Medium | Forces fork for every bug fix |
| **Structural hash + per-node cache** | Error + fork | Cache miss → re-compute | Medium | Best of both? Cache detects code changes at node level |

**Temporal's approach (for reference)**: No upfront detection. At replay time, if the workflow code generates a different command sequence than what's in history → `[TMPRL1100] Nondeterminism error`. Safe changes (activity internals, logic inside activities) are invisible. Unsafe changes (reordering activities, adding/removing workflow API calls) are caught.

**Restate's approach**: Pins invocations to deployment endpoints. Code changes at the same endpoint → `RT0016 Journal mismatch`. New code must be deployed to a new endpoint, then invocations migrated.

**Key question**: Since we don't do replay, what level of hash gives us the right tradeoff between safety and DX friction?

**Industry survey** (4 systems):

| System | Resume model | Code hash | Code change handling |
|---|---|---|---|
| **Temporal** | Full replay | None | Replay mismatch error (lazy) |
| **Restate** | Journal replay | None (deployment ID) | Journal mismatch error (lazy) |
| **DBOS** | Re-execute + memoize | SHA-256 of all functions | Version mismatch → no auto-recovery; use `fork_workflow` |
| **LangGraph** | Snapshot + `next` | None | Not detected; manual time-travel |

Nobody does per-node hashing or structural-only hashing. The split is: replay-based systems get detection for free (Temporal, Restate), snapshot-based systems either hash the whole app (DBOS) or punt entirely (LangGraph).

**Our closest analog is LangGraph** (snapshot restore, no replay). They chose "no detection, user time-travels manually." DBOS is the cautious alternative — hash everything, fork to recover.

### Value Override on Resume — OPEN

Passing new values to the same `workflow_id` is conceptually a fork, not a resume. But should we enforce this strictly?

**All four systems agree: same identity = same inputs.**
- Temporal: cannot change inputs on retry; new execution required
- Restate: `restart-as-new` uses original input; new invocation for different inputs
- DBOS: `resume_workflow` always uses stored inputs; new workflow for different inputs
- LangGraph: `update_state` creates a **new checkpoint** (effectively a fork within the thread); or use time-travel

LangGraph's `update_state` is interesting — it modifies state but creates a new checkpoint, so the old state is preserved. It's a fork in disguise, within the same thread_id.

**Proposed**: Resume = no new values. New values = requires fork (new workflow_id + checkpoint).

### History in Fork with Changed Graph
When forking across graph versions, inherited steps reference the old graph's structure. The staleness machinery handles most cases correctly, but node name collisions across versions remain a risk.

**Question**: Should we validate inherited steps against the new graph at fork time? Or just let the engine handle it and warn?

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

### Durable Execution Engines: Detailed Comparison

Research into how four production systems handle resume, code changes, forks, and input overrides.

**The two resume families:**

| Family | Systems | Mechanism | Code change detection |
|---|---|---|---|
| **Replay-based** | Temporal, Restate, DBOS | Re-execute from top; skip completed via memoization or event matching | Implicit (mismatch during replay) |
| **Snapshot-based** | LangGraph, **Hypergraph** | Store full state + "what's next"; jump to pending work | Must be explicit (no replay to compare) |

**Full comparison:**

| | Temporal | Restate | DBOS | LangGraph | Hypergraph |
|---|---|---|---|---|---|
| **Resume** | Full replay | Journal replay | Re-exec + memoize | Snapshot + `next` field | Snapshot + `get_ready_nodes` |
| **Code hash** | None | None (deploy ID) | SHA-256 all functions | None | TBD |
| **Code change** | Replay mismatch | Journal mismatch | No auto-recovery | Not detected | TBD |
| **Input override** | New execution | New invocation | New workflow | `update_state` (new checkpoint) | TBD |
| **Fork** | Reset (kills old) | `restart-as-new` | `fork_workflow(id, step, version)` | `update_state` on historical checkpoint | `checkpoint` + new workflow_id |
| **Structure change** | `GetVersion`/`patched()` | New deployment | Version mismatch | Error if `next` → removed node | TBD |
| **Versioning** | In-code markers | Infra (deployments) | App-level hash | None | TBD |
| **Old run preserved?** | No (terminated) | Yes | Yes (independent) | Yes (checkpoint DAG) | Yes |

**Key details per system:**

**Temporal**: `GetVersion(ctx, changeId, min, max)` records marker events in history for in-code branching. Worker Versioning (build IDs) for infra-level routing. Reset API rewinds to a `WorkflowTaskStarted` event — same workflow ID, new run ID.

**Restate**: Invocations pinned to deployment ID. No in-code versioning API. `restart-as-new?from=N&deployment=new_id` copies journal prefix, resumes with new code. `RT0016 Journal mismatch` if code changes at same endpoint.

**DBOS**: Hashes ALL registered workflow functions into one SHA-256. Version-mismatched workflows sit in PENDING, not auto-recovered. `fork_workflow(id, start_step, application_version)` is the explicit recovery — copies step outputs 0..N-1, starts fresh at step N. `DBOS.patch("change_id")` for safe incremental code evolution (returns True if workflow hasn't passed this point yet).

**LangGraph**: Stores `next: tuple[str, ...]` in every checkpoint — literal list of nodes to execute. No code change detection at all. `update_state(config, values, as_node)` creates new checkpoint (respects reducers); `as_node` controls what `next` becomes. Time-travel = `update_state` on a historical `checkpoint_id`. `put_writes` stores partial superstep results for parallel crash recovery.

**What this means for us:**
- All four agree: same identity = same inputs
- LangGraph is our closest model (snapshot-based, no replay). They punt on code changes entirely.
- DBOS shows the "hash everything + fork" approach works in practice
- Fork is the universal recovery mechanism across all four systems
- LangGraph's `put_writes` is worth studying for our parallel superstep crash recovery

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
