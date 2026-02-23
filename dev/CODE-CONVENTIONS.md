# Code Conventions

How to write code that fits the hypergraph codebase.

## Error Messages

Three-part structure with actionable guidance:

```python
raise GraphConfigError(
    f"Route target '{target}' not found in graph.\n\n"
    f"Valid targets: {sorted(valid_targets)}\n\n"
    f"How to fix: Check spelling. Did you mean '{closest_match}'?"
)
```

**Rules**:
- Always include "How to fix:" with actionable guidance
- Include suggestions ("Did you mean 'X'?") where possible using fuzzy matching
- Show valid options when a value is invalid
- Show what was provided vs what was expected

## Immutability Pattern

All `with_*` methods follow this pattern:

```python
def with_name(self, name: str) -> Self:
    clone = self._copy()          # 1. shallow copy
    clone._name = name            # 2. modify the clone
    clone._invalidate_cached()    # 3. clear cached properties
    return clone                  # 4. return new instance
```

- `_copy()` → modify → `_invalidate_cached_properties()` → return
- Rename history tracked with batch IDs (see `_rename.py`)
- Never mutate `self` in a `with_*` method

## Type Hints

- `TypeVar("_T", bound="HyperNode")` for self-referential returns (`-> _T`)
- `type | None` for optional types (not `Optional[type]`)
- Positional-only params with `/` for ambiguous names
- Public API methods always have type hints
- Internal functions: type hints preferred but not mandatory

## Module Naming

- Internal modules: `_` prefix (`_callable.py`, `_rename.py`, `_conflict.py`, `_shared/`)
- Public modules: no prefix
- Everything in `__init__.py` with `__all__` is public API
- **Cross-module internal APIs**: Functions within `_shared/` that are imported across sibling modules (e.g., `validation.py` → `template_sync.py`) should **not** have a `_` prefix. The underscore signals "don't depend on this" — if multiple modules already depend on it, drop the underscore. Example: `resolve_runtime_selected` (not `_resolve_runtime_selected`).

## Naming Conventions

| Entity | Convention | Example |
|--------|-----------|---------|
| Node types | PascalCase, descriptive | `FunctionNode`, `GraphNode`, `RouteNode` |
| Decorators | lowercase, verb-like | `@node`, `@route`, `@ifelse`, `@interrupt` |
| Properties | snake_case, `@cached_property` when expensive | `input_spec`, `output_names` |
| Internal helpers | `_` prefix (see [Module Naming](#module-naming) for `_shared/` exception) | `_resolve_outputs()`, `_validate_names()` |
| Constants | UPPER_SNAKE_CASE | `END` |

**Reserved characters**: `.` and `/` cannot appear in node or output names.

## Docstrings

Follow `Args → Returns → Raises → Note → Example` order:

```python
def bind(self, **values: Any) -> "Graph":
    """Return a new graph with bound parameter values.

    Args:
        **values: Parameter names and their bound values.

    Returns:
        A new Graph with the bindings applied.

    Raises:
        GraphConfigError: If a parameter name doesn't match any node input.

    Note:
        Bound values are shared (not deep-copied) across runs.
    """
```

## Events

Frozen dataclasses with `span_id` / `parent_span_id` envelope:

```python
@dataclass(frozen=True)
class NodeStartEvent(BaseEvent):
    node_name: str
    inputs: dict[str, Any]
```

## Exception Hierarchy

| Exception | When |
|-----------|------|
| `GraphConfigError` | Build-time: invalid graph structure |
| `MissingInputError` | Run-time: required input not provided |
| `InfiniteLoopError` | Run-time: exceeded max_iterations |
| `IncompatibleRunnerError` | Run-time: runner can't handle graph features |
| `ExecutionError` | Run-time: wraps node exception with partial state |
| `RenameError` | Build-time: invalid rename operation |
