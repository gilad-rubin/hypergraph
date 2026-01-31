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

### You Need Real-Time Event Streaming

Hypergraph is designed for **workflows** — discrete runs with inputs and outputs. It's not designed for continuous event streams where events arrive unpredictably.

For event streaming, consider dedicated tools like Kafka, Flink, or Temporal.

### You Need Production Maturity Today

Hypergraph is in **alpha**. The core features work, but:

- Breaking API changes are possible
- Some features are "coming soon" (checkpointing, observability hooks)
- Ecosystem integrations are limited

If you need battle-tested production infrastructure today, consider Prefect (for DAGs) or LangGraph (for agents) which have larger communities and more integrations.

## Coming From Other Frameworks

### From Hamilton or Pipefunc

You'll feel right at home. The name-based edge inference is similar, and you already think in DAGs.

**What's new:**
- `@route` and `END` for cycles and agentic loops
- `.as_node()` for hierarchical composition
- Same patterns, but now you can build agents too

### From LangGraph or Pydantic-Graph

The mental model shift: **functions return values, not state updates**.

**What's different:**
- Functions use parameters directly — inputs and outputs are in the signature
- Edges are inferred from parameter names matching output names
- Routing uses `@route` decorators that return target names

**What's the same:**
- Cyclic graphs work
- Conditional routing exists (`@route` ≈ conditional edges)
- Multi-turn workflows are first-class

### From Prefect or Airflow

Hypergraph is for **in-process orchestration**, not distributed job scheduling.

**Use hypergraph for:** The logic inside a single workflow — the graph of functions that run together.

**Use Prefect/Airflow for:** Scheduling, retries, infrastructure, distributed execution across machines.

They can work together: Prefect schedules and monitors jobs, hypergraph defines what happens inside each job.

## Summary

| If you want... | Use hypergraph? |
|----------------|-----------------|
| One framework for DAGs and agents | Yes |
| Hierarchical workflow composition | Yes |
| Pure, testable functions | Yes |
| Multi-turn AI workflows | Yes |
| Minimal boilerplate | Yes |
| Simple scripts | No — just use functions |
| Real-time event streaming | No — use Kafka/Flink |
| Production maturity today | Maybe — evaluate alpha status |
