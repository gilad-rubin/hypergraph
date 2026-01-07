# State Durability Matrix (Exactly-Once × Determinism × Output Weight)

Note: This content is consolidated in `specs/reviewed/durability.md`. This file remains as a standalone exploration of the matrix and its implications.

This doc records the design insight behind the “serialization issue” and why **“persist everything by default”** is simultaneously:
- the easiest way to get *resume correctness + forking + time-travel* right, and
- a trap for *huge outputs, unsafe serializers, and non-persistable objects*.

It proposes a simple mental model and a matrix of combinations with the recommended persistence behavior.

Related reading:
- `specs/tmp/issues_summary.md` (serialization concerns, scalability, production hardening)
- `specs/references/serialization.md` (what other systems do)
- `specs/reviewed/state-model.md` (“outputs ARE state”)
- `specs/reviewed/checkpointer.md` (StepRecord + serializer hooks)

---

## The core contradiction (why “narrowing outputs” breaks resume)

In the current model:

- A **completed step** is treated as “safe to skip on resume”.
- Skipping is only correct if the workflow can still obtain that step’s outputs.
- With “outputs ARE state”, the only general way to obtain outputs later is: **they were persisted** (or can be recomputed safely).

So if we do:

1. run a step,
2. mark it completed,
3. but *do not persist* its output,

then on resume the runner will skip the step and downstream nodes will see missing inputs.

This is the root reason “narrowing persisted outputs” caused issues for:
- exactly-once scenarios (skip semantics),
- resume correctness (missing prerequisites),
- forking/time-travel (can’t reconstruct historical state without recomputation),
- and anything non-deterministic (recomputation diverges).

---

## Definitions (ELI20, but precise)

### “Exactly-once” (in this doc)

Means: **the node body is not re-executed on resume** once its step is recorded as completed.

This is stronger than “idempotent” and is the default mental model users adopt in durable systems:
> “If the step is recorded as done, it won’t run again.”

### Deterministic vs non-deterministic

- **Deterministic:** rerunning the step with the same inputs produces the same outputs (or differences are acceptable).
- **Non-deterministic:** rerunning can change outputs (LLM calls, network reads, time, randomness, model drift, etc.).

### Light vs heavy outputs

- **Light:** small enough to store inline in the checkpointer DB (JSON-ish).
- **Heavy:** too large/expensive to store inline (embeddings, dataframes, images, big transcripts, etc.).

### Persistable vs non-persistable outputs (the “hidden 4th axis”)

Even “light” outputs may be **non-persistable**:
- arbitrary Python class instances,
- live resources (DB connections, file handles),
- closures, generators,
- objects whose meaning is “in-process only”.

If an output is not persistable, you have only two real options:
1. change the output shape to a persistable representation (IDs, params, handles, refs), or
2. treat it as transient (not durable) and accept rerun requirements and/or weaker guarantees.

---

## The invariant that keeps everything consistent

> **If a step can be skipped on resume, then the step must have durable outputs (inline or referenced).**

Equivalently:

- **No persisted outputs ⇒ the step is not skippable** (it must rerun to recreate the missing values).

This invariant is what keeps:
- resume correctness,
- forking from checkpoints,
- and time-travel “view historical state”

from silently breaking.

---

## The 8-cell matrix (2×2×2) and what to do

Persistence actions used below:

- **Persist inline:** store the output value in StepRecord `values` (JSON-ish)
- **Persist ref:** store a small `ArtifactRef`/`BlobRef` in StepRecord, and store the large bytes elsewhere
- **Don’t persist:** output is not durable; step must rerun on resume if downstream needs it
- **Event (not state):** output is emitted for UX/observability, but not used for value resolution/state

### Table (each row is one combination)

| Skip on resume? | Deterministic? | Output weight | Recommended behavior | Why |
|---|---|---|---|---|
| **Yes** (exactly-once) | Yes | Light | **Persist inline** | Cheap and makes resume/fork/time-travel exact |
| **Yes** (exactly-once) | Yes | Heavy | **Persist ref** | Still needs durable output; store bytes out-of-row |
| **Yes** (exactly-once) | No | Light | **Persist inline** | Rerun would diverge; snapshot semantics required |
| **Yes** (exactly-once) | No | Heavy | **Persist ref** | Snapshot semantics + size constraints |
| **No** (rerunnable) | Yes | Light | **Don’t persist** *or* persist inline | If truly derived/cheap, recompute is fine |
| **No** (rerunnable) | Yes | Heavy | Prefer **Persist ref** (or recompute if cheap) | Heavy usually implies expensive recompute |
| **No** (rerunnable) | No | Light | Choose explicitly: **persist** (stable) vs **don’t** (fresh) | “Resume” may change behavior if recomputed |
| **No** (rerunnable) | No | Heavy | Usually **Persist ref** or persist “source pointer” | Recompute is expensive and may diverge |

