# When to Use Hypergraph

This guide helps you decide if hypergraph is the right tool for your project.

## Use Hypergraph When...

### You Want One Framework to Master

Instead of learning one tool for DAGs and another for agents, learn one framework that handles both. The same patterns work across the entire spectrum:

- ETL pipelines? Same `@node` decorator, same `Graph`, same runner
- Agentic loops? Add `@route` and `END`, same everything else
- Multi-agent orchestration? Nest graphs with `.as_node()`, same mental model

### Your Workflows Have Natural Hierarchy

Real AI systems aren't flat. They have structure:

```
Evaluation Loop
└── Multi-turn Chat (cyclic)
    └── RAG Pipeline (DAG)
        └── Embedding (single node)
```

If your workflows have this kind of nesting — DAGs inside cycles, cycles inside DAGs — hypergraph's hierarchical composition is a natural fit.

This is where hypergraph tends to stand out most clearly:

- A retrieval pipeline inside a multi-turn chat loop
- A per-document processing graph fanned out over a batch
- A support workflow that routes into a nested technical-review subgraph
- An evaluation DAG that contains the workflow under test as a nested node

**Concrete examples:**

| Outer Layer | Inner Layer | Pattern |
|-------------|-------------|---------|
| Evaluation harness (DAG) | Chat agent (cyclic) | Test cyclic workflows at scale |
| Prompt optimization (cyclic) | Pipeline under test (DAG) | Iterate on prompts |
| Batch processing (DAG) | Per-item workflow (may have branches) | Fan-out with `.map()` |

### You Value Pure, Testable Functions

If you want to test your logic without framework setup or mocking:

```python
# This works — no Graph, no Runner, no setup
def test_my_node():
    result = my_node.func(input_value)
    assert result == expected
```

Your functions are just functions. The `@node` decorator adds metadata, not magic.

### You're Building Multi-Turn AI Workflows

Conversational AI, agentic loops, iterative refinement — these require cycles:

```python
@route(targets=["generate", END])
def should_continue(quality: float, attempts: int) -> str:
    if quality > 0.9 or attempts >= 5:
        return END
    return "generate"
```

Hypergraph handles cycles naturally with `@route` and `END`.

### You Want To Write One Workflow, Then Scale It

Hypergraph works especially well when the logic for **one** item is clear, and scale comes later:

```python
# One item
@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return model.embed(text)

single_item = Graph([embed], name="single_item")

# Many items
batch = Graph([
    load_texts,
    single_item.as_node().rename_inputs(text="texts").map_over("texts"),
    summarize_embeddings,
])
```

This "think singular, scale later" pattern is one of the cleanest ways to use hypergraph for:

- document pipelines
- feature extraction
- evaluation harnesses
- ML preprocessing and model comparison

### You Want Minimal Boilerplate

Define functions, name outputs, and let hypergraph infer the edges:

```python
@node(output_name="result")
def process(data: str) -> str:
    return data.upper()

# Inputs from parameters, outputs from output_name. Edges inferred.
graph = Graph([process, format_output])
```

## Don't Use Hypergraph When...

### You Need a Simple Script

If your task is "call this function, then call that function," you don't need a graph framework:

```python
# Just do this
result1 = step_one(input)
result2 = step_two(result1)
```

Hypergraph adds value when you have non-trivial composition, reuse, or control flow.

### You Need Production Maturity Today

Hypergraph is in **alpha**. The core features work, but:

- Breaking API changes are possible
- Ecosystem integrations are limited
- Durable workflow runtime patterns are possible, but still more manual than in platforms like Inngest, DBOS, or Restate

### You Want A Turnkey Durable Workflow Platform

Hypergraph already has:

- interrupts for pause/resume
- checkpointing and lineage
- nested graphs and batch execution

But if your main need is a runtime that natively owns:

- external event delivery
- approval inboxes
- waiting webhook resumes
- lifecycle operations for long-lived workflows

then you should evaluate whether a more orchestration-heavy platform is a better fit today.

See the [Comparison](comparison.md) page for how hypergraph relates to other frameworks.

## Summary

| If you want... | Use hypergraph? |
|----------------|-----------------|
| One framework for DAGs and agents | Yes |
| Hierarchical workflow composition | Yes |
| Write one workflow, scale with mapped subgraphs | Yes |
| Pure, testable functions | Yes |
| Multi-turn AI workflows | Yes |
| Minimal boilerplate | Yes |
| Simple scripts | No — just use functions |
| Production maturity today | Maybe — evaluate alpha status |
| A turnkey durable workflow runtime | Not yet the primary strength |
