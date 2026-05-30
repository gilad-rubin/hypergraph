"""Stateful resource markers for Daft execution."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from hypergraph.runners.daft._options import DEFAULT_OPTIONS, Options
from hypergraph.stateful import (
    DAFT_OPTIONS_ATTR,
    STATEFUL_METADATA_ATTR,
    StatefulHandle,
)
from hypergraph.stateful import (
    is_stateful as _is_stateful,
)
from hypergraph.stateful import (
    stateful as _core_stateful,
)

STATEFUL_ATTR = "__daft_stateful__"
STATEFUL_OPTIONS_ATTR = DAFT_OPTIONS_ATTR


@runtime_checkable
class DaftStateful(Protocol):
    """Protocol for objects marked as Daft stateful resources."""

    __daft_stateful__: ClassVar[bool]


def stateful(
    cls: type | None = None,
    *,
    cpus: float | None = None,
    gpus: float | None = None,
    use_process: bool | None = None,
    max_concurrency: int | None = None,
    max_retries: int | None = None,
    on_error: Literal["raise", "log", "ignore"] | None = None,
    ray_options: dict[str, Any] | None = None,
) -> type | Callable[[type], type]:
    """Mark a class for per-worker construction in DaftRunner.

    Accepts only ``daft.cls`` placement controls. Resource lifecycle
    (``resource``/``close``/``aclose``) is a core ``@stateful`` + Sync/Async
    concern: Daft constructs resources per worker and exposes no deterministic
    teardown hook, so this decorator intentionally does not own that lifecycle.
    """
    daft_options = Options(
        cpus=cpus,
        gpus=gpus,
        use_process=use_process,
        max_concurrency=max_concurrency,
        max_retries=max_retries,
        on_error=on_error,
        ray_options=ray_options,
    )

    def decorate(class_: type) -> type:
        return _core_stateful(class_, _daft_options=daft_options)

    if cls is not None:
        return decorate(cls)
    return decorate


def is_stateful(value: Any) -> bool:
    """Return whether ``value`` is marked as a Daft stateful resource."""
    return _is_stateful(value) or getattr(type(value), STATEFUL_ATTR, False) is True


def is_resource_stateful(value: Any) -> bool:
    """Return whether a stateful value declares ``resource=True`` lifecycle."""
    if isinstance(value, StatefulHandle):
        return value.policy.resource
    metadata = getattr(type(value), STATEFUL_METADATA_ATTR, None)
    return bool(metadata is not None and metadata.policy.resource)


def has_stateful_values(bound_values: dict[str, Any]) -> bool:
    """Check if any bound values are marked as Daft stateful resources."""
    return any(is_stateful(value) for value in bound_values.values())


def get_stateful_options(value: Any) -> Options:
    """Return Daft class options attached to a stateful resource."""
    if isinstance(value, StatefulHandle):
        return value.metadata.daft_options or DEFAULT_OPTIONS
    return getattr(type(value), STATEFUL_OPTIONS_ATTR, DEFAULT_OPTIONS)
