# Technical Specification: FunctionNode Implementation

## Overview

This document specifies the implementation of `FunctionNode` - the primary node type that wraps Python functions as graph nodes in the hypergraph framework. This is the foundational component upon which all other graph functionality will be built.

---

## Code Flow: User Perspective to Implementation

### User-Facing API Syntax

Users interact with FunctionNode through two primary patterns:

```python
# Pattern 1: Decorator syntax (most common)
@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return model.embed(text)

# Pattern 2: Constructor syntax (for dynamic configuration)
node_a = FunctionNode(my_func, output_name="result", name="processor")
```

### Flow: From User Code to Internal Classes

```
User Code                    Internal Flow
---------                    -------------

@node(output_name="x")       1. node() receives output_name="x"
def foo(a, b): ...           2. node() returns decorator function
                             3. decorator(foo) called with the function
                             4. FunctionNode.__init__(foo, output_name="x")
                             5. Extracts inputs via inspect.signature(foo)
                             6. Computes definition_hash via hash_definition(foo)
                             7. Detects is_async/is_generator via inspect
                             8. Returns FunctionNode instance

foo.inputs                   → ("a", "b")  # from inspect.signature
foo.outputs                  → ("x",)      # from output_name
foo.name                     → "foo"       # from func.__name__
foo.is_async                 → False       # from inspect.iscoroutinefunction
foo.is_generator             → False       # from inspect.isgeneratorfunction
foo.definition_hash          → "abc123..." # SHA256 of source

foo(1, 2)                    → foo.func(1, 2)  # __call__ delegates to wrapped func
```

### Renaming Flow

```
foo.with_inputs(a="x")       1. _with_renamed("inputs", {"a": "x"})
                             2. _copy() creates shallow clone
                             3. Clone's _rename_history gets: [RenameEntry("inputs", "a", "x")]
                             4. Clone's inputs becomes: ("x", "b")
                             5. Returns clone (original unchanged)

renamed = foo.with_inputs(a="x")
renamed.inputs               → ("x", "b")
renamed.with_inputs(a="y")   → RenameError: 'a' was renamed to 'x'. Current inputs: ('x', 'b')
```

---

## Class Signatures and Relationships

### Dependency Graph

```
┌─────────────────┐     ┌──────────────────┐
│   _utils.py     │     │     base.py      │
│                 │     │                  │
│  ensure_tuple() │◄────│  HyperNode (ABC) │
│  hash_definition│     │  RenameEntry     │
└─────────────────┘     │  RenameError     │
         │              │  _apply_renames  │
         │              └────────▲─────────┘
         │                       │ inherits
         ▼                       │
┌─────────────────────────────────────────┐
│              function.py                │
│                                         │
│  FunctionNode(HyperNode)                │
│  node() decorator                       │
└─────────────────────────────────────────┘
```

### File: `src/hypergraph/_utils.py`

```python
"""Utility functions for hypergraph."""

import hashlib
import inspect
from typing import Callable


def ensure_tuple(value: str | tuple[str, ...]) -> tuple[str, ...]:
    """Convert single string to 1-tuple, pass tuples through.

    Args:
        value: A string or tuple of strings

    Returns:
        Tuple of strings (single string becomes 1-tuple)

    Examples:
        >>> ensure_tuple("foo")
        ('foo',)
        >>> ensure_tuple(("a", "b"))
        ('a', 'b')
    """


def hash_definition(func: Callable) -> str:
    """Compute SHA256 hash of function source code.

    Args:
        func: Function to hash

    Returns:
        64-character hex string (SHA256 hash)

    Raises:
        ValueError: If function source cannot be retrieved
                    (built-ins, C extensions, dynamically created)

    Examples:
        >>> def foo(): pass
        >>> len(hash_definition(foo))
        64
    """
```

### File: `src/hypergraph/nodes/base.py`

