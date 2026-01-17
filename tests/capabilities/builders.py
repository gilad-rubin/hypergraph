"""
Graph builders that create test graphs from Capability specs.

Each builder function creates nodes/graphs matching the requested capability.
"""

from typing import Any

from hypergraph import Graph, node
from hypergraph.nodes import FunctionNode

from .matrix import (
    Capability,
    NodeType,
    Topology,
    NestingDepth,
    MapMode,
)


# =============================================================================
# Node factories - create nodes of specific types
# =============================================================================


def _make_sync_func(name: str, input_name: str, output_name: str) -> FunctionNode:
    """Create a sync function node."""

    @node(output_name=output_name)
    def sync_node(**kwargs: Any) -> int:
        val = kwargs.get(input_name, 0)
        return val * 2 if isinstance(val, int) else 0

    # Rename to match expected interface
    return sync_node.with_name(name).with_inputs(**{list(sync_node.inputs)[0]: input_name})


def _make_async_func(name: str, input_name: str, output_name: str) -> FunctionNode:
    """Create an async function node."""

    @node(output_name=output_name)
    async def async_node(**kwargs: Any) -> int:
        val = kwargs.get(input_name, 0)
        return val * 2 if isinstance(val, int) else 0

    return async_node.with_name(name).with_inputs(**{list(async_node.inputs)[0]: input_name})


def _make_sync_generator(name: str, input_name: str, output_name: str) -> FunctionNode:
    """Create a sync generator node."""

    @node(output_name=output_name)
    def sync_gen(**kwargs: Any):
        val = kwargs.get(input_name, 0)
        if isinstance(val, int):
            yield val
            yield val * 2

    return sync_gen.with_name(name).with_inputs(**{list(sync_gen.inputs)[0]: input_name})


def _make_async_generator(name: str, input_name: str, output_name: str) -> FunctionNode:
    """Create an async generator node."""

    @node(output_name=output_name)
    async def async_gen(**kwargs: Any):
        val = kwargs.get(input_name, 0)
        if isinstance(val, int):
            yield val
            yield val * 2

    return async_gen.with_name(name).with_inputs(**{list(async_gen.inputs)[0]: input_name})


def _make_node(node_type: NodeType, name: str, input_name: str, output_name: str) -> FunctionNode:
    """Create a node of the specified type."""
    factories = {
        NodeType.SYNC_FUNC: _make_sync_func,
        NodeType.ASYNC_FUNC: _make_async_func,
        NodeType.SYNC_GENERATOR: _make_sync_generator,
        NodeType.ASYNC_GENERATOR: _make_async_generator,
    }
    factory = factories.get(node_type)
    if factory is None:
        raise ValueError(f"Cannot create FunctionNode for {node_type}")
    return factory(name, input_name, output_name)


# =============================================================================
# Simple node creators (for common cases)
# =============================================================================


@node(output_name="a")
def double_a(x: int) -> int:
    return x * 2


@node(output_name="b")
def double_b(a: int) -> int:
    return a * 2


@node(output_name="c")
def double_c(b: int) -> int:
    return b * 2


@node(output_name="b1")
def branch1(a: int) -> int:
    return a + 1


@node(output_name="b2")
def branch2(a: int) -> int:
    return a + 2


@node(output_name="c")
def merge(b1: int, b2: int) -> int:
    return b1 + b2


@node(output_name="result")
def add_one(x: int) -> int:
    return x + 1


@node(output_name="a")
async def async_double_a(x: int) -> int:
    return x * 2


@node(output_name="b")
async def async_double_b(a: int) -> int:
    return a * 2


@node(output_name="b1")
async def async_branch1(a: int) -> int:
    return a + 1


@node(output_name="b2")
async def async_branch2(a: int) -> int:
    return a + 2


@node(output_name="c")
async def async_merge(b1: int, b2: int) -> int:
    return b1 + b2


# =============================================================================
# Topology builders
# =============================================================================


def _build_linear(prefer_async: bool = False) -> Graph:
    """Build A -> B -> C linear graph."""
    if prefer_async:
        return Graph([async_double_a, async_double_b], name="linear")
    return Graph([double_a, double_b], name="linear")


def _build_branching(prefer_async: bool = False) -> Graph:
    """Build A -> B1, A -> B2 (fan-out) graph."""
    if prefer_async:
        return Graph(
            [async_double_a, async_branch1.with_inputs(a="a"), async_branch2.with_inputs(a="a")],
            name="branching",
        )
    return Graph([double_a, branch1.with_inputs(a="a"), branch2.with_inputs(a="a")], name="branching")


