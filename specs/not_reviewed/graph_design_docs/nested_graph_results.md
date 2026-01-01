# Nested Graph Results

> **Status**: Design Document
> **Type**: Feature Specification

---

## Overview

When running a graph containing nested graphs, results are returned as nested `GraphResult` objects. All outputs at all depths are accessible by default.

---

## Graph Names Are Mandatory

Nested graphs must have names to be addressable in results:

```python
# Required for nested graphs
rag = Graph(nodes=[...], name="rag_pipeline")

# Or when using as_node
rag_node = inner_graph.as_node(name="rag_pipeline")
```

---

## Result Structure

```python
result = pipeline.run(inputs={...})

# Direct outputs (from output_name)
result["answer"]                      # value
result["cleaned"]                     # value

# Nested graphs (by name) -> GraphResult objects
result["rag_pipeline"]                # GraphResult
result["rag_pipeline"]["embedding"]   # value inside nested result
result["rag_pipeline"]["inner"]       # another nested GraphResult
```

Both `output_name` values and nested graph names share the same namespace:

```python
result.outputs = {
    "answer": "...",                  # from output_name
    "rag_pipeline": GraphResult(...), # nested graph by name
}
```

---

## Filtering with `select`

Use `select` to filter what's included in the result:

```python
# Default: everything accessible
result = pipeline.run(inputs={...})
result.keys()  # ["answer", "cleaned", "rag_pipeline", "other_graph"]

# Filtered: specific outputs only
result = pipeline.run(inputs={...}, select=["answer"])
result.keys()  # ["answer"]

# With patterns
result = pipeline.run(inputs={...}, select=["answer", "rag_pipeline/*"])
```

---

## Pattern Syntax

| Pattern | Meaning |
|---------|---------|
| `"answer"` | Specific output |
| `"rag_pipeline"` | Nested graph as GraphResult |
| `"rag_pipeline/*"` | All direct outputs from rag_pipeline |
| `"rag_pipeline/**"` | All outputs recursively |
| `"**/embedding"` | Any "embedding" at any depth |
| `"*/docs"` | "docs" from any direct child graph |

---

## Examples

```python
# Full access (default)
result = pipeline.run(inputs={...})
result["rag_pipeline"]["embedding"]  # accessible

# Only top-level values
result = pipeline.run(inputs={...}, select=["answer", "cleaned"])
result["rag_pipeline"]  # KeyError

# Specific nested outputs
result = pipeline.run(inputs={...}, select=["answer", "rag_pipeline/embedding"])
result["rag_pipeline"]["embedding"]  # accessible
result["rag_pipeline"]["docs"]       # KeyError

# Everything from a nested graph
result = pipeline.run(inputs={...}, select=["rag_pipeline/**"])
```

---

## GraphResult Structure

```python
@dataclass
class GraphResult:
    outputs: dict[str, Any | "GraphResult"]
    status: Literal["complete", "interrupted", "error"]
    history: list[NodeExecution] | None

    # Dict-like access
    def __getitem__(self, key: str) -> Any | "GraphResult":
        return self.outputs[key]

    def keys(self): ...
    def items(self): ...
    def __contains__(self, key: str): ...
```