```python
"""Base classes for all node types."""

from __future__ import annotations

import copy
from abc import ABC
from dataclasses import dataclass
from typing import Literal, Self


@dataclass(frozen=True)
class RenameEntry:
    """Tracks a single rename operation for error messages.

    Attributes:
        kind: Which attribute was renamed ("name", "inputs", or "outputs")
        old: Original value before rename
        new: New value after rename
    """
    kind: Literal["name", "inputs", "outputs"]
    old: str
    new: str


class RenameError(Exception):
    """Raised when a rename operation references a non-existent name.

    The error message includes context from rename history to help
    users understand what happened (e.g., if the name was already renamed).
    """
    pass


def _apply_renames(
    values: tuple[str, ...],
    mapping: dict[str, str] | None,
    kind: Literal["inputs", "outputs"],
) -> tuple[tuple[str, ...], list[RenameEntry]]:
    """Apply renames to a tuple, returning (new_values, history).

    Args:
        values: Original tuple of names
        mapping: Optional {old: new} rename mapping
        kind: Type of rename for history tracking

    Returns:
        Tuple of (renamed_values, history_entries)

    Note:
        Does NOT validate that old names exist in values.
        Validation is handled by _with_renamed at rename time.
    """


class HyperNode(ABC):
    """Abstract base class for all node types with shared rename functionality.

    Defines the minimal interface that all nodes share:
    - name: Public node name
    - inputs: Input parameter names
    - outputs: Output value names
    - _rename_history: Tracks renames for error messages

    All with_* methods return new instances (immutable pattern).

    Subclasses must set these attributes in __init__:
    - name: str
    - inputs: tuple[str, ...]
    - outputs: tuple[str, ...]
    - _rename_history: list[RenameEntry]  (typically starts as [])
    """

    # Type annotations for IDE support (set by subclass __init__)
    name: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    _rename_history: list[RenameEntry]

    # === Public API ===

    def with_name(self, name: str) -> Self:
        """Return new node with different name.

        Args:
            name: New node name

        Returns:
            New node instance with updated name
        """

    def with_inputs(
        self,
        mapping: dict[str, str] | None = None,
        /,
        **kwargs: str
    ) -> Self:
        """Return new node with renamed inputs.

        Args:
            mapping: Optional dict {old_name: new_name}
            **kwargs: Additional renames as keyword args

        Returns:
            New node instance with updated inputs

        Raises:
            RenameError: If any old name not found in current inputs

        Note:
            The `/` makes mapping positional-only, allowing kwargs
            like `mapping="foo"` if your node has an input named "mapping".
        """

    def with_outputs(
        self,
        mapping: dict[str, str] | None = None,
        /,
        **kwargs: str
    ) -> Self:
        """Return new node with renamed outputs.

        Args:
            mapping: Optional dict {old_name: new_name}
            **kwargs: Additional renames as keyword args

        Returns:
            New node instance with updated outputs

        Raises:
            RenameError: If any old name not found in current outputs
        """

    # === Internal Helpers ===

    def _copy(self) -> Self:
        """Create shallow copy with independent history list.

        Only _rename_history needs deep copy (mutable list).
        All other attributes are immutable (str, tuple, bool).
        """

    def _with_renamed(self, attr: str, mapping: dict[str, str]) -> Self:
        """Rename entries in an attribute (name, inputs, or outputs).

        Args:
            attr: Attribute name to modify
            mapping: {old: new} rename mapping

        Returns:
            New node with renamed attribute

        Raises:
            RenameError: If old name not found in current attribute value
        """

    def _make_rename_error(self, name: str, attr: str) -> RenameError:
        """Build helpful error message using history.

        Checks if `name` was previously renamed and includes that
        context in the error message.
        """
```

### File: `src/hypergraph/nodes/function.py`