def _build_converging(prefer_async: bool = False) -> Graph:
    """Build A -> C, B -> C (fan-in) graph."""
    if prefer_async:

        @node(output_name="a")
        async def async_make_a(x: int) -> int:
            return x

        @node(output_name="b")
        async def async_make_b(y: int) -> int:
            return y

        @node(output_name="c")
        async def async_combine(a: int, b: int) -> int:
            return a + b

        return Graph([async_make_a, async_make_b, async_combine], name="converging")

    @node(output_name="a")
    def make_a(x: int) -> int:
        return x

    @node(output_name="b")
    def make_b(y: int) -> int:
        return y

    @node(output_name="c")
    def combine(a: int, b: int) -> int:
        return a + b

    return Graph([make_a, make_b, combine], name="converging")


def _build_diamond(prefer_async: bool = False) -> Graph:
    """Build A -> B1 -> C, A -> B2 -> C (diamond) graph."""
    if prefer_async:
        return Graph(
            [
                async_double_a,
                async_branch1.with_inputs(a="a"),
                async_branch2.with_inputs(a="a"),
                async_merge,
            ],
            name="diamond",
        )
    return Graph([double_a, branch1.with_inputs(a="a"), branch2.with_inputs(a="a"), merge], name="diamond")


def _build_cyclic(prefer_async: bool = False) -> Graph:
    """Build graph with a self-stabilizing cycle."""

    @node(output_name="count")
    def counter_stop(count: int, limit: int = 5) -> int:
        """Increment until limit, then stabilize."""
        if count >= limit:
            return count  # Stabilize - same value means cycle stops
        return count + 1

    @node(output_name="count")
    async def async_counter_stop(count: int, limit: int = 5) -> int:
        """Async version of counter_stop."""
        if count >= limit:
            return count
        return count + 1

    if prefer_async:
        return Graph([async_counter_stop], name="cyclic")
    return Graph([counter_stop], name="cyclic")


def _build_topology(topology: Topology, prefer_async: bool = False) -> Graph:
    """Build a graph with the specified topology."""
    builders = {
        Topology.LINEAR: _build_linear,
        Topology.BRANCHING: _build_branching,
        Topology.CONVERGING: _build_converging,
        Topology.DIAMOND: _build_diamond,
        Topology.CYCLIC: _build_cyclic,
    }
    return builders[topology](prefer_async)


# =============================================================================
# Nesting builders
# =============================================================================


def _wrap_in_nesting(
    graph: Graph, depth: NestingDepth, map_mode: MapMode, map_input: str | None = None
) -> Graph:
    """Wrap a graph in the specified nesting depth."""
    if depth == NestingDepth.FLAT:
        return graph

    current = graph
    for level in range(depth.value):
        gn = current.as_node()

        # Apply map_over on the outermost level if requested
        if level == depth.value - 1 and map_mode != MapMode.NONE:
            # Use specified input or first available
            inputs = list(gn.inputs)
            if inputs:
                input_to_map = map_input if map_input and map_input in inputs else inputs[0]
                mode = "zip" if map_mode == MapMode.ZIP else "product"
                gn = gn.map_over(input_to_map, mode=mode)

        current = Graph([gn], name=f"level_{level + 1}")

    return current


# =============================================================================
# Main builder
# =============================================================================


def build_graph_for_capability(cap: Capability) -> Graph:
    """
    Build a test graph matching the given capability spec.

    Returns a Graph that can be executed with the appropriate runner.
    """
    # Determine if we should prefer async nodes
    prefer_async = cap.has_async_nodes

    # Build base topology
    graph = _build_topology(cap.topology, prefer_async)

    # Determine which input to map over (topology-specific)
    map_input = _get_map_input_for_topology(cap.topology)

    # Apply nesting
    graph = _wrap_in_nesting(graph, cap.nesting, cap.map_mode, map_input)

    return graph


def _get_map_input_for_topology(topology: Topology) -> str | None:
    """Get the input name to use for map_over based on topology."""
    topology_map_inputs = {
        Topology.CYCLIC: "count",  # Map over seed, not limit
        Topology.CONVERGING: "x",  # Map over first input
    }
    return topology_map_inputs.get(topology)


def get_test_inputs(cap: Capability) -> dict:
    """Get appropriate test inputs for a capability."""
    # Cyclic graphs need seed values and limit
    if cap.topology == Topology.CYCLIC:
        base_inputs = {"count": 0, "limit": 3}
    # Converging topology needs two inputs
    elif cap.topology == Topology.CONVERGING:
        base_inputs = {"x": 3, "y": 4}
    else:
        base_inputs = {"x": 5}

    # Map mode needs list inputs for the mapped param only
    if cap.map_mode != MapMode.NONE and cap.has_nesting:
        map_input = _get_map_input_for_topology(cap.topology)
        if map_input and map_input in base_inputs:
            # Only convert the mapped input to a list, others stay as broadcast
            base_inputs[map_input] = [base_inputs[map_input] + i for i in range(3)]
        else:
            # Default: convert first input to list
            first_key = next(iter(base_inputs))
            base_inputs[first_key] = [base_inputs[first_key] + i for i in range(3)]

    return base_inputs


def get_expected_status(_cap: Capability) -> str:
    """Get expected run status for a capability."""
    # All valid combinations should complete successfully
    return "COMPLETED"
