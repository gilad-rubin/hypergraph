"""Dataclasses for explicit visualization instructions.

These dataclasses define the contract between Python (renderer) and JavaScript (layout).
Python generates complete, unambiguous instructions; JavaScript just positions and renders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from hypergraph.viz._common import (
    build_expansion_state,
    build_param_to_consumer_map,
    enumerate_valid_expansion_states,
    expansion_state_to_key,
    get_expandable_nodes,
    get_nesting_depth,
    is_node_visible,
)

if TYPE_CHECKING:
    import networkx as nx


class NodeType(str, Enum):
    """Types of nodes in the visualization."""
    INPUT = "INPUT"           # Individual input parameter
    INPUT_GROUP = "INPUT_GROUP"  # Grouped inputs (optional)
    FUNCTION = "FUNCTION"     # Function node
    DATA = "DATA"             # Output data node
    CONTAINER = "CONTAINER"   # Expanded nested graph container


def _format_type(t: type | None) -> str | None:
    """Format a type annotation for display."""
    if t is None:
        return None
    if hasattr(t, "__name__"):
        return t.__name__
    return str(t).replace("typing.", "")


@dataclass
class VizNode:
    """A node in the visualization.

    Represents any visual element: inputs, functions, outputs, or containers.
    """
    id: str
    type: NodeType
    label: str
    parent_id: str | None = None  # ID of containing CONTAINER, or None for root
    is_expanded: bool = False     # Only relevant for CONTAINER nodes

    # INPUT node fields
    type_hint: str | None = None  # Type annotation (e.g., "str", "int")

    # INPUT_GROUP fields (for grouped inputs)
    params: list[str] | None = None       # Parameter names in group
    param_types: list[str] | None = None  # Type hints for each param

    # FUNCTION node fields
    outputs: list[str] | None = None  # Output names produced by this function

    # DATA node fields
    value_name: str | None = None  # The output value this represents

    # Visual properties
    theme: str = "dark"
    show_types: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to React Flow node format."""
        # Map our types to React Flow types
        rf_type_map = {
            NodeType.INPUT: "INPUT",
            NodeType.INPUT_GROUP: "INPUT_GROUP",
            NodeType.FUNCTION: "FUNCTION",
            NodeType.DATA: "DATA",
            NodeType.CONTAINER: "PIPELINE",  # React Flow uses PIPELINE for containers
        }

        data: dict[str, Any] = {
            "nodeType": rf_type_map[self.type],
            "label": self.label,
            "theme": self.theme,
            "showTypes": self.show_types,
        }

        # Add type-specific fields
        if self.type == NodeType.INPUT:
            if self.type_hint:
                data["typeHint"] = self.type_hint

        elif self.type == NodeType.INPUT_GROUP:
            data["params"] = self.params or []
            data["paramTypes"] = self.param_types or []

        elif self.type == NodeType.FUNCTION:
            data["outputs"] = self.outputs or []

        elif self.type == NodeType.DATA:
            if self.value_name:
                data["valueName"] = self.value_name

        elif self.type == NodeType.CONTAINER:
            data["isExpanded"] = self.is_expanded

        result: dict[str, Any] = {
            "id": self.id,
            "type": rf_type_map[self.type],
            "data": data,
            "position": {"x": 0, "y": 0},  # Layout will compute actual position
        }

        if self.parent_id:
            result["parentNode"] = self.parent_id
            result["extent"] = "parent"

        return result


@dataclass
class VizEdge:
    """An edge in the visualization.

    Edges are explicit: source and target are always the actual nodes
    that should be visually connected. No re-routing in JavaScript.
    """
    source: str  # Source node ID (always explicit - the actual visual source)
    target: str  # Target node ID (always explicit - the actual visual target)
    id: str | None = None  # Optional explicit ID, generated if not provided
    label: str | None = None  # Optional edge label

    def __post_init__(self):
        if self.id is None:
            self.id = f"e_{self.source}_to_{self.target}"

    def to_dict(self) -> dict[str, Any]:
        """Convert to React Flow edge format."""
        result: dict[str, Any] = {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "type": "customEdge",
        }
        if self.label:
            result["label"] = self.label
        return result


