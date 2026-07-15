# Node API Reference

Nodes are the building blocks of hypergraph. Wrap functions, compose graphs, adapt interfaces.

- **FunctionNode** - Wrap any Python function (sync, async, generator)
- **GraphNode** - Nest a graph as a node for hierarchical composition
- **HyperNode** - Abstract base class defining the common interface

## HyperNode

**HyperNode** is the abstract base class for all node types. It defines the minimal interface that all nodes share: name, inputs, outputs, and rename capabilities.

### Cannot Be Instantiated Directly

```python
from hypergraph import HyperNode

HyperNode()  # TypeError: HyperNode cannot be instantiated directly
```

Use `FunctionNode` (via `@node` decorator) or other concrete node types instead.

### Core Attributes

Every HyperNode has these attributes (set by subclass `__init__`):

```python
node.name: str                           # Public node name
node.inputs: tuple[str, ...]             # Input parameter names
node.outputs: tuple[str, ...]            # Output value names
node._rename_history: list[RenameEntry]  # Internal: tracks renames for error messages
```

### Public Methods

### `with_name(name: str) -> HyperNode`

Return a new node with a different name.

```python
from hypergraph import node

@node(output_name="result")
def process(x: int) -> int:
    return x * 2

renamed = process.with_name("preprocessor")
print(renamed.name)     # "preprocessor"
print(process.name)     # "process" (original unchanged)
```

**Returns:** New node instance (immutable pattern)

**Raises:** None (always succeeds)

### `rename_inputs(mapping=None, /, **kwargs) -> HyperNode`

Return a new node with renamed inputs.

```python
@node(output_name="result")
def process(text: str, config: dict) -> str:
    return text.upper()

# Using keyword args
adapted = process.rename_inputs(text="raw_input", config="settings")
print(adapted.inputs)  # ("raw_input", "settings")

# Using dict (for reserved keywords or dynamic renames)
adapted = process.rename_inputs({"text": "raw_input", "config": "class"})
```

**Args:**
- `mapping` (optional, positional-only): Dict of `{old_name: new_name}`
- `**kwargs`: Additional renames as keyword arguments

**Returns:** New node instance with updated inputs

**Raises:**
- `RenameError` - If any old name not found in current inputs
- `RenameError` - Includes helpful history if name was already renamed

**Compatibility:** `with_inputs(...)` remains available as an alias.

### `rename_outputs(mapping=None, /, **kwargs) -> HyperNode`

Return a new node with renamed outputs.

```python
@node(output_name="result")
def process(x: int) -> int:
    return x * 2

# Using keyword args
adapted = process.rename_outputs(result="doubled")
print(adapted.outputs)  # ("doubled",)

# Using dict
adapted = process.rename_outputs({"result": "doubled"})
```

**Args:**
- `mapping` (optional, positional-only): Dict of `{old_name: new_name}`
- `**kwargs`: Additional renames as keyword arguments

**Returns:** New node instance with updated outputs

**Raises:**
- `RenameError` - If any old name not found in current outputs
- `RenameError` - Includes helpful history if name was already renamed

**Compatibility:** `with_outputs(...)` remains available as an alias.

### Immutability Pattern

All rename/configuration methods return new instances. The original is never modified:

```python
original = process
v1 = original.with_name("v1")
v2 = original.with_name("v2")
v3 = v1.rename_inputs(x="input")

print(original.name)  # "process" (unchanged)
print(v1.name)        # "v1"
print(v2.name)        # "v2"
print(v3.name)        # "v1" (same as v1)
print(v3.inputs)      # ("input",) (renamed from v1)
```

### Type Checking

Use `isinstance()` to check node types:

```python
from hypergraph import HyperNode, FunctionNode

node = FunctionNode(lambda x: x)

isinstance(node, HyperNode)      # True
isinstance(node, FunctionNode)   # True
```

---

## FunctionNode

**FunctionNode** wraps a Python function as a graph node. Created via the `@node` decorator or `FunctionNode()` constructor directly.

### Constructor

```python
def __init__(
    self,
    source: Callable | FunctionNode,
    name: str | None = None,
    output_name: str | tuple[str, ...] | None = None,
    *,
    rename_inputs: dict[str, str] | None = None,
    cache: bool = False,
    hide: bool = False,
    emit: str | tuple[str, ...] | None = None,
    wait_for: str | tuple[str, ...] | None = None,
) -> None: ...
```

**Args:**

