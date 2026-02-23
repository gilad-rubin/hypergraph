"""
Graph builders that create test graphs from Capability specs.

Each builder function creates nodes/graphs matching the requested capability.
"""

from typing import Any

from hypergraph import Graph, node
from hypergraph.nodes import FunctionNode

from .matrix import (
    Binding,
    Capability,
    MapMode,
    NestingDepth,
    NodeType,
    Renaming,
    Topology,
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


def _wrap_in_nesting(graph: Graph, depth: NestingDepth, map_mode: MapMode, map_input: str | None = None) -> Graph:
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
# Renaming helpers
# =============================================================================


def _apply_renaming(graph: Graph, renaming: Renaming) -> Graph:
    """Apply renaming to the graph's nodes based on the renaming mode.

    Returns a new graph with renamed nodes/inputs/outputs.
    """
    if renaming == Renaming.NONE:
        return graph

    nodes = list(graph._nodes.values())
    if not nodes:
        return graph

    new_nodes = list(nodes)

    if renaming == Renaming.NODE_NAME:
        # Rename the first node
        target_node = nodes[0]
        renamed = target_node.with_name(f"{target_node.name}_renamed")
        new_nodes[0] = renamed

    elif renaming == Renaming.INPUTS:
        # Rename an input on the first node (if it has external inputs)
        target_node = nodes[0]
        if target_node.inputs:
            old_input = target_node.inputs[0]
            new_input = f"{old_input}_renamed"
            renamed = target_node.with_inputs({old_input: new_input})
            new_nodes[0] = renamed

    elif renaming == Renaming.OUTPUTS:
        # Rename an output on a LEAF node (so we don't break edges)
        # Find a leaf node (node with out_degree == 0)
        leaf_nodes = [(i, n) for i, n in enumerate(nodes) if graph._nx_graph.out_degree(n.name) == 0]
        if leaf_nodes and leaf_nodes[0][1].outputs:
            idx, target_node = leaf_nodes[0]
            old_output = target_node.outputs[0]
            new_output = f"{old_output}_renamed"
            renamed = target_node.with_outputs({old_output: new_output})
            new_nodes[idx] = renamed

    return Graph(new_nodes, name=graph.name, strict_types=graph.strict_types)


def _apply_binding(graph: Graph, binding: Binding, topology: Topology) -> Graph:
    """Apply binding to the graph based on the binding mode.

    Returns a new graph with bound values.
    """
    if binding == Binding.NONE:
        return graph

    # Find an optional input to bind (one with a default or that we can provide)
    # For cyclic graphs, we can bind the 'limit' parameter
    # For other graphs, we can bind any input that has a default
    if topology == Topology.CYCLIC:
        # Bind the limit parameter which has a default
        # Use limit=3 so it stabilizes quickly (within 5 iterations)
        if "limit" in graph.inputs.all:
            return graph.bind(limit=3)
    elif topology == Topology.CONVERGING:
        # For converging, bind one of the inputs
        if "y" in graph.inputs.all:
            return graph.bind(y=100)
    else:
        # For other topologies, we need to be careful not to bind required inputs
        # that don't have defaults. Check if x has a default or is optional
        optional = graph.inputs.optional
        if optional:
            # Bind the first optional input
            return graph.bind(**{optional[0]: 999})

    return graph


# =============================================================================
# Main builder
# =============================================================================

# Cache for built graphs - Capability is frozen/hashable
_graph_cache: dict[Capability, Graph] = {}


def build_graph_for_capability(cap: Capability) -> Graph:
    """
    Build a test graph matching the given capability spec.

    Returns a Graph that can be executed with the appropriate runner.
    Results are cached since Capability is immutable/hashable.
    """
    # Check cache first
    if cap in _graph_cache:
        return _graph_cache[cap]

    # Determine if we should prefer async nodes
    prefer_async = cap.has_async_nodes

    # Build base topology
    graph = _build_topology(cap.topology, prefer_async)

    # Apply renaming before nesting (so renamed nodes get wrapped)
    graph = _apply_renaming(graph, cap.renaming)

    # Determine which input to map over (topology-specific)
    map_input = _get_map_input_for_topology(cap.topology)

    # Adjust map_input if inputs were renamed
    if cap.renaming == Renaming.INPUTS and map_input and map_input not in graph.inputs.all:
        map_input = f"{map_input}_renamed"

    # Apply nesting
    graph = _wrap_in_nesting(graph, cap.nesting, cap.map_mode, map_input)

    # Apply binding after nesting
    graph = _apply_binding(graph, cap.binding, cap.topology)

    # Cache and return
    _graph_cache[cap] = graph
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

    # Handle input renaming - rename the key if inputs were renamed
    if cap.renaming == Renaming.INPUTS:
        # The first input gets renamed with "_renamed" suffix
        if cap.topology == Topology.CYCLIC:
            # 'count' is the first input for cyclic
            if "count" in base_inputs:
                base_inputs["count_renamed"] = base_inputs.pop("count")
        elif cap.topology == Topology.CONVERGING:
            # 'x' is the first input for converging
            if "x" in base_inputs:
                base_inputs["x_renamed"] = base_inputs.pop("x")
        else:
            # 'x' is the first input for other topologies
            if "x" in base_inputs:
                base_inputs["x_renamed"] = base_inputs.pop("x")

    # Handle binding - remove bound inputs from what we provide
    if cap.binding == Binding.BOUND:
        if cap.topology == Topology.CYCLIC:
            # 'limit' is bound, so don't provide it
            base_inputs.pop("limit", None)
        elif cap.topology == Topology.CONVERGING:
            # 'y' is bound, so don't provide it
            base_inputs.pop("y", None)

    # Map mode needs list inputs for the mapped param only
    if cap.map_mode != MapMode.NONE and cap.has_nesting:
        map_input = _get_map_input_for_topology(cap.topology)

        # Adjust map_input name if it was renamed
        if cap.renaming == Renaming.INPUTS and map_input:
            renamed_input = f"{map_input}_renamed"
            if renamed_input in base_inputs:
                map_input = renamed_input

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
