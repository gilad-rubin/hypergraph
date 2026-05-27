"""Shared input normalization for runner entrypoints."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from functools import cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hypergraph.graph.core import Graph
    from hypergraph.runners._shared.validation import _InputValidationContext

_NON_OPTION_PARAMETER_NAMES = frozenset({"dataframe", "graph", "self", "values"})


def runner_option_names(method: Any) -> frozenset[str]:
    """Return explicit runner control kwargs from a runner method signature."""
    return _runner_option_names(getattr(method, "__func__", method))


@cache
def _runner_option_names(method: Any) -> frozenset[str]:
    """Cached implementation for runner_option_names()."""
    return frozenset(
        name
        for name, param in inspect.signature(method).parameters.items()
        if name not in _NON_OPTION_PARAMETER_NAMES and param.kind is inspect.Parameter.KEYWORD_ONLY
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
    other_option_names: frozenset[str] | None = None,
    other_call_name: str | None = None,
    other_option_call_names: Mapping[str, str] | None = None,
    call_name: str | None = None,
    graph: Graph | None = None,
    validation_ctx: _InputValidationContext | None = None,
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
            raise ValueError(
                _reserved_input_error(
                    conflicts,
                    call_name,
                    other_option_names,
                    other_call_name,
                    other_option_call_names,
                )
            )

    if graph is not None and input_kwargs:
        runtime_input_names = _valid_runtime_input_names(graph, validation_ctx)
        unexpected = _unexpected_input_kwargs(input_kwargs, runtime_input_names)
        if unexpected:
            raise ValueError(_unexpected_input_kwarg_error(unexpected, runtime_input_names, call_name))

    merged = base_values if not input_kwargs else merge_with_duplicate_check(base_values, input_kwargs)

    if graph is None:
        return merged
    from hypergraph.graph._helpers import flatten_subgraph_addressing

    return flatten_subgraph_addressing(merged, graph)


def _reserved_input_error(
    conflicts: list[str],
    call_name: str | None,
    other_option_names: frozenset[str] | None,
    other_call_name: str | None,
    other_option_call_names: Mapping[str, str] | None,
) -> str:
    """Create a specific error for kwargs that collide with runner controls."""
    location = call_name or "this runner method"
    if len(conflicts) == 1:
        name = conflicts[0]
        literal = f"{name!r}"
        resolved_other_call_name = None
        if other_option_call_names is not None:
            resolved_other_call_name = other_option_call_names.get(name)
        elif other_option_names and other_call_name and name in other_option_names:
            resolved_other_call_name = other_call_name
        if resolved_other_call_name is not None:
            other_call = resolved_other_call_name.removesuffix("()")
            return (
                f"{location} does not accept {name}=. {name}= is only for "
                f"{resolved_other_call_name}. Use {other_call}(..., {name}=...) "
                f"if that is what you meant. If your graph input is named {literal}, "
                f"pass it through values={{{literal}: ...}}."
            )
        return (
            f"{location} cannot use {name}= as an input keyword because {name}= "
            f"is a Hypergraph runner option. If your graph input is named {literal}, "
            f"pass it through values={{{literal}: ...}}."
        )

    conflicts_str = ", ".join(repr(name) for name in conflicts)
    return (
        f"{location} cannot use these names as input keywords because they are "
        f"Hypergraph runner options: {conflicts_str}. If they are graph inputs, "
        f"pass them through values={{...}}."
    )


def _unexpected_input_kwargs(input_kwargs: dict[str, Any], runtime_input_names: set[str]) -> list[str]:
    """Return kwargs that are not flat graph input names."""
    flat_input_names = {name for name in runtime_input_names if "." not in name}
    return sorted(name for name in input_kwargs if name not in flat_input_names)


def _valid_runtime_input_names(
    graph: Graph,
    validation_ctx: _InputValidationContext | None = None,
) -> set[str]:
    """Return graph boundary inputs, bound keys, and interrupt resume keys."""
    if validation_ctx is None:
        from hypergraph.runners._shared.validation import precompute_input_validation

        validation_ctx = precompute_input_validation(graph)
    return set(validation_ctx.input_spec.all) | set(validation_ctx.input_spec.bound) | validation_ctx.interrupt_outputs


def _unexpected_input_kwarg_error(unexpected: list[str], runtime_input_names: set[str], call_name: str | None) -> str:
    """Create a helpful error for kwargs outside the flat graph input surface."""
    location = call_name or "this runner method"
    flat_inputs = sorted(name for name in runtime_input_names if "." not in name)

    if len(unexpected) == 1:
        name = unexpected[0]
        if "." in name and name in runtime_input_names:
            head, leaf = name.split(".", 1)
            return (
                f"Dotted input address {name!r} cannot be passed as a keyword argument to {location}. "
                f"Pass it through values={{{name!r}: ...}} or values={{{head!r}: {{{leaf!r}: ...}}}}."
            )
        expected = ", ".join(repr(name) for name in flat_inputs) if flat_inputs else "(none)"
        return (
            f"{location} got unexpected input keyword {name!r}. kwargs shorthand only accepts flat graph inputs. "
            f"Expected input keywords: {expected}. Use values={{...}} for dotted or nested graph inputs."
        )

    unexpected_str = ", ".join(repr(name) for name in unexpected)
    expected = ", ".join(repr(name) for name in flat_inputs) if flat_inputs else "(none)"
    return (
        f"{location} got unexpected input keywords: {unexpected_str}. kwargs shorthand only accepts flat graph inputs. "
        f"Expected input keywords: {expected}. Use values={{...}} for dotted or nested graph inputs."
    )
