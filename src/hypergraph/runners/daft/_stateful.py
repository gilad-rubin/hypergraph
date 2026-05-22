"""Stateful resource markers for Daft execution."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from hypergraph.runners.daft._options import DEFAULT_OPTIONS, Options

STATEFUL_ATTR = "__daft_stateful__"
STATEFUL_OPTIONS_ATTR = "__daft_options__"


@runtime_checkable
class DaftStateful(Protocol):
    """Protocol for objects marked as Daft stateful resources."""

    __daft_stateful__: ClassVar[bool]


def stateful(
    cls: type | None = None,
    *,
    options: Options | None = None,
    cpus: float | None = None,
    gpus: float | None = None,
    use_process: bool | None = None,
    max_concurrency: int | None = None,
    max_retries: int | None = None,
    on_error: Literal["raise", "log", "ignore"] | None = None,
    ray_options: dict[str, Any] | None = None,
) -> type | Callable[[type], type]:
    """Mark a class for per-worker initialization in DaftRunner."""
    daft_options = _merge_stateful_options(
        options,
        cpus=cpus,
        gpus=gpus,
        use_process=use_process,
        max_concurrency=max_concurrency,
        max_retries=max_retries,
        on_error=on_error,
        ray_options=ray_options,
    )

    def decorate(class_: type) -> type:
        setattr(class_, STATEFUL_ATTR, True)
        setattr(class_, STATEFUL_OPTIONS_ATTR, daft_options)
        return class_

    if cls is not None:
        return decorate(cls)
    return decorate


def is_stateful(value: Any) -> bool:
    """Return whether ``value`` is marked as a Daft stateful resource."""
    return getattr(type(value), STATEFUL_ATTR, False) is True


def has_stateful_values(bound_values: dict[str, Any]) -> bool:
    """Check if any bound values are marked as Daft stateful resources."""
    return any(is_stateful(value) for value in bound_values.values())


def get_stateful_options(value: Any) -> Options:
    """Return Daft class options attached to a stateful resource."""
    return getattr(type(value), STATEFUL_OPTIONS_ATTR, DEFAULT_OPTIONS)


def _merge_stateful_options(
    options: Options | None,
    *,
    cpus: float | None,
    gpus: float | None,
    use_process: bool | None,
    max_concurrency: int | None,
    max_retries: int | None,
    on_error: Literal["raise", "log", "ignore"] | None,
    ray_options: dict[str, Any] | None,
) -> Options:
    direct_values = (cpus, gpus, use_process, max_concurrency, max_retries, on_error, ray_options)
    if options is not None and any(value is not None for value in direct_values):
        raise TypeError("Pass either options=Options(...) or direct Daft option keywords, not both")

    if options is not None:
        options.validate_stateful_class_options()
        return options

    return Options(
        cpus=cpus,
        gpus=gpus,
        use_process=use_process,
        max_concurrency=max_concurrency,
        max_retries=max_retries,
        on_error=on_error,
        ray_options=ray_options,
    )
