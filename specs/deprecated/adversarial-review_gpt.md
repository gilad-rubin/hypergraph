# Adversarial Design Review (Reviewed Specs)

**Date:** 2026-01-03  
**Scope:** `specs/reviewed/*.md` (10 files)  
**Goal:** Find loopholes, contradictions, underspecification, and production risks for interactive AI apps, data pipelines (DAGs), and scientific workflows.

Reviewed files:
- `specs/reviewed/checkpointer.md`
- `specs/reviewed/durable-execution.md`
- `specs/reviewed/execution-types.md`
- `specs/reviewed/graph.md`
- `specs/reviewed/node-types.md`
- `specs/reviewed/observability.md`
- `specs/reviewed/persistence.md`
- `specs/reviewed/runners-api-reference.md`
- `specs/reviewed/runners.md`
- `specs/reviewed/state-model.md`

---

## 1) Executive Summary (What Will Break in Production First)

### Critical (design contradictions / undefined semantics)

1. **Cycles + resume semantics are currently self-contradictory.**
   - Multiple docs say “step history is the cursor” and “versions drive staleness”, but the proposed “skip if node name is in completed steps” logic makes **cycles impossible** (a node would only ever run once).
   - This is the single biggest correctness hole because it affects *resume*, *fork*, *loops*, and *parallelism determinism*.

2. **Persistence policy conflicts across specs (and even within specs).**
   - Some places: “**all outputs are persisted**; no selective persistence.”
   - Elsewhere: “Checkpointer stores **only `persist=True` outputs**” and Node types include `persist: bool | None`.
   - This is not a “docs nit”; it changes safety properties (double-sends, double-charges) and storage feasibility (GB-scale pipelines).

3. **Terminology/API shape is inconsistent (`inputs` vs `values`, `at_step` vs `superstep`, pause fields, event fields).**
   - Inconsistent names imply an unstable mental model and will become permanent footguns once any public API ships.

4. **Stop/partial-output semantics are inconsistent.**
   - Some sections say “partial output => `StepStatus.COMPLETED` + `partial=True`”.
   - Other sections say “partial output saved with `StepStatus.STOPPED`”.
   - This directly affects recovery, UX (“is this usable?”), and idempotency assumptions.

### High (production risks / predictable failure modes)

5. **Schema-less “outputs are state” has no story for in-flight workflows when code changes.**
   - A paused workflow can resume weeks later with state produced by older code. Without versioning/migrations/contracts, resumes will fail nondeterministically.

6. **Serialization guidance currently encourages insecure/fragile paths (e.g., pickle).**
   - Pickle = RCE risk if persistence storage is ever attacker-controlled or shared; also brittle across Python versions and code refactors.

7. **Observability event payloads are underspecified for size/privacy/transport.**
   - Events include `inputs`, `outputs`, and even `Exception` objects in some definitions; that’s not safely serializable to UIs or vendors and can leak secrets.

8. **Workflow concurrency/locking is asserted but not specified.**
   - “One active execution per workflow” is claimed, but there’s no concrete locking strategy across processes for SQLite/Postgres/custom stores.

9. **State reconstruction cost is unbounded (`get_state()` = fold over history).**
   - Several specs imply `get_state()` reconstructs state by folding step results.
   - For long-lived workflows (chat threads, scheduled workflows), this becomes O(number of steps) per resume/inspection unless you introduce snapshots/materialization.

10. **Atomicity of persistence writes is not specified (Step vs StepResult split).**
   - Step metadata and StepResult payloads are separate entities. Without transactional writes, crashes can leave “dangling steps” or “dangling results”, breaking resume and audit queries.

---

## 2) Cross-Spec Contradictions (These Must Be Resolved First)

### 2.1 `inputs` vs `values` is inconsistent across the API surface

You currently have three incompatible “truths”:
- `execution-types.md` explicitly defines terminology: **use `inputs` when calling the runner**.
- `runners-api-reference.md` defines `BaseRunner.run(graph, values=...)` and `AsyncRunner.run(..., values=...)`.
- `runners.md` and `persistence.md` mix `inputs=` and `values=` in examples (sometimes within the same section).

Why this matters:
- It’s not cosmetic: it signals whether “inputs” is a first-class concept distinct from “values/state”.
- Docs and code drift will be inevitable unless one canonical term is chosen and enforced.

