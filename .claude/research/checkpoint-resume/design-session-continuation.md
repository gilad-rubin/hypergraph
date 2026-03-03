# Checkpoint Resume: Continuation Notes

Continuation from `design-session-notes.md` with concrete decisions, break examples, and an implementation-ready path.

Date: 2026-03-02
Status: Proposed continuation (ready for review)

---

## 1) Where We Left Off

The prior session already settled:
- Runner/checkpointer boundary
- Version replay over remap-to-1
- Gate outputs as `_gate_name` values
- Resume semantics (`resume = same identity, no new values`; fork for branching)

The open dilemmas to close:
- Definition hash scope (structural vs structural+code)
- Fork behavior when graph changed (how much validation)
- Policy for new input values on resume (enforce strictly vs permissive mode)

---

## 2) Dilemma A: Definition Hash Scope

### Option A: Structural Hash Only (Recommended for MVP)

Hash includes:
- node names
- node types
- node inputs / wait_for / outputs
- edges
- gate config shape

What it catches:
- Added/removed/rewired nodes
- Signature changes

What it does NOT catch:
- Function body bug fixes with identical structure

Concrete break example:
- Pipeline `A -> B -> C`
- `B` had a logic bug, run completed with wrong `b_out`
- You fix `B` body only (same inputs/outputs)
- Resume/fork with structural hash only: checkpoint layer sees "same graph", so B may be treated as completed unless another mechanism invalidates it

Pros:
- Low implementation complexity
- Minimal DX friction for common refactors
- Aligns with snapshot model and current architecture

Cons:
- Checkpoint layer cannot detect code-only drift

---

### Option B: Structural + Code Hash

Hash includes Option A + node function bytecode/source digest.

Concrete break example:
- Long-running workflow failed at step 98/100
- You only rename a local variable inside node `sanitize_email`
- Entire workflow hash changes, resume blocked, forced fork/recompute path

Pros:
- Strong safety against stale completed results
- Detects bug-fix changes explicitly

Cons:
- High DX friction (frequent forced forks)
- More brittle hashing (source availability, decorators/wrappers)
- Harder mental model for users

---

### Recommendation

Adopt **Option A for MVP**, then combine with cache for code-change ergonomics:
- Checkpoint safety: structural hash
- Code-change recompute: per-node cache invalidation by function digest (already natural in caching model)

This gives:
- Safe graph-structure resume
- Practical bug-fix iteration without forcing "hash everything" at checkpoint layer

---

## 3) Dilemma B: Resume With New Values

### Option A: Allow New Values on Resume

Example:
```python
await runner.run(graph, {"x": 100}, workflow_id="job-1")
```

Pros:
- Convenient one-liner

Cons:
- Ambiguous semantics ("override and continue" vs "branch from checkpoint")
- History integrity becomes confusing
- Harder to explain and test

---

### Option B: Disallow New Values on Resume (Recommended)

Example:
```python
# Resume only
await runner.run(graph, workflow_id="job-1")

# Branch/fork with new values
cp = await checkpointer.get_checkpoint("job-1")
await runner.run(graph, {"x": 100}, checkpoint=cp, workflow_id="job-1-branch")
```

Pros:
- Clear invariant: same identity = same inputs
- Clean observability and reproducibility
- Matches Temporal/Restate/DBOS semantics

Cons:
- Slightly more verbose for users

---

### Recommendation

Adopt **Option B** strictly.

Error message should be explicit:
- `"Cannot pass input values when resuming workflow_id='job-1'. Use checkpoint + new workflow_id to fork."`

---

## 4) Dilemma C: Fork Across Graph Versions (Inherited Step Validation)

### Option A: Minimal Validation (Engine Resolves Lazily)

Behavior:
- Load checkpoint
- Reconstruct state
- Let scheduler staleness determine reruns

Pros:
- Very low complexity

Cons:
- Node name collision risk remains silent

Concrete break example:
- Old graph: node `process` means "normalize currency"
- New graph: node `process` means "call LLM classification"
- Same name, different semantics; inherited completion may be incorrectly trusted

---

### Option B: Fork-Time Compatibility Validation (Recommended)

Before restoring node completions for fork:
- Require node exists in new graph
- Require same node type
- Require same declared inputs/wait_for/outputs
- If mismatch: do not restore this node completion (treat as never-executed) and emit a warning event/log

Pros:
- Prevents the worst silent corruption class
- Keeps MVP complexity moderate (metadata-level checks only)

Cons:
- Needs a small compatibility checker utility
- Warnings/log surface needs definition

---

### Recommendation

Adopt **Option B** with metadata-level compatibility only (no function-body hashing in MVP).

---

## 5) Implementation Sequence (Concrete)

### Phase 1: Correctness Core

1. Gate outputs in values (`_gate_name`) + routing derivation utility
2. Resume reconstruction from checkpoint steps:
   - exact version replay
   - populate `node_executions` from completed steps
   - populate `routing_decisions` from gate values/decisions
3. Status guards:
   - `COMPLETED` => error
   - `FAILED` and `ACTIVE` => allow
4. Enforce `resume != new values`

Exit criteria:
- DAG resume skips completed nodes
- Mid-cycle crash repro executes the missing gate/node correctly
- Branch disambiguation works from gate outputs

---

### Phase 2: Graph Identity + Fork Contract

1. Add `graph_hash` to run metadata
2. Hash check on resume/fork preparation
3. Add `checkpoint` param on run
4. Add fork metadata (`forked_from`, `fork_superstep`)
5. Implement fork-time compatibility filter for restoring node completions

Exit criteria:
- Same workflow_id + changed graph raises deterministic error
- Fork path works with inherited history + new appended steps

---

### Phase 3: Recursive/Nested Resume Parity

1. Reuse skip-completed logic for `GraphNode.map_over`
2. Ensure child workflow traversal is recursive
3. Add nested tests (map -> nested graph -> map_over)

Exit criteria:
- Completed inner map items are skipped on retry/fork
- No duplicate side effects from already-completed nested children

---

## 6) Tests To Add Next (Minimal High-Value Set)

1. Mid-cycle crash resume (version replay correctness)
2. Same workflow_id + runtime values => explicit error
3. Completed workflow resume => explicit error
4. Graph structure mismatch => explicit error
5. Fork with changed node signature => completion dropped, node reruns
6. Fork with unchanged signature => completion restored, node skipped
7. Nested `map_over` retry skips completed inner children

---

## 7) Suggested Default Policy (One-Liner)

Resume should be strict and deterministic:
- same `workflow_id` => same graph structure, same inputs
- any branching intent (new values, graph changes, historical point) => explicit fork

