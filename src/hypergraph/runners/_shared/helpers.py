"""Shared helper functions for runners."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterator

from hypergraph.nodes.base import HyperNode
from hypergraph.runners._shared.types import GraphState, NodeExecution

if TYPE_CHECKING:
    from hypergraph.graph import Graph


def get_ready_nodes(graph: "Graph", state: GraphState) -> list[HyperNode]:
    """Find nodes whose inputs are all satisfied and not stale.

    A node is ready when:
    1. All its inputs have values in state (or have defaults/bounds)
    2. The node hasn't been executed yet, OR
    3. The node was executed but its inputs have changed (stale)

    Args:
        graph: The graph being executed
        state: Current execution state

    Returns:
        List of nodes ready to execute
    """
    ready = []
    for node in graph._nodes.values():
        if _is_node_ready(node, graph, state):
            ready.append(node)
    return ready


def _is_node_ready(node: HyperNode, graph: "Graph", state: GraphState) -> bool:
    """Check if a single node is ready to execute."""
    # Check if all inputs are available
    if not _has_all_inputs(node, graph, state):
        return False

    # Check if node needs execution (not executed or stale)
    return _needs_execution(node, state)


def _has_all_inputs(node: HyperNode, graph: "Graph", state: GraphState) -> bool:
    """Check if all inputs for a node are available."""
    for param in node.inputs:
        if not _has_input(param, node, graph, state):
            return False
    return True


def _has_input(param: str, node: HyperNode, graph: "Graph", state: GraphState) -> bool:
    """Check if a single input parameter is available."""
    # Value in state (from edge or initial input)
    if param in state.values:
        return True

    # Bound value in graph
    if param in graph.inputs.bound:
        return True

    # Node has default for this parameter
    if node.has_default_for(param):
        return True

    return False


def _needs_execution(node: HyperNode, state: GraphState) -> bool:
    """Check if node needs (re-)execution."""
    if node.name not in state.node_executions:
        return True  # Never executed

    # Check if any input has changed since last execution
    last_exec = state.node_executions[node.name]
    return _is_stale(node, state, last_exec)


def _is_stale(node: HyperNode, state: GraphState, last_exec: NodeExecution) -> bool:
    """Check if node inputs have changed since last execution."""
    for param in node.inputs:
        current_version = state.get_version(param)
        consumed_version = last_exec.input_versions.get(param, 0)
        if current_version != consumed_version:
            return True
    return False


def collect_inputs_for_node(
    node: HyperNode,
    graph: "Graph",
    state: GraphState,
    provided_values: dict[str, Any],
) -> dict[str, Any]:
    """Gather inputs for a node following value resolution order.

    Resolution order (first wins):
    1. Edge value (from state.values, produced by upstream node)
    2. Input value (from provided_values dict)
    3. Bound value (from graph.bind())
    4. Function default

    Args:
        node: The node to collect inputs for
        graph: The graph being executed
        state: Current execution state
        provided_values: Values provided to runner.run()

    Returns:
        Dict mapping input names to their values
    """
    inputs = {}
    for param in node.inputs:
        inputs[param] = _resolve_input(param, node, graph, state, provided_values)
    return inputs


def _resolve_input(
    param: str,
    node: HyperNode,
    graph: "Graph",
    state: GraphState,
    provided_values: dict[str, Any],
) -> Any:
    """Resolve a single input value following the precedence order."""
    # 1. Edge value (from upstream node output)
    if param in state.values:
        return state.values[param]

    # 2. Input value (from run() call)
    if param in provided_values:
        return provided_values[param]

    # 3. Bound value (from graph.bind())
    if param in graph.inputs.bound:
        return graph.inputs.bound[param]

    # 4. Function default
    if node.has_default_for(param):
        return node.get_default_for(param)

    # This shouldn't happen if validation passed
    raise KeyError(f"No value for input '{param}'")


def map_inputs_to_func_params(
    node: HyperNode, inputs: dict[str, Any]
) -> dict[str, Any]:
    """Map renamed input names back to original function parameter names.

    Args:
        node: The node with potential renames
        inputs: Dict with renamed input names as keys

    Returns:
        Dict with original function parameter names as keys
    """
    from hypergraph.nodes.function import FunctionNode

    if not isinstance(node, FunctionNode):
        return inputs

    # Build reverse mapping: renamed_name -> original_name
    reverse_map: dict[str, str] = {}
    for entry in node._rename_history:
        if entry.kind == "inputs":
            reverse_map[entry.new] = entry.old

    # Map inputs to original parameter names
    result = {}
    for key, value in inputs.items():
        original_name = reverse_map.get(key, key)
        result[original_name] = value

    return result


def wrap_outputs(node: HyperNode, result: Any) -> dict[str, Any]:
    """Wrap execution result in a dict mapping output names to values."""
    outputs = node.outputs

    # No outputs (side-effect only)
    if not outputs:
        return {}

    # Single output
    if len(outputs) == 1:
        return {outputs[0]: result}

    # Multiple outputs - unpack tuple
    if len(outputs) != len(result):
        raise ValueError(
            f"Node '{node.name}' has {len(outputs)} outputs but returned "
            f"{len(result)} values"
        )
    return dict(zip(outputs, result))


def initialize_state(
    graph: "Graph",
    values: dict[str, Any],
) -> GraphState:
    """Initialize execution state with provided input values.

    Args:
        graph: The graph being executed
        values: Input values provided to runner.run()

    Returns:
        Initial GraphState with input values set
    """
    state = GraphState()

    # Set initial values for all provided inputs
    for name, value in values.items():
        state.update_value(name, value)

    return state


def filter_outputs(
    state: GraphState,
    graph: "Graph",
    select: list[str] | None,
) -> dict[str, Any]:
    """Filter state values to only include requested outputs.

    Args:
        state: Final execution state
        graph: The executed graph
        select: Optional list of output names to include (None = all outputs)

    Returns:
        Dict of output values
    """
    if select is not None:
        return {k: state.values[k] for k in select if k in state.values}

    # Default: return all graph outputs
    return {k: state.values[k] for k in graph.outputs if k in state.values}


def generate_map_inputs(
    values: dict[str, Any],
    map_over: list[str],
    map_mode: str,
) -> Iterator[dict[str, Any]]:
    """Generate input dicts for each map iteration.

    Args:
        values: Input values dict
        map_over: Parameter names to iterate over
        map_mode: "zip" for parallel iteration, "product" for cartesian product

    Yields:
        Input dict for each iteration

    Raises:
        ValueError: If zip mode with unequal lengths
    """
    mapped_values = {k: values[k] for k in map_over}
    broadcast_values = {k: v for k, v in values.items() if k not in map_over}

    if map_mode == "zip":
        yield from _generate_zip_inputs(mapped_values, broadcast_values)
    elif map_mode == "product":
        yield from _generate_product_inputs(mapped_values, broadcast_values)
    else:
        raise ValueError(f"Unknown map_mode: {map_mode}")


def _generate_zip_inputs(
    mapped_values: dict[str, list],
    broadcast_values: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Generate inputs for zip mode (parallel iteration)."""
    if not mapped_values:
        yield dict(broadcast_values)
        return

    lengths = [len(v) for v in mapped_values.values()]
    if len(set(lengths)) > 1:
        raise ValueError(
            f"map_over parameters must have equal lengths in zip mode. "
            f"Got lengths: {dict(zip(mapped_values.keys(), lengths))}"
        )

    if not lengths:
        return

    for i in range(lengths[0]):
        yield {
            **broadcast_values,
            **{k: v[i] for k, v in mapped_values.items()},
        }


def _generate_product_inputs(
    mapped_values: dict[str, list],
    broadcast_values: dict[str, Any],
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
            **broadcast_values,
            **dict(zip(keys, combo)),
        }