Concrete failure mode:
- Users write wrappers/middleware expecting `inputs=...`; half the examples use `values=...`; your API becomes “guess-and-check”.

**Recommendation:** Pick one. Prefer `inputs=` for run-entry and reserve `values` for accumulated state and persisted values. Then make every spec file obey it.

---

### 2.2 Persistence policy: “persist everything” vs “persist only `persist=True`”

Conflicts:
- `durable-execution.md` and `execution-types.md` state: **“All outputs are persisted; no selective persistence.”**
- `node-types.md` defines `FunctionNode(..., persist: bool | None)` and describes node-level persistence overrides.
- `observability.md` states Checkpointer stores “Only `persist=True` outputs”.
- `checkpointer.md` and `persistence.md` assert “Everything is saved.”

Why this matters:
- This is a core safety contract. “Persist everything” prevents double side effects on crash recovery. “Selective persistence” is often needed for cost/PII reasons, but creates correctness hazards.
- Until this is settled, you can’t correctly specify recovery semantics, storage schema, or user expectations.

Adversarial example (data pipeline):
- Node outputs a 2GB DataFrame intermediate. “Persist everything” destroys throughput and storage. User quietly sets `persist=False` (because the API suggests it exists). Recovery now re-executes a downstream side-effecting node and duplicates actions.

**Recommendation:** Decide explicitly:
- Option A (safety-first): No selective persistence, delete `persist` from node types and every mention of `persist=True`.
- Option B (pragmatic): Keep `persist`, but require **idempotency guardrails** (idempotency keys, “side effect nodes must be persisted”, explicit “unsafe to resume” markers, etc.).

Right now you have both policies at once, which is the worst possible state.

---

### 2.3 Checkpoint addressing: `step_index` vs `superstep` vs `at_step`

Conflicts:
- `execution-types.md` uses **`superstep`** as user-facing checkpoint boundary.
- `checkpointer.md` also uses `superstep`.
- `persistence.md` uses `at_step` / `up_to_step`.
- `durable-execution.md` sometimes says checkpoint is identified by `workflow_id + step_index`, and sometimes shows `superstep`.

Why this matters:
- Users will build “resume from point N” features. If “N” is ambiguous, your API will become unfixable.
- Internally, you also need a stable unit for determinism constraints.

Adversarial example:
- UI shows “resume from step 5” (meaning superstep). Backend interprets as raw index and forks from a partial superstep. State is inconsistent (“some parallel nodes completed, some didn’t”), causing missing inputs or wrong results.

**Recommendation:** Make `superstep` the only user-facing unit (as you already argue in `durable-execution.md`) and remove `at_step` naming from reviewed docs.

---

### 2.4 DBOSAsyncRunner streaming support is contradictory

Conflicts:
- `runners.md` compatibility matrix: DBOSAsyncRunner supports `.iter()` streaming.
- `durable-execution.md` states: `.iter()` streaming ✅ for checkpointer, ❌ for DBOS (“use EventProcessor”).
- `runners-api-reference.md` says DBOS streaming is “via EventProcessor only, not `.iter()`”.

Why this matters:
- Streaming is a primary requirement for interactive AI apps.
- If DBOS cannot provide `.iter()`, you need a first-class alternative pattern for UIs (websocket bridge, durable streaming offsets, etc.).

**Recommendation:** Pick one:
- Support `.iter()` for DBOS (harder; DBOS “workflow replay” constraints can complicate), or
- Explicitly ban `.iter()` and provide a canonical “event sink + UI transport” recipe with durability semantics.

---

### 2.5 Stop + partial output semantics contradict the status model

Conflicts:
- `execution-types.md`: partial output is represented by `StepStatus.COMPLETED` + `StepResult.partial=True`.
- `durable-execution.md` stop section claims partial is saved with `StepStatus.STOPPED` (and then used downstream), which contradicts the StepStatus definition (“STOPPED => no usable output”).

Why this matters:
- Resume logic depends on statuses.
- “Is this output safe to use?” depends on whether it’s “completed but truncated” vs “stopped with no output”.

**Recommendation:** Make exactly one representation:
- If partial is usable, it must be `COMPLETED + partial=True`.
- Reserve `STOPPED` for “no usable output was produced”.

