# Graph API Reference

A **Graph** defines a computation graph from nodes with automatic edge inference.

- **Automatic wiring** - Edges inferred from matching output/input names
- **Build-time validation (`strict_types`)** - Type mismatches caught at construction when `strict_types=True`
- **Hierarchical composition** - Graphs nest as nodes via `.as_node()`
- **Immutable** - `bind()`, `select()`, and other methods return new instances

```python
from hypergraph import node, Graph

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="result")
def add_one(doubled: int) -> int:
    return doubled + 1

# Edges inferred: double → add_one (via "doubled")
g = Graph([double, add_one])
```

Edges are inferred automatically: if node A produces output "x" and node B has input "x", an edge A→B is created.

## Constructor

### `Graph(nodes, *, name=None, strict_types=False)`

Create a graph from nodes.

```python
from hypergraph import node, Graph

@node(output_name="y")
def process(x: int) -> int:
    return x * 2

# Basic usage
g = Graph([process])

# With name (required for nesting)
g = Graph([process], name="processor")

# With type validation
g = Graph([process], strict_types=True)
```

**Args:**
- `nodes` (list[HyperNode]): List of nodes to include in the graph
- `name` (str | None): Optional graph name. Required if using `as_node()` for composition.
- `strict_types` (bool): If True, validate type compatibility between connected nodes at construction time. Default: False.

**Raises:**
- `GraphConfigError` - If duplicate node names exist
- `GraphConfigError` - If multiple nodes produce the same output name
- `GraphConfigError` - If `strict_types=True` and types are incompatible or missing

## Properties

### `name: str | None`

The graph's name. Used for identification and required for nesting via `as_node()`.

```python
g = Graph([process], name="my_graph")
print(g.name)  # "my_graph"

g2 = Graph([process])
print(g2.name)  # None
```

### `strict_types: bool`

Whether type validation is enabled for this graph.

```python
g = Graph([producer, consumer], strict_types=True)
print(g.strict_types)  # True
```

### `nodes: dict[str, HyperNode]`

Map of node name → node object. Returns a copy to prevent mutation.

```python
g = Graph([double, add_one])
print(list(g.nodes.keys()))  # ['double', 'add_one']
print(g.nodes['double'])     # FunctionNode('double')
```

### `nx_graph: nx.DiGraph`

The underlying NetworkX directed graph. Useful for advanced graph analysis.

```python
g = Graph([double, add_one])
print(g.nx_graph.edges())  # [('double', 'add_one')]
print(g.nx_graph.has_edge('double', 'add_one'))  # True
```

### `inputs: InputSpec`

Specification of graph input parameters. See [InputSpec Reference](inputspec.md) for details.

```python
g = Graph([double, add_one])
print(g.inputs.required)  # ('x',)
print(g.inputs.optional)  # ()
```

### `outputs: tuple[str, ...]`

All output names produced by nodes in the graph.

```python
g = Graph([double, add_one])
print(g.outputs)  # ('doubled', 'result')
```

### `leaf_outputs: tuple[str, ...]`

Outputs from terminal nodes (nodes with no downstream consumers).

```python
g = Graph([double, add_one])
print(g.leaf_outputs)  # ('result',) - only add_one is a leaf
```

### `selected: tuple[str, ...] | None`

Default output selection set via `select()`, or `None` if all outputs are returned.

```python
g = Graph([double, add_one])
print(g.selected)  # None

g2 = g.select("result")
print(g2.selected)  # ('result',)
```

### `has_cycles: bool`

True if the graph contains cycles.

```python
@node(output_name="x")
def feedback(x: int) -> int:
    return x + 1

g = Graph([feedback])
print(g.has_cycles)  # True - x feeds back to itself
```

### `has_async_nodes: bool`

True if any node in the graph is async.

```python
@node(output_name="result")
async def fetch(url: str) -> dict:
    return {}

g = Graph([fetch])
print(g.has_async_nodes)  # True
```

