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
    *,
    entry_point: str | None = None,
) -> None:
    """Validate that all required inputs are provided.

    Follows a 5-step pipeline:
    1. Merge bound + provided (provided overwrites bound)
    2. Warn about internal parameters
    3. Bypass detection (considering merged values)
    4. Cycle entry point matching
    5. Completeness check

    Args:
        graph: The graph to validate against
        values: The input values provided
        entry_point: Optional explicit entry point node name for cycle

    Raises:
        MissingInputError: If required inputs or cycle entry points are missing
        ValueError: If entry_point is invalid or cycle entry is ambiguous
    """
    inputs_spec = graph.inputs
    # Step 1: Merge bound + provided
    merged = {**inputs_spec.bound, **values}
    provided = set(merged.keys())

    # Step 2: Warn about internal edge-produced outputs
    expected_inputs = set(inputs_spec.all)
    interrupt_outputs = _get_interrupt_outputs(graph)
    unexpected = provided - expected_inputs - interrupt_outputs
    if unexpected:
        import warnings
        warnings.warn(
            f"Providing values for internal parameters: {sorted(unexpected)}. "
            f"These are produced by graph edges and will override node outputs. "
            f"Expected inputs: {sorted(expected_inputs)}",
            UserWarning,
            stacklevel=3,
        )

    # Step 3: Bypass detection (considering merged values)
    bypassed_inputs = _find_bypassed_inputs(graph, provided)

    # Step 4: Cycle entry point matching
    if inputs_spec.entry_points:
        _validate_cycle_entry(graph, provided, bypassed_inputs, entry_point)

    # Step 5: Completeness check for required (acyclic) inputs
    required = set(inputs_spec.required) - bypassed_inputs
    missing_required = required - provided

    if not missing_required:
        return

    all_inputs = set(inputs_spec.all)
    suggestions = _get_suggestions(sorted(missing_required), all_inputs, provided)
    message = _build_missing_input_message(
        missing=sorted(missing_required),
        provided=list(provided),
        suggestions=suggestions,
        entry_points=inputs_spec.entry_points,
    )

    raise MissingInputError(
        missing=sorted(missing_required),
        provided=list(provided),
        message=message,
    )


def _validate_cycle_entry(
    graph: "Graph",
    provided: set[str],
    bypassed: set[str],
    entry_point: str | None,
) -> None:
    """Validate cycle entry points.

    For each cycle, exactly one entry point must be satisfied by provided values.
    If entry_point is explicit, validate that it matches.

    Raises:
        ValueError: If entry_point is invalid, ambiguous, or mismatched.
        MissingInputError: If no entry point is satisfied for a cycle.
    """
    ep = graph.inputs.entry_points

    # If explicit entry_point, validate it
    if entry_point is not None:
        if entry_point not in ep:
            valid = sorted(ep.keys())
            raise ValueError(
                f"'{entry_point}' is not a valid entry point. "
                f"Valid entry points: {valid}"
            )
        needed = set(ep[entry_point]) - bypassed
        if not needed <= provided:
            missing = sorted(needed - provided)
            raise ValueError(
                f"Entry point '{entry_point}' needs: {', '.join(sorted(ep[entry_point]))}. "
                f"Missing: {', '.join(missing)}"
            )
        return

    # Implicit: group entry points by SCC and check each cycle
    scc_groups = _group_entry_points_by_scc(graph, ep)
    for scc_entries in scc_groups.values():
        _check_cycle_entry(scc_entries, ep, provided, bypassed)


def _group_entry_points_by_scc(
    graph: "Graph",
    entry_points: dict[str, tuple[str, ...]],
) -> dict[int, list[str]]:
    """Group entry point node names by which cycle (SCC) they belong to.

    Uses the graph's data-only subgraph to compute SCCs. Entry points
    from independent cycles get separate groups — ambiguity is checked
    per cycle, not across cycles.
    """
    if not entry_points:
        return {}

    import networkx as nx
    from hypergraph.graph.input_spec import _data_only_subgraph

    data_graph = _data_only_subgraph(graph.nx_graph)
    sccs = list(nx.strongly_connected_components(data_graph))

    # Map node_name → scc_index
    node_to_scc: dict[str, int] = {}
    for idx, scc in enumerate(sccs):
        for node_name in scc:
            node_to_scc[node_name] = idx

    # Group entry points by SCC
    groups: dict[int, list[str]] = {}
    for node_name in entry_points:
        scc_idx = node_to_scc.get(node_name)
        if scc_idx is not None:
            groups.setdefault(scc_idx, []).append(node_name)

    return groups