---

### 2.6 Pause / interrupt field naming is inconsistent (node_name vs node, interrupt_name, etc.)

Conflicts across `execution-types.md`, `runners-api-reference.md`, `observability.md`:
- `PauseInfo.node_name` vs `PauseInfo.node`
- `InterruptEvent.node_name` vs `InterruptEvent.interrupt_name`
- Resume examples sometimes use `response_param`, sometimes (incorrectly) use `node_name` as the key in `inputs`.

Why this matters:
- HITL is the most brittle integration point. Any ambiguity here turns into production outages (“resume writes response into wrong key”).

**Recommendation:** Canonicalize:
- Interrupt identity: `interrupt_name` (node name) and `interrupt_path` (for nested).
- Resume key: `response_param` (namespaced if nested; see below).

---

## 3) Core Execution Semantics (Underspecified or Incorrect)

### 3.1 Cycles: “step history is cursor” + “skip completed nodes” cannot both be true

Multiple locations propose logic like:
```python
should_skip = node.name in {step.node_name for step in steps if step.status == COMPLETED}
```

This makes a cycle impossible, because on the second iteration, `node.name` is already present and will be skipped forever.

What you need instead (one of these):
- **Version-based re-execution**: store, per node execution, the versions of the inputs it consumed (or a hash of resolved inputs). Re-run if any dependency version changed.
- **Iteration-scoped completion**: treat “completed steps” as scoped to a given superstep/iteration, not global by node name.

Concrete example:
- Cycle `generate -> accumulate -> check_done -> generate`.
- `generate` must run multiple times; “node name seen before” is irrelevant; what matters is “did inputs change since last time”.

**Recommendation:** Write down the canonical algorithm for:
1. computing runnable nodes,
2. determining whether a node is stale,
3. persisting the minimal metadata needed to resume deterministically.

Right now `GraphState.versions` exists, but the persistence layer does not persist the info required to recreate `is_stale()` decisions.

---

### 3.2 Parallelism: deterministic ordering vs dynamic scheduling is only half-specified

You claim deterministic ordering by “alphabetical node_name within superstep”. This has sharp edges:
- Renaming a node changes indices (and potentially DBOS determinism assumptions).
- Gate decisions can change which nodes are in a superstep; “preassign alphabetical indices” implies you know the set before execution.
- For reproducibility, you need a canonical ordering definition that survives refactors (e.g., stable node IDs or a build-time topological order).

Adversarial example:
- Two independent nodes `fetch_A` and `fetch_B` run in parallel. A refactor renames `fetch_A` -> `fetch_user`. Ordering flips. If any downstream behavior depends on “which one writes first” (conflict handling, checkpoint folding), you get silent divergence.

**Recommendation:** Define:
- If two steps are in the same superstep, does the *folding order* matter? (If yes, you need a stable order.)
- If it must be stable, define a stable identifier independent of display name (e.g., `node_id`).

---

### 3.3 “No reducers needed” is not true in practice; you’re forcing users into awkward patterns

`state-model.md` claims reducers aren’t needed because each node produces distinct outputs. But real workflows often need aggregation:
- two retrievers each produce `docs`, combined into one list
- multiple validators produce `errors`, combined into a list
- map/reduce style pipelines

Your current stance turns this into:
- “make each output unique, then write a merge node later”

That’s workable, but then:
- conflict validation must allow “multiple producers” patterns, and
- you should provide a first-class aggregation story (even if it’s explicit).

Adversarial example:
- `retrieve_web -> docs`, `retrieve_db -> docs`. Your validator forces renames (`web_docs`, `db_docs`) and adds a merge node. Fine. But then `select=["docs"]` and other features become less ergonomic.

**Recommendation:** Either:
- keep the “no implicit reducers” stance but document explicit merge patterns as first-class (and loosen conflict rules for mergeable types), or
- introduce optional reducer semantics for advanced users (carefully; can be a footgun).

---

### 3.4 “Provide upstream outputs as inputs to skip nodes” is a correctness loophole

This is powerful, but dangerous without guardrails:
- You can “skip” a node that normally enforces invariants or sanitization by injecting its output.
- Caching/checkpointing may treat injected outputs as equivalent to computed outputs (they are not).
- In cycles, injected values can break version semantics (“edge value beats input”).

