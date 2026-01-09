# Durability (Consolidated): Steps, Serialization, Artifacts, and GraphNode Boundaries

This document consolidates the reviewed durability specs into one coherent design:
- `specs/deprecated/durability-boundaries.md`
- `specs/deprecated/serialization-and-durability.md`
- `specs/deprecated/state-durability-matrix.md`

If you only read one doc about durability, read this one.

Related reading:
- `specs/reviewed/state-model.md` (“outputs ARE state”)
- `specs/reviewed/execution-types.md` (`RunResult`, `GraphState`, events)
- `specs/reviewed/checkpointer.md` (`Checkpointer`, `StepRecord`, serializer hooks)
- `specs/reviewed/durable-execution.md` (durable runs, stop/interrupt semantics)
- `specs/reviewed/observability.md` (events are not persistence)
- `specs/references/serialization.md` (LangGraph/Temporal/DBOS/Mastra/Inngest survey)

---

## 1) The core invariant (the thing we must not break)

> If a step can be skipped on resume, then the step must have **durable outputs** (inline or referenced).

Equivalently:
- **No persisted outputs ⇒ the step is not skippable** (it must rerun to recreate missing values).

This invariant is what makes the following features correct:
- resume correctness (downstream inputs exist),
- forking from checkpoints,
- time-travel/snapshot inspection,
- and “exactly-once at the step boundary”.

It’s also why “just persist fewer outputs” caused hard-to-explain bugs around:
exactly-once, non-determinism, resume, and time-travel.

---

## 2) The layers (what changes durability vs what doesn’t)

Think of hypergraph as four layers, with a fifth “missing piece” proposed for production.

### A. Graph layer (structure)

**What it is:** `Graph` + node types (`FunctionNode`, `GraphNode`, gates, interrupts).

**What it decides:**
- names, inputs/outputs, edges (dataflow),
- what can connect to what,
- build-time validation.

**What it does NOT do:** run anything; persist anything; emit anything.

### B. Execution layer (runtime)

**What it is:** runners + `GraphState` + scheduling + value resolution.

**What it decides (at runtime):**
- which nodes execute, in what order,
- “staleness” via `input_versions`,
- how stop/interrupt behaves during execution.

**Internal storage:** `GraphState.values` holds everything produced so far during a run (even if it won’t be returned).

### C. Observability layer (events)

**What it is:** event stream (`NodeStartEvent`, `StreamingChunkEvent`, …) + processors + `.iter()`.

**What it’s for:** UI, logs, tracing, metrics, debugging.

**Crucial separation:** events are *not* the source of truth for resume/fork/time-travel.

### D. Persistence layer (steps + checkpointer)

**What it is:** `Checkpointer` writes `StepRecord`s; state is computed by folding step values.

**What it’s for:** resume/fork/time-travel correctness and “skip completed steps”.

### E. Proposed missing piece: artifact storage

We currently have “serialize values into StepRecord”.

Production systems add a tier:
- store small values inline,
- store large/non-JSON values as **artifacts** (blob/object-store),
- store only an `ArtifactRef` in the StepRecord.

This keeps “persist everything by default” viable without “store huge blobs in your DB”.

---

## 3) The contradiction: “persist everything” is right and also a trap

### Why “persist everything” is right

In hypergraph, “outputs ARE state”. If we treat completed steps as skippable, then on resume we must still be able to resolve their outputs for downstream nodes. Persisting outputs is the general solution.

### Why “persist everything” is a trap (naively)

Persisting everything *without tiers and guardrails* causes:
- **storage explosion** (embeddings, dataframes, images),
- **serialization failures** (arbitrary class instances, file handles, generators),
- **security risks** (pickle-style RCE if storage is compromised),
- **version brittleness** (deserialization breaks across refactors).

The refinement is:

> Persist everything, but not all outputs are stored the same way.

---

## 4) The durability matrix (exactly-once × determinism × output weight)

Three dimensions capture most of the real-world tradeoffs:

### Dimension 1: Skip on resume? (exactly-once vs rerunnable)

- **Skip on resume (exactly-once):** once recorded as done, the node body must not re-run on resume.
  - Examples: send email, charge card, call external API you don’t want duplicated, generate UUID that controls downstream logic.
