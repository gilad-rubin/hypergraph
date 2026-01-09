# Durability Layers and GraphNode Boundaries

Note: This content is consolidated in `specs/reviewed/durability.md`. This file remains as a focused deep-dive on GraphNode boundaries.

This doc “connects the dots” across hypergraph’s layers (events, steps, persistence, results) and refines a key design direction:

> Use **nested graphs (GraphNode)** as an explicit boundary where we can control **what becomes durable state** and **at what granularity**.

This gives users a principled escape hatch for:
- heavy intermediates (embeddings, large arrays, dataframes),
- arbitrary/non-persistable objects,
- and non-deterministic sub-steps,

without encouraging “just hide it in a monolithic function node”.

Related reading:
- `specs/reviewed/state-model.md` (“outputs ARE state”)
- `specs/reviewed/checkpointer.md` (StepRecord, serializer)
- `specs/reviewed/durable-execution.md` (nested workflows, stop semantics)
- `specs/reviewed/observability.md` (events are not persistence)
- `specs/deprecated/state-durability-matrix.md` (exactly-once × determinism × output weight)
- `specs/references/serialization.md` (LangGraph/Temporal/DBOS/Mastra/Inngest patterns)

---

## 1) The layers we already have (and what each one is for)

Think of hypergraph as four layers with a fifth “missing piece” for production:

### A. Graph layer (structure)

**What it is:** `Graph` + node types (`FunctionNode`, `GraphNode`, gates, interrupts).

**What it decides:**
- names, inputs/outputs, edges (dataflow)
- what can connect to what
- build-time validation

**What it does NOT do:** run anything; persist anything; emit anything.

### B. Execution layer (runtime)

**What it is:** runners + `GraphState` + scheduling/cycles + value resolution.

**What it decides (at runtime):**
- which nodes execute, in what order
- “staleness” via `input_versions`
- how interrupts/stop behave during execution

**Internal storage:** `GraphState.values` holds *everything produced so far* during a run (even if it won’t be returned).

### C. Observability layer (events)

**What it is:** the event stream (`NodeStartEvent`, `StreamingChunkEvent`, etc.) + `EventProcessor` + `.iter()`.

**What it’s for:** UI, logs, tracing, metrics, debugging.

**Crucial separation:** events are *write-only* and **not** a durability mechanism.

> Events can tell you what happened, but they are not the source of truth for resuming, forking, or time-travel.

### D. Persistence layer (steps + checkpointer)

**What it is:** `Checkpointer` writes `StepRecord`s; state is computed by folding step values.

**What it’s for:** correctness of resume, fork, time-travel, and “exactly-once at the step boundary”.

**Key invariant (from `specs/deprecated/state-durability-matrix.md`):**
> If a step can be skipped on resume, then the step must have durable outputs (inline or referenced).

### E. Missing piece: “artifact storage” (proposed)

We currently have “serialize values into StepRecord”.

Production systems add a tier:
- store small stuff inline,
- store large/non-JSON stuff as **artifacts** (blob/object-store),
- persist only a `BlobRef` in StepRecord.

This solves “persist everything” without “store huge JSON/pickle blobs in your DB”.

---

## 2) Why nested graphs are the right place for this

There are *two different boundaries* people want:

1. **Dataflow boundary:** “outer graph depends only on these outputs”
2. **Durability boundary:** “this set of internal steps should (or should not) be persisted individually”

`GraphNode` is already a boundary for (1), but it’s important to distinguish:

- **Wiring surface (lifted outputs):** the subset of inner outputs that are
  lifted into the parent graph’s value namespace and can be consumed by
  other outer nodes under normal parameter names.

- **Return surface (nested RunResult):** the nested graph’s `RunResult` can
  still contain all inner outputs (subject to `select=` filtering), even if
  most of them are not lifted for wiring.

We can extend it to cleanly express (2), while keeping the inner steps explicit and inspectable when desired.

---

## 3) What GraphNode already does today (and the gap)

Today:
- `Graph.as_node()` returns a `GraphNode` wrapper.
- `GraphNode.outputs` defaults to the inner graph’s `outputs`, which controls the **lifted outputs** (what can be wired to downstream nodes in the parent under normal names).
- Nested graphs are persisted as **child workflows** (`workflow_id/node_name`) with `child_workflow_id` links.
- Results are nested `RunResult` objects, so you can do `result["rag"]["embedding"]`.

**The gap:** we don’t have an explicit way to say:
- “this nested graph is durable internally” vs “treat it as an atomic step”
- “store only lifted outputs in the parent step record (don’t duplicate every internal output)”

That’s exactly the escape hatch you were reaching for.

---

## 4) Refined proposal: GraphNode as a “durability boundary”

Add two orthogonal configuration concepts to GraphNode:

### 4.1 Lifted output surface (already present, but easy to confuse)

**Lifted outputs** = the outputs that cross the boundary into the parent workflow’s state and can be consumed by outer nodes under normal parameter names.

Today this is `GraphNode.outputs` (default: all inner outputs), but we should make it explicit and allow selecting a subset.

API sketch:

```python
rag = Graph(nodes=[embed, retrieve, generate], name="rag")

rag_node = (
    rag.as_node()
    .lift("docs", "response")  # lifted outputs (subset, optional)
)
```

Design intent:
- lifted outputs are **the parent-visible state** of the subgraph for wiring
- non-lifted outputs remain internal to the parent (not usable for outer wiring)

Important: this does *not* change what the nested `RunResult` can return. The caller may still select and inspect any inner output via `result["rag"][...]` or `select=["rag/embedding"]`. “Lifted outputs” only govern parent-level wiring/value resolution.

