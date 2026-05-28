"""Lazy stateful object handles."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

STATEFUL_ATTR = "__hypergraph_stateful__"
STATEFUL_METADATA_ATTR = "__hypergraph_stateful_metadata__"
DAFT_STATEFUL_ATTR = "__daft_stateful__"
DAFT_OPTIONS_ATTR = "__daft_options__"

_INFER = object()


@dataclass(frozen=True)
class StatefulResourcePolicy:
    """Lifecycle policy for a stateful handle."""

    resource: bool
    close: str | None = None
    aclose: str | None = None

    @property
    def async_only(self) -> bool:
        return self.resource and self.close is None and self.aclose is not None


@dataclass(frozen=True)
class StatefulMetadata:
    """Metadata captured by ``@stateful``."""

    cls: type
    policy: StatefulResourcePolicy
    daft_options: Any = None


class StatefulHandle:
    """Lazy constructor call produced by a ``@stateful`` class."""

    __hypergraph_stateful__ = True
    __daft_stateful__ = True

    def __init__(
        self,
        metadata: StatefulMetadata,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        stateful_cls: type | None = None,
    ) -> None:
        self.metadata = metadata
        self.args = args
        self.kwargs = dict(kwargs)
        self.stateful_cls = stateful_cls
        self.__daft_options__ = metadata.daft_options

    @property
    def cls(self) -> type:
        return self.metadata.cls

    @property
    def policy(self) -> StatefulResourcePolicy:
        return self.metadata.policy

    def materialize(self) -> Any:
        """Construct the wrapped object."""
        if self.stateful_cls is not None:
            return self.stateful_cls.__hypergraph_materialize__(*self.args, **self.kwargs)
        return self.cls(*self.args, **self.kwargs)

    def __reduce__(self) -> Any:
        if self.stateful_cls is None:
            return (_rebuild_legacy_stateful_handle, (self.metadata, self.args, self.kwargs))
        return (_rebuild_stateful_handle, (self.stateful_cls, self.args, self.kwargs))

    def __repr__(self) -> str:
        return f"StatefulHandle({self.cls.__name__})"


def _rebuild_stateful_handle(stateful_cls: type, args: tuple[Any, ...], kwargs: dict[str, Any]) -> StatefulHandle:
    return stateful_cls(*args, **kwargs)


def _rebuild_legacy_stateful_handle(
    metadata: StatefulMetadata,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> StatefulHandle:
    return StatefulHandle(metadata, args, kwargs)


def stateful(
    cls: type | None = None,
    *,
    resource: bool = False,
    close: str | None | object = _INFER,
    aclose: str | None | object = _INFER,
    _daft_options: Any = None,
) -> type | Any:
    """Decorate a class so construction produces a lazy handle."""

    def decorate(class_: type) -> type:
        policy = _resource_policy(class_, resource=resource, close=close, aclose=aclose)
        metadata = StatefulMetadata(class_, policy, _daft_options)

        class StatefulClass(class_):  # type: ignore[misc, valid-type]
            __wrapped__ = class_
            __hypergraph_stateful__ = True
            __hypergraph_stateful_metadata__ = metadata
            __daft_stateful__ = True
            __daft_options__ = _daft_options

            def __new__(proxy_cls, *args: Any, **kwargs: Any) -> Any:  # noqa: N804
                if proxy_cls is StatefulClass:
                    return StatefulHandle(metadata, args, kwargs, StatefulClass)
                return super().__new__(proxy_cls)

            @classmethod
            def __hypergraph_materialize__(stateful_cls, *args: Any, **kwargs: Any) -> Any:
                return _construct_stateful_instance(class_, stateful_cls, args, kwargs)

        StatefulClass.__name__ = class_.__name__
        StatefulClass.__qualname__ = class_.__qualname__
        StatefulClass.__module__ = class_.__module__
        StatefulClass.__doc__ = class_.__doc__
        return StatefulClass

    if cls is not None:
        return decorate(cls)
    return decorate


def _construct_stateful_instance(
    original_cls: type,
    stateful_cls: type,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Construct the proxy instance without producing another lazy handle."""
    new = original_cls.__new__
    instance = object.__new__(stateful_cls) if new is object.__new__ else new(stateful_cls, *args, **kwargs)

    if isinstance(instance, stateful_cls):
        original_cls.__init__(instance, *args, **kwargs)
    return instance


def is_stateful(value: Any) -> bool:
    """Return whether ``value`` is a lazy stateful handle."""
    return isinstance(value, StatefulHandle) or getattr(value, STATEFUL_ATTR, False) is True


def is_stateful_handle(value: Any) -> bool:
    """Return whether ``value`` is a lazy constructor handle."""
    return isinstance(value, StatefulHandle)


def _resource_policy(
    cls: type,
    *,
    resource: bool,
    close: str | None | object,
    aclose: str | None | object,
) -> StatefulResourcePolicy:
    if not resource:
        return StatefulResourcePolicy(resource=False)

    close_name = _resolve_cleanup_name(cls, close, default="close", sync=True)
    aclose_name = _resolve_cleanup_name(cls, aclose, default="aclose", sync=False)

    if close_name is None and aclose_name is None:
        raise TypeError(
            f"{cls.__name__} is marked resource=True but has no close()/aclose() cleanup method. "
            "Add close(), add async aclose(), or use resource=False for lazy state without lifecycle ownership."
        )

    return StatefulResourcePolicy(resource=True, close=close_name, aclose=aclose_name)


def _resolve_cleanup_name(
    cls: type,
    value: str | None | object,
    *,
    default: str,
    sync: bool,
) -> str | None:
    if value is None:
        return None

    if value is _INFER:
        name = default if callable(getattr(cls, default, None)) else None
    else:
        if not isinstance(value, str):
            raise TypeError("close/aclose must be method names, None, or omitted")
        name = value
        if not callable(getattr(cls, name, None)):
            raise TypeError(f"{cls.__name__} has no cleanup method {name!r}")

    if name is None:
        return None

    method = getattr(cls, name)
    is_async = inspect.iscoroutinefunction(method)
    if sync and is_async:
        raise TypeError(f"{cls.__name__}.{name} is async; pass it as aclose={name!r}")
    if not sync and not is_async:
        raise TypeError(f"{cls.__name__}.{name} is sync; pass it as close={name!r}")
    return name
