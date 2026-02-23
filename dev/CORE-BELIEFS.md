# Core Beliefs

Non-negotiable design principles. Agents must never violate these. Each entry states the principle, why it matters, and what breaks if violated.

---

## 1. Outputs ARE State

Node outputs define what state is. There is no separate state schema.

**What breaks**: Adding a parallel state management system (TypedDict, Pydantic model, etc.) creates two sources of truth. Nodes stop being independently testable because they depend on the schema.

---

## 2. Names Are Contracts (Automatic Edge Inference)

Matching output/input names create edges. No manual wiring.

**What breaks**: Manual edge wiring defeats the core value proposition. Graph definitions become verbose, and renaming an output requires updating every consumer manually.

---

## 3. Pure Functions

Nodes are testable without the framework: `node.func(x)` works. The `@node` decorator adds metadata, not behavior.

**What breaks**: If removing `@node` breaks business logic, the function is coupled to the framework. Testing becomes expensive — you need a full `Graph` + `Runner` for every unit test.

---

## 4. Build-Time Validation

Errors caught at `Graph()` construction, not hours into a run. Invalid targets, duplicate names, type mismatches, missing inputs — all fail before any node executes.

**What breaks**: Runtime validation means a 3-hour pipeline crashes at step 47 for a structural error that was knowable at build time.

---

## 5. Immutability

`with_*`, `bind`, `select`, `unbind` return new instances. No in-place mutation.

**What breaks**: In-place mutation creates spooky action at a distance. `base = Graph([a, b, c]); configured = base.bind(x=1)` — if `bind` mutates `base`, anyone holding a reference to `base` gets the binding unexpectedly.

---

## 6. Composition Over Configuration

Nest graphs as nodes (`.as_node()`) instead of adding flags or config surfaces.

**What breaks**: Feature-flag spaghetti. A flat mega-graph with conditional logic baked into every node is impossible to test in isolation and hard to reason about.

---

## 7. Explicit Over Implicit

Output names declared, renames deliberate, no magic defaults.

**What breaks**: Implicit behavior creates "why did my graph break?" moments. If output names were auto-inferred from function names, renaming a function silently changes graph topology.

---

## 8. Routing Is Cheap

Gate nodes decide WHERE execution goes, not WHAT computation happens. Heavy work goes in regular nodes.

**What breaks**: Expensive routing functions (LLM calls, DB queries) make graphs hard to reason about. Routing is supposed to be a quick branch — if it's doing real work, it should be a regular node feeding a gate.

---

## 9. Events Separate From Logic

Observability via events, not control flow. `EventProcessor` is best-effort delivery.

**What breaks**: If event processing failures can break execution, you've coupled observation to computation. A flaky logger shouldn't crash your pipeline.

---

## 10. One Framework, Full Spectrum

Same primitives (`@node`, `@route`, `Graph`, runners) for DAGs, branches, loops, and nested hierarchies. No "advanced mode" switch.

**What breaks**: If simple pipelines use different primitives than agentic loops, users must learn two programming models. Composing across the boundary becomes impossible.

---

## The Quick Test

A design likely fits hypergraph when:

- Functions are testable as plain Python
- Graph wiring comes from meaningful names
- Structural mistakes surface before execution
- Nested composition reduces complexity
- Diffs track business logic, not framework plumbing
