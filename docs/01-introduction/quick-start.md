# Quick Start

Get up and running with hypergraph in 5 minutes.

## Installation

```bash
uv add git+https://github.com/gilad-rubin/hypergraph.git
# or
pip install git+https://github.com/gilad-rubin/hypergraph.git
```

## Your First Graph

### 1. Define Nodes

A node is a function wrapped with the `@node` decorator. Declare what it produces with `output_name`:

```python
from hypergraph import node

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="result")
def add_ten(doubled: int) -> int:
    return doubled + 10
```

### 2. Build a Graph

Pass nodes to `Graph`. Edges are inferred automatically from matching names:

```python
from hypergraph import Graph

# 'double' produces "doubled", 'add_ten' takes "doubled"
# → automatic edge: double → add_ten
graph = Graph([double, add_ten])
```

### 3. Run It

```python
from hypergraph import SyncRunner

runner = SyncRunner()
result = runner.run(graph, {"x": 5})

print(result["doubled"])  # 10
print(result["result"])   # 20
```

## Complete Example: RAG Pipeline

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
    context = "\n".join(docs)
    return f"Based on: {context}\nAnswer to: {query}"

# Build and run
graph = Graph([embed, retrieve, generate])
runner = SyncRunner()
result = runner.run(graph, {"text": "RAG tutorial", "query": "What is RAG?"})

print(result["answer"])
```

**How it connects:**
- `embed` produces `"embedding"`
- `retrieve` takes `embedding` as a parameter → edge created
- `retrieve` produces `"docs"`
- `generate` takes `docs` as a parameter → edge created
- `generate` also takes `query` → provided as input

## What's Next?

- [Core Concepts](../02-core-concepts/getting-started.md) — Deeper dive into nodes, graphs, and runners
- [Routing](../03-patterns/02-routing.md) — Add conditional logic and loops
- [Hierarchical Composition](../03-patterns/04-hierarchical.md) — Nest graphs for complex workflows
