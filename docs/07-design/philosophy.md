# Philosophy: Why Hypergraph Exists

Hypergraph is a graph-native execution system that supports DAGs, cycles, branches, and multi-turn interactions - all while maintaining pure, portable functions.

- **Pure functions** - Nodes are testable without the framework
- **Automatic wiring** - Edges inferred from matching names, no manual configuration
- **Unified execution** - Same model for DAGs, agents, and everything in between
- **Build-time validation** - Catch errors at construction, not runtime

---

## The Journey: From Hierarchical DAGs to Full Graph Support

### Where It Started: DAGs Done Right

Hypergraph began as an answer to existing DAG frameworks. The key innovation was **hierarchical composition** - pipelines are nodes that can be nested infinitely.

This enabled:
- Reusable pipeline components
- Modular testing (test small pipelines, compose into large ones)
- Visual hierarchy (expand/collapse nested pipelines)
- "Think singular, scale with map" - write for one item, map over collections

**DAGs remain a first-class citizen in hypergraph.** For ETL, batch processing, and single-pass ML inference, DAGs are the right model. Hypergraph executes them efficiently, with optional distributed execution via DaftRunner.

### Where DAGs Hit the Wall

The DAG constraint (no cycles) works beautifully for:
- ETL workflows
- Single-pass ML inference
- Batch data processing

But it **fundamentally breaks** for modern AI workflows:

| Use Case | Why DAGs Fail |
|----------|---------------|
| **Multi-turn RAG** | User asks, system retrieves and answers, user follows up, system needs to retrieve **more** and refine. Needs to loop back. |
| **Agentic workflows** | LLM decides next action, may need to retry/refine until satisfied |
| **Iterative refinement** | Generate, evaluate, if not good enough, generate again |
| **Conversational AI** | Maintain conversation state, allow user to steer at any point |

### The Inciting Incident

The breaking point was building a multi-turn RAG system where:

1. User asks a question
2. System retrieves documents and generates answer
3. User says "can you explain X in more detail?"
4. System needs to **retrieve more documents** using conversation context
5. System refines the answer

Step 4 is **impossible** in a DAG - you cannot loop back to retrieval. The entire architecture assumes single-pass execution.

---

## The Design Choice: Functions as Contracts

When it came time to support cycles, hypergraph doubled down on its core idea: **functions define their own contracts through parameters and named outputs.**

This means:
- **Inputs are parameters** — what a function needs is visible in its signature
- **Outputs are named** — what a function produces is declared with `output_name`
- **Edges are inferred** — matching names create connections automatically
- **Functions are portable** — they work standalone, testable without the framework

This extends naturally to cycles. A function that accumulates conversation history just takes `history` as a parameter and returns a new `history`. The graph handles iteration.

---

## The Core Insight: Automatic Edge Inference

In hypergraph, edges are inferred from matching names. **Name your outputs, and the framework connects them to matching inputs.**

Nodes define what flows through the system via their signatures:
- Input parameters declare what a node needs
- Output names declare what a node produces
- Edges are inferred from matching names - no manual wiring

Pure functions with clear contracts — the framework handles the wiring.

---

## Dynamic Graphs with Build-Time Validation

Hypergraph enables **fully dynamic graph construction** with validation at build time (when Graph() is called), not compile time.

This matters for AI applications where:
- Available tools may be discovered at runtime
- Graph structure depends on configuration
- Nodes are generated programmatically

### Why This Works in the AI Era

LLMs already work in a write-then-validate loop - they write code, then get compiler/runtime feedback to fix issues. **Build-time validation = compiler feedback.**

Both approaches catch errors before runtime. The difference is *when* validation happens (compile time vs build time), not *whether* it happens.

---

## Design Principles

### Automatic Edge Inference
Edges are inferred from matching output/input names. No manual wiring or edge configuration needed.

### Pure Functions
Nodes are pure functions. They take inputs, produce outputs, and have no side effects. This makes them testable without the framework.

### Explicit Over Implicit
Output names must be declared explicitly. Rename operations are explicit. No magic defaults or surprise behavior.

### Immutability
Nodes are immutable - `with_*` methods return new instances. Outputs flow forward. No retroactive state modifications.

### Build-Time Validation
Graphs are validated when constructed. Missing inputs, invalid routes, and type mismatches are caught before execution.

---

## What This Enables

**DAG workflows (where it started):**
- ETL and data pipelines
- Single-pass ML inference
- Batch processing with distributed execution (DaftRunner)
- Hierarchical composition - graphs as nodes

**Beyond DAGs (where it evolved):**
- Cycles - multi-turn conversational RAG, agentic loops
- Runtime conditional branches - routing based on LLM decisions
- Iterative refinement - generate, evaluate, retry until satisfied
- Human-in-the-loop - pause, get user input, resume
- Token-by-token streaming
- Event streaming for observability
- Checkpointing and crash recovery

---

## When to Use Hypergraph

**Ideal for:**
- Workflow automation - ETL, data pipelines, orchestration
- AI/ML pipelines - Multi-step LLM workflows, RAG systems
- Business processes - Multi-turn interactions, approvals, routing
- Observable systems - Full event stream, replay-able execution
- Durability required - Crash recovery, pause/resume, multi-turn

**Less ideal for:**
- Stateless microservices - API endpoints don't need graphs
- Simple scripts - Single functions don't need composition
- Real-time event streaming - Event streams, not batch workflows

---

## Inspiration & Acknowledgments

Hypergraph stands on the shoulders of giants:

### [Hamilton](https://github.com/DAGWorks-Inc/hamilton)

Hamilton pioneered the idea that a Python function's signature *is* the graph definition — inputs become edges, outputs flow downstream. Hypergraph's automatic edge inference is a direct descendant of this insight. Hamilton's team has built something genuinely elegant, and their focus on lineage, observability, and production-grade data pipelines continues to push the ecosystem forward.

### [Pipefunc](https://github.com/pipefunc/pipefunc)

Pipefunc inspired hypergraph's `.map()` operation and parallelization model — the idea that you write logic for one item and the framework handles fan-out. Much of the syntax for defining nodes and composing graphs was influenced by Pipefunc's clean, expressive API.

### [Kedro-Viz](https://github.com/kedro-org/kedro-viz)

Kedro-Viz showed what graph visualization could look like — interactive, collapsible, and hierarchical. The aspiration for hypergraph's visualization layer draws heavily from Kedro-Viz's approach to making complex pipelines understandable at a glance.

---

## Summary

Hypergraph started as a better DAG framework with hierarchical composition. It evolved to support cycles, runtime conditional branches, and multi-turn interactions when DAGs proved insufficient for modern AI workflows.

Rather than adopting the state-object pattern of existing agent frameworks, hypergraph kept its core insight: **automatic edge inference from matching names.** Define pure functions with clear inputs and outputs. Let the framework infer edges, validate at build time, and handle persistence automatically.

The mental model is simple: Nodes are pure functions. Outputs flow between them. DAGs execute in one pass. Cycles iterate until a termination condition. When a checkpointer is present, everything is saved for crash recovery. That's the whole architecture.
