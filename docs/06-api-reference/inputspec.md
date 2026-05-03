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

Reserved for compatibility. For configured graphs, this field is `{}`.

Cyclic graphs must be constructed with graph-level `entrypoint` configuration (`Graph(..., entrypoint=...)`). Cycle bootstrap parameters are represented directly in canonical `required`/`optional`.

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

### Cycles Require Entrypoint At Construction

```python
@node(output_name="state")
def update(state: dict, input: int) -> dict:
    return {**state, "value": input}

# Invalid: cycle with no constructor entrypoint
# Graph([update])  # GraphConfigError

g = Graph([update], entrypoint="update")
print(g.inputs.required)    # ('state', 'input')
print(g.inputs.entrypoints) # {}
```

### Nested Subgraph Inputs

When a graph contains nested `GraphNode`s, each subgraph is its own scope. An input name not declared at a graph's scope — meaning no leaf node consumes or produces it, and no nested `GraphNode` exposes it as an output — is **private** to its subgraph and surfaces in the outer `InputSpec` under a dot-path: `"<graphnode_name>.<input>"`.

This applies to both `inputs.required` and `inputs.bound`.

```python
from hypergraph import Graph, node

@node(output_name="result")
def inner_func(x: int) -> int:
    return x * 2

inner = Graph([inner_func], name="inner")
outer = Graph([inner.as_node()], name="outer")

print(outer.inputs.required)  # ('inner.x',)
```

Sibling subgraphs that share an input name stay independent — there is no merge:

```python
@node(output_name="out_a")
def use_a(x: int) -> int:
    return x + 1

@node(output_name="out_b")
def use_b(x: int) -> int:
    return x * 10

inner_a = Graph([use_a], name="A")
inner_b = Graph([use_b], name="B")
outer = Graph([inner_a.as_node(), inner_b.as_node()], name="outer")

print(outer.inputs.required)  # ('A.x', 'B.x')
```

Adding a leaf node at the outer scope that declares the same name links the two together — the inner `GraphNode`'s input auto-wires to the outer-scope name, and the dot-path goes away:

```python
@node(output_name="result")
def inner_func(x: int) -> int:
    return x * 2

@node(output_name="final")
def outer_func(result: int, x: int) -> int:  # outer also consumes 'x'
    return result + x

inner = Graph([inner_func], name="inner")
outer = Graph([inner.as_node(), outer_func], name="outer")

print(outer.inputs.required)  # ('x',) — outer 'x' feeds both leaves
```

A bound value on an inner subgraph surfaces under the same dot-path:

```python
inner = Graph([inner_func], name="inner").bind(x=5)
outer = Graph([inner.as_node()], name="outer")

print(outer.inputs.bound)     # {'inner.x': 5}
print(outer.inputs.required)  # ()
```

If a `bind` on an inner subgraph would be shadowed by a leaf at any ancestor scope, graph construction fails with `GraphConfigError` at build time. See [Graph.bind](graph.md) for the addressing forms accepted by `bind()`.

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
print(spec.entrypoints)  # {} (reserved for compatibility)
print(spec.bound)         # dict of bound values
```

### Getting All Input Names

Use the `all` property to get all input names:

```python
spec = g.inputs
print(spec.all)  # ('x',) - required + optional
```

### Iteration

InputSpec is not directly iterable. Use `all` or access individual tuples:

```python
for param in g.inputs.required:
    print(f"Required: {param}")

for param in g.inputs.optional:
    print(f"Optional: {param}")
```

## DAG Slicing Matrix

```python
from hypergraph import Graph, node

@node(output_name="a")
def step_a(x: int) -> int: return x * 2

@node(output_name="b")
def step_b(a: int, y: int) -> int: return a + y

@node(output_name="c")
def step_c(b: int, z: int = 10) -> int: return b * z

base = Graph([step_a, step_b, step_c])
```

- `base`: `required=('x', 'y')`, `optional=('z',)`
- `base.with_entrypoint("step_b")`: `required=('a', 'y')`, `optional=('z',)`
- `base.select("b")`: `required=('x', 'y')`, `optional=()`
- `base.with_entrypoint("step_b").select("b")`: `required=('a', 'y')`, `optional=()`

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

### Graph with Cycles

```python
@node(output_name="messages")
def chat(messages: list[str], user_input: str) -> list[str]:
    return messages + [user_input]

# Invalid (cycle without constructor entrypoint):
# Graph([chat])

g = Graph([chat], entrypoint="chat")
print(g.inputs.required)      # ('messages', 'user_input')
print(g.inputs.entrypoints)   # {}
```

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

### Canonical Scope

InputSpec is computed from graph-level scope only:

- `entrypoint` / `with_entrypoint(...)`
- `select(...)`
- `bind(...)`

Runtime scope switching (`run(..., select=...)`, `run(..., entrypoint=...)`) is not supported.

### Value Resolution Order

When multiple sources provide the same value, the runner uses: **EDGE > PROVIDED > BOUND > DEFAULT**

- Internal edge-produced parameters supplied at runtime are rejected deterministically (`ValueError`).

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
    return history + results

# Build graph (cycle -> constructor entrypoint required)
g = Graph([embed, search, update_history], entrypoint="update_history")

# Inspect InputSpec
print("Required:", g.inputs.required)        # ('text', 'history')
print("Optional:", g.inputs.optional)        # ('top_k', 'threshold')
print("Entry points:", g.inputs.entrypoints)  # {}
print("Bound:", g.inputs.bound)              # {}
print("All:", g.inputs.all)                  # ('text', 'history', 'top_k', 'threshold')

# Bind some values
configured = g.bind(top_k=10, threshold=0.9)
print("\nAfter bind:")
print("Required:", configured.inputs.required)        # ('text', 'history')
print("Optional:", configured.inputs.optional)        # ('top_k', 'threshold') - still have fallback
print("Entry points:", configured.inputs.entrypoints)  # {}
print("Bound:", configured.inputs.bound)              # {'top_k': 10, 'threshold': 0.9}
```
