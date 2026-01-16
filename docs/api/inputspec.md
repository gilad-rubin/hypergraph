# InputSpec API Reference

Complete API documentation for the InputSpec class.

## Overview

**InputSpec** describes the input parameters a graph needs. It categorizes parameters into four groups based on how they must be provided at runtime.

```python
from hypergraph import node, Graph

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="result")
def add(doubled: int, offset: int = 10) -> int:
    return doubled + offset

g = Graph([double, add])

# InputSpec categorizes parameters
print(g.inputs.required)  # ('x',) - must provide
print(g.inputs.optional)  # ('offset',) - has default
print(g.inputs.seeds)     # () - none in this graph
print(g.inputs.bound)     # {} - none bound
```

## The InputSpec NamedTuple

InputSpec is a frozen dataclass with four fields:

```python
@dataclass(frozen=True)
class InputSpec:
    required: tuple[str, ...]
    optional: tuple[str, ...]
    seeds: tuple[str, ...]
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

### `seeds: tuple[str, ...]`

Parameters that are part of a **cycle** and need initial values for the first iteration.

```python
@node(output_name="count")
def counter(count: int) -> int:  # count feeds back to itself
    return count + 1

g = Graph([counter])
print(g.inputs.seeds)  # ('count',) - cycle requires seed value
```

Seed parameters appear when a node's input is also its own output (or part of a multi-node cycle).

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

The categorization follows the "edge cancels default" rule:

| Condition | Category |
|-----------|----------|
| No incoming edge, no default, not bound | **required** |
| No incoming edge, has default OR is bound | **optional** |
| Has incoming edge from cycle | **seed** |
| Has incoming edge from another node | Not in InputSpec |
| Is bound via `bind()` | In `bound` dict, moves from required to optional |

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

### Cycles Create Seeds

When a parameter is part of a cycle, it becomes a seed:

```python
@node(output_name="state")
def update(state: dict, input: int) -> dict:
    return {**state, "value": input}

g = Graph([update])
print(g.inputs.required)  # ('input',)
print(g.inputs.seeds)     # ('state',) - state feeds back
```

## Accessing InputSpec

### From a Graph

```python
g = Graph([double, add])
spec = g.inputs  # Returns InputSpec

print(spec.required)  # tuple of required param names
print(spec.optional)  # tuple of optional param names
print(spec.seeds)     # tuple of seed param names
print(spec.bound)     # dict of bound values
```

### Getting All Input Names

Use the `all` property to get all input names:

```python
spec = g.inputs
print(spec.all)  # ('x', 'offset') - required + optional + seeds
```

Or combine tuples manually:

```python
all_inputs = spec.required + spec.optional + spec.seeds
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

print(g.inputs.required)  # ('text',)
print(g.inputs.optional)  # ('top_k',)
print(g.inputs.seeds)     # ()
print(g.inputs.bound)     # {}
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

### Graph with Cycles (Seeds)

```python
@node(output_name="messages")
def chat(messages: list[str], user_input: str) -> list[str]:
    return messages + [user_input]

g = Graph([chat])

print(g.inputs.required)  # ('user_input',)
print(g.inputs.seeds)     # ('messages',) - cycle: messages → chat → messages
```

The `messages` parameter is a seed because:
1. `chat` outputs `messages`
2. `chat` takes `messages` as input
3. This creates a cycle, requiring an initial value

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
    """Seed input: history (forms cycle)"""
    return history + results

# Build graph
g = Graph([embed, search, update_history])

# Inspect InputSpec
print("Required:", g.inputs.required)   # ('text',)
print("Optional:", g.inputs.optional)   # ('top_k', 'threshold')
print("Seeds:", g.inputs.seeds)         # ('history',)
print("Bound:", g.inputs.bound)         # {}
print("All:", g.inputs.all)             # ('text', 'top_k', 'threshold', 'history')

# Bind some values
configured = g.bind(top_k=10, threshold=0.9)
print("\nAfter bind:")
print("Required:", configured.inputs.required)  # ('text',)
print("Optional:", configured.inputs.optional)  # ('top_k', 'threshold') - still have fallback
print("Seeds:", configured.inputs.seeds)        # ('history',)
print("Bound:", configured.inputs.bound)        # {'top_k': 10, 'threshold': 0.9}
```
