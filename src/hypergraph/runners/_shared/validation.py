"""Validation helpers for runner execution."""

from __future__ import annotations

from difflib import get_close_matches
from typing import TYPE_CHECKING, Any

from hypergraph.exceptions import IncompatibleRunnerError, MissingInputError
from hypergraph.runners._shared.types import RunnerCapabilities

if TYPE_CHECKING:
    from hypergraph.graph import Graph
    from hypergraph.nodes.base import HyperNode


def validate_inputs(
    graph: "Graph",
    values: dict[str, Any],
) -> None:
    """Validate that all required inputs are provided.

    Checks:
    - All required inputs must be provided
    - All seed inputs (for cycles) must be provided
    - Warns if values are provided for edge-produced outputs (internal values)

    Args:
        graph: The graph to validate against
        values: The input values provided

    Raises:
        MissingInputError: If required or seed inputs are missing

    Warns:
        UserWarning: If values are provided for internal edge-produced parameters
    """
    inputs_spec = graph.inputs
    provided = set(values.keys())

    # Warn about providing values for internal edge-produced outputs
    # These bypass normal graph execution flow (similar to graph.bind() restriction)
    expected_inputs = set(inputs_spec.all)
    unexpected = provided - expected_inputs
    if unexpected:
        import warnings
        warnings.warn(
            f"Providing values for internal parameters: {sorted(unexpected)}. "
            f"These are produced by graph edges and will override node outputs. "
            f"Expected inputs: {sorted(expected_inputs)}",
            UserWarning,
            stacklevel=3,  # Point to caller's caller (run method)
        )

    # Required inputs must be provided
    required = set(inputs_spec.required)
    missing_required = required - provided

    # Seed inputs must also be provided (for cyclic graphs)
    seeds = set(inputs_spec.seeds)
    missing_seeds = seeds - provided

    missing = list(missing_required | missing_seeds)
    if not missing:
        return

    # Build helpful error message with suggestions
    all_inputs = set(inputs_spec.all)
    suggestions = _get_suggestions(missing, all_inputs, provided)

    message = _build_missing_input_message(
        missing=missing,
        provided=list(provided),
        suggestions=suggestions,
        required=list(required),
        seeds=list(seeds),
    )

    raise MissingInputError(
        missing=missing,
        provided=list(provided),
        message=message,
    )


def _get_suggestions(
    missing: list[str],
    all_inputs: set[str],
    provided: set[str],
) -> dict[str, list[str]]:
    """Find similar names to suggest for typos."""
    suggestions: dict[str, list[str]] = {}
    # Check if provided values have similar names to missing ones
    for m in missing:
        matches = get_close_matches(m, provided, n=1, cutoff=0.6)
        if matches:
            suggestions[m] = matches
    return suggestions


def _build_missing_input_message(
    missing: list[str],
    provided: list[str],
    suggestions: dict[str, list[str]],
    required: list[str],
    seeds: list[str],
) -> str:
    """Build a helpful error message for missing inputs."""
    missing_str = ", ".join(f"'{m}'" for m in sorted(missing))
    msg = f"Missing required inputs: {missing_str}"

    # Separate which are required vs seeds
    missing_required = [m for m in missing if m in required]
    missing_seeds = [m for m in missing if m in seeds]

    if missing_required and missing_seeds:
        msg += f"\n  - Required: {', '.join(sorted(missing_required))}"
        msg += f"\n  - Seeds (for cycles): {', '.join(sorted(missing_seeds))}"

    if provided:
        msg += f"\n\nProvided: {', '.join(f'{p!r}' for p in sorted(provided))}"

    if suggestions:
        msg += "\n\nDid you mean:"
        for m, sugg in suggestions.items():
            msg += f"\n  - '{m}' -> '{sugg[0]}'?"

    return msg


def validate_runner_compatibility(
    graph: "Graph",
    capabilities: RunnerCapabilities,
) -> None:
    """Validate that a runner can execute a graph.

    Checks:
    - If graph has async nodes, runner must support them
    - If graph has cycles, runner must support them (currently all do)

    Args:
        graph: The graph to validate
        capabilities: The runner's capabilities

    Raises:
        IncompatibleRunnerError: If runner can't handle graph features
    """
    # Check async nodes
    if graph.has_async_nodes and not capabilities.supports_async_nodes:
        # Find the async node(s) for a helpful error message
        async_nodes = [node.name for node in graph._nodes.values() if node.is_async]
        raise IncompatibleRunnerError(
            f"Graph contains async node(s) but runner doesn't support async: "
            f"{', '.join(async_nodes)}. Use AsyncRunner instead.",
            node_name=async_nodes[0] if async_nodes else None,
            capability="supports_async_nodes",
        )

    # Check cycles (all current runners support cycles, but future-proofing)
    if graph.has_cycles and not capabilities.supports_cycles:
        raise IncompatibleRunnerError(
            "Graph contains cycles but runner doesn't support cycles.",
            capability="supports_cycles",
        )


def validate_map_compatible(graph: "Graph") -> None:
    """Validate that a graph can be used with map().

    Currently a placeholder for Phase 2 interrupt validation.

    Args:
        graph: The graph to validate

    Raises:
        GraphConfigError: If graph contains features incompatible with map
    """
    # Phase 2: Check for interrupt nodes
    # For now, all graphs are map-compatible
    pass


def validate_node_types(
    graph: "Graph",
    supported_types: set[type["HyperNode"]],
) -> None:
    """Validate that all nodes in graph have registered executors.

    Args:
        graph: The graph to validate
        supported_types: Set of node types the runner supports

    Raises:
        TypeError: If a node type is not supported by the runner
    """
    for node in graph._nodes.values():
        node_type = type(node)
        if node_type not in supported_types:
            supported_names = [t.__name__ for t in supported_types]
            raise TypeError(
                f"Runner does not support node type '{node_type.__name__}'. "
                f"Supported types: {supported_names}"
            )