- **Rerunnable:** safe to re-run on resume (idempotent and/or deterministic and/or explicitly “fresh-on-resume”).
  - Examples: parse JSON, charge card request (with idempotent key that doesn't actually charge the card if it already has been charged, but returns the same verificiation result), compute hash, pure transforms.

### Dimension 2: Deterministic vs non-deterministic

- **Deterministic:** same inputs ⇒ same outputs (or differences are acceptable).
- **Non-deterministic:** re-run can change outputs (LLMs, randomness, time, external reads (DB), model drift).

### Dimension 3: Output weight (light vs heavy)

- **Light:** small enough for inline DB storage (order of ~MB or less).
- **Heavy:** too large/expensive to store inline (embeddings, dataframes, images, long transcripts).

### Hidden “4th axis”: persistable vs non-persistable outputs

Even “light” outputs can be non-persistable (arbitrary classes, resources, generators). For those:
- return a persistable representation (IDs/config/params), or
- route the heavy thing via artifacts, or
- keep it transient (only with rerunnable semantics).

### The 8-cell matrix (recommended behavior)

Persistence actions used below:
- **Persist inline:** store value in StepRecord `values` (JSON-ish).
- **Persist ref:** store `ArtifactRef` in StepRecord; store bytes elsewhere.
- **Don’t persist:** output is transient; step must rerun if downstream needs it.

| Skip on resume? | Deterministic? | Weight | Recommended behavior | Why |
|---|---|---|---|---|
| Yes | Yes | Light | Persist inline | Cheap and enables exact resume/fork/time-travel |
| Yes | Yes | Heavy | Persist ref | Still needs durable output; store bytes out-of-row |
| Yes | No | Light | Persist inline | Rerun would diverge; snapshot semantics required |
| Yes | No | Heavy | Persist ref | Snapshot semantics + size constraints |
| No | Yes | Light | Don’t persist *or* persist inline | Recompute is fine if truly derived/cheap |
| No | Yes | Heavy | Prefer persist ref (or recompute if cheap) | Heavy often implies expensive recompute |
| No | No | Light | Choose explicitly: persist (stable) vs don’t (fresh) | “Resume” may change control flow if recomputed |
| No | No | Heavy | Usually persist ref (or persist a source pointer) | Recompute is expensive and may diverge |

---

## 5) Serialization architecture (safe-by-default)

### Two-stage pipeline (serializer + codec)

```
Node Output → Serializer → bytes → Codec → bytes → Storage
              (format)            (transform)
```

- **Serializer:** converts Python values to bytes (JSON/msgpack/pickle).
- **Codec (optional):** transforms bytes (encryption, compression).
- **Storage:** persists bytes (SQLite/Postgres/object-store).

### Serializer and codec interfaces (conceptual)

```python
class Serializer(Protocol):
    def serialize(self, value: Any) -> bytes: ...
    def deserialize(self, data: bytes) -> Any: ...

class Codec(Protocol):
    def encode(self, data: bytes) -> bytes: ...
    def decode(self, data: bytes) -> bytes: ...
```

### Security posture

- Default serializer should be safe (JSON-like).
- Pickle-like serializers can exist, but must be **explicit opt-in** and treated as “dev/trusted storage only”.
- Avoid “mysterious auto-pickle fallback” when JSON fails; that’s how durable systems get surprising security bugs.

### Schema evolution

Durability implies old data will be read by new code. Prefer:
- version metadata on persisted payloads,
- graceful degradation for introspection tooling (don’t crash when a value fails to deserialize),
- migration tooling as a separate concern (out of scope for initial design, but enabled by versioned payloads).

---

## 6) Large value handling: ArtifactRef + ArtifactStore

### Why

Large values do not fit well into checkpointer DB rows (even if technically possible). A tiered model prevents DB bloat and slow restores.

### The model

- Inline values go into StepRecord.
- Values above a threshold (or explicitly marked) go to an artifact store.
- StepRecord stores a small reference:

```python
@dataclass(frozen=True)
class ArtifactRef:
    storage: str
    key: str
    size: int
    content_type: str
    checksum: str
```

This makes “persist everything” practical for heavy outputs: the persisted state remains small, while the heavy bytes live in blob storage.

---

## 7) GraphNode as a boundary (two different boundaries, two different surfaces)

Nested graphs are the clean place to express *local* durability tradeoffs without hiding logic in monolithic “service nodes”.

There are two boundaries people want:

1. **Dataflow boundary:** “outer graph depends only on these outputs”
2. **Durability boundary:** “internal steps are (or are not) persisted individually”

`GraphNode` already provides (1). We extend it to express (2).

### Wiring surface vs return surface

- **Wiring surface (lifted outputs):** the set of inner outputs that become available to other outer nodes under normal parameter names.
  - This is controlled by `GraphNode.outputs`.
  - Current reviewed default: `GraphNode.outputs = graph.outputs` (all inner outputs).
  - Use `.with_outputs()` to rename lifted output names.

- **Return surface (nested RunResult):** the nested graph can still be returned as a nested `RunResult` under `result[graphnode.name][...]` (subject to `select=` filtering).

These are intentionally different:
- wiring surface controls dependency/value-resolution in the parent graph,
- return surface controls what the caller sees in the run result.

### Practical consequence of the current default

If `GraphNode.outputs` lifts all inner outputs, then outer nodes can depend on *any* inner output name (unless restricted).

That increases risk of name collisions in the parent namespace; renaming via `.with_outputs()` is the escape hatch.

If we want a principled way to lift only a subset (for “hide heavy intermediates”), we should add an explicit API (proposed in `durability-boundaries.md` as `.lift(...)`), rather than overloading `.with_outputs()` which only renames.

---

## 8) “Atomic nested graph” (GraphNode as a durability boundary)

We introduce a durability mode on GraphNode that changes **persistence** behavior and therefore **resume** capabilities.

### Mode A: `durability="nested"` (default)

- Inner graph executes as a child workflow (`parent_id/node_name`).
- Inner nodes write their own StepRecords.
- Resume/fork/time-travel can work inside the subgraph.
- Interrupts inside the subgraph are supported (pause/resume requires durable cursor/history).

### Mode B: `durability="atomic"` (opt-in)

Atomic mode treats the nested graph like a single durable step boundary:
- no child workflow is created,
- only one StepRecord is written (for the GraphNode),
- only the lifted outputs are persisted as that step’s outputs.

#### What changes vs what stays the same

Same (conceptually):
- **Execution semantics inside the boundary:** it still runs the same inner nodes in the same order.
- **Events / observability:** it can still emit full inner-node events (same span hierarchy), so observability stays rich.

Different (necessarily):
- **Persistence / resume cursor:** there is no durable inner cursor. After a crash mid-subgraph, resume reruns the whole subgraph because there are no inner StepRecords to replay.
- **Replay semantics in events:** in nested mode you can see `replayed=True` for inner nodes; in atomic mode you can’t replay inner nodes (they’ll just run again), so those flags differ.
- **Interrupt capability:** atomic mode can’t support `InterruptNode` inside, because pausing requires a durable cursor/state to resume inside the boundary.

#### Capabilities table

| Capability | `nested` | `atomic` |
|---|---:|---:|
| Resume correctness | ✅ | ✅ (at boundary) |
| Exactly-once granularity | ✅ (per inner step) | ⚠️ (only at boundary) |
| Fork/time-travel inside subgraph | ✅ | ❌ |
| Interrupts inside | ✅ | ❌ |
| Heavy intermediates persisted | ✅ (prefer artifact refs) | ❌ (if not lifted) |
| Arbitrary/non-persistable intermediates | ✅ only if serializable | ✅ if not lifted |

#### Build-time validation (proposed)

If a GraphNode is configured `durability="atomic"` and the inner graph contains interrupts, raise a configuration error with a clear fix:
- use `durability="nested"`, or
- move the interrupt outside the atomic boundary.

---

## 9) Concrete example: “don’t persist embeddings, but keep the graph explicit”

Inner graph is explicit (not hidden in one big node):

```python
@node(output_name="embedding")
def embed(query: str) -> list[float]: ...

@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]: ...

@node(output_name="response")
def generate(docs: list[str]) -> str: ...

rag = Graph(nodes=[embed, retrieve, generate], name="rag")
```

Option 1 (preferred for production): nested durability + artifacts
- persist everything, but embeddings become an `ArtifactRef` automatically,
- time-travel/fork inside the RAG pipeline remains possible.

Option 2 (escape hatch): atomic boundary + restricted lifted outputs
- lift only `docs`/`response` to the parent graph (do not lift `embedding`),
- do not persist inner steps (no inner StepRecords),
- after a crash mid-RAG, rerun the whole RAG subgraph on resume.

---

## 10) How other systems converge on similar constraints (short)

- **Temporal:** requires deterministic workflows; non-determinism goes into Activities; persists event history and activity results.
- **DBOS:** step-boundary replay; non-determinism goes into `@step`; persists step outputs.
- **LangGraph:** pragmatic caching/checkpointing at node boundaries; multiple durability modes.
- **Inngest:** strict JSON + strict size budgets push users toward references.
- **Mastra:** workflow snapshots with best-practice idempotency.

hypergraph’s key difference is UX:
- “nodes are steps” (no need to annotate `@step`), and
- durability tradeoffs can be localized via GraphNode boundaries + artifact tiers.

---

## 11) Summary: the simple default + explicit escape hatches

Default mental model stays simple:
1. Every node is a step.
2. Step outputs become state.
3. Completed steps are skipped on resume.
4. Outputs persist inline or as artifact refs automatically.

Escape hatches stay explicit and local:
- artifact refs for heavy outputs,
- error (or explicit opt-in) for unsafe serializers,
- GraphNode `durability="atomic"` when you truly want “no inner persistence”,
- (proposed) GraphNode lifting to restrict which outputs cross the boundary for wiring.
