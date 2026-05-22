"""Internal typed Daft options shared by the public integration and runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

NODE_OPTIONS_ATTR = "_daft_options"


@dataclass(frozen=True, slots=True)
class Options:
    """Daft lowering options for integration-specific nodes and resources."""

    return_dtype: Any | None = None
    batch: bool = False
    batch_size: int | None = None
    unnest: bool | None = None
    cpus: float | None = None
    use_process: bool | None = None
    max_concurrency: int | None = None
    max_retries: int | None = None
    on_error: Literal["raise", "log", "ignore"] | None = None
    gpus: float | None = None
    ray_options: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.on_error not in (None, "raise", "log", "ignore"):
            raise ValueError("on_error must be one of 'raise', 'log', or 'ignore'")
        if self.max_concurrency is not None and self.max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")
        if self.cpus is not None and self.cpus < 0:
            raise ValueError(f"cpus must be non-negative, got {self.cpus}")
        if self.gpus is not None:
            if self.gpus < 0:
                raise ValueError(f"gpus must be non-negative, got {self.gpus}")
            if self.gpus > 1 and not float(self.gpus).is_integer():
                raise ValueError(f"gpus greater than 1 must be an integer, got {self.gpus}")
        if self.ray_options is not None:
            unsupported = sorted({"memory", "num_cpus", "num_gpus"} & set(self.ray_options))
            if unsupported:
                names = ", ".join(repr(name) for name in unsupported)
                raise ValueError(f"ray_options cannot include {names}; use cpus/gpus instead")

    def validate_stateful_class_options(self) -> None:
        """Reject node/method-only settings when options are used for ``stateful``."""
        unsupported = []
        if self.return_dtype is not None:
            unsupported.append("return_dtype")
        if self.batch:
            unsupported.append("batch")
        if self.batch_size is not None:
            unsupported.append("batch_size")
        if self.unnest is not None:
            unsupported.append("unnest")
        if unsupported:
            names = ", ".join(unsupported)
            raise ValueError(f"stateful Options cannot include {names}; put node/method options on daft_node(...)")

    def for_func(self) -> dict[str, Any]:
        """Keyword arguments accepted by ``daft.func``."""
        return _drop_none(
            {
                "return_dtype": self.return_dtype,
                "unnest": self.unnest,
                "cpus": self.cpus,
                "gpus": self.gpus,
                "use_process": self.use_process,
                "max_concurrency": self.max_concurrency,
                "max_retries": self.max_retries,
                "on_error": self.on_error,
                "ray_options": self.ray_options,
            }
        )

    def for_batch_func(self) -> dict[str, Any]:
        """Keyword arguments accepted by ``daft.func.batch``."""
        return _drop_none(
            {
                "return_dtype": self.return_dtype,
                "unnest": self.unnest,
                "cpus": self.cpus,
                "gpus": self.gpus,
                "use_process": self.use_process,
                "max_concurrency": self.max_concurrency,
                "batch_size": self.batch_size,
                "max_retries": self.max_retries,
                "on_error": self.on_error,
                "ray_options": self.ray_options,
            }
        )

    def for_cls(self) -> dict[str, Any]:
        """Keyword arguments accepted by ``daft.cls``."""
        return _drop_none(
            {
                "cpus": self.cpus,
                "gpus": self.gpus,
                "use_process": self.use_process,
                "max_concurrency": self.max_concurrency,
                "max_retries": self.max_retries,
                "on_error": self.on_error,
                "ray_options": self.ray_options,
            }
        )

    def for_method(self) -> dict[str, Any]:
        """Keyword arguments accepted by ``daft.method``."""
        return _drop_none(
            {
                "return_dtype": self.return_dtype,
                "unnest": self.unnest,
                "max_retries": self.max_retries,
                "on_error": self.on_error,
            }
        )

    def for_batch_method(self) -> dict[str, Any]:
        """Keyword arguments accepted by ``daft.method.batch``."""
        return _drop_none(
            {
                "return_dtype": self.return_dtype,
                "unnest": self.unnest,
                "batch_size": self.batch_size,
                "max_retries": self.max_retries,
                "on_error": self.on_error,
            }
        )


DEFAULT_OPTIONS = Options()


def get_node_options(node: Any) -> Options:
    """Return Daft lowering options attached to a node."""
    options = getattr(node, NODE_OPTIONS_ATTR, None)
    if options is None:
        return DEFAULT_OPTIONS
    return options


def set_node_options(node: Any, options: Options) -> None:
    """Attach Daft lowering options to a node."""
    setattr(node, NODE_OPTIONS_ATTR, options)


def _drop_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}
