"""Shared input normalization for runner entrypoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hypergraph.graph.core import Graph

RUN_RESERVED_OPTION_NAMES = frozenset(
    {
        "select",
        "max_iterations",
        "event_processors",
        "show_progress",
        "on_internal_override",
        "_parent_span_id",
    }
)


ASYNC_RUN_RESERVED_OPTION_NAMES = frozenset(
    {
        *RUN_RESERVED_OPTION_NAMES,
        "max_concurrency",
    }
)


MAP_RESERVED_OPTION_NAMES = frozenset(
    {
        "map_over",
        "map_mode",
        "select",
        "error_handling",
        "event_processors",
        "show_progress",
        "on_internal_override",
        "_parent_span_id",
    }
)


ASYNC_MAP_RESERVED_OPTION_NAMES = frozenset(
    {
        *MAP_RESERVED_OPTION_NAMES,
        "max_concurrency",
    }
)


def merge_with_duplicate_check(
    values: dict[str, Any],
    input_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Merge values + kwargs, raising on duplicate keys."""
    overlap = sorted(set(values) & set(input_kwargs))
    if overlap:
        overlap_str = ", ".join(repr(k) for k in overlap)
        raise ValueError(f"Input keys provided in both values and kwargs: {overlap_str}. Use one source per key.")
    return {**values, **input_kwargs}


def normalize_inputs(
    values: dict[str, Any] | None,
    input_kwargs: dict[str, Any],
    *,
    reserved_option_names: frozenset[str] | None = None,
    graph: Graph | None = None,
) -> dict[str, Any]:
    """Normalize inputs from values dict + kwargs shorthand.

    When ``graph`` is provided, nested-dict entries whose top-level keys match a
    namespaced GraphNode in the graph are flattened to resolved port addresses
    (``{"A": {"overwrite": True}}`` becomes ``{"A.overwrite": True}``).
    Dict values whose top-level key is not a namespaced GraphNode are passed
    through unchanged.
    """
    base_values = dict(values) if values is not None else {}

    if reserved_option_names:
        conflicts = sorted(set(input_kwargs) & reserved_option_names)
        if conflicts:
            conflicts_str = ", ".join(repr(name) for name in conflicts)
            raise ValueError(f"Input keys are reserved runner options: {conflicts_str}. Pass these keys via values={{...}}.")

    merged = base_values if not input_kwargs else merge_with_duplicate_check(base_values, input_kwargs)

    if graph is None:
        return merged
    from hypergraph.graph._helpers import flatten_subgraph_addressing

    return flatten_subgraph_addressing(merged, graph)
