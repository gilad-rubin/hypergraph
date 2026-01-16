"""Core execution logic for runners."""

from __future__ import annotations

import asyncio
import inspect
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Iterator

from hypergraph.nodes.base import HyperNode
from hypergraph.runners._types import GraphState, NodeExecution

if TYPE_CHECKING:
    from hypergraph.graph import Graph

# Context variable for concurrency limiting across nested graphs
_concurrency_limiter: ContextVar[asyncio.Semaphore | None] = ContextVar(
    "_concurrency_limiter", default=None
)


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


def _has_input(
    param: str, node: HyperNode, graph: "Graph", state: GraphState
) -> bool:
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


def _is_stale(
    node: HyperNode, state: GraphState, last_exec: NodeExecution
) -> bool:
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


def execute_node_sync(node: HyperNode, inputs: dict[str, Any]) -> dict[str, Any]:
    """Execute a single node synchronously.

    Handles:
    - Regular function calls
    - Sync generators (accumulated to list)

    Args:
        node: The node to execute
        inputs: Input values for the node

    Returns:
        Dict mapping output names to their values
    """
    # Import here to avoid circular imports
    from hypergraph.nodes.function import FunctionNode

    if isinstance(node, FunctionNode):
        result = node.func(**inputs)

        # Handle generators
        if node.is_generator:
            result = list(result)

        return _wrap_outputs(node, result)

    # GraphNode - delegate to its execution (handled by runner)
    raise NotImplementedError(
        f"GraphNode execution should be handled by runner, not execute_node_sync"
    )


async def execute_node_async(
    node: HyperNode, inputs: dict[str, Any]
) -> dict[str, Any]:
    """Execute a single node, handling both sync and async functions.

    Handles:
    - Sync functions (called directly)
    - Async functions (awaited)
    - Sync generators (accumulated to list)
    - Async generators (accumulated to list)

    Args:
        node: The node to execute
        inputs: Input values for the node

    Returns:
        Dict mapping output names to their values
    """
    from hypergraph.nodes.function import FunctionNode

    if isinstance(node, FunctionNode):
        result = node.func(**inputs)

        # Await if coroutine
        if inspect.iscoroutine(result):
            result = await result

        # Handle async generators
        if inspect.isasyncgen(result):
            result = [item async for item in result]
        # Handle sync generators
        elif inspect.isgenerator(result):
            result = list(result)

        return _wrap_outputs(node, result)

    raise NotImplementedError(
        f"GraphNode execution should be handled by runner, not execute_node_async"
    )


def _wrap_outputs(node: HyperNode, result: Any) -> dict[str, Any]:
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


def run_superstep_sync(
    graph: "Graph",
    state: GraphState,
    ready_nodes: list[HyperNode],
    provided_values: dict[str, Any],
) -> GraphState:
    """Execute one superstep: run all ready nodes and update state.

    In sync mode, nodes are executed sequentially.

    Args:
        graph: The graph being executed
        state: Current state (will be copied, not mutated)
        ready_nodes: Nodes to execute in this superstep
        provided_values: Values provided to runner.run()

    Returns:
        New state with updated values and versions
    """
    new_state = state.copy()

    for node in ready_nodes:
        inputs = collect_inputs_for_node(node, graph, new_state, provided_values)

        # Record input versions before execution
        input_versions = {
            param: new_state.get_version(param) for param in node.inputs
        }

        # Execute node
        outputs = execute_node_sync(node, inputs)

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


async def run_superstep_async(
    graph: "Graph",
    state: GraphState,
    ready_nodes: list[HyperNode],
    provided_values: dict[str, Any],
    max_concurrency: int | None = None,
) -> GraphState:
    """Execute one superstep with concurrent node execution.

    Args:
        graph: The graph being executed
        state: Current state (will be copied, not mutated)
        ready_nodes: Nodes to execute in this superstep
        provided_values: Values provided to runner.run()
        max_concurrency: Max parallel tasks (None = unlimited)

    Returns:
        New state with updated values and versions
    """
    new_state = state.copy()

    # Get or create semaphore for concurrency limiting
    semaphore = _concurrency_limiter.get()
    if semaphore is None and max_concurrency is not None:
        semaphore = asyncio.Semaphore(max_concurrency)
        _concurrency_limiter.set(semaphore)

    async def execute_one(node: HyperNode) -> tuple[HyperNode, dict[str, Any], dict[str, int]]:
        """Execute a single node, respecting concurrency limit."""
        inputs = collect_inputs_for_node(node, graph, state, provided_values)
        input_versions = {
            param: state.get_version(param) for param in node.inputs
        }

        if semaphore:
            async with semaphore:
                outputs = await execute_node_async(node, inputs)
        else:
            outputs = await execute_node_async(node, inputs)

        return node, outputs, input_versions

    # Execute all ready nodes concurrently
    tasks = [execute_one(node) for node in ready_nodes]
    results = await asyncio.gather(*tasks)

    # Update state with all results
    for node, outputs, input_versions in results:
        for name, value in outputs.items():
            new_state.update_value(name, value)

        new_state.node_executions[node.name] = NodeExecution(
            node_name=node.name,
            input_versions=input_versions,
            outputs=outputs,
        )

    return new_state


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
