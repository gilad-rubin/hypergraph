# Hypergraph Documentation

A unified framework for Python workflow orchestration. DAG pipelines, agentic workflows, and everything in between.

## Core Idea: Automatic Edge Inference

Define functions. Name their outputs. Hypergraph connects them automatically. If node A produces "embedding" and node B takes "embedding" as input, they're connected. No manual wiring needed.

## Quick Start

```python
from hypergraph import Graph, node, SyncRunner

@node(output_name="embedding")
def embed(query: str) -> list[float]:
    return model.embed(query)

@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]:
    return db.search(embedding)

@node(output_name="answer")
def generate(docs: list[str], query: str) -> str:
    return llm.generate(docs, query)

# Edges inferred from names - no wiring needed
graph = Graph(nodes=[embed, retrieve, generate])

# Run the graph
runner = SyncRunner()
result = runner.run(graph, {"query": "What is RAG?"})
print(result["answer"])
```

## What's Implemented

**Working now:**
- `@node` decorator for wrapping functions (sync, async, generators)
- `Graph` construction with automatic edge inference
- `InputSpec` categorization (required, optional, bound, internal)
- Rename API (`.with_inputs()`, `.with_outputs()`, `.with_name()`)
- Hierarchical composition (`.as_node()`, `.map_over()`)
- Build-time validation with helpful error messages
- `SyncRunner` for sequential execution
- `AsyncRunner` with concurrency control (`max_concurrency`)
- Batch processing with `runner.map()` (zip and product modes)

**Coming soon:**
- Control flow (`@route`, `@branch`)
- Checkpointing and durability
- Event streaming (`.iter()`)
- Observability hooks
- `InterruptNode` for human-in-the-loop

## Documentation

- [Getting Started](getting-started.md) - Core concepts and creating your first node
- [Philosophy](philosophy.md) - Why hypergraph exists and design principles
- [API Reference: Nodes](api/nodes.md) - Complete FunctionNode, GraphNode, and HyperNode documentation
- [API Reference: Runners](api/runners.md) - SyncRunner, AsyncRunner, and execution model
- [Framework Comparison](comparison.md) - How hypergraph compares to LangGraph, Hamilton, and others

## Design Principles

1. **Automatic wiring** - Edges inferred from matching output/input names
2. **Pure functions** - Nodes are testable without the framework
3. **Composition over configuration** - Nest graphs, don't configure flags
4. **Unified execution** - Same algorithm for DAGs, branches, and loops
5. **Fail fast** - Validate at build time, not runtime