### `definition_hash: str`

Merkle-tree style hash of graph structure. Used for cache invalidation.

```python
g = Graph([double, add_one])
print(len(g.definition_hash))  # 64 (SHA256 hex string)
```

The hash includes:
- Node names and their definition hashes
- Graph edges (data dependencies)

The hash excludes:
- Bound values (runtime values, not structure)
- Node order in constructor list (normalized by name)

## Methods

### `bind(**values) -> Graph`

Pre-fill input parameters with values. Returns a new Graph (immutable pattern).

```python
g = Graph([double, add_one])
print(g.inputs.required)  # ('x',)

bound = g.bind(x=5)
print(bound.inputs.required)  # ()
print(bound.inputs.bound)     # {'x': 5}

# Original unchanged
print(g.inputs.required)  # ('x',)
```

**Args:**
- `**values`: Named values to bind

**Returns:** New Graph with bindings applied

**Raises:**
- `ValueError` - If binding a name that is an output of another node
- `ValueError` - If binding a name not in `graph.inputs.all`

### `unbind(*names) -> Graph`

Remove specific bindings. Returns a new Graph.

```python
bound = g.bind(x=5, y=10)
print(bound.inputs.bound)  # {'x': 5, 'y': 10}

partial = bound.unbind('x')
print(partial.inputs.bound)  # {'y': 10}
```

**Args:**
- `*names`: Names to unbind

**Returns:** New Graph with specified bindings removed

### `select(*names) -> Graph`

Set a default output selection. Returns a new Graph (immutable pattern).

This controls which outputs are returned by `runner.run()` and which outputs are exposed when the graph is used as a nested node via `as_node()`.

All nodes still execute and all intermediate values are still computed internally. `select` only filters what is **returned to the caller**.

```python
g = Graph([embed, retrieve, generate])
print(g.outputs)  # ('embedding', 'docs', 'answer')

# Only return "answer" by default
g_selected = g.select("answer")
result = runner.run(g_selected, {"text": "hello", "query": "what?"})
print(result.values.keys())  # dict_keys(['answer'])

# Runtime select= overrides graph default
result = runner.run(g_selected, inputs, select=["docs", "answer"])
print(result.values.keys())  # dict_keys(['docs', 'answer'])
```

**Args:**
- `*names`: Output names to include. Must be valid graph outputs.

**Returns:** New Graph with default selection set

**Raises:**
- `ValueError` - If any name is not in `graph.outputs`

#### Nested graph behavior

When a graph with `select` is used as a nested node, only the selected outputs are visible to the parent graph. Unselected outputs cannot be wired to downstream nodes.

```python
inner = Graph([embed, retrieve, generate], name="rag").select("answer")
gn = inner.as_node()
print(gn.outputs)  # ('answer',) — only "answer" is exposed

# Parent graph can only use "answer" from the nested graph
outer = Graph([gn, postprocess])  # postprocess must consume "answer", not "docs"
```

If the parent graph needs an intermediate output, add it to the selection:

```python
inner = Graph([embed, retrieve, generate], name="rag").select("answer", "docs")
```

### `as_node(*, name=None) -> GraphNode`

Wrap graph as a node for composition. Returns a new GraphNode.

```python
inner = Graph([double], name="doubler")
gn = inner.as_node()

# Use in outer graph
outer = Graph([gn, add_one])
```

**Args:**
- `name` (str | None): Node name. If not provided, uses `graph.name`.

**Returns:** GraphNode wrapping this graph

**Raises:**
- `ValueError` - If `name` is None and `graph.name` is None

