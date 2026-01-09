# Node API Reference

Complete API documentation for HyperNode and FunctionNode.

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

#### with_name(name: str) -> HyperNode

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

#### with_inputs(mapping=None, /, **kwargs) -> HyperNode

Return a new node with renamed inputs.

```python
@node(output_name="result")
def process(text: str, config: dict) -> str:
    return text.upper()

# Using keyword args
adapted = process.with_inputs(text="raw_input", config="settings")
print(adapted.inputs)  # ("raw_input", "settings")

# Using dict (for reserved keywords or dynamic renames)
adapted = process.with_inputs({"text": "raw_input", "class": "category"})
```

**Args:**
- `mapping` (optional, positional-only): Dict of `{old_name: new_name}`
- `**kwargs`: Additional renames as keyword arguments

**Returns:** New node instance with updated inputs

**Raises:**
- `RenameError` - If any old name not found in current inputs
- `RenameError` - Includes helpful history if name was already renamed

#### with_outputs(mapping=None, /, **kwargs) -> HyperNode

Return a new node with renamed outputs.

```python
@node(output_name="result")
def process(x: int) -> int:
    return x * 2

# Using keyword args
adapted = process.with_outputs(result="doubled")
print(adapted.outputs)  # ("doubled",)

# Using dict
adapted = process.with_outputs({"result": "doubled"})
```

**Args:**
- `mapping` (optional, positional-only): Dict of `{old_name: new_name}`
- `**kwargs`: Additional renames as keyword arguments

**Returns:** New node instance with updated outputs

**Raises:**
- `RenameError` - If any old name not found in current outputs
- `RenameError` - Includes helpful history if name was already renamed

### Immutability Pattern

All `with_*` methods return new instances. The original is never modified:

```python
original = process
v1 = original.with_name("v1")
v2 = original.with_name("v2")
v3 = v1.with_inputs(x="input")

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
FunctionNode(
    source: Callable | FunctionNode,
    name: str | None = None,
    output_name: str | tuple[str, ...] | None = None,
    *,
    rename_inputs: dict[str, str] | None = None,
    cache: bool = False,
) -> FunctionNode
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

**Returns:** FunctionNode instance

**Raises:**
- `ValueError` - If function source cannot be retrieved (for definition_hash)
- `UserWarning` - If function has return annotation but no output_name provided

#### Creating from a Function

```python
from hypergraph import FunctionNode

def double(x: int) -> int:
    return x * 2

node = FunctionNode(double, name="double_value", output_name="result")

print(node.name)     # "double_value"
print(node.inputs)   # ("x",)
print(node.outputs)  # ("result",)
```

#### Creating from Existing FunctionNode

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

#### func: Callable

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

#### name: str

Public node name (may differ from function name).

```python
@node(output_name="result")
def process(x: int) -> int:
    return x * 2

print(process.name)  # "process"

renamed = process.with_name("preprocessor")
print(renamed.name)  # "preprocessor"
```

#### inputs: tuple[str, ...]

Input parameter names from function signature (after renaming).

```python
@node(output_name="result")
def add(x: int, y: int) -> int:
    return x + y

print(add.inputs)  # ("x", "y")

adapted = add.with_inputs(x="a", y="b")
print(adapted.inputs)  # ("a", "b")
```

#### outputs: tuple[str, ...]

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

#### cache: bool

Whether results are cached (default: False). Set via constructor.

```python
@node(output_name="result", cache=True)
def expensive(x: int) -> int:
    return x ** 100

print(expensive.cache)  # True
```

#### definition_hash: str

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

#### is_async: bool

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

#### is_generator: bool

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

#### \_\_call\_\_(\_args, \_\_kwargs)

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

#### \_\_repr\_\_() -> str

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
) -> FunctionNode | Callable[[Callable], FunctionNode]
```

**Args:**
- `source`: The function (when used without parens like `@node`)
- `output_name`: Output name(s). If None, side-effect only (outputs = ())
- `rename_inputs`: Optional dict to rename inputs
- `cache`: Whether to cache results

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
    process.with_inputs(y="renamed")  # 'y' doesn't exist
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
renamed = process.with_inputs(x="input")

# Try to use old name
try:
    renamed.with_inputs(x="something_else")
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
    .with_inputs(first="a", second="b")
    .with_outputs(sum="total", product="multiply")
)

print(adapted.name)     # "math_ops"
print(adapted.inputs)   # ("a", "b")
print(adapted.outputs)  # ("total", "multiply")

# Call the function
result = node_instance(5, 3)
print(result)           # (8, 15)
```