Adversarial example:
- A pipeline expects `validated_data` produced by `validate()`; user passes `validated_data` directly and bypasses validation.

**Recommendation:** If this is a supported feature, specify it as “expert mode” and consider:
- “invariant nodes” that cannot be skipped,
- a validation mode that detects missing provenance (“value was injected, not produced”),
- explicit provenance metadata in `GraphState` and persisted `StepResult`.

---

### 3.5 Build-time validation claims more than it can prove (cycles, exclusivity)

`graph.md` contains several “build-time validation” checks (cycle termination, deadlock detection, mutual exclusivity of parallel producers).

These are useful, but the current framing risks giving users **false confidence**:
- “Cycle has a path to a leaf” does not mean the route will ever choose that path at runtime.
- “Mutually exclusive producers” is non-trivial to prove unless the routing model is very constrained (and the spec does not define the formal model).

Adversarial examples:
- A `@route` node *can* return `END`, so validation passes, but due to a bug/data distribution it never returns `END` in production → infinite loop.
- Two nodes produce the same output name; validation believes they are mutually exclusive, but a refactor changes routing so both execute in one run → nondeterministic state / persistence ambiguity.

**Recommendation:** Treat these validations as best-effort and specify the hard guarantees:
- “What is provable at build time” vs “what is only detectable at runtime”.
- Add runtime protections: max-iteration caps (already mentioned), explicit conflict errors when both producers actually run, and clearer guidance for explicit merge nodes.

---

### 3.6 `strict_types` is a placeholder unless you define what it checks and when

`Graph(..., strict_types: bool = False)` exists, but the specs don’t define:
- what is checked (annotation compatibility? runtime `isinstance`? `typing` support?),
- how it handles `Any`, `Optional`, generics, `Protocol`,
- whether type checks happen at build time, run time, or both.

Adversarial example:
- Upstream node returns `dict`, downstream expects `MyTypedDict` or `pydantic.BaseModel`. Static hints “look compatible” but runtime behavior breaks, and “strict_types=True” doesn’t clearly help.

**Recommendation:** Decide the minimal contract:
- Build-time: validate *declared* output annotations vs downstream parameter annotations where meaningful (and clearly document the limitations).
- Runtime (optional): allow users to plug in validators (pydantic, beartype, custom functions) rather than pretending Python typing is enforceable by default.

## 4) Nested Graphs (Namespace, Persistence, and Resume Ambiguities)

### 4.1 Namespacing is inconsistent: nested dict access vs path access

`graph.md` examples use both:
- `result["rag"]["docs"]` (nested `RunResult`)
- references to path-like access such as `result["rag/docs"]` and `select=["rag/docs"]`

You also reserve `/` in names to support path syntax, but it’s unclear whether path access is actually supported or only used for `select`.

**Recommendation:** Choose:
- Option A: only nested `RunResult` access, no path access in `__getitem__`.
- Option B: support both, but define precedence and ambiguity rules.

---

### 4.2 Resume keys for nested interrupts will collide unless namespaced

Top-level resume pattern is:
```python
inputs={result.pause.response_param: user_response}
```

If you have two nested graphs each with an interrupt using `response_param="decision"`, the resume key collides.

You partially solve nested identity via `PauseInfo.node_name` as a path (e.g., `"review/approval"`), but you do not specify how inputs should be namespaced.

**Recommendation:** Make resume inputs unambiguous:
- Either require response params to be globally unique across the workflow (hard),
- Or namespace them automatically: `response_param="review/decision"` (or carry both `response_param` and `response_path`).

---

### 4.3 Persistence of nested RunResults is underspecified

You want:
- parent `RunResult.values["rag"]` to be a `RunResult`
- persistence layer to store nested workflows separately via `child_workflow_id`

But then:
- does `checkpointer.get_state(parent)` include nested RunResults as values?
- if yes, does it query child workflow state eagerly? (expensive)
- if no, how does the user reconstruct the nested `RunResult` tree from persistence?

**Recommendation:** Specify a single canonical behavior:
- either `get_state()` returns only *flat* values and nested is resolved separately, or
- `get_state()` can optionally include nested RunResult objects via an `include_nested=` parameter with clear cost semantics.

