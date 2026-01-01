# Node Configuration API

**How to create, rename, and configure nodes for composition and batching.**

---

## Quick Example

```python
from hypernodes import Graph, Node, node

# Example: Reuse the same function with different interfaces
def transform(text: str) -> str:
    return text.upper()

# Create multiple nodes from one function
uppercase_query = (
    Node(func=transform, outputs="result")
    .rename(inputs={"text": "raw_query"}, outputs={"result": "query"})
)

uppercase_title = (
    Node(func=transform, outputs="result")
    .rename(inputs={"text": "raw_title"}, outputs={"result": "title"})
)

# Use in graph
graph = Graph(nodes=[uppercase_query, uppercase_title, ...])
```

---

## Node() Constructor

### Purpose

The `Node()` constructor wraps any function as a node, using the same API as the `@node` decorator. This enables creating multiple distinct nodes from the same underlying function.

### Signature

```python
def Node(
    func: Callable,
    outputs: str | tuple[str, ...],
    cache: bool = True,
    name: str | None = None,
) -> HyperNode:
    """
    Wrap a function as a graph node.
    
    Args:
        func: The function to wrap
        outputs: Name(s) of output value(s)
        cache: Whether to cache this node's results (default: True)
        name: Optional custom node name (default: func.__name__)
    
    Returns:
        A HyperNode that can be used in Graph()
    """
```

### Basic Usage

```python
def process(x: int) -> int:
    return x * 2

# Create a node programmatically
node_a = Node(func=process, outputs="result_a")
node_b = Node(func=process, outputs="result_b")

graph = Graph(nodes=[node_a, node_b])
```

### From Existing Decorated Node

```python
@node(outputs="original")
def compute(x: int) -> int:
    return x * 2

# Access underlying function and create new node
alternate = Node(func=compute.func, outputs="alternate")
```

### Use Cases

| Scenario | Example |
|----------|---------|
| **Reuse logic** | Same transform function for different fields |
| **Multiple outputs** | Same computation feeding different parts of graph |
| **Testing** | Create isolated node instances for unit tests |
| **Dynamic graphs** | Build nodes in loops from runtime configuration |

---

## .rename() Method

### Purpose

The `.rename()` method changes the public interface of a node, mapping internal parameter names to public (external) names. This is essential when composing graphs where names don't naturally align.

### Signature

```python
def rename(
    self,
    inputs: dict[str, str] | None = None,
    outputs: dict[str, str] | None = None,
) -> Self:
    """
    Rename node's inputs and/or outputs.

    Args:
        inputs: Map current public names to new public names
        outputs: Map current public names to new public names

    Returns:
        New node instance with renamed interface (immutable).
        The `params` property never changes (original signature).
    """
```

### Direction Convention

```python
{current_public_name: new_public_name}
```

Read as: **"Rename current `x` to `y`"**

On first rename, `current_public_name` equals the original param name.
On subsequent renames, use the name from the previous rename.

### Examples

#### Rename Inputs

```python
@node(outputs="result")
def process(x: int) -> int:
    return x * 2

# Expose internal "x" as public "value"
adapted = process.rename(inputs={"x": "value"})

graph = Graph(nodes=[adapted])
runner.run(graph, inputs={"value": 5})  # Use public name
```

#### Rename Outputs

```python
@node(outputs="result")
def process(x: int) -> int:
    return x * 2

# Expose internal "result" as public "answer"
adapted = process.rename(outputs={"result": "answer"})

result = runner.run(graph, inputs={"x": 5})
print(result["answer"])  # Use public name
```

#### Rename Both

```python
adapted = process.rename(
    inputs={"x": "query"},
    outputs={"result": "answer"}
)
```

#### Nested Graph Interface

