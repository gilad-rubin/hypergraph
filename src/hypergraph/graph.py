"""Graph and InputSpec classes for hypergraph."""

from __future__ import annotations

import hashlib
import networkx as nx
from dataclasses import dataclass
from typing import Any, Iterator, TYPE_CHECKING

from hypergraph.nodes.base import HyperNode

if TYPE_CHECKING:
    from hypergraph.nodes.graph_node import GraphNode


@dataclass(frozen=True)
class InputSpec:
    """Specification of graph input parameters.

    Categories follow the "edge cancels default" rule:
    - required: No edge, no default, not bound -> must always provide
    - optional: No edge, has default OR bound -> can omit (fallback exists)
    - seeds: Has cycle edge -> must provide initial value for first iteration
    """

    required: tuple[str, ...]
    optional: tuple[str, ...]
    seeds: tuple[str, ...]
    bound: dict[str, Any]

    @property
    def all(self) -> tuple[str, ...]:
        """All input names (required + optional + seeds)."""
        return self.required + self.optional + self.seeds


class GraphConfigError(Exception):
    """Raised when graph configuration is invalid."""

    pass


class Graph:
    """Define a computation graph from nodes.

    Graph is a pure structure definition - it describes what nodes exist and how
    they connect, but doesn't execute anything. Pass a Graph to a Runner for execution.

    Edges are inferred automatically: if node A produces output "x" and node B
    has input "x", an edge A→B is created.

    Attributes:
        name: Optional graph name (required for nesting via as_node)
        nodes: Map of node name → HyperNode
        outputs: All output names produced by nodes
        leaf_outputs: Outputs from terminal nodes (no downstream consumers)
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
            strict_types: If True, enable type validation between connected nodes.
                         Type validation will be performed in a later phase.
                         Default is False (no type checking).
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

        When True, type compatibility between connected nodes will be validated.
        Type validation logic is implemented in a later phase.
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
        """Outputs from leaf nodes (no downstream consumers)."""
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
        edge_produced = self._get_edge_produced_values()
        cycle_params = self._get_cycle_params()

        required, optional, seeds = [], [], []

        for param in self._unique_params():
            category = self._categorize_param(param, edge_produced, cycle_params)
            if category == "required":
                required.append(param)
            elif category == "optional":
                optional.append(param)
            elif category == "seed":
                seeds.append(param)

        return InputSpec(
            required=tuple(required),
            optional=tuple(optional),
            seeds=tuple(seeds),
            bound=dict(self._bound),
        )

    def _unique_params(self) -> Iterator[str]:
        """Yield each unique parameter name across all nodes."""
        seen: set[str] = set()
        for node in self._nodes.values():
            for param in node.inputs:
                if param not in seen:
                    seen.add(param)
                    yield param

    def _get_edge_produced_values(self) -> set[str]:
        """Get all value names that are produced by edges."""
        return {
            data["value_name"]
            for _, _, data in self._nx_graph.edges(data=True)
        }

    def _categorize_param(
        self, param: str, edge_produced: set[str], cycle_params: set[str]
    ) -> str | None:
        """Categorize a parameter: 'required', 'optional', 'seed', or None."""
        has_edge = param in edge_produced

        if has_edge:
            return "seed" if param in cycle_params else None

        if param in self._bound or self._any_node_has_default(param):
            return "optional"

        return "required"

    def _any_node_has_default(self, param: str) -> bool:
        """Check if any node consuming this param has a default value."""
        for node in self._nodes.values():
            if param in node.inputs:
                # Check if node has defaults attribute (FunctionNode does)
                if hasattr(node, "defaults") and param in node.defaults:
                    return True
        return False

    def _get_cycle_params(self) -> set[str]:
        """Get parameter names that are part of cycles."""
        cycles = list(nx.simple_cycles(self._nx_graph))
        if not cycles:
            return set()

        return {
            param
            for cycle in cycles
            for param in self._params_flowing_in_cycle(cycle)
        }

    def _params_flowing_in_cycle(self, cycle: list[str]) -> Iterator[str]:
        """Yield params that flow within a cycle."""
        cycle_nodes = set(cycle)
        edge_produced = self._get_edge_produced_values()

        for node_name in cycle:
            for param in self._nodes[node_name].inputs:
                if param not in edge_produced:
                    continue
                if any(p in cycle_nodes for p in self._sources_of(param)):
                    yield param

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
        return G

    def _create_output_source_mapping(
        self, nodes: list[HyperNode]
    ) -> dict[str, str]:
        """Map each output name to its source node name."""
        result: dict[str, str] = {}
        for node in nodes:
            for output in node.outputs:
                if output in result:
                    raise GraphConfigError(
                        f"Multiple nodes produce '{output}'\n\n"
                        f"  -> {result[output]} creates '{output}'\n"
                        f"  -> {node.name} creates '{output}'\n\n"
                        f"How to fix: Rename one output to avoid conflict"
                    )
                result[output] = node.name
        return result

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
        """True if any FunctionNode is async."""
        from hypergraph.nodes.function import FunctionNode

        return any(
            isinstance(node, FunctionNode) and node.is_async
            for node in self._nodes.values()
        )

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
            # FunctionNode has definition_hash, others may not
            if hasattr(node, "definition_hash"):
                node_hashes.append(f"{node.name}:{node.definition_hash}")
            else:
                # Fallback: hash the node's name and structure
                node_str = f"{node.name}:{node.inputs}:{node.outputs}"
                node_hashes.append(
                    f"{node.name}:{hashlib.sha256(node_str.encode()).hexdigest()}"
                )

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
        self._validate_graph_names()
        self._validate_valid_identifiers()
        self._validate_no_namespace_collision()
        self._validate_consistent_defaults()

    def _validate_graph_names(self) -> None:
        """Graph names cannot contain reserved path separators."""
        reserved_chars = {'.', '/'}
        if self.name is not None:
            for char in reserved_chars:
                if char in self.name:
                    raise GraphConfigError(
                        f"Invalid graph name: '{self.name}'\n\n"
                        f"  -> Graph names cannot contain '{char}'\n\n"
                        f"How to fix:\n"
                        f"  Use underscores or hyphens instead"
                    )

    def _validate_valid_identifiers(self) -> None:
        """Node and output names must be valid Python identifiers."""
        for node in self._nodes.values():
            # Skip GraphNode when implemented - it uses graph name validation
            if not node.name.isidentifier():
                raise GraphConfigError(
                    f"Invalid node name: '{node.name}'\n\n"
                    f"  -> Names must be valid Python identifiers\n\n"
                    f"How to fix:\n"
                    f"  Use letters, numbers, underscores only"
                )
            for output in node.outputs:
                if not output.isidentifier():
                    raise GraphConfigError(
                        f"Invalid output name: '{output}' (from node '{node.name}')\n\n"
                        f"  -> Output names must be valid Python identifiers"
                    )

    def _validate_no_namespace_collision(self) -> None:
        """Output names cannot match GraphNode names."""
        # Deferred: requires GraphNode implementation
        pass

    def _validate_consistent_defaults(self) -> None:
        """Shared input parameters must have ALL-or-NONE consistent defaults."""
        param_info = self._collect_param_default_info()

        for param, info_list in param_info.items():
            if len(info_list) < 2:
                continue
            self._check_defaults_consistency(param, info_list)

    def _collect_param_default_info(self) -> dict[str, list[tuple[bool, Any, str]]]:
        """Collect default info for each parameter across all nodes."""
        from collections import defaultdict

        param_info: dict[str, list[tuple[bool, Any, str]]] = defaultdict(list)
        for node in self._nodes.values():
            if not hasattr(node, 'defaults'):
                continue
            for param in node.inputs:
                has_default = param in node.defaults
                default_value = node.defaults.get(param)
                param_info[param].append((has_default, default_value, node.name))
        return param_info

    def _check_defaults_consistency(
        self, param: str, info_list: list[tuple[bool, Any, str]]
    ) -> None:
        """Check that defaults are consistent for a shared parameter."""
        with_default = [(v, n) for has, v, n in info_list if has]
        without_default = [n for has, v, n in info_list if not has]

        if with_default and without_default:
            raise GraphConfigError(
                f"Inconsistent defaults for '{param}'\n\n"
                f"  -> Nodes with default: {', '.join(n for _, n in with_default)}\n"
                f"  -> Nodes without default: {', '.join(without_default)}\n\n"
                f"How to fix:\n"
                f"  Add the same default to all nodes, or use graph.bind()"
            )

        self._check_default_values_match(param, with_default)

    def _check_default_values_match(
        self, param: str, with_default: list[tuple[Any, str]]
    ) -> None:
        """Check that all default values for a parameter are identical."""
        if len(with_default) <= 1:
            return

        first_value, first_node = with_default[0]
        for value, node_name in with_default[1:]:
            if value != first_value:
                raise GraphConfigError(
                    f"Inconsistent defaults for '{param}'\n\n"
                    f"  -> Node '{first_node}' has default: {first_value!r}\n"
                    f"  -> Node '{node_name}' has default: {value!r}\n\n"
                    f"How to fix:\n"
                    f"  Use the same default in both nodes"
                )

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