```python
"""FunctionNode - wraps Python functions as graph nodes."""

from __future__ import annotations

import inspect
from typing import Any, Callable

from hypergraph._utils import ensure_tuple, hash_definition
from hypergraph.nodes.base import HyperNode, RenameEntry, _apply_renames


class FunctionNode(HyperNode):
    """Wraps a Python function as a graph node.

    Created via the @node decorator or FunctionNode() constructor.
    Supports all four execution modes: sync, async, sync generator,
    and async generator.

    Attributes:
        name: Public node name (default: func.__name__)
        inputs: Input parameter names from function signature
        outputs: Output value names (default: (func.__name__,))
        func: The wrapped function
        cache: Whether to cache results (default: False)

    Properties:
        definition_hash: SHA256 hash of function source (cached)
        is_async: True if async def or async generator
        is_generator: True if yields values

    Example:
        >>> @node(output_name="doubled")
        ... def double(x: int) -> int:
        ...     return x * 2
        >>> double.inputs
        ('x',)
        >>> double.outputs
        ('doubled',)
        >>> double(5)
        10
    """

    def __init__(
        self,
        source: Callable | FunctionNode,
        output_name: str | tuple[str, ...] | None = None,
        *,
        name: str | None = None,
        rename_inputs: dict[str, str] | None = None,
        cache: bool = False,
    ) -> None:
        """Wrap a function as a node.

        Args:
            source: Function to wrap, or existing FunctionNode (extracts .func)
            output_name: Name(s) for output value(s). Default: function name.
            name: Public node name (default: func.__name__)
            rename_inputs: Mapping to rename inputs {old: new}
            cache: Whether to cache results (default: False)

        Note:
            When source is a FunctionNode, only source.func is extracted.
            All other configuration (name, outputs, renames, cache) from
            the source node is ignored - the new node is built fresh.
        """

    @property
    def definition_hash(self) -> str:
        """SHA256 hash of function source (cached at creation)."""

    @property
    def is_async(self) -> bool:
        """True if requires await (async def or async generator)."""

    @property
    def is_generator(self) -> bool:
        """True if yields multiple values (sync or async generator)."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Call the wrapped function directly.

        Delegates to self.func(*args, **kwargs).
        """

    def __repr__(self) -> str:
        """Informative string representation.

        Shows original function name and current node configuration.
        If renamed, shows "original as 'new_name'".

        Examples:
            FunctionNode(process, outputs=('result',))
            FunctionNode(process as 'preprocessor', outputs=('result',))
        """


def node(
    source: Callable | None = None,
    output_name: str | tuple[str, ...] | None = None,
    *,
    name: str | None = None,
    rename_inputs: dict[str, str] | None = None,
    cache: bool = False,
) -> FunctionNode | Callable[[Callable], FunctionNode]:
    """Decorator to wrap a function as a FunctionNode.

    Can be used with or without parentheses:

        @node
        def foo(): ...

        @node(output_name="result")
        def bar(): ...

    Args:
        source: The function to wrap (when used without parens)
        output_name: Name(s) for output value(s). Default: function name.
        name: Public node name (default: func.__name__)
        rename_inputs: Mapping to rename inputs {old: new}
        cache: Whether to cache results (default: False)

    Returns:
        FunctionNode if source provided, else decorator function.
    """
```

### File: `src/hypergraph/nodes/__init__.py`

```python
"""Node types for hypergraph."""

from hypergraph.nodes.base import HyperNode, RenameEntry, RenameError
from hypergraph.nodes.function import FunctionNode, node

__all__ = [
    "HyperNode",
    "RenameEntry",
    "RenameError",
    "FunctionNode",
    "node",
]
```

### File: `src/hypergraph/__init__.py`

```python
"""Hypergraph - A hierarchical and modular graph workflow framework."""

from hypergraph.nodes import FunctionNode, HyperNode, RenameError, node

__all__ = [
    "node",
    "FunctionNode",
    "HyperNode",
    "RenameError",
]
```

---

## Detailed Implementation Specifications

### `ensure_tuple()` - Functionality and Tests

**Functionality**: Converts a single string to a 1-tuple, passes tuples through unchanged.

```python
def ensure_tuple(value: str | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    return value
```

**Unit Tests** (`tests/test_utils.py`):

| Test Case | Input | Expected Output |
|-----------|-------|-----------------|
| Single string | `"foo"` | `("foo",)` |
| Empty string | `""` | `("",)` |
| 1-tuple | `("foo",)` | `("foo",)` |
| Multi-tuple | `("a", "b", "c")` | `("a", "b", "c")` |
| Empty tuple | `()` | `()` |

---

### `hash_definition()` - Functionality and Tests

**Functionality**: Computes SHA256 hash of function source code using `inspect.getsource()`.

```python
def hash_definition(func: Callable) -> str:
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError) as e:
        raise ValueError(f"Cannot hash function {func.__name__}: {e}")
    return hashlib.sha256(source.encode()).hexdigest()
```

