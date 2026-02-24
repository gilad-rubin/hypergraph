# InputSpec API Reference

**InputSpec** describes what inputs a graph needs and how they must be provided.

What counts as "required" depends on four dimensions that narrow the active subgraph:

| Dimension | Method | Narrows from | Effect on `required` |
|-----------|--------|-------------|---------------------|
| **Entrypoint** (start) | `with_entrypoint(...)` | The front | Excludes upstream nodes |
| **Select** (end) | `select(...)` | The back | Excludes nodes not needed for selected outputs |
| **Bind** (pre-fill) | `bind(...)` | Individual params | Moves params from required to optional |
| **Defaults** (fallback) | Function signatures | Individual params | Params with defaults are optional |

```python
from hypergraph import node, Graph

@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return [0.1, 0.2, 0.3]

@node(output_name="docs")
def retrieve(embedding: list[float], top_k: int = 5) -> list[str]:
    return ["doc1", "doc2"]

@node(output_name="answer")
def generate(docs: list[str], query: str) -> str:
    return f"Answer based on {len(docs)} docs"

g = Graph([embed, retrieve, generate])

# Full graph: all inputs
print(g.inputs.required)  # ('text', 'query')
print(g.inputs.optional)  # ('top_k',)

# Entrypoint narrows from the front
g2 = g.with_entrypoint("retrieve")
print(g2.inputs.required)  # ('embedding', 'query') - text no longer needed

# Select narrows from the back
g3 = g.select("docs")
print(g3.inputs.required)  # ('text',) - query no longer needed

# Bind pre-fills a value
g4 = g.bind(query="What is RAG?")
print(g4.inputs.required)  # ('text',)

# All four compose
configured = g.with_entrypoint("retrieve").select("answer").bind(top_k=10)
print(configured.inputs.required)  # ('embedding', 'query')
print(configured.inputs.optional)  # ('top_k',)
```

## The InputSpec Dataclass

InputSpec is a frozen dataclass with four fields:

```python
@dataclass(frozen=True)
class InputSpec:
    required: tuple[str, ...]
    optional: tuple[str, ...]
    entrypoints: dict[str, tuple[str, ...]]
    bound: dict[str, Any]
```

### `required: tuple[str, ...]`

Parameters that **must** be provided at runtime. They have no default value and aren't bound.

```python
@node(output_name="y")
def process(x: int) -> int:  # x has no default
    return x * 2

g = Graph([process])
print(g.inputs.required)  # ('x',)
```

### `optional: tuple[str, ...]`

Parameters that **can** be omitted at runtime. They have default values.

```python
@node(output_name="y")
def process(x: int, scale: int = 2) -> int:  # scale has default
    return x * scale

g = Graph([process])
print(g.inputs.required)  # ('x',)
print(g.inputs.optional)  # ('scale',)
```

### `entrypoints: dict[str, tuple[str, ...]]`

For cyclic graphs, entrypoints group cycle parameters by the node where execution can start. Each key is a node name, and the value is a tuple of parameters needed to enter the cycle at that node.

**Pick ONE entrypoint per cycle** — provide the parameters it needs, and the runner starts execution from there.

```python
@node(output_name="messages", emit="turn_done")
def accumulate(messages: list, response: str) -> list:
    return messages + [{"role": "assistant", "content": response}]

@node(output_name="response")
def generate(messages: list) -> str:
    return llm.chat(messages)

@route(targets=["generate", END], wait_for="turn_done")
def should_continue(messages: list) -> str:
    return END if len(messages) >= 10 else "generate"

g = Graph([generate, accumulate, should_continue])

print(g.inputs.entrypoints)
# {'accumulate': ('messages',), 'generate': ('messages',)}
# Pick ONE: provide messages to start at either node
```

For DAGs (no cycles), `entrypoints` is an empty dict `{}`.

**Ambiguity detection**: If your provided values match multiple entrypoints in the same cycle, the runner raises `ValueError`. Use `entrypoint=` on `runner.run()` to disambiguate.

### `bound: dict[str, Any]`

Parameters that are **pre-filled** with values via `bind()`.

```python
g = Graph([process])
print(g.inputs.bound)  # {}

bound = g.bind(x=5)
print(bound.inputs.bound)     # {'x': 5}
print(bound.inputs.required)  # () - x is no longer required
```

## How Categories Are Determined

Categorization happens in two phases:

