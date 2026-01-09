# hypergraph v0.5 - Overview

**A graph-native execution system that supports cycles, multi-turn interactions, and complex control flow while maintaining pure, portable functions.**

***

## Quick Example: Multi-Turn RAG

Here's what using hypergraph looks like:

```python
from hypergraph import Graph, node, route, END, AsyncRunner, SyncRunner, DiskCache

# Define nodes as pure functions
@node(outputs="docs")
def retrieve(query: str, messages: list) -> list:
    """Retrieve documents from vector DB."""
    return vector_db.search(query, context=messages)

@node(outputs="response")
async def generate(docs: list, messages: list, llm) -> str:
    """Generate response using LLM."""
    async for chunk in llm.stream(docs, messages):
        yield chunk  # Framework handles streaming automatically

@node(outputs="messages")
def add_response(messages: list, response: str) -> list:
    """Accumulator: append assistant response to conversation."""
    return messages + [{"role": "assistant", "content": response}]

# Control flow with routing
@route(targets=["retrieve", END])  # Explicit targets, validated at build time
def should_continue(messages: list) -> str:
    """Decide whether to continue or end."""
    if len(messages) > 10 or detect_done(messages[-1]):
        return END
    return "retrieve"  # Loops back! Creates cycle

# Build graph - edges inferred from function signatures
graph = Graph(nodes=[retrieve, generate, add_response, should_continue])

# Execute with runner
runner = AsyncRunner(cache=DiskCache("./cache"))
result = await runner.run(
    graph,
    inputs={
        "query": "What is RAG?",
        "messages": [],  # Initialize the cycle
        "llm": my_llm
    }
)

print(result["messages"])  # Full conversation history
```

**Key features in this example:**

* ✅ Pure functions - testable without framework
* ✅ Cycles - `should_continue` loops back to `retrieve`
* ✅ No state objects - just function signatures
* ✅ Build-time validation - targets checked when `Graph()` is called
* ✅ Streaming - `async for` chunks handled automatically
* ✅ No infinite loops - staleness detection + explicit termination

***

## The Journey: From Hierarchical DAGs to Reactive Graphs

### Where It Started (v0.1-0.4)

hypergraph began as an answer to existing DAG frameworks like Hamilton and Pipefunc. The key innovation: **hierarchical composition** - pipelines are nodes that can be nested infinitely.

```python
# The original vision: pipelines as composable building blocks
inner = Pipeline(nodes=[clean, tokenize])
outer = Pipeline(nodes=[fetch, inner.as_node(), analyze])
```

This enabled:

* ✅ Reusable pipeline components
* ✅ Modular testing (test small pipelines, compose into large ones)
* ✅ Visual hierarchy (expand/collapse nested pipelines)
* ✅ "Think singular, scale with map" - write for one item, map over collections

### Where It Hit the Wall

The DAG constraint (no cycles) works beautifully for:

* ETL workflows
* Single-pass ML inference
* Batch data processing

But **fundamentally breaks** for modern AI workflows:

| Use Case                 | Why DAGs Fail                                                                                       |
| ------------------------ | --------------------------------------------------------------------------------------------------- |
| **Multi-turn RAG**       | User asks → retrieve → answer → _user follows up_ → retrieve **more** → refine (needs to loop back) |
| **Agentic workflows**    | LLM decides next action, may need to retry/refine until satisfied                                   |
| **Iterative refinement** | Generate → evaluate → if not good enough → generate again                                           |
| **Conversational AI**    | Maintain conversation state, allow user to steer at any point                                       |

### The Inciting Incident

Building a multi-turn RAG system where:

1. User asks a question
2. System retrieves documents and generates answer
3. User says "can you explain X in more detail?"
4. System needs to **retrieve more documents** using conversation context
5. System refines the answer

Step 4 is **impossible** in a DAG - can't loop back to retrieval. The entire architecture assumes single-pass execution.

### Why Not LangGraph or Pydantic-Graph?

Both solve cycles, but both require:

* Explicit state objects that functions must read from and write to
* Manual edge wiring
* Framework-coupled functions that are not portable
* Reducer annotations for append semantics
* Field names repeated in state class, reads, writes, and edges (not DRY)

**The frustration - we want to write this:**

```python
@node(outputs="messages")
def add_response(messages: list, response: str) -> list:
    return messages + [response]
```

**Not this:**

```python
def add_response(state: AgentState) -> dict:
    messages = state["messages"]  # Read from state
    response = state["response"]
    return {"messages": messages + [response]}  # Write to state
```

## Key Differentiators

| Aspect                   | LangGraph / Pydantic-Graph                    | hypergraph                                |
| ------------------------ | --------------------------------------------- | ----------------------------------------- |
| **State definition**     | Static `TypedDict` or Pydantic model required | No state class - just function signatures |
| **Graph construction**   | Edges defined at class definition time        | Build graphs dynamically at runtime       |
| **Validation timing**    | Compile time (static types)                   | Build time (`Graph()` construction)       |
| **Type hints**           | Mandatory                                     | Optional (opt-in for extra checks)        |
| **Function portability** | Framework-coupled                             | Pure functions, testable without imports  |

## The Solution: Dynamic Graphs with Build-Time Validation

