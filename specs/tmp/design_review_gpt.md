# hypergraph design review (GPT) — flaws, missing pieces, and production risks

Date: 2026-01-07  
Scope: `specs/reviewed/*.md` (plus `specs/references/*` for external inspiration)

This is an adversarial design review: the goal is to spot what will break (or confuse) in persistence, reliability, production usage, and developer experience — while the design is still cheap to change.

---

## Executive summary (the big problems to fix first)

1. **The specs disagree on core APIs and semantics** (forking inputs, `iter()` shape, default outputs, stop/partial behavior, ordering). If this ships as-is, you’ll get inconsistent implementations and lots of “but docs said…” support burden.
2. **Control-flow decisions are not clearly durable** (route/branch choices). Without persisting routing decisions, crash recovery and time-travel are not correct for graphs with gates.
3. **Streaming semantics are under-specified and currently contradictory** (what yields mean, how to combine “progress” with output, what gets persisted on stop/crash). This will be a major source of subtle bugs.
4. **“Persist everything” is correct for resume, but unsafe without tiers** (artifact store + size budgets + safe serialization + schema evolution). The durability docs propose this, but it’s not integrated across reviewed specs.
5. **Multi-process production constraints are missing** (workflow run locking/leases, nested-graph atomicity windows, partial writes, retention/compaction). You can’t claim “production-grade” without these.

---

## Spec mismatches / contradictions to resolve (high priority)

These are not “nice to have” cleanups — they change what the runtime must do.

### 1) Forking/resume API is inconsistent: `history=` vs `checkpoint=` vs “just values”

- In `specs/reviewed/persistence.md`, forking uses `history=checkpoint.steps` and merges `checkpoint.values` into `values`.
- In `specs/reviewed/execution-types.md`, forking uses `checkpoint=checkpoint` parameter (and shows additional behavior combos).
- In `specs/reviewed/runners-api-reference.md`, `AsyncRunner.run()` has **no** `history` or `checkpoint` parameter.

Why this matters (concrete failure):
```python
# Branch A and Branch B both produce "processed"
@node(output_name="processed")  # branch A
def process_a(x: str) -> str: ...
@node(output_name="processed")  # branch B
def process_b(x: str) -> str: ...

@node(output_name="final")
def finalize(processed: str) -> str: ...
```
If you “fork” with **values only** (no step history) and `processed` is present, both downstream nodes that depend on `processed` may appear runnable unless the engine also knows which branch it is “in”. Your own `execution-types.md` argues that **step history is the implicit cursor** — so forking must carry it (or a durable equivalent).

Recommendation:
- Pick **one canonical fork API** and make everything else sugar:
  - Either `checkpoint=Checkpoint(values, steps)` (clean)  
  - Or `values=..., history=...` (more flexible but easier to misuse)
- Then update *all* docs and runner signatures to match.

### 2) `AsyncRunner.iter()` is shown in two incompatible shapes

Examples in reviewed specs use both:
- `async for event in runner.iter(...)` (iterator)
- `async with runner.iter(...) as run: async for event in run: ...` (context manager + handle)

This is a DX footgun unless the returned object intentionally supports both patterns.

Recommendation:
- Decide on one: either “`iter()` returns a `RunHandle` that is both async-iterable and an async context manager” **or** “`iter()` returns only an async context manager”.
- Then make the type signature and examples consistent everywhere (`runners.md`, `execution-types.md`, `durable-execution.md`, `observability.md`, `runners-api-reference.md`).

### 3) Default outputs returned are inconsistent (all outputs vs leaf outputs)

- `specs/reviewed/execution-types.md` and `specs/reviewed/state-model.md` imply “all outputs unless `select=` filters”.
- `specs/reviewed/runners-api-reference.md` says `SyncRunner.run(select=None)` returns **leaf outputs** by default.

This affects:
- result size and privacy (returning intermediates like embeddings by default),
- nested graphs (what shows up at top-level),
- ergonomics (do users see what they expect?).

Recommendation:
- Make the default explicit and consistent across runners:
  - If you want “debuggability by default”: return **all** outputs.
  - If you want “safe/typical DAG behavior”: return **leaf outputs**.
- Then define how nested `RunResult` behaves under `select` (does selecting `rag/*` include `rag` key itself?).

