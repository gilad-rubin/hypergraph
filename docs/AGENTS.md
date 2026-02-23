# Documentation Guidelines for Hypergraph

Instructions for writing and maintaining hypergraph documentation.

## Core Philosophy

### User-First, Problem-Focused

- Lead with **positioning and problems**, not features
- Users need to know *when* to use hypergraph and *why* it's different before they learn *how*
- Every page should answer: "What problem does this solve for me?"

### Progressive Complexity

- Start simple, add complexity gradually
- Each concept builds on the previous
- Pattern: simple pipeline → branching → agentic loops → hierarchical → multi-agent

### Task-Based Organization

- Organize guides by **user task** ("How do I..."), not framework feature
- Bad: "The with_inputs() method"
- Good: "How do I rename inputs to fit my naming convention?"

## Hypergraph's Key Differentiators

Reinforce these throughout the documentation:

1. **One framework to master** — Learn one tool that works for everything, from simple pipelines to complex agents
2. **Natural hierarchy** — Real AI workflows nest DAGs inside cycles and cycles inside DAGs (see examples below)
3. **Minimal** — No state schemas, no boilerplate (contrast with LangGraph's TypedDict)
4. **Automatic wiring** — Edges inferred from names (not manually wired)
5. **Pure functions** — Testable without the framework (always show `node.func(...)`)
6. **Build-time validation** — Catch errors at construction, not runtime

### The Natural Hierarchy of AI Workflows

This is the core insight: **DAGs and cycles naturally nest within each other**. Hypergraph's hierarchical composition makes this explicit and clean.

**Example 1: Multi-turn RAG**
- The conversation loop is cyclic (user asks → retrieve → generate → user follows up → loop)
- But the retrieval/RAG step inside each turn is a DAG (embed → search → rerank → generate)
- In hypergraph: the RAG DAG is a nested graph inside the conversation cycle

**Example 2: Evaluation**
- You build a multi-turn chat (cyclic graph with loops)
- To evaluate it, you run it against a dataset or interactive personas
- The evaluation is a DAG that contains your cyclic chat as a nested node
- Same graph, different context — inference vs evaluation

**Example 3: Prompt Optimization / Context Engineering**
- Inner loop: Run the workflow, evaluate results
- Outer loop: Receive human feedback, improve prompts/tools, repeat
- Multiple levels of nesting, mixing DAGs and cycles at each level

**The key message**: You don't build one graph and that's it. You build graphs, compose them, and reuse them in many different situations. Hierarchical composition isn't a nice-to-have — it's how real AI systems are structured.

### Think Singular, Scale with Map

Another core pattern: **write logic for one item, scale to many with composition and `.map()`**.

```
1. Think Singular  → Write a function that processes ONE document/query/item
2. Compose         → Build a graph from these functions, nest graphs as nodes
3. Scale           → Use .map() or .as_node(map_over=...) to fan out over collections
```

This means:
- No batch loops cluttering your code
- Each function is testable with a single input
- The framework handles fan-out, caching, and parallelism

**Example**: Process 1000 documents
- Write `embed(text) -> vector` for ONE document
- Build a graph: `embed → chunk → index`
- Scale: `runner.map(graph, {"text": documents}, map_over="text")`

The same pattern applies at every level of nesting. A graph that processes one conversation can be mapped over a dataset of conversations for evaluation.

## Writing Style

### Examples Over Explanations

- Show code first, explain after
- Every concept needs a runnable example
- Prefer real-world scenarios (RAG, ETL, agents) over abstract examples

### Production Patterns Early

- Don't bury production patterns in "advanced" sections
- Show error handling, edge cases, and real-world concerns throughout
- Users need to see how things work in practice, not just happy paths

### Keep Framework Comparisons in One Place

**Only mention other frameworks (LangGraph, Hamilton, Prefect, etc.) in the comparison section.**

Outside of `comparison.md` and `when-to-use.md`:
- Focus on what hypergraph does, not how it differs
- Let the reader discover the benefits without constant "unlike X" references
- The docs should stand on their own

Key framing: It's not "use hypergraph if you need both DAGs and agents" — it's "master one framework that handles the natural hierarchy of AI workflows."

## What to Avoid

- **Architecture-first**: Users want problems solved, not academic explanations
- **Feature enumeration**: Features alone don't help users decide
- **Toy-only examples**: Users need production patterns to understand real usage
- **Missing "when NOT to use"**: Filtering wrong users early improves satisfaction
- **Disconnected "advanced" section**: Integrate production patterns throughout

## Target Audiences

Consider users coming from:

| From | Familiar With | New to Them |
|------|---------------|-------------|
| Hamilton | Name-based edges | @route for cycles, .as_node() |
| LangGraph | Cycles, agents | No state schema, automatic wiring |
| Prefect | @task decorators | @node outputs, automatic edges |

## Documentation Success Criteria

A good doc page should enable:
- New user can run first example in <5 minutes
- User can describe when to use hypergraph vs alternatives
- User can build workflows without reading entire API reference
- User finds "how do I X" by task, not by feature hunting
- Clear progression from simple example → production deployment

## Sources

These guidelines are derived from analysis of documentation strategies in:
- Pydantic-Graphs (progressive complexity, "when NOT to use")
- Prefect (task-centric organization, recipes)
- LangGraph (concept-driven, production patterns)
- Hamilton (name-based inference, lineage focus)
- Mastra (use-case driven, integration story)

See `tmp/docs-ideas/` for detailed framework analysis.
