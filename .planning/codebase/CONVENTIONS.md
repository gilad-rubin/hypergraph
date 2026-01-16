# Coding Conventions

**Analysis Date:** 2026-01-16

## Naming Patterns

**Files:**
- snake_case for modules: `graph.py`, `function.py`, `base.py`
- Leading underscore for internal modules: `_utils.py`, `_rename.py`
- Test files: `test_<module_name>.py`

**Functions:**
- snake_case: `ensure_tuple()`, `hash_definition()`, `_resolve_outputs()`
- Leading underscore for internal/private: `_warn_if_has_return_annotation()`, `_apply_renames()`

**Variables:**
- snake_case: `output_name`, `rename_inputs`, `edge_produced`
- Leading underscore for internal state: `_bound`, `_nodes`, `_cached_hash`

**Types:**
- PascalCase for classes: `HyperNode`, `FunctionNode`, `GraphNode`, `InputSpec`
- PascalCase for exceptions: `GraphConfigError`, `RenameError`
- ALL_CAPS not observed (no constants defined)

**Type Variables:**
- Single uppercase letter with bound: `_T = TypeVar("_T", bound="HyperNode")`

## Code Style

**Formatting:**
- No explicit formatter config in project root
- Uses standard Python formatting (4-space indentation)
- Line length appears to be ~88-100 characters

**Linting:**
- `.ruff_cache/` present, suggesting ruff is used
- No explicit ruff.toml in project root

**Type Hints:**
- Comprehensive type hints throughout codebase
- Uses `from __future__ import annotations` for forward references
- Uses `TYPE_CHECKING` guard for circular imports:
```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hypergraph.graph import Graph
```

## Import Organization

**Order:**
1. `from __future__ import annotations` (when needed)
2. Standard library (`hashlib`, `inspect`, `warnings`, `copy`)
3. Third-party (`networkx as nx`)
4. Local package imports

**Examples from `src/hypergraph/graph.py`:**
```python
from __future__ import annotations

import hashlib
import networkx as nx
from dataclasses import dataclass
from typing import Any, Iterator, TYPE_CHECKING

from hypergraph.nodes.base import HyperNode
```

**Path Aliases:**
- No path aliases configured
- Uses relative package imports: `from hypergraph.nodes.base import HyperNode`

## Error Handling

**Patterns:**
- Custom exceptions inherit from `Exception` directly
- Exception message format includes context, problem, and "How to fix" guidance:
```python
raise GraphConfigError(
    f"Duplicate node name: '{node.name}'\n\n"
    f"  -> First defined: {result[node.name]}\n"
    f"  -> Also defined: {node}\n\n"
    f"How to fix: Rename one of the nodes"
)
```

**Validation approach:**
- Validate at construction time (fail fast)
- Use descriptive messages with context
- Include actionable fix suggestions

**Try/except patterns:**
- Catch specific exceptions, not bare `except:`
```python
try:
    hints = get_type_hints(func)
except Exception:
    # get_type_hints can fail on some edge cases, skip warning
    return
```

## Logging

**Framework:** None - uses `warnings.warn()` for user-facing warnings

**Patterns:**
- Use `warnings.warn()` with `UserWarning` for recoverable issues:
```python
warnings.warn(
    f"Function '{func.__name__}' has return type '{return_hint}' but no output_name. "
    f"If you want to capture the return value, use @node(output_name='...'). "
    f"Otherwise, ignore this warning for side-effect only nodes.",
    UserWarning,
    stacklevel=4,
)
```

## Comments

**When to Comment:**
- Module-level docstring explaining purpose
- Class-level docstrings with attributes and examples
- Method docstrings with Args/Returns/Raises
- Inline comments for non-obvious logic

**Docstring Style:**
- Google-style docstrings with sections: Args, Returns, Raises, Example, Note, Warning
- Examples use `>>> ` format (doctest compatible):
```python
"""Wrap graph as node for composition. Returns new GraphNode.

Args:
    name: Optional node name. If not provided, uses graph.name.

Returns:
    GraphNode wrapping this graph

Raises:
    ValueError: If name is None and graph.name is None
"""
```

## Function Design

**Size:**
- Functions are small and focused (typically 5-20 lines)
- Complex operations split into helper methods

**Parameters:**
- Use positional-only (`/`) when parameter names might conflict:
```python
def with_inputs(
    self: _T,
    mapping: dict[str, str] | None = None,
    /,
    **kwargs: str,
) -> _T:
```
- Keyword-only (`*`) for optional configuration parameters

**Return Values:**
- Single return type when possible
- Use tuples for multiple returns: `tuple[tuple[str, ...], list[RenameEntry]]`
- None return for side-effect functions

## Module Design

**Exports:**
- Explicit `__all__` in all public modules:
```python
__all__ = [
    "HyperNode",
    "RenameEntry",
    "RenameError",
    "FunctionNode",
    "GraphNode",
    "node",
]
```

**Barrel Files:**
- Package `__init__.py` re-exports public API
- `src/hypergraph/__init__.py` exports top-level symbols
- `src/hypergraph/nodes/__init__.py` exports all node types

## Class Design

**Dataclasses:**
- Use `@dataclass(frozen=True)` for immutable value objects:
```python
@dataclass(frozen=True)
class InputSpec:
    required: tuple[str, ...]
    optional: tuple[str, ...]
    seeds: tuple[str, ...]
    bound: dict[str, Any]
```

- Use `@dataclass(frozen=True)` for tracking records:
```python
@dataclass(frozen=True)
class RenameEntry:
    kind: Literal["name", "inputs", "outputs"]
    old: str
    new: str
```

**Abstract Base Classes:**
- Use `ABC` for abstract classes
- Prevent direct instantiation via `__new__`:
```python
def __new__(cls, *args, **kwargs):
    if cls is HyperNode:
        raise TypeError("HyperNode cannot be instantiated directly")
    return super().__new__(cls)
```

**Immutability Pattern:**
- All `with_*` methods return new instances
- Use `_copy()` helper with shallow copy + selective deep copy:
```python
def _copy(self: _T) -> _T:
    clone = copy.copy(self)
    clone._rename_history = list(self._rename_history)
    return clone
```

**Properties:**
- Use `@property` for computed values that should feel like attributes
- Cache expensive computations with private attributes:
```python
@property
def definition_hash(self) -> str:
    if self._cached_hash is None:
        self._cached_hash = self._compute_definition_hash()
    return self._cached_hash
```

## Decorator Pattern

**Multi-form decorators:**
- Support both `@decorator` and `@decorator(args)` syntax:
```python
def node(
    source: Callable | None = None,
    output_name: str | tuple[str, ...] | None = None,
    *,
    rename_inputs: dict[str, str] | None = None,
    cache: bool = False,
) -> FunctionNode | Callable[[Callable], FunctionNode]:
    def decorator(func: Callable) -> FunctionNode:
        return FunctionNode(...)

    if source is not None:
        return decorator(source)  # @node
    return decorator  # @node(...)
```

---

*Convention analysis: 2026-01-16*
