"""Graph class for hypergraph."""

from __future__ import annotations

import functools
import hashlib
from typing import TYPE_CHECKING, Any

import networkx as nx

from hypergraph.graph._conflict import validate_output_conflicts
from hypergraph.graph._helpers import get_edge_produced_values, sources_of
from hypergraph.graph.input_spec import InputSpec, compute_input_spec
from hypergraph.graph.validation import GraphConfigError, validate_graph
from hypergraph.nodes.base import HyperNode

if TYPE_CHECKING:
    from collections.abc import Iterable

    from hypergraph.nodes.graph_node import GraphNode
    from hypergraph.viz.debug import VizDebugger


def _ambiguous_producer_message(param: str, consumer: str, producers: list[str]) -> str:
    """Build a human-readable error for ambiguous implicit wiring."""
    # ASCII diagram showing competing producers
    pad = max(len(p) for p in producers)
    lines = []
    for i, p in enumerate(producers):
        connector = "├" if i < len(producers) - 1 else "└"
        if len(producers) == 2:
            connector = "┐" if i == 0 else "┘"
        lines.append(f"  {p:<{pad}} ──({param})──{connector}")

    target = f"──> {consumer}({param})"
    mid = len(lines) // 2
    diagram_lines = []
    for i, line in enumerate(lines):
        if i == mid:
            diagram_lines.append(f"{line}{target}")
        else:
            diagram_lines.append(f"{line}")

    diagram = "\n".join(diagram_lines)

    return (
        f"Input '{param}' on node '{consumer}' has {len(producers)} competing producers "
        f"and auto-wiring can't pick one.\n\n"
        f"{diagram}\n\n"
        f"How to fix:\n"
        f"  - Add explicit edges to declare the intended dependency, OR\n"
        f"  - Add ordering (emit/wait_for) so one producer is shadowed, OR\n"
        f"  - Rename output channels to avoid the conflict."
    )


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

    By default, edges are inferred automatically: if node A produces output "x"
    and node B has input "x", an edge A→B is created.

    For cyclic graphs with shared output names, use ``edges`` to declare
    topology explicitly::

        Graph([a, b, c], edges=[(a, b), (b, c), (c, a)])

    Attributes:
        name: Optional graph name (required for nesting via as_node)
        nodes: Map of node name → HyperNode
        outputs: All output names produced by nodes
        leaf_outputs: Outputs from terminal nodes (no downstream destinations)
        inputs: InputSpec describing required/optional/entrypoint parameters
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
        edges: list[tuple] | None = None,
        entrypoint: str | list[str] | tuple[str, ...] | None = None,
        name: str | None = None,
        strict_types: bool = False,
        shared: list[str] | None = None,
    ) -> None:
        """Create a graph from nodes.

        Args:
            nodes: List of HyperNode objects
            edges: Explicit edge declarations. Each edge is a tuple of
                ``(source, target)`` or ``(source, target, values)`` where
                source/target are node names (str) or node objects, and values
                is a str or list of str specifying which outputs flow on the
                edge. If omitted on a 2-tuple, values are auto-detected from
                the intersection of source outputs and target inputs. Edges
                with no matching values become ordering-only edges.
                When ``edges`` is provided, auto-inference is disabled.
            entrypoint: Convenience shortcut for ``with_entrypoint(...)``.
                Accepts a node name or a list/tuple of node names and applies
                the same validation and semantics as ``with_entrypoint``.
            name: Optional graph name for nesting
            strict_types: If True, validate type compatibility between connected
                         nodes at graph construction time. Calls _validate_types()
                         which raises GraphConfigError on missing annotations or
                         type mismatches. Default is False (no type checking).
            shared: Parameter names that are shared state across the graph.
                Shared params are excluded from auto-wiring (no data edges
                inferred) and allow multiple producers without conflict.
                Nodes read the latest value from run state. The user must
                provide ordering via ``edges`` or ``emit/wait_for``.
                Shared params are required at ``run()`` time unless bound.
        """
        self.name = name
        self._strict_types = strict_types
        self._shared: frozenset[str] = frozenset(shared) if shared else frozenset()
        self._bound: dict[str, Any] = {}
        self._selected: tuple[str, ...] | None = None
        self._nodes = self._build_nodes_dict(nodes)
        self._validate_shared_params()
        self._entrypoints = self._normalize_constructor_entrypoints(entrypoint)
        self._explicit_edges = self._normalize_edges(edges) if edges is not None else None
        self._nx_graph = self._build_graph(nodes)
        self._cached_hash: str | None = None
        self._cached_structural_hash: str | None = None
        self._controlled_by: dict[str, list[str]] | None = None
        self._validate()

    def _normalize_constructor_entrypoints(
        self,
        entrypoint: str | list[str] | tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        """Normalize constructor ``entrypoint=`` into validated tuple form."""
        if entrypoint is None:
            return None

        if isinstance(entrypoint, str):
            node_names = (entrypoint,)
        elif isinstance(entrypoint, (list, tuple)):
            node_names = tuple(entrypoint)
        else:
            raise GraphConfigError("entrypoint must be a node name (str) or a list/tuple of node names")

        self._validate_entrypoint_names(node_names)
        return tuple(dict.fromkeys(node_names))

    def _validate_shared_params(self) -> None:
        """Validate that shared param names actually exist as outputs in the graph."""
        if not self._shared:
            return
        all_outputs: set[str] = set()
        for node in self._nodes.values():
            all_outputs.update(node.outputs)
        unknown = sorted(self._shared - all_outputs)
        if unknown:
            raise GraphConfigError(f"shared params {unknown} are not produced by any node.\nAvailable outputs: {sorted(all_outputs)}")

    def _validate_shared_connectivity(self, G: nx.DiGraph, nodes: list[HyperNode]) -> None:
        """Validate that shared params don't leave the graph disconnected.

        After auto-wiring with shared params excluded, some nodes may become
        unreachable. Report the gap so the user can add ordering edges.
        """
        undirected = G.to_undirected()
        components = list(nx.connected_components(undirected))
        if len(components) <= 1:
            return

        # Build a readable description of each island
        islands: list[str] = []
        for component in sorted(components, key=lambda c: min(c)):
            # Show edges within this island
            edges_in = []
            for u, v, data in G.edges(data=True):
                if u in component and v in component:
                    value_names = data.get("value_names", [])
                    label = f"({', '.join(value_names)})" if value_names else ""
                    edge_type = data.get("edge_type", "data")
                    style = " (control)" if edge_type == "control" else ""
                    edges_in.append(f"    {u} -> {v}{' ' + label if label else ''}{style}")

            node_list = sorted(component)
            edge_desc = "\n".join(edges_in) if edges_in else "    (no edges)"
            islands.append(f"  [{', '.join(node_list)}]\n{edge_desc}")

        raise GraphConfigError(
            f"Graph is disconnected after auto-wiring with shared={sorted(self._shared)}.\n\n"
            f"These groups of nodes have no edges connecting them:\n\n" + "\n\n".join(islands) + "\n\n"
            "How to fix:\n"
            "  Add edges=[(node_a, node_b), ...] or emit/wait_for to connect them."
        )

    def _validate_entrypoint_names(self, node_names: tuple[str, ...]) -> None:
        """Validate entrypoint node names and ensure they are non-gate nodes."""
        from hypergraph.nodes.gate import GateNode

        for name in node_names:
            if not isinstance(name, str):
                raise GraphConfigError(f"Entry point names must be strings, got {type(name).__name__}")

            if name not in self._nodes:
                raise GraphConfigError(
                    f"Unknown entry point node: '{name}'\n\n  -> '{name}' is not in the graph\n  -> Available nodes: {sorted(self._nodes.keys())}"
                )
            if isinstance(self._nodes[name], GateNode):
                raise GraphConfigError(
                    f"Cannot use gate '{name}' as entry point\n\n"
                    f"  -> Gates control routing, they cannot be entry points\n\n"
                    f"How to fix:\n"
                    f"  Use a non-gate node as the entry point"
                )

    @property
    def controlled_by(self) -> dict[str, list[str]]:
        """Map of node_name -> list of controlling gate names."""
        if self._controlled_by is None:
            self._controlled_by = self._compute_controlled_by()
        return self._controlled_by

    def _compute_controlled_by(self) -> dict[str, list[str]]:
        from hypergraph.nodes.gate import END, GateNode

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

    def iter_nodes(self) -> Iterable[HyperNode]:
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
        leaves = [self._nodes[name] for name in self._nodes if self._nx_graph.out_degree(name) == 0]
        return _unique_outputs(leaves)

    @functools.cached_property
    def inputs(self) -> InputSpec:
        """Graph input specification (cached per instance)."""
        return compute_input_spec(
            self._nodes,
            self._nx_graph,
            self._bound,
            entrypoints=self._entrypoints,
            selected=self._selected,
        )

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
        return {output: set(sources) for output, sources in output_sources.items()}

    @functools.cached_property
    def downstream_produced(self) -> dict[str, frozenset[str]]:
        """Map node_name → frozenset of params produced by descendants only.

        In a DAG, when ALL producers of a param are descendants (downstream)
        of the consuming node, their writes should not trigger re-execution —
        the natural execution order is consumer-first, producer-second, and
        the producer's output is the final value, not a signal to loop.

        Only computed for acyclic graphs; returns empty for cyclic ones
        (where staleness is driven by gates instead).
        """
        if self.has_cycles:
            return {}

        from hypergraph.graph.input_spec import _data_only_subgraph

        data_graph = _data_only_subgraph(self._nx_graph)
        result: dict[str, frozenset[str]] = {}
        for node_name in data_graph.nodes():
            node = self._nodes.get(node_name)
            if node is None:
                continue
            descendants = nx.descendants(data_graph, node_name)
            exempt = frozenset(param for param in node.inputs if (producers := self.self_producers.get(param)) and producers <= descendants)
            if exempt:
                result[node_name] = exempt
        return result

    @functools.cached_property
    def sole_producers(self) -> dict[str, str]:
        """Map output_name → node_name for outputs with exactly one producer.

        Convenience accessor; prefer ``self_producers`` for staleness checks.
        """
        return {output: next(iter(nodes)) for output, nodes in self.self_producers.items() if len(nodes) == 1}

    @functools.cached_property
    def input_data_producers(self) -> dict[str, dict[str, frozenset[str]]]:
        """Map node_name -> input_name -> producers on incoming DATA edges.

        In explicit-edge graphs, this captures exactly which producers are
        wired to each input. The runner uses it to avoid marking a node stale
        when an identically named value changes via a non-wired producer.
        """
        producer_map: dict[str, dict[str, set[str]]] = {}
        for src, dst, data in self._nx_graph.edges(data=True):
            if data.get("edge_type") != "data":
                continue
            for value_name in data.get("value_names", []):
                producer_map.setdefault(dst, {}).setdefault(value_name, set()).add(src)

        frozen: dict[str, dict[str, frozenset[str]]] = {}
        for node_name, inputs in producer_map.items():
            frozen[node_name] = {input_name: frozenset(names) for input_name, names in inputs.items()}
        return frozen

    @functools.cached_property
    def explicit_predecessors(self) -> dict[str, frozenset[str]]:
        """Map node_name -> direct predecessors declared via explicit edges.

        Only populated for graphs built with ``edges=[...]``. This captures the
        user-declared topology directly from constructor edge specs (data and
        ordering declarations alike), and is used by scheduler readiness checks
        to enforce predecessor-driven startup semantics.
        """
        if self._explicit_edges is None:
            return {}

        predecessors: dict[str, set[str]] = {}
        for src, dst, _value_names in self._explicit_edges:
            predecessors.setdefault(dst, set()).add(src)

        return {node_name: frozenset(sources) for node_name, sources in predecessors.items()}

    @property
    def has_explicit_edges(self) -> bool:
        """Whether the graph was built in explicit-edges mode."""
        return self._explicit_edges is not None

    def _get_edge_produced_values(self) -> set[str]:
        """Get all value names that are produced by data edges."""
        return get_edge_produced_values(self._nx_graph)

    def _get_emit_only_outputs(self) -> set[str]:
        """Get outputs that are only emitted (ordering signals, not data)."""
        data_outputs: set[str] = set()
        all_outputs: set[str] = set()
        for node in self._nodes.values():
            data_outputs.update(node.data_outputs)
            all_outputs.update(node.outputs)
        return all_outputs - data_outputs

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

    def _normalize_edges(
        self,
        edges: list[tuple],
    ) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
        """Normalize edge specs to frozen (source, target, value_names) triples.

        Supports:
        - 2-tuple: (source, target) — values inferred from output/input overlap
        - 3-tuple: (source, target, values) — explicit value names

        Source/target can be node name strings or HyperNode objects.
        """
        normalized: list[tuple[str, str, tuple[str, ...]]] = []
        for edge in edges:
            if not isinstance(edge, tuple) or len(edge) not in (2, 3):
                raise GraphConfigError(
                    f"Edge must be a 2-tuple or 3-tuple, got {type(edge).__name__} with {len(edge) if isinstance(edge, tuple) else '?'} elements"
                )

            src_raw, dst_raw = edge[0], edge[1]
            src = src_raw.name if isinstance(src_raw, HyperNode) else src_raw
            dst = dst_raw.name if isinstance(dst_raw, HyperNode) else dst_raw

            if src not in self._nodes:
                raise GraphConfigError(f"Edge references unknown source node '{src}'")
            if dst not in self._nodes:
                raise GraphConfigError(f"Edge references unknown target node '{dst}'")

            if len(edge) == 3:
                values = edge[2]
                if isinstance(values, str):
                    values = [values]
                src_outputs = set(self._nodes[src].outputs)
                dst_inputs = set(self._nodes[dst].inputs)
                for v in values:
                    if v not in src_outputs:
                        raise GraphConfigError(f"Edge ({src}, {dst}): '{v}' is not an output of '{src}'. Outputs: {self._nodes[src].outputs}")
                    if v not in dst_inputs:
                        raise GraphConfigError(f"Edge ({src}, {dst}): '{v}' is not an input of '{dst}'. Inputs: {self._nodes[dst].inputs}")
                # Shared params become ordering-only (data flows through state)
                value_names = tuple(v for v in dict.fromkeys(values) if v not in self._shared)
            else:
                # Infer from intersection (preserve target input order)
                # Shared params excluded — they don't create data edges
                src_outputs = set(self._nodes[src].outputs)
                value_names = tuple(v for v in self._nodes[dst].inputs if v in src_outputs and v not in self._shared)
                # Empty intersection is allowed — creates ordering-only edge

            normalized.append((src, dst, value_names))

        return tuple(normalized)

    def _add_explicit_data_edges(
        self,
        G: nx.DiGraph,
        normalized_edges: tuple[tuple[str, str, tuple[str, ...]], ...],
    ) -> None:
        """Add user-declared edges to the graph.

        Edges with values get edge_type="data". Edges with no values
        (empty intersection) get edge_type="ordering" (structural dependency).
        """
        from collections import defaultdict

        data_edges: dict[tuple[str, str], list[str]] = defaultdict(list)
        ordering_pairs: set[tuple[str, str]] = set()

        for src, dst, value_names in normalized_edges:
            if value_names:
                data_edges[(src, dst)].extend(value_names)
            else:
                ordering_pairs.add((src, dst))

        # Add data edges (dedup value_names per pair)
        G.add_edges_from((src, dst, {"edge_type": "data", "value_names": list(dict.fromkeys(names))}) for (src, dst), names in data_edges.items())

        # Add ordering-only edges (skip if data edge already exists)
        for src, dst in ordering_pairs:
            if not G.has_edge(src, dst):
                G.add_edge(src, dst, edge_type="ordering", value_names=[])

    def _build_graph(self, nodes: list[HyperNode]) -> nx.DiGraph:
        """Build NetworkX DiGraph from nodes.

        Uses explicit edges if provided, otherwise infers edges from
        matching output/input names.
        """
        G = nx.DiGraph()
        self._add_nodes_to_graph(G, nodes)

        output_to_sources = self._collect_output_sources(nodes)
        # Shared params allow multiple producers — exclude from conflict checks
        conflict_sources = {k: v for k, v in output_to_sources.items() if k not in self._shared} if self._shared else output_to_sources

        if self._explicit_edges is not None and not self._shared:
            # Explicit mode: user-declared data edges (no auto-inference)
            self._add_explicit_data_edges(G, self._explicit_edges)
            self._add_control_edges(G, nodes)
            self._add_ordering_edges(G, nodes, output_to_sources)
            validate_output_conflicts(
                G,
                nodes,
                conflict_sources,
                explicit_edges=True,
            )
        elif self._shared:
            # Shared mode: auto-infer non-shared edges, add explicit as ordering
            self._add_data_edges(G, nodes, output_to_sources)
            if self._explicit_edges is not None:
                self._add_explicit_data_edges(G, self._explicit_edges)
            self._add_control_edges(G, nodes)
            self._add_ordering_edges(G, nodes, output_to_sources)
            validate_output_conflicts(G, nodes, conflict_sources)
        else:
            # Auto-inference mode (default)
            self._add_data_edges(G, nodes, output_to_sources)
            self._add_control_edges(G, nodes)
            self._add_ordering_edges(G, nodes, output_to_sources)
            validate_output_conflicts(G, nodes, conflict_sources)

        if self._shared:
            self._validate_shared_connectivity(G, nodes)

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
        output_to_sources: dict[str, list[str]],
    ) -> None:
        """Infer implicit data edges by realizable producer->consumer handoff.

        For each consumer input name, all matching producers are considered.
        Then producer-shadow elimination is applied:
        remove producer ``u -> v (p)`` iff every valid alternate path by which
        ``p`` could flow from ``u`` to ``v`` passes through another producer of
        ``p`` first.

        Ambiguous cycle inputs are rejected when multiple non-shadowed
        producers remain for the same ``(consumer, input_name)`` pair.
        """
        from collections import defaultdict

        # Build producer candidates per (consumer, input_name).
        # Shared params are excluded — they flow through run state, not edges.
        by_input: dict[tuple[str, str], list[str]] = {}
        for consumer in nodes:
            for param in consumer.inputs:
                if param in self._shared:
                    continue
                producers = [producer for producer in output_to_sources.get(param, [])]
                if producers:
                    by_input[(consumer.name, param)] = list(dict.fromkeys(producers))

        if not by_input:
            return

        by_input = self._apply_shadow_elimination(nodes, by_input, output_to_sources)
        self._validate_no_cycle_input_ambiguity(nodes, by_input)

        # Group value names by (source, target) pair because DiGraph supports
        # one edge per pair.
        edge_values: dict[tuple[str, str], list[str]] = defaultdict(list)
        for (consumer, param), producers in by_input.items():
            for producer in producers:
                edge_values[(producer, consumer)].append(param)

        G.add_edges_from(
            (
                src,
                dst,
                {
                    "edge_type": "data",
                    "value_names": list(dict.fromkeys(names)),
                },
            )
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
        from hypergraph.nodes.gate import END, GateNode

        for node in nodes:
            if not isinstance(node, GateNode):
                continue

            for target in node.targets:
                if target is END:
                    continue  # END is not a node
                if target in G.nodes and not G.has_edge(node.name, target):
                    # Only add control edge if no data edge exists
                    G.add_edge(node.name, target, edge_type="control")

    def _add_ordering_edges(
        self,
        G: nx.DiGraph,
        nodes: list[HyperNode],
        output_to_sources: dict[str, list[str]],
    ) -> None:
        """Add ordering edges for wait_for dependencies.

        Ordering edges connect each producer of a wait_for value to the
        consumer node. They carry no data but express execution order.
        Only added if no data or control edge already exists between the pair.
        """
        for node in nodes:
            for name in node.wait_for:
                producers = output_to_sources.get(name, [])
                if not producers:
                    continue  # Validation catches missing producers
                for producer in producers:
                    if producer == node.name:
                        continue  # Skip self-loops
                    if not G.has_edge(producer, node.name):
                        G.add_edge(
                            producer,
                            node.name,
                            edge_type="ordering",
                            value_names=[name],
                        )

    def _apply_shadow_elimination(
        self,
        nodes: list[HyperNode],
        by_input: dict[tuple[str, str], list[str]],
        output_to_sources: dict[str, list[str]],
    ) -> dict[tuple[str, str], list[str]]:
        """Remove shadowed producer candidates from implicit input wiring.

        Shadow rule:
        For consumer input ``p``, producer edge ``u -> v (p)`` is removed iff:
        1) ``u -> v`` has at least one alternate execution path
           (excluding the direct candidate edge), and
        2) every such path passes through another producer of ``p`` first.
        """
        candidate_graph = nx.DiGraph()
        candidate_graph.add_nodes_from(node.name for node in nodes)
        for (consumer, _param), producers in by_input.items():
            for producer in producers:
                candidate_graph.add_edge(producer, consumer)
        self._add_candidate_control_and_ordering_edges(candidate_graph, nodes, output_to_sources)
        invalid_nodes_by_consumer = self._invalid_shadow_path_nodes_by_consumer(nodes)

        pruned: dict[tuple[str, str], list[str]] = {}
        for key, producers in by_input.items():
            consumer, param = key
            if len(producers) <= 1:
                pruned[key] = producers
                continue

            kept = [
                producer
                for producer in producers
                if not self._is_shadowed_producer(
                    candidate_graph=candidate_graph,
                    producer=producer,
                    consumer=consumer,
                    competing_producers=set(producers),
                    invalid_path_nodes=invalid_nodes_by_consumer.get(consumer),
                )
            ]

            if kept:
                pruned[key] = kept
            else:
                raise GraphConfigError(_ambiguous_producer_message(param, consumer, sorted(producers)))

        return pruned

    def _invalid_shadow_path_nodes_by_consumer(self, nodes: list[HyperNode]) -> dict[str, set[str]]:
        """Nodes that should not be traversed in shadow-path checks per consumer.

        For gate consumers, paths that route through the gate's own targets are
        not considered valid evidence of upstream shadowing. Those targets are
        activated by the gate decision itself and would otherwise create
        circular self-justifying paths (gate -> target ... -> gate).
        """
        from hypergraph.nodes.gate import END, GateNode

        invalid: dict[str, set[str]] = {}
        for node in nodes:
            if not isinstance(node, GateNode):
                continue
            blocked = {target for target in node.targets if target is not END and isinstance(target, str) and target != node.name}
            if blocked:
                invalid[node.name] = blocked
        return invalid

    def _is_shadowed_producer(
        self,
        *,
        candidate_graph: nx.DiGraph,
        producer: str,
        consumer: str,
        competing_producers: set[str],
        invalid_path_nodes: set[str] | None = None,
    ) -> bool:
        """Whether producer->consumer is shadowed by other producers.

        Edge ``producer -> consumer`` is shadowed when every alternate path from
        producer to consumer (excluding the direct edge) must pass through at
        least one other competing producer first.
        """
        others = competing_producers - {producer}
        if not others:
            return False

        blocked = set(invalid_path_nodes or ())

        has_alt_path = self._has_path_avoiding_nodes(
            candidate_graph,
            producer,
            consumer,
            blocked_nodes=blocked,
            skip_edge=(producer, consumer),
        )
        if not has_alt_path:
            return False

        has_path_without_others = self._has_path_avoiding_nodes(
            candidate_graph,
            producer,
            consumer,
            blocked_nodes=blocked | (others - {consumer}),
            skip_edge=(producer, consumer),
        )
        return not has_path_without_others

    def _add_candidate_control_and_ordering_edges(
        self,
        graph: nx.DiGraph,
        nodes: list[HyperNode],
        output_to_sources: dict[str, list[str]],
    ) -> None:
        """Add control/order precedence edges used for cycle-context detection."""
        from hypergraph.nodes.gate import END, GateNode

        node_names = {node.name for node in nodes}

        for node in nodes:
            if isinstance(node, GateNode):
                for target in node.targets:
                    if target is END or target not in node_names:
                        continue
                    graph.add_edge(node.name, target)

            for wait_name in node.wait_for:
                for producer in output_to_sources.get(wait_name, []):
                    if producer != node.name:
                        graph.add_edge(producer, node.name)

    def _validate_no_cycle_input_ambiguity(
        self,
        nodes: list[HyperNode],
        by_input: dict[tuple[str, str], list[str]],
    ) -> None:
        """Reject cycle inputs that still have multiple producer candidates."""
        from hypergraph.nodes.gate import GateNode

        node_by_name = {node.name: node for node in nodes}
        candidate_graph = nx.DiGraph()
        candidate_graph.add_nodes_from(node.name for node in nodes)
        for (consumer, _param), producers in by_input.items():
            for producer in producers:
                candidate_graph.add_edge(producer, consumer)

        cycle_nodes: set[str] = set()
        for component in nx.strongly_connected_components(candidate_graph):
            comp = set(component)
            if len(comp) > 1 or any(candidate_graph.has_edge(n, n) for n in comp):
                cycle_nodes.update(comp)

        for (consumer, param), producers in by_input.items():
            if consumer not in cycle_nodes:
                continue
            if isinstance(node_by_name.get(consumer), GateNode):
                continue
            external_producers = [p for p in producers if p != consumer]
            if len(external_producers) <= 1:
                continue
            raise GraphConfigError(_ambiguous_producer_message(param, consumer, sorted(external_producers)))

    def _has_path_avoiding_nodes(
        self,
        graph: nx.DiGraph,
        source: str,
        target: str,
        *,
        blocked_nodes: set[str],
        skip_edge: tuple[str, str] | None = None,
    ) -> bool:
        """Return True if a path exists while avoiding blocked nodes."""
        from collections import deque

        queue: deque[str] = deque([source])
        visited: set[str] = {source}

        while queue:
            node_name = queue.popleft()
            for nxt in graph.successors(node_name):
                if skip_edge is not None and (node_name, nxt) == skip_edge:
                    continue
                if nxt in blocked_nodes and nxt != target:
                    continue
                if nxt == target:
                    return True
                if nxt in visited:
                    continue
                visited.add(nxt)
                queue.append(nxt)

        return False

    def bind(self, **values: Any) -> Graph:
        """Bind default values. Returns new Graph (immutable).

        Accepts any graph input or output name. Bound values act as
        pre-filled run() values — overridable at run time.

        Raises:
            ValueError: If key is not a valid graph input or output
            ValueError: If key is an emit-only output (ordering signal)
        """
        valid_names = set(self.inputs.all) | set(self.outputs)
        emit_only = self._get_emit_only_outputs()
        valid_names -= emit_only

        for key in values:
            if key in emit_only:
                raise ValueError(f"Cannot bind '{key}': emit-only output (ordering signal, not data)")
            if key not in valid_names:
                raise ValueError(f"Cannot bind '{key}': not a graph input or output. Valid names: {sorted(valid_names)}")

        new_graph = self._shallow_copy()
        new_graph._bound = {**self._bound, **values}
        return new_graph

    def unbind(self, *keys: str) -> Graph:
        """Remove specific bindings. Returns new Graph."""
        new_graph = self._shallow_copy()
        new_graph._bound = {k: v for k, v in self._bound.items() if k not in keys}
        return new_graph

    def select(self, *names: str) -> Graph:
        """Set default output selection. Returns new Graph (immutable).

        Controls which outputs are returned by runner.run() and which outputs
        are exposed when this graph is used as a nested node via as_node().

        Also narrows ``graph.inputs`` to only parameters needed to produce
        the selected outputs. Nodes that don't contribute are excluded from
        InputSpec computation. However, at execution time all reachable nodes
        still run — use ``with_entrypoint()`` to skip upstream execution.

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
            raise ValueError(f"Cannot select {invalid}: not graph outputs. Valid outputs: {self.outputs}")
        if len(names) != len(set(names)):
            raise ValueError(f"select() requires unique output names. Received: {names}")

        new_graph = self._shallow_copy()
        new_graph._selected = names
        return new_graph

    def add_nodes(self, *nodes: HyperNode) -> Graph:
        """Add nodes to graph. Returns new Graph (immutable).

        Equivalent to rebuilding the graph with the combined node list,
        then replaying bind/select.

        Raises GraphConfigError if graph was constructed with explicit edges.
        Raises ValueError if existing bindings become invalid after
        adding nodes (e.g., a bound key becomes emit-only).
        Fix: call unbind() before add_nodes().
        """
        if self._explicit_edges is not None:
            raise GraphConfigError(
                "Cannot use add_nodes() on a graph with explicit edges.\nCreate a new Graph with the complete node and edge lists instead."
            )

        if not nodes:
            return self

        all_nodes = list(self._nodes.values()) + list(nodes)
        new_graph = Graph(all_nodes, name=self.name, strict_types=self._strict_types)

        if self._bound:
            valid_names = set(new_graph.inputs.all) | set(new_graph.outputs)
            valid_names -= new_graph._get_emit_only_outputs()
            invalid = [k for k in self._bound if k not in valid_names]
            if invalid:
                raise ValueError(
                    f"Cannot replay bind after add_nodes: {invalid} no longer valid. Call unbind({', '.join(repr(k) for k in invalid)}) first."
                )
            new_graph._bound = dict(self._bound)
            new_graph.__dict__.pop("inputs", None)

        if self._selected is not None:
            new_graph = new_graph.select(*self._selected)

        return new_graph

    @property
    def selected(self) -> tuple[str, ...] | None:
        """Default output selection, or None if all outputs are returned."""
        return self._selected

    def with_entrypoint(self, *node_names: str) -> Graph:
        """Set execution entry points. Returns new Graph (immutable).

        Entry points define where execution enters the graph. Upstream
        nodes are excluded — their outputs become direct user inputs.

        Works for both DAGs and cycles:
        - DAG: entry point determines where computation starts
        - Cycle: entry point determines cycle bootstrap requirements

        Chainable: ``graph.with_entrypoint("A").with_entrypoint("B")``

        Args:
            *node_names: One or more node names to use as entry points.

        Returns:
            New Graph with entry points configured.

        Raises:
            GraphConfigError: If node doesn't exist or is a gate.

        Example:
            >>> # Skip upstream, provide intermediate values directly
            >>> g = Graph([root, process, output])
            >>> g2 = g.with_entrypoint("process")
            >>> g2.inputs.required  # only process's unproduced inputs
        """
        self._validate_entrypoint_names(node_names)

        new_graph = self._shallow_copy()
        existing = self._entrypoints or ()
        new_graph._entrypoints = tuple(dict.fromkeys(existing + tuple(node_names)))
        return new_graph

    @property
    def entrypoints_config(self) -> tuple[str, ...] | None:
        """Configured entry points, or None if all nodes are active."""
        return self._entrypoints

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
        """True if any node is an interrupt node."""
        return any(node.is_interrupt for node in self._nodes.values())

    @property
    def interrupt_nodes(self) -> list:
        """Ordered list of interrupt node instances."""
        return [node for node in self._nodes.values() if node.is_interrupt]

    @property
    def definition_hash(self) -> str:
        """Merkle-tree hash of graph structure (cached)."""
        if self._cached_hash is None:
            self._cached_hash = self._compute_definition_hash()
        return self._cached_hash

    @property
    def code_hash(self) -> str:
        """Code-sensitive hash used by caching and change observability.

        This includes node definition hashes (function/code sensitive).
        """
        return self.definition_hash

    @property
    def structural_hash(self) -> str:
        """Hash of graph topology and node signatures (excluding function code)."""
        if self._cached_structural_hash is None:
            self._cached_structural_hash = self._compute_structural_hash()
        return self._cached_structural_hash

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
        edges = sorted((u, v, ",".join(data.get("value_names", []))) for u, v, data in self._nx_graph.edges(data=True))
        edge_str = str(edges)

        # 3. Combine and hash
        combined = "|".join(node_hashes) + "|" + edge_str
        return hashlib.sha256(combined.encode()).hexdigest()

    def _compute_structural_hash(self) -> str:
        """Compute hash for resume compatibility checks.

        Includes node identity/topology and declared interfaces, but excludes
        function source code so bug-fix edits do not force forking.
        """
        from hypergraph.nodes.gate import GateNode
        from hypergraph.nodes.graph_node import GraphNode

        node_signatures: list[str] = []
        for node in sorted(self._nodes.values(), key=lambda n: n.name):
            parts = [
                node.__class__.__name__,
                node.name,
                ",".join(node.inputs),
                ",".join(node.outputs),
                ",".join(getattr(node, "wait_for", ())),
            ]
            if isinstance(node, GateNode):
                targets = [t if isinstance(t, str) else "END" for t in node.targets]
                parts.extend(
                    [
                        "targets=" + ",".join(targets),
                        f"default_open={getattr(node, 'default_open', True)}",
                    ]
                )
                if hasattr(node, "multi_target"):
                    parts.append(f"multi_target={node.multi_target}")
                if hasattr(node, "fallback"):
                    fb = node.fallback
                    parts.append(f"fallback={fb if isinstance(fb, str) else 'END' if fb is not None else 'None'}")
                if hasattr(node, "when_true") and hasattr(node, "when_false"):
                    wt = node.when_true
                    wf = node.when_false
                    parts.append(f"when_true={wt if isinstance(wt, str) else 'END'}")
                    parts.append(f"when_false={wf if isinstance(wf, str) else 'END'}")
            if isinstance(node, GraphNode):
                parts.append(f"nested_struct={node.graph.structural_hash}")
                map_config = node.map_config
                if map_config is not None:
                    map_params, map_mode, error_handling = map_config
                    parts.append("map_over=" + ",".join(map_params))
                    parts.append(f"map_mode={map_mode}")
                    parts.append(f"map_error_handling={error_handling}")
                    clone_cfg = getattr(node, "_clone", False)
                    if isinstance(clone_cfg, list):
                        parts.append("map_clone=" + ",".join(clone_cfg))
                    else:
                        parts.append(f"map_clone={clone_cfg}")
            node_signatures.append("|".join(parts))

        edges = sorted((u, v, ",".join(data.get("value_names", []))) for u, v, data in self._nx_graph.edges(data=True))
        edge_str = str(edges)
        return hashlib.sha256(("|".join(node_signatures) + "|" + edge_str).encode()).hexdigest()

    def _shallow_copy(self) -> Graph:
        """Create a shallow copy of this graph.

        Preserves: name, strict_types, nodes, nx_graph, cached_hash
        Creates new: _bound dict (to allow independent modifications)
        Clears: cached inputs (depends on _bound, _selected, _entrypoints)
        """
        import copy

        new_graph = copy.copy(self)
        new_graph._bound = dict(self._bound)
        # Clear cached_property values that depend on _bound / _selected / _entrypoints
        new_graph.__dict__.pop("inputs", None)
        # _selected and _entrypoints are immutable tuples (or None), safe to share via copy.copy
        # All other attributes preserved: _strict_types, _nodes, _nx_graph, _cached_hash
        return new_graph

    def _validate(self) -> None:
        """Run all build-time validations."""
        # Note: Duplicate node names caught in _build_nodes_dict()
        # Note: Duplicate outputs caught in validate_output_conflicts()
        validate_graph(self._nodes, self._nx_graph, self.name, self._strict_types)
        if self.has_cycles and self._entrypoints is None:
            raise GraphConfigError(
                "Cyclic graphs require an explicit entrypoint.\n\n"
                "How to fix:\n"
                "  graph = graph.with_entrypoint('<node_name>')\n"
                "  # or Graph(..., entrypoint='<node_name>')"
            )

    def as_node(self, *, name: str | None = None) -> GraphNode:
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

    def __repr__(self) -> str:
        from hypergraph._utils import plural

        n_nodes = len(self._nodes)
        n_edges = self._nx_graph.number_of_edges()
        props = []
        if self.has_cycles:
            props.append("cycles")
        if self.has_async_nodes:
            props.append("async")
        if self.has_interrupts:
            props.append("interrupts")
        prop_str = f" | {', '.join(props)}" if props else " | no cycles"
        name = self.name or "unnamed"
        return f"Graph: {name} | {plural(n_nodes, 'node')} | {plural(n_edges, 'edge')}{prop_str}"

    def _repr_html_(self) -> str:
        from hypergraph._repr import _code, html_detail, html_panel, html_table, status_badge, theme_wrap, widget_state_key
        from hypergraph._utils import plural

        n_nodes = len(self._nodes)
        n_edges = self._nx_graph.number_of_edges()
        name = self.name or "unnamed"

        # Node table
        headers = ["Node", "Type", "Inputs", "Outputs"]
        rows = []
        for node_name, node in self._nodes.items():
            node_type = type(node).__name__
            inputs = ", ".join(_code(i) for i in node.inputs[:6])
            if len(node.inputs) > 6:
                inputs += f" (+{len(node.inputs) - 6})"
            outputs = ", ".join(_code(o) for o in node.outputs[:6])
            if len(node.outputs) > 6:
                outputs += f" (+{len(node.outputs) - 6})"
            rows.append([_code(node_name), node_type, inputs or "—", outputs or "—"])

        table_html = html_table(headers, rows)

        # Properties
        props = []
        if self.has_cycles:
            props.append(status_badge("active").replace("active", "cycles"))
        if self.has_async_nodes:
            props.append(status_badge("cached").replace("cached", "async"))
        if self.has_interrupts:
            props.append(status_badge("paused").replace("paused", "interrupts"))
        if not props:
            props.append(status_badge("completed").replace("completed", "DAG"))
        prop_html = " ".join(props)

        body = f"{prop_html} &nbsp; <b>{plural(n_edges, 'edge')}</b><br><br>{table_html}"

        # Collapsible visualization (if viz module is available)
        try:
            widget = self.visualize()
            if hasattr(widget, "_repr_html_"):
                viz_html = widget._repr_html_()
                body += html_detail("Show interactive graph", viz_html, state_key="interactive-graph")
        except (ImportError, Exception):
            pass

        return theme_wrap(
            html_panel(f"Graph: {name} ({plural(n_nodes, 'node')})", body),
            state_key=widget_state_key("graph", name, self.structural_hash),
        )

    def visualize(
        self,
        *,
        depth: int = 0,
        theme: str = "auto",
        show_types: bool = False,
        separate_outputs: bool = False,
        show_external_inputs: bool = False,
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
            show_external_inputs: Whether to show external INPUT/INPUT_GROUP nodes
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
            show_external_inputs=show_external_inputs,
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
        - Graph attributes include `input_spec`, `output_to_sources`, and
          configured execution entrypoints (if any)

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
            "entrypoints": {k: list(v) for k, v in input_spec.entrypoints.items()},
        }
        G.graph["output_to_sources"] = output_to_sources
        G.graph["configured_entrypoints"] = list(self._entrypoints or ())
        G.graph["shared"] = sorted(self._shared) if self._shared else []
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

    def _build_name_to_id_lookup(self, G: nx.DiGraph, parent_id: str | None) -> dict[str, str]:
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

    def _add_nested_edges(self, G: nx.DiGraph, node: HyperNode, parent_id: str) -> None:
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

    def debug_viz(self) -> VizDebugger:
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
