"""Type compatibility utilities for hypergraph.

This module provides type checking utilities for validating type compatibility
between connected nodes in a graph. Adapted from pipefunc patterns with
simplifications for standard library only.

Key exports:
    - is_type_compatible: Check if an outgoing type satisfies an incoming type
    - NoAnnotation: Marker for missing type annotations (skip check)
    - Unresolvable: Wrapper for unresolvable type hints (warn, skip check)
    - TypeCheckMemo: Context for forward reference resolution
    - safe_get_type_hints: Wrapper around get_type_hints with graceful fallback
"""

from __future__ import annotations

import sys
import warnings
from types import UnionType
from typing import (
    Annotated,
    Any,
    Callable,
    ForwardRef,
    Literal,
    NamedTuple,
    TypeVar,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

try:
    from typing import evaluate_forward_ref as _evaluate_forward_ref_public
except ImportError:
    _evaluate_forward_ref_public = None  # type: ignore[misc,assignment]


class NoAnnotation:
    """Marker class for missing type annotations.

    When a parameter or return value has no type annotation, this marker
    is used to indicate that type checking should be skipped for that value.

    Example:
        >>> from hypergraph._typing import is_type_compatible, NoAnnotation
        >>> is_type_compatible(int, NoAnnotation)
        True
        >>> is_type_compatible(NoAnnotation, str)
        True
    """


class Unresolvable:
    """Wrapper for unresolvable type hints.

    When a forward reference cannot be resolved (e.g., refers to an undefined
    class), it is wrapped in this class. Type comparisons involving Unresolvable
    emit a warning and return True (skipping the check).

    Attributes:
        type_str: The original string representation of the unresolvable type

    Example:
        >>> from hypergraph._typing import Unresolvable
        >>> u = Unresolvable("SomeUndefinedClass")
        >>> u.type_str
        'SomeUndefinedClass'
        >>> repr(u)
        "Unresolvable['SomeUndefinedClass']"
    """

    def __init__(self, type_str: str) -> None:
        """Initialize the Unresolvable instance.

        Args:
            type_str: String representation of the unresolvable type hint
        """
        self.type_str = type_str

    def __repr__(self) -> str:
        """Return a string representation."""
        return f"Unresolvable['{self.type_str}']"

    def __eq__(self, other: object) -> bool:
        """Check equality between Unresolvable instances."""
        if isinstance(other, Unresolvable):
            return self.type_str == other.type_str
        return False

    def __hash__(self) -> int:
        """Make Unresolvable hashable."""
        return hash(self.type_str)


class TypeCheckMemo(NamedTuple):
    """Context for forward reference resolution.

    Stores globals and locals namespaces needed to resolve forward references
    (string annotations or ForwardRef objects).

    Attributes:
        globals: Global namespace dict for resolution (or None)
        locals: Local namespace dict for resolution (or None)

    Example:
        >>> from hypergraph._typing import TypeCheckMemo
        >>> memo = TypeCheckMemo(globals={"MyClass": MyClass}, locals=None)
    """

    globals: dict[str, Any] | None
    locals: dict[str, Any] | None


def _evaluate_forwardref(ref: ForwardRef, memo: TypeCheckMemo) -> Any:
    """Evaluate a forward reference using the provided memo.

    Uses the public evaluate_forward_ref API when available (Python 3.14+),
    falling back to the private ForwardRef._evaluate for older versions.

    Args:
        ref: The ForwardRef to evaluate
        memo: Context containing globals/locals for resolution

    Returns:
        The resolved type

    Raises:
        NameError: If the forward reference cannot be resolved
    """
    if _evaluate_forward_ref_public is not None:
        return _evaluate_forward_ref_public(
            ref, globals=memo.globals, locals=memo.locals, type_params=()
        )
    # Fallback for Python < 3.14
    if sys.version_info < (3, 13):
        return ref._evaluate(memo.globals, memo.locals, recursive_guard=frozenset())
    return ref._evaluate(
        memo.globals, memo.locals, recursive_guard=frozenset(), type_params={}
    )


def _resolve_type(type_: Any, memo: TypeCheckMemo) -> Any:
    """Resolve forward references in a type hint.

    Recursively resolves forward references (strings or ForwardRef objects)
    and handles generic types by resolving their arguments.

    Args:
        type_: The type to resolve (may contain forward refs)
        memo: Context for forward reference resolution

    Returns:
        The resolved type (with all forward refs evaluated)
    """
    if isinstance(type_, str):
        return _evaluate_forwardref(ForwardRef(type_), memo)
    if isinstance(type_, ForwardRef):
        return _evaluate_forwardref(type_, memo)

    origin = get_origin(type_)
    if origin:
        args = get_args(type_)

        # Handle Literal specially - its arguments are VALUES, not types
        # Don't try to resolve strings like "a" or "b" as forward references
        if origin is Literal:
            return type_

        # Handle Annotated specially - only resolve the primary type, keep metadata as-is
        if origin is Annotated:
            if args:
                primary = _resolve_type(args[0], memo)
                # Reconstruct Annotated with resolved primary type and original metadata
                return Annotated[(primary, *args[1:])]
            return type_

        resolved_args = tuple(_resolve_type(arg, memo) for arg in args)
        # Handle both Union and UnionType (| syntax)
        if origin in {Union, UnionType}:
            return Union[resolved_args]
        return origin[resolved_args]

    return type_


def safe_get_type_hints(
    func: Callable[..., Any],
    include_extras: bool = False,
) -> dict[str, Any]:
    """Safely get type hints for a function, handling failures gracefully.

    Wrapper around `get_type_hints()` that catches failures and wraps
    unresolvable hints in the Unresolvable class.

    Args:
        func: The function to get type hints from
        include_extras: If True, preserve Annotated metadata

    Returns:
        dict mapping parameter names (and 'return') to resolved types.
        Unresolvable hints are wrapped in Unresolvable class.

    Example:
        >>> def greet(name: str) -> str: return f"Hello {name}"
        >>> safe_get_type_hints(greet)
        {'name': str, 'return': str}
    """
    try:
        hints = get_type_hints(func, include_extras=include_extras)
    except Exception:
        # Fall back to raw __annotations__ on failure
        hints = getattr(func, "__annotations__", {})

    _globals = getattr(func, "__globals__", {})
    memo = TypeCheckMemo(globals=_globals, locals=None)

    resolved_hints: dict[str, Any] = {}
    for arg, hint in hints.items():
        # Convert None literal to type(None) for proper comparison
        processed_hint = type(None) if hint is None else hint
        try:
            resolved = _resolve_type(processed_hint, memo)
            if resolved is None:
                resolved = type(None)
            resolved_hints[arg] = resolved
        except (NameError, Exception):
            resolved_hints[arg] = Unresolvable(str(processed_hint))

    return resolved_hints


# ---------------------------------------------------------------------------
# Type compatibility checking
# ---------------------------------------------------------------------------


def _check_identical_or_any(incoming_type: Any, required_type: Any) -> bool:
    """Check if types are identical, Any, or should skip check.

    Returns True (compatible) if:
    - Types are identical (==)
    - Required type is Any (accepts anything)
    - Either type is NoAnnotation (skip check)
    - Either type is Unresolvable (warn and skip check)

    Args:
        incoming_type: The type being provided (e.g., output type)
        required_type: The type being required (e.g., input type)

    Returns:
        True if types are compatible via identity/Any/skip rules
    """
    # Handle Unresolvable - warn and skip
    for t in (incoming_type, required_type):
        if isinstance(t, Unresolvable):
            warnings.warn(
                f"Unresolvable type hint: '{t.type_str}'. Skipping type comparison.",
                stacklevel=4,
            )
            return True

    return (
        incoming_type == required_type
        or required_type is Any
        or incoming_type is NoAnnotation
        or required_type is NoAnnotation
    )


def _all_types_compatible(
    incoming_args: tuple[Any, ...],
    required_args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> bool:
    """Check if all incoming types are compatible with some required type.

    Used for Union-to-Union compatibility: each member of incoming must
    be compatible with at least one member of required.

    Args:
        incoming_args: Types from the incoming Union
        required_args: Types from the required Union
        memo: Context for forward reference resolution

    Returns:
        True if every incoming type is compatible with some required type
    """
    return all(
        any(is_type_compatible(t1, t2, memo) for t2 in required_args)
        for t1 in incoming_args
    )


def _handle_union_types(
    incoming_type: Any,
    required_type: Any,
    memo: TypeCheckMemo,
) -> bool | None:
    """Handle compatibility for Union types (both Union and | syntax).

    Logic:
    - Both Union: all incoming members must be compatible with some required member
    - Incoming Union only: all members must be compatible with required
    - Required Union only: incoming must be compatible with some member

    Args:
        incoming_type: The type being provided
        required_type: The type being required
        memo: Context for forward reference resolution

    Returns:
        True/False if Union logic applies, None if neither is a Union
    """
    incoming_is_union = isinstance(incoming_type, UnionType) or get_origin(
        incoming_type
    ) is Union
    required_is_union = isinstance(required_type, UnionType) or get_origin(
        required_type
    ) is Union

    # Both are Union types
    if incoming_is_union and required_is_union:
        incoming_args = get_args(incoming_type)
        required_args = get_args(required_type)
        return _all_types_compatible(incoming_args, required_args, memo)

    # Only incoming is Union: all members must satisfy required
    if incoming_is_union:
        return all(
            is_type_compatible(t, required_type, memo)
            for t in get_args(incoming_type)
        )

    # Only required is Union: incoming must satisfy some member
    if required_is_union:
        return any(
            is_type_compatible(incoming_type, t, memo)
            for t in get_args(required_type)
        )

    # Neither is Union
    return None


def _handle_generic_types(
    incoming_type: Any,
    required_type: Any,
    memo: TypeCheckMemo,
) -> bool | None:
    """Handle compatibility for generic types (list[int], dict[str, int], etc).

    Also handles Annotated types by stripping metadata and comparing primary types.

    Args:
        incoming_type: The type being provided
        required_type: The type being required
        memo: Context for forward reference resolution

    Returns:
        True/False if generic logic applies, None otherwise
    """
    incoming_origin = get_origin(incoming_type) or incoming_type
    required_origin = get_origin(required_type) or required_type

    # Handle Annotated types
    if incoming_origin is Annotated and required_origin is Annotated:
        incoming_primary, *_ = get_args(incoming_type)
        required_primary, *_ = get_args(required_type)
        return is_type_compatible(incoming_primary, required_primary, memo)

    if incoming_origin is Annotated:
        incoming_primary, *_ = get_args(incoming_type)
        return is_type_compatible(incoming_primary, required_type, memo)

    if required_origin is Annotated:
        required_primary, *_ = get_args(required_type)
        return is_type_compatible(incoming_type, required_primary, memo)

    # Handle other generic types
    if incoming_origin and required_origin:
        # Check origin compatibility
        if isinstance(incoming_origin, type) and isinstance(required_origin, type):
            if not issubclass(incoming_origin, required_origin):
                return False
        elif incoming_origin != required_origin:
            return False

        # Check type arguments
        incoming_args = get_args(incoming_type)
        required_args = get_args(required_type)

        # If required has no args (unparameterized), accept any
        if not required_args or not incoming_args:
            return True

        # Require same arity for generic args
        if len(incoming_args) != len(required_args):
            return False

        # Compare args pairwise
        return all(
            is_type_compatible(t1, t2, memo)
            for t1, t2 in zip(incoming_args, required_args, strict=True)
        )

    return None


def _is_typevar_compatible(
    incoming_type: Any,
    required_type: Any,
    memo: TypeCheckMemo,
) -> bool | None:
    """Check if incoming type satisfies a required TypeVar.

    Args:
        incoming_type: The type being provided
        required_type: The required type (may be TypeVar)
        memo: Context for forward reference resolution

    Returns:
        True/False if required is TypeVar, None otherwise
    """
    if not isinstance(required_type, TypeVar):
        return None

    # Unconstrained TypeVar accepts anything
    if not required_type.__constraints__ and not required_type.__bound__:
        return True

    # Check constraints
    if required_type.__constraints__:
        if any(
            is_type_compatible(incoming_type, c, memo)
            for c in required_type.__constraints__
        ):
            return True

    # Check bound
    if required_type.__bound__:
        return is_type_compatible(incoming_type, required_type.__bound__, memo)

    return False


def is_type_compatible(
    incoming_type: Any,
    required_type: Any,
    memo: TypeCheckMemo | None = None,
) -> bool:
    """Check if an incoming type is compatible with a required type.

    This is the main entry point for type compatibility checking.
    Handles Union types, generics, forward references, TypeVars, and
    provides graceful degradation for missing/unresolvable annotations.

    Compatibility rules:
    - Identical types are compatible
    - Any as required accepts anything
    - NoAnnotation skips the check (returns True)
    - Unresolvable warns and skips (returns True)
    - Union[A, B] -> C requires both A and B compatible with C
    - A -> Union[B, C] requires A compatible with B or C
    - list[int] -> list[int] is compatible
    - list[int] -> list[str] is NOT compatible
    - list[int] -> list (unparameterized) IS compatible

    Args:
        incoming_type: The type being provided (e.g., output of upstream node)
        required_type: The type being required (e.g., input of downstream node)
        memo: Context for forward reference resolution. If None, uses empty dicts.

    Returns:
        True if incoming_type can satisfy required_type, False otherwise

    Example:
        >>> from hypergraph._typing import is_type_compatible
        >>> is_type_compatible(int, int)
        True
        >>> is_type_compatible(str, int)
        False
        >>> is_type_compatible(int, int | str)
        True
        >>> is_type_compatible(int | str, int)
        False
    """
    if memo is None:
        memo = TypeCheckMemo(globals={}, locals={})

    # Resolve forward references
    incoming_type = _resolve_type(incoming_type, memo)
    required_type = _resolve_type(required_type, memo)

    # If incoming is TypeVar, we can't know the concrete type without runtime info
    # For now, accept (same as pipefunc)
    if isinstance(incoming_type, TypeVar):
        return True

    # Check identical, Any, NoAnnotation, Unresolvable
    if _check_identical_or_any(incoming_type, required_type):
        return True

    # Check TypeVar compatibility
    result = _is_typevar_compatible(incoming_type, required_type, memo)
    if result is not None:
        return result

    # Check Union compatibility
    result = _handle_union_types(incoming_type, required_type, memo)
    if result is not None:
        return result

    # Check generic compatibility
    result = _handle_generic_types(incoming_type, required_type, memo)
    if result is not None:
        return result

    return False
