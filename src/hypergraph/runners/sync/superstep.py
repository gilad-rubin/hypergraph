"""Superstep execution for sync runner."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.nodes.base import HyperNode
from hypergraph.runners._shared.helpers import collect_inputs_for_node
from hypergraph.runners._shared.types import GraphState, NodeExecution

if TYPE_CHECKING:
    from hypergraph.graph import Graph
    from hypergraph.runners._shared.protocols import NodeExecutor


def run_superstep_sync(
    graph: "Graph",
    state: GraphState,
    ready_nodes: list[HyperNode],
    provided_values: dict[str, Any],
    execute_node: "NodeExecutor",
) -> GraphState:
    """Execute one superstep: run all ready nodes and update state.

    In sync mode, nodes are executed sequentially.

    Args:
        graph: The graph being executed
        state: Current state (will be copied, not mutated)
        ready_nodes: Nodes to execute in this superstep
        provided_values: Values provided to runner.run()
        execute_node: Function to execute a single node

    Returns:
        New state with updated values and versions
    """
    new_state = state.copy()

    for node in ready_nodes:
        inputs = collect_inputs_for_node(node, graph, new_state, provided_values)

        # Record input versions before execution
        input_versions = {param: new_state.get_version(param) for param in node.inputs}

        # Execute node using the provided executor
        outputs = execute_node(node, new_state, inputs)

        # Update state with outputs
        for name, value in outputs.items():
            new_state.update_value(name, value)

        # Record execution
        new_state.node_executions[node.name] = NodeExecution(
            node_name=node.name,
            input_versions=input_versions,
            outputs=outputs,
        )

    return new_state
