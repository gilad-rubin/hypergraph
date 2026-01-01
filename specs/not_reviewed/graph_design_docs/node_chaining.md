# Node Configuration via Chaining

> **Status**: Design Document
> **Type**: Feature Specification

---

## Overview

Nodes are configured using a **Builder Pattern** (chaining). Each method in the chain refers to the *current public interface* of the node, eliminating ambiguity between internal and external names.

---

## The Chain Order

```
1. Creation   →  as_node() / Node()
2. Interface  →  .rename()      (define public names)
3. Behavior   →  .map_over()    (uses public names)
```

---

## Core Principle

**You always refer to names as they currently exist in the chain.**

```python
node = (
    inner.as_node(name="rag")
    .rename(inputs={"doc": "documents"})  # internal "doc" → public "documents"
    .map_over("documents")                # uses the NEW public name
)
```

---

## Examples

### Single Input → Batched List

```python
# Inner graph expects single "doc"
# Outer graph provides list of "documents"

node = (
    inner.as_node(name="rag")
    .rename(inputs={"doc": "documents"})
    .map_over("documents")
)
```

### Multiple Renames + Batch

```python
node = (
    calculator.as_node(name="calc")
    .rename(
        inputs={"a": "prices", "b": "taxes"},
        outputs={"result": "totals"}
    )
    .map_over(["prices", "taxes"])
)
```

### Rename Only (No Batching)

```python
node = (
    inner.as_node(name="rag")
    .rename(inputs={"q": "query"}, outputs={"resp": "answer"})
)
```

### No Configuration Needed

```python
node = inner.as_node(name="rag")  # all names pass through
```

---

## Name Resolution Flow

```python
# 1. inner.as_node(name="rag")
#    Public interface: ["doc", "q"]

# 2. .rename(inputs={"doc": "documents"})
#    Public interface: ["documents", "q"]
#    Internal wiring: "documents" → "doc"

# 3. .map_over("documents")
#    Looks up "documents" in public interface ✓
#    Resolves to internal "doc"
#    Sets: map_over=["doc"]
```

---

## API Reference

### `.rename(inputs={...}, outputs={...})`

Maps internal names to public names.

| Parameter | Type | Description |
|-----------|------|-------------|
| `inputs` | `dict[str, str]` | `{internal: public}` |
| `outputs` | `dict[str, str]` | `{internal: public}` |

### `.map_over(names, mode="zip")`

Configure internal batching over inputs.

| Parameter | Type | Description |
|-----------|------|-------------|
| `names` | `str \| list[str]` | Public input name(s) to batch over |
| `mode` | `str` | `"zip"` or `"product"` |

---

## Rules

1. **Immutable**: Each method returns a new node instance
2. **Context-aware**: Methods use current public interface
3. **Fail-fast**: Invalid names raise immediate errors

```python
# Error: 'docs' doesn't exist (it was renamed to 'documents')
node.map_over("docs")  # raises: Input 'docs' not found. Available: ['documents', 'q']
```
