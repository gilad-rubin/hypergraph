# What is Hypergraph?

**One framework for the full spectrum of Python workflows** — from batch data pipelines to multi-turn AI agents.

## The Idea

Data pipelines and agentic AI share more than you'd expect. Both are graphs of functions — the difference is whether the graph has cycles. Hypergraph gives you one framework that handles the full spectrum:


```text
┌─────────────────────────────────────────────────────────────────┐
│                        THE SPECTRUM                             │
│                                                                 │
│  Batch Pipelines    →    Branching    →    Agentic Loops       │
│  ────────────────────────────────────────────────────────────  │
│  ETL, ML inference       @ifelse          @route, END          │
│  (DAG)                   (conditional)    (cycles)             │
│                                                                 │
│  ─────────────── hypergraph handles all of it ────────────────  │
└─────────────────────────────────────────────────────────────────┘
```

## How It Works

Define functions. Name their outputs. Hypergraph connects them automatically.

```python
from hypergraph import Graph, node, SyncRunner

@node(output_name="embedding")
def embed(text: str) -> list[float]:
    # Your embedding model here
    return [0.1, 0.2, 0.3]

@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]:
    # Your vector search here
    return ["Document 1", "Document 2"]

@node(output_name="answer")
def generate(docs: list[str], query: str) -> str:
    # Your LLM here
    return f"Based on {len(docs)} docs: answer to {query}"

# Edges inferred from matching names
graph = Graph(nodes=[embed, retrieve, generate])

runner = SyncRunner()
result = runner.run(graph, {"text": "RAG tutorial", "query": "What is RAG?"})
print(result["answer"])
```

`embed` produces `embedding`. `retrieve` takes `embedding`. Connected automatically.

## Key Differentiators

### 1. One Framework to Master

Learn one tool that works for everything — from simple ETL to complex multi-agent systems. The same patterns and APIs apply across the entire spectrum.

### 2. Natural Hierarchy

Real AI workflows nest DAGs inside cycles and cycles inside DAGs:

- **Multi-turn RAG**: The conversation loop is cyclic, but retrieval inside each turn is a DAG
- **Evaluation**: Your cyclic chat becomes a node inside an evaluation DAG
- **Prompt optimization**: Run → Evaluate → Feedback → Improve, at multiple nesting levels

Hypergraph's hierarchical composition makes this explicit:

```python
# The chat is a cyclic graph
chat = Graph([retrieve, generate, accumulate, should_continue])

# Wrap it as a node for evaluation
eval_pipeline = Graph([
    load_test_cases,
    chat.as_node(),  # Cyclic graph as a single node
    score_responses,
    aggregate_metrics,
])
```

### 3. Just Functions

Define functions, name their outputs, and let hypergraph wire them together:

```python
@node(output_name="response")
def chatbot(messages: list) -> str:
    # Your LLM here
    return f"Response to: {messages[-1]}"

@node(output_name="history")
def accumulate(history: list, response: str, query: str) -> list:
    return history + [{"role": "user", "content": query},
                      {"role": "assistant", "content": response}]

# Edges inferred from parameter names
graph = Graph([chatbot, accumulate])
```

For a detailed comparison with other frameworks, see [Comparison](comparison.md).

### 4. Pure, Testable Functions

Your functions are just functions. Test them directly:

```python
# Test with any test framework — functions work standalone
def test_embed():
    result = embed.func("hello world")
    assert len(result) == 768
```

### 5. Build-Time Validation

Catch errors when you build the graph, not at 2am in production:

```python
@route(targets=["step_a", "step_b", END])
def decide(x: int) -> str:
    return "step_c"  # Typo

graph = Graph([decide, step_a, step_b])
# GraphConfigError: Route target 'step_c' not found.
# Valid targets: ['step_a', 'step_b', 'END']
# Did you mean 'step_a'?
```

### 6. Think Singular, Scale with Map

Write logic for one item. Scale to many with `.map()`:

```python
# Write for ONE document
@node(output_name="features")
def extract(document: str) -> dict:
    return analyze(document)

# Scale to 1000 documents
results = runner.map(graph, {"document": documents}, map_over="document")
```

The framework handles fan-out, parallelism, and caching.

## What's Next?

- [When to Use Hypergraph](when-to-use.md) — Is hypergraph right for your use case?
- [Quick Start](quick-start.md) — Run your first graph in 5 minutes
- [Comparison](comparison.md) — Detailed comparison with other frameworks
