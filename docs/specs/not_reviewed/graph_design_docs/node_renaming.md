# Node Renaming API

> **Status**: Design Document
> **Type**: Feature Specification

---

## Overview

The `.rename()` method changes the public interface of a node, mapping internal names to public (external) names.

**See also:** [Node Chaining](node_chaining.md) for the full builder pattern.

---

## API

```python
node.rename(inputs={"internal": "public"}, outputs={"internal": "public"})
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `inputs` | `dict[str, str]` | Map internal param names to public names |
| `outputs` | `dict[str, str]` | Map internal output names to public names |

Both parameters are optional.

---

## Direction Convention

```
{internal_name: public_name}
```

Read as: "Expose internal `x` as public `y`"

---

## Examples

### Regular Node

```python
@node(output_name="result")
def process(x: int) -> int:
    return x * 2

# Expose internal "x" as "query", internal "result" as "answer"
adapted = process.rename(
    inputs={"x": "query"},
    outputs={"result": "answer"}
)
```

### Graph as Node

```python
node = (
    inner.as_node(name="rag")
    .rename(
        inputs={"doc": "documents", "q": "query"},
        outputs={"resp": "answer"}
    )
)
```

### Chaining with map_over

```python
node = (
    inner.as_node(name="rag")
    .rename(inputs={"doc": "documents"})
    .map_over("documents")  # uses the NEW public name
)
```

---

## Design Notes

- **Immutable**: `.rename()` returns a new node instance
- **Chainable**: Works with `.map_over()` and other builder methods
- **Universal**: Works on all `HyperNode` types (Node, GraphNode, PipelineNode)