### 4) Stop + partial output semantics contradict each other

- `specs/reviewed/execution-types.md` defines: partial output is `StepStatus.COMPLETED` with `partial=True`; `StepStatus.STOPPED` means no usable output.
- `specs/reviewed/durable-execution.md` claims a streaming node stopped mid-stream “saves with `StepStatus.STOPPED`” but then downstream nodes run using the partial output.

These can’t both be true.

Recommendation:
- Define a single rule:
  - If partial output is usable downstream, then the step must be persisted as **COMPLETED + partial=True + values present**.
  - Use `STOPPED` only when there is **no** usable output.

### 5) Ordering is claimed deterministic, but uses unordered containers

`specs/reviewed/graph.md` claims deterministic order for `frozenset` properties. In Python, `set`/`frozenset` iteration order is not stable across processes (hash randomization).

Recommendation:
- Anything that needs deterministic iteration should be a `tuple` (or at least explicitly sorted).
- Use sets only for membership, not for ordering guarantees.

### 6) Step indexing rule disagrees with “node list order”

- `specs/reviewed/execution-types.md` assigns `StepRecord.index` “alphabetically by node_name within superstep”.
- `specs/reviewed/graph.md` says order follows “node list order passed to Graph”.

Recommendation:
- Choose one canonical ordering rule (ideally: **Graph construction order** to match user expectations) and use it for:
  - step index assignment,
  - output ordering,
  - deterministic hashing (where relevant).

---

## Persistence & durability: likely failures in production

### 1) Routing decisions must be durable (not just events)

Right now:
- Gates (`RouteNode`, `BranchNode`, `TypeRouteNode`) have `outputs=()` by design.
- A `RouteDecisionEvent` exists, but events are explicitly non-durable.

Why this breaks crash recovery:
1. Route gate runs and decides `target="A"`.
2. The process crashes **after** saving the gate step but **before** running `A`.
3. On resume, if you re-run the route function (or if it’s nondeterministic), it might now pick `B`.

This violates “resume the workflow” semantics and makes time travel non-reproducible.

Recommendation (pick one):
- Persist gate decision in `StepRecord.values` under a reserved key (e.g., `{"__route__": "A"}`), **or**
- Add an explicit `decision` field on `StepRecord` for gate nodes (cleaner), **or**
- Require gate functions to be deterministic *pure functions of durable state* (hard to guarantee in practice).

### 2) Interrupt persistence model is not fully specified

You persist `PauseInfo` (good), but the spec doesn’t clearly answer:
- When a response arrives, do you **update** the paused `StepRecord` or **append** a new one?
- If “append-only history” is a hard rule, how does the engine avoid re-pausing on resume?
- How does this interact with the `unique constraint on (workflow_id, superstep, node_name)` mentioned in `checkpointer.md`?

Concrete operational requirement:
- After a restart, an external UI must be able to query:
  - “What is this workflow waiting for?”
  - “Has the response been provided?”
  - “Did we already continue past the interrupt?”

Recommendation:
- Specify the exact persistence transitions for interrupts, e.g.:
  - Step saved as `PAUSED` with `pause=...` (no values).
  - On response: either
    - **Upsert** same step (status `COMPLETED`, values include response), or
    - Append a new step “`interrupt_response`” that writes the response output and unblocks downstream.
- Then make it consistent with the “append-only” story.

### 3) “Persist everything” needs an artifact tier + size budgets (already proposed, not integrated)

`specs/reviewed/durability.md` correctly calls out the missing piece:
- Large outputs (embeddings, dataframes, images, long transcripts) should not live inline in DB rows.

Without this, production failures look like:
- DB row limits exceeded,
- slow resume due to huge state loads,
- expensive storage bills,
- brittle JSON serialization failures.

Recommendation (minimum viable):
- Add `ArtifactStore` + `ArtifactRef` as a first-class concept (inline vs ref).
- Add hard budgets (warn/error thresholds) similar to Temporal/Inngest:
  - “max inline bytes per step”
  - “max total workflow state bytes”
- Add a codec layer (compression/encryption) as in Temporal (`payload -> codec -> storage`).

### 4) Serialization security posture is not safe-by-default yet