**Phase 1: Scope narrowing.** Determine which nodes are active using entrypoints (forward-reachable) and select (backward-reachable). Only active nodes contribute parameters to InputSpec.

**Phase 2: Parameter classification.** For each parameter in the active subgraph, apply the "edge cancels default" rule:

| Condition | Category |
|-----------|----------|
| No incoming edge, no default, not bound | **required** |
| No incoming edge, has default OR is bound | **optional** |
| Has incoming edge from cycle | **entrypoint** (grouped by node) |
| Has incoming edge from another node | Not in InputSpec |
| Is bound via `bind()` | In `bound` dict, moves from required to optional |
| Node excluded by `with_entrypoint()` or `select()` | Not in InputSpec |

### Edge Cancels Default

When a parameter receives data from another node (has an incoming edge), it's not in InputSpec at all:

```python
@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="result")
def add(doubled: int) -> int:  # doubled comes from double node
    return doubled + 1

g = Graph([double, add])
print(g.inputs.required)  # ('x',) - only x
# 'doubled' is not in inputs - it comes from the double node
```

### Cycles Create Entry Points

When parameters participate in a cycle, they become entrypoint parameters grouped by which node consumes them:

```python
@node(output_name="state")
def update(state: dict, input: int) -> dict:
    return {**state, "value": input}

g = Graph([update])
print(g.inputs.required)      # ('input',)
print(g.inputs.entrypoints)  # {'update': ('state',)}
```

### Scope Narrowing (Entrypoint and Select)

`with_entrypoint()` and `select()` narrow which nodes are considered when computing InputSpec. Parameters from excluded nodes do not appear in `required`, `optional`, or `entrypoints`.

```python
from hypergraph import node, Graph

@node(output_name="a_val")
def step_a(x: int) -> int:
    return x * 2

@node(output_name="b_val")
def step_b(a_val: int, y: int) -> int:
    return a_val + y

@node(output_name="c_val")
def step_c(b_val: int) -> int:
    return b_val * 3

g = Graph([step_a, step_b, step_c])
print(g.inputs.required)  # ('x', 'y')

# Entrypoint: skip step_a, start at step_b
g2 = g.with_entrypoint("step_b")
print(g2.inputs.required)  # ('a_val', 'y') - a_val is now a user input

# Select: only need b_val, not c_val
g3 = g.select("b_val")
print(g3.inputs.required)  # ('x', 'y') - same, since step_a is still needed

# Both: start at step_b, only need b_val
g4 = g.with_entrypoint("step_b").select("b_val")
print(g4.inputs.required)  # ('a_val', 'y')
```

## Accessing InputSpec

### From a Graph

```python
g = Graph([double, add])
spec = g.inputs  # Returns InputSpec

print(spec.required)      # tuple of required param names
print(spec.optional)      # tuple of optional param names
print(spec.entrypoints)  # dict of cycle entrypoints
print(spec.bound)         # dict of bound values
```

### Getting All Input Names

Use the `all` property to get all input names:

```python
spec = g.inputs
print(spec.all)  # ('x', 'offset') - required + optional + entrypoint params
```

### Iteration

InputSpec is not directly iterable. Use `all` or access individual tuples:

```python
for param in g.inputs.required:
    print(f"Required: {param}")

for param in g.inputs.optional:
    print(f"Optional: {param}")
```

## Examples

### Simple Graph: Required and Optional

```python
from hypergraph import node, Graph

@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return [0.1, 0.2, 0.3]

@node(output_name="docs")
def retrieve(embedding: list[float], top_k: int = 5) -> list[str]:
    return ["doc1", "doc2"]

g = Graph([embed, retrieve])

print(g.inputs.required)      # ('text',)
print(g.inputs.optional)      # ('top_k',)
print(g.inputs.entrypoints)  # {}
print(g.inputs.bound)         # {}
```

### Bound Values

```python
# Bind a value
bound = g.bind(top_k=10)

print(bound.inputs.required)  # ('text',) - unchanged
print(bound.inputs.optional)  # ('top_k',) - still optional (has fallback)
print(bound.inputs.bound)     # {'top_k': 10}

# Bind another value
fully_bound = bound.bind(text="hello")

print(fully_bound.inputs.required)  # ()
print(fully_bound.inputs.bound)     # {'top_k': 10, 'text': 'hello'}
```

### Graph with Cycles (Entry Points)

```python
@node(output_name="messages")
def chat(messages: list[str], user_input: str) -> list[str]:
    return messages + [user_input]

g = Graph([chat])

print(g.inputs.required)      # ('user_input',)
print(g.inputs.entrypoints)  # {'chat': ('messages',)}
```

