# Coding Conventions

**Analysis Date:** 2026-01-21

## Naming Patterns

**Files:**
- Module files use snake_case: `function.py`, `graph_node.py`, `input_spec.py`
- Private/internal modules prefixed with underscore: `_rename.py`, `_typing.py`, `_utils.py`
- Test files follow pattern: `test_*.py` for unit tests, organized in `tests/` directory
- Subdirectories use lowercase: `nodes/`, `runners/`, `graph/`, `viz/`

**Functions:**
- Public functions and decorators use snake_case: `node()`, `route()`, `ifelse()`
- Helper functions prefixed with underscore: `_resolve_outputs()`, `_warn_if_has_return_annotation()`, `_build_forward_rename_map()`
- Private module-level functions use underscore: `_validate_graph_name()`, `_make_sync_func()`
- Class methods use camelCase for properties, snake_case for regular methods: `definition_hash` (property), `get_input_type()` (method)

**Variables:**
- Local variables use snake_case: `output_name`, `rename_map`, `node_types`
- Constants use UPPER_SNAKE_CASE: `SHADOW_OFFSET`, `GRAPH_PADDING`, `HEADER_HEIGHT`
- Private instance attributes prefixed with underscore: `_cache`, `_definition_hash`, `_is_async`, `_rename_history`

**Types:**
- Type aliases use PascalCase: `NodeExecutor`, `AsyncNodeExecutor` (Protocol classes)
- Enum classes use PascalCase: `Runner`, `NodeType`, `Topology`, `MapMode`
- Enum members use UPPER_SNAKE_CASE: `SYNC_FUNC`, `ASYNC_GENERATOR`

## Code Style

**Formatting:**
- Line length: No explicit limit enforced, but code tends toward 80-100 character practical limit
- Indentation: 4 spaces per level (PEP 8 standard)
- String quotes: Double quotes for docstrings and error messages, single or double for regular strings (consistent within module)

**Linting:**
- No explicit linting configuration found in pyproject.toml
- Use `from __future__ import annotations` at top of all modules for forward compatibility (Python 3.10+)
- Type hints used extensively throughout codebase

**Imports:**
- Standard library imports first
- Third-party imports (networkx) second
- Local imports last
- Each import on separate line (except multiple items from same module can be grouped)
- Examples from `function.py`:
  ```python
  from __future__ import annotations

  import inspect
  import warnings
  from typing import Any, Callable, get_type_hints

  from hypergraph._utils import ensure_tuple, hash_definition
  from hypergraph.nodes._rename import _apply_renames, build_reverse_rename_map
  from hypergraph.nodes.base import HyperNode
  ```

## Import Organization

**Order:**
1. `from __future__ import annotations` (always first)
2. Standard library imports
3. Third-party imports (networkx, typing, etc.)
4. Local hypergraph imports

**Path Aliases:**
- No path aliases observed; all imports use full relative paths
- Imports follow module structure: `from hypergraph.nodes.base import HyperNode`

**Conditional Imports:**
- `TYPE_CHECKING` blocks used for circular dependency avoidance:
  ```python
  if TYPE_CHECKING:
      from hypergraph.nodes.graph_node import GraphNode
  ```

## Error Handling

**Patterns:**
- Custom exceptions inherit from base Python exceptions: `class GraphConfigError(Exception):`
- Exceptions include descriptive messages with "How to fix:" guidance
- Multi-line error messages use formatted strings with context:
  ```python
  raise GraphConfigError(
      f"Invalid graph name: '{graph_name}'\n\n"
      f"  -> Graph names cannot contain '{char}'\n\n"
      f"How to fix:\n"
      f"  Use underscores or hyphens instead"
  )
  ```
- Exceptions stored in `exceptions.py` with custom attributes for programmatic access:
  ```python
  class MissingInputError(Exception):
      def __init__(self, missing: list[str], provided: list[str] | None = None):
          self.missing = missing
          self.provided = provided or []
  ```
- Broad exception catching only where necessary, with explicit comment:
  ```python
  try:
      hints = get_type_hints(func)
  except Exception:
      # get_type_hints can fail on forward references, etc.
      return {}
  ```

## Logging

**Framework:** `warnings.warn()` from standard library, no custom logging framework

