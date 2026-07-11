"""Utility functions for hypergraph."""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import math
from collections.abc import Callable
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from types import CodeType
from typing import Any


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

    For bound methods, an instance fingerprint is mixed into the hash so the
    same method on two differently-configured instances hashes differently
    (e.g. two ``Summarizer`` instances with different ``model`` attributes).
    The fingerprint prefers the instance's ``cache_key()`` method when one is
    defined (mirroring hypercache's convention), falling back to a
    canonical, type-preserving serialization of ``vars(instance)``. Unsupported
    opaque state and cycles fail with guidance to define ``cache_key()``. The
    fingerprint is captured when the hash is computed — node construction — so
    mutating instance state after construction is not tracked.

    Args:
        func: Function to hash

    Returns:
        64-character hex string (SHA256 hash)

    Examples:
        >>> def foo(): pass
        >>> len(hash_definition(foo))
        64
    """
    if isinstance(func, functools.partial):
        if type(func) is not functools.partial:
            raise TypeError(
                f"Cannot deterministically fingerprint functools.partial subclass {_type_name(type(func))}; use an exact functools.partial"
            )
        return _hash_partial(func)

    if callable(func) and not inspect.isroutine(func) and not isinstance(func, type):
        return _hash_callable_object(func)

    code_hash = _hash_code(func)

    # Bound methods: mix in instance state so differently-configured
    # instances of the same class do not share a hash (and a cache).
    instance = getattr(func, "__self__", None)
    if instance is None or isinstance(instance, type) or inspect.ismodule(instance):
        # Plain function, builtin function, or classmethod (class/module-level
        # state is not deterministic across processes — keep code-only hashing).
        return code_hash

    fingerprint = _instance_fingerprint(instance)
    if fingerprint is None:
        return code_hash
    return hashlib.sha256(f"{code_hash}:{fingerprint}".encode()).hexdigest()


def _hash_partial(func: functools.partial) -> str:
    """Hash an exact partial from its callable and bound arguments."""
    payload = {
        "kind": "partial",
        "callable": hash_definition(func.func),
        "args": func.args,
        "keywords": func.keywords or {},
    }
    canonical = _canonical_json(_canonicalize(payload, active={id(func)}))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _hash_callable_object(func: Callable) -> str:
    """Hash a callable instance from its call definition and typed state."""
    call_definition = type(func).__call__

    fingerprint = _instance_fingerprint(func)
    if fingerprint is None:
        raise TypeError(
            f"Cannot deterministically fingerprint callable object "
            f"{_type_name(type(func))}: instance state is inaccessible; "
            "define cache_key() returning supported deterministic state"
        )
    call_hash = hash_definition(call_definition)
    return hashlib.sha256(f"{call_hash}:{fingerprint}".encode()).hexdigest()


def _instance_fingerprint(instance: Any) -> str | None:
    """Deterministic fingerprint of a bound method's instance state.

    Prefers the instance's ``cache_key()`` method when defined. Falls back
    to canonical typed JSON for ``vars(instance)``. Returns None when no state
    is accessible (e.g. ``__slots__`` without ``__dict__``).
    """
    cache_key = getattr(instance, "cache_key", None)
    if callable(cache_key):
        source = "cache_key"
        state = cache_key()
    else:
        try:
            state = vars(instance)
        except TypeError:
            return None
        source = "vars"

    payload = {
        "kind": "bound-instance",
        "source": source,
        "type": _type_name(type(instance)),
        "value": _canonicalize(state, active={id(instance)}),
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _canonicalize(value: Any, *, active: set[int]) -> Any:
    """Convert supported state to type-preserving canonical JSON values."""
    value_type = type(value)
    if value_type is type(None):
        return {"type": "none"}
    if value_type is bool:
        return {"type": "bool", "value": value}
    if value_type is int:
        return {"type": "int", "value": value}
    if value_type is float:
        if not math.isfinite(value):
            raise TypeError("Cannot deterministically fingerprint a non-finite float; define cache_key() returning supported deterministic state")
        return {"type": "float", "value": value}
    if value_type is str:
        return {"type": "str", "value": value}
    if value_type is bytes:
        return {"type": "bytes", "value": value.hex()}

    object_id = id(value)
    if object_id in active:
        raise TypeError(
            f"Cannot deterministically fingerprint a cycle through "
            f"{_type_name(value_type)}; define cache_key() returning acyclic "
            "supported deterministic state"
        )

    active.add(object_id)
    try:
        if value_type is CodeType:
            return {
                "type": "code",
                "name": value.co_name,
                "bytecode": value.co_code.hex(),
                "constants": _canonicalize(value.co_consts, active=active),
                "names": _canonicalize(value.co_names, active=active),
                "varnames": _canonicalize(value.co_varnames, active=active),
                "freevars": _canonicalize(value.co_freevars, active=active),
                "cellvars": _canonicalize(value.co_cellvars, active=active),
                "argcount": value.co_argcount,
                "posonlyargcount": value.co_posonlyargcount,
                "kwonlyargcount": value.co_kwonlyargcount,
                "flags": value.co_flags,
                "exceptiontable": getattr(value, "co_exceptiontable", b"").hex(),
            }

        cache_key = getattr(value, "cache_key", None)
        if callable(cache_key):
            return {
                "type": _type_name(value_type),
                "via": "cache_key",
                "value": _canonicalize(cache_key(), active=active),
            }

        if isinstance(value, Enum):
            return {
                "type": _type_name(value_type),
                "via": "enum",
                "name": value.name,
                "value": _canonicalize(value.value, active=active),
            }

        if is_dataclass(value) and not isinstance(value, type):
            return {
                "type": _type_name(value_type),
                "via": "dataclass",
                "fields": [[field.name, _canonicalize(getattr(value, field.name), active=active)] for field in fields(value)],
            }

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return {
                "type": _type_name(value_type),
                "via": "model_dump",
                "value": _canonicalize(model_dump(), active=active),
            }

        if value_type is dict:
            entries = [
                (
                    _canonicalize(key, active=active),
                    _canonicalize(item, active=active),
                )
                for key, item in value.items()
            ]
            entries.sort(key=lambda pair: _canonical_json(pair[0]))
            return {"type": "dict", "items": entries}

        if value_type is list:
            return {
                "type": "list",
                "items": [_canonicalize(item, active=active) for item in value],
            }

        if value_type is tuple:
            return {
                "type": "tuple",
                "items": [_canonicalize(item, active=active) for item in value],
            }

        if value_type is set or value_type is frozenset:
            items = [_canonicalize(item, active=active) for item in value]
            items.sort(key=_canonical_json)
            return {"type": value_type.__name__, "items": items}
    finally:
        active.remove(object_id)

    raise TypeError(f"Cannot deterministically fingerprint {_type_name(value_type)}; define cache_key() returning supported deterministic state")


def _canonical_json(value: Any) -> str:
    """Serialize one normalized value as canonical JSON."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _type_name(value_type: type[Any]) -> str:
    """Return a stable discriminator for a supported user-defined type."""
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _hash_code(func: Callable) -> str:
    """Hash a callable's code: source, bytecode, or qualified-name fallback."""
    # Prefer source code — most precise, captures comments and formatting
    try:
        source = inspect.getsource(func)
        return hashlib.sha256(source.encode()).hexdigest()
    except (OSError, TypeError):
        pass

    # Bytecode fallback — for exec/eval/Jupyter-defined functions
    code = getattr(func, "__code__", None)
    if code is not None:
        closure_state = []
        closure = getattr(func, "__closure__", None)
        if closure:
            for cell in closure:
                try:
                    closure_state.append(("value", cell.cell_contents))
                except ValueError:
                    closure_state.append(("empty",))

        payload = {
            "code": code,
            "defaults": getattr(func, "__defaults__", None),
            "keyword_defaults": getattr(func, "__kwdefaults__", None),
            "closure": tuple(closure_state),
        }
        canonical = _canonical_json(_canonicalize(payload, active=set()))
        return hashlib.sha256(canonical.encode()).hexdigest()

    # Name-based fallback — for builtins/C extensions
    module = getattr(func, "__module__", "") or ""
    qualname = getattr(func, "__qualname__", None) or getattr(func, "__name__", repr(func))
    identity = f"{module}:{qualname}"
    return hashlib.sha256(identity.encode()).hexdigest()