**Unit Tests** (`tests/test_utils.py`):

| Test Case | Input | Expected Behavior |
|-----------|-------|-------------------|
| Regular function | `def foo(): pass` | Returns 64-char hex string |
| Same source = same hash | Two identical functions | Hashes are equal |
| Different source = different hash | Two different functions | Hashes differ |
| Lambda in file | `lambda x: x * 2` | Returns hash (works if defined in file) |
| Built-in function | `len` | Raises `ValueError` |
| Method | `obj.method` | Returns hash of method source |
| Nested function | Function inside function | Returns hash |

---

### `RenameEntry` - Functionality and Tests

**Functionality**: Frozen dataclass tracking a single rename operation.

```python
@dataclass(frozen=True)
class RenameEntry:
    kind: Literal["name", "inputs", "outputs"]
    old: str
    new: str
```

**Unit Tests** (`tests/test_nodes_base.py`):

| Test Case | Expected Behavior |
|-----------|-------------------|
| Create entry | `RenameEntry("inputs", "a", "b")` works |
| Is frozen | Attempting to modify raises `FrozenInstanceError` |
| Equality | Two entries with same values are equal |
| Hashable | Can be used in sets/dict keys |

---

### `RenameError` - Functionality and Tests

**Functionality**: Exception with helpful context about rename history.

**Unit Tests** (`tests/test_nodes_base.py`):

| Test Case | Expected Behavior |
|-----------|-------------------|
| Is Exception subclass | `isinstance(e, Exception)` is True |
| Message preserved | `str(e)` returns the message |

---

### `_apply_renames()` - Functionality and Tests

**Functionality**: Applies rename mapping to a tuple, returns new tuple and history entries.

```python
def _apply_renames(
    values: tuple[str, ...],
    mapping: dict[str, str] | None,
    kind: Literal["inputs", "outputs"],
) -> tuple[tuple[str, ...], list[RenameEntry]]:
    if not mapping:
        return values, []

    history = [RenameEntry(kind, old, new) for old, new in mapping.items()]
    return tuple(mapping.get(v, v) for v in values), history
```

**Unit Tests** (`tests/test_nodes_base.py`):

| Test Case | Input | Expected Output |
|-----------|-------|-----------------|
| None mapping | `(("a", "b"), None, "inputs")` | `(("a", "b"), [])` |
| Empty mapping | `(("a", "b"), {}, "inputs")` | `(("a", "b"), [])` |
| Single rename | `(("a", "b"), {"a": "x"}, "inputs")` | `(("x", "b"), [RenameEntry("inputs", "a", "x")])` |
| Multiple renames | `(("a", "b"), {"a": "x", "b": "y"}, "inputs")` | `(("x", "y"), [RenameEntry(...), RenameEntry(...)])` |
| Rename non-existent | `(("a", "b"), {"c": "x"}, "inputs")` | `(("a", "b"), [RenameEntry("inputs", "c", "x")])` (no validation) |
| Outputs kind | `(("a",), {"a": "x"}, "outputs")` | `(("x",), [RenameEntry("outputs", "a", "x")])` |

---

### `HyperNode` - Functionality and Tests

**Functionality**: Abstract base class providing shared rename functionality.

**`_copy()` Implementation**:
```python
def _copy(self) -> Self:
    clone = copy.copy(self)
    clone._rename_history = list(self._rename_history)
    return clone
```

**`_with_renamed()` Implementation**:
```python
def _with_renamed(self, attr: str, mapping: dict[str, str]) -> Self:
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
    current = getattr(self, attr)
    for entry in self._rename_history:
        if entry.kind == attr and entry.old == name:
            return RenameError(
                f"'{name}' was renamed to '{entry.new}'. "
                f"Current {attr}: {current}"
            )
    return RenameError(f"'{name}' not found. Current {attr}: {current}")
```

**Unit Tests** (`tests/test_nodes_base.py`):

