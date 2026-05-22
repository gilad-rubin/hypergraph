"""Public decorators for Hypergraph's Daft integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any, Literal

from hypergraph.nodes.function import FunctionNode
from hypergraph.runners.daft._options import Options, set_node_options
from hypergraph.runners.daft._stateful import stateful

__all__ = ["node", "stateful"]


def node(
    source: Callable | None = None,
    output_name: str | tuple[str, ...] | None = None,
    *,
    rename_inputs: dict[str, str] | None = None,
    cache: bool = False,
    hide: bool = False,
    emit: str | tuple[str, ...] | None = None,
    wait_for: str | tuple[str, ...] | None = None,
    options: Options | None = None,
    batch: bool = False,
    return_dtype: Any | None = None,
    batch_size: int | None = None,
    unnest: bool | None = None,
    cpus: float | None = None,
    use_process: bool | None = None,
    max_concurrency: int | None = None,
    max_retries: int | None = None,
    on_error: Literal["raise", "log", "ignore"] | None = None,
    gpus: float | None = None,
    ray_options: dict[str, Any] | None = None,
) -> FunctionNode | Callable[[Callable], FunctionNode]:
    """Wrap a function as a Hypergraph node with Daft-specific lowering hints."""

    daft_options = _merge_options(
        options,
        batch=batch,
        return_dtype=return_dtype,
        batch_size=batch_size,
        unnest=unnest,
        cpus=cpus,
        use_process=use_process,
        max_concurrency=max_concurrency,
        max_retries=max_retries,
        on_error=on_error,
        gpus=gpus,
        ray_options=ray_options,
    )

    def decorator(func: Callable) -> FunctionNode:
        fn_node = FunctionNode(
            source=func,
            output_name=output_name,
            rename_inputs=rename_inputs,
            cache=cache,
            hide=hide,
            emit=emit,
            wait_for=wait_for,
        )
        fn_node.__wrapped__ = func
        set_node_options(fn_node, daft_options)
        return fn_node

    if source is not None:
        return decorator(source)
    return decorator


def _merge_options(
    options: Options | None,
    *,
    batch: bool,
    return_dtype: Any | None,
    batch_size: int | None,
    unnest: bool | None,
    cpus: float | None,
    use_process: bool | None,
    max_concurrency: int | None,
    max_retries: int | None,
    on_error: Literal["raise", "log", "ignore"] | None,
    gpus: float | None,
    ray_options: dict[str, Any] | None,
) -> Options:
    if options is not None and any(
        value is not None
        for value in (
            return_dtype,
            batch_size,
            unnest,
            cpus,
            use_process,
            max_concurrency,
            max_retries,
            on_error,
            gpus,
            ray_options,
        )
    ):
        raise TypeError("Pass either options=Options(...) or direct Daft option keywords, not both")

    if options is None:
        return Options(
            return_dtype=return_dtype,
            batch=batch,
            batch_size=batch_size,
            unnest=unnest,
            cpus=cpus,
            use_process=use_process,
            max_concurrency=max_concurrency,
            max_retries=max_retries,
            on_error=on_error,
            gpus=gpus,
            ray_options=ray_options,
        )
    if batch and not options.batch:
        return replace(options, batch=True)
    return options