```python
# Inner graph has different names than outer graph expects
inner = Graph(nodes=[fetch, parse], name="preprocessing")

# Adapt interface to match outer graph
adapted = (
    inner.as_node()  # name already set in Graph()
    .rename(
        inputs={"raw_data": "input_docs"},
        outputs={"parsed": "documents"}
    )
)

outer = Graph(nodes=[load, adapted, analyze])

# Alternatively, provide name in as_node()
inner = Graph(nodes=[fetch, parse])
adapted = (
    inner.as_node(name="preprocessing")  # name required here
    .rename(
        inputs={"raw_data": "input_docs"},
        outputs={"parsed": "documents"}
    )
)
```

### Key Properties

- **Immutable**: `.rename()` returns a new node instance
- **Chainable**: Can be followed by `.map_over()` or other methods
- **Universal**: Works on all HyperNode types (Node, nested Graph, etc.)

---

## .map_over() Method

### Purpose

The `.map_over()` method configures a node to perform internal batch processing. This is the "map" in "think singular, scale with map" - you write nodes for single items, then mark which inputs should be batched.

### Signature

```python
def map_over(
    self,
    names: str | list[str],
    mode: Literal["zip", "product"] = "zip",
) -> HyperNode:
    """
    Configure node to batch process over one or more inputs.
    
    Args:
        names: Public input name(s) to batch over
        mode: How to combine multiple batched inputs
            - "zip": Parallel iteration (default)
            - "product": Cartesian product
    
    Returns:
        New node instance configured for batching (immutable)
    """
```

### Single Input Batching

```python
# Inner graph processes single document
@node(outputs="summary")
def summarize(doc: str) -> str:
    return llm.summarize(doc)

inner = Graph(nodes=[summarize])

# Outer graph provides list of documents
batched = (
    inner.as_node(name="summarizer")
    .rename(inputs={"doc": "documents"})  # Rename first
    .map_over("documents")                 # Then batch over public name
)

outer = Graph(nodes=[batched])

# Run with list
result = runner.run(outer, inputs={
    "documents": ["doc1", "doc2", "doc3"]
})
# result["summary"] is a list: ["summary1", "summary2", "summary3"]
```

### Multiple Input Batching

#### Zip Mode (Parallel)

```python
batched = (
    calculator.as_node(name="calc")
    .rename(inputs={"a": "prices", "b": "taxes"})
    .map_over(["prices", "taxes"], mode="zip")  # Parallel iteration
)

result = runner.run(graph, inputs={
    "prices": [10, 20, 30],
    "taxes": [1, 2, 3]
})
# Processes: (10, 1), (20, 2), (30, 3)
```

#### Product Mode (Cartesian)

```python
batched = (
    calculator.as_node(name="calc")
    .rename(inputs={"a": "values", "b": "multipliers"})
    .map_over(["values", "multipliers"], mode="product")
)

result = runner.run(graph, inputs={
    "values": [1, 2],
    "multipliers": [10, 100]
})
# Processes: (1, 10), (1, 100), (2, 10), (2, 100)
```

### Key Properties

- **References public names**: `.map_over()` uses names AFTER `.rename()`
- **Immutable**: Returns new node instance
- **Chainable**: Can follow `.rename()` in builder pattern

---

## Builder Pattern (Chaining)

### Core Principle

**You always refer to names as they currently exist in the chain.**

Each method in the chain operates on the node's *current* public interface, eliminating ambiguity between internal and external names.

### Chain Order

```
1. Creation   →  as_node() / Node()
2. Interface  →  .rename()      (define public names)
3. Behavior   →  .map_over()    (uses public names)
```

### Complete Example

```python
# Inner graph expects single "doc"
@node(outputs="embedding")
def embed(doc: str) -> list[float]:
    return model.embed(doc)

@node(outputs="summary")
def summarize(embedding: list[float]) -> str:
    return generate_summary(embedding)

# Option 1: Name in Graph constructor (recommended)
inner = Graph(nodes=[embed, summarize], name="doc_processor")

# Outer graph provides list of "documents"
# We want to process each document through inner graph
adapted = (
    inner.as_node()                            # Step 1: Wrap as node (name from Graph)
    .rename(inputs={"doc": "documents"})       # Step 2: Rename interface
    .map_over("documents")                     # Step 3: Batch over public name
)

outer = Graph(nodes=[load_docs, adapted, aggregate])

result = runner.run(outer, inputs={"source": "file.txt"})

# Option 2: Name in as_node() call
inner_unnamed = Graph(nodes=[embed, summarize])
adapted = (
    inner_unnamed.as_node(name="doc_processor")  # Name required here
    .rename(inputs={"doc": "documents"})
    .map_over("documents")
)
```