### 4.2 Durability mode (new, the key refinement)

GraphNode chooses the durability semantics for its inner graph:

#### Mode A: `durability="nested"` (default)

- Inner graph executes as a **child workflow** (`parent_id/node_name`).
- Inner nodes are persisted as their own `StepRecord`s.
- Full resume/fork/time-travel works *inside* the subgraph.
- Interrupts are allowed (pause/resume needs persistence).

#### Mode B: `durability="atomic"` (opt-in)

- Inner graph executes, but **does not create/persist a child workflow**.
- Only **one** durable step exists: the parent’s `GraphNode` step.
- Only the **lifted outputs** are persisted as that step’s `values`.
- Crash mid-subgraph ⇒ rerun the whole subgraph on resume.
- **Interrupts are forbidden** (can’t reliably pause/resume inside without persisted cursor).

API sketch:

```python
rag_node = (
    rag.as_node()
    .lift("docs", "response")
    .with_durability("atomic")
)
```

---

## 5) Capabilities and constraints (why interrupts matter)

### Capabilities table

| Capability | `durability="nested"` | `durability="atomic"` |
|---|---:|---:|
| Resume correctness | ✅ | ✅ (at boundary) |
| Exactly-once granularity | ✅ (per inner step) | ⚠️ (only at boundary) |
| Fork/time-travel *inside* subgraph | ✅ | ❌ |
| Interrupts inside | ✅ | ❌ (validate at build time) |
| Heavy intermediates persisted | ✅ (but should be artifact refs) | ❌ (if not exported) |
| Arbitrary/non-persistable intermediates | ✅ only if serializable | ✅ if not exported |
| Cost after crash mid-subgraph | low (resume from last inner step) | higher (rerun entire subgraph) |

### Build-time validation rules (proposed)

At `Graph` construction time:
- If a `GraphNode` is `durability="atomic"` and the inner graph has interrupts → raise `GraphConfigError` with a clear message and recommended fixes.

Suggested error message shape:
> “GraphNode ‘rag’ is configured as atomic (no inner persistence), but the inner graph contains InterruptNodes. Interrupts require durable step history to resume reliably. Use durability=’nested’ or move the interrupt outside the atomic boundary.”

---

## 6) How this maps onto steps + persistence

### Nested mode (current model, refined)

Persistence model:
- Child workflow has StepRecords for inner nodes.
- Parent workflow has one StepRecord for the GraphNode boundary that includes:
  - `child_workflow_id`
  - and **only lifted outputs** (avoid duplicating the full child state)

This matters for heavy intermediates: even if the child persists them, we don’t want to also store them again in the parent step.

### Atomic mode (new model)

Persistence model:
- No child workflow.
- Parent workflow has one StepRecord (the GraphNode) that stores only lifted outputs.

This is effectively “treat the subgraph like a node”, but the subgraph remains explicit and observable.

---

## 7) How this maps onto events (observability)

Events are orthogonal: both modes can emit full inner events.

But note the semantics:
- In `nested` mode, inner nodes can be **replayed** from checkpoint; events may carry `replayed=True` on `NodeEndEvent` when outputs come from persistence.
- In `atomic` mode, there is no inner checkpoint, so “replayed inner nodes” doesn’t exist; if rerun happens after crash, you’ll see a second set of events.

This is fine as long as it’s documented: events are not the resume source of truth.

---

## 8) Concrete example: “don’t persist embeddings, but keep the graph explicit”

### The inner graph (explicit, not hidden)

```python
@node(output_name="embedding")
def embed(query: str) -> list[float]: ...

@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]: ...

@node(output_name="response")
def generate(docs: list[str]) -> str: ...

rag = Graph(nodes=[embed, retrieve, generate], name="rag")
```

### Option 1 (preferred for production): nested durability + artifacts

- Persist everything, but embeddings are stored as `BlobRef` (not inline).
- Full time-travel/fork inside the RAG pipeline works.

This is the “Temporal/LangGraph” style solution.

### Option 2 (your escape hatch): atomic boundary

```python
outer = Graph(nodes=[
    preprocess,
    rag.as_node()
      .lift("docs", "response")
      .with_durability("atomic"),
    postprocess,
])
```

Semantics:
- `embedding` never crosses the boundary (not parent state).
- `embedding` is not persisted (no inner step records).
- If the process crashes halfway through RAG, resume reruns the whole RAG subgraph.
- Interrupts are not allowed inside this boundary.

### What atomic changes (and what stays the same)

Same (conceptually):
- Execution semantics inside the boundary: it still runs the same inner nodes in the same order.
- Events: it can still emit full inner-node events (same span hierarchy), so observability stays rich.

Different (necessarily):
- Persistence / resume cursor: there is no durable inner cursor. After a crash mid-subgraph, you rerun the whole subgraph because there are no inner StepRecords to replay.
- Replay semantics in events: in nested mode you can get `replayed=True` for inner nodes; in atomic mode you can’t replay inner nodes (they’ll just run again), so those flags differ.
- Interrupt capability: atomic mode can’t support InterruptNode inside, because pausing requires a durable cursor/state to resume inside the boundary.

This is a clean, explicit trade: you’re choosing durability only at the boundary.

---

## 9) Where this fits with “persist everything by default”

We can keep:
> **Default: persist everything.**

But make it safe and usable by adding:

1. **Artifact tier** (heavy outputs become refs automatically, not giant DB rows)
2. **GraphNode durability modes** (rare opt-in escape hatch when “don’t persist at all” is truly desired)
3. **Clear capability validation** (especially around interrupts)

This keeps the default interface simple while making advanced trade-offs explicit and local:
- you don’t redesign the whole state model,
- you annotate a boundary.