@dataclass
class VizInstructions:
    """Complete visualization instructions for JavaScript.

    This is the single source of truth that Python sends to JavaScript.
    JavaScript trusts these instructions completely - no interpretation needed.
    """
    nodes: list[VizNode] = field(default_factory=list)
    edges: list[VizEdge] = field(default_factory=list)

    # Pre-computed edges for all expansion state combinations
    # Key format: "node1:0,node2:1" (sorted by node id, 0=collapsed, 1=expanded)
    edges_by_state: dict[str, list[VizEdge]] = field(default_factory=dict)

    # List of node IDs that can be expanded/collapsed (CONTAINER nodes)
    expandable_nodes: list[str] = field(default_factory=list)

    # Metadata for debugging/testing
    depth: int = 0
    theme: str = "dark"
    show_types: bool = False
    debug_overlays: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to format expected by React Flow."""
        # Sort for deterministic ordering
        sorted_nodes = sorted(self.nodes, key=lambda n: n.id)
        sorted_edges = sorted(self.edges, key=lambda e: e.id or "")

        # Convert edges_by_state to dict format
        edges_by_state_dict = {
            key: [e.to_dict() for e in sorted(edges, key=lambda e: e.id or "")]
            for key, edges in self.edges_by_state.items()
        }

        return {
            "nodes": [n.to_dict() for n in sorted_nodes],
            "edges": [e.to_dict() for e in sorted_edges],
            "meta": {
                "depth": self.depth,
                "theme": self.theme,
                "showTypes": self.show_types,
                "debugOverlays": self.debug_overlays,
                "edgesByState": edges_by_state_dict,
                "expandableNodes": sorted(self.expandable_nodes),
            },
        }

    def add_node(self, node: VizNode) -> None:
        """Add a node to the instructions."""
        self.nodes.append(node)

    def add_edge(self, source: str, target: str, **kwargs) -> None:
        """Add an edge to the instructions."""
        self.edges.append(VizEdge(source=source, target=target, **kwargs))

    def get_node(self, node_id: str) -> VizNode | None:
        """Find a node by ID."""
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def get_edges_from(self, source_id: str) -> list[VizEdge]:
        """Get all edges originating from a node."""
        return [e for e in self.edges if e.source == source_id]

    def get_edges_to(self, target_id: str) -> list[VizEdge]:
        """Get all edges going to a node."""
        return [e for e in self.edges if e.target == target_id]


# =============================================================================
# Edge computation for VizInstructions
# =============================================================================


def _pick_primary_consumer(
    consumers: list[str],
    flat_graph: nx.DiGraph,
) -> str | None:
    """Pick the single deepest consumer from a list (for VizInstructions edges)."""
    if not consumers:
        return None
    return max(
        consumers,
        key=lambda node_id: (get_nesting_depth(node_id, flat_graph), node_id),
    )


def _compute_edges_for_state(
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
) -> list[VizEdge]:
    """Compute edges for a specific expansion state."""
    edges = []

    param_to_consumers = build_param_to_consumer_map(flat_graph, expansion_state)

    input_spec = flat_graph.graph.get("input_spec", {})
    required = input_spec.get("required", ())
    optional = input_spec.get("optional", ())
    external_inputs = set(required) | set(optional)

    # 1. Add edges from INPUT nodes to their actual consumers
    for param in external_inputs:
        input_node_id = f"input_{param}"
        actual_target = _pick_primary_consumer(
            param_to_consumers.get(param, []), flat_graph
        )

        if actual_target:
            edges.append(VizEdge(source=input_node_id, target=actual_target))

    # 2. Add edges between function nodes (from graph structure)
    for source, target, edge_data in flat_graph.edges(data=True):
        if not is_node_visible(source, flat_graph, expansion_state):
            continue
        if not is_node_visible(target, flat_graph, expansion_state):
            continue

        value_names = edge_data.get("value_names", [])

        if value_names:
            for value_name in value_names:
                edge_id = f"e_{source}_{value_name}_to_{target}"
                edges.append(VizEdge(source=source, target=target, id=edge_id))
        else:
            edge_id = f"e_{source}_to_{target}"
            edges.append(VizEdge(source=source, target=target, id=edge_id))

    return edges


# =============================================================================
# Builder: Convert NetworkX graph to VizInstructions
# =============================================================================
def build_instructions(
    flat_graph: nx.DiGraph,
    *,
    depth: int = 0,
    theme: str = "dark",
    show_types: bool = False,
) -> VizInstructions:
    """Build visualization instructions from a flattened NetworkX graph.

    This is the main entry point for generating explicit visualization instructions.
    All edge routing decisions are made here - JavaScript just renders.

    Pre-computes edges for ALL valid expansion state combinations, so JavaScript
    can simply select the right edge set when expansion state changes.
    """
    instructions = VizInstructions(depth=depth, theme=theme, show_types=show_types)

    input_spec = flat_graph.graph.get("input_spec", {})

    initial_expansion_state = build_expansion_state(flat_graph, depth)

    param_to_consumers = build_param_to_consumer_map(flat_graph, initial_expansion_state)

    # Create INPUT nodes for external inputs
    _add_input_nodes(instructions, flat_graph, input_spec, theme, show_types)

    # Create FUNCTION and CONTAINER nodes (include ALL nodes - JS handles visibility)
    _add_all_graph_nodes(instructions, flat_graph, initial_expansion_state, theme, show_types)

    # Create edges for initial state
    _add_edges(instructions, flat_graph, initial_expansion_state, param_to_consumers)

    # Pre-compute edges for ALL valid expansion state combinations
    expandable = get_expandable_nodes(flat_graph)
    instructions.expandable_nodes = expandable

    if expandable:
        valid_states = enumerate_valid_expansion_states(flat_graph, expandable)
        for state in valid_states:
            key = expansion_state_to_key(state)
            edges = _compute_edges_for_state(flat_graph, state)
            instructions.edges_by_state[key] = edges

    return instructions


def _add_input_nodes(
    instructions: VizInstructions,
    flat_graph: nx.DiGraph,
    input_spec: dict,
    theme: str,
    show_types: bool,
) -> None:
    """Add INPUT nodes for each external input parameter."""
    required = input_spec.get("required", ())
    optional = input_spec.get("optional", ())
    external_inputs = list(required) + list(optional)

    for param in external_inputs:
        param_type = None
        for _node_id, attrs in flat_graph.nodes(data=True):
            if param in attrs.get("inputs", ()):
                param_type = attrs.get("input_types", {}).get(param)
                if param_type is not None:
                    break

        instructions.add_node(VizNode(
            id=f"input_{param}",
            type=NodeType.INPUT,
            label=param,
            type_hint=_format_type(param_type),
            theme=theme,
            show_types=show_types,
        ))


def _add_all_graph_nodes(
    instructions: VizInstructions,
    flat_graph: nx.DiGraph,
    initial_expansion_state: dict[str, bool],
    theme: str,
    show_types: bool,
) -> None:
    """Add ALL FUNCTION and CONTAINER nodes from the graph."""
    for node_id, attrs in flat_graph.nodes(data=True):
        parent_id = attrs.get("parent")
        node_type = attrs.get("node_type", "FUNCTION")

        if node_type == "GRAPH":
            is_expanded = initial_expansion_state.get(node_id, False)
            instructions.add_node(VizNode(
                id=node_id,
                type=NodeType.CONTAINER,
                label=attrs.get("label", node_id),
                parent_id=parent_id,
                is_expanded=is_expanded,
                outputs=list(attrs.get("outputs", ())),
                theme=theme,
                show_types=show_types,
            ))
        else:
            instructions.add_node(VizNode(
                id=node_id,
                type=NodeType.FUNCTION,
                label=attrs.get("label", node_id),
                parent_id=parent_id,
                outputs=list(attrs.get("outputs", ())),
                theme=theme,
                show_types=show_types,
            ))


def _add_edges(
    instructions: VizInstructions,
    flat_graph: nx.DiGraph,
    expansion_state: dict[str, bool],
    param_to_consumers: dict[str, list[str]],
) -> None:
    """Add edges with explicit routing based on expansion state."""
    input_spec = flat_graph.graph.get("input_spec", {})
    required = input_spec.get("required", ())
    optional = input_spec.get("optional", ())
    external_inputs = set(required) | set(optional)

    # 1. Add edges from INPUT nodes to their actual consumers
    for param in external_inputs:
        input_node_id = f"input_{param}"
        actual_target = _pick_primary_consumer(
            param_to_consumers.get(param, []), flat_graph
        )

        if actual_target:
            instructions.add_edge(input_node_id, actual_target)

    # 2. Add edges between function nodes (from graph structure)
    for source, target, edge_data in flat_graph.edges(data=True):
        if not is_node_visible(source, flat_graph, expansion_state):
            continue
        if not is_node_visible(target, flat_graph, expansion_state):
            continue

        value_names = edge_data.get("value_names", [])

        if value_names:
            for value_name in value_names:
                edge_id = f"e_{source}_{value_name}_to_{target}"
                instructions.add_edge(source, target, id=edge_id)
        else:
            edge_id = f"e_{source}_to_{target}"
            instructions.add_edge(source, target, id=edge_id)
