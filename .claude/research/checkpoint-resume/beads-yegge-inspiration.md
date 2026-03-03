# Steve Yegge's Beads: Inspiration for Checkpoint Resume

Research into [steveyegge/beads](https://github.com/steveyegge/beads) — a distributed, Git-backed issue tracker designed for AI agent workflows — and what its design ideas mean for hypergraph's checkpoint/resume system.

**Note**: This is a different project from the `bead-project/bead` computational archive system covered in `beads-dolt-inspiration.md`. Yegge's Beads is an issue tracker with dependency graphs, not a data packaging tool. The overlap is in name only.

---

## 1. What Is Beads?

A Git-native, dependency-graph issue tracker that gives AI coding agents persistent memory across sessions.

**The problem it solves**: "50 First Dates" for AI agents — every session starts with zero memory of prior work, decisions, and dependencies. Agents get "dementia" when faced with competing, obsolete, or conflicting markdown files scattered across a project. Beads replaces chaotic markdown plans with structured, version-controlled, queryable state.

**Core model**: A "bead" is a work item (issue/task) with:
- Hash-based ID (`bd-a1b2`) — collision-free in distributed/multi-agent environments
- Type, priority, status, labels
- **Explicit dependencies** forming a DAG — `blocks`, `parent-child`, `conditional-blocks`, `waits-for`
- Stored in Git (originally JSONL, now Dolt) alongside the code it describes

**Key metaphor**: Work items are beads threaded on strings of dependencies. The strings define execution order. Some strings interweave (fan-in/fan-out), some are serial. All beads must honor the constraint that they hang on their strings in dependency order.

**Built by Yegge in 6 days** using Claude, guiding the AI on outcomes (parent/child pointers, blocking dependencies) while letting it design the schema. Has 13,700+ GitHub stars.

---

## 2. Architecture Evolution

### Phase 1: Git + JSONL + SQLite (original)

Three-layer hybrid storage:

| Layer | Role | Versioned? |
|-------|------|------------|
| **JSONL** (`.beads/issues.jsonl`) | Operational source of truth, append-only log | Yes (git-tracked) |
| **SQLite** (`.beads/beads.db`) | Fast query cache, derived state | No (gitignored, rebuildable) |
| **Git** | Historical source of truth, distribution, branching | Yes |

JSONL is append-only — creating a task appends a line, editing a task appends a new record. This makes Git merges conflict-resistant: two agents creating tasks on different branches produce independent log entries that concatenate cleanly.

The SQLite cache provides fast indexed queries (`bd ready` in milliseconds) without parsing growing append-only logs. A sync mechanism keeps SQLite and JSONL in agreement. The SQLite DB is derived — it can always be rebuilt from JSONL via `bd sync --force-rebuild`.

### Phase 2: Dolt backend (v0.51+)

Yegge acknowledged the hybrid was "reaching for Dolt without knowing about it." The Dolt backend collapses all three layers into one:

- Version-controlled SQL database (Git semantics on structured data)
- Automatic Dolt commit on every write (full audit trail)
- Cell-level merging (less conflicts than line-level JSONL merge)
- Time-travel queries (`AS OF` any commit)
- Native branch/checkout/merge for issue state
- Prolly tree storage — structural sharing across versions

The JSONL sync pipeline was removed in v0.56.0. JSONL is now export-only for migration/portability.

---

## 3. How Versioning and Branching Work

### Issue state branches with code

With the Dolt backend, creating a Git branch also branches the issue database. Agents working on an experimental branch see only the tasks relevant to that experiment. When the branch merges back, task states merge too.

With the older JSONL model, branching was implicit — hash-based IDs prevent collisions across branches, and the append-only log merges cleanly. The merge driver performs field-level merging: last-write-wins for scalars, union for arrays, priority rules for status fields.

### Hash-based IDs: collision-free distributed creation

Traditional issue trackers use sequential IDs (1, 2, 3) — these collide when multiple agents create tasks on different branches. Beads uses hash-based IDs (`bd-a1b2`) with progressive length scaling:
- Small projects: 4-char hashes
- Medium: 5-char
- Large: 6+
- Birthday paradox math determines when to extend

This eliminates the need for centralized coordination. Multiple agents, machines, and branches can all create work items simultaneously, and Git merges them naturally.

### Atomic operations

The Dolt backend wraps operations in transactions — SQL commit first, then Dolt version commit. The `RunInTransaction` pattern ensures atomicity. Retry logic with exponential backoff handles transient connection errors.

---

## 4. Dependency DAG and "Ready Work"

The most relevant feature for hypergraph. Beads models work as a directed acyclic graph with explicit dependencies.

### Dependency types

**Blocking** (affect ready-work calculation):
- `blocks`: Hard dependency — A must close before B can start
- `parent-child`: Children blocked if parent is blocked
- `conditional-blocks`: B runs only if A fails (error handling)
- `waits-for`: Fan-in aggregation — wait for ALL dependencies

**Non-blocking** (informational):
- `related`, `tracks`, `discovered-from`, `caused-by`, `validates`

### The "ready" calculation

`bd ready` performs a topological sort and returns only issues with no open blockers. This is the key insight: **the tool does the deterministic thinking, not the agent**. Instead of making an LLM analyze a dependency graph (burning tokens and being error-prone), Beads handles it in Go and serves only what's actionable.

**Direct parallel to hypergraph**: Our `get_ready_nodes()` does exactly this — returns nodes whose inputs are satisfied and that haven't been executed yet (or are stale). The difference: Beads' "ready" is about task-level completion status; ours is about data-flow availability + staleness detection.

### Workflow automation constructs

| Construct | What | Persistence |
|-----------|------|-------------|
| **Formula** | Declarative workflow template (TOML/JSON) — defines steps with `needs` dependencies | Template (reusable) |
| **Molecule** | Persistent instance of a formula — parent epic with children as steps | Git-synced |
| **Wisp** | Ephemeral molecule — auto-expires, never syncs | Local only |
| **Gate** | Async coordination primitive — blocks until external condition met | Attached to issue |
| **Bond** | Dependency between work graphs (molecules/epics) | Git-synced |

Gate types: `gh:pr` (wait for PR merge), `gh:run` (wait for CI), `timer` (wait N seconds), `bead` (wait for cross-repo issue), `human` (manual approval).

---

## 5. What We Can Steal

### 5.1. Append-Only Log as Checkpoint Format

**Beads insight**: JSONL is append-only. Creating or editing a task appends a new line — old lines are never modified. This makes the log conflict-resistant and mergeable.

**Application to hypergraph**: Our `StepRecord` chain is already append-only — each superstep appends new records, never overwrites old ones. But we store them in a normalized SQLite schema (rows in a `steps` table). Consider:

- **Export format**: An append-only JSONL representation of step history would be portable, human-readable, and diff-friendly. Useful for debugging ("show me what happened in chronological order") and for checkpoint migration between backends.
- **Conflict-free run merging**: If two forked runs need to be compared or merged, append-only logs are easier to reason about than normalized tables. Each step is self-contained.

### 5.2. Externalized State for Bounded Agents

**Beads insight** (the "divers and CNC machines" thesis): AI agents are like divers with finite oxygen tanks — context windows are just bigger tanks. The real solution is not bigger tanks but orchestrated swarms with external state management. Beads externalizes ALL task state so agents can be small, focused, and specialized.

**Application to hypergraph**: Our checkpoint/resume system already externalizes execution state. But the deeper lesson is about the *design philosophy*: checkpoint state should be self-describing enough that a "fresh" execution engine (no prior context) can load it and continue. This validates our "Restore + Continue" approach over "Replay" — a fresh engine loads state and runs, just like a fresh agent runs `bd ready` and starts working.

Concretely, this means the checkpoint should include:
- All values (data state)
- All step records with input_versions (position state)
- Routing decisions (which branch was taken)
- Graph hash (structural verification)
- Enough metadata to fully reconstruct where we are without any in-memory history

We're already heading this direction. The Beads philosophy confirms it's the right frame.

### 5.3. "Ready Work" as the Universal Scheduling Primitive

**Beads insight**: A single function (`bd ready`) answers "what can I work on now?" by evaluating the full dependency graph. This is the agent's primary interaction with the system.

**Application to hypergraph**: Our `get_ready_nodes()` is exactly this. But the Beads framing crystallizes something: **the resume problem IS the scheduling problem**. On resume, we don't need special "resume logic" — we need the normal scheduler to produce correct results given the restored state. If the state is correctly restored (values + steps + routing), `get_ready_nodes()` will naturally skip completed nodes and schedule only what's pending.

This is what we described as the "remap-to-1 trick" in the spec. The engine doesn't know it's resuming. It just sees state and finds what's ready. Beads validates this pattern — `bd ready` doesn't care whether tasks were completed in this session or a previous one. It just evaluates the current graph.

### 5.4. Source of Truth vs Read Cache Separation

**Beads insight**: JSONL (source of truth) + SQLite (read cache) = distributed sync + fast queries. The cache is derived and rebuildable.

**Application to hypergraph**: We use SQLite as both source of truth and query engine. This is fine for a single-process checkpointer. But if we ever need:
- Multiple runners reading the same checkpoint state
- Portable checkpoint format (export/import between backends)
- Debugging tools that need human-readable checkpoint history

...the separation becomes valuable. The JSONL-as-export-format idea (5.1) is a lightweight version of this: keep SQLite as the primary store, but support JSONL export for portability and debugging.

### 5.5. Hash-Based IDs for Distributed Workflow Creation

**Beads insight**: Sequential IDs collide in distributed environments. Hash-based IDs enable collision-free creation without coordination.

**Application to hypergraph**: Our `workflow_id` is user-provided (explicit). Our `run_id` is a UUID (already collision-free). But for automatically generated child run IDs in `map()`, we use a pattern like `{parent_id}/{index}`. This is deterministic and avoids collisions because the parent coordinates.

The Beads pattern would be more relevant if we supported:
- Multiple independent processes creating sub-workflows under the same parent
- Distributed `map_over` where items are processed by different machines
- Cross-process checkpoint writes

Not needed now, but the progressive hash-length scaling is an elegant solution worth remembering.

### 5.6. Gates as Coordination Primitives

**Beads insight**: Gates block dependent work until an external condition is met. Types: PR merge, CI completion, timer, manual approval. They bridge internal state and external systems.

**Application to hypergraph**: We already have `InterruptNode` (human-in-the-loop) and gate nodes (`IfElseNode`, `RouteNode`). Beads' gates are a broader concept — they're async coordination points that can block on *any* external system.

For checkpoint/resume, the relevant idea is: **gates are natural checkpoint boundaries**. When a workflow hits a gate, it should:
1. Checkpoint all current state
2. Pause (status = ACTIVE, waiting on gate)
3. Resume when the gate condition is met

Our `InterruptNode` already does this (checkpoint before interrupting, resume with new input). Beads confirms this is the right pattern and suggests we could generalize it: any node that blocks on an external condition is a checkpoint+suspend point.

### 5.7. "Land the Plane" = Structured Session Closure

**Beads insight**: At end of session, the agent: updates issues with progress, syncs to Git, cleans up, and generates a ready-to-paste prompt for the next session. The output is a structured handoff, not a raw state dump.

**Application to hypergraph**: When a workflow completes (or is interrupted), the checkpoint should include enough context for a clean handoff:
- What completed successfully
- What failed (and why, if available)
- What's still pending
- What the "next step" would be on resume

We store step records with status (COMPLETED/FAILED/SKIPPED), which gives us the first three. The fourth is implicit in the graph structure + checkpoint state (just call `get_ready_nodes()`). But explicitly generating a "resume plan" as part of the checkpoint metadata could be useful for debugging and observability.

### 5.8. Semantic Memory Decay (Compaction)

**Beads insight**: Old completed tasks get summarized by an LLM to free context space. Detailed implementation notes become one-paragraph summaries. Like human memory consolidation.

**Application to hypergraph**: For checkpoint history, this suggests a compaction strategy:
- Recent runs: keep full step-by-step detail
- Older runs: keep only final values + summary statistics (node count, duration, error info)
- Very old runs: keep only metadata (workflow_id, status, timestamps)

This is relevant for `CheckpointPolicy` (retention/TTL) but goes further — instead of binary keep/delete, compress old history into summaries. Not a priority for MVP but an elegant long-term approach.

---

## 6. Key Design Differences

| Dimension | Beads | Hypergraph |
|-----------|-------|------------|
| **Unit of work** | Issue (human/agent task) | Node execution (function call) |
| **Granularity** | Task-level (create/close) | Step-level (input versions, output values) |
| **DAG semantics** | Dependency ordering (what can start) | Data flow (what values are available) |
| **Scheduling** | `bd ready` (topological sort on status) | `get_ready_nodes()` (data availability + staleness) |
| **Resume** | Load task statuses, find ready work | Load values + steps, let engine schedule |
| **Branching** | Git branches (code + issues branch together) | Run forks (new workflow_id from checkpoint) |
| **State location** | Externalized (Dolt/JSONL/SQLite) | Externalized (checkpointer) + in-memory (GraphState) |
| **Merge** | Git three-way merge / Dolt cell-level merge | Value merge (runtime wins) + staleness detection |

The core insight is the same: **position and data must both be persisted for correct resume**. Beads stores task statuses (position) and task details (data). Hypergraph stores step records with input_versions (position) and output values (data).

---

## 7. Conceptual Framework: Beads' Approach vs Hypergraph's

### Beads: Status-Based Scheduling

```
For each task:
  if task.status == "open" and all blockers are "closed":
    → READY (agent can work on it)
```

Simple. Status is the only position signal. Dependencies are the only constraint.

### Hypergraph: Version-Based Scheduling

```
For each node:
  if all inputs available:
    if never executed:
      → READY
    elif input_versions changed since last execution:
      → STALE → READY (re-execute)
    else:
      → SKIP (already done with current inputs)
```

More granular. Version-based staleness detection handles cascading re-execution, which Beads doesn't need (tasks are independent units, not data-flow transformations).

### The Common Principle

Both systems answer the same question: "Given the current state, what should execute next?"

Both derive the answer from persistent state, not in-memory history. The scheduling algorithm is deterministic — given the same state, it always produces the same answer. This is what makes resume work: restore state → run scheduler → get correct next steps.

---

## 8. Gas Town: Multi-Agent Orchestration Layer

Yegge's [Gas Town](https://github.com/steveyegge/gastown) builds on Beads to orchestrate fleets of parallel agents. Relevant design ideas:

- **Operational roles, not personas**: Mayor (orchestrator), Polecat (worker), Witness (monitor), Deacon (merger), Refinery (integration). Each is a bounded, focused role.
- **State lives externally**: Agents don't maintain state in context windows. All task state is in Beads. This allows unlimited horizontal scaling.
- **Git worktrees for isolation**: Each Polecat gets its own worktree — an isolated working directory sharing the same .git history. Worktrees are cheap (symlinks to .git) and mergeable.

**Application to hypergraph**: The Gas Town architecture suggests that for distributed/parallel execution, the checkpoint store should be the coordination mechanism. Multiple runners reading from the same checkpointer could implement fan-out/fan-in patterns where the checkpointer is the shared state, not message passing.

Not relevant to current scope, but validates the design direction of externalizing all state to the checkpointer.

---

## 9. Connections to Existing Research

### vs. bead-project/bead (our other research)

| | bead-project/bead | steveyegge/beads |
|---|---|---|
| Domain | Data pipeline provenance | AI agent workflow coordination |
| Core unit | Immutable archive (code + data + metadata) | Mutable issue with status lifecycle |
| Versioning | Append-only (new archive per version) | Dolt commits or append-only JSONL |
| Resume | N/A (each archive is complete) | Load status, find ready work |
| Forking | New bead with same inputs, different code | Git branch (code + issues branch together) |

Both reinforce: immutability for completed state, append-only for history, explicit dependencies over inferred ones.

### vs. Dolt (our other research)

Beads literally uses Dolt as its backend. The Dolt research in `beads-dolt-inspiration.md` covers the storage/versioning primitives. This document covers the *workflow-level* ideas that Beads builds on top of Dolt.

### vs. our checkpoint/resume gap analysis

The gap analysis in `spec-vs-implementation.md` identifies "step history as implicit cursor" as the critical gap. Beads validates this indirectly: `bd ready` (their scheduler) needs task *status* (their position signal) to be correct. Without it, the scheduler returns wrong results. Same for us — without step history, `get_ready_nodes()` returns wrong results on resume.

---

## 10. Summary: The Steal List

**Confirmed directions** (things we're already doing that Beads validates):
1. Restore + Continue over Replay — Beads' agents don't replay history, they load state and find ready work
2. Externalized state as single source of truth — context windows are finite, state must live outside
3. Deterministic scheduling from persistent state — `bd ready` / `get_ready_nodes()` are the same pattern
4. Append-only step history — never overwrite, always append

**New ideas worth adopting**:
5. **JSONL export for checkpoint portability** — append-only, human-readable, diff-friendly representation of step history
6. **Structured resume metadata** — "land the plane" pattern: include a summary of what completed, what's pending, and what's next in checkpoint metadata
7. **Checkpoint compaction** — semantic compression of old run history (full detail → summary → metadata only)
8. **Gates as natural checkpoint boundaries** — generalize InterruptNode's checkpoint-before-suspend pattern to any blocking operation

**Longer-term inspiration**:
9. Source-of-truth / read-cache separation (if we need multi-process checkpointing)
10. Hash-based IDs for distributed sub-workflow creation (if we need distributed map)
11. Cell-level merge semantics (if we need to merge divergent forked runs)
