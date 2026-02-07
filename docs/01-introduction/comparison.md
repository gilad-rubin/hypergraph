# Framework Comparison

How hypergraph compares to other Python workflow frameworks.

- **vs LangGraph/Pydantic-Graph** - No state schemas, pure functions, automatic edge inference
- **vs Hamilton/Pipefunc** - Same clean DAG model, plus cycles and agentic patterns
- **Best of both** - DAG simplicity meets agent flexibility

## Quick Comparison

| Feature | hypergraph | LangGraph | Hamilton | Pipefunc | Pydantic-Graph |
|---------|:---:|:---:|:---:|:---:|:---:|
| DAG Pipelines | ✓ | ✓ | ✓ | ✓ | ✓ |
| Agentic Loops | ✓ | ✓ | — | — | ✓ |
| Hierarchical | First-class | ✓ | ✓ | ✓ | ✓ |
| Human-in-the-Loop | ✓ | ✓ | — | — | ✓ |

## The Design Space

### DAG-First Frameworks

**Hamilton** and **Pipefunc** excel at data pipelines. Functions define nodes, edges are inferred from parameter names. Clean, testable, minimal boilerplate.

But they can't express cycles. Multi-turn conversations, agentic workflows, iterative refinement - none of these are possible when the framework fundamentally assumes DAG execution.

### Agent-First Frameworks

**LangGraph** and **Pydantic-Graph** were built for agents. They support cycles, conditional routing, and human-in-the-loop patterns.

But they require explicit state schemas. Every node reads from and writes to a shared state object. Functions become framework-coupled. Testing requires mocking state.

### Hypergraph: The Middle Path

Hypergraph takes the best of both:

- **From DAG frameworks**: Functions are pure. Edges are inferred. No state schema.
- **From agent frameworks**: Cycles, routing, and human-in-the-loop.

## Code Comparison: RAG Pipeline

The same RAG pipeline in each framework.

### Hypergraph

```python
from hypergraph import Graph, node

@node(output_name="embedding")
def embed(query: str) -> list[float]:
    return model.embed(query)

@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]:
    return db.search(embedding)

@node(output_name="answer")
def generate(docs: list[str], query: str) -> str:
    return llm.generate(docs, query)

graph = Graph(nodes=[embed, retrieve, generate])
```

**Lines of code**: 12
**Boilerplate**: None
**State schema**: None

### LangGraph

```python
from langgraph.graph import StateGraph
from typing import TypedDict

class State(TypedDict):
    query: str
    embedding: list[float]
    docs: list[str]
    answer: str

def embed(state: State) -> dict:
    return {"embedding": model.embed(state["query"])}

def retrieve(state: State) -> dict:
    return {"docs": db.search(state["embedding"])}

def generate(state: State) -> dict:
    return {"answer": llm.generate(state["docs"], state["query"])}

graph = StateGraph(State)
graph.add_node("embed", embed)
graph.add_node("retrieve", retrieve)
graph.add_node("generate", generate)
graph.add_edge("embed", "retrieve")
graph.add_edge("retrieve", "generate")
graph.set_entry_point("embed")
graph.set_finish_point("generate")
compiled = graph.compile()
```

**Lines of code**: 25
**Boilerplate**: State TypedDict, manual edges, entry/finish points
**State schema**: Required

### Hamilton

```python
from hamilton.function_modifiers import tag

def embedding(query: str) -> list[float]:
    return model.embed(query)

def docs(embedding: list[float]) -> list[str]:
    return db.search(embedding)

def answer(docs: list[str], query: str) -> str:
    return llm.generate(docs, query)

# Driver setup required
from hamilton import driver
dr = driver.Builder().with_modules(this_module).build()
result = dr.execute(["answer"], inputs={"query": "hello"})
```

**Lines of code**: 14
**Boilerplate**: Driver setup
**State schema**: None

### Pipefunc

```python
from pipefunc import pipefunc, Pipeline

@pipefunc(output_name="embedding")
def embed(query: str) -> list[float]:
    return model.embed(query)

@pipefunc(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]:
    return db.search(embedding)

@pipefunc(output_name="answer")
def generate(docs: list[str], query: str) -> str:
    return llm.generate(docs, query)

pipeline = Pipeline([embed, retrieve, generate])
```

**Lines of code**: 13
**Boilerplate**: None
**State schema**: None

## Code Comparison: Agentic Loop

A multi-turn conversation with iterative retrieval.

### Hypergraph