---

### 4.4 Workflow ID path composition needs constraints (or escaping)

Several specs propose nested workflow IDs like:
```python
child_workflow_id = f"{parent_id}/{node_name}"
```

You correctly forbid `/` in node names and output names, but you do not clearly constrain `workflow_id`.

Adversarial example:
- User chooses `workflow_id="customer/123"` for their own hierarchy needs.
- Nested graph `rag` becomes `"customer/123/rag"`, which is now ambiguous (is `"customer"` the parent and `"123"` a node?).

**Recommendation:** Make this explicit and enforceable:
- Forbid `/` in `workflow_id`, or
- Define an escaping/encoding scheme and provide helper functions as part of the public API.

---

## 5) Durability, Idempotency, and Side Effects (Production Reality Check)

### 5.1 “Persist everything” is safe but often infeasible; “persist selectively” is feasible but unsafe

This is the central tradeoff for production:
- AI apps: persisting every token/trace can be expensive and a privacy problem.
- Scientific workflows: persisting every intermediate is often impossible.
- Business workflows: side effects (emails, payments) require strong safety contracts.

Right now the specs oscillate between both models without resolving the implications.

Concrete “two-tier” solution to consider:
- **Durability tier:** persist enough to guarantee “no repeated side effects” and “resume works”.
- **Cache tier:** optional, for performance; can be dropped without changing correctness.

If you keep “persist everything”, you need:
- storage/retention/compression guidance,
- clear “values must be serializable” constraints,
- redaction rules for PII,
- a plan for large blobs (object storage pointers rather than inline DB).

If you allow selective persistence, you need:
- explicit marking of unsafe nodes,
- idempotency keys,
- “exactly-once side effect” primitives (or clear boundaries and warnings).

---

### 5.2 Workflow ID is a footgun without explicit locking and “create vs resume” modes

You repeatedly promise “just re-run with same workflow_id to resume”.

Two big missing pieces:
1. **Concurrency/locking:** What happens if two workers call `run(..., workflow_id="same")` concurrently?
2. **Intent:** What if the user accidentally reuses an old workflow_id and mutates an existing workflow?

**Recommendation:**
- Specify locking semantics (DB row locks / advisory locks / optimistic concurrency).
- Consider an explicit mode: `mode="create|resume|create_or_resume"` or separate APIs.

---

### 5.3 Schema evolution: paused workflows + code changes = inevitable crashes

Your framework is explicitly targeting long-lived workflows (pause/resume, HITL, scheduling).

That implies you must answer:
- What happens if code changes between pause and resume?
- Are workflows pinned to a “graph version”?
- Do you support migrations (transforming checkpointed state to new schema)?

Concrete failure:
- Step outputs persisted as JSON with a field renamed in code. Resume loads old shape and downstream code breaks.

**Recommendation:** Add a minimal versioning story:
- include `graph_hash` / `code_version` in workflow metadata,
- on resume, detect mismatch and require explicit migration strategy (or fail loudly with actionable error).

---

### 5.4 Step write atomicity is underspecified (you will get corrupt histories)

You model persistence as:
- `Step` (metadata) + `StepResult` (payload), sometimes stored separately.

If `save_step()` (or its internal equivalent) isn’t **transactional**, you can easily end up with:
- step exists, result missing (crash after writing step row)
- result exists, step missing (crash after writing result blob)
- duplicates (retry writes without idempotency)

Adversarial example:
- A worker writes `Step(status=COMPLETED)` then crashes before writing `StepResult.values`.
- Resume logic sees a “completed” step and skips re-execution, but the value is missing → downstream missing input errors.

**Recommendation:** Make `save_step()` atomic and idempotent:
- single transaction for step + result,
- unique constraints on `(workflow_id, index)` and/or `(workflow_id, superstep, node_name)` (whatever your model is),
- upsert semantics that are safe under retries.

---

### 5.5 `get_state()` scalability and “time travel” need a materialization strategy

The “steps are the source of truth” philosophy is clean, but it implies:
- `get_state(workflow_id)` = fold over potentially huge histories.

This is fine for small workflows; it collapses for:
- chat sessions with thousands of turns,
- long ETL jobs with many steps,
- scheduled workflows running for weeks.

