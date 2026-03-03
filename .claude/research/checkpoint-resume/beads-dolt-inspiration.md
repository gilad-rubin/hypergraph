# Beads & Dolt: Inspiration for Checkpoint Resume

Research into two projects and how their ideas apply to hypergraph's checkpoint/resume system with branching/versioning.

---

## 2. Dolt (dolthub/dolt)

### Core Model

Dolt is a SQL database with Git-like version control. Every database operation is versioned: branches, commits, diffs, and merges work on structured tabular data.

**Primitives**:
- **Commit**: Snapshot of the entire database state (`RootValue` — all tables + schemas). Contains: root value hash, parent commit hashes, author, timestamp, message.
- **Branch**: Named pointer to a commit (lightweight, O(1) to create)
- **WorkingSet**: Current mutable state — has `WorkingRoot` (uncommitted changes) and `StagedRoot` (staged changes). Analogous to Git's working tree + index.
- **HEAD**: Points to the current branch tip
- **RootValue**: The actual database content — all tables, schemas, stored procedures. Content-addressed via Prolly tree root hash.
- **Tag**: Immutable named reference to a commit

The commit graph is a DAG (exactly like Git), with commits pointing to parent(s).

### Position vs Data

Dolt cleanly separates these:

| Concept | "Where you are" | "What the data is" |
|---------|-----------------|---------------------|
| Primitive | Branch pointer, HEAD, WorkingSet | RootValue (content-addressed tree) |
| How it changes | `checkout`, `commit`, `merge` (pointer moves) | INSERT/UPDATE/DELETE (new tree nodes created) |
| Cost of change | O(1) — just move a pointer | O(changed rows) — new Prolly tree nodes |
| Identity | Branch name / commit hash | Content hash of the Prolly tree root |

**Key insight**: The position (branch + HEAD) is just a pointer into the content-addressed data store. Changing position is free. Changing data creates new content. They never interfere.

This maps to our system:

| Dolt | Hypergraph |
|------|------------|
| Branch pointer (HEAD) | Current superstep index + routing decisions |
| RootValue (content hash) | `GraphState.values` + `GraphState.versions` |
| Commit (snapshot + metadata) | `StepRecord` (node execution + input_versions + values) |
| WorkingSet (uncommitted changes) | In-flight node executions (not yet checkpointed) |

### Storage: Prolly Trees and Structural Sharing

Dolt stores all data in **Prolly trees** — a content-addressed data structure that combines B-tree search with Merkle tree verification:

1. Key-value pairs are sorted and serialized into a byte stream
2. A rolling hash determines block boundaries (~4KB average blocks)
3. Each block is content-addressed (hash of its content = its identity)
4. Internal nodes hold `<first_key, child_hash>` pairs
5. The root hash identifies the entire tree

**Why this matters for us**: Structural sharing means that two versions of the data that differ by one row share almost all their physical blocks. The cost model:

| Operation | Cost |
|-----------|------|
| Create branch | O(1) — copy a pointer |
| Commit with small change | O(changed blocks) — ~4KB x tree depth (~3-4 levels) |
| Diff two commits | O(changed blocks) — only walk diverging hash paths |
| Fork from arbitrary commit | O(1) — just create a branch pointer at that commit |

**Implication for checkpoint forking**: If we used content-addressed storage for checkpoint values (hash the value dict, store blocks), forking from a checkpoint would be nearly free — the new run shares all the parent's stored values until it diverges. This is the "cheap fork" property we want.

We don't need to implement Prolly trees. But the principle is clear: **content-addressed storage makes forking cheap because identical content is shared automatically**.

### Schema Evolution Across Branches

Dolt's three-way merge handles schema changes by comparing ancestor, left (ours), and right (theirs):

**Compatible changes** (auto-merged):
- One branch adds a column, the other doesn't touch schema
- One branch adds an index, the other adds data

**Conflicting changes** (require manual resolution):
- Same column modified to different types on different branches
- Same column renamed differently on different branches
- Primary key changed differently on different branches

Schema conflicts are detected during merge and stored in `dolt_schema_conflicts` table. The merge blocks until resolved.

**Implication for graph_hash**: Dolt's schema conflict detection is analogous to our `graph_hash` mismatch problem. When the graph structure changes between runs:

```
Dolt:   schema changed between branches → merge conflict → must resolve
Hypergraph: graph changed between runs → VersionMismatchError → must force_resume
```

Dolt is more granular — it can tell you *what* changed (which columns, which tables). We could do the same: instead of just "graph hash differs", report *what* changed (which nodes were added/removed, which edges changed).

### Fork / Branch Semantics

**Branch from any point**: Dolt supports `dolt branch <name> <commit>` — create a branch from any commit in history, not just HEAD. This directly maps to our "fork from checkpoint" feature:

```
Dolt:   dolt branch experiment abc123   (branch from commit abc123)
Hypergraph: runner.run(graph, checkpoint=checkpoint, workflow_id="experiment")  (fork from checkpoint)
```

**What's shared**: Everything. A new branch is just a pointer to the same commit. The underlying Prolly tree blocks are fully shared. Only when you commit new changes on the branch do new blocks get created.