def _check_cycle_entry(
    node_names: list[str],
    entry_points: dict[str, tuple[str, ...]],
    provided: set[str],
    bypassed: set[str],
) -> None:
    """Check that exactly one entry point in a cycle is satisfied."""
    satisfied = []
    for name in node_names:
        needed = set(entry_points[name]) - bypassed
        if needed <= provided:
            satisfied.append(name)

    if len(satisfied) == 0:
        # No entry point matched — build helpful error
        lines = ["No entry point for cycle. Provide values for one of:"]
        for name in sorted(node_names):
            params = ", ".join(entry_points[name]) if entry_points[name] else "(no params needed)"
            lines.append(f"  {name} → {params}")
        raise MissingInputError(
            missing=[],
            provided=list(provided),
            message="\n".join(lines),
        )

    if len(satisfied) > 1:
        # If all satisfied entry points need the same params, it's not ambiguous —
        # the user is seeding the cycle regardless of which node runs first.
        distinct_param_sets = {entry_points[name] for name in satisfied}
        if len(distinct_param_sets) > 1:
            lines = [
                "Ambiguous cycle entry — provided values match multiple entry points:"
            ]
            for name in satisfied:
                params = ", ".join(entry_points[name])
                lines.append(f"  {name} (needs: {params})")
            lines.append("Provide values for exactly one entry point.")
            raise ValueError("\n".join(lines))


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
    entry_points: dict[str, tuple[str, ...]] | None = None,
) -> str:
    """Build a helpful error message for missing inputs."""
    missing_str = ", ".join(f"'{m}'" for m in sorted(missing))
    msg = f"Missing required inputs: {missing_str}"

    if provided:
        msg += f"\n\nProvided: {', '.join(f'{p!r}' for p in sorted(provided))}"

    if entry_points:
        msg += "\n\nCycle entry points:"
        for name, params in sorted(entry_points.items()):
            params_str = ", ".join(params) if params else "(no params needed)"
            msg += f"\n  {name} → {params_str}"

    if suggestions:
        msg += "\n\nDid you mean:"
        for m, sugg in suggestions.items():
            msg += f"\n  - '{m}' -> '{sugg[0]}'?"

    msg += "\n\nHint: If you used graph.bind(), remember it returns a NEW graph."
    msg += "\n  ❌ graph.bind(x=10)           # Result discarded!"
    msg += "\n  ✅ graph = graph.bind(x=10)   # Correct"

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


def _get_interrupt_outputs(graph: "Graph") -> set[str]:
    """Get all output names produced by InterruptNodes in the graph."""
    from hypergraph.nodes.interrupt import InterruptNode

    return {
        output
        for n in graph._nodes.values()
        if isinstance(n, InterruptNode)
        for output in n.outputs
    }


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

    # Cycle entry point params: providing these means bootstrapping a cycle,
    # NOT bypassing the producer node. Exclude from bypass check.
    cycle_ep_params = {
        p for params in graph.inputs.entry_points.values() for p in params
    }

    # A node is bypassed only if ALL its non-cycle consumed outputs are provided
    bypassed_nodes: set[str] = set()
    for node in graph._nodes.values():
        node_consumed_outputs = (set(node.outputs) & consumed_outputs) - cycle_ep_params
        if node_consumed_outputs and node_consumed_outputs <= provided:
            # All consumed outputs are provided — node is bypassed
            bypassed_nodes.add(node.name)

    if not bypassed_nodes:
        return set()

    # Also mark nodes as bypassed if ALL their non-cycle outputs are provided
    for node in graph._nodes.values():
        non_cycle_outputs = set(node.outputs) - cycle_ep_params
        if non_cycle_outputs and non_cycle_outputs <= provided:
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
