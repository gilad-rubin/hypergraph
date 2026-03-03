"""Utility functions for hypergraph."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Callable
from datetime import datetime


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


def plural(n: int, word: str) -> str:
    """Pluralize a word based on count.

    Examples:
        >>> plural(1, "node")
        '1 node'
        >>> plural(3, "node")
        '3 nodes'
        >>> plural(0, "error")
        '0 errors'
    """
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def format_duration_ms(ms: float | None) -> str:
    """Format milliseconds into human-readable duration.

    Examples:
        >>> format_duration_ms(42)
        '42ms'
        >>> format_duration_ms(1500)
        '1.5s'
        >>> format_duration_ms(125000)
        '2m05.0s'
        >>> format_duration_ms(None)
        '—'
    """
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    minutes = int(ms // 60_000)
    seconds = (ms % 60_000) / 1000
    return f"{minutes}m{seconds:04.1f}s"


def format_datetime(dt: datetime | None) -> str:
    """Format datetime for human display.

    Examples:
        >>> from datetime import datetime, timezone
        >>> format_datetime(datetime(2026, 3, 1, 12, 30, tzinfo=timezone.utc))
        '2026-03-01 12:30'
        >>> format_datetime(None)
        '—'
    """
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M")


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

        # Serialize co_consts deterministically (replace nested code objects with names)
        consts_serialized = tuple(c if not hasattr(c, "co_name") else c.co_name for c in code.co_consts)
        h.update(repr(consts_serialized).encode())

        # Include function defaults to distinguish f(x=1) from f(x=2)
        h.update(repr(getattr(func, "__defaults__", None)).encode())
        h.update(repr(getattr(func, "__kwdefaults__", None)).encode())

        # Include closure values to distinguish functions with different captured variables
        closure = getattr(func, "__closure__", None)
        if closure:
            for cell in closure:
                try:
                    h.update(repr(cell.cell_contents).encode())
                except ValueError:
                    h.update(b"<empty_cell>")

        return h.hexdigest()

    # Name-based fallback — for builtins/C extensions/functools.partial
    module = getattr(func, "__module__", "") or ""
    qualname = getattr(func, "__qualname__", None) or getattr(func, "__name__", repr(func))
    identity = f"{module}:{qualname}"
    return hashlib.sha256(identity.encode()).hexdigest()
