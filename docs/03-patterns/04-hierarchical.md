# Hierarchical Composition

This is hypergraph's core insight: **real AI workflows naturally nest DAGs inside cycles and cycles inside DAGs**. Hierarchical composition makes this explicit and clean.

## The Pattern

```
1. Build a graph for one task
2. Use it as a node in a larger graph
3. The larger graph can be used as a node in an even larger graph
4. Repeat at any depth
```

```python
# A graph...
rag = Graph([embed, retrieve, generate], name="rag")

# ...becomes a node
workflow = Graph([
    validate,
    rag.as_node(),  # Graph as a single node
    format_output,
])
```

## Why This Matters

You don't build one graph and stop. You build graphs, compose them, and reuse them in many contexts:

| Context | The Same Graph Used As... |
|---------|---------------------------|
| **Inference** | Direct execution for user queries |
| **Evaluation** | A node inside a test harness |
| **Optimization** | A component in a prompt tuning loop |
| **Batch processing** | Mapped over a dataset |

Build once. Use everywhere.

---

## Example 1: DAG Inside a Cycle (Multi-Turn RAG)

A multi-turn conversation is **cyclic** — the user can keep asking follow-up questions. But retrieval within each turn is a **DAG**.

```python
from hypergraph import Graph, node, route, END, AsyncRunner

# ─────────────────────────────────────────────────────────────
# The RAG pipeline (DAG) — processes one turn
# ─────────────────────────────────────────────────────────────

@node(output_name="embedding")
async def embed(query: str) -> list[float]:
    return await embedder.embed(query)

@node(output_name="docs")
async def retrieve(embedding: list[float]) -> list[str]:
    return await vector_db.search(embedding, k=5)

@node(output_name="response")
async def generate(docs: list[str], query: str, history: list) -> str:
    context = "\n".join(docs)
    return await llm.generate(
        system=f"Context:\n{context}",
        messages=history + [{"role": "user", "content": query}]
    )

# This is a DAG — no cycles
rag_pipeline = Graph([embed, retrieve, generate], name="rag")

# ─────────────────────────────────────────────────────────────
# The conversation loop (Cyclic) — wraps the RAG DAG
# ─────────────────────────────────────────────────────────────

@node(output_name="history")
def accumulate(history: list, query: str, response: str) -> list:
    return history + [
        {"role": "user", "content": query},
        {"role": "assistant", "content": response},
    ]

@route(targets=["rag", END])
def should_continue(history: list) -> str:
    # In practice: check for [END] token, max turns, etc.
    if len(history) >= 20:  # Max 10 turns
        return END
    return "rag"  # Continue conversation

# Compose: RAG DAG inside conversation cycle
conversation = Graph([
    rag_pipeline.as_node(),  # The DAG becomes a single node
    accumulate,
    should_continue,
], name="conversation")
```

**The structure:**
```
Conversation Loop (cyclic)
├── RAG Pipeline (DAG)
│   ├── embed
│   ├── retrieve
│   └── generate
├── accumulate
└── should_continue → loops back to RAG or exits
```

The RAG pipeline runs to completion on each turn. The outer loop decides whether to continue.

---

## Example 2: Cycle Inside a DAG (Evaluation Harness)

Now flip it: your **cyclic** conversation graph becomes a node inside an **evaluation DAG**.

```python
# ─────────────────────────────────────────────────────────────
# Evaluation harness (DAG) — contains the cyclic conversation
# ─────────────────────────────────────────────────────────────

@node(output_name="test_cases")
def load_test_cases(dataset_path: str) -> list[dict]:
    """Load test conversations from a dataset."""
    return json.load(open(dataset_path))

@node(output_name="scores")
def score_responses(history: list, expected: str) -> dict:
    """Score the conversation against expected outcomes."""
    final_response = history[-1]["content"]
    return {
        "relevance": compute_relevance(final_response, expected),
        "coherence": compute_coherence(history),
        "turn_count": len(history) // 2,
    }

@node(output_name="report")
def aggregate_metrics(scores: list[dict]) -> dict:
    """Aggregate scores across all test cases."""
    return {
        "avg_relevance": mean([s["relevance"] for s in scores]),
        "avg_coherence": mean([s["coherence"] for s in scores]),
        "avg_turns": mean([s["turn_count"] for s in scores]),
    }

# The evaluation pipeline — a DAG containing our cyclic conversation
evaluation = Graph([
    load_test_cases,
    conversation.as_node(),  # Cyclic graph as a single node
    score_responses,
    aggregate_metrics,
], name="evaluation")

# Run evaluation: the cyclic conversation runs inside the DAG
runner = AsyncRunner()
report = await runner.run(evaluation, {
    "dataset_path": "test_conversations.json",
    "query": "initial query",  # First query for each test case
    "history": [],
})
```

**The structure:**
```
Evaluation Pipeline (DAG)
├── load_test_cases
├── Conversation Loop (cyclic)  ← nested
│   └── RAG Pipeline (DAG)      ← nested within nested
├── score_responses
└── aggregate_metrics
```

**Same graph, different context.** In inference, `conversation` handles live users. In evaluation, it's a component being tested.

---

## Example 3: Prompt Optimization (Multiple Nesting Levels)

Context engineering and prompt optimization involve nested loops:

