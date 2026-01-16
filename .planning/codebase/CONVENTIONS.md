# Coding Conventions

**Analysis Date:** 2026-01-16

## Naming Patterns

**Files:**
- Module names: lowercase with underscores (e.g., `graph_node.py`, `_typing.py`)
- Private modules: prefixed with underscore (e.g., `_utils.py`, `_typing.py`, `_rename.py`)
- Test files: `test_<module>.py` (e.g., `test_graph.py`, `test_nodes_function.py`)

**Functions:**
- snake_case for all functions and methods
- Private methods prefixed with underscore: `_build_graph()`, `_validate_types()`
- Helper functions: descriptive verb phrases: `ensure_tuple()`, `hash_definition()`

**Variables:**
- snake_case for local variables and parameters
- UPPER_CASE for module-level constants (though none currently exist)

**Classes:**
- PascalCase: `HyperNode`, `FunctionNode`, `GraphNode`, `InputSpec`
- Exception classes end with `Error`: `RenameError`, `GraphConfigError`

**Types:**
- TypeVars use single uppercase: `_T = TypeVar("_T", bound="HyperNode")`

## Code Style

**Formatting:**
- No explicit formatter configured in pyproject.toml
- Use Ruff for linting (configured implicitly via `.ruff_cache/` presence)
- Line length appears to be ~88-100 characters

**Type Hints:**
- Use `from __future__ import annotations` at top of all modules
- Use modern union syntax: `str | None` instead of `Optional[str]`
- Use `TYPE_CHECKING` blocks for circular imports:
```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hypergraph.graph import Graph
```

**Docstrings:**
- Google-style docstrings with Args, Returns, Raises, Example sections
- Class docstrings include Attributes section listing public attributes
- All public functions and classes have docstrings
- Examples use doctests format with `>>>` prefix

Example docstring pattern:
```python
def method_name(self, param: str) -> str:
    """Brief description of method.

    Longer description if needed that explains behavior,
    edge cases, or important notes.

    Args:
        param: Description of parameter

    Returns:
        Description of return value.

    Raises:
        ErrorType: When this error occurs.

    Example:
        >>> obj.method_name("value")
        'result'
    """
```

## Import Organization

**Order:**
1. `from __future__ import annotations` (always first when present)
2. Standard library imports (sorted alphabetically)
3. Third-party imports (e.g., `networkx`)
4. Local/relative imports

**Import Style:**
- Explicit imports preferred over `import *`
- Group related imports from same module:
```python
from typing import Any, Callable, TypeVar
```

**Path Aliases:**
- No path aliases configured
- Use relative imports within package: `from hypergraph.nodes.base import HyperNode`

## Error Handling

**Patterns:**
- Use custom exception classes inheriting from `Exception`
- Error messages include context and "How to fix" guidance:
```python
raise GraphConfigError(
    f"Duplicate node name: '{node.name}'\n\n"
    f"  -> First defined: {result[node.name]}\n"
    f"  -> Also defined: {node}\n\n"
    f"How to fix: Rename one of the nodes"
)
```

- Use `try/except` with specific exceptions, not bare `except:`
- Return sensible defaults or empty structures when safe:
```python
try:
    hints = get_type_hints(func)
except Exception:
    return {}
```

## Logging

**Framework:** None configured - using `warnings` module for non-critical issues

**Patterns:**
- Use `warnings.warn()` for deprecation or potential issues:
```python
warnings.warn(
    f"Function '{func.__name__}' has return type but no output_name. "
    f"Use @node(output_name='...') to capture the return value.",
    UserWarning,
    stacklevel=4,
)
```

## Comments

**When to Comment:**
- Comments explain "why" not "what"
- Type ignore comments include reason: `# type: ignore[arg-type]`
- TODO/FIXME comments for known technical debt (minimal in codebase)

**Inline Comments:**
- Place on same line as code, separated by two spaces
- Keep brief and relevant

## Function Design

**Size:**
- Keep functions focused on single responsibility
- Split large validation/construction into private helpers
- Example from `graph.py`: `_validate()` delegates to specific validators

**Parameters:**
- Use keyword-only args after `*` for optional configuration
- Use positional-only args with `/` to prevent keyword conflicts:
```python
def with_inputs(
    self: _T,
    mapping: dict[str, str] | None = None,
    /,
    **kwargs: str,
) -> _T:
```

**Return Values:**
- Return immutable types (tuples) for collections exposed via properties
- Return `self` type for fluent/chained methods using TypeVar
- Return copies to prevent mutation of internal state

## Module Design

**Exports:**
- Use `__all__` in `__init__.py` to define public API
- Keep `__all__` minimal - only user-facing classes/functions

**Package Structure:**
- One primary class per module (e.g., `Graph` in `graph.py`)
- Helper functions can live in same module or `_utils.py`
- Private modules prefixed with underscore

## Class Design

**Immutability:**
- Prefer immutable patterns: `with_*` methods return new instances
- Use `frozen=True` for dataclasses: `@dataclass(frozen=True)`
- Store internal state as private attributes (`_nodes`, `_bound`)

**Properties:**
- Use `@property` for computed/derived values
- Cache expensive computations in private attributes
- Return defensive copies of mutable internal state:
```python
@property
def nodes(self) -> dict[str, HyperNode]:
    return dict(self._nodes)  # Return copy
```

**Abstract Base Classes:**
- Use ABC for interface definitions (`HyperNode`)
- Override `__new__` to prevent direct instantiation of ABC
- Define abstract interface in docstring, not via `@abstractmethod`

## Decorator Patterns

**Flexible Decorator:**
Support both `@decorator` and `@decorator(args)` syntax:
```python
def node(
    source: Callable | None = None,
    output_name: str | tuple[str, ...] | None = None,
    *,
    cache: bool = False,
) -> FunctionNode | Callable[[Callable], FunctionNode]:
    def decorator(func: Callable) -> FunctionNode:
        return FunctionNode(source=func, output_name=output_name, cache=cache)

    if source is not None:
        return decorator(source)  # @node without parens
    return decorator  # @node(...) with parens
```

---

*Convention analysis: 2026-01-16*
