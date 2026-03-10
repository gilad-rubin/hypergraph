"""Input extraction with framework-injectable type filtering.

Separates function parameters into graph inputs and injectable context
parameters (e.g., NodeContext). Uses type-hint detection following the
FastAPI Depends() pattern — see dev/ARCHITECTURE.md "Framework Context Injection".
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import get_type_hints

# Registry of types the framework injects at execution time.
# Currently only NodeContext; kept as a set for future extensibility.
_INJECTABLE_TYPES: set[type] = set()


def register_injectable(cls: type) -> None:
    """Register a type as framework-injectable (excluded from node.inputs)."""
    _INJECTABLE_TYPES.add(cls)


def extract_inputs(func: Callable) -> tuple[tuple[str, ...], str | None]:
    """Extract graph input names from a function signature.

    Parameters annotated with a registered injectable type (e.g., NodeContext)
    are excluded from the returned inputs and returned separately.

    Args:
        func: The function to inspect.

    Returns:
        (inputs, context_param): tuple of input names, and the name of the
        injectable parameter (or None if not present).

    Raises:
        TypeError: If more than one injectable parameter is declared.
    """
    sig = inspect.signature(func)
    hints = _safe_get_type_hints(func)

    inputs: list[str] = []
    context_param: str | None = None

    for name in sig.parameters:
        hint = hints.get(name)
        if hint is not None and _is_injectable(hint):
            if context_param is not None:
                raise TypeError(
                    f"Function '{func.__name__}' has multiple injectable parameters: '{context_param}' and '{name}'. Only one is allowed."
                )
            context_param = name
        else:
            inputs.append(name)

    return tuple(inputs), context_param


def _safe_get_type_hints(func: Callable) -> dict:
    """get_type_hints that never raises (returns {} on failure)."""
    try:
        return get_type_hints(func)
    except Exception:
        return {}


def _is_injectable(hint: type) -> bool:
    """Check if a type hint matches a registered injectable type."""
    return hint in _INJECTABLE_TYPES
