# Guiding Principles

These principles govern hypergraph's design and day-to-day usage.

This version combines:

- **explicit principles** from README/docs/API
- **implicit invariants** enforced in implementation and tests

Use this as a practical rubric for deciding whether a workflow design fits hypergraph.

---

## 1. Portable Functions

Your functions should look the same whether they run inside hypergraph or not. The `@node` decorator adds metadata; it should not force a framework-specific coding style.

### Explicit signals

- Docs emphasize pure/testable functions and testing via `node.func(...)`.
- Comparison docs position hypergraph against state-schema-driven frameworks.

### Implicit invariants

- `FunctionNode` remains directly callable.
- Core behavior is normal Python function execution.

### Good example

```python
@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return model.embed(text)

assert embed.func("hello")
```

### Break example

- Function logic requires framework state object structure to work.
- Removing `@node` breaks core business logic.

---

## 2. Zero Ceremony

Graph complexity should match problem complexity, not framework requirements.

No state schemas. No manual edge wiring for ordinary data flow.

### Explicit signals

- “Minimal”, “No state schemas”, “No boilerplate”.

### Implicit invariants

- Data edges are inferred by name.
- `Graph([nodes...])` is the standard happy path.

### Good example

```python
graph = Graph([embed, retrieve, generate])
```

### Break example

- Adding adapter/plumbing nodes only to satisfy orchestration mechanics.

---

## 3. Names Are Contracts

Edges are inferred from matching output and input names. Output names are API contracts.

### Explicit signals

- README/API repeatedly explain automatic wiring by matching names.

### Implicit invariants

- Graph construction infers data edges from output/input matches.
- Name validity is enforced (identifiers, keyword checks, reserved-name checks).

### Good example

```python
@node(output_name="embedding")
def embed(text: str) -> list[float]: ...

@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]: ...
```

### Break example

- Invalid names (`"bad-output"`, keyword output names).
- Ambiguous naming collisions.

---

## 4. Validate Early, Fail Clearly

Structural errors should fail when building the graph, not while a long run is in progress.

### Explicit signals

- “Fail fast”, “Build-time validation”, `strict_types=True` docs.

### Implicit invariants (enforced checks)

- Duplicate node names.
- Invalid graph/node/output names.
- Shared-parameter default consistency.
- Invalid gate targets and gate self-targeting.
- `multi_target` output conflicts.
- Disallowed cache usage on unsupported node types.
- Strict type compatibility when enabled.

### Concrete break example

```python
@route(targets=["step_a", "step_b", END])
def decide(x: int) -> str:
    return "step_c"  # invalid target

Graph([decide, step_a, step_b])  # GraphConfigError
```

---

## 5. Composition Over Configuration

When workflows grow, nest graphs instead of adding flags or ad-hoc configuration surfaces.

A graph is a node: build and test independently, then reuse via `.as_node()`.

### Explicit signals

- Hierarchical composition is a core design pillar.

### Implicit invariants

- `GraphNode` projects inner graph inputs/outputs.
- Nested graph naming and namespace rules prevent ambiguous access.
- Nested graphs execute as encapsulated units inside outer graphs.

### Good example

```python
rag = Graph([embed, retrieve, generate], name="rag")
workflow = Graph([validate, rag.as_node(), format_output])
```

### Break example

- Forcing unrelated concerns into one flat, hard-to-reason-about graph.

---

## 6. Keep Routing Simple

Routing nodes decide **where** execution goes, not **what heavy computation** happens.

### Explicit signals

- Routing should be quick and based on already-computed values.

### Implicit invariants

- Async routing functions are rejected.
- Generator routing functions are rejected.
- Returned decisions are validated against declared targets.
- String `"END"` is guarded against sentinel confusion.

### Good example

```python
@route(targets=["retry", END])
def should_continue(score: float) -> str:
    return END if score >= 0.8 else "retry"
```

### Break example

- Doing expensive LLM/tool work inside a routing function.
- Returning undeclared targets.

---

## 7. Cycles Require Entry Points

When a value participates in a cycle, the first iteration needs an initial value. Entry points group these by the node where execution starts.

### Explicit signals

- InputSpec docs define `entrypoints` for cyclic inputs, grouped by node name.

### Implicit invariants

- Cycle parameters are classified as entrypoint parameters.
- Missing entrypoint values fail input validation.
- Entry point values can be bound with `bind()` or provided at `runner.run()` time.

### Good example

```python
result = runner.run(graph, {"messages": [], "query": "..."})
```

### Break example

- Running cyclic flows without initializing entrypoint values.

---

## 8. Immutability

Nodes and graphs behave like values. Transformations produce new instances.

### Explicit signals

- `with_*`, `bind`, `select` are documented as immutable transformations.

### Implicit invariants

