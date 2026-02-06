# Guiding Principles

These principles govern hypergraph's design. They should guide how you build workflows and how you evaluate whether something "fits" the framework.

---

## 1. Portable Functions

Your functions should look the same whether they run inside hypergraph or not. The `@node` decorator adds metadata — it doesn't change behavior.

```python
@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return model.embed(text)

# Test directly — no framework needed
def test_embed():
    assert len(embed.func("hello")) == 768
```

If removing `@node` would break your function, something is wrong. Functions are yours — the framework borrows them.

---

## 2. Zero Ceremony

The complexity of your graph should match the complexity of your problem — not more. You shouldn't need extra code just to satisfy the framework.

No state schemas. Inputs are function parameters. Outputs are return values.

```python
# Just write functions. That's it.
graph = Graph([embed, retrieve, generate])
```

If you find yourself writing code that exists only to make the graph work (boilerplate types, manual edge wiring, entry/exit point declarations), the framework has failed.

---

## 3. Names Are Edges

Edges are inferred from matching output and input names. If `embed` produces `"embedding"` and `retrieve` takes `embedding` as a parameter, they're connected automatically.

```python
@node(output_name="embedding")
def embed(text: str) -> list[float]: ...

@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]: ...

# embed → retrieve: connected because "embedding" matches
```

Name your outputs intentionally — they are your API.

---

## 4. Validate Early, Fail Clearly

Structural errors are caught when you *build* the graph, not when you *run* it. Typos, missing connections, type mismatches, invalid route targets — all detected at `Graph()` construction.

```python
@route(targets=["step_a", "step_b", END])
def decide(x: int) -> str:
    return "step_c"  # Typo

graph = Graph([decide, step_a, step_b])
# GraphValidationError: Route target 'step_c' not found.
# Did you mean 'step_a'?
```

Every error message shows what's wrong, why, and how to fix it. The framework treats error messages as a teaching opportunity — not a stack trace to decipher.

---

## 5. Composition Over Configuration

Build structure by nesting graphs, not by adding flags. A graph *is* a node. Test it alone, then compose it into larger workflows.

```python
# Build and test independently
rag = Graph([embed, retrieve, generate], name="rag")

# Compose into a larger workflow
workflow = Graph([validate, rag.as_node(), format_output])
```

Nesting gives you encapsulation (inner details hidden), reuse (same graph in different contexts), and visual clarity (expand/collapse in visualization). If your graph is getting complex, break it into smaller graphs — the same way you'd extract a function.

---

## 6. Think Singular, Scale with Map

Write logic for one item. Scale to many with `.map()`.

```python
# Write for ONE document
@node(output_name="summary")
def summarize(doc: str) -> str: ...

graph = Graph([summarize, validate])

# Scale to many
results = runner.map(graph, [{"doc": d} for d in documents])
```

No batch loops in your code. Each function is testable with a single input. The framework handles fan-out.

---

## 7. Immutability

Nodes and graphs are values. Every transformation returns a new instance — the original is untouched.

```python
original = Graph([a, b, c])
configured = original.bind(model="gpt-4")   # New graph
focused = original.select("answer")          # New graph
# original is unchanged

renamed = my_node.with_inputs(text="query")  # New node
# my_node is unchanged
```

This prevents action-at-a-distance bugs. You can safely pass graphs around, bind different configurations, and know that nothing mutates underneath you.

---

## 8. Explicit Over Implicit

Output names must be declared. Renames are visible. There are no magic defaults or hidden conventions.

```python
# Explicit output
@node(output_name="embedding")
def embed(text: str) -> list[float]: ...

# Explicit rename
adapted = embed.with_inputs(text="document")

# Explicit binding
graph = graph.bind(model="gpt-4")
```

If hypergraph warns you about something (like a return annotation without `output_name`), it's asking you to state your intent clearly. The framework prefers a moment of explicitness over a lifetime of confusion.

---

## 9. Keep Routing Simple

Routing functions decide *where* execution goes, not *what* computation happens. They should be fast, deterministic, and free of side effects.

```python
# Good: simple decision based on pre-computed value
@route(targets=["retry", END])
def should_continue(score: float) -> str:
    return END if score >= 0.8 else "retry"

# Bad: heavy computation in a routing function
@route(targets=["a", "b"])
def decide(text: str) -> str:
    result = expensive_llm_call(text)  # Move to a regular node
    return "a" if result.positive else "b"
```

The framework enforces this — async and generator routing functions are rejected. Pre-compute in regular nodes. Route based on their outputs.

---

## 10. One Framework, Full Spectrum

DAGs, conditional branches, agentic loops, and multi-turn interactions use the same primitives: `@node`, `@route`, `Graph`, and runners.

```python
# DAG: just nodes
pipeline = Graph([clean, transform, load])

# Add branching: add a route
pipeline = Graph([classify, route_by_type, pdf_path, text_path])

# Add loops: route back to an earlier node
agent = Graph([generate, evaluate, should_continue])

# Compose: nest any of these inside each other
workflow = Graph([validate, agent.as_node(), report])
```

You don't switch frameworks when your requirements evolve from a pipeline to an agent. You add a `@route` and an `END`.

---

## The Underlying Test

A principle is being followed when:

- **Every function works without the framework** — call it, test it, debug it directly
- **The graph reads like the problem** — no extra nodes, types, or wiring that exist only for the framework
- **Errors appear at build time** — before any code executes
- **Nesting feels natural** — like extracting a function, not like fighting an API
- **Diffs are surgical** — changes to one part don't ripple through unrelated code