| Test Case | Expected Behavior |
|-----------|-------------------|
| Cannot instantiate directly | `HyperNode()` raises `TypeError` (abstract) |
| `with_name()` returns new instance | Original unchanged, new has updated name |
| `with_inputs()` with kwargs | `node.with_inputs(a="x")` renames a→x |
| `with_inputs()` with dict | `node.with_inputs({"a": "x"})` renames a→x |
| `with_inputs()` combined | `node.with_inputs({"a": "x"}, b="y")` renames both |
| `with_outputs()` same patterns | Same behavior as with_inputs |
| Rename non-existent raises | `node.with_inputs(nonexistent="x")` raises `RenameError` |
| Error shows history | After rename a→x, `with_inputs(a="y")` error mentions "was renamed to 'x'" |
| Chained renames track history | History contains all rename entries |
| `_copy()` independent history | Modifying clone's history doesn't affect original |

---

### `FunctionNode` - Functionality and Tests

**`__init__()` Implementation**:
```python
def __init__(
    self,
    source: Callable | FunctionNode,
    output_name: str | tuple[str, ...] | None = None,
    *,
    name: str | None = None,
    rename_inputs: dict[str, str] | None = None,
    cache: bool = False,
) -> None:
    # Extract func if source is FunctionNode
    func = source.func if isinstance(source, FunctionNode) else source

    self.func = func
    self.cache = cache
    self._definition_hash = hash_definition(func)

    # Core HyperNode attributes
    self.name = name or func.__name__
    self.outputs = ensure_tuple(output_name) if output_name else (func.__name__,)
    inputs = tuple(inspect.signature(func).parameters.keys())
    self.inputs, self._rename_history = _apply_renames(inputs, rename_inputs, "inputs")

    # Auto-detect execution mode
    self._is_async = (
        inspect.iscoroutinefunction(func) or
        inspect.isasyncgenfunction(func)
    )
    self._is_generator = (
        inspect.isgeneratorfunction(func) or
        inspect.isasyncgenfunction(func)
    )
```

**Unit Tests** (`tests/test_nodes_function.py`):

#### Construction Tests

| Test Case | Input | Expected |
|-----------|-------|----------|
| Basic sync function | `FunctionNode(lambda x: x)` | name="\<lambda\>", inputs=("x",), outputs=("\<lambda\>",) |
| With output_name string | `FunctionNode(foo, "result")` | outputs=("result",) |
| With output_name tuple | `FunctionNode(foo, ("a", "b"))` | outputs=("a", "b") |
| With custom name | `FunctionNode(foo, name="custom")` | name="custom" |
| With rename_inputs | `FunctionNode(foo, rename_inputs={"x": "y"})` | inputs contains "y" instead of "x" |
| With cache=True | `FunctionNode(foo, cache=True)` | cache is True |
| From FunctionNode | `FunctionNode(existing_node, "new")` | Extracts func, ignores old config |

#### Execution Mode Detection Tests

| Test Case | Function | is_async | is_generator |
|-----------|----------|----------|--------------|
| Sync function | `def foo(): pass` | False | False |
| Async function | `async def foo(): pass` | True | False |
| Sync generator | `def foo(): yield` | False | True |
| Async generator | `async def foo(): yield` | True | True |

#### Property Tests

| Test Case | Expected |
|-----------|----------|
| `definition_hash` is string | 64 character hex string |
| `definition_hash` is cached | Same value on repeated access |
| `is_async` returns bool | True or False |
| `is_generator` returns bool | True or False |

#### `__call__` Tests

| Test Case | Expected |
|-----------|----------|
| Delegates to func | `node(5)` returns `node.func(5)` |
| Passes args | `node(1, 2)` passes both args |
| Passes kwargs | `node(x=1)` passes kwargs |

#### `__repr__` Tests

| Test Case | Expected Format |
|-----------|-----------------|
| Default name | `FunctionNode(foo, outputs=('foo',))` |
| Custom outputs | `FunctionNode(foo, outputs=('result',))` |
| After with_name | `FunctionNode(foo as 'custom', outputs=('foo',))` |

---

### `node()` Decorator - Functionality and Tests

**Implementation**:
```python
def node(
    source: Callable | None = None,
    output_name: str | tuple[str, ...] | None = None,
    *,
    name: str | None = None,
    rename_inputs: dict[str, str] | None = None,
    cache: bool = False,
) -> FunctionNode | Callable[[Callable], FunctionNode]:
    def decorator(func: Callable) -> FunctionNode:
        return FunctionNode(
            source=func,
            output_name=output_name,
            name=name,
            rename_inputs=rename_inputs,
            cache=cache,
        )

    if source is not None:
        # Used without parentheses: @node
        return decorator(source)
    # Used with parentheses: @node(...)
    return decorator
```