**Recommendation:** Specify an implementation strategy:
- periodic snapshots of computed state (per superstep),
- incremental materialization,
- or a queryable “latest values” table keyed by output name + version.

Keep “steps as source of truth” as the correctness model, but don’t require O(n) reconstruction on every resume.

---

## 6) Serialization & Security

### 6.1 Pickle as a recommended serializer is dangerous in production

Pickle is:
- not safe against untrusted input,
- fragile across Python versions and refactors,
- hard to inspect/debug.

If users follow the current examples, they will eventually create “deserialize arbitrary bytes from DB” code paths.

**Recommendation:** Position pickle as dev-only and provide production guidance:
- JSON/msgpack + explicit encoders,
- object storage pointers for large blobs,
- encryption-at-rest + redaction hooks,
- schema version tagging.

---

## 7) Observability (Events Are Great, Payload Semantics Aren’t)

### 7.1 Event types include non-transportable fields

Several specs define:
- `NodeErrorEvent.error: Exception`
- `NodeStartEvent.inputs: dict[str, Any]` and `NodeEndEvent.outputs: Any`

That’s not something you can safely:
- send over WebSocket to a browser,
- emit to Langfuse/Logfire without scrubbing,
- store in logs without leaking secrets.

**Recommendation:** Define two payload tiers:
- internal in-process event objects (can carry rich types),
- external/serialized event schema (must be JSON-safe + redacted).

---

### 7.2 “Events are not persisted” undermines debugging and compliance unless optional persistence exists

You separate steps (durability) from events (observability), which is good.

But production teams often need:
- audit trails,
- reconstructable traces for incidents,
- “what did the model see?” proofs.

If events aren’t persisted, you need an endorsed pattern:
- processors that persist sanitized events (and how to correlate with steps).

---

### 7.3 Processor failure/backpressure semantics are not specified (observability can change behavior)

Specs describe processors as “fire-and-forget”, but also mention lifecycle hooks and force-flush triggers.

Unanswered questions that become production incidents:
- If an EventProcessor throws, does the run fail? Is it swallowed? Is it retried?
- Are processors invoked sequentially or concurrently? Can one slow processor stall the run?
- Do async processors backpressure execution (especially with streaming chunk events)?

**Recommendation:** Define explicit rules:
- Processor errors never fail the run by default (but can be configured to).
- Processors run in a safe isolation boundary (timeouts/backpressure).
- Define drop/coalescing policies for high-rate streaming events.

---

## 8) File-by-File Notes (Key Findings)

### `specs/reviewed/state-model.md`
- Claims “reducers not needed” because outputs are distinct; real workflows still need aggregation; the spec should acknowledge the explicit-merge pattern as first-class.
- “Inputs override loaded state” is powerful but dangerous for auditability (you can rewrite history/state without leaving a provenance trail).

### `specs/reviewed/graph.md`
- Immutability language is inconsistent (“returns new graph” vs comment “returns self”), minor but signals drift.
- `InputSpec.bound` is a mutable dict inside a frozen dataclass (implied immutability is leaky).
- Deterministic ordering claims conflict with use of `frozenset` for input collections (iteration order is not a stable API).
- Feature detection via `isinstance/hasattr` is used in examples; if you care about clean abstractions, define explicit interfaces/properties.
- Namespacing/path access is ambiguous (`result["rag/embedding"]` implied in errors vs nested access elsewhere).

### `specs/reviewed/node-types.md`
- `FunctionNode.persist` exists but contradicts the “no selective persistence” stance elsewhere.
- `TypeRouteNode` type matching via `isinstance()` is underspecified for overlapping classes/subclasses and precedence.
- `GraphNode.inputs = graph.inputs.all` is described as a tuple, but `InputSpec.all` is a `frozenset`; this breaks deterministic ordering and type expectations.
- Copy/immutability patterns are underspecified for fields like `_map_over` (lists can alias across clones if shallow-copied).

### `specs/reviewed/execution-types.md`
- The “implicit cursor” pseudo-code and later “should_skip_node by name” are incompatible with cycles.
- `PauseInfo.response_param` resume examples do not address collisions across nested graphs.
- Multiple conflicting examples use node name as resume key vs response param.
- Event payloads include exceptions and arbitrary objects; not production-safe without a serialization tier.

