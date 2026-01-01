# Design Principles

## Core Philosophy

When implementing Hypernodes, follow these principles to make good judgment calls.

---

## 1. Pure Functions Over State Objects

**Principle**: Node functions must be testable without the framework.

```python
# ✅ GOOD - pure function, testable
@node(outputs="result")
def process(query: str, config: Config) -> str:
    return transform(query, config)

# Test without framework:
assert process.func("hello", my_config) == expected

# ❌ BAD - requires framework state
def process(state: GraphState) -> dict:
    query = state["query"]
    return {"result": transform(query)}
```

**Why**: This is THE key differentiator from LangGraph/Pydantic-Graph. Users write normal Python functions. The framework connects them.

---

## 2. Explicit Over Implicit

**Principle**: All dependencies visible in function signatures.

```python
# ✅ GOOD - dependencies explicit
@node(outputs="response")
def generate(messages: list, model: str, temperature: float) -> str:
    return llm.chat(messages, model=model, temperature=temperature)

# ❌ BAD - hidden dependency
@node(outputs="response")
def generate(messages: list) -> str:
    return llm.chat(messages, model=CONFIG.model)  # Where does CONFIG come from?
```

**Why**: Explicit dependencies make testing easy, debugging possible, and behavior predictable.

---

## 3. Fail Fast

**Principle**: Validate as early as possible. Build-time > Run-time > Never.

| Validation | When | Example |
|------------|------|---------|
| Build-time | `Graph()` construction | Route targets exist |
| Pre-execution | `runner.run()` before first node | No input conflicts |
| Runtime | During execution | Route returns valid value |

```python
# Build-time: catch immediately
graph = Graph(nodes=[...])  # ← Validates here, fails fast

# Not at runtime when you're debugging something else
result = runner.run(graph, inputs={...})  # ← Too late!
```

**Why**: Early failures are cheaper to debug. Don't let invalid graphs execute.


## 7. Unified Execution Model

**Principle**: Same algorithm handles DAGs, branches, AND cycles.

```python
# Don't have separate code paths
if graph.has_cycles:
    run_cyclic_mode(graph)
else:
    run_dag_mode(graph)

# Instead, one algorithm that handles all cases
def execute(graph, state):
    while not terminated(state):
        ready = compute_ready_set(graph, state)  # Works for all graph types
        state = execute_ready_nodes(ready, state)
    return state
```

**Why**: Multiple code paths mean multiple places for bugs. Unification forces you to find the right abstraction.

---

## 8. NetworkX for Graph Theory, Hypernodes for Execution

**Principle**: Don't reinvent graph algorithms.

```python
# ✅ GOOD - use NetworkX
import networkx as nx

def has_cycle(graph):
    return not nx.is_directed_acyclic_graph(graph.nx_graph)

def find_path(graph, source, target):
    return nx.shortest_path(graph.nx_graph, source, target)

# ❌ BAD - rolling our own
def has_cycle(graph):
    visited = set()
    def dfs(node, path):
        # ... 50 lines of cycle detection ...
```

**Why**: NetworkX is battle-tested. Our job is execution semantics, not graph theory.

---

## 9. Error Messages as Documentation

**Principle**: Every error message teaches the user something.

```python
# ✅ GOOD - explains, shows, suggests
raise GraphConfigError(
    "Two nodes create 'messages' at the same time\n\n"
    "  → add_user creates 'messages'\n"
    "  → add_assistant creates 'messages'\n\n"
    "The problem: Which value should we use?\n\n"
    "How to fix:\n"
    "  Option A: Rename one output\n"
    "  Option B: Add @branch to make them exclusive"
)

# ❌ BAD - cryptic
raise ValueError("Conflict: messages")
```

**Why**: Good errors reduce support burden and make users successful faster.

---

## 10. Composition Over Configuration

**Principle**: Build complex behavior by composing simple pieces.

```python
# ✅ GOOD - compose graphs
rag_graph = Graph(nodes=[retrieve, generate, refine])
outer = Graph(nodes=[
    preprocess,
    rag_graph.as_node(),  # Composition!
    postprocess,
])

# ❌ BAD - configure everything
graph = Graph(
    nodes=[...],
    mode="rag",
    refinement_enabled=True,
    preprocessing_steps=["clean", "tokenize"],
)
```

**Why**: Composition is more flexible and easier to test than configuration.

---

## Anti-Patterns to Avoid

| Anti-Pattern | Why Bad | Alternative |
|--------------|---------|-------------|
| Global registries | Hidden dependencies, testing nightmare | Explicit parameters |
| Magic method resolution | Can't reason about behavior | Explicit in signatures |
| Silent failures | Errors discovered too late | Fail fast with clear message |
| Framework-coupled functions | Can't reuse outside Hypernodes | Pure functions |
| Implicit reducers | Unclear append semantics | Return complete list |
| State mutation | Debugging nightmare | Immutable state |
| Inheritance hierarchies | Rigid, hard to change | Composition |

---

## Decision Framework

When implementing something new, ask:

1. **Is it testable without the framework?** If no, redesign.
2. **Is the failure mode clear?** If not, improve error message.
3. **Would NetworkX already do this?** If yes, use NetworkX.
4. **Does this add a configuration option?** Consider composition instead.
5. **Is this optimizing something?** Profile first, prove it matters.
6. **Can a new user understand the error?** If not, rewrite it.