### Name Resolution Flow

```python
# Step 1: inner.as_node(name="rag")
#    params:  ("doc", "q")   ← Original, never changes
#    inputs:  ("doc", "q")   ← Public interface, same as params initially

# Step 2: .rename(inputs={"doc": "documents"})
#    params:  ("doc", "q")   ← Still unchanged
#    inputs:  ("documents", "q")   ← Public interface updated

# Step 3: .map_over("documents")
#    Looks up "documents" in inputs ✓
#    Configures batching on that public name
```

### Chained Renames

Each rename operates on the **current** public interface:

```python
node = graph.as_node(name="x")
# params: ("a", "b"), inputs: ("a", "b")

node = node.rename(inputs={"a": "alpha"})
# params: ("a", "b"), inputs: ("alpha", "b")

node = node.rename(inputs={"alpha": "first"})
# params: ("a", "b"), inputs: ("first", "b")

# NOT this:
node.rename(inputs={"a": "first"})  # ❌ Error: 'a' doesn't exist
```

### Validation

Invalid names raise immediate errors:

```python
node = inner.as_node(name="rag")
node = node.rename(inputs={"doc": "documents"})

# ❌ Error: 'doc' doesn't exist (it was renamed to 'documents')
node.map_over("doc")
# → Error: Input 'doc' not found. Available: ['documents', 'q']

# Collision detection:
node.rename(inputs={"a": "b", "c": "b"})  # ❌ Error: collision on 'b'
```

---

## Common Patterns

### Pattern 1: Adapt External Interface

```python
# Library provides graph with non-standard names
library_graph = get_pretrained_graph()  # Expects: raw_input, config

# Adapt to your naming convention
adapted = (
    library_graph.as_node(name="model")
    .rename(
        inputs={"raw_input": "text", "config": "model_config"},
        outputs={"prediction": "result"}
    )
)

my_graph = Graph(nodes=[preprocess, adapted, postprocess])
```

### Pattern 2: Reuse with Different Types

```python
# Same validation logic, different fields
def validate_format(value: str) -> bool:
    return bool(re.match(PATTERN, value))

validate_email = (
    Node(func=validate_format, outputs="is_valid")
    .rename(inputs={"value": "email"})
)

validate_phone = (
    Node(func=validate_format, outputs="is_valid")
    .rename(inputs={"value": "phone"})
)
```

### Pattern 3: Batch Processing Pipeline

```python
# Single-item pipeline
@node(outputs="cleaned")
def clean(text: str) -> str:
    return text.strip().lower()

@node(outputs="tokens")
def tokenize(cleaned: str) -> list[str]:
    return cleaned.split()

single_item = Graph(nodes=[clean, tokenize])

# Batch version
batch_processor = (
    single_item.as_node(name="processor")
    .rename(inputs={"text": "documents"})
    .map_over("documents")
)

# Processes list of documents, returns list of token lists
```

---

## API Summary

| Method | Purpose | Returns | Immutable |
|--------|---------|---------|-----------|
| `Node(func, outputs)` | Create node from function | `Node` | N/A |
| `Graph.as_node(...)` | Wrap graph as node | `GraphNode` | N/A |
| `.rename(inputs, outputs)` | Change public interface | `Self` | Yes |
| `.map_over(names, mode)` | Configure batching | `Self` | Yes |

**Key points:**
- All configuration methods are chainable and immutable (return new instance)
- `params` never changes (original signature for hashing/debugging)
- `inputs`/`outputs` reflect the current public interface