The `messages` parameter appears as an entrypoint because:
1. `chat` outputs `messages`
2. `chat` takes `messages` as input
3. This creates a cycle — provide `messages` to start at the `chat` node

### Multiple Nodes Sharing a Parameter

When multiple nodes use the same parameter name, defaults must be consistent:

```python
@node(output_name="x")
def a(value: int = 10) -> int:
    return value

@node(output_name="y")
def b(value: int = 10) -> int:  # Same default as 'a'
    return value * 2

g = Graph([a, b])
print(g.inputs.optional)  # ('value',) - appears once despite two nodes
```

If defaults don't match, Graph construction fails:

```python
@node(output_name="x")
def a(value: int = 10) -> int:
    return value

@node(output_name="y")
def b(value: int = 20) -> int:  # Different default!
    return value * 2

Graph([a, b])
# GraphConfigError: Inconsistent defaults for 'value'
```

## Design Decisions

### bind() accepts outputs — intentional bypass

`bind()` accepts both input parameter names and node output names. When you bind an output, you're providing a value that the producing node *would* have computed. If the producer can't run (because its own inputs are missing), your bound value is used instead.

This enables "run from anywhere" — provide an intermediate value and skip all upstream nodes:

```python
# embed(text) → embedding → retrieve(embedding) → docs
graph = Graph([embed, retrieve])

# Skip embed entirely — start from embedding
result = runner.run(graph, {"embedding": [1, 2, 3]})
```

Emit-only outputs (ordering signals from `emit=`) cannot be bound — they are internal coordination, not data.

### Value resolution order

When multiple sources provide the same value, the runner uses: **EDGE > PROVIDED > BOUND > DEFAULT**

- A node that CAN run WILL run, and its output overwrites any provided/bound value
- For non-conflicting internal overrides, `run(..., on_internal_override="ignore"|"warn"|"error")` controls policy (default: `"warn"`)
  - `map(...)` inherits the same policy and applies it per iteration
- Hard conflict rule: **either compute or inject, never both for the same node**
  - Injecting a node's outputs while also making that node runnable is rejected with `ValueError`
  - Partial injection of a multi-output producer is rejected when downstream still needs missing outputs
- Bound values bootstrap cycles: on the first iteration the bound value is used, then the cycle produces subsequent values

### Cycle entrypoints exclude certain parameters

Entry point computation intentionally excludes:

- **Bound parameters**: Already available, not needed from the user
- **Interrupt-produced parameters**: Produced by InterruptNode at runtime, not user-provided
- **Defaulted parameters**: Have fallback values, so the node can start without them

### Bypass preserves cycle semantics

When you provide a node's output, that node may be "bypassed" (its exclusive inputs aren't required). But cycle entrypoint parameters are excluded from bypass checks — providing a cycle entrypoint value means *bootstrapping the cycle*, not bypassing the producer node.

## Complete Example

```python
from hypergraph import node, Graph

# Nodes with various input types
@node(output_name="embedding")
def embed(text: str) -> list[float]:
    """Required input: text"""
    return [0.1, 0.2]

@node(output_name="results")
def search(embedding: list[float], top_k: int = 5, threshold: float = 0.8) -> list[str]:
    """Optional inputs: top_k, threshold"""
    return ["result1", "result2"]

@node(output_name="history")
def update_history(history: list[str], results: list[str]) -> list[str]:
    """Entry point input: history (forms cycle)"""
    return history + results

# Build graph
g = Graph([embed, search, update_history])

# Inspect InputSpec
print("Required:", g.inputs.required)        # ('text',)
print("Optional:", g.inputs.optional)        # ('top_k', 'threshold')
print("Entry points:", g.inputs.entrypoints)  # {'update_history': ('history',)}
print("Bound:", g.inputs.bound)              # {}
print("All:", g.inputs.all)                  # ('text', 'top_k', 'threshold', 'history')

# Bind some values
configured = g.bind(top_k=10, threshold=0.9)
print("\nAfter bind:")
print("Required:", configured.inputs.required)        # ('text',)
print("Optional:", configured.inputs.optional)        # ('top_k', 'threshold') - still have fallback
print("Entry points:", configured.inputs.entrypoints)  # {'update_history': ('history',)}
print("Bound:", configured.inputs.bound)              # {'top_k': 10, 'threshold': 0.9}
```
