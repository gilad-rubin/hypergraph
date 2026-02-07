"""Graph class for hypergraph."""

from __future__ import annotations

import functools
import hashlib
import networkx as nx
from typing import Any, TYPE_CHECKING

from hypergraph.nodes.base import HyperNode
from hypergraph.graph._conflict import validate_output_conflicts
from hypergraph.graph._helpers import get_edge_produced_values, sources_of
from hypergraph.graph.input_spec import InputSpec, compute_input_spec
from hypergraph.graph.validation import GraphConfigError, validate_graph

if TYPE_CHECKING:
    from collections.abc import Iterable
    from hypergraph.nodes.graph_node import GraphNode
    from hypergraph.viz.debug import VizDebugger


def _build_hierarchical_id(node_name: str, parent_id: str | None) -> str:
    """Build hierarchical ID: root nodes keep bare name, nested get 'parent/child'."""
    if parent_id is None:
        return node_name
    return f"{parent_id}/{node_name}"


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

    def iter_nodes(self) -> "Iterable[HyperNode]":
        """Iterate over all nodes without copying the internal dict."""
        return self._nodes.values()

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

    @functools.cached_property
    def inputs(self) -> InputSpec:
        """Graph input specification (cached per instance)."""
        return compute_input_spec(self._nodes, self._nx_graph, self._bound)

    @functools.cached_property
    def self_producers(self) -> dict[str, set[str]]:
        """Map output_name → set of node names that produce it.

        Used by the staleness detector: a node skips staleness checks for
        outputs it produces itself, even when other nodes in the same cycle
        also produce that output. This prevents infinite loops in accumulator
        patterns like ``add_response(messages) -> messages``.

        Cyclic re-execution should instead be driven by gates (``@route``).
        """
        output_sources = self._collect_output_sources(list(self._nodes.values()))
        return {
            output: set(sources)
            for output, sources in output_sources.items()
        }

    @functools.cached_property
    def sole_producers(self) -> dict[str, str]:
        """Map output_name → node_name for outputs with exactly one producer.

        Convenience accessor; prefer ``self_producers`` for staleness checks.
        """
        return {
            output: next(iter(nodes))
            for output, nodes in self.self_producers.items()
            if len(nodes) == 1
        }

    def _get_edge_produced_values(self) -> set[str]:
        """Get all value names that are produced by data edges."""
        return get_edge_produced_values(self._nx_graph)

    def _sources_of(self, output: str) -> list[str]:
        """Get all nodes that produce the given output."""
        return sources_of(output, self._nodes)

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
        self._add_ordering_edges(G, nodes, output_to_source)

        # Validate output conflicts with full graph structure
        # This allows mutex branch detection using reachability analysis
        validate_output_conflicts(G, nodes, output_to_sources)

        return G

    def _add_nodes_to_graph(self, G: nx.DiGraph, nodes: list[HyperNode]) -> None:
        """Add nodes with flattened attributes to the graph."""
        for node in nodes:
            attrs = node.nx_attrs
            attrs["parent"] = None  # Root-level nodes
            G.add_node(node.name, **attrs)

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

    def _add_ordering_edges(
        self,
        G: nx.DiGraph,
        nodes: list[HyperNode],
        output_to_source: dict[str, str],
    ) -> None:
        """Add ordering edges for wait_for dependencies.

        Ordering edges connect the producer of a wait_for value to the
        consumer node. They carry no data but express execution order.
        Only added if no data or control edge already exists between the pair.
        """
        for node in nodes:
            for name in node.wait_for:
                producer = output_to_source.get(name)
                if producer is None:
                    continue  # Validation catches missing producers
                if producer == node.name:
                    continue  # Skip self-loops
                if not G.has_edge(producer, node.name):
                    G.add_edge(
                        producer, node.name,
                        edge_type="ordering",
                        value_names=[name],
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
        Clears: cached inputs (depends on _bound which may differ)
        """
        import copy

        new_graph = copy.copy(self)
        new_graph._bound = dict(self._bound)
        # Clear cached_property values that depend on _bound
        new_graph.__dict__.pop("inputs", None)
        # _selected is an immutable tuple (or None), safe to share via copy.copy
        # All other attributes preserved: _strict_types, _nodes, _nx_graph, _cached_hash
        return new_graph

    def _validate(self) -> None:
        """Run all build-time validations."""
        # Note: Duplicate node names caught in _build_nodes_dict()
        # Note: Duplicate outputs caught in validate_output_conflicts()
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

    def visualize(
        self,
        *,
        depth: int = 0,
        theme: str = "auto",
        show_types: bool = False,
        separate_outputs: bool = False,
        filepath: str | None = None,
    ) -> Any:
        """Create an interactive visualization of this graph.

        Renders the graph using React Flow with Kiwi constraint-based layout
        in a Jupyter/VSCode notebook. Works offline with all assets bundled.

        Args:
            depth: How many levels of nested graphs to expand (default: 0)
            theme: "dark", "light", or "auto" to detect from environment
            show_types: Whether to show type annotations on nodes
            separate_outputs: Whether to render outputs as separate DATA nodes
            filepath: Path to save HTML file (default: None, display in notebook)

        Returns:
            ScrollablePipelineWidget if output is None, otherwise None (saves to file)

        Example:
            >>> graph = Graph(nodes=[double, add_one])
            >>> graph.visualize()  # Displays interactive graph
            >>> graph.visualize(theme="light", show_types=True)
            >>> graph.visualize(filepath="graph.html")  # Save to HTML file
        """
        from hypergraph.viz import visualize as viz_func

        return viz_func(
            self,
            depth=depth,
            theme=theme,
            show_types=show_types,
            separate_outputs=separate_outputs,
            filepath=filepath,
        )

    def to_mermaid(
        self,
        *,
        depth: int = 0,
        show_types: bool = False,
        separate_outputs: bool = False,
        direction: str = "TD",
        colors: dict[str, dict[str, str]] | None = None,
    ) -> Any:
        """Generate a Mermaid flowchart diagram for this graph.

        Returns a ``MermaidDiagram`` that auto-renders in Jupyter/VS Code
        notebooks and converts to raw Mermaid source via ``str()`` or
        ``print()``.

        Args:
            depth: How many levels of nested graphs to expand (default: 0)
            show_types: Whether to show type annotations in labels
            separate_outputs: Whether to render outputs as separate DATA nodes
            direction: Flowchart direction — "TD", "TB", "LR", "RL", "BT"
            colors: Custom color overrides per node class, e.g.
                ``{"function": {"fill": "#fff", "stroke": "#000"}}``

        Returns:
            MermaidDiagram — renders in notebooks, ``str()`` gives raw source.

        Example:
            >>> graph.to_mermaid()            # renders in notebook
            >>> print(graph.to_mermaid())      # raw Mermaid source
            >>> graph.to_mermaid().source       # access source directly
        """
        from hypergraph.viz.mermaid import to_mermaid

        return to_mermaid(
            self.to_flat_graph(),
            depth=depth,
            show_types=show_types,
            separate_outputs=separate_outputs,
            direction=direction,
            colors=colors,
        )

    def to_flat_graph(self) -> nx.DiGraph:
        """Create a flattened NetworkX graph with all nested nodes.

        Returns a new DiGraph where:
        - All nodes (root + nested) are in one graph
        - Node attributes include `parent` for hierarchy
        - Node IDs are hierarchical to prevent collisions (e.g., "pipeline1/process")
        - Edges include both root-level and nested edges
        - Graph attributes include `input_spec` and `output_to_sources`

        This is the canonical representation for visualization and analysis.
        """
        G = nx.DiGraph()
        self._flatten_nodes(G, list(self._nodes.values()), parent=None)
        self._flatten_edges(G)

        # Build output_to_sources mapping (supports mutex outputs with multiple sources)
        output_to_sources: dict[str, list[str]] = {}
        for node_id, attrs in G.nodes(data=True):
            for output in attrs.get("outputs", ()):
                if output not in output_to_sources:
                    output_to_sources[output] = []
                output_to_sources[output].append(node_id)

        # Add graph-level attributes
        input_spec = self.inputs
        G.graph["input_spec"] = {
            "required": input_spec.required,
            "optional": input_spec.optional,
            "bound": dict(input_spec.bound),
            "seeds": input_spec.seeds,
        }
        G.graph["output_to_sources"] = output_to_sources
        return G

    def _flatten_nodes(
        self,
        G: nx.DiGraph,
        nodes: list[HyperNode],
        parent: str | None,
    ) -> None:
        """Recursively add nodes to graph with parent relationships.

        Uses hierarchical IDs to prevent collisions across nested graphs:
        - Root nodes: bare name (e.g., "process")
        - Nested nodes: "parent/child" (e.g., "pipeline1/process")
        """
        for node in nodes:
            node_id = _build_hierarchical_id(node.name, parent)
            attrs = node.nx_attrs
            attrs["parent"] = parent
            attrs["original_name"] = node.name  # Store for lookups
            G.add_node(node_id, **attrs)

            inner = node.nested_graph
            if inner is not None:
                self._flatten_nodes(G, list(inner.nodes.values()), parent=node_id)

    def _build_name_to_id_lookup(
        self, G: nx.DiGraph, parent_id: str | None
    ) -> dict[str, str]:
        """Build a lookup from original node names to hierarchical IDs for a scope.

        Args:
            G: The flattened graph with hierarchical IDs
            parent_id: The parent container ID (None for root level)

        Returns:
            Dict mapping original_name -> hierarchical_id for nodes in this scope
        """
        lookup: dict[str, str] = {}
        for node_id, attrs in G.nodes(data=True):
            if attrs.get("parent") == parent_id:
                original_name = attrs.get("original_name", node_id)
                lookup[original_name] = node_id
        return lookup

    def _flatten_edges(self, G: nx.DiGraph) -> None:
        """Add all edges (root + nested) to the flattened graph.

        Translates node names to hierarchical IDs for proper edge routing.
        """
        # Build lookup for root-level nodes
        root_lookup = self._build_name_to_id_lookup(G, None)

        # Add root-level edges with translated IDs
        for src, tgt, data in self._nx_graph.edges(data=True):
            src_id = root_lookup.get(src, src)
            tgt_id = root_lookup.get(tgt, tgt)
            G.add_edge(src_id, tgt_id, **data)

        # Recursively add edges from nested graphs
        for node in self._nodes.values():
            node_id = root_lookup.get(node.name, node.name)
            self._add_nested_edges(G, node, node_id)

    def _add_nested_edges(
        self, G: nx.DiGraph, node: HyperNode, parent_id: str
    ) -> None:
        """Recursively add edges from nested graphs.

        Args:
            G: The flattened graph
            node: The container node
            parent_id: The hierarchical ID of the container
        """
        inner = node.nested_graph
        if inner is None:
            return

        # Build lookup for this container's children
        child_lookup = self._build_name_to_id_lookup(G, parent_id)

        # Add edges with translated IDs
        for src, tgt, data in inner.nx_graph.edges(data=True):
            src_id = child_lookup.get(src, src)
            tgt_id = child_lookup.get(tgt, tgt)
            G.add_edge(src_id, tgt_id, **data)

        # Recurse into children
        for child_node in inner.nodes.values():
            child_id = child_lookup.get(child_node.name, child_node.name)
            self._add_nested_edges(G, child_node, child_id)

    def debug_viz(self) -> "VizDebugger":
        """Get a debugger for this graph's visualization.

        Returns a VizDebugger instance for tracing nodes/edges and finding issues.

        Returns:
            VizDebugger instance

        Example:
            >>> debugger = graph.debug_viz()
            >>> info = debugger.trace_node("my_node")
            >>> print(f"Points from: {info.incoming_edges}")
            >>> print(f"Points to: {info.outgoing_edges}")
            >>> issues = debugger.find_issues()
        """
        from hypergraph.viz.debug import VizDebugger

        return VizDebugger(self)
