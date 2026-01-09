# Product Requirements Document: FunctionNode Implementation

## Overview

**Feature**: Implement the FunctionNode - the primary node type that wraps Python functions as graph nodes in the hypergraph framework.

**Context**: This is the foundational component of the hypergraph framework. FunctionNode wraps regular Python functions to make them usable as nodes in computational graphs. It is the most common node type users will interact with.

**Priority**: P0 - Core infrastructure (framework cannot function without this)

---

## Goals

1. **Enable function-to-node conversion**: Allow any Python function (sync, async, generator, async generator) to be wrapped as a graph node
2. **Provide intuitive API**: Support both decorator syntax (`@node`) and constructor syntax (`FunctionNode(func)`)
3. **Enable graph composition**: Expose input/output names for wiring nodes together
4. **Support renaming/adaptation**: Allow nodes to be renamed and their inputs/outputs remapped for reuse in different contexts
5. **Enable caching**: Support deterministic caching via definition hashing

---

## User Stories

### US-1: Basic Function Wrapping
**As a** developer building ML pipelines,
**I want to** wrap my Python functions as graph nodes,
**So that** I can compose them into workflows.

```python
@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return model.embed(text)

# Use in graph
graph = Graph(nodes=[embed, retrieve, generate])
```

### US-2: Multiple Output Names
**As a** developer with functions returning multiple values,
**I want to** specify multiple output names,
**So that** each value can be consumed by different downstream nodes.

```python
@node(output_name=("mean", "std"))
def statistics(data: list) -> tuple[float, float]:
    return calculate_mean(data), calculate_std(data)
```

### US-3: Node Adaptation/Renaming
**As a** developer reusing the same function in different contexts,
**I want to** rename node inputs/outputs,
**So that** I can wire the same function into different parts of my graph.

```python
# Same function, different configurations
node_a = process.with_inputs(x="raw_data")
node_b = process.with_inputs(x="processed_data")
```

### US-4: Async and Generator Support
**As a** developer building I/O-bound or streaming workflows,
**I want to** use async functions and generators as nodes,
**So that** I can build efficient, non-blocking pipelines.

```python
@node(output_name="tokens")
async def stream_llm(prompt: str) -> AsyncIterator[str]:
    async for chunk in client.stream(prompt):
        yield chunk
```

### US-5: Direct Function Invocation
**As a** developer debugging my workflow,
**I want to** call the wrapped function directly,
**So that** I can test it outside the graph context.

```python
# Direct call still works
result = embed("hello world")  # Calls embed.func("hello world")
```

---

## Functional Requirements

### FR-1: HyperNode Base Class

The abstract base class for all node types. Inherits from `ABC` (from `abc` module).

```python
from abc import ABC

class HyperNode(ABC):
    """Base class for all node types with shared rename functionality."""
```

**Attributes**:
| Attribute | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Public node name |
| `inputs` | `tuple[str, ...]` | Input parameter names |
| `outputs` | `tuple[str, ...]` | Output value names |
| `_rename_history` | `list[RenameEntry]` | Tracks renames for error messages |

**Public Methods**:
| Method | Signature | Description |
|--------|-----------|-------------|
| `with_name` | `(name: str) -> Self` | Return new node with different name |
| `with_inputs` | `(mapping?, **kwargs) -> Self` | Return new node with renamed inputs |
| `with_outputs` | `(mapping?, **kwargs) -> Self` | Return new node with renamed outputs |

**Internal Methods** (implementation helpers):
| Method | Signature | Description |
|--------|-----------|-------------|
| `_copy` | `() -> Self` | Create shallow copy with independent history list |
| `_with_renamed` | `(attr: str, mapping: dict[str, str]) -> Self` | Rename entries in an attribute |
| `_make_rename_error` | `(name: str, attr: str) -> RenameError` | Build helpful error message using history |

**`_copy()` Implementation**:
```python
def _copy(self) -> Self:
    """Create shallow copy with independent history list."""
    clone = copy.copy(self)
    clone._rename_history = list(self._rename_history)
    return clone
```