### `specs/reviewed/runners.md`
- Examples mix `inputs=` and `values=`; needs a single convention.
- Generator example uses `async def ... -> str` while yielding (should be an async generator type).
- DBOS streaming support conflicts with other specs.
- DaftRunner “supports async nodes” claim is plausible only with extra machinery; otherwise it’s misleading.
- Cancellation/stop is described as graceful, but “what if the node is stuck on blocking I/O?” is not addressed (stop may never complete).

### `specs/reviewed/runners-api-reference.md`
- Canonical signatures use `values`, contradicting other docs’ “inputs vs values” terminology.
- Event type fields differ from `execution-types.md` (`RunEndEvent` shape, interrupt naming, pause naming).
- Capability flags differ across specs (`supports_durable_execution` vs `supports_automatic_recovery`).
- Cross-runner strategy includes `asyncio.run()` in sync contexts; this breaks in environments with an already-running event loop (Jupyter, some servers) unless you specify an alternative.

### `specs/reviewed/checkpointer.md`
- Specifies “full persistence (everything)”, contradicting `persist` and some observability text.
- Custom serializer example recommends pickle without prominent security caveats.
- Does not specify atomic write/idempotency requirements for Step+StepResult under crashes/retries.
- Does not specify a scalability plan for `get_state()` over long histories (snapshots/materialization).
- Nested workflow state retrieval is stated, but how nested RunResults are reconstructed is not defined.

### `specs/reviewed/persistence.md`
- Uses `at_step` / `up_to_step` and a `history=` parameter that conflict with the newer `superstep` / `checkpoint=` design in `execution-types.md`.
- The value resolution hierarchy includes checkpoint as a separate priority layer, while other specs say checkpoint is merged into inputs up-front.

### `specs/reviewed/durable-execution.md`
- “Why no selective persistence” is well argued for safety, but contradicts node-level `persist`.
- Stop/partial semantics contradict `execution-types.md` status model (`STOPPED` vs `COMPLETED+partial`).
- Confirms DBOS streaming not supported via `.iter()`, contradicting `runners.md`.

### `specs/reviewed/observability.md`
- States Checkpointer persists only `persist=True` outputs, contradicting “persist everything.”
- Event model includes `Exception` objects and full inputs/outputs; needs a scrubbed/serialized schema for external sinks.
- Does not define how processor failures/backpressure interact with execution and streaming.
- Typed dispatch is convenient but “magical”; you should document method naming stability guarantees if this becomes a public extension point.

---

## 9) Prioritized Recommendations (Concrete Next Steps)

1. **Pick one canonical vocabulary and enforce it everywhere.**
   - `inputs` for runner entrypoints; `values` for accumulated/persisted outputs; remove other variants.

2. **Write the real execution/resume algorithm for cycles and persist the needed metadata.**
   - Define staleness precisely; define what’s persisted to reconstruct it; define deterministic scheduling rules.

3. **Resolve persistence policy decisively.**
   - Either remove `persist` entirely and go all-in on “persist everything”, or keep it and add correctness guardrails.

4. **Unify checkpoint addressing (`superstep` vs index) and delete conflicting APIs (`history=`, `at_step`).**

5. **Canonicalize interrupt/resume naming and prevent collisions in nested graphs.**
   - Introduce namespaced resume keys or global uniqueness constraints.

6. **Split internal event objects from external/serialized event payloads.**
   - Define what gets scrubbed, how large payloads are handled, and how to correlate events with persisted steps.

7. **Define concurrency/locking semantics for `workflow_id`.**
   - Especially for Postgres and multi-worker deployments.

8. **Add a minimal “workflow versioning/migration” story.**
   - Even if v1 is “fail fast with a clear error when code version changes,” that’s better than silent corruption.

9. **Specify Step/StepResult atomicity and idempotent persistence writes.**
   - This is required for correctness under crashes, retries, and concurrent writers.

10. **Add a scalability plan for `get_state()` (snapshots/materialization).**
   - Otherwise long-lived workflows will degrade linearly over time.

11. **Define EventProcessor failure/backpressure semantics.**
   - Prevent observability from silently corrupting runtime behavior (or vice versa).