**What diverges**: Only the changed data. If you modify one row, only the Prolly tree path from that row to the root gets new blocks. Everything else is shared with the parent.

**Merge**: Three-way merge with the common ancestor as the base. Conflicts detected at the cell level (individual column values in the same row).

### Diff System: How "What Changed" Works

Dolt's diff uses the `ThreeWayDiffer` which compares three `RootValue` trees:

```
Ancestor (common base)
    ├── Ours (left branch changes)
    └── Theirs (right branch changes)
```

Diff events generated:
- `LeftAdd` / `RightAdd` — one side added a row
- `LeftModify` / `RightModify` — one side changed a row
- `RightDelete` — one side deleted a row
- `DivergentModifyConflict` — both sides changed the same row differently
- `ConvergentAdd` / `ConvergentModify` — both sides made the same change (auto-resolved)

**Cell-level merging**: The `valueMerger.TryMerge()` function checks individual column values:
- Identical values → no conflict
- Only one side changed → use changed value
- Both sides changed differently → conflict

**Implication for checkpoint merge**: When we merge checkpoint values with runtime values, we're doing a simplified version of Dolt's merge:

```
checkpoint_values (ancestor/base)
    ├── existing checkpoint values (what was computed)
    └── runtime values (what the user provides now)
```

Runtime always wins in our model (no conflicts). But the concept of cell-level merge is relevant if we ever support merging two divergent runs.

### Transaction Model

Dolt's transaction model provides useful analogies:

- **Optimistic concurrency**: Each session works on its own copy. At commit time, Dolt checks if HEAD moved and merges if needed.
- **Commit-time merge**: If two sessions modify the same table, the later commit triggers a three-way merge against the current HEAD.
- **Session isolation**: Changes are invisible to other sessions until committed.

This maps to our concurrent execution concern: two runners executing the same `workflow_id` would be like two Dolt sessions committing to the same branch. Dolt's answer: optimistic concurrency with merge-on-commit. Our answer (from the spec): concurrent execution guard that prevents it entirely.

### Inspiration for Hypergraph

**1. Commit = StepRecord (Position + Data Together)**

A Dolt commit stores both the data snapshot (RootValue) and the metadata (parent hashes, author, timestamp). Our `StepRecord` already does this: it stores both the output values and the execution metadata (input_versions, superstep, index).

The commit graph structure suggests we should think of step records as forming a chain:

```
StepRecord[0] → StepRecord[1] → StepRecord[2] → ...
     ↑                                    ↑
  (parent: none)                     (parent: previous superstep's state)
```

Each step record implicitly points to its predecessor through `(superstep, index)` ordering. The full chain IS the execution history, and the latest step IS the position.

**2. Branch = Fork Point (Cheap Reference)**

Dolt teaches us that a fork should be **just a reference**, not a copy:

```python
# Current: fork copies all values
checkpoint = checkpointer.get_state(workflow_id)  # load everything
runner.run(graph, values=checkpoint, workflow_id="fork-1")  # store everything again

# Dolt-inspired: fork references parent
fork_run = Run(
    id="fork-1",
    parent_run_id=original_run_id,
    fork_point=superstep_3,  # "branch from this commit"
)
# Only store delta values (what changes after fork point)
```

We already have `parent_run_id` on `Run`. The missing piece is `fork_point` — which superstep in the parent we forked from. This is analogous to Dolt's "branch from commit X".

**3. RootValue Hash = State Hash (Cheap Equality Check)**

Dolt's content-addressed root hash lets you instantly check if two database states are identical: same hash = same data. We could hash our `GraphState.values` to get the same property:

```python
state_hash = hash(sorted(state.values.items()))  # simplified
```

Uses:
- **Skip redundant checkpoints**: If state_hash didn't change, don't write a new checkpoint
- **Detect stale forks**: If two forks produce the same state_hash, they're equivalent
- **Graph_hash for structure**: Already planned — hash of graph topology

**4. Three-Way Merge for Checkpoint Conflict Detection**

If we ever support merging two divergent runs (e.g., two branches of an A/B test that reconverge), Dolt's three-way merge model applies directly:

```
Common ancestor checkpoint (fork point)
    ├── Branch A values (one execution path)
    └── Branch B values (another execution path)
```

For now, our merge is simple (runtime overrides checkpoint). But the three-way model is the right framework if we add merge capabilities later.

**5. WorkingSet = In-Flight Execution State**

Dolt's distinction between WorkingSet (uncommitted changes) and committed state maps perfectly to our execution model:

| Dolt | Hypergraph |
|------|------------|
| WorkingRoot (current mutations) | GraphState during execution (in-memory) |
| StagedRoot (staged for commit) | Values computed but not yet checkpointed |
| Committed (saved to history) | StepRecord written to checkpointer |
| HEAD (where you are) | Current superstep + routing decisions |

The WorkingSet is ephemeral — it exists only during a transaction. Similarly, our GraphState is ephemeral during execution. The checkpointed StepRecords are the permanent history.

