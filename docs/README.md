# Hypergraph

A unified framework for Python workflow orchestration. DAG pipelines, agentic workflows, and everything in between.

- **Unified** - One framework for data pipelines and agentic AI. Same elegant code.
- **Hierarchical** - Graphs nest as nodes. Build big from small, tested pieces.
- **Versatile** - Sync, async, streaming. Branches, loops, human-in-the-loop. No limits.
- **Minimal** - No state schemas. No boilerplate. Just functions.

## Quick Start

Define functions. Name their outputs. Hypergraph connects them automatically.

```python
from hypergraph import Graph, node, SyncRunner

@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return model.embed(text)

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

`embed` produces `embedding`. `retrieve` takes `embedding`. Connected automatically.

## Why Hypergraph?

**Pure Functions Stay Pure**

```python
# Test without the framework
def test_embed():
    result = embed.func("hello")
    assert len(result) == 768
```

Your functions are just functions. No state objects to mock. No framework setup.

**Build-Time Validation**

```python
Graph([producer, consumer], strict_types=True)
# GraphConfigError: Type mismatch on edge 'producer' → 'consumer'
#   Output type: int
#   Input type:  str
```

Type mismatches, missing connections, invalid configurations - caught when you build the graph, not at runtime.

**Hierarchical Composition**

```python
# Inner graph: RAG pipeline
rag = Graph(nodes=[embed, retrieve, generate], name="rag")

# Outer graph: full workflow
workflow = Graph(nodes=[
    validate_input,
    rag.as_node(),      # Nested graph as a node
    format_output,
])
```

Test pieces independently. Reuse across workflows.

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
- `@route` for conditional routing with `END` sentinel
- `@ifelse` for binary boolean routing
- Cyclic graphs for agentic loops and multi-turn workflows

**Coming soon:**
- Checkpointing and durability
- Event streaming (`.iter()`)
- Observability hooks
- `InterruptNode` for human-in-the-loop

## Documentation

### Introduction
- [What is Hypergraph?](01-introduction/what-is-hypergraph.md) - The problem, solution, and key differentiators
- [When to Use](01-introduction/when-to-use.md) - Is hypergraph right for your use case?
- [Quick Start](01-introduction/quick-start.md) - Get running in 5 minutes
- [Comparison](01-introduction/comparison.md) - How hypergraph compares to other frameworks

### Core Concepts
- [Getting Started](02-core-concepts/getting-started.md) - Core concepts, creating nodes, building graphs, running workflows

### Patterns
- [Routing](03-patterns/02-routing.md) - Conditional routing, agentic loops
- [Hierarchical Composition](03-patterns/04-hierarchical.md) - Nest graphs, Think Singular Scale with Map

### API Reference
- [Graph](06-api-reference/graph.md) - Graph construction, validation, and properties
- [Nodes](06-api-reference/nodes.md) - FunctionNode, GraphNode, and HyperNode
- [Gates](06-api-reference/gates.md) - RouteNode, IfElseNode, @route, @ifelse
- [Runners](06-api-reference/runners.md) - SyncRunner, AsyncRunner, and execution model
- [InputSpec](06-api-reference/inputspec.md) - Input categorization and requirements

### Design
- [Philosophy](07-design/philosophy.md) - Why hypergraph exists and design principles

## Design Principles

1. **Pure functions** - Nodes are testable without the framework
2. **Automatic wiring** - Edges inferred from matching output/input names
3. **Composition over configuration** - Nest graphs, don't configure flags
4. **Unified execution** - Same algorithm for DAGs, branches, and loops
5. **Fail fast** - Validate at build time, not runtime
6. **Explicit dependencies** - All inputs visible in function signatures

## Beyond AI/ML

While the examples focus on AI/ML use cases, hypergraph is a general-purpose workflow framework. It has no dependencies on LLMs, vector databases, or any AI tooling. Use it for any multi-step workflow: ETL pipelines, business process automation, testing harnesses, or anything else that benefits from graph-based orchestration.
