"""Shared helper functions for runners."""

from __future__ import annotations

import copy
import warnings
from typing import TYPE_CHECKING, Any, Iterator

from hypergraph.nodes.base import HyperNode
from hypergraph.runners._shared.types import GraphState, NodeExecution

if TYPE_CHECKING:
    from hypergraph.graph import Graph


def _safe_deepcopy(value: Any) -> Any:
    """Deep-copy a value, falling back gracefully for non-copyable objects.

    Some objects (locks, file handles, C extensions) cannot be deep-copied.
    For these, we return the original value and emit a warning.
    """
    try:
        return copy.deepcopy(value)
    except (TypeError, copy.Error) as e:
        warnings.warn(
            f"Cannot deep-copy default value of type {type(value).__name__}: {e}. "
            f"Using original value. Mutating this default may affect future runs.",
            UserWarning,
            stacklevel=4,  # Point to the function using the default
        )
        return value


def get_ready_nodes(graph: "Graph", state: GraphState) -> list[HyperNode]:
    """Find nodes whose inputs are all satisfied and not stale.

    A node is ready when:
    1. All its inputs have values in state (or have defaults/bounds)
    2. The node hasn't been executed yet, OR
    3. The node was executed but its inputs have changed (stale)
    4. If controlled by a gate, the gate has routed to this node

    Args:
        graph: The graph being executed
        state: Current execution state

    Returns:
        List of nodes ready to execute
    """
    # First, identify which nodes are activated by gates
    activated_nodes = _get_activated_nodes(graph, state)

    ready = []
    for node in graph._nodes.values():
        if _is_node_ready(node, graph, state, activated_nodes):
            ready.append(node)
    return ready


def _get_activated_nodes(graph: "Graph", state: GraphState) -> set[str]:
    """Get all nodes that have been activated by gate routing decisions.

    A node is activated if:
    - It has no controlling gates, OR
    - At least one controlling gate has routed to it

    Returns:
        Set of activated node names
    """
    # Clear stale gate decisions: if a gate will re-execute (inputs changed),
    # its previous routing decision is outdated and must not activate targets
    _clear_stale_gate_decisions(graph, state)

    activated = set()

    # Use cached map of node -> controlling gates
    for node_name in graph._nodes:
        gates = graph.controlled_by.get(node_name, [])
        if not gates:
            # No controlling gates - always activated
            activated.add(node_name)
        else:
            # Check if any controlling gate has routed to this node
            for gate_name in gates:
                decision = state.routing_decisions.get(gate_name)
                if decision is None:
                    continue  # Gate hasn't made a decision yet

                # Check if this node was activated by the decision
                if _is_node_activated_by_decision(node_name, decision):
                    activated.add(node_name)
                    break

    return activated


def _clear_stale_gate_decisions(graph: "Graph", state: GraphState) -> None:
    """Clear routing decisions for gates that will re-execute.

    If a gate's inputs have changed since its last execution, its previous
    routing decision is stale. Keeping it would let targets activate before
    the gate re-evaluates — causing off-by-one iterations in cycles.
    """
    from hypergraph.nodes.gate import GateNode

    for node in graph._nodes.values():
        if isinstance(node, GateNode) and node.name in state.routing_decisions:
            if _needs_execution(node, graph, state):
                del state.routing_decisions[node.name]


def _is_node_activated_by_decision(node_name: str, decision: Any) -> bool:
    """Check if a routing decision activates a specific node."""
    from hypergraph.nodes.gate import END

    if decision is END:
        return False
    if decision is None:
        return False
    if isinstance(decision, list):
        return node_name in decision
    return decision == node_name


def _is_node_ready(
    node: HyperNode,
    graph: "Graph",
    state: GraphState,
    activated_nodes: set[str],
) -> bool:
    """Check if a single node is ready to execute."""
    # Check if node is activated (not blocked by gate decisions)
    if node.name not in activated_nodes:
        return False

    # Check if all inputs are available
    if not _has_all_inputs(node, graph, state):
        return False

    # Check if node needs execution (not executed or stale)
    return _needs_execution(node, graph, state)


def _is_controlled_by_gate(node: HyperNode, graph: "Graph") -> bool:
    """Check if a node is controlled by any gate."""
    from hypergraph.nodes.gate import GateNode, END

    for gate_node in graph._nodes.values():
        if isinstance(gate_node, GateNode):
            for target in gate_node.targets:
                if target is not END and target == node.name:
                    return True
    return False


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


def _needs_execution(
    node: HyperNode, graph: "Graph", state: GraphState
) -> bool:
    """Check if node needs (re-)execution."""
    if node.name not in state.node_executions:
        return True  # Never executed

    # Check if any input has changed since last execution
    last_exec = state.node_executions[node.name]
    return _is_stale(node, graph, state, last_exec)


def _is_stale(
    node: HyperNode,
    graph: "Graph",
    state: GraphState,
    last_exec: NodeExecution,
) -> bool:
    """Check if node inputs have changed since last execution.

    Implements the Sole Producer Rule: when a node is the only producer of a
    value that it also consumes, changes to that value are skipped in the
    staleness check. Without this, patterns like ``add_response(messages) ->
    messages`` would re-trigger infinitely because the node's own output makes
    it appear stale.

    EXCEPTION: For gate-controlled nodes, the Sole Producer Rule does NOT apply.
    Gates explicitly drive cycle execution, so self-produced inputs should
    trigger re-execution when the gate routes back to the node.
    """
    sole_producers = graph.sole_producers
    is_gated = _is_controlled_by_gate(node, graph)

    for param in node.inputs:
        # Apply Sole Producer Rule only to non-gated nodes
        if not is_gated and sole_producers.get(param) == node.name:
            continue  # Sole Producer Rule: skip self-produced inputs
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

    # 4. Function default (deep-copy to prevent cross-run mutation)
    if node.has_default_for(param):
        default = node.get_default_for(param)
        return _safe_deepcopy(default)

    # This shouldn't happen if validation passed
    raise KeyError(f"No value for input '{param}'")


def map_inputs_to_func_params(
    node: HyperNode, inputs: dict[str, Any]
) -> dict[str, Any]:
    """Map renamed input names back to original function parameter names.

    Delegates to node.map_inputs_to_params() for polymorphic behavior.
    Each node type handles its own rename mapping logic.

    Args:
        node: The node with potential renames
        inputs: Dict with renamed input names as keys

    Returns:
        Dict with original function parameter names as keys
    """
    return node.map_inputs_to_params(inputs)


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

    Warns:
        UserWarning: If select contains names not found in state values
    """
    # Runtime select= overrides graph-level default
    effective_select = select if select is not None else graph.selected

    if effective_select is not None:
        result = {}
        missing = []
        for k in effective_select:
            if k in state.values:
                result[k] = state.values[k]
            else:
                missing.append(k)

        if missing:
            import warnings
            available = list(state.values.keys())
            warnings.warn(
                f"Requested outputs not found: {missing}. "
                f"Available outputs: {available}",
                UserWarning,
                stacklevel=4,  # Point to caller's caller (run method)
            )

        return result

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