```
Outer loop: Human reviews results, provides feedback
└── Inner loop: Run variants, evaluate, select best
    └── Pipeline under test: The actual workflow being optimized
```

```python
# ─────────────────────────────────────────────────────────────
# The pipeline being optimized
# ─────────────────────────────────────────────────────────────

@node(output_name="response")
def generate_with_prompt(query: str, system_prompt: str) -> str:
    return llm.generate(system=system_prompt, user=query)

pipeline = Graph([generate_with_prompt], name="pipeline")
runner = SyncRunner()

# ─────────────────────────────────────────────────────────────
# Variant testing loop (cyclic) — tests multiple prompts
# ─────────────────────────────────────────────────────────────

@node(output_name="variants")
def generate_prompt_variants(base_prompt: str, feedback: str) -> list[str]:
    """Generate prompt variations based on feedback."""
    return prompt_generator.create_variants(base_prompt, feedback, n=5)

@node(output_name="results")
def test_variants(variants: list[str], test_queries: list[str]) -> list[dict]:
    """Test each variant on the test set."""
    results = []
    for variant in variants:
        scores = []
        for query in test_queries:
            response = runner.run(pipeline, {"query": query, "system_prompt": variant})
            scores.append(evaluate(response, query))
        results.append({"prompt": variant, "avg_score": mean(scores)})
    return results

@node(output_name="best_prompt")
def select_best(results: list[dict]) -> str:
    return max(results, key=lambda r: r["avg_score"])["prompt"]

@route(targets=["generate_variants", END])
def optimization_gate(best_prompt: str, target_score: float, results: list) -> str:
    best_score = max(r["avg_score"] for r in results)
    if best_score >= target_score:
        return END
    return "generate_variants"  # Keep optimizing

variant_tester = Graph([
    generate_prompt_variants,
    test_variants,
    select_best,
    optimization_gate,
], name="variant_tester")

# ─────────────────────────────────────────────────────────────
# Human-in-the-loop wrapper (cyclic) — gets human feedback
# ─────────────────────────────────────────────────────────────

@node(output_name="feedback")
def get_human_feedback(best_prompt: str, results: list) -> str:
    """Display results to human, get feedback for next iteration."""
    display_results(best_prompt, results)
    return input("Feedback (or 'done'): ")

@route(targets=["variant_tester", END])
def human_gate(feedback: str) -> str:
    if feedback.lower() == "done":
        return END
    return "variant_tester"

optimization_loop = Graph([
    variant_tester.as_node(),  # Cyclic graph as a node
    get_human_feedback,
    human_gate,
], name="optimization")
```

**Three levels of nesting:**
```
Human Optimization Loop (cyclic)
├── Variant Testing Loop (cyclic)
│   ├── generate_prompt_variants
│   ├── test_variants
│   │   └── Pipeline Under Test (DAG)  ← innermost
│   ├── select_best
│   └── optimization_gate
├── get_human_feedback
└── human_gate
```

---

## Think Singular, Scale with Map

Another dimension of hierarchy: **write logic for one item, scale to many**.

```python
# Write for ONE document
@node(output_name="features")
def extract_features(document: str) -> dict:
    return {
        "length": len(document),
        "entities": extract_entities(document),
        "sentiment": analyze_sentiment(document),
    }

pipeline = Graph([extract_features])

# Scale to 1000 documents
runner = SyncRunner()
results = runner.map(
    pipeline,
    {"document": documents},  # List of 1000 documents
    map_over="document",
)
# Returns: list of 1000 feature dicts
```

**Why this works:**
- No batch loops in your code
- Each function is testable with a single input
- The framework handles fan-out, parallelism, and caching

**This combines with hierarchical composition:**

```python
# Complex pipeline, still written for one item
analysis = Graph([
    preprocess,
    extract_features,
    classify,
    generate_summary,
], name="analysis")

# Use in batch processing
batch_pipeline = Graph([
    load_documents,
    analysis.as_node().map_over("document"),  # Fan out over documents
    aggregate_results,
])
```

---

## The `.as_node()` API

Convert any graph to a node:

```python
# Basic usage
graph_node = my_graph.as_node()

# With custom name
graph_node = my_graph.as_node(name="custom_name")

# With input/output renaming
graph_node = my_graph.as_node().with_inputs(old="new")

# With map_over for fan-out
graph_node = my_graph.as_node().map_over("items")
```

**Key properties:**
- The nested graph runs to completion before the outer graph continues
- Inputs and outputs are determined by the nested graph's `InputSpec`
- Type annotations flow through for `strict_types` validation

---

## When to Use Hierarchical Composition

| Use Case | Pattern |
|----------|---------|
| Reusable components | Build once, `.as_node()` everywhere |
| Testing complex flows | Test the inner graph independently |
| Evaluation harnesses | Wrap production graph in test DAG |
| Multi-agent systems | Each agent is a graph, orchestrator composes them |
| Prompt optimization | Nested loops for run → evaluate → improve |
| Batch processing | `.as_node().map_over(...)` for fan-out |

## What's Next?

- [Multi-Agent Orchestration](05-multi-agent.md) — Agent teams as composed graphs
- [Real-World: Evaluation Harness](../04-real-world/evaluation-harness.md) — Complete example
- [Real-World: Prompt Optimization](../04-real-world/prompt-optimization.md) — Complete example