See [GraphNode section in Nodes Reference](nodes.md#graphnode) for more details.

## Type Validation (strict_types)

When `strict_types=True`, the Graph validates type compatibility between connected nodes at construction time.

### How It Works

For each edge (source_node → target_node):
1. Get the output type from source node
2. Get the input type from target node
3. Check if output type is compatible with input type
4. Raise `GraphConfigError` if incompatible or missing

### Compatible Types

```python
@node(output_name="value")
def producer() -> int:
    return 42

@node(output_name="result")
def consumer(value: int) -> int:
    return value * 2

# Types match - construction succeeds
g = Graph([producer, consumer], strict_types=True)
```

### Type Mismatch Error

```python
@node(output_name="value")
def producer() -> int:
    return 42

@node(output_name="result")
def consumer(value: str) -> str:  # Expects str, gets int
    return value.upper()

Graph([producer, consumer], strict_types=True)
# GraphConfigError: Type mismatch between nodes
#   -> Node 'producer' output 'value' has type: int
#   -> Node 'consumer' input 'value' expects type: str
#
# How to fix:
#   Either change the type annotation on one of the nodes, or add a
#   conversion node between them.
```

### Missing Annotation Error

```python
@node(output_name="value")
def producer():  # Missing return type
    return 42

@node(output_name="result")
def consumer(value: int) -> int:
    return value * 2

Graph([producer, consumer], strict_types=True)
# GraphConfigError: Missing type annotation in strict_types mode
#   -> Node 'producer' output 'value' has no type annotation
#
# How to fix:
#   Add type annotation: def producer(...) -> ReturnType
```

### Union Type Compatibility

A more specific type satisfies a broader union type:

```python
@node(output_name="value")
def producer() -> int:
    return 42

@node(output_name="result")
def consumer(value: int | str) -> str:  # Accepts int OR str
    return str(value)

# Works! int is compatible with int | str
g = Graph([producer, consumer], strict_types=True)
```

### When to Use strict_types

- **Development**: Enable it to catch wiring mistakes early
- **Production**: Enable it for safety in critical pipelines
- **Prototyping**: Disable it (default) for quick experiments

```python
# Quick prototype
g = Graph([node1, node2])  # strict_types=False by default

# Production code
g = Graph([node1, node2], strict_types=True)
```

## GraphConfigError

Raised when graph configuration is invalid.

### Common Causes

**Duplicate node names:**
```python
@node(output_name="x")
def process(a: int) -> int: return a

Graph([process, process])
# GraphConfigError: Duplicate node name: 'process'
```

**Multiple nodes produce same output:**
```python
@node(output_name="result")
def a(x: int) -> int: return x

@node(output_name="result")
def b(x: int) -> int: return x

Graph([a, b])
# GraphConfigError: Multiple nodes produce 'result'
```

**Invalid identifiers:**
```python
process.with_name("123-invalid")
# Node names must be valid Python identifiers
```

**Inconsistent defaults:**
```python
@node(output_name="x")
def a(value: int = 10) -> int: return value

@node(output_name="y")
def b(value: int) -> int: return value  # No default!

Graph([a, b])
# GraphConfigError: Inconsistent defaults for 'value'
```

## Complete Example

```python
from hypergraph import node, Graph

# Define nodes
@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return [0.1, 0.2, 0.3]  # Simplified

@node(output_name="docs")
def retrieve(embedding: list[float], top_k: int = 5) -> list[str]:
    return ["doc1", "doc2"]  # Simplified

@node(output_name="answer")
def generate(docs: list[str], query: str) -> str:
    return f"Answer based on {len(docs)} docs"

# Build graph with type validation
g = Graph([embed, retrieve, generate], strict_types=True)

# Inspect graph
print(g.inputs.required)  # ('text', 'query')
print(g.inputs.optional)  # ('top_k',)
print(g.outputs)          # ('embedding', 'docs', 'answer')

# Bind default query
bound = g.bind(query="What is hypergraph?")
print(bound.inputs.required)  # ('text',)

# Create nested graph
g_named = Graph([embed, retrieve, generate], name="rag", strict_types=True)
rag_node = g_named.as_node()
print(rag_node.name)    # 'rag'
print(rag_node.inputs)  # ('text', 'top_k', 'query')
```
