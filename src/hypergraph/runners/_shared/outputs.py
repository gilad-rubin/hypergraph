"""Runner output wrapping, selection, and collection policy."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hypergraph.nodes.base import HyperNode
from hypergraph.runners._shared.types import ErrorHandling, GraphState, RunResult, RunStatus

if TYPE_CHECKING:
    from hypergraph.graph import Graph
    from hypergraph.nodes.graph_node import GraphNode


def wrap_outputs(node: HyperNode, result: Any) -> dict[str, Any]:
    """Wrap execution result in a dict mapping output names to values.

    Uses node.data_outputs for unpacking the function return value, then
    auto-produces _EMIT_SENTINEL for each emit output.
    """
    from hypergraph.nodes.base import _EMIT_SENTINEL

    data_outputs = node.data_outputs
    emit_outputs = node.outputs[len(data_outputs) :]  # emit portion

    # Wrap data outputs
    if not data_outputs:
        wrapped = {}
    elif len(data_outputs) == 1:
        wrapped = {data_outputs[0]: result}
    else:
        if len(data_outputs) != len(result):
            raise ValueError(f"Node '{node.name}' has {len(data_outputs)} data outputs but returned {len(result)} values")
        wrapped = dict(zip(data_outputs, result, strict=True))

    # Auto-produce sentinel for each emit output
    for name in emit_outputs:
        wrapped[name] = _EMIT_SENTINEL

    return wrapped


SELECT_UNSET: Any = object()
"""Sentinel distinguishing 'user didn't pass select' from explicit '**'."""


def filter_outputs(
    state: GraphState,
    graph: Graph,
    select: str | list[str] | Any = SELECT_UNSET,
    on_missing: str = "ignore",
) -> dict[str, Any]:
    """Filter state values to only include requested outputs.

    Excludes emit sentinel values from the output — they are internal
    ordering signals and should never be visible to the user.

    Args:
        state: Final execution state
        graph: The executed graph
        select: Which outputs to return. ``"**"`` = all outputs (default).
            A list of exact names returns only those. Unset = check
            ``graph.selected`` first, then fall back to ``"**"``.
        on_missing: How to handle missing selected outputs.
            ``"ignore"`` (default) = silently omit.
            ``"warn"`` = warn and return available.
            ``"error"`` = raise ValueError.

    Returns:
        Dict of output values
    """
    if on_missing not in _VALID_ON_MISSING:
        raise ValueError(f"Invalid on_missing={on_missing!r}. Expected one of: {', '.join(_VALID_ON_MISSING)}")

    from hypergraph.nodes.base import _EMIT_SENTINEL

    effective = _resolve_select(select, graph)

    if effective == "**":
        return _collect_all_outputs(state, graph, _EMIT_SENTINEL)

    names = [effective] if isinstance(effective, str) else effective
    return _collect_selected_outputs(state, names, _EMIT_SENTINEL, on_missing)


def _resolve_select(select: Any, graph: Graph) -> str | list[str]:
    """Resolve effective select: unset → graph.selected → '**'."""
    if select is SELECT_UNSET:
        return list(graph.selected) if graph.selected is not None else "**"
    return select


def _collect_all_outputs(
    state: GraphState,
    graph: Graph,
    sentinel: Any,
) -> dict[str, Any]:
    """Return all graph outputs present in state, excluding emit sentinels."""
    return {k: state.values[k] for k in graph.outputs if k in state.values and state.values[k] is not sentinel}


def _collect_selected_outputs(
    state: GraphState,
    names: list[str],
    sentinel: Any,
    on_missing: str,
) -> dict[str, Any]:
    """Return selected outputs, handling missing per on_missing policy."""
    result = {}
    missing = []
    for k in names:
        if k in state.values and state.values[k] is not sentinel:
            result[k] = state.values[k]
        elif k not in state.values:
            missing.append(k)

    if missing:
        _handle_missing_outputs(missing, state, sentinel, on_missing)

    return result


_VALID_ON_MISSING = ("ignore", "warn", "error")


def validate_on_missing(on_missing: str) -> None:
    """Validate on_missing parameter eagerly (before execution)."""
    if on_missing not in _VALID_ON_MISSING:
        raise ValueError(f"Invalid on_missing={on_missing!r}. Expected one of: {', '.join(_VALID_ON_MISSING)}")


_VALID_ERROR_HANDLING = ("raise", "continue")


def validate_error_handling(error_handling: str) -> None:
    """Validate error_handling parameter eagerly (before execution)."""
    if error_handling not in _VALID_ERROR_HANDLING:
        valid = ", ".join(repr(v) for v in _VALID_ERROR_HANDLING)
        raise ValueError(
            f"Invalid error_handling={error_handling!r}.\n\n"
            f"Valid options: {valid}\n\n"
            f"How to fix: Pass error_handling='raise' or error_handling='continue'."
        )


def _handle_missing_outputs(
    missing: list[str],
    state: GraphState,
    sentinel: Any,
    on_missing: str,
) -> None:
    """Handle missing outputs per policy: ignore, warn, or error."""
    if on_missing not in _VALID_ON_MISSING:
        raise ValueError(f"Invalid on_missing={on_missing!r}. Expected one of: {', '.join(_VALID_ON_MISSING)}")
    if on_missing == "ignore":
        return

    available = [k for k in state.values if state.values[k] is not sentinel]
    msg = f"Requested outputs not found: {missing}. Available outputs: {available}"

    if on_missing == "warn":
        import warnings

        # stacklevel=6: _handle → _collect_selected → filter_outputs → run → user
        warnings.warn(msg, UserWarning, stacklevel=6)
    elif on_missing == "error":
        raise ValueError(msg)


def collect_as_lists(
    results: Sequence[RunResult],
    node: GraphNode,
    error_handling: ErrorHandling = "raise",
) -> dict[str, list]:
    """Collect multiple RunResults into lists per output.

    Handles output name translation: inner graph produces original names,
    but we need to return renamed names to match the GraphNode's interface.

    Args:
        results: List of RunResult from runner.map()
        node: The GraphNode (used for output name translation)
        error_handling: How to handle failed results. "raise" raises on first
            failure, "continue" uses None placeholders to preserve list length.

    Returns:
        Dict mapping renamed output names to lists of values
    """
    collected: dict[str, list] = {name: [] for name in node.outputs}
    for result in results:
        if result.status == RunStatus.FAILED:
            if error_handling == "raise":
                raise result.error  # type: ignore[misc]
            # Continue mode: use None placeholders to preserve list length
            for name in node.outputs:
                collected[name].append(None)
            continue
        # Translate original output names to renamed names
        renamed_values = node.map_outputs_from_original(result.values)
        for name in node.outputs:
            if name in renamed_values:
                collected[name].append(renamed_values[name])
    return collected