- Node rename APIs clone.
- Graph transformations clone (`bind`, `select`, `unbind`).

### Good example

```python
base = Graph([a, b, c])
configured = base.bind(model="gpt-4")
# base is unchanged
```

### Break example

- Assuming `graph.bind(...)` mutates in place and discarding the returned graph.

---

## 9. Explicit Over Implicit

Be explicit about outputs, renames, and control flow intent.

### Explicit signals

- Output names are declared.
- Renames are deliberate (`with_inputs`, `with_outputs`, `with_name`).

### Implicit invariants

- Warnings surface potentially ambiguous intent (for example return annotation without `output_name`).
- Reserved names and collisions are rejected.

### Good example

```python
@node(output_name="embedding")
def embed(text: str) -> list[float]: ...

adapted = embed.with_inputs(text="document")
```

### Break example

- Relying on hidden conventions instead of explicit naming and routing declarations.

---

## 10. Think Singular, Scale with Map

Write logic for one item. Scale orchestration with `.map()` or `GraphNode.map_over(...)`.

### Explicit signals

- Design docs emphasize this workflow pattern.

### Implicit invariants

- `map_over` validates mapped parameters exist.
- Zip/product modes define combination semantics.
- Mapped outputs are list-shaped.
- Interrupts are incompatible with map execution.

### Good example

```python
mapped = inner.as_node().map_over("x")
```

### Break example

- Embedding manual batch loops into node logic instead of mapping execution.

---

## 11. Caching Is Opt-In and Deterministic

Caching should depend on stable node definition + input values.

### Explicit signals

- Requires node opt-in (`cache=True`) and a runner cache backend.

### Implicit invariants

- Cache key includes `definition_hash` and resolved inputs.
- Code changes invalidate cache naturally via definition hash changes.
- `InterruptNode` and `GraphNode` caching is disallowed.
- Gate routing decisions can be cached and replayed safely.

### Good example

```python
@node(output_name="embedding", cache=True)
def embed(text: str) -> list[float]: ...
```

### Break example

- Expecting cache semantics on human-interrupt nodes.

---

## 12. Use `.bind()` for Shared Resources

Provide stateful/non-copyable dependencies through bindings, not mutable signature defaults.

### Explicit signals

- Graph docs recommend `.bind()` for dependency injection and shared resources.

### Implicit invariants

- Bound values are intentionally shared (not deep-copied).
- Signature defaults are deep-copied per run; non-copyable defaults fail clearly.

### Good example

```python
graph = Graph([embed_query]).bind(embedder=my_embedder)
```

### Break example

```python
@node(output_name="embedding")
def embed_query(query: str, embedder: Embedder = Embedder()): ...
# risky/non-copyable default behavior
```

---

## 13. Separate Computation From Observation

Execution logic and observability should stay decoupled.

### Explicit signals

- Event processors are the first-class way to observe execution.

### Implicit invariants

- Runners emit structured lifecycle events.
- Processors are best-effort; observability should not alter business logic.

### Good example

```python
runner.run(graph, values, event_processors=[RichProgressProcessor()])
```

### Break example

- Embedding logging/telemetry control flow directly into node computation.

---

## 14. One Framework, Full Spectrum

The same primitives (`@node`, `@route`, `Graph`, runners) span DAGs, branches, loops, and nested workflows.

### Explicit signals

- Core docs position hypergraph as a unified model across orchestration styles.

### Implicit invariants

- Same execution model supports DAGs and cyclic patterns.
- Composition and routing remain consistent as complexity grows.

### Good example

```python
pipeline = Graph([clean, transform, load])
agent = Graph([generate, evaluate, should_continue])
workflow = Graph([validate, agent.as_node(), report])
```

### Break example

- Switching programming models mid-workflow because abstractions don’t compose.

---

## Common Design Dilemmas (Option A vs Option B)

| Dilemma | Option A | Option B | Prefer | Why |
|---|---|---|---|---|
| Function design | Framework-coupled `state` dict | Plain function params/returns | **B** | Keeps portability and local testability |
| Growing complexity | Flat mega-graph | Nested graphs via `.as_node()` | **B** | Better encapsulation and reuse |
| Validation timing | Catch during execution | Catch at `Graph(...)` construction | **B** | Faster feedback, lower run-time risk |
| Scaling strategy | Manual loops in nodes | `.map()` / `map_over` | **B** | Cleaner node semantics |
| Shared dependencies | Signature defaults for clients/connections | `.bind()` shared resources | **B** | Correct lifecycle and copy semantics |

---

## The Underlying Test

A design likely fits hypergraph when:

- Functions are testable as plain Python.
- Graph wiring mostly comes from meaningful names.
- Structural mistakes surface before execution.
- Nested composition reduces complexity.
- Diffs track business logic, not framework plumbing.
