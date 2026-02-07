"""Shared input normalization for runner entrypoints."""

from __future__ import annotations

from typing import Any


RUN_RESERVED_OPTION_NAMES = frozenset({
    "select",
    "max_iterations",
    "event_processors",
    "_parent_span_id",
})


ASYNC_RUN_RESERVED_OPTION_NAMES = frozenset({
    *RUN_RESERVED_OPTION_NAMES,
    "max_concurrency",
})


MAP_RESERVED_OPTION_NAMES = frozenset({
    "map_over",
    "map_mode",
    "select",
    "error_handling",
    "event_processors",
    "_parent_span_id",
})


ASYNC_MAP_RESERVED_OPTION_NAMES = frozenset({
    *MAP_RESERVED_OPTION_NAMES,
    "max_concurrency",
})


def merge_with_duplicate_check(
    values: dict[str, Any],
    input_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Merge values + kwargs, raising on duplicate keys."""
    overlap = sorted(set(values) & set(input_kwargs))
    if overlap:
        overlap_str = ", ".join(repr(k) for k in overlap)
        raise ValueError(
            f"Input keys provided in both values and kwargs: {overlap_str}. "
            "Use one source per key."
        )
    return {**values, **input_kwargs}


def normalize_inputs(
    values: dict[str, Any] | None,
    input_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Normalize inputs from values dict + kwargs shorthand."""
    base_values = dict(values) if values is not None else {}
    if not input_kwargs:
        return base_values
    return merge_with_duplicate_check(base_values, input_kwargs)
