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
    """Compute SHA256 hash of a function's definition.

    Uses source code when available (file-defined functions), falls back to
    bytecode for dynamically created functions (exec/eval), and finally to
    qualified name for builtins/C extensions.

    Args:
        func: Function to hash

    Returns:
        64-character hex string (SHA256 hash)

    Examples:
        >>> def foo(): pass
        >>> len(hash_definition(foo))
        64
    """
    # Prefer source code — most precise, captures comments and formatting
    try:
        source = inspect.getsource(func)
        return hashlib.sha256(source.encode()).hexdigest()
    except (OSError, TypeError):
        pass

    # Bytecode fallback — for exec/eval/Jupyter-defined functions
    code = getattr(func, "__code__", None)
    if code is not None:
        h = hashlib.sha256()
        h.update(code.co_code)
        h.update(repr(code.co_consts).encode())
        return h.hexdigest()

    # Name-based fallback — for builtins/C extensions
    module = getattr(func, "__module__", "") or ""
    qualname = getattr(func, "__qualname__", func.__name__)
    identity = f"{module}:{qualname}"
    return hashlib.sha256(identity.encode()).hexdigest()