**`_with_renamed()` Implementation**:
```python
def _with_renamed(self, attr: str, mapping: dict[str, str]) -> Self:
    """Rename entries in an attribute (name, inputs, or outputs)."""
    clone = self._copy()
    current = getattr(clone, attr)

    if isinstance(current, str):
        # Single value (name)
        old, new = current, mapping.get(current, current)
        if old != new:
            clone._rename_history.append(RenameEntry(attr, old, new))
            setattr(clone, attr, new)
    else:
        # Tuple (inputs/outputs)
        for old, new in mapping.items():
            if old not in current:
                raise clone._make_rename_error(old, attr)
            clone._rename_history.append(RenameEntry(attr, old, new))
        setattr(clone, attr, tuple(mapping.get(v, v) for v in current))

    return clone
```

**`_make_rename_error()` Implementation**:
```python
def _make_rename_error(self, name: str, attr: str) -> RenameError:
    """Build helpful error message using history."""
    current = getattr(self, attr)
    for entry in self._rename_history:
        if entry.kind == attr and entry.old == name:
            return RenameError(
                f"'{name}' was renamed to '{entry.new}'. "
                f"Current {attr}: {current}"
            )
    return RenameError(f"'{name}' not found. Current {attr}: {current}")
```

**Behavior**:
- All `with_*` methods return new instances (immutable pattern)
- Rename history enables helpful error messages when users reference old names
- `_copy()` creates shallow copy; only `_rename_history` list is deep-copied

### FR-2: FunctionNode Class

Concrete node wrapping a Python function.

**Constructor**:
```python
def __init__(
    self,
    source: Callable | FunctionNode,
    output_name: str | tuple[str, ...] | None = None,
    *,
    name: str | None = None,
    rename_inputs: dict[str, str] | None = None,
    cache: bool = False,
)
```

**Additional Attributes**:
| Attribute | Type | Description |
|-----------|------|-------------|
| `func` | `Callable` | The wrapped function |
| `cache` | `bool` | Whether to cache results |
| `_definition_hash` | `str` | SHA256 hash of function source |
| `_is_async` | `bool` | Auto-detected from function |
| `_is_generator` | `bool` | Auto-detected from function |

**Properties**:
| Property | Type | Description |
|----------|------|-------------|
| `definition_hash` | `str` | Returns `_definition_hash` |
| `is_async` | `bool` | True if async def or async generator |
| `is_generator` | `bool` | True if yields values |

**Special Methods**:
| Method | Description |
|--------|-------------|
| `__call__(*args, **kwargs)` | Delegates to `self.func` |
| `__repr__()` | Informative string representation (see below) |

**`__repr__()` Implementation**:
```python
def __repr__(self) -> str:
    # Find original name from history (if renamed) or use func name
    original = self.func.__name__
    for entry in self._rename_history:
        if entry.kind == "name" and entry.new == self.name:
            original = entry.old
            break

    if self.name == original:
        return f"FunctionNode({self.name}, outputs={self.outputs})"
    else:
        return f"FunctionNode({original} as '{self.name}', outputs={self.outputs})"
```

**Example Output**:
```python
>>> process
FunctionNode(process, outputs=('result',))

>>> process.with_name("preprocessor")
FunctionNode(process as 'preprocessor', outputs=('result',))
```

### FR-3: @node Decorator

Convenient decorator for creating FunctionNode.

**Signatures**:
```python
@node  # Without parens - uses defaults
def foo(): ...

@node(output_name="result")  # With parens
def bar(): ...
```

**Parameters**: Same as FunctionNode constructor except `source`.

### FR-4: RenameEntry Dataclass

Tracks rename operations for error messages.

```python
@dataclass(frozen=True)
class RenameEntry:
    kind: Literal["name", "inputs", "outputs"]
    old: str
    new: str
```

### FR-5: RenameError Exception

Custom exception with helpful context. Simple `Exception` subclass.