### What this means in practice

1. **Stable durability features (resume correctness + fork + time-travel)** require snapshot semantics for anything that influences downstream decisions.
2. “Don’t persist” is only safe when either:
   - the step is deterministic and cheap to rerun, or
   - you are explicitly choosing “fresh on resume” semantics (and documenting that this can change control flow).

---

## Why “persist everything by default” still needs tiers

Persist-everything is the simplest way to ensure:
- resume correctness (“completed means we can skip”),
- fork correctness (“forked state is exactly what happened”),
- time-travel correctness (“state at superstep N is what existed then”),
- and non-determinism correctness (no recompute drift).

But without tiers it becomes problematic:

1. **Heavy outputs** blow up DB sizes and restore times.
2. **Serialization safety** becomes unclear if users reach for pickle.
3. **Non-persistable objects** will either fail serialization or (worse) persist meaningless/unsafe state.

So the refinement is not “stop persisting” — it’s:

> **Persist everything, but not all outputs are stored the same way.**

### Proposed tiers (state vs artifact vs event)

1. **State (inline):** small JSON-ish values persisted directly.
2. **Artifact (ref):** large/non-JSON values stored out-of-row; state contains a `BlobRef` (URI/key, hash, size, mime/type, serializer id).
3. **Event:** streaming/UI/debug data; persisted separately (event log) or not at all; not part of value resolution.
4. **Transient (advanced):** in-process only; never persisted; only valid if the step is rerunnable under your chosen semantics.

This mirrors what works in other systems:
- Temporal: payloads + codec + “store big data elsewhere, pass references”
- LangGraph: inline values + checkpoint blob table
- Inngest: strict JSON + strict size budgets (forces refs)

---

## Non-persistable outputs (arbitrary classes) — what to do

If a node returns an arbitrary class instance, persisting it is often wrong:
- it may embed live resources,
- it may not be stable across versions,
- and it may not have a meaningful “serialized form”.

The clean durable patterns are:

### Pattern A: Return a persistable handle (recommended)

Instead of returning a live object, return a small description that can recreate it:

- config dict
- IDs
- file paths
- connection DSN (not a live connection)

This keeps graph code pure and durable.

### Pattern B: Return an artifact (runner stores bytes, state stores ref)

If you want to preserve the “thing” (e.g., a dataframe), return something the runner can capture:
- bytes + metadata, or
- a standardized wrapper type (conceptually: `Artifact(value)`), which the runner writes to an artifact store and replaces with `ArtifactRef` in persisted state.

### Pattern C: Mark as transient (only when rerunnable semantics allow it)

If the value is inherently in-process (like a DB connection), it must not be state.

But then any downstream computation that depends on it must either:
- be within the same step (so it never crosses a durable boundary), or
- accept rerun (and rebuild the connection), which implies the step is rerunnable.

---

## Time-travel and fork semantics (why non-determinism forces persistence)

There are two fundamentally different “time-travel” stories:

1. **Snapshot time-travel (strong):**
   - “Show me the state as it was at step N.”
   - Requires persisted outputs for all steps up to N.
   - Works with non-determinism.

2. **Replay time-travel (weak):**
   - “Re-run until step N and show me the result.”
   - Only valid for deterministic steps (or if you accept divergence).
   - Breaks the moment you have non-determinism or external reads.

Forking inherits the same distinction:
- **Fork from snapshot:** reliable and reproducible.
- **Fork by replay:** only safe for deterministic pipelines.

This is why “persist everything by default” felt necessary: it gives snapshot semantics automatically.

---

## A simple user-facing interface (without exposing the whole matrix)

Goal: keep the *default* mental model:
> “Completed steps don’t rerun; the workflow resumes exactly where it left off.”

But add two high-value escape hatches that don’t break invariants:

### 1) Output storage tier: `auto | inline | artifact | event | transient`

- Default: `auto`
  - try inline JSON
  - if too large → artifact ref
  - if non-serializable → error that tells user to choose `transient` or change output type

### 2) Resume semantics per step: `snapshot | recompute`

- Default: `snapshot` (exactly-once; skippable; requires durable outputs)
- `recompute` opts into rerun-on-resume semantics and accepts that time-travel/fork may be replay-based for that step
