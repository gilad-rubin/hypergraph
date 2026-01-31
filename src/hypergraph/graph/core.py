"""Graph class for hypergraph."""

from __future__ import annotations

import functools
import hashlib
import networkx as nx
from collections import Counter
from typing import Any, TYPE_CHECKING

from hypergraph.nodes.base import HyperNode
from hypergraph.graph.input_spec import InputSpec, compute_input_spec
from hypergraph.graph.validation import GraphConfigError, validate_graph

if TYPE_CHECKING:
    from collections.abc import Iterable
    from hypergraph.nodes.graph_node import GraphNode


def _unique_outputs(nodes: Iterable[HyperNode]) -> tuple[str, ...]:
    """Collect outputs from nodes, deduplicating while preserving order.

    GraphNodes wrapping mutex branches can list the same output multiple
    times (e.g., skip_path and process_path both produce 'result').
    """
    all_outputs = [o for n in nodes for o in n.outputs]
    return tuple(dict.fromkeys(all_outputs))


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
        self._selected: tuple[str, ...] | None = None
        self._nodes = self._build_nodes_dict(nodes)
        self._nx_graph = self._build_graph(nodes)
        self._cached_hash: str | None = None
        self._controlled_by: dict[str, list[str]] | None = None
        self._validate()

    @property
    def controlled_by(self) -> dict[str, list[str]]:
        """Map of node_name -> list of controlling gate names."""
        if self._controlled_by is None:
            self._controlled_by = self._compute_controlled_by()
        return self._controlled_by

    def _compute_controlled_by(self) -> dict[str, list[str]]:
        from hypergraph.nodes.gate import GateNode, END

        controlled_by: dict[str, list[str]] = {}
        for node in self._nodes.values():
            if isinstance(node, GateNode):
                for target in node.targets:
                    if target is not END and target in self._nodes:
                        controlled_by.setdefault(target, []).append(node.name)
        return controlled_by

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
        """All unique output names produced by nodes."""
        return _unique_outputs(self._nodes.values())

    @property
    def leaf_outputs(self) -> tuple[str, ...]:
        """Unique outputs from leaf nodes (no downstream destinations)."""
        leaves = [
            self._nodes[name]
            for name in self._nodes
            if self._nx_graph.out_degree(name) == 0
        ]
        return _unique_outputs(leaves)

    @property
    def inputs(self) -> InputSpec:
        """Compute graph input specification."""
        return compute_input_spec(self._nodes, self._nx_graph, self._bound)

    @functools.cached_property
    def sole_producers(self) -> dict[str, str]:
        """Map output_name → node_name for outputs with exactly one producer.

        Used by the staleness detector to implement the Sole Producer Rule:
        a node should not re-trigger from changes to values it produced itself.
        This prevents infinite loops in accumulator patterns like
        ``add_response(messages, response) -> messages`` and self-loops like
        ``transform(x) -> x``.

        Without this rule, the node's own output would make it appear stale,
        causing immediate re-execution in an infinite loop. Cyclic re-execution
        should instead be driven by gates (``@route``).
        """
        output_sources = self._collect_output_sources(list(self._nodes.values()))
        return {
            output: sources[0]
            for output, sources in output_sources.items()
            if len(sources) == 1
        }

    def _get_edge_produced_values(self) -> set[str]:
        """Get all value names that are produced by data edges."""
        result: set[str] = set()
        for _, _, data in self._nx_graph.edges(data=True):
            if data.get("edge_type") == "data":
                result.update(data.get("value_names", []))
        return result

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

    def _collect_output_sources(self, nodes: list[HyperNode]) -> dict[str, list[str]]:
        """Collect all nodes that produce each output name.

        Returns a mapping from output name to list of node names that produce it.
        Used for detecting duplicate outputs before validating mutex exclusivity.
        """
        output_sources: dict[str, list[str]] = {}
        for node in nodes:
            for output in node.outputs:
                output_sources.setdefault(output, []).append(node.name)
        return output_sources

    def _build_graph(self, nodes: list[HyperNode]) -> nx.DiGraph:
        """Build NetworkX DiGraph from nodes with edge inference.

        Process:
        1. Add nodes to graph
        2. Collect all output sources (allowing duplicates temporarily)
        3. Build edges using first source for each output
        4. Add control edges
        5. Validate output conflicts with full graph structure (using reachability)

        This ordering allows mutex branch detection using graph reachability analysis.
        """
        G = nx.DiGraph()
        self._add_nodes_to_graph(G, nodes)

        # Collect all output sources (allowing duplicates temporarily)
        output_to_sources = self._collect_output_sources(nodes)

        # Use first source for each output when building edges
        output_to_source = {k: v[0] for k, v in output_to_sources.items()}
        self._add_data_edges(G, nodes, output_to_source)
        self._add_control_edges(G, nodes)

        # Validate output conflicts with full graph structure
        # This allows mutex branch detection using reachability analysis
        self._validate_output_conflicts(G, nodes, output_to_sources)

        return G

    def _are_all_mutex(
        self, node_names: list[str], mutex_groups: list[list[set[str]]]
    ) -> bool:
        """Check if all given nodes are mutually exclusive.

        Two nodes are mutex if they're in different branches of the same gate.
        All given nodes are mutex if each is in a different branch of the same gate.
        """
        if len(node_names) < 2:
            return True

        # Check each mutex group (each represents a gate with multiple branches)
        for branches in mutex_groups:
            # Check if all nodes are in different branches of this gate
            nodes_by_branch = []
            for branch in branches:
                branch_nodes = [n for n in node_names if n in branch]
                if branch_nodes:
                    nodes_by_branch.append(branch_nodes)

            # If we found each node in a different branch, they're all mutex
            if len(nodes_by_branch) == len(node_names):
                # Each node is in a different branch
                return True

        return False

    def _compute_exclusive_reachability(
        self, G: nx.DiGraph, targets: list[str]
    ) -> dict[str, set[str]]:
        """For each target, find nodes reachable ONLY through that target.

        This is used to expand mutex groups to include downstream nodes.
        A node is "exclusively reachable" from target T if:
        - It is reachable from T (via graph edges)
        - It is NOT reachable from any other target

        Args:
            G: The NetworkX directed graph
            targets: List of gate target node names

        Returns:
            Mapping from target name to set of exclusively reachable node names
        """
        # Get all reachable nodes from each target (including the target itself)
        reachable: dict[str, set[str]] = {
            t: set(nx.descendants(G, t)) | {t} for t in targets
        }

        # Count how many targets can reach each node
        # Optimization: Instead of N^2 set operations, count node occurrences
        # A node is exclusive to a target if it appears exactly once across all reachable sets
        all_reachable_nodes = [node for nodes in reachable.values() for node in nodes]
        node_counts = Counter(all_reachable_nodes)

        # For each target, select nodes that are only reachable from this target (count == 1)
        exclusive: dict[str, set[str]] = {}
        for t in targets:
            exclusive[t] = {
                node for node in reachable[t] if node_counts[node] == 1
            }

        return exclusive

    def _expand_mutex_groups(
        self, G: nx.DiGraph, nodes: list[HyperNode]
    ) -> list[list[set[str]]]:
        """Expand mutex groups to include downstream exclusive nodes.

        For each gate with mutually exclusive targets (RouteNode with multi_target=False
        or IfElseNode), this expands the mutex relationship to include all nodes
        that are exclusively reachable through each target.

        Two nodes are considered mutex if they are in different exclusive branches
        of the same gate - meaning they can never both execute in the same run.

        Args:
            G: The NetworkX directed graph (with edges already added)
            nodes: List of all nodes in the graph

        Returns:
            List of mutex group sets, where each element is a list of branch sets.
            Nodes are mutex only if they're in DIFFERENT branch sets of the same gate.
            Example: [[{A, B}, {C, D}]] means A and B are not mutex with each other,
            but A is mutex with C and D (being in different branches of the gate).
        """
        from hypergraph.nodes.gate import RouteNode, IfElseNode, END

        expanded_groups: list[list[set[str]]] = []

        for node in nodes:
            # Only process gates with mutex targets
            if isinstance(node, RouteNode):
                if node.multi_target:
                    continue  # multi_target means branches can run together
            elif not isinstance(node, IfElseNode):
                continue  # Not a gate node

            # Get real targets (filter out END sentinel)
            targets = [t for t in node.targets if t is not END and isinstance(t, str)]
            if len(targets) < 2:
                continue  # Need at least 2 targets for mutex relationship

            # Compute exclusively reachable nodes for each target
            exclusive_sets = self._compute_exclusive_reachability(G, targets)

            # Store branch sets separately - nodes are mutex only if in DIFFERENT branches
            expanded_groups.append(list(exclusive_sets.values()))

        return expanded_groups

    def _validate_output_conflicts(
        self, G: nx.DiGraph, nodes: list[HyperNode], output_to_sources: dict[str, list[str]]
    ) -> None:
        """Validate that duplicate outputs are in mutually exclusive branches.

        This is called after the graph structure is built (edges added) so we can
        use graph reachability to determine if nodes producing the same output
        are in mutex branches.

        Args:
            G: The NetworkX directed graph (with edges)
            nodes: List of all nodes in the graph
            output_to_sources: Mapping from output name to list of nodes producing it

        Raises:
            GraphConfigError: If multiple nodes produce the same output and they
                are not in mutually exclusive branches.
        """
        # Get expanded mutex groups (includes downstream nodes)
        expanded_groups = self._expand_mutex_groups(G, nodes)

        for output, sources in output_to_sources.items():
            if len(sources) <= 1:
                continue  # No conflict possible

            if self._are_all_mutex(sources, expanded_groups):
                continue  # All sources are mutually exclusive - OK

            # Not all sources are mutex - this is an error
            raise GraphConfigError(
                f"Multiple nodes produce '{output}'\n\n"
                f"  -> {sources[0]} creates '{output}'\n"
                f"  -> {sources[1]} creates '{output}'\n\n"
                f"How to fix: Rename one output to avoid conflict"
            )

    def _add_nodes_to_graph(self, G: nx.DiGraph, nodes: list[HyperNode]) -> None:
        """Add all nodes with their attributes to the graph."""
        G.add_nodes_from((node.name, {"hypernode": node}) for node in nodes)

    def _add_data_edges(
        self,
        G: nx.DiGraph,
        nodes: list[HyperNode],
        output_to_source: dict[str, str],
    ) -> None:
        """Infer data edges by matching parameter names to output names.

        Multiple values between the same node pair are merged into a single
        edge with a ``value_names`` list, since NetworkX DiGraph only allows
        one edge per (source, target) pair.
        """
        from collections import defaultdict

        # Group values by (source, target) pair
        edge_values: dict[tuple[str, str], list[str]] = defaultdict(list)
        for n in nodes:
            for param in n.inputs:
                if param in output_to_source:
                    edge_values[(output_to_source[param], n.name)].append(param)

        G.add_edges_from(
            (src, dst, {"edge_type": "data", "value_names": names})
            for (src, dst), names in edge_values.items()
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

    def select(self, *names: str) -> "Graph":
        """Set default output selection. Returns new Graph (immutable).

        Controls which outputs are returned by runner.run() and which outputs
        are exposed when this graph is used as a nested node via as_node().

        This does NOT affect internal graph execution — all nodes still run
        and all intermediate values are still computed. It only filters what
        is returned to the caller.

        A runtime ``select=`` passed to runner.run() overrides this default.

        Args:
            *names: Output names to include. Must be valid graph outputs.

        Returns:
            New Graph with default selection set.

        Raises:
            ValueError: If any name is not a graph output.

        Example:
            >>> graph = Graph([embed, retrieve, generate]).select("answer")
            >>> result = runner.run(graph, inputs)
            >>> assert list(result.keys()) == ["answer"]

            >>> # As nested node, only "answer" is visible to the parent graph
            >>> outer = Graph([graph.as_node(), postprocess])
        """
        all_outputs = set(self.outputs)
        invalid = [n for n in names if n not in all_outputs]
        if invalid:
            raise ValueError(
                f"Cannot select {invalid}: not graph outputs. "
                f"Valid outputs: {self.outputs}"
            )
        if len(names) != len(set(names)):
            raise ValueError(
                f"select() requires unique output names. Received: {names}"
            )

        new_graph = self._shallow_copy()
        new_graph._selected = names
        return new_graph

    @property
    def selected(self) -> tuple[str, ...] | None:
        """Default output selection, or None if all outputs are returned."""
        return self._selected

    @property
    def has_cycles(self) -> bool:
        """True if graph contains cycles."""
        return not nx.is_directed_acyclic_graph(self._nx_graph)

    @property
    def has_async_nodes(self) -> bool:
        """True if any node requires async execution."""
        return any(node.is_async for node in self._nodes.values())


    @property
    def has_interrupts(self) -> bool:
        """True if any node is an InterruptNode."""
        from hypergraph.nodes.interrupt import InterruptNode
        return any(isinstance(node, InterruptNode) for node in self._nodes.values())

    @property
    def interrupt_nodes(self) -> list:
        """Ordered list of InterruptNode instances."""
        from hypergraph.nodes.interrupt import InterruptNode
        return [node for node in self._nodes.values() if isinstance(node, InterruptNode)]

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
            (u, v, ",".join(data.get("value_names", [])))
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
        # _selected is an immutable tuple (or None), safe to share via copy.copy
        # All other attributes preserved: _strict_types, _nodes, _nx_graph, _cached_hash
        return new_graph

    def _validate(self) -> None:
        """Run all build-time validations."""
        # Note: Duplicate node names caught in _build_nodes_dict()
        # Note: Duplicate outputs caught in _validate_output_conflicts()
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