The reviewed specs show JSON default and allow pickle-style serializer examples.
The production reality:
- Deserializing pickle from attacker-controlled storage can be RCE.
- Even “trusted” DBs become untrusted when backups/leaks exist.

Recommendation:
- Safe default serializer (JSON/MsgPack with explicit adapters).
- Pickle allowed only behind explicit “trusted storage only” flag.
- Add “best-effort introspection” behavior: listing workflows should not crash if a value fails to deserialize (DBOS/Mastra pattern).
- Attach version metadata to payloads for evolution.

### 5) Nested graphs have an atomicity window that must be handled

Current model:
- A `GraphNode` creates a child workflow (`parent/child` id).
- Parent step is saved **after** child completes.

Crash window:
1. Child workflow completes and is persisted.
2. Crash before parent `GraphNode` step is persisted.
3. On resume: parent sees “GraphNode step missing” — what happens?

If the runtime naively “re-runs the GraphNode”, it may:
- re-run the child (duplicating work/side effects), or
- create inconsistent parent/child linkage.

Recommendation:
- Make the child id deterministic (it is) **and** specify idempotent behavior:
  - If child workflow exists and is completed, treat GraphNode as “replayed” and only persist the parent step.
- Consider making parent step persistence transactional with child completion where possible (hard across two workflows unless same DB transaction).

### 6) Workflow-level concurrency control is underspecified

`persistence.md` says “one active execution per workflow_id”, but nothing defines:
- how to enforce that across multiple worker processes/servers,
- what happens to stuck “running” attempts when a worker dies,
- how to implement “leases” without storing RUNNING in step history.

Recommendation:
- Add a **workflow lock/lease** mechanism in the persistence layer:
  - Acquire lease when starting a run.
  - Renew lease heartbeat.
  - Release on completion/pause/failure.
  - Allow take-over after lease expires.
- Keep it separate from step history (step history stays minimal/correctness-focused).

---

## Streaming & progress: the design is not crisp enough yet

### 1) What does `yield` mean for node outputs?

Reviewed docs imply:
- “final value is concatenation of all chunks”
- “yield progress objects, yield results last”

But in Python:
- async generators cannot `return value`; the *only* produced values are yields.
So “yield progress + yield result” means the output stream contains both progress and results, unless the runtime has special filtering rules.

Recommendation:
- Define a strict streaming contract:
  - Option A: yields are **always** output chunks; final output is list/concat of chunks; progress must be emitted via a separate API (`stream_writer`).
  - Option B: yields can be “events” of multiple kinds (`Progress`, `Chunk`, `Final`), and only `Final` is persisted as output.
- If you keep “single unified event stream”, you still need a “custom event writer” like LangGraph for progress that should not pollute outputs.

### 2) Durable streaming needs “pending writes” (or you lose work)

For long streaming nodes, crashes are common. Today’s model (“only save at step end”) means:
- crash mid-stream ⇒ step re-runs ⇒ you may duplicate output and/or diverge (LLM nondeterminism).

LangGraph addresses this with “pending writes” tables.

Recommendation:
- Add an optional “pending writes” channel for streaming nodes:
  - periodically persist partial chunks (or last committed chunk index),
  - resume can either replay chunks or continue from last safe point.

---

## Developer experience: likely confusion points

### 1) Implicit wiring by name is powerful, but easy to get wrong

Accidental coupling example:
- A node outputs `config`, another node takes `config` — you get an edge even if that was not intended.

Recommendation:
- Provide tooling that explains inferred edges:
  - “why is node B receiving `config`?”
  - show producer/consumer mapping
  - suggest renames (`with_inputs`/`with_outputs`) to disambiguate
- Consider an “explicit edges mode” for advanced users (pydantic-graph style) without abandoning the default.

### 2) GraphNode “lifts all inner outputs” by default (collision + bloat risk)

This is convenient but makes it too easy to:
- depend on inner intermediates accidentally,
- collide names in the parent namespace,
- leak heavy intermediates into parent state/persistence.

`specs/reviewed/durability.md` proposes a `.lift(...)`-like concept and `durability="atomic"` mode, but these are not present in `graph.md` / `node-types.md`.

Recommendation:
- Add a first-class “lift only these outputs” API (separate from renaming).
- Decide whether GraphNode supports `durability="atomic"` and document constraints (e.g., no interrupts inside).