**Unit Tests** (`tests/test_nodes_function.py`):

| Test Case | Syntax | Expected |
|-----------|--------|----------|
| Without parens | `@node` | Returns FunctionNode, outputs=(func_name,) |
| With empty parens | `@node()` | Returns FunctionNode, outputs=(func_name,) |
| With output_name | `@node(output_name="x")` | outputs=("x",) |
| With all params | `@node(output_name="x", name="y", cache=True)` | All params applied |
| Preserves function behavior | `@node def foo(x): return x * 2` | `foo(5) == 10` |

---

## Integration Tests

### End-to-End Workflow Tests (`tests/test_integration.py`)

| Test Scenario | Description |
|---------------|-------------|
| Create node, rename, call | Full workflow from decoration to execution |
| Chain multiple renames | `node.with_inputs(...).with_outputs(...).with_name(...)` |
| Error message accuracy | Verify error messages contain helpful context |
| Multiple nodes from same func | Different configs work independently |
| Async function node | Create and verify is_async detection |
| Generator function node | Create and verify is_generator detection |

---

## File Structure

```
src/hypergraph/
├── __init__.py          # Public exports: node, FunctionNode, HyperNode, RenameError
├── _utils.py            # ensure_tuple(), hash_definition()
└── nodes/
    ├── __init__.py      # Node module exports
    ├── base.py          # HyperNode, RenameEntry, RenameError, _apply_renames()
    └── function.py      # FunctionNode, node()

tests/
├── __init__.py
├── test_utils.py        # Tests for _utils.py
├── test_nodes_base.py   # Tests for nodes/base.py
├── test_nodes_function.py # Tests for nodes/function.py
└── test_integration.py  # End-to-end integration tests
```

---

## Implementation Notes

### Type Hints

- Use `Self` from `typing` for method return types that return the same class
- Use `Callable` from `typing` for function types
- Use `Literal` from `typing` for string literal unions
- Full type coverage for IDE autocompletion support

### Immutability Pattern

- All `with_*` methods return new instances
- Original nodes are never modified
- This enables safe reuse: same node can have different configurations in different contexts

### Error Message Design

- RenameError includes context from rename history
- Shows what the current values are
- If a name was previously renamed, mentions that fact
- Helps users understand what happened and how to fix it

### Performance Considerations

- `definition_hash` computed once at creation (cached in `_definition_hash`)
- `is_async` and `is_generator` computed once at creation
- `_copy()` does shallow copy; only `_rename_history` list is deep-copied

### Edge Cases

- Lambda functions: Work if defined in files (inspect.getsource works)
- Built-in functions: `hash_definition` raises ValueError
- Empty function: Valid, returns hash of empty body
- No parameters: `inputs = ()` (empty tuple)
- Positional-only `/` in `with_inputs`: Allows kwargs named "mapping"

---

## Public API Summary

```python
from hypergraph import node, FunctionNode, HyperNode, RenameError

# Decorator usage
@node
def simple(x): ...

@node(output_name="result")
def with_output(x): ...

@node(output_name=("a", "b"), cache=True)
def multi_output(x): ...

# Constructor usage
fn = FunctionNode(my_func, output_name="result", name="processor")

# Renaming
renamed = fn.with_inputs(x="input_data").with_outputs(result="output_data")

# Direct call
result = fn(42)

# Properties
fn.name         # str
fn.inputs       # tuple[str, ...]
fn.outputs      # tuple[str, ...]
fn.func         # Callable
fn.cache        # bool
fn.definition_hash  # str (64-char hex)
fn.is_async         # bool
fn.is_generator     # bool
```

---

## References

- `specs/reviewed/node-types.md` - Complete node type specification
- `specs/reviewed/graph.md` - Graph structure and hashing
- `specs/reviewed/state-model.md` - "Outputs ARE state" philosophy
- `.zenflow/tasks/node-implementation-00b4/requirements.md` - Product requirements
