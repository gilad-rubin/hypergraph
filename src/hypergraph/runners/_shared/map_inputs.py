"""Map-input expansion and broadcast-cloning policy."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

from hypergraph.graph.validation import GraphConfigError


def _clone_value(value: Any, param_name: str) -> Any:
    """Deep-copy a value for clone, with clone-specific error."""
    try:
        return copy.deepcopy(value)
    except (TypeError, copy.Error) as e:
        raise GraphConfigError(
            f"Parameter '{param_name}' cannot be deep-copied for clone.\n\n"
            f"Options:\n"
            f"  1. Use clone=[...] to clone only specific params\n"
            f"  2. Use .bind({param_name}=...) on the inner graph to share it\n"
            f"     (bind values bypass clone entirely)\n\n"
            f"Technical details: {e}"
        ) from e


def _maybe_clone_broadcast(
    broadcast_values: dict[str, Any],
    clone: bool | list[str],
) -> dict[str, Any]:
    """Clone broadcast values based on clone config."""
    if clone is False:
        return broadcast_values
    if clone is True:
        return {k: _clone_value(v, k) for k, v in broadcast_values.items()}
    # clone is a list of param names
    return {k: _clone_value(v, k) if k in clone else v for k, v in broadcast_values.items()}


def generate_map_inputs(
    values: dict[str, Any],
    map_over: list[str],
    map_mode: str,
    clone: bool | list[str] = False,
) -> Iterator[dict[str, Any]]:
    """Generate input dicts for each map iteration.

    Args:
        values: Input values dict
        map_over: Parameter names to iterate over
        map_mode: "zip" for parallel iteration, "product" for cartesian product
        clone: Deep-copy broadcast values per iteration.
            False = share by reference (default).
            True = deep-copy all broadcast values.
            list[str] = deep-copy only named params.

    Yields:
        Input dict for each iteration

    Raises:
        ValueError: If zip mode with unequal lengths
    """
    mapped_values = {k: values[k] for k in map_over}
    broadcast_values = {k: v for k, v in values.items() if k not in map_over}

    if map_mode == "zip":
        yield from _generate_zip_inputs(mapped_values, broadcast_values, clone)
    elif map_mode == "product":
        yield from _generate_product_inputs(mapped_values, broadcast_values, clone)
    else:
        raise ValueError(f"Unknown map_mode: {map_mode}")


def _generate_zip_inputs(
    mapped_values: dict[str, list],
    broadcast_values: dict[str, Any],
    clone: bool | list[str] = False,
) -> Iterator[dict[str, Any]]:
    """Generate inputs for zip mode (parallel iteration)."""
    if not mapped_values:
        yield dict(broadcast_values)
        return

    lengths = [len(v) for v in mapped_values.values()]
    if len(set(lengths)) > 1:
        raise ValueError(
            f"map_over parameters must have equal lengths in zip mode. Got lengths: {dict(zip(mapped_values.keys(), lengths, strict=False))}"
        )

    if not lengths:
        return

    for i in range(lengths[0]):
        yield {
            **_maybe_clone_broadcast(broadcast_values, clone),
            **{k: v[i] for k, v in mapped_values.items()},
        }


def _generate_product_inputs(
    mapped_values: dict[str, list],
    broadcast_values: dict[str, Any],
    clone: bool | list[str] = False,
) -> Iterator[dict[str, Any]]:
    """Generate inputs for product mode (cartesian product)."""
    from itertools import product as iter_product

    if not mapped_values:
        yield dict(broadcast_values)
        return

    keys = list(mapped_values.keys())
    value_lists = [mapped_values[k] for k in keys]

    for combo in iter_product(*value_lists):
        yield {
            **_maybe_clone_broadcast(broadcast_values, clone),
            **dict(zip(keys, combo, strict=False)),
        }