```python
from hypergraph import Graph, node, route, END

@node(output_name="response")
def generate(docs: list, messages: list) -> str:
    return llm.chat(docs, messages)

@node(output_name="messages")
def accumulate(messages: list, response: str) -> list:
    return messages + [{"role": "assistant", "content": response}]

@route(targets=["generate", END])
def should_continue(messages: list) -> str:
    if is_complete(messages):
        return END
    return "generate"

graph = Graph(nodes=[generate, accumulate, should_continue])
```

### LangGraph

```python
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated
from operator import add

class State(TypedDict):
    messages: Annotated[list, add]  # Reducer required
    docs: list
    response: str

def generate(state: State) -> dict:
    response = llm.chat(state["docs"], state["messages"])
    return {"response": response}

def accumulate(state: State) -> dict:
    return {"messages": [{"role": "assistant", "content": state["response"]}]}

def should_continue(state: State) -> str:
    if is_complete(state["messages"]):
        return END
    return "generate"

graph = StateGraph(State)
graph.add_node("generate", generate)
graph.add_node("accumulate", accumulate)
graph.add_conditional_edges("accumulate", should_continue)
graph.add_edge("generate", "accumulate")
graph.set_entry_point("generate")
compiled = graph.compile()
```

### Hamilton / Pipefunc

Hamilton and pipefunc are DAG frameworks — cycles are outside their scope. For iterative patterns, you'd handle the loop externally (e.g., a `while` loop calling the pipeline repeatedly).

## Key Differences

### State Model

| Framework | State Model |
|-----------|-------------|
| hypergraph | Edges inferred from names. No schema needed. |
| LangGraph | Explicit TypedDict with reducers for appends |
| Pydantic-Graph | Pydantic models with explicit read/write |
| Hamilton | Outputs flow forward, no shared state |
| Pipefunc | Outputs flow forward, no shared state |

### Function Portability

Can you test functions without the framework?

| Framework | Portability |
|-----------|-------------|
| hypergraph | `embed.func("hello")` - direct access |
| LangGraph | Functions take `State` dict - framework-coupled |
| Pydantic-Graph | Functions take context - framework-coupled |
| Hamilton | Pure functions - fully portable |
| Pipefunc | `embed.func("hello")` - direct access |

### Graph Construction

| Framework | Construction |
|-----------|--------------|
| hypergraph | Dynamic at runtime, validated at build time |
| LangGraph | Static class definition |
| Pydantic-Graph | Static class definition |
| Hamilton | Dynamic via driver |
| Pipefunc | Dynamic list of functions |

## When to Choose Each

### Choose hypergraph when

- You need both DAGs and agentic patterns
- You want minimal boilerplate
- Hierarchical composition is important
- You're building multi-agent systems

### Choose LangGraph when

- You're already in the LangChain ecosystem
- You need LangChain integrations
- You want a mature, production-tested solution

### Choose Hamilton when

- You're doing data engineering, feature engineering, or ML pipelines
- Lineage tracking and observability matter (Hamilton UI)
- You want a mature framework with years of production use at scale
- You need portability across execution environments (notebooks, Airflow, Spark)

### Choose Pipefunc when

- You're doing scientific computing, simulations, or parameter sweeps
- You need HPC/SLURM integration for cluster execution
- Low orchestration overhead matters for compute-intensive workloads
- You want n-dimensional map operations with adaptive scheduling

### Choose Pydantic-Graph when

- You want Pydantic integration
- Type validation at runtime is important
- You're building API-driven workflows

## Honest Tradeoffs

Hypergraph is younger than these alternatives. Tradeoffs to consider:

| Area | Status |
|------|--------|
| Maturity | Alpha - API may change |
| Production use | Limited testing at scale |
| Ecosystem | Smaller community |
| Integrations | Fewer pre-built connectors |
| Routing | ✓ (`@route`, `END`) |
| Caching | ✓ (in-memory and disk) |

If you need a battle-tested solution today, LangGraph or Hamilton may be safer choices. If you value the unified model and cleaner API, hypergraph is worth evaluating.

## Migration Path

### From Hamilton/Pipefunc

Minimal changes - the decorator pattern is similar:

```python
# Hamilton
def embedding(query: str) -> list[float]: ...

# Hypergraph
@node(output_name="embedding")
def embed(query: str) -> list[float]: ...
```

### From LangGraph

Bigger changes - remove state schema, refactor functions:

```python
# LangGraph
def generate(state: State) -> dict:
    return {"response": llm.chat(state["docs"], state["query"])}

# Hypergraph
@node(output_name="response")
def generate(docs: list, query: str) -> str:
    return llm.chat(docs, query)
```

The function becomes pure - takes inputs directly, returns output directly.