hypergraph 0.5 introduces **fully dynamic graph construction** with validation at build time (when `Graph()` is called), not compile time.

```python
# LangGraph - static, tied to schema
class AgentState(TypedDict):
    messages: list[str]  # Must know fields at definition time
graph = StateGraph(AgentState)

# hypergraph - fully dynamic
nodes = [create_tool_node(t) for t in available_tools]  # Built at runtime!
graph = Graph(nodes=nodes)  # Validation happens here
```

### Why Implicit Edges by String Are Fine in the AI Era

LLMs already work in a write-then-validate loop - they write code, then get compiler/runtime feedback to fix issues. **Build-time validation = compiler feedback**.

```
Traditional: Write code → Compiler error → Fix → Repeat
hypergraph:  Write code → Graph() error → Fix → Repeat
```

Both catch errors before runtime. The difference is _when_ validation happens (compile time vs build time), not _whether_ it happens.

## API Surface at a Glance

### Defining Nodes

```python
from hypergraph import node, route, branch, InterruptNode, END

# Regular node
@node(outputs="result")
def process(x: int) -> int:
    return x * 2

# Multiple outputs
@node(outputs=("mean", "std"))
def statistics(data: list) -> tuple[float, float]:
    return (compute_mean(data), compute_std(data))

# Multi-way routing (gate)
@route(targets=["option_a", "option_b", END])  # Validated at build time
def decide(state: dict) -> str:
    if state["ready"]:
        return END
    return "option_a"

# Boolean routing (specialized gate)
@branch(when_true="valid_path", when_false="error_path")
def check(data: dict) -> bool:
    return data.get("valid", False)

# Human-in-the-loop pause point
approval = InterruptNode(
    name="approval",
    input_param="prompt",      # Value to surface to user
    response_param="decision"  # Where to write user's response
)
```

### Building Graphs

```python
from hypergraph import Graph

# Build graph - edges inferred from parameter names matching output names
graph = Graph(nodes=[retrieve, generate, decide, process])

# Bind default values (like functools.partial)
graph.bind(model="gpt-4", temperature=0.7)

# Nest graphs - name in Graph constructor (recommended)
inner = Graph(nodes=[clean, tokenize], name="preprocessing")
outer = Graph(nodes=[
    fetch,
    inner.as_node(),  # name already set
    analyze
])

# Or provide name in as_node()
inner_unnamed = Graph(nodes=[clean, tokenize])
outer = Graph(nodes=[
    fetch,
    inner_unnamed.as_node(name="preprocessing"),  # name required
    analyze
])

# Rename interfaces when nesting
rag = Graph(nodes=[retrieve, generate], name="rag")
adapted = (
    rag.as_node()  # name from Graph()
    .rename(inputs={"doc": "documents"}, outputs={"resp": "answer"})
)
```

### Executing Graphs

```python
from hypergraph import SyncRunner, AsyncRunner, DiskCache

# Synchronous execution
runner = SyncRunner(cache=DiskCache("./cache"))
result = runner.run(graph, inputs={"query": "hello"})

# Async execution (required for streaming and InterruptNode)
async_runner = AsyncRunner(cache=DiskCache("./cache"))
result = await async_runner.run(graph, inputs={"query": "hello"})

# Event streaming
async with async_runner.iter(graph, inputs={...}) as run:
    async for event in run:
        match event:
            case NodeEndEvent(node_name=name, outputs=outputs):
                print(f"{name} → {outputs}")
            case StreamingChunkEvent(chunk=chunk):
                print(chunk, end="")

# Batch execution
results = runner.map(
    graph,
    inputs={"query": ["Q1", "Q2", "Q3"]},
    map_over="query"
)
```

### Filtering Results

```python
# Get specific outputs only
result = runner.run(graph, inputs={...}, select=["answer"])

# Get nested graph outputs
result = runner.run(graph, inputs={...}, select=["rag_pipeline/*"])
```

***

## Core Architectural Changes from v0.4

1. **`Graph` replaces `Pipeline`** - Pure definition, constructed from list of nodes
2. **`SyncRunner` / `AsyncRunner`** - Execution separated from definition; runners own cache and callbacks
3. **Reactive dataflow with versioning** - Values have versions, staleness drives execution
4. **Unified execution algorithm** - Same code handles DAGs, branches, AND cycles

## What This Enables

* ✅ Multi-turn conversational RAG
* ✅ Agentic workflows with loops
* ✅ Retry patterns
* ✅ Iterative refinement
* ✅ Message accumulators that don't infinite loop
* ✅ Human-in-the-loop with pause/resume (`InterruptNode`)
* ✅ Token-by-token streaming (`.iter()` API)
* ✅ Event streaming for observability
* ✅ Checkpointing and resume
* ✅ Distributed batch processing (DaftRunner for DAG-only graphs)

## Next Steps

* [**Design Principles**](design-principles.md) - Philosophy for making good decisions
* [**API Reference**](../../../specs/not_reviewed/api/) - Detailed API specifications
* [**Architecture**](../../../specs/not_reviewed/architecture/) - How the system works internally
* [**Tests**](../../../specs/not_reviewed/tests/) - Test specifications and examples
