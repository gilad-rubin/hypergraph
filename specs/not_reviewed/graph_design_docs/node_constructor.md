# Node Constructor API

> **Status**: Design Document
> **Type**: Feature Specification

---

## Overview

The `Node()` constructor wraps any function as a node, using the same API as the `@node` decorator. This enables creating multiple distinct nodes from the same underlying function.

---

## API

```python
Node(func, output_name, cache=True, ...)
```

Same parameters as `@node` decorator.

---

## Examples

### Basic Usage

```python
def process(x: int) -> int:
    return x * 2

# Create multiple nodes from the same function
node_a = Node(func=process, output_name="result_a")
node_b = Node(func=process, output_name="result_b")
```

### With Renaming

```python
def transform(text: str) -> str:
    return text.upper()

# Same function, different public interfaces
uppercase_query = (
    Node(func=transform, output_name="result")
    .rename(inputs={"text": "raw_query"}, outputs={"result": "query"})
)

uppercase_title = (
    Node(func=transform, output_name="result")
    .rename(inputs={"text": "raw_title"}, outputs={"result": "title"})
)
```

### From Existing Node

```python
@node(output_name="original")
def compute(x: int) -> int:
    return x * 2

# Wrap the underlying function with different output name
alternate = Node(func=compute.func, output_name="alternate")
```

---

## Use Cases

| Scenario | Example |
|----------|---------|
| **Reuse logic** | Same transform function for different fields |
| **Multiple outputs** | Same computation feeding different parts of graph |
| **Testing** | Create isolated node instances for unit tests |

---

## Design Notes

- `Node()` constructor has identical signature to `@node` decorator
- Works with raw functions or `.func` from existing nodes
- Produces independent node instances (no shared state)
- Chainable with `.rename()` and `.map_over()`