- `source` (required): Function to wrap, or existing FunctionNode (extracts underlying function)
- `name`: Public node name. Defaults to `source.__name__` if source is a function
- `output_name`: Name(s) for the output value(s). If None, node is side-effect only (outputs = ())
  - `str` - Single output (becomes 1-tuple)
  - `tuple[str, ...]` - Multiple outputs
  - `None` - Side-effect only, no outputs
- `rename_inputs`: Optional dict `{old_param: new_param}` for input renaming
- `cache`: Whether to cache results (default: False)
- `hide`: Whether to hide this node from visualization (default: False)
- `emit`: Ordering-only local output name(s). Auto-produced when the node runs
- `wait_for`: Ordering-only graph-scope output/emit address(es). Node waits until these values exist and are fresh

**Returns:** FunctionNode instance

**Raises:**
- `ValueError` - If function source cannot be retrieved (for definition_hash)
- `TypeError` - If the signature contains a positional-only, `*args`, or `**kwargs` parameter (see [Supported Parameter Kinds](#supported-parameter-kinds))
- `UserWarning` - If function has return annotation but no output_name provided

### Supported Parameter Kinds

Hypergraph invokes node functions with keyword arguments, so every parameter must be addressable by keyword. This applies to all function-backed node types: `@node`, `@route`, `@ifelse`, and `@interrupt`.

| Parameter kind | Example | Supported |
|----------------|---------|-----------|
| Regular | `def f(a)` | Yes |
| Keyword-only | `def f(*, kw)` | Yes |
| Positional-only | `def f(a, /)` | No — rejected at construction |
| Variadic positional | `def f(*args)` | No — rejected at construction |
| Variadic keyword | `def f(**kwargs)` | No — rejected at construction |

Unsupported kinds raise `TypeError` at construction time, naming the offending parameter:

```python
@node(output_name="result")
def bad(*args):
    ...
# TypeError: Function 'bad' has parameter(s) that cannot be called by keyword:
#   -> parameter 'args' is variadic positional (*args)
```

### Creating from a function

```python
from hypergraph import FunctionNode

def double(x: int) -> int:
    return x * 2

node = FunctionNode(double, name="double_value", output_name="result")

print(node.name)     # "double_value"
print(node.inputs)   # ("x",)
print(node.outputs)  # ("result",)
```

### Creating from existing FunctionNode

When source is a FunctionNode, only the underlying function is extracted. All other config is discarded:

```python
# Original node
original = FunctionNode(double, name="original_name", output_name="original_output")

# Creating fresh from existing FunctionNode
fresh = FunctionNode(original, name="new_name", output_name="new_output")

print(fresh.func is original.func)     # True (same function)
print(fresh.name)                      # "new_name" (new config)
print(fresh.outputs)                   # ("new_output",)
print(original.name)                   # "original_name" (unchanged)
```

### Properties

### `func: Callable`

The wrapped Python function. Read-only.

```python
@node(output_name="result")
def process(x: int) -> int:
    return x * 2

# Call directly
result = process.func(5)
print(result)  # 10

# Or use __call__
result = process(5)
print(result)  # 10
```

### `name: str`

Public node name (may differ from function name).

```python
@node(output_name="result")
def process(x: int) -> int:
    return x * 2

print(process.name)  # "process"

renamed = process.with_name("preprocessor")
print(renamed.name)  # "preprocessor"
```

### `inputs: tuple[str, ...]`

Input parameter names from function signature (after renaming).

```python
@node(output_name="result")
def add(x: int, y: int) -> int:
    return x + y

print(add.inputs)  # ("x", "y")

adapted = add.rename_inputs(x="a", y="b")
print(adapted.inputs)  # ("a", "b")
```

### `outputs: tuple[str, ...]`

Output value names (empty if no output_name).

```python
@node(output_name="result")
def process(x: int) -> int:
    return x * 2

print(process.outputs)  # ("result",)

# Multiple outputs
@node(output_name=("mean", "std"))
def stats(data: list) -> tuple:
    return ...

print(stats.outputs)  # ("mean", "std")

# Side-effect only
@node
def log(msg: str) -> None:
    print(msg)

print(log.outputs)  # ()
```

### `data_outputs: tuple[str, ...]`

Output names that carry data (excludes emit outputs). Same as `outputs` when no `emit` is set.

```python
@node(output_name="result", emit="done")
def producer(x: int) -> int:
    return x + 1

print(producer.outputs)       # ("result", "done")
print(producer.data_outputs)  # ("result",)
```

### `wait_for: tuple[str, ...]`

Ordering-only graph-scope output/emit addresses. Empty tuple when not set.

```python
@node(output_name="result", wait_for="signal")
def consumer(x: int) -> int:
    return x

print(consumer.wait_for)  # ("signal",)
```

### `cache: bool`

Whether results are cached (default: False). Set via constructor.

```python
@node(output_name="result", cache=True)
def expensive(x: int) -> int:
    return x ** 100

print(expensive.cache)  # True
```

### `definition_hash: str`

SHA256 hash of function source code (64-character hex string). Computed at node creation.

Used for cache invalidation - if function source changes, hash changes, and cached results are invalidated.

```python
@node(output_name="result")
def process(x: int) -> int:
    return x * 2

hash_val = process.definition_hash
print(len(hash_val))  # 64
print(hash_val)       # "a3f5f6d7e8c9b0a1..." (example)
```

**Raises ValueError** if source cannot be retrieved (built-ins, C extensions):

```python
# This will raise ValueError
import os
node = FunctionNode(os.path.exists, output_name="exists")
# ValueError: Cannot hash function exists: could not get source code
```

### `is_async: bool`

True if function is async or async generator. Read-only, auto-detected.

```python
# Sync function
@node(output_name="result")
def sync_func(x: int) -> int:
    return x * 2

print(sync_func.is_async)  # False

# Async function
@node(output_name="result")
async def async_func(x: int) -> int:
    return x * 2

print(async_func.is_async)  # True

# Async generator
@node(output_name="items")
async def async_gen(n: int):
    for i in range(n):
        yield i

print(async_gen.is_async)  # True
```

### `is_generator: bool`

True if function yields values (sync or async generator). Read-only, auto-detected.

```python
# Sync function
@node(output_name="result")
def sync_func(x: int) -> int:
    return x * 2

print(sync_func.is_generator)  # False

# Sync generator
@node(output_name="items")
def sync_gen(n: int):
    for i in range(n):
        yield i

print(sync_gen.is_generator)  # True

# Async generator
@node(output_name="items")
async def async_gen(n: int):
    for i in range(n):
        yield i

print(async_gen.is_generator)  # True
```

### Special Methods

### \_\_call\_\_(\*args, \*\*kwargs)

Call the wrapped function directly. Delegates to `self.func(*args, **kwargs)`.

```python
@node(output_name="result")
def double(x: int) -> int:
    return x * 2

# Both equivalent
result1 = double(5)
result2 = double.func(5)

print(result1)  # 10
print(result2)  # 10
```

### \_\_repr\_\_() -> str

Informative string representation showing function name and node config.

```python
@node(output_name="result")
def process(x: int) -> int:
    return x * 2

print(repr(process))
# FunctionNode(process, outputs=('result',))

renamed = process.with_name("preprocessor")
print(repr(renamed))
# FunctionNode(process as 'preprocessor', outputs=('result',))
```

---

## @node Decorator

```python
@node
def foo(x): ...

# or

@node(output_name="result", cache=True)
def foo(x): ...
```

Decorator to wrap a function as a FunctionNode. Can be used with or without parentheses.

### Signature

```python
def node(
    source: Callable | None = None,
    output_name: str | tuple[str, ...] | None = None,
    *,
    rename_inputs: dict[str, str] | None = None,
    cache: bool = False,
    hide: bool = False,
    emit: str | tuple[str, ...] | None = None,
    wait_for: str | tuple[str, ...] | None = None,
) -> FunctionNode | Callable[[Callable], FunctionNode]: ...
```

**Args:**
- `source`: The function (when used without parens like `@node`)
- `output_name`: Output name(s). If None, side-effect only (outputs = ())
- `rename_inputs`: Optional dict to rename inputs
- `cache`: Enable result caching for this node. Requires a cache backend on the runner. See [Caching](../03-patterns/08-caching.md). Not allowed on GraphNode
- `hide`: Whether to hide this node from visualization (default: False)
- `emit`: Ordering-only local output name(s). Auto-produced when the node runs. Used with `wait_for` to enforce execution order without data dependency. See [Ordering](../03-patterns/03-agentic-loops.md#ordering-with-emitwait_for)
- `wait_for`: Ordering-only graph-scope output/emit address(es). Node won't run until these values exist and are fresh. Must reference an `emit` or `output_name` of another node at the current graph scope

**Returns:**
- FunctionNode if source provided (decorator without parens)
- Decorator function if source is None (decorator with parens)

### Usage Without Parentheses

```python
@node
def double(x: int) -> int:
    return x * 2

print(double.name)     # "double"
print(double.outputs)  # ()  ← Side-effect only! Warning emitted.
```

The decorator always uses `func.__name__` for the node name. To customize, use FunctionNode directly.

### Usage With Parentheses

```python
@node(output_name="result")
def double(x: int) -> int:
    return x * 2

print(double.name)     # "double"
print(double.outputs)  # ("result",)
```

### With All Parameters

```python
@node(
    output_name="result",
    cache=True,
)
def expensive_operation(x: int) -> int:
    return x ** 100

print(expensive_operation.name)   # "expensive_operation"
print(expensive_operation.cache)  # True
```

### Warning on Missing output_name

If your function has a return type annotation but no output_name, a warning is emitted:

```python
@node  # Missing output_name!
def fetch(url: str) -> dict:
    return requests.get(url).json()

# UserWarning: Function 'fetch' has return type '<class 'dict'>' but no output_name.
# If you want to capture the return value, use @node(output_name='...').
# Otherwise, ignore this warning for side-effect only nodes.
```

This helps catch accidental omissions. If the function is truly side-effect only, add type hints:

```python
from typing import NoReturn

@node
def log(msg: str) -> None:  # Explicitly None → no warning
    print(msg)

# or

@node
def log(msg: str):  # No return annotation → no warning
    print(msg)
```

---

## RenameError

Exception raised when a rename operation references a non-existent name.

```python
from hypergraph import RenameError, node

@node(output_name="result")
def process(x: int) -> int:
    return x * 2

try:
    process.rename_inputs(y="renamed")  # 'y' doesn't exist
except RenameError as e:
    print(e)
    # 'y' not found. Current inputs: ('x',)
```

### Error Messages Include History

When a name was previously renamed, the error message helps you understand what happened:

```python
@node(output_name="result")
def process(x: int) -> int:
    return x * 2

# Rename x to input
renamed = process.rename_inputs(x="input")

# Try to use old name
try:
    renamed.rename_inputs(x="something_else")
except RenameError as e:
    print(e)
    # 'x' was renamed to 'input'. Current inputs: ('input',)
```

### Exception Details

- Type: `Exception`
- Module: `hypergraph.nodes._rename`
- Public export: `from hypergraph import RenameError`

---

## Execution Modes

FunctionNode supports four execution modes, auto-detected from the function signature:

### 1. Synchronous Function

```python
@node(output_name="result")
def sync(x: int) -> int:
    return x * 2

print(sync.is_async)      # False
print(sync.is_generator)  # False
```

### 2. Asynchronous Function

```python
@node(output_name="data")
async def async_func(url: str) -> dict:
    async with client.get(url) as resp:
        return await resp.json()

print(async_func.is_async)      # True
print(async_func.is_generator)  # False
```

### 3. Synchronous Generator

```python
from typing import Iterator

@node(output_name="chunks")
def sync_gen(text: str, size: int = 100) -> Iterator[str]:
    for i in range(0, len(text), size):
        yield text[i:i+size]

print(sync_gen.is_async)      # False
print(sync_gen.is_generator)  # True
```

### 4. Asynchronous Generator

```python
from typing import AsyncIterator

@node(output_name="tokens")
async def async_gen(prompt: str) -> AsyncIterator[str]:
    async for chunk in llm.stream(prompt):
        yield chunk.text

print(async_gen.is_async)      # True
print(async_gen.is_generator)  # True
```

---

## Complete Example

Combining all features:

```python
from hypergraph import node, FunctionNode

# Define a function
def calculate(x: int, y: int) -> tuple[int, int]:
    return x + y, x * y

# Create node with full config
node_instance = FunctionNode(
    source=calculate,
    name="arithmetic",
    output_name=("sum", "product"),
    rename_inputs={"x": "first", "y": "second"},
    cache=True,
)

# Access properties
print(node_instance.name)           # "arithmetic"
print(node_instance.inputs)         # ("first", "second")
print(node_instance.outputs)        # ("sum", "product")
print(node_instance.cache)          # True
print(node_instance.is_async)       # False
print(node_instance.is_generator)   # False

# Transform with fluent API
adapted = (
    node_instance
    .with_name("math_ops")
    .rename_inputs(first="a", second="b")
    .rename_outputs(sum="total", product="multiply")
)

print(adapted.name)     # "math_ops"
print(adapted.inputs)   # ("a", "b")
print(adapted.outputs)  # ("total", "multiply")

# Call the function
result = node_instance(5, 3)
print(result)           # (8, 15)
```

## GraphNode

**GraphNode** wraps a Graph for use as a node in another graph. This enables hierarchical composition: a graph can contain other graphs as nodes.

### Creating GraphNode

Create via `Graph.as_node()` rather than directly:

```python
from hypergraph import node, Graph

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

# Inner graph must have a name
inner = Graph([double], name="doubler")

# Wrap as node
gn = inner.as_node()
print(gn.name)     # "doubler"
print(gn.inputs)   # ('x',)
print(gn.outputs)  # ('doubled',)
```

By default the GraphNode surface is flat in the parent graph. Use
`as_node(namespaced=True)` when the parent-facing ports should be prefixed by
the resolved GraphNode name:

```python
gn = inner.as_node(namespaced=True)
print(gn.inputs)   # ('doubler.x',)
print(gn.outputs)  # ('doubler.doubled',)
```

On a namespaced GraphNode, `.expose(...)` replaces selected namespaced ports
with flat parent-facing addresses:

```python
gn = inner.as_node(namespaced=True).expose("x", doubled="result")
print(gn.inputs)   # ('x',)
print(gn.outputs)  # ('result',)
```

GraphNodes also inherit the parent runner by default. Use
`as_node(runner=...)` or `.with_runner(...)` only when a nested graph should
delegate to a different compatible runner:

```python
from hypergraph.integrations.daft import DaftRunner

gn = inner.as_node(runner=DaftRunner())
gn = inner.as_node().with_runner(DaftRunner())  # equivalent
```

Runner overrides are execution configuration, not graph structure. `DaftRunner`
does not support nested runner overrides inside a Daft plan.

### Overriding the Name

You can override the name when calling `as_node()`:

```python
gn = inner.as_node(name="my_doubler")
print(gn.name)  # "my_doubler"
```

### Properties

GraphNode inherits from HyperNode and has these properties:

#### `name: str`

The node name. Either from `graph.name` or explicitly provided to `as_node()`.

#### `inputs: tuple[str, ...]`

Resolved parent-facing input addresses after boundary projection.

```python
gn = inner.as_node()
print(gn.inputs)  # ('x',)
```

#### `outputs: tuple[str, ...]`

Resolved parent-facing output addresses after boundary projection.

```python
gn = inner.as_node()
print(gn.outputs)  # ('doubled',)
```

#### `graph: Graph`

The wrapped Graph instance.

```python
gn = inner.as_node()
print(gn.graph.name)  # "doubler"
```

#### `is_async: bool`

True if the wrapped graph contains any async nodes.

```python
@node(output_name="data")
async def fetch(url: str) -> dict:
    return {}

async_graph = Graph([fetch], name="fetcher")
gn = async_graph.as_node()
print(gn.is_async)  # True
```

#### `definition_hash: str`

Includes the wrapped graph and the GraphNode boundary surface, including
`namespaced`, local renames, exposed ports, and map settings.

```python
gn = inner.as_node()
print(gn.definition_hash == inner.definition_hash)  # False
```

#### `complete_on_stop: bool`

The effective stop behavior configured by `Graph.as_node(complete_on_stop=...)`.

```python
finishing = inner.as_node(complete_on_stop=True)
print(finishing.complete_on_stop)  # True
```

### Type Annotation Forwarding

GraphNode forwards type annotations from the inner graph for `strict_types` validation:

```python
@node(output_name="value")
def producer() -> int:
    return 42

inner = Graph([producer], name="inner")
gn = inner.as_node()

# Type forwarding works
print(gn.get_output_type("value"))  # <class 'int'>

# Allows strict_types validation in outer graph
@node(output_name="result")
def consumer(value: int) -> int:
    return value * 2

outer = Graph([gn, consumer], strict_types=True)  # Works!
```

#### Heterogeneous boundary types

When multiple inner nodes share one boundary name with different annotations, the reported type is deterministic: the first *annotated* inner node in sorted inner-node-name order wins.

```python
@node(output_name="out1")
def node_int(x: int) -> int: ...

@node(output_name="out2")
def node_str(x: str) -> str: ...

gn = Graph([node_str, node_int], name="inner").as_node()
print(gn.get_input_type("x"))  # <class 'int'> — 'node_int' sorts first
```

Under `strict_types=True`, a wired boundary port with conflicting inner annotations is rejected at construction time instead, naming both inner nodes and both types:

```text
GraphConfigError: Conflicting type annotations for input 'x' on GraphNode 'inner' in strict_types mode
  -> inner node 'node_int' declares: <class 'int'>
  -> inner node 'node_str' declares: <class 'str'>
```

The permissive default (`strict_types=False`) is unchanged.

### Nested Composition Example

```python
from hypergraph import node, Graph

# Level 1: Simple nodes
@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3

# Level 2: Inner graph
inner = Graph([double, triple], name="multiply")
print(inner.inputs.required)  # ('x',)
print(inner.outputs)          # ('doubled', 'tripled')

# Level 3: Wrap and use in outer graph
@node(output_name="final")
def finalize(tripled: int) -> str:
    return f"Result: {tripled}"

outer = Graph([inner.as_node(), finalize])
print(outer.inputs.required)  # ('x',)
print(outer.outputs)          # ('doubled', 'tripled', 'final')

outer_ns = Graph([inner.as_node(namespaced=True)])
print(outer_ns.inputs.required)  # ('multiply.x',)
print(outer_ns.outputs)          # ('multiply.doubled', 'multiply.tripled')
```

### Rename Methods

GraphNode supports the same rename methods as other nodes:

```python
gn = inner.as_node()

# Rename inputs
adapted = gn.rename_inputs(x="input_value")
print(adapted.inputs)  # ('input_value',)

# Rename outputs
adapted = gn.rename_outputs(doubled="result")
print(adapted.outputs)  # ('result',)

# Rename the node itself
adapted = gn.with_name("my_processor")
print(adapted.name)  # "my_processor"
```

For namespaced GraphNodes, `rename_inputs(...)` and `rename_outputs(...)` names target the current local port names before namespace projection. `map_over(...)` and `clone` accept either current local names or projected parent-facing input addresses, then normalize to local names internally.

### expose()

Expose selected local ports from a namespaced GraphNode as flat parent-facing addresses.

```python
gn = inner.as_node(namespaced=True).expose("query", answer="final_answer")

print(gn.inputs)   # ('query',)
print(gn.outputs)  # ('final_answer',)
```

`expose(...)` is only valid on namespaced GraphNodes. It replaces the namespaced address at that boundary rather than adding a second alias. If a local name exists as both an input and output, exposing that name exposes both directions. Duplicate aliases inside one GraphNode are rejected; different GraphNodes may still expose inputs to the same parent address.

### map_over()

Configure a GraphNode to iterate over input parameters. When the outer graph runs, the inner graph executes multiple times—once per value in the mapped parameters.

```python
def map_over(
    self,
    *params: str,
    mode: Literal["zip", "product"] = "zip",
    error_handling: Literal["raise", "continue"] = "raise",
    clone: bool | list[str] = False,
    identity: str | None = None,
    schema: type | None = None,
) -> GraphNode: ...
```

**Args:**
- `*params` - Input parameter names to iterate over. Use current local names or projected parent-facing input addresses.
- `mode` - How to combine multiple parameters:
  - `"zip"` (default): Parallel iteration, equal-length lists required
  - `"product"`: Cartesian product, all combinations
- `error_handling` - How to handle failures during mapped execution:
  - `"raise"` (default): Stop on first failure and raise the error
  - `"continue"`: Collect all results, using `None` as placeholder for failed items (preserving list length)

**Returns:** New GraphNode with map_over configuration

**Raises:**
- `ValueError` - If no parameters specified
- `ValueError` - If parameter not in node's inputs

**Example: Basic Iteration**

```python
from hypergraph import Graph, node, SyncRunner

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

# Inner graph
inner = Graph([double], name="inner")

# Configure for iteration over x
gn = inner.as_node().map_over("x")

# Use in outer graph
outer = Graph([gn])

runner = SyncRunner()
result = runner.run(outer, {"x": [1, 2, 3]})

# Output is a list of results
print(result["doubled"])  # [2, 4, 6]
```

**Example: Zip Mode (Multiple Parameters)**

```python
@node(output_name="sum")
def add(a: int, b: int) -> int:
    return a + b

inner = Graph([add], name="adder")
gn = inner.as_node().map_over("a", "b", mode="zip")

outer = Graph([gn])
result = runner.run(outer, {"a": [1, 2, 3], "b": [10, 20, 30]})

# Pairs: (1,10), (2,20), (3,30)
print(result["sum"])  # [11, 22, 33]
```

**Example: Product Mode**

```python
gn = inner.as_node().map_over("a", "b", mode="product")

outer = Graph([gn])
result = runner.run(outer, {"a": [1, 2], "b": [10, 20]})

# All combinations: (1,10), (1,20), (2,10), (2,20)
print(result["sum"])  # [11, 21, 12, 22]
```

**Example: Error Handling**

```python
# Continue on errors — failed items become None, preserving list length
gn = inner.as_node().map_over("x", error_handling="continue")

outer = Graph([gn])
result = runner.run(outer, {"x": [1, 2, "bad_input", 4]})

# result["doubled"] → [2, 4, None, 8]
# None placeholders keep list aligned with inputs
```

**Output Types with map_over**

When `map_over` is configured, output types are automatically wrapped in `list[]`:

```python
@node(output_name="value")
def produce() -> int:
    return 42

inner = Graph([produce], name="inner")

# Without map_over
gn = inner.as_node()
print(gn.get_output_type("value"))  # <class 'int'>

# With map_over
gn_mapped = gn.map_over("x")
print(gn_mapped.get_output_type("value"))  # list[int]
```

This enables `strict_types=True` validation in outer graphs.

**Rename Integration**

When you rename inputs, map_over configuration updates automatically:

```python
gn = inner.as_node().map_over("x")
renamed = gn.rename_inputs(x="input_value")

# map_over now references "input_value"
print(renamed.inputs)  # ('input_value',)
```

### map_config Property

Check the current map_over configuration:

```python
@property
def map_config(self) -> tuple[list[str], Literal["zip", "product"], ErrorHandling] | None: ...
```

```python
gn = inner.as_node()
print(gn.map_config)  # None

gn_mapped = gn.map_over("x", "y", mode="product")
print(gn_mapped.map_config)  # (['x', 'y'], 'product', 'raise')
```

### map_execution_config Property

Use the immutable execution view when code needs every setting that changes
mapped execution. It includes cloning and HyperTable child identity/schema in
addition to the legacy `map_config` fields. List-shaped values are tuples so
callers cannot mutate the node through the returned value.

```python
from hypergraph import GraphNodeMapExecutionConfig

mapped = inner.as_node().map_over(
    "items",
    clone=True,
    identity="item_id",
    schema=Item,
)

assert mapped.map_execution_config == GraphNodeMapExecutionConfig(
    params=("items",),
    mode="zip",
    error_handling="raise",
    clone=True,
    identity="item_id",
    schema=Item,
)
```

The property is `None` when `map_over(...)` has not been configured.

### Error: Missing Name

If neither the graph nor `as_node()` provides a name, an error is raised:

```python
unnamed = Graph([double])  # No name
unnamed.as_node()

# ValueError: GraphNode requires a name. Either set name on Graph(..., name='x')
# or pass name to as_node(name='x')
```

---

## NodeContext

**NodeContext** provides framework capabilities to nodes that need them: cooperative stop signals and live streaming. Injected automatically when detected in the function signature via type hint.

### Usage

Add `ctx: NodeContext` to any node function:

```python
from hypergraph import node, NodeContext

@node(output_name="response")
async def llm_reply(messages: list, ctx: NodeContext) -> str:
    response = ""
    async for chunk in llm.stream(messages):
        if ctx.stop_requested:
            break
        response += chunk
        ctx.stream(chunk)
    return response
```

The framework detects `NodeContext` in the signature and injects it at execution time. The parameter is excluded from the node's inputs — it never appears in `node.inputs` and cannot be provided via `bind()` or `values`.

```python
llm_reply.inputs   # ("messages",) — ctx is not a graph input
llm_reply.outputs  # ("response",)
```

### Properties

#### `stop_requested: bool`

Read-only. `True` when cooperative stop was requested through either
`runner.stop(workflow_id)` or the `SyncHandle` / `AsyncHandle` returned by a
background start method. The node checks this cooperatively and decides when
to break.

```python
@node(output_name="results")
async def process_batch(items: list, ctx: NodeContext) -> list:
    results = []
    for item in items:
        if ctx.stop_requested:
            break
        results.append(await process(item))
    return results
```

A handle can request stop even when the execution has no workflow ID. The same
signal propagates through nested graphs; it does not hard-cancel a thread or
async task. See
[Control Work After It Starts](../05-how-to/control-background-execution.md#request-a-cooperative-stop).

### Methods

#### `stream(chunk: Any) -> None`

Emit a `StreamingChunkEvent` for live UI preview. Does not affect the node's return value — the node controls its own output type.

```python
@node(output_name="response")
async def generate(prompt: str, ctx: NodeContext) -> str:
    response = ""
    async for chunk in llm.stream(prompt):
        response += chunk
        ctx.stream(chunk)  # UI sees tokens live
    return response         # output: final string
```

Streaming is a side-channel. The framework doesn't accumulate chunks, manage reducers, or touch output types. `ctx.stream()` is silently skipped if `stop_requested` is `True`.

### Injection Mechanism

NodeContext uses the same type-hint inspection that powers automatic edge inference. This is the same pattern FastAPI uses for `Request` and `BackgroundTasks`:

- The **type annotation** determines injection, not the parameter name. `ctx`, `context`, `nc` — all work.
- Functions **without** `NodeContext` work exactly as before. Backward compatible.
- Testing is plain Python: `llm_reply(messages=["hi"], ctx=mock_context)`.

### Testing

```python
from unittest.mock import MagicMock

def test_llm_reply_stops():
    ctx = MagicMock(spec=NodeContext)
    ctx.stop_requested = True

    result = llm_reply(messages=["hello"], ctx=ctx)
    assert result == ""  # stopped immediately
```

No framework setup needed — pass a mock or stub directly.

---

## InterruptNode

**InterruptNode** is a FunctionNode subclass that asks a typed question and
declares where the eventual answer enters dataflow. Reaching the node always
pauses unless that answer is already supplied.

### `@interrupt` Decorator

The `@interrupt` decorator creates an InterruptNode from a function, just like `@node` creates a FunctionNode:

```python
from dataclasses import dataclass
from typing import ClassVar

from hypergraph import interrupt

@dataclass(frozen=True)
class Confirm:  # Stand-in for the companion question-vocabulary package.
    answer_type: ClassVar[object] = bool
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()

@interrupt(answer_name="decision")
def approval(draft: str) -> Confirm:
    return Confirm(prompt="Publish this draft?", evidence=(draft,))
```

**Decorator args:**

- `answer_name` (required, keyword-only): Name of the single answer output port
- `rename_inputs`: Optional dict to rename inputs
- `cache`: Enable result caching (default: `False`)
- `emit`: Ordering-only local output name(s) (see [emit/wait_for](../03-patterns/03-agentic-loops.md#ordering-with-emitwait_for))
- `wait_for`: Ordering-only graph-scope output/emit address(es)
- `hide`: Whether to hide from visualization

### Constructor

Like FunctionNode, InterruptNode can also be created via the constructor.
`answer_name` is required, keyword-only, and must be a string.

```python
from hypergraph import InterruptNode

def my_handler(draft: str) -> Confirm:
    return Confirm(prompt="Publish?", evidence=(draft,))

approval = InterruptNode(my_handler, answer_name="decision")
# Or with all options:
approval = InterruptNode(
    my_handler,
    name="review",
    answer_name="decision",
    emit="reviewed",
    wait_for="ready",
)
```

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `inputs` | `tuple[str, ...]` | Input parameter names (from function signature) |
| `outputs` | `tuple[str, ...]` | All output names (data + emit) |
| `data_outputs` | `tuple[str, ...]` | Data-only outputs (excluding emit) |
| `answer_name` | `str` | Current local answer port (including output renames) |
| `is_interrupt` | `bool` | Always `True` |
| `cache` | `bool` | Whether caching is enabled (default: `False`) |
| `hide` | `bool` | Whether hidden from visualization |
| `wait_for` | `tuple[str, ...]` | Ordering-only graph-scope output/emit addresses |
| `is_async` | `bool` | True if handler is async |
| `is_generator` | `bool` | True if handler yields |
| `definition_hash` | `str` | SHA256 hash of function source |

### Methods

#### Inherited: `with_name()`, `rename_inputs()`, `rename_outputs()`

All HyperNode rename methods work as expected.

### Example: Pause and Resume

```python
from hypergraph import Graph, node, AsyncRunner, interrupt

@node(output_name="draft")
def make_draft(query: str) -> str:
    return f"Draft for: {query}"

@interrupt(answer_name="decision")
def approval(draft: str) -> Confirm:
    return Confirm(prompt="Publish?", evidence=(draft,))

@node(output_name="result")
def finalize(decision: bool) -> str:
    return f"Final: {decision}"

graph = Graph([make_draft, approval, finalize])
runner = AsyncRunner()

# Pauses at the interrupt
result = await runner.run(graph, {"query": "hello"})
assert result.paused
assert isinstance(result.pause.value, Confirm)
assert result.pause.value.evidence == ("Draft for: hello",)

# Resume with response
result = await runner.run(graph, {
    "query": "hello",
    result.pause.response_key: True,
})
assert result["result"] == "Final: True"
```

The return annotation must expose class-level `answer_type`; under strict type
checking that exact type is compared with consumers of `answer_name`. The
returned instance must expose `prompt`, `options`, and `evidence`. Returning
`None` is an error, and tuple answer names are not supported. Multi-field
questions use one structured answer value, such as `Form[Model]`.

For a full guide with multiple interrupts, nested graphs, and handler patterns, see [Human-in-the-Loop](../03-patterns/07-human-in-the-loop.md).