**6. Schema Diff Granularity for Graph Changes**

Instead of just `graph_hash differs → error`, we could report what changed:

```python
class GraphDiff:
    added_nodes: list[str]
    removed_nodes: list[str]
    changed_edges: list[tuple[str, str]]
    changed_node_types: list[str]

# On resume:
if current_hash != stored_hash:
    diff = compute_graph_diff(stored_graph_info, current_graph)
    raise VersionMismatchError(
        f"Graph changed since last run: {diff}",
        diff=diff,
        force_resume_hint=True,
    )
```

This is more helpful than a binary "hash mismatch" — the user can see exactly what changed and decide if `force_resume=True` is safe.

**7. Optimistic Concurrency for Concurrent Execution**

Dolt's approach to concurrent writes is instructive:
1. Each session works independently (optimistic)
2. At commit time, detect if state changed underneath
3. If so, attempt merge; if conflicts, abort

For hypergraph, the simplest version:
1. Before writing a step record, check that the run is still ACTIVE
2. If the run was completed/failed by another process, abort
3. If another process wrote steps we didn't expect, abort

This is simpler than Dolt's full merge but follows the same pattern: optimistic execution with conflict detection at write time.

---

## Synthesis: What to Steal

### High-Value, Low-Effort Ideas

| Idea | Source | Maps To | Effort |
|------|--------|---------|--------|
| **Workspace/Archive lifecycle** | Beads | ACTIVE/COMPLETED status guards | Already designed |
| **Graph diff on hash mismatch** | Dolt schema diff | Richer VersionMismatchError | Small |
| **Explicit dependency verification** | Beads manifests | Load all values + verify, don't silently filter | Medium |
| **Fork as reference, not copy** | Dolt branches | `parent_run_id` + `fork_point` on Run | Medium |
| **State hash for skip detection** | Dolt RootValue hash | Skip redundant checkpoint writes | Small |

### Medium-Value, Higher-Effort Ideas

| Idea | Source | Maps To | Effort |
|------|--------|---------|--------|
| **Content-addressed value storage** | Dolt Prolly trees | Dedup large values across forks/runs | Large |
| **Three-way merge for run convergence** | Dolt merge system | Merging divergent execution branches | Large |
| **Cell-level conflict detection** | Dolt valueMerger | Detecting conflicting outputs from parallel forks | Large |

### Conceptual Frameworks

**From Beads**: Think of a completed run as an immutable archive. Once done, don't mutate — fork. The kind/content_id duality maps to graph_name/workflow_id. The manifest concept maps to graph_hash + step integrity checks.

**From Dolt**: Think of execution state as content-addressed data where the "position" (branch/HEAD) is just a cheap pointer into it. Forking is creating a pointer, not copying data. Merging is reconciling divergent pointers. Schema evolution (graph changes) should be handled with granular diff reporting, not binary hash comparison.

### The Unified Mental Model

```
Git (code)     │  Dolt (data)      │  Beads (computation)  │  Hypergraph (execution)
───────────────┼───────────────────┼───────────────────────┼─────────────────────────
Repository     │  Database         │  Box (collection)     │  Checkpointer
Commit         │  Commit           │  Archive (bead)       │  StepRecord chain
Branch         │  Branch           │  (implicit in DAG)    │  Run (workflow_id)
HEAD           │  HEAD/WorkingSet  │  Workspace            │  Current superstep + routing
Working tree   │  WorkingRoot      │  Workspace files      │  GraphState (in-memory)
Staging area   │  StagedRoot       │  (none)               │  Computed but not yet saved
Tag            │  Tag              │  (none)               │  Run(status=COMPLETED)
Fork           │  Branch from X    │  New bead, same input │  run(checkpoint=X, id=new)
Merge          │  Three-way merge  │  (manual)             │  (not yet, but framework exists)
Diff           │  Three-way diff   │  Hash verification    │  input_versions staleness
Schema         │  Table schema     │  Bead structure       │  Graph topology (graph_hash)
```

---

## Open Questions

1. **Should we store a `fork_point` on Run?** Dolt's "branch from commit X" is powerful. We have `parent_run_id` but not which specific superstep we forked from. Adding this would make fork semantics precise.

2. **Should checkpoint writes be content-addressed?** If two steps produce identical values, should we dedup at the storage level? Dolt says yes (Prolly trees). For us, probably not worth it yet — but worth keeping in mind for large-value workloads.

3. **Should graph changes be diffed, not just hashed?** Dolt's schema diff is more useful than Git's binary "file changed" signal. A `GraphDiff` that reports added/removed nodes and changed edges would help users decide whether `force_resume=True` is safe.

4. **Should we support run merging?** If two forks of a workflow complete and we want to combine their results, Dolt's three-way merge gives us a framework. Not needed now, but the spec's Checkpoint type (values + steps) already has the structure for it.

5. **Is beads' "no automatic cascade" the right default?** When a graph changes between runs, should we automatically re-run affected nodes (cascade like Dolt's merge) or require explicit intervention (like beads' manual update)? Our current `force_resume` is closer to beads — explicit and safe.
