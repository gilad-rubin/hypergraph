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

    # If the user provided intermediate values (internal outputs),
    # upstream nodes that produce those values don't need to run,
    # so their inputs shouldn't be required.
    required = set(inputs_spec.required)
    seeds = set(inputs_spec.seeds)

    # Remove inputs that belong to nodes bypassed by intermediate injection
    bypassed_inputs = _find_bypassed_inputs(graph, provided)
    required -= bypassed_inputs
    seeds -= bypassed_inputs

    missing_required = required - provided
    missing_seeds = seeds - provided

    missing = sorted(missing_required | missing_seeds)
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
    """Find similar names to suggest for typos.

    Checks provided values first (for typo detection), then falls back to
    valid inputs that weren't provided (for discoverability).
    """
    suggestions: dict[str, list[str]] = {}
    for m in missing:
        # Check provided values first (typo detection)
        matches = get_close_matches(m, provided, n=1, cutoff=0.6)
        # Fall back to valid inputs that weren't provided
        # Exclude the missing parameter itself to avoid suggesting "embedder -> embedder?"
        if not matches:
            matches = get_close_matches(m, all_inputs - provided - {m}, n=1, cutoff=0.6)
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

    # Check interrupts
    if graph.has_interrupts and not capabilities.supports_interrupts:
        from hypergraph.nodes.interrupt import InterruptNode
        interrupt_names = [
            node.name for node in graph._nodes.values()
            if isinstance(node, InterruptNode)
        ]
        raise IncompatibleRunnerError(
            f"Graph contains InterruptNode(s) but runner doesn't support interrupts: "
            f"{', '.join(interrupt_names)}. Use AsyncRunner instead.",
            node_name=interrupt_names[0] if interrupt_names else None,
            capability="supports_interrupts",
        )


def validate_map_compatible(graph: "Graph") -> None:
    """Validate that a graph can be used with map().

    Checks that graphs with interrupts are not used with map().

    Args:
        graph: The graph to validate

    Raises:
        IncompatibleRunnerError: If graph contains InterruptNodes
    """
    if graph.has_interrupts:
        raise IncompatibleRunnerError(
            "Graph contains InterruptNode(s) which are incompatible with .map(). "
            "Use .run() for graphs with interrupts.",
            capability="supports_interrupts",
        )


def _find_bypassed_inputs(graph: "Graph", provided: set[str]) -> set[str]:
    """Find inputs that belong to nodes fully bypassed by intermediate injection.

    A node is bypassed ONLY if ALL of its outputs that are consumed downstream
    are provided by the user. If any consumed output is missing, the node must
    still run to produce it.

    When a node is bypassed, its exclusive inputs (not needed by other nodes)
    can be removed from the required set.
    """
    # Build output -> producer nodes mapping (handle mutex branches)
    output_to_nodes: dict[str, list[str]] = {}
    for node in graph._nodes.values():
        for output in node.outputs:
            output_to_nodes.setdefault(output, []).append(node.name)

    # Find which outputs are consumed by downstream nodes
    consumed_outputs: set[str] = set()
    for node in graph._nodes.values():
        consumed_outputs.update(node.inputs)

    # A node is bypassed only if ALL its consumed outputs are provided
    bypassed_nodes: set[str] = set()
    for node in graph._nodes.values():
        node_consumed_outputs = set(node.outputs) & consumed_outputs
        if node_consumed_outputs and node_consumed_outputs <= provided:
            # All consumed outputs are provided — node is bypassed
            bypassed_nodes.add(node.name)

    if not bypassed_nodes:
        return set()

    # Also mark nodes as bypassed if ALL their outputs (consumed or not) are provided
    # This handles the case where user provides all outputs even if some aren't consumed
    for node in graph._nodes.values():
        if set(node.outputs) <= provided:
            bypassed_nodes.add(node.name)

    # Collect inputs exclusively needed by bypassed nodes
    bypassed_inputs: set[str] = set()
    for node_name in bypassed_nodes:
        node = graph._nodes[node_name]
        bypassed_inputs.update(node.inputs)

    # Remove inputs that are also consumed by non-bypassed nodes
    for node in graph._nodes.values():
        if node.name not in bypassed_nodes:
            bypassed_inputs -= set(node.inputs)

    return bypassed_inputs


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
