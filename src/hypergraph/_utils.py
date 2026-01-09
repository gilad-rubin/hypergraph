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
    if isinstance(value, str):
        return (value,)
    return value


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
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError) as e:
        raise ValueError(f"Cannot hash function {func.__name__}: {e}")
    return hashlib.sha256(source.encode()).hexdigest()