**Patterns:**
- User-facing warnings for potentially problematic code:
  ```python
  warnings.warn(
      f"Function '{func.__name__}' has return type '{return_hint}' but no output_name. "
      f"If you want to capture the return value, use @node(output_name='...'). "
      f"Otherwise, ignore this warning for side-effect only nodes.",
      UserWarning,
      stacklevel=4,
  )
  ```
- `stacklevel` set to point warning to user code (not library internals)

## Comments

**When to Comment:**
- Explain non-obvious design decisions or tradeoffs
- Clarify complex algorithms (e.g., rename mapping, constraint validation)
- Mark known limitations or TODOs
- Comment when "why" is not obvious from code

**JSDoc/TSDoc:**
- Docstrings on all public classes and functions
- Google-style docstring format with sections: Summary, Args, Returns, Raises, Examples
- Example from `function.py`:
  ```python
  def _resolve_outputs(
      func: Callable,
      output_name: str | tuple[str, ...] | None,
  ) -> tuple[str, ...]:
      """Resolve output names, warning if return annotation exists without output_name.

      Args:
          func: The wrapped function
          output_name: User-provided output name(s), or None for side-effect only

      Returns:
          Tuple of output names (empty for side-effect only nodes)
      """
  ```
- Class docstrings include detailed description of behavior:
  ```python
  class FunctionNode(HyperNode):
      """Wraps a Python function as a graph node.

      Created via the @node decorator or FunctionNode() constructor.
      Supports all four execution modes: sync, async, sync generator,
      and async generator.

      Attributes:
          name: Public node name (default: func.__name__)
          inputs: Input parameter names from function signature
          outputs: Output value names (empty tuple if no output_name)

      Properties:
          definition_hash: SHA256 hash of function source (cached)
          is_async: True if async def or async generator
  ```

## Function Design

**Size:**
- Helper functions are typically 10-30 lines
- Complex functions extracted into multiple focused helpers
- Example: `_resolve_outputs()` delegates output resolution, and `_warn_if_has_return_annotation()` is extracted for specific behavior

**Parameters:**
- Parameters use type hints: `func: Callable`, `output_name: str | tuple[str, ...] | None`
- Use union types with `|` operator (Python 3.10+ with `from __future__ import annotations`)
- Optional parameters go after required ones
- Keyword-only parameters use `*` separator when useful: `def __init__(self, source, name=None, *, rename_inputs=None, cache=False)`

**Return Values:**
- All functions have explicit return type hints: `-> tuple[str, ...]`, `-> dict[str, Any]`
- Side-effect only functions use `-> None`
- Multiple return values use tuple type hints: `-> tuple[dict[str, str], list[RenameEntry]]`

**Pure Functions Preferred:**
- Most node processing functions avoid side effects
- Immutability patterns used throughout: methods like `with_name()`, `with_inputs()` return new instances rather than mutating

## Module Design

**Exports:**
- Public API defined in module `__init__.py` using `__all__`:
  ```python
  __all__ = [
      "node",
      "ifelse",
      "route",
      "FunctionNode",
      "GraphNode",
      "Graph",
      "InputSpec",
      "SyncRunner",
      "AsyncRunner",
  ]
  ```
- Only public classes and functions exported, helper functions remain private

**Barrel Files:**
- Main package `__init__.py` re-exports from submodules for clean API:
  ```python
  from hypergraph.graph import Graph, GraphConfigError, InputSpec
  from hypergraph.nodes import (
      END,
      FunctionNode,
      GateNode,
      GraphNode,
      HyperNode,
      IfElseNode,
      RenameError,
      RouteNode,
      ifelse,
      node,
      route,
  )
  ```
- Each subpackage has its own `__init__.py` organizing exports

## Docstring Examples

Real examples from `function.py`:

**With Raises:**
```python
def get_default_for(self, param: str) -> Any:
    """Get default value for a parameter.

    Args:
        param: Input parameter name (using current/renamed name)

    Returns:
        The default value.

    Raises:
        KeyError: If parameter has no default.
    """
```

**With Complex Type Hint:**
```python
def map_inputs_to_params(self, inputs: dict[str, Any]) -> dict[str, Any]:
    """Map renamed input names back to original function parameter names.

    Handles chained renames: if a->x->z (via separate calls), z maps to a.
    Handles parallel renames: if x->y, y->z (same call), they don't chain.

    Args:
        inputs: Dict with current (potentially renamed) input names as keys

    Returns:
        Dict with original function parameter names as keys
    """
```

---

*Convention analysis: 2026-01-21*
