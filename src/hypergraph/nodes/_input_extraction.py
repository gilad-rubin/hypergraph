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

# Node functions are invoked with keyword arguments (func(**inputs)), so
# parameters that cannot be addressed by keyword can never receive a value.
_UNSUPPORTED_PARAM_KINDS: dict[inspect._ParameterKind, str] = {
    inspect.Parameter.POSITIONAL_ONLY: "positional-only",
    inspect.Parameter.VAR_POSITIONAL: "variadic positional (*args)",
    inspect.Parameter.VAR_KEYWORD: "variadic keyword (**kwargs)",
}


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
        TypeError: If the signature contains a positional-only, ``*args``,
            or ``**kwargs`` parameter. Nodes are invoked with keyword
            arguments, so such parameters could never receive a value.
    """
    sig = inspect.signature(func)
    _reject_non_keyword_callable_params(func, sig)
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


def _reject_non_keyword_callable_params(func: Callable, sig: inspect.Signature) -> None:
    """Reject signatures the framework could never invoke with keyword args."""
    offending = [(name, _UNSUPPORTED_PARAM_KINDS[param.kind]) for name, param in sig.parameters.items() if param.kind in _UNSUPPORTED_PARAM_KINDS]
    if not offending:
        return

    offending_str = "\n".join(f"  -> parameter '{name}' is {kind}" for name, kind in offending)
    raise TypeError(
        f"Function '{func.__name__}' has parameter(s) that cannot be called by keyword:\n\n"
        f"{offending_str}\n\n"
        f"Hypergraph calls node functions with keyword arguments, so every parameter\n"
        f"must be a regular or keyword-only parameter.\n\n"
        f"How to fix:\n"
        f"  Declare each input as a named parameter, e.g. def {func.__name__}(a, b) or\n"
        f"  def {func.__name__}(a, *, b). Remove '/', '*args', and '**kwargs' from the signature."
    )


def _safe_get_type_hints(func: Callable) -> dict:
    """get_type_hints that never raises (returns {} on failure)."""
    try:
        return get_type_hints(func)
    except Exception:
        return {}


def _is_injectable(hint: type) -> bool:
    """Check if a type hint matches a registered injectable type."""
    return hint in _INJECTABLE_TYPES
