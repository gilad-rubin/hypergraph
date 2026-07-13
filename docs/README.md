# Hypergraph

A unified framework for Python workflow orchestration. DAG pipelines, agentic workflows, and everything in between.

- **Unified** - One framework for data pipelines and agentic AI. Same elegant code.
- **Hierarchical** - Graphs nest as nodes. Build big from small, tested pieces.
- **Versatile** - Sync, async, streaming. Branches, loops, human-in-the-loop. No limits.
- **Minimal** - Pure functions with named outputs. Edges inferred automatically.

## Quick Start

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

# Run the graph
runner = SyncRunner()
result = runner.run(graph, {"text": "RAG tutorial", "query": "What is RAG?"})
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

Your functions are just functions. Test them directly, with any test framework.

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

## Documentation Authority

Use current-behavior docs as evidence for shipped APIs; use intent docs only to
understand planned work.

| Location | Authority |
|---|---|
| `01-introduction/` through `06-api-reference/`, plus `08-hypertable/` | Canon for current user-facing behavior |
| `../examples/` | Canonical runnable patterns; examples must match the real API |
| `adr/` | Canonical durable decisions; later ADRs supersede earlier decisions explicitly |
| `changelog.md` | Historical record of shipped change sets |
| `prd/` and `07-design/roadmap.md` | Intent and planning, not evidence of current behavior |
| `research/` | Dated investigations and decision evidence; intent, not evidence of shipped behavior |

For background execution specifically, the
[task guide](05-how-to/control-background-execution.md),
[runner reference](06-api-reference/runners.md), and executable examples are
the current-behavior surfaces. ADRs 0003 and 0004 explain why that API has its
minimal shape.

For native execution inspection, start with
[Debug Workflows](05-how-to/debug-workflows.md), then use the
[runner reference](06-api-reference/runners.md) for exact signatures.

## Documentation

### Getting Started
- [What is Hypergraph?](01-introduction/what-is-hypergraph.md) - The problem, solution, and key differentiators
- [When to Use](01-introduction/when-to-use.md) - Is hypergraph right for your use case?
- [Quick Start](01-introduction/quick-start.md) - Get running in 5 minutes
- [Comparison](01-introduction/comparison.md) - How hypergraph compares to other frameworks

### Core Concepts
- [Getting Started](02-core-concepts/getting-started.md) - Core concepts, creating nodes, building graphs, running workflows

### Patterns
- [Simple Pipeline](03-patterns/01-simple-pipeline.md) - Linear DAGs, data transformations
- [Routing](03-patterns/02-routing.md) - Conditional routing with @ifelse and @route
- [Agentic Loops](03-patterns/03-agentic-loops.md) - Iterative refinement, multi-turn workflows
- [Hierarchical Composition](03-patterns/04-hierarchical.md) - Nest graphs, Think Singular Scale with Map
- [Multi-Agent](03-patterns/05-multi-agent.md) - Agent teams, orchestration patterns
- [Streaming](03-patterns/06-streaming.md) - Stream LLM responses token-by-token
- [Human-in-the-Loop](03-patterns/07-human-in-the-loop.md) - `@interrupt` decorator, pause/resume, and handler patterns
- [Caching](03-patterns/08-caching.md) - Skip redundant computation with in-memory or disk caching

### Real-World Examples
- [RAG Pipeline](04-real-world/rag-pipeline.md) - Single-pass retrieval-augmented generation
- [Multi-Turn RAG](04-real-world/multi-turn-rag.md) - Conversational RAG with follow-up questions
- [Evaluation Harness](04-real-world/evaluation-harness.md) - Test conversation systems at scale
- [Data Pipeline](04-real-world/data-pipeline.md) - Classic ETL without LLMs
- [Prompt Optimization](04-real-world/prompt-optimization.md) - Iterative prompt improvement with nested graphs

### How-To Guides
- [Batch Processing](05-how-to/batch-processing.md) - Process multiple inputs with runner.map()
- [Control Work After It Starts](05-how-to/control-background-execution.md) - Background handles, cooperative stop, and process recovery
- [Rename and Adapt](05-how-to/rename-and-adapt.md) - Reuse functions in different contexts
- [Integrate with LLMs](05-how-to/integrate-with-llms.md) - Patterns for OpenAI, Anthropic, and others
- [Test Without Framework](05-how-to/test-without-framework.md) - Test nodes as pure functions
- [Debug Workflows](05-how-to/debug-workflows.md) - Inspect current values and failures, or query durable history
- [Observe Execution](05-how-to/observe-execution.md) - Progress bars, custom event processors, and monitoring
- [Visualize Graphs](05-how-to/visualize-graphs.md) - Interactive graph visualization in notebooks and HTML

### API Reference
- [Graph](06-api-reference/graph.md) - Graph construction, validation, and properties
- [Nodes](06-api-reference/nodes.md) - FunctionNode, GraphNode, and HyperNode
- [Gates](06-api-reference/gates.md) - RouteNode, IfElseNode, @route, @ifelse
- [Runners](06-api-reference/runners.md) - SyncRunner, AsyncRunner, and execution model
- [Events](06-api-reference/events.md) - Event types, processors, and RichProgressProcessor
- [InputSpec](06-api-reference/inputspec.md) - Input categorization and requirements
- [Checkpointers](06-api-reference/checkpointers.md) - Persistence, resume, fork/retry lineage, and inspection

### HyperTable (Incremental Tables)
- [Overview](08-hypertable/overview.md) - What HyperTable is, when to use it
- [Getting Started](08-hypertable/getting-started.md) - Build your first table, incrementality, child tables, error handling
- [API Reference](08-hypertable/api-reference.md) - Complete method documentation
- [Implementing a TableStore](08-hypertable/implementing-a-store.md) - Back HyperTable with your own database; the store contract + conformance harness
- [Example: Document Processing](08-hypertable/examples/document-processing.md) - PDF ingestion with LLM enrichment
- [Example: Media Knowledge Base](08-hypertable/examples/media-knowledge-base.md) - Video/audio transcription and search

### Design
- [Guiding Principles](07-design/guiding-principles.md) - Design rubric and invariants
- [Philosophy](07-design/philosophy.md) - Why hypergraph exists and design principles
- [Inspiration](07-design/inspiration.md) - Frameworks that influenced hypergraph's design
- [Roadmap](07-design/roadmap.md) - What's implemented, what's coming next
- [Changelog](changelog.md) - Release history and changes

## Design Principles

1. **Pure functions** - Nodes are testable without the framework
2. **Automatic wiring** - Edges inferred from matching output/input names
3. **Composition over configuration** - Nest graphs, don't configure flags
4. **Unified execution** - Topo over SCCs, local iteration for feedback
5. **Fail fast** - Validate at build time, not runtime
6. **Explicit dependencies** - All inputs visible in function signatures

## Beyond AI/ML

While the examples focus on AI/ML use cases, hypergraph is a general-purpose workflow framework. It has no dependencies on LLMs, vector databases, or any AI tooling. Use it for any multi-step workflow: ETL pipelines, business process automation, testing harnesses, or anything else that benefits from graph-based orchestration.