```python
class RenameError(Exception):
    """Raised when a rename operation references a non-existent name."""
    pass
```

The error message is constructed by `HyperNode._make_rename_error()` (see FR-1).

**Error message format examples**:
```
RenameError: 'text' was renamed to 'raw'. Current inputs: ('raw', 'config')
```

```
RenameError: 'foo' not found. Current inputs: ('bar', 'baz')
```

### FR-6: Module-Level Helper Functions

These functions live in `base.py` alongside `HyperNode` and `RenameEntry`.

**`_apply_renames(values, mapping, kind)`**: Apply rename mapping to tuple of values, return (new_values, history_entries).

```python
def _apply_renames(
    values: tuple[str, ...],
    mapping: dict[str, str] | None,
    kind: Literal["inputs", "outputs"],
) -> tuple[tuple[str, ...], list[RenameEntry]]:
    """Apply renames to a tuple, returning (new_values, history)."""
    if not mapping:
        return values, []

    history = [RenameEntry(kind, old, new) for old, new in mapping.items()]
    return tuple(mapping.get(v, v) for v in values), history
```

### FR-7: Utility Functions

These functions live in `_utils.py` for general use.

**`ensure_tuple(value)`**: Convert single string to 1-tuple, pass tuples through.

```python
def ensure_tuple(value: str | tuple[str, ...]) -> tuple[str, ...]:
    """Convert single string to 1-tuple, pass tuples through."""
    if isinstance(value, str):
        return (value,)
    return value
```

**`hash_definition(func)`**: Compute SHA256 hash of function source code using `inspect.getsource()`.

```python
import hashlib
import inspect

def hash_definition(func: Callable) -> str:
    """Compute SHA256 hash of function source code."""
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError) as e:
        # Built-ins, C extensions, or dynamically created functions
        raise ValueError(f"Cannot hash function {func.__name__}: {e}")
    return hashlib.sha256(source.encode()).hexdigest()
```

**Note on `hash_definition` edge cases**:
- Works for regular functions, lambdas defined in files, methods, closures
- Raises `ValueError` for built-in functions, C extensions, or functions without source
- Lambdas in REPL may fail depending on environment

---

## Execution Mode Detection

FunctionNode must auto-detect all four execution modes:

| Mode | Detection | `is_async` | `is_generator` |
|------|-----------|------------|----------------|
| Sync function | `def foo()` | False | False |
| Async function | `async def foo()` | True | False |
| Sync generator | `def foo(): yield` | False | True |
| Async generator | `async def foo(): yield` | True | True |

**Detection Logic**:
```python
is_async = inspect.iscoroutinefunction(func) or inspect.isasyncgenfunction(func)
is_generator = inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func)
```

---

## Output Validation Behavior

**Single output name** (string or 1-tuple): Return value stored as-is, no unpacking.

**Multiple output names** (2+ tuple): Return value is unpacked and validated.

```python
# Single output - tuple stays as tuple
@node(output_name="items")
def get_list() -> list:
    return [1, 2, 3]  # Stored as outputs["items"]

# Multiple outputs - tuple is unpacked
@node(output_name=("a", "b"))
def split() -> tuple:
    return (1, 2)  # outputs["a"]=1, outputs["b"]=2
```

**Note**: Output validation happens at runtime (runner responsibility), not at node creation.

---

## Non-Functional Requirements

### NFR-1: Zero Dependencies
The core implementation must have no external dependencies (use stdlib only).

### NFR-2: Python Version Support
Must support Python 3.10+ (as specified in pyproject.toml).

### NFR-3: Type Hints
Full type hint coverage for IDE support and documentation.

### NFR-4: Immutability
All `with_*` methods must return new instances, never mutate.

### NFR-5: Performance
- `definition_hash` computed once at creation, cached
- `is_async`/`is_generator` computed once at creation
- Shallow copy for `_copy()` (only history list needs deep copy)

---

## Out of Scope

