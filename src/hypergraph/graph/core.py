"""Graph class for hypergraph."""

from __future__ import annotations

import hashlib
import networkx as nx
from typing import Any, TYPE_CHECKING

from hypergraph.nodes.base import HyperNode
from hypergraph.graph.input_spec import InputSpec, compute_input_spec
from hypergraph.graph.validation import GraphConfigError, validate_graph

if TYPE_CHECKING:
    from hypergraph.nodes.graph_node import GraphNode


class Graph:
    """Define a computation graph from nodes.

    Graph is a pure structure definition - it describes what nodes exist and how
    they connect.

    Edges are inferred automatically: if node A produces output "x" and node B
    has input "x", an edge A→B is created.

    Attributes:
        name: Optional graph name (required for nesting via as_node)
        nodes: Map of node name → HyperNode
        outputs: All output names produced by nodes
        leaf_outputs: Outputs from terminal nodes (no downstream destinations)
        inputs: InputSpec describing required/optional/seed parameters
        has_cycles: True if graph contains cycles
        has_async_nodes: True if any FunctionNode is async
        strict_types: If True, type validation between nodes is enabled
        definition_hash: Merkle-tree hash of graph structure (for caching)

    Example:
        >>> @node(output_name="doubled")
        ... def double(x: int) -> int:
        ...     return x * 2
        >>> @node(output_name="result")
        ... def add_one(doubled: int) -> int:
        ...     return doubled + 1
        >>> g = Graph([double, add_one])
        >>> g.inputs.required
        ('x',)
        >>> g.outputs
        ('doubled', 'result')
    """

    def __init__(
        self,
        nodes: list[HyperNode],
        *,
        name: str | None = None,
        strict_types: bool = False,
    ) -> None:
        """Create a graph from nodes.

        Args:
            nodes: List of HyperNode objects
            name: Optional graph name for nesting
            strict_types: If True, validate type compatibility between connected
                         nodes at graph construction time. Calls _validate_types()
                         which raises GraphConfigError on missing annotations or
                         type mismatches. Default is False (no type checking).
        """
        self.name = name
        self._strict_types = strict_types
        self._bound: dict[str, Any] = {}
        self._nodes = self._build_nodes_dict(nodes)
        self._nx_graph = self._build_graph(nodes)
        self._cached_hash: str | None = None
        self._validate()

    @property
    def nodes(self) -> dict[str, HyperNode]:
        """Map of node name -> node object."""
        return dict(self._nodes)  # Return copy to prevent mutation

    @property
    def strict_types(self) -> bool:
        """Whether type validation is enabled for this graph.

        When True, type compatibility between connected nodes is validated
        at graph construction time via _validate_types().
        """
        return self._strict_types

    @property
    def nx_graph(self) -> nx.DiGraph:
        """Underlying NetworkX graph."""
        return self._nx_graph

    @property
    def outputs(self) -> tuple[str, ...]:
        """All output names produced by nodes."""
        return tuple(
            output for node in self._nodes.values() for output in node.outputs
        )

    @property
    def leaf_outputs(self) -> tuple[str, ...]:
        """Outputs from leaf nodes (no downstream destinations)."""
        leaf_names = [
            name for name in self._nodes if self._nx_graph.out_degree(name) == 0
        ]
        return tuple(
            output
            for name in leaf_names
            for output in self._nodes[name].outputs
        )

    @property
    def inputs(self) -> InputSpec:
        """Compute graph input specification."""
        return compute_input_spec(self._nodes, self._nx_graph, self._bound)

    def _get_edge_produced_values(self) -> set[str]:
        """Get all value names that are produced by edges."""
        return {data["value_name"] for _, _, data in self._nx_graph.edges(data=True)}

    def _sources_of(self, output: str) -> list[str]:
        """Get all nodes that produce the given output."""
        return [node.name for node in self._nodes.values() if output in node.outputs]

    def _build_nodes_dict(self, nodes: list[HyperNode]) -> dict[str, HyperNode]:
        """Build nodes dict, raising immediately on duplicate names."""
        result: dict[str, HyperNode] = {}
        for node in nodes:
            if node.name in result:
                raise GraphConfigError(
                    f"Duplicate node name: '{node.name}'\n\n"
                    f"  -> First defined: {result[node.name]}\n"
                    f"  -> Also defined: {node}\n\n"
                    f"How to fix: Rename one of the nodes"
                )
            result[node.name] = node
        return result

    def _build_graph(self, nodes: list[HyperNode]) -> nx.DiGraph:
        """Build NetworkX DiGraph from nodes with edge inference."""
        G = nx.DiGraph()
        output_to_source = self._create_output_source_mapping(nodes)
        self._add_nodes_to_graph(G, nodes)
        self._add_data_edges(G, nodes, output_to_source)
        self._add_control_edges(G, nodes)
        return G

    def _create_output_source_mapping(
        self, nodes: list[HyperNode]
    ) -> dict[str, str]:
        """Map each output name to its source node name.

        Multiple nodes can produce the same output if they are in mutex paths
        controlled by a gate with multi_target=False.
        """
        from hypergraph.nodes.gate import GateNode, RouteNode

        # First pass: find which nodes are mutex via gate control
        mutex_groups = self._find_mutex_output_groups(nodes)

        result: dict[str, str] = {}
        output_sources: dict[str, list[str]] = {}  # output -> [node_names]

        for node in nodes:
            for output in node.outputs:
                output_sources.setdefault(output, []).append(node.name)

        for output, sources in output_sources.items():
            if len(sources) == 1:
                result[output] = sources[0]
            elif self._are_all_mutex(sources, mutex_groups):
                # All sources are mutually exclusive - pick first for edge building
                result[output] = sources[0]
            else:
                raise GraphConfigError(
                    f"Multiple nodes produce '{output}'\n\n"
                    f"  -> {sources[0]} creates '{output}'\n"
                    f"  -> {sources[1]} creates '{output}'\n\n"
                    f"How to fix: Rename one output to avoid conflict"
                )
        return result

    def _find_mutex_output_groups(
        self, nodes: list[HyperNode]
    ) -> list[set[str]]:
        """Find groups of nodes that are mutually exclusive via gate control.

        Nodes in the same group are mutex if they are targets of a gate
        with multi_target=False (RouteNode) or always-exclusive (IfElseNode).
        """
        from hypergraph.nodes.gate import RouteNode, IfElseNode, END

        mutex_groups: list[set[str]] = []

        for node in nodes:
            # RouteNode with multi_target=False: targets are mutually exclusive
            if isinstance(node, RouteNode) and not node.multi_target:
                targets = {
                    t for t in node.targets
                    if t is not END and isinstance(t, str)
                }
                if len(targets) >= 2:
                    mutex_groups.append(targets)
            # IfElseNode: targets are always mutually exclusive (binary gate)
            elif isinstance(node, IfElseNode):
                targets = {
                    t for t in node.targets
                    if t is not END and isinstance(t, str)
                }
                if len(targets) >= 2:
                    mutex_groups.append(targets)

        return mutex_groups

    def _are_all_mutex(
        self, node_names: list[str], mutex_groups: list[set[str]]
    ) -> bool:
        """Check if all given nodes are mutually exclusive."""
        if len(node_names) < 2:
            return True

        # All nodes must be in the same mutex group
        node_set = set(node_names)
        for group in mutex_groups:
            if node_set <= group:  # All nodes are in this group
                return True
        return False

    def _add_nodes_to_graph(self, G: nx.DiGraph, nodes: list[HyperNode]) -> None:
        """Add all nodes with their attributes to the graph."""
        G.add_nodes_from((node.name, {"hypernode": node}) for node in nodes)

    def _add_data_edges(
        self,
        G: nx.DiGraph,
        nodes: list[HyperNode],
        output_to_source: dict[str, str],
    ) -> None:
        """Infer data edges by matching parameter names to output names."""
        G.add_edges_from(
            (
                output_to_source[param],
                node.name,
                {"edge_type": "data", "value_name": param},
            )
            for node in nodes
            for param in node.inputs
            if param in output_to_source
        )

    def _add_control_edges(
        self,
        G: nx.DiGraph,
        nodes: list[HyperNode],
    ) -> None:
        """Add control edges from gate nodes to their targets.

        Control edges indicate routing relationships but don't carry data.
        """
        from hypergraph.nodes.gate import GateNode, END

        for node in nodes:
            if not isinstance(node, GateNode):
                continue

            for target in node.targets:
                if target is END:
                    continue  # END is not a node
                if target in G.nodes:
                    # Only add control edge if no data edge exists
                    if not G.has_edge(node.name, target):
                        G.add_edge(node.name, target, edge_type="control")

    def bind(self, **values: Any) -> "Graph":
        """Bind default values. Returns new Graph (immutable).

        Raises:
            ValueError: If attempting to bind an output of another node
            ValueError: If attempting to bind a key not in graph.inputs.all
        """
        # Validate: no bound key is edge-produced
        edge_produced = self._get_edge_produced_values()
        for key in values:
            if key in edge_produced:
                # Find which node produces it
                sources = self._sources_of(key)
                raise ValueError(
                    f"Cannot bind '{key}': output of node '{sources[0]}'"
                )

        # Validate: all keys must be valid graph inputs
        all_inputs = set(self.inputs.all)
        for key in values:
            if key not in all_inputs:
                raise ValueError(
                    f"Cannot bind '{key}': not a graph input. "
                    f"Valid inputs: {self.inputs.all}"
                )

        # Create new graph with merged bindings
        new_graph = self._shallow_copy()
        new_graph._bound = {**self._bound, **values}
        return new_graph

    def unbind(self, *keys: str) -> "Graph":
        """Remove specific bindings. Returns new Graph."""
        new_graph = self._shallow_copy()
        new_graph._bound = {k: v for k, v in self._bound.items() if k not in keys}
        return new_graph

    @property
    def has_cycles(self) -> bool:
        """True if graph contains cycles."""
        return not nx.is_directed_acyclic_graph(self._nx_graph)

    @property
    def has_async_nodes(self) -> bool:
        """True if any node requires async execution."""
        return any(node.is_async for node in self._nodes.values())

    @property
    def definition_hash(self) -> str:
        """Merkle-tree hash of graph structure (cached)."""
        if self._cached_hash is None:
            self._cached_hash = self._compute_definition_hash()
        return self._cached_hash

    def _compute_definition_hash(self) -> str:
        """Recursive Merkle-tree style hash.

        Hash includes:
        - Node names and their definition hashes (sorted for determinism)
        - Graph edges (data dependencies)

        Hash excludes:
        - Bindings (runtime values, not structure)
        - Node order in list (sorted by name)
        """
        # 1. Collect node hashes (sorted for determinism)
        node_hashes = []
        for node in sorted(self._nodes.values(), key=lambda n: n.name):
            # All nodes have definition_hash (universal capability on HyperNode)
            node_hashes.append(f"{node.name}:{node.definition_hash}")

        # 2. Include structure (edges)
        edges = sorted(
            (u, v, data.get("value_name", ""))
            for u, v, data in self._nx_graph.edges(data=True)
        )
        edge_str = str(edges)

        # 3. Combine and hash
        combined = "|".join(node_hashes) + "|" + edge_str
        return hashlib.sha256(combined.encode()).hexdigest()

    def _shallow_copy(self) -> "Graph":
        """Create a shallow copy of this graph.

        Preserves: name, strict_types, nodes, nx_graph, cached_hash
        Creates new: _bound dict (to allow independent modifications)
        """
        import copy

        new_graph = copy.copy(self)
        new_graph._bound = dict(self._bound)
        # All other attributes preserved: _strict_types, _nodes, _nx_graph, _cached_hash
        return new_graph

    def _validate(self) -> None:
        """Run all build-time validations."""
        # Note: Duplicate node names caught in _build_nodes_dict()
        # Note: Duplicate outputs caught in _create_output_source_mapping()
        validate_graph(self._nodes, self._nx_graph, self.name, self._strict_types)

    def as_node(self, *, name: str | None = None) -> "GraphNode":
        """Wrap graph as node for composition. Returns new GraphNode.

        Args:
            name: Optional node name. If not provided, uses graph.name.

        Returns:
            GraphNode wrapping this graph

        Raises:
            ValueError: If name is None and graph.name is None
        """
        from hypergraph.nodes.graph_node import GraphNode

        return GraphNode(self, name=name)