### 3) Map/batch lacks an in-graph join story

You have:
- `runner.map()` (batch outside the graph)
- `GraphNode.map_over()` (batch a nested graph, but limited and not described end-to-end)

What’s missing is the “collect/join/reduce” primitive for parallel branches and map fan-out.

Inspiration: pydantic-graph’s join nodes + reducers (collect parallel results deterministically).

Recommendation:
- Add an explicit `JoinNode`/reducer concept (even if you keep “no reducers” for ordinary outputs).
- Or define a standard pattern for “map then aggregate” that doesn’t require users to leave the graph model.

### 4) Separators (`.` vs `/`) are clever but still confusing in practice

You currently use:
- `.` for nested `values` keys (`"rag.query"`)
- `/` for nested paths (`select=["rag/**"]`, `pause.node_name="review/approval"`)

Recommendation:
- Add a “single helper” API for users so they rarely construct these manually:
  - `hg.path("review", "approval") -> "review/approval"`
  - `hg.key("review", "decision") -> "review.decision"`
  - `PauseInfo.response_key` is a good start — extend the idea.

---

## What’s missing (zoomed-out checklist)

### Production blockers
- Durable routing decisions (gates) and interrupt state transitions fully specified.
- Artifact store + size budgets + safe serialization posture + schema evolution metadata.
- Workflow concurrency control (distributed lock/lease).
- Streaming durability story (pending writes or clear “at-least-once streaming” semantics).
- Nested graph atomicity/idempotency explicitly defined.

### Big DX multipliers
- One coherent runner API surface (forking, iter, select defaults, naming).
- Tooling: “explain graph wiring”, “why did this node run?”, “what’s next?”.
- Time-travel ergonomics: list checkpoints/supersteps, compute next nodes, show failed tasks (LangGraph’s `StateSnapshot.next`/`tasks` is a good reference).

### Optional (but likely needed later)
- Per-node concurrency groups / rate limiting (separate from global `max_concurrency`).
- Built-in retry policy (or a documented integration contract) so retries show up in step metadata/observability consistently.
- Cross-workflow memory store interface (even if the default is “bring your own DB”).
- Environment/version capture beyond `definition_hash` (dependency versions, Python version, config hash).

---

## Borrowed ideas worth copying (with concrete mapping to hypergraph)

### LangGraph
- **Streaming modes** (`values`/`updates`/`messages`/`custom`/`debug`): your unified event stream can support mode filters to reduce noise.
- **Custom stream writer**: tools can emit progress without polluting outputs.
- **Pending writes**: makes streaming + durability sane.
- **Blob tier** for large values (`checkpoint_blobs`): matches your proposed artifact store.

### Temporal
- **Payload codec** layer for compression/encryption without changing business logic.
- **Hard size limits** (prevents “it worked locally” failures in prod).
- **Determinism discipline**: if you don’t persist control decisions, you must enforce determinism (hard for LLM workflows).

### Mastra / DBOS
- **Best-effort decode** for introspection paths (don’t brick your dashboard because one value won’t deserialize).
- **Idempotency-first guidance** for external side effects.

### Pydantic Graph (pydantic-graph beta)
- **Joins + reducers** for parallel/map aggregation.
- **Explicit decision nodes** with labels (good for diagramming and explainability).
- **Map over async iterables** (progressive fan-out) — aligns with streaming workloads.

---

## A short “if we do nothing, what will break?” story (ELI20)

Imagine a production workflow:
1. It routes based on an LLM (“route to A or B”).
2. It streams a long answer.
3. It pauses for human approval.
4. It nests a subgraph for a RAG pipeline.

With the current spec gaps:
- A crash after a route decision can take a different branch on resume.
- A crash mid-stream loses all progress; re-run may produce different text.
- A pause may not have a well-defined durable transition to “response received”.
- A crash between nested child completion and parent step write can duplicate work or produce inconsistent state.
- Large embeddings get shoved into DB rows and eventually blow up storage or timeouts.

None of these are theoretical — they’re the common failure modes in real workflow frameworks, and the reviewed docs already hint at the fixes. The work now is mostly about making the semantics consistent, explicit, and implementable.