The following are explicitly **not** part of this implementation:

1. **GateNode and subclasses** (RouteNode, BranchNode, TypeRouteNode) - separate task
2. **InterruptNode** - separate task
3. **GraphNode** - requires Graph implementation first
4. **Graph class** - separate task
5. **Runners** (SyncRunner, AsyncRunner, etc.) - separate task
6. **Output validation at runtime** - runner responsibility
7. **Cache storage/retrieval** - runner/checkpointer responsibility
8. **`hash_depth` for transitive imports** - Graph-level concern

---

## File Structure

```
src/hypergraph/
├── __init__.py          # Public exports
├── nodes/
│   ├── __init__.py      # Node module exports
│   ├── base.py          # HyperNode, RenameEntry, RenameError, _apply_renames
│   └── function.py      # FunctionNode, node decorator
└── _utils.py            # ensure_tuple, hash_definition
```

---

## Public API

```python
from hypergraph import node, FunctionNode, HyperNode, RenameError
```

---

## Testing Requirements

### Test Categories

1. **Unit Tests**: FunctionNode creation, properties, methods
2. **Decorator Tests**: `@node` with/without parentheses
3. **Rename Tests**: All `with_*` methods, error messages
4. **Execution Mode Tests**: All four mode combinations
5. **Edge Cases**: Empty functions, lambdas, methods, closures

### Key Test Scenarios

| Scenario | Expected Behavior |
|----------|-------------------|
| `@node` without parens | output_name defaults to function name |
| `@node(output_name="x")` | output_name is ("x",) |
| `FunctionNode(existing_node)` | Extracts `.func`, ignores other config |
| `node.with_inputs(old="new")` | Returns new node, updates history |
| `node.with_inputs(nonexistent="x")` | RenameError with helpful message |
| Chained renames | History shows full chain |
| `hash_definition` on lambda in file | Works (uses `inspect.getsource`) |
| `hash_definition` on built-in | Raises `ValueError` (cannot get source) |
| `hash_definition` on C extension | Raises `ValueError` (cannot get source) |

---

## Acceptance Criteria

1. **AC-1**: Can create FunctionNode from sync function with `@node` decorator
2. **AC-2**: Can create FunctionNode from async function with `@node` decorator
3. **AC-3**: Can create FunctionNode from generator with `@node` decorator
4. **AC-4**: Can create FunctionNode from async generator with `@node` decorator
5. **AC-5**: `is_async` and `is_generator` are correctly auto-detected for all modes
6. **AC-6**: `with_name`, `with_inputs`, `with_outputs` return new immutable instances
7. **AC-7**: RenameError provides helpful context from rename history
8. **AC-8**: `definition_hash` returns consistent SHA256 for same function
9. **AC-9**: `__call__` delegates to wrapped function
10. **AC-10**: All tests pass with `pytest`
11. **AC-11**: Type hints enable IDE autocompletion

---

## Design Decisions

### DD-1: Why `_rename_history` instead of `original_*` attributes?
The history approach enables tracking multiple renames and provides better error messages. It also handles cases where a name is renamed multiple times.

### DD-2: Why cache `_definition_hash` at creation?
Computing the hash requires reading source code via `inspect.getsource()`, which involves file I/O. Caching avoids repeated I/O and ensures consistent hash even if source file changes during runtime.

### DD-3: Why positional-only `/` in `with_inputs`?
```python
def with_inputs(self, mapping: dict[str, str] | None = None, /, **kwargs: str)
```
This allows `**kwargs` to contain parameter names that would otherwise conflict (e.g., `mapping` itself).

### DD-4: Why extract only `.func` when source is FunctionNode?
When wrapping an existing FunctionNode, we want fresh configuration. Inheriting the old node's settings would be confusing and error-prone.

---

## References

- `specs/reviewed/node-types.md` - Complete node type specification
- `specs/reviewed/graph.md` - Graph structure and hashing
- `specs/reviewed/state-model.md` - "Outputs ARE state" philosophy
